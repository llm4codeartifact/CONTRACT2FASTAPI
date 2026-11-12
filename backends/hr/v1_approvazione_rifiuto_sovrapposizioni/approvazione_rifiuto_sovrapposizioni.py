"""
Human Resources - Variant B: Approval, rejection, and overlaps
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
logger = logging.getLogger("hr_approvals_overlaps")
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


# -----------------------------------------------------------------------------
# Helper Utilities & Domain Validators
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
    PARENTAL = "PARENTAL"
    UNPAID = "UNPAID"


class LeaveStatusEnum(str, enum.Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


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
    manager_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True, index=True)
    department = Column(String(80), nullable=False)
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
    subordinates = relationship("EmployeeModel", backref="manager", remote_side=[id])
    leave_requests = relationship(
        "LeaveRequestModel",
        back_populates="employee",
        cascade="all, delete-orphan",
        foreign_keys="[LeaveRequestModel.employee_id]",
    )


class ApprovalDelegationModel(Base):
    __tablename__ = "approval_delegations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    manager_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    delegate_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    manager_employee = relationship("EmployeeModel", foreign_keys=[manager_employee_id])
    delegate_employee = relationship("EmployeeModel", foreign_keys=[delegate_employee_id])


class LeaveRequestModel(Base):
    __tablename__ = "leave_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    leave_type = Column(String(20), nullable=False)
    start_date = Column(Date, nullable=False, index=True)
    end_date = Column(Date, nullable=False, index=True)
    requested_days = Column(Float, nullable=False)
    status = Column(String(20), nullable=False, default=LeaveStatusEnum.DRAFT.value, index=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_reason = Column(String(500), nullable=True)
    decider_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    employee = relationship("EmployeeModel", back_populates="leave_requests", foreign_keys=[employee_id])
    decider = relationship("EmployeeModel", foreign_keys=[decider_employee_id])
    histories = relationship("LeaveRequestHistoryModel", back_populates="request", cascade="all, delete-orphan")


class LeaveRequestHistoryModel(Base):
    __tablename__ = "leave_request_histories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    leave_request_id = Column(Integer, ForeignKey("leave_requests.id"), nullable=False, index=True)
    action = Column(String(30), nullable=False)
    actor_employee_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    reason = Column(String(500), nullable=True)
    occurred_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    request = relationship("LeaveRequestModel", back_populates="histories")


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
    employee_code: str = Field(..., pattern=r"^[A-Z0-9-]{3,20}$")
    full_name: str = Field(..., min_length=3, max_length=120)
    manager_employee_id: Optional[int] = Field(None, gt=0)
    department: str = Field(..., min_length=2, max_length=80)
    active: Optional[bool] = True


class EmployeePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_code: Optional[str] = Field(None, pattern=r"^[A-Z0-9-]{3,20}$")
    full_name: Optional[str] = Field(None, min_length=3, max_length=120)
    manager_employee_id: Optional[int] = Field(None, gt=0)
    department: Optional[str] = Field(None, min_length=2, max_length=80)
    active: Optional[bool] = None

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("employee_code", "full_name", "department", "active"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class EmployeeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    employee_code: str
    full_name: str
    manager_employee_id: Optional[int]
    department: str
    active: bool
    is_deleted: bool
    created_at: str
    updated_at: str


class EmployeeListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[EmployeeResponse]
    limit: int
    next_after_id: Optional[int]


# Delegation Schemas
class DelegationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manager_employee_id: int = Field(..., gt=0)
    delegate_employee_id: int = Field(..., gt=0)
    effective_from: str = Field(..., description="YYYY-MM-DD")
    effective_to: str = Field(..., description="YYYY-MM-DD")

    @field_validator("effective_from", "effective_to")
    @classmethod
    def validate_dates(cls, v: str) -> str:
        return parse_date(v).isoformat()

    @model_validator(mode="after")
    def validate_range(self):
        if self.manager_employee_id == self.delegate_employee_id:
            raise ValueError("manager_employee_id and delegate_employee_id cannot be the same")
        s = parse_date(self.effective_from)
        e = parse_date(self.effective_to)
        if s > e:
            raise ValueError("effective_from must be less than or equal to effective_to")
        return self


class DelegationPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manager_employee_id: Optional[int] = Field(None, gt=0)
    delegate_employee_id: Optional[int] = Field(None, gt=0)
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None

    @field_validator("effective_from", "effective_to")
    @classmethod
    def validate_dates(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return parse_date(v).isoformat()

    @model_validator(mode="after")
    def validate_patch(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("manager_employee_id", "delegate_employee_id", "effective_from", "effective_to"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        if self.effective_from and self.effective_to:
            if parse_date(self.effective_from) > parse_date(self.effective_to):
                raise ValueError("effective_from must be less than or equal to effective_to")
        return self


class DelegationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    manager_employee_id: int
    delegate_employee_id: int
    effective_from: str
    effective_to: str
    created_at: str
    updated_at: str


class DelegationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[DelegationResponse]
    limit: int
    next_after_id: Optional[int]


# Leave Request Schemas
class LeaveRequestCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: int = Field(..., gt=0)
    leave_type: LeaveTypeEnum
    start_date: str = Field(..., description="YYYY-MM-DD")
    end_date: str = Field(..., description="YYYY-MM-DD")
    requested_days: float = Field(..., ge=0.5, le=365.0)
    status: Optional[LeaveStatusEnum] = LeaveStatusEnum.SUBMITTED

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
    status: Optional[LeaveStatusEnum] = None

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
        for f in ("employee_id", "leave_type", "start_date", "end_date", "requested_days", "status"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        if self.start_date and self.end_date:
            if parse_date(self.start_date) > parse_date(self.end_date):
                raise ValueError("start_date must be less than or equal to end_date")
        return self


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decided_at: str = Field(..., description="UTC ISO-8601 timestamp")
    decision_reason: str = Field(..., min_length=3, max_length=500)

    @field_validator("decided_at")
    @classmethod
    def validate_decided_at(cls, v: str) -> str:
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
    decided_at: Optional[str]
    decision_reason: Optional[str]
    decider_employee_id: Optional[int]
    submitted_at: Optional[str]
    cancelled_at: Optional[str]
    created_at: str
    updated_at: str


class LeaveRequestListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[LeaveRequestResponse]
    limit: int
    next_after_id: Optional[int]


class HistoryItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    leave_request_id: int
    action: str
    actor_employee_id: Optional[int]
    reason: Optional[str]
    occurred_at: str


class HistoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[HistoryItemResponse]
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
            manager_employee_id=emp.manager_employee_id,
            department=emp.department,
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
        department: Optional[str],
        manager_employee_id: Optional[int],
        active: Optional[bool],
        limit: int,
        after_id: Optional[int],
    ) -> EmployeeListResponse:
        query = select(EmployeeModel).where(EmployeeModel.is_deleted == False)

        if department is not None:
            query = query.where(EmployeeModel.department == department)
        if manager_employee_id is not None:
            if manager_employee_id <= 0:
                raise AppException(404, "manager_employee_id must be a positive integer", "Not Found")
            query = query.where(EmployeeModel.manager_employee_id == manager_employee_id)
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

        if payload.manager_employee_id is not None:
            mgr = db.scalar(select(EmployeeModel).where(EmployeeModel.id == payload.manager_employee_id, EmployeeModel.is_deleted == False))
            if not mgr:
                raise AppException(404, f"Manager employee {payload.manager_employee_id} not found", "Not Found")

        emp = EmployeeModel(
            employee_code=payload.employee_code,
            full_name=payload.full_name,
            manager_employee_id=payload.manager_employee_id,
            department=payload.department,
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
            raise AppException(403, "Only administrators can modify employee details", "Forbidden")

        emp = cls.get_employee(db, current_user, employee_id)

        if payload.employee_code is not None and payload.employee_code != emp.employee_code:
            existing = db.scalar(select(EmployeeModel).where(EmployeeModel.employee_code == payload.employee_code))
            if existing:
                raise AppException(409, f"Employee code {payload.employee_code} already exists", "Conflict")
            emp.employee_code = payload.employee_code

        if payload.manager_employee_id is not None:
            if payload.manager_employee_id == emp.id:
                raise AppException(409, "Employee cannot be their own manager", "Conflict")
            mgr = db.scalar(select(EmployeeModel).where(EmployeeModel.id == payload.manager_employee_id, EmployeeModel.is_deleted == False))
            if not mgr:
                raise AppException(404, f"Manager employee {payload.manager_employee_id} not found", "Not Found")
            emp.manager_employee_id = payload.manager_employee_id

        if payload.full_name is not None:
            emp.full_name = payload.full_name
        if payload.department is not None:
            emp.department = payload.department
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

        # Subordinate relation check
        subordinate = db.scalar(select(EmployeeModel.id).where(EmployeeModel.manager_employee_id == emp.id, EmployeeModel.is_deleted == False).limit(1))
        if subordinate:
            raise AppException(409, "Cannot delete employee who has active subordinate relations", "Conflict")

        # Request relation check
        has_requests = db.scalar(select(LeaveRequestModel.id).where(LeaveRequestModel.employee_id == emp.id).limit(1))
        if has_requests:
            raise AppException(409, "Cannot delete employee with existing leave request relations", "Conflict")

        emp.active = False
        emp.is_deleted = True
        emp.updated_at = datetime.now(timezone.utc)
        db.commit()


class DelegationController:
    @classmethod
    def serialize(cls, dlg: ApprovalDelegationModel) -> DelegationResponse:
        return DelegationResponse(
            id=dlg.id,
            manager_employee_id=dlg.manager_employee_id,
            delegate_employee_id=dlg.delegate_employee_id,
            effective_from=dlg.effective_from.isoformat(),
            effective_to=dlg.effective_to.isoformat(),
            created_at=dlg.created_at.isoformat(),
            updated_at=dlg.updated_at.isoformat(),
        )

    @classmethod
    def check_delegation_overlap(
        cls,
        db: Session,
        manager_id: int,
        effective_from: date,
        effective_to: date,
        exclude_id: Optional[int] = None,
    ) -> None:
        query = select(ApprovalDelegationModel).where(
            ApprovalDelegationModel.manager_employee_id == manager_id,
            ApprovalDelegationModel.effective_from <= effective_to,
            ApprovalDelegationModel.effective_to >= effective_from,
        )
        if exclude_id is not None:
            query = query.where(ApprovalDelegationModel.id != exclude_id)
        overlapping = db.scalar(query.limit(1))
        if overlapping:
            raise AppException(
                409,
                f"Manager {manager_id} already has an active delegation (id={overlapping.id}) in interval {overlapping.effective_from}..{overlapping.effective_to}",
                "Conflict",
            )

    @classmethod
    def list_delegations(
        cls,
        db: Session,
        current_user: UserModel,
        manager_employee_id: Optional[int],
        delegate_employee_id: Optional[int],
        limit: int,
        after_id: Optional[int],
    ) -> DelegationListResponse:
        query = select(ApprovalDelegationModel)

        if manager_employee_id is not None:
            if manager_employee_id <= 0:
                raise AppException(404, "manager_employee_id must be a positive integer", "Not Found")
            query = query.where(ApprovalDelegationModel.manager_employee_id == manager_employee_id)

        if delegate_employee_id is not None:
            if delegate_employee_id <= 0:
                raise AppException(404, "delegate_employee_id must be a positive integer", "Not Found")
            query = query.where(ApprovalDelegationModel.delegate_employee_id == delegate_employee_id)

        if after_id is not None:
            query = query.where(ApprovalDelegationModel.id > after_id)

        query = query.order_by(ApprovalDelegationModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return DelegationListResponse(
            items=[cls.serialize(d) for d in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def get_delegation(cls, db: Session, current_user: UserModel, delegation_id: int) -> ApprovalDelegationModel:
        if delegation_id <= 0:
            raise AppException(404, "delegation_id must be a positive integer", "Not Found")
        dlg = db.scalar(select(ApprovalDelegationModel).where(ApprovalDelegationModel.id == delegation_id))
        if not dlg:
            raise AppException(404, f"Delegation {delegation_id} not found", "Not Found")
        return dlg

    @classmethod
    def create_delegation(
        cls, db: Session, current_user: UserModel, payload: DelegationCreateRequest
    ) -> ApprovalDelegationModel:
        if current_user.role != UserRole.ADMIN.value:
            if current_user.employee_id != payload.manager_employee_id:
                raise AppException(403, "Cannot create delegation on behalf of another manager", "Forbidden")

        mgr = db.scalar(select(EmployeeModel).where(EmployeeModel.id == payload.manager_employee_id, EmployeeModel.is_deleted == False))
        if not mgr:
            raise AppException(404, f"Manager employee {payload.manager_employee_id} not found", "Not Found")

        del_emp = db.scalar(select(EmployeeModel).where(EmployeeModel.id == payload.delegate_employee_id, EmployeeModel.is_deleted == False))
        if not del_emp:
            raise AppException(404, f"Delegate employee {payload.delegate_employee_id} not found", "Not Found")

        from_d = parse_date(payload.effective_from)
        to_d = parse_date(payload.effective_to)

        cls.check_delegation_overlap(db, payload.manager_employee_id, from_d, to_d)

        dlg = ApprovalDelegationModel(
            manager_employee_id=payload.manager_employee_id,
            delegate_employee_id=payload.delegate_employee_id,
            effective_from=from_d,
            effective_to=to_d,
        )
        db.add(dlg)
        db.commit()
        db.refresh(dlg)
        return dlg

    @classmethod
    def patch_delegation(
        cls, db: Session, current_user: UserModel, delegation_id: int, payload: DelegationPatchRequest
    ) -> ApprovalDelegationModel:
        dlg = cls.get_delegation(db, current_user, delegation_id)
        if current_user.role != UserRole.ADMIN.value and current_user.employee_id != dlg.manager_employee_id:
            raise AppException(403, "Only delegating manager or admin can modify delegation", "Forbidden")

        target_mgr = payload.manager_employee_id if payload.manager_employee_id is not None else dlg.manager_employee_id
        target_del = payload.delegate_employee_id if payload.delegate_employee_id is not None else dlg.delegate_employee_id
        target_from = parse_date(payload.effective_from) if payload.effective_from is not None else dlg.effective_from
        target_to = parse_date(payload.effective_to) if payload.effective_to is not None else dlg.effective_to

        if target_mgr == target_del:
            raise AppException(409, "manager_employee_id and delegate_employee_id cannot be identical", "Conflict")

        if target_from > target_to:
            raise AppException(422, "effective_from must be less than or equal to effective_to", "Unprocessable Entity")

        cls.check_delegation_overlap(db, target_mgr, target_from, target_to, exclude_id=dlg.id)

        dlg.manager_employee_id = target_mgr
        dlg.delegate_employee_id = target_del
        dlg.effective_from = target_from
        dlg.effective_to = target_to
        dlg.updated_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(dlg)
        return dlg

    @classmethod
    def delete_delegation(cls, db: Session, current_user: UserModel, delegation_id: int) -> None:
        dlg = cls.get_delegation(db, current_user, delegation_id)
        if current_user.role != UserRole.ADMIN.value and current_user.employee_id != dlg.manager_employee_id:
            raise AppException(403, "Only delegating manager or admin can end delegation", "Forbidden")

        db.delete(dlg)
        db.commit()


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
            decided_at=req.decided_at.isoformat() if req.decided_at else None,
            decision_reason=req.decision_reason,
            decider_employee_id=req.decider_employee_id,
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
        start_date: date,
        end_date: date,
        exclude_id: Optional[int] = None,
    ) -> None:
        """Enforce REQ-7-004: An employee cannot have overlapping SUBMITTED or APPROVED requests of ANY leave type."""
        active_statuses = [LeaveStatusEnum.SUBMITTED.value, LeaveStatusEnum.APPROVED.value]
        query = select(LeaveRequestModel).where(
            LeaveRequestModel.employee_id == employee_id,
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
                f"Employee already has an overlapping {overlapping.status} request (id={overlapping.id}) in interval {overlapping.start_date}..{overlapping.end_date}",
                "Conflict",
            )

    @classmethod
    def list_requests(
        cls,
        db: Session,
        current_user: UserModel,
        employee_id: Optional[int],
        status_val: Optional[LeaveStatusEnum],
        leave_type: Optional[LeaveTypeEnum],
        limit: int,
        after_id: Optional[int],
    ) -> LeaveRequestListResponse:
        query = select(LeaveRequestModel)

        if current_user.role != UserRole.ADMIN.value:
            if current_user.employee_id is None:
                raise AppException(403, "User not associated with employee", "Forbidden")
            # Managers see own requests and subordinate requests
            subordinate_ids = list(
                db.scalars(
                    select(EmployeeModel.id).where(
                        EmployeeModel.manager_employee_id == current_user.employee_id,
                        EmployeeModel.is_deleted == False,
                    )
                ).all()
            )
            allowed_emp_ids = [current_user.employee_id] + subordinate_ids

            if employee_id is not None:
                if employee_id not in allowed_emp_ids:
                    raise AppException(403, "Access forbidden to requested employee records", "Forbidden")
                query = query.where(LeaveRequestModel.employee_id == employee_id)
            else:
                query = query.where(LeaveRequestModel.employee_id.in_(allowed_emp_ids))
        else:
            if employee_id is not None:
                if employee_id <= 0:
                    raise AppException(404, "employee_id must be a positive integer", "Not Found")
                query = query.where(LeaveRequestModel.employee_id == employee_id)

        if status_val is not None:
            query = query.where(LeaveRequestModel.status == status_val.value)
        if leave_type is not None:
            query = query.where(LeaveRequestModel.leave_type == leave_type.value)
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

        if current_user.role != UserRole.ADMIN.value:
            if current_user.employee_id != req.employee_id:
                # Check if current user is manager of requester
                requester = db.scalar(select(EmployeeModel).where(EmployeeModel.id == req.employee_id))
                if not requester or requester.manager_employee_id != current_user.employee_id:
                    raise AppException(403, "Access denied to request of non-subordinate employee", "Forbidden")
        return req

    @classmethod
    def create_request(
        cls, db: Session, current_user: UserModel, payload: LeaveRequestCreateRequest
    ) -> LeaveRequestModel:
        if current_user.role != UserRole.ADMIN.value:
            if current_user.employee_id != payload.employee_id:
                raise AppException(403, "Cannot create leave request for another employee", "Forbidden")

        emp = db.scalar(select(EmployeeModel).where(EmployeeModel.id == payload.employee_id, EmployeeModel.is_deleted == False))
        if not emp:
            raise AppException(404, f"Employee {payload.employee_id} not found", "Not Found")

        start_d = parse_date(payload.start_date)
        end_d = parse_date(payload.end_date)
        init_status = payload.status.value if payload.status else LeaveStatusEnum.SUBMITTED.value

        if init_status in (LeaveStatusEnum.SUBMITTED.value, LeaveStatusEnum.APPROVED.value):
            cls.check_overlap(db, payload.employee_id, start_d, end_d)

        now = datetime.now(timezone.utc)
        req = LeaveRequestModel(
            employee_id=payload.employee_id,
            leave_type=payload.leave_type.value,
            start_date=start_d,
            end_date=end_d,
            requested_days=payload.requested_days,
            status=init_status,
            submitted_at=now if init_status == LeaveStatusEnum.SUBMITTED.value else None,
        )
        db.add(req)
        db.flush()

        hist = LeaveRequestHistoryModel(
            leave_request_id=req.id,
            action=init_status,
            actor_employee_id=current_user.employee_id,
            reason="Initial request created",
            occurred_at=now,
        )
        db.add(hist)
        db.commit()
        db.refresh(req)
        return req

    @classmethod
    def patch_request(
        cls, db: Session, current_user: UserModel, leave_request_id: int, payload: LeaveRequestPatchRequest
    ) -> LeaveRequestModel:
        req = cls.get_request(db, current_user, leave_request_id)

        # REQ-7-006: terminal states are immutable
        if req.status in (LeaveStatusEnum.APPROVED.value, LeaveStatusEnum.REJECTED.value, LeaveStatusEnum.CANCELLED.value):
            raise AppException(409, f"Terminal request state {req.status} is immutable", "Conflict")

        if req.status != LeaveStatusEnum.DRAFT.value and current_user.role != UserRole.ADMIN.value:
            raise AppException(409, "Only DRAFT requests can be updated by employee", "Conflict")

        target_emp_id = payload.employee_id if payload.employee_id is not None else req.employee_id
        target_type = payload.leave_type.value if payload.leave_type is not None else req.leave_type
        target_start = parse_date(payload.start_date) if payload.start_date is not None else req.start_date
        target_end = parse_date(payload.end_date) if payload.end_date is not None else req.end_date
        target_days = payload.requested_days if payload.requested_days is not None else req.requested_days
        target_status = payload.status.value if payload.status is not None else req.status

        if target_start > target_end:
            raise AppException(422, "start_date must be less than or equal to end_date", "Unprocessable Entity")

        if target_status in (LeaveStatusEnum.SUBMITTED.value, LeaveStatusEnum.APPROVED.value):
            cls.check_overlap(db, target_emp_id, target_start, target_end, exclude_id=req.id)

        req.employee_id = target_emp_id
        req.leave_type = target_type
        req.start_date = target_start
        req.end_date = target_end
        req.requested_days = target_days
        req.status = target_status
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
    def can_decide_request(cls, db: Session, current_user: UserModel, req: LeaveRequestModel, decided_dt: datetime) -> bool:
        """
        REQ-7-005: Only the active requester manager or admin may decide a SUBMITTED request; self-approval is prohibited.
        Delegation applies if manager delegated authority.
        """
        if current_user.employee_id == req.employee_id:
            return False  # Self-approval prohibited

        if current_user.role == UserRole.ADMIN.value:
            return True

        requester = db.scalar(select(EmployeeModel).where(EmployeeModel.id == req.employee_id))
        if not requester or requester.manager_employee_id is None:
            return False

        # Direct manager
        if current_user.employee_id == requester.manager_employee_id:
            return True

        # Active delegate
        decided_d = decided_dt.date()
        delegation = db.scalar(
            select(ApprovalDelegationModel).where(
                ApprovalDelegationModel.manager_employee_id == requester.manager_employee_id,
                ApprovalDelegationModel.delegate_employee_id == current_user.employee_id,
                ApprovalDelegationModel.effective_from <= decided_d,
                ApprovalDelegationModel.effective_to >= decided_d,
            )
        )
        return delegation is not None

    @classmethod
    def approve_request(
        cls, db: Session, current_user: UserModel, leave_request_id: int, payload: DecisionRequest
    ) -> LeaveRequestModel:
        req = cls.get_request(db, current_user, leave_request_id)
        dec_dt = parse_utc_timestamp(payload.decided_at)

        # Idempotence for identical valid approval
        if req.status == LeaveStatusEnum.APPROVED.value:
            if req.decided_at and req.decided_at.replace(tzinfo=timezone.utc) == dec_dt and req.decision_reason == payload.decision_reason:
                return req
            raise AppException(409, "Request is already in terminal state APPROVED", "Conflict")

        if req.status != LeaveStatusEnum.SUBMITTED.value:
            raise AppException(409, f"Only SUBMITTED requests can be approved (current: {req.status})", "Conflict")

        if not cls.can_decide_request(db, current_user, req, dec_dt):
            if current_user.employee_id == req.employee_id:
                raise AppException(403, "Self-approval is prohibited", "Forbidden")
            raise AppException(403, "Only manager, active delegate, or admin may approve this request", "Forbidden")

        # Overlap check under REQ-7-004
        cls.check_overlap(db, req.employee_id, req.start_date, req.end_date, exclude_id=req.id)

        req.status = LeaveStatusEnum.APPROVED.value
        req.decided_at = dec_dt
        req.decision_reason = payload.decision_reason
        req.decider_employee_id = current_user.employee_id
        req.updated_at = datetime.now(timezone.utc)

        hist = LeaveRequestHistoryModel(
            leave_request_id=req.id,
            action=LeaveStatusEnum.APPROVED.value,
            actor_employee_id=current_user.employee_id,
            reason=payload.decision_reason,
            occurred_at=dec_dt,
        )
        db.add(hist)
        db.commit()
        db.refresh(req)
        return req

    @classmethod
    def reject_request(
        cls, db: Session, current_user: UserModel, leave_request_id: int, payload: DecisionRequest
    ) -> LeaveRequestModel:
        req = cls.get_request(db, current_user, leave_request_id)
        dec_dt = parse_utc_timestamp(payload.decided_at)

        # Idempotence check
        if req.status == LeaveStatusEnum.REJECTED.value:
            if req.decided_at and req.decided_at.replace(tzinfo=timezone.utc) == dec_dt and req.decision_reason == payload.decision_reason:
                return req
            raise AppException(409, "Request is already in terminal state REJECTED", "Conflict")

        if req.status != LeaveStatusEnum.SUBMITTED.value:
            raise AppException(409, f"Only SUBMITTED requests can be rejected (current: {req.status})", "Conflict")

        if not cls.can_decide_request(db, current_user, req, dec_dt):
            if current_user.employee_id == req.employee_id:
                raise AppException(403, "Self-rejection is prohibited", "Forbidden")
            raise AppException(403, "Only manager, active delegate, or admin may reject this request", "Forbidden")

        req.status = LeaveStatusEnum.REJECTED.value
        req.decided_at = dec_dt
        req.decision_reason = payload.decision_reason
        req.decider_employee_id = current_user.employee_id
        req.updated_at = datetime.now(timezone.utc)

        hist = LeaveRequestHistoryModel(
            leave_request_id=req.id,
            action=LeaveStatusEnum.REJECTED.value,
            actor_employee_id=current_user.employee_id,
            reason=payload.decision_reason,
            occurred_at=dec_dt,
        )
        db.add(hist)
        db.commit()
        db.refresh(req)
        return req

    @classmethod
    def get_conflicts(
        cls,
        db: Session,
        current_user: UserModel,
        employee_id: int,
        start_date: date,
        end_date: date,
        limit: int,
        after_id: Optional[int],
    ) -> LeaveRequestListResponse:
        if employee_id <= 0:
            raise AppException(404, "employee_id must be a positive integer", "Not Found")

        emp = db.scalar(select(EmployeeModel).where(EmployeeModel.id == employee_id, EmployeeModel.is_deleted == False))
        if not emp:
            raise AppException(404, f"Employee {employee_id} not found", "Not Found")

        if current_user.role != UserRole.ADMIN.value:
            if current_user.employee_id != employee_id and current_user.employee_id != emp.manager_employee_id:
                raise AppException(403, "Access restricted to employee, direct manager, or admin", "Forbidden")

        if start_date > end_date:
            raise AppException(422, "start_date must be less than or equal to end_date", "Unprocessable Entity")

        active_statuses = [LeaveStatusEnum.SUBMITTED.value, LeaveStatusEnum.APPROVED.value]
        query = select(LeaveRequestModel).where(
            LeaveRequestModel.employee_id == employee_id,
            LeaveRequestModel.status.in_(active_statuses),
            LeaveRequestModel.start_date <= end_date,
            LeaveRequestModel.end_date >= start_date,
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

    @classmethod
    def list_history(
        cls,
        db: Session,
        current_user: UserModel,
        leave_request_id: int,
        limit: int,
        after_id: Optional[int],
    ) -> HistoryListResponse:
        req = cls.get_request(db, current_user, leave_request_id)
        query = select(LeaveRequestHistoryModel).where(LeaveRequestHistoryModel.leave_request_id == req.id)
        if after_id is not None:
            query = query.where(LeaveRequestHistoryModel.id > after_id)

        query = query.order_by(LeaveRequestHistoryModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return HistoryListResponse(
            items=[
                HistoryItemResponse(
                    id=h.id,
                    leave_request_id=h.leave_request_id,
                    action=h.action,
                    actor_employee_id=h.actor_employee_id,
                    reason=h.reason,
                    occurred_at=h.occurred_at.isoformat(),
                )
                for h in items
            ],
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

    # 3: Carol Director (Executive, no manager)
    e3 = EmployeeModel(
        id=3,
        employee_code="EMP-003",
        full_name="Carol Director",
        manager_employee_id=None,
        department="Executive",
        active=True,
        is_deleted=False,
    )
    # 2: Bob Manager (Engineering, manager Carol)
    e2 = EmployeeModel(
        id=2,
        employee_code="EMP-002",
        full_name="Bob Manager",
        manager_employee_id=3,
        department="Engineering",
        active=True,
        is_deleted=False,
    )
    # 1: Alice User (Engineering, manager Bob)
    e1 = EmployeeModel(
        id=1,
        employee_code="EMP-001",
        full_name="Alice User",
        manager_employee_id=2,
        department="Engineering",
        active=True,
        is_deleted=False,
    )
    db.add_all([e3, e2, e1])
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

    # Seed an approval delegation: Bob delegates to Carol for June 2025
    dlg1 = ApprovalDelegationModel(
        id=1,
        manager_employee_id=2,
        delegate_employee_id=3,
        effective_from=date(2025, 6, 1),
        effective_to=date(2025, 6, 30),
    )
    db.add(dlg1)
    db.flush()

    # Seed a submitted leave request for Alice
    req1 = LeaveRequestModel(
        id=1,
        employee_id=1,
        leave_type=LeaveTypeEnum.VACATION.value,
        start_date=date(2025, 7, 1),
        end_date=date(2025, 7, 5),
        requested_days=5.0,
        status=LeaveStatusEnum.SUBMITTED.value,
        submitted_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
    )
    db.add(req1)
    db.flush()

    hist1 = LeaveRequestHistoryModel(
        id=1,
        leave_request_id=1,
        action=LeaveStatusEnum.SUBMITTED.value,
        actor_employee_id=1,
        reason="Initial summer vacation request",
        occurred_at=datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
    )
    db.add(hist1)
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
    title="Human Resources - Approval, Rejection & Overlaps API",
    version="1.0.0",
    description="Deterministic RESTful API for employee hierarchies, leave requests, delegation, and approval workflow.",
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
# Endpoints (20 Specified Endpoints for Variant B)
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
    department: Optional[str] = Query(None),
    manager_employee_id: Optional[int] = Query(None, gt=0),
    active: Optional[bool] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return EmployeeController.list_employees(
        db, current_user, department, manager_employee_id, active, limit, after_id
    )


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


# 7. GET /leave-requests
@app.get(
    "/leave-requests",
    tags=["Leave Requests"],
    response_model=LeaveRequestListResponse,
)
def list_leave_requests(
    employee_id: Optional[int] = Query(None, gt=0),
    status_val: Optional[LeaveStatusEnum] = Query(None, alias="status"),
    leave_type: Optional[LeaveTypeEnum] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return LeaveRequestController.list_requests(
        db, current_user, employee_id, status_val, leave_type, limit, after_id
    )


# 8. POST /leave-requests
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


# 9. GET /leave-requests/{leave_request_id}
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


# 10. PATCH /leave-requests/{leave_request_id}
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


# 11. DELETE /leave-requests/{leave_request_id}
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


# 12. GET /approval-delegations
@app.get(
    "/approval-delegations",
    tags=["Approval Delegations"],
    response_model=DelegationListResponse,
)
def list_approval_delegations(
    manager_employee_id: Optional[int] = Query(None, gt=0),
    delegate_employee_id: Optional[int] = Query(None, gt=0),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return DelegationController.list_delegations(
        db, current_user, manager_employee_id, delegate_employee_id, limit, after_id
    )


# 13. POST /approval-delegations
@app.post(
    "/approval-delegations",
    tags=["Approval Delegations"],
    response_model=DelegationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_approval_delegation(
    payload: DelegationCreateRequest,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    dlg = DelegationController.create_delegation(db, current_user, payload)
    response.headers["Location"] = f"/approval-delegations/{dlg.id}"
    return DelegationController.serialize(dlg)


# 14. GET /approval-delegations/{delegation_id}
@app.get(
    "/approval-delegations/{delegation_id}",
    tags=["Approval Delegations"],
    response_model=DelegationResponse,
)
def get_approval_delegation(
    delegation_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    dlg = DelegationController.get_delegation(db, current_user, delegation_id)
    return DelegationController.serialize(dlg)


# 15. PATCH /approval-delegations/{delegation_id}
@app.patch(
    "/approval-delegations/{delegation_id}",
    tags=["Approval Delegations"],
    response_model=DelegationResponse,
)
def patch_approval_delegation(
    delegation_id: int,
    payload: DelegationPatchRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    dlg = DelegationController.patch_delegation(db, current_user, delegation_id, payload)
    return DelegationController.serialize(dlg)


# 16. DELETE /approval-delegations/{delegation_id}
@app.delete(
    "/approval-delegations/{delegation_id}",
    tags=["Approval Delegations"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_approval_delegation(
    delegation_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    DelegationController.delete_delegation(db, current_user, delegation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 17. POST /leave-requests/{leave_request_id}/approve
@app.post(
    "/leave-requests/{leave_request_id}/approve",
    tags=["Leave Requests"],
    response_model=LeaveRequestResponse,
)
def approve_leave_request(
    leave_request_id: int,
    payload: DecisionRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    req = LeaveRequestController.approve_request(db, current_user, leave_request_id, payload)
    return LeaveRequestController.serialize(req)


# 18. POST /leave-requests/{leave_request_id}/reject
@app.post(
    "/leave-requests/{leave_request_id}/reject",
    tags=["Leave Requests"],
    response_model=LeaveRequestResponse,
)
def reject_leave_request(
    leave_request_id: int,
    payload: DecisionRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    req = LeaveRequestController.reject_request(db, current_user, leave_request_id, payload)
    return LeaveRequestController.serialize(req)


# 19. GET /employees/{employee_id}/leave-conflicts
@app.get(
    "/employees/{employee_id}/leave-conflicts",
    tags=["Leave Requests"],
    response_model=LeaveRequestListResponse,
)
def get_leave_conflicts(
    employee_id: int,
    start_date: str = Query(..., description="YYYY-MM-DD"),
    end_date: str = Query(..., description="YYYY-MM-DD"),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    try:
        s_date = parse_date(start_date)
        e_date = parse_date(end_date)
    except ValueError as exc:
        raise AppException(422, str(exc), "Unprocessable Entity")
    return LeaveRequestController.get_conflicts(db, current_user, employee_id, s_date, e_date, limit, after_id)


# 20. GET /leave-requests/{leave_request_id}/history
@app.get(
    "/leave-requests/{leave_request_id}/history",
    tags=["Leave Requests"],
    response_model=HistoryListResponse,
)
def get_request_history(
    leave_request_id: int,
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return LeaveRequestController.list_history(db, current_user, leave_request_id, limit, after_id)


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
