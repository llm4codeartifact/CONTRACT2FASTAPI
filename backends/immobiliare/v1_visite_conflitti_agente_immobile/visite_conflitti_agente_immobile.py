"""
Real-Estate Management - Variant B: Visits and agent-property conflicts
CASE_ID: 6

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
    DateTime,
    ForeignKey,
    Integer,
    String,
    and_,
    create_engine,
    or_,
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
logger = logging.getLogger("immobiliare_variant_b")
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
LICENSE_REGEX = re.compile(r"^[A-Z0-9-]{4,20}$")


# -----------------------------------------------------------------------------
# Helper Security Utilities & Domain Validators
# -----------------------------------------------------------------------------
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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


class PropertyStatusEnum(str, enum.Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class VisitStatusEnum(str, enum.Enum):
    SCHEDULED = "SCHEDULED"
    CHECKED_IN = "CHECKED_IN"
    COMPLETED = "COMPLETED"
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
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True)

    agent = relationship("AgentModel", back_populates="user", foreign_keys=[agent_id])
    visits = relationship("VisitModel", back_populates="visitor_user")


class AgentModel(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    license_code = Column(String(20), unique=True, nullable=False, index=True)
    active = Column(Boolean, nullable=False, default=True)
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user = relationship("UserModel", back_populates="agent", uselist=False)
    properties = relationship("PropertyModel", back_populates="agent")
    visits = relationship("VisitModel", back_populates="agent")


class PropertyModel(Base):
    __tablename__ = "properties"

    id = Column(Integer, primary_key=True, autoincrement=True)
    address = Column(String(200), nullable=False)
    city = Column(String(80), nullable=False, default="Milano")
    status = Column(String(20), nullable=False, default=PropertyStatusEnum.ACTIVE.value)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    agent = relationship("AgentModel", back_populates="properties", foreign_keys=[agent_id])
    visits = relationship("VisitModel", back_populates="property")


class VisitModel(Base):
    __tablename__ = "visits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=False, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    visitor_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    visitor_name = Column(String(120), nullable=False)
    start_at = Column(DateTime(timezone=True), nullable=False, index=True)
    end_at = Column(DateTime(timezone=True), nullable=False, index=True)
    status = Column(String(20), nullable=False, default=VisitStatusEnum.SCHEDULED.value, index=True)
    checked_in_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    property = relationship("PropertyModel", back_populates="visits", foreign_keys=[property_id])
    agent = relationship("AgentModel", back_populates="visits", foreign_keys=[agent_id])
    visitor_user = relationship("UserModel", back_populates="visits", foreign_keys=[visitor_user_id])
    histories = relationship("VisitHistoryModel", back_populates="visit", cascade="all, delete-orphan")


class VisitHistoryModel(Base):
    __tablename__ = "visit_histories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    visit_id = Column(Integer, ForeignKey("visits.id"), nullable=False, index=True)
    from_state = Column(String(20), nullable=False)
    to_state = Column(String(20), nullable=False)
    action_time = Column(DateTime(timezone=True), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    visit = relationship("VisitModel", back_populates="histories")
    user = relationship("UserModel")


# -----------------------------------------------------------------------------
# Custom Domain Exceptions
# -----------------------------------------------------------------------------
class AppException(Exception):
    def __init__(self, status_code: int, detail: str, error_type: str, headers: Optional[Dict[str, str]] = None):
        self.status_code = status_code
        self.detail = detail
        self.error_type = error_type
        self.headers = headers or {}
        super().__init__(detail)


# -----------------------------------------------------------------------------
# Pydantic Schemas (MVC: View / Data Transfer Objects)
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


# Agent Schemas
class AgentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=3, max_length=120)
    license_code: str = Field(..., min_length=4, max_length=20)
    active: Optional[bool] = True

    @field_validator("name", "license_code")
    @classmethod
    def strip_strings(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("String fields must not be empty or whitespace only")
        return s

    @field_validator("license_code")
    @classmethod
    def validate_license(cls, v: str) -> str:
        s = v.strip().upper()
        if not LICENSE_REGEX.match(s):
            raise ValueError("License code must match ^[A-Z0-9-]{4,20}$")
        return s


class AgentPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=3, max_length=120)
    license_code: Optional[str] = Field(None, min_length=4, max_length=20)
    active: Optional[bool] = None

    @field_validator("name", "license_code")
    @classmethod
    def strip_strings(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if not s:
            raise ValueError("String fields must not be empty or whitespace only")
        return s

    @field_validator("license_code")
    @classmethod
    def validate_license(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip().upper()
        if not LICENSE_REGEX.match(s):
            raise ValueError("License code must match ^[A-Z0-9-]{4,20}$")
        return s

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("name", "license_code", "active"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class AgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    name: str
    license_code: str
    active: bool
    created_at: str
    updated_at: str


class AgentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[AgentResponse]
    limit: int
    next_after_id: Optional[int]


# Property Schemas
class PropertyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: str = Field(..., min_length=5, max_length=200)
    city: Optional[str] = Field("Milano", min_length=2, max_length=80)
    status: Optional[PropertyStatusEnum] = PropertyStatusEnum.ACTIVE
    agent_id: int = Field(..., gt=0)

    @field_validator("address", "city")
    @classmethod
    def strip_strings(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if len(s) < 2:
            raise ValueError("Field length too short after trimming")
        return s


class PropertyPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    address: Optional[str] = Field(None, min_length=5, max_length=200)
    city: Optional[str] = Field(None, min_length=2, max_length=80)
    status: Optional[PropertyStatusEnum] = None
    agent_id: Optional[int] = Field(None, gt=0)

    @field_validator("address", "city")
    @classmethod
    def strip_strings(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if len(s) < 2:
            raise ValueError("Field length too short after trimming")
        return s

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("address", "city", "status", "agent_id"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class PropertyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    address: str
    city: str
    status: PropertyStatusEnum
    agent_id: int
    created_at: str
    updated_at: str


class PropertyListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[PropertyResponse]
    limit: int
    next_after_id: Optional[int]


# Visit Schemas
class VisitCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    property_id: int = Field(..., gt=0)
    agent_id: int = Field(..., gt=0)
    visitor_name: str = Field(..., min_length=2, max_length=120)
    start_at: str = Field(..., description="UTC ISO-8601 timestamp in 2025-2035")
    end_at: str = Field(..., description="UTC ISO-8601 timestamp in 2025-2035")

    @field_validator("visitor_name")
    @classmethod
    def strip_visitor_name(cls, v: str) -> str:
        s = v.strip()
        if len(s) < 2:
            raise ValueError("visitor_name must have at least 2 characters")
        return s

    @field_validator("start_at", "end_at")
    @classmethod
    def validate_ts(cls, v: str) -> str:
        return parse_utc_timestamp(v).isoformat()

    @model_validator(mode="after")
    def validate_interval(self):
        s_dt = parse_utc_timestamp(self.start_at)
        e_dt = parse_utc_timestamp(self.end_at)
        if e_dt <= s_dt:
            raise ValueError("end_at must be strictly greater than start_at")
        duration_minutes = (e_dt - s_dt).total_seconds() / 60.0
        if not (15.0 <= duration_minutes <= 240.0):
            raise ValueError("Visit interval must be between 15 and 240 minutes")
        return self


class VisitPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    property_id: Optional[int] = Field(None, gt=0)
    agent_id: Optional[int] = Field(None, gt=0)
    visitor_name: Optional[str] = Field(None, min_length=2, max_length=120)
    start_at: Optional[str] = Field(None, description="UTC ISO-8601 timestamp in 2025-2035")
    end_at: Optional[str] = Field(None, description="UTC ISO-8601 timestamp in 2025-2035")

    @field_validator("visitor_name")
    @classmethod
    def strip_visitor_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if len(s) < 2:
            raise ValueError("visitor_name must have at least 2 characters")
        return s

    @field_validator("start_at", "end_at")
    @classmethod
    def validate_ts(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return parse_utc_timestamp(v).isoformat()

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("property_id", "agent_id", "visitor_name", "start_at", "end_at"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        if (self.start_at is not None and self.end_at is None) or (self.start_at is None and self.end_at is not None):
            raise ValueError("start_at and end_at must be updated together")
        if self.start_at is not None and self.end_at is not None:
            s_dt = parse_utc_timestamp(self.start_at)
            e_dt = parse_utc_timestamp(self.end_at)
            if e_dt <= s_dt:
                raise ValueError("end_at must be strictly greater than start_at")
            duration_minutes = (e_dt - s_dt).total_seconds() / 60.0
            if not (15.0 <= duration_minutes <= 240.0):
                raise ValueError("Visit interval must be between 15 and 240 minutes")
        return self


class VisitCheckInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checked_in_at: str = Field(..., description="UTC ISO-8601 timestamp in 2025-2035")

    @field_validator("checked_in_at")
    @classmethod
    def validate_ts(cls, v: str) -> str:
        return parse_utc_timestamp(v).isoformat()


class VisitCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completed_at: str = Field(..., description="UTC ISO-8601 timestamp in 2025-2035")

    @field_validator("completed_at")
    @classmethod
    def validate_ts(cls, v: str) -> str:
        return parse_utc_timestamp(v).isoformat()


class VisitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    property_id: int
    agent_id: int
    visitor_user_id: int
    visitor_name: str
    start_at: str
    end_at: str
    status: VisitStatusEnum
    checked_in_at: Optional[str]
    completed_at: Optional[str]
    cancelled_at: Optional[str]
    created_at: str
    updated_at: str


class VisitListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[VisitResponse]
    limit: int
    next_after_id: Optional[int]


# -----------------------------------------------------------------------------
# Database Dependency & Auth (MVC: Controller)
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
        if not authorization:
            raise AppException(401, "Authorization header required", "Unauthorized")
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise AppException(401, "Invalid authorization scheme, Bearer required", "Unauthorized")

        raw_token = parts[1]
        token_hashed = hash_token(raw_token)
        user = db.scalar(select(UserModel).where(UserModel.token_hash == token_hashed))
        if not user:
            raise AppException(401, "Invalid or expired token", "Unauthorized")
        return user


# -----------------------------------------------------------------------------
# Business Controllers (MVC: Controller)
# -----------------------------------------------------------------------------
class AgentController:
    @classmethod
    def serialize(cls, a: AgentModel) -> AgentResponse:
        return AgentResponse(
            id=a.id,
            name=a.name,
            license_code=a.license_code,
            active=a.active,
            created_at=a.created_at.isoformat(),
            updated_at=a.updated_at.isoformat(),
        )

    @classmethod
    def get_by_id(cls, db: Session, agent_id: int) -> AgentModel:
        if agent_id <= 0:
            raise AppException(404, "Agent not found", "Not Found")
        agent = db.scalar(select(AgentModel).where(AgentModel.id == agent_id, AgentModel.is_deleted == False))
        if not agent:
            raise AppException(404, "Agent not found", "Not Found")
        return agent

    @classmethod
    def list_agents(
        cls,
        db: Session,
        active: Optional[bool] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> AgentListResponse:
        query = select(AgentModel).where(AgentModel.is_deleted == False)
        if active is not None:
            query = query.where(AgentModel.active == active)
        if after_id is not None:
            query = query.where(AgentModel.id > after_id)

        query = query.order_by(AgentModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return AgentListResponse(
            items=[cls.serialize(r) for r in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def create_agent(cls, db: Session, current_user: UserModel, payload: AgentCreateRequest) -> AgentModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Agents are admin-managed", "Forbidden")

        if db.scalar(select(AgentModel).where(AgentModel.license_code == payload.license_code, AgentModel.is_deleted == False)):
            raise AppException(409, "Agent with this license code already exists", "Conflict")

        agent = AgentModel(
            name=payload.name,
            license_code=payload.license_code,
            active=payload.active if payload.active is not None else True,
            is_deleted=False,
        )
        db.add(agent)
        try:
            db.commit()
            db.refresh(agent)
            return agent
        except Exception:
            db.rollback()
            raise AppException(500, "Database error creating agent", "Internal Server Error")

    @classmethod
    def patch_agent(cls, db: Session, current_user: UserModel, agent_id: int, payload: AgentPatchRequest) -> AgentModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Agents are admin-managed", "Forbidden")

        agent = cls.get_by_id(db, agent_id)

        if payload.license_code is not None and payload.license_code != agent.license_code:
            if db.scalar(select(AgentModel).where(AgentModel.license_code == payload.license_code, AgentModel.id != agent.id, AgentModel.is_deleted == False)):
                raise AppException(409, "Agent with this license code already exists", "Conflict")
            agent.license_code = payload.license_code

        if payload.name is not None:
            agent.name = payload.name

        if payload.active is not None and payload.active is False and agent.active is True:
            has_active_props = db.scalar(
                select(PropertyModel).where(
                    PropertyModel.agent_id == agent.id,
                    PropertyModel.status == PropertyStatusEnum.ACTIVE.value,
                    PropertyModel.is_deleted == False,
                ).limit(1)
            )
            if has_active_props:
                raise AppException(409, "Cannot deactivate agent with active assigned properties", "Conflict")
            agent.active = False
        elif payload.active is not None:
            agent.active = payload.active

        try:
            db.commit()
            db.refresh(agent)
            return agent
        except Exception:
            db.rollback()
            raise AppException(500, "Database error updating agent", "Internal Server Error")

    @classmethod
    def delete_agent(cls, db: Session, current_user: UserModel, agent_id: int) -> None:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Agents are admin-managed", "Forbidden")

        agent = cls.get_by_id(db, agent_id)

        has_props = db.scalar(select(PropertyModel).where(PropertyModel.agent_id == agent.id, PropertyModel.is_deleted == False).limit(1))
        if has_props:
            raise AppException(409, "Cannot delete agent referenced by properties", "Conflict")
        has_visits = db.scalar(select(VisitModel).where(VisitModel.agent_id == agent.id).limit(1))
        if has_visits:
            raise AppException(409, "Cannot delete agent referenced by visits", "Conflict")

        agent.is_deleted = True
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(500, "Database error deleting agent", "Internal Server Error")


class PropertyController:
    @classmethod
    def serialize(cls, p: PropertyModel) -> PropertyResponse:
        return PropertyResponse(
            id=p.id,
            address=p.address,
            city=p.city,
            status=PropertyStatusEnum(p.status),
            agent_id=p.agent_id,
            created_at=p.created_at.isoformat(),
            updated_at=p.updated_at.isoformat(),
        )

    @classmethod
    def get_by_id(cls, db: Session, property_id: int) -> PropertyModel:
        if property_id <= 0:
            raise AppException(404, "Property not found", "Not Found")
        prop = db.scalar(select(PropertyModel).where(PropertyModel.id == property_id, PropertyModel.is_deleted == False))
        if not prop:
            raise AppException(404, "Property not found", "Not Found")
        return prop

    @classmethod
    def list_properties(
        cls,
        db: Session,
        status: Optional[PropertyStatusEnum] = None,
        agent_id: Optional[int] = None,
        city: Optional[str] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> PropertyListResponse:
        query = select(PropertyModel).where(PropertyModel.is_deleted == False)
        if status is not None:
            query = query.where(PropertyModel.status == status.value)
        if agent_id is not None:
            query = query.where(PropertyModel.agent_id == agent_id)
        if city is not None:
            query = query.where(PropertyModel.city.ilike(city.strip()))
        if after_id is not None:
            query = query.where(PropertyModel.id > after_id)

        query = query.order_by(PropertyModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return PropertyListResponse(
            items=[cls.serialize(r) for r in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def create_property(cls, db: Session, current_user: UserModel, payload: PropertyCreateRequest) -> PropertyModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Properties are admin-managed", "Forbidden")

        agent = AgentController.get_by_id(db, payload.agent_id)
        if not agent.active:
            raise AppException(409, "Assigned agent must be active", "Conflict")

        prop = PropertyModel(
            address=payload.address,
            city=payload.city or "Milano",
            status=payload.status.value if payload.status else PropertyStatusEnum.ACTIVE.value,
            agent_id=agent.id,
            is_deleted=False,
        )
        db.add(prop)
        try:
            db.commit()
            db.refresh(prop)
            return prop
        except Exception:
            db.rollback()
            raise AppException(500, "Database error creating property", "Internal Server Error")

    @classmethod
    def patch_property(cls, db: Session, current_user: UserModel, property_id: int, payload: PropertyPatchRequest) -> PropertyModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Properties are admin-managed", "Forbidden")

        prop = cls.get_by_id(db, property_id)

        if payload.agent_id is not None and payload.agent_id != prop.agent_id:
            new_agent = AgentController.get_by_id(db, payload.agent_id)
            if not new_agent.active:
                raise AppException(409, "Assigned agent must be active", "Conflict")
            prop.agent_id = new_agent.id

        if payload.address is not None:
            prop.address = payload.address
        if payload.city is not None:
            prop.city = payload.city
        if payload.status is not None:
            prop.status = payload.status.value

        # Active property must have active assigned agent
        if prop.status == PropertyStatusEnum.ACTIVE.value:
            assigned_agent = AgentController.get_by_id(db, prop.agent_id)
            if not assigned_agent.active:
                raise AppException(409, "Active property must have an active assigned agent", "Conflict")

        try:
            db.commit()
            db.refresh(prop)
            return prop
        except Exception:
            db.rollback()
            raise AppException(500, "Database error updating property", "Internal Server Error")

    @classmethod
    def delete_property(cls, db: Session, current_user: UserModel, property_id: int) -> None:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Properties are admin-managed", "Forbidden")

        prop = cls.get_by_id(db, property_id)

        # REQ-6-014: soft-delete only an ARCHIVED property without visits
        if prop.status != PropertyStatusEnum.ARCHIVED.value:
            raise AppException(409, "Only ARCHIVED properties can be deleted", "Conflict")

        has_visits = db.scalar(select(VisitModel).where(VisitModel.property_id == prop.id).limit(1))
        if has_visits:
            raise AppException(409, "Cannot delete property with associated visits", "Conflict")

        prop.is_deleted = True
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(500, "Database error deleting property", "Internal Server Error")


class VisitController:
    @classmethod
    def serialize(cls, v: VisitModel) -> VisitResponse:
        return VisitResponse(
            id=v.id,
            property_id=v.property_id,
            agent_id=v.agent_id,
            visitor_user_id=v.visitor_user_id,
            visitor_name=v.visitor_name,
            start_at=v.start_at.isoformat(),
            end_at=v.end_at.isoformat(),
            status=VisitStatusEnum(v.status),
            checked_in_at=v.checked_in_at.isoformat() if v.checked_in_at else None,
            completed_at=v.completed_at.isoformat() if v.completed_at else None,
            cancelled_at=v.cancelled_at.isoformat() if v.cancelled_at else None,
            created_at=v.created_at.isoformat(),
            updated_at=v.updated_at.isoformat(),
        )

    @classmethod
    def get_by_id(cls, db: Session, visit_id: int) -> VisitModel:
        if visit_id <= 0:
            raise AppException(404, "Visit not found", "Not Found")
        visit = db.scalar(select(VisitModel).where(VisitModel.id == visit_id))
        if not visit:
            raise AppException(404, "Visit not found", "Not Found")
        return visit

    @classmethod
    def check_conflicts(
        cls,
        db: Session,
        agent_id: int,
        property_id: int,
        start_at: datetime,
        end_at: datetime,
        exclude_visit_id: Optional[int] = None,
    ) -> None:
        # REQ-6-005: An agent and a property cannot have overlapping SCHEDULED or CHECKED_IN visits
        # Intervals are half-open: [s1, e1) and [s2, e2) overlap iff s1 < e2 and s2 < e1
        active_statuses = [VisitStatusEnum.SCHEDULED.value, VisitStatusEnum.CHECKED_IN.value]

        # Agent conflict query
        agent_query = select(VisitModel).where(
            VisitModel.agent_id == agent_id,
            VisitModel.status.in_(active_statuses),
            VisitModel.start_at < end_at,
            VisitModel.end_at > start_at,
        )
        if exclude_visit_id:
            agent_query = agent_query.where(VisitModel.id != exclude_visit_id)
        if db.scalar(agent_query.limit(1)):
            raise AppException(409, "Agent has an overlapping scheduled or checked-in visit", "Conflict")

        # Property conflict query
        prop_query = select(VisitModel).where(
            VisitModel.property_id == property_id,
            VisitModel.status.in_(active_statuses),
            VisitModel.start_at < end_at,
            VisitModel.end_at > start_at,
        )
        if exclude_visit_id:
            prop_query = prop_query.where(VisitModel.id != exclude_visit_id)
        if db.scalar(prop_query.limit(1)):
            raise AppException(409, "Property has an overlapping scheduled or checked-in visit", "Conflict")

    @classmethod
    def list_visits(
        cls,
        db: Session,
        current_user: UserModel,
        property_id: Optional[int] = None,
        agent_id: Optional[int] = None,
        status: Optional[VisitStatusEnum] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> VisitListResponse:
        query = select(VisitModel)

        # REQ-6-007: Users own their visits; assigned agents and admins can view visits
        if current_user.role != UserRole.ADMIN.value:
            if current_user.agent_id:
                # Agent user: can see visits where they are assigned OR where they are visitor
                query = query.where(
                    or_(
                        VisitModel.agent_id == current_user.agent_id,
                        VisitModel.visitor_user_id == current_user.id,
                    )
                )
            else:
                # Regular visitor user: only own visits
                query = query.where(VisitModel.visitor_user_id == current_user.id)

        if property_id is not None:
            query = query.where(VisitModel.property_id == property_id)
        if agent_id is not None:
            query = query.where(VisitModel.agent_id == agent_id)
        if status is not None:
            query = query.where(VisitModel.status == status.value)
        if after_id is not None:
            query = query.where(VisitModel.id > after_id)

        query = query.order_by(VisitModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return VisitListResponse(
            items=[cls.serialize(r) for r in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def create_visit(cls, db: Session, current_user: UserModel, payload: VisitCreateRequest) -> VisitModel:
        prop = PropertyController.get_by_id(db, payload.property_id)
        if prop.status != PropertyStatusEnum.ACTIVE.value:
            raise AppException(409, "Visits can only be scheduled for ACTIVE properties", "Conflict")

        agent = AgentController.get_by_id(db, payload.agent_id)
        if not agent.active:
            raise AppException(409, "Assigned agent must be active", "Conflict")

        if prop.agent_id != agent.id:
            raise AppException(409, "Assigned agent does not manage this property", "Conflict")

        s_dt = parse_utc_timestamp(payload.start_at)
        e_dt = parse_utc_timestamp(payload.end_at)

        # Conflict check
        cls.check_conflicts(db, agent.id, prop.id, s_dt, e_dt)

        visit = VisitModel(
            property_id=prop.id,
            agent_id=agent.id,
            visitor_user_id=current_user.id,
            visitor_name=payload.visitor_name,
            start_at=s_dt,
            end_at=e_dt,
            status=VisitStatusEnum.SCHEDULED.value,
        )
        db.add(visit)
        db.flush()

        history = VisitHistoryModel(
            visit_id=visit.id,
            from_state="NONE",
            to_state=VisitStatusEnum.SCHEDULED.value,
            action_time=s_dt,
            user_id=current_user.id,
        )
        db.add(history)

        try:
            db.commit()
            db.refresh(visit)
            return visit
        except Exception:
            db.rollback()
            raise AppException(500, "Database error creating visit", "Internal Server Error")

    @classmethod
    def patch_visit(cls, db: Session, current_user: UserModel, visit_id: int, payload: VisitPatchRequest) -> VisitModel:
        visit = cls.get_by_id(db, visit_id)

        # Authorization: visitor owner or admin
        if current_user.role != UserRole.ADMIN.value and current_user.id != visit.visitor_user_id:
            raise AppException(403, "Only the visitor owner or an admin can edit this visit", "Forbidden")

        if visit.status != VisitStatusEnum.SCHEDULED.value:
            raise AppException(409, "Only visits in SCHEDULED status can be updated", "Conflict")

        target_prop_id = payload.property_id or visit.property_id
        target_agent_id = payload.agent_id or visit.agent_id
        target_start = parse_utc_timestamp(payload.start_at) if payload.start_at else visit.start_at
        target_end = parse_utc_timestamp(payload.end_at) if payload.end_at else visit.end_at

        # Verify property & agent
        prop = PropertyController.get_by_id(db, target_prop_id)
        if prop.status != PropertyStatusEnum.ACTIVE.value:
            raise AppException(409, "Property must be ACTIVE", "Conflict")
        agent = AgentController.get_by_id(db, target_agent_id)
        if not agent.active:
            raise AppException(409, "Agent must be active", "Conflict")
        if prop.agent_id != agent.id:
            raise AppException(409, "Assigned agent does not manage this property", "Conflict")

        # Check conflicts with new params
        cls.check_conflicts(db, target_agent_id, target_prop_id, target_start, target_end, exclude_visit_id=visit.id)

        visit.property_id = target_prop_id
        visit.agent_id = target_agent_id
        visit.start_at = target_start
        visit.end_at = target_end
        if payload.visitor_name is not None:
            visit.visitor_name = payload.visitor_name

        try:
            db.commit()
            db.refresh(visit)
            return visit
        except Exception:
            db.rollback()
            raise AppException(500, "Database error updating visit", "Internal Server Error")

    @classmethod
    def delete_visit(cls, db: Session, current_user: UserModel, visit_id: int) -> None:
        visit = cls.get_by_id(db, visit_id)

        # Authorization: visitor owner, assigned agent, or admin
        is_visitor = current_user.id == visit.visitor_user_id
        is_agent = current_user.agent_id and current_user.agent_id == visit.agent_id
        is_admin = current_user.role == UserRole.ADMIN.value
        if not (is_visitor or is_agent or is_admin):
            raise AppException(403, "Permission denied to cancel this visit", "Forbidden")

        # REQ-6-024: delete only a SCHEDULED visit and retain cancellation audit
        if visit.status != VisitStatusEnum.SCHEDULED.value:
            raise AppException(409, "Only visits in SCHEDULED status can be cancelled", "Conflict")

        now_utc = datetime.now(timezone.utc)
        visit.status = VisitStatusEnum.CANCELLED.value
        visit.cancelled_at = now_utc

        history = VisitHistoryModel(
            visit_id=visit.id,
            from_state=VisitStatusEnum.SCHEDULED.value,
            to_state=VisitStatusEnum.CANCELLED.value,
            action_time=now_utc,
            user_id=current_user.id,
        )
        db.add(history)

        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(500, "Database error cancelling visit", "Internal Server Error")

    @classmethod
    def check_in(cls, db: Session, current_user: UserModel, visit_id: int, payload: VisitCheckInRequest) -> VisitModel:
        visit = cls.get_by_id(db, visit_id)

        # REQ-6-025: visitor, assigned agent, or admin
        is_visitor = current_user.id == visit.visitor_user_id
        is_agent = current_user.agent_id and current_user.agent_id == visit.agent_id
        is_admin = current_user.role == UserRole.ADMIN.value
        if not (is_visitor or is_agent or is_admin):
            raise AppException(403, "Permission denied to check in this visit", "Forbidden")

        dt = parse_utc_timestamp(payload.checked_in_at)

        # Idempotent check
        if visit.status == VisitStatusEnum.CHECKED_IN.value:
            curr_in = visit.checked_in_at
            if curr_in and curr_in.tzinfo is None:
                curr_in = curr_in.replace(tzinfo=timezone.utc)
            if curr_in == dt:
                return visit

        # REQ-6-006: transition SCHEDULED to CHECKED_IN
        if visit.status != VisitStatusEnum.SCHEDULED.value:
            raise AppException(409, f"Cannot check in visit in status {visit.status}", "Conflict")

        from_state = visit.status
        visit.status = VisitStatusEnum.CHECKED_IN.value
        visit.checked_in_at = dt

        history = VisitHistoryModel(
            visit_id=visit.id,
            from_state=from_state,
            to_state=VisitStatusEnum.CHECKED_IN.value,
            action_time=dt,
            user_id=current_user.id,
        )
        db.add(history)

        try:
            db.commit()
            db.refresh(visit)
            return visit
        except Exception:
            db.rollback()
            raise AppException(500, "Database error during check-in", "Internal Server Error")

    @classmethod
    def complete(cls, db: Session, current_user: UserModel, visit_id: int, payload: VisitCompleteRequest) -> VisitModel:
        visit = cls.get_by_id(db, visit_id)

        # REQ-6-026: visitor, assigned agent, or admin
        is_visitor = current_user.id == visit.visitor_user_id
        is_agent = current_user.agent_id and current_user.agent_id == visit.agent_id
        is_admin = current_user.role == UserRole.ADMIN.value
        if not (is_visitor or is_agent or is_admin):
            raise AppException(403, "Permission denied to complete this visit", "Forbidden")

        dt = parse_utc_timestamp(payload.completed_at)

        # Idempotent check
        if visit.status == VisitStatusEnum.COMPLETED.value:
            curr_comp = visit.completed_at
            if curr_comp and curr_comp.tzinfo is None:
                curr_comp = curr_comp.replace(tzinfo=timezone.utc)
            if curr_comp == dt:
                return visit

        # REQ-6-006: transition CHECKED_IN to COMPLETED
        if visit.status != VisitStatusEnum.CHECKED_IN.value:
            raise AppException(409, f"Cannot complete visit in status {visit.status}", "Conflict")

        from_state = visit.status
        visit.status = VisitStatusEnum.COMPLETED.value
        visit.completed_at = dt

        history = VisitHistoryModel(
            visit_id=visit.id,
            from_state=from_state,
            to_state=VisitStatusEnum.COMPLETED.value,
            action_time=dt,
            user_id=current_user.id,
        )
        db.add(history)

        try:
            db.commit()
            db.refresh(visit)
            return visit
        except Exception:
            db.rollback()
            raise AppException(500, "Database error completing visit", "Internal Server Error")

    @classmethod
    def list_agent_conflicts(
        cls,
        db: Session,
        agent_id: int,
        start_at: datetime,
        end_at: datetime,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> VisitListResponse:
        AgentController.get_by_id(db, agent_id)

        active_statuses = [VisitStatusEnum.SCHEDULED.value, VisitStatusEnum.CHECKED_IN.value]
        query = select(VisitModel).where(
            VisitModel.agent_id == agent_id,
            VisitModel.status.in_(active_statuses),
            VisitModel.start_at < end_at,
            VisitModel.end_at > start_at,
        )
        if after_id is not None:
            query = query.where(VisitModel.id > after_id)

        query = query.order_by(VisitModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return VisitListResponse(
            items=[cls.serialize(r) for r in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def list_property_conflicts(
        cls,
        db: Session,
        property_id: int,
        start_at: datetime,
        end_at: datetime,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> VisitListResponse:
        PropertyController.get_by_id(db, property_id)

        active_statuses = [VisitStatusEnum.SCHEDULED.value, VisitStatusEnum.CHECKED_IN.value]
        query = select(VisitModel).where(
            VisitModel.property_id == property_id,
            VisitModel.status.in_(active_statuses),
            VisitModel.start_at < end_at,
            VisitModel.end_at > start_at,
        )
        if after_id is not None:
            query = query.where(VisitModel.id > after_id)

        query = query.order_by(VisitModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return VisitListResponse(
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

    # Agents
    a1 = AgentModel(
        id=1,
        name="Bob Agent",
        license_code="LIC-001",
        active=True,
        is_deleted=False,
    )
    a2 = AgentModel(
        id=2,
        name="Carol Admin Agent",
        license_code="LIC-002",
        active=True,
        is_deleted=False,
    )
    db.add_all([a1, a2])
    db.flush()

    # Users
    u1 = UserModel(
        id=1,
        username="user1",
        role=UserRole.USER.value,
        token_hash=hash_token(user1_token),
        token=user1_token,
        agent_id=None,
    )
    u2 = UserModel(
        id=2,
        username="user2",
        role=UserRole.USER.value,
        token_hash=hash_token(user2_token),
        token=user2_token,
        agent_id=1,
    )
    u3 = UserModel(
        id=3,
        username="admin",
        role=UserRole.ADMIN.value,
        token_hash=hash_token(admin_token),
        token=admin_token,
        agent_id=2,
    )
    db.add_all([u1, u2, u3])
    db.flush()

    # Properties
    p1 = PropertyModel(
        id=1,
        address="Via Montenapoleone 10",
        city="Milano",
        status=PropertyStatusEnum.ACTIVE.value,
        agent_id=1,
        is_deleted=False,
    )
    p2 = PropertyModel(
        id=2,
        address="Corso Como 15",
        city="Milano",
        status=PropertyStatusEnum.ACTIVE.value,
        agent_id=1,
        is_deleted=False,
    )
    p3 = PropertyModel(
        id=3,
        address="Via Bellagio 5",
        city="Como",
        status=PropertyStatusEnum.ARCHIVED.value,
        agent_id=2,
        is_deleted=False,
    )
    db.add_all([p1, p2, p3])
    db.flush()

    # Visits
    dt_start = datetime(2025, 5, 10, 10, 0, 0, tzinfo=timezone.utc)
    dt_end = datetime(2025, 5, 10, 11, 0, 0, tzinfo=timezone.utc)
    v1 = VisitModel(
        id=1,
        property_id=1,
        agent_id=1,
        visitor_user_id=1,
        visitor_name="Alice Visitor",
        start_at=dt_start,
        end_at=dt_end,
        status=VisitStatusEnum.SCHEDULED.value,
    )
    db.add(v1)
    db.flush()

    h1 = VisitHistoryModel(
        id=1,
        visit_id=1,
        from_state="NONE",
        to_state=VisitStatusEnum.SCHEDULED.value,
        action_time=dt_start,
        user_id=1,
    )
    db.add(h1)
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
    title="Real-Estate Management - Visits & Agent-Property Conflicts API",
    version="1.0.0",
    description="Deterministic RESTful API for properties, agents, visits scheduling, lifecycle transitions and overlap conflict detection.",
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


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
    error_schema = {"$ref": "#/components/schemas/ErrorResponse"}
    error_content = {"application/json": {"schema": error_schema}}
    error_descriptions = {
        "400": "Bad Request",
        "401": "Unauthorized",
        "403": "Forbidden",
        "404": "Not Found",
        "409": "Conflict",
        "413": "Payload Too Large",
        "415": "Unsupported Media Type",
        "422": "Unprocessable Entity",
    }
    for path, item in schema.get("paths", {}).items():
        if path == "/health":
            continue
        for method, operation in item.items():
            responses = operation.setdefault("responses", {})
            codes = {"401", "403", "422"}
            if "{" in path:
                codes.add("404")
            if method in {"post", "patch", "put"}:
                codes.update({"400", "413", "415", "409"})
            if method == "delete":
                codes.add("409")
            for code in codes:
                responses[code] = {"description": error_descriptions[code], "content": error_content}
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


# -----------------------------------------------------------------------------
# Endpoints (20 Specified Endpoints for Variant B)
# -----------------------------------------------------------------------------

# 1. GET /health
@app.get(
    "/health",
    tags=["Health"],
    response_model=HealthResponse,
    responses={
        200: {"model": HealthResponse, "description": "Service operational and database connected"},
        503: {"model": ErrorResponse, "description": "Service Unavailable"},
    },
)
def get_health(db: Session = Depends(get_db)):
    try:
        db.execute(select(1))
        return HealthResponse(status="ok", database="connected")
    except Exception as ex:
        logger.error("Health probe failed: %s", ex)
        raise AppException(503, "Database connectivity probe failed", "Service Unavailable")


# 2. GET /properties
@app.get(
    "/properties",
    tags=["Properties"],
    response_model=PropertyListResponse,
)
def list_properties(
    status: Optional[PropertyStatusEnum] = Query(None),
    agent_id: Optional[int] = Query(None, gt=0),
    city: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return PropertyController.list_properties(db, status, agent_id, city, limit, after_id)


# 3. POST /properties
@app.post(
    "/properties",
    tags=["Properties"],
    status_code=status.HTTP_201_CREATED,
    response_model=PropertyResponse,
)
def create_property(
    payload: PropertyCreateRequest,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    prop = PropertyController.create_property(db, current_user, payload)
    response.headers["Location"] = f"/properties/{prop.id}"
    return PropertyController.serialize(prop)


# 4. GET /properties/{property_id}
@app.get(
    "/properties/{property_id}",
    tags=["Properties"],
    response_model=PropertyResponse,
)
def get_property(
    property_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    prop = PropertyController.get_by_id(db, property_id)
    return PropertyController.serialize(prop)


# 5. PATCH /properties/{property_id}
@app.patch(
    "/properties/{property_id}",
    tags=["Properties"],
    response_model=PropertyResponse,
)
def patch_property(
    property_id: int,
    payload: PropertyPatchRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    prop = PropertyController.patch_property(db, current_user, property_id, payload)
    return PropertyController.serialize(prop)


# 6. DELETE /properties/{property_id}
@app.delete(
    "/properties/{property_id}",
    tags=["Properties"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_property(
    property_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    PropertyController.delete_property(db, current_user, property_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 7. GET /agents
@app.get(
    "/agents",
    tags=["Agents"],
    response_model=AgentListResponse,
)
def list_agents(
    active: Optional[bool] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return AgentController.list_agents(db, active, limit, after_id)


# 8. POST /agents
@app.post(
    "/agents",
    tags=["Agents"],
    status_code=status.HTTP_201_CREATED,
    response_model=AgentResponse,
)
def create_agent(
    payload: AgentCreateRequest,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    agent = AgentController.create_agent(db, current_user, payload)
    response.headers["Location"] = f"/agents/{agent.id}"
    return AgentController.serialize(agent)


# 9. GET /agents/{agent_id}
@app.get(
    "/agents/{agent_id}",
    tags=["Agents"],
    response_model=AgentResponse,
)
def get_agent(
    agent_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    agent = AgentController.get_by_id(db, agent_id)
    return AgentController.serialize(agent)


# 10. PATCH /agents/{agent_id}
@app.patch(
    "/agents/{agent_id}",
    tags=["Agents"],
    response_model=AgentResponse,
)
def patch_agent(
    agent_id: int,
    payload: AgentPatchRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    agent = AgentController.patch_agent(db, current_user, agent_id, payload)
    return AgentController.serialize(agent)


# 11. DELETE /agents/{agent_id}
@app.delete(
    "/agents/{agent_id}",
    tags=["Agents"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_agent(
    agent_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    AgentController.delete_agent(db, current_user, agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 12. GET /visits
@app.get(
    "/visits",
    tags=["Visits"],
    response_model=VisitListResponse,
)
def list_visits(
    property_id: Optional[int] = Query(None, gt=0),
    agent_id: Optional[int] = Query(None, gt=0),
    status: Optional[VisitStatusEnum] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return VisitController.list_visits(db, current_user, property_id, agent_id, status, limit, after_id)


# 13. POST /visits
@app.post(
    "/visits",
    tags=["Visits"],
    status_code=status.HTTP_201_CREATED,
    response_model=VisitResponse,
)
def create_visit(
    payload: VisitCreateRequest,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    visit = VisitController.create_visit(db, current_user, payload)
    response.headers["Location"] = f"/visits/{visit.id}"
    return VisitController.serialize(visit)


# 14. GET /visits/{visit_id}
@app.get(
    "/visits/{visit_id}",
    tags=["Visits"],
    response_model=VisitResponse,
)
def get_visit(
    visit_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    visit = VisitController.get_by_id(db, visit_id)
    is_visitor = current_user.id == visit.visitor_user_id
    is_agent = current_user.agent_id and current_user.agent_id == visit.agent_id
    is_admin = current_user.role == UserRole.ADMIN.value
    if not (is_visitor or is_agent or is_admin):
        raise AppException(403, "Permission denied to view this visit", "Forbidden")
    return VisitController.serialize(visit)


# 15. PATCH /visits/{visit_id}
@app.patch(
    "/visits/{visit_id}",
    tags=["Visits"],
    response_model=VisitResponse,
)
def patch_visit(
    visit_id: int,
    payload: VisitPatchRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    visit = VisitController.patch_visit(db, current_user, visit_id, payload)
    return VisitController.serialize(visit)


# 16. DELETE /visits/{visit_id}
@app.delete(
    "/visits/{visit_id}",
    tags=["Visits"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_visit(
    visit_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    VisitController.delete_visit(db, current_user, visit_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 17. POST /visits/{visit_id}/check-in
@app.post(
    "/visits/{visit_id}/check-in",
    tags=["Visits"],
    response_model=VisitResponse,
)
def check_in_visit(
    visit_id: int,
    payload: VisitCheckInRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    visit = VisitController.check_in(db, current_user, visit_id, payload)
    return VisitController.serialize(visit)


# 18. POST /visits/{visit_id}/complete
@app.post(
    "/visits/{visit_id}/complete",
    tags=["Visits"],
    response_model=VisitResponse,
)
def complete_visit(
    visit_id: int,
    payload: VisitCompleteRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    visit = VisitController.complete(db, current_user, visit_id, payload)
    return VisitController.serialize(visit)


# 19. GET /agents/{agent_id}/visit-conflicts
@app.get(
    "/agents/{agent_id}/visit-conflicts",
    tags=["Agents"],
    response_model=VisitListResponse,
)
def get_agent_conflicts(
    agent_id: int,
    start_at: str = Query(..., description="UTC ISO timestamp in 2025-2035"),
    end_at: str = Query(..., description="UTC ISO timestamp in 2025-2035"),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role != UserRole.ADMIN.value:
        raise AppException(403, "Only admins can view agent conflict reports", "Forbidden")
    try:
        s_dt = parse_utc_timestamp(start_at)
        e_dt = parse_utc_timestamp(end_at)
    except ValueError as exc:
        raise AppException(422, str(exc), "Unprocessable Entity")
    if e_dt <= s_dt:
        raise AppException(422, "end_at must be strictly greater than start_at", "Unprocessable Entity")
    return VisitController.list_agent_conflicts(db, agent_id, s_dt, e_dt, limit, after_id)


# 20. GET /properties/{property_id}/visit-conflicts
@app.get(
    "/properties/{property_id}/visit-conflicts",
    tags=["Properties"],
    response_model=VisitListResponse,
)
def get_property_conflicts(
    property_id: int,
    start_at: str = Query(..., description="UTC ISO timestamp in 2025-2035"),
    end_at: str = Query(..., description="UTC ISO timestamp in 2025-2035"),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.role != UserRole.ADMIN.value:
        raise AppException(403, "Only admins can view property conflict reports", "Forbidden")
    try:
        s_dt = parse_utc_timestamp(start_at)
        e_dt = parse_utc_timestamp(end_at)
    except ValueError as exc:
        raise AppException(422, str(exc), "Unprocessable Entity")
    if e_dt <= s_dt:
        raise AppException(422, "end_at must be strictly greater than start_at", "Unprocessable Entity")
    return VisitController.list_property_conflicts(db, property_id, s_dt, e_dt, limit, after_id)
