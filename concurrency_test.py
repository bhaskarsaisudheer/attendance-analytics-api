"""Concurrency checks: the brief says two simultaneous punch-ins must yield exactly
one 201, and two simultaneous punch-outs exactly one 200. Runs both against a live
server with real threads. Usage: python concurrency_test.py [base_url]"""
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8081"
IST = timezone(timedelta(hours=5, minutes=30))


def call(method, path, body=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def ms_ist(y, mo, d, h, mi, s=0):
    return int((datetime(y, mo, d, h, mi, s, tzinfo=IST) -
                datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() * 1000)


def run_parallel(fn, n):
    results = [None] * n
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()  # release all threads at the same instant
        results[i] = fn(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


print("=" * 70, "\nCONCURRENCY\n", "=" * 70)
passed = True

# --- 12 simultaneous punch-ins ---------------------------------------------
N = 12
call("POST", "/employees", {"emp_code": "EMP9100", "name": "Race In", "email": "racein@example.com",
                            "department": "Ops", "shift_start": "09:30", "shift_end": "18:30",
                            "joined_on": "2026-01-01"})
res = run_parallel(lambda _: call("POST", "/attendance/punch-in",
                                 {"emp_code": "EMP9100", "punched_at": ms_ist(2026, 7, 9, 9, 30, 0)}), N)
codes = [r[0] for r in res]
created, conflicts = codes.count(201), codes.count(409)
ok = created == 1 and conflicts == N - 1 and set(codes) <= {201, 409}
passed &= ok
print(f"{'PASS' if ok else 'FAIL'}  {N} simultaneous punch-ins -> exactly one 201, {N-1} 409s  {sorted(codes)}")

# --- 12 simultaneous punch-outs --------------------------------------------
res = run_parallel(lambda _: call("POST", "/attendance/punch-out",
                                 {"emp_code": "EMP9100", "punched_at": ms_ist(2026, 7, 9, 18, 45, 0)}), N)
codes = [r[0] for r in res]
ok200, conflict = codes.count(200), codes.count(409)
ok = ok200 == 1 and conflict == N - 1 and set(codes) <= {200, 409}
passed &= ok
print(f"{'PASS' if ok else 'FAIL'}  {N} simultaneous punch-outs -> exactly one 200, {N-1} 409s  {sorted(codes)}")

# the surviving record must hold exactly one punch-out and one audit-free history.
# Punch-out at 18:45 against an 18:30 shift end is 15 minutes, below R3's 30-minute
# threshold, so overtime_minutes is correctly 0.
st, rec = call("GET", "/attendance?emp_code=EMP9100&date_from=2026-07-09&date_to=2026-07-09")
rec = rec["items"][0] if st == 200 and rec["items"] else {}
ok = (len(rec.get("history", [])) == 0 and rec.get("work_hours") == 9.25
      and rec.get("overtime_minutes") == 0 and rec.get("half_day") is False)
passed &= ok
print(f"{'PASS' if ok else 'FAIL'}  winning record is intact: work_hours={rec.get('work_hours')} "
      f"overtime={rec.get('overtime_minutes')} history={len(rec.get('history', []))}")

# --- 12 simultaneous duplicate employee creates ------------------------------
res = run_parallel(lambda _: call("POST", "/employees",
                                 {"emp_code": "EMP9101", "name": "Race Create",
                                  "email": "racecreate@example.com", "department": "Ops",
                                  "joined_on": "2026-01-01"}), N)
codes = [r[0] for r in res]
ok = codes.count(201) == 1 and set(codes) <= {201, 409}
passed &= ok
print(f"{'PASS' if ok else 'FAIL'}  {N} simultaneous employee creates -> exactly one 201  {sorted(codes)}")

# --- 10 simultaneous conflicting corrections on one record -------------------
call("POST", "/employees", {"emp_code": "EMP9102", "name": "Race Patch", "email": "racepatch@example.com",
                            "department": "Ops", "shift_start": "09:30", "shift_end": "18:30",
                            "joined_on": "2026-01-01"})
call("POST", "/attendance/punch-in", {"emp_code": "EMP9102", "punched_at": ms_ist(2026, 7, 10, 9, 30, 0)})
res = run_parallel(lambda i: call("PATCH", "/attendance/EMP9102/2026-07-10",
                                  {"punch_in": ms_ist(2026, 7, 10, 10, i),
                                   "reason": f"Concurrent correction number {i}",
                                   "regularized_by": f"hr{i}@example.com"}), 10)
codes = [r[0] for r in res]
ok = set(codes) <= {200, 409} and codes.count(200) + codes.count(409) == 10
passed &= ok
st, rec = call("GET", "/attendance?emp_code=EMP9102&date_from=2026-07-10&date_to=2026-07-10")
hist = rec["items"][0]["history"] if st == 200 and rec["items"] else []
ok2 = len(hist) == codes.count(200)
passed &= ok2
print(f"{'PASS' if ok and ok2 else 'FAIL'}  10 simultaneous corrections -> {codes.count(200)} 200s, "
      f"{codes.count(409)} 409s, {len(hist)} history entries (no lost update)")

print("\n" + "=" * 70)
print("ALL CONCURRENCY CHECKS PASSED" if passed else "SOME CONCURRENCY CHECKS FAILED")
sys.exit(0 if passed else 1)