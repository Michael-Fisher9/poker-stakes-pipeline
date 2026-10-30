#!/usr/bin/env python3
"""
report.py
=========
One figure and one table, built only from the stat panel (stats.py), so every
number on the page comes from the same query that the tests check.

    python src/report.py --out out --site Synthetic --planted config/planted.csv
    python src/report.py --out out/pokermeta.db --site PokerStars --seats 6

Writes <out>/report/<date>/stakes.png and stakes.md. With --planted (synthetic
data only) the planted rates are drawn as open markers next to the estimates.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stats  # noqa: E402

ACCENT, GRAY, INK = "#1f4e79", "#9aa5b1", "#222222"
PANELS = [  # (stat, title, planted column)
    ("fold_vs_river_jam", "Fold rate when facing a river all-in", "fold_vs_river_jam"),
    ("flop_cbet", "Flop continuation-bet rate", "flop_cbet"),
    ("fold_to_flop_cbet", "Fold rate when facing a flop continuation bet", "fold_to_flop_cbet"),
    ("preflop_3bet", "Preflop re-raise rate when facing an open", "threebet_rate"),
]


def stakes_in(con, site, seats) -> list[str]:
    where = stats.where_clause(site=site, seats=seats)
    rows = con.execute(f"SELECT stake, min(stake_bb) bb, count(*) n FROM hands WHERE {where} GROUP BY 1 ORDER BY 2").fetchall()
    return [s for s, _, n in rows if n >= 500]  # thinner stakes would only add noise to the page


def collect(con, site, seats, stakes) -> dict[str, dict]:
    out = {}
    for stake in stakes:
        out[stake] = {r["stat"]: r for r in stats.full_panel(con, site=site, stake=stake, seats=seats)}
        out[stake]["hands"] = con.execute(
            f"SELECT count(*) FROM hands WHERE {stats.where_clause(site=site, stake=stake, seats=seats)}").fetchone()[0]
    return out


def figure(data, stakes, planted, title, source, path):
    plt.rcParams.update({"font.family": "sans-serif", "font.size": 9, "axes.edgecolor": GRAY,
                         "axes.labelcolor": INK, "xtick.color": INK, "ytick.color": INK})
    fig, axes = plt.subplots(2, 2, figsize=(10, 6.6), sharex=True)
    x = range(len(stakes))
    for ax, (stat, panel_title, col) in zip(axes.flat, PANELS):
        rows = [data[s][stat] for s in stakes]
        p = [r["p"] * 100 if r["den"] else float("nan") for r in rows]
        err = [[(r["p"] - r["lo"]) * 100 if r["den"] else 0 for r in rows],
               [(r["hi"] - r["p"]) * 100 if r["den"] else 0 for r in rows]]
        ax.errorbar(x, p, yerr=err, fmt="o", color=ACCENT, ecolor=ACCENT, elinewidth=1, capsize=3, markersize=5,
                    label="Estimate, 95% interval")
        if planted:
            ax.plot([i + 0.18 for i in x], [float(planted[s][col]) * 100 for s in stakes], "D", mfc="white",
                    mec=GRAY, markersize=5, linestyle="none", label="Planted rate")
        for i, r in zip(x, rows):
            ax.annotate(f"n={r['den']:,}", (i, 0), xycoords=("data", "axes fraction"), xytext=(0, 3),
                        textcoords="offset points", ha="center", fontsize=7, color=GRAY)
        ax.set_title(panel_title, loc="left", fontsize=10, color=INK)
        ax.set_ylabel("Percent of decisions")
        lo = min(v for v in p if v == v)
        hi = max(v for v in p if v == v)
        pad = max(5, (hi - lo) * 0.6)
        ax.set_ylim(max(0, lo - pad), min(100, hi + pad))  # same rule on every panel, never a 2-point window
        ax.set_xlim(-0.6, len(stakes) - 0.4)
        ax.grid(axis="y", color="#e6e9ed", linewidth=0.8)
        ax.spines[["top", "right"]].set_visible(False)
    for ax in axes[1]:
        ax.set_xticks(list(x), stakes)
        ax.set_xlabel("Stake (no-limit, big blind in cents)")
    axes[0][0].legend(frameon=False, fontsize=8, loc="upper left")
    fig.suptitle(title, x=0.01, ha="left", fontsize=12, color=INK)
    fig.text(0.01, 0.005, source, fontsize=7, color=GRAY)
    fig.tight_layout(rect=(0, 0.02, 1, 0.97))
    fig.savefig(path, dpi=160)
    plt.close(fig)


def table(data, stakes, planted) -> str:
    head = "| Stake | Hands | " + " | ".join(t for _, t, _ in PANELS) + " |"
    lines = [head, "|---|---:|" + "---:|" * len(PANELS)]
    for s in stakes:
        cells = []
        for stat, _, col in PANELS:
            r = data[s][stat]
            cell = f"{r['p']:.1%} ({r['lo']:.1%}–{r['hi']:.1%}), n={r['den']:,}" if r["den"] else "no data"
            if planted:
                cell += f"; planted {float(planted[s][col]):.0%}"
            cells.append(cell)
        lines.append(f"| {s} | {data[s]['hands']:,} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Stat panel by stake: one figure, one table")
    ap.add_argument("--out", required=True, help="parquet store dir or .db file")
    ap.add_argument("--site", default=None)
    ap.add_argument("--seats", type=int, default=None, help="players dealt in (not table size)")
    ap.add_argument("--planted", default=None, help="config/planted.csv, for synthetic data")
    ap.add_argument("--source", default=None, help="source line under the figure")
    a = ap.parse_args(argv)

    con = stats.connect(a.out)
    stakes = stakes_in(con, a.site, a.seats)
    if not stakes:
        print("No stake with 500 or more hands for that filter.", file=sys.stderr)
        return 1
    data = collect(con, a.site, a.seats, stakes)
    planted = None
    if a.planted:
        with open(a.planted, newline="") as f:
            planted = {r["stake"]: r for r in csv.DictReader(f)}
    seats = f", {a.seats} players dealt in" if a.seats else ""
    title = f"{a.site or 'All sites'}{seats}: four betting rates by stake"
    source = a.source or ("Synthetic hands from src/simulate.py (config/planted.csv)." if planted else
                          "Data: Kim, J. (2024). A Dataset of Poker Hand Histories. Zenodo, doi:10.5281/zenodo.13997158 (CC BY 4.0).")

    base = os.path.dirname(a.out) if a.out.endswith(".db") else a.out
    folder = os.path.join(base, "report", date.today().isoformat())
    os.makedirs(folder, exist_ok=True)
    figure(data, stakes, planted, title, source, os.path.join(folder, "stakes.png"))
    with open(os.path.join(folder, "stakes.md"), "w") as f:
        f.write(f"# {title}\n\nEach rate is a share of the decisions where it could happen, with a Wilson 95% "
                f"interval. Definitions are in codebook.md.\n\n{table(data, stakes, planted)}\n\n{source}\n")
    print(f"-> {folder}/stakes.png, stakes.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
