#!/usr/bin/env python3
"""
build_db.py
===========
One-time conversion: read all Parquet shards in a store directory and
materialise them as native DuckDB tables in a single .db file.

After this runs, all scripts that accept --out can be given the .db path
directly (e.g. ``--out out/pokermeta.db``) instead of the parquet directory.
Queries go from ~30 s (scanning 23 K shard files) to milliseconds.

Usage
-----
    python src/build_db.py --out out --db out/pokermeta.db
"""
from __future__ import annotations

import argparse
import os
import time
from glob import glob

import duckdb


def build(out_dir: str, db_path: str) -> None:
    hands_glob = os.path.join(out_dir, "hands", "*.parquet")
    actions_glob = os.path.join(out_dir, "actions", "*.parquet")

    if not glob(hands_glob):
        raise FileNotFoundError(f"no hand shards at {hands_glob} — run phh_to_parquet.py first")
    if not glob(actions_glob):
        raise FileNotFoundError(f"no action shards at {actions_glob} — run phh_to_parquet.py first")

    if os.path.exists(db_path):
        os.remove(db_path)

    print(f"Building {db_path} from {out_dir} ...")
    t0 = time.time()

    con = duckdb.connect(db_path)
    con.execute(f"""
        CREATE TABLE hands AS
        SELECT * FROM read_parquet('{hands_glob}', union_by_name=true)
    """)
    n_hands = con.execute("SELECT COUNT(*) FROM hands").fetchone()[0]
    print(f"  hands  : {n_hands:>14,}")

    con.execute(f"""
        CREATE TABLE actions AS
        SELECT * FROM read_parquet('{actions_glob}', union_by_name=true)
    """)
    n_actions = con.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    print(f"  actions: {n_actions:>14,}")

    con.close()
    size_mb = os.path.getsize(db_path) / (1024 * 1024)
    print(f"\nDone in {time.time() - t0:.1f}s  ->  {db_path}  ({size_mb:,.0f} MB)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Materialise parquet shards into a DuckDB file.")
    ap.add_argument("--out", required=True, help="parquet store directory")
    ap.add_argument("--db", required=True, help="output .db file path")
    a = ap.parse_args()
    build(a.out, a.db)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
