#!/usr/bin/env python3
"""
stats.py
========
Compute the Phase-0 *stat panel* over the Parquet tables written by
``phh_to_parquet.py``, using DuckDB to query the columnar files directly
(no full load into RAM).

Every metric is a **binomial proportion** -- "of the decisions that reached
node X, what fraction were action Y" -- so each is reported as

    p = numerator / denominator   with a 95% Wilson score interval.

The Wilson interval is used instead of the normal approximation because the
headline node (folding to a river shove) is *rare*: a few thousand events out
of millions of hands, sometimes thin once sliced, and Wilson stays sensible at
small n and near p=0/1 where the textbook +/-1.96*sqrt(pq/n) does not.

The exact operational definition of every stat lives in ``codebook.md``.
This module is the executable copy of those definitions; if the two ever
disagree, the codebook is wrong and should be fixed to match the SQL.

Usage
-----
    # headline slice, pretty table
    python src/stats.py --out out --site PokerStars --stakes NL50 --seats 6

    # everything in the store, as JSON (for a notebook / further analysis)
    python src/stats.py --out out --json

It is also importable:

    from stats import connect, panel, where_clause
    con = connect("out")
    rows = panel(con, where_clause(site="PokerStars", stake="NL50", seats=6))
"""
from __future__ import annotations

import argparse
import json
import math
import os
from glob import glob

import duckdb

Z95 = 1.959963984540054  # standard normal quantile for a two-sided 95% interval


