"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is read for convenience; real environment variables win)

Everything lives in this one file, in this order:
  1. configuration and database handle
  2. types, validation and request models
  3. pure helpers (time zone, R1-R5 calculations, rounding, document -> JSON)
  4. indexes
  5. endpoints: system, employees, attendance, analytics, admin/explain
The contract is openapi.yaml, the stored shapes are in DATA_MODEL.md.
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, OperationFailure, PyMongoError

log = logging.getLogger("attendance")

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv()  # does NOT override variables that are already set in the environment

# tz_aware=True: PyMongo hands back timezone-aware UTC datetimes instead of naive ones, so
# "naive local time vs UTC" mistakes cannot happen when we convert to epoch milliseconds.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=10000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
NOT_WORKING = ("ABSENT", "LEAVE")
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_HOURS = 4.5  # R5
MAX_SHIFT_SECONDS = 24 * 3600
MAX_TREND_DAYS = 92

_indexes_ready = False


# --------------------------------------------------------------------------- #
# 2. Types, validation, request models
# --------------------------------------------------------------------------- #
def _check_date(v: str) -> str:
    try:
        Date.fromisoformat(v)
    except ValueError:
        raise ValueError("not a valid calendar date") from None
    return v


def _check_month(v: str) -> str:
    if int(v[:4]) < 1:
        raise ValueError("not a valid month")
    return v


