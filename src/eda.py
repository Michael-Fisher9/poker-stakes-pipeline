#!/usr/bin/env python3
"""
eda.py
======
Phase-0 exploratory analysis over the Parquet store. Produces the deliverable
the plan asks for:

  * a sanity dump of every (site, stake, seats) cell actually present -- so you
    can immediately confirm the file-path -> site parsing did the right thing
    BEFORE trusting any number;
  * the headline sentence -- "July 2009 <site> <stake> <seats>-max, fold vs a
    river shove = X% (95% CI ..., n=...)";
  * the cross-stake gradient of that same metric (does over/under-folding move
    with stake?);
  * the cross-site comparison at the fixed stake;
  * the decision funnel (hands -> saw flop -> saw river -> faced a river bet ->
    faced a river all-in), which is what governs whether a future purchased
    slice is large enough to split by line;
  * a chart (PNG) of the cross-stake gradient with Wilson error bars.

Run after phh_to_parquet.py:

    python src/eda.py --out out --site PokerStars --stake NL50 --seats 6 \
        --chart out/eda_river_jam_by_stake.png
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stats import connect, full_panel, wilson, where_clause  # noqa: E402


# --------------------------------------------------------------------------- #
# small query helpers
# --------------------------------------------------------------------------- #
def cells(con) -> list[tuple]:
    """Every (site, stake, n_seats, n_hands) present, busiest first."""
    return con.execute("""
        SELECT site, stake, n_seats, COUNT(*) AS n
        FROM hands
        GROUP BY 1, 2, 3
        ORDER BY n DESC
    """).fetchall()


def stakes_for(con, site, seats) -> list[str]:
    """Stake labels present for a site+seats, ordered by big-blind size."""
    rows = con.execute(f"""
        SELECT stake FROM hands
        WHERE {where_clause(site=site, seats=seats)}
        GROUP BY stake ORDER BY MAX(stake_bb)
    """).fetchall()
    return [r[0] for r in rows]


def sites_for(con, stake, seats) -> list[str]:
    rows = con.execute(f"""
        SELECT site, COUNT(*) n FROM hands
        WHERE {where_clause(stake=stake, seats=seats)}
        GROUP BY site ORDER BY n DESC
    """).fetchall()
    return [r[0] for r in rows]


def river_jam_rate(con, site=None, stake=None, seats=None) -> tuple[int, int, float, float, float]:
    """(num_folds, n_decisions, p, lo, hi) for fold-vs-river-jam on one slice."""
    w = where_clause(site=site, stake=stake, seats=seats)
    num, den = con.execute(f"""
        SELECT
          COALESCE(SUM(CASE WHEN action_type='fold' THEN 1 ELSE 0 END),0),
          COALESCE(SUM(CASE WHEN action_type IN ('fold','call','raise') THEN 1 ELSE 0 END),0)
        FROM actions
        WHERE {w} AND street=3 AND facing_all_in
    """).fetchone()
    p, lo, hi = wilson(int(num), int(den))
    return int(num), int(den), p, lo, hi


def funnel(con, site=None, stake=None, seats=None) -> dict:
    """Decision-count funnel for sample-size budgeting."""
    wh = where_clause(site=site, stake=stake, seats=seats)         # hands view
    wa = where_clause(site=site, stake=stake, seats=seats)         # actions view
    n_hands, n_flop, n_river = con.execute(f"""
        SELECT COUNT(*),
               SUM(CASE WHEN saw_flop  THEN 1 ELSE 0 END),
               SUM(CASE WHEN saw_river THEN 1 ELSE 0 END)
        FROM hands WHERE {wh}
    """).fetchone()
    faced_river, faced_jam = con.execute(f"""
        SELECT
          SUM(CASE WHEN street=3 AND faced_bet THEN 1 ELSE 0 END),
          SUM(CASE WHEN street=3 AND facing_all_in THEN 1 ELSE 0 END)
        FROM actions WHERE {wa}
    """).fetchone()
    return {
        "hands": int(n_hands or 0),
        "saw_flop": int(n_flop or 0),
        "saw_river": int(n_river or 0),
        "river_bet_decisions": int(faced_river or 0),
        "river_jam_decisions": int(faced_jam or 0),
    }


# --------------------------------------------------------------------------- #
# chart
# --------------------------------------------------------------------------- #
def chart_gradient(labels, ps, los, his, ns, title, path) -> bool:
    """Bar chart of fold-vs-river-jam by stake with asymmetric Wilson error bars."""
    pts = [(l, p, lo, hi, n) for l, p, lo, hi, n in zip(labels, ps, los, his, ns)
           if p == p and n > 0]  # drop empty / NaN cells
    if not pts:
        return False
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels, ps, los, his, ns = map(list, zip(*pts))
    x = range(len(labels))
    # asymmetric error deltas, clamped at 0 so float noise / Wilson recentring
    # at p=0 or p=1 can never produce a (rejected) negative yerr.
    lower = [max(0.0, 100 * (p - lo)) for p, lo in zip(ps, los)]
    upper = [max(0.0, 100 * (hi - p)) for p, hi in zip(ps, his)]
    yerr = np.array([lower, upper])

    fig, ax = plt.subplots(figsize=(max(5, 1.3 * len(labels)), 4.2))
    ax.bar(x, [100 * p for p in ps], yerr=yerr,
           capsize=4, color="#3b6ea5", edgecolor="black", linewidth=0.6)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.set_ylabel("fold vs river shove  (%)")
    ax.set_title(title)
    ax.set_ylim(0, 100)
    for xi, p, n in zip(x, ps, ns):
        ax.text(xi, 100 * p + 1.5, f"n={n:,}", ha="center", va="bottom", fontsize=8)
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return True


# --------------------------------------------------------------------------- #
# cross-stake panel table
# --------------------------------------------------------------------------- #
def _cross_stake_panel(con, site: str, seats: int) -> None:
    """Print all panel stats × all stakes for one site+seats combination.

    Each cell shows the point estimate; cells with n < 200 are flagged with *
    so thin slices are obvious without drowning the table in CI columns.
    """
    from stats import full_panel

    stakes = stakes_for(con, site, seats)
    if not stakes:
        return

    # Collect panel rows for every stake.
    stat_by_stake: dict[str, dict[str, dict]] = {}
    stat_names: list[str] = []
    for st in stakes:
        rows = full_panel(con, site=site, stake=st, seats=seats)
        stat_by_stake[st] = {r["stat"]: r for r in rows}
        if not stat_names:
            stat_names = [r["stat"] for r in rows]

    name_w = max(len(n) for n in stat_names)
    col_w = 9  # enough for "100.0%* " (7 chars + padding)

    header_parts = [f"{'stat'.ljust(name_w)}"] + [f"{st:>{col_w}}" for st in stakes]
    header = "  ".join(header_parts)
    title = f"\nCross-stake full panel: {site} {seats}-max"
    print(title)
    print("=" * len(title.strip()))
    print(header)
    print("-" * len(header))

    any_thin = False
    for name in stat_names:
        parts = [name.ljust(name_w)]
        for st in stakes:
            r = stat_by_stake[st].get(name, {})
            p = r.get("p", float("nan"))
            n = r.get("den", 0)
            if p != p or n == 0:
                cell = "n/a"
            else:
                cell = f"{100 * p:.1f}%"
                if n < 200:
                    cell += "*"
                    any_thin = True
            parts.append(f"{cell:>{col_w}}")
        print("  ".join(parts))

    if any_thin:
        print("  * n < 200 (CI is wide; treat as indicative only)")


def _cross_site_panel(con, stake: str, seats: int) -> None:
    """Print all panel stats × all sites for a given stake+seats combination."""
    from stats import full_panel

    sites = sites_for(con, stake, seats)
    if not sites:
        return

    stat_by_site: dict[str, dict[str, dict]] = {}
    stat_names: list[str] = []
    for s in sites:
        rows = full_panel(con, site=s, stake=stake, seats=seats)
        stat_by_site[s] = {r["stat"]: r for r in rows}
        if not stat_names:
            stat_names = [r["stat"] for r in rows]

    name_w = max(len(n) for n in stat_names)
    col_w = 14  # site names can be long ("AbsolutePoker")

    header_parts = [f"{'stat'.ljust(name_w)}"] + [f"{s:>{col_w}}" for s in sites]
    header = "  ".join(header_parts)
    title = f"\nCross-site full panel: {stake} {seats}-max"
    print(title)
    print("=" * len(title.strip()))
    print(header)
    print("-" * len(header))

    any_thin = False
    for name in stat_names:
        parts = [name.ljust(name_w)]
        for s in sites:
            r = stat_by_site[s].get(name, {})
            p = r.get("p", float("nan"))
            n = r.get("den", 0)
            if p != p or n == 0:
                cell = "n/a"
            else:
                cell = f"{100 * p:.1f}%"
                if n < 200:
                    cell += "*"
                    any_thin = True
            parts.append(f"{cell:>{col_w}}")
        print("  ".join(parts))

    if any_thin:
        print("  * n < 200 (CI is wide; treat as indicative only)")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase-0 EDA on the parsed snapshot.")
    ap.add_argument("--out", required=True, help="parquet store dir")
    ap.add_argument("--site", default="PokerStars", help="primary site for the headline slice")
    ap.add_argument("--stake", default="NL50", help="primary stake, e.g. NL50")
    ap.add_argument("--seats", type=int, default=6, help="primary table size")
    ap.add_argument("--chart", default=None, help="path to write the gradient PNG (optional)")
    a = ap.parse_args(argv)

    con = connect(a.out)

    # ---- 1. cells present (path-parsing sanity) ---------------------------- #
    print("Cells present (site, stake, seats -> #hands)  [verify site parsing!]")
    print("-" * 64)
    present = cells(con)
    for site, stake, seats, n in present[:40]:
        print(f"  {site:<14} {stake:<8} {seats:>2}-max   {n:>12,}")
    if any(s == "UNKNOWN" for s, *_ in present):
        print("  !! UNKNOWN site found -- pass --site to phh_to_parquet.py or fix the path layout.")
    if len(present) > 40:
        print(f"  ... and {len(present) - 40} more cells")

    # ---- 2. headline ------------------------------------------------------- #
    num, den, p, lo, hi = river_jam_rate(con, a.site, a.stake, a.seats)
    print("\nHEADLINE")
    print("-" * 64)
    if den == 0:
        print(f"  No river-all-in decisions for {a.site} {a.stake} {a.seats}-max in this store.")
        print(f"  (Check the cell list above -- the slice may be absent or mislabeled.)")
    else:
        print(f"  July 2009 {a.site} {a.stake} {a.seats}-max:")
        print(f"  fold vs a river shove = {100*p:.1f}%  "
              f"(95% CI {100*lo:.1f}-{100*hi:.1f}%, n={den:,} decisions, {num:,} folds)")

    # ---- 3. cross-stake gradient ------------------------------------------- #
    print(f"\nCross-stake gradient  ({a.site}, {a.seats}-max) -- fold vs river shove")
    print("-" * 64)
    g_labels, g_p, g_lo, g_hi, g_n = [], [], [], [], []
    for st in stakes_for(con, a.site, a.seats):
        n, d, pp, l, h = river_jam_rate(con, a.site, st, a.seats)
        g_labels.append(st); g_p.append(pp); g_lo.append(l); g_hi.append(h); g_n.append(d)
        cell = f"{100*pp:.1f}% [{100*l:.1f}-{100*h:.1f}]" if d else "n/a"
        print(f"  {st:<8} {cell:>22}   n={d:,}")

    # ---- 4. cross-site comparison ------------------------------------------ #
    print(f"\nCross-site comparison  ({a.stake}, {a.seats}-max) -- fold vs river shove")
    print("-" * 64)
    for s in sites_for(con, a.stake, a.seats):
        n, d, pp, l, h = river_jam_rate(con, s, a.stake, a.seats)
        cell = f"{100*pp:.1f}% [{100*l:.1f}-{100*h:.1f}]" if d else "n/a"
        print(f"  {s:<14} {cell:>22}   n={d:,}")

    # ---- 5. decision funnel ------------------------------------------------ #
    f = funnel(con, a.site, a.stake, a.seats)
    print(f"\nDecision funnel  ({a.site} {a.stake} {a.seats}-max) -- sample-size budgeting")
    print("-" * 64)
    h = max(f["hands"], 1)
    for k in ["hands", "saw_flop", "saw_river", "river_bet_decisions", "river_jam_decisions"]:
        print(f"  {k:<22} {f[k]:>12,}   ({100*f[k]/h:5.2f}% of hands)")

    # ---- 6. full panel for the primary slice ------------------------------- #
    from stats import print_panel
    print_panel(full_panel(con, site=a.site, stake=a.stake, seats=a.seats),
                title=f"\nFull stat panel: {a.site} {a.stake} {a.seats}-max")

    # ---- 7. cross-stake full panel ---------------------------------------- #
    _cross_stake_panel(con, a.site, a.seats)

    # ---- 8. cross-site full panel ----------------------------------------- #
    _cross_site_panel(con, a.stake, a.seats)

    # ---- 9. chart ---------------------------------------------------------- #
    if a.chart:
        ok = chart_gradient(g_labels, g_p, g_lo, g_hi, g_n,
                            f"Fold vs river shove by stake — {a.site} {a.seats}-max (Jul 2009)",
                            a.chart)
        print(f"\n{'wrote chart -> ' + a.chart if ok else 'chart skipped (no non-empty cells)'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
