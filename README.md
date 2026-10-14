# Employee Attendance & Analytics API

FastAPI + MongoDB 7.0. Attendance punches, corrections, and four analytics endpoints
over one attendance collection.

## Run it

Python 3.11+, MongoDB 7.0 reachable on `localhost:27017`.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env          # optional; the defaults below already work locally

uvicorn app.main:app --reload
```

Then open <http://127.0.0.1:8000/docs>. Startup creates the indexes if they are
missing and is safe to run repeatedly against a database that already has data.

Configuration is read from the environment, with `.env` as the fallback — a real
environment variable always wins:

| Variable | Default |
|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` |
| `MONGO_DB` | `attendance_db` |

To point at another database without touching `.env`:

```bash
MONGO_DB=my_db uvicorn app.main:app
```

Seed data is optional — `python sample_seed.py` loads the fixtures from the brief.

## Layout

| File | What it is |
|---|---|
| `app/main.py` | The whole application: config, index setup, models, endpoints, and the aggregation pipelines. |
| `requirements.txt` | Runtime dependencies. |
| `sample_seed.py` | The brief's seed script, unchanged. |
| `REVIEW.md` | Every defect in the starter code, its symptom, and the fix. |
| `DECISIONS.md` | Plain-language answers to the five design questions. |
| `smoke_test.py` | 100 end-to-end checks of every endpoint and rule boundary. |
| `concurrency_test.py` | The race conditions R asks about, run with real threads. |
| `scale_test.py` | Times every endpoint over a 100,000-log dataset. |

The three test scripts are mine, not the brief's. They need the app already
running (except `scale_test.py`, which seeds and starts its own server):

```bash
python smoke_test.py http://127.0.0.1:8000
python concurrency_test.py http://127.0.0.1:8000
python scale_test.py --logs 100000     # separate database, dropped after
```

## Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | 200 only when MongoDB answers, else 503. |
| POST | `/employees` | 201; 409 if `emp_code` exists. |
| GET | `/employees` | `department`, `page`, `page_size` (max 100). |
| POST | `/attendance/punch-in` | 201; 409 if already punched in for that date. |
| POST | `/attendance/punch-out` | 200; 404 with no punch-in, 422 if before it. |
| GET | `/attendance` | `emp_code`, `date_from`, `date_to`, `status`, `page`, `page_size`. |
| PATCH | `/attendance/{emp_code}/{date}` | Appends to `history`, recomputes derived fields. |
| GET | `/analytics/employees/{emp_code}/monthly` | `month=YYYY-MM`. |
| GET | `/analytics/departments/summary` | `month`, optional `department`. |
| GET | `/analytics/leaderboard/late` | `month`, `limit` (max 50), optional `department`. |
| GET | `/analytics/departments/{department}/trend` | `from`, `to`; range capped at 92 days. |
| GET | `/admin/explain/{endpoint}` | Runs the endpoint's own query with `executionStats`. |

Instants go in and come back as **integer epoch milliseconds**. Dates are plain
`YYYY-MM-DD` strings. Attendance arithmetic is done in **IST**; instants are stored
as UTC BSON dates.

## Notes on behaviour

- **R1** truncates every instant to whole seconds before storing or calculating. An
  overnight shift (`shift_end <= shift_start`) owns a punch-in that lands before
  `shift_end`, so that punch-in belongs to the previous day's record.
- **R2** the 10-minute grace period is exclusive: 10:00 late is not late, 10:01 is.
- **R3** overtime only counts from 30 minutes.
- **R4** hours are `seconds / 3600`, rounded **half-up** to two places with `Decimal`
  (Python's built-in `round()` is banker's rounding and would turn 8.625 into 8.62).
- **R5** a half day is under 4.50 hours after rounding, and counts 0.5.
- **R6** `PRESENT`, `WFH` and `ON_DUTY` count as present.
- **R7** working days are Monday to Friday, counted from `joined_on`.
- **R9** headcount includes everyone who had joined by the end of the period,
  including people with no attendance records at all.
- **R10** `page` starts at 1, `page_size` caps at 100, and `total` reflects the
  active filters.

Concurrency is enforced by the database, not by a read-then-write check: a unique
index on `attendance_logs(emp_code, date)` makes a second punch-in a 409, and
`find_one_and_update` with `punch_out: None` in the filter lets exactly one punch-out
win. Corrections use optimistic locking on the record's history length, so two
concurrent patches produce two history entries rather than one silently overwriting
the other.

## Verify it

```bash
python sample_seed.py
curl "http://127.0.0.1:8000/admin/explain/attendance_list?page=1&page_size=20"
```

Each named endpoint returns the plan MongoDB chose, with `executionStats`. The five
worth checking: `attendance_list`, `employee_monthly`, `department_summary`,
`late_leaderboard`, `department_trend`.

## What I verified

Run against MongoDB 7.0.43 with the brief's seed data, plus a 100,000-record
dataset:

| Check | Result |
|---|---|
| `smoke_test.py` | 100/100 pass — every endpoint, both error branches, and the R1–R10 boundaries |
| `concurrency_test.py` | 12 parallel punch-ins → 1×201 + 11×409; 12 parallel punch-outs → 1×200 + 11×409; parallel corrections keep every history entry |
| `scale_test.py` at 100k logs | All five explain plans IXSCAN with no COLLSCAN; slowest endpoint 0.27 s |
| Health from cold start | `/health` returns 200 well inside the 20 s budget |

The awkward cases the brief asks for are in the tests as their own checks:
overnight shifts, a mid-month joiner, an employee with no logs at all, weekend
records, a missing weekday, and the exact R2/R3 grace boundaries (`09:40:00` vs
`09:40:01`, `18:59:59` vs `19:00:00`).

## Known gaps

Nothing is missing from the brief. Two honest caveats:

- **Analytics are verified on my own generated data, not the hidden set.** The
  numbers are self-consistent and the rules are applied literally, but a dataset
  shaped differently from my generator could still surface an interpretation I got
  wrong. `attendance_pct` on a month with no working days returns `null`, and the
  trend's `attendance_rate` is `null` on non-working days — both are my reading of
  "rate" rather than an explicit line in the contract.
- **`README.md` documents `MONGO_DB` but the grader injects its own values**, so
  the defaults here only matter for local runs.
