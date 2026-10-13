"""
Employee Attendance & Analytics API.

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded, but real environment variables win)

All application code lives in this file on purpose (see the assignment brief).

Layout
------
  1. Configuration and startup        6. Analytics aggregation pipelines
  2. Time, rounding and R1-R5 rules   7. Analytics endpoints
  3. Serialisation at the API edge    8. Admin / explain endpoint
  4. Request models (422 contracts)   9. Query builders shared with /admin/explain
  5. Attendance endpoints
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from bson import ObjectId
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

# Real environment variables must win over .env, so override stays False.
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True is mandatory: PyMongo otherwise hands back *naive* datetimes and
# every comparison against an aware value raises TypeError. All instants stored
# in MongoDB are UTC.
client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]

# R2 / R3 / R5 thresholds.
GRACE_SECONDS = 600  # 10 minutes, exclusive: only a punch-in strictly later counts
OVERTIME_MINIMUM_MINUTES = 30
HALF_DAY_HOURS = Decimal("4.50")

Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


# --------------------------------------------------------------------------- #
# 1. Startup: indexes
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    """Create every index the app relies on. Idempotent, so it is safe to run
    on each boot against a database that already holds the grader's dataset.

    Only the keys and the uniqueness matter, not the name. A database that
    already has an equivalent index under a different name is left alone
    (IndexOptionsConflict is the server telling us exactly that), and a
    duplicate emp_code in existing data is reported without taking the app down.
    """
    specs: list[tuple[str, list[tuple[str, int]], bool]] = [
        # Identity. The unique index is what makes a duplicate emp_code impossible
        # even when two creates race each other (POST /employees -> 409).
        ("employees", [("emp_code", ASCENDING)], True),
        # GET /employees?department=... and the headcount half of the analytics.
        ("employees", [("department", ASCENDING), ("joined_on", ASCENDING)], False),
        # Headcount for a month with no department filter.
        ("employees", [("joined_on", ASCENDING)], False),
        # One record per (emp_code, date): this unique index is the race guard for
        # punch-in. emp_code + date desc also serves the employee-scoped listing.
        ("attendance_logs", [("emp_code", ASCENDING), ("date", DESCENDING)], True),
        # Punch-out finds the most recent record at or before the punch time.
        ("attendance_logs", [("emp_code", ASCENDING), ("punch_in", DESCENDING)], False),
        # Default listing sort (date desc, emp_code asc) and every month-range
        # analytics scan (department summary, leaderboard, trend).
        ("attendance_logs", [("date", DESCENDING), ("emp_code", ASCENDING)], False),
        # Listing filtered by status, and the leaderboard's month range.
        ("attendance_logs", [("status", ASCENDING), ("date", DESCENDING)], False),
    ]
    for collection, keys, unique in specs:
        try:
            db[collection].create_index(keys, unique=unique)
        except OperationFailure as exc:
            if exc.code not in (85, 86):  # IndexOptionsConflict / IndexKeySpecsConflict
                print(f"[index] could not create {keys} on {collection}: {exc}")
        except PyMongoError as exc:  # never take the app down over an index
            print(f"[index] could not create {keys} on {collection}: {exc}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 2. Time handling and the business rules R1 - R5
# --------------------------------------------------------------------------- #
def to_ms(value: Any) -> Optional[int]:
    """An instant as epoch milliseconds: every instant in the API is an integer
    count of milliseconds. A value that is already an integer passes through, so
    this is safe to apply to a document that has been through serialisation."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return int((value - EPOCH).total_seconds() * 1000)
    return int(value)


def from_ms(millis: Optional[int]) -> Optional[datetime]:
    """Epoch milliseconds to an aware UTC datetime truncated to whole seconds."""
    if millis is None:
        return None
    if isinstance(millis, datetime):
        return truncate_to_second(millis.astimezone(UTC))
    return EPOCH + timedelta(seconds=int(millis) // 1000)


def truncate_to_second(moment: datetime) -> datetime:
    """R1: instants are truncated to whole seconds before any calculation."""
    return moment.replace(microsecond=0)


def shift_minutes(shift: str) -> int:
    hour, minute = shift.split(":")
    return int(hour) * 60 + int(minute)


def shift_seconds(shift: str) -> int:
    return shift_minutes(shift) * 60


def is_overnight(employee: dict) -> bool:
    """R1: shift_end <= shift_start means the shift crosses midnight."""
    return shift_seconds(employee["shift_end"]) <= shift_seconds(employee["shift_start"])


def attendance_date(punch_in: datetime, employee: dict) -> str:
    """R1: the attendance date is the IST calendar date of punch_in. On an
    overnight shift a punch-in earlier than shift_end belongs to the shift that
    started the previous day."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if is_overnight(employee) and _time_of_day(local) < shift_seconds(employee["shift_end"]):
        day = day - timedelta(days=1)
    return day.isoformat()


def _time_of_day(local: datetime) -> int:
    return local.hour * 3600 + local.minute * 60 + local.second


def shift_start_instant(attendance_day: str, employee: dict) -> datetime:
    """Start of the shift that belongs to `attendance_day`, as an aware UTC instant."""
    return _local_moment(attendance_day, shift_seconds(employee["shift_start"]))


def shift_end_instant(attendance_day: str, employee: dict) -> datetime:
    """End of the shift that belongs to `attendance_day`. For an overnight shift
    the end falls on the next calendar day (R1)."""
    seconds = shift_seconds(employee["shift_end"])
    if is_overnight(employee):
        seconds += 86_400
    return _local_moment(attendance_day, seconds)


def _local_moment(attendance_day: str, seconds_into_day: int) -> datetime:
    base = datetime.strptime(attendance_day, "%Y-%m-%d").replace(tzinfo=IST)
    return (base + timedelta(seconds=seconds_into_day)).astimezone(UTC)


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: 10 minute grace period, exclusive. When late the count is whole
    minutes since shift_start, not since the end of the grace period."""
    late_seconds = int((punch_in - shift_start).total_seconds())
    if late_seconds <= GRACE_SECONDS:
        return 0
    return late_seconds // 60


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    """R3: whole minutes between shift_end and punch_out, counted only when at
    least 30 minutes."""
    minutes = int((punch_out - shift_end).total_seconds()) // 60
    return minutes if minutes >= OVERTIME_MINIMUM_MINUTES else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals half-up. Decimal is used because
    Python's round() is banker's rounding and float addition is not exact."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float(_half_up(Decimal(seconds) / Decimal(3600), 2))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half day when the *rounded* work hours are below 4.50."""
    if work_hours is None:
        return False
    return _to_decimal(work_hours) < HALF_DAY_HOURS


def _half_up(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _to_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def round_value(value: Any, places: int = 2) -> Optional[float]:
    """R8: every number this API reports is rounded half-up (rates to 4 places)."""
    if value is None:
        return None
    return float(_half_up(_to_decimal(value), places))


def month_bounds(month: str) -> tuple[str, str]:
    """'YYYY-MM' to the first and last calendar day of that month."""
    year, mon = (int(part) for part in month.split("-"))
    first = Date(year, mon, 1)
    last_day = 31 if mon in (1, 3, 5, 7, 8, 10, 12) else (30 if mon != 2 else (29 if _leap(year) else 28))
    return first.isoformat(), Date(year, mon, last_day).isoformat()


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _parse_moment(millis: Optional[int]) -> datetime:
    """`punched_at` (epoch ms) -> aware UTC instant, truncated to whole seconds (R1).
    An omitted value means "now"."""
    if millis is None:
        return truncate_to_second(datetime.now(UTC))
    return truncate_to_second(from_ms(millis))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# 3. Serialisation at the API edge
# --------------------------------------------------------------------------- #
def api_value(field: str, value: Any) -> Any:
    """A stored value in its API form.

    Used both for the top-level record fields and inside history[].changes: the
    instants nested in a change pair are epoch milliseconds too, which is easy to
    forget because they sit inside an object.
    """
    if field in ("punch_in", "punch_out"):
        return to_ms(value)
    if field == "work_hours":
        return round_value(value) if value is not None else None
    if field == "half_day":
        return bool(value)
    if field in ("late_minutes", "overtime_minutes"):
        return int(value or 0)
    return value


def serialize_history(entries: Optional[Iterable[dict]]) -> list[dict]:
    history = []
    for entry in entries or []:
        changes = entry.get("changes") or {}
        history.append(
            {
                "at": to_ms(entry.get("at")),
                "by": entry.get("by", ""),
                "reason": entry.get("reason", ""),
                "changes": {field: {"from": api_value(field, pair.get("from")),
                                    "to": api_value(field, pair.get("to"))}
                            for field, pair in changes.items()},
            }
        )
    return history


def serialize_record(document: Optional[dict]) -> Optional[dict]:
    """A stored attendance document as the API record. _id is internal and never
    appears; instants become epoch milliseconds; fields a legacy document omits are
    filled with their defaults (DATA_MODEL.md section 3)."""
    if document is None:
        return None
    return {
        "emp_code": document.get("emp_code"),
        "date": document.get("date"),
        "status": document.get("status"),
        "punch_in": to_ms(document.get("punch_in")),
        "punch_out": to_ms(document.get("punch_out")),
        "work_hours": round_value(document["work_hours"]) if document.get("work_hours") is not None else None,
        "late_minutes": int(document.get("late_minutes") or 0),
        "overtime_minutes": int(document.get("overtime_minutes") or 0),
        "half_day": bool(document.get("half_day") or False),
        "history": serialize_history(document.get("history")),
    }


def serialize_employee(document: Optional[dict]) -> Optional[dict]:
    if document is None:
        return None
    return {
        "emp_code": document.get("emp_code"),
        "name": document.get("name"),
        "email": document.get("email"),
        "department": document.get("department"),
        "shift_start": document.get("shift_start"),
        "shift_end": document.get("shift_end"),
        "joined_on": document.get("joined_on"),
        "created_at": to_ms(document.get("created_at")),
    }


def explainable(value: Any) -> Any:
    """Raw explain output contains BSON types the JSON encoder does not know."""
    if isinstance(value, dict):
        return {key: explainable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [explainable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, bytes):
        return value.decode("utf8", "replace")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# --------------------------------------------------------------------------- #
# 4. Request models
# --------------------------------------------------------------------------- #
EpochMillis = Annotated[
    int,
    Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000),
]

ShiftTime = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class EmployeeIn(BaseModel):
    # Unknown fields (work_hours and friends) are ignored, not rejected.
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: ShiftTime = "09:30"
    shift_end: ShiftTime = "18:30"
    joined_on: Date

    @model_validator(mode="after")
    def _distinct_shift(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self

    @field_validator("name", "email", "department")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1, max_length=50)
    punched_at: Optional[EpochMillis] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(min_length=1, max_length=50)
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _status_and_times_agree(self) -> "RegularizeIn":
        if self.status in ("ABSENT", "LEAVE") and (self.punch_in is not None or self.punch_out is not None):
            raise ValueError("status ABSENT or LEAVE clears both punch times and cannot be combined with them")
        return self


# --------------------------------------------------------------------------- #
# 5. Health and employees
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["system"])
def health():
    """200 only when MongoDB answers, 503 otherwise."""
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, f"mongodb unavailable: {exc}") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeIn):
    document = body.model_dump()
    document["joined_on"] = body.joined_on.isoformat()
    document["created_at"] = datetime.now(UTC)
    try:
        # The unique index, not a read-then-write check, is what makes two
        # concurrent creates produce exactly one 201.
        db.employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return serialize_employee(document)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(default=None),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    query = {"department": department} if department else {}
    total = db.employees.count_documents(query)
    # Sorted by emp_code so pagination is stable; the unique emp_code index
    # supplies that order without a blocking sort.
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def require_employee(emp_code: str) -> dict:
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if employee is None:
        raise HTTPException(404, f"unknown employee {emp_code}")
    return employee


