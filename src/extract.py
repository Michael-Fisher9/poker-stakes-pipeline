"""
extract.py
==========
Turn a single PokerKit ``HandHistory`` into tidy rows.

The hard part of a poker-stats pipeline is reconstructing, at every decision,
"what was this player facing" -- pot size, the bet faced, whether the bettor was
all-in, the line so far. We get this *for free* by replaying the hand through
PokerKit's own state machine (``HandHistory.state_actions``) and reading the
state at each decision node, rather than re-implementing pot/all-in logic
ourselves. See codebook.md for the exact definition of every field below.

Two tables come out:
  * one ``hand`` dict  (one row per hand)
  * a list of ``action`` dicts (one row per *voluntary* betting decision:
    fold / check / call / bet / raise)

Everything here is verified against hand-computed examples in tests/.
"""
from __future__ import annotations

from dataclasses import dataclass

# Street indices used by PokerKit for No-limit Texas Hold'em (verified empirically).
PREFLOP, FLOP, TURN, RIVER = 0, 1, 2, 3
STREET_NAME = {PREFLOP: "preflop", FLOP: "flop", TURN: "turn", RIVER: "river"}

# 0-indexed seat -> position label, assuming the small blind is seat 0 (PHH blind
# vector convention). This is a *secondary* cut; the headline river metrics do not
# depend on it. Verify against a few of your own hands -- see README.
_POS = {
    2: {0: "SB", 1: "BB"},
    3: {0: "SB", 1: "BB", 2: "BTN"},
    4: {0: "SB", 1: "BB", 2: "CO", 3: "BTN"},
    5: {0: "SB", 1: "BB", 2: "UTG", 3: "CO", 4: "BTN"},
    6: {0: "SB", 1: "BB", 2: "UTG", 3: "HJ", 4: "CO", 5: "BTN"},
    7: {0: "SB", 1: "BB", 2: "UTG", 3: "UTG1", 4: "HJ", 5: "CO", 6: "BTN"},
    8: {0: "SB", 1: "BB", 2: "UTG", 3: "UTG1", 4: "MP", 5: "HJ", 6: "CO", 7: "BTN"},
    9: {0: "SB", 1: "BB", 2: "UTG", 3: "UTG1", 4: "UTG2", 5: "MP", 6: "HJ", 7: "CO", 8: "BTN"},
}


def position_label(seat: int, n_seats: int) -> str:
    return _POS.get(n_seats, {}).get(seat, "UNK")


def stake_label_from_blinds(blinds) -> tuple[int, str]:
    """Big blind in cents and a label like 'NL50'.

    Handles both integer chip units (e.g. [25, 50]) and decimal dollar amounts
    (e.g. [0.25, 0.50] from the Zenodo/HandHQ set).  In both cases the result
    is (50, 'NL50') for a $0.25/$0.50 game.
    """
    bb_raw = max(blinds) if blinds else 0
    # If blinds are in dollars (all < 20), convert to cents:
    # $0.50 -> 50, $1.00 -> 100, $2.00 -> 200, etc.
    if bb_raw < 20:
        bb = int(round(bb_raw * 100))
    else:
        bb = int(bb_raw)
    return bb, f"NL{bb}"


def _parse_action(tok: str):
    """'p3 cbr 2050' -> (2, 'cbr', 2050); 'p2 f' -> (1, 'f', None).

    Returns (seat0_index, verb, amount|None) for *player* actions, else None for
    dealer actions ('d db ...', 'd dh ...') and anything non-player.
    """
    parts = tok.split()
    if not parts or not parts[0].startswith("p") or parts[0] == "p":
        return None
    try:
        seat = int(parts[0][1:]) - 1  # 'p3' -> index 2
    except ValueError:
        return None
    verb = parts[1] if len(parts) > 1 else ""
    amt = None
    if verb == "cbr" and len(parts) > 2:
        try:
            amt = float(parts[2])
        except ValueError:
            amt = None
    return seat, verb, amt


