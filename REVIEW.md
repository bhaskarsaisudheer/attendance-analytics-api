# REVIEW.md

Defects found in the starter `app/main.py`, how to notice each one, and the fix.

## Helper functions

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | The grace period is compared against **whole minutes**, so a punch-in one second past the grace boundary reads as 0 minutes late. R2 says late only if the punch-in is *strictly more than* 10:00 after shift start. | Shift 09:30. `09:40:00` → 0 (correct), but `09:40:01` → **0, should be 10**; `10:15:59` → 45 (correct). | Compare elapsed **seconds** against 600 and floor the minutes: `if seconds <= 600: return 0; return seconds // 60`. Verified at 09:40:00→0, 09:40:01→10, 10:15:59→45. |
| 2 | `compute_late_minutes` | Takes `shift_start` as an `"HH:MM"` string and does `punch_in.replace(hour=…, minute=…)`. That builds a time in whatever zone `punch_in` carries and ignores the shift's date entirely, so a punch-in after midnight UTC is measured against the wrong day. R1 works in IST. | Punch in at 22:00 IST on an overnight shift (16:30 UTC). `replace` produces 22:00 **UTC** the same day, so lateness is measured six hours out. | Pass the shift start as a real aware instant: `shift_start_instant(attendance_day, employee)` converts the record's attendance date plus `shift_start` in IST to UTC, and the helper just subtracts two instants. |
| 3 | `compute_work_hours` | Uses Python's `round()`, which is **banker's rounding** and also fights float representation. R4 requires half-up. | 8.625 h: `round(8.625, 2)` → **8.62** in this case, because 8.625 is not exactly representable in binary. R4 wants 8.63. | `Decimal(seconds) / Decimal(3600)` then `.quantize(…, rounding=ROUND_HALF_UP)`. Exact, and the same helper serves R8. |
| 4 | `compute_overtime` | No 30-minute minimum. R3 only counts overtime once it reaches 30 minutes. | Punch out 10 minutes after shift end → **10**, should be 0. | `minutes = seconds // 60; return minutes if minutes >= 30 else 0`. Boundary checked: 18:59:59→0, 19:00:00→30. |
| 5 | `compute_overtime` | Places the shift end on `date_str`, the same day as the punch-in. An overnight shift ends on the **next** calendar day (R1). | Shift 22:00–06:00, punch in 21:55 and out 06:40 the next morning → overtime reads ~0 instead of 40. | `shift_end_instant()` adds 86,400 s when `shift_end <= shift_start`. |
| 6 | `compute_overtime` | `datetime.fromisoformat(date_str)` is **naive**, then `.replace(tzinfo=IST)`, while `punch_out` read from MongoDB is naive too — but the moment `tz_aware=True` is added (which R1 and `sample_seed.py` both require) the subtraction raises `TypeError`. | Punch-out on any overnight record crashes with "can't subtract offset-naive and offset-aware datetimes". | Both operands are aware UTC instants, built by `shift_end_instant()`. |

## Global

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 7 | `MongoClient(...)` | No `tz_aware=True`, so PyMongo returns **naive** datetimes. Every arithmetic helper then mixes naive and aware values, and the API has no reliable way to serialise an instant. | Any read-back of a seeded record followed by a calculation raises `TypeError`. `sample_seed.py` seeds with `tz_aware=True`, so the two halves of the app disagree. | `MongoClient(MONGO_URI, tz_aware=True)`. Naive input is still coerced to UTC in `to_ms()` so nothing depends on the client setting. |
| 8 | Startup | **No indexes are created at all.** The brief says the seeded database has only `_id`, and that creating the ones you need is your job. | `explain` shows `COLLSCAN` for every query; `/attendance` loads 100k documents. | `ensure_indexes()` runs in a FastAPI `lifespan` handler. It creates each index by keys and uniqueness and tolerates an equivalent index that already exists under a different name (`IndexOptionsConflict`), so it is idempotent against a database that already holds the grader's data. |
| 9 | `load_dotenv()` | **Not a defect.** `override` defaults to `False`, so a real environment variable wins over `.env` — which is exactly what the brief requires. | `MONGO_DB=other python app.py` still used `.env`? It does not. | Left as is. Listed here because it looks like a bug and is not one. |
| 10 | `create_employee` | No validation model: `emp_code` format, email, `HH:MM` shift times, `joined_on`, and `shift_start == shift_end` are all accepted. | `POST /employees {"emp_code": "X", "email": "nope", "shift_start": "25:00"}` returns **201**. | `EmployeeIn` with the contract's patterns and bounds, plus a model validator rejecting `shift_start == shift_end`. `extra="ignore"` so a client sending `created_at` or `work_hours` gets them dropped rather than a 422. |

