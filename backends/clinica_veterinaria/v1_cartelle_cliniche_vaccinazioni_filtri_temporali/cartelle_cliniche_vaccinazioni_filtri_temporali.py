"""
Veterinary Clinic - Variant C: Clinical records, vaccinations, and temporal filters
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
logger = logging.getLogger("vet_clinical_records")
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

MIN_TIMESTAMP = datetime(1900, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
MAX_TIMESTAMP = datetime(2100, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


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


class RecordTypeEnum(str, enum.Enum):
    VISIT = "VISIT"
    DIAGNOSIS = "DIAGNOSIS"
    PRESCRIPTION = "PRESCRIPTION"
    LAB_NOTE = "LAB_NOTE"


class ClinicalRecordStatusEnum(str, enum.Enum):
    DRAFT = "DRAFT"
    FINAL = "FINAL"


class VaccineCodeEnum(str, enum.Enum):
    RABIES = "RABIES"
    DHPP = "DHPP"
    FELV = "FELV"
    LEPTOSPIROSIS = "LEPTOSPIROSIS"
    OTHER = "OTHER"


class VaccinationStatusEnum(str, enum.Enum):
    CURRENT = "CURRENT"
    EXPIRED = "EXPIRED"
    NOT_RECORDED = "NOT_RECORDED"


class TimelineEventKindEnum(str, enum.Enum):
    RECORD = "RECORD"
    VACCINATION = "VACCINATION"


# ============================================================================
# SQLAlchemy Models
# ============================================================================
class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    role = Column(String(20), nullable=False)  # user or admin
    token = Column(String(100), unique=True, nullable=False)


class AnimalModel(Base):
    __tablename__ = "animals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_account_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(80), nullable=False)
    species = Column(String(20), nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    is_deleted = Column(Boolean, default=False, nullable=False)
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


class ClinicalRecordModel(Base):
    __tablename__ = "clinical_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    animal_id = Column(Integer, ForeignKey("animals.id"), nullable=False, index=True)
    record_type = Column(String(20), nullable=False)
    occurred_on = Column(Date, nullable=False, index=True)
    summary = Column(String(2000), nullable=False)
    diagnosis_code = Column(String(20), nullable=True)
    status = Column(
        String(20),
        nullable=False,
        default=ClinicalRecordStatusEnum.DRAFT.value,
        index=True,
    )
    finalized_at = Column(DateTime(timezone=True), nullable=True)
    finalized_by = Column(Integer, ForeignKey("users.id"), nullable=True)
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


class VaccinationModel(Base):
    __tablename__ = "vaccinations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    animal_id = Column(Integer, ForeignKey("animals.id"), nullable=False, index=True)
    vaccine_code = Column(String(20), nullable=False, index=True)
    vaccine_name = Column(String(120), nullable=False)
    administered_on = Column(Date, nullable=False, index=True)
    expires_on = Column(Date, nullable=False, index=True)
    batch_number = Column(String(80), nullable=False)
    is_void = Column(Boolean, default=False, nullable=False, index=True)
    voided_on = Column(Date, nullable=True)
    void_reason = Column(String(500), nullable=True)
    voided_by = Column(Integer, ForeignKey("users.id"), nullable=True)
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


def parse_and_validate_timestamp(v: Any) -> datetime:
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
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    if not (MIN_TIMESTAMP <= dt <= MAX_TIMESTAMP):
        raise ValueError(
            f"Timestamp must be between {MIN_TIMESTAMP.isoformat()} and {MAX_TIMESTAMP.isoformat()}"
        )
    return dt


def to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class AnimalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Animal name")
    species: SpeciesEnum = Field(..., description="Species")
    active: Optional[bool] = Field(default=True, description="Active status")
    client_account_id: Optional[int] = Field(
        default=None, gt=0, description="Client account ID (admin only)"
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not (1 <= len(v) <= 80):
            raise ValueError("name must be between 1 and 80 characters")
        return v


class AnimalPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    species: Optional[SpeciesEnum] = None
    active: Optional[bool] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (1 <= len(v) <= 80):
            raise ValueError("name must be between 1 and 80 characters")
        return v


class AnimalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    client_account_id: int
    name: str
    species: SpeciesEnum
    active: bool
    created_at: datetime
    updated_at: datetime


class AnimalListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[AnimalResponse]
    limit: int
    next_after_id: Optional[int] = None


class ClinicalRecordCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_type: RecordTypeEnum = Field(..., description="Record type")
    occurred_on: date = Field(..., description="Date record occurred")
    summary: str = Field(..., description="Clinical summary")
    diagnosis_code: Optional[str] = Field(default=None, description="Diagnosis code")

    @field_validator("occurred_on", mode="before")
    @classmethod
    def validate_occurred_on(cls, v: Any) -> date:
        return parse_and_validate_date(v)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, v: str) -> str:
        v = v.strip()
        if not (3 <= len(v) <= 2000):
            raise ValueError("summary must be between 3 and 2000 characters")
        return v

    @field_validator("diagnosis_code")
    @classmethod
    def validate_diag_code(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not re.match(r"^[A-Z][A-Z0-9.-]{1,19}$", v):
            raise ValueError("diagnosis_code must match ^[A-Z][A-Z0-9.-]{1,19}$")
        return v


class ClinicalRecordPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_type: Optional[RecordTypeEnum] = None
    occurred_on: Optional[date] = None
    summary: Optional[str] = None
    diagnosis_code: Optional[str] = None

    @field_validator("occurred_on", mode="before")
    @classmethod
    def validate_occurred_on(cls, v: Any) -> Optional[date]:
        if v is None:
            return None
        return parse_and_validate_date(v)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (3 <= len(v) <= 2000):
            raise ValueError("summary must be between 3 and 2000 characters")
        return v

    @field_validator("diagnosis_code")
    @classmethod
    def validate_diag_code(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not re.match(r"^[A-Z][A-Z0-9.-]{1,19}$", v):
            raise ValueError("diagnosis_code must match ^[A-Z][A-Z0-9.-]{1,19}$")
        return v


class ClinicalRecordFinalizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finalized_at: datetime = Field(..., description="Offset-aware UTC finalization timestamp")

    @field_validator("finalized_at", mode="before")
    @classmethod
    def validate_finalized_at(cls, v: Any) -> datetime:
        return parse_and_validate_timestamp(v)


class ClinicalRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    animal_id: int
    record_type: RecordTypeEnum
    occurred_on: date
    summary: str
    diagnosis_code: Optional[str] = None
    status: ClinicalRecordStatusEnum
    finalized_at: Optional[datetime] = None
    finalized_by: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class ClinicalRecordListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[ClinicalRecordResponse]
    limit: int
    next_after_id: Optional[int] = None


class VaccinationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vaccine_code: VaccineCodeEnum = Field(..., description="Vaccine code")
    vaccine_name: str = Field(..., description="Vaccine commercial/generic name")
    administered_on: date = Field(..., description="Administration date")
    expires_on: date = Field(..., description="Expiration date")
    batch_number: str = Field(..., description="Batch number")

    @field_validator("vaccine_name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not (2 <= len(v) <= 120):
            raise ValueError("vaccine_name must be between 2 and 120 characters")
        return v

    @field_validator("administered_on", mode="before")
    @classmethod
    def validate_admin_on(cls, v: Any) -> date:
        return parse_and_validate_date(v)

    @field_validator("expires_on", mode="before")
    @classmethod
    def validate_exp_on(cls, v: Any) -> date:
        return parse_and_validate_date(v)

    @field_validator("batch_number")
    @classmethod
    def validate_batch(cls, v: str) -> str:
        v = v.strip()
        if not (1 <= len(v) <= 80):
            raise ValueError("batch_number must be between 1 and 80 characters")
        return v


class VaccinationVoidRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voided_on: date = Field(..., description="Voiding date")
    void_reason: str = Field(..., description="Reason for voiding")

    @field_validator("voided_on", mode="before")
    @classmethod
    def validate_voided_on(cls, v: Any) -> date:
        return parse_and_validate_date(v)

    @field_validator("void_reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        v = v.strip()
        if not (3 <= len(v) <= 500):
            raise ValueError("void_reason must be between 3 and 500 characters")
        return v


class VaccinationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    animal_id: int
    vaccine_code: VaccineCodeEnum
    vaccine_name: str
    administered_on: date
    expires_on: date
    batch_number: str
    is_void: bool
    voided_on: Optional[date] = None
    void_reason: Optional[str] = None
    voided_by: Optional[int] = None
    created_at: datetime


class VaccinationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[VaccinationResponse]
    limit: int
    next_after_id: Optional[int] = None


class VaccineStatusItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vaccine_code: VaccineCodeEnum
    status: VaccinationStatusEnum
    administered_on: Optional[date] = None
    expires_on: Optional[date] = None


class VaccinationStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    animal_id: int
    as_of: date
    statuses: List[VaccineStatusItem]


class TimelineEventItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: int
    event_date: date
    event_kind: TimelineEventKindEnum
    details: Dict[str, Any]


class ClinicalTimelineResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[TimelineEventItem]
    limit: int
    next_after_event_date: Optional[date] = None
    next_after_event_kind: Optional[TimelineEventKindEnum] = None
    next_after_event_id: Optional[int] = None


# ============================================================================
# Seed Data Initialization
# ============================================================================
def init_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        existing = db.scalar(select(UserModel).limit(1))
        if not existing:
            # Users
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

            # Animals
            a1 = AnimalModel(
                id=1,
                client_account_id=1,
                name="Max",
                species=SpeciesEnum.DOG.value,
                active=True,
                is_deleted=False,
            )
            a2 = AnimalModel(
                id=2,
                client_account_id=2,
                name="Milo",
                species=SpeciesEnum.CAT.value,
                active=True,
                is_deleted=False,
            )
            db.add_all([a1, a2])
            db.flush()

            # Records: One FINAL and one DRAFT
            r1 = ClinicalRecordModel(
                id=1,
                animal_id=1,
                record_type=RecordTypeEnum.VISIT.value,
                occurred_on=date(2025, 1, 15),
                summary="Annual wellness checkup completed successfully",
                diagnosis_code=None,
                status=ClinicalRecordStatusEnum.FINAL.value,
                finalized_at=datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc),
                finalized_by=3,
            )
            r2 = ClinicalRecordModel(
                id=2,
                animal_id=1,
                record_type=RecordTypeEnum.DIAGNOSIS.value,
                occurred_on=date(2025, 2, 1),
                summary="Mild ear infection preliminary observation",
                diagnosis_code="OTITIS-01",
                status=ClinicalRecordStatusEnum.DRAFT.value,
                finalized_at=None,
                finalized_by=None,
            )
            db.add_all([r1, r2])
            db.flush()

            # Vaccinations: One active (non-void) and one void
            v1 = VaccinationModel(
                id=1,
                animal_id=1,
                vaccine_code=VaccineCodeEnum.RABIES.value,
                vaccine_name="Rabies Core",
                administered_on=date(2025, 1, 15),
                expires_on=date(2026, 1, 15),
                batch_number="RAB-2025-001",
                is_void=False,
            )
            v2 = VaccinationModel(
                id=2,
                animal_id=1,
                vaccine_code=VaccineCodeEnum.DHPP.value,
                vaccine_name="DHPP Combo",
                administered_on=date(2024, 6, 1),
                expires_on=date(2025, 6, 1),
                batch_number="DHPP-ERR-99",
                is_void=True,
                voided_on=date(2024, 6, 2),
                void_reason="Wrong dose administered initially",
                voided_by=3,
            )
            db.add_all([v1, v2])
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
class AnimalController:
    @staticmethod
    def list_animals(
        db: Session,
        current_user: UserModel,
        species: Optional[SpeciesEnum],
        active: Optional[bool],
        client_account_id: Optional[int],
        limit: int,
        after_id: Optional[int],
    ) -> AnimalListResponse:
        if client_account_id is not None and current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "client_account_id filter permitted only to admins", "Forbidden")

        query = select(AnimalModel).where(AnimalModel.is_deleted == False)

        if current_user.role != RoleEnum.ADMIN.value:
            query = query.where(AnimalModel.client_account_id == current_user.id)
        elif client_account_id is not None:
            if client_account_id <= 0:
                raise AppException(400, "client_account_id must be positive", "Bad Request")
            query = query.where(AnimalModel.client_account_id == client_account_id)

        if species is not None:
            query = query.where(AnimalModel.species == species.value)

        if active is not None:
            query = query.where(AnimalModel.active == active)

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
        target_account_id = current_user.id
        if payload.client_account_id is not None:
            if current_user.role != RoleEnum.ADMIN.value and payload.client_account_id != current_user.id:
                raise AppException(403, "Only admins may assign animals to other accounts", "Forbidden")
            target_user = db.scalar(
                select(UserModel).where(UserModel.id == payload.client_account_id)
            )
            if not target_user:
                raise AppException(404, f"Account {payload.client_account_id} not found", "Not Found")
            target_account_id = payload.client_account_id

        animal = AnimalModel(
            client_account_id=target_account_id,
            name=payload.name,
            species=payload.species.value,
            active=payload.active if payload.active is not None else True,
            is_deleted=False,
        )
        db.add(animal)
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

        if (
            current_user.role != RoleEnum.ADMIN.value
            and animal.client_account_id != current_user.id
        ):
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

        if payload.name is not None:
            animal.name = payload.name
        if payload.species is not None:
            animal.species = payload.species.value
        if payload.active is not None:
            animal.active = payload.active

        db.commit()
        db.refresh(animal)
        return animal

    @staticmethod
    def delete_animal(db: Session, current_user: UserModel, animal_id: int):
        animal = AnimalController.get_animal(db, current_user, animal_id)

        # REQ-3-008: Soft-deleted only when no clinical record and no vaccination references it
        rec = db.scalar(
            select(ClinicalRecordModel).where(ClinicalRecordModel.animal_id == animal_id).limit(1)
        )
        if rec:
            raise AppException(
                409, "Cannot delete animal referenced by clinical records", "Conflict"
            )

        vac = db.scalar(
            select(VaccinationModel).where(VaccinationModel.animal_id == animal_id).limit(1)
        )
        if vac:
            raise AppException(
                409, "Cannot delete animal referenced by vaccination events", "Conflict"
            )

        animal.is_deleted = True
        animal.active = False
        db.commit()


class ClinicalRecordController:
    @staticmethod
    def list_records(
        db: Session,
        current_user: UserModel,
        animal_id: Optional[int],
        record_type: Optional[RecordTypeEnum],
        record_status: Optional[ClinicalRecordStatusEnum],
        occurred_from: Optional[date],
        occurred_to: Optional[date],
        diagnosis_code_prefix: Optional[str],
        limit: int,
        after_id: Optional[int],
    ) -> ClinicalRecordListResponse:
        query = select(ClinicalRecordModel).join(
            AnimalModel, ClinicalRecordModel.animal_id == AnimalModel.id
        ).where(AnimalModel.is_deleted == False)

        if current_user.role != RoleEnum.ADMIN.value:
            query = query.where(AnimalModel.client_account_id == current_user.id)

        if animal_id is not None:
            if animal_id <= 0:
                raise AppException(404, f"Animal {animal_id} not found", "Not Found")
            animal = db.scalar(
                select(AnimalModel).where(
                    AnimalModel.id == animal_id, AnimalModel.is_deleted == False
                )
            )
            if not animal:
                raise AppException(404, f"Animal {animal_id} not found", "Not Found")
            if (
                current_user.role != RoleEnum.ADMIN.value
                and animal.client_account_id != current_user.id
            ):
                raise AppException(403, "Access to animal records is forbidden", "Forbidden")
            query = query.where(ClinicalRecordModel.animal_id == animal_id)

        if record_type is not None:
            query = query.where(ClinicalRecordModel.record_type == record_type.value)

        if record_status is not None:
            query = query.where(ClinicalRecordModel.status == record_status.value)

        if occurred_from is not None and occurred_to is not None:
            if occurred_from > occurred_to:
                raise AppException(400, "occurred_from cannot be later than occurred_to", "Bad Request")

        if occurred_from is not None:
            query = query.where(ClinicalRecordModel.occurred_on >= occurred_from)

        if occurred_to is not None:
            query = query.where(ClinicalRecordModel.occurred_on <= occurred_to)

        if diagnosis_code_prefix is not None:
            pref = diagnosis_code_prefix.strip()
            if not (1 <= len(pref) <= 20) or not re.match(r"^[A-Z0-9.-]+$", pref):
                raise AppException(400, "Invalid diagnosis_code_prefix", "Bad Request")
            query = query.where(ClinicalRecordModel.diagnosis_code.startswith(pref))

        if after_id is not None:
            if after_id <= 0:
                raise AppException(400, "after_id must be positive", "Bad Request")
            query = query.where(ClinicalRecordModel.id > after_id)

        query = query.order_by(ClinicalRecordModel.id.asc()).limit(limit + 1)
        rows = list(db.scalars(query).all())

        next_after_id = None
        if len(rows) > limit:
            items = rows[:limit]
            next_after_id = items[-1].id
        else:
            items = rows

        return ClinicalRecordListResponse(
            items=[ClinicalRecordResponse.model_validate(r) for r in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_record(
        db: Session,
        current_user: UserModel,
        animal_id: int,
        payload: ClinicalRecordCreateRequest,
    ) -> ClinicalRecordModel:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Only admins may create clinical records", "Forbidden")

        if animal_id <= 0:
            raise AppException(404, f"Animal {animal_id} not found", "Not Found")

        animal = db.scalar(
            select(AnimalModel).where(
                AnimalModel.id == animal_id, AnimalModel.is_deleted == False
            )
        )
        if not animal:
            raise AppException(404, f"Animal {animal_id} not found", "Not Found")

        rec = ClinicalRecordModel(
            animal_id=animal_id,
            record_type=payload.record_type.value,
            occurred_on=payload.occurred_on,
            summary=payload.summary,
            diagnosis_code=payload.diagnosis_code,
            status=ClinicalRecordStatusEnum.DRAFT.value,
            finalized_at=None,
            finalized_by=None,
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        return rec

    @staticmethod
    def get_record(
        db: Session, current_user: UserModel, record_id: int
    ) -> ClinicalRecordModel:
        if record_id <= 0:
            raise AppException(404, f"Clinical record {record_id} not found", "Not Found")

        rec = db.scalar(
            select(ClinicalRecordModel).where(ClinicalRecordModel.id == record_id)
        )
        if not rec:
            raise AppException(404, f"Clinical record {record_id} not found", "Not Found")

        animal = db.scalar(
            select(AnimalModel).where(
                AnimalModel.id == rec.animal_id, AnimalModel.is_deleted == False
            )
        )
        if not animal:
            raise AppException(404, "Referenced animal not found", "Not Found")

        if (
            current_user.role != RoleEnum.ADMIN.value
            and animal.client_account_id != current_user.id
        ):
            raise AppException(403, "Access to clinical record is forbidden", "Forbidden")

        return rec

    @staticmethod
    def patch_record(
        db: Session,
        current_user: UserModel,
        record_id: int,
        payload: ClinicalRecordPatchRequest,
    ) -> ClinicalRecordModel:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Only admins may update clinical records", "Forbidden")

        rec = ClinicalRecordController.get_record(db, current_user, record_id)

        if rec.status == ClinicalRecordStatusEnum.FINAL.value:
            raise AppException(
                409, "Cannot modify a FINAL clinical record", "Conflict"
            )

        fields_set = payload.model_dump(exclude_unset=True)
        if not fields_set:
            raise AppException(400, "At least one field must be updated", "Bad Request")

        if payload.record_type is not None:
            rec.record_type = payload.record_type.value
        if payload.occurred_on is not None:
            rec.occurred_on = payload.occurred_on
        if payload.summary is not None:
            rec.summary = payload.summary
        if payload.diagnosis_code is not None:
            rec.diagnosis_code = payload.diagnosis_code

        db.commit()
        db.refresh(rec)
        return rec

    @staticmethod
    def finalize_record(
        db: Session,
        current_user: UserModel,
        record_id: int,
        payload: ClinicalRecordFinalizeRequest,
    ) -> ClinicalRecordModel:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Only admins may finalize clinical records", "Forbidden")

        rec = ClinicalRecordController.get_record(db, current_user, record_id)

        if rec.status == ClinicalRecordStatusEnum.FINAL.value:
            raise AppException(
                409, "Clinical record is already finalized", "Conflict"
            )

        rec.status = ClinicalRecordStatusEnum.FINAL.value
        rec.finalized_at = payload.finalized_at
        rec.finalized_by = current_user.id
        db.commit()
        db.refresh(rec)
        return rec

    @staticmethod
    def delete_record(db: Session, current_user: UserModel, record_id: int):
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Only admins may delete clinical records", "Forbidden")

        if record_id <= 0:
            raise AppException(404, f"Clinical record {record_id} not found", "Not Found")

        rec = db.scalar(
            select(ClinicalRecordModel).where(ClinicalRecordModel.id == record_id)
        )
        if not rec:
            raise AppException(404, f"Clinical record {record_id} not found", "Not Found")

        if rec.status == ClinicalRecordStatusEnum.FINAL.value:
            raise AppException(
                409, "Cannot delete a FINAL clinical record", "Conflict"
            )

        db.delete(rec)
        db.commit()


class VaccinationController:
    @staticmethod
    def list_vaccinations(
        db: Session,
        current_user: UserModel,
        animal_id: Optional[int],
        vaccine_code: Optional[VaccineCodeEnum],
        administered_from: Optional[date],
        administered_to: Optional[date],
        expires_from: Optional[date],
        expires_to: Optional[date],
        include_void: bool,
        limit: int,
        after_id: Optional[int],
    ) -> VaccinationListResponse:
        if include_void and current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "include_void permitted only to admins", "Forbidden")

        query = select(VaccinationModel).join(
            AnimalModel, VaccinationModel.animal_id == AnimalModel.id
        ).where(AnimalModel.is_deleted == False)

        if current_user.role != RoleEnum.ADMIN.value:
            query = query.where(
                AnimalModel.client_account_id == current_user.id,
                VaccinationModel.is_void == False,
            )
        elif not include_void:
            query = query.where(VaccinationModel.is_void == False)

        if animal_id is not None:
            if animal_id <= 0:
                raise AppException(404, f"Animal {animal_id} not found", "Not Found")
            animal = db.scalar(
                select(AnimalModel).where(
                    AnimalModel.id == animal_id, AnimalModel.is_deleted == False
                )
            )
            if not animal:
                raise AppException(404, f"Animal {animal_id} not found", "Not Found")
            if (
                current_user.role != RoleEnum.ADMIN.value
                and animal.client_account_id != current_user.id
            ):
                raise AppException(403, "Access to animal vaccinations is forbidden", "Forbidden")
            query = query.where(VaccinationModel.animal_id == animal_id)

        if vaccine_code is not None:
            query = query.where(VaccinationModel.vaccine_code == vaccine_code.value)

        if administered_from is not None and administered_to is not None:
            if administered_from > administered_to:
                raise AppException(400, "administered_from cannot be later than administered_to", "Bad Request")

        if administered_from is not None:
            query = query.where(VaccinationModel.administered_on >= administered_from)

        if administered_to is not None:
            query = query.where(VaccinationModel.administered_on <= administered_to)

        if expires_from is not None and expires_to is not None:
            if expires_from > expires_to:
                raise AppException(400, "expires_from cannot be later than expires_to", "Bad Request")

        if expires_from is not None:
            query = query.where(VaccinationModel.expires_on >= expires_from)

        if expires_to is not None:
            query = query.where(VaccinationModel.expires_on <= expires_to)

        if after_id is not None:
            if after_id <= 0:
                raise AppException(400, "after_id must be positive", "Bad Request")
            query = query.where(VaccinationModel.id > after_id)

        query = query.order_by(VaccinationModel.id.asc()).limit(limit + 1)
        rows = list(db.scalars(query).all())

        next_after_id = None
        if len(rows) > limit:
            items = rows[:limit]
            next_after_id = items[-1].id
        else:
            items = rows

        return VaccinationListResponse(
            items=[VaccinationResponse.model_validate(v) for v in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_vaccination(
        db: Session,
        current_user: UserModel,
        animal_id: int,
        payload: VaccinationCreateRequest,
    ) -> VaccinationModel:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Only admins may create vaccinations", "Forbidden")

        if animal_id <= 0:
            raise AppException(404, f"Animal {animal_id} not found", "Not Found")

        animal = db.scalar(
            select(AnimalModel).where(
                AnimalModel.id == animal_id, AnimalModel.is_deleted == False
            )
        )
        if not animal:
            raise AppException(404, f"Animal {animal_id} not found", "Not Found")

        # REQ-3-005: expires_on strictly later than administered_on and no more than 1095 calendar days later
        if payload.expires_on <= payload.administered_on:
            raise AppException(
                422, "expires_on must be strictly later than administered_on", "Unprocessable Entity"
            )

        days_diff = (payload.expires_on - payload.administered_on).days
        if days_diff > 1095:
            raise AppException(
                422,
                f"expires_on cannot be more than 1095 calendar days later than administered_on, got {days_diff} days",
                "Unprocessable Entity",
            )

        # REQ-3-006: For a non-void vaccination, (animal_id, vaccine_code, administered_on) is unique
        dup = db.scalar(
            select(VaccinationModel).where(
                VaccinationModel.animal_id == animal_id,
                VaccinationModel.vaccine_code == payload.vaccine_code.value,
                VaccinationModel.administered_on == payload.administered_on,
                VaccinationModel.is_void == False,
            )
        )
        if dup:
            raise AppException(
                409,
                f"Active vaccination for {payload.vaccine_code.value} on {payload.administered_on.isoformat()} already exists",
                "Conflict",
            )

        vac = VaccinationModel(
            animal_id=animal_id,
            vaccine_code=payload.vaccine_code.value,
            vaccine_name=payload.vaccine_name,
            administered_on=payload.administered_on,
            expires_on=payload.expires_on,
            batch_number=payload.batch_number,
            is_void=False,
            voided_on=None,
            void_reason=None,
            voided_by=None,
        )
        db.add(vac)
        db.commit()
        db.refresh(vac)
        return vac

    @staticmethod
    def get_vaccination(
        db: Session, current_user: UserModel, vaccination_id: int
    ) -> VaccinationModel:
        if vaccination_id <= 0:
            raise AppException(404, f"Vaccination {vaccination_id} not found", "Not Found")

        vac = db.scalar(
            select(VaccinationModel).where(VaccinationModel.id == vaccination_id)
        )
        if not vac:
            raise AppException(404, f"Vaccination {vaccination_id} not found", "Not Found")

        animal = db.scalar(
            select(AnimalModel).where(
                AnimalModel.id == vac.animal_id, AnimalModel.is_deleted == False
            )
        )
        if not animal:
            raise AppException(404, "Referenced animal not found", "Not Found")

        if (
            current_user.role != RoleEnum.ADMIN.value
            and animal.client_account_id != current_user.id
        ):
            raise AppException(403, "Access to vaccination is forbidden", "Forbidden")

        return vac

    @staticmethod
    def void_vaccination(
        db: Session,
        current_user: UserModel,
        vaccination_id: int,
        payload: VaccinationVoidRequest,
    ) -> VaccinationModel:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Only admins may void vaccinations", "Forbidden")

        vac = VaccinationController.get_vaccination(db, current_user, vaccination_id)

        if vac.is_void:
            raise AppException(409, "Vaccination is already void", "Conflict")

        if payload.voided_on < vac.administered_on:
            raise AppException(
                409,
                f"voided_on ({payload.voided_on.isoformat()}) cannot precede administered_on ({vac.administered_on.isoformat()})",
                "Conflict",
            )

        vac.is_void = True
        vac.voided_on = payload.voided_on
        vac.void_reason = payload.void_reason
        vac.voided_by = current_user.id
        db.commit()
        db.refresh(vac)
        return vac

    @staticmethod
    def get_vaccination_status(
        db: Session,
        current_user: UserModel,
        animal_id: int,
        as_of: date,
        requested_code: Optional[VaccineCodeEnum],
    ) -> VaccinationStatusResponse:
        AnimalController.get_animal(db, current_user, animal_id)

        # Codes to check
        codes = (
            [requested_code]
            if requested_code is not None
            else [c for c in VaccineCodeEnum]
        )

        query = select(VaccinationModel).where(
            VaccinationModel.animal_id == animal_id,
            VaccinationModel.is_void == False,
        )
        vacs = list(db.scalars(query).all())

        status_items: List[VaccineStatusItem] = []
        for code in codes:
            code_vacs = [v for v in vacs if v.vaccine_code == code.value]
            if not code_vacs:
                status_items.append(
                    VaccineStatusItem(
                        vaccine_code=code,
                        status=VaccinationStatusEnum.NOT_RECORDED,
                        administered_on=None,
                        expires_on=None,
                    )
                )
                continue

            # Sort by administered_on descending to find most relevant
            code_vacs.sort(key=lambda x: (x.administered_on, x.id), reverse=True)
            # Find if any non-void event is CURRENT: administered_on <= as_of and expires_on > as_of
            current_event = next(
                (v for v in code_vacs if v.administered_on <= as_of and v.expires_on > as_of),
                None,
            )
            if current_event:
                status_items.append(
                    VaccineStatusItem(
                        vaccine_code=code,
                        status=VaccinationStatusEnum.CURRENT,
                        administered_on=current_event.administered_on,
                        expires_on=current_event.expires_on,
                    )
                )
            else:
                # Expired event: expires_on <= as_of
                expired_event = next(
                    (v for v in code_vacs if v.expires_on <= as_of),
                    None,
                )
                if expired_event:
                    status_items.append(
                        VaccineStatusItem(
                            vaccine_code=code,
                            status=VaccinationStatusEnum.EXPIRED,
                            administered_on=expired_event.administered_on,
                            expires_on=expired_event.expires_on,
                        )
                    )
                else:
                    status_items.append(
                        VaccineStatusItem(
                            vaccine_code=code,
                            status=VaccinationStatusEnum.NOT_RECORDED,
                            administered_on=None,
                            expires_on=None,
                        )
                    )

        return VaccinationStatusResponse(
            animal_id=animal_id,
            as_of=as_of,
            statuses=status_items,
        )

    @staticmethod
    def get_clinical_timeline(
        db: Session,
        current_user: UserModel,
        animal_id: int,
        event_from: Optional[date],
        event_to: Optional[date],
        include_void: bool,
        limit: int,
        after_event_date: Optional[date],
        after_event_kind: Optional[TimelineEventKindEnum],
        after_event_id: Optional[int],
    ) -> ClinicalTimelineResponse:
        AnimalController.get_animal(db, current_user, animal_id)

        if include_void and current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "include_void permitted only to admins", "Forbidden")

        # Validate three-part cursor: must be supplied together or omitted together
        cursor_parts = [after_event_date, after_event_kind, after_event_id]
        provided_parts = [p is not None for p in cursor_parts]
        if any(provided_parts) and not all(provided_parts):
            raise AppException(
                400,
                "after_event_date, after_event_kind, and after_event_id must be supplied together or omitted together",
                "Bad Request",
            )

        if after_event_id is not None and after_event_id <= 0:
            raise AppException(400, "after_event_id must be positive", "Bad Request")

        if event_from is not None and event_to is not None and event_from > event_to:
            raise AppException(400, "event_from cannot be later than event_to", "Bad Request")

        # 1. Fetch clinical records
        q_rec = select(ClinicalRecordModel).where(ClinicalRecordModel.animal_id == animal_id)
        if event_from is not None:
            q_rec = q_rec.where(ClinicalRecordModel.occurred_on >= event_from)
        if event_to is not None:
            q_rec = q_rec.where(ClinicalRecordModel.occurred_on <= event_to)
        recs = list(db.scalars(q_rec).all())

        # 2. Fetch vaccinations
        q_vac = select(VaccinationModel).where(VaccinationModel.animal_id == animal_id)
        if not include_void:
            q_vac = q_vac.where(VaccinationModel.is_void == False)
        if event_from is not None:
            q_vac = q_vac.where(VaccinationModel.administered_on >= event_from)
        if event_to is not None:
            q_vac = q_vac.where(VaccinationModel.administered_on <= event_to)
        vacs = list(db.scalars(q_vac).all())

        # 3. Merge into timeline items
        timeline: List[TimelineEventItem] = []
        for r in recs:
            timeline.append(
                TimelineEventItem(
                    event_id=r.id,
                    event_date=r.occurred_on,
                    event_kind=TimelineEventKindEnum.RECORD,
                    details={
                        "record_type": r.record_type,
                        "summary": r.summary,
                        "diagnosis_code": r.diagnosis_code,
                        "status": r.status,
                        "finalized_at": r.finalized_at.isoformat() if r.finalized_at else None,
                    },
                )
            )

        for v in vacs:
            timeline.append(
                TimelineEventItem(
                    event_id=v.id,
                    event_date=v.administered_on,
                    event_kind=TimelineEventKindEnum.VACCINATION,
                    details={
                        "vaccine_code": v.vaccine_code,
                        "vaccine_name": v.vaccine_name,
                        "expires_on": v.expires_on.isoformat(),
                        "batch_number": v.batch_number,
                        "is_void": v.is_void,
                        "void_reason": v.void_reason,
                    },
                )
            )

        # Sort order: event_date ascending, RECORD before VACCINATION, then event_id ascending
        def sort_key(item: TimelineEventItem):
            kind_rank = 0 if item.event_kind == TimelineEventKindEnum.RECORD else 1
            return (item.event_date, kind_rank, item.event_id)

        timeline.sort(key=sort_key)

        # Apply cursor if present
        if all(provided_parts):
            cursor_rank = 0 if after_event_kind == TimelineEventKindEnum.RECORD else 1
            cursor_tuple = (after_event_date, cursor_rank, after_event_id)
            timeline = [item for item in timeline if sort_key(item) > cursor_tuple]

        # Apply limit + next cursor
        next_date = None
        next_kind = None
        next_id = None
        if len(timeline) > limit:
            items = timeline[:limit]
            last = items[-1]
            next_date = last.event_date
            next_kind = last.event_kind
            next_id = last.event_id
        else:
            items = timeline

        return ClinicalTimelineResponse(
            items=items,
            limit=limit,
            next_after_event_date=next_date,
            next_after_event_kind=next_kind,
            next_after_event_id=next_id,
        )


# ============================================================================
# FastAPI Application & Routes
# ============================================================================
app = FastAPI(
    title="Veterinary Clinic - Clinical Records, Vaccinations, and Temporal Filters",
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


# 2. GET /animals
@app.get("/animals", tags=["Animals"], response_model=AnimalListResponse)
def list_animals(
    species: Optional[SpeciesEnum] = None,
    active: Optional[bool] = None,
    client_account_id: Optional[int] = Query(None, gt=0),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return AnimalController.list_animals(
        db, current_user, species, active, client_account_id, limit, after_id
    )


# 3. POST /animals
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


# 4. GET /animals/{animal_id}
@app.get("/animals/{animal_id}", tags=["Animals"], response_model=AnimalResponse)
def get_animal(
    animal_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    animal = AnimalController.get_animal(db, current_user, animal_id)
    return AnimalResponse.model_validate(animal)


# 5. PATCH /animals/{animal_id}
@app.patch("/animals/{animal_id}", tags=["Animals"], response_model=AnimalResponse)
def patch_animal(
    animal_id: int,
    payload: AnimalPatchRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    animal = AnimalController.patch_animal(db, current_user, animal_id, payload)
    return AnimalResponse.model_validate(animal)


# 6. DELETE /animals/{animal_id}
@app.delete(
    "/animals/{animal_id}",
    tags=["Animals"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_animal(
    animal_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    AnimalController.delete_animal(db, current_user, animal_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 7. GET /clinical-records
@app.get(
    "/clinical-records",
    tags=["Clinical Records"],
    response_model=ClinicalRecordListResponse,
)
def list_clinical_records(
    animal_id: Optional[int] = Query(None, gt=0),
    record_type: Optional[RecordTypeEnum] = None,
    record_status: Optional[ClinicalRecordStatusEnum] = Query(None, alias="status"),
    occurred_from: Optional[date] = None,
    occurred_to: Optional[date] = None,
    diagnosis_code_prefix: Optional[str] = Query(None, min_length=1, max_length=20),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ClinicalRecordController.list_records(
        db,
        current_user,
        animal_id,
        record_type,
        record_status,
        occurred_from,
        occurred_to,
        diagnosis_code_prefix,
        limit,
        after_id,
    )


# 8. POST /animals/{animal_id}/clinical-records
@app.post(
    "/animals/{animal_id}/clinical-records",
    tags=["Clinical Records"],
    response_model=ClinicalRecordResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_clinical_record(
    animal_id: int,
    payload: ClinicalRecordCreateRequest,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rec = ClinicalRecordController.create_record(
        db, current_user, animal_id, payload
    )
    response.headers["Location"] = f"/clinical-records/{rec.id}"
    return ClinicalRecordResponse.model_validate(rec)


# 9. GET /clinical-records/{record_id}
@app.get(
    "/clinical-records/{record_id}",
    tags=["Clinical Records"],
    response_model=ClinicalRecordResponse,
)
def get_clinical_record(
    record_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rec = ClinicalRecordController.get_record(db, current_user, record_id)
    return ClinicalRecordResponse.model_validate(rec)


# 10. PATCH /clinical-records/{record_id}
@app.patch(
    "/clinical-records/{record_id}",
    tags=["Clinical Records"],
    response_model=ClinicalRecordResponse,
)
def patch_clinical_record(
    record_id: int,
    payload: ClinicalRecordPatchRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rec = ClinicalRecordController.patch_record(
        db, current_user, record_id, payload
    )
    return ClinicalRecordResponse.model_validate(rec)


# 11. POST /clinical-records/{record_id}/finalize
@app.post(
    "/clinical-records/{record_id}/finalize",
    tags=["Clinical Records"],
    response_model=ClinicalRecordResponse,
)
def finalize_clinical_record(
    record_id: int,
    payload: ClinicalRecordFinalizeRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rec = ClinicalRecordController.finalize_record(
        db, current_user, record_id, payload
    )
    return ClinicalRecordResponse.model_validate(rec)


# 12. DELETE /clinical-records/{record_id}
@app.delete(
    "/clinical-records/{record_id}",
    tags=["Clinical Records"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_clinical_record(
    record_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ClinicalRecordController.delete_record(db, current_user, record_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 13. GET /vaccinations
@app.get(
    "/vaccinations",
    tags=["Vaccinations"],
    response_model=VaccinationListResponse,
)
def list_vaccinations(
    animal_id: Optional[int] = Query(None, gt=0),
    vaccine_code: Optional[VaccineCodeEnum] = None,
    administered_from: Optional[date] = None,
    administered_to: Optional[date] = None,
    expires_from: Optional[date] = None,
    expires_to: Optional[date] = None,
    include_void: bool = Query(False),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return VaccinationController.list_vaccinations(
        db,
        current_user,
        animal_id,
        vaccine_code,
        administered_from,
        administered_to,
        expires_from,
        expires_to,
        include_void,
        limit,
        after_id,
    )


# 14. POST /animals/{animal_id}/vaccinations
@app.post(
    "/animals/{animal_id}/vaccinations",
    tags=["Vaccinations"],
    response_model=VaccinationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_vaccination(
    animal_id: int,
    payload: VaccinationCreateRequest,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    vac = VaccinationController.create_vaccination(
        db, current_user, animal_id, payload
    )
    response.headers["Location"] = f"/vaccinations/{vac.id}"
    return VaccinationResponse.model_validate(vac)


# 15. GET /vaccinations/{vaccination_id}
@app.get(
    "/vaccinations/{vaccination_id}",
    tags=["Vaccinations"],
    response_model=VaccinationResponse,
)
def get_vaccination(
    vaccination_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    vac = VaccinationController.get_vaccination(db, current_user, vaccination_id)
    return VaccinationResponse.model_validate(vac)


# 16. POST /vaccinations/{vaccination_id}/void
@app.post(
    "/vaccinations/{vaccination_id}/void",
    tags=["Vaccinations"],
    response_model=VaccinationResponse,
)
def void_vaccination(
    vaccination_id: int,
    payload: VaccinationVoidRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    vac = VaccinationController.void_vaccination(
        db, current_user, vaccination_id, payload
    )
    return VaccinationResponse.model_validate(vac)


# 17. GET /animals/{animal_id}/vaccination-status
@app.get(
    "/animals/{animal_id}/vaccination-status",
    tags=["Vaccinations"],
    response_model=VaccinationStatusResponse,
)
def get_vaccination_status(
    animal_id: int,
    as_of: date = Query(..., description="Evaluation date (YYYY-MM-DD)"),
    vaccine_code: Optional[VaccineCodeEnum] = None,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return VaccinationController.get_vaccination_status(
        db, current_user, animal_id, as_of, vaccine_code
    )


# 18. GET /animals/{animal_id}/clinical-timeline
@app.get(
    "/animals/{animal_id}/clinical-timeline",
    tags=["Clinical Timeline"],
    response_model=ClinicalTimelineResponse,
)
def get_clinical_timeline(
    animal_id: int,
    event_from: Optional[date] = None,
    event_to: Optional[date] = None,
    include_void: bool = Query(False),
    limit: int = Query(20, ge=1, le=100),
    after_event_date: Optional[date] = None,
    after_event_kind: Optional[TimelineEventKindEnum] = None,
    after_event_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return VaccinationController.get_clinical_timeline(
        db,
        current_user,
        animal_id,
        event_from,
        event_to,
        include_void,
        limit,
        after_event_date,
        after_event_kind,
        after_event_id,
    )
