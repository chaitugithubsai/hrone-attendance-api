# REVIEW.md

DRAFT: rewrite each row in your own words after you have reproduced the problem yourself.

| # | Where | What is wrong | How you'd notice it | How it was fixed |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Builds the shift start from the punch-in's own (naive, server-zone) date, not IST. Wrong day/zone, overnight shifts ignored. | Run the server in UTC: a 09:50 IST punch-in is 04:20 UTC, so "late" is computed against 09:30 UTC. | `shift_context()` converts to IST and handles overnight shifts. |
| 2 | `compute_late_minutes` | Floors minutes first, then tests `> 10`: 09:40:30 gives 10, so not late, but it is more than 10:00 after start. Microseconds not truncated. | Punch in at 09:40:01 and expect `late_minutes = 10`. | Compare in whole seconds (`> 600`), then floor. |
| 3 | `compute_work_hours` | Python `round()` on a float is not half-up (R4); no truncation to whole seconds. | A duration whose hours end in exactly `.xx5`. | `Decimal` + `ROUND_HALF_UP` on truncated instants. |
| 4 | `compute_overtime` | Uses `date_str` for the shift end, so overnight shifts are wrong. No 30-minute minimum (R3). Subtracts an aware datetime from a naive one, which raises `TypeError`. | Overnight punch-out; 10 minutes of overtime returns 10 instead of 0. | `derive()` uses the IST-aware shift end and the `>= 30 min` rule. |
| 5 | `create_employee` | Check-then-insert race with no unique index, so duplicates are possible. No validation (emp_code format, HH:MM, `shift_start == shift_end`, bad `joined_on`). `created_at` is naive local time and returned as a raw datetime instead of epoch ms. | Two parallel POSTs both return 201. | Unique index + `DuplicateKeyError` -> 409; pydantic constraints; UTC ms. |
| 6 | `list_employees` | `skip = page * page_size` skips the first page. `total` ignores the department filter. No sort, so paging is unstable. No bounds on `page` / `page_size`. | `page=1` returns page 2's rows; filter by department and compare `total`. | `(page-1)*page_size`, filtered count, sort by `emp_code`, `Query(ge, le)`. |
| 7 | `punch_in` | Unknown employee -> `emp` is `None` -> 500 instead of 404. | POST an unknown `emp_code`. | Explicit 404. |
| 8 | `punch_in` | Date comes from `datetime.fromtimestamp` in the server's time zone, not IST. `if body.punched_at` treats 0 as missing. Not truncated to seconds. No overnight rule (R1). | Run in UTC and punch in at 00:30 IST. | `ms_to_dt` + `shift_context`. |
| 9 | `punch_in` | Check-then-insert race: two simultaneous requests can both return 201. | Fire parallel requests. | Unique index on `(emp_code, date)`; `DuplicateKeyError` -> 409. |
| 10 | `punch_in` | `status` is an unvalidated string (ABSENT/LEAVE/garbage accepted). Response leaks `id` (ObjectId) and raw datetimes. | Send `"status": "ABSENT"`. | `Literal` status; `record_out()` returns epoch ms and no id. |
| 11 | `list_attendance` | Loads the whole collection into memory, sorts and paginates in Python. Sort has no `emp_code` tie-break. No index. No date validation; `date_from > date_to` is not rejected. Legacy documents (missing fields) are returned raw. | 100k records; `date_from=2026-07-10&date_to=2026-07-01`. | DB-side filter, sort, skip, limit; indexes; 422 on bad dates; `record_out()` defaults. |
| 12 | `health` | Always returns ok even when MongoDB is down. | Stop MongoDB, call `/health`. | `ping`, 503 on failure. |
| 13 | Client setup | `MongoClient` returns naive datetimes. No indexes at startup. | Datetime arithmetic with aware values raises `TypeError`. | `tz_aware=True`; `ensure_indexes()` in the lifespan hook. |

Looked at and decided not to change:
- Sync `def` endpoints with sync PyMongo are fine: FastAPI runs them in a thread pool and `MongoClient` is thread-safe.
- `load_dotenv()` does not override real environment variables, which is what the contract needs.