## `/health`

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 11 | `health()` | Returns `{"status": "ok"}` unconditionally. The contract says 200 **only** when MongoDB answers, else 503, and the runner polls this before testing. | Stop MongoDB: `/health` still returns 200, so the runner starts testing against a dead database and every call fails with a 500. | `client.admin.command("ping")` inside the handler; `PyMongoError` becomes a 503. |

## `/employees`

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 12 | `list_employees` | `skip = page * page_size` — page 1 **skips the first page**. R10 says page starts at 1. | `GET /employees?page=1&page_size=3` returns employees 4–6; page 2 returns 7–9. | `(page - 1) * page_size`. |
| 13 | `list_employees` | `total = db.employees.count_documents({})` ignores the `department` filter, so `total` never matches the items. | `GET /employees?department=Engineering` on 1 of 6 employees reports `"total": 6`. | `count_documents(query)` with the same filter. |
| 14 | `list_employees` | No sort, so MongoDB returns natural order and pagination is not stable. | Two pages of the same filter overlap or skip records. | `.sort("emp_code", ASCENDING)`; the unique index supplies that order for free. |
| 15 | `list_employees` | No bounds on `page` / `page_size`. R10 caps `page_size` at 100 and starts `page` at 1. | `?page_size=100000` is accepted; `?page=0` is accepted. | `Query(ge=1)` and `Query(ge=1, le=100)`. |
| 16 | `create_employee` | Check-then-insert: `find_one` for the code, then `insert_one`. Two concurrent creates can both pass the check. | Fire 10 identical `POST /employees` in parallel: **more than one 201**. | A unique index on `employees.emp_code`, and `DuplicateKeyError` → 409. The index is the guard, not the pre-read. |

