#!/usr/bin/env python3
"""Quick validation checks on a freshly built poker.db."""
import sys
import duckdb

db_path = sys.argv[1] if len(sys.argv) > 1 else "out/poker.db"
con = duckdb.connect(db_path, read_only=True)

print("=== 1. Duplicate (hand_id, action_idx) groups ===")
r = con.execute("""
    SELECT COUNT(*) AS dup_groups FROM (
        SELECT hand_id, action_idx
        FROM actions
        GROUP BY hand_id, action_idx
        HAVING COUNT(*) > 1
    )
""").fetchone()
print(f"  duplicate groups: {r[0]:,}  {'OK' if r[0] == 0 else 'FAIL'}")

print()
print("=== 2. NULL bet_pct_pot on bet/raise actions by site ===")
rows = con.execute("""
    SELECT site,
           COUNT(*) AS total,
           SUM(CASE WHEN bet_pct_pot IS NULL THEN 1 ELSE 0 END) AS null_ct,
           ROUND(100.0 * SUM(CASE WHEN bet_pct_pot IS NULL THEN 1 ELSE 0 END) / COUNT(*), 2) AS pct_null
    FROM actions
    WHERE action_type IN ('bet', 'raise')
    GROUP BY site
    ORDER BY pct_null DESC
""").fetchall()
for r in rows:
    status = "OK" if r[3] < 5 else "WARN" if r[3] < 20 else "FAIL"
    print(f"  {r[0]:15s}: {r[1]:>10,} cbr | {r[2]:>8,} NULL | {r[3]:5.1f}% null  {status}")

print()
print("=== 3. Impossible fold rows (fold + faced_bet=false) ===")
r = con.execute("""
    SELECT COUNT(*) FROM actions
    WHERE action_type = 'fold' AND faced_bet = false
""").fetchone()
print(f"  impossible fold rows: {r[0]:,}  {'OK' if r[0] == 0 else 'FAIL'}")

print()
print("=== 4. Sample hand_ids (should contain relative path) ===")
rows = con.execute("""
    SELECT DISTINCT hand_id FROM hands
    WHERE site = 'AbsolutePoker'
    LIMIT 5
""").fetchall()
for r in rows:
    print(f"  {r[0]}")

con.close()
