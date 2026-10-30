# Stat codebook

This is the authoritative, operational definition of every field the pipeline
emits and every statistic it computes. The plan is explicit that *"fold vs
river jam" needs an exact operational definition or future-you will silently
change it* — this file is that definition. The SQL in `src/stats.py` and the
extraction logic in `src/extract.py` are the executable copies; **if this
document and the code ever disagree, the code is ground truth and this file is
the bug.**

All numbers below were verified against a hand-built 5-hand fixture with known
answers (`tests/sample.phh`, checked by `tests/test_extract.py`, 18/18), a hand in dollars
(`tests/sample_dollars.phh`), and synthetic hands with planted rates (`tests/test_synthetic.py`).

---

## 1. Pipeline model

Each hand is a PHH (Poker Hand History) TOML record. We do **not** reconstruct
pot/stack/facing-bet logic by hand. Instead we replay the hand through
PokerKit's own state machine (`HandHistory.state_actions`), which yields a
`(state, action)` pair at every step. We read the engine's state at each
*decision* and record what the player did. This makes the pot math, all-in
detection, and street tracking a matter of *observation*, not re-derivation.

Two tables come out, both as Parquet:

- **`hands`** — one row per hand (site, stake, date, board, pot peak, how far
  the hand went, showdown flag).
- **`actions`** — one row per *voluntary betting decision* (fold / check /
  call / bet / raise). Forced blind and ante posts are **not** rows; they are
  part of the state the first voluntary actor faces.

A "decision node" is the engine state at the moment a player is on the clock
(`actor_index` is set). The action recorded for that node is the action that
state then transitions on.

### Money units
Monetary columns (`to_call`, `node_pot`, `bet_to`, `bet_added`, `pot_peak`) are
stored as **float64 in the PHH file's native unit**. The HandHQ/Zenodo set is
denominated in dollars (blinds `[0.25, 0.50]`), so these are dollar amounts;
a chip-denominated source would store chips. PokerKit returns these as
`Decimal`/`Fraction` for exact arithmetic and the ETL casts them to float on
write. `stake_bb` is the one exception: it is normalized to **integer cents**
(NL50 -> 50) so stake labels are stable across dollar- and chip-denominated
sources. Pot-relative ratios (`pot_odds`, `bet_pct_pot`, `facing_pct_pot`) are
dimensionless and unit-independent, and are the analytically load-bearing
fields; the raw amount columns are retained for inspection but no Phase-0 stat
reads them directly.

---

## 2. Hand-level fields (`hands` table)

| field | definition |
|---|---|
| `hand_id` | `"<source_file>#<source_idx>"`, unique within the store. |
| `site` | Inferred from the **file path** (PHH bodies omit site). See §6. |
| `stake_bb` | Big blind = `max(blinds_or_straddles)`. See §5. |
| `stake` | `"NL" + stake_bb`, e.g. blinds `[25,50]` → `"NL50"`. |
| `n_seats` | Number of starting stacks = players dealt in. **Not the table size**: a 6-max table with four players is `n_seats = 4`. The source files don't record table size, so charts say "six players dealt in", never "6-max". |
| `variant` | PHH variant code; `"NT"` = no-limit Texas hold'em. |
| `day` / `month` / `year` | From the PHH record (the Zenodo set is July 2009). |
| `board` | Concatenated board cards as dealt, e.g. `"2c7hKd5s9d"`. `""` if no flop. |
| `pot_peak` | Max `total_pot_amount` observed during the hand (includes bets live on the table at the moment of the peak). |
| `max_street` | Furthest street reached: 0 preflop, 1 flop, 2 turn, 3 river. |
| `saw_flop` / `saw_turn` / `saw_river` | `max_street >= 1 / 2 / 3`. |
| `went_to_showdown` | River was dealt **and** ≥ 2 players never folded. See note. |
| `source_file` / `source_idx` | Provenance: which file and which hand index within it. |

**`went_to_showdown` note.** This is a *hand-level* flag: did the hand reach a
showdown at all. It is **not** a per-player "this seat saw a showdown" flag.
The hand-level WTSD stat (§4) divides showdown hands by flop-seeing hands;
treat it as a population texture measure, not the textbook per-player WTSD.

---

## 3. Action-level fields (`actions` table)

One row per voluntary decision. Fields that describe *what the player faced*
are measured **at the decision node, before the action is applied**.

### Identity / position
| field | definition |
|---|---|
| `action_idx` | 0-based index of this decision within the hand. |
| `street` / `street_name` | 0/preflop, 1/flop, 2/turn, 3/river. |
| `seat` | 0-based seat index. Seat 0 = small blind by the PHH blind-vector convention. |
| `position` | Label (`SB`, `BB`, `UTG`, `CO`, `BTN`, …) from `seat` and `n_seats`. **Secondary cut — verify against your own data before trusting** (see §7). |

