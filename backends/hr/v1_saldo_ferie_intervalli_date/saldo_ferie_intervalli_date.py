"""
Human Resources - Variant A: Leave balances and date intervals
CASE_ID: 7

Compliant with:
- Python 3.10+, FastAPI, Pydantic v2, SQLAlchemy ORM, SQLite in-memory with StaticPool.
- MVC architecture: SQLAlchemy models, Pydantic v2 schemas, static controllers, thin FastAPI routes.
- Fully autonomous module exporting `app`.
- Rigorous permission checks, atomic transactions, standardized error responses.
"""

from __future__ import annotations

import enum
import hashlib
import http
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    create_engine,
    select,
)
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# -----------------------------------------------------------------------------
# Logging Configuration
# -----------------------------------------------------------------------------
logger = logging.getLogger("hr_leave_balances")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(handler)

# -----------------------------------------------------------------------------
# Database Setup
# -----------------------------------------------------------------------------
DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

MAX_PAYLOAD_SIZE = 10 * 1024 * 1024  # 10 MiB
MIN_DATE = date(2025, 1, 1)
MAX_DATE = date(2035, 12, 31)
MIN_YEAR = 2025
MAX_YEAR = 2035


# -----------------------------------------------------------------------------
# Helper Security Utilities & Domain Validators
# -----------------------------------------------------------------------------
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_half_day_increment(val: float, field_name: str = "days") -> float:
    if round(val * 2) != val * 2:
        raise ValueError(f"{field_name} must be in 0.5 increments")
    return val


def parse_date(v: Any) -> date:
    if isinstance(v, date) and not isinstance(v, datetime):
        d = v
    elif isinstance(v, datetime):
        d = v.date()
    elif isinstance(v, str):
        try:
            d = date.fromisoformat(v)
        except Exception:
            raise ValueError("Invalid ISO-8601 date format, expected YYYY-MM-DD")
    else:
        raise ValueError("Date value required")

    if not (MIN_DATE <= d <= MAX_DATE):
        raise ValueError(f"Date must be within {MIN_DATE.isoformat()} and {MAX_DATE.isoformat()}")
    return d


def parse_utc_timestamp(v: Any) -> datetime:
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            raise ValueError("Invalid ISO-8601 timestamp")
    elif isinstance(v, datetime):
        dt = v
    else:
        raise ValueError("Timestamp required")

    if dt.tzinfo is None:
        raise ValueError("Datetime must be timezone-aware UTC")
    dt = dt.astimezone(timezone.utc)
    if not (MIN_DATE.year <= dt.year <= MAX_DATE.year):
        raise ValueError("Timestamp year must be between 2025 and 2035")
    return dt


# -----------------------------------------------------------------------------
# Enums
# -----------------------------------------------------------------------------
class UserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


class LeaveTypeEnum(str, enum.Enum):
    VACATION = "VACATION"
    SICK = "SICK"
    PERSONAL = "PERSONAL"
    UNPAID = "UNPAID"


class LeaveStatusEnum(str, enum.Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class BalanceChangeTypeEnum(str, enum.Enum):
    INITIAL = "INITIAL"
    ADJUSTMENT = "ADJUSTMENT"
    DEDUCTION = "DEDUCTION"
    RESTORATION = "RESTORATION"


# -----------------------------------------------------------------------------
# SQLAlchemy Models (MVC: Model)
# -----------------------------------------------------------------------------
class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    role = Column(String(20), nullable=False, default=UserRole.USER.value)
    token_hash = Column(String(64), unique=True, nullable=False)
    token = Column(String(128), unique=True, nullable=False)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)

    employee = relationship("EmployeeModel", back_populates="user", foreign_keys=[employee_id])


