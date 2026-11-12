"""
Human Resources - Variant C: Departments, roles, and leave reports
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
    func,
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
logger = logging.getLogger("hr_departments_roles_reports")
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
# Helper Utilities & Domain Validators
# -----------------------------------------------------------------------------
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_half_day_increment(val: float, field_name: str = "annual_leave_days") -> float:
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
    assignments = relationship("AssignmentModel", back_populates="employee", cascade="all, delete-orphan")
    approved_leaves = relationship("ApprovedLeaveRecordModel", back_populates="employee", cascade="all, delete-orphan")


class DepartmentModel(Base):
    __tablename__ = "departments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(12), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)
    parent_department_id = Column(Integer, ForeignKey("departments.id"), nullable=True, index=True)
    active = Column(Boolean, nullable=False, default=True)
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    parent_department = relationship("DepartmentModel", remote_side=[id], backref="children")
    assignments = relationship("AssignmentModel", back_populates="department")


class RoleModel(Base):
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(20), unique=True, nullable=False, index=True)
    title = Column(String(120), nullable=False)
    annual_leave_days = Column(Float, nullable=False, default=25.0)
    active = Column(Boolean, nullable=False, default=True)
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    assignments = relationship("AssignmentModel", back_populates="role")


class AssignmentModel(Base):
    __tablename__ = "assignments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    department_id = Column(Integer, ForeignKey("departments.id"), nullable=False, index=True)
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=False, index=True)
    effective_from = Column(Date, nullable=False)
    effective_to = Column(Date, nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    employee = relationship("EmployeeModel", back_populates="assignments")
    department = relationship("DepartmentModel", back_populates="assignments")
    role = relationship("RoleModel", back_populates="assignments")


class ApprovedLeaveRecordModel(Base):
    __tablename__ = "approved_leave_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    leave_type = Column(String(20), nullable=False, default=LeaveTypeEnum.VACATION.value)
    year = Column(Integer, nullable=False, index=True)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    requested_days = Column(Float, nullable=False)
    status = Column(String(20), nullable=False, default="APPROVED")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    employee = relationship("EmployeeModel", back_populates="approved_leaves")


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


# Department Schemas
class DepartmentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(..., pattern=r"^[A-Z0-9-]{2,12}$")
    name: str = Field(..., min_length=2, max_length=100)
    parent_department_id: Optional[int] = Field(None, gt=0)
    active: Optional[bool] = True


class DepartmentPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Optional[str] = Field(None, pattern=r"^[A-Z0-9-]{2,12}$")
    name: Optional[str] = Field(None, min_length=2, max_length=100)
    parent_department_id: Optional[int] = Field(None, gt=0)
    active: Optional[bool] = None

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("code", "name", "active"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class DepartmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    code: str
    name: str
    parent_department_id: Optional[int]
    active: bool
    is_deleted: bool
    created_at: str
    updated_at: str


class DepartmentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[DepartmentResponse]
    limit: int
    next_after_id: Optional[int]


class DepartmentHierarchyProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    department: DepartmentResponse
    parent: Optional[DepartmentResponse]
    children: List[DepartmentResponse]


# Role Schemas
class RoleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(..., pattern=r"^[A-Z0-9-]{2,20}$")
    title: str = Field(..., min_length=2, max_length=120)
    annual_leave_days: float = Field(..., ge=0.0, le=365.0)
    active: Optional[bool] = True

    @field_validator("annual_leave_days")
    @classmethod
    def validate_days(cls, v: float) -> float:
        return validate_half_day_increment(v, "annual_leave_days")


class RolePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Optional[str] = Field(None, pattern=r"^[A-Z0-9-]{2,20}$")
    title: Optional[str] = Field(None, min_length=2, max_length=120)
    annual_leave_days: Optional[float] = Field(None, ge=0.0, le=365.0)
    active: Optional[bool] = None

    @field_validator("annual_leave_days")
    @classmethod
    def validate_days(cls, v: Optional[float]) -> Optional[float]:
        if v is None:
            return None
        return validate_half_day_increment(v, "annual_leave_days")

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("code", "title", "annual_leave_days", "active"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class RoleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    code: str
    title: str
    annual_leave_days: float
    active: bool
    is_deleted: bool
    created_at: str
    updated_at: str


class RoleListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[RoleResponse]
    limit: int
    next_after_id: Optional[int]


# Assignment Schemas
class AssignmentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: int = Field(..., gt=0)
    department_id: int = Field(..., gt=0)
    role_id: int = Field(..., gt=0)
    effective_from: str = Field(..., description="YYYY-MM-DD")
    effective_to: str = Field(..., description="YYYY-MM-DD")

    @field_validator("effective_from", "effective_to")
    @classmethod
    def validate_dates(cls, v: str) -> str:
        return parse_date(v).isoformat()

    @model_validator(mode="after")
    def validate_interval(self):
        s = parse_date(self.effective_from)
        e = parse_date(self.effective_to)
        if s > e:
            raise ValueError("effective_from must be less than or equal to effective_to")
        return self


class AssignmentPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: Optional[int] = Field(None, gt=0)
    department_id: Optional[int] = Field(None, gt=0)
    role_id: Optional[int] = Field(None, gt=0)
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
        for f in ("employee_id", "department_id", "role_id", "effective_from", "effective_to"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        if self.effective_from and self.effective_to:
            if parse_date(self.effective_from) > parse_date(self.effective_to):
                raise ValueError("effective_from must be less than or equal to effective_to")
        return self


class AssignmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    employee_id: int
    department_id: int
    role_id: int
    effective_from: str
    effective_to: str
    active: bool
    is_deleted: bool
    created_at: str
    updated_at: str


class AssignmentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[AssignmentResponse]
    limit: int
    next_after_id: Optional[int]


# Report Schemas
class DepartmentLeaveReportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    department_id: int
    department_code: str
    department_name: str
    total_employees: int
    total_approved_leave_days: float
    approved_requests_count: int


class DepartmentLeaveReportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: int
    items: List[DepartmentLeaveReportItem]
    limit: int
    next_after_id: Optional[int]


class RoleLeaveReportItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role_id: int
    role_code: str
    role_title: str
    annual_leave_entitlement: float
    total_employees: int
    total_approved_leave_days: float
    approved_requests_count: int


class RoleLeaveReportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: int
    items: List[RoleLeaveReportItem]
    limit: int
    next_after_id: Optional[int]


class ApprovedLeaveDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    leave_type: str
    start_date: str
    end_date: str
    requested_days: float


class EmployeeLeaveReportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: int
    employee_code: str
    full_name: str
    year: int
    department_name: Optional[str]
    role_title: Optional[str]
    annual_leave_days: float
    total_approved_leave_days: float
    remaining_leave_days: float
    approved_requests: List[ApprovedLeaveDetail]


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
class DepartmentController:
    @classmethod
    def serialize(cls, dep: DepartmentModel) -> DepartmentResponse:
        return DepartmentResponse(
            id=dep.id,
            code=dep.code,
            name=dep.name,
            parent_department_id=dep.parent_department_id,
            active=dep.active,
            is_deleted=dep.is_deleted,
            created_at=dep.created_at.isoformat(),
            updated_at=dep.updated_at.isoformat(),
        )

    @classmethod
    def validate_no_cycle(cls, db: Session, department_id: Optional[int], proposed_parent_id: Optional[int]):
        if proposed_parent_id is None:
            return
        if department_id is not None and proposed_parent_id == department_id:
            raise AppException(409, "Department cannot be its own parent", "Conflict")

        curr = proposed_parent_id
        visited = set()
        if department_id:
            visited.add(department_id)

        while curr is not None:
            if curr in visited:
                raise AppException(409, "Cycle detected in department hierarchy", "Conflict")
            visited.add(curr)
            parent = db.scalar(select(DepartmentModel).where(DepartmentModel.id == curr, DepartmentModel.is_deleted == False))
            if not parent:
                raise AppException(404, f"Parent department {curr} not found", "Not Found")
            curr = parent.parent_department_id

    @classmethod
    def list_departments(
        cls,
        db: Session,
        current_user: UserModel,
        parent_department_id: Optional[int],
        active: Optional[bool],
        limit: int,
        after_id: Optional[int],
    ) -> DepartmentListResponse:
        query = select(DepartmentModel).where(DepartmentModel.is_deleted == False)

        if parent_department_id is not None:
            if parent_department_id <= 0:
                raise AppException(404, "parent_department_id must be a positive integer", "Not Found")
            query = query.where(DepartmentModel.parent_department_id == parent_department_id)

        if active is not None:
            query = query.where(DepartmentModel.active == active)
        if after_id is not None:
            query = query.where(DepartmentModel.id > after_id)

        query = query.order_by(DepartmentModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return DepartmentListResponse(
            items=[cls.serialize(d) for d in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def get_department(cls, db: Session, current_user: UserModel, department_id: int) -> DepartmentModel:
        if department_id <= 0:
            raise AppException(404, "department_id must be a positive integer", "Not Found")
        dep = db.scalar(select(DepartmentModel).where(DepartmentModel.id == department_id, DepartmentModel.is_deleted == False))
        if not dep:
            raise AppException(404, f"Department {department_id} not found", "Not Found")
        return dep

    @classmethod
    def create_department(
        cls, db: Session, current_user: UserModel, payload: DepartmentCreateRequest
    ) -> DepartmentModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can manage departments", "Forbidden")

        existing = db.scalar(select(DepartmentModel).where(DepartmentModel.code == payload.code))
        if existing:
            raise AppException(409, f"Department code {payload.code} already exists", "Conflict")

        cls.validate_no_cycle(db, None, payload.parent_department_id)

        dep = DepartmentModel(
            code=payload.code,
            name=payload.name,
            parent_department_id=payload.parent_department_id,
            active=payload.active if payload.active is not None else True,
            is_deleted=False,
        )
        db.add(dep)
        db.commit()
        db.refresh(dep)
        return dep

    @classmethod
    def patch_department(
        cls, db: Session, current_user: UserModel, department_id: int, payload: DepartmentPatchRequest
    ) -> DepartmentModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can update departments", "Forbidden")

        dep = cls.get_department(db, current_user, department_id)

        if payload.code is not None and payload.code != dep.code:
            existing = db.scalar(select(DepartmentModel).where(DepartmentModel.code == payload.code))
            if existing:
                raise AppException(409, f"Department code {payload.code} already exists", "Conflict")
            dep.code = payload.code

        if "parent_department_id" in payload.model_fields_set:
            cls.validate_no_cycle(db, dep.id, payload.parent_department_id)
            dep.parent_department_id = payload.parent_department_id

        if payload.name is not None:
            dep.name = payload.name
        if payload.active is not None:
            dep.active = payload.active

        dep.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(dep)
        return dep

    @classmethod
    def delete_department(cls, db: Session, current_user: UserModel, department_id: int) -> None:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can delete departments", "Forbidden")

        dep = cls.get_department(db, current_user, department_id)

        # REQ-7-005: Departments and roles cannot be deleted while active assignments reference them
        active_assignment = db.scalar(
            select(AssignmentModel.id).where(
                AssignmentModel.department_id == dep.id,
                AssignmentModel.active == True,
                AssignmentModel.is_deleted == False,
            ).limit(1)
        )
        if active_assignment:
            raise AppException(409, "Cannot delete department referenced by active assignments", "Conflict")

        # Child departments check
        has_child = db.scalar(
            select(DepartmentModel.id).where(
                DepartmentModel.parent_department_id == dep.id,
                DepartmentModel.is_deleted == False,
            ).limit(1)
        )
        if has_child:
            raise AppException(409, "Cannot delete department with active child departments", "Conflict")

        dep.active = False
        dep.is_deleted = True
        dep.updated_at = datetime.now(timezone.utc)
        db.commit()

    @classmethod
    def get_hierarchy(
        cls, db: Session, current_user: UserModel, department_id: int, limit: int, after_id: Optional[int]
    ) -> DepartmentHierarchyProjection:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can view full department hierarchy", "Forbidden")

        dep = cls.get_department(db, current_user, department_id)
        parent_dep = None
        if dep.parent_department_id:
            parent_dep = db.scalar(select(DepartmentModel).where(DepartmentModel.id == dep.parent_department_id))

        query = select(DepartmentModel).where(
            DepartmentModel.parent_department_id == dep.id,
            DepartmentModel.is_deleted == False,
        )
        if after_id is not None:
            query = query.where(DepartmentModel.id > after_id)

        query = query.order_by(DepartmentModel.id.asc()).limit(limit)
        children = list(db.scalars(query).all())

        return DepartmentHierarchyProjection(
            department=cls.serialize(dep),
            parent=cls.serialize(parent_dep) if parent_dep else None,
            children=[cls.serialize(c) for c in children],
        )


class RoleController:
    @classmethod
    def serialize(cls, role: RoleModel) -> RoleResponse:
        return RoleResponse(
            id=role.id,
            code=role.code,
            title=role.title,
            annual_leave_days=role.annual_leave_days,
            active=role.active,
            is_deleted=role.is_deleted,
            created_at=role.created_at.isoformat(),
            updated_at=role.updated_at.isoformat(),
        )

    @classmethod
    def list_roles(
        cls,
        db: Session,
        current_user: UserModel,
        active: Optional[bool],
        limit: int,
        after_id: Optional[int],
    ) -> RoleListResponse:
        query = select(RoleModel).where(RoleModel.is_deleted == False)
        if active is not None:
            query = query.where(RoleModel.active == active)
        if after_id is not None:
            query = query.where(RoleModel.id > after_id)

        query = query.order_by(RoleModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return RoleListResponse(
            items=[cls.serialize(r) for r in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def get_role(cls, db: Session, current_user: UserModel, role_id: int) -> RoleModel:
        if role_id <= 0:
            raise AppException(404, "role_id must be a positive integer", "Not Found")
        role = db.scalar(select(RoleModel).where(RoleModel.id == role_id, RoleModel.is_deleted == False))
        if not role:
            raise AppException(404, f"Role {role_id} not found", "Not Found")
        return role

    @classmethod
    def create_role(
        cls, db: Session, current_user: UserModel, payload: RoleCreateRequest
    ) -> RoleModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can configure roles", "Forbidden")

        existing = db.scalar(select(RoleModel).where(RoleModel.code == payload.code))
        if existing:
            raise AppException(409, f"Role code {payload.code} already exists", "Conflict")

        role = RoleModel(
            code=payload.code,
            title=payload.title,
            annual_leave_days=payload.annual_leave_days,
            active=payload.active if payload.active is not None else True,
            is_deleted=False,
        )
        db.add(role)
        db.commit()
        db.refresh(role)
        return role

    @classmethod
    def patch_role(
        cls, db: Session, current_user: UserModel, role_id: int, payload: RolePatchRequest
    ) -> RoleModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can update roles", "Forbidden")

        role = cls.get_role(db, current_user, role_id)

        if payload.code is not None and payload.code != role.code:
            existing = db.scalar(select(RoleModel).where(RoleModel.code == payload.code))
            if existing:
                raise AppException(409, f"Role code {payload.code} already exists", "Conflict")
            role.code = payload.code

        if payload.title is not None:
            role.title = payload.title
        if payload.annual_leave_days is not None:
            role.annual_leave_days = payload.annual_leave_days
        if payload.active is not None:
            role.active = payload.active

        role.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(role)
        return role

    @classmethod
    def delete_role(cls, db: Session, current_user: UserModel, role_id: int) -> None:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can delete roles", "Forbidden")

        role = cls.get_role(db, current_user, role_id)

        # REQ-7-005: Departments and roles cannot be deleted while active assignments reference them
        active_assignment = db.scalar(
            select(AssignmentModel.id).where(
                AssignmentModel.role_id == role.id,
                AssignmentModel.active == True,
                AssignmentModel.is_deleted == False,
            ).limit(1)
        )
        if active_assignment:
            raise AppException(409, "Cannot delete role referenced by active assignments", "Conflict")

        role.active = False
        role.is_deleted = True
        role.updated_at = datetime.now(timezone.utc)
        db.commit()


class AssignmentController:
    @classmethod
    def serialize(cls, asg: AssignmentModel) -> AssignmentResponse:
        return AssignmentResponse(
            id=asg.id,
            employee_id=asg.employee_id,
            department_id=asg.department_id,
            role_id=asg.role_id,
            effective_from=asg.effective_from.isoformat(),
            effective_to=asg.effective_to.isoformat(),
            active=asg.active,
            is_deleted=asg.is_deleted,
            created_at=asg.created_at.isoformat(),
            updated_at=asg.updated_at.isoformat(),
        )

    @classmethod
    def check_assignment_overlap(
        cls,
        db: Session,
        employee_id: int,
        effective_from: date,
        effective_to: date,
        exclude_id: Optional[int] = None,
    ) -> None:
        """REQ-7-004: Employees have one non-overlapping active department-role assignment."""
        query = select(AssignmentModel).where(
            AssignmentModel.employee_id == employee_id,
            AssignmentModel.active == True,
            AssignmentModel.is_deleted == False,
            AssignmentModel.effective_from <= effective_to,
            AssignmentModel.effective_to >= effective_from,
        )
        if exclude_id is not None:
            query = query.where(AssignmentModel.id != exclude_id)

        overlapping = db.scalar(query.limit(1))
        if overlapping:
            raise AppException(
                409,
                f"Employee already has an active assignment (id={overlapping.id}) in interval {overlapping.effective_from}..{overlapping.effective_to}",
                "Conflict",
            )

    @classmethod
    def list_assignments(
        cls,
        db: Session,
        current_user: UserModel,
        employee_id: Optional[int],
        department_id: Optional[int],
        role_id: Optional[int],
        active: Optional[bool],
        limit: int,
        after_id: Optional[int],
    ) -> AssignmentListResponse:
        query = select(AssignmentModel).where(AssignmentModel.is_deleted == False)

        if current_user.role != UserRole.ADMIN.value:
            if current_user.employee_id is None:
                raise AppException(403, "User not associated with employee", "Forbidden")
            if employee_id is not None and employee_id != current_user.employee_id:
                raise AppException(403, "Users can only view their own assignments", "Forbidden")
            query = query.where(AssignmentModel.employee_id == current_user.employee_id)
        else:
            if employee_id is not None:
                if employee_id <= 0:
                    raise AppException(404, "employee_id must be a positive integer", "Not Found")
                query = query.where(AssignmentModel.employee_id == employee_id)

        if department_id is not None:
            if department_id <= 0:
                raise AppException(404, "department_id must be a positive integer", "Not Found")
            query = query.where(AssignmentModel.department_id == department_id)
        if role_id is not None:
            if role_id <= 0:
                raise AppException(404, "role_id must be a positive integer", "Not Found")
            query = query.where(AssignmentModel.role_id == role_id)
        if active is not None:
            query = query.where(AssignmentModel.active == active)
        if after_id is not None:
            query = query.where(AssignmentModel.id > after_id)

        query = query.order_by(AssignmentModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return AssignmentListResponse(
            items=[cls.serialize(a) for a in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def get_assignment(cls, db: Session, current_user: UserModel, assignment_id: int) -> AssignmentModel:
        if assignment_id <= 0:
            raise AppException(404, "assignment_id must be a positive integer", "Not Found")
        asg = db.scalar(select(AssignmentModel).where(AssignmentModel.id == assignment_id, AssignmentModel.is_deleted == False))
        if not asg:
            raise AppException(404, f"Assignment {assignment_id} not found", "Not Found")
        if current_user.role != UserRole.ADMIN.value and current_user.employee_id != asg.employee_id:
            raise AppException(403, "Access restricted to own assignment", "Forbidden")
        return asg

    @classmethod
    def create_assignment(
        cls, db: Session, current_user: UserModel, payload: AssignmentCreateRequest
    ) -> AssignmentModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can assign departments and roles", "Forbidden")

        emp = db.scalar(select(EmployeeModel).where(EmployeeModel.id == payload.employee_id, EmployeeModel.is_deleted == False))
        if not emp:
            raise AppException(404, f"Employee {payload.employee_id} not found", "Not Found")

        dep = db.scalar(select(DepartmentModel).where(DepartmentModel.id == payload.department_id, DepartmentModel.is_deleted == False))
        if not dep:
            raise AppException(404, f"Department {payload.department_id} not found", "Not Found")

        role = db.scalar(select(RoleModel).where(RoleModel.id == payload.role_id, RoleModel.is_deleted == False))
        if not role:
            raise AppException(404, f"Role {payload.role_id} not found", "Not Found")

        from_d = parse_date(payload.effective_from)
        to_d = parse_date(payload.effective_to)

        cls.check_assignment_overlap(db, payload.employee_id, from_d, to_d)

        asg = AssignmentModel(
            employee_id=payload.employee_id,
            department_id=payload.department_id,
            role_id=payload.role_id,
            effective_from=from_d,
            effective_to=to_d,
            active=True,
            is_deleted=False,
        )
        db.add(asg)
        db.commit()
        db.refresh(asg)
        return asg

    @classmethod
    def patch_assignment(
        cls, db: Session, current_user: UserModel, assignment_id: int, payload: AssignmentPatchRequest
    ) -> AssignmentModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can update assignments", "Forbidden")

        asg = cls.get_assignment(db, current_user, assignment_id)

        target_emp = payload.employee_id if payload.employee_id is not None else asg.employee_id
        target_dep = payload.department_id if payload.department_id is not None else asg.department_id
        target_role = payload.role_id if payload.role_id is not None else asg.role_id
        target_from = parse_date(payload.effective_from) if payload.effective_from is not None else asg.effective_from
        target_to = parse_date(payload.effective_to) if payload.effective_to is not None else asg.effective_to

        if target_from > target_to:
            raise AppException(422, "effective_from must be less than or equal to effective_to", "Unprocessable Entity")

        if payload.employee_id is not None and payload.employee_id != asg.employee_id:
            emp = db.scalar(select(EmployeeModel).where(EmployeeModel.id == payload.employee_id, EmployeeModel.is_deleted == False))
            if not emp:
                raise AppException(404, f"Employee {payload.employee_id} not found", "Not Found")

        if payload.department_id is not None and payload.department_id != asg.department_id:
            dep = db.scalar(select(DepartmentModel).where(DepartmentModel.id == payload.department_id, DepartmentModel.is_deleted == False))
            if not dep:
                raise AppException(404, f"Department {payload.department_id} not found", "Not Found")

        if payload.role_id is not None and payload.role_id != asg.role_id:
            r = db.scalar(select(RoleModel).where(RoleModel.id == payload.role_id, RoleModel.is_deleted == False))
            if not r:
                raise AppException(404, f"Role {payload.role_id} not found", "Not Found")

        cls.check_assignment_overlap(db, target_emp, target_from, target_to, exclude_id=asg.id)

        asg.employee_id = target_emp
        asg.department_id = target_dep
        asg.role_id = target_role
        asg.effective_from = target_from
        asg.effective_to = target_to
        asg.updated_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(asg)
        return asg

    @classmethod
    def delete_assignment(cls, db: Session, current_user: UserModel, assignment_id: int) -> None:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can remove assignments", "Forbidden")

        asg = cls.get_assignment(db, current_user, assignment_id)
        asg.active = False
        asg.is_deleted = True
        asg.updated_at = datetime.now(timezone.utc)
        db.commit()


class ReportController:
    @classmethod
    def report_by_department(
        cls,
        db: Session,
        current_user: UserModel,
        year: int,
        department_id: Optional[int],
        limit: int,
        after_id: Optional[int],
    ) -> DepartmentLeaveReportResponse:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can access department leave reports", "Forbidden")

        if not (MIN_YEAR <= year <= MAX_YEAR):
            raise AppException(422, f"Year must be between {MIN_YEAR} and {MAX_YEAR}", "Unprocessable Entity")

        query = select(DepartmentModel).where(DepartmentModel.is_deleted == False)
        if department_id is not None:
            if department_id <= 0:
                raise AppException(404, "department_id must be a positive integer", "Not Found")
            query = query.where(DepartmentModel.id == department_id)
        if after_id is not None:
            query = query.where(DepartmentModel.id > after_id)

        query = query.order_by(DepartmentModel.id.asc()).limit(limit + 1)
        deps = list(db.scalars(query).all())
        has_more = len(deps) > limit
        items = deps[:limit]
        next_id = items[-1].id if (has_more and items) else None

        result_items = []
        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)

        for dep in items:
            # Active assignments in that department for that year
            assigned_emps = db.scalars(
                select(AssignmentModel.employee_id).where(
                    AssignmentModel.department_id == dep.id,
                    AssignmentModel.is_deleted == False,
                    AssignmentModel.effective_from <= year_end,
                    AssignmentModel.effective_to >= year_start,
                ).distinct()
            ).all()

            emp_ids = list(assigned_emps)
            total_emps = len(emp_ids)

            if emp_ids:
                leaves = list(
                    db.scalars(
                        select(ApprovedLeaveRecordModel).where(
                            ApprovedLeaveRecordModel.employee_id.in_(emp_ids),
                            ApprovedLeaveRecordModel.year == year,
                            ApprovedLeaveRecordModel.status == "APPROVED",
                        )
                    ).all()
                )
                total_days = sum(l.requested_days for l in leaves)
                total_count = len(leaves)
            else:
                total_days = 0.0
                total_count = 0

            result_items.append(
                DepartmentLeaveReportItem(
                    department_id=dep.id,
                    department_code=dep.code,
                    department_name=dep.name,
                    total_employees=total_emps,
                    total_approved_leave_days=total_days,
                    approved_requests_count=total_count,
                )
            )

        return DepartmentLeaveReportResponse(
            year=year,
            items=result_items,
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def report_by_role(
        cls,
        db: Session,
        current_user: UserModel,
        year: int,
        role_id: Optional[int],
        limit: int,
        after_id: Optional[int],
    ) -> RoleLeaveReportResponse:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Only administrators can access role leave reports", "Forbidden")

        if not (MIN_YEAR <= year <= MAX_YEAR):
            raise AppException(422, f"Year must be between {MIN_YEAR} and {MAX_YEAR}", "Unprocessable Entity")

        query = select(RoleModel).where(RoleModel.is_deleted == False)
        if role_id is not None:
            if role_id <= 0:
                raise AppException(404, "role_id must be a positive integer", "Not Found")
            query = query.where(RoleModel.id == role_id)
        if after_id is not None:
            query = query.where(RoleModel.id > after_id)

        query = query.order_by(RoleModel.id.asc()).limit(limit + 1)
        roles = list(db.scalars(query).all())
        has_more = len(roles) > limit
        items = roles[:limit]
        next_id = items[-1].id if (has_more and items) else None

        result_items = []
        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)

        for r in items:
            assigned_emps = db.scalars(
                select(AssignmentModel.employee_id).where(
                    AssignmentModel.role_id == r.id,
                    AssignmentModel.is_deleted == False,
                    AssignmentModel.effective_from <= year_end,
                    AssignmentModel.effective_to >= year_start,
                ).distinct()
            ).all()

            emp_ids = list(assigned_emps)
            total_emps = len(emp_ids)

            if emp_ids:
                leaves = list(
                    db.scalars(
                        select(ApprovedLeaveRecordModel).where(
                            ApprovedLeaveRecordModel.employee_id.in_(emp_ids),
                            ApprovedLeaveRecordModel.year == year,
                            ApprovedLeaveRecordModel.status == "APPROVED",
                        )
                    ).all()
                )
                total_days = sum(l.requested_days for l in leaves)
                total_count = len(leaves)
            else:
                total_days = 0.0
                total_count = 0

            result_items.append(
                RoleLeaveReportItem(
                    role_id=r.id,
                    role_code=r.code,
                    role_title=r.title,
                    annual_leave_entitlement=r.annual_leave_days,
                    total_employees=total_emps,
                    total_approved_leave_days=total_days,
                    approved_requests_count=total_count,
                )
            )

        return RoleLeaveReportResponse(
            year=year,
            items=result_items,
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def employee_leave_report(
        cls,
        db: Session,
        current_user: UserModel,
        employee_id: int,
        year: int,
    ) -> EmployeeLeaveReportResponse:
        if employee_id <= 0:
            raise AppException(404, "employee_id must be a positive integer", "Not Found")
        if current_user.role != UserRole.ADMIN.value and current_user.employee_id != employee_id:
            raise AppException(403, "Users can only view their own leave report", "Forbidden")

        if not (MIN_YEAR <= year <= MAX_YEAR):
            raise AppException(422, f"Year must be between {MIN_YEAR} and {MAX_YEAR}", "Unprocessable Entity")

        emp = db.scalar(select(EmployeeModel).where(EmployeeModel.id == employee_id, EmployeeModel.is_deleted == False))
        if not emp:
            raise AppException(404, f"Employee {employee_id} not found", "Not Found")

        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)

        asg = db.scalar(
            select(AssignmentModel).where(
                AssignmentModel.employee_id == emp.id,
                AssignmentModel.is_deleted == False,
                AssignmentModel.effective_from <= year_end,
                AssignmentModel.effective_to >= year_start,
            ).order_by(AssignmentModel.id.desc())
        )

        dep_name = asg.department.name if asg and asg.department else None
        role_title = asg.role.title if asg and asg.role else None
        entitlement = asg.role.annual_leave_days if asg and asg.role else 25.0

        leaves = list(
            db.scalars(
                select(ApprovedLeaveRecordModel).where(
                    ApprovedLeaveRecordModel.employee_id == emp.id,
                    ApprovedLeaveRecordModel.year == year,
                    ApprovedLeaveRecordModel.status == "APPROVED",
                ).order_by(ApprovedLeaveRecordModel.start_date.asc())
            ).all()
        )

        total_used = sum(l.requested_days for l in leaves)
        remaining = max(0.0, entitlement - total_used)

        return EmployeeLeaveReportResponse(
            employee_id=emp.id,
            employee_code=emp.employee_code,
            full_name=emp.full_name,
            year=year,
            department_name=dep_name,
            role_title=role_title,
            annual_leave_days=entitlement,
            total_approved_leave_days=total_used,
            remaining_leave_days=remaining,
            approved_requests=[
                ApprovedLeaveDetail(
                    id=l.id,
                    leave_type=l.leave_type,
                    start_date=l.start_date.isoformat(),
                    end_date=l.end_date.isoformat(),
                    requested_days=l.requested_days,
                )
                for l in leaves
            ],
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

    # Departments
    d1 = DepartmentModel(id=1, code="CORP", name="Corporate Headquarters", parent_department_id=None, active=True, is_deleted=False)
    d2 = DepartmentModel(id=2, code="ENG", name="Engineering", parent_department_id=1, active=True, is_deleted=False)
    d3 = DepartmentModel(id=3, code="HR", name="Human Resources", parent_department_id=1, active=True, is_deleted=False)
    db.add_all([d1, d2, d3])
    db.flush()

    # Roles
    r1 = RoleModel(id=1, code="DEV", title="Software Engineer", annual_leave_days=25.0, active=True, is_deleted=False)
    r2 = RoleModel(id=2, code="LEAD", title="Team Lead", annual_leave_days=28.0, active=True, is_deleted=False)
    r3 = RoleModel(id=3, code="DIR", title="Director", annual_leave_days=30.0, active=True, is_deleted=False)
    db.add_all([r1, r2, r3])
    db.flush()

    # Employees
    e1 = EmployeeModel(id=1, employee_code="EMP-001", full_name="Alice User", hire_date=date(2025, 1, 15), active=True, is_deleted=False)
    e2 = EmployeeModel(id=2, employee_code="EMP-002", full_name="Bob Colleague", hire_date=date(2025, 2, 1), active=True, is_deleted=False)
    e3 = EmployeeModel(id=3, employee_code="EMP-003", full_name="Carol Administrator", hire_date=date(2025, 1, 1), active=True, is_deleted=False)
    db.add_all([e1, e2, e3])
    db.flush()

    # Users
    u1 = UserModel(id=1, username="user1", role=UserRole.USER.value, token_hash=hash_token(user1_token), token=user1_token, employee_id=1)
    u2 = UserModel(id=2, username="user2", role=UserRole.USER.value, token_hash=hash_token(user2_token), token=user2_token, employee_id=2)
    u3 = UserModel(id=3, username="admin", role=UserRole.ADMIN.value, token_hash=hash_token(admin_token), token=admin_token, employee_id=3)
    db.add_all([u1, u2, u3])
    db.flush()

    # Assignments
    a1 = AssignmentModel(id=1, employee_id=1, department_id=2, role_id=1, effective_from=date(2025, 1, 1), effective_to=date(2025, 12, 31), active=True, is_deleted=False)
    a2 = AssignmentModel(id=2, employee_id=2, department_id=2, role_id=2, effective_from=date(2025, 1, 1), effective_to=date(2025, 12, 31), active=True, is_deleted=False)
    a3 = AssignmentModel(id=3, employee_id=3, department_id=1, role_id=3, effective_from=date(2025, 1, 1), effective_to=date(2025, 12, 31), active=True, is_deleted=False)
    db.add_all([a1, a2, a3])
    db.flush()

    # Approved leave records for 2025
    l1 = ApprovedLeaveRecordModel(id=1, employee_id=1, leave_type=LeaveTypeEnum.VACATION.value, year=2025, start_date=date(2025, 7, 1), end_date=date(2025, 7, 5), requested_days=5.0, status="APPROVED")
    l2 = ApprovedLeaveRecordModel(id=2, employee_id=2, leave_type=LeaveTypeEnum.VACATION.value, year=2025, start_date=date(2025, 8, 10), end_date=date(2025, 8, 15), requested_days=5.0, status="APPROVED")
    db.add_all([l1, l2])
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
    title="Human Resources - Departments, Roles & Leave Reports API",
    version="1.0.0",
    description="Deterministic RESTful API for department hierarchies, roles, employee assignments, and leave reports.",
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
# Endpoints (20 Specified Endpoints for Variant C)
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


# 2. GET /departments
@app.get(
    "/departments",
    tags=["Departments"],
    response_model=DepartmentListResponse,
)
def list_departments(
    parent_department_id: Optional[int] = Query(None, gt=0),
    active: Optional[bool] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return DepartmentController.list_departments(
        db, current_user, parent_department_id, active, limit, after_id
    )


# 3. POST /departments
@app.post(
    "/departments",
    tags=["Departments"],
    response_model=DepartmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_department(
    payload: DepartmentCreateRequest,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    dep = DepartmentController.create_department(db, current_user, payload)
    response.headers["Location"] = f"/departments/{dep.id}"
    return DepartmentController.serialize(dep)


# 4. GET /departments/{department_id}
@app.get(
    "/departments/{department_id}",
    tags=["Departments"],
    response_model=DepartmentResponse,
)
def get_department(
    department_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    dep = DepartmentController.get_department(db, current_user, department_id)
    return DepartmentController.serialize(dep)


# 5. PATCH /departments/{department_id}
@app.patch(
    "/departments/{department_id}",
    tags=["Departments"],
    response_model=DepartmentResponse,
)
def patch_department(
    department_id: int,
    payload: DepartmentPatchRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    dep = DepartmentController.patch_department(db, current_user, department_id, payload)
    return DepartmentController.serialize(dep)


# 6. DELETE /departments/{department_id}
@app.delete(
    "/departments/{department_id}",
    tags=["Departments"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_department(
    department_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    DepartmentController.delete_department(db, current_user, department_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 7. GET /roles
@app.get(
    "/roles",
    tags=["Roles"],
    response_model=RoleListResponse,
)
def list_roles(
    active: Optional[bool] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return RoleController.list_roles(db, current_user, active, limit, after_id)


# 8. POST /roles
@app.post(
    "/roles",
    tags=["Roles"],
    response_model=RoleResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_role(
    payload: RoleCreateRequest,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    role = RoleController.create_role(db, current_user, payload)
    response.headers["Location"] = f"/roles/{role.id}"
    return RoleController.serialize(role)


# 9. GET /roles/{role_id}
@app.get(
    "/roles/{role_id}",
    tags=["Roles"],
    response_model=RoleResponse,
)
def get_role(
    role_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    role = RoleController.get_role(db, current_user, role_id)
    return RoleController.serialize(role)


# 10. PATCH /roles/{role_id}
@app.patch(
    "/roles/{role_id}",
    tags=["Roles"],
    response_model=RoleResponse,
)
def patch_role(
    role_id: int,
    payload: RolePatchRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    role = RoleController.patch_role(db, current_user, role_id, payload)
    return RoleController.serialize(role)


# 11. DELETE /roles/{role_id}
@app.delete(
    "/roles/{role_id}",
    tags=["Roles"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_role(
    role_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    RoleController.delete_role(db, current_user, role_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 12. GET /assignments
@app.get(
    "/assignments",
    tags=["Assignments"],
    response_model=AssignmentListResponse,
)
def list_assignments(
    employee_id: Optional[int] = Query(None, gt=0),
    department_id: Optional[int] = Query(None, gt=0),
    role_id: Optional[int] = Query(None, gt=0),
    active: Optional[bool] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return AssignmentController.list_assignments(
        db, current_user, employee_id, department_id, role_id, active, limit, after_id
    )


# 13. POST /assignments
@app.post(
    "/assignments",
    tags=["Assignments"],
    response_model=AssignmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_assignment(
    payload: AssignmentCreateRequest,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    asg = AssignmentController.create_assignment(db, current_user, payload)
    response.headers["Location"] = f"/assignments/{asg.id}"
    return AssignmentController.serialize(asg)


# 14. GET /assignments/{assignment_id}
@app.get(
    "/assignments/{assignment_id}",
    tags=["Assignments"],
    response_model=AssignmentResponse,
)
def get_assignment(
    assignment_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    asg = AssignmentController.get_assignment(db, current_user, assignment_id)
    return AssignmentController.serialize(asg)


# 15. PATCH /assignments/{assignment_id}
@app.patch(
    "/assignments/{assignment_id}",
    tags=["Assignments"],
    response_model=AssignmentResponse,
)
def patch_assignment(
    assignment_id: int,
    payload: AssignmentPatchRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    asg = AssignmentController.patch_assignment(db, current_user, assignment_id, payload)
    return AssignmentController.serialize(asg)


# 16. DELETE /assignments/{assignment_id}
@app.delete(
    "/assignments/{assignment_id}",
    tags=["Assignments"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_assignment(
    assignment_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    AssignmentController.delete_assignment(db, current_user, assignment_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 17. GET /leave-reports/by-department
@app.get(
    "/leave-reports/by-department",
    tags=["Leave Reports"],
    response_model=DepartmentLeaveReportResponse,
)
def get_leave_report_by_department(
    year: int = Query(..., ge=MIN_YEAR, le=MAX_YEAR, description="Report year (2025-2035)"),
    department_id: Optional[int] = Query(None, gt=0),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return ReportController.report_by_department(db, current_user, year, department_id, limit, after_id)


# 18. GET /leave-reports/by-role
@app.get(
    "/leave-reports/by-role",
    tags=["Leave Reports"],
    response_model=RoleLeaveReportResponse,
)
def get_leave_report_by_role(
    year: int = Query(..., ge=MIN_YEAR, le=MAX_YEAR, description="Report year (2025-2035)"),
    role_id: Optional[int] = Query(None, gt=0),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return ReportController.report_by_role(db, current_user, year, role_id, limit, after_id)


# 19. GET /employees/{employee_id}/leave-report
@app.get(
    "/employees/{employee_id}/leave-report",
    tags=["Leave Reports"],
    response_model=EmployeeLeaveReportResponse,
)
def get_employee_leave_report(
    employee_id: int,
    year: int = Query(..., ge=MIN_YEAR, le=MAX_YEAR, description="Report year (2025-2035)"),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return ReportController.employee_leave_report(db, current_user, employee_id, year)


# 20. GET /departments/{department_id}/hierarchy
@app.get(
    "/departments/{department_id}/hierarchy",
    tags=["Departments"],
    response_model=DepartmentHierarchyProjection,
)
def get_department_hierarchy(
    department_id: int,
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return DepartmentController.get_hierarchy(db, current_user, department_id, limit, after_id)


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