### Action taken
| field | definition |
|---|---|
| `action_type` | One of `fold`, `check`, `call`, `bet`, `raise`. |
| `bet_to` | For an aggressive action, the absolute amount the player is betting/raising **to** (PHH `cbr` is a target, not an increment). 0 otherwise. |
| `bet_added` | Chips this aggressive action puts in *beyond the player's current street contribution* = `bet_to − (player's bet already on this street)`. This is the "size of the bet" people mean colloquially. |
| `actor_all_in_after` | True if the actor's stack is 0 after the action (they are all-in). |

**Open = raise.** A preflop opening raise *faces the big blind's forced bet*,
so it is classified `raise`, not `bet`. The first voluntary bet on a postflop
street (nothing in front) is `bet`. Consequence: counting preflop `raise`
actions with no prior raise gives you opens; see `preflop_3bet` (§4) for the
re-raise definition.

### What the player faced
| field | definition |
|---|---|
| `faced_bet` | True if there was a live amount to call (`to_call > 0`) at the node. |
| `to_call` | Chips required to call (PokerKit `checking_or_calling_amount`). |
| `node_pot` | `total_pot_amount` at the node. **Includes the bet currently being faced** (and any other uncollected bets on the table). |
| `pot_odds` | `to_call / (node_pot + to_call)` — the break-even calling threshold. *Note `node_pot` already contains the faced bet, so the caller's own call is the only thing added in the denominator.* Verified: a river shove of 2050 into a 3975 pot gives `2050/(3975+2050) = 0.340`. |
| `facing_pct_pot` | Size of the bet the player is facing, as a fraction of the pot **before that bet went in**. Propagated from the standing aggressor's `bet_pct_pot` on that street (exact, since we carry it forward during replay). |
| `facing_all_in` | True iff some **other** player owns the current maximum bet **and** that player's stack is 0 — i.e. the player is facing a live all-in. |
| `call_is_all_in` | True if calling would put *this* player all-in. |

### Aggression sizing (when the player bets/raises)
| field | definition |
|---|---|
| `bet_pct_pot` | `bet_added / node_pot` — the bet as a fraction of the pot it was made into. Verified: a 2050 river shove with 1925 already reflected gives `2050/1925 = 1.065` (a slight overbet). |

### Line / role context
| field | definition |
|---|---|
| `n_live` | Players not yet folded entering the node. |
| `n_agg_before_on_street` | Number of aggressive actions (bets/raises) already made on this street **before** this action. On the open this is 0; the first re-raiser sees 1. |
| `preflop_n_raises` | `n_agg_before_on_street` on preflop rows, else null. |
| `is_pf_aggressor` | True if **this seat is the standing preflop raiser** — i.e. the last person to raise preflop. False on the open itself; True for that player's later actions. Drives c-bet flags. |
| `is_cbet` | True if this action is the preflop aggressor's **first aggressive action on a postflop street with nobody having bet yet** (a continuation bet). |
| `facing_cbet` | True if the player is facing the PFR's continuation bet **before anyone has raised it** (exactly one aggressive action stands, and it is the PFR's). |

A "flop c-bet" in the usual sense is `is_cbet AND street = 1`. The pipeline
flags c-bets on any postflop street; filter by street as needed.

---

## 4. The stat panel

Every stat is a **binomial proportion**: `numerator / denominator`, where the
denominator is the set of decisions that reached a defined node and the
numerator is the subset taking a defined action. Each is reported with a
**Wilson 95% score interval** (§8). Definitions below are exactly the SQL in
`src/stats.py`.

### Headline — `fold_vs_river_jam`
> Of voluntary river decisions made **while facing a live all-in bet**, the
> fraction that **fold**.

- **Universe (denominator):** `street = 3 AND facing_all_in` and
  `action_type ∈ {fold, call, raise}`.
- **Numerator:** `action_type = 'fold'`.
- **Variant `fold_vs_river_jam_excl_raise`:** same node, denominator drops the
  rare raise-over-shove (`{fold, call}` only). It should sit within a hair of
  the headline; a visible gap means an unusual number of re-raises over shoves
  and is worth a look. This is a built-in sanity check, not a separate claim.

### `fold_to_river_bet_*` (sizing buckets)
> Of voluntary river decisions made **while facing any bet/raise**, the
> fraction that fold, split by the size of the bet faced.

- **Universe:** `street = 3 AND faced_bet`, `action_type ∈ {fold, call, raise}`.
- **Sizing measure:** `facing_pct_pot` when available; otherwise derived as
  `to_call / (node_pot − to_call)` — exact for first-bet situations (~88% of
  affected rows), approximate for raise-over-bet situations (~12%). See
  **Known Limitations §6** for the parse-bug context.
- **Buckets** (mutually exclusive, exhaustive):
  - `le33` — not all-in, sizing ≤ 33%
  - `33_66` — not all-in, 33% < sizing ≤ 66%
  - `66_100` — not all-in, 66% < sizing ≤ 100%
  - `100_200` — not all-in, 100% < sizing ≤ 200%
  - `gt200` — not all-in, sizing > 200%
  - `allin` — `facing_all_in` (any size)
- **Consistency check:** `fold_to_river_bet_allin` is computed over the *same
  events* as `fold_vs_river_jam` via a different query path; their fold rates
  must be identical. (In the fixture both are 1/2 = 50.0.%)

### `river_aggression_freq` (RAF)
> Aggressive river actions as a fraction of all river actions —
> `(bet + raise) / (bet + raise + call + fold + check)`, restricted to
> `street = 3`. (Frequency form of the aggression factor.)

### `preflop_3bet`
> Of preflop decisions made **while facing exactly one standing raise** (the
> open), the fraction that **re-raise** (the 3-bet).

- **Universe:** `street = 0 AND faced_bet AND n_agg_before_on_street = 1`,
  `action_type ∈ {fold, call, raise}`.
- **Numerator:** `action_type = 'raise'`.

### `flop_cbet`
> Of flops where the preflop aggressor is **first to act with no bet in front
> of them**, the fraction on which they continuation-bet.

- **Universe:** `street = 1 AND is_pf_aggressor AND n_agg_before_on_street = 0`,
  `action_type ∈ {bet, check}`.
- **Numerator:** `is_cbet`.

### `fold_to_flop_cbet`
> Of decisions made **while facing the PFR's flop c-bet** (before anyone raises
> it), the fraction that fold.

- **Universe:** `street = 1 AND facing_cbet`, `action_type ∈ {fold, call, raise}`.
- **Numerator:** `action_type = 'fold'`.

### `wtsd_hand_level`
> Hands reaching a showdown / hands that saw a flop. **Hand-level**, not the
> per-player WTSD (which needs per-seat reach tracking, deferred past Phase 0).

---

## 5. Stake from blinds

`stake_bb = max(blinds_or_straddles)`; `stake = "NL" + stake_bb`. Using the max
(rather than the second element) is robust to straddles and to ante-only games.
NL50 has blinds `[25, 50]`. If a future dataset encodes stakes differently,
this is the one place to change.

---

## 6. Site from file path

PHH bodies in the Zenodo set do **not** contain the site, so site is inferred
by matching known tokens against the file path (case-insensitive substring
match). Two sets of tokens are recognised:

| Token(s) | Site label |
|---|---|
| `pokerstars`, `ps` | `PokerStars` |
| `partypoker`, `party`, `pty` | `PartyPoker` |
| `fulltilt`, `full_tilt`, `ftp` | `FullTilt` |
| `ipoker`, `ipn` | `iPoker` |
| `ongame`, `ong` | `Ongame` |
| `absolute`, `abs` | `AbsolutePoker` |

The short tokens (`ps`, `pty`, `ipn`, `ong`, `abs`) cover the HandHQ/Zenodo
flat-file naming convention where files are named `ps NLH handhq_N.phh`,
`ipn NLH handhq_N.phh`, etc. Full-name tokens cover structured paths like
`pokerstars/NL50/hand.phh`. If a path matches none, the site is `UNKNOWN`
and the ETL prints a warning; pass `--site` to force it. **`eda.py` prints
every (site, stake, seats) cell first precisely so you can confirm this
parsing before trusting any number.**

---

## 7. Known limitations / caveats

1. **Position labels are provisional.** They assume seat 0 = SB from the PHH
   blind vector and a standard seat→name map per table size. Button/blind
   identification in scraped histories is not guaranteed; headline metrics do
   **not** depend on position, but any by-position cut must be validated
   against hands whose positions you know.
2. **WTSD is hand-level**, not per-player (see §4).
3. **Single snapshot.** The Zenodo data is July 1–23 2009 only. Phase 0
   produces a *level*, not a trend; trends require the purchased later slices
   (Phases 1–2). Pool composition is the dominant confounder, which is why the
   whole design fixes site + stake + format.
4. **Site coverage is uneven** across stakes; thin cells will have wide CIs.
   The decision funnel in `eda.py` tells you how thin before you over-read a
   point estimate.
5. **Corrupt hands** are counted and skipped, not fatal; the ETL reports the
   count per file.
6. **Bet-sizing fields are NULL for fractional-dollar bets** in the initial
   Parquet/DuckDB store. The bug: `extract.py` parsed bet amounts with
   `int()`, which raises `ValueError` on floats like `"2.50"`, silently
   setting `bet_to`, `bet_added`, `bet_pct_pot`, and downstream
   `facing_pct_pot` to `NULL`. Affected ~52% of PS bets, ~61% of PartyPoker,
   and smaller fractions at other sites; whole-dollar bets (`"2"`, `"4"`)
   parsed correctly. **Fix:** `int()` → `float()` in `_parse_action`
   (one-line change). **Workaround for existing store:** `stats.py` fills the
   gap using `to_call / (node_pot − to_call)`, which equals the aggressor's
   `bet_pct_pot` exactly for first-bet situations and is approximate for
   raise-over-bet situations. Re-running the ETL after the fix produces exact
   `facing_pct_pot` values everywhere.

---

## 8. Confidence intervals

Proportions use the **Wilson score interval** at 95% (`z = 1.95996`), not the
normal approximation. Wilson stays inside `[0, 1]` and remains sensible at
small `n` and near `p = 0` or `p = 1` — important because the river-jam node is
rare and gets thin once sliced by line or position. Report both the point
estimate and the interval; never quote a fold-vs-jam percentage without its `n`.
