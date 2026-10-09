"""Employee Attendance & Analytics API."""

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = (*PRESENCE_STATUSES, "ABSENT", "LEAVE")
EpochMillis = Annotated[
    StrictInt, Field(ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
]
MonthString = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]
PageNumber = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@app.on_event("startup")
def create_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employee_department_code",
    )
    db.employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_employee_department_joined",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)],
        name="ix_employee_joined_department",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_employee_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_employee",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)],
        name="ix_attendance_employee_punch_in",
    )


def _invalid(message: str) -> None:
    raise HTTPException(status_code=422, detail=message)


def _datetime_from_ms(value: int) -> datetime:
    return (datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)).replace(
        microsecond=0
    )


def _truncate_seconds(value: datetime) -> datetime:
    return value.replace(microsecond=0)


def _epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    value = _truncate_seconds(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def _time_minutes(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def _is_overnight(shift_start: str, shift_end: str) -> bool:
    return _time_minutes(shift_end) <= _time_minutes(shift_start)


def _attendance_day(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = punch_in.astimezone(IST)
    day = local.date()
    if _is_overnight(shift_start, shift_end) and (
        local.hour * 60 + local.minute < _time_minutes(shift_end)
    ):
        day -= timedelta(days=1)
    return day


def _shift_instant(day: date, shift_time: str) -> datetime:
    hour, minute = map(int, shift_time.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_day: date) -> int:
    shift_start_at = _shift_instant(attendance_day, shift_start)
    elapsed = int(
        (_truncate_seconds(punch_in).astimezone(IST) - shift_start_at).total_seconds()
    )
    if elapsed <= 10 * 60:
        return 0
    return elapsed // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    duration = _truncate_seconds(punch_out) - _truncate_seconds(punch_in)
    elapsed_microseconds = (
        duration.days * 86_400_000_000
        + duration.seconds * 1_000_000
        + duration.microseconds
    )
    hours = Decimal(elapsed_microseconds) / Decimal(3_600_000_000)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    attendance_day: date,
    shift_start: str,
) -> int:
    end_day = attendance_day + timedelta(days=int(_is_overnight(shift_start, shift_end)))
    shift_end_at = _shift_instant(end_day, shift_end)
    elapsed = int(
        (_truncate_seconds(punch_out).astimezone(IST) - shift_end_at).total_seconds()
    )
    whole_minutes = elapsed // 60
    return whole_minutes if whole_minutes >= 30 else 0


def _rounded(value: int | float | Decimal, places: int) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def _month_range(month: str) -> tuple[str, str]:
    year, month_number = map(int, month.split("-"))
    first = date(year, month_number, 1)
    if month_number == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month_number + 1, 1)
    return first.isoformat(), (next_month - timedelta(days=1)).isoformat()


def _working_days(first: date, last: date) -> int:
    if first > last:
        return 0
    whole_weeks, remainder = divmod((last - first).days + 1, 7)
    count = whole_weeks * 5
    for offset in range(remainder):
        if (first.weekday() + offset) % 7 < 5:
            count += 1
    return count


def _serialize_record(document: dict) -> dict:
    result = {key: value for key, value in document.items() if key != "_id"}
    result.setdefault("punch_in", None)
    result.setdefault("punch_out", None)
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    result.setdefault("half_day", False)
    history = []
    for entry in result.get("history") or []:
        serialized_entry = dict(entry)
        serialized_entry["at"] = _epoch_ms(serialized_entry.get("at"))
        changes = {}
        for field, change in serialized_entry.get("changes", {}).items():
            item = dict(change)
            if field in ("punch_in", "punch_out"):
                item["from"] = _epoch_ms(item.get("from"))
                item["to"] = _epoch_ms(item.get("to"))
            changes[field] = item
        serialized_entry["changes"] = changes
        history.append(serialized_entry)
    result["history"] = history
    result["punch_in"] = _epoch_ms(result["punch_in"])
    result["punch_out"] = _epoch_ms(result["punch_out"])
    return result


def _month_log_match(emp_code: str, month: str) -> dict:
    start, end = _month_range(month)
    return {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}


def _employee_monthly_pipeline(emp_code: str, month: str) -> list[dict]:
    return [
        {"$match": _month_log_match(emp_code, month)},
        {
            "$set": {
                "_weekday": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    "$_weekday",
                                ]
                            },
                            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
        {"$project": {"_id": 0}},
    ]


def _department_summary_pipeline(month: str, department: str | None) -> list[dict]:
    start, end = _month_range(month)
    employee_match: dict = {"joined_on": {"$lte": end}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", start]},
                                    {"$lte": ["$date", end]},
                                ]
                            }
                        }
                    },
                    {
                        "$set": {
                            "_weekday": {
                                "$in": [
                                    {
                                        "$dayOfWeek": {
                                            "$dateFromString": {
                                                "dateString": "$date",
                                                "format": "%Y-%m-%d",
                                            }
                                        }
                                    },
                                    [2, 3, 4, 5, 6],
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                                "$_weekday",
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$eq": [{"$ifNull": ["$half_day", False]}, True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                                {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                                {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                            },
                        }
                    },
                ],
                "as": "monthly",
            }
        },
        {"$unwind": {"path": "$monthly", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$monthly.present_days", 0]}},
                "work_hours_sum": {"$sum": {"$ifNull": ["$monthly.work_hours_sum", 0]}},
                "work_hours_count": {"$sum": {"$ifNull": ["$monthly.work_hours_count", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$monthly.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$monthly.total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$monthly.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$monthly.on_duty_count", 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]


def _leaderboard_pipeline(month: str, limit: int, department: str | None) -> list[dict]:
    start, end = _month_range(month)
    pipeline: list[dict] = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    return pipeline


def _department_trend_pipeline(
    department: str, first: date, last: date
) -> list[dict]:
    start_datetime = datetime.combine(first, time.min, tzinfo=UTC)
    end_datetime = datetime.combine(last, time.min, tzinfo=UTC)
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": "$department"}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "dates": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": start_datetime,
                                                "endDate": end_datetime,
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": start_datetime,
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$dates"},
        {
            "$set": {
                "date": {"$dateToString": {"date": "$dates", "format": "%Y-%m-%d", "timezone": "UTC"}},
                "_day_of_week": {"$dayOfWeek": "$dates"},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date", "department": "$department"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", "$$department"]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "_headcount",
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date", "department": "$department"},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "_employee",
                        }
                    },
                    {"$unwind": "$_employee"},
                    {"$match": {"$expr": {"$eq": ["$_employee.department", "$$department"]}}},
                    {"$project": {"status": 1, "late_minutes": 1, "half_day": 1}},
                ],
                "as": "_logs",
            }
        },
        {
            "$set": {
                "is_working_day": {"$not": [{"$in": ["$_day_of_week", [1, 7]]}]},
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$_headcount.count", 0]}, 0]},
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": "$_logs",
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$in": ["$$log.status", list(PRESENCE_STATUSES)]},
                                    {
                                        "$cond": [
                                            {"$eq": [{"$ifNull": ["$$log.half_day", False]}, True]},
                                            Decimal128("0.5"),
                                            Decimal128("1"),
                                        ]
                                    },
                                    Decimal128("0"),
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$_logs",
                            "as": "log",
                            "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]},
                        }
                    }
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            {"$toDecimal": "$present_count"},
                                                            {"$toDecimal": "$headcount"},
                                                        ]
                                                    },
                                                    Decimal128("10000"),
                                                ]
                                            },
                                            Decimal128("0.5"),
                                        ]
                                    }
                                },
                                Decimal128("10000"),
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"dates": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]


