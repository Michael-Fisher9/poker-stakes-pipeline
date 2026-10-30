"""
test_extract.py -- proves the extractor on tests/sample.phh (known answers).
Run:  python -m pytest     (or python tests/test_extract.py for the itemized list)
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pokerkit.notation import HandHistory
from extract import extract_hand

HERE = os.path.dirname(__file__)


def load():
    with open(os.path.join(HERE, "sample.phh"), "rb") as f:
        hhs = list(HandHistory.load_all(f))
    hands, actions = [], []
    for i, hh in enumerate(hhs):
        hr, ar = extract_hand(hh, "sample.phh", i, "PokerStars")
        hands.append(hr)
        actions.extend(ar)
    return hands, actions


def run_checks():
    hands, actions = load()
    checks = []

    def ck(name, cond, got=None):
        checks.append((name, bool(cond), got))

    # ---- hand-level -------------------------------------------------------
    ck("5 hands parsed", len(hands) == 5, len(hands))
    ck("all NL50", all(h["stake"] == "NL50" for h in hands))
    ck("all 6 seats", all(h["n_seats"] == 6 for h in hands))
    wtsd = [h["hand_id"] for h in hands if h["went_to_showdown"]]
    ck("exactly 1 showdown (hand_2)", len(wtsd) == 1 and wtsd[0].endswith(":1"), wtsd)
    ck("hand_4 saw no flop", not hands[3]["saw_flop"])
    ck("hand_1 full board len 10 chars", len(hands[0]["board"]) == 10, hands[0]["board"])

    # ---- headline: fold vs river all-in ----------------------------------
    river_allin = [a for a in actions if a["street"] == 3 and a["facing_all_in"]]
    ck("2 river all-in decisions", len(river_allin) == 2, len(river_allin))
    folds = sum(a["action_type"] == "fold" for a in river_allin)
    calls = sum(a["action_type"] == "call" for a in river_allin)
    ck("river all-in: 1 fold 1 call", folds == 1 and calls == 1, (folds, calls))
    ck("fold-to-river-allin == 0.5", abs(folds / (folds + calls) - 0.5) < 1e-9)
    # pot odds + faced sizing on the jam (hand-computed: 2050/6025, 2050/1925)
    jam = river_allin[0]
    ck("jam pot_odds ~0.340", abs(jam["pot_odds"] - 2050 / 6025) < 1e-6, jam["pot_odds"])
    ck("jam faced_pct ~1.065", abs(jam["facing_pct_pot"] - 2050 / 1925) < 1e-6, jam["facing_pct_pot"])

    # ---- sub-pot river bet (hand_3): fold in bucket (0, 0.5] -------------
    river_bet_nonallin = [a for a in actions if a["street"] == 3 and a["faced_bet"] and not a["facing_all_in"]]
    ck("1 non-all-in river bet faced", len(river_bet_nonallin) == 1, len(river_bet_nonallin))
    b = river_bet_nonallin[0]
    ck("it was a fold", b["action_type"] == "fold")
    ck("faced_pct in (0,0.5]", 0 < b["facing_pct_pot"] <= 0.5, b["facing_pct_pot"])

    # ---- flop c-bet / fold-to-flop-c-bet ---------------------------------
    flop_cbets = [a for a in actions if a["street"] == 1 and a["is_cbet"]]
    ck("flop c-bets present (hands 1,2,3,5)", len(flop_cbets) == 4, len(flop_cbets))
    fold_to_flop_cbet = [a for a in actions if a["street"] == 1 and a["facing_cbet"]]
    folds_vs_cbet = sum(a["action_type"] == "fold" for a in fold_to_flop_cbet)
    ck("hand_5 is the only flop-c-bet fold", folds_vs_cbet == 1, folds_vs_cbet)

    # ---- preflop opens are 'raise' (they face the BB), and the opener
    #      becomes the standing PFR (is_pf_aggressor True on their flop c-bet) ---
    pf_open = [a for a in actions if a["street"] == 0 and a["action_type"] == "raise"
               and a["n_agg_before_on_street"] == 0]
    ck("5 preflop opens (classified as raise)", len(pf_open) == 5, len(pf_open))
    ck("flop c-bettors are the standing PFR", all(a["is_pf_aggressor"] for a in flop_cbets))

    return checks


def test_known_answers():
    failed = [(name, got) for name, ok, got in run_checks() if not ok]
    assert not failed, failed


def main():
    checks = run_checks()
    # ---- report -----------------------------------------------------------
    width = max(len(n) for n, _, _ in checks)
    passed = 0
    for name, ok, got in checks:
        tag = "PASS" if ok else "FAIL"
        extra = "" if (ok or got is None) else f"   got={got!r}"
        print(f"[{tag}] {name:<{width}}{extra}")
        passed += ok
    print(f"\n{passed}/{len(checks)} checks passed")
    sys.exit(0 if passed == len(checks) else 1)


if __name__ == "__main__":
    main()
