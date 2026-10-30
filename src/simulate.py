#!/usr/bin/env python3
"""
simulate.py
===========
Write synthetic no-limit hold'em hands in PHH format, in dollars like the HandHQ
set, with known tendencies planted per stake (config/planted.csv).

Two uses:
  * demo data, so the whole pipeline and report run without the 20 GB download;
  * a recovery test: the stat panel has to find each planted rate within its 95%
    interval (tests/test_recovery.py). A pipeline that drops or misreads hands
    fails it, which the 5-hand fixture alone didn't catch.

The players are not trying to play well. Each decision is a weighted coin flip
from the planted table, so the true rate of every headline stat is known. Every
hand is played through PokerKit's engine in cents (so every action is legal),
then written out in dollars and read back by the same loader the ETL uses.

    python src/simulate.py --hands 20000 --out data/synthetic
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import warnings
from decimal import Decimal

from pokerkit import Automation, Mode, NoLimitTexasHoldem

RANKS, SUITS = "23456789TJQKA", "cdhs"
warnings.filterwarnings("ignore", message="A card being dealt")  # we deal from our own seeded deck on purpose
AUTOMATIONS = (
    Automation.ANTE_POSTING, Automation.BET_COLLECTION, Automation.BLIND_OR_STRADDLE_POSTING,
    Automation.CARD_BURNING, Automation.HOLE_CARDS_SHOWING_OR_MUCKING, Automation.HAND_KILLING,
    Automation.CHIPS_PUSHING, Automation.CHIPS_PULLING,
)
SEAT_MIX = [(6, 0.80), (5, 0.10), (4, 0.06), (3, 0.04)]  # players dealt in; mostly full 6-max


def load_planted(path="config/planted.csv") -> dict[str, dict]:
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out[r["stake"]] = {k: (r[k] if k == "stake" else Decimal(r[k]) if k.endswith("blind") else float(r[k]))
                               for k in r}
    return out


def dollars(cents: int) -> str:
    return f"{cents / 100:.2f}".rstrip("0").rstrip(".")


def play_hand(rng: random.Random, p: dict) -> dict:
    """One hand, played in cents. Returns the PHH fields with amounts in dollars."""
    n = rng.choices([s for s, _ in SEAT_MIX], [w for _, w in SEAT_MIX])[0]
    sb, bb = int(p["small_blind"] * 100), int(p["big_blind"] * 100)
    # stacks from 40 to 200 big blinds, so river jams range from small to huge
    stacks = [bb * rng.randint(40, 200) for _ in range(n)]
    state = NoLimitTexasHoldem.create_state(AUTOMATIONS, True, 0, (sb, bb), bb, stacks, n, mode=Mode.CASH_GAME)
    deck = [r + s for r in RANKS for s in SUITS]
    rng.shuffle(deck)
    actions = []

    pf_raiser = None
    street_bettor: dict[int, int] = {}
    while state.status:
        if state.can_deal_hole():
            seat = state.hole_dealee_index
            state.deal_hole(deck.pop() + deck.pop())
            actions.append(f"d dh p{seat + 1} ????")  # hole cards hidden, as in the HandHQ files
            continue
        if state.can_deal_board():
            count = 3 if not state.board_cards else 1
            cards = "".join(deck.pop() for _ in range(count))
            state.deal_board(cards)
            actions.append(f"d db {cards}")
            continue
        if state.actor_index is None:
            break
        seat, street = state.actor_index, state.street_index
        to_call = state.checking_or_calling_amount
        pot = state.total_pot_amount
        stack = state.stacks[seat]
        lo = state.min_completion_betting_or_raising_to_amount
        hi = state.max_completion_betting_or_raising_to_amount
        can_raise = state.can_complete_bet_or_raise_to()

        def bet_to(amount):
            amount = max(lo, min(hi, amount))
            state.complete_bet_or_raise_to(amount)
            actions.append(f"p{seat + 1} cbr {dollars(amount)}")

        def call():
            state.check_or_call()
            actions.append(f"p{seat + 1} cc")

        def fold():
            state.fold()
            actions.append(f"p{seat + 1} f")

        if street == 0:
            raised = max(state.bets) > bb
            if not raised:
                if can_raise and rng.random() < p["open_rate"]:
                    bet_to(3 * bb)
                    pf_raiser = seat
                elif to_call == 0:
                    call()                      # big blind checks when folded to
                else:
                    fold()
            elif pf_raiser is not None and state.bets[pf_raiser] == max(state.bets) and max(state.bets) <= 3 * bb:
                # facing the single open
                if can_raise and rng.random() < p["threebet_rate"]:
                    bet_to(3 * max(state.bets))
                    pf_raiser = seat
                elif rng.random() < (0.35 if seat == 1 else 0.15):
                    call()
                else:
                    fold()
            else:
                (call if rng.random() < 0.4 else fold)()  # facing a 3-bet: call or fold, no 4-bets
            continue

        facing = to_call > 0
        if not facing:
            if street == 1 and seat == pf_raiser and street not in street_bettor:
                if rng.random() < p["flop_cbet"]:
                    bet_to(int(pot * rng.uniform(0.4, 0.75)))
                    street_bettor[street] = seat
                else:
                    call()
            elif street == 3 and can_raise and rng.random() < p["river_jam_rate"]:
                bet_to(hi)                      # all in
                street_bettor[street] = seat
            elif can_raise and rng.random() < (0.30 if street == 2 else 0.15):
                bet_to(int(pot * rng.uniform(0.5, 1.0)))
                street_bettor[street] = seat
            else:
                call()                          # check
            continue

        bettor = street_bettor.get(street)
        bettor_all_in = bettor is not None and state.stacks[bettor] == 0
        if street == 3 and bettor_all_in:
            fold_p = p["fold_vs_river_jam"]
        elif street == 1 and bettor == pf_raiser:
            fold_p = p["fold_to_flop_cbet"]
        else:
            fold_p = 0.45
        if rng.random() < fold_p:
            fold()
        else:
            call()

    return {"n": n, "sb": p["small_blind"], "bb": p["big_blind"],
            "stacks": [dollars(s) for s in stacks], "actions": actions}


def phh(hand: dict, name: str, date: tuple[int, int, int]) -> str:
    n = hand["n"]
    blinds = [str(hand["sb"]), str(hand["bb"])] + ["0"] * (n - 2)
    acts = ",\n  ".join(f'"{a}"' for a in hand["actions"])
    return (f"[{name}]\nvariant = \"NT\"\nantes = [{', '.join(['0'] * n)}]\n"
            f"blinds_or_straddles = [{', '.join(blinds)}]\nmin_bet = {hand['bb']}\n"
            f"starting_stacks = [{', '.join(hand['stacks'])}]\nactions = [\n  {acts},\n]\n"
            f"year = {date[0]}\nmonth = {date[1]}\nday = {date[2]}\n")


def write(out_dir: str, hands_per_stake: int, seed: int, planted_path="config/planted.csv",
          per_file: int = 2000) -> dict[str, int]:
    """Writes out_dir/<stake>/synthetic_<k>.phhs. Returns hands written per stake."""
    planted = load_planted(planted_path)
    rng = random.Random(seed)
    written = {}
    for stake, p in planted.items():
        os.makedirs(os.path.join(out_dir, stake), exist_ok=True)
        for k in range(0, hands_per_stake, per_file):
            n_here = min(per_file, hands_per_stake - k)
            with open(os.path.join(out_dir, stake, f"synthetic_{k // per_file:03d}.phhs"), "w") as f:
                for i in range(n_here):
                    f.write(phh(play_hand(rng, p), f"hand_{k + i + 1}", (2026, 1, 1 + (k + i) % 28)) + "\n")
        written[stake] = hands_per_stake
    return written


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Synthetic PHH hands with planted tendencies")
    ap.add_argument("--hands", type=int, default=20000, help="hands per stake")
    ap.add_argument("--out", default="data/synthetic")
    ap.add_argument("--seed", type=int, default=2009)
    ap.add_argument("--planted", default="config/planted.csv")
    a = ap.parse_args()
    print(write(a.out, a.hands, a.seed, a.planted))