def _attendance_query(
    emp_code: str | None,
    date_from: date | None,
    date_to: date | None,
    status: str | None,
) -> dict:
    query: dict = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to is not None:
            query["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        query["status"] = status
    return query


def _explain_aggregate(collection: str, pipeline: list[dict]) -> dict:
    return db.command(
        {
            "explain": {
                "aggregate": collection,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
    )


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EmployeeIn(ApiModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be YYYY-MM-DD") from exc
        if len(value) != 10:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(ApiModel):
    emp_code: str
    punched_at: EpochMillis | None = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def timestamp_not_null(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class PunchOutIn(ApiModel):
    emp_code: str
    punched_at: EpochMillis | None = None

    @model_validator(mode="after")
    def timestamp_not_null(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class RegularizeIn(ApiModel):
    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def optional_values_not_null(self):
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


@app.get("/health")
def health() -> dict:
    try:
        db.command("ping")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict:
    created_at = datetime.now(UTC).replace(microsecond=0)
    document = {**body.model_dump(), "created_at": created_at}
    try:
        db.employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    document["created_at"] = _epoch_ms(created_at)
    return document


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: PageNumber = 1,
    page_size: PageSize = 20,
) -> dict:
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    documents = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING)
    items = list(documents.skip((page - 1) * page_size).limit(page_size))
    for employee in items:
        employee["created_at"] = _epoch_ms(employee.get("created_at"))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_at = (
        _datetime_from_ms(body.punched_at)
        if body.punched_at is not None
        else datetime.now(UTC).replace(microsecond=0)
    )
    shift_day = _attendance_day(punched_at, employee["shift_start"], employee["shift_end"])
    document = {
        "emp_code": body.emp_code,
        "date": shift_day.isoformat(),
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, employee["shift_start"], shift_day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for this date") from exc
    return _serialize_record(document)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> dict:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    punched_at = (
        _datetime_from_ms(body.punched_at)
        if body.punched_at is not None
        else datetime.now(UTC).replace(microsecond=0)
    )
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punched_at + timedelta(milliseconds=999)},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="record is already punched out")
    stored_punch_in = record["punch_in"]
    punch_in_at = _truncate_seconds(stored_punch_in)
    elapsed = (punched_at - punch_in_at).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        _invalid("punched_at must be after punch_in and within 24 hours")
    day = date.fromisoformat(record["date"])
    work_hours = compute_work_hours(punch_in_at, punched_at)
    update = {
        "punch_out": punched_at,
        "punch_in": punch_in_at,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(
            punched_at, employee["shift_end"], day, employee["shift_start"]
        ),
        "half_day": work_hours < 4.50,
    }
    result = db.attendance_logs.update_one(
        {
            "emp_code": body.emp_code,
            "date": record["date"],
            "punch_in": stored_punch_in,
            "$or": [{"punch_out": None}, {"punch_out": {"$exists": False}}],
        },
        {"$set": update},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="record is already punched out")
    updated = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "date": record["date"]}, {"_id": 0}
    )
    return _serialize_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    page: PageNumber = 1,
    page_size: PageSize = 20,
) -> dict:
    if date_from is not None and date_to is not None and date_from > date_to:
        _invalid("date_from must be on or before date_to")
    query = _attendance_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [_serialize_record(document) for document in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date_str: Annotated[str, Path(alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$")],
    body: RegularizeIn,
) -> dict:
    try:
        record_day = date.fromisoformat(date_str)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD") from exc
    if record_day.isoformat() != date_str:
        _invalid("date must be YYYY-MM-DD")
    employee = db.employees.find_one({"emp_code": emp_code})
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if employee is None or record is None:
        raise HTTPException(status_code=404, detail="employee or attendance record not found")

    changes_in = body.model_dump(exclude_unset=True)
    changes_in.pop("reason")
    regularized_by = changes_in.pop("regularized_by")
    proposed_status = changes_in.get("status", record.get("status"))
    if proposed_status in ("ABSENT", "LEAVE"):
        if "punch_in" in changes_in or "punch_out" in changes_in:
            _invalid("punch times cannot be supplied with ABSENT or LEAVE")
        final_punch_in = None
        final_punch_out = None
    else:
        final_punch_in = (
            _datetime_from_ms(changes_in["punch_in"])
            if "punch_in" in changes_in
            else (
                _truncate_seconds(record["punch_in"])
                if record.get("punch_in") is not None
                else None
            )
        )
        final_punch_out = (
            _datetime_from_ms(changes_in["punch_out"])
            if "punch_out" in changes_in
            else (
                _truncate_seconds(record["punch_out"])
                if record.get("punch_out") is not None
                else None
            )
        )
        if final_punch_in is None:
            _invalid("presence status requires punch_in")
        if _attendance_day(
            final_punch_in, employee["shift_start"], employee["shift_end"]
        ) != record_day:
            _invalid("punch_in must belong to the record's attendance date")
        if final_punch_out is not None:
            elapsed = (final_punch_out - final_punch_in).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 60 * 60:
                _invalid("punch_out must be after punch_in and within 24 hours")

    late_minutes = (
        compute_late_minutes(final_punch_in, employee["shift_start"], record_day)
        if final_punch_in is not None
        else 0
    )
    work_hours = (
        compute_work_hours(final_punch_in, final_punch_out)
        if final_punch_in is not None and final_punch_out is not None
        else None
    )
    overtime_minutes = (
        compute_overtime(final_punch_out, employee["shift_end"], record_day, employee["shift_start"])
        if final_punch_out is not None
        else 0
    )
    half_day = work_hours is not None and work_hours < 4.50

    new_values = {
        "status": proposed_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }
    old_values = {
        "status": record.get("status"),
        "punch_in": (
            _truncate_seconds(record["punch_in"])
            if record.get("punch_in") is not None
            else None
        ),
        "punch_out": (
            _truncate_seconds(record["punch_out"])
            if record.get("punch_out") is not None
            else None
        ),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    field_changes = {
        field: {"from": old_values[field], "to": new_values[field]}
        for field in new_values
        if old_values[field] != new_values[field]
    }
    if not field_changes:
        _invalid("regularization must change at least one field")

    history_entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": regularized_by,
        "reason": body.reason,
        "changes": field_changes,
    }
    old_history = record.get("history", [])
    history_filter = (
        {"history": old_history}
        if "history" in record
        else {"history": {"$exists": False}}
    )
    result = db.attendance_logs.update_one(
        {"emp_code": emp_code, "date": date_str, **history_filter},
        {"$set": new_values, "$push": {"history": history_entry}},
    )
    if result.modified_count != 1:
        raise HTTPException(status_code=409, detail="attendance record changed concurrently")
    updated = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    return _serialize_record(updated)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthString) -> dict:
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    first, last = _month_range(month)
    result = list(db.attendance_logs.aggregate(_employee_monthly_pipeline(emp_code, month)))
    metrics = result[0] if result else {}
    working_days = _working_days(max(date.fromisoformat(first), date.fromisoformat(employee["joined_on"])), date.fromisoformat(last))
    present_days = Decimal(str(metrics.get("present_days", 0)))
    attendance_pct = (
        _rounded(present_days * Decimal(100) / Decimal(working_days), 2)
        if working_days
        else None
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present_days),
        "leave_days": int(metrics.get("leave_days", 0)),
        "late_count": int(metrics.get("late_count", 0)),
        "total_late_minutes": int(metrics.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(metrics.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: MonthString,
    department: str | None = None,
) -> dict:
    rows = db.employees.aggregate(_department_summary_pipeline(month, department))
    items = []
    for row in rows:
        average = row.get("avg_work_hours")
        items.append(
            {
                "department": row["department"],
                "headcount": int(row["headcount"]),
                "present_days": float(Decimal(str(row.get("present_days", 0)))),
                "avg_work_hours": _rounded(average, 2) if average is not None else None,
                "late_count": int(row.get("late_count", 0)),
                "total_late_minutes": int(row.get("total_late_minutes", 0)),
                "leave_count": int(row.get("leave_count", 0)),
                "on_duty_count": int(row.get("on_duty_count", 0)),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: MonthString,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: str | None = None,
) -> dict:
    pipeline = _leaderboard_pipeline(month, limit, department)
    rows = db.attendance_logs.aggregate(pipeline)
    items = [
        {
            "rank": int(row["rank"]),
            "emp_code": row["emp_code"],
            "name": row["name"],
            "department": row["department"],
            "total_late_minutes": int(row["total_late_minutes"]),
            "late_count": int(row["late_count"]),
        }
        for row in rows
    ]
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: Annotated[date, Query(alias="from")],
    to_date: Annotated[date, Query(alias="to")],
) -> dict:
    if to_date < from_date:
        _invalid("to must be on or after from")
    if (to_date - from_date).days + 1 > 92:
        _invalid("date range cannot exceed 92 days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(status_code=404, detail="department not found")
    rows = db.employees.aggregate(
        _department_trend_pipeline(department, from_date, to_date)
    )
    items = []
    for row in rows:
        rate = row.get("attendance_rate")
        moving_average = row.get("moving_avg_7d")
        items.append(
            {
                "date": row["date"],
                "is_working_day": row["is_working_day"],
                "headcount": int(row["headcount"]),
                "present_count": float(Decimal(str(row["present_count"]))),
                "late_count": int(row["late_count"]),
                "attendance_rate": _rounded(Decimal(str(rate)), 4) if rate is not None else None,
                "moving_avg_7d": (
                    _rounded(Decimal(str(moving_average)), 4)
                    if moving_average is not None
                    else None
                ),
            }
        )
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: str | None = None,
    month: str | None = None,
    department: str | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: date | None = None,
    date_to: date | None = None,
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
    page: PageNumber = 1,
    page_size: PageSize = 20,
    from_date: Annotated[date | None, Query(alias="from")] = None,
    to_date: Annotated[date | None, Query(alias="to")] = None,
) -> dict:
    if month is not None and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month) is None:
        _invalid("month must be YYYY-MM")
    if endpoint == "attendance_list":
        if date_from is not None and date_to is not None and date_from > date_to:
            _invalid("date_from must be on or before date_to")
        query = _attendance_query(emp_code, date_from, date_to, status)
        explain = db.command(
            {
                "explain": {
                    "find": "attendance_logs",
                    "filter": query,
                    "sort": {"date": -1, "emp_code": 1},
                    "skip": (page - 1) * page_size,
                    "limit": page_size,
                },
                "verbosity": "executionStats",
            }
        )
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            _invalid("emp_code and month are required")
        explain = _explain_aggregate(
            "attendance_logs", _employee_monthly_pipeline(emp_code, month)
        )
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if not month:
            _invalid("month is required")
        explain = _explain_aggregate(
            "employees", _department_summary_pipeline(month, department)
        )
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if not month:
            _invalid("month is required")
        explain = _explain_aggregate(
            "attendance_logs", _leaderboard_pipeline(month, limit, department)
        )
        collection = "attendance_logs"
    else:
        if not department or from_date is None or to_date is None:
            _invalid("department, from, and to are required")
        if to_date < from_date:
            _invalid("to must be on or after from")
        if (to_date - from_date).days + 1 > 92:
            _invalid("date range cannot exceed 92 days")
        explain = _explain_aggregate(
            "employees",
            _department_trend_pipeline(department, from_date, to_date),
        )
        collection = "employees"
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json_util.loads(json_util.dumps(explain)),
    }