class EmployeeModel(Base):
    __tablename__ = "employees"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_code = Column(String(20), unique=True, nullable=False, index=True)
    full_name = Column(String(120), nullable=False)
    hire_date = Column(Date, nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user = relationship("UserModel", back_populates="employee", uselist=False)
    balances = relationship("LeaveBalanceModel", back_populates="employee", cascade="all, delete-orphan")
    leave_requests = relationship("LeaveRequestModel", back_populates="employee", cascade="all, delete-orphan")


class LeaveBalanceModel(Base):
    __tablename__ = "leave_balances"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    leave_type = Column(String(20), nullable=False)
    year = Column(Integer, nullable=False)
    opening_days = Column(Float, nullable=False)
    used_days = Column(Float, nullable=False, default=0.0)
    remaining_days = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    employee = relationship("EmployeeModel", back_populates="balances")
    histories = relationship("LeaveBalanceHistoryModel", back_populates="balance", cascade="all, delete-orphan")


class LeaveBalanceHistoryModel(Base):
    __tablename__ = "leave_balance_histories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    leave_balance_id = Column(Integer, ForeignKey("leave_balances.id"), nullable=False, index=True)
    change_type = Column(String(30), nullable=False)
    amount = Column(Float, nullable=False)
    resulting_remaining_days = Column(Float, nullable=False)
    note = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    balance = relationship("LeaveBalanceModel", back_populates="histories")


class LeaveRequestModel(Base):
    __tablename__ = "leave_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    leave_type = Column(String(20), nullable=False)
    start_date = Column(Date, nullable=False, index=True)
    end_date = Column(Date, nullable=False, index=True)
    requested_days = Column(Float, nullable=False)
    status = Column(String(20), nullable=False, default=LeaveStatusEnum.DRAFT.value, index=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    employee = relationship("EmployeeModel", back_populates="leave_requests")


# -----------------------------------------------------------------------------
# Application Exception
# -----------------------------------------------------------------------------
class AppException(Exception):
    def __init__(
        self,
        status_code: int,
        detail: str,
        error_type: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.status_code = status_code
        self.detail = detail
        self.headers = headers or {}
        if error_type is None:
            self.error_type = (
                http.HTTPStatus(status_code).phrase
                if status_code in http.HTTPStatus._value2member_map_
                else "Error"
            )
        else:
            self.error_type = error_type
        super().__init__(detail)


# -----------------------------------------------------------------------------
# Pydantic v2 Schemas (MVC: View/Schema)
# -----------------------------------------------------------------------------
class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: str
    status_code: int
    error_type: str


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    database: str


# Employee Schemas
class EmployeeCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_code: str = Field(..., pattern=r"^[A-Z0-9-]{3,20}$", description="Unique code 3-20 uppercase letters, numbers, hyphens")
    full_name: str = Field(..., min_length=3, max_length=120)
    hire_date: str = Field(..., description="Hire date YYYY-MM-DD in 2025-2035")
    active: Optional[bool] = True

    @field_validator("hire_date")
    @classmethod
    def validate_hire_date_field(cls, v: str) -> str:
        d = parse_date(v)
        return d.isoformat()


class EmployeePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_code: Optional[str] = Field(None, pattern=r"^[A-Z0-9-]{3,20}$")
    full_name: Optional[str] = Field(None, min_length=3, max_length=120)
    hire_date: Optional[str] = None
    active: Optional[bool] = None

    @field_validator("hire_date")
    @classmethod
    def validate_hire_date_field(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        d = parse_date(v)
        return d.isoformat()

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("employee_code", "full_name", "hire_date", "active"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class EmployeeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    employee_code: str
    full_name: str
    hire_date: str
    active: bool
    is_deleted: bool
    created_at: str
    updated_at: str


class EmployeeListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[EmployeeResponse]
    limit: int
    next_after_id: Optional[int]


# Leave Balance Schemas
class LeaveBalanceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: int = Field(..., gt=0)
    leave_type: LeaveTypeEnum
    year: int = Field(..., ge=MIN_YEAR, le=MAX_YEAR)
    opening_days: float = Field(..., ge=0.0, le=365.0)

    @field_validator("opening_days")
    @classmethod
    def validate_opening_days(cls, v: float) -> float:
        return validate_half_day_increment(v, "opening_days")


class LeaveBalancePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: Optional[int] = Field(None, gt=0)
    leave_type: Optional[LeaveTypeEnum] = None
    year: Optional[int] = Field(None, ge=MIN_YEAR, le=MAX_YEAR)
    opening_days: Optional[float] = Field(None, ge=0.0, le=365.0)

    @field_validator("opening_days")
    @classmethod
    def validate_opening_days(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        return validate_half_day_increment(v, "opening_days")

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("employee_id", "leave_type", "year", "opening_days"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class LeaveBalanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    employee_id: int
    leave_type: LeaveTypeEnum
    year: int
    opening_days: float
    used_days: float
    remaining_days: float
    created_at: str
    updated_at: str


class LeaveBalanceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[LeaveBalanceResponse]
    limit: int
    next_after_id: Optional[int]


class LeaveBalanceHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    leave_balance_id: int
    change_type: str
    amount: float
    resulting_remaining_days: float
    note: Optional[str]
    created_at: str


class LeaveBalanceHistoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[LeaveBalanceHistoryResponse]
    limit: int
    next_after_id: Optional[int]


# Leave Request Schemas
class LeaveRequestCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: int = Field(..., gt=0)
    leave_type: LeaveTypeEnum
    start_date: str = Field(..., description="Start date YYYY-MM-DD in 2025-2035")
    end_date: str = Field(..., description="End date YYYY-MM-DD in 2025-2035")
    requested_days: float = Field(..., ge=0.5, le=365.0)

    @field_validator("start_date", "end_date")
    @classmethod
    def validate_dates(cls, v: str) -> str:
        return parse_date(v).isoformat()

    @field_validator("requested_days")
    @classmethod
    def validate_req_days(cls, v: float) -> float:
        return validate_half_day_increment(v, "requested_days")

    @model_validator(mode="after")
    def validate_interval(self):
        s = parse_date(self.start_date)
        e = parse_date(self.end_date)
        if s > e:
            raise ValueError("start_date must be less than or equal to end_date")
        return self


class LeaveRequestPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: Optional[int] = Field(None, gt=0)
    leave_type: Optional[LeaveTypeEnum] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    requested_days: Optional[float] = Field(None, ge=0.5, le=365.0)

    @field_validator("start_date", "end_date")
    @classmethod
    def validate_dates(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return parse_date(v).isoformat()

    @field_validator("requested_days")
    @classmethod
    def validate_req_days(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        return validate_half_day_increment(v, "requested_days")

    @model_validator(mode="after")
    def validate_patch(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("employee_id", "leave_type", "start_date", "end_date", "requested_days"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        if self.start_date and self.end_date:
            if parse_date(self.start_date) > parse_date(self.end_date):
                raise ValueError("start_date must be less than or equal to end_date")
        return self


class LeaveSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submitted_at: str = Field(..., description="UTC ISO-8601 timestamp")

    @field_validator("submitted_at")
    @classmethod
    def validate_submitted_at(cls, v: str) -> str:
        return parse_utc_timestamp(v).isoformat()


class LeaveCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cancelled_at: str = Field(..., description="UTC ISO-8601 timestamp")

    @field_validator("cancelled_at")
    @classmethod
    def validate_cancelled_at(cls, v: str) -> str:
        return parse_utc_timestamp(v).isoformat()


class LeaveRequestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    employee_id: int
    leave_type: LeaveTypeEnum
    start_date: str
    end_date: str
    requested_days: float
    status: LeaveStatusEnum
    submitted_at: Optional[str]
    cancelled_at: Optional[str]
    created_at: str
    updated_at: str


class LeaveRequestListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[LeaveRequestResponse]
    limit: int
    next_after_id: Optional[int]


# -----------------------------------------------------------------------------
# Dependency: Database Session & Auth
# -----------------------------------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class AuthController:
    @staticmethod
    def get_current_user(
        authorization: Optional[str] = Header(None),
        db: Session = Depends(get_db),
    ) -> UserModel:
        if not authorization or not authorization.startswith("Bearer "):
            raise AppException(401, "Missing or invalid Bearer authorization header", "Unauthorized")
        token_str = authorization[7:].strip()
        if not token_str:
            raise AppException(401, "Missing session token", "Unauthorized")
        thash = hash_token(token_str)
        user = db.scalar(
            select(UserModel).where((UserModel.token_hash == thash) | (UserModel.token == token_str))
        )
        if not user:
            raise AppException(401, "Invalid or expired session token", "Unauthorized")
        return user

    @staticmethod
    def require_admin(current_user: UserModel = Depends(get_current_user)) -> UserModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Administrator privilege required", "Forbidden")
        return current_user


# -----------------------------------------------------------------------------
# Static Controllers (MVC: Controller)
# -----------------------------------------------------------------------------
class EmployeeController:
    @classmethod
    def serialize(cls, emp: EmployeeModel) -> EmployeeResponse:
        return EmployeeResponse(
            id=emp.id,
            employee_code=emp.employee_code,
            full_name=emp.full_name,
            hire_date=emp.hire_date.isoformat(),
            active=emp.active,
            is_deleted=emp.is_deleted,
            created_at=emp.created_at.isoformat(),
            updated_at=emp.updated_at.isoformat(),
        )

    @classmethod
    def list_employees(
        cls,
        db: Session,
        current_user: UserModel,
        active: Optional[bool],
        limit: int,
        after_id: Optional[int],
    ) -> EmployeeListResponse:
        query = select(EmployeeModel).where(EmployeeModel.is_deleted == False)
        if current_user.role != UserRole.ADMIN.value:
            if current_user.employee_id is None:
                raise AppException(403, "User is not linked to any employee record", "Forbidden")
            query = query.where(EmployeeModel.id == current_user.employee_id)

        if active is not None:
            query = query.where(EmployeeModel.active == active)
        if after_id is not None:
            query = query.where(EmployeeModel.id > after_id)

        query = query.order_by(EmployeeModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return EmployeeListResponse(
            items=[cls.serialize(e) for e in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def get_employee(cls, db: Session, current_user: UserModel, employee_id: int) -> EmployeeModel:
        if employee_id <= 0:
            raise AppException(404, "employee_id must be a positive integer", "Not Found")
        emp = db.scalar(select(EmployeeModel).where(EmployeeModel.id == employee_id, EmployeeModel.is_deleted == False))
        if not emp:
            raise AppException(404, f"Employee {employee_id} not found", "Not Found")
        if current_user.role != UserRole.ADMIN.value and current_user.employee_id != emp.id:
            raise AppException(403, "Forbidden: access restricted to own record", "Forbidden")
        return emp

    @classmethod
    def create_employee(
        cls, db: Session, current_user: UserModel, payload: EmployeeCreateRequest
    ) -> EmployeeModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can create employees", "Forbidden")

        existing = db.scalar(select(EmployeeModel).where(EmployeeModel.employee_code == payload.employee_code))
        if existing:
            raise AppException(409, f"Employee code {payload.employee_code} already exists", "Conflict")

        emp = EmployeeModel(
            employee_code=payload.employee_code,
            full_name=payload.full_name,
            hire_date=parse_date(payload.hire_date),
            active=payload.active if payload.active is not None else True,
            is_deleted=False,
        )
        db.add(emp)
        db.commit()
        db.refresh(emp)
        return emp

    @classmethod
    def patch_employee(
        cls, db: Session, current_user: UserModel, employee_id: int, payload: EmployeePatchRequest
    ) -> EmployeeModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can update employee details", "Forbidden")

        emp = cls.get_employee(db, current_user, employee_id)

        if payload.employee_code is not None and payload.employee_code != emp.employee_code:
            existing = db.scalar(select(EmployeeModel).where(EmployeeModel.employee_code == payload.employee_code))
            if existing:
                raise AppException(409, f"Employee code {payload.employee_code} is already taken", "Conflict")
            emp.employee_code = payload.employee_code

        if payload.full_name is not None:
            emp.full_name = payload.full_name
        if payload.hire_date is not None:
            emp.hire_date = parse_date(payload.hire_date)
        if payload.active is not None:
            emp.active = payload.active

        emp.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(emp)
        return emp

    @classmethod
    def delete_employee(cls, db: Session, current_user: UserModel, employee_id: int) -> None:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can delete employees", "Forbidden")

        emp = cls.get_employee(db, current_user, employee_id)

        has_balances = db.scalar(select(LeaveBalanceModel.id).where(LeaveBalanceModel.employee_id == emp.id).limit(1))
        if has_balances:
            raise AppException(409, "Cannot delete employee with existing leave balance relations", "Conflict")

        has_requests = db.scalar(select(LeaveRequestModel.id).where(LeaveRequestModel.employee_id == emp.id).limit(1))
        if has_requests:
            raise AppException(409, "Cannot delete employee with existing leave request relations", "Conflict")

        emp.active = False
        emp.is_deleted = True
        emp.updated_at = datetime.now(timezone.utc)
        db.commit()


class LeaveBalanceController:
    @classmethod
    def serialize(cls, bal: LeaveBalanceModel) -> LeaveBalanceResponse:
        return LeaveBalanceResponse(
            id=bal.id,
            employee_id=bal.employee_id,
            leave_type=LeaveTypeEnum(bal.leave_type),
            year=bal.year,
            opening_days=bal.opening_days,
            used_days=bal.used_days,
            remaining_days=bal.remaining_days,
            created_at=bal.created_at.isoformat(),
            updated_at=bal.updated_at.isoformat(),
        )

    @classmethod
    def list_balances(
        cls,
        db: Session,
        current_user: UserModel,
        employee_id: Optional[int],
        leave_type: Optional[LeaveTypeEnum],
        year: Optional[int],
        limit: int,
        after_id: Optional[int],
    ) -> LeaveBalanceListResponse:
        query = select(LeaveBalanceModel)
        if current_user.role != UserRole.ADMIN.value:
            if current_user.employee_id is None:
                raise AppException(403, "User not associated with employee", "Forbidden")
            if employee_id is not None and employee_id != current_user.employee_id:
                raise AppException(403, "Users cannot list balances of other employees", "Forbidden")
            query = query.where(LeaveBalanceModel.employee_id == current_user.employee_id)
        else:
            if employee_id is not None:
                if employee_id <= 0:
                    raise AppException(404, "employee_id must be a positive integer", "Not Found")
                query = query.where(LeaveBalanceModel.employee_id == employee_id)

        if leave_type is not None:
            query = query.where(LeaveBalanceModel.leave_type == leave_type.value)
        if year is not None:
            query = query.where(LeaveBalanceModel.year == year)
        if after_id is not None:
            query = query.where(LeaveBalanceModel.id > after_id)

        query = query.order_by(LeaveBalanceModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return LeaveBalanceListResponse(
            items=[cls.serialize(b) for b in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def get_balance(cls, db: Session, current_user: UserModel, leave_balance_id: int) -> LeaveBalanceModel:
        if leave_balance_id <= 0:
            raise AppException(404, "leave_balance_id must be a positive integer", "Not Found")
        bal = db.scalar(select(LeaveBalanceModel).where(LeaveBalanceModel.id == leave_balance_id))
        if not bal:
            raise AppException(404, f"Leave balance {leave_balance_id} not found", "Not Found")
        if current_user.role != UserRole.ADMIN.value and current_user.employee_id != bal.employee_id:
            raise AppException(403, "Access denied to foreign employee balance", "Forbidden")
        return bal

    @classmethod
    def create_balance(
        cls, db: Session, current_user: UserModel, payload: LeaveBalanceCreateRequest
    ) -> LeaveBalanceModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can configure leave balances", "Forbidden")

        emp = db.scalar(select(EmployeeModel).where(EmployeeModel.id == payload.employee_id, EmployeeModel.is_deleted == False))
        if not emp:
            raise AppException(404, f"Employee {payload.employee_id} not found", "Not Found")

        existing = db.scalar(
            select(LeaveBalanceModel).where(
                LeaveBalanceModel.employee_id == payload.employee_id,
                LeaveBalanceModel.leave_type == payload.leave_type.value,
                LeaveBalanceModel.year == payload.year,
            )
        )
        if existing:
            raise AppException(409, "Balance for this employee, leave type and year already exists", "Conflict")

        bal = LeaveBalanceModel(
            employee_id=payload.employee_id,
            leave_type=payload.leave_type.value,
            year=payload.year,
            opening_days=payload.opening_days,
            used_days=0.0,
            remaining_days=payload.opening_days,
        )
        db.add(bal)
        db.flush()

        hist = LeaveBalanceHistoryModel(
            leave_balance_id=bal.id,
            change_type=BalanceChangeTypeEnum.INITIAL.value,
            amount=payload.opening_days,
            resulting_remaining_days=payload.opening_days,
            note="Initial opening balance created",
        )
        db.add(hist)
        db.commit()
        db.refresh(bal)
        return bal

    @classmethod
    def patch_balance(
        cls, db: Session, current_user: UserModel, leave_balance_id: int, payload: LeaveBalancePatchRequest
    ) -> LeaveBalanceModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can modify leave balances", "Forbidden")

        bal = cls.get_balance(db, current_user, leave_balance_id)

        target_emp_id = payload.employee_id if payload.employee_id is not None else bal.employee_id
        target_type = payload.leave_type.value if payload.leave_type is not None else bal.leave_type
        target_year = payload.year if payload.year is not None else bal.year

        if (
            target_emp_id != bal.employee_id
            or target_type != bal.leave_type
            or target_year != bal.year
        ):
            emp = db.scalar(select(EmployeeModel).where(EmployeeModel.id == target_emp_id, EmployeeModel.is_deleted == False))
            if not emp:
                raise AppException(404, f"Employee {target_emp_id} not found", "Not Found")
            conflict = db.scalar(
                select(LeaveBalanceModel).where(
                    LeaveBalanceModel.id != bal.id,
                    LeaveBalanceModel.employee_id == target_emp_id,
                    LeaveBalanceModel.leave_type == target_type,
                    LeaveBalanceModel.year == target_year,
                )
            )
            if conflict:
                raise AppException(409, "Conflicting balance exists for target employee, type and year", "Conflict")
            bal.employee_id = target_emp_id
            bal.leave_type = target_type
            bal.year = target_year

        if payload.opening_days is not None:
            if payload.opening_days < bal.used_days:
                raise AppException(409, f"Opening days cannot be less than already used days ({bal.used_days})", "Conflict")
            diff = payload.opening_days - bal.opening_days
            bal.opening_days = payload.opening_days
            bal.remaining_days = bal.opening_days - bal.used_days
            hist = LeaveBalanceHistoryModel(
                leave_balance_id=bal.id,
                change_type=BalanceChangeTypeEnum.ADJUSTMENT.value,
                amount=diff,
                resulting_remaining_days=bal.remaining_days,
                note=f"Opening days adjusted by {diff:+.1f}",
            )
            db.add(hist)

        bal.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(bal)
        return bal

    @classmethod
    def delete_balance(cls, db: Session, current_user: UserModel, leave_balance_id: int) -> None:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can delete leave balances", "Forbidden")

        bal = cls.get_balance(db, current_user, leave_balance_id)

        if bal.used_days > 0:
            raise AppException(409, "Cannot delete balance that has been partially or fully used", "Conflict")

        # Check if active requests reference it
        has_requests = db.scalar(
            select(LeaveRequestModel.id).where(
                LeaveRequestModel.employee_id == bal.employee_id,
                LeaveRequestModel.leave_type == bal.leave_type,
            ).limit(1)
        )
        if has_requests:
            raise AppException(409, "Cannot delete balance with referencing leave requests", "Conflict")

        db.delete(bal)
        db.commit()

    @classmethod
    def list_history(
        cls,
        db: Session,
        current_user: UserModel,
        leave_balance_id: int,
        limit: int,
        after_id: Optional[int],
    ) -> LeaveBalanceHistoryListResponse:
        bal = cls.get_balance(db, current_user, leave_balance_id)
        query = select(LeaveBalanceHistoryModel).where(LeaveBalanceHistoryModel.leave_balance_id == bal.id)
        if after_id is not None:
            query = query.where(LeaveBalanceHistoryModel.id > after_id)

        query = query.order_by(LeaveBalanceHistoryModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return LeaveBalanceHistoryListResponse(
            items=[
                LeaveBalanceHistoryResponse(
                    id=h.id,
                    leave_balance_id=h.leave_balance_id,
                    change_type=h.change_type,
                    amount=h.amount,
                    resulting_remaining_days=h.resulting_remaining_days,
                    note=h.note,
                    created_at=h.created_at.isoformat(),
                )
                for h in items
            ],
            limit=limit,
            next_after_id=next_id,
        )


class LeaveRequestController:
    @classmethod
    def serialize(cls, req: LeaveRequestModel) -> LeaveRequestResponse:
        return LeaveRequestResponse(
            id=req.id,
            employee_id=req.employee_id,
            leave_type=LeaveTypeEnum(req.leave_type),
            start_date=req.start_date.isoformat(),
            end_date=req.end_date.isoformat(),
            requested_days=req.requested_days,
            status=LeaveStatusEnum(req.status),
            submitted_at=req.submitted_at.isoformat() if req.submitted_at else None,
            cancelled_at=req.cancelled_at.isoformat() if req.cancelled_at else None,
            created_at=req.created_at.isoformat(),
            updated_at=req.updated_at.isoformat(),
        )

    @classmethod
    def check_overlap(
        cls,
        db: Session,
        employee_id: int,
        leave_type: str,
        start_date: date,
        end_date: date,
        exclude_id: Optional[int] = None,
    ) -> None:
        """Enforce REQ-7-005: An employee cannot hold overlapping SUBMITTED or APPROVED intervals for the same leave type."""
        active_statuses = [LeaveStatusEnum.SUBMITTED.value, LeaveStatusEnum.APPROVED.value]
        query = select(LeaveRequestModel).where(
            LeaveRequestModel.employee_id == employee_id,
            LeaveRequestModel.leave_type == leave_type,
            LeaveRequestModel.status.in_(active_statuses),
            LeaveRequestModel.start_date <= end_date,
            LeaveRequestModel.end_date >= start_date,
        )
        if exclude_id is not None:
            query = query.where(LeaveRequestModel.id != exclude_id)

        overlapping = db.scalar(query.limit(1))
        if overlapping:
            raise AppException(
                409,
                f"Overlapping {overlapping.status} leave request (id={overlapping.id}) exists for interval {overlapping.start_date}..{overlapping.end_date}",
                "Conflict",
            )

    @classmethod
    def list_requests(
        cls,
        db: Session,
        current_user: UserModel,
        employee_id: Optional[int],
        leave_type: Optional[LeaveTypeEnum],
        status_val: Optional[LeaveStatusEnum],
        limit: int,
        after_id: Optional[int],
    ) -> LeaveRequestListResponse:
        query = select(LeaveRequestModel)
        if current_user.role != UserRole.ADMIN.value:
            if current_user.employee_id is None:
                raise AppException(403, "User not associated with employee", "Forbidden")
            if employee_id is not None and employee_id != current_user.employee_id:
                raise AppException(403, "Users cannot list requests of other employees", "Forbidden")
            query = query.where(LeaveRequestModel.employee_id == current_user.employee_id)
        else:
            if employee_id is not None:
                if employee_id <= 0:
                    raise AppException(404, "employee_id must be a positive integer", "Not Found")
                query = query.where(LeaveRequestModel.employee_id == employee_id)

        if leave_type is not None:
            query = query.where(LeaveRequestModel.leave_type == leave_type.value)
        if status_val is not None:
            query = query.where(LeaveRequestModel.status == status_val.value)
        if after_id is not None:
            query = query.where(LeaveRequestModel.id > after_id)

        query = query.order_by(LeaveRequestModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return LeaveRequestListResponse(
            items=[cls.serialize(r) for r in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def get_request(cls, db: Session, current_user: UserModel, leave_request_id: int) -> LeaveRequestModel:
        if leave_request_id <= 0:
            raise AppException(404, "leave_request_id must be a positive integer", "Not Found")
        req = db.scalar(select(LeaveRequestModel).where(LeaveRequestModel.id == leave_request_id))
        if not req:
            raise AppException(404, f"Leave request {leave_request_id} not found", "Not Found")
        if current_user.role != UserRole.ADMIN.value and current_user.employee_id != req.employee_id:
            raise AppException(403, "Access denied to request of another employee", "Forbidden")
        return req

    @classmethod
    def create_request(
        cls, db: Session, current_user: UserModel, payload: LeaveRequestCreateRequest
    ) -> LeaveRequestModel:
        if current_user.role != UserRole.ADMIN.value:
            if current_user.employee_id != payload.employee_id:
                raise AppException(403, "Cannot submit leave request for another employee", "Forbidden")

        emp = db.scalar(select(EmployeeModel).where(EmployeeModel.id == payload.employee_id, EmployeeModel.is_deleted == False))
        if not emp:
            raise AppException(404, f"Employee {payload.employee_id} not found", "Not Found")

        start_d = parse_date(payload.start_date)
        end_d = parse_date(payload.end_date)

        req = LeaveRequestModel(
            employee_id=payload.employee_id,
            leave_type=payload.leave_type.value,
            start_date=start_d,
            end_date=end_d,
            requested_days=payload.requested_days,
            status=LeaveStatusEnum.DRAFT.value,
        )
        db.add(req)
        db.commit()
        db.refresh(req)
        return req

    @classmethod
    def patch_request(
        cls, db: Session, current_user: UserModel, leave_request_id: int, payload: LeaveRequestPatchRequest
    ) -> LeaveRequestModel:
        req = cls.get_request(db, current_user, leave_request_id)

        if req.status != LeaveStatusEnum.DRAFT.value and current_user.role != UserRole.ADMIN.value:
            raise AppException(409, f"Only DRAFT requests can be modified by employee (current: {req.status})", "Conflict")

        target_emp_id = payload.employee_id if payload.employee_id is not None else req.employee_id
        target_type = payload.leave_type.value if payload.leave_type is not None else req.leave_type
        target_start = parse_date(payload.start_date) if payload.start_date is not None else req.start_date
        target_end = parse_date(payload.end_date) if payload.end_date is not None else req.end_date
        target_days = payload.requested_days if payload.requested_days is not None else req.requested_days

        if target_start > target_end:
            raise AppException(422, "start_date must be less than or equal to end_date", "Unprocessable Entity")

        if req.status in (LeaveStatusEnum.SUBMITTED.value, LeaveStatusEnum.APPROVED.value):
            cls.check_overlap(db, target_emp_id, target_type, target_start, target_end, exclude_id=req.id)

        req.employee_id = target_emp_id
        req.leave_type = target_type
        req.start_date = target_start
        req.end_date = target_end
        req.requested_days = target_days
        req.updated_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(req)
        return req

    @classmethod
    def delete_request(cls, db: Session, current_user: UserModel, leave_request_id: int) -> None:
        req = cls.get_request(db, current_user, leave_request_id)

        if req.status != LeaveStatusEnum.DRAFT.value:
            raise AppException(409, f"Cannot delete leave request in non-DRAFT status ({req.status})", "Conflict")

        db.delete(req)
        db.commit()

    @classmethod
    def submit_request(
        cls, db: Session, current_user: UserModel, leave_request_id: int, payload: LeaveSubmitRequest
    ) -> LeaveRequestModel:
        req = cls.get_request(db, current_user, leave_request_id)
        sub_dt = parse_utc_timestamp(payload.submitted_at)

        # Idempotence check
        if req.status == LeaveStatusEnum.SUBMITTED.value:
            if req.submitted_at and req.submitted_at.replace(tzinfo=timezone.utc) == sub_dt:
                return req

        if req.status != LeaveStatusEnum.DRAFT.value:
            raise AppException(409, f"Only DRAFT requests can be submitted (current: {req.status})", "Conflict")

        cls.check_overlap(db, req.employee_id, req.leave_type, req.start_date, req.end_date, exclude_id=req.id)

        req.status = LeaveStatusEnum.SUBMITTED.value
        req.submitted_at = sub_dt
        req.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(req)
        return req

    @classmethod
    def cancel_request(
        cls, db: Session, current_user: UserModel, leave_request_id: int, payload: LeaveCancelRequest
    ) -> LeaveRequestModel:
        req = cls.get_request(db, current_user, leave_request_id)
        canc_dt = parse_utc_timestamp(payload.cancelled_at)

        # Idempotency check
        if req.status == LeaveStatusEnum.CANCELLED.value:
            return req

        if req.status == LeaveStatusEnum.REJECTED.value:
            raise AppException(409, "Rejected requests cannot be cancelled", "Conflict")

        # If it was APPROVED, restore the balance atomically
        if req.status == LeaveStatusEnum.APPROVED.value:
            req_year = req.start_date.year
            bal = db.scalar(
                select(LeaveBalanceModel).where(
                    LeaveBalanceModel.employee_id == req.employee_id,
                    LeaveBalanceModel.leave_type == req.leave_type,
                    LeaveBalanceModel.year == req_year,
                )
            )
            if bal:
                bal.used_days = max(0.0, bal.used_days - req.requested_days)
                bal.remaining_days = bal.opening_days - bal.used_days
                hist = LeaveBalanceHistoryModel(
                    leave_balance_id=bal.id,
                    change_type=BalanceChangeTypeEnum.RESTORATION.value,
                    amount=req.requested_days,
                    resulting_remaining_days=bal.remaining_days,
                    note=f"Restored {req.requested_days:.1f} days due to cancellation of request {req.id}",
                )
                db.add(hist)

        req.status = LeaveStatusEnum.CANCELLED.value
        req.cancelled_at = canc_dt
        req.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(req)
        return req

    @classmethod
    def get_calendar(
        cls,
        db: Session,
        current_user: UserModel,
        employee_id: int,
        date_from: date,
        date_to: date,
        limit: int,
        after_id: Optional[int],
    ) -> LeaveRequestListResponse:
        if employee_id <= 0:
            raise AppException(404, "employee_id must be a positive integer", "Not Found")
        if current_user.role != UserRole.ADMIN.value and current_user.employee_id != employee_id:
            raise AppException(403, "Access denied to foreign employee calendar", "Forbidden")

        emp = db.scalar(select(EmployeeModel).where(EmployeeModel.id == employee_id, EmployeeModel.is_deleted == False))
        if not emp:
            raise AppException(404, f"Employee {employee_id} not found", "Not Found")

        if date_from > date_to:
            raise AppException(422, "date_from must be less than or equal to date_to", "Unprocessable Entity")

        visible_statuses = [LeaveStatusEnum.APPROVED.value, LeaveStatusEnum.SUBMITTED.value]
        query = select(LeaveRequestModel).where(
            LeaveRequestModel.employee_id == employee_id,
            LeaveRequestModel.status.in_(visible_statuses),
            LeaveRequestModel.start_date <= date_to,
            LeaveRequestModel.end_date >= date_from,
        )
        if after_id is not None:
            query = query.where(LeaveRequestModel.id > after_id)

        query = query.order_by(LeaveRequestModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return LeaveRequestListResponse(
            items=[cls.serialize(r) for r in items],
            limit=limit,
            next_after_id=next_id,
        )


# -----------------------------------------------------------------------------
# Seed Data Initialization
# -----------------------------------------------------------------------------
def seed_database(db: Session):
    Base.metadata.create_all(bind=engine)
    if db.scalar(select(UserModel).limit(1)):
        return

    admin_token = os.getenv("API_ADMIN_TOKEN", "admin-token-secret-12345")
    user1_token = os.getenv("API_USER_TOKEN", "user1-token-secret-12345")
    user2_token = os.getenv("API_SECOND_USER_TOKEN", "user2-token-secret-12345")

    e1 = EmployeeModel(
        id=1,
        employee_code="EMP-001",
        full_name="Alice User",
        hire_date=date(2025, 1, 15),
        active=True,
        is_deleted=False,
    )
    e2 = EmployeeModel(
        id=2,
        employee_code="EMP-002",
        full_name="Bob Colleague",
        hire_date=date(2025, 2, 1),
        active=True,
        is_deleted=False,
    )
    e3 = EmployeeModel(
        id=3,
        employee_code="EMP-003",
        full_name="Carol Administrator",
        hire_date=date(2025, 1, 1),
        active=True,
        is_deleted=False,
    )
    db.add_all([e1, e2, e3])
    db.flush()

    u1 = UserModel(
        id=1,
        username="user1",
        role=UserRole.USER.value,
        token_hash=hash_token(user1_token),
        token=user1_token,
        employee_id=1,
    )
    u2 = UserModel(
        id=2,
        username="user2",
        role=UserRole.USER.value,
        token_hash=hash_token(user2_token),
        token=user2_token,
        employee_id=2,
    )
    u3 = UserModel(
        id=3,
        username="admin",
        role=UserRole.ADMIN.value,
        token_hash=hash_token(admin_token),
        token=admin_token,
        employee_id=3,
    )
    db.add_all([u1, u2, u3])
    db.flush()

    b1 = LeaveBalanceModel(
        id=1,
        employee_id=1,
        leave_type=LeaveTypeEnum.VACATION.value,
        year=2025,
        opening_days=25.0,
        used_days=0.0,
        remaining_days=25.0,
    )
    b2 = LeaveBalanceModel(
        id=2,
        employee_id=1,
        leave_type=LeaveTypeEnum.SICK.value,
        year=2025,
        opening_days=10.0,
        used_days=0.0,
        remaining_days=10.0,
    )
    b3 = LeaveBalanceModel(
        id=3,
        employee_id=2,
        leave_type=LeaveTypeEnum.VACATION.value,
        year=2025,
        opening_days=20.0,
        used_days=0.0,
        remaining_days=20.0,
    )
    db.add_all([b1, b2, b3])
    db.flush()

    h1 = LeaveBalanceHistoryModel(
        id=1,
        leave_balance_id=1,
        change_type=BalanceChangeTypeEnum.INITIAL.value,
        amount=25.0,
        resulting_remaining_days=25.0,
        note="Initial opening balance",
    )
    h2 = LeaveBalanceHistoryModel(
        id=2,
        leave_balance_id=2,
        change_type=BalanceChangeTypeEnum.INITIAL.value,
        amount=10.0,
        resulting_remaining_days=10.0,
        note="Initial opening balance",
    )
    h3 = LeaveBalanceHistoryModel(
        id=3,
        leave_balance_id=3,
        change_type=BalanceChangeTypeEnum.INITIAL.value,
        amount=20.0,
        resulting_remaining_days=20.0,
        note="Initial opening balance",
    )
    db.add_all([h1, h2, h3])
    db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    with SessionLocal() as db:
        seed_database(db)
    yield


with SessionLocal() as _startup_db:
    seed_database(_startup_db)


# -----------------------------------------------------------------------------
# Pure ASGI Middleware for JSON & Content-Type Validation
# -----------------------------------------------------------------------------
class JsonValidationASGIMiddleware:
    def __init__(self, app_instance: ASGIApp, routes: list):
        self.app = app_instance
        self.routes = routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        if method not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return

        has_full_match = False
        for route in self.routes:
            match_status, _ = route.matches(scope)
            if match_status == Match.FULL:
                has_full_match = True
                break

        if not has_full_match:
            await self.app(scope, receive, send)
            return

        body_chunks = []
        total_len = 0

        while True:
            message = await receive()
            chunk = message.get("body", b"")
            total_len += len(chunk)
            if total_len > MAX_PAYLOAD_SIZE:
                err_body = json.dumps({
                    "detail": "Payload too large; maximum permitted is 10 MiB",
                    "status_code": 413,
                    "error_type": "Payload Too Large",
                }).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(err_body)).encode("ascii")),
                    ],
                })
                await send({"type": "http.response.body", "body": err_body, "more_body": False})
                return
            body_chunks.append(chunk)
            if not message.get("more_body", False):
                break

        full_body = b"".join(body_chunks)
        if full_body:
            headers = dict(scope.get("headers", []))
            content_type = headers.get(b"content-type", b"").decode("latin-1").lower()
            if not (content_type == "application/json" or content_type.startswith("application/json;")):
                err_body = json.dumps({
                    "detail": "Media type must be application/json",
                    "status_code": 415,
                    "error_type": "Unsupported Media Type",
                }).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 415,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(err_body)).encode("ascii")),
                    ],
                })
                await send({"type": "http.response.body", "body": err_body, "more_body": False})
                return
            try:
                json.loads(full_body.decode("utf-8"))
            except Exception:
                err_body = json.dumps({
                    "detail": "Malformed JSON or invalid UTF-8 payload",
                    "status_code": 400,
                    "error_type": "Bad Request",
                }).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(err_body)).encode("ascii")),
                    ],
                })
                await send({"type": "http.response.body", "body": err_body, "more_body": False})
                return

        body_sent = False

        async def replay_receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": full_body, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)


# -----------------------------------------------------------------------------
# FastAPI Application & Routes (MVC: View/Routing)
# -----------------------------------------------------------------------------
app = FastAPI(
    title="Human Resources - Leave Balances & Date Intervals API",
    version="1.0.0",
    description="Deterministic RESTful API for managing employees, leave balances, requests, and date intervals.",
    lifespan=lifespan,
)

app.add_middleware(JsonValidationASGIMiddleware, routes=app.routes)


# -----------------------------------------------------------------------------
# Exception Handlers
# -----------------------------------------------------------------------------
@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "status_code": exc.status_code, "error_type": exc.error_type},
        headers=exc.headers,
    )


@app.exception_handler(StarletteHTTPException)
async def starlette_exception_handler(request: Request, exc: StarletteHTTPException):
    status_phrase = (
        http.HTTPStatus(exc.status_code).phrase
        if exc.status_code in http.HTTPStatus._value2member_map_
        else "HTTP Exception"
    )
    headers = getattr(exc, "headers", None) or {}
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc.detail), "status_code": exc.status_code, "error_type": status_phrase},
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": str(exc.errors()), "status_code": 422, "error_type": "Unprocessable Entity"},
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "status_code": 500, "error_type": "Internal Server Error"},
    )


# -----------------------------------------------------------------------------
# Endpoints (20 Specified Endpoints for Variant A)
# -----------------------------------------------------------------------------

# 1. GET /health
@app.get(
    "/health",
    tags=["Health"],
    summary="Health check probe",
    response_model=HealthResponse,
    responses={
        200: {"model": HealthResponse, "description": "Database connected"},
        503: {"model": ErrorResponse, "description": "Service Unavailable"},
    },
)
def get_health(db: Session = Depends(get_db)):
    try:
        res = db.scalar(select(1))
        if res != 1:
            raise AppException(503, "Database probe failed", "Service Unavailable")
    except Exception:
        raise AppException(503, "Database probe failed", "Service Unavailable")
    return HealthResponse(status="ok", database="connected")


# 2. GET /employees
@app.get(
    "/employees",
    tags=["Employees"],
    response_model=EmployeeListResponse,
)
def list_employees(
    active: Optional[bool] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return EmployeeController.list_employees(db, current_user, active, limit, after_id)


# 3. POST /employees
@app.post(
    "/employees",
    tags=["Employees"],
    response_model=EmployeeResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_employee(
    payload: EmployeeCreateRequest,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    emp = EmployeeController.create_employee(db, current_user, payload)
    response.headers["Location"] = f"/employees/{emp.id}"
    return EmployeeController.serialize(emp)


# 4. GET /employees/{employee_id}
@app.get(
    "/employees/{employee_id}",
    tags=["Employees"],
    response_model=EmployeeResponse,
)
def get_employee(
    employee_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    emp = EmployeeController.get_employee(db, current_user, employee_id)
    return EmployeeController.serialize(emp)


# 5. PATCH /employees/{employee_id}
@app.patch(
    "/employees/{employee_id}",
    tags=["Employees"],
    response_model=EmployeeResponse,
)
def patch_employee(
    employee_id: int,
    payload: EmployeePatchRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    emp = EmployeeController.patch_employee(db, current_user, employee_id, payload)
    return EmployeeController.serialize(emp)


# 6. DELETE /employees/{employee_id}
@app.delete(
    "/employees/{employee_id}",
    tags=["Employees"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_employee(
    employee_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    EmployeeController.delete_employee(db, current_user, employee_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 7. GET /leave-balances
@app.get(
    "/leave-balances",
    tags=["Leave Balances"],
    response_model=LeaveBalanceListResponse,
)
def list_leave_balances(
    employee_id: Optional[int] = Query(None, gt=0),
    leave_type: Optional[LeaveTypeEnum] = Query(None),
    year: Optional[int] = Query(None, ge=MIN_YEAR, le=MAX_YEAR),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return LeaveBalanceController.list_balances(db, current_user, employee_id, leave_type, year, limit, after_id)


# 8. POST /leave-balances
@app.post(
    "/leave-balances",
    tags=["Leave Balances"],
    response_model=LeaveBalanceResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_leave_balance(
    payload: LeaveBalanceCreateRequest,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    bal = LeaveBalanceController.create_balance(db, current_user, payload)
    response.headers["Location"] = f"/leave-balances/{bal.id}"
    return LeaveBalanceController.serialize(bal)


# 9. GET /leave-balances/{leave_balance_id}
@app.get(
    "/leave-balances/{leave_balance_id}",
    tags=["Leave Balances"],
    response_model=LeaveBalanceResponse,
)
def get_leave_balance(
    leave_balance_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    bal = LeaveBalanceController.get_balance(db, current_user, leave_balance_id)
    return LeaveBalanceController.serialize(bal)


# 10. PATCH /leave-balances/{leave_balance_id}
@app.patch(
    "/leave-balances/{leave_balance_id}",
    tags=["Leave Balances"],
    response_model=LeaveBalanceResponse,
)
def patch_leave_balance(
    leave_balance_id: int,
    payload: LeaveBalancePatchRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    bal = LeaveBalanceController.patch_balance(db, current_user, leave_balance_id, payload)
    return LeaveBalanceController.serialize(bal)


# 11. DELETE /leave-balances/{leave_balance_id}
@app.delete(
    "/leave-balances/{leave_balance_id}",
    tags=["Leave Balances"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_leave_balance(
    leave_balance_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    LeaveBalanceController.delete_balance(db, current_user, leave_balance_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 12. GET /leave-requests
@app.get(
    "/leave-requests",
    tags=["Leave Requests"],
    response_model=LeaveRequestListResponse,
)
def list_leave_requests(
    employee_id: Optional[int] = Query(None, gt=0),
    leave_type: Optional[LeaveTypeEnum] = Query(None),
    status_val: Optional[LeaveStatusEnum] = Query(None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return LeaveRequestController.list_requests(db, current_user, employee_id, leave_type, status_val, limit, after_id)


# 13. POST /leave-requests
@app.post(
    "/leave-requests",
    tags=["Leave Requests"],
    response_model=LeaveRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_leave_request(
    payload: LeaveRequestCreateRequest,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    req = LeaveRequestController.create_request(db, current_user, payload)
    response.headers["Location"] = f"/leave-requests/{req.id}"
    return LeaveRequestController.serialize(req)


# 14. GET /leave-requests/{leave_request_id}
@app.get(
    "/leave-requests/{leave_request_id}",
    tags=["Leave Requests"],
    response_model=LeaveRequestResponse,
)
def get_leave_request(
    leave_request_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    req = LeaveRequestController.get_request(db, current_user, leave_request_id)
    return LeaveRequestController.serialize(req)


# 15. PATCH /leave-requests/{leave_request_id}
@app.patch(
    "/leave-requests/{leave_request_id}",
    tags=["Leave Requests"],
    response_model=LeaveRequestResponse,
)
def patch_leave_request(
    leave_request_id: int,
    payload: LeaveRequestPatchRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    req = LeaveRequestController.patch_request(db, current_user, leave_request_id, payload)
    return LeaveRequestController.serialize(req)


# 16. DELETE /leave-requests/{leave_request_id}
@app.delete(
    "/leave-requests/{leave_request_id}",
    tags=["Leave Requests"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_leave_request(
    leave_request_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    LeaveRequestController.delete_request(db, current_user, leave_request_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 17. POST /leave-requests/{leave_request_id}/submit
@app.post(
    "/leave-requests/{leave_request_id}/submit",
    tags=["Leave Requests"],
    response_model=LeaveRequestResponse,
)
def submit_leave_request(
    leave_request_id: int,
    payload: LeaveSubmitRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    req = LeaveRequestController.submit_request(db, current_user, leave_request_id, payload)
    return LeaveRequestController.serialize(req)


# 18. POST /leave-requests/{leave_request_id}/cancel
@app.post(
    "/leave-requests/{leave_request_id}/cancel",
    tags=["Leave Requests"],
    response_model=LeaveRequestResponse,
)
def cancel_leave_request(
    leave_request_id: int,
    payload: LeaveCancelRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    req = LeaveRequestController.cancel_request(db, current_user, leave_request_id, payload)
    return LeaveRequestController.serialize(req)


# 19. GET /employees/{employee_id}/leave-calendar
@app.get(
    "/employees/{employee_id}/leave-calendar",
    tags=["Leave Requests"],
    response_model=LeaveRequestListResponse,
)
def get_employee_leave_calendar(
    employee_id: int,
    date_from: str = Query(..., alias="date_from", description="Start of date range YYYY-MM-DD"),
    date_to: str = Query(..., alias="date_to", description="End of date range YYYY-MM-DD"),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    try:
        d_from = parse_date(date_from)
        d_to = parse_date(date_to)
    except ValueError as exc:
        raise AppException(422, str(exc), "Unprocessable Entity")
    return LeaveRequestController.get_calendar(db, current_user, employee_id, d_from, d_to, limit, after_id)


# 20. GET /leave-balances/{leave_balance_id}/history
@app.get(
    "/leave-balances/{leave_balance_id}/history",
    tags=["Leave Balances"],
    response_model=LeaveBalanceHistoryListResponse,
)
def get_leave_balance_history(
    leave_balance_id: int,
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return LeaveBalanceController.list_history(db, current_user, leave_balance_id, limit, after_id)


# -----------------------------------------------------------------------------
# Custom OpenAPI Export
# -----------------------------------------------------------------------------
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi
