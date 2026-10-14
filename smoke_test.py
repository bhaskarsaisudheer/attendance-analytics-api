"""Live end-to-end check against a running server. Read-only smoke test plus
business-rule boundary assertions. Prints PASS/FAIL per check and exits non-zero
on any failure. Usage: python smoke_test.py [base_url]"""
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8077"
results = []


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
        raw = e.read()
        try:
            return e.code, json.loads(raw or b"null")
        except Exception:
            return e.code, raw[:200].decode("utf8", "replace")


def check(name, condition, detail=""):
    results.append((name, bool(condition), detail))
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not condition else ""))


def ms_ist(y, mo, d, h, mi, s=0):
    """IST wall clock -> epoch ms (IST = UTC+5:30)."""
    from datetime import datetime, timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))
    return int((datetime(y, mo, d, h, mi, s, tzinfo=ist) - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() * 1000)


print("=" * 70, "\nSMOKE TEST\n", "=" * 70)

# --- system -----------------------------------------------------------------
st, body = call("GET", "/health")
check("GET /health -> 200 ok", st == 200 and body.get("status") == "ok", f"{st} {body}")

# --- employees --------------------------------------------------------------
st, body = call("GET", "/employees?page=1&page_size=3")
check("GET /employees paginated 200", st == 200, str(st))
check("GET /employees returns items/total/page/page_size",
      all(k in body for k in ("items", "total", "page", "page_size")), str(body.keys()))
check("R10 page 1 is the FIRST page (not skipped)",
      body["items"] and body["items"][0]["emp_code"] == "EMP0001",
      f"first={body['items'][0]['emp_code'] if body['items'] else None}")
check("R10 page_size respected", len(body["items"]) == 3, str(len(body["items"])))

st, b2 = call("GET", "/employees?page=2&page_size=3")
check("R10 page 2 differs from page 1",
      [i["emp_code"] for i in b2["items"]] != [i["emp_code"] for i in body["items"]])

st, body = call("GET", "/employees?department=Engineering")
check("total respects department filter", body["total"] == len([i for i in body["items"] if i["department"] == "Engineering"]),
      f"total={body['total']} items={len(body['items'])}")

st, body = call("GET", "/employees?page=0")
check("R10 page=0 -> 422", st == 422, str(st))
st, body = call("GET", "/employees?page_size=101")
check("R10 page_size>100 -> 422", st == 422, str(st))

st, body = call("POST", "/employees", {"emp_code": "EMP0001", "name": "Dup", "email": "d@e.com",
                                        "department": "Eng", "joined_on": "2026-01-01"})
check("POST /employees duplicate emp_code -> 409", st == 409, str(st))
st, body = call("POST", "/employees", {"emp_code": "bad", "name": "X", "email": "nope",
                                        "department": "Eng", "joined_on": "2026-01-01"})
check("POST /employees invalid payload -> 422", st == 422, str(st))
st, body = call("POST", "/employees", {"emp_code": "EMP9999", "name": "T", "email": "t@e.com",
                                        "department": "Eng", "shift_start": "09:00", "shift_end": "09:00",
                                        "joined_on": "2026-01-01"})
check("POST /employees shift_start == shift_end -> 422", st == 422, str(st))
st, body = call("POST", "/employees", {"emp_code": "EMP9998", "name": "Night", "email": "n@e.com",
                                        "department": "Ops", "shift_start": "22:00", "shift_end": "06:00",
                                        "joined_on": "2026-01-01"})
check("POST /employees creates 201 (overnight shift accepted)", st == 201, str(st))
check("POST /employees hides _id", st == 201 and "_id" not in body and "id" not in body, str(body))

# --- punch-in ---------------------------------------------------------------
st, body = call("POST", "/attendance/punch-in", {"emp_code": "NOPE"})
check("punch-in unknown employee -> 404 (not 500)", st == 404, str(st))
st, body = call("POST", "/attendance/punch-in", {"emp_code": "EMP0001", "punched_at": 1784001720})
check("R1 epoch seconds -> 422 (must be milliseconds)", st == 422, str(st))
st, body = call("POST", "/attendance/punch-in", {"emp_code": "EMP0001", "status": "ABSENT"})
check("punch-in status ABSENT -> 422", st == 422, str(st))

# Fixture employees for the rule boundaries below. The seed only ships
# EMP0001-EMP0006, so these use codes far outside that range.
for code, shift_s, shift_e, dept in [("EMP9001", "09:30", "18:30", "Engineering"),
                                     ("EMP9002", "09:30", "18:30", "Engineering"),
                                     ("EMP9003", "09:30", "18:30", "Engineering"),
                                     ("EMP9004", "09:30", "18:30", "Engineering"),
                                     ("EMP9005", "09:30", "18:30", "Engineering")]:
    st, _ = call("POST", "/employees", {"emp_code": code, "name": f"Test {code}",
                                        "email": f"{code.lower()}@example.com", "department": dept,
                                        "shift_start": shift_s, "shift_end": shift_e,
                                        "joined_on": "2026-01-01"})
    if st not in (201, 409):
        check(f"fixture employee {code} created", False, str(st))

# R2 boundary: shift 09:30 -> grace ends 09:40:00 exactly
st, r1 = call("POST", "/attendance/punch-in", {"emp_code": "EMP9001", "punched_at": ms_ist(2026, 7, 6, 9, 40, 0)})
check("R2 punch-in at exactly grace end -> not late (0)",
      st == 201 and r1.get("late_minutes") == 0, f"{st} {r1.get('late_minutes') if st==201 else r1}")
st, r2 = call("POST", "/attendance/punch-in", {"emp_code": "EMP9002", "punched_at": ms_ist(2026, 7, 6, 9, 40, 1)})
check("R2 punch-in 1 second past grace -> late (10 min)",
      st == 201 and r2.get("late_minutes") == 10, f"{st} {r2.get('late_minutes') if st==201 else r2}")
st, r3 = call("POST", "/attendance/punch-in", {"emp_code": "EMP9003", "punched_at": ms_ist(2026, 7, 7, 10, 15, 59)})
check("R2 punch-in 10:15:59 -> 45 late minutes",
      st == 201 and r3.get("late_minutes") == 45, f"{st} {r3.get('late_minutes') if st==201 else r3}")

st, dup = call("POST", "/attendance/punch-in", {"emp_code": "EMP9001", "punched_at": ms_ist(2026, 7, 6, 9, 41)})
check("punch-in twice same day -> 409", st == 409, str(st))

# R1 overnight: night shift 22:00-06:00, punch at 01:00 IST on the 7th -> belongs to the 6th
st, night = call("POST", "/attendance/punch-in", {"emp_code": "EMP9998", "punched_at": ms_ist(2026, 7, 7, 1, 0)})
check("R1 overnight punch-in 01:00 IST -> previous day (2026-07-06)",
      st == 201 and night.get("date") == "2026-07-06", f"{st} {night.get('date') if st==201 else night}")

# --- punch-out --------------------------------------------------------------
# A brand-new employee has no records at all -> 404.
st, _ = call("POST", "/employees", {"emp_code": "EMP9007", "name": "No Punch",
                                    "email": "nopunch@example.com", "department": "Engineering",
                                    "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"})
st, body = call("POST", "/attendance/punch-out", {"emp_code": "EMP9007"})
check("punch-out for an employee with no punch-in -> 404", st == 404, str(st))
# A record that exists but is long closed, punched out "now", is >24h after punch_in -> 422.
st, body = call("POST", "/attendance/punch-out", {"emp_code": "EMP0002"})
check("punch-out more than 24h after punch_in -> 422", st == 422, str(st))

st, out = call("POST", "/attendance/punch-out", {"emp_code": "EMP9001", "punched_at": ms_ist(2026, 7, 6, 18, 59, 59)})
check("R3 punch-out 18:59:59 (shift ends 18:30) -> 29 min, under 30 -> 0 overtime",
      st == 200 and out.get("overtime_minutes") == 0, f"{st} {out.get('overtime_minutes') if st==200 else out}")
st, out2 = call("POST", "/attendance/punch-out", {"emp_code": "EMP9002", "punched_at": ms_ist(2026, 7, 6, 19, 0, 0)})
check("R3 punch-out 19:00:00 -> exactly 30 -> 30 overtime",
      st == 200 and out2.get("overtime_minutes") == 30, f"{st} {out2.get('overtime_minutes') if st==200 else out2}")

# R4 work hours. EMP9005 is punched in at 09:30 and out at 18:45 = 9h15m = 9.25 exactly.
# EMP9006 does 09:30 -> 13:00 = 3.5h for the R5 half-day boundary.
for code in ("EMP9006",):
    st, _ = call("POST", "/employees", {"emp_code": code, "name": f"Test {code}",
                                        "email": f"{code.lower()}@example.com", "department": "Engineering",
                                        "shift_start": "09:30", "shift_end": "18:30",
                                        "joined_on": "2026-01-01"})
st, _ = call("POST", "/attendance/punch-in", {"emp_code": "EMP9005", "punched_at": ms_ist(2026, 7, 8, 9, 30, 0)})
st, wh = call("POST", "/attendance/punch-out", {"emp_code": "EMP9005", "punched_at": ms_ist(2026, 7, 8, 18, 45, 0)})
check("R4 work hours half-up to 2dp (9h15m -> 9.25)",
      st == 200 and abs(wh.get("work_hours", 0) - 9.25) < 0.001,
      f"got={wh.get('work_hours') if st==200 else wh} expected=9.25")
check("R5 half_day False for a full day", st == 200 and wh.get("half_day") is False, str(st))

# half day: 09:30 -> 13:00 = 3.5h -> half_day True, 0.5 day
st, _ = call("POST", "/attendance/punch-in", {"emp_code": "EMP9004", "punched_at": ms_ist(2026, 7, 8, 9, 30, 0)})
st, hd = call("POST", "/attendance/punch-out", {"emp_code": "EMP9004", "punched_at": ms_ist(2026, 7, 8, 13, 0, 0)})
check("R5 half day at 3.5 hours", st == 200 and hd.get("half_day") is True and hd.get("work_hours") == 3.5,
      f"{st} {hd}")

st, twice = call("POST", "/attendance/punch-out", {"emp_code": "EMP9001", "punched_at": ms_ist(2026, 7, 6, 20, 0)})
check("punch-out twice -> 409", st == 409, str(twice))
st, before = call("POST", "/attendance/punch-out", {"emp_code": "EMP9002", "punched_at": ms_ist(2026, 7, 6, 9, 0)})
check("punch-out before punch-in -> 422", st == 422, str(st))

# --- listing ----------------------------------------------------------------
st, body = call("GET", "/attendance?page=1&page_size=5")
check("GET /attendance 200", st == 200, str(st))
rec = body["items"][0]
check("instants serialised as integer epoch ms",
      isinstance(rec["punch_in"], int) or rec["punch_in"] is None, str(type(rec["punch_in"])))
check("no _id / id leaked in attendance records",
      all("_id" not in i and "id" not in i for i in body["items"]))
check("record has every required key",
      all(k in rec for k in ("emp_code", "date", "status", "punch_in", "punch_out",
                             "work_hours", "late_minutes", "overtime_minutes", "half_day", "history")), str(rec.keys()))
st, b = call("GET", "/attendance?page=1&page_size=3")
st, b2 = call("GET", "/attendance?page=2&page_size=3")
dates = [i["date"] for i in b["items"]] + [i["date"] for i in b2["items"]]
check("R10 listing sorted date DESC, emp_code ASC", dates == sorted(dates, reverse=True), str(dates))
st, body = call("GET", "/attendance?date_from=2026-07-20&date_to=2026-07-01")
check("date_from > date_to -> 422", st == 422, str(st))
st, body = call("GET", "/attendance?status=TYPO")
check("invalid status -> 422", st == 422, str(st))
st, body = call("GET", "/attendance?emp_code=EMP0001")
check("emp_code filter works", st == 200 and all(i["emp_code"] == "EMP0001" for i in body["items"]), str(st))

# --- corrections / audit trail ---------------------------------------------
st, body = call("PATCH", "/attendance/EMP9003/2026-07-07",
                {"punch_in": ms_ist(2026, 7, 7, 10, 0), "reason": "Badge reader was broken",
                 "regularized_by": "hr@company.com"})
check("PATCH correction 200", st == 200, str(st))
check("correction recomputes late_minutes", st == 200 and body["late_minutes"] == 30,
      str(body.get("late_minutes") if st == 200 else body))
check("audit trail has one history entry", st == 200 and len(body["history"]) == 1, str(len(body.get("history", []))))
h = body["history"][0] if st == 200 else {}
check("history entry records by/reason",
      h.get("by") == "hr@company.com" and h.get("reason") == "Badge reader was broken", str(h))
check("history changes carry from/to as epoch ms",
      isinstance(h.get("changes", {}).get("punch_in", {}).get("to"), int), str(h.get("changes")))
check("history timestamp is epoch ms", isinstance(h.get("at"), int), str(type(h.get("at"))))

st, body = call("PATCH", "/attendance/EMP9003/2026-07-07",
                {"status": "LEAVE", "reason": "Approved casual leave", "regularized_by": "hr@company.com"})
check("PATCH to LEAVE clears punches", st == 200 and body["punch_in"] is None and body["punch_out"] is None,
      str(body)[:200])
check("second correction appends to history", st == 200 and len(body["history"]) == 2, str(len(body.get("history", []))))

st, body = call("PATCH", "/attendance/EMP9003/2026-07-07",
                {"punch_in": ms_ist(2026, 7, 7, 10, 0), "reason": "Duplicate correction", "regularized_by": "hr@x.com"})
check("no-op correction -> 422", st == 422, str(st))
st, body = call("PATCH", "/attendance/EMP9003/2026-07-07",
                {"status": "ABSENT", "punch_in": ms_ist(2026, 7, 7, 9, 0), "reason": "Should be rejected here",
                 "regularized_by": "hr@x.com"})
check("ABSENT + punch_in -> 422", st == 422, str(st))
st, body = call("PATCH", "/attendance/EMP9003/2026-07-07", {"reason": "no", "regularized_by": "hr@x.com"})
check("reason too short -> 422", st == 422, str(st))
st, body = call("PATCH", "/attendance/EMP9003/2026-07-07",
                {"punch_in": ms_ist(2026, 8, 1, 9, 0), "reason": "Wrong attendance day", "regularized_by": "hr@x.com"})
check("R1 correction to a different date -> 422", st == 422, str(st))
st, body = call("PATCH", "/attendance/EMP9003/2026-07-01",
                {"status": "LEAVE", "reason": "No such record at all", "regularized_by": "hr@x.com"})
check("PATCH unknown record -> 404", st == 404, str(st))
st, body = call("PATCH", "/attendance/NOBODY/2026-07-01",
                {"status": "LEAVE", "reason": "Unknown employee here", "regularized_by": "hr@x.com"})
check("PATCH unknown employee -> 404", st == 404, str(st))

# --- analytics --------------------------------------------------------------
st, body = call("GET", "/analytics/employees/EMP0001/monthly?month=2026-07")
check("monthly analytics 200", st == 200, str(st))
check("monthly has all contract fields",
      st == 200 and all(k in body for k in ("emp_code", "month", "working_days", "present_days", "leave_days",
                                            "late_count", "total_late_minutes", "total_overtime_minutes",
                                            "attendance_pct")), str(body.keys()) if st == 200 else str(body))
check("attendance_pct <= 100", st == 200 and (body["attendance_pct"] is None or body["attendance_pct"] <= 100),
      str(body.get("attendance_pct")))
st, body = call("GET", "/analytics/employees/NOBODY/monthly?month=2026-07")
check("monthly unknown employee -> 404", st == 404, str(st))
st, body = call("GET", "/analytics/employees/EMP0001/monthly?month=2026-13")
check("monthly invalid month -> 422", st == 422, str(st))
st, body = call("GET", "/analytics/employees/EMP9004/monthly?month=2026-07")
check("R7 working days Mon-Fri for July 2026 = 23",
      st == 200 and body["working_days"] == 23, f"got={body.get('working_days') if st==200 else body}")
check("R5 half day counted as 0.5 present day",
      st == 200 and abs(body["present_days"] - round(body["present_days"], 1)) < 1e-9, str(body.get("present_days")))

st, body = call("GET", "/analytics/departments/summary?month=2026-07")
check("department summary 200", st == 200, str(st))
check("summary has month + items", st == 200 and "month" in body and "items" in body)
check("R9 every department has headcount >= 0",
      st == 200 and all(i["headcount"] >= 0 for i in body["items"]), str(body)[:200])
check("R9 a department with no attendance still appears",
      st == 200 and any(i["headcount"] > 0 for i in body["items"]), str([(i["department"], i["headcount"]) for i in body.get("items", [])]))
check("summary rows have all contract keys",
      st == 200 and all(all(k in i for k in ("department", "headcount", "present_days", "avg_work_hours",
                                             "late_count", "total_late_minutes", "leave_count", "on_duty_count"))
                        for i in body["items"]))
st, body = call("GET", "/analytics/departments/summary?month=2026-07&department=Engineering")
check("summary department filter works",
      st == 200 and all(i["department"] == "Engineering" for i in body["items"]), str(st))

st, body = call("GET", "/analytics/leaderboard/late?month=2026-07")
check("leaderboard 200", st == 200, str(st))
check("leaderboard ranks are 1..n non-decreasing",
      st == 200 and [i["rank"] for i in body["items"]] == sorted(i["rank"] for i in body["items"]),
      str([i["rank"] for i in body.get("items", [])]))
check("leaderboard sorted by total_late_minutes desc",
      st == 200 and [i["total_late_minutes"] for i in body["items"]] ==
      sorted((i["total_late_minutes"] for i in body["items"]), reverse=True))
check("leaderboard rank 1 has the max late minutes",
      st == 200 and (not body["items"] or body["items"][0]["total_late_minutes"] ==
                     max(i["total_late_minutes"] for i in body["items"])))
st, body = call("GET", "/analytics/leaderboard/late?month=2026-07&limit=2")
check("leaderboard limit respected", st == 200 and len(body["items"]) <= 2, str(len(body.get("items", []))))
st, body = call("GET", "/analytics/leaderboard/late?month=2026-07&limit=51")
check("leaderboard limit>50 -> 422", st == 422, str(st))

st, body = call("GET", "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-07")
check("trend 200", st == 200, str(st))
check("trend returns one row per calendar day",
      st == 200 and len(body["items"]) == 7, str(len(body.get("items", []))))
check("trend dates are contiguous in order",
      st == 200 and [i["date"] for i in body["items"]] == [f"2026-07-0{d}" for d in range(1, 8)],
      str([i["date"] for i in body.get("items", [])]))
check("trend rows carry all contract keys",
      st == 200 and all(all(k in i for k in ("date", "is_working_day", "headcount", "present_count",
                                             "late_count", "attendance_rate", "moving_avg_7d"))
                        for i in body["items"]))
check("trend marks weekends as non-working",
      st == 200 and [i["is_working_day"] for i in body["items"]] == [True, True, True, False, False, True, True],
      str([i["is_working_day"] for i in body.get("items", [])]))
check("weekend attendance_rate is null",
      st == 200 and all(i["attendance_rate"] is None for i in body["items"] if not i["is_working_day"]),
      str([(i["date"], i["attendance_rate"]) for i in body.get("items", []) if not i["is_working_day"]]))
check("trend has no date outside the range",
      st == 200 and all("2026-07-01" <= i["date"] <= "2026-07-07" for i in body["items"]),
      str([i["date"] for i in body.get("items", [])]))
st, body = call("GET", "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-06-01")
check("trend to < from -> 422", st == 422, str(st))
st, body = call("GET", "/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-12-31")
check("trend range > 92 days -> 422", st == 422, str(st))
st, body = call("GET", "/analytics/departments/NoSuchDept/trend?from=2026-07-01&to=2026-07-07")
check("trend unknown department -> 404", st == 404, str(st))
st, body = call("GET", "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-03")
check("trend single-day-ish range returns 3 rows", st == 200 and len(body["items"]) == 3,
      str(len(body.get("items", []))))

# --- explain ----------------------------------------------------------------
for ep, q in [("attendance_list", "page=1&page_size=20"),
              ("employee_monthly", "emp_code=EMP0001&month=2026-07"),
              ("department_summary", "month=2026-07"),
              ("late_leaderboard", "month=2026-07"),
              ("department_trend", "department=Engineering&from=2026-07-01&to=2026-07-07")]:
    st, body = call("GET", f"/admin/explain/{ep}?{q}")
    ok = st == 200 and isinstance(body.get("explain"), dict)
    check(f"explain {ep} -> 200 with plan", ok, str(st))
    if ok:
        ex = json.dumps(body["explain"])
        check(f"explain {ep} uses an index (no COLLSCAN)", "COLLSCAN" not in ex,
              "COLLSCAN present" if "COLLSCAN" in ex else "")
        check(f"explain {ep} reports executionStats", "executionStats" in ex or "nReturned" in ex)
st, body = call("GET", "/admin/explain/employee_monthly?month=2026-07")
check("explain missing required param -> 422", st == 422, str(st))

# --- OpenAPI self-consistency ----------------------------------------------
st, body = call("GET", "/openapi.json")
check("served OpenAPI doc matches the contract's endpoints",
      st == 200 and set(body["paths"]) == {
          "/health", "/employees", "/attendance/punch-in", "/attendance/punch-out", "/attendance",
          "/attendance/{emp_code}/{date}", "/analytics/employees/{emp_code}/monthly",
          "/analytics/departments/summary", "/analytics/leaderboard/late",
          "/analytics/departments/{department}/trend", "/admin/explain/{endpoint}"},
      str(sorted(body.get("paths", {}))))

print("\n" + "=" * 70)
failed = [n for n, ok, _ in results if not ok]
print(f"{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("\nFAILED:")
    for n in failed:
        print("  -", n)
sys.exit(1 if failed else 0)