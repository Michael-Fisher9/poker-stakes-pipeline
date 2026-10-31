## 2025-06-09

### Fix: decimal dollar amounts (extract.py, phh_to_parquet.py)
The HandHQ/Zenodo dataset denominates all monetary values in dollars
(blinds [0.25, 0.50]) rather than integer chips. PokerKit returns these
as Python Decimal objects. Fixed stake_label_from_blinds() to multiply
by 100 when bb < 20, so NL50 labels correctly. Changed monetary schema
columns from int64 to float64 and added _coerce_float() in _table() to
prevent pyarrow ArrowInvalid errors on Decimal input.

### Fix: site labels (phh_to_parquet.py + in-place Parquet relabeling)
SITE_TOKENS only recognized 'pokerstars'/'ftp' by full name. The HandHQ
folder/file naming uses short prefixes (ABS, IPN, ONG, PTY, PS-).
Added prefix mappings to SITE_TOKENS. Existing UNKNOWN-labeled shards
relabeled in-place via DuckDB COPY ... SELECT * REPLACE.
Affected shards: all out/hands/ and out/actions/ files whose names
contain abs/ipn/ong/pty/ps/ftp.

## 2026-10-30

### Fix: dollar hands with a bet were dropped (extract.py, phh_to_parquet.py)
PokerKit keeps dollar amounts as Decimal. `bet_added` was a float and the pot a
Decimal, so the first bet or raise in any hand with sub-dollar amounts raised
TypeError, and the ETL's per-hand `except` skipped the hand without reporting it.
Every NL25 and NL50 hand with a bet was lost (0.0% bets and raises in the store,
99% showdown), and most NL100 ones. Stakes from NL200 up use whole dollars and
were unaffected. Amounts are now converted to float once, in `_snapshot`.
Stores built before this date are wrong at low stakes and need re-parsing.

Re-parsed the full HandHQ set the same day: 21,605,332 hands kept of 21,606,087
(755 dropped, all "Unable to repair the hand history" from PokerKit, i.e. broken
in the source), against 12.9 million before. NL25 and NL50 now show 15-21% bets
and raises, in line with the higher stakes. 30 minutes on 10 cores.

### Dropped hands are recorded and fail the run
Every hand that won't replay goes to `qa/parse_errors.csv`; the run exits 2 if
more than `--max-error-rate` (default 0.5%) fail. Site is matched against the
path under the data folder only.

### Synthetic hands, recovery test, report
`simulate.py` writes dollar-denominated hands with rates planted per stake
(`config/planted.csv`). `tests/test_synthetic.py` checks the stat panel recovers
them; it fails on the code before this fix. `report.py` replaces the notebook
figures: one figure and one table, built only from `stats.py`. Tests moved to
pytest with a GitHub Actions check. The notebooks were removed.
