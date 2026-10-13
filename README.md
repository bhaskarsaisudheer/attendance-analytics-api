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