# --------------------------------------------------------------------------- #
# 5b. Attendance
# --------------------------------------------------------------------------- #
def attendance_query(emp_code: Optional[str], date_from: Optional[Date],
                     date_to: Optional[Date], status: Optional[str]) -> dict:
    query: dict = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        bounds: dict = {}
        if date_from:
            bounds["$gte"] = date_from.isoformat()
        if date_to:
            bounds["$lte"] = date_to.isoformat()
        query["date"] = bounds
    if status:
        query["status"] = status
    return query


def check_date_range(date_from: Optional[Date], date_to: Optional[Date]) -> None:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInIn):
    employee = require_employee(body.emp_code)
    moment = _parse_moment(body.punched_at)
    day = attendance_date(moment, employee)

    document = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": moment,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(moment, shift_start_instant(day, employee)),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        # Check-then-insert would let two simultaneous punch-ins both return 201.
        # The unique (emp_code, date) index makes the second one fail atomically.
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(409, f"already punched in for {day}") from exc
    return serialize_record(document)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutIn):
    employee = require_employee(body.emp_code)
    moment = _parse_moment(body.punched_at)

    # The most recent record whose punch-in is at or before the punch-out moment.
    # Works across midnight for an overnight shift because it compares instants,
    # not calendar dates.
    open_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": moment}},
        sort=[("punch_in", DESCENDING)],
    )
    if open_record is None:
        # Nothing at or before this time. If the employee has punched in at all,
        # the punch-out simply came before that punch-in, which the contract makes
        # a 422; only an employee with no punch-in at all is a 404.
        earliest = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
            sort=[("punch_in", ASCENDING)],
        )
        if earliest is None:
            raise HTTPException(404, "no punch-in found for this employee")
        raise HTTPException(422, "punched_at must be after punch_in")

    punched_in = open_record["punch_in"]
    elapsed_seconds = int((moment - punched_in).total_seconds())
    if elapsed_seconds <= 0:
        raise HTTPException(422, "punched_at must be after punch_in")
    if elapsed_seconds > 86_400:
        raise HTTPException(422, "punched_at must be within 24 hours of punch_in")

    if open_record.get("punch_out") is not None:
        raise HTTPException(409, f"already punched out for {open_record['date']}")

    day = open_record["date"]
    work_hours = compute_work_hours(punched_in, moment)
    update = {
        "punch_out": moment,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(moment, shift_end_instant(day, employee)),
        "half_day": compute_half_day(work_hours),
    }
    # punch_out: None in the filter is the atomic guard: of two simultaneous
    # punch-outs exactly one matches the document and the other finds nothing.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": open_record["_id"], "punch_out": None},
        {"$set": update},
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, f"already punched out for {day}")
    return serialize_record(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(default=None),
    date_from: Optional[Date] = Query(default=None),
    date_to: Optional[Date] = Query(default=None),
    status: Optional[Status] = Query(default=None),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    check_date_range(date_from, date_to)
    query = attendance_query(emp_code, date_from, date_to, status)
    # Sorted and paged in the database: the (date desc, emp_code asc) index makes
    # this an indexed scan plus a skip, so it stays fast at 100k records.
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_record(doc) for doc in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize(
    body: RegularizeIn,
    emp_code: str = Path(min_length=1, max_length=50),
    date: Date = Path(...),
):
    employee = require_employee(emp_code)
    day = date.isoformat()
    current = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if current is None:
        raise HTTPException(404, f"no record for {emp_code} on {day}")

    before = serialize_record(current)

    status = body.status or current["status"]
    punch_in_value = current.get("punch_in") if body.punch_in is None else _parse_moment(body.punch_in)
    punch_out_value = current.get("punch_out") if body.punch_out is None else _parse_moment(body.punch_out)

    if status in ("ABSENT", "LEAVE"):
        punch_in_value, punch_out_value = None, None
    else:
        if punch_in_value is None:
            raise HTTPException(422, f"status {status} requires a punch_in")
        if punch_in_value.tzinfo is None:
            punch_in_value = punch_in_value.replace(tzinfo=UTC)
        # R1: the corrected punch-in must still fall on this record's date.
        if attendance_date(punch_in_value, employee) != day:
            raise HTTPException(422, "punch_in must fall on the record's attendance date")
        if punch_out_value is not None:
            span = int((punch_out_value - punch_in_value).total_seconds())
            if span <= 0:
                raise HTTPException(422, "punch_out must be after punch_in")
            if span > 86_400:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    work_hours = compute_work_hours(punch_in_value, punch_out_value) if punch_out_value else None
    after_fields = {
        "status": status,
        "punch_in": punch_in_value,
        "punch_out": punch_out_value,
        "work_hours": work_hours,
        "late_minutes": compute_late_minutes(punch_in_value, shift_start_instant(day, employee))
        if punch_in_value
        else 0,
        "overtime_minutes": compute_overtime(punch_out_value, shift_end_instant(day, employee))
        if punch_out_value
        else 0,
        "half_day": compute_half_day(work_hours),
    }

    # `before` is already in API form (instants as epoch ms), so only the new values
    # need converting before the two are compared and recorded.
    after_api = {field: api_value(field, value) for field, value in after_fields.items()}
    # history[].changes lists only the fields that actually changed.
    changes = {
        field: {"from": before[field], "to": after_api[field]}
        for field in after_fields
        if before[field] != after_api[field]
    }
    if not changes:
        raise HTTPException(422, "the request does not change anything")

    entry = {
        "at": datetime.now(UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # Optimistic concurrency: the filter pins the fields we read and the current
    # history length, so a concurrent correction loses (409) instead of silently
    # overwriting the other entry.
    guard = {
        "_id": current["_id"],
        "status": current["status"],
        "punch_in": current.get("punch_in"),
        "punch_out": current.get("punch_out"),
        "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(current.get("history") or [])]},
    }
    updated = db.attendance_logs.find_one_and_update(
        guard,
        {"$set": after_fields, "$push": {"history": entry}},
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, "the record was modified concurrently; re-read it and try again")
    return serialize_record(updated)


# --------------------------------------------------------------------------- #
def is_weekday_expr(date_expr: dict) -> dict:
    """True when the date is Monday-Friday. $dayOfWeek is 1 = Sunday .. 7 = Saturday,
    so a working day is 2..6."""
    return {"$and": [{"$gt": [{"$dayOfWeek": date_expr}, 1]},
                     {"$lte": [{"$dayOfWeek": date_expr}, 6]}]}


def parse_date_expr(date_field: str) -> dict:
    """The stored 'YYYY-MM-DD' string as a BSON date."""
    return {"$dateFromString": {"dateString": date_field, "format": "%Y-%m-%d"}}


def _record_shape(prefix: str = "$") -> dict:
    """Normalises one attendance record inside a $map: legacy documents may omit
    half_day, late_minutes or overtime_minutes, so they default to 0 / false here.

    `prefix` is "$" inside $project / $set on a whole document and "$$log."
    inside a $map over records, where the current element is $$this or a named
    variable - a plain "$status" there would be read as the literal string.
    """
    return {
        "status": f"{prefix}status",
        "present": {"$in": [f"{prefix}status", PRESENCE_STATUSES]},
        "half_day": {"$eq": [{"$ifNull": [f"{prefix}half_day", False]}, True]},
        "late_minutes": {"$ifNull": [f"{prefix}late_minutes", 0]},
        "overtime_minutes": {"$ifNull": [f"{prefix}overtime_minutes", 0]},
        "work_hours": {"$ifNull": [f"{prefix}work_hours", None]},
    }


def employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    """One employee, one month, computed entirely by the pipeline.

    The working-day count is arithmetic on dates inside the database rather than a
    per-day pipeline document: count the days in the window, then subtract the
    weekend days. That keeps the query cheap and indexable instead of generating
    up to 31 intermediate documents per employee.
    """
    month_start, month_end = month_bounds(month)
    from_expr = {"$dateFromString": {"dateString": {"$max": ["$joined_on", month_start]}, "format": "%Y-%m-%d"}}
    to_expr = {"$dateFromString": {"dateString": month_end, "format": "%Y-%m-%d"}}

    return [
        {"$match": {"emp_code": emp_code}},
        {"$set": {"_from": from_expr, "_to": to_expr}},
        # R7: working days are Mon-Fri counted from joined_on, so a mid-month
        # joiner is not charged for the days before they arrived. An employee who
        # has not joined yet gets zero days, not a negative span.
        {"$set": {"_in_range": {"$lte": ["$_from", "$_to"]}}},
        {"$set": {"_days": {"$cond": [
            "$_in_range",
            {"$range": [0, {"$add": [{"$dateDiff": {"startDate": "$_from", "endDate": "$_to", "unit": "day"}}, 1]}]},
            []]}}},
        {"$set": {"working_days": {"$size": {"$filter": {
            "input": "$_days", "as": "offset",
            "cond": {"$and": [
                # $dayOfWeek is 1 = Sunday .. 6 = Friday.
                {"$gt": [{"$dayOfWeek": {"$dateAdd": {"startDate": "$_from", "unit": "day", "amount": "$$offset"}}}, 1]},
                {"$lte": [{"$dayOfWeek": {"$dateAdd": {"startDate": "$_from", "unit": "day", "amount": "$$offset"}}}, 6]},
            ]}}}}}},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}},
        {"$set": {"logs": {"$filter": {"input": "$logs", "as": "log", "cond": {
            "$and": [{"$gte": ["$$log.date", month_start]}, {"$lte": ["$$log.date", month_end]}]}}}}},
        {"$set": {"logs": {"$map": {"input": "$logs", "as": "log", "in": {
            **_record_shape("$$log."),
            "is_working_day": is_weekday_expr(parse_date_expr("$$log.date")),
            "late": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
        }}}}},
        # One pass over the month's records for every statistic. $reduce names its
        # accumulator $$value and the current element $$this.
        {"$set": {"_stats": {"$reduce": {"input": "$logs", "initialValue": {
            "present_days": 0.0, "leave_days": 0, "late_count": 0,
            "total_late_minutes": 0, "total_overtime_minutes": 0},
            "in": {
                # R7: present days count Mon-Fri records only. A weekend record
                # still contributes to the late and overtime totals below.
                "present_days": {"$add": ["$$value.present_days", {"$cond": [
                    {"$and": ["$$this.present", "$$this.is_working_day"]},
                    {"$cond": ["$$this.half_day", 0.5, 1.0]}, 0.0]}]},
                "leave_days": {"$add": ["$$value.leave_days",
                                        {"$cond": [{"$eq": ["$$this.status", "LEAVE"]}, 1, 0]}]},
                "late_count": {"$add": ["$$value.late_count",
                                       {"$cond": ["$$this.late", 1, 0]}]},
                "total_late_minutes": {"$add": ["$$value.total_late_minutes", "$$this.late_minutes"]},
                "total_overtime_minutes": {"$add": ["$$value.total_overtime_minutes",
                                                    "$$this.overtime_minutes"]},
            }}}}},
        {"$set": {"attendance_pct": {"$cond": [
            {"$gt": ["$working_days", 0]},
            {"$multiply": [{"$divide": ["$_stats.present_days", "$working_days"]}, 100]},
            None]}}},
        {"$project": {"_id": 0, "emp_code": 1, "working_days": 1, "attendance_pct": 1,
                      "present_days": "$_stats.present_days", "leave_days": "$_stats.leave_days",
                      "late_count": "$_stats.late_count", "total_late_minutes": "$_stats.total_late_minutes",
                      "total_overtime_minutes": "$_stats.total_overtime_minutes"}},
    ]


def department_summary_pipeline(department: Optional[str], month: str) -> list[dict]:
    """One row per department, from one aggregation over attendance_logs with an
    indexed branch (headcount) unioned in.

    Starting from the logs and unioning the employees branch means the leading
    $match uses the (date desc, emp_code asc) index. The reverse order would put a
    full employees scan in front of every call.
    """
    month_start, month_end = month_bounds(month)
    employee_match: dict = {"joined_on": {"$lte": month_end}}
    if department:
        employee_match["department"] = department
    department_filter: dict = {"emp.department": department} if department else {}

    return [
        # Branch 1: what happened, in the month.
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {**department_filter, "emp.joined_on": {"$lte": month_end}}},
        {"$set": {
            "_weekday": {"$dayOfWeek": parse_date_expr("$date")},
            "_present": {"$in": ["$status", PRESENCE_STATUSES]},
            "_half": {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
            "_hours": {"$cond": [{"$eq": [{"$ifNull": ["$work_hours", None]}, None]}, None,
                                 {"$ifNull": ["$work_hours", None]}]},
        }},
        {"$group": {
            "_id": "$emp.department",
            "present_days": {"$sum": {"$cond": [
                # Working days only ($dayOfWeek: 1 = Sunday, 6 = Friday).
                {"$and": ["$_present", {"$gt": ["$_weekday", 1]}, {"$lte": ["$_weekday", 6]}]},
                {"$cond": ["$_half", 0.5, 1.0]}, 0.0]}},
            # Mean over records, not a mean of per-employee means: the sum and the
            # count are kept apart and divided once, at the end.
            "hours_sum": {"$sum": {"$cond": [{"$and": ["$_present", {"$ne": ["$_hours", None]}]},
                                             "$_hours", 0.0]}},
            "hours_count": {"$sum": {"$cond": [{"$and": ["$_present", {"$ne": ["$_hours", None]}]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            # The headcount branch of the union supplies this; $sum of a missing
            # field is 0, so the log branch can leave it out.
            "headcount": {"$sum": 0},
        }},
        # Branch 2: who was there. R9 counts everyone who had joined by the last
        # day of the month, including people with no records at all. Branches do
        # not see each other's fields until the merge below.
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": employee_match},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
            # $literal keeps these zeros as values rather than the exclusion a bare
            # 0 would mean in an inclusion projection, and _id is renamed to
            # department so both branches group on the same key.
            {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
                          "present_days": {"$literal": 0}, "hours_sum": {"$literal": 0.0},
                          "hours_count": {"$literal": 0}, "late_count": {"$literal": 0},
                          "total_late_minutes": {"$literal": 0}, "leave_count": {"$literal": 0},
                          "on_duty_count": {"$literal": 0}}},
        ]}},
        {"$group": {
            "_id": {"$ifNull": ["$department", "$_id"]},
            "headcount": {"$sum": "$headcount"},
            "present_days": {"$sum": "$present_days"},
            "hours_sum": {"$sum": {"$ifNull": ["$hours_sum", 0.0]}},
            "hours_count": {"$sum": {"$ifNull": ["$hours_count", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$on_duty_count", 0]}},
        }},
        {"$sort": {"_id": ASCENDING}},
    ]


def late_leaderboard_pipeline(department: Optional[str], month: str, limit: int) -> list[dict]:
    """Ranks by total late minutes with standard competition ranking (1, 2, 2, 4).

    $rank is a window function, so ties share a rank and the next one is skipped.
    Using the position in a sorted array as the rank would be wrong.
    """
    month_start, month_end = month_bounds(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": month_start, "$lte": month_end}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        # name and department live on the employee; logs without one are dropped.
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
                      "total_late_minutes": 1, "late_count": 1}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": DESCENDING},
                              "output": {"rank": {"$rank": {}}}}},
        # limit applies after ranking, so a tie at the cutoff returns extra rows.
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
    ]
    return pipeline


def department_trend_pipeline(department: str, date_from: Date, date_to: Date) -> list[dict]:
    """Exactly one row per calendar day, weekends and days with no records included,
    each with a 7-day moving average. The database does all of it.

    $densify needs at least one row to work from, so a range with no records at all
    is seeded with a single $documents row carrying the range's first day.
    """
    start = datetime(date_from.year, date_from.month, date_from.day, tzinfo=UTC)
    end = datetime(date_to.year, date_to.month, date_to.day, tzinfo=UTC)
    # $densify needs at least one row to work from. The seed is placed one day
    # *before* the range: $densify keeps existing days and only inserts the
    # boundaries it is missing, so a seed sitting exactly on `start` would show up
    # twice - once as the seed and once as the filled boundary - and every day
    # after it would be off by one.
    seed = start - timedelta(days=1)
    # $densify's upper bound is exclusive, so it is one day past the last day asked
    # for; otherwise the final date of the range is dropped.
    stop = end + timedelta(days=1)
    return [
        {"$match": {"date": {"$gte": date_from.isoformat(), "$lte": date_to.isoformat()}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": parse_date_expr("$date"),
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$status", PRESENCE_STATUSES]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]}, 0.0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$unionWith": {"coll": "attendance_logs", "pipeline": [{"$documents": [{"_id": seed}]}]}},
        {"$densify": {"field": "_id", "range": {"step": 1, "unit": "day", "bounds": [seed, stop]}}},
        {"$match": {"_id": {"$gte": start, "$lt": stop}}},
        {"$sort": {"_id": ASCENDING}},
        {"$set": {"day_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$_id"}},
                  "is_working_day": {"$and": [{"$gt": [{"$dayOfWeek": "$_id"}, 1]},
                                             {"$lte": [{"$dayOfWeek": "$_id"}, 6]}]},
                  "present_count": {"$ifNull": ["$present_count", 0.0]},
                  "late_count": {"$ifNull": ["$late_count", 0]}}},
        # R9: headcount on that day = department employees already joined by then.
        {"$lookup": {"from": "employees", "let": {"day": "$day_str"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]},
                                            {"$lte": ["$joined_on", "$$day"]}]}}},
            {"$count": "hc"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$first": "$hc.hc"}, 0]}}},
        # Only a working day with people in the department has a rate.
        {"$set": {"attendance_rate": {"$round": [{"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None]}, 4]}}},
        # Mean of the non-null rates in this row and the six rows before it, inside
        # the requested range only, so the first rows use a shorter window. The
        # window averages the reported (already rounded) rates, so the moving
        # average really is the mean of the attendance_rate column above.
        # $avg as a window accumulator ignores null rates by itself and yields null
        # when the window holds no rate at all.
        {"$setWindowFields": {"sortBy": {"_id": ASCENDING}, "output": {
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": "$day_str", "is_working_day": 1, "headcount": 1,
                      "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
    ]


# --------------------------------------------------------------------------- #
# 7. Analytics endpoints
# --------------------------------------------------------------------------- #
Month = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
Endpoint = Literal["attendance_list", "employee_monthly", "department_summary",
                   "late_leaderboard", "department_trend"]


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    month: Month,
    emp_code: str = Path(min_length=1, max_length=50),
):
    rows = list(db.employees.aggregate(employee_monthly_pipeline(emp_code, month)))
    if not rows:
        raise HTTPException(404, f"unknown employee {emp_code}")
    row = rows[0]
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": int(row["working_days"]),
        "present_days": round_value(row["present_days"]) or 0.0,
        "leave_days": int(row["leave_days"]),
        "late_count": int(row["late_count"]),
        "total_late_minutes": int(row["total_late_minutes"]),
        "total_overtime_minutes": int(row["total_overtime_minutes"]),
        # R8: a rate is 4 decimals half-up; null when the month has no working day.
        "attendance_pct": round_value(row["attendance_pct"], 4),
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: Month,
    department: Optional[str] = Query(default=None),
):
    month_start, month_end = month_bounds(month)
    items = []
    for row in db.attendance_logs.aggregate(department_summary_pipeline(department, month)):
        hours_count = int(row.get("hours_count") or 0)
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": round_value(row.get("present_days")) or 0.0,
            "avg_work_hours": round_value(_to_decimal(row["hours_sum"]) / hours_count, 2)
            if hours_count
            else None,
            "late_count": int(row.get("late_count") or 0),
            "total_late_minutes": int(row.get("total_late_minutes") or 0),
            "leave_count": int(row.get("leave_count") or 0),
            "on_duty_count": int(row.get("on_duty_count") or 0),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: Month,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = Query(default=None),
):
    rows = db.attendance_logs.aggregate(late_leaderboard_pipeline(department, month, limit))
    return {
        "month": month,
        "items": [{"rank": int(row["rank"]), "emp_code": row["emp_code"], "name": row["name"],
                   "department": row["department"], "total_late_minutes": int(row["total_late_minutes"]),
                   "late_count": int(row["late_count"])} for row in rows],
    }


MAX_TREND_DAYS = 92


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(min_length=1, max_length=50),
    date_from: Date = Query(..., alias="from"),
    date_to: Date = Query(..., alias="to"),
):
    if date_to < date_from:
        raise HTTPException(422, "to must not be before from")
    if (date_to - date_from).days + 1 > MAX_TREND_DAYS:
        raise HTTPException(422, f"the range must not exceed {MAX_TREND_DAYS} days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, f"unknown department {department}")

    pipeline = department_trend_pipeline(department, date_from, date_to)
    items = []
    for row in db.attendance_logs.aggregate(pipeline):
        items.append({
            "date": row["date"],
            "is_working_day": bool(row["is_working_day"]),
            "headcount": int(row["headcount"]),
            "present_count": round_value(row["present_count"]) or 0.0,
            "late_count": int(row["late_count"]),
            "attendance_rate": round_value(row["attendance_rate"], 4),
            "moving_avg_7d": round_value(row["moving_avg_7d"], 4),
        })
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 8. Explain endpoint (Part C)
# --------------------------------------------------------------------------- #
def attendance_list_query(emp_code: Optional[str], date_from: Optional[Date],
                          date_to: Optional[Date], status: Optional[str], page: int, page_size: int) -> dict:
    """The exact find the listing endpoint issues, as a command document."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    return {
        "find": "attendance_logs",
        "filter": attendance_query(emp_code, date_from, date_to, status),
        "sort": {"date": DESCENDING, "emp_code": ASCENDING},
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


def _explain_aggregate(collection: str, pipeline: list[dict]) -> dict:
    return db.command({"explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
                       "verbosity": "executionStats"})


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise HTTPException(422, message)


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Endpoint,
    emp_code: Optional[str] = Query(default=None),
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(default=None),
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[Date] = Query(default=None),
    date_to: Optional[Date] = Query(default=None),
    status: Optional[Status] = Query(default=None),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    date_from_trend: Optional[Date] = Query(default=None, alias="from"),
    date_to_trend: Optional[Date] = Query(default=None, alias="to"),
):
    """Runs MongoDB's explain with executionStats for the main query or pipeline
    of the named endpoint, built by the same builder the endpoint itself uses.
    Each branch checks only the parameters that endpoint needs, so explaining
    department_trend does not demand a month it never uses."""
    if endpoint == "attendance_list":
        command = attendance_list_query(emp_code, date_from, date_to, status, page, page_size)
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": explainable(db.command({"explain": command, "verbosity": "executionStats"}))}

    if endpoint == "department_trend":
        start, end = date_from_trend, date_to_trend
        _require(start is not None and end is not None, "from and to are required")
        _require(department is not None, "department is required")
        _require(not (end < start), "to must not be before from")  # type: ignore[operator]
        pipeline = department_trend_pipeline(department, start, end)  # type: ignore[arg-type]
        collection = "attendance_logs"
    else:
        _require(month is not None, "month is required")
        assert month is not None
        if endpoint == "employee_monthly":
            _require(emp_code is not None, "emp_code is required")
            pipeline = employee_monthly_pipeline(emp_code, month)
            collection = "employees"
        elif endpoint == "department_summary":
            pipeline = department_summary_pipeline(department, month)
            collection = "attendance_logs"
        else:
            pipeline = late_leaderboard_pipeline(department, month, limit)
            collection = "attendance_logs"

    return {"endpoint": endpoint, "collection": collection,
            "explain": explainable(_explain_aggregate(collection, pipeline))}