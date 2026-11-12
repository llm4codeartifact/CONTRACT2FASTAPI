"""
Veterinary Clinic - Variant B: Appointments, rooms, and scheduling conflicts
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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    Boolean,
    Column,
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
logger = logging.getLogger("vet_appointments")
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

MIN_TIMESTAMP = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
MAX_TIMESTAMP = datetime(2035, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


# ============================================================================
# Enums
# ============================================================================
class RoleEnum(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


class RoomTypeEnum(str, enum.Enum):
    CONSULTATION = "CONSULTATION"
    SURGERY = "SURGERY"
    IMAGING = "IMAGING"


class SpeciesEnum(str, enum.Enum):
    DOG = "DOG"
    CAT = "CAT"
    RABBIT = "RABBIT"
    BIRD = "BIRD"
    OTHER = "OTHER"


class AppointmentStatusEnum(str, enum.Enum):
    SCHEDULED = "SCHEDULED"
    CHECKED_IN = "CHECKED_IN"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


# ============================================================================
# SQLAlchemy Models
# ============================================================================
class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    role = Column(String(20), nullable=False)  # user or admin
    token = Column(String(100), unique=True, nullable=False)


class RoomModel(Base):
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(60), nullable=False)
    room_type = Column(String(20), nullable=False)
    capacity = Column(Integer, nullable=False)
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


class PatientAnimalModel(Base):
    __tablename__ = "patient_animals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_account_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(80), nullable=False)
    species = Column(String(20), nullable=False)
    active = Column(Boolean, default=True, nullable=False)
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


class AppointmentModel(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False, index=True)
    animal_id = Column(
        Integer, ForeignKey("patient_animals.id"), nullable=False, index=True
    )
    reason = Column(String(250), nullable=False)
    start_at = Column(DateTime(timezone=True), nullable=False, index=True)
    end_at = Column(DateTime(timezone=True), nullable=False, index=True)
    status = Column(
        String(20),
        nullable=False,
        default=AppointmentStatusEnum.SCHEDULED.value,
        index=True,
    )
    checked_in_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancellation_reason = Column(String(500), nullable=True)
    cancelled_by_account_id = Column(Integer, ForeignKey("users.id"), nullable=True)
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


# ============================================================================
# Pydantic Schemas
# ============================================================================
class AppException(HTTPException):
    def __init__(self, status_code: int, detail: str, error_type: str = "Error"):
        super().__init__(status_code=status_code, detail=detail)
        self.error_type = error_type


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


def validate_five_minute_boundary(dt: datetime, name: str = "timestamp"):
    if dt.minute % 5 != 0 or dt.second != 0 or dt.microsecond != 0:
        raise ValueError(f"{name} must be on a 5-minute UTC boundary (e.g. :00, :05, :10, etc.)")


def to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)



class RoomCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Room name")
    room_type: RoomTypeEnum = Field(..., description="Room type")
    capacity: int = Field(..., ge=1, le=20, description="Capacity 1..20")
    active: Optional[bool] = Field(default=True, description="Active status")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not (2 <= len(v) <= 60):
            raise ValueError("name must be between 2 and 60 characters")
        return v


class RoomPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    room_type: Optional[RoomTypeEnum] = None
    capacity: Optional[int] = Field(default=None, ge=1, le=20)
    active: Optional[bool] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (2 <= len(v) <= 60):
            raise ValueError("name must be between 2 and 60 characters")
        return v


class RoomResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    name: str
    room_type: RoomTypeEnum
    capacity: int
    active: bool
    is_deleted: bool
    created_at: datetime
    updated_at: datetime


class RoomListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[RoomResponse]
    limit: int
    next_after_id: Optional[int] = None


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


class AppointmentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    animal_id: int = Field(..., gt=0, description="Patient animal ID")
    room_id: int = Field(..., gt=0, description="Room ID")
    reason: str = Field(..., description="Reason for appointment")
    start_at: datetime = Field(..., description="Start timestamp (UTC ISO-8601)")
    end_at: datetime = Field(..., description="End timestamp (UTC ISO-8601)")

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        v = v.strip()
        if not (3 <= len(v) <= 250):
            raise ValueError("reason must be between 3 and 250 characters")
        return v

    @field_validator("start_at", mode="before")
    @classmethod
    def validate_start_at(cls, v: Any) -> datetime:
        dt = parse_and_validate_timestamp(v)
        validate_five_minute_boundary(dt, "start_at")
        return dt

    @field_validator("end_at", mode="before")
    @classmethod
    def validate_end_at(cls, v: Any) -> datetime:
        dt = parse_and_validate_timestamp(v)
        validate_five_minute_boundary(dt, "end_at")
        return dt


class AppointmentPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Optional[str] = None
    room_id: Optional[int] = Field(default=None, gt=0)
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (3 <= len(v) <= 250):
            raise ValueError("reason must be between 3 and 250 characters")
        return v

    @field_validator("start_at", mode="before")
    @classmethod
    def validate_start_at(cls, v: Any) -> Optional[datetime]:
        if v is None:
            return None
        dt = parse_and_validate_timestamp(v)
        validate_five_minute_boundary(dt, "start_at")
        return dt

    @field_validator("end_at", mode="before")
    @classmethod
    def validate_end_at(cls, v: Any) -> Optional[datetime]:
        if v is None:
            return None
        dt = parse_and_validate_timestamp(v)
        validate_five_minute_boundary(dt, "end_at")
        return dt


class AppointmentCheckInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checked_in_at: datetime = Field(..., description="Check-in timestamp")

    @field_validator("checked_in_at", mode="before")
    @classmethod
    def validate_checked_in_at(cls, v: Any) -> datetime:
        return parse_and_validate_timestamp(v)


class AppointmentCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completed_at: datetime = Field(..., description="Completion timestamp")

    @field_validator("completed_at", mode="before")
    @classmethod
    def validate_completed_at(cls, v: Any) -> datetime:
        return parse_and_validate_timestamp(v)


class AppointmentCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cancelled_at: datetime = Field(..., description="Cancellation timestamp")
    cancellation_reason: str = Field(..., description="Reason for cancellation")

    @field_validator("cancelled_at", mode="before")
    @classmethod
    def validate_cancelled_at(cls, v: Any) -> datetime:
        return parse_and_validate_timestamp(v)

    @field_validator("cancellation_reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        v = v.strip()
        if not (3 <= len(v) <= 500):
            raise ValueError("cancellation_reason must be between 3 and 500 characters")
        return v


class AppointmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    room_id: int
    animal_id: int
    reason: str
    start_at: datetime
    end_at: datetime
    status: AppointmentStatusEnum
    checked_in_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    cancellation_reason: Optional[str] = None
    cancelled_by_account_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class AppointmentListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[AppointmentResponse]
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

            # Active Patient Animals
            a1 = PatientAnimalModel(
                id=1,
                client_account_id=1,
                name="Bella",
                species=SpeciesEnum.DOG.value,
                active=True,
            )
            a2 = PatientAnimalModel(
                id=2,
                client_account_id=2,
                name="Luna",
                species=SpeciesEnum.CAT.value,
                active=True,
            )
            db.add_all([a1, a2])
            db.flush()

            # Active Rooms of each allowed room_type
            r1 = RoomModel(
                id=1,
                name="Exam Room 1",
                room_type=RoomTypeEnum.CONSULTATION.value,
                capacity=4,
                active=True,
                is_deleted=False,
            )
            r2 = RoomModel(
                id=2,
                name="Surgery Suite",
                room_type=RoomTypeEnum.SURGERY.value,
                capacity=6,
                active=True,
                is_deleted=False,
            )
            r3 = RoomModel(
                id=3,
                name="X-Ray Imaging Room",
                room_type=RoomTypeEnum.IMAGING.value,
                capacity=3,
                active=True,
                is_deleted=False,
            )
            db.add_all([r1, r2, r3])
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
# Helpers: Conflict Checking
# ============================================================================
def check_overlap_conflicts(
    db: Session,
    room_id: int,
    animal_id: int,
    start_at: datetime,
    end_at: datetime,
    exclude_appointment_id: Optional[int] = None,
):
    """
    Check if room or patient animal has overlapping SCHEDULED or CHECKED_IN appointments.
    Intervals are half-open: [start_at, end_at). Overlap iff s1 < e2 and s2 < e1.
    """
    active_statuses = [
        AppointmentStatusEnum.SCHEDULED.value,
        AppointmentStatusEnum.CHECKED_IN.value,
    ]
    s_req = to_utc(start_at)
    e_req = to_utc(end_at)

    # Room conflict
    q_room = select(AppointmentModel).where(
        AppointmentModel.room_id == room_id,
        AppointmentModel.status.in_(active_statuses),
    )
    if exclude_appointment_id is not None:
        q_room = q_room.where(AppointmentModel.id != exclude_appointment_id)
    for appt in db.scalars(q_room).all():
        s_exist = to_utc(appt.start_at)
        e_exist = to_utc(appt.end_at)
        if s_req < e_exist and s_exist < e_req:
            raise AppException(
                409,
                f"Room {room_id} has a conflicting appointment (ID: {appt.id}) during requested time interval",
                "Conflict",
            )

    # Animal conflict
    q_animal = select(AppointmentModel).where(
        AppointmentModel.animal_id == animal_id,
        AppointmentModel.status.in_(active_statuses),
    )
    if exclude_appointment_id is not None:
        q_animal = q_animal.where(AppointmentModel.id != exclude_appointment_id)
    for appt in db.scalars(q_animal).all():
        s_exist = to_utc(appt.start_at)
        e_exist = to_utc(appt.end_at)
        if s_req < e_exist and s_exist < e_req:
            raise AppException(
                409,
                f"Patient animal {animal_id} has a conflicting appointment (ID: {appt.id}) during requested time interval",
                "Conflict",
            )



def validate_appointment_interval(start_at: datetime, end_at: datetime):
    if end_at <= start_at:
        raise AppException(422, "end_at must be strictly later than start_at", "Unprocessable Entity")
    duration_minutes = (end_at - start_at).total_seconds() / 60
    if not (15 <= duration_minutes <= 240):
        raise AppException(
            422,
            f"Appointment duration must be between 15 and 240 minutes, got {int(duration_minutes)} minutes",
            "Unprocessable Entity",
        )


# ============================================================================
# Controllers (Static MVC)
# ============================================================================
class RoomController:
    @staticmethod
    def list_rooms(
        db: Session,
        current_user: UserModel,
        room_type: Optional[RoomTypeEnum],
        active: Optional[bool],
        include_deleted: bool,
        name_prefix: Optional[str],
        limit: int,
        after_id: Optional[int],
    ) -> RoomListResponse:
        if include_deleted and current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "include_deleted permitted only to admins", "Forbidden")

        query = select(RoomModel)
        if not include_deleted:
            query = query.where(RoomModel.is_deleted == False)

        if room_type is not None:
            query = query.where(RoomModel.room_type == room_type.value)

        if active is not None:
            query = query.where(RoomModel.active == active)

        if name_prefix is not None:
            prefix = name_prefix.strip()
            if not (1 <= len(prefix) <= 60):
                raise AppException(400, "name_prefix must be 1..60 characters", "Bad Request")
            query = query.where(RoomModel.name.startswith(prefix))

        if after_id is not None:
            if after_id <= 0:
                raise AppException(400, "after_id must be positive", "Bad Request")
            query = query.where(RoomModel.id > after_id)

        query = query.order_by(RoomModel.id.asc()).limit(limit + 1)
        rows = list(db.scalars(query).all())

        next_after_id = None
        if len(rows) > limit:
            items = rows[:limit]
            next_after_id = items[-1].id
        else:
            items = rows

        return RoomListResponse(
            items=[RoomResponse.model_validate(r) for r in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_room(
        db: Session, current_user: UserModel, payload: RoomCreateRequest
    ) -> RoomModel:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Only admins may manage rooms", "Forbidden")

        existing = db.scalar(
            select(RoomModel).where(
                RoomModel.name == payload.name, RoomModel.is_deleted == False
            )
        )
        if existing:
            raise AppException(409, f"Room name '{payload.name}' already exists", "Conflict")

        room = RoomModel(
            name=payload.name,
            room_type=payload.room_type.value,
            capacity=payload.capacity,
            active=payload.active if payload.active is not None else True,
            is_deleted=False,
        )
        db.add(room)
        db.commit()
        db.refresh(room)
        return room

    @staticmethod
    def get_room(db: Session, current_user: UserModel, room_id: int) -> RoomModel:
        if room_id <= 0:
            raise AppException(404, f"Room {room_id} not found", "Not Found")

        room = db.scalar(
            select(RoomModel).where(RoomModel.id == room_id, RoomModel.is_deleted == False)
        )
        if not room:
            raise AppException(404, f"Room {room_id} not found", "Not Found")
        return room

    @staticmethod
    def patch_room(
        db: Session, current_user: UserModel, room_id: int, payload: RoomPatchRequest
    ) -> RoomModel:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Only admins may manage rooms", "Forbidden")

        if room_id <= 0:
            raise AppException(404, f"Room {room_id} not found", "Not Found")

        room = db.scalar(
            select(RoomModel).where(RoomModel.id == room_id, RoomModel.is_deleted == False)
        )
        if not room:
            raise AppException(404, f"Room {room_id} not found", "Not Found")

        fields_set = payload.model_dump(exclude_unset=True)
        if not fields_set:
            raise AppException(400, "At least one field must be updated", "Bad Request")

        if payload.name is not None and payload.name != room.name:
            dup = db.scalar(
                select(RoomModel).where(
                    RoomModel.name == payload.name,
                    RoomModel.id != room_id,
                    RoomModel.is_deleted == False,
                )
            )
            if dup:
                raise AppException(409, f"Room name '{payload.name}' already exists", "Conflict")
            room.name = payload.name

        if payload.room_type is not None:
            room.room_type = payload.room_type.value

        if payload.capacity is not None:
            room.capacity = payload.capacity

        if payload.active is not None:
            if payload.active is False and room.active is True:
                # REQ-3-008: A room may be deactivated only if it has no SCHEDULED or CHECKED_IN appointment
                active_appt = db.scalar(
                    select(AppointmentModel)
                    .where(
                        AppointmentModel.room_id == room_id,
                        AppointmentModel.status.in_(
                            [
                                AppointmentStatusEnum.SCHEDULED.value,
                                AppointmentStatusEnum.CHECKED_IN.value,
                            ]
                        ),
                    )
                    .limit(1)
                )
                if active_appt:
                    raise AppException(
                        409,
                        "Cannot deactivate room while SCHEDULED or CHECKED_IN appointments exist",
                        "Conflict",
                    )
            room.active = payload.active

        db.commit()
        db.refresh(room)
        return room

    @staticmethod
    def delete_room(db: Session, current_user: UserModel, room_id: int):
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Only admins may manage rooms", "Forbidden")

        if room_id <= 0:
            raise AppException(404, f"Room {room_id} not found", "Not Found")

        room = db.scalar(
            select(RoomModel).where(RoomModel.id == room_id, RoomModel.is_deleted == False)
        )
        if not room:
            raise AppException(404, f"Room {room_id} not found", "Not Found")

        # REQ-3-008: A room may be soft-deleted only if no appointment of any status references it
        appt_exists = db.scalar(
            select(AppointmentModel).where(AppointmentModel.room_id == room_id).limit(1)
        )
        if appt_exists:
            raise AppException(
                409,
                "Cannot delete room referenced by appointment records",
                "Conflict",
            )

        room.is_deleted = True
        room.active = False
        db.commit()


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

        query = select(PatientAnimalModel)
        if current_user.role != RoleEnum.ADMIN.value:
            query = query.where(PatientAnimalModel.client_account_id == current_user.id)
        elif client_account_id is not None:
            if client_account_id <= 0:
                raise AppException(400, "client_account_id must be positive", "Bad Request")
            query = query.where(PatientAnimalModel.client_account_id == client_account_id)

        if species is not None:
            query = query.where(PatientAnimalModel.species == species.value)

        if active is not None:
            query = query.where(PatientAnimalModel.active == active)

        if after_id is not None:
            if after_id <= 0:
                raise AppException(400, "after_id must be positive", "Bad Request")
            query = query.where(PatientAnimalModel.id > after_id)

        query = query.order_by(PatientAnimalModel.id.asc()).limit(limit + 1)
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
    ) -> PatientAnimalModel:
        target_client_id = current_user.id
        if payload.client_account_id is not None:
            if current_user.role != RoleEnum.ADMIN.value and payload.client_account_id != current_user.id:
                raise AppException(
                    403, "Only admins may assign animals to other accounts", "Forbidden"
                )
            target_user = db.scalar(
                select(UserModel).where(UserModel.id == payload.client_account_id)
            )
            if not target_user:
                raise AppException(
                    404, f"Target account {payload.client_account_id} not found", "Not Found"
                )
            target_client_id = payload.client_account_id

        animal = PatientAnimalModel(
            client_account_id=target_client_id,
            name=payload.name,
            species=payload.species.value,
            active=payload.active if payload.active is not None else True,
        )
        db.add(animal)
        db.commit()
        db.refresh(animal)
        return animal

    @staticmethod
    def get_animal(
        db: Session, current_user: UserModel, animal_id: int
    ) -> PatientAnimalModel:
        if animal_id <= 0:
            raise AppException(404, f"Animal {animal_id} not found", "Not Found")

        animal = db.scalar(
            select(PatientAnimalModel).where(PatientAnimalModel.id == animal_id)
        )
        if not animal:
            raise AppException(404, f"Animal {animal_id} not found", "Not Found")

        if (
            current_user.role != RoleEnum.ADMIN.value
            and animal.client_account_id != current_user.id
        ):
            raise AppException(403, "Access to animal is forbidden", "Forbidden")

        return animal


class AppointmentController:
    @staticmethod
    def list_appointments(
        db: Session,
        current_user: UserModel,
        animal_id: Optional[int],
        room_id: Optional[int],
        status_val: Optional[AppointmentStatusEnum],
        from_time: Optional[datetime],
        to_time: Optional[datetime],
        limit: int,
        after_id: Optional[int],
    ) -> AppointmentListResponse:
        if room_id is not None and current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "room_id filter permitted only to admins", "Forbidden")

        query = select(AppointmentModel).join(
            PatientAnimalModel, AppointmentModel.animal_id == PatientAnimalModel.id
        )

        if current_user.role != RoleEnum.ADMIN.value:
            query = query.where(PatientAnimalModel.client_account_id == current_user.id)

        if animal_id is not None:
            if animal_id <= 0:
                raise AppException(404, f"Animal {animal_id} not found", "Not Found")
            animal = db.scalar(
                select(PatientAnimalModel).where(PatientAnimalModel.id == animal_id)
            )
            if not animal:
                raise AppException(404, f"Animal {animal_id} not found", "Not Found")
            if (
                current_user.role != RoleEnum.ADMIN.value
                and animal.client_account_id != current_user.id
            ):
                raise AppException(403, "Access to animal appointments is forbidden", "Forbidden")
            query = query.where(AppointmentModel.animal_id == animal_id)

        if room_id is not None:
            if room_id <= 0:
                raise AppException(404, f"Room {room_id} not found", "Not Found")
            room = db.scalar(
                select(RoomModel).where(RoomModel.id == room_id, RoomModel.is_deleted == False)
            )
            if not room:
                raise AppException(404, f"Room {room_id} not found", "Not Found")
            query = query.where(AppointmentModel.room_id == room_id)

        if status_val is not None:
            query = query.where(AppointmentModel.status == status_val.value)

        if from_time is not None and to_time is not None:
            if from_time > to_time:
                raise AppException(400, "from must not be later than to", "Bad Request")

        if from_time is not None:
            query = query.where(AppointmentModel.start_at >= from_time)

        if to_time is not None:
            query = query.where(AppointmentModel.start_at <= to_time)

        if after_id is not None:
            if after_id <= 0:
                raise AppException(400, "after_id must be positive", "Bad Request")
            query = query.where(AppointmentModel.id > after_id)

        query = query.order_by(AppointmentModel.id.asc()).limit(limit + 1)
        rows = list(db.scalars(query).all())

        next_after_id = None
        if len(rows) > limit:
            items = rows[:limit]
            next_after_id = items[-1].id
        else:
            items = rows

        return AppointmentListResponse(
            items=[AppointmentResponse.model_validate(a) for a in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_appointment(
        db: Session, current_user: UserModel, payload: AppointmentCreateRequest
    ) -> AppointmentModel:
        validate_appointment_interval(payload.start_at, payload.end_at)

        animal = db.scalar(
            select(PatientAnimalModel).where(
                PatientAnimalModel.id == payload.animal_id, PatientAnimalModel.active == True
            )
        )
        if not animal:
            raise AppException(404, f"Active animal {payload.animal_id} not found", "Not Found")

        if (
            current_user.role != RoleEnum.ADMIN.value
            and animal.client_account_id != current_user.id
        ):
            raise AppException(403, "Caller does not own this animal", "Forbidden")

        room = db.scalar(
            select(RoomModel).where(
                RoomModel.id == payload.room_id,
                RoomModel.active == True,
                RoomModel.is_deleted == False,
            )
        )
        if not room:
            raise AppException(404, f"Active room {payload.room_id} not found", "Not Found")

        # Conflict check
        check_overlap_conflicts(
            db, payload.room_id, payload.animal_id, payload.start_at, payload.end_at
        )

        appt = AppointmentModel(
            room_id=payload.room_id,
            animal_id=payload.animal_id,
            reason=payload.reason,
            start_at=payload.start_at,
            end_at=payload.end_at,
            status=AppointmentStatusEnum.SCHEDULED.value,
        )
        db.add(appt)
        db.commit()
        db.refresh(appt)
        return appt

    @staticmethod
    def get_appointment(
        db: Session, current_user: UserModel, appointment_id: int
    ) -> AppointmentModel:
        if appointment_id <= 0:
            raise AppException(404, f"Appointment {appointment_id} not found", "Not Found")

        appt = db.scalar(
            select(AppointmentModel).where(AppointmentModel.id == appointment_id)
        )
        if not appt:
            raise AppException(404, f"Appointment {appointment_id} not found", "Not Found")

        animal = db.scalar(
            select(PatientAnimalModel).where(PatientAnimalModel.id == appt.animal_id)
        )
        if not animal:
            raise AppException(404, "Referenced animal not found", "Not Found")

        if (
            current_user.role != RoleEnum.ADMIN.value
            and animal.client_account_id != current_user.id
        ):
            raise AppException(403, "Access to appointment is forbidden", "Forbidden")

        return appt

    @staticmethod
    def patch_appointment(
        db: Session,
        db_user: UserModel,
        appointment_id: int,
        payload: AppointmentPatchRequest,
    ) -> AppointmentModel:
        appt = AppointmentController.get_appointment(db, db_user, appointment_id)

        if appt.status != AppointmentStatusEnum.SCHEDULED.value:
            raise AppException(
                409,
                f"Appointment {appointment_id} is in '{appt.status}' status and cannot be modified",
                "Conflict",
            )

        fields_set = payload.model_dump(exclude_unset=True)
        if not fields_set:
            raise AppException(400, "At least one field must be updated", "Bad Request")

        new_room_id = payload.room_id if payload.room_id is not None else appt.room_id
        new_start_at = payload.start_at if payload.start_at is not None else to_utc(appt.start_at)
        new_end_at = payload.end_at if payload.end_at is not None else to_utc(appt.end_at)
        new_reason = payload.reason if payload.reason is not None else appt.reason

        validate_appointment_interval(new_start_at, new_end_at)

        if new_room_id != appt.room_id:
            room = db.scalar(
                select(RoomModel).where(
                    RoomModel.id == new_room_id,
                    RoomModel.active == True,
                    RoomModel.is_deleted == False,
                )
            )
            if not room:
                raise AppException(404, f"Active room {new_room_id} not found", "Not Found")

        # Conflict check excluding this appointment
        check_overlap_conflicts(
            db,
            new_room_id,
            appt.animal_id,
            new_start_at,
            new_end_at,
            exclude_appointment_id=appt.id,
        )

        appt.room_id = new_room_id
        appt.start_at = new_start_at
        appt.end_at = new_end_at
        appt.reason = new_reason

        db.commit()
        db.refresh(appt)
        return appt

    @staticmethod
    def check_in_appointment(
        db: Session,
        current_user: UserModel,
        appointment_id: int,
        payload: AppointmentCheckInRequest,
    ) -> AppointmentModel:
        appt = AppointmentController.get_appointment(db, current_user, appointment_id)

        if appt.status != AppointmentStatusEnum.SCHEDULED.value:
            raise AppException(
                409,
                f"Appointment must be in SCHEDULED status to check-in, current status is '{appt.status}'",
                "Conflict",
            )

        # checked_in_at must be within planned interval [start_at, end_at]
        start_utc = to_utc(appt.start_at)
        end_utc = to_utc(appt.end_at)
        chk_utc = to_utc(payload.checked_in_at)
        if not (start_utc <= chk_utc <= end_utc):
            raise AppException(
                409,
                f"checked_in_at ({payload.checked_in_at.isoformat()}) must be within planned interval [{start_utc.isoformat()}, {end_utc.isoformat()}]",
                "Conflict",
            )

        appt.status = AppointmentStatusEnum.CHECKED_IN.value
        appt.checked_in_at = payload.checked_in_at
        db.commit()
        db.refresh(appt)
        return appt

    @staticmethod
    def complete_appointment(
        db: Session,
        current_user: UserModel,
        appointment_id: int,
        payload: AppointmentCompleteRequest,
    ) -> AppointmentModel:
        appt = AppointmentController.get_appointment(db, current_user, appointment_id)

        if appt.status != AppointmentStatusEnum.CHECKED_IN.value:
            raise AppException(
                409,
                f"Appointment must be in CHECKED_IN status to complete, current status is '{appt.status}'",
                "Conflict",
            )

        if appt.checked_in_at:
            chk_utc = to_utc(appt.checked_in_at)
            comp_utc = to_utc(payload.completed_at)
            if comp_utc < chk_utc:
                raise AppException(
                    409,
                    f"completed_at ({payload.completed_at.isoformat()}) cannot precede checked_in_at ({chk_utc.isoformat()})",
                    "Conflict",
                )

        appt.status = AppointmentStatusEnum.COMPLETED.value
        appt.completed_at = payload.completed_at
        db.commit()
        db.refresh(appt)
        return appt

    @staticmethod
    def cancel_appointment(
        db: Session,
        current_user: UserModel,
        appointment_id: int,
        payload: AppointmentCancelRequest,
    ) -> AppointmentModel:
        appt = AppointmentController.get_appointment(db, current_user, appointment_id)

        if appt.status in (
            AppointmentStatusEnum.COMPLETED.value,
            AppointmentStatusEnum.CANCELLED.value,
        ):
            raise AppException(
                409,
                f"Appointment is in terminal status '{appt.status}' and cannot be cancelled",
                "Conflict",
            )

        appt.status = AppointmentStatusEnum.CANCELLED.value
        appt.cancelled_at = payload.cancelled_at
        appt.cancellation_reason = payload.cancellation_reason
        appt.cancelled_by_account_id = current_user.id
        db.commit()
        db.refresh(appt)
        return appt

    @staticmethod
    def get_room_conflicts(
        db: Session,
        current_user: UserModel,
        room_id: int,
        start_at: datetime,
        end_at: datetime,
        exclude_appointment_id: Optional[int],
        limit: int,
        after_id: Optional[int],
    ) -> AppointmentListResponse:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Room appointment conflicts view is restricted to admins", "Forbidden")

        if room_id <= 0:
            raise AppException(404, f"Room {room_id} not found", "Not Found")

        room = db.scalar(
            select(RoomModel).where(RoomModel.id == room_id, RoomModel.is_deleted == False)
        )
        if not room:
            raise AppException(404, f"Room {room_id} not found", "Not Found")

        validate_appointment_interval(start_at, end_at)

        query = select(AppointmentModel).where(
            AppointmentModel.room_id == room_id,
            AppointmentModel.status.in_(
                [
                    AppointmentStatusEnum.SCHEDULED.value,
                    AppointmentStatusEnum.CHECKED_IN.value,
                ]
            ),
            AppointmentModel.start_at < end_at,
            AppointmentModel.end_at > start_at,
        )

        if exclude_appointment_id is not None:
            if exclude_appointment_id <= 0:
                raise AppException(400, "exclude_appointment_id must be positive", "Bad Request")
            query = query.where(AppointmentModel.id != exclude_appointment_id)

        if after_id is not None:
            if after_id <= 0:
                raise AppException(400, "after_id must be positive", "Bad Request")
            query = query.where(AppointmentModel.id > after_id)

        query = query.order_by(AppointmentModel.id.asc()).limit(limit + 1)
        rows = list(db.scalars(query).all())

        next_after_id = None
        if len(rows) > limit:
            items = rows[:limit]
            next_after_id = items[-1].id
        else:
            items = rows

        return AppointmentListResponse(
            items=[AppointmentResponse.model_validate(a) for a in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def get_animal_conflicts(
        db: Session,
        current_user: UserModel,
        animal_id: int,
        start_at: datetime,
        end_at: datetime,
        exclude_appointment_id: Optional[int],
        limit: int,
        after_id: Optional[int],
    ) -> AppointmentListResponse:
        if animal_id <= 0:
            raise AppException(404, f"Animal {animal_id} not found", "Not Found")

        animal = db.scalar(
            select(PatientAnimalModel).where(PatientAnimalModel.id == animal_id)
        )
        if not animal:
            raise AppException(404, f"Animal {animal_id} not found", "Not Found")

        if (
            current_user.role != RoleEnum.ADMIN.value
            and animal.client_account_id != current_user.id
        ):
            raise AppException(403, "Access to animal conflicts is forbidden", "Forbidden")

        validate_appointment_interval(start_at, end_at)

        query = select(AppointmentModel).where(
            AppointmentModel.animal_id == animal_id,
            AppointmentModel.status.in_(
                [
                    AppointmentStatusEnum.SCHEDULED.value,
                    AppointmentStatusEnum.CHECKED_IN.value,
                ]
            ),
            AppointmentModel.start_at < end_at,
            AppointmentModel.end_at > start_at,
        )

        if exclude_appointment_id is not None:
            if exclude_appointment_id <= 0:
                raise AppException(400, "exclude_appointment_id must be positive", "Bad Request")
            query = query.where(AppointmentModel.id != exclude_appointment_id)

        if after_id is not None:
            if after_id <= 0:
                raise AppException(400, "after_id must be positive", "Bad Request")
            query = query.where(AppointmentModel.id > after_id)

        query = query.order_by(AppointmentModel.id.asc()).limit(limit + 1)
        rows = list(db.scalars(query).all())

        next_after_id = None
        if len(rows) > limit:
            items = rows[:limit]
            next_after_id = items[-1].id
        else:
            items = rows

        return AppointmentListResponse(
            items=[AppointmentResponse.model_validate(a) for a in items],
            limit=limit,
            next_after_id=next_after_id,
        )


# ============================================================================
# FastAPI Application & Routes
# ============================================================================
app = FastAPI(
    title="Veterinary Clinic - Appointments, Rooms, and Scheduling Conflicts",
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

        # Check route matches to preserve 405 Method Not Allowed with Allow header
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


# 2. GET /rooms
@app.get("/rooms", tags=["Rooms"], response_model=RoomListResponse)
def list_rooms(
    room_type: Optional[RoomTypeEnum] = None,
    active: Optional[bool] = None,
    include_deleted: bool = Query(False),
    name_prefix: Optional[str] = Query(None, min_length=1, max_length=60),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return RoomController.list_rooms(
        db,
        current_user,
        room_type,
        active,
        include_deleted,
        name_prefix,
        limit,
        after_id,
    )


# 3. POST /rooms
@app.post(
    "/rooms",
    tags=["Rooms"],
    response_model=RoomResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_room(
    payload: RoomCreateRequest,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    room = RoomController.create_room(db, current_user, payload)
    response.headers["Location"] = f"/rooms/{room.id}"
    return RoomResponse.model_validate(room)


# 4. GET /rooms/{room_id}
@app.get("/rooms/{room_id}", tags=["Rooms"], response_model=RoomResponse)
def get_room(
    room_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    room = RoomController.get_room(db, current_user, room_id)
    return RoomResponse.model_validate(room)


# 5. PATCH /rooms/{room_id}
@app.patch("/rooms/{room_id}", tags=["Rooms"], response_model=RoomResponse)
def patch_room(
    room_id: int,
    payload: RoomPatchRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    room = RoomController.patch_room(db, current_user, room_id, payload)
    return RoomResponse.model_validate(room)


# 6. DELETE /rooms/{room_id}
@app.delete(
    "/rooms/{room_id}",
    tags=["Rooms"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_room(
    room_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    RoomController.delete_room(db, current_user, room_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 7. GET /animals
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


# 8. POST /animals
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


# 9. GET /animals/{animal_id}
@app.get("/animals/{animal_id}", tags=["Animals"], response_model=AnimalResponse)
def get_animal(
    animal_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    animal = AnimalController.get_animal(db, current_user, animal_id)
    return AnimalResponse.model_validate(animal)


# 10. GET /appointments
@app.get("/appointments", tags=["Appointments"], response_model=AppointmentListResponse)
def list_appointments(
    animal_id: Optional[int] = Query(None, gt=0),
    room_id: Optional[int] = Query(None, gt=0),
    appt_status: Optional[AppointmentStatusEnum] = Query(None, alias="status"),
    from_time: Optional[datetime] = Query(None, alias="from"),
    to_time: Optional[datetime] = Query(None, alias="to"),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return AppointmentController.list_appointments(
        db,
        current_user,
        animal_id,
        room_id,
        appt_status,
        from_time,
        to_time,
        limit,
        after_id,
    )


# 11. POST /appointments
@app.post(
    "/appointments",
    tags=["Appointments"],
    response_model=AppointmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_appointment(
    payload: AppointmentCreateRequest,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    appt = AppointmentController.create_appointment(db, current_user, payload)
    response.headers["Location"] = f"/appointments/{appt.id}"
    return AppointmentResponse.model_validate(appt)


# 12. GET /appointments/{appointment_id}
@app.get(
    "/appointments/{appointment_id}",
    tags=["Appointments"],
    response_model=AppointmentResponse,
)
def get_appointment(
    appointment_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    appt = AppointmentController.get_appointment(db, current_user, appointment_id)
    return AppointmentResponse.model_validate(appt)


# 13. PATCH /appointments/{appointment_id}
@app.patch(
    "/appointments/{appointment_id}",
    tags=["Appointments"],
    response_model=AppointmentResponse,
)
def patch_appointment(
    appointment_id: int,
    payload: AppointmentPatchRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    appt = AppointmentController.patch_appointment(
        db, current_user, appointment_id, payload
    )
    return AppointmentResponse.model_validate(appt)


# 14. POST /appointments/{appointment_id}/check-in
@app.post(
    "/appointments/{appointment_id}/check-in",
    tags=["Appointments"],
    response_model=AppointmentResponse,
)
def check_in_appointment(
    appointment_id: int,
    payload: AppointmentCheckInRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    appt = AppointmentController.check_in_appointment(
        db, current_user, appointment_id, payload
    )
    return AppointmentResponse.model_validate(appt)


# 15. POST /appointments/{appointment_id}/complete
@app.post(
    "/appointments/{appointment_id}/complete",
    tags=["Appointments"],
    response_model=AppointmentResponse,
)
def complete_appointment(
    appointment_id: int,
    payload: AppointmentCompleteRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    appt = AppointmentController.complete_appointment(
        db, current_user, appointment_id, payload
    )
    return AppointmentResponse.model_validate(appt)


# 16. POST /appointments/{appointment_id}/cancel
@app.post(
    "/appointments/{appointment_id}/cancel",
    tags=["Appointments"],
    response_model=AppointmentResponse,
)
def cancel_appointment(
    appointment_id: int,
    payload: AppointmentCancelRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    appt = AppointmentController.cancel_appointment(
        db, current_user, appointment_id, payload
    )
    return AppointmentResponse.model_validate(appt)


# 17. GET /rooms/{room_id}/appointment-conflicts
@app.get(
    "/rooms/{room_id}/appointment-conflicts",
    tags=["Appointments"],
    response_model=AppointmentListResponse,
)
def get_room_conflicts(
    room_id: int,
    start_at: datetime = Query(..., description="Query interval start (UTC ISO-8601)"),
    end_at: datetime = Query(..., description="Query interval end (UTC ISO-8601)"),
    exclude_appointment_id: Optional[int] = Query(None, gt=0),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return AppointmentController.get_room_conflicts(
        db,
        current_user,
        room_id,
        start_at,
        end_at,
        exclude_appointment_id,
        limit,
        after_id,
    )

# 18. GET /animals/{animal_id}/appointment-conflicts
@app.get(
    "/animals/{animal_id}/appointment-conflicts",
    tags=["Appointments"],
    response_model=AppointmentListResponse,
)
def get_animal_conflicts(
    animal_id: int,
    start_at: datetime = Query(..., description="Query interval start (UTC ISO-8601)"),
    end_at: datetime = Query(..., description="Query interval end (UTC ISO-8601)"),
    exclude_appointment_id: Optional[int] = Query(None, gt=0),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return AppointmentController.get_animal_conflicts(
        db,
        current_user,
        animal_id,
        start_at,
        end_at,
        exclude_appointment_id,
        limit,
        after_id,
    )