# "2026-07-06" - a real calendar date, zero padded (so "2026-7-6" and "2026-02-30" are 422).
ISODate = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"), AfterValidator(_check_date)]
Month = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"), AfterValidator(_check_month)]
HHMM = Annotated[str, StringConstraints(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
# strict=True: only real JSON integers; floats, numeric strings and booleans are rejected (422).
EpochMillis = Annotated[int, Field(strict=True, ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1, le=1_000_000_000)]
PageSize = Annotated[int, Query(ge=1, le=100)]  # R10


class EmployeeIn(BaseModel):
    emp_code: Annotated[str, StringConstraints(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: Annotated[str, StringConstraints(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: ISODate

    @model_validator(mode="after")
    def _shift_times_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis = None  # type: ignore[assignment]  (omitted = now; an explicit null is 422)
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: EpochMillis = None  # type: ignore[assignment]


class RegularizeIn(BaseModel):
    # None means "not supplied"; an explicit null is rejected by the types above.
    status: Status = None  # type: ignore[assignment]
    punch_in: EpochMillis = None  # type: ignore[assignment]
    punch_out: EpochMillis = None  # type: ignore[assignment]
    reason: Annotated[str, StringConstraints(min_length=5, max_length=200)]
    regularized_by: Annotated[str, StringConstraints(min_length=1, max_length=50)]


def unprocessable(msg: str, *loc: str) -> RequestValidationError:
    """A 422 with FastAPI's standard body, for rules that depend on stored data (e.g. punch order)."""
    return RequestValidationError([{"type": "value_error", "loc": loc or ("body",), "msg": msg, "input": None}])


# --------------------------------------------------------------------------- #
# 3. Pure helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    """Current time, UTC-aware, truncated to whole milliseconds (BSON precision)."""
    n = datetime.now(UTC)
    return n.replace(microsecond=n.microsecond // 1000 * 1000)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(milliseconds=ms - ms % 1000)


def dt_to_ms(d: datetime) -> int:
    if d.tzinfo is None:  # defensive: seeded or legacy naive values are UTC by definition
        d = d.replace(tzinfo=UTC)
    return (d - EPOCH) // timedelta(milliseconds=1)


def trunc_s(d: datetime) -> datetime:
    return d.replace(microsecond=0)


def half_up(value: Any, places: int) -> float:
    """Round half-up (R4, R8). Python's round() and MongoDB's $round both round half to even."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _hm(hhmm: str) -> tuple[int, int]:
    h, m = hhmm.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded "HH:MM" strings compare correctly


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; an overnight shift punched in before shift_end belongs to yesterday."""
    local = trunc_s(punch_in).astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < time(*_hm(shift_end)):
        day -= timedelta(days=1)
    return day.isoformat()


def _ist_at(date_str: str, hhmm: str, plus_days: int = 0) -> datetime:
    y, mo, d = (int(p) for p in date_str.split("-"))
    h, mi = _hm(hhmm)
    return datetime(y, mo, d, h, mi, tzinfo=IST) + timedelta(days=plus_days)


def compute_late_minutes(punch_in: datetime, date_str: str, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then whole minutes since shift_start."""
    secs = int((trunc_s(punch_in) - _ist_at(date_str, shift_start)).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals half-up."""
    secs = int((trunc_s(punch_out) - trunc_s(punch_in)).total_seconds())
    return half_up(Decimal(secs) / Decimal(3600), 2)


def compute_overtime(punch_out: datetime, date_str: str, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift_end, only if at least 30. An overnight shift ends on the next calendar day."""
    end = _ist_at(date_str, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    mins = int((trunc_s(punch_out) - end).total_seconds()) // 60  # floor
    return mins if mins >= MIN_OVERTIME_MINUTES else 0


def derive(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], date_str: str, emp: dict) -> dict:
    """The four derived fields for a record, from its final status and times (R2-R5)."""
    out = {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    if status in NOT_WORKING or punch_in is None:
        return out
    out["late_minutes"] = compute_late_minutes(punch_in, date_str, emp["shift_start"])
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, date_str, emp["shift_start"], emp["shift_end"])
        out["half_day"] = out["work_hours"] < HALF_DAY_HOURS
    return out


def working_days(month: str, joined_on: str) -> int:
    """R7: Mon-Fri days of the month, counted from joined_on if that is later than the 1st."""
    year, mon = int(month[:4]), int(month[5:])
    first = max(Date(year, mon, 1), Date.fromisoformat(joined_on))
    last = Date(year, mon, calendar.monthrange(year, mon)[1])
    count = 0
    day = first
    while day <= last:
        count += day.weekday() < 5
        day += timedelta(days=1)
    return count


def month_bounds(month: str) -> tuple[str, str]:
    """First and last calendar date of a YYYY-MM month, as strings (comparable with stored dates)."""
    year, mon = int(month[:4]), int(month[5:])
    return f"{month}-01", f"{month}-{calendar.monthrange(year, mon)[1]:02d}"


def employee_out(doc: dict) -> dict:
    created = doc.get("created_at")
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": dt_to_ms(created) if created else None,
    }


def _api_value(v: Any) -> Any:
    return dt_to_ms(v) if isinstance(v, datetime) else v


def attendance_out(doc: dict) -> dict:
    """Stored document -> API record. Missing legacy fields get their defaults; _id is never exposed."""
    history = []
    for h in doc.get("history") or []:
        changes = {
            k: {"from": _api_value(v.get("from")), "to": _api_value(v.get("to"))}
            for k, v in (h.get("changes") or {}).items()
        }
        history.append({"at": dt_to_ms(h["at"]), "by": h.get("by"), "reason": h.get("reason"), "changes": changes})
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _api_value(doc.get("punch_in")),
        "punch_out": _api_value(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# 4. Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    global _indexes_ready
    specs = [
        # (collection, keys, name, unique)
        # employees
        (employees, [("emp_code", 1)], "uniq_emp_code", True),  # identity, 409 on duplicates, $lookup target
        (employees, [("department", 1), ("emp_code", 1)], "dept_emp_code", False),  # GET /employees?department=
        (employees, [("department", 1), ("joined_on", 1)], "dept_joined_on", False),  # headcount per department
        (employees, [("joined_on", 1)], "joined_on", False),  # headcount with no department filter
        # attendance_logs
        (logs, [("emp_code", 1), ("date", 1)], "uniq_emp_code_date", True),  # one record per day, race-safe punch-in
        (logs, [("date", -1), ("emp_code", 1)], "date_emp_code", False),  # list sort order, monthly scans
        (logs, [("status", 1), ("date", -1), ("emp_code", 1)], "status_date_emp_code", False),  # list ?status=
        (logs, [("emp_code", 1), ("punch_in", -1)], "emp_code_punch_in", False),  # punch-out: latest open punch-in
    ]
    ok = True
    for coll, keys, name, unique in specs:
        try:
            coll.create_index(keys, name=name, unique=unique)
        except OperationFailure as exc:
            if unique:
                # The data already holds duplicates (should not happen): keep the service usable, loudly.
                log.error("could not build unique index %s (%s); creating a non-unique one", name, exc)
                try:
                    coll.create_index(keys, name=name)
                except PyMongoError as exc2:
                    ok = False
                    log.error("could not build index %s: %s", name, exc2)
            else:
                ok = False
                log.error("could not build index %s: %s", name, exc)
    _indexes_ready = ok


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # MongoDB not reachable yet: /health answers 503 and retries the indexes
        log.error("index creation failed at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def _mongo_error(_request, exc: PyMongoError):
    log.exception("database error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 5a. System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError as exc:
        log.error("health check failed: %s", exc)
        raise HTTPException(503, "MongoDB is not available") from None
    if not _indexes_ready:  # startup could not create them; retry, but never fail health because of it
        try:
            ensure_indexes()
        except PyMongoError as exc:
            log.error("index creation failed: %s", exc)
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# 5b. Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:  # the unique index on emp_code decides; no check-then-insert race
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists") from None
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    cursor = employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 5c. Attendance
# --------------------------------------------------------------------------- #
def _employee_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = _employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else trunc_s(now_utc())
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(body.status, ts, None, day, emp),
        "history": [],
    }
    try:  # the unique (emp_code, date) index makes exactly one of N concurrent requests win
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date") from None
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = _employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else trunc_s(now_utc())
    # Latest record that started at or before the punch-out. Not filtered by date, so it works across midnight.
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", -1)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    started = trunc_s(rec["punch_in"])
    if ts <= started:
        raise unprocessable("punched_at must be after the punch-in", "body", "punched_at")
    if (ts - started).total_seconds() > MAX_SHIFT_SECONDS:
        raise unprocessable("punched_at is more than 24 hours after the punch-in", "body", "punched_at")
    derived = derive(rec["status"], started, ts, rec["date"], emp)
    # Compare-and-set: only succeeds while the record is still open and unchanged, so of two
    # simultaneous punch-outs exactly one matches.
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"], "status": rec["status"]},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return attendance_out(updated)


def attendance_filter(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]) -> dict:
    if date_from and date_to and date_from > date_to:
        raise unprocessable("date_from must not be after date_to", "query", "date_from")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[ISODate] = None,
    date_to: Optional[ISODate] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = logs.count_documents(q)
    cursor = logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: ISODate, body: RegularizeIn):
    emp = _employee_or_404(emp_code)
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or rec["status"]
    if new_status in NOT_WORKING:
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable(f"{new_status} clears the punch times; do not send punch_in / punch_out", "body")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise unprocessable("a presence status needs a punch_in", "body", "punch_in")
        if body.punch_in is not None and attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            raise unprocessable("punch_in must stay on the record's attendance date", "body", "punch_in")
        if new_out is not None and (body.punch_in is not None or body.punch_out is not None):
            gap = (trunc_s(new_out) - trunc_s(new_in)).total_seconds()
            if gap <= 0 or gap > MAX_SHIFT_SECONDS:
                raise unprocessable("punch_out must be after punch_in and within 24 hours of it", "body", "punch_out")

    old = {
        "status": rec["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day")),
    }
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derive(new_status, new_in, new_out, date, emp)}
    changes = {k: {"from": old[k], "to": new[k]} for k in old if old[k] != new[k]}
    if not changes:
        raise unprocessable("the request changes nothing", "body")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: the update only applies if the fields we read (and the length of history)
    # are unchanged. A concurrent writer makes this match nothing -> 409, and its history entry is never lost.
    history = rec.get("history")
    guard = {
        "_id": rec["_id"],
        "status": old["status"],
        "punch_in": old_in,
        "punch_out": old_out,
        "history": {"$size": len(history)} if isinstance(history, list) else history,
    }
    updated = logs.find_one_and_update(
        guard,
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "the record was modified concurrently; retry")
    return attendance_out(updated)


# --------------------------------------------------------------------------- #
# 5d. Analytics - aggregation pipeline builders (shared with /admin/explain)
# --------------------------------------------------------------------------- #
# Expressions over one attendance_logs document. They read STORED values only (never recompute from punches).
_IS_PRESENT = {"$in": ["$status", PRESENCE]}
# dates are "YYYY-MM-DD" strings; parse as UTC midnight, then ISO weekday 1..7 (Mon..Sun). A bad string counts as Sunday.
_IS_WEEKDAY = {
    "$lte": [
        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "onError": datetime(1970, 1, 4, tzinfo=UTC)}}},
        5,
    ]
}
_HALVES = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 1, 2]}  # a present day = 2 halves, a half day = 1
_IS_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}


def _log_totals() -> dict:
    """$group accumulators shared by the employee and department summaries. Present days are counted in
    halves (exact integers) and work hours are summed as Decimal128, so no floating point drift reaches R8 rounding."""
    return {
        "present_halves": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, _HALVES, 0]}},  # R6, R7
        "hours_sum": {"$sum": {"$cond": [_HAS_HOURS, {"$toDecimal": "$work_hours"}, 0]}},
        "hours_n": {"$sum": {"$cond": [_HAS_HOURS, 1, 0]}},
        "late_count": {"$sum": {"$cond": [_IS_LATE, 1, 0]}},
        "late_minutes": {"$sum": {"$cond": [_IS_LATE, "$late_minutes", 0]}},
        "overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
    }


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},  # IXSCAN (emp_code, date)
        {"$group": {"_id": None, **_log_totals()}},
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    match: dict = {"joined_on": {"$lte": end}}  # R9: joined on or before the last day, with or without logs
    if department is not None:
        match["department"] = department
    return [
        {"$match": match},
        # One row per employee, so employees with zero logs survive (an inner join would drop them).
        # The per-employee totals are computed inside the lookup; the outer $group only adds them up.
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": start, "$lte": end}}},
                    {"$group": {"_id": None, **_log_totals()}},
                ],
                "as": "t",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                **{
                    f: {"$sum": {"$ifNull": [{"$first": f"$t.{f}"}, 0]}}
                    for f in ("present_halves", "hours_sum", "hours_n", "late_count", "late_minutes", "leave_count", "on_duty_count")
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    start, end = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},  # IXSCAN on the date index
        {
            "$group": {
                "_id": "$emp_code",
                "total": {"$sum": {"$cond": [_IS_LATE, "$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [_IS_LATE, 1, 0]}},
            }
        },
        {"$match": {"total": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped here, BEFORE ranking
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4); the window is ordered by the total only.
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$e.name",
                "department": "$e.department",
                "total_late_minutes": "$total",
                "late_count": 1,
            }
        },
    ]
    return pipeline


def check_trend_range(d_from: str, d_to: str) -> None:
    if d_to < d_from:
        raise unprocessable("'to' must not be before 'from'", "query", "to")
    if (Date.fromisoformat(d_to) - Date.fromisoformat(d_from)).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(f"the range may not exceed {MAX_TREND_DAYS} days", "query", "to")


def department_trend_pipeline(department: str, d_from: str, d_to: str) -> list:
    lo = datetime.fromisoformat(d_from).replace(tzinfo=UTC)
    hi = datetime.fromisoformat(d_to).replace(tzinfo=UTC) + timedelta(days=1)
    return [
        # Branch 1 (employees): how many people become part of the headcount on each day.
        # Anyone who joined before 'from' is counted on 'from'; anyone who joins after 'to' is excluded.
        {"$match": {"department": department, "joined_on": {"$lte": d_to}}},
        {"$group": {"_id": {"$cond": [{"$lt": ["$joined_on", d_from]}, d_from, "$joined_on"]}, "joined": {"$sum": 1}}},
        {"$project": {"_id": 0, "day": "$_id", "joined": 1}},
        # Branch 2 (logs of the department's employees): halves present and late records per day.
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {
                        "$lookup": {
                            "from": "attendance_logs",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "pipeline": [
                                {"$match": {"date": {"$gte": d_from, "$lte": d_to}}},
                                {"$project": {"_id": 0, "date": 1, "ph": {"$cond": [_IS_PRESENT, _HALVES, 0]}, "late": {"$cond": [_IS_LATE, 1, 0]}}},
                            ],
                            "as": "l",
                        }
                    },
                    {"$unwind": "$l"},
                    {"$group": {"_id": "$l.date", "ph": {"$sum": "$l.ph"}, "late": {"$sum": "$l.late"}}},
                    {"$project": {"_id": 0, "day": "$_id", "ph": 1, "late": 1}},
                ],
            }
        },
        # Anchor row so $densify always has input, even if nobody joined and nobody logged in the range.
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {"$project": {"_id": 0, "day": {"$literal": d_from}, "joined": {"$literal": 0}}},
                ],
            }
        },
        {
            "$group": {
                "_id": "$day",
                "joined": {"$sum": {"$ifNull": ["$joined", 0]}},
                "ph": {"$sum": {"$ifNull": ["$ph", 0]}},
                "late": {"$sum": {"$ifNull": ["$late", 0]}},
            }
        },
        {"$set": {"date": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$sort": {"date": 1}},
        # Gap filling: one row for every calendar day of the range.
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$match": {"date": {"$gte": lo, "$lt": hi}}},
        # Headcount = running total of joiners.
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {"headcount": {"$sum": {"$ifNull": ["$joined", 0]}, "window": {"documents": ["unbounded", "current"]}}},
            }
        },
        {
            "$set": {
                "ph": {"$ifNull": ["$ph", 0]},
                "late": {"$ifNull": ["$late", 0]},
                "is_wd": {"$lte": [{"$isoDayOfWeek": "$date"}, 5]},
            }
        },
        {
            "$set": {
                "rate": {
                    "$cond": [
                        {"$and": ["$is_wd", {"$gt": ["$headcount", 0]}]},
                        {"$divide": [{"$divide": ["$ph", 2]}, "$headcount"]},
                        None,
                    ]
                }
            }
        },
        # Moving average over the current row and up to 6 previous rows; nulls are ignored by $avg.
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"ma": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "is_working_day": "$is_wd",
                "headcount": 1,
                "present_halves": "$ph",
                "late_count": "$late",
                "rate": 1,
                "ma": 1,
            }
        },
    ]


# --------------------------------------------------------------------------- #
# 5e. Analytics endpoints
# --------------------------------------------------------------------------- #
def _int(v: Any) -> int:
    return int(round(v or 0))


def _decimal(v: Any) -> Decimal:
    return v.to_decimal() if isinstance(v, Decimal128) else Decimal(str(v or 0))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = _employee_or_404(emp_code)
    rows = list(logs.aggregate(employee_monthly_pipeline(emp_code, month)))
    t = rows[0] if rows else {}
    wd = working_days(month, emp["joined_on"])
    halves = _int(t.get("present_halves"))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": halves / 2,
        "leave_days": _int(t.get("leave_count")),
        "late_count": _int(t.get("late_count")),
        "total_late_minutes": _int(t.get("late_minutes")),
        "total_overtime_minutes": _int(t.get("overtime_minutes")),
        "attendance_pct": half_up(Decimal(halves) * 50 / Decimal(wd), 2) if wd else None,  # (halves/2)/wd*100
    }


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    items = []
    for r in employees.aggregate(department_summary_pipeline(month, department)):
        n = _int(r["hours_n"])
        items.append(
            {
                "department": r["_id"],
                "headcount": _int(r["headcount"]),
                "present_days": _int(r["present_halves"]) / 2,
                # mean over RECORDS: total hours / number of records (not a mean of per-employee means)
                "avg_work_hours": half_up(_decimal(r["hours_sum"]) / n, 2) if n else None,
                "late_count": _int(r["late_count"]),
                "total_late_minutes": _int(r["late_minutes"]),
                "leave_count": _int(r["leave_count"]),
                "on_duty_count": _int(r["on_duty_count"]),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    items = [
        {
            "rank": _int(r["rank"]),
            "emp_code": r["emp_code"],
            "name": r["name"],
            "department": r["department"],
            "total_late_minutes": _int(r["total_late_minutes"]),
            "late_count": _int(r["late_count"]),
        }
        for r in logs.aggregate(late_leaderboard_pipeline(month, limit, department))
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[ISODate, Query(alias="from")], to: ISODate):
    check_trend_range(from_, to)
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "unknown department")
    items = []
    for r in employees.aggregate(department_trend_pipeline(department, from_, to)):
        items.append(
            {
                "date": r["date"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": _int(r["headcount"]),
                "present_count": _int(r["present_halves"]) / 2,
                "late_count": _int(r["late_count"]),
                "attendance_rate": half_up(r["rate"], 4) if r.get("rate") is not None else None,
                "moving_avg_7d": half_up(r["ma"], 4) if r.get("ma") is not None else None,
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 5f. Admin: explain
# --------------------------------------------------------------------------- #
def _need(value: Any, name: str) -> Any:
    if value is None:
        raise RequestValidationError([{"type": "missing", "loc": ("query", name), "msg": "Field required", "input": None}])
    return value


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[Month] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[ISODate] = None,
    date_to: Optional[ISODate] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[ISODate], Query(alias="from")] = None,
    to: Optional[ISODate] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    """executionStats explain of the endpoint's main query, built by the same functions the endpoint uses."""
    if endpoint == "attendance_list":
        collection = logs.name
        command: dict = {
            "find": collection,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        if endpoint == "employee_monthly":
            collection = logs.name
            pipeline = employee_monthly_pipeline(_need(emp_code, "emp_code"), _need(month, "month"))
        elif endpoint == "department_summary":
            collection = employees.name
            pipeline = department_summary_pipeline(_need(month, "month"), department)
        elif endpoint == "late_leaderboard":
            collection = logs.name
            pipeline = late_leaderboard_pipeline(_need(month, "month"), limit, department)
        else:  # department_trend
            collection = employees.name
            _need(department, "department"), _need(from_, "from"), _need(to, "to")
            check_trend_range(from_, to)
            pipeline = department_trend_pipeline(department, from_, to)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(raw))}
