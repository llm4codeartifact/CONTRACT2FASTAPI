"""
Real-Estate Management - Variant C: Listing publication, state, and authorization
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
    Date,
    DateTime,
    Enum as SqlEnum,
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
logger = logging.getLogger("immobiliare_variant_c")
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
SLUG_REGEX = re.compile(r"^[a-z0-9-]{8,120}$")
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


class PropertyTypeEnum(str, enum.Enum):
    APARTMENT = "APARTMENT"
    HOUSE = "HOUSE"
    VILLA = "VILLA"
    OFFICE = "OFFICE"
    LAND = "LAND"


class PropertyStatusEnum(str, enum.Enum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ListingStateEnum(str, enum.Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    UNPUBLISHED = "UNPUBLISHED"
    ARCHIVED = "ARCHIVED"
    REJECTED = "REJECTED"


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


class AgentModel(Base):
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
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
    properties = relationship("PropertyModel", back_populates="creator_agent")
    listings = relationship("ListingModel", back_populates="creator_agent")


class PropertyModel(Base):
    __tablename__ = "properties"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(160), nullable=False)
    address = Column(String(200), nullable=False)
    price_cents = Column(Integer, nullable=False)
    property_type = Column(String(20), nullable=False)
    creator_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    status = Column(String(20), nullable=False, default=PropertyStatusEnum.ACTIVE.value)
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    creator_agent = relationship("AgentModel", back_populates="properties", foreign_keys=[creator_agent_id])
    listings = relationship("ListingModel", back_populates="property")


class ListingModel(Base):
    __tablename__ = "listings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=False, index=True)
    creator_agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False, index=True)
    state = Column(String(20), nullable=False, default=ListingStateEnum.DRAFT.value, index=True)
    public_slug = Column(String(120), unique=True, nullable=False, index=True)
    headline = Column(String(160), nullable=True)
    description = Column(String(2000), nullable=True)
    published_at = Column(DateTime(timezone=True), nullable=True)
    unpublished_at = Column(DateTime(timezone=True), nullable=True)
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    property = relationship("PropertyModel", back_populates="listings", foreign_keys=[property_id])
    creator_agent = relationship("AgentModel", back_populates="listings", foreign_keys=[creator_agent_id])
    histories = relationship("ListingHistoryModel", back_populates="listing", cascade="all, delete-orphan")


class ListingHistoryModel(Base):
    __tablename__ = "listing_histories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    listing_id = Column(Integer, ForeignKey("listings.id"), nullable=False, index=True)
    from_state = Column(String(20), nullable=False)
    to_state = Column(String(20), nullable=False)
    reason = Column(String(500), nullable=True)
    transition_time = Column(DateTime(timezone=True), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    listing = relationship("ListingModel", back_populates="histories")
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
    email: str = Field(..., min_length=5, max_length=254)
    license_code: str = Field(..., min_length=4, max_length=20)
    active: bool = True

    @field_validator("name", "email", "license_code")
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
    name: Optional[str] = Field(None, min_length=3, max_length=120)
    email: Optional[str] = Field(None, min_length=5, max_length=254)
    license_code: Optional[str] = Field(None, min_length=4, max_length=20)
    active: Optional[bool] = None

    @field_validator("name", "email", "license_code")
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
        for f in ("name", "email", "license_code", "active"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class AgentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    name: str
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
    address: str = Field(..., min_length=5, max_length=200)
    price_cents: int = Field(..., ge=1000, le=2000000000)
    property_type: PropertyTypeEnum
    agent_id: Optional[int] = Field(None, gt=0)
    status: Optional[PropertyStatusEnum] = PropertyStatusEnum.ACTIVE

    @field_validator("title", "address")
    @classmethod
    def strip_strings(cls, v: str) -> str:
        s = v.strip()
        if len(s) < 5:
            raise ValueError("Field length must be at least 5 characters after trimming")
        return s


class PropertyPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = Field(None, min_length=5, max_length=160)
    address: Optional[str] = Field(None, min_length=5, max_length=200)
    price_cents: Optional[int] = Field(None, ge=1000, le=2000000000)
    property_type: Optional[PropertyTypeEnum] = None
    agent_id: Optional[int] = Field(None, gt=0)
    status: Optional[PropertyStatusEnum] = None

    @field_validator("title", "address")
    @classmethod
    def strip_strings(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if len(s) < 5:
            raise ValueError("Field length must be at least 5 characters after trimming")
        return s

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("title", "address", "price_cents", "property_type", "agent_id", "status"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class PropertyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    title: str
    address: str
    price_cents: int
    property_type: PropertyTypeEnum
    creator_agent_id: int
    status: PropertyStatusEnum
    created_at: str
    updated_at: str


class PropertyListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[PropertyResponse]
    limit: int
    next_after_id: Optional[int]


# Listing Schemas
class ListingCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    property_id: int = Field(..., gt=0)
    public_slug: str = Field(..., min_length=8, max_length=120)
    headline: Optional[str] = Field(None, min_length=5, max_length=160)
    description: Optional[str] = Field(None, min_length=5, max_length=2000)

    @field_validator("public_slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        s = v.strip().lower()
        if not SLUG_REGEX.match(s):
            raise ValueError("public_slug must match ^[a-z0-9-]{8,120}$")
        return s

    @field_validator("headline", "description")
    @classmethod
    def strip_optional_strings(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if len(s) < 5:
            raise ValueError("Editorial field length must be at least 5 characters after trimming")
        return s


class ListingPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    property_id: Optional[int] = Field(None, gt=0)
    public_slug: Optional[str] = Field(None, min_length=8, max_length=120)
    headline: Optional[str] = Field(None, min_length=5, max_length=160)
    description: Optional[str] = Field(None, min_length=5, max_length=2000)
    state: Optional[ListingStateEnum] = None

    @field_validator("public_slug")
    @classmethod
    def validate_slug(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip().lower()
        if not SLUG_REGEX.match(s):
            raise ValueError("public_slug must match ^[a-z0-9-]{8,120}$")
        return s

    @field_validator("headline", "description")
    @classmethod
    def strip_optional_strings(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if len(s) < 5:
            raise ValueError("Editorial field length must be at least 5 characters after trimming")
        return s

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("property_id", "public_slug", "headline", "description", "state"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class ListingPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    published_at: str = Field(..., description="UTC ISO-8601 timestamp in 2025-2035")

    @field_validator("published_at")
    @classmethod
    def validate_timestamp(cls, v: str) -> str:
        return parse_utc_timestamp(v).isoformat()


class ListingUnpublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    unpublished_at: str = Field(..., description="UTC ISO-8601 timestamp in 2025-2035")
    reason: str = Field(..., min_length=3, max_length=500)

    @field_validator("unpublished_at")
    @classmethod
    def validate_timestamp(cls, v: str) -> str:
        return parse_utc_timestamp(v).isoformat()

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        s = v.strip()
        if len(s) < 3:
            raise ValueError("Reason must be between 3 and 500 characters")
        return s


class ListingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    property_id: int
    creator_agent_id: int
    state: ListingStateEnum
    public_slug: str
    headline: Optional[str]
    description: Optional[str]
    published_at: Optional[str]
    unpublished_at: Optional[str]
    created_at: str
    updated_at: str


class ListingListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[ListingResponse]
    limit: int
    next_after_id: Optional[int]


class PublicListingPropertyProjection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    title: str
    address: str
    price_cents: int
    property_type: PropertyTypeEnum


class PublicListingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    public_slug: str
    headline: Optional[str]
    description: Optional[str]
    published_at: str
    agent_name: str
    property: PublicListingPropertyProjection


class PublicListingListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[PublicListingResponse]
    limit: int
    next_after_id: Optional[int]


class ListingHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    listing_id: int
    from_state: str
    to_state: str
    reason: Optional[str]
    transition_time: str
    user_id: int
    created_at: str


class ListingHistoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[ListingHistoryResponse]
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
    def create_agent(cls, db: Session, payload: AgentCreateRequest) -> AgentModel:
        # Check uniqueness
        if db.scalar(select(AgentModel).where(AgentModel.email == payload.email, AgentModel.is_deleted == False)):
            raise AppException(409, "Agent with this email already exists", "Conflict")
        if db.scalar(select(AgentModel).where(AgentModel.license_code == payload.license_code, AgentModel.is_deleted == False)):
            raise AppException(409, "Agent with this license code already exists", "Conflict")

        agent = AgentModel(
            name=payload.name,
            email=payload.email,
            license_code=payload.license_code,
            active=payload.active,
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
    def patch_agent(cls, db: Session, agent_id: int, payload: AgentPatchRequest) -> AgentModel:
        agent = cls.get_by_id(db, agent_id)

        if payload.email is not None and payload.email != agent.email:
            if db.scalar(select(AgentModel).where(AgentModel.email == payload.email, AgentModel.id != agent.id, AgentModel.is_deleted == False)):
                raise AppException(409, "Agent with this email already exists", "Conflict")
            agent.email = payload.email

        if payload.license_code is not None and payload.license_code != agent.license_code:
            if db.scalar(select(AgentModel).where(AgentModel.license_code == payload.license_code, AgentModel.id != agent.id, AgentModel.is_deleted == False)):
                raise AppException(409, "Agent with this license code already exists", "Conflict")
            agent.license_code = payload.license_code

        if payload.name is not None:
            agent.name = payload.name

        if payload.active is not None:
            agent.active = payload.active

        try:
            db.commit()
            db.refresh(agent)
            return agent
        except Exception:
            db.rollback()
            raise AppException(500, "Database error updating agent", "Internal Server Error")

    @classmethod
    def delete_agent(cls, db: Session, agent_id: int) -> None:
        agent = cls.get_by_id(db, agent_id)

        # Check references: cannot soft delete if referenced by properties or listings
        has_prop = db.scalar(select(PropertyModel).where(PropertyModel.creator_agent_id == agent.id, PropertyModel.is_deleted == False).limit(1))
        if has_prop:
            raise AppException(409, "Cannot delete agent referenced by existing properties", "Conflict")
        has_listing = db.scalar(select(ListingModel).where(ListingModel.creator_agent_id == agent.id, ListingModel.is_deleted == False).limit(1))
        if has_listing:
            raise AppException(409, "Cannot delete agent referenced by existing listings", "Conflict")

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
            address=p.address,
            price_cents=p.price_cents,
            property_type=PropertyTypeEnum(p.property_type),
            creator_agent_id=p.creator_agent_id,
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
    def list_properties(
        cls,
        db: Session,
        property_type: Optional[PropertyTypeEnum] = None,
        status: Optional[PropertyStatusEnum] = None,
        agent_id: Optional[int] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> PropertyListResponse:
        query = select(PropertyModel).where(PropertyModel.is_deleted == False)
        if property_type is not None:
            query = query.where(PropertyModel.property_type == property_type.value)
        if status is not None:
            query = query.where(PropertyModel.status == status.value)
        if agent_id is not None:
            query = query.where(PropertyModel.creator_agent_id == agent_id)
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
        # Determine creator agent
        if current_user.role == UserRole.ADMIN.value:
            if payload.agent_id is not None:
                agent = AgentController.get_by_id(db, payload.agent_id)
            else:
                agent = current_user.agent or AgentController.get_by_id(db, 1)
        else:
            if not current_user.agent_id:
                raise AppException(403, "Caller has no associated agent profile", "Forbidden")
            if payload.agent_id is not None and payload.agent_id != current_user.agent_id:
                raise AppException(403, "Cannot create property for another agent", "Forbidden")
            agent = AgentController.get_by_id(db, current_user.agent_id)

        if not agent.active:
            raise AppException(409, "Assigned agent must be active", "Conflict")

        prop = PropertyModel(
            title=payload.title,
            address=payload.address,
            price_cents=payload.price_cents,
            property_type=payload.property_type.value,
            creator_agent_id=agent.id,
            status=payload.status.value if payload.status else PropertyStatusEnum.ACTIVE.value,
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

        # Check ownership: creator agent or admin
        if current_user.role != UserRole.ADMIN.value and current_user.agent_id != prop.creator_agent_id:
            raise AppException(403, "Only the creator agent or an admin can edit this property", "Forbidden")

        if payload.agent_id is not None and payload.agent_id != prop.creator_agent_id:
            if current_user.role != UserRole.ADMIN.value:
                raise AppException(403, "Only admins can reassign properties to other agents", "Forbidden")
            new_agent = AgentController.get_by_id(db, payload.agent_id)
            if not new_agent.active:
                raise AppException(409, "Assigned agent must be active", "Conflict")
            prop.creator_agent_id = new_agent.id

        if payload.title is not None:
            prop.title = payload.title
        if payload.address is not None:
            prop.address = payload.address
        if payload.price_cents is not None:
            prop.price_cents = payload.price_cents
        if payload.property_type is not None:
            prop.property_type = payload.property_type.value
        if payload.status is not None:
            prop.status = payload.status.value

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

        if current_user.role != UserRole.ADMIN.value and current_user.agent_id != prop.creator_agent_id:
            raise AppException(403, "Only the creator agent or an admin can delete this property", "Forbidden")

        # REQ-6-014: soft-delete only an unlisted ARCHIVED property
        if prop.status != PropertyStatusEnum.ARCHIVED.value:
            raise AppException(409, "Only ARCHIVED properties can be deleted", "Conflict")

        # Check if unlisted: no listings or all listings are ARCHIVED or deleted
        active_listing = db.scalar(
            select(ListingModel).where(
                ListingModel.property_id == prop.id,
                ListingModel.is_deleted == False,
                ListingModel.state != ListingStateEnum.ARCHIVED.value,
            ).limit(1)
        )
        if active_listing:
            raise AppException(409, "Cannot delete property with active or non-archived listings", "Conflict")

        prop.is_deleted = True
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(500, "Database error deleting property", "Internal Server Error")


class ListingController:
    @classmethod
    def serialize(cls, l: ListingModel) -> ListingResponse:
        return ListingResponse(
            id=l.id,
            property_id=l.property_id,
            creator_agent_id=l.creator_agent_id,
            state=ListingStateEnum(l.state),
            public_slug=l.public_slug,
            headline=l.headline,
            description=l.description,
            published_at=l.published_at.isoformat() if l.published_at else None,
            unpublished_at=l.unpublished_at.isoformat() if l.unpublished_at else None,
            created_at=l.created_at.isoformat(),
            updated_at=l.updated_at.isoformat(),
        )

    @classmethod
    def get_by_id(cls, db: Session, listing_id: int) -> ListingModel:
        if listing_id <= 0:
            raise AppException(404, "Listing not found", "Not Found")
        listing = db.scalar(select(ListingModel).where(ListingModel.id == listing_id, ListingModel.is_deleted == False))
        if not listing:
            raise AppException(404, "Listing not found", "Not Found")
        return listing

    @classmethod
    def list_listings(
        cls,
        db: Session,
        property_id: Optional[int] = None,
        state: Optional[ListingStateEnum] = None,
        creator_agent_id: Optional[int] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> ListingListResponse:
        query = select(ListingModel).where(ListingModel.is_deleted == False)
        if property_id is not None:
            query = query.where(ListingModel.property_id == property_id)
        if state is not None:
            query = query.where(ListingModel.state == state.value)
        if creator_agent_id is not None:
            query = query.where(ListingModel.creator_agent_id == creator_agent_id)
        if after_id is not None:
            query = query.where(ListingModel.id > after_id)

        query = query.order_by(ListingModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return ListingListResponse(
            items=[cls.serialize(r) for r in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def create_listing(cls, db: Session, current_user: UserModel, payload: ListingCreateRequest) -> ListingModel:
        prop = PropertyController.get_by_id(db, payload.property_id)

        # Only creator agent of property or admin can create listing
        if current_user.role != UserRole.ADMIN.value and current_user.agent_id != prop.creator_agent_id:
            raise AppException(403, "Only the property creator agent or an admin can create listings for this property", "Forbidden")

        # Check slug uniqueness
        if db.scalar(select(ListingModel).where(ListingModel.public_slug == payload.public_slug, ListingModel.is_deleted == False)):
            raise AppException(409, "Listing with this public_slug already exists", "Conflict")

        creator_agent_id = prop.creator_agent_id
        listing = ListingModel(
            property_id=prop.id,
            creator_agent_id=creator_agent_id,
            state=ListingStateEnum.DRAFT.value,
            public_slug=payload.public_slug,
            headline=payload.headline,
            description=payload.description,
            published_at=None,
            unpublished_at=None,
            is_deleted=False,
        )
        db.add(listing)
        try:
            db.commit()
            db.refresh(listing)
            return listing
        except Exception:
            db.rollback()
            raise AppException(500, "Database error creating listing", "Internal Server Error")

    @classmethod
    def patch_listing(cls, db: Session, current_user: UserModel, listing_id: int, payload: ListingPatchRequest) -> ListingModel:
        listing = cls.get_by_id(db, listing_id)

        # REQ-6-005: Only creator agent or admin edits listing
        if current_user.role != UserRole.ADMIN.value and current_user.agent_id != listing.creator_agent_id:
            raise AppException(403, "Only the creator agent or an admin can edit this listing", "Forbidden")

        if payload.property_id is not None and payload.property_id != listing.property_id:
            prop = PropertyController.get_by_id(db, payload.property_id)
            if current_user.role != UserRole.ADMIN.value and current_user.agent_id != prop.creator_agent_id:
                raise AppException(403, "Cannot reassign listing to property owned by another agent", "Forbidden")
            listing.property_id = prop.id

        if payload.public_slug is not None and payload.public_slug != listing.public_slug:
            if db.scalar(select(ListingModel).where(ListingModel.public_slug == payload.public_slug, ListingModel.id != listing.id, ListingModel.is_deleted == False)):
                raise AppException(409, "Listing with this public_slug already exists", "Conflict")
            listing.public_slug = payload.public_slug

        if payload.headline is not None:
            listing.headline = payload.headline
        if payload.description is not None:
            listing.description = payload.description

        # State transitions if provided in PATCH
        if payload.state is not None and payload.state.value != listing.state:
            target_state = payload.state.value
            current_state = listing.state

            # Only admin can reject
            if target_state == ListingStateEnum.REJECTED.value and current_user.role != UserRole.ADMIN.value:
                raise AppException(403, "Only admins can reject listings", "Forbidden")

            # Validate state transitions (REQ-6-006)
            allowed_transitions = {
                ListingStateEnum.DRAFT.value: [
                    ListingStateEnum.PUBLISHED.value,
                    ListingStateEnum.REJECTED.value,
                    ListingStateEnum.ARCHIVED.value,
                ],
                ListingStateEnum.PUBLISHED.value: [
                    ListingStateEnum.UNPUBLISHED.value,
                    ListingStateEnum.ARCHIVED.value,
                ],
                ListingStateEnum.UNPUBLISHED.value: [
                    ListingStateEnum.PUBLISHED.value,
                    ListingStateEnum.ARCHIVED.value,
                ],
            }
            if target_state not in allowed_transitions.get(current_state, []):
                raise AppException(409, f"Invalid state transition from {current_state} to {target_state}", "Conflict")

            history = ListingHistoryModel(
                listing_id=listing.id,
                from_state=current_state,
                to_state=target_state,
                reason="State changed via PATCH",
                transition_time=datetime.now(timezone.utc),
                user_id=current_user.id,
            )
            listing.state = target_state
            db.add(history)

        try:
            db.commit()
            db.refresh(listing)
            return listing
        except Exception:
            db.rollback()
            raise AppException(500, "Database error updating listing", "Internal Server Error")

    @classmethod
    def delete_listing(cls, db: Session, current_user: UserModel, listing_id: int) -> None:
        listing = cls.get_by_id(db, listing_id)

        if current_user.role != UserRole.ADMIN.value and current_user.agent_id != listing.creator_agent_id:
            raise AppException(403, "Only the creator agent or an admin can delete this listing", "Forbidden")

        # REQ-6-024: delete only a DRAFT listing without deleting property
        if listing.state != ListingStateEnum.DRAFT.value:
            raise AppException(409, "Only listings in DRAFT state can be deleted", "Conflict")

        listing.is_deleted = True
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(500, "Database error deleting listing", "Internal Server Error")

    @classmethod
    def publish_listing(
        cls,
        db: Session,
        current_user: UserModel,
        listing_id: int,
        payload: ListingPublishRequest,
    ) -> ListingModel:
        listing = cls.get_by_id(db, listing_id)

        # Authorization: creator agent or admin
        if current_user.role != UserRole.ADMIN.value and current_user.agent_id != listing.creator_agent_id:
            raise AppException(403, "Only the creator agent or an admin can publish this listing", "Forbidden")

        pub_dt = parse_utc_timestamp(payload.published_at)

        # Idempotence: if already PUBLISHED and published_at matches, return 200
        if listing.state == ListingStateEnum.PUBLISHED.value:
            current_pub = listing.published_at
            if current_pub and current_pub.tzinfo is None:
                current_pub = current_pub.replace(tzinfo=timezone.utc)
            if current_pub == pub_dt:
                return listing

        # Validate transition: DRAFT to PUBLISHED or UNPUBLISHED to PUBLISHED
        if listing.state not in (ListingStateEnum.DRAFT.value, ListingStateEnum.UNPUBLISHED.value):
            raise AppException(409, f"Cannot publish listing in state {listing.state}", "Conflict")

        # REQ-6-007: Publishing requires complete property
        prop = PropertyController.get_by_id(db, listing.property_id)
        if prop.status != PropertyStatusEnum.ACTIVE.value:
            raise AppException(409, "Cannot publish listing for non-active property", "Conflict")
        if not (prop.title and prop.address and prop.price_cents > 0 and prop.property_type):
            raise AppException(409, "Property details incomplete for publication", "Conflict")

        # Creator agent must be active
        agent = AgentController.get_by_id(db, listing.creator_agent_id)
        if not agent.active:
            raise AppException(409, "Creator agent must be active to publish", "Conflict")

        # Slug uniqueness
        if not listing.public_slug or not SLUG_REGEX.match(listing.public_slug):
            raise AppException(422, "Valid public_slug required before publication", "Unprocessable Entity")

        from_state = listing.state
        listing.state = ListingStateEnum.PUBLISHED.value
        listing.published_at = pub_dt

        history = ListingHistoryModel(
            listing_id=listing.id,
            from_state=from_state,
            to_state=ListingStateEnum.PUBLISHED.value,
            reason="Listing published",
            transition_time=pub_dt,
            user_id=current_user.id,
        )
        db.add(history)

        try:
            db.commit()
            db.refresh(listing)
            return listing
        except Exception:
            db.rollback()
            raise AppException(500, "Database error publishing listing", "Internal Server Error")

    @classmethod
    def unpublish_listing(
        cls,
        db: Session,
        current_user: UserModel,
        listing_id: int,
        payload: ListingUnpublishRequest,
    ) -> ListingModel:
        listing = cls.get_by_id(db, listing_id)

        # Authorization: creator agent or admin
        if current_user.role != UserRole.ADMIN.value and current_user.agent_id != listing.creator_agent_id:
            raise AppException(403, "Only the creator agent or an admin can unpublish this listing", "Forbidden")

        unpub_dt = parse_utc_timestamp(payload.unpublished_at)

        # Idempotence: if already UNPUBLISHED and unpublished_at matches, return 200
        if listing.state == ListingStateEnum.UNPUBLISHED.value:
            current_unpub = listing.unpublished_at
            if current_unpub and current_unpub.tzinfo is None:
                current_unpub = current_unpub.replace(tzinfo=timezone.utc)
            if current_unpub == unpub_dt:
                return listing

        # REQ-6-026: validate PUBLISHED transition
        if listing.state != ListingStateEnum.PUBLISHED.value:
            raise AppException(409, f"Cannot unpublish listing in state {listing.state}", "Conflict")

        from_state = listing.state
        listing.state = ListingStateEnum.UNPUBLISHED.value
        listing.unpublished_at = unpub_dt

        history = ListingHistoryModel(
            listing_id=listing.id,
            from_state=from_state,
            to_state=ListingStateEnum.UNPUBLISHED.value,
            reason=payload.reason,
            transition_time=unpub_dt,
            user_id=current_user.id,
        )
        db.add(history)

        try:
            db.commit()
            db.refresh(listing)
            return listing
        except Exception:
            db.rollback()
            raise AppException(500, "Database error unpublishing listing", "Internal Server Error")

    @classmethod
    def list_public(
        cls,
        db: Session,
        property_type: Optional[PropertyTypeEnum] = None,
        price_min: Optional[int] = None,
        price_max: Optional[int] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> PublicListingListResponse:
        query = (
            select(ListingModel)
            .join(PropertyModel, ListingModel.property_id == PropertyModel.id)
            .join(AgentModel, ListingModel.creator_agent_id == AgentModel.id)
            .where(
                ListingModel.is_deleted == False,
                ListingModel.state == ListingStateEnum.PUBLISHED.value,
                PropertyModel.is_deleted == False,
            )
        )

        if property_type is not None:
            query = query.where(PropertyModel.property_type == property_type.value)
        if price_min is not None:
            query = query.where(PropertyModel.price_cents >= price_min)
        if price_max is not None:
            query = query.where(PropertyModel.price_cents <= price_max)
        if after_id is not None:
            query = query.where(ListingModel.id > after_id)

        query = query.order_by(ListingModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        projections = []
        for l in items:
            projections.append(
                PublicListingResponse(
                    id=l.id,
                    public_slug=l.public_slug,
                    headline=l.headline,
                    description=l.description,
                    published_at=l.published_at.isoformat() if l.published_at else "",
                    agent_name=l.creator_agent.name,
                    property=PublicListingPropertyProjection(
                        id=l.property.id,
                        title=l.property.title,
                        address=l.property.address,
                        price_cents=l.property.price_cents,
                        property_type=PropertyTypeEnum(l.property.property_type),
                    ),
                )
            )

        return PublicListingListResponse(
            items=projections,
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def list_history(
        cls,
        db: Session,
        current_user: UserModel,
        listing_id: int,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> ListingHistoryListResponse:
        listing = cls.get_by_id(db, listing_id)

        if current_user.role != UserRole.ADMIN.value and current_user.agent_id != listing.creator_agent_id:
            raise AppException(403, "Only the creator agent or an admin can view listing history", "Forbidden")

        query = select(ListingHistoryModel).where(ListingHistoryModel.listing_id == listing.id)
        if after_id is not None:
            query = query.where(ListingHistoryModel.id > after_id)

        query = query.order_by(ListingHistoryModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return ListingHistoryListResponse(
            items=[
                ListingHistoryResponse(
                    id=h.id,
                    listing_id=h.listing_id,
                    from_state=h.from_state,
                    to_state=h.to_state,
                    reason=h.reason,
                    transition_time=h.transition_time.isoformat(),
                    user_id=h.user_id,
                    created_at=h.created_at.isoformat(),
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

    # Agents
    a1 = AgentModel(
        id=1,
        name="Alice Agent",
        email="alice.agent@example.com",
        license_code="LIC-001",
        active=True,
        is_deleted=False,
    )
    a2 = AgentModel(
        id=2,
        name="Bob Agent",
        email="bob.agent@example.com",
        license_code="LIC-002",
        active=True,
        is_deleted=False,
    )
    a3 = AgentModel(
        id=3,
        name="Carol Admin Agent",
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
        address="Via Monte Napoleone 12, Milano",
        price_cents=45000000,
        property_type=PropertyTypeEnum.APARTMENT.value,
        creator_agent_id=1,
        status=PropertyStatusEnum.ACTIVE.value,
        is_deleted=False,
    )
    p2 = PropertyModel(
        id=2,
        title="Modern Suburban House in Monza",
        address="Via Brianza 8, Monza",
        price_cents=65000000,
        property_type=PropertyTypeEnum.HOUSE.value,
        creator_agent_id=2,
        status=PropertyStatusEnum.ACTIVE.value,
        is_deleted=False,
    )
    p3 = PropertyModel(
        id=3,
        title="Exclusive Lake Como Villa",
        address="Via Bellagio 3, Como",
        price_cents=190000000,
        property_type=PropertyTypeEnum.VILLA.value,
        creator_agent_id=1,
        status=PropertyStatusEnum.ARCHIVED.value,
        is_deleted=False,
    )
    db.add_all([p1, p2, p3])
    db.flush()

    # Listings
    dt_now = datetime(2025, 3, 1, 10, 0, 0, tzinfo=timezone.utc)
    l1 = ListingModel(
        id=1,
        property_id=1,
        creator_agent_id=1,
        state=ListingStateEnum.PUBLISHED.value,
        public_slug="cozy-downtown-milano-apartment",
        headline="Exclusive Apartment in the Fashion District",
        description="Luxury apartment fully renovated in 2025.",
        published_at=dt_now,
        unpublished_at=None,
        is_deleted=False,
    )
    l2 = ListingModel(
        id=2,
        property_id=2,
        creator_agent_id=2,
        state=ListingStateEnum.DRAFT.value,
        public_slug="modern-suburban-house-monza",
        headline="Spacious Family House with Garden",
        description="Ideal for families seeking calm and green spaces.",
        published_at=None,
        unpublished_at=None,
        is_deleted=False,
    )
    db.add_all([l1, l2])
    db.flush()

    h1 = ListingHistoryModel(
        id=1,
        listing_id=1,
        from_state=ListingStateEnum.DRAFT.value,
        to_state=ListingStateEnum.PUBLISHED.value,
        reason="Initial official publication",
        transition_time=dt_now,
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
    title="Real-Estate Management - Listing Publication, State & Authorization API",
    version="1.0.0",
    description="Deterministic RESTful API for properties, agents, listing lifecycle, state transitions and public projections.",
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
            for code in codes:
                responses[code] = {"description": error_descriptions[code], "content": error_content}
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


# -----------------------------------------------------------------------------
# Endpoints (20 Specified Endpoints for Variant C)
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
    property_type: Optional[PropertyTypeEnum] = Query(None),
    status: Optional[PropertyStatusEnum] = Query(None),
    agent_id: Optional[int] = Query(None, gt=0),
    limit: int = Query(20, ge=1, le=100),
    after_id: int = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return PropertyController.list_properties(db, property_type, status, agent_id, limit, after_id)


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
    agent = AgentController.create_agent(db, payload)
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
    if current_user.role != UserRole.ADMIN.value and current_user.agent_id != agent_id:
        raise AppException(403, "Cannot edit another agent's profile", "Forbidden")
    agent = AgentController.patch_agent(db, agent_id, payload)
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
    if current_user.role != UserRole.ADMIN.value and current_user.agent_id != agent_id:
        raise AppException(403, "Cannot delete another agent", "Forbidden")
    AgentController.delete_agent(db, agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 12. GET /listings
@app.get(
    "/listings",
    tags=["Listings"],
    response_model=ListingListResponse,
)
def list_listings(
    property_id: Optional[int] = Query(None, gt=0),
    state: Optional[ListingStateEnum] = Query(None),
    creator_agent_id: Optional[int] = Query(None, gt=0),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return ListingController.list_listings(db, property_id, state, creator_agent_id, limit, after_id)


# 13. POST /listings
@app.post(
    "/listings",
    tags=["Listings"],
    status_code=status.HTTP_201_CREATED,
    response_model=ListingResponse,
)
def create_listing(
    payload: ListingCreateRequest,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    listing = ListingController.create_listing(db, current_user, payload)
    response.headers["Location"] = f"/listings/{listing.id}"
    return ListingController.serialize(listing)


# 14. GET /listings/{listing_id}
@app.get(
    "/listings/{listing_id}",
    tags=["Listings"],
    response_model=ListingResponse,
)
def get_listing(
    listing_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    listing = ListingController.get_by_id(db, listing_id)
    return ListingController.serialize(listing)


# 15. PATCH /listings/{listing_id}
@app.patch(
    "/listings/{listing_id}",
    tags=["Listings"],
    response_model=ListingResponse,
)
def patch_listing(
    listing_id: int,
    payload: ListingPatchRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    listing = ListingController.patch_listing(db, current_user, listing_id, payload)
    return ListingController.serialize(listing)


# 16. DELETE /listings/{listing_id}
@app.delete(
    "/listings/{listing_id}",
    tags=["Listings"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_listing(
    listing_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    ListingController.delete_listing(db, current_user, listing_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 17. POST /listings/{listing_id}/publish
@app.post(
    "/listings/{listing_id}/publish",
    tags=["Listings"],
    response_model=ListingResponse,
)
def publish_listing(
    listing_id: int,
    payload: ListingPublishRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    listing = ListingController.publish_listing(db, current_user, listing_id, payload)
    return ListingController.serialize(listing)


# 18. POST /listings/{listing_id}/unpublish
@app.post(
    "/listings/{listing_id}/unpublish",
    tags=["Listings"],
    response_model=ListingResponse,
)
def unpublish_listing(
    listing_id: int,
    payload: ListingUnpublishRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    listing = ListingController.unpublish_listing(db, current_user, listing_id, payload)
    return ListingController.serialize(listing)


# 19. GET /public/listings (PUBLIC ROUTE - NO AUTH)
@app.get(
    "/public/listings",
    tags=["Public"],
    response_model=PublicListingListResponse,
)
def list_public_listings(
    property_type: PropertyTypeEnum = Query(None),
    price_min: int = Query(None, ge=1000, le=2000000000),
    price_max: int = Query(None, ge=1000, le=2000000000),
    limit: int = Query(20, ge=1, le=100),
    after_id: int = Query(None, gt=0, le=9223372036854775807),
    request: Request = None,
    db: Session = Depends(get_db),
):
    if set(request.query_params) - {"property_type", "price_min", "price_max", "limit", "after_id"}:
        raise AppException(422, "Unknown query parameter", "Unprocessable Entity")
    if price_min is not None and price_max is not None and price_min > price_max:
        raise AppException(422, "price_min must be less than or equal to price_max", "Unprocessable Entity")
    return ListingController.list_public(db, property_type, price_min, price_max, limit, after_id)


# 20. GET /listings/{listing_id}/history
@app.get(
    "/listings/{listing_id}/history",
    tags=["Listings"],
    response_model=ListingHistoryListResponse,
)
def get_listing_history(
    listing_id: int,
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return ListingController.list_history(db, current_user, listing_id, limit, after_id)
