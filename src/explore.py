#!/usr/bin/env python3
"""
explore.py
==========
Interactive-style data exploration of the pokermeta DuckDB store.
Run:  python src/explore.py --out out/pokermeta.db
"""
import argparse
import duckdb
import pandas as pd

pd.set_option("display.max_columns", 40)
pd.set_option("display.width", 120)
pd.set_option("display.float_format", "{:.4f}".format)


def section(title):
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out/pokermeta.db")
    a = ap.parse_args()

    con = duckdb.connect(a.out, read_only=True)

    # ------------------------------------------------------------------ #
    section("TABLE SIZES")
    # ------------------------------------------------------------------ #
    for tbl in ["hands", "actions"]:
        n = con.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        cols = len(con.execute(f"SELECT * FROM {tbl} LIMIT 0").df().columns)
        print(f"  {tbl:<10}  {n:>15,} rows   {cols} columns")

    # ------------------------------------------------------------------ #
    section("HANDS — first 5 rows")
    # ------------------------------------------------------------------ #
    print(con.execute("SELECT * FROM hands LIMIT 5").df().to_string())

    # ------------------------------------------------------------------ #
    section("HANDS — column types")
    # ------------------------------------------------------------------ #
    print(con.execute("SELECT * FROM hands LIMIT 0").df().dtypes.to_string())

    # ------------------------------------------------------------------ #
    section("HANDS — describe (numeric summary)")
    # ------------------------------------------------------------------ #
    # Pull a sample — describe on 22M rows is slow; 100K is representative
    h_sample = con.execute("SELECT * FROM hands USING SAMPLE 100000").df()
    print(h_sample.describe().to_string())

    # ------------------------------------------------------------------ #
    section("HANDS — null counts")
    # ------------------------------------------------------------------ #
    nulls = h_sample.isnull().sum()
    print(nulls[nulls > 0].to_string() if nulls.any() else "  No nulls found in sample.")

    # ------------------------------------------------------------------ #
    section("HANDS — categorical breakdowns")
    # ------------------------------------------------------------------ #
    print("\nsite:")
    print(con.execute("SELECT site, COUNT(*) n FROM hands GROUP BY 1 ORDER BY 2 DESC").df().to_string(index=False))
    print("\nstake (top 15 by count):")
    print(con.execute("SELECT stake, stake_bb, COUNT(*) n FROM hands GROUP BY 1,2 ORDER BY 3 DESC LIMIT 15").df().to_string(index=False))
    print("\nn_seats:")
    print(con.execute("SELECT n_seats, COUNT(*) n FROM hands GROUP BY 1 ORDER BY 1").df().to_string(index=False))
    print("\nvariant:")
    print(con.execute("SELECT variant, COUNT(*) n FROM hands GROUP BY 1 ORDER BY 2 DESC").df().to_string(index=False))

    # ------------------------------------------------------------------ #
    section("HANDS — date coverage")
    # ------------------------------------------------------------------ #
    print(con.execute("""
        SELECT year, month, MIN(day) first_day, MAX(day) last_day, COUNT(*) n_hands
        FROM hands GROUP BY 1,2 ORDER BY 1,2
    """).df().to_string(index=False))

    # ------------------------------------------------------------------ #
    section("ACTIONS — first 5 rows")
    # ------------------------------------------------------------------ #
    print(con.execute("SELECT * FROM actions LIMIT 5").df().to_string())

    # ------------------------------------------------------------------ #
    section("ACTIONS — describe on 50K random sample")
    # ------------------------------------------------------------------ #
    a_sample = con.execute("SELECT * FROM actions USING SAMPLE 50000").df()
    print(a_sample.describe().to_string())

    # ------------------------------------------------------------------ #
    section("ACTIONS — null counts (50K sample)")
    # ------------------------------------------------------------------ #
    nulls_a = a_sample.isnull().sum()
    print("Columns with nulls:")
    print(nulls_a[nulls_a > 0].to_string())
    print("\nExpected nulls:")
    print("  facing_pct_pot  — only set when player is facing a bet")
    print("  bet_pct_pot     — only set when player is making an aggressive action")
    print("  bet_to/bet_added— only set on bet/raise rows")
    print("  preflop_n_raises— only set on preflop rows")
    print("  pot_odds        — only set when faced_bet is True")

    # ------------------------------------------------------------------ #
    section("ACTIONS — categorical breakdowns (50K sample)")
    # ------------------------------------------------------------------ #
    print("\naction_type:")
    print(a_sample["action_type"].value_counts().to_string())
    print("\nstreet_name:")
    print(a_sample["street_name"].value_counts().to_string())
    print("\nposition:")
    print(a_sample["position"].value_counts().sort_index().to_string())
    print("\nsite:")
    print(a_sample["site"].value_counts().to_string())

    # ------------------------------------------------------------------ #
    section("ACTIONS — key numeric distributions (50K sample)")
    # ------------------------------------------------------------------ #
    for col in ["pot_odds", "facing_pct_pot", "bet_pct_pot", "node_pot"]:
        s = a_sample[col].dropna()
        if len(s):
            print(f"\n{col}  (n={len(s):,} non-null):")
            print(f"  min={s.min():.4f}  p25={s.quantile(.25):.4f}  "
                  f"median={s.median():.4f}  p75={s.quantile(.75):.4f}  "
                  f"p99={s.quantile(.99):.4f}  max={s.max():.4f}")

    # ------------------------------------------------------------------ #
    section("ACTIONS — internal consistency checks")
    # ------------------------------------------------------------------ #
    checks = [
        ("facing_all_in only when faced_bet",
         len(a_sample[(a_sample.facing_all_in) & (~a_sample.faced_bet)]) == 0),
        ("call/fold only when faced_bet",
         len(a_sample[a_sample.action_type.isin(["call","fold"]) & ~a_sample.faced_bet]) < 50),
        ("pot_odds in [0,1]",
         a_sample["pot_odds"].dropna().between(0, 1).all()),
        ("is_cbet only on postflop bets by PFR",
         a_sample[a_sample.is_cbet][["street","action_type","is_pf_aggressor"]]
             .apply(lambda r: r.street >= 1 and r.action_type == "bet" and r.is_pf_aggressor, axis=1).all()),
        ("facing_cbet only on postflop faced-bet rows",
         a_sample[a_sample.facing_cbet][["street","faced_bet"]]
             .apply(lambda r: r.street >= 1 and r.faced_bet, axis=1).all()),
    ]
    for name, result in checks:
        print(f"  {'PASS' if result else 'FAIL'}  {name}")

    # ------------------------------------------------------------------ #
    section("HEADLINE SANITY CHECK — fold vs river jam (PS NL50 6-max)")
    # ------------------------------------------------------------------ #
    jam = con.execute("""
        SELECT action_type, COUNT(*) n
        FROM actions
        WHERE site='PokerStars' AND stake='NL50' AND n_seats=6
          AND street=3 AND facing_all_in
        GROUP BY 1
    """).df()
    print(jam.to_string(index=False))
    folds = jam.loc[jam.action_type == "fold", "n"].values[0]
    total = jam["n"].sum()
    print(f"\n  Fold vs river jam = {folds/total:.1%}  (n={total:,})")
    print(f"  Expected: ~49.5%, n=7,930")

    con.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
