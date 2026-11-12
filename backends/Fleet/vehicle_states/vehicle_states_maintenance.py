"""
Vehicle States and Maintenance Management Backend API
Domain: Corporate fleet management
Variant: Vehicle states and maintenance management (fleet-vehicle-states-v1)
Architecture: MVC Pattern, FastAPI, Pydantic v2, SQLAlchemy ORM, SQLite In-Memory
"""

import enum
import hashlib
import http
import ipaddress
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Integer,
    String,
    create_engine,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match

# -----------------------------------------------------------------------------
# Logging Configuration
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("fleet_service")

# -----------------------------------------------------------------------------
# Database Setup (SQLite in-memory with StaticPool for shared memory connection)
# -----------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///:memory:")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# -----------------------------------------------------------------------------
# Helper Security Utilities
# -----------------------------------------------------------------------------
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def validate_safe_url(url_str: str) -> str:
    parsed = urlparse(url_str)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL scheme must be http or https")
    hostname = parsed.hostname
    if not hostname or hostname in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Host localhost or loopback is forbidden")
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified or ip.is_reserved:
            raise ValueError("Private and local IP addresses are forbidden")
    except ValueError as err:
        if "does not appear to be an IPv4 or IPv6 address" not in str(err):
            raise
    return url_str


# -----------------------------------------------------------------------------
# Enums
# -----------------------------------------------------------------------------
class VehicleStateEnum(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    ASSIGNED = "ASSIGNED"
    OUT_OF_SERVICE = "OUT_OF_SERVICE"
    MAINTENANCE = "MAINTENANCE"


class MaintenanceTypeEnum(str, enum.Enum):
    PREVENTIVE = "PREVENTIVE"
    CORRECTIVE = "CORRECTIVE"
    INSPECTION = "INSPECTION"
    REPAIR = "REPAIR"


class MaintenanceStatusEnum(str, enum.Enum):
    SCHEDULED = "SCHEDULED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class NextVehicleStateEnum(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    OUT_OF_SERVICE = "OUT_OF_SERVICE"


class UserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


# -----------------------------------------------------------------------------
# SQLAlchemy Models (MVC: Model)
# -----------------------------------------------------------------------------
class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=UserRole.USER.value)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)


class VehicleModel(Base):
    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    license_plate: Mapped[str] = mapped_column(String(12), unique=True, nullable=False)
    manufacturer: Mapped[str] = mapped_column(String(60), nullable=False)
    model: Mapped[str] = mapped_column(String(60), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    state_events: Mapped[List["VehicleStateEventModel"]] = relationship(
        "VehicleStateEventModel",
        back_populates="vehicle",
        cascade="all, delete-orphan",
        order_by="VehicleStateEventModel.id",
    )
    maintenances: Mapped[List["MaintenanceModel"]] = relationship(
        "MaintenanceModel",
        back_populates="vehicle",
    )


class VehicleStateEventModel(Base):
    __tablename__ = "vehicle_state_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehicle_id: Mapped[int] = mapped_column(Integer, ForeignKey("vehicles.id"), nullable=False)
    target_state: Mapped[str] = mapped_column(String(20), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(250), nullable=False)

    vehicle: Mapped[VehicleModel] = relationship("VehicleModel", back_populates="state_events")


class MaintenanceModel(Base):
    __tablename__ = "maintenances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehicle_id: Mapped[int] = mapped_column(Integer, ForeignKey("vehicles.id"), nullable=False)
    requester_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=MaintenanceStatusEnum.SCHEDULED.value)
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scheduled_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actual_start: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completion_notes: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    vehicle: Mapped[VehicleModel] = relationship("VehicleModel", back_populates="maintenances")
    requester: Mapped[UserModel] = relationship("UserModel")


# -----------------------------------------------------------------------------
# Custom App Exception
# -----------------------------------------------------------------------------
class AppException(Exception):
    def __init__(self, status_code: int, detail: str, error_type: Optional[str] = None):
        self.status_code = status_code
        self.detail = detail
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


class VehicleStateEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    vehicle_id: int
    target_state: VehicleStateEnum
    effective_at: datetime
    reason: str


class VehicleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    license_plate: str = Field(..., pattern=r"^[A-Z0-9 -]{3,12}$")
    manufacturer: str = Field(..., min_length=2, max_length=60)
    model: str = Field(..., min_length=1, max_length=60)
    year: int = Field(..., ge=1990, le=2100)
    active: bool = True

    @field_validator("license_plate")
    @classmethod
    def normalize_plate(cls, v: str) -> str:
        return v.strip().upper()


class VehiclePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    license_plate: Optional[str] = Field(None, pattern=r"^[A-Z0-9 -]{3,12}$")
    manufacturer: Optional[str] = Field(None, min_length=2, max_length=60)
    model: Optional[str] = Field(None, min_length=1, max_length=60)
    year: Optional[int] = Field(None, ge=1990, le=2100)
    active: Optional[bool] = None

    @field_validator("license_plate")
    @classmethod
    def normalize_plate(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return v.strip().upper()
        return v

    @model_validator(mode="after")
    def reject_null_values(self):
        for field_name in ("license_plate", "manufacturer", "model", "year", "active"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class VehicleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    license_plate: str
    manufacturer: str
    model: str
    year: int
    active: bool
    current_state: VehicleStateEnum


class VehicleListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[VehicleResponse]
    next_after_id: Optional[int]


class VehicleStateHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[VehicleStateEventResponse]
    next_after_id: Optional[int]


class StateTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_state: VehicleStateEnum
    effective_at: datetime
    reason: str = Field(..., min_length=3, max_length=250)


class MaintenanceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vehicle_id: int = Field(..., gt=0)
    type: MaintenanceTypeEnum
    title: str = Field(..., min_length=3, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    scheduled_start: datetime
    scheduled_end: datetime


class MaintenancePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = Field(None, min_length=3, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    scheduled_start: Optional[datetime] = None
    scheduled_end: Optional[datetime] = None

    @model_validator(mode="after")
    def reject_null_for_non_nullable_fields(self):
        for field_name in ("title", "scheduled_start", "scheduled_end"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class MaintenanceStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actual_start: Optional[datetime] = None


class MaintenanceCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    completed_at: datetime
    completion_notes: str = Field(..., min_length=1, max_length=2000)
    next_vehicle_state: NextVehicleStateEnum


class MaintenanceCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cancelled_at: datetime
    cancellation_reason: str = Field(..., min_length=3, max_length=500)
    next_vehicle_state: Optional[NextVehicleStateEnum] = None


class MaintenanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    vehicle_id: int
    requester_id: int
    type: MaintenanceTypeEnum
    title: str
    description: Optional[str]
    status: MaintenanceStatusEnum
    scheduled_start: datetime
    scheduled_end: datetime
    actual_start: Optional[datetime]
    completed_at: Optional[datetime]
    completion_notes: Optional[str]
    cancelled_at: Optional[datetime]
    cancellation_reason: Optional[str]


class MaintenanceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[MaintenanceResponse]
    next_after_id: Optional[int]


# -----------------------------------------------------------------------------
# Static Controllers (MVC: Controller)
# -----------------------------------------------------------------------------
class AuthController:
    @staticmethod
    def get_current_user(
        authorization: Optional[str] = Header(None),
        db: Session = Depends(get_db),
    ) -> UserModel:
        if not authorization or not authorization.startswith("Bearer "):
            raise AppException(401, "Missing or invalid Bearer authorization header", "Unauthorized")
        token = authorization[7:].strip()
        thash = hash_token(token)
        user = db.scalar(select(UserModel).where(UserModel.token_hash == thash))
        if not user:
            raise AppException(401, "Invalid or expired session token", "Unauthorized")
        return user

    @staticmethod
    def require_admin(
        current_user: UserModel = Depends(get_current_user),
    ) -> UserModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Administrator privilege required", "Forbidden")
        return current_user


class VehicleController:
    @staticmethod
    def get_latest_state(vehicle: VehicleModel) -> VehicleStateEventModel:
        if not vehicle.state_events:
            raise AppException(500, "Corrupted vehicle state: no state events found", "Internal Server Error")
        return max(vehicle.state_events, key=lambda e: (e.effective_at, e.id))

    @classmethod
    def serialize_vehicle(cls, vehicle: VehicleModel) -> VehicleResponse:
        latest = cls.get_latest_state(vehicle)
        return VehicleResponse(
            id=vehicle.id,
            license_plate=vehicle.license_plate,
            manufacturer=vehicle.manufacturer,
            model=vehicle.model,
            year=vehicle.year,
            active=vehicle.active,
            current_state=VehicleStateEnum(latest.target_state),
        )

    @classmethod
    def list_vehicles(
        cls,
        db: Session,
        active: Optional[bool],
        current_state: Optional[str],
        plate_prefix: Optional[str],
        limit: int,
        after_id: Optional[int],
    ) -> VehicleListResponse:
        query = select(VehicleModel)
        if after_id is not None:
            if after_id <= 0:
                raise AppException(422, "after_id must be a positive integer", "Unprocessable Entity")
            query = query.where(VehicleModel.id > after_id)

        if active is not None:
            query = query.where(VehicleModel.active == active)

        if plate_prefix is not None:
            norm_prefix = plate_prefix.strip().upper()
            if not (1 <= len(norm_prefix) <= 12):
                raise AppException(400, "plate_prefix length must be between 1 and 12", "Bad Request")
            query = query.where(VehicleModel.license_plate.like(f"{norm_prefix}%"))

        query = query.order_by(VehicleModel.id.asc()).limit(limit + 1)
        vehicles = list(db.scalars(query).all())

        if current_state is not None:
            try:
                state_enum = VehicleStateEnum(current_state)
            except ValueError:
                raise AppException(400, f"Invalid current_state filter: {current_state}", "Bad Request")
            vehicles = [v for v in vehicles if cls.get_latest_state(v).target_state == state_enum.value]

        has_more = len(vehicles) > limit
        result_items = vehicles[:limit]
        next_id = result_items[-1].id if (has_more and result_items) else None

        return VehicleListResponse(
            items=[cls.serialize_vehicle(v) for v in result_items],
            next_after_id=next_id,
        )

    @classmethod
    def create_vehicle(cls, db: Session, payload: VehicleCreateRequest) -> VehicleModel:
        if db.scalar(select(VehicleModel).where(VehicleModel.license_plate == payload.license_plate)):
            raise AppException(409, f"Vehicle with license plate '{payload.license_plate}' already exists", "Conflict")

        vehicle = VehicleModel(
            license_plate=payload.license_plate,
            manufacturer=payload.manufacturer,
            model=payload.model,
            year=payload.year,
            active=payload.active,
        )
        db.add(vehicle)
        db.flush()

        initial_event = VehicleStateEventModel(
            vehicle_id=vehicle.id,
            target_state=VehicleStateEnum.AVAILABLE.value,
            effective_at=utc_now(),
            reason="Initial vehicle registration",
        )
        db.add(initial_event)
        db.commit()
        db.refresh(vehicle)
        return vehicle

    @classmethod
    def get_vehicle_by_id(cls, db: Session, vehicle_id: int) -> VehicleModel:
        if vehicle_id <= 0:
            raise AppException(404, "Vehicle id must be positive", "Not Found")
        vehicle = db.scalar(select(VehicleModel).where(VehicleModel.id == vehicle_id))
        if not vehicle:
            raise AppException(404, f"Vehicle {vehicle_id} not found", "Not Found")
        return vehicle

    @classmethod
    def patch_vehicle(cls, db: Session, vehicle_id: int, payload: VehiclePatchRequest) -> VehicleModel:
        vehicle = cls.get_vehicle_by_id(db, vehicle_id)
        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AppException(400, "Patch payload cannot be empty", "Bad Request")

        if "license_plate" in data and data["license_plate"] != vehicle.license_plate:
            existing = db.scalar(select(VehicleModel).where(VehicleModel.license_plate == data["license_plate"]))
            if existing and existing.id != vehicle.id:
                raise AppException(409, f"License plate '{data['license_plate']}' is already in use", "Conflict")
            vehicle.license_plate = data["license_plate"]

        if "manufacturer" in data:
            vehicle.manufacturer = data["manufacturer"]
        if "model" in data:
            vehicle.model = data["model"]
        if "year" in data:
            vehicle.year = data["year"]
        if "active" in data:
            vehicle.active = data["active"]

        db.commit()
        db.refresh(vehicle)
        return vehicle

    @classmethod
    def delete_vehicle(cls, db: Session, vehicle_id: int):
        vehicle = cls.get_vehicle_by_id(db, vehicle_id)
        if vehicle.maintenances:
            raise AppException(409, "Vehicle with maintenance history cannot be deleted", "Conflict")

        db.delete(vehicle)
        db.commit()

    @classmethod
    def create_state_transition(
        cls,
        db: Session,
        vehicle_id: int,
        payload: StateTransitionRequest,
    ) -> VehicleStateEventModel:
        vehicle = cls.get_vehicle_by_id(db, vehicle_id)
        latest = cls.get_latest_state(vehicle)

        eff_at = payload.effective_at
        if eff_at.tzinfo is None:
            eff_at = eff_at.replace(tzinfo=timezone.utc)
        latest_eff = latest.effective_at
        if latest_eff.tzinfo is None:
            latest_eff = latest_eff.replace(tzinfo=timezone.utc)

        if payload.target_state.value == latest.target_state:
            raise AppException(409, f"Target state '{payload.target_state.value}' is identical to current state", "Conflict")

        if eff_at <= latest_eff:
            raise AppException(409, "Effective timestamp must be strictly greater than latest state timestamp", "Conflict")

        if payload.target_state == VehicleStateEnum.MAINTENANCE:
            now_dt = utc_now()
            qualifying_maintenance = db.scalars(
                select(MaintenanceModel).where(
                    MaintenanceModel.vehicle_id == vehicle_id,
                    MaintenanceModel.status.in_([MaintenanceStatusEnum.SCHEDULED.value, MaintenanceStatusEnum.IN_PROGRESS.value]),
                    MaintenanceModel.scheduled_start <= eff_at,
                )
            ).first()
            if not qualifying_maintenance:
                raise AppException(
                    409,
                    "Vehicle cannot enter MAINTENANCE without a SCHEDULED or IN_PROGRESS maintenance starting on or before effective_at",
                    "Conflict",
                )

        event = VehicleStateEventModel(
            vehicle_id=vehicle.id,
            target_state=payload.target_state.value,
            effective_at=eff_at.replace(microsecond=0),
            reason=payload.reason,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return event

    @classmethod
    def list_state_history(
        cls,
        db: Session,
        vehicle_id: int,
        limit: int,
        after_id: Optional[int],
    ) -> VehicleStateHistoryResponse:
        cls.get_vehicle_by_id(db, vehicle_id)
        query = select(VehicleStateEventModel).where(VehicleStateEventModel.vehicle_id == vehicle_id)
        if after_id is not None:
            if after_id <= 0:
                raise AppException(422, "after_id must be a positive integer", "Unprocessable Entity")
            query = query.where(VehicleStateEventModel.id > after_id)

        query = query.order_by(VehicleStateEventModel.id.asc()).limit(limit + 1)
        events = list(db.scalars(query).all())
        has_more = len(events) > limit
        items = events[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return VehicleStateHistoryResponse(
            items=[
                VehicleStateEventResponse(
                    id=e.id,
                    vehicle_id=e.vehicle_id,
                    target_state=VehicleStateEnum(e.target_state),
                    effective_at=e.effective_at,
                    reason=e.reason,
                )
                for e in items
            ],
            next_after_id=next_id,
        )


class MaintenanceController:
    @staticmethod
    def check_interval_overlap(
        db: Session,
        vehicle_id: int,
        start: datetime,
        end: datetime,
        exclude_id: Optional[int] = None,
    ):
        active_list = db.scalars(
            select(MaintenanceModel).where(
                MaintenanceModel.vehicle_id == vehicle_id,
                MaintenanceModel.status.in_([MaintenanceStatusEnum.SCHEDULED.value, MaintenanceStatusEnum.IN_PROGRESS.value]),
            )
        ).all()

        for m in active_list:
            if exclude_id and m.id == exclude_id:
                continue
            m_start = m.scheduled_start
            if m_start.tzinfo is None:
                m_start = m_start.replace(tzinfo=timezone.utc)
            m_end = m.scheduled_end
            if m_end.tzinfo is None:
                m_end = m_end.replace(tzinfo=timezone.utc)

            # REQ-1-006: open-ended IN_PROGRESS overlaps every later interval
            if m.status == MaintenanceStatusEnum.IN_PROGRESS.value:
                # An open-ended IN_PROGRESS interval starting at m_start overlaps any interval whose end > m_start
                if end > m_start:
                    raise AppException(409, f"Interval overlaps with open-ended active maintenance {m.id}", "Conflict")
            else:
                # Standard non-touching interval overlap: max(s1, s2) < min(e1, e2)
                if max(start, m_start) < min(end, m_end):
                    raise AppException(409, f"Interval overlaps with scheduled maintenance {m.id}", "Conflict")

    @classmethod
    def list_maintenances(
        cls,
        db: Session,
        current_user: UserModel,
        vehicle_id: Optional[int],
        status: Optional[str],
        type_val: Optional[str],
        from_dt: Optional[datetime],
        to_dt: Optional[datetime],
        limit: int,
        after_id: Optional[int],
    ) -> MaintenanceListResponse:
        query = select(MaintenanceModel)
        if current_user.role != UserRole.ADMIN.value:
            query = query.where(MaintenanceModel.requester_id == current_user.id)

        if vehicle_id is not None:
            if vehicle_id <= 0:
                raise AppException(404, "vehicle_id must be positive", "Not Found")
            # Verify vehicle exists
            if not db.scalar(select(VehicleModel.id).where(VehicleModel.id == vehicle_id)):
                raise AppException(404, f"Vehicle {vehicle_id} not found", "Not Found")
            query = query.where(MaintenanceModel.vehicle_id == vehicle_id)

        if status is not None:
            try:
                st_enum = MaintenanceStatusEnum(status)
                query = query.where(MaintenanceModel.status == st_enum.value)
            except ValueError:
                raise AppException(400, f"Invalid maintenance status: {status}", "Bad Request")

        if type_val is not None:
            try:
                t_enum = MaintenanceTypeEnum(type_val)
                query = query.where(MaintenanceModel.type == t_enum.value)
            except ValueError:
                raise AppException(400, f"Invalid maintenance type: {type_val}", "Bad Request")

        if from_dt and to_dt:
            if from_dt > to_dt:
                raise AppException(400, "'from' date cannot be later than 'to' date", "Bad Request")
        if from_dt:
            query = query.where(MaintenanceModel.scheduled_start >= from_dt)
        if to_dt:
            query = query.where(MaintenanceModel.scheduled_end <= to_dt)

        if after_id is not None:
            if after_id <= 0:
                raise AppException(422, "after_id must be a positive integer", "Unprocessable Entity")
            query = query.where(MaintenanceModel.id > after_id)

        query = query.order_by(MaintenanceModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return MaintenanceListResponse(
            items=[
                MaintenanceResponse(
                    id=m.id,
                    vehicle_id=m.vehicle_id,
                    requester_id=m.requester_id,
                    type=MaintenanceTypeEnum(m.type),
                    title=m.title,
                    description=m.description,
                    status=MaintenanceStatusEnum(m.status),
                    scheduled_start=m.scheduled_start,
                    scheduled_end=m.scheduled_end,
                    actual_start=m.actual_start,
                    completed_at=m.completed_at,
                    completion_notes=m.completion_notes,
                    cancelled_at=m.cancelled_at,
                    cancellation_reason=m.cancellation_reason,
                )
                for m in items
            ],
            next_after_id=next_id,
        )

    @classmethod
    def create_maintenance(
        cls,
        db: Session,
        current_user: UserModel,
        payload: MaintenanceCreateRequest,
    ) -> MaintenanceModel:
        vehicle = VehicleController.get_vehicle_by_id(db, payload.vehicle_id)

        start = payload.scheduled_start
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = payload.scheduled_end
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        if end <= start:
            raise AppException(400, "scheduled_end must be strictly later than scheduled_start", "Bad Request")

        cls.check_interval_overlap(db, vehicle.id, start, end)

        maintenance = MaintenanceModel(
            vehicle_id=vehicle.id,
            requester_id=current_user.id,
            type=payload.type.value,
            title=payload.title,
            description=payload.description,
            status=MaintenanceStatusEnum.SCHEDULED.value,
            scheduled_start=start.replace(microsecond=0),
            scheduled_end=end.replace(microsecond=0),
        )
        db.add(maintenance)
        db.commit()
        db.refresh(maintenance)
        return maintenance

    @classmethod
    def get_maintenance_by_id(
        cls,
        db: Session,
        current_user: UserModel,
        maintenance_id: int,
    ) -> MaintenanceModel:
        if maintenance_id <= 0:
            raise AppException(404, "Maintenance id must be positive", "Not Found")
        m = db.scalar(select(MaintenanceModel).where(MaintenanceModel.id == maintenance_id))
        if not m:
            raise AppException(404, f"Maintenance {maintenance_id} not found", "Not Found")
        if current_user.role != UserRole.ADMIN.value and m.requester_id != current_user.id:
            raise AppException(403, "Access denied to maintenance owned by another user", "Forbidden")
        return m

    @classmethod
    def patch_maintenance(
        cls,
        db: Session,
        current_user: UserModel,
        maintenance_id: int,
        payload: MaintenancePatchRequest,
    ) -> MaintenanceModel:
        m = cls.get_maintenance_by_id(db, current_user, maintenance_id)
        if m.status != MaintenanceStatusEnum.SCHEDULED.value:
            raise AppException(409, f"Only SCHEDULED maintenance can be modified; current is {m.status}", "Conflict")

        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AppException(400, "Patch payload cannot be empty", "Bad Request")

        new_start = data.get("scheduled_start", m.scheduled_start)
        if new_start.tzinfo is None:
            new_start = new_start.replace(tzinfo=timezone.utc)
        new_end = data.get("scheduled_end", m.scheduled_end)
        if new_end.tzinfo is None:
            new_end = new_end.replace(tzinfo=timezone.utc)

        if new_end <= new_start:
            raise AppException(400, "scheduled_end must be strictly later than scheduled_start", "Bad Request")

        cls.check_interval_overlap(db, m.vehicle_id, new_start, new_end, exclude_id=m.id)

        if "title" in data:
            m.title = data["title"]
        if "description" in data:
            m.description = data["description"]
        m.scheduled_start = new_start.replace(microsecond=0)
        m.scheduled_end = new_end.replace(microsecond=0)

        db.commit()
        db.refresh(m)
        return m

    @classmethod
    def start_maintenance(
        cls,
        db: Session,
        current_user: UserModel,
        maintenance_id: int,
        payload: Optional[MaintenanceStartRequest] = None,
    ) -> MaintenanceModel:
        m = cls.get_maintenance_by_id(db, current_user, maintenance_id)
        if m.status != MaintenanceStatusEnum.SCHEDULED.value:
            raise AppException(409, f"Cannot start maintenance in status {m.status}", "Conflict")

        vehicle = VehicleController.get_vehicle_by_id(db, m.vehicle_id)
        latest_state = VehicleController.get_latest_state(vehicle)
        if latest_state.target_state != VehicleStateEnum.MAINTENANCE.value:
            raise AppException(
                409,
                f"Vehicle must be in MAINTENANCE state before starting maintenance; currently {latest_state.target_state}",
                "Conflict",
            )

        start_time = payload.actual_start if (payload and payload.actual_start) else utc_now()
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)

        m.status = MaintenanceStatusEnum.IN_PROGRESS.value
        m.actual_start = start_time.replace(microsecond=0)
        db.commit()
        db.refresh(m)
        return m

    @classmethod
    def complete_maintenance(
        cls,
        db: Session,
        current_user: UserModel,
        maintenance_id: int,
        payload: MaintenanceCompleteRequest,
    ) -> MaintenanceModel:
        m = cls.get_maintenance_by_id(db, current_user, maintenance_id)
        if m.status != MaintenanceStatusEnum.IN_PROGRESS.value:
            raise AppException(409, f"Cannot complete maintenance in status {m.status}", "Conflict")

        comp_dt = payload.completed_at
        if comp_dt.tzinfo is None:
            comp_dt = comp_dt.replace(tzinfo=timezone.utc)
        act_start = m.actual_start or m.scheduled_start
        if act_start.tzinfo is None:
            act_start = act_start.replace(tzinfo=timezone.utc)

        if comp_dt < act_start:
            raise AppException(409, "completed_at cannot be earlier than actual_start", "Conflict")

        m.status = MaintenanceStatusEnum.COMPLETED.value
        m.completed_at = comp_dt.replace(microsecond=0)
        m.completion_notes = payload.completion_notes

        # REQ-1-005: If no other active maintenance exists for this vehicle, atomically update vehicle state
        other_active = db.scalars(
            select(MaintenanceModel).where(
                MaintenanceModel.vehicle_id == m.vehicle_id,
                MaintenanceModel.id != m.id,
                MaintenanceModel.status.in_([MaintenanceStatusEnum.SCHEDULED.value, MaintenanceStatusEnum.IN_PROGRESS.value]),
            )
        ).first()

        if not other_active:
            event = VehicleStateEventModel(
                vehicle_id=m.vehicle_id,
                target_state=payload.next_vehicle_state.value,
                effective_at=comp_dt.replace(microsecond=0),
                reason=f"Maintenance {m.id} completed: {payload.completion_notes[:50]}",
            )
            db.add(event)

        db.commit()
        db.refresh(m)
        return m

    @classmethod
    def cancel_maintenance(
        cls,
        db: Session,
        current_user: UserModel,
        maintenance_id: int,
        payload: MaintenanceCancelRequest,
    ) -> MaintenanceModel:
        m = cls.get_maintenance_by_id(db, current_user, maintenance_id)
        if m.status in (MaintenanceStatusEnum.COMPLETED.value, MaintenanceStatusEnum.CANCELLED.value):
            raise AppException(409, f"Cannot cancel terminal maintenance in status {m.status}", "Conflict")

        canc_dt = payload.cancelled_at
        if canc_dt.tzinfo is None:
            canc_dt = canc_dt.replace(tzinfo=timezone.utc)

        was_in_progress = m.status == MaintenanceStatusEnum.IN_PROGRESS.value
        if was_in_progress:
            act_start = m.actual_start or m.scheduled_start
            if act_start.tzinfo is None:
                act_start = act_start.replace(tzinfo=timezone.utc)
            if canc_dt < act_start:
                raise AppException(409, "cancelled_at cannot precede actual_start", "Conflict")
            if not payload.next_vehicle_state:
                raise AppException(422, "next_vehicle_state is required when cancelling IN_PROGRESS maintenance", "Unprocessable Entity")

        m.status = MaintenanceStatusEnum.CANCELLED.value
        m.cancelled_at = canc_dt.replace(microsecond=0)
        m.cancellation_reason = payload.cancellation_reason

        if was_in_progress and payload.next_vehicle_state:
            other_active = db.scalars(
                select(MaintenanceModel).where(
                    MaintenanceModel.vehicle_id == m.vehicle_id,
                    MaintenanceModel.id != m.id,
                    MaintenanceModel.status.in_([MaintenanceStatusEnum.SCHEDULED.value, MaintenanceStatusEnum.IN_PROGRESS.value]),
                )
            ).first()
            if not other_active:
                event = VehicleStateEventModel(
                    vehicle_id=m.vehicle_id,
                    target_state=payload.next_vehicle_state.value,
                    effective_at=canc_dt.replace(microsecond=0),
                    reason=f"Maintenance {m.id} cancelled: {payload.cancellation_reason[:50]}",
                )
                db.add(event)

        db.commit()
        db.refresh(m)
        return m


# -----------------------------------------------------------------------------
# Pure ASGI Content-Type & Size Enforcement Middleware
# -----------------------------------------------------------------------------
class JsonValidationASGIMiddleware:
    def __init__(self, app_instance, routes):
        self.app = app_instance
        self.routes = routes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        if method not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return

        # Check whether the route actually exists and accepts this method
        has_full_match = False
        for route in self.routes:
            match_status, _ = route.matches(scope)
            if match_status == Match.FULL:
                has_full_match = True
                break

        # If method is not allowed on this path or route doesn't exist, pass through
        # so Starlette returns 405 (with Allow header) or 404
        if not has_full_match:
            await self.app(scope, receive, send)
            return

        body_chunks = []
        total_len = 0
        max_size = 10 * 1024 * 1024  # 10 MiB limit

        while True:
            message = await receive()
            chunk = message.get("body", b"")
            total_len += len(chunk)
            if total_len > max_size:
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
# Seed Data Initialization
# -----------------------------------------------------------------------------
def seed_database(db: Session):
    Base.metadata.create_all(bind=engine)
    if db.scalar(select(UserModel).limit(1)):
        return

    admin_user = UserModel(
        username="admin",
        role=UserRole.ADMIN.value,
        token_hash=hash_token("admin-token-secret-12345"),
    )
    normal_user_1 = UserModel(
        username="user1",
        role=UserRole.USER.value,
        token_hash=hash_token("user1-token-secret-12345"),
    )
    normal_user_2 = UserModel(
        username="user2",
        role=UserRole.USER.value,
        token_hash=hash_token("user2-token-secret-12345"),
    )
    db.add_all([admin_user, normal_user_1, normal_user_2])
    db.commit()

    v1 = VehicleModel(
        license_plate="ABC-101",
        manufacturer="Volvo",
        model="FH16",
        year=2022,
        active=True,
    )
    v2 = VehicleModel(
        license_plate="XYZ-999",
        manufacturer="Mercedes",
        model="Actros",
        year=2023,
        active=True,
    )
    db.add_all([v1, v2])
    db.flush()

    e1 = VehicleStateEventModel(
        vehicle_id=v1.id,
        target_state=VehicleStateEnum.AVAILABLE.value,
        effective_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        reason="Initial vehicle registration",
    )
    e2 = VehicleStateEventModel(
        vehicle_id=v2.id,
        target_state=VehicleStateEnum.MAINTENANCE.value,
        effective_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        reason="Scheduled service setup",
    )
    db.add_all([e1, e2])
    db.flush()

    m1 = MaintenanceModel(
        vehicle_id=v2.id,
        requester_id=normal_user_1.id,
        type=MaintenanceTypeEnum.PREVENTIVE.value,
        title="Quarterly Engine Check",
        description="Routine fluids and filters inspection",
        status=MaintenanceStatusEnum.SCHEDULED.value,
        scheduled_start=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
        scheduled_end=datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc),
    )
    db.add(m1)
    db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    with SessionLocal() as db:
        seed_database(db)
    yield


# -----------------------------------------------------------------------------
# FastAPI Application & Routes (MVC: View/Routing)
# -----------------------------------------------------------------------------
app = FastAPI(
    title="Corporate Fleet Management - Vehicle States & Maintenance API",
    version="1.0.0",
    description="Deterministic RESTful API for vehicle lifecycle, append-only state audits, and maintenance schedules.",
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
    logger.error("Unhandled internal exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "status_code": 500, "error_type": "Internal Server Error"},
    )


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------
@app.get(
    "/health",
    tags=["Health"],
    summary="Check service health",
    response_model=HealthResponse,
    responses={
        200: {"description": "Service operational and database connected", "model": HealthResponse},
        503: {"description": "Database connection probe failed", "model": ErrorResponse},
    },
)
def get_health(db: Session = Depends(get_db)):
    """The system shall expose GET /health as a public endpoint returning deterministic status."""
    try:
        db.execute(select(1))
        return {"status": "ok", "database": "connected"}
    except Exception:
        raise AppException(503, "Database connection unavailable", "Service Unavailable")


@app.get(
    "/vehicles",
    tags=["Vehicles"],
    summary="List vehicles",
    response_model=VehicleListResponse,
    responses={
        200: {"description": "Vehicles listed successfully", "model": VehicleListResponse},
        400: {"description": "Semantic query filter error", "model": ErrorResponse},
        401: {"description": "Missing or invalid token", "model": ErrorResponse},
        403: {"description": "Admin privileges required", "model": ErrorResponse},
        422: {"description": "Invalid query parameter syntax", "model": ErrorResponse},
    },
)
def list_vehicles(
    active: Optional[bool] = Query(None, description="Filter by active status"),
    current_state: Optional[str] = Query(None, description="Filter by current vehicle state"),
    plate_prefix: Optional[str] = Query(None, description="Filter by plate prefix (1..12 chars)"),
    limit: int = Query(20, ge=1, le=100, description="Items per page"),
    after_id: Optional[int] = Query(None, description="Cursor for pagination"),
    db: Session = Depends(get_db),
    admin: UserModel = Depends(AuthController.require_admin),
):
    """The system shall expose GET /vehicles as an admin-only endpoint with stable pagination."""
    return VehicleController.list_vehicles(db, active, current_state, plate_prefix, limit, after_id)


@app.post(
    "/vehicles",
    tags=["Vehicles"],
    summary="Create a new vehicle",
    status_code=201,
    response_model=VehicleResponse,
    responses={
        201: {"description": "Vehicle created successfully", "model": VehicleResponse},
        400: {"description": "Semantic validation error", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        409: {"description": "Duplicate license plate", "model": ErrorResponse},
        415: {"description": "Unsupported media type", "model": ErrorResponse},
        422: {"description": "Malformed input body", "model": ErrorResponse},
    },
)
def create_vehicle(
    payload: VehicleCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
    admin: UserModel = Depends(AuthController.require_admin),
):
    """The system shall expose POST /vehicles as an admin-only endpoint creating vehicle and first AVAILABLE state event."""
    vehicle = VehicleController.create_vehicle(db, payload)
    response.headers["Location"] = f"/vehicles/{vehicle.id}"
    return VehicleController.serialize_vehicle(vehicle)


@app.get(
    "/vehicles/{vehicle_id}",
    tags=["Vehicles"],
    summary="Get vehicle details",
    response_model=VehicleResponse,
    responses={
        200: {"description": "Vehicle retrieved successfully", "model": VehicleResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Vehicle not found", "model": ErrorResponse},
    },
)
def get_vehicle(
    vehicle_id: int,
    db: Session = Depends(get_db),
    admin: UserModel = Depends(AuthController.require_admin),
):
    """The system shall expose GET /vehicles/{vehicle_id} as an admin-only endpoint."""
    vehicle = VehicleController.get_vehicle_by_id(db, vehicle_id)
    return VehicleController.serialize_vehicle(vehicle)


@app.patch(
    "/vehicles/{vehicle_id}",
    tags=["Vehicles"],
    summary="Update vehicle details",
    response_model=VehicleResponse,
    responses={
        200: {"description": "Vehicle updated successfully", "model": VehicleResponse},
        400: {"description": "Empty or invalid patch payload", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Vehicle not found", "model": ErrorResponse},
        409: {"description": "License plate conflict", "model": ErrorResponse},
        415: {"description": "Unsupported media type", "model": ErrorResponse},
        422: {"description": "Unprocessable payload", "model": ErrorResponse},
    },
)
def patch_vehicle(
    vehicle_id: int,
    payload: VehiclePatchRequest,
    db: Session = Depends(get_db),
    admin: UserModel = Depends(AuthController.require_admin),
):
    """The system shall expose PATCH /vehicles/{vehicle_id} as an admin-only endpoint updating supplied fields."""
    vehicle = VehicleController.patch_vehicle(db, vehicle_id, payload)
    return VehicleController.serialize_vehicle(vehicle)


@app.delete(
    "/vehicles/{vehicle_id}",
    tags=["Vehicles"],
    summary="Delete unused vehicle",
    status_code=204,
    responses={
        204: {"description": "Vehicle deleted successfully"},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Vehicle not found", "model": ErrorResponse},
        409: {"description": "Vehicle has maintenance history", "model": ErrorResponse},
    },
)
def delete_vehicle(
    vehicle_id: int,
    db: Session = Depends(get_db),
    admin: UserModel = Depends(AuthController.require_admin),
):
    """The system shall expose DELETE /vehicles/{vehicle_id} as an admin-only endpoint deleting unused vehicle and history."""
    VehicleController.delete_vehicle(db, vehicle_id)
    return Response(status_code=204)


@app.get(
    "/vehicles/{vehicle_id}/state",
    tags=["Vehicle States"],
    summary="Get current vehicle state",
    response_model=VehicleStateEventResponse,
    responses={
        200: {"description": "Latest state retrieved successfully", "model": VehicleStateEventResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        404: {"description": "Vehicle not found", "model": ErrorResponse},
    },
)
def get_vehicle_state(
    vehicle_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /vehicles/{vehicle_id}/state as an authenticated endpoint returning latest state."""
    vehicle = VehicleController.get_vehicle_by_id(db, vehicle_id)
    latest = VehicleController.get_latest_state(vehicle)
    return VehicleStateEventResponse(
        id=latest.id,
        vehicle_id=latest.vehicle_id,
        target_state=VehicleStateEnum(latest.target_state),
        effective_at=latest.effective_at,
        reason=latest.reason,
    )


@app.get(
    "/vehicles/{vehicle_id}/state-history",
    tags=["Vehicle States"],
    summary="List vehicle state transitions history",
    response_model=VehicleStateHistoryResponse,
    responses={
        200: {"description": "History retrieved successfully", "model": VehicleStateHistoryResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Vehicle not found", "model": ErrorResponse},
        422: {"description": "Invalid pagination parameters", "model": ErrorResponse},
    },
)
def list_vehicle_state_history(
    vehicle_id: int,
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    admin: UserModel = Depends(AuthController.require_admin),
):
    """The system shall expose GET /vehicles/{vehicle_id}/state-history as an admin-only endpoint."""
    return VehicleController.list_state_history(db, vehicle_id, limit, after_id)


@app.post(
    "/vehicles/{vehicle_id}/state-transitions",
    tags=["Vehicle States"],
    summary="Record vehicle state transition",
    status_code=201,
    response_model=VehicleStateEventResponse,
    responses={
        201: {"description": "State transition recorded successfully", "model": VehicleStateEventResponse},
        400: {"description": "Semantic validation error", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Vehicle not found", "model": ErrorResponse},
        409: {"description": "State transition conflict", "model": ErrorResponse},
        415: {"description": "Unsupported media type", "model": ErrorResponse},
        422: {"description": "Malformed payload", "model": ErrorResponse},
    },
)
def create_state_transition(
    vehicle_id: int,
    payload: StateTransitionRequest,
    response: Response,
    db: Session = Depends(get_db),
    admin: UserModel = Depends(AuthController.require_admin),
):
    """The system shall expose POST /vehicles/{vehicle_id}/state-transitions as an admin-only endpoint."""
    event = VehicleController.create_state_transition(db, vehicle_id, payload)
    response.headers["Location"] = f"/vehicles/{vehicle_id}/state"
    return VehicleStateEventResponse(
        id=event.id,
        vehicle_id=event.vehicle_id,
        target_state=VehicleStateEnum(event.target_state),
        effective_at=event.effective_at,
        reason=event.reason,
    )


@app.get(
    "/maintenances",
    tags=["Maintenances"],
    summary="List maintenances",
    response_model=MaintenanceListResponse,
    responses={
        200: {"description": "Maintenances retrieved successfully", "model": MaintenanceListResponse},
        400: {"description": "Invalid filter parameters", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        404: {"description": "Vehicle not found", "model": ErrorResponse},
        422: {"description": "Invalid query syntax", "model": ErrorResponse},
    },
)
def list_maintenances(
    vehicle_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    from_dt: Optional[datetime] = Query(None, alias="from"),
    to_dt: Optional[datetime] = Query(None, alias="to"),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /maintenances filtering by owner for non-admin callers."""
    return MaintenanceController.list_maintenances(
        db, current_user, vehicle_id, status, type, from_dt, to_dt, limit, after_id
    )


@app.post(
    "/maintenances",
    tags=["Maintenances"],
    summary="Schedule maintenance",
    status_code=201,
    response_model=MaintenanceResponse,
    responses={
        201: {"description": "Maintenance scheduled successfully", "model": MaintenanceResponse},
        400: {"description": "Semantic validation error", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        404: {"description": "Vehicle not found", "model": ErrorResponse},
        409: {"description": "Interval overlap conflict", "model": ErrorResponse},
        415: {"description": "Unsupported media type", "model": ErrorResponse},
        422: {"description": "Malformed body", "model": ErrorResponse},
    },
)
def create_maintenance(
    payload: MaintenanceCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose POST /maintenances creating a SCHEDULED record."""
    m = MaintenanceController.create_maintenance(db, current_user, payload)
    response.headers["Location"] = f"/maintenances/{m.id}"
    return MaintenanceResponse(
        id=m.id,
        vehicle_id=m.vehicle_id,
        requester_id=m.requester_id,
        type=MaintenanceTypeEnum(m.type),
        title=m.title,
        description=m.description,
        status=MaintenanceStatusEnum(m.status),
        scheduled_start=m.scheduled_start,
        scheduled_end=m.scheduled_end,
        actual_start=m.actual_start,
        completed_at=m.completed_at,
        completion_notes=m.completion_notes,
        cancelled_at=m.cancelled_at,
        cancellation_reason=m.cancellation_reason,
    )


@app.get(
    "/maintenances/{maintenance_id}",
    tags=["Maintenances"],
    summary="Get maintenance details",
    response_model=MaintenanceResponse,
    responses={
        200: {"description": "Maintenance retrieved successfully", "model": MaintenanceResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Access denied to other users' maintenance", "model": ErrorResponse},
        404: {"description": "Maintenance not found", "model": ErrorResponse},
    },
)
def get_maintenance(
    maintenance_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /maintenances/{maintenance_id} for requester or admin."""
    m = MaintenanceController.get_maintenance_by_id(db, current_user, maintenance_id)
    return MaintenanceResponse(
        id=m.id,
        vehicle_id=m.vehicle_id,
        requester_id=m.requester_id,
        type=MaintenanceTypeEnum(m.type),
        title=m.title,
        description=m.description,
        status=MaintenanceStatusEnum(m.status),
        scheduled_start=m.scheduled_start,
        scheduled_end=m.scheduled_end,
        actual_start=m.actual_start,
        completed_at=m.completed_at,
        completion_notes=m.completion_notes,
        cancelled_at=m.cancelled_at,
        cancellation_reason=m.cancellation_reason,
    )


@app.patch(
    "/maintenances/{maintenance_id}",
    tags=["Maintenances"],
    summary="Update scheduled maintenance",
    response_model=MaintenanceResponse,
    responses={
        200: {"description": "Maintenance updated successfully", "model": MaintenanceResponse},
        400: {"description": "Invalid values or empty patch", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Maintenance not found", "model": ErrorResponse},
        409: {"description": "Maintenance not in SCHEDULED status or interval overlap", "model": ErrorResponse},
        415: {"description": "Unsupported media type", "model": ErrorResponse},
        422: {"description": "Unprocessable payload", "model": ErrorResponse},
    },
)
def patch_maintenance(
    maintenance_id: int,
    payload: MaintenancePatchRequest,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose PATCH /maintenances/{maintenance_id} allowing changes only to SCHEDULED records."""
    m = MaintenanceController.patch_maintenance(db, current_user, maintenance_id, payload)
    return MaintenanceResponse(
        id=m.id,
        vehicle_id=m.vehicle_id,
        requester_id=m.requester_id,
        type=MaintenanceTypeEnum(m.type),
        title=m.title,
        description=m.description,
        status=MaintenanceStatusEnum(m.status),
        scheduled_start=m.scheduled_start,
        scheduled_end=m.scheduled_end,
        actual_start=m.actual_start,
        completed_at=m.completed_at,
        completion_notes=m.completion_notes,
        cancelled_at=m.cancelled_at,
        cancellation_reason=m.cancellation_reason,
    )


@app.post(
    "/maintenances/{maintenance_id}/start",
    tags=["Maintenances"],
    summary="Start scheduled maintenance",
    response_model=MaintenanceResponse,
    responses={
        200: {"description": "Maintenance started successfully", "model": MaintenanceResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Maintenance not found", "model": ErrorResponse},
        409: {"description": "Vehicle not in MAINTENANCE state or maintenance not SCHEDULED", "model": ErrorResponse},
        415: {"description": "Unsupported media type", "model": ErrorResponse},
        422: {"description": "Invalid input", "model": ErrorResponse},
    },
)
def start_maintenance(
    maintenance_id: int,
    payload: Optional[MaintenanceStartRequest] = None,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose POST /maintenances/{maintenance_id}/start moving record to IN_PROGRESS."""
    m = MaintenanceController.start_maintenance(db, current_user, maintenance_id, payload)
    return MaintenanceResponse(
        id=m.id,
        vehicle_id=m.vehicle_id,
        requester_id=m.requester_id,
        type=MaintenanceTypeEnum(m.type),
        title=m.title,
        description=m.description,
        status=MaintenanceStatusEnum(m.status),
        scheduled_start=m.scheduled_start,
        scheduled_end=m.scheduled_end,
        actual_start=m.actual_start,
        completed_at=m.completed_at,
        completion_notes=m.completion_notes,
        cancelled_at=m.cancelled_at,
        cancellation_reason=m.cancellation_reason,
    )


@app.post(
    "/maintenances/{maintenance_id}/complete",
    tags=["Maintenances"],
    summary="Complete in-progress maintenance",
    response_model=MaintenanceResponse,
    responses={
        200: {"description": "Maintenance completed successfully", "model": MaintenanceResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Maintenance not found", "model": ErrorResponse},
        409: {"description": "Maintenance not IN_PROGRESS or invalid completion date", "model": ErrorResponse},
        415: {"description": "Unsupported media type", "model": ErrorResponse},
        422: {"description": "Malformed body", "model": ErrorResponse},
    },
)
def complete_maintenance(
    maintenance_id: int,
    payload: MaintenanceCompleteRequest,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose POST /maintenances/{maintenance_id}/complete moving record to COMPLETED."""
    m = MaintenanceController.complete_maintenance(db, current_user, maintenance_id, payload)
    return MaintenanceResponse(
        id=m.id,
        vehicle_id=m.vehicle_id,
        requester_id=m.requester_id,
        type=MaintenanceTypeEnum(m.type),
        title=m.title,
        description=m.description,
        status=MaintenanceStatusEnum(m.status),
        scheduled_start=m.scheduled_start,
        scheduled_end=m.scheduled_end,
        actual_start=m.actual_start,
        completed_at=m.completed_at,
        completion_notes=m.completion_notes,
        cancelled_at=m.cancelled_at,
        cancellation_reason=m.cancellation_reason,
    )


@app.post(
    "/maintenances/{maintenance_id}/cancel",
    tags=["Maintenances"],
    summary="Cancel scheduled or in-progress maintenance",
    response_model=MaintenanceResponse,
    responses={
        200: {"description": "Maintenance cancelled successfully", "model": MaintenanceResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Maintenance not found", "model": ErrorResponse},
        409: {"description": "Maintenance is already terminal or invalid cancellation date", "model": ErrorResponse},
        415: {"description": "Unsupported media type", "model": ErrorResponse},
        422: {"description": "Malformed body", "model": ErrorResponse},
    },
)
def cancel_maintenance(
    maintenance_id: int,
    payload: MaintenanceCancelRequest,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose POST /maintenances/{maintenance_id}/cancel moving record to CANCELLED."""
    m = MaintenanceController.cancel_maintenance(db, current_user, maintenance_id, payload)
    return MaintenanceResponse(
        id=m.id,
        vehicle_id=m.vehicle_id,
        requester_id=m.requester_id,
        type=MaintenanceTypeEnum(m.type),
        title=m.title,
        description=m.description,
        status=MaintenanceStatusEnum(m.status),
        scheduled_start=m.scheduled_start,
        scheduled_end=m.scheduled_end,
        actual_start=m.actual_start,
        completed_at=m.completed_at,
        completion_notes=m.completion_notes,
        cancelled_at=m.cancelled_at,
        cancellation_reason=m.cancellation_reason,
    )