## `/attendance/punch-in`

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 17 | `punch_in` | `emp = db.employees.find_one(...)` with no check, then `emp["shift_start"]` → `KeyError` on an unknown employee. A 500 is always a failed test. | `POST /attendance/punch-in {"emp_code": "NOPE"}` → **500**. | `require_employee()` raises 404. |
| 18 | `punch_in` | `datetime.fromtimestamp(ms/1000)` uses the **server's local time zone**. On a UTC host this is six hours off IST, so `date` lands on the wrong day and the stored instant is the wrong instant. | Punch in at 09:00 IST on 2026-07-06 (03:30 UTC) with the server on UTC → `date` comes out **2026-07-05**. | `from_ms()` builds an aware UTC instant from epoch ms; `attendance_date()` converts to IST before taking the calendar date. |
| 19 | `punch_in` | No overnight-shift rule. R1: a punch-in **earlier than shift_end** belongs to the shift that started the previous day. | Shift 22:00–06:00, punch in 01:00 IST → `date` should be the previous day; the starter returns the current one. | `attendance_date()` checks `shift_end <= shift_start` and steps back one day when the IST time-of-day is before `shift_end`. |
| 20 | `punch_in` | `if db.attendance_logs.find_one(...)` then `insert_one` — check-then-insert. The contract explicitly says not to rely on it. | 10 parallel identical punch-ins → **more than one 201**. | Unique index on `(emp_code, date)`; `DuplicateKeyError` → 409. Verified: 12 concurrent requests give exactly one 201 and eleven 409s. |
| 21 | `punch_in` | `punched_at` is unvalidated, so seconds, floats and strings all pass. R1 and the schema require integer epoch ms, seconds-looking values 422. | `{"punched_at": 1784001720}` (seconds) → **201**, silently 1970. | `Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]`; `strict=True` rejects floats and strings. |
| 22 | `punch_in` | `status` accepts any string. Only `PRESENT`, `WFH`, `ON_DUTY` may be created here. | `{"status": "ABSENT"}` → 201, creating a record the contract says cannot be created by this endpoint. | `Literal["PRESENT", "WFH", "ON_DUTY"]`. |
| 23 | `punch_in` | `if body.punched_at else datetime.now()` — a falsy check, so `0` means "now". | `{"punched_at": 0}` silently uses the server clock. | `if millis is None` in `_parse_moment()`. |
| 24 | `punch_in` | Stores `ts` **untruncated**, then computes lateness from it. R1 says truncate to whole seconds before any calculation and store the truncated value. | Punch in at 09:40:00.900 against a 09:30 shift → lateness computed from `.900`. | `truncate_to_second()` applied in `_parse_moment()`, before storage and before any arithmetic. |
| 25 | `punch_in`, `list_attendance` | The response adds `doc["id"] = str(res.inserted_id)` / `str(d.pop("_id"))`. `_id` is internal and **must never** appear in a request or response; a record is addressed by `(emp_code, date)`. | Any record in a response carries an `id` the contract does not define. | `serialize_record()` builds the response from an explicit field list; `_id` is never read. |

## `/attendance` (listing)

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 26 | `list_attendance` | `docs = list(db.attendance_logs.find(q))` pulls **the whole collection into memory** and sorts in Python. | With ~100k records the response takes seconds and the process holds every document; the brief calls this out explicitly. | `.find(q).sort([("date", -1), ("emp_code", 1)]).skip(…).limit(…)` — the database does the sorting and returns only the page. An index on `(date desc, emp_code asc)` makes it an indexed scan with a skip. |
| 27 | `list_attendance` | Sorts only by `date`, descending. The contract is date **descending, then `emp_code` ascending**, so equal dates have no defined order. | Two records on the same date can swap between pages. | Compound sort, matching the compound index. |
| 28 | `list_attendance` | No check that `date_from <= date_to`. | `?date_from=2026-07-20&date_to=2026-07-01` returns 200 with an empty list instead of 422. | `check_date_range()` → 422. |
| 29 | `list_attendance` | Returns stored values raw, so instants come back as `datetime` objects and missing `history` / `half_day` legacy fields stay missing. | A response body containing `"2026-07-06T03:58:00+00:00"` instead of `1783310280000`; a legacy record without a `half_day` key, which the schema marks required. | `serialize_record()` converts instants to epoch ms and fills `history: []`, `half_day: false`, `late_minutes: 0`, `overtime_minutes: 0` when absent. |
| 30 | `list_attendance` | `total = len(docs)` is computed after loading everything, and no page bounds are enforced. | `?page_size=100000` is accepted. | `count_documents(query)` plus `Query(ge=1, le=100)`. |
| 31 | `list_attendance` | `status` is unvalidated, so `?status=TYPO` silently returns every record. | `?status=WORKING` → 200 with all records. | `Literal` of the five statuses → 422. |

## Considered and **not** a defect

- **`load_dotenv()` without `override=True`.** Correct as written: real environment variables take priority over `.env`, which is what the brief demands.
- **`total` in `list_employees` counting the whole collection** *would* be a bug — but only because the filter is applied at all; the fix is the same either way, and it is listed as #13.
- **`punch_in` returning the stored document with a leftover `punch_in` as `datetime`** is covered by #25 (serialisation), not a separate defect.
- **`sample_seed.py` using `tz_aware=True` while `main.py` did not** is a symptom of #1/#7, not a separate finding; the seed script is correct.
- **No `Dockerfile` or `.env` in the repo** is a submission rule, not a code defect.