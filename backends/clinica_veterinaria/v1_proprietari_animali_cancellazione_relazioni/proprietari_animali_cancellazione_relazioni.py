"""
Veterinary Clinic - Variant A: Owners, animals, and relationship deletion
CASE_ID: 3

Compliant with:
- Python 3.10+, FastAPI, Pydantic v2, SQLAlchemy ORM, SQLite in-memory with StaticPool.
- MVC architecture: SQLAlchemy models, Pydantic schemas, static controllers, thin FastAPI routes.
- Fully autonomous module exporting `app`.
- Rigorous permission checks, atomic rollback transactions, standardized error responses.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Setup standard logging
logger = logging.getLogger("vet_owners")
logger.setLevel(logging.INFO)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    ch.setFormatter(formatter)
    logger.addHandler(ch)

# Database Engine & Session
DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MiB

MIN_DATE = date(1900, 1, 1)
MAX_DATE = date(2100, 12, 31)


# ============================================================================
# Enums
# ============================================================================
class RoleEnum(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


class SpeciesEnum(str, enum.Enum):
    DOG = "DOG"
    CAT = "CAT"
    RABBIT = "RABBIT"
    BIRD = "BIRD"
    OTHER = "OTHER"


class SexEnum(str, enum.Enum):
    MALE = "MALE"
    FEMALE = "FEMALE"
    UNKNOWN = "UNKNOWN"


class OwnershipStatusEnum(str, enum.Enum):
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"


# ============================================================================
# SQLAlchemy Models
# ============================================================================
class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    role = Column(String(20), nullable=False)
    token = Column(String(100), unique=True, nullable=False)


class OwnerModel(Base):
    __tablename__ = "owners"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    full_name = Column(String(120), nullable=False)
    email = Column(String(254), nullable=False, index=True)
    phone = Column(String(25), nullable=True)
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class AnimalModel(Base):
    __tablename__ = "animals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(80), nullable=False)
    species = Column(String(20), nullable=False)
    sex = Column(String(20), nullable=False)
    birth_date = Column(Date, nullable=True)
    microchip = Column(String(15), nullable=True, index=True)
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class OwnershipModel(Base):
    __tablename__ = "ownerships"

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_id = Column(Integer, ForeignKey("owners.id"), nullable=False, index=True)
    animal_id = Column(Integer, ForeignKey("animals.id"), nullable=False, index=True)
    started_on = Column(Date, nullable=False, index=True)
    status = Column(
        String(20),
        nullable=False,
        default=OwnershipStatusEnum.ACTIVE.value,
        index=True,
    )
    ended_on = Column(Date, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


# ============================================================================
# Pydantic Schemas
# ============================================================================
class AppException(HTTPException):
    def __init__(self, status_code: int, detail: str, error_type: str = "Error"):
        super().__init__(status_code=status_code, detail=detail)
        self.error_type = error_type


def parse_and_validate_date(v: Any) -> date:
    if isinstance(v, str):
        try:
            d = date.fromisoformat(v)
        except Exception:
            raise ValueError("Invalid ISO-8601 date (expected YYYY-MM-DD)")
    elif isinstance(v, date):
        d = v
    else:
        raise ValueError("Date required")

    if not (MIN_DATE <= d <= MAX_DATE):
        raise ValueError(f"Date must be between {MIN_DATE.isoformat()} and {MAX_DATE.isoformat()}")
    return d


class OwnerCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(..., description="Owner full name")
    email: str = Field(..., description="Owner email")
    phone: Optional[str] = Field(default=None, description="Optional phone number")
    account_id: Optional[int] = Field(
        default=None, gt=0, description="Mapped account ID (admin only)"
    )

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, v: str) -> str:
        v = v.strip()
        if not (3 <= len(v) <= 120):
            raise ValueError("full_name must be between 3 and 120 characters")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not (5 <= len(v) <= 254) or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("Invalid email format")
        return v

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (7 <= len(v) <= 25) or not re.match(r"^[0-9 +()-]{7,25}$", v):
            raise ValueError(
                "phone must be 7..25 characters containing only digits, spaces, plus signs, hyphens, and parentheses"
            )
        return v


class OwnerPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (3 <= len(v) <= 120):
            raise ValueError("full_name must be between 3 and 120 characters")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().lower()
        if not (5 <= len(v) <= 254) or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("Invalid email format")
        return v

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (7 <= len(v) <= 25) or not re.match(r"^[0-9 +()-]{7,25}$", v):
            raise ValueError(
                "phone must be 7..25 characters containing only digits, spaces, plus signs, hyphens, and parentheses"
            )
        return v


class OwnerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    account_id: int
    full_name: str
    email: str
    phone: Optional[str] = None
    is_deleted: bool
    created_at: datetime
    updated_at: datetime


class OwnerListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[OwnerResponse]
    limit: int
    next_after_id: Optional[int] = None


class AnimalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Animal name")
    species: SpeciesEnum = Field(..., description="Species")
    sex: SexEnum = Field(..., description="Sex")
    birth_date: Optional[date] = Field(default=None, description="Birth date (optional)")
    microchip: Optional[str] = Field(default=None, description="15-digit microchip")
    initial_owner_id: Optional[int] = Field(
        default=None, gt=0, description="Initial owner ID"
    )
    ownership_started_on: Optional[date] = Field(
        default=None, description="Ownership start date"
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not (1 <= len(v) <= 80):
            raise ValueError("name must be between 1 and 80 characters")
        return v

    @field_validator("birth_date", mode="before")
    @classmethod
    def validate_birth_date(cls, v: Any) -> Optional[date]:
        if v is None:
            return None
        return parse_and_validate_date(v)

    @field_validator("ownership_started_on", mode="before")
    @classmethod
    def validate_start_date(cls, v: Any) -> Optional[date]:
        if v is None:
            return None
        return parse_and_validate_date(v)

    @field_validator("microchip")
    @classmethod
    def validate_microchip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not re.match(r"^[0-9]{15}$", v):
            raise ValueError("microchip must be exactly 15 decimal digits")
        return v


class AnimalPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    species: Optional[SpeciesEnum] = None
    sex: Optional[SexEnum] = None
    birth_date: Optional[date] = None
    microchip: Optional[str] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (1 <= len(v) <= 80):
            raise ValueError("name must be between 1 and 80 characters")
        return v

    @field_validator("birth_date", mode="before")
    @classmethod
    def validate_birth_date(cls, v: Any) -> Optional[date]:
        if v is None:
            return None
        return parse_and_validate_date(v)

    @field_validator("microchip")
    @classmethod
    def validate_microchip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not re.match(r"^[0-9]{15}$", v):
            raise ValueError("microchip must be exactly 15 decimal digits")
        return v


class AnimalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    name: str
    species: SpeciesEnum
    sex: SexEnum
    birth_date: Optional[date] = None
    microchip: Optional[str] = None
    is_deleted: bool
    created_at: datetime
    updated_at: datetime


class AnimalListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[AnimalResponse]
    limit: int
    next_after_id: Optional[int] = None


class OwnershipCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_id: int = Field(..., gt=0, description="Owner ID")
    started_on: date = Field(..., description="Ownership started date")

    @field_validator("started_on", mode="before")
    @classmethod
    def validate_started_on(cls, v: Any) -> date:
        return parse_and_validate_date(v)


class OwnershipResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    owner_id: int
    animal_id: int
    started_on: date
    status: OwnershipStatusEnum
    ended_on: Optional[date] = None
    created_at: datetime


class OwnershipListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[OwnershipResponse]
    limit: int
    next_after_id: Optional[int] = None


# ============================================================================
# Seed Data Initialization
# ============================================================================
def init_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        existing = db.scalar(select(UserModel).limit(1))
        if not existing:
            u1 = UserModel(
                id=1,
                username="user1",
                role=RoleEnum.USER.value,
                token="user1-token-secret-12345",
            )
            u2 = UserModel(
                id=2,
                username="user2",
                role=RoleEnum.USER.value,
                token="user2-token-secret-12345",
            )
            admin = UserModel(
                id=3,
                username="admin",
                role=RoleEnum.ADMIN.value,
                token="admin-token-secret-12345",
            )
            db.add_all([u1, u2, admin])
            db.flush()

            # Owners
            o1 = OwnerModel(
                id=1,
                account_id=1,
                full_name="Alice Smith",
                email="alice@example.com",
                phone="+1 555-0101",
                is_deleted=False,
            )
            o2 = OwnerModel(
                id=2,
                account_id=2,
                full_name="Bob Jones",
                email="bob@example.com",
                phone="+1 555-0102",
                is_deleted=False,
            )
            db.add_all([o1, o2])
            db.flush()

            # Animals
            a1 = AnimalModel(
                id=1,
                name="Rocky",
                species=SpeciesEnum.DOG.value,
                sex=SexEnum.MALE.value,
                birth_date=date(2021, 5, 10),
                microchip="123456789012345",
                is_deleted=False,
            )
            a2 = AnimalModel(
                id=2,
                name="Cleo",
                species=SpeciesEnum.CAT.value,
                sex=SexEnum.FEMALE.value,
                birth_date=date(2022, 8, 20),
                microchip="987654321098765",
                is_deleted=False,
            )
            db.add_all([a1, a2])
            db.flush()

            # Ownerships
            own1 = OwnershipModel(
                id=1,
                owner_id=1,
                animal_id=1,
                started_on=date(2021, 6, 1),
                status=OwnershipStatusEnum.ACTIVE.value,
                ended_on=None,
            )
            own2 = OwnershipModel(
                id=2,
                owner_id=2,
                animal_id=2,
                started_on=date(2022, 9, 1),
                status=OwnershipStatusEnum.ACTIVE.value,
                ended_on=None,
            )
            db.add_all([own1, own2])
            db.commit()
    finally:
        db.close()


init_db()


# ============================================================================
# Dependency: DB Session & Current User
# ============================================================================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    authorization: Optional[str] = Header(None), db: Session = Depends(get_db)
) -> UserModel:
    if not authorization or not authorization.startswith("Bearer "):
        raise AppException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header with Bearer token",
            error_type="Unauthorized",
        )
    token = authorization.split("Bearer ", 1)[1].strip()
    user = db.scalar(select(UserModel).where(UserModel.token == token))
    if not user:
        raise AppException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token",
            error_type="Unauthorized",
        )
    return user


# ============================================================================
# Controllers (Static MVC)
# ============================================================================
class OwnerController:
    @staticmethod
    def list_owners(
        db: Session,
        current_user: UserModel,
        email_prefix: Optional[str],
        include_deleted: bool,
        limit: int,
        after_id: Optional[int],
    ) -> OwnerListResponse:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Listing owners is restricted to admins", "Forbidden")

        query = select(OwnerModel)
        if not include_deleted:
            query = query.where(OwnerModel.is_deleted == False)

        if email_prefix is not None:
            pref = email_prefix.strip().lower()
            if not (1 <= len(pref) <= 254):
                raise AppException(400, "email_prefix must be 1..254 characters", "Bad Request")
            query = query.where(OwnerModel.email.startswith(pref))

        if after_id is not None:
            if after_id <= 0:
                raise AppException(400, "after_id must be positive", "Bad Request")
            query = query.where(OwnerModel.id > after_id)

        query = query.order_by(OwnerModel.id.asc()).limit(limit + 1)
        rows = list(db.scalars(query).all())

        next_after_id = None
        if len(rows) > limit:
            items = rows[:limit]
            next_after_id = items[-1].id
        else:
            items = rows

        return OwnerListResponse(
            items=[OwnerResponse.model_validate(o) for o in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_owner(
        db: Session, current_user: UserModel, payload: OwnerCreateRequest
    ) -> OwnerModel:
        target_account_id = current_user.id
        if payload.account_id is not None:
            if current_user.role != RoleEnum.ADMIN.value:
                raise AppException(403, "Only admins may specify an account_id for owner", "Forbidden")
            target_account = db.scalar(
                select(UserModel).where(UserModel.id == payload.account_id)
            )
            if not target_account:
                raise AppException(404, f"Account {payload.account_id} not found", "Not Found")
            target_account_id = payload.account_id

        # Check if active owner profile already exists for this account
        existing_owner = db.scalar(
            select(OwnerModel).where(
                OwnerModel.account_id == target_account_id,
                OwnerModel.is_deleted == False,
            )
        )
        if existing_owner:
            raise AppException(
                409,
                f"Account {target_account_id} already has an active owner profile",
                "Conflict",
            )

        # Check normalized email uniqueness among non-deleted owners
        email_dup = db.scalar(
            select(OwnerModel).where(
                OwnerModel.email == payload.email,
                OwnerModel.is_deleted == False,
            )
        )
        if email_dup:
            raise AppException(409, f"Email '{payload.email}' already exists", "Conflict")

        owner = OwnerModel(
            account_id=target_account_id,
            full_name=payload.full_name,
            email=payload.email,
            phone=payload.phone,
            is_deleted=False,
        )
        db.add(owner)
        db.commit()
        db.refresh(owner)
        return owner

    @staticmethod
    def get_owner(db: Session, current_user: UserModel, owner_id: int) -> OwnerModel:
        if owner_id <= 0:
            raise AppException(404, f"Owner {owner_id} not found", "Not Found")

        owner = db.scalar(
            select(OwnerModel).where(
                OwnerModel.id == owner_id, OwnerModel.is_deleted == False
            )
        )
        if not owner:
            raise AppException(404, f"Owner {owner_id} not found", "Not Found")

        if (
            current_user.role != RoleEnum.ADMIN.value
            and owner.account_id != current_user.id
        ):
            raise AppException(403, "Access to owner profile is forbidden", "Forbidden")

        return owner

    @staticmethod
    def patch_owner(
        db: Session, current_user: UserModel, owner_id: int, payload: OwnerPatchRequest
    ) -> OwnerModel:
        owner = OwnerController.get_owner(db, current_user, owner_id)

        fields_set = payload.model_dump(exclude_unset=True)
        if not fields_set:
            raise AppException(400, "At least one field must be updated", "Bad Request")

        if payload.email is not None and payload.email != owner.email:
            dup = db.scalar(
                select(OwnerModel).where(
                    OwnerModel.email == payload.email,
                    OwnerModel.id != owner_id,
                    OwnerModel.is_deleted == False,
                )
            )
            if dup:
                raise AppException(409, f"Email '{payload.email}' is already in use", "Conflict")
            owner.email = payload.email

        if payload.full_name is not None:
            owner.full_name = payload.full_name

        if payload.phone is not None:
            owner.phone = payload.phone

        db.commit()
        db.refresh(owner)
        return owner

    @staticmethod
    def delete_owner(db: Session, current_user: UserModel, owner_id: int):
        owner = OwnerController.get_owner(db, current_user, owner_id)

        # REQ-3-007: Prohibited while owner has any ACTIVE ownership
        active_ownership = db.scalar(
            select(OwnershipModel)
            .where(
                OwnershipModel.owner_id == owner_id,
                OwnershipModel.status == OwnershipStatusEnum.ACTIVE.value,
            )
            .limit(1)
        )
        if active_ownership:
            raise AppException(
                409,
                "Cannot delete owner with ACTIVE animal ownership relations",
                "Conflict",
            )

        owner.is_deleted = True
        owner.email = f"redacted_{owner.id}@deleted.local"
        owner.phone = None
        db.commit()

    @staticmethod
    def list_owner_animals(
        db: Session,
        current_user: UserModel,
        owner_id: int,
        active_only: bool,
        limit: int,
        after_id: Optional[int],
    ) -> AnimalListResponse:
        OwnerController.get_owner(db, current_user, owner_id)

        query = (
            select(AnimalModel)
            .join(OwnershipModel, AnimalModel.id == OwnershipModel.animal_id)
            .where(
                OwnershipModel.owner_id == owner_id,
                AnimalModel.is_deleted == False,
            )
        )
        if active_only:
            query = query.where(OwnershipModel.status == OwnershipStatusEnum.ACTIVE.value)

        if after_id is not None:
            if after_id <= 0:
                raise AppException(400, "after_id must be positive", "Bad Request")
            query = query.where(AnimalModel.id > after_id)

        query = query.order_by(AnimalModel.id.asc()).limit(limit + 1)
        rows = list(db.scalars(query).all())

        next_after_id = None
        if len(rows) > limit:
            items = rows[:limit]
            next_after_id = items[-1].id
        else:
            items = rows

        return AnimalListResponse(
            items=[AnimalResponse.model_validate(a) for a in items],
            limit=limit,
            next_after_id=next_after_id,
        )


class AnimalController:
    @staticmethod
    def list_animals(
        db: Session,
        current_user: UserModel,
        species: Optional[SpeciesEnum],
        owner_id: Optional[int],
        unassigned: Optional[bool],
        limit: int,
        after_id: Optional[int],
    ) -> AnimalListResponse:
        if owner_id is not None and unassigned is True:
            raise AppException(
                400, "owner_id and unassigned filters are mutually exclusive", "Bad Request"
            )

        query = select(AnimalModel).where(AnimalModel.is_deleted == False)

        if current_user.role != RoleEnum.ADMIN.value:
            # Constrain to caller owner profile's ACTIVE ownership
            caller_owner = db.scalar(
                select(OwnerModel).where(
                    OwnerModel.account_id == current_user.id,
                    OwnerModel.is_deleted == False,
                )
            )
            if not caller_owner:
                return AnimalListResponse(items=[], limit=limit, next_after_id=None)

            active_animal_ids = select(OwnershipModel.animal_id).where(
                OwnershipModel.owner_id == caller_owner.id,
                OwnershipModel.status == OwnershipStatusEnum.ACTIVE.value,
            )
            query = query.where(AnimalModel.id.in_(active_animal_ids))
        else:
            # Admin filters
            if owner_id is not None:
                if owner_id <= 0:
                    raise AppException(404, f"Owner {owner_id} not found", "Not Found")
                owner = db.scalar(
                    select(OwnerModel).where(
                        OwnerModel.id == owner_id, OwnerModel.is_deleted == False
                    )
                )
                if not owner:
                    raise AppException(404, f"Owner {owner_id} not found", "Not Found")
                owned_animal_ids = select(OwnershipModel.animal_id).where(
                    OwnershipModel.owner_id == owner_id,
                    OwnershipModel.status == OwnershipStatusEnum.ACTIVE.value,
                )
                query = query.where(AnimalModel.id.in_(owned_animal_ids))

            if unassigned is True:
                active_animal_ids = select(OwnershipModel.animal_id).where(
                    OwnershipModel.status == OwnershipStatusEnum.ACTIVE.value
                )
                query = query.where(~AnimalModel.id.in_(active_animal_ids))

        if species is not None:
            query = query.where(AnimalModel.species == species.value)

        if after_id is not None:
            if after_id <= 0:
                raise AppException(400, "after_id must be positive", "Bad Request")
            query = query.where(AnimalModel.id > after_id)

        query = query.order_by(AnimalModel.id.asc()).limit(limit + 1)
        rows = list(db.scalars(query).all())

        next_after_id = None
        if len(rows) > limit:
            items = rows[:limit]
            next_after_id = items[-1].id
        else:
            items = rows

        return AnimalListResponse(
            items=[AnimalResponse.model_validate(a) for a in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_animal(
        db: Session, current_user: UserModel, payload: AnimalCreateRequest
    ) -> AnimalModel:
        if payload.microchip is not None:
            dup = db.scalar(
                select(AnimalModel).where(
                    AnimalModel.microchip == payload.microchip,
                    AnimalModel.is_deleted == False,
                )
            )
            if dup:
                raise AppException(409, f"Microchip '{payload.microchip}' already in use", "Conflict")

        target_owner_id = None
        if payload.initial_owner_id is not None:
            owner = db.scalar(
                select(OwnerModel).where(
                    OwnerModel.id == payload.initial_owner_id,
                    OwnerModel.is_deleted == False,
                )
            )
            if not owner:
                raise AppException(404, f"Owner {payload.initial_owner_id} not found", "Not Found")

            if current_user.role != RoleEnum.ADMIN.value:
                if owner.account_id != current_user.id:
                    raise AppException(
                        403, "User may only assign animal to user's own owner profile", "Forbidden"
                    )
            target_owner_id = owner.id

        animal = AnimalModel(
            name=payload.name,
            species=payload.species.value,
            sex=payload.sex.value,
            birth_date=payload.birth_date,
            microchip=payload.microchip,
            is_deleted=False,
        )
        db.add(animal)
        db.flush()

        if target_owner_id is not None:
            started_on = payload.ownership_started_on or date.today()
            ownership = OwnershipModel(
                owner_id=target_owner_id,
                animal_id=animal.id,
                started_on=started_on,
                status=OwnershipStatusEnum.ACTIVE.value,
                ended_on=None,
            )
            db.add(ownership)

        db.commit()
        db.refresh(animal)
        return animal

    @staticmethod
    def get_animal(db: Session, current_user: UserModel, animal_id: int) -> AnimalModel:
        if animal_id <= 0:
            raise AppException(404, f"Animal {animal_id} not found", "Not Found")

        animal = db.scalar(
            select(AnimalModel).where(
                AnimalModel.id == animal_id, AnimalModel.is_deleted == False
            )
        )
        if not animal:
            raise AppException(404, f"Animal {animal_id} not found", "Not Found")

        if current_user.role != RoleEnum.ADMIN.value:
            # Check caller has active ownership relation
            caller_owner = db.scalar(
                select(OwnerModel).where(
                    OwnerModel.account_id == current_user.id,
                    OwnerModel.is_deleted == False,
                )
            )
            if not caller_owner:
                raise AppException(403, "Access to animal is forbidden", "Forbidden")

            active_own = db.scalar(
                select(OwnershipModel).where(
                    OwnershipModel.animal_id == animal_id,
                    OwnershipModel.owner_id == caller_owner.id,
                    OwnershipModel.status == OwnershipStatusEnum.ACTIVE.value,
                )
            )
            if not active_own:
                raise AppException(403, "Access to animal is forbidden", "Forbidden")

        return animal

    @staticmethod
    def patch_animal(
        db: Session, current_user: UserModel, animal_id: int, payload: AnimalPatchRequest
    ) -> AnimalModel:
        animal = AnimalController.get_animal(db, current_user, animal_id)

        fields_set = payload.model_dump(exclude_unset=True)
        if not fields_set:
            raise AppException(400, "At least one field must be updated", "Bad Request")

        if payload.microchip is not None and payload.microchip != animal.microchip:
            dup = db.scalar(
                select(AnimalModel).where(
                    AnimalModel.microchip == payload.microchip,
                    AnimalModel.id != animal_id,
                    AnimalModel.is_deleted == False,
                )
            )
            if dup:
                raise AppException(409, f"Microchip '{payload.microchip}' already in use", "Conflict")
            animal.microchip = payload.microchip

        if payload.name is not None:
            animal.name = payload.name
        if payload.species is not None:
            animal.species = payload.species.value
        if payload.sex is not None:
            animal.sex = payload.sex.value
        if payload.birth_date is not None:
            animal.birth_date = payload.birth_date

        db.commit()
        db.refresh(animal)
        return animal

    @staticmethod
    def delete_animal(
        db: Session, current_user: UserModel, animal_id: int, ended_on_val: date
    ):
        animal = AnimalController.get_animal(db, current_user, animal_id)

        # REQ-3-008: Any ACTIVE ownership is ended using supplied ended_on
        active_own = db.scalar(
            select(OwnershipModel).where(
                OwnershipModel.animal_id == animal_id,
                OwnershipModel.status == OwnershipStatusEnum.ACTIVE.value,
            )
        )
        if active_own:
            if ended_on_val < active_own.started_on:
                raise AppException(
                    409,
                    f"ended_on ({ended_on_val.isoformat()}) cannot precede ownership started_on ({active_own.started_on.isoformat()})",
                    "Conflict",
                )
            active_own.status = OwnershipStatusEnum.ENDED.value
            active_own.ended_on = ended_on_val

        animal.is_deleted = True
        db.commit()


class OwnershipController:
    @staticmethod
    def list_ownerships(
        db: Session,
        current_user: UserModel,
        owner_id: Optional[int],
        animal_id: Optional[int],
        status_val: Optional[OwnershipStatusEnum],
        started_from: Optional[date],
        started_to: Optional[date],
        limit: int,
        after_id: Optional[int],
    ) -> OwnershipListResponse:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Listing ownerships is restricted to admins", "Forbidden")

        query = select(OwnershipModel)

        if owner_id is not None:
            if owner_id <= 0:
                raise AppException(404, f"Owner {owner_id} not found", "Not Found")
            owner = db.scalar(select(OwnerModel).where(OwnerModel.id == owner_id))
            if not owner:
                raise AppException(404, f"Owner {owner_id} not found", "Not Found")
            query = query.where(OwnershipModel.owner_id == owner_id)

        if animal_id is not None:
            if animal_id <= 0:
                raise AppException(404, f"Animal {animal_id} not found", "Not Found")
            animal = db.scalar(select(AnimalModel).where(AnimalModel.id == animal_id))
            if not animal:
                raise AppException(404, f"Animal {animal_id} not found", "Not Found")
            query = query.where(OwnershipModel.animal_id == animal_id)

        if status_val is not None:
            query = query.where(OwnershipModel.status == status_val.value)

        if started_from is not None and started_to is not None:
            if started_from > started_to:
                raise AppException(400, "started_from cannot be later than started_to", "Bad Request")

        if started_from is not None:
            query = query.where(OwnershipModel.started_on >= started_from)

        if started_to is not None:
            query = query.where(OwnershipModel.started_on <= started_to)

        if after_id is not None:
            if after_id <= 0:
                raise AppException(400, "after_id must be positive", "Bad Request")
            query = query.where(OwnershipModel.id > after_id)

        query = query.order_by(OwnershipModel.id.asc()).limit(limit + 1)
        rows = list(db.scalars(query).all())

        next_after_id = None
        if len(rows) > limit:
            items = rows[:limit]
            next_after_id = items[-1].id
        else:
            items = rows

        return OwnershipListResponse(
            items=[OwnershipResponse.model_validate(o) for o in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def list_animal_ownerships(
        db: Session,
        current_user: UserModel,
        animal_id: int,
        status_val: Optional[OwnershipStatusEnum],
        limit: int,
        after_id: Optional[int],
    ) -> OwnershipListResponse:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Listing animal ownerships is restricted to admins", "Forbidden")

        if animal_id <= 0:
            raise AppException(404, f"Animal {animal_id} not found", "Not Found")

        animal = db.scalar(
            select(AnimalModel).where(
                AnimalModel.id == animal_id, AnimalModel.is_deleted == False
            )
        )
        if not animal:
            raise AppException(404, f"Animal {animal_id} not found", "Not Found")

        query = select(OwnershipModel).where(OwnershipModel.animal_id == animal_id)
        if status_val is not None:
            query = query.where(OwnershipModel.status == status_val.value)

        if after_id is not None:
            if after_id <= 0:
                raise AppException(400, "after_id must be positive", "Bad Request")
            query = query.where(OwnershipModel.id > after_id)

        query = query.order_by(OwnershipModel.id.asc()).limit(limit + 1)
        rows = list(db.scalars(query).all())

        next_after_id = None
        if len(rows) > limit:
            items = rows[:limit]
            next_after_id = items[-1].id
        else:
            items = rows

        return OwnershipListResponse(
            items=[OwnershipResponse.model_validate(o) for o in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_ownership(
        db: Session,
        current_user: UserModel,
        animal_id: int,
        payload: OwnershipCreateRequest,
    ) -> OwnershipModel:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Only admins may create ownership relations", "Forbidden")

        if animal_id <= 0:
            raise AppException(404, f"Animal {animal_id} not found", "Not Found")

        animal = db.scalar(
            select(AnimalModel).where(
                AnimalModel.id == animal_id, AnimalModel.is_deleted == False
            )
        )
        if not animal:
            raise AppException(404, f"Animal {animal_id} not found", "Not Found")

        owner = db.scalar(
            select(OwnerModel).where(
                OwnerModel.id == payload.owner_id, OwnerModel.is_deleted == False
            )
        )
        if not owner:
            raise AppException(404, f"Owner {payload.owner_id} not found", "Not Found")

        # REQ-3-006: Check if animal already has an ACTIVE ownership
        active_own = db.scalar(
            select(OwnershipModel).where(
                OwnershipModel.animal_id == animal_id,
                OwnershipModel.status == OwnershipStatusEnum.ACTIVE.value,
            )
        )
        if active_own:
            raise AppException(
                409,
                f"Animal {animal_id} already has an ACTIVE ownership (ID: {active_own.id})",
                "Conflict",
            )

        ownership = OwnershipModel(
            owner_id=payload.owner_id,
            animal_id=animal_id,
            started_on=payload.started_on,
            status=OwnershipStatusEnum.ACTIVE.value,
            ended_on=None,
        )
        db.add(ownership)
        db.commit()
        db.refresh(ownership)
        return ownership

    @staticmethod
    def get_ownership(
        db: Session, current_user: UserModel, ownership_id: int
    ) -> OwnershipModel:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Access to ownership records is restricted to admins", "Forbidden")

        if ownership_id <= 0:
            raise AppException(404, f"Ownership {ownership_id} not found", "Not Found")

        own = db.scalar(
            select(OwnershipModel).where(OwnershipModel.id == ownership_id)
        )
        if not own:
            raise AppException(404, f"Ownership {ownership_id} not found", "Not Found")

        return own

    @staticmethod
    def end_ownership(
        db: Session, current_user: UserModel, ownership_id: int, ended_on_val: date
    ):
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Ending ownership relations is restricted to admins", "Forbidden")

        if ownership_id <= 0:
            raise AppException(404, f"Ownership {ownership_id} not found", "Not Found")

        own = db.scalar(
            select(OwnershipModel).where(OwnershipModel.id == ownership_id)
        )
        if not own:
            raise AppException(404, f"Ownership {ownership_id} not found", "Not Found")

        if own.status == OwnershipStatusEnum.ENDED.value:
            raise AppException(409, "Ownership is already ENDED", "Conflict")

        if ended_on_val < own.started_on:
            raise AppException(
                409,
                f"ended_on ({ended_on_val.isoformat()}) cannot precede started_on ({own.started_on.isoformat()})",
                "Conflict",
            )

        own.status = OwnershipStatusEnum.ENDED.value
        own.ended_on = ended_on_val
        db.commit()


# ============================================================================
# FastAPI Application & Routes
# ============================================================================
app = FastAPI(
    title="Veterinary Clinic - Owners, Animals, and Relationship Deletion",
    version="1.0.0",
)


# ASGI Middleware
class JsonValidationASGIMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"].upper()
        path = scope["path"]

        if path == "/health":
            await self.app(scope, receive, send)
            return

        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        from starlette.routing import Match

        fastapi_app: FastAPI = scope.get("app")
        if fastapi_app and hasattr(fastapi_app, "router"):
            has_matching_path = False
            is_method_allowed = False
            for route in fastapi_app.router.routes:
                match, _ = route.matches(scope)
                if match == Match.FULL:
                    has_matching_path = True
                    is_method_allowed = True
                    break
                elif match == Match.PARTIAL:
                    has_matching_path = True

            if has_matching_path and not is_method_allowed:
                await self.app(scope, receive, send)
                return

        headers_dict = {
            k.decode("latin1").lower(): v.decode("latin1")
            for k, v in scope["headers"]
        }
        content_length_str = headers_dict.get("content-length")
        content_type = headers_dict.get("content-type", "")

        if content_length_str:
            try:
                cl = int(content_length_str)
                if cl > MAX_BODY_SIZE:
                    await self._send_json_error(
                        send,
                        413,
                        f"Request payload exceeds {MAX_BODY_SIZE} bytes limit",
                        "Payload Too Large",
                    )
                    return
            except ValueError:
                pass

        if method in ("POST", "PATCH", "PUT"):
            if not content_type.startswith("application/json"):
                await self._send_json_error(
                    send,
                    415,
                    "Content-Type must be application/json",
                    "Unsupported Media Type",
                )
                return

            body = bytearray()
            more_body = True
            while more_body:
                message = await receive()
                chunk = message.get("body", b"")
                body.extend(chunk)
                if len(body) > MAX_BODY_SIZE:
                    await self._send_json_error(
                        send,
                        413,
                        f"Request payload exceeds {MAX_BODY_SIZE} bytes limit",
                        "Payload Too Large",
                    )
                    return
                more_body = message.get("more_body", False)

            body_bytes = bytes(body)
            if body_bytes.strip():
                try:
                    json.loads(body_bytes.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as err:
                    await self._send_json_error(
                        send,
                        400,
                        f"Malformed JSON payload: {str(err)}",
                        "Bad Request",
                    )
                    return

            sent = False

            async def custom_receive() -> Message:
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body_bytes, "more_body": False}
                return {"type": "http.request", "body": b"", "more_body": False}

            await self.app(scope, custom_receive, send)
            return

        await self.app(scope, receive, send)

    async def _send_json_error(
        self, send: Send, status_code: int, detail: str, error_type: str
    ):
        body = json.dumps(
            {"detail": detail, "status_code": status_code, "error_type": error_type}
        ).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("latin1")),
        ]
        await send({"type": "http.response.start", "status": status_code, "headers": headers})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(JsonValidationASGIMiddleware)


# Exception Handlers
@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "status_code": exc.status_code,
            "error_type": exc.error_type,
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    detail = errors[0]["msg"] if errors else "Validation error"
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": detail,
            "status_code": 422,
            "error_type": "Unprocessable Entity",
        },
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled error: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "An unexpected internal server error occurred",
            "status_code": 500,
            "error_type": "Internal Server Error",
        },
    )


# 1. GET /health
@app.get(
    "/health",
    tags=["Health"],
    response_model=Dict[str, str],
    responses={503: {"description": "Service Unavailable"}},
)
def get_health(db: Session = Depends(get_db)):
    try:
        db.execute(select(1))
        return {"status": "ok", "database": "connected"}
    except Exception:
        raise AppException(503, "Local persistence probe failed", "Service Unavailable")


# 2. GET /owners
@app.get("/owners", tags=["Owners"], response_model=OwnerListResponse)
def list_owners(
    email_prefix: Optional[str] = Query(None, min_length=1, max_length=254),
    include_deleted: bool = Query(False),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return OwnerController.list_owners(
        db, current_user, email_prefix, include_deleted, limit, after_id
    )


# 3. POST /owners
@app.post(
    "/owners",
    tags=["Owners"],
    response_model=OwnerResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_owner(
    payload: OwnerCreateRequest,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owner = OwnerController.create_owner(db, current_user, payload)
    response.headers["Location"] = f"/owners/{owner.id}"
    return OwnerResponse.model_validate(owner)


# 4. GET /owners/{owner_id}
@app.get("/owners/{owner_id}", tags=["Owners"], response_model=OwnerResponse)
def get_owner(
    owner_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owner = OwnerController.get_owner(db, current_user, owner_id)
    return OwnerResponse.model_validate(owner)


# 5. PATCH /owners/{owner_id}
@app.patch("/owners/{owner_id}", tags=["Owners"], response_model=OwnerResponse)
def patch_owner(
    owner_id: int,
    payload: OwnerPatchRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    owner = OwnerController.patch_owner(db, current_user, owner_id, payload)
    return OwnerResponse.model_validate(owner)


# 6. DELETE /owners/{owner_id}
@app.delete(
    "/owners/{owner_id}",
    tags=["Owners"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_owner(
    owner_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    OwnerController.delete_owner(db, current_user, owner_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 7. GET /owners/{owner_id}/animals
@app.get("/owners/{owner_id}/animals", tags=["Owners"], response_model=AnimalListResponse)
def list_owner_animals(
    owner_id: int,
    active_only: bool = Query(True),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return OwnerController.list_owner_animals(
        db, current_user, owner_id, active_only, limit, after_id
    )


# 8. GET /animals
@app.get("/animals", tags=["Animals"], response_model=AnimalListResponse)
def list_animals(
    species: Optional[SpeciesEnum] = None,
    owner_id: Optional[int] = Query(None, gt=0),
    unassigned: Optional[bool] = None,
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return AnimalController.list_animals(
        db, current_user, species, owner_id, unassigned, limit, after_id
    )


# 9. POST /animals
@app.post(
    "/animals",
    tags=["Animals"],
    response_model=AnimalResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_animal(
    payload: AnimalCreateRequest,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    animal = AnimalController.create_animal(db, current_user, payload)
    response.headers["Location"] = f"/animals/{animal.id}"
    return AnimalResponse.model_validate(animal)


# 10. GET /animals/{animal_id}
@app.get("/animals/{animal_id}", tags=["Animals"], response_model=AnimalResponse)
def get_animal(
    animal_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    animal = AnimalController.get_animal(db, current_user, animal_id)
    return AnimalResponse.model_validate(animal)


# 11. PATCH /animals/{animal_id}
@app.patch("/animals/{animal_id}", tags=["Animals"], response_model=AnimalResponse)
def patch_animal(
    animal_id: int,
    payload: AnimalPatchRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    animal = AnimalController.patch_animal(db, current_user, animal_id, payload)
    return AnimalResponse.model_validate(animal)


# 12. DELETE /animals/{animal_id}
@app.delete(
    "/animals/{animal_id}",
    tags=["Animals"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_animal(
    animal_id: int,
    ended_on: date = Query(..., description="Date ownership ended (YYYY-MM-DD)"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    parse_and_validate_date(ended_on)
    AnimalController.delete_animal(db, current_user, animal_id, ended_on)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 13. GET /ownerships
@app.get("/ownerships", tags=["Ownerships"], response_model=OwnershipListResponse)
def list_ownerships(
    owner_id: Optional[int] = Query(None, gt=0),
    animal_id: Optional[int] = Query(None, gt=0),
    ownership_status: Optional[OwnershipStatusEnum] = Query(None, alias="status"),
    started_from: Optional[date] = None,
    started_to: Optional[date] = None,
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return OwnershipController.list_ownerships(
        db,
        current_user,
        owner_id,
        animal_id,
        ownership_status,
        started_from,
        started_to,
        limit,
        after_id,
    )


# 14. GET /animals/{animal_id}/ownerships
@app.get(
    "/animals/{animal_id}/ownerships",
    tags=["Ownerships"],
    response_model=OwnershipListResponse,
)
def list_animal_ownerships(
    animal_id: int,
    ownership_status: Optional[OwnershipStatusEnum] = Query(None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return OwnershipController.list_animal_ownerships(
        db, current_user, animal_id, ownership_status, limit, after_id
    )


# 15. POST /animals/{animal_id}/ownerships
@app.post(
    "/animals/{animal_id}/ownerships",
    tags=["Ownerships"],
    response_model=OwnershipResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_ownership(
    animal_id: int,
    payload: OwnershipCreateRequest,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    own = OwnershipController.create_ownership(
        db, current_user, animal_id, payload
    )
    response.headers["Location"] = f"/ownerships/{own.id}"
    return OwnershipResponse.model_validate(own)


# 16. GET /ownerships/{ownership_id}
@app.get(
    "/ownerships/{ownership_id}",
    tags=["Ownerships"],
    response_model=OwnershipResponse,
)
def get_ownership(
    ownership_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    own = OwnershipController.get_ownership(db, current_user, ownership_id)
    return OwnershipResponse.model_validate(own)


# 17. DELETE /ownerships/{ownership_id}
@app.delete(
    "/ownerships/{ownership_id}",
    tags=["Ownerships"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def end_ownership(
    ownership_id: int,
    ended_on: date = Query(..., description="Date ownership ended (YYYY-MM-DD)"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    parse_and_validate_date(ended_on)
    OwnershipController.end_ownership(db, current_user, ownership_id, ended_on)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