@dataclass
class _Snap:
    """Primitive snapshot of a state (state objects are mutated in place during
    replay, so we copy out the scalars we need at every yield)."""
    street: int
    actor: int | None
    pot: int                # total_pot_amount: collected pot + bets on the table
    bets: tuple
    stacks: tuple
    to_call: int            # checking_or_calling_amount for the actor (0 if none)
    board_len: int
    n_live: int             # players not yet folded


def _snapshot(state) -> _Snap:
    # PokerKit keeps money as Decimal when the history is in dollars (HandHQ: blinds 0.25/0.50)
    # and as int when it's in chips. Everything here is float from this point on. Mixing a
    # float bet with a Decimal pot raised TypeError on the first bet of every dollar hand,
    # and the ETL dropped those hands without saying so (found Oct 2026; see changelog).
    bets = tuple(float(b) for b in state.bets)
    stacks = tuple(float(x) for x in state.stacks)
    actor = state.actor_index
    to_call = float(state.checking_or_calling_amount) if actor is not None else 0.0
    # statuses[i] is True while player i is still in the hand (not folded).
    n_live = sum(1 for s in state.statuses if s)
    board_len = len(state.board_cards) if state.board_cards is not None else 0
    return _Snap(
        street=state.street_index if state.street_index is not None else -1,
        actor=actor,
        pot=float(state.total_pot_amount),
        bets=bets,
        stacks=stacks,
        to_call=to_call or 0,
        board_len=board_len,
        n_live=n_live,
    )


def _facing_all_in(snap: _Snap, actor: int) -> bool:
    """True iff the actor faces a bet owned by an all-in player.

    The bet being faced is the largest outstanding wager that exceeds the
    actor's own. If any *other* player holds that max bet and has a zero stack,
    the actor is facing an all-in.
    """
    if snap.to_call <= 0:
        return False
    max_bet = max(snap.bets)
    if max_bet <= snap.bets[actor]:
        return False
    for j, b in enumerate(snap.bets):
        if j != actor and b == max_bet and snap.stacks[j] == 0:
            return True
    return False


