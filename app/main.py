"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded for convenience; real env vars win)

Layout of this single file
  1. configuration, indexes, startup
  2. time + rule helpers (R1-R5, R8)       <- pure functions, easy to unit test
  3. request models (validation -> 422)
  4. serialisation (Mongo document -> API JSON)
  5. endpoints: system, employees, attendance
  6. pipeline builders + analytics endpoints
  7. explain endpoint (re-uses the exact same builders as the real endpoints)
"""
import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration, indexes, startup
# --------------------------------------------------------------------------- #
load_dotenv()  # python-dotenv never overrides variables that are already set

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# tz_aware=True: PyMongo hands back aware UTC datetimes instead of naive ones.
client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=5000)
db = client[MONGO_DB]
employees = db["employees"]
logs = db["attendance_logs"]


def ensure_indexes() -> None:
    """Idempotent: creating an index that already exists is a no-op."""
    # employees
    employees.create_index([("emp_code", ASCENDING)], unique=True)  # identity + 409 on duplicate
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])  # filtered, sorted list
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])  # headcount per dept
    employees.create_index([("joined_on", ASCENDING)])  # headcount without a dept filter
    # attendance_logs
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)  # one record/day + $lookup join
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])  # global list order + month scans
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])  # punch-out finds latest punch-in
    logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)])  # status filter


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 2. Time + rule helpers
# --------------------------------------------------------------------------- #
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")  # R6
GRACE_SECONDS = 10 * 60  # R2
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000  # contract range for epoch milliseconds


# --- helpers:begin (pure functions, no database) ---
def trunc(dt: datetime) -> datetime:
    """Aware UTC datetime truncated to whole seconds (R1)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).replace(microsecond=0)


