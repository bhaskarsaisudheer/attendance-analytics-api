"""Scale check: the brief says the queries must hold at ~100x the sample data.
Seeds a large dataset (default 40k attendance logs over 200 employees, about
1000x the sample), rebuilds indexes the way the app does, then times every
endpoint and asserts the explain plans stay index-backed.

Usage: python scale_test.py [base_url] [--logs N] [--employees N]
"""
import datetime
import os
import random
import sys
import time

from pymongo import ASCENDING, DESCENDING, MongoClient

BASE = "http://127.0.0.1:8083"
LOGS = 40_000
STAFF = 200
DB_NAME = "attendance_scale"
if "--logs" in sys.argv:
    LOGS = int(sys.argv[sys.argv.index("--logs") + 1])

# MONGO_DB must be set BEFORE app.main is imported: the module resolves the
# database at import time, and the app's own index builder uses that handle.
os.environ["MONGO_URI"] = "mongodb://localhost:27017"
os.environ["MONGO_DB"] = DB_NAME

sys.path.insert(0, ".")
from app.main import ensure_indexes  # noqa: E402  the app's own index builder

random.seed(7)
client = MongoClient("mongodb://localhost:27017", tz_aware=True)
db = client[DB_NAME]

DEPARTMENTS = ["Engineering", "Sales", "Support", "Finance", "Operations"]
print(f"Seeding {LOGS} attendance logs for {STAFF} employees...")

db.employees.delete_many({})
db.attendance_logs.delete_many({})

base_day = datetime.date(2026, 1, 1)
employees = []
for i in range(STAFF):
    shift_start = "09:30"
    shift_end = "18:30"
    if i % 11 == 0:  # some overnight shifts exercise the R1 path
        shift_start, shift_end = "22:00", "06:00"
    employees.append({
        "emp_code": f"EMP{10000 + i}",
        "name": f"Employee {i}",
        "email": f"emp{i}@example.com",
        "department": DEPARTMENTS[i % len(DEPARTMENTS)],
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": (base_day - datetime.timedelta(days=random.randint(0, 40))).isoformat(),
        "created_at": datetime.datetime(2025, 12, 1, tzinfo=datetime.timezone.utc),
    })
db.employees.insert_many(employees)

STATUSES = ["PRESENT"] * 8 + ["WFH", "ON_DUTY", "LEAVE", "ABSENT"]
# Walk day-by-day x employee so every (emp_code, date) pair is generated at most
# once; the unique (emp_code, date) index the app creates would reject collisions.
docs = []
day_count = max(1, -(-LOGS // STAFF)) + 1
for emp in employees:
        overnight = emp["shift_start"] > emp["shift_end"]
        for day_offset in range(day_count):
            if len(docs) >= LOGS:
                break
            day = base_day + datetime.timedelta(days=day_offset)
            if random.random() < 0.15:      # a few gaps in the month
                continue
            status = random.choice(STATUSES)
            # Punch at a shift-relative hour in IST. An overnight punch-in that lands
            # before shift_end rolls back to the previous attendance day (R1), so keep
            # those punches late in the day to stay inside this employee's own day.
            ist_hour = random.randint(8, 11) if not overnight else random.randint(20, 23)
            in_ist = datetime.datetime(day.year, day.month, day.day, ist_hour, random.randint(0, 59),
                                       tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
            punch_in = in_ist.astimezone(datetime.timezone.utc).replace(microsecond=0)
            has_punch = status in ("PRESENT", "WFH", "ON_DUTY")
            punch_out = punch_in + datetime.timedelta(hours=random.uniform(3, 11)) if has_punch else None
            work_hours = None
            if punch_out:
                seconds = int((punch_out - punch_in).total_seconds())
                work_hours = round(seconds / 3600, 2)
            late = random.choice([0, 0, 0, 5, 12, 25, 47]) if has_punch else 0
            docs.append({
                "emp_code": emp["emp_code"], "date": day.isoformat(), "status": status,
                "punch_in": punch_in if has_punch else None, "punch_out": punch_out,
                "work_hours": work_hours, "late_minutes": late,
                "overtime_minutes": random.choice([0, 0, 35, 60]) if punch_out else 0,
                "half_day": bool(work_hours and work_hours < 4.5), "history": [],
            })
        if len(docs) >= LOGS:
            break
db.attendance_logs.insert_many(docs, ordered=False)
print(f"Inserted {len(docs)} logs.")

t0 = time.time()
ensure_indexes()
print(f"Index build: {time.time() - t0:.1f}s")
print("Indexes:", json_ind := ", ".join(f"{c}: {[i for i in db[c].list_indexes() if i['name'] != '_id_']}"
                                          for c in ("employees", "attendance_logs")))

# --- launch the app against the scale database --------------------------------
import threading  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

import uvicorn  # noqa: E402

from app import main as app_main  # noqa: E402

server = uvicorn.Server(uvicorn.Config(app_main.app, host="127.0.0.1", port=8083, log_level="error"))
threading.Thread(target=server.run, daemon=True).start()
for _ in range(60):
    try:
        urllib.request.urlopen(BASE + "/health", timeout=2)
        break
    except Exception:
        time.sleep(0.5)
print(f"Server up against {LOGS} logs. Indexes now: "
      + ", ".join(f"{c}=[{len([i for i in db[c].list_indexes() if i['name'] != '_id_'])}]"
                  for c in ("employees", "attendance_logs")) + "\n")

import json as _json  # noqa: E402


def timed(path, budget=5.0):
    start = time.time()
    try:
        req = urllib.request.Request(BASE + path, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            payload = _json.loads(r.read() or b"null")
        elapsed = time.time() - start
        return r.status, elapsed, payload
    except urllib.error.HTTPError as e:
        return e.code, time.time() - start, _json.loads(e.read() or b"null")


print(f"{'endpoint':<62} {'code':>5} {'seconds':>9}  verdict")
print("-" * 95)
cases = [
    ("/health", 5.0),
    ("/employees?page=1&page_size=50", 5.0),
    ("/employees?department=Engineering&page=1&page_size=100", 5.0),
    ("/attendance?page=1&page_size=100", 5.0),
    ("/attendance?page=40&page_size=100", 5.0),
    ("/attendance?emp_code=EMP10000&date_from=2026-03-01&date_to=2026-03-31", 5.0),
    ("/attendance?status=LEAVE&date_from=2026-02-01&date_to=2026-02-28&page=1&page_size=50", 5.0),
    ("/analytics/employees/EMP10000/monthly?month=2026-03", 8.0),
    ("/analytics/departments/summary?month=2026-03", 8.0),
    ("/analytics/departments/summary?month=2026-03&department=Engineering", 8.0),
    ("/analytics/leaderboard/late?month=2026-03&limit=10", 8.0),
    ("/analytics/leaderboard/late?month=2026-03&limit=50&department=Sales", 8.0),
    ("/analytics/departments/Engineering/trend?from=2026-03-01&to=2026-03-31", 10.0),
    ("/analytics/departments/Sales/trend?from=2026-01-01&to=2026-03-31", 12.0),
]
worst = 0.0
failures = []
for path, budget in cases:
    code, elapsed, body = timed(path)
    worst = max(worst, elapsed)
    good = code == 200 and elapsed < budget
    if not good:
        failures.append((path, code, elapsed, budget))
    print(f"{path[:60]:<62} {code:>5} {elapsed:>8.3f}s  {'ok' if good else 'TOO SLOW / WRONG CODE'}")

# deep pagination + explain sanity
code, elapsed, body = timed("/attendance?page=200&page_size=100")
print(f"{'/attendance?page=200&page_size=100 (deep skip)':<62} {code:>5} {elapsed:>8.3f}s")
if code != 200 or elapsed > 5:
    failures.append(("deep pagination", code, elapsed, 5))

print("\nExplain plans (must not COLLSCAN):")
for ep, q in [("attendance_list", "page=1&page_size=100"),
              ("employee_monthly", "emp_code=EMP10000&month=2026-03"),
              ("department_summary", "month=2026-03"),
              ("late_leaderboard", "month=2026-03"),
              ("department_trend", "department=Engineering&from=2026-03-01&to=2026-03-31")]:
    code, elapsed, body = timed(f"/admin/explain/{ep}?{q}")
    blob = _json.dumps(body)
    scan = "COLLSCAN" in blob
    docs_returned = body.get("explain", {}).get("executionStats", {}) if isinstance(body, dict) else {}
    if scan:
        failures.append((f"explain {ep}", code, elapsed, 0))
    print(f"  {ep:<22} {'COLLSCAN!' if scan else 'indexed'}  ({elapsed:.2f}s)")

print("\n" + "=" * 95)
if failures:
    print("FAILURES:")
    for f in failures:
        print("  -", f)
else:
    print(f"ALL SCALE CHECKS PASSED. Slowest endpoint: {worst:.3f}s over {LOGS} logs.")
server.should_exit = True
sys.exit(1 if failures else 0)