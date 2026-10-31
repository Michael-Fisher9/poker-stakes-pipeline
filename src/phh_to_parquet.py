#!/usr/bin/env python3
"""
phh_to_parquet.py
=================
Walk a directory of ``.phh`` files (e.g. the unzipped Zenodo dataset), replay
every hand through PokerKit, and write two columnar tables as Parquet shards:

    OUT/hands/<shard>.parquet      one row per hand
    OUT/actions/<shard>.parquet    one row per voluntary betting decision

Design choices (see README + codebook):
  * Streaming + sharded: one shard per input file, written by a pool of worker
    processes (one per core). Memory stays bounded by the largest single file.
  * Stake comes from the PHH blinds (authoritative); site from the file path.
  * Fixed pyarrow schema so every shard is identical -> DuckDB reads the glob
    with zero schema-union surprises.
  * A hand that won't replay is skipped, but never silently: every one is written to
    OUT/qa/parse_errors.csv with its file, index and error, and the run exits non-zero
    if more than --max-error-rate of hands failed. (A silent skip once dropped every
    low-stakes hand with a bet in it; see changelog 2026-10.)

Examples
--------
  # everything under data/, all sites/stakes, all cores
  python src/phh_to_parquet.py --data data --out out

  # just PokerStars NL50 with six players dealt in, 8 workers
  python src/phh_to_parquet.py --data data --out out \
      --site PokerStars --stakes NL50 --seats 6 --workers 8

  # quick smoke test on the bundled fixture
  python src/phh_to_parquet.py --data tests --out out_test --glob 'sample.phh'
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from glob import glob

import pyarrow as pa
import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract import extract_hand  # noqa: E402

warnings.filterwarnings("ignore")  # silence PokerKit '???? not recommended' deal warnings

# Known site tokens -> canonical name. Matched (case-insensitive) against the
# file path so site survives even though it is not in the PHH body.
SITE_TOKENS = {
    # Full-name tokens (authoritative for structured paths like pokerstars/NL50/...)
    "pokerstars": "PokerStars",
    "partypoker": "PartyPoker",
    "fulltilt": "FullTilt",
    "full_tilt": "FullTilt",
    "ipoker": "iPoker",
    "ongame": "Ongame",
    "absolute": "AbsolutePoker",
    # Short-prefix tokens for the HandHQ/Zenodo flat-file naming convention
    # (files named "ps NLH handhq_N.phh", "ipn NLH handhq_N.phh", etc.)
    "party": "PartyPoker",
    "ftp": "FullTilt",
    "ps": "PokerStars",
    "pty": "PartyPoker",
    "ipn": "iPoker",
    "ong": "Ongame",
    "abs": "AbsolutePoker",
}

# ----------------------------------------------------------------------------- schemas
HANDS_SCHEMA = pa.schema([
    ("hand_id", pa.string()), ("site", pa.string()), ("stake_bb", pa.int64()),
    ("stake", pa.string()), ("n_seats", pa.int32()), ("variant", pa.string()),
    ("day", pa.int32()), ("month", pa.int32()), ("year", pa.int32()),
    ("board", pa.string()), ("pot_peak", pa.float64()), ("max_street", pa.int32()),
    ("saw_flop", pa.bool_()), ("saw_turn", pa.bool_()), ("saw_river", pa.bool_()),
    ("went_to_showdown", pa.bool_()), ("source_file", pa.string()), ("source_idx", pa.int32()),
])

ACTIONS_SCHEMA = pa.schema([
    ("hand_id", pa.string()), ("site", pa.string()), ("stake_bb", pa.int64()),
    ("stake", pa.string()), ("n_seats", pa.int32()), ("action_idx", pa.int32()),
    ("street", pa.int32()), ("street_name", pa.string()), ("seat", pa.int32()),
    ("position", pa.string()), ("action_type", pa.string()), ("faced_bet", pa.bool_()),
    ("to_call", pa.float64()), ("node_pot", pa.float64()), ("pot_odds", pa.float64()),
    ("facing_pct_pot", pa.float64()), ("facing_all_in", pa.bool_()),
    ("call_is_all_in", pa.bool_()), ("bet_to", pa.float64()), ("bet_added", pa.float64()),
    ("bet_pct_pot", pa.float64()), ("actor_all_in_after", pa.bool_()),
    ("n_live", pa.int32()), ("preflop_n_raises", pa.int32()),
    ("is_pf_aggressor", pa.bool_()), ("is_cbet", pa.bool_()),
    ("facing_cbet", pa.bool_()), ("n_agg_before_on_street", pa.int32()),
])


def site_from_path(path: str, override: str | None) -> str:
    """Site from the path *under the data folder*. Short tokens like 'ps' would match
    almost anything in an absolute path, so callers pass the relative one."""
    if override:
        return override
    low = path.lower()
    for tok, name in SITE_TOKENS.items():
        if tok in low:
            return name
    return "UNKNOWN"


def _coerce_int(v):
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _coerce_float(v):
    # PokerKit returns Decimal/Fraction for fractional-dollar stakes; pyarrow
    # will not auto-convert those into a float64 column, so cast explicitly.
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _table(rows: list[dict], schema: pa.Schema) -> pa.Table:
    cols = {}
    for f in schema:
        vals = [r.get(f.name) for r in rows]
        if pa.types.is_integer(f.type):
            vals = [_coerce_int(v) for v in vals]
        elif pa.types.is_floating(f.type):
            vals = [_coerce_float(v) for v in vals]
        cols[f.name] = pa.array(vals, type=f.type)
    return pa.table(cols, schema=schema)


def process_file(args) -> dict:
    """Worker: parse one .phh file, write its two shards, return a small report."""
    from pokerkit.notation import HandHistory  # imported in worker

    path, data_root, out_dir, site_override, stakes, seats, limit = args
    # Use path relative to data_root as the source_file key so that
    # files with the same basename in different subdirectories get unique
    # hand_ids. Fall back to absolute path if relpath fails.
    try:
        rel_path = os.path.relpath(path, data_root)
    except ValueError:
        rel_path = os.path.abspath(path)
    shard = os.path.splitext(os.path.basename(path))[0] + "__" + str(abs(hash(rel_path)) % (10 ** 8))

    site = site_from_path(rel_path, site_override)
    hand_rows, action_rows, errors = [], [], []
    n_hands = n_err = 0
    try:
        with open(path, "rb") as f:
            hhs = HandHistory.load_all(f)
            for idx, hh in enumerate(hhs):
                if limit and n_hands >= limit:
                    break
                try:
                    hr, ar = extract_hand(hh, rel_path, idx, site)
                except Exception as e:  # noqa: BLE001 - recorded, and the run fails if there are many
                    n_err += 1
                    errors.append({"file": rel_path, "idx": idx, "error": f"{type(e).__name__}: {str(e)[:200]}"})
                    continue
                if stakes and hr["stake"] not in stakes:
                    continue
                if seats and hr["n_seats"] != seats:
                    continue
                hand_rows.append(hr)
                action_rows.extend(ar)
                n_hands += 1
    except Exception as e:
        return {"path": path, "ok": False, "error": str(e), "hands": 0, "actions": 0, "parse_errors": n_err,
                "errors": errors, "site": site}

    if hand_rows:
        os.makedirs(os.path.join(out_dir, "hands"), exist_ok=True)
        os.makedirs(os.path.join(out_dir, "actions"), exist_ok=True)
        pq.write_table(_table(hand_rows, HANDS_SCHEMA),
                       os.path.join(out_dir, "hands", shard + ".parquet"), compression="zstd")
        pq.write_table(_table(action_rows, ACTIONS_SCHEMA),
                       os.path.join(out_dir, "actions", shard + ".parquet"), compression="zstd")
    return {"path": path, "ok": True, "hands": len(hand_rows), "actions": len(action_rows),
            "parse_errors": n_err, "errors": errors, "site": site}


def main():
    ap = argparse.ArgumentParser(description="PHH -> Parquet ETL")
    ap.add_argument("--data", required=True, help="root dir of .phh files")
    ap.add_argument("--out", required=True, help="output dir for parquet shards")
    ap.add_argument("--glob", default="**/*.phh", help="glob under --data (default **/*.phh)")
    ap.add_argument("--site", default=None, help="force site label (else inferred from path)")
    ap.add_argument("--stakes", default=None, help="comma list e.g. NL50,NL100 (else all)")
    ap.add_argument("--seats", type=int, default=None, help="keep only hands with N seats")
    ap.add_argument("--workers", type=int, default=os.cpu_count(), help="parallel workers")
    ap.add_argument("--limit", type=int, default=None, help="max hands per file (debug)")
    ap.add_argument("--max-error-rate", type=float, default=0.005,
                    help="fail the run if more than this share of hands won't replay (default 0.5%%)")
    a = ap.parse_args()

    files = sorted(glob(os.path.join(a.data, a.glob), recursive=True))
    if not files:
        print(f"No files matched {os.path.join(a.data, a.glob)!r}", file=sys.stderr)
        sys.exit(1)
    stakes = set(s.strip() for s in a.stakes.split(",")) if a.stakes else None
    print(f"{len(files)} files | workers={a.workers} | site={a.site or 'auto'} "
          f"| stakes={stakes or 'all'} | seats={a.seats or 'all'}")

    data_root = os.path.abspath(a.data)
    tasks = [(p, data_root, a.out, a.site, stakes, a.seats, a.limit) for p in files]
    t0 = time.time()
    tot_h = tot_a = tot_e = done = 0
    sites_seen: dict[str, int] = {}
    all_errors: list[dict] = []
    bad_files: list[str] = []
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(process_file, t) for t in tasks]
        for fut in as_completed(futs):
            r = fut.result()
            done += 1
            all_errors += r["errors"]
            if not r["ok"]:
                print(f"  ! {os.path.basename(r['path'])}: {r['error']}", file=sys.stderr)
                bad_files.append(r["path"])
                continue
            tot_h += r["hands"]; tot_a += r["actions"]; tot_e += r["parse_errors"]
            sites_seen[r["site"]] = sites_seen.get(r["site"], 0) + r["hands"]
            if done % 50 == 0 or done == len(files):
                el = time.time() - t0
                print(f"  {done}/{len(files)} files | {tot_h:,} hands | {tot_a:,} actions "
                      f"| {tot_e} parse-errors | {tot_h/el:,.0f} hands/s")
    print(f"\nDONE in {time.time()-t0:.1f}s -> {a.out}/hands, {a.out}/actions")
    os.makedirs(os.path.join(a.out, "qa"), exist_ok=True)
    with open(os.path.join(a.out, "qa", "parse_errors.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["file", "idx", "error"])
        w.writeheader()
        w.writerows(all_errors)
    rate = tot_e / (tot_h + tot_e) if tot_h + tot_e else 0.0
    print(f"parse errors: {tot_e:,} of {tot_h + tot_e:,} hands ({rate:.2%}) -> {a.out}/qa/parse_errors.csv")
    print("hands by inferred site:", {k: f"{v:,}" for k, v in sorted(sites_seen.items())})
    if "UNKNOWN" in sites_seen:
        print("  NOTE: some files' site could not be inferred from path -- pass --site "
              "or check SITE_TOKENS against your directory layout.")
    if bad_files or rate > a.max_error_rate:
        print(f"FAILED: {len(bad_files)} unreadable files, {rate:.2%} of hands failed "
              f"(limit {a.max_error_rate:.2%}). Read qa/parse_errors.csv before using any output.", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