# --------------------------------------------------------------------------- #
# connection / filtering
# --------------------------------------------------------------------------- #
def connect(out_dir: str) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection exposing ``hands`` and ``actions``.

    ``out_dir`` can be either:
    - A parquet store directory (original mode) — creates in-memory views over
      the shard globs. First query is slow (~30 s for the full store) because
      DuckDB must open every file.
    - A pre-built .db file from ``build_db.py`` — opens it read-only. Queries
      run in milliseconds since the data is already in native DuckDB storage.
    """
    if os.path.isfile(out_dir):
        if not out_dir.endswith(".db"):
            raise ValueError(f"expected a .db file or a parquet store directory, got: {out_dir!r}")
        con = duckdb.connect(out_dir, read_only=True)
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        if "hands" not in tables or "actions" not in tables:
            raise FileNotFoundError(f"{out_dir} is missing 'hands' or 'actions' tables -- re-run build_db.py")
        return con

    hands_glob = os.path.join(out_dir, "hands", "*.parquet")
    actions_glob = os.path.join(out_dir, "actions", "*.parquet")
    if not glob(hands_glob):
        raise FileNotFoundError(f"no hand shards at {hands_glob} -- run phh_to_parquet.py first")
    if not glob(actions_glob):
        raise FileNotFoundError(f"no action shards at {actions_glob} -- run phh_to_parquet.py first")

    con = duckdb.connect()
    # union_by_name guards against a stray older shard with a different column
    # order; with the fixed schema it is a no-op but costs nothing.
    con.execute(
        f"CREATE VIEW hands  AS SELECT * FROM read_parquet('{hands_glob}',  union_by_name=true)"
    )
    con.execute(
        f"CREATE VIEW actions AS SELECT * FROM read_parquet('{actions_glob}', union_by_name=true)"
    )
    return con


def where_clause(site=None, stake=None, seats=None, alias="") -> str:
    """Build a SQL predicate string (without the leading WHERE/AND).

    ``alias`` is an optional table alias prefix, e.g. "a." so the same filter
    works whether applied to the actions or hands view."""
    p = f"{alias}." if alias else ""
    parts = ["1=1"]
    if site:
        parts.append(f"{p}site = '{site}'")
    if stake:
        parts.append(f"{p}stake = '{stake}'")
    if seats:
        parts.append(f"{p}n_seats = {int(seats)}")
    return " AND ".join(parts)


# --------------------------------------------------------------------------- #
# proportions
# --------------------------------------------------------------------------- #
def wilson(num: int, den: int, z: float = Z95) -> tuple[float, float, float]:
    """Return (p, lo, hi) -- point estimate and Wilson 95% CI. (nan,nan,nan) if den==0."""
    if den == 0:
        return (float("nan"), float("nan"), float("nan"))
    p = num / den
    z2 = z * z
    denom = 1.0 + z2 / den
    center = (p + z2 / (2 * den)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / den + z2 / (4 * den * den))
    return (p, max(0.0, center - half), min(1.0, center + half))


def _ratio(con, num_sql: str, den_sql: str, where: str) -> tuple[int, int]:
    """Run ``SUM(CASE WHEN <num>) , SUM(CASE WHEN <den>)`` over actions filtered by ``where``."""
    q = f"""
        SELECT
          COALESCE(SUM(CASE WHEN {num_sql} THEN 1 ELSE 0 END), 0) AS num,
          COALESCE(SUM(CASE WHEN {den_sql} THEN 1 ELSE 0 END), 0) AS den
        FROM actions
        WHERE {where}
    """
    num, den = con.execute(q).fetchone()
    return int(num), int(den)


def _stat(con, name: str, num_sql: str, den_sql: str, where: str, note: str = "") -> dict:
    num, den = _ratio(con, num_sql, den_sql, where)
    p, lo, hi = wilson(num, den)
    return {"stat": name, "num": num, "den": den, "p": p, "lo": lo, "hi": hi, "note": note}


# --------------------------------------------------------------------------- #
# the panel
# --------------------------------------------------------------------------- #
def panel(con, where: str) -> list[dict]:
    """Compute the full Phase-0 stat panel for the action rows matching ``where``."""
    rows: list[dict] = []

    # ---- HEADLINE: fold vs a river all-in ---------------------------------- #
    # Universe: a voluntary decision on the river while facing a live all-in bet.
    river_jam = f"({where}) AND street = 3 AND facing_all_in"
    # Variant A (headline): folds / (folds + calls + raises) -- "fold vs jam".
    rows.append(_stat(
        con, "fold_vs_river_jam",
        num_sql="action_type = 'fold'",
        den_sql="action_type IN ('fold','call','raise')",
        where=river_jam,
        note="P(fold | facing a river all-in). Headline metric.",
    ))
    # Variant B: folds / (folds + calls), dropping the rare raise-over-shove
    # case. Should be within a hair of Variant A; the gap is a sanity check.
    rows.append(_stat(
        con, "fold_vs_river_jam_excl_raise",
        num_sql="action_type = 'fold'",
        den_sql="action_type IN ('fold','call')",
        where=river_jam,
        note="Same node, denominator excludes raise-over-shove. Cross-check on Variant A.",
    ))

    # ---- fold-to-river-bet, bucketed by sizing ----------------------------- #
    # Universe: facing ANY bet/raise on the river. Buckets are mutually
    # exclusive and exhaustive; the 'all_in' bucket is the same events as the
    # headline node, so its fold rate must equal fold_vs_river_jam (a built-in
    # consistency check across the two query paths).
    #
    # facing_pct_pot was NULL in stores built before an earlier int() parse fix.
    # For those stores, the COALESCE below derives a proxy from PokerKit's
    # to_call and node_pot, which is exact for first-bet situations (~88% of
    # NULL rows) and approximate for raise-over-bet situations (~12%).
    river_faced = f"({where}) AND street = 3 AND faced_bet"
    eff_pct = (
        "COALESCE(facing_pct_pot, "
        "CASE WHEN to_call > 0 AND node_pot - to_call > 0 "
        "THEN to_call / (node_pot - to_call) END)"
    )
    buckets = [
        ("fold_to_river_bet_le33",    f"NOT facing_all_in AND ({eff_pct}) <= 0.33"),
        ("fold_to_river_bet_33_66",   f"NOT facing_all_in AND ({eff_pct}) > 0.33 AND ({eff_pct}) <= 0.66"),
        ("fold_to_river_bet_66_100",  f"NOT facing_all_in AND ({eff_pct}) > 0.66 AND ({eff_pct}) <= 1.0"),
        ("fold_to_river_bet_100_200", f"NOT facing_all_in AND ({eff_pct}) > 1.0 AND ({eff_pct}) <= 2.0"),
        ("fold_to_river_bet_gt200",   f"NOT facing_all_in AND ({eff_pct}) > 2.0"),
        ("fold_to_river_bet_allin",   "facing_all_in"),
    ]
    for name, cond in buckets:
        rows.append(_stat(
            con, name,
            num_sql="action_type = 'fold'",
            den_sql="action_type IN ('fold','call','raise')",
            where=f"{river_faced} AND ({cond})",
            note="P(fold | facing a river bet in this size bucket).",
        ))

    # ---- river aggression frequency ---------------------------------------- #
    # AFq form: aggressive actions / all actions, restricted to the river.
    rows.append(_stat(
        con, "river_aggression_freq",
        num_sql="action_type IN ('bet','raise')",
        den_sql="action_type IN ('bet','raise','call','fold','check')",
        where=f"({where}) AND street = 3",
        note="(bet+raise)/(all river actions). Frequency form of AF.",
    ))

    # ---- preflop 3-bet ------------------------------------------------------ #
    # Opportunity: a preflop decision while facing exactly one standing raise
    # (the open). 3-bet = re-raising it. n_agg_before_on_street == 1 means one
    # raise already stands when the player acts.
    threebet_opp = f"({where}) AND street = 0 AND faced_bet AND n_agg_before_on_street = 1"
    rows.append(_stat(
        con, "preflop_3bet",
        num_sql="action_type = 'raise'",
        den_sql="action_type IN ('fold','call','raise')",
        where=threebet_opp,
        note="P(re-raise | facing a single preflop open). Standard 3-bet%.",
    ))

    # ---- flop c-bet --------------------------------------------------------- #
    # Opportunity: the preflop aggressor's first action on the flop, with no
    # bet yet in front of them (they may c-bet or check).
    cbet_opp = f"({where}) AND street = 1 AND is_pf_aggressor AND n_agg_before_on_street = 0"
    rows.append(_stat(
        con, "flop_cbet",
        num_sql="is_cbet",
        den_sql="action_type IN ('bet','check')",
        where=cbet_opp,
        note="P(continuation bet | PFR first to act on flop).",
    ))

    # ---- fold to flop c-bet ------------------------------------------------- #
    # Universe: facing the PFR's flop continuation bet, before anyone raises it.
    foldcbet = f"({where}) AND street = 1 AND facing_cbet"
    rows.append(_stat(
        con, "fold_to_flop_cbet",
        num_sql="action_type = 'fold'",
        den_sql="action_type IN ('fold','call','raise')",
        where=foldcbet,
        note="P(fold | facing a flop c-bet).",
    ))

    return rows


def wtsd(con, where_hands: str) -> dict:
    """Went-to-showdown rate at the HAND level: hands reaching showdown divided
    by hands that saw a flop. (A per-player WTSD needs per-seat reach flags,
    which Phase 0 does not track; this hand-level proxy is documented as such
    in the codebook.)"""
    q = f"""
        SELECT
          COALESCE(SUM(CASE WHEN went_to_showdown THEN 1 ELSE 0 END), 0) AS num,
          COALESCE(SUM(CASE WHEN saw_flop        THEN 1 ELSE 0 END), 0) AS den
        FROM hands
        WHERE {where_hands}
    """
    num, den = con.execute(q).fetchone()
    p, lo, hi = wilson(int(num), int(den))
    return {"stat": "wtsd_hand_level", "num": int(num), "den": int(den),
            "p": p, "lo": lo, "hi": hi,
            "note": "Hands to showdown / hands that saw flop (hand-level, not per-player)."}


def full_panel(con, site=None, stake=None, seats=None) -> list[dict]:
    """Panel (action-based) + the hand-level WTSD row, for one slice."""
    rows = panel(con, where_clause(site, stake, seats, alias=""))
    rows.append(wtsd(con, where_clause(site, stake, seats, alias="")))
    return rows


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def _fmt_pct(x: float) -> str:
    return "   n/a" if x != x else f"{100 * x:5.1f}%"  # x!=x catches NaN


def print_panel(rows: list[dict], title: str = "") -> None:
    if title:
        print(f"\n{title}")
        print("=" * len(title))
    name_w = max(len(r["stat"]) for r in rows)
    print(f"{'stat'.ljust(name_w)}   {'p':>6}   {'95% CI':>15}   {'num':>9} / {'den':<9}")
    print("-" * (name_w + 52))
    for r in rows:
        ci = f"[{_fmt_pct(r['lo']).strip()}, {_fmt_pct(r['hi']).strip()}]"
        print(f"{r['stat'].ljust(name_w)}   {_fmt_pct(r['p'])}   {ci:>15}   "
              f"{r['num']:>9,} / {r['den']:<9,}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase-0 poker stat panel over Parquet (DuckDB).")
    ap.add_argument("--out", required=True, help="parquet store dir (the --out of phh_to_parquet.py)")
    ap.add_argument("--site", default=None)
    ap.add_argument("--stakes", dest="stake", default=None, help="single stake label, e.g. NL50")
    ap.add_argument("--seats", type=int, default=None)
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    a = ap.parse_args(argv)

    con = connect(a.out)
    rows = full_panel(con, site=a.site, stake=a.stake, seats=a.seats)

    if a.json:
        meta = {"site": a.site, "stake": a.stake, "seats": a.seats}
        print(json.dumps({"slice": meta, "panel": rows}, indent=2))
    else:
        slug = " / ".join(str(x) for x in [a.site, a.stake,
                                           f"{a.seats}-max" if a.seats else None] if x)
        print_panel(rows, title=f"Stat panel: {slug or 'ALL hands'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
