# DECISIONS.md

DRAFT: rewrite in your own words once you understand each point.

1. **Indexes.** Unique `(emp_code, date)` on attendance: one record per day and the join key. `(date desc, emp_code)` for list order and month scans. `(emp_code, punch_in desc)` for punch-out. `(status, date, emp_code)` for the status filter. On employees: unique `emp_code`, plus `(department, emp_code)`, `(department, joined_on)` and `joined_on`. Rejected: a single-field `emp_code` index, because the compound one already covers it.
2. **Punch-in race.** Both requests try `insert_one`. The unique index lets exactly one succeed; the other raises `DuplicateKeyError`, which I return as 409. There is no check-then-insert.
3. **Ties.** `$setWindowFields` with `$rank` gives ties the same rank and skips the next. I filter `rank <= limit` after ranking, so tied employees at the cutoff are all returned, and the result can have more than `limit` rows.
4. **Headcount.** The summary starts from `employees`, filtered by `joined_on <= month end`, then `$lookup`s logs. Employees with no logs get an empty array and still count once.
5. **100x data.** Pre-aggregate monthly stats into a summary collection (or materialized view) updated on each punch, so analytics don't scan raw logs.
