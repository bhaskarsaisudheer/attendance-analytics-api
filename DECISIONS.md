# DECISIONS.md

**1. Indexes.** Unique on employees' `emp_code` and logs' `(emp_code, date)` — the latter is punch-in's race guard and the listing order. Plus `emp_code+punch_in` (punch-out's latest record), `date desc+emp_code` (sort and analytics scans), `status+date` (filtered listings), and `department+joined_on` for headcount. I skipped `work_hours`: nothing sorts by it.

**2. Punch-in race.** Both requests build the same document and call `insert_one`. The unique index is the only arbiter: the server serialises them, one succeeds, the other raises `DuplicateKeyError`, which I turn into 409 — one 201. No `find_one` first, since a read-then-write lets both see nothing.

**3. Ties.** `$rank` in `$setWindowFields` gives standard competition ranking: tied totals share a rank, the next is skipped (1, 2, 2, 4). I match `rank <= limit`, so a tie on the cutoff returns both — `limit=3` can return 4 rows.

**4. Headcount.** The pipeline starts from logs and `$unionWith`s a branch grouping employees by department. A department whose only staff have no logs still appears, with zeroed counters and a real headcount. Filtering `joined_on <= month end` drops those not yet joined.

**5. At 100× the data.** The summary and leaderboard hurt most, since both group a full month. The summary becomes a daily rollup, the leaderboard a per-employee monthly one, and logs get sharded by date.
