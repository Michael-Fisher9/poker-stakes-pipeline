"""Dollar amounts, dropped hands, and whether the stat panel finds rates planted in
synthetic hands. The fixture in sample.phh is whole chips, which is why it never caught
the dollar bug; these run the real path: write PHH, ETL, DuckDB stats."""
import csv
import os
import subprocess
import sys

from pokerkit.notation import HandHistory

import simulate
import stats
from extract import extract_hand

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, "..")


def test_a_dollar_hand_keeps_its_bets():
    with open(os.path.join(HERE, "sample_dollars.phh"), "rb") as f:
        hand, actions = extract_hand(HandHistory.load(f), "sample_dollars.phh", 0, "PokerStars")
    kinds = [(a["street"], a["action_type"]) for a in actions if a["action_type"] in ("bet", "raise")]
    assert hand["stake"] == "NL50" and kinds == [(0, "raise"), (1, "bet"), (2, "bet"), (3, "bet")]
    jam = next(a for a in actions if a["street"] == 3 and a["facing_all_in"])
    assert jam["action_type"] == "fold" and abs(jam["facing_pct_pot"] - 20.5 / 19.25) < 1e-9


def test_planted_rates_come_back_within_their_intervals(tmp_path):
    data, out = tmp_path / "data", tmp_path / "out"
    simulate.write(str(data), 3000, seed=7, planted_path=os.path.join(ROOT, "config", "planted.csv"))
    subprocess.run([sys.executable, os.path.join(ROOT, "src", "phh_to_parquet.py"), "--data", str(data),
                    "--out", str(out), "--glob", "**/*.phhs", "--site", "Synthetic", "--workers", "2"],
                   check=True, capture_output=True)
    with open(out / "qa" / "parse_errors.csv") as f:
        assert len(f.readlines()) == 1  # header only: no hand dropped
    con = stats.connect(str(out))
    planted = {r["stake"]: r for r in csv.DictReader(open(os.path.join(ROOT, "config", "planted.csv")))}
    missed = []
    for stake, p in planted.items():
        got = {r["stat"]: r for r in stats.full_panel(con, site="Synthetic", stake=stake)}
        for stat, col in (("fold_vs_river_jam", "fold_vs_river_jam"), ("flop_cbet", "flop_cbet"),
                          ("fold_to_flop_cbet", "fold_to_flop_cbet"), ("preflop_3bet", "threebet_rate")):
            # 20 comparisons at 95% would miss about one by chance alone, so the test uses
            # z = 3.29 (99.9%). A real bug moves a rate far further than that (the dollar
            # bug took river aggression from about 20% to 0).
            r, want = got[stat], float(p[col])
            _, lo, hi = stats.wilson(r["num"], r["den"], z=3.29)
            if not (r["den"] and lo <= want <= hi):
                missed.append((stake, stat, want, round(r["p"], 3), r["den"]))
    assert not missed, missed


def test_the_etl_fails_loudly_when_hands_are_dropped(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    good = open(os.path.join(HERE, "sample.phh")).read()
    broken = good.replace('"p2 cc", "p3 cbr 2050", "p2 f",', '"p2 cc", "p3 cbr 999999", "p2 f",')
    (data / "ps hands.phh").write_text(broken)
    run = subprocess.run([sys.executable, os.path.join(ROOT, "src", "phh_to_parquet.py"), "--data", str(data),
                          "--out", str(tmp_path / "out"), "--workers", "1"], capture_output=True, text=True)
    assert run.returncode == 2 and "FAILED" in run.stderr
    rows = list(csv.DictReader(open(tmp_path / "out" / "qa" / "parse_errors.csv")))
    assert len(rows) == 1 and rows[0]["idx"] == "0"