def extract_hand(hh, source_file: str, source_idx: int, site: str) -> tuple[dict, list[dict]]:
    """Replay one hand; return (hand_row, [action_rows])."""
    bb, stake = stake_label_from_blinds(hh.blinds_or_straddles)
    n_seats = len(hh.starting_stacks)
    hand_id = f"{site}:{source_file}:{source_idx}"

    # Per-hand / per-street bookkeeping built up during the single replay pass.
    pf_aggressor: int | None = None      # last raiser preflop == "the PFR"
    street_agg_count = {}                # number of aggressive actions seen so far this street
    street_last_agg_seat = {}            # seat of most recent aggressor this street
    street_last_agg_pct = {}             # that aggressor's bet-size as a fraction of pot
    folded = set()
    max_street = -1
    pot_peak = 0

    action_rows: list[dict] = []
    prev: _Snap | None = None
    action_idx = 0

    for state, action in hh.state_actions:
        snap = _snapshot(state)
        if snap.street > max_street and snap.street >= 0:
            max_street = snap.street
        if snap.pot > pot_peak:
            pot_peak = snap.pot

        parsed = _parse_action(action) if action is not None else None
        # Only fold / check-call / bet-raise are decisions we record.
        if parsed is not None and parsed[1] in ("f", "cc", "cbr") and prev is not None:
            seat, verb, amt = parsed
            street = prev.street
            agg_n = street_agg_count.get(street, 0)
            to_call = prev.to_call
            node_pot = prev.pot                       # pot at the moment of decision
            faced = to_call > 0
            faced_all_in = _facing_all_in(prev, seat)

            # Sanity guard: skip nonsensical action+state combinations that
            # indicate a malformed PHH (e.g. some sites record a fold with
            # no bet in front, or blind posts appear as cbr tokens).
            if verb == "f" and not faced:
                prev = snap
                continue
            if verb == "cc" and not faced and street < 0:
                prev = snap
                continue

            # ---- classify the action -------------------------------------
            if verb == "f":
                atype = "fold"
            elif verb == "cc":
                atype = "call" if faced else "check"
            else:  # cbr
                atype = "raise" if faced else "bet"

            # ---- sizing of an aggressive action --------------------------
            bet_added = None
            bet_pct_pot = None
            actor_all_in_after = False
            if verb == "cbr" and amt is not None:
                bet_added = amt - prev.bets[seat]      # chips this action puts in
                # Guard: bet_added must be positive. Zero or negative means the
                # parsed amount is at or below the player's existing stake, which
                # is a malformed action (e.g. a blind post mis-classified as cbr).
                if bet_added <= 0:
                    bet_added = None
                else:
                    bet_pct_pot = (bet_added / node_pot) if node_pot > 0 else None
                    actor_all_in_after = bet_added >= prev.stacks[seat]

            # ---- sizing of the bet being faced ---------------------------
            facing_pct_pot = street_last_agg_pct.get(street) if faced else None
            pot_odds = (to_call / (node_pot + to_call)) if faced and (node_pot + to_call) > 0 else None
            call_is_all_in = faced and to_call >= prev.stacks[seat]

            # ---- continuation-bet flags ----------------------------------
            # A c-bet here = the preflop aggressor making the first aggressive
            # action on a postflop street.
            is_cbet = (
                verb == "cbr" and street >= FLOP and agg_n == 0 and seat == pf_aggressor
            )
            # Facing a c-bet = facing a bet that is the PFR's first postflop
            # aggression on this street, before anyone has raised it.
            facing_cbet = (
                faced and street >= FLOP and agg_n == 1
                and street_last_agg_seat.get(street) == pf_aggressor
                and pf_aggressor is not None and seat != pf_aggressor
            )

            action_rows.append({
                "hand_id": hand_id,
                "site": site,
                "stake_bb": bb,
                "stake": stake,
                "n_seats": n_seats,
                "action_idx": action_idx,
                "street": street,
                "street_name": STREET_NAME.get(street, "?"),
                "seat": seat,
                "position": position_label(seat, n_seats),
                "action_type": atype,
                "faced_bet": faced,
                "to_call": to_call,
                "node_pot": node_pot,
                "pot_odds": pot_odds,
                "facing_pct_pot": facing_pct_pot,
                "facing_all_in": faced_all_in,
                "call_is_all_in": call_is_all_in,
                "bet_to": amt,
                "bet_added": bet_added,
                "bet_pct_pot": bet_pct_pot,
                "actor_all_in_after": actor_all_in_after,
                "n_live": prev.n_live,
                "preflop_n_raises": agg_n if street == PREFLOP else None,
                "is_pf_aggressor": (seat == pf_aggressor),
                "is_cbet": is_cbet,
                "facing_cbet": facing_cbet,
                "n_agg_before_on_street": agg_n,
            })
            action_idx += 1

            # ---- update bookkeeping AFTER recording the row --------------
            if verb == "f":
                folded.add(seat)
            if verb == "cbr":
                street_agg_count[street] = agg_n + 1
                street_last_agg_seat[street] = seat
                street_last_agg_pct[street] = bet_pct_pot
                if street == PREFLOP:
                    pf_aggressor = seat

        prev = snap

    # ---- hand-level row ------------------------------------------------------
    final = prev
    board = ""
    if state.board_cards:
        board = "".join(repr(c) for grp in state.board_cards for c in grp)
    # showdown reached: river was dealt AND >= 2 players never folded.
    live_at_end = n_seats - len(folded)
    went_to_showdown = (max_street >= RIVER) and (live_at_end >= 2)

    hand_row = {
        "hand_id": hand_id,
        "site": site,
        "stake_bb": bb,
        "stake": stake,
        "n_seats": n_seats,
        "variant": hh.variant,
        "day": hh.day,
        "month": hh.month,
        "year": hh.year,
        "board": board,
        "pot_peak": pot_peak,
        "max_street": max_street,
        "saw_flop": max_street >= FLOP,
        "saw_turn": max_street >= TURN,
        "saw_river": max_street >= RIVER,
        "went_to_showdown": went_to_showdown,
        "source_file": source_file,
        "source_idx": source_idx,
    }
    return hand_row, action_rows
