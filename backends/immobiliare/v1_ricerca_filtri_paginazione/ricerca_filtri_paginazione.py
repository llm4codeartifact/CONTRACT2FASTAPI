"""
Real-Estate Management - Variant A: Search with filters and pagination
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
    Text,
    create_engine,
    func,
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
logger = logging.getLogger("immobiliare_variant_a")
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
LICENSE_REGEX = re.compile(r"^[A-Z0-9-]{4,20}$")


# -----------------------------------------------------------------------------
# Helper Security Utilities & Domain Validators
# -----------------------------------------------------------------------------
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# -----------------------------------------------------------------------------
# Enums
# -----------------------------------------------------------------------------
class UserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


class PropertyTypeEnum(str, enum.Enum):
    APARTMENT = "APARTMENT"
    HOUSE = "HOUSE"
    VILLA = "VILLA"
    OFFICE = "OFFICE"
    LAND = "LAND"


class PropertyStatusEnum(str, enum.Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class SortEnum(str, enum.Enum):
    PRICE_ASC = "PRICE_ASC"
    PRICE_DESC = "PRICE_DESC"
    AREA_DESC = "AREA_DESC"
    ID_ASC = "ID_ASC"


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
    saved_searches = relationship("SavedSearchModel", back_populates="user", cascade="all, delete-orphan")
    favorites = relationship("FavoriteModel", back_populates="user", cascade="all, delete-orphan")


class AgentModel(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    full_name = Column(String(120), nullable=False)
    email = Column(String(254), unique=True, nullable=False, index=True)
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


class PropertyModel(Base):
    __tablename__ = "properties"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(160), nullable=False)
    property_type = Column(String(20), nullable=False)
    city = Column(String(80), nullable=False, index=True)
    area_sqm = Column(Integer, nullable=False)
    rooms = Column(Integer, nullable=False)
    price_cents = Column(Integer, nullable=False, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    status = Column(String(20), nullable=False, default=PropertyStatusEnum.ACTIVE.value)
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    agent = relationship("AgentModel", back_populates="properties", foreign_keys=[agent_id])
    favorites = relationship("FavoriteModel", back_populates="property", cascade="all, delete-orphan")


class SavedSearchModel(Base):
    __tablename__ = "saved_searches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(80), nullable=False)
    filters_json = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user = relationship("UserModel", back_populates="saved_searches")


class FavoriteModel(Base):
    __tablename__ = "favorites"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    user = relationship("UserModel", back_populates="favorites")
    property = relationship("PropertyModel", back_populates="favorites")


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
    full_name: str = Field(..., min_length=3, max_length=120)
    email: str = Field(..., min_length=5, max_length=254)
    license_code: str = Field(..., min_length=4, max_length=20)
    active: Optional[bool] = True

    @field_validator("full_name", "email", "license_code")
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

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, v: str) -> str:
        s = v.strip().lower()
        if "@" not in s or "." not in s.split("@")[-1]:
            raise ValueError("Invalid email format")
        return s


class AgentPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: Optional[str] = Field(None, min_length=3, max_length=120)
    email: Optional[str] = Field(None, min_length=5, max_length=254)
    license_code: Optional[str] = Field(None, min_length=4, max_length=20)
    active: Optional[bool] = None

    @field_validator("full_name", "email", "license_code")
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

    @field_validator("email")
    @classmethod
    def validate_email_format(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip().lower()
        if "@" not in s or "." not in s.split("@")[-1]:
            raise ValueError("Invalid email format")
        return s

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("full_name", "email", "license_code", "active"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class AgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    full_name: str
    email: str
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
    title: str = Field(..., min_length=5, max_length=160)
    property_type: PropertyTypeEnum
    city: str = Field(..., min_length=2, max_length=80)
    area_sqm: int = Field(..., ge=10, le=10000)
    rooms: int = Field(..., ge=0, le=50)
    price_cents: int = Field(..., ge=1000, le=2000000000)
    agent_id: Optional[int] = Field(None, gt=0)

    @field_validator("title", "city")
    @classmethod
    def strip_strings(cls, v: str) -> str:
        s = v.strip()
        if len(s) < 2:
            raise ValueError("Field length must be at least 2 characters after trimming")
        return s


class PropertyPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = Field(None, min_length=5, max_length=160)
    property_type: Optional[PropertyTypeEnum] = None
    city: Optional[str] = Field(None, min_length=2, max_length=80)
    area_sqm: Optional[int] = Field(None, ge=10, le=10000)
    rooms: Optional[int] = Field(None, ge=0, le=50)
    price_cents: Optional[int] = Field(None, ge=1000, le=2000000000)
    status: Optional[PropertyStatusEnum] = None
    agent_id: Optional[int] = Field(None, gt=0)

    @field_validator("title", "city")
    @classmethod
    def strip_strings(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if len(s) < 2:
            raise ValueError("Field length must be at least 2 characters after trimming")
        return s

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("title", "property_type", "city", "area_sqm", "rooms", "price_cents", "status", "agent_id"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class PropertyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    title: str
    property_type: PropertyTypeEnum
    city: str
    area_sqm: int
    rooms: int
    price_cents: int
    agent_id: int
    status: PropertyStatusEnum
    created_at: str
    updated_at: str


class PropertySearchListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[PropertyResponse]
    limit: int
    next_cursor: Optional[str]


class PropertyListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[PropertyResponse]
    limit: int
    next_after_id: Optional[int]


# Saved Search Schemas
class SearchFilterValues(BaseModel):
    model_config = ConfigDict(extra="forbid")
    city: Optional[str] = Field(None, min_length=2, max_length=80)
    property_type: Optional[PropertyTypeEnum] = None
    price_min: Optional[int] = Field(None, ge=1000, le=2000000000)
    price_max: Optional[int] = Field(None, ge=1000, le=2000000000)
    area_min: Optional[int] = Field(None, ge=10, le=10000)
    area_max: Optional[int] = Field(None, ge=10, le=10000)
    rooms_min: Optional[int] = Field(None, ge=0, le=50)
    rooms_max: Optional[int] = Field(None, ge=0, le=50)
    agent_id: Optional[int] = Field(None, gt=0)

    @model_validator(mode="after")
    def validate_bounds(self):
        if self.price_min is not None and self.price_max is not None and self.price_min > self.price_max:
            raise ValueError("price_min must be <= price_max")
        if self.area_min is not None and self.area_max is not None and self.area_min > self.area_max:
            raise ValueError("area_min must be <= area_max")
        if self.rooms_min is not None and self.rooms_max is not None and self.rooms_min > self.rooms_max:
            raise ValueError("rooms_min must be <= rooms_max")
        return self


class SavedSearchCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=2, max_length=80)
    filters: SearchFilterValues

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        s = v.strip()
        if len(s) < 2:
            raise ValueError("name must be at least 2 characters")
        return s


class SavedSearchPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=2, max_length=80)
    filters: Optional[SearchFilterValues] = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if len(s) < 2:
            raise ValueError("name must be at least 2 characters")
        return s

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("name", "filters"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class SavedSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    user_id: int
    name: str
    filters: SearchFilterValues
    created_at: str
    updated_at: str


class SavedSearchListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[SavedSearchResponse]
    limit: int
    next_after_id: Optional[int]


# Favorite Schemas
class FavoriteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    user_id: int
    property_id: int
    property: PropertyResponse
    created_at: str


class FavoriteListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[FavoriteResponse]
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
            full_name=a.full_name,
            email=a.email,
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
            raise AppException(403, "Only admins can create agents", "Forbidden")

        if db.scalar(select(AgentModel).where(AgentModel.email == payload.email, AgentModel.is_deleted == False)):
            raise AppException(409, "Agent with this email already exists", "Conflict")
        if db.scalar(select(AgentModel).where(AgentModel.license_code == payload.license_code, AgentModel.is_deleted == False)):
            raise AppException(409, "Agent with this license code already exists", "Conflict")

        agent = AgentModel(
            full_name=payload.full_name,
            email=payload.email,
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
            raise AppException(403, "Only admins can update agents", "Forbidden")

        agent = cls.get_by_id(db, agent_id)

        if payload.email is not None and payload.email != agent.email:
            if db.scalar(select(AgentModel).where(AgentModel.email == payload.email, AgentModel.id != agent.id, AgentModel.is_deleted == False)):
                raise AppException(409, "Agent with this email already exists", "Conflict")
            agent.email = payload.email

        if payload.license_code is not None and payload.license_code != agent.license_code:
            if db.scalar(select(AgentModel).where(AgentModel.license_code == payload.license_code, AgentModel.id != agent.id, AgentModel.is_deleted == False)):
                raise AppException(409, "Agent with this license code already exists", "Conflict")
            agent.license_code = payload.license_code

        if payload.full_name is not None:
            agent.full_name = payload.full_name

        # REQ-6-018: update supplied agent fields without orphaning active properties
        if payload.active is not None and payload.active is False and agent.active is True:
            has_active_props = db.scalar(
                select(PropertyModel).where(
                    PropertyModel.agent_id == agent.id,
                    PropertyModel.status == PropertyStatusEnum.ACTIVE.value,
                    PropertyModel.is_deleted == False,
                ).limit(1)
            )
            if has_active_props:
                raise AppException(409, "Cannot deactivate agent with active properties", "Conflict")
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
            raise AppException(403, "Only admins can delete agents", "Forbidden")

        agent = cls.get_by_id(db, agent_id)

        # REQ-6-004 & REQ-6-019: soft-delete an agent with no active properties
        has_active_props = db.scalar(
            select(PropertyModel).where(
                PropertyModel.agent_id == agent.id,
                PropertyModel.status == PropertyStatusEnum.ACTIVE.value,
                PropertyModel.is_deleted == False,
            ).limit(1)
        )
        if has_active_props:
            raise AppException(409, "Cannot delete agent with active properties", "Conflict")

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
            title=p.title,
            property_type=PropertyTypeEnum(p.property_type),
            city=p.city,
            area_sqm=p.area_sqm,
            rooms=p.rooms,
            price_cents=p.price_cents,
            agent_id=p.agent_id,
            status=PropertyStatusEnum(p.status),
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
    def search_properties(
        cls,
        db: Session,
        city: Optional[str] = None,
        property_type: Optional[PropertyTypeEnum] = None,
        price_min: Optional[int] = None,
        price_max: Optional[int] = None,
        area_min: Optional[int] = None,
        area_max: Optional[int] = None,
        rooms_min: Optional[int] = None,
        rooms_max: Optional[int] = None,
        agent_id: Optional[int] = None,
        status: Optional[PropertyStatusEnum] = PropertyStatusEnum.ACTIVE,
        sort: SortEnum = SortEnum.ID_ASC,
        cursor: Optional[str] = None,
        limit: int = 20,
    ) -> PropertySearchListResponse:
        # REQ-6-007: Validations
        if price_min is not None and price_max is not None and price_min > price_max:
            raise AppException(422, "price_min must be <= price_max", "Unprocessable Entity")
        if area_min is not None and area_max is not None and area_min > area_max:
            raise AppException(422, "area_min must be <= area_max", "Unprocessable Entity")
        if rooms_min is not None and rooms_max is not None and rooms_min > rooms_max:
            raise AppException(422, "rooms_min must be <= rooms_max", "Unprocessable Entity")

        query = select(PropertyModel).where(PropertyModel.is_deleted == False)

        # Status defaults to ACTIVE (REQ-6-007)
        if status is not None:
            query = query.where(PropertyModel.status == status.value)
        else:
            query = query.where(PropertyModel.status == PropertyStatusEnum.ACTIVE.value)

        # Filters with AND semantics
        if city is not None:
            c_clean = city.strip()
            query = query.where(func.lower(PropertyModel.city) == c_clean.lower())

        if property_type is not None:
            query = query.where(PropertyModel.property_type == property_type.value)

        if price_min is not None:
            query = query.where(PropertyModel.price_cents >= price_min)
        if price_max is not None:
            query = query.where(PropertyModel.price_cents <= price_max)

        if area_min is not None:
            query = query.where(PropertyModel.area_sqm >= area_min)
        if area_max is not None:
            query = query.where(PropertyModel.area_sqm <= area_max)

        if rooms_min is not None:
            query = query.where(PropertyModel.rooms >= rooms_min)
        if rooms_max is not None:
            query = query.where(PropertyModel.rooms <= rooms_max)

        if agent_id is not None:
            query = query.where(PropertyModel.agent_id == agent_id)

        # REQ-6-008: Cursor parsing and ordering
        if cursor:
            try:
                if sort == SortEnum.ID_ASC:
                    cur_id = int(cursor)
                    query = query.where(PropertyModel.id > cur_id)
                elif sort == SortEnum.PRICE_ASC:
                    parts = cursor.split("_")
                    cur_price = int(parts[0])
                    cur_id = int(parts[1]) if len(parts) > 1 else 0
                    query = query.where(
                        or_(
                            PropertyModel.price_cents > cur_price,
                            (PropertyModel.price_cents == cur_price) & (PropertyModel.id > cur_id),
                        )
                    )
                elif sort == SortEnum.PRICE_DESC:
                    parts = cursor.split("_")
                    cur_price = int(parts[0])
                    cur_id = int(parts[1]) if len(parts) > 1 else 0
                    query = query.where(
                        or_(
                            PropertyModel.price_cents < cur_price,
                            (PropertyModel.price_cents == cur_price) & (PropertyModel.id > cur_id),
                        )
                    )
                elif sort == SortEnum.AREA_DESC:
                    parts = cursor.split("_")
                    cur_area = int(parts[0])
                    cur_id = int(parts[1]) if len(parts) > 1 else 0
                    query = query.where(
                        or_(
                            PropertyModel.area_sqm < cur_area,
                            (PropertyModel.area_sqm == cur_area) & (PropertyModel.id > cur_id),
                        )
                    )
            except Exception:
                raise AppException(400, "Invalid cursor format", "Bad Request")

        # Sorting
        if sort == SortEnum.PRICE_ASC:
            query = query.order_by(PropertyModel.price_cents.asc(), PropertyModel.id.asc())
        elif sort == SortEnum.PRICE_DESC:
            query = query.order_by(PropertyModel.price_cents.desc(), PropertyModel.id.asc())
        elif sort == SortEnum.AREA_DESC:
            query = query.order_by(PropertyModel.area_sqm.desc(), PropertyModel.id.asc())
        else:
            query = query.order_by(PropertyModel.id.asc())

        query = query.limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]

        next_cursor = None
        if has_more and items:
            last = items[-1]
            if sort == SortEnum.PRICE_ASC:
                next_cursor = f"{last.price_cents}_{last.id}"
            elif sort == SortEnum.PRICE_DESC:
                next_cursor = f"{last.price_cents}_{last.id}"
            elif sort == SortEnum.AREA_DESC:
                next_cursor = f"{last.area_sqm}_{last.id}"
            else:
                next_cursor = str(last.id)

        return PropertySearchListResponse(
            items=[cls.serialize(r) for r in items],
            limit=limit,
            next_cursor=next_cursor,
        )

    @classmethod
    def create_property(cls, db: Session, current_user: UserModel, payload: PropertyCreateRequest) -> PropertyModel:
        # REQ-6-011: active agent user or admin
        if current_user.role == UserRole.ADMIN.value:
            if payload.agent_id is not None:
                agent = AgentController.get_by_id(db, payload.agent_id)
            else:
                agent = current_user.agent or AgentController.get_by_id(db, 1)
        else:
            if not current_user.agent_id:
                raise AppException(403, "Caller must be an active agent", "Forbidden")
            if payload.agent_id is not None and payload.agent_id != current_user.agent_id:
                raise AppException(403, "Cannot assign property to another agent", "Forbidden")
            agent = AgentController.get_by_id(db, current_user.agent_id)

        if not agent.active:
            raise AppException(409, "Assigned agent must be active", "Conflict")

        prop = PropertyModel(
            title=payload.title,
            property_type=payload.property_type.value,
            city=payload.city,
            area_sqm=payload.area_sqm,
            rooms=payload.rooms,
            price_cents=payload.price_cents,
            agent_id=agent.id,
            status=PropertyStatusEnum.ACTIVE.value,
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
        prop = cls.get_by_id(db, property_id)

        # REQ-6-013: assigned agent or admin
        if current_user.role != UserRole.ADMIN.value and current_user.agent_id != prop.agent_id:
            raise AppException(403, "Only the assigned agent or an admin can update this property", "Forbidden")

        if payload.agent_id is not None and payload.agent_id != prop.agent_id:
            if current_user.role != UserRole.ADMIN.value:
                raise AppException(403, "Only admins can reassign properties to other agents", "Forbidden")
            new_agent = AgentController.get_by_id(db, payload.agent_id)
            if not new_agent.active:
                raise AppException(409, "Assigned agent must be active", "Conflict")
            prop.agent_id = new_agent.id

        if payload.title is not None:
            prop.title = payload.title
        if payload.property_type is not None:
            prop.property_type = payload.property_type.value
        if payload.city is not None:
            prop.city = payload.city
        if payload.area_sqm is not None:
            prop.area_sqm = payload.area_sqm
        if payload.rooms is not None:
            prop.rooms = payload.rooms
        if payload.price_cents is not None:
            prop.price_cents = payload.price_cents
        if payload.status is not None:
            prop.status = payload.status.value

        # REQ-6-004: active property must have active agent
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
        prop = cls.get_by_id(db, property_id)

        if current_user.role != UserRole.ADMIN.value and current_user.agent_id != prop.agent_id:
            raise AppException(403, "Only the assigned agent or an admin can delete this property", "Forbidden")

        # REQ-6-014: soft-delete an ARCHIVED property only when it has no favorites
        if prop.status != PropertyStatusEnum.ARCHIVED.value:
            raise AppException(409, "Only ARCHIVED properties can be deleted", "Conflict")

        has_favs = db.scalar(select(FavoriteModel).where(FavoriteModel.property_id == prop.id).limit(1))
        if has_favs:
            raise AppException(409, "Cannot delete property referenced by favorites", "Conflict")

        prop.is_deleted = True
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(500, "Database error deleting property", "Internal Server Error")

    @classmethod
    def list_similar(
        cls,
        db: Session,
        property_id: int,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> PropertyListResponse:
        subject = cls.get_by_id(db, property_id)

        # REQ-6-028: same-city and same-type active properties excluding subject
        query = (
            select(PropertyModel)
            .where(
                PropertyModel.is_deleted == False,
                PropertyModel.id != subject.id,
                PropertyModel.status == PropertyStatusEnum.ACTIVE.value,
                PropertyModel.property_type == subject.property_type,
                func.lower(PropertyModel.city) == subject.city.lower(),
            )
        )

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


class SavedSearchController:
    @classmethod
    def serialize(cls, s: SavedSearchModel) -> SavedSearchResponse:
        filters_dict = json.loads(s.filters_json)
        return SavedSearchResponse(
            id=s.id,
            user_id=s.user_id,
            name=s.name,
            filters=SearchFilterValues(**filters_dict),
            created_at=s.created_at.isoformat(),
            updated_at=s.updated_at.isoformat(),
        )

    @classmethod
    def get_by_id(cls, db: Session, current_user: UserModel, search_id: int) -> SavedSearchModel:
        if search_id <= 0:
            raise AppException(404, "Saved search not found", "Not Found")
        search = db.scalar(select(SavedSearchModel).where(SavedSearchModel.id == search_id))
        if not search:
            raise AppException(404, "Saved search not found", "Not Found")
        if current_user.role != UserRole.ADMIN.value and search.user_id != current_user.id:
            raise AppException(403, "Access denied to this saved search", "Forbidden")
        return search

    @classmethod
    def list_saved_searches(
        cls,
        db: Session,
        current_user: UserModel,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> SavedSearchListResponse:
        query = select(SavedSearchModel)
        if current_user.role != UserRole.ADMIN.value:
            query = query.where(SavedSearchModel.user_id == current_user.id)
        if after_id is not None:
            query = query.where(SavedSearchModel.id > after_id)

        query = query.order_by(SavedSearchModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return SavedSearchListResponse(
            items=[cls.serialize(r) for r in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def create_saved_search(
        cls,
        db: Session,
        current_user: UserModel,
        payload: SavedSearchCreateRequest,
    ) -> SavedSearchModel:
        # Check name unique per user account (REQ-6-005)
        existing = db.scalar(
            select(SavedSearchModel).where(
                SavedSearchModel.user_id == current_user.id,
                SavedSearchModel.name == payload.name,
            )
        )
        if existing:
            raise AppException(409, "Saved search with this name already exists for account", "Conflict")

        search = SavedSearchModel(
            user_id=current_user.id,
            name=payload.name,
            filters_json=payload.filters.model_dump_json(exclude_none=True),
        )
        db.add(search)
        try:
            db.commit()
            db.refresh(search)
            return search
        except Exception:
            db.rollback()
            raise AppException(500, "Database error creating saved search", "Internal Server Error")

    @classmethod
    def patch_saved_search(
        cls,
        db: Session,
        current_user: UserModel,
        search_id: int,
        payload: SavedSearchPatchRequest,
    ) -> SavedSearchModel:
        search = cls.get_by_id(db, current_user, search_id)

        if payload.name is not None and payload.name != search.name:
            dup = db.scalar(
                select(SavedSearchModel).where(
                    SavedSearchModel.user_id == search.user_id,
                    SavedSearchModel.name == payload.name,
                    SavedSearchModel.id != search.id,
                )
            )
            if dup:
                raise AppException(409, "Saved search with this name already exists for account", "Conflict")
            search.name = payload.name

        if payload.filters is not None:
            search.filters_json = payload.filters.model_dump_json(exclude_none=True)

        try:
            db.commit()
            db.refresh(search)
            return search
        except Exception:
            db.rollback()
            raise AppException(500, "Database error updating saved search", "Internal Server Error")

    @classmethod
    def delete_saved_search(cls, db: Session, current_user: UserModel, search_id: int) -> None:
        search = cls.get_by_id(db, current_user, search_id)
        db.delete(search)
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(500, "Database error deleting saved search", "Internal Server Error")


class FavoriteController:
    @classmethod
    def serialize(cls, f: FavoriteModel) -> FavoriteResponse:
        return FavoriteResponse(
            id=f.id,
            user_id=f.user_id,
            property_id=f.property_id,
            property=PropertyController.serialize(f.property),
            created_at=f.created_at.isoformat(),
        )

    @classmethod
    def list_favorites(
        cls,
        db: Session,
        current_user: UserModel,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> FavoriteListResponse:
        query = select(FavoriteModel).join(PropertyModel, FavoriteModel.property_id == PropertyModel.id).where(PropertyModel.is_deleted == False)
        if current_user.role != UserRole.ADMIN.value:
            query = query.where(FavoriteModel.user_id == current_user.id)
        if after_id is not None:
            query = query.where(FavoriteModel.id > after_id)

        query = query.order_by(FavoriteModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return FavoriteListResponse(
            items=[cls.serialize(r) for r in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def create_favorite(cls, db: Session, current_user: UserModel, property_id: int) -> FavoriteModel:
        prop = PropertyController.get_by_id(db, property_id)

        # REQ-6-006: A favorite may reference only an ACTIVE property
        if prop.status != PropertyStatusEnum.ACTIVE.value:
            raise AppException(409, "Cannot favorite non-active property", "Conflict")

        # Check unique by caller account and property
        existing = db.scalar(
            select(FavoriteModel).where(
                FavoriteModel.user_id == current_user.id,
                FavoriteModel.property_id == prop.id,
            )
        )
        if existing:
            raise AppException(409, "Property already in favorites", "Conflict")

        fav = FavoriteModel(
            user_id=current_user.id,
            property_id=prop.id,
        )
        db.add(fav)
        try:
            db.commit()
            db.refresh(fav)
            return fav
        except Exception:
            db.rollback()
            raise AppException(500, "Database error creating favorite", "Internal Server Error")

    @classmethod
    def delete_favorite(cls, db: Session, current_user: UserModel, property_id: int) -> None:
        if property_id <= 0:
            raise AppException(404, "Favorite not found", "Not Found")

        query = select(FavoriteModel).where(FavoriteModel.property_id == property_id)
        if current_user.role != UserRole.ADMIN.value:
            query = query.where(FavoriteModel.user_id == current_user.id)

        fav = db.scalar(query)
        if not fav:
            raise AppException(404, "Favorite not found", "Not Found")

        db.delete(fav)
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(500, "Database error deleting favorite", "Internal Server Error")


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
        full_name="Alice Agent",
        email="alice.agent@example.com",
        license_code="LIC-001",
        active=True,
        is_deleted=False,
    )
    a2 = AgentModel(
        id=2,
        full_name="Bob Agent",
        email="bob.agent@example.com",
        license_code="LIC-002",
        active=True,
        is_deleted=False,
    )
    a3 = AgentModel(
        id=3,
        full_name="Carol Admin Agent",
        email="carol.admin@example.com",
        license_code="LIC-003",
        active=True,
        is_deleted=False,
    )
    db.add_all([a1, a2, a3])
    db.flush()

    # Users
    u1 = UserModel(
        id=1,
        username="user1",
        role=UserRole.USER.value,
        token_hash=hash_token(user1_token),
        token=user1_token,
        agent_id=1,
    )
    u2 = UserModel(
        id=2,
        username="user2",
        role=UserRole.USER.value,
        token_hash=hash_token(user2_token),
        token=user2_token,
        agent_id=2,
    )
    u3 = UserModel(
        id=3,
        username="admin",
        role=UserRole.ADMIN.value,
        token_hash=hash_token(admin_token),
        token=admin_token,
        agent_id=3,
    )
    db.add_all([u1, u2, u3])
    db.flush()

    # Properties
    p1 = PropertyModel(
        id=1,
        title="Cozy Downtown Milano Apartment",
        property_type=PropertyTypeEnum.APARTMENT.value,
        city="Milano",
        area_sqm=75,
        rooms=3,
        price_cents=45000000,
        agent_id=1,
        status=PropertyStatusEnum.ACTIVE.value,
        is_deleted=False,
    )
    p2 = PropertyModel(
        id=2,
        title="Luxury Penthouse in Milano Center",
        property_type=PropertyTypeEnum.APARTMENT.value,
        city="Milano",
        area_sqm=150,
        rooms=5,
        price_cents=120000000,
        agent_id=1,
        status=PropertyStatusEnum.ACTIVE.value,
        is_deleted=False,
    )
    p3 = PropertyModel(
        id=3,
        title="Modern Suburban House Monza",
        property_type=PropertyTypeEnum.HOUSE.value,
        city="Monza",
        area_sqm=180,
        rooms=6,
        price_cents=65000000,
        agent_id=2,
        status=PropertyStatusEnum.ACTIVE.value,
        is_deleted=False,
    )
    p4 = PropertyModel(
        id=4,
        title="Historic Lake Como Villa",
        property_type=PropertyTypeEnum.VILLA.value,
        city="Como",
        area_sqm=400,
        rooms=10,
        price_cents=250000000,
        agent_id=1,
        status=PropertyStatusEnum.ARCHIVED.value,
        is_deleted=False,
    )
    db.add_all([p1, p2, p3, p4])
    db.flush()

    # Saved Searches
    s1 = SavedSearchModel(
        id=1,
        user_id=1,
        name="Milano Apartments",
        filters_json=json.dumps({"city": "Milano", "property_type": "APARTMENT", "price_max": 100000000}),
    )
    db.add(s1)

    # Favorites
    f1 = FavoriteModel(
        id=1,
        user_id=1,
        property_id=2,
    )
    db.add(f1)
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
    title="Real-Estate Management - Search, Filters & Pagination API",
    version="1.0.0",
    description="Deterministic RESTful API for properties, agents, saved searches, favorites with filter and cursor sorting.",
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
        protected = not path.startswith("/public")
        for method, operation in item.items():
            responses = operation.setdefault("responses", {})
            codes = {"422"}
            if protected:
                codes.update({"401", "403"})
            if "{" in path:
                codes.add("404")
            if method in {"post", "patch", "put"}:
                codes.update({"400", "413", "415", "409"})
            if method == "delete":
                codes.add("409")
            if method == "get" and path == "/properties":
                codes.add("400")
            for code in codes:
                responses[code] = {"description": error_descriptions[code], "content": error_content}
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


# -----------------------------------------------------------------------------
# Endpoints (20 Specified Endpoints for Variant A)
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
    response_model=PropertySearchListResponse,
)
def search_properties(
    city: Optional[str] = Query(None, min_length=2, max_length=80),
    property_type: Optional[PropertyTypeEnum] = Query(None),
    price_min: Optional[int] = Query(None, ge=1000, le=2000000000),
    price_max: Optional[int] = Query(None, ge=1000, le=2000000000),
    area_min: Optional[int] = Query(None, ge=10, le=10000),
    area_max: Optional[int] = Query(None, ge=10, le=10000),
    rooms_min: Optional[int] = Query(None, ge=0, le=50),
    rooms_max: Optional[int] = Query(None, ge=0, le=50),
    agent_id: Optional[int] = Query(None, gt=0),
    status: Optional[PropertyStatusEnum] = Query(PropertyStatusEnum.ACTIVE),
    sort: SortEnum = Query(SortEnum.ID_ASC),
    cursor: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return PropertyController.search_properties(
        db, city, property_type, price_min, price_max, area_min, area_max, rooms_min, rooms_max, agent_id, status, sort, cursor, limit
    )


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
    city: Optional[str] = Query(None),
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


# 12. GET /saved-searches
@app.get(
    "/saved-searches",
    tags=["Saved Searches"],
    response_model=SavedSearchListResponse,
)
def list_saved_searches(
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return SavedSearchController.list_saved_searches(db, current_user, limit, after_id)


# 13. POST /saved-searches
@app.post(
    "/saved-searches",
    tags=["Saved Searches"],
    status_code=status.HTTP_201_CREATED,
    response_model=SavedSearchResponse,
)
def create_saved_search(
    payload: SavedSearchCreateRequest,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    search = SavedSearchController.create_saved_search(db, current_user, payload)
    response.headers["Location"] = f"/saved-searches/{search.id}"
    return SavedSearchController.serialize(search)


# 14. GET /saved-searches/{search_id}
@app.get(
    "/saved-searches/{search_id}",
    tags=["Saved Searches"],
    response_model=SavedSearchResponse,
)
def get_saved_search(
    search_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    search = SavedSearchController.get_by_id(db, current_user, search_id)
    return SavedSearchController.serialize(search)


# 15. PATCH /saved-searches/{search_id}
@app.patch(
    "/saved-searches/{search_id}",
    tags=["Saved Searches"],
    response_model=SavedSearchResponse,
)
def patch_saved_search(
    search_id: int,
    payload: SavedSearchPatchRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    search = SavedSearchController.patch_saved_search(db, current_user, search_id, payload)
    return SavedSearchController.serialize(search)


# 16. DELETE /saved-searches/{search_id}
@app.delete(
    "/saved-searches/{search_id}",
    tags=["Saved Searches"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_saved_search(
    search_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    SavedSearchController.delete_saved_search(db, current_user, search_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 17. GET /favorites
@app.get(
    "/favorites",
    tags=["Favorites"],
    response_model=FavoriteListResponse,
)
def list_favorites(
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return FavoriteController.list_favorites(db, current_user, limit, after_id)


# 18. POST /properties/{property_id}/favorite
@app.post(
    "/properties/{property_id}/favorite",
    tags=["Favorites"],
    status_code=status.HTTP_201_CREATED,
    response_model=FavoriteResponse,
)
def create_favorite(
    property_id: int,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    fav = FavoriteController.create_favorite(db, current_user, property_id)
    response.headers["Location"] = f"/properties/{property_id}/favorite"
    return FavoriteController.serialize(fav)


# 19. DELETE /properties/{property_id}/favorite
@app.delete(
    "/properties/{property_id}/favorite",
    tags=["Favorites"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_favorite(
    property_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    FavoriteController.delete_favorite(db, current_user, property_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 20. GET /properties/{property_id}/similar
@app.get(
    "/properties/{property_id}/similar",
    tags=["Properties"],
    response_model=PropertyListResponse,
)
def list_similar_properties(
    property_id: int,
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return PropertyController.list_similar(db, property_id, limit, after_id)