def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to the second (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: datetime) -> int:
    """Datetime -> epoch milliseconds using integer arithmetic (no float rounding)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def round_half_up(value: Any, places: int) -> float:
    """R8: half-up rounding (Python's round() is banker's rounding on floats)."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _minutes(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def shift_context(shift_start: str, shift_end: str, punch_in: datetime):
    """R1. Returns (attendance_date 'YYYY-MM-DD', shift_start_dt, shift_end_dt), all IST-aware.

    Overnight shift (end <= start): a punch-in earlier than shift_end (IST) belongs to the shift
    that started the previous day, and the shift ends on the next calendar day.
    """
    start_min, end_min = _minutes(shift_start), _minutes(shift_end)
    overnight = end_min <= start_min
    local = trunc(punch_in).astimezone(IST)
    day = local.date()
    if overnight:
        seconds_of_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_of_day < end_min * 60:
            day -= timedelta(days=1)
    start = datetime.combine(day, time(start_min // 60, start_min % 60), tzinfo=IST)
    end_day = day + timedelta(days=1) if overnight else day
    end = datetime.combine(end_day, time(end_min // 60, end_min % 60), tzinfo=IST)
    return day.isoformat(), start, end


def derive(shift_start: str, shift_end: str, status: str, punch_in: Optional[datetime],
           punch_out: Optional[datetime]) -> dict:
    """Derived fields from R2-R5 for a record in its final state."""
    if status not in PRESENCE or punch_in is None:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    pin = trunc(punch_in)
    _, start, end = shift_context(shift_start, shift_end, pin)
    # R2: strictly more than 10:00 after shift start; then whole minutes since shift start.
    late_seconds = int((pin - start).total_seconds())
    late = late_seconds // 60 if late_seconds > GRACE_SECONDS else 0
    out = {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    if punch_out is not None:
        pout = trunc(punch_out)
        # R3: whole minutes after shift end, only when at least 30.
        ot_seconds = int((pout - end).total_seconds())
        out["overtime_minutes"] = ot_seconds // 60 if ot_seconds >= 30 * 60 else 0
        # R4: seconds / 3600, 2 decimals half-up, using Decimal so no float surprises.
        hours = (Decimal(int((pout - pin).total_seconds())) / Decimal(3600)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP)
        out["work_hours"] = float(hours)
        out["half_day"] = hours < Decimal("4.50")  # R5 uses the ROUNDED value
    return out
# --- helpers:end ---


def parse_date(value: str, name: str = "date") -> Date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value or ""):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return Date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} is not a real calendar date")


def month_bounds(month: str):
    """'2026-07' -> ('2026-07-01', '2026-07-31', date, date)."""
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    year, mon = int(month[:4]), int(month[5:])
    first = Date(year, mon, 1)
    last = Date(year, mon, calendar.monthrange(year, mon)[1])
    return first.isoformat(), last.isoformat(), first, last


def count_weekdays(first: Date, last: Date) -> int:
    """Mon-Fri days in [first, last] inclusive (R7). Pure calendar arithmetic, no data."""
    n, day = 0, first
    while day <= last:
        if day.weekday() < 5:
            n += 1
        day += timedelta(days=1)
    return n


# --------------------------------------------------------------------------- #
# 3. Request models (anything invalid -> automatic 422)
# --------------------------------------------------------------------------- #
# strict=True: floats, strings and bools are rejected; ge/le reject "seconds-looking" numbers.
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
AnyStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
HM_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HM_PATTERN)
    shift_end: str = Field("18:30", pattern=HM_PATTERN)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        Date.fromisoformat(v)  # raises ValueError for 2026-02-30
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    status: Optional[AnyStatus] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    # derived fields sent by a client are simply ignored (pydantic drops unknown keys)


# --------------------------------------------------------------------------- #
# 4. Serialisation: Mongo document -> API JSON  (never leak _id)
# --------------------------------------------------------------------------- #
def _api_value(v: Any) -> Any:
    return dt_to_ms(v) if isinstance(v, datetime) else v


def employee_out(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"], "name": d.get("name"), "email": d.get("email"),
        "department": d.get("department"), "shift_start": d.get("shift_start"),
        "shift_end": d.get("shift_end"), "joined_on": d.get("joined_on"),
        "created_at": _api_value(d.get("created_at")),
    }


def record_out(d: dict) -> dict:
    """Legacy documents may miss late_minutes / overtime_minutes / half_day / history."""
    history = []
    for h in d.get("history") or []:
        changes = {k: {"from": _api_value(c.get("from")), "to": _api_value(c.get("to"))}
                   for k, c in (h.get("changes") or {}).items()}
        history.append({"at": _api_value(h.get("at")), "by": h.get("by"),
                        "reason": h.get("reason"), "changes": changes})
    return {
        "emp_code": d["emp_code"], "date": d["date"], "status": d["status"],
        "punch_in": _api_value(d.get("punch_in")), "punch_out": _api_value(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# 5. Endpoints: system, employees, attendance
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    created = datetime.now(UTC)
    created = created.replace(microsecond=created.microsecond // 1000 * 1000)  # Mongo keeps ms
    doc = body.model_dump()
    doc["created_at"] = created
    try:
        employees.insert_one(doc)  # the unique index decides, not a find-then-insert check
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = employees.count_documents(q)  # total reflects the filter (R10)
    cursor = (employees.find(q).sort("emp_code", ASCENDING)
              .skip((page - 1) * page_size).limit(page_size))
    return {"items": [employee_out(d) for d in cursor], "total": total,
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    day, _, _ = shift_context(emp["shift_start"], emp["shift_end"], ts)
    doc = {
        "emp_code": body.emp_code, "date": day, "status": body.status,
        "punch_in": ts, "punch_out": None,
        **derive(emp["shift_start"], emp["shift_end"], body.status, ts, None),
        "history": [],
    }
    try:
        # Race safety: the unique index on (emp_code, date) lets exactly one insert win.
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # Latest record whose punch_in <= ts. Works across midnight because we look at the
    # instant, not at the calendar date of the punch-out.
    rec = logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
                        sort=[("punch_in", DESCENDING)])
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = trunc(rec["punch_in"])
    if ts <= pin:
        raise HTTPException(422, "punched_at must be after punch_in")
    if ts - pin > timedelta(hours=24):
        raise HTTPException(422, "punch_out is more than 24 hours after punch_in")
    new = derive(emp["shift_start"], emp["shift_end"], rec["status"], pin, ts)
    # Atomic compare-and-set: only succeeds while punch_out is still empty, so of two
    # simultaneous punch-outs exactly one matches and the other gets None -> 409.
    updated = logs.find_one_and_update(
        {"emp_code": rec["emp_code"], "date": rec["date"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": new["work_hours"],
                  "overtime_minutes": new["overtime_minutes"], "half_day": new["half_day"]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    """Shared by GET /attendance and its explain."""
    if date_from:
        d_from = parse_date(date_from, "date_from")
    if date_to:
        d_to = parse_date(date_to, "date_to")
    if date_from and date_to and d_from > d_to:
        raise HTTPException(422, "date_from must not be after date_to")
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


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[AnyStatus] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = logs.count_documents(q)
    cursor = logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cursor], "total": total,
            "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    parse_date(date, "date")
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    rec = logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    stored_in = trunc(rec["punch_in"]) if rec.get("punch_in") else None
    stored_out = trunc(rec["punch_out"]) if rec.get("punch_out") else None
    new_status = body.status or rec["status"]

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE clear the punch times; do not supply them")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else stored_in
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else stored_out
        if body.punch_in is not None:
            day, _, _ = shift_context(emp["shift_start"], emp["shift_end"], new_in)
            if day != date:
                raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if (body.punch_in is not None or body.punch_out is not None) and new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                raise HTTPException(422, "punch_out is more than 24 hours after punch_in")

    if new_status == rec["status"] and new_in == stored_in and new_out == stored_out:
        raise HTTPException(422, "request changes nothing")

    new_vals = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
                **derive(emp["shift_start"], emp["shift_end"], new_status, new_in, new_out)}
    old_vals = {
        "status": rec["status"], "punch_in": stored_in, "punch_out": stored_out,
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
    }
    changes = {k: {"from": old_vals[k], "to": v} for k, v in new_vals.items() if old_vals[k] != v}
    now = datetime.now(UTC)
    entry = {"at": now.replace(microsecond=now.microsecond // 1000 * 1000),
             "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the update only matches if nobody changed the record since we
    # read it. "history.<n> does not exist" means no one appended after our read.
    n_history = len(rec.get("history") or [])
    guard = {"emp_code": emp_code, "date": date, f"history.{n_history}": {"$exists": False},
             "status": rec["status"], "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out")}
    updated = logs.find_one_and_update(
        guard, {"$set": new_vals, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER)
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# 6. Analytics: pipeline builders (shared with the explain endpoint) + endpoints
# --------------------------------------------------------------------------- #
class E:
    """Aggregation expression snippets.  prefix '$' = field of the current document,
    prefix '$$l.' = field of the loop variable l inside $map/$filter over a joined array."""

    def __init__(self, prefix: str = "$"):
        f = lambda name: prefix + name  # noqa: E731
        self.is_present = {"$in": [f("status"), list(PRESENCE)]}
        self.is_weekday = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": f("date")}}}, [2, 3, 4, 5, 6]]}
        self.half = {"$ifNull": [f("half_day"), False]}
        self.late = {"$ifNull": [f("late_minutes"), 0]}
        self.overtime = {"$ifNull": [f("overtime_minutes"), 0]}
        self.is_late = {"$gt": [self.late, 0]}
        self.is_leave = {"$eq": [f("status"), "LEAVE"]}
        self.is_on_duty = {"$eq": [f("status"), "ON_DUTY"]}
        self.has_hours = {"$and": [self.is_present, {"$isNumber": f("work_hours")}]}
        self.work_hours = f("work_hours")
        day_value = {"$cond": [self.half, 0.5, 1]}  # a half day counts 0.5 (R5)
        self.present_any_day = {"$cond": [self.is_present, day_value, 0]}
        self.present_weekday = {"$cond": [{"$and": [self.is_present, self.is_weekday]}, day_value, 0]}  # R7


def _flag(cond: dict) -> dict:
    return {"$cond": [cond, 1, 0]}


def build_employee_monthly(emp_code: str, month: str) -> list:
    start, end, _, _ = month_bounds(month)
    e = E("$")
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": e.present_weekday},
            "leave_days": {"$sum": _flag(e.is_leave)},
            "late_count": {"$sum": _flag(e.is_late)},
            "total_late_minutes": {"$sum": e.late},
            "total_overtime_minutes": {"$sum": e.overtime},
        }},
    ]


def build_department_summary(month: str, department: Optional[str]) -> list:
    """Start from employees (so people with zero logs still count), then join their logs."""
    start, end, _, _ = month_bounds(month)
    emp_match: dict = {"joined_on": {"$lte": end}}  # R9
    if department:
        emp_match["department"] = department
    e = E("$$l.")

    def over_logs(expr):  # per-employee sum of an expression over the joined logs
        return {"$sum": {"$map": {"input": "$logs", "as": "l", "in": expr}}}

    def count_logs(cond):
        return {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": cond}}}

    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": start, "$lte": end}}}],
            "as": "logs",
        }},
        {"$addFields": {
            "p_days": over_logs(e.present_weekday),
            "p_late_count": count_logs(e.is_late),
            "p_late_minutes": over_logs(e.late),
            "p_leave": count_logs(e.is_leave),
            "p_on_duty": count_logs(e.is_on_duty),
            "p_hours_sum": over_logs({"$cond": [e.has_hours, e.work_hours, 0]}),
            "p_hours_n": over_logs(_flag(e.has_hours)),
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$p_days"},
            "late_count": {"$sum": "$p_late_count"},
            "total_late_minutes": {"$sum": "$p_late_minutes"},
            "leave_count": {"$sum": "$p_leave"},
            "on_duty_count": {"$sum": "$p_on_duty"},
            "hours_sum": {"$sum": "$p_hours_sum"},
            "hours_n": {"$sum": "$p_hours_n"},  # average over RECORDS, not over employee averages
        }},
        {"$sort": {"_id": 1}},
    ]


def build_late_leaderboard(month: str, limit: int, department: Optional[str]) -> list:
    start, end, _, _ = month_bounds(month)
    e = E("$")
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": e.late},
                    "late_count": {"$sum": _flag(e.is_late)}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees disappear here
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # rank only inside the department
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next rank is skipped.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def build_department_trend(department: str, d_from: str, d_to: str) -> list:
    first, last = parse_date(d_from, "from"), parse_date(d_to, "to")
    if last < first:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (last - first).days + 1 > 92:
        raise HTTPException(422, "range exceeds 92 days")
    members = list(employees.find({"department": department}, {"emp_code": 1, "joined_on": 1, "_id": 0}))
    if not members:
        raise HTTPException(404, "unknown department")
    codes = [m["emp_code"] for m in members]
    joined = [m["joined_on"] for m in members]
    lo = datetime(first.year, first.month, first.day, tzinfo=UTC)
    hi = datetime(last.year, last.month, last.day, tzinfo=UTC) + timedelta(days=1)  # upper bound is exclusive
    e = E("$")
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": d_from, "$lte": d_to}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": e.present_any_day},
                    "late_count": {"$sum": _flag(e.is_late)}}},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        # gap filling: one document per calendar day in [from, to]
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$addFields": {
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
        }},
        {"$addFields": {  # R9: employees who have joined on or before that day
            "headcount": {"$size": {"$filter": {"input": joined, "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}},
        }},
        {"$addFields": {
            "attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                                          {"$divide": ["$present_count", "$headcount"]}, None]},
        }},
        # $avg ignores nulls; window = current row + up to 6 previous rows inside the range
        {"$setWindowFields": {"sortBy": {"day": 1},
                              "output": {"moving_avg": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"day": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
                      "late_count": 1, "attendance_rate": 1, "moving_avg_7d": "$moving_avg"}},
    ]


def _rate(v: Any) -> Optional[float]:
    return None if v is None else round_half_up(v, 4)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")):
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    _, _, first, last = month_bounds(month)
    row = next(iter(logs.aggregate(build_employee_monthly(emp_code, month))), None) or {}
    present = float(row.get("present_days", 0))
    # R7: working days count from joined_on for mid-month joiners (pure calendar arithmetic)
    joined = Date.fromisoformat(emp["joined_on"])
    working_days = count_weekdays(max(first, joined), last) if joined <= last else 0
    pct = round_half_up(Decimal(str(present)) / Decimal(working_days) * 100, 2) if working_days else None
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": present, "leave_days": int(row.get("leave_days", 0)),
        "late_count": int(row.get("late_count", 0)),
        "total_late_minutes": int(row.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
                       department: Optional[str] = None):
    items = []
    for r in employees.aggregate(build_department_summary(month, department)):
        items.append({
            "department": r["_id"], "headcount": int(r["headcount"]),
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": round_half_up(r["hours_sum"] / r["hours_n"], 2) if r["hours_n"] else None,
            "late_count": int(r["late_count"]), "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]), "on_duty_count": int(r["on_duty_count"]),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Query(..., pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
                     limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    rows = logs.aggregate(build_late_leaderboard(month, limit, department))
    items = [{"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"],
              "department": r["department"], "total_late_minutes": int(r["total_late_minutes"]),
              "late_count": int(r["late_count"])} for r in rows]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    rows = logs.aggregate(build_department_trend(department, from_, to))
    items = [{
        "date": r["date"], "is_working_day": bool(r["is_working_day"]),
        "headcount": int(r["headcount"]), "present_count": float(r["present_count"]),
        "late_count": int(r["late_count"]), "attendance_rate": _rate(r.get("attendance_rate")),
        "moving_avg_7d": _rate(r.get("moving_avg_7d")),
    } for r in rows]
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# 7. Explain endpoint - uses the SAME builders as the real endpoints
# --------------------------------------------------------------------------- #
def _explain(collection: str, *, find: Optional[dict] = None, pipeline: Optional[list] = None) -> dict:
    inner = ({"aggregate": collection, "pipeline": pipeline, "cursor": {}} if pipeline is not None
             else {"find": collection, **(find or {})})
    raw = db.command({"explain": inner, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(raw))  # BSON types -> plain JSON


def _need(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing query parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[AnyStatus] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        q = attendance_filter(emp_code, date_from, date_to, status)
        find = {"filter": q, "sort": dict(ATTENDANCE_SORT), "skip": (page - 1) * page_size, "limit": page_size}
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _explain("attendance_logs", find=find)}
    if endpoint == "employee_monthly":
        _need(emp_code=emp_code, month=month)
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _explain("attendance_logs", pipeline=build_employee_monthly(emp_code, month))}
    if endpoint == "department_summary":
        _need(month=month)
        return {"endpoint": endpoint, "collection": "employees",
                "explain": _explain("employees", pipeline=build_department_summary(month, department))}
    if endpoint == "late_leaderboard":
        _need(month=month)
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _explain("attendance_logs", pipeline=build_late_leaderboard(month, limit, department))}
    _need(department=department, **{"from": from_}, to=to)
    return {"endpoint": endpoint, "collection": "attendance_logs",
            "explain": _explain("attendance_logs", pipeline=build_department_trend(department, from_, to))}
