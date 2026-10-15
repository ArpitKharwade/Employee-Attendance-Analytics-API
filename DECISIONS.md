# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (identity, 409, `$lookup` target); `(department, emp_code)` for the
   sorted department list; `(department, joined_on)` and `(joined_on)` for headcount. `attendance_logs`: unique
   `(emp_code, date)` (one record per day, per-employee queries); `(date -1, emp_code)` (list order, month scans);
   `(status, date -1, emp_code)` (status filter); `(emp_code, punch_in -1)` (latest open punch-in). Rejected: an index
   on `late_minutes`; the date range already narrows the scan and most rows would still qualify.

2. **Punch-in race.** Both requests build the same document and call `insert_one`. The unique `(emp_code, date)` index
   lets one succeed (201); the other gets `DuplicateKeyError`, which I turn into 409. There is no "check first" step.

3. **Ties.** I rank with `$rank` over the totals *before* applying `limit`, then keep `rank <= limit`. Everyone tied at
   the cutoff is returned, so the list can be longer than `limit`. Rows are ordered by total, then `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (joined on or before month end) and left-joins the logs with
   `$lookup`, so an employee with no logs still produces a row and is counted once.

5. **100x the data.** I would keep pre-computed daily roll-ups per employee, with `department` copied onto them, so
   analytics read a few rows instead of scanning and joining raw logs.
