"""
Maintenance Planning, Cancellation, and History Backend API
Domain: Corporate fleet management
Variant: Maintenance planning, cancellation, and history (fleet-maintenance-planning-v1)
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
logger = logging.getLogger("maintenance_history_service")

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
    current_state: Mapped[str] = mapped_column(String(20), nullable=False, default=VehicleStateEnum.AVAILABLE.value)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    maintenances: Mapped[List["MaintenanceModel"]] = relationship("MaintenanceModel", back_populates="vehicle")


class MaintenanceModel(Base):
    __tablename__ = "maintenances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehicle_id: Mapped[int] = mapped_column(Integer, ForeignKey("vehicles.id"), nullable=False)
    requester_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=MaintenanceStatusEnum.SCHEDULED.value)
    planned_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    planned_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actual_start: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completion_notes: Mapped[Optional[str]] = mapped_column(String(2000), nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    vehicle: Mapped[VehicleModel] = relationship("VehicleModel", back_populates="maintenances")
    requester: Mapped[UserModel] = relationship("UserModel")
    history_events: Mapped[List["MaintenanceHistoryEventModel"]] = relationship(
        "MaintenanceHistoryEventModel",
        back_populates="maintenance",
        cascade="all, delete-orphan",
        order_by="MaintenanceHistoryEventModel.id",
    )


class MaintenanceHistoryEventModel(Base):
    __tablename__ = "maintenance_history_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    maintenance_id: Mapped[int] = mapped_column(Integer, ForeignKey("maintenances.id"), nullable=False)
    previous_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    new_status: Mapped[str] = mapped_column(String(20), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    maintenance: Mapped[MaintenanceModel] = relationship("MaintenanceModel", back_populates="history_events")
    actor: Mapped[UserModel] = relationship("UserModel")


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


class VehicleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    license_plate: str
    manufacturer: str
    model: str
    year: int
    current_state: VehicleStateEnum
    active: bool


class VehicleListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[VehicleResponse]
    next_after_id: Optional[int]


class MaintenanceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vehicle_id: int = Field(..., gt=0)
    type: MaintenanceTypeEnum
    title: str = Field(..., min_length=3, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    planned_start: datetime
    planned_end: datetime


class MaintenancePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = Field(None, min_length=3, max_length=120)
    description: Optional[str] = Field(None, max_length=2000)
    planned_start: Optional[datetime] = None
    planned_end: Optional[datetime] = None

    @model_validator(mode="after")
    def reject_null_for_non_nullable_fields(self):
        for field_name in ("title", "planned_start", "planned_end"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class MaintenanceStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actual_start: datetime


class MaintenanceCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actual_end: datetime
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
    planned_start: datetime
    planned_end: datetime
    actual_start: Optional[datetime]
    actual_end: Optional[datetime]
    completion_notes: Optional[str]
    cancelled_at: Optional[datetime]
    cancellation_reason: Optional[str]


class MaintenanceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[MaintenanceResponse]
    next_after_id: Optional[int]


class MaintenanceHistoryEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    maintenance_id: int
    previous_status: Optional[str]
    new_status: MaintenanceStatusEnum
    effective_at: datetime
    actor_id: int
    reason: Optional[str]


class MaintenanceHistoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[MaintenanceHistoryEventResponse]
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
    def list_vehicles(
        db: Session,
        active: Optional[bool],
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

        query = query.order_by(VehicleModel.id.asc()).limit(limit + 1)
        vehicles = list(db.scalars(query).all())
        has_more = len(vehicles) > limit
        items = vehicles[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return VehicleListResponse(
            items=[
                VehicleResponse(
                    id=v.id,
                    license_plate=v.license_plate,
                    manufacturer=v.manufacturer,
                    model=v.model,
                    year=v.year,
                    current_state=VehicleStateEnum(v.current_state),
                    active=v.active,
                )
                for v in items
            ],
            next_after_id=next_id,
        )

    @staticmethod
    def get_vehicle_by_id(db: Session, vehicle_id: int) -> VehicleModel:
        if vehicle_id <= 0:
            raise AppException(404, "Vehicle id must be positive", "Not Found")
        v = db.scalar(select(VehicleModel).where(VehicleModel.id == vehicle_id))
        if not v:
            raise AppException(404, f"Vehicle {vehicle_id} not found", "Not Found")
        return v


class MaintenanceController:
    @staticmethod
    def check_interval_overlap(
        db: Session,
        vehicle_id: int,
        start: datetime,
        end: datetime,
        exclude_id: Optional[int] = None,
    ):
        query = select(MaintenanceModel).where(
            MaintenanceModel.vehicle_id == vehicle_id,
            MaintenanceModel.status.in_([MaintenanceStatusEnum.SCHEDULED.value, MaintenanceStatusEnum.IN_PROGRESS.value]),
        )
        if exclude_id:
            query = query.where(MaintenanceModel.id != exclude_id)
        active_list = db.scalars(query).all()

        for m in active_list:
            m_s = m.planned_start
            if m_s.tzinfo is None:
                m_s = m_s.replace(tzinfo=timezone.utc)
            m_e = m.planned_end
            if m_e.tzinfo is None:
                m_e = m_e.replace(tzinfo=timezone.utc)

            # Touching endpoints do not overlap: max(s1, s2) < min(e1, e2)
            if max(start, m_s) < min(end, m_e):
                raise AppException(409, f"Interval overlaps with active maintenance {m.id}", "Conflict")

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
        include_cancelled: bool,
        limit: int,
        after_id: Optional[int],
    ) -> MaintenanceListResponse:
        query = select(MaintenanceModel)
        if current_user.role != UserRole.ADMIN.value:
            query = query.where(MaintenanceModel.requester_id == current_user.id)

        if vehicle_id is not None:
            if vehicle_id <= 0:
                raise AppException(404, "vehicle_id must be positive", "Not Found")
            if not db.scalar(select(VehicleModel.id).where(VehicleModel.id == vehicle_id)):
                raise AppException(404, f"Vehicle {vehicle_id} not found", "Not Found")
            query = query.where(MaintenanceModel.vehicle_id == vehicle_id)

        if status is not None:
            try:
                st_enum = MaintenanceStatusEnum(status)
                query = query.where(MaintenanceModel.status == st_enum.value)
            except ValueError:
                raise AppException(400, f"Invalid status: {status}", "Bad Request")
        elif not include_cancelled:
            query = query.where(MaintenanceModel.status != MaintenanceStatusEnum.CANCELLED.value)

        if type_val is not None:
            try:
                t_enum = MaintenanceTypeEnum(type_val)
                query = query.where(MaintenanceModel.type == t_enum.value)
            except ValueError:
                raise AppException(400, f"Invalid type: {type_val}", "Bad Request")

        if from_dt and to_dt:
            if from_dt > to_dt:
                raise AppException(400, "'from' date cannot be later than 'to' date", "Bad Request")
        if from_dt:
            query = query.where(MaintenanceModel.planned_start >= from_dt)
        if to_dt:
            query = query.where(MaintenanceModel.planned_end <= to_dt)

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
            items=[cls.serialize_maintenance(m) for m in items],
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

        start = payload.planned_start
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = payload.planned_end
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        if end <= start:
            raise AppException(400, "planned_end must be strictly later than planned_start", "Bad Request")

        cls.check_interval_overlap(db, vehicle.id, start, end)

        m = MaintenanceModel(
            vehicle_id=vehicle.id,
            requester_id=current_user.id,
            type=payload.type.value,
            title=payload.title,
            description=payload.description,
            status=MaintenanceStatusEnum.SCHEDULED.value,
            planned_start=start.replace(microsecond=0),
            planned_end=end.replace(microsecond=0),
        )
        db.add(m)
        db.flush()

        initial_history = MaintenanceHistoryEventModel(
            maintenance_id=m.id,
            previous_status=None,
            new_status=MaintenanceStatusEnum.SCHEDULED.value,
            effective_at=utc_now(),
            actor_id=current_user.id,
            reason="Initial maintenance schedule created",
        )
        db.add(initial_history)
        db.commit()
        db.refresh(m)
        return m

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

        new_start = data.get("planned_start", m.planned_start)
        if new_start.tzinfo is None:
            new_start = new_start.replace(tzinfo=timezone.utc)
        new_end = data.get("planned_end", m.planned_end)
        if new_end.tzinfo is None:
            new_end = new_end.replace(tzinfo=timezone.utc)

        if new_end <= new_start:
            raise AppException(400, "planned_end must be strictly later than planned_start", "Bad Request")

        cls.check_interval_overlap(db, m.vehicle_id, new_start, new_end, exclude_id=m.id)

        if "title" in data:
            m.title = data["title"]
        if "description" in data:
            m.description = data["description"]
        m.planned_start = new_start.replace(microsecond=0)
        m.planned_end = new_end.replace(microsecond=0)

        db.commit()
        db.refresh(m)
        return m

    @classmethod
    def start_maintenance(
        cls,
        db: Session,
        current_user: UserModel,
        maintenance_id: int,
        payload: MaintenanceStartRequest,
    ) -> MaintenanceModel:
        m = cls.get_maintenance_by_id(db, current_user, maintenance_id)
        if m.status != MaintenanceStatusEnum.SCHEDULED.value:
            raise AppException(409, f"Cannot start maintenance in status {m.status}", "Conflict")

        start_dt = payload.actual_start
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        plan_start = m.planned_start
        if plan_start.tzinfo is None:
            plan_start = plan_start.replace(tzinfo=timezone.utc)

        if start_dt < plan_start:
            raise AppException(409, "actual_start cannot precede planned_start", "Conflict")

        vehicle = VehicleController.get_vehicle_by_id(db, m.vehicle_id)
        if vehicle.current_state != VehicleStateEnum.MAINTENANCE.value:
            raise AppException(
                409,
                f"Vehicle must be in MAINTENANCE state; currently {vehicle.current_state}",
                "Conflict",
            )

        m.status = MaintenanceStatusEnum.IN_PROGRESS.value
        m.actual_start = start_dt.replace(microsecond=0)

        history = MaintenanceHistoryEventModel(
            maintenance_id=m.id,
            previous_status=MaintenanceStatusEnum.SCHEDULED.value,
            new_status=MaintenanceStatusEnum.IN_PROGRESS.value,
            effective_at=start_dt.replace(microsecond=0),
            actor_id=current_user.id,
            reason="Maintenance started",
        )
        db.add(history)
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

        end_dt = payload.actual_end
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        act_start = m.actual_start or m.planned_start
        if act_start.tzinfo is None:
            act_start = act_start.replace(tzinfo=timezone.utc)

        if end_dt < act_start:
            raise AppException(409, "actual_end cannot precede actual_start", "Conflict")

        m.status = MaintenanceStatusEnum.COMPLETED.value
        m.actual_end = end_dt.replace(microsecond=0)
        m.completion_notes = payload.completion_notes

        history = MaintenanceHistoryEventModel(
            maintenance_id=m.id,
            previous_status=MaintenanceStatusEnum.IN_PROGRESS.value,
            new_status=MaintenanceStatusEnum.COMPLETED.value,
            effective_at=end_dt.replace(microsecond=0),
            actor_id=current_user.id,
            reason=payload.completion_notes,
        )
        db.add(history)

        # REQ-1-005: If last active maintenance, atomically update vehicle state
        other_active = db.scalars(
            select(MaintenanceModel).where(
                MaintenanceModel.vehicle_id == m.vehicle_id,
                MaintenanceModel.id != m.id,
                MaintenanceModel.status.in_([MaintenanceStatusEnum.SCHEDULED.value, MaintenanceStatusEnum.IN_PROGRESS.value]),
            )
        ).first()

        if not other_active:
            vehicle = VehicleController.get_vehicle_by_id(db, m.vehicle_id)
            vehicle.current_state = payload.next_vehicle_state.value

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
        plan_start = m.planned_start
        if plan_start.tzinfo is None:
            plan_start = plan_start.replace(tzinfo=timezone.utc)

        if canc_dt < plan_start:
            raise AppException(409, "cancelled_at cannot precede planned_start", "Conflict")

        was_in_progress = m.status == MaintenanceStatusEnum.IN_PROGRESS.value
        if was_in_progress:
            act_start = m.actual_start or m.planned_start
            if act_start.tzinfo is None:
                act_start = act_start.replace(tzinfo=timezone.utc)
            if canc_dt < act_start:
                raise AppException(409, "cancelled_at cannot precede actual_start", "Conflict")
            if not payload.next_vehicle_state:
                raise AppException(422, "next_vehicle_state is required when cancelling IN_PROGRESS maintenance", "Unprocessable Entity")

        previous_st = m.status
        m.status = MaintenanceStatusEnum.CANCELLED.value
        m.cancelled_at = canc_dt.replace(microsecond=0)
        m.cancellation_reason = payload.cancellation_reason

        history = MaintenanceHistoryEventModel(
            maintenance_id=m.id,
            previous_status=previous_st,
            new_status=MaintenanceStatusEnum.CANCELLED.value,
            effective_at=canc_dt.replace(microsecond=0),
            actor_id=current_user.id,
            reason=payload.cancellation_reason,
        )
        db.add(history)

        if was_in_progress and payload.next_vehicle_state:
            other_active = db.scalars(
                select(MaintenanceModel).where(
                    MaintenanceModel.vehicle_id == m.vehicle_id,
                    MaintenanceModel.id != m.id,
                    MaintenanceModel.status.in_([MaintenanceStatusEnum.SCHEDULED.value, MaintenanceStatusEnum.IN_PROGRESS.value]),
                )
            ).first()
            if not other_active:
                vehicle = VehicleController.get_vehicle_by_id(db, m.vehicle_id)
                vehicle.current_state = payload.next_vehicle_state.value

        db.commit()
        db.refresh(m)
        return m

    @classmethod
    def list_history(
        cls,
        db: Session,
        current_user: UserModel,
        maintenance_id: int,
        limit: int,
        after_id: Optional[int],
    ) -> MaintenanceHistoryListResponse:
        cls.get_maintenance_by_id(db, current_user, maintenance_id)
        query = select(MaintenanceHistoryEventModel).where(
            MaintenanceHistoryEventModel.maintenance_id == maintenance_id
        )
        if after_id is not None:
            if after_id <= 0:
                raise AppException(422, "after_id must be a positive integer", "Unprocessable Entity")
            query = query.where(MaintenanceHistoryEventModel.id > after_id)

        query = query.order_by(MaintenanceHistoryEventModel.id.asc()).limit(limit + 1)
        events = list(db.scalars(query).all())
        has_more = len(events) > limit
        items = events[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return MaintenanceHistoryListResponse(
            items=[
                MaintenanceHistoryEventResponse(
                    id=e.id,
                    maintenance_id=e.maintenance_id,
                    previous_status=e.previous_status,
                    new_status=MaintenanceStatusEnum(e.new_status),
                    effective_at=e.effective_at,
                    actor_id=e.actor_id,
                    reason=e.reason,
                )
                for e in items
            ],
            next_after_id=next_id,
        )

    @classmethod
    def list_vehicle_maintenance_history(
        cls,
        db: Session,
        vehicle_id: int,
        status: Optional[str],
        type_val: Optional[str],
        limit: int,
        after_id: Optional[int],
    ) -> MaintenanceListResponse:
        VehicleController.get_vehicle_by_id(db, vehicle_id)
        query = select(MaintenanceModel).where(MaintenanceModel.vehicle_id == vehicle_id)

        if status is not None:
            try:
                st_enum = MaintenanceStatusEnum(status)
                query = query.where(MaintenanceModel.status == st_enum.value)
            except ValueError:
                raise AppException(422, f"Invalid status filter: {status}", "Unprocessable Entity")

        if type_val is not None:
            try:
                t_enum = MaintenanceTypeEnum(type_val)
                query = query.where(MaintenanceModel.type == t_enum.value)
            except ValueError:
                raise AppException(422, f"Invalid type filter: {type_val}", "Unprocessable Entity")

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
            items=[cls.serialize_maintenance(m) for m in items],
            next_after_id=next_id,
        )

    @classmethod
    def check_maintenance_availability(
        cls,
        db: Session,
        vehicle_id: int,
        planned_start: datetime,
        planned_end: datetime,
        exclude_maintenance_id: Optional[int],
        limit: int,
        after_id: Optional[int],
    ) -> MaintenanceListResponse:
        VehicleController.get_vehicle_by_id(db, vehicle_id)
        if planned_start.tzinfo is None:
            planned_start = planned_start.replace(tzinfo=timezone.utc)
        if planned_end.tzinfo is None:
            planned_end = planned_end.replace(tzinfo=timezone.utc)

        if planned_end <= planned_start:
            raise AppException(400, "planned_end must be strictly later than planned_start", "Bad Request")

        query = select(MaintenanceModel).where(
            MaintenanceModel.vehicle_id == vehicle_id,
            MaintenanceModel.status.in_([MaintenanceStatusEnum.SCHEDULED.value, MaintenanceStatusEnum.IN_PROGRESS.value]),
        )
        if exclude_maintenance_id:
            if exclude_maintenance_id <= 0:
                raise AppException(400, "exclude_maintenance_id must be positive", "Bad Request")
            query = query.where(MaintenanceModel.id != exclude_maintenance_id)

        if after_id is not None:
            if after_id <= 0:
                raise AppException(422, "after_id must be positive", "Unprocessable Entity")
            query = query.where(MaintenanceModel.id > after_id)

        query = query.order_by(MaintenanceModel.id.asc())
        candidates = list(db.scalars(query).all())

        conflicts = [
            m for m in candidates
            if max(planned_start, m.planned_start.replace(tzinfo=timezone.utc) if m.planned_start.tzinfo is None else m.planned_start)
            < min(planned_end, m.planned_end.replace(tzinfo=timezone.utc) if m.planned_end.tzinfo is None else m.planned_end)
        ]
        has_more = len(conflicts) > limit
        items = conflicts[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return MaintenanceListResponse(
            items=[cls.serialize_maintenance(m) for m in items],
            next_after_id=next_id,
        )

    @staticmethod
    def serialize_maintenance(m: MaintenanceModel) -> MaintenanceResponse:
        return MaintenanceResponse(
            id=m.id,
            vehicle_id=m.vehicle_id,
            requester_id=m.requester_id,
            type=MaintenanceTypeEnum(m.type),
            title=m.title,
            description=m.description,
            status=MaintenanceStatusEnum(m.status),
            planned_start=m.planned_start,
            planned_end=m.planned_end,
            actual_start=m.actual_start,
            actual_end=m.actual_end,
            completion_notes=m.completion_notes,
            cancelled_at=m.cancelled_at,
            cancellation_reason=m.cancellation_reason,
        )


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

        has_full_match = False
        for route in self.routes:
            match_status, _ = route.matches(scope)
            if match_status == Match.FULL:
                has_full_match = True
                break

        if not has_full_match:
            await self.app(scope, receive, send)
            return

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

        body_chunks = []
        total_len = 0
        max_size = 10 * 1024 * 1024

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
    user_1 = UserModel(
        username="user1",
        role=UserRole.USER.value,
        token_hash=hash_token("user1-token-secret-12345"),
    )
    user_2 = UserModel(
        username="user2",
        role=UserRole.USER.value,
        token_hash=hash_token("user2-token-secret-12345"),
    )
    db.add_all([admin_user, user_1, user_2])
    db.commit()

    v1 = VehicleModel(
        license_plate="MN-101-AA",
        manufacturer="Scania",
        model="S500",
        year=2021,
        current_state=VehicleStateEnum.AVAILABLE.value,
        active=True,
    )
    v2 = VehicleModel(
        license_plate="MN-102-BB",
        manufacturer="MAN",
        model="TGX",
        year=2023,
        current_state=VehicleStateEnum.MAINTENANCE.value,
        active=True,
    )
    db.add_all([v1, v2])
    db.flush()

    m1 = MaintenanceModel(
        vehicle_id=v2.id,
        requester_id=user_1.id,
        type=MaintenanceTypeEnum.PREVENTIVE.value,
        title="Annual Brake System Overhaul",
        description="Comprehensive pad, rotor, and sensor replacement",
        status=MaintenanceStatusEnum.SCHEDULED.value,
        planned_start=datetime(2026, 9, 15, 8, 0, 0, tzinfo=timezone.utc),
        planned_end=datetime(2026, 9, 15, 17, 0, 0, tzinfo=timezone.utc),
    )
    db.add(m1)
    db.flush()

    h1 = MaintenanceHistoryEventModel(
        maintenance_id=m1.id,
        previous_status=None,
        new_status=MaintenanceStatusEnum.SCHEDULED.value,
        effective_at=datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc),
        actor_id=user_1.id,
        reason="Initial schedule created",
    )
    db.add(h1)
    db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    with SessionLocal() as db:
        seed_database(db)
    yield


# -----------------------------------------------------------------------------
# FastAPI Application & Routes
# -----------------------------------------------------------------------------
app = FastAPI(
    title="Corporate Fleet Management - Maintenance Planning & History API",
    version="1.0.0",
    description="Deterministic RESTful API for maintenance planning, status transitions, cancellation tracking, and immutable audit history.",
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
    summary="Health check probe",
    response_model=HealthResponse,
    responses={
        200: {"description": "Service operational and database connected", "model": HealthResponse},
        503: {"description": "Database connection probe failed", "model": ErrorResponse},
    },
)
def get_health(db: Session = Depends(get_db)):
    """The system shall expose GET /health publicly with no input or authorization requirement."""
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
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        422: {"description": "Invalid query parameters", "model": ErrorResponse},
    },
)
def list_vehicles(
    active: Optional[bool] = Query(None, description="Filter by active flag"),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    admin: UserModel = Depends(AuthController.require_admin),
):
    """The system shall expose GET /vehicles as an admin-only endpoint with stable pagination."""
    return VehicleController.list_vehicles(db, active, limit, after_id)


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
    include_cancelled: bool = Query(True, description="Include cancelled maintenances"),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /maintenances restricting standard users to requester-owned records."""
    return MaintenanceController.list_maintenances(
        db, current_user, vehicle_id, status, type, from_dt, to_dt, include_cancelled, limit, after_id
    )


@app.post(
    "/maintenances",
    tags=["Maintenances"],
    summary="Schedule maintenance",
    status_code=201,
    response_model=MaintenanceResponse,
    responses={
        201: {"description": "Maintenance scheduled successfully", "model": MaintenanceResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
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
    """The system shall expose POST /maintenances creating SCHEDULED record and initial history event."""
    m = MaintenanceController.create_maintenance(db, current_user, payload)
    response.headers["Location"] = f"/maintenances/{m.id}"
    return MaintenanceController.serialize_maintenance(m)


@app.get(
    "/maintenances/{maintenance_id}",
    tags=["Maintenances"],
    summary="Get maintenance details",
    response_model=MaintenanceResponse,
    responses={
        200: {"description": "Maintenance retrieved successfully", "model": MaintenanceResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Maintenance not found", "model": ErrorResponse},
    },
)
def get_maintenance(
    maintenance_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /maintenances/{maintenance_id} returning record to requester or admin."""
    m = MaintenanceController.get_maintenance_by_id(db, current_user, maintenance_id)
    return MaintenanceController.serialize_maintenance(m)


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
        409: {"description": "Maintenance not SCHEDULED or interval overlap", "model": ErrorResponse},
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
    """The system shall expose PATCH /maintenances/{maintenance_id} updating only SCHEDULED records."""
    m = MaintenanceController.patch_maintenance(db, current_user, maintenance_id, payload)
    return MaintenanceController.serialize_maintenance(m)


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
        409: {"description": "Record not SCHEDULED or vehicle state != MAINTENANCE", "model": ErrorResponse},
        415: {"description": "Unsupported media type", "model": ErrorResponse},
        422: {"description": "Malformed body", "model": ErrorResponse},
    },
)
def start_maintenance(
    maintenance_id: int,
    payload: MaintenanceStartRequest,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose POST /maintenances/{maintenance_id}/start moving record to IN_PROGRESS."""
    m = MaintenanceController.start_maintenance(db, current_user, maintenance_id, payload)
    return MaintenanceController.serialize_maintenance(m)


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
        409: {"description": "Record not IN_PROGRESS or invalid actual_end", "model": ErrorResponse},
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
    return MaintenanceController.serialize_maintenance(m)


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
        409: {"description": "Record already terminal or invalid cancelled_at", "model": ErrorResponse},
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
    """The system shall expose POST /maintenances/{maintenance_id}/cancel retaining immutable history."""
    m = MaintenanceController.cancel_maintenance(db, current_user, maintenance_id, payload)
    return MaintenanceController.serialize_maintenance(m)


@app.get(
    "/maintenances/{maintenance_id}/history",
    tags=["Maintenances"],
    summary="List maintenance status history events",
    response_model=MaintenanceHistoryListResponse,
    responses={
        200: {"description": "History events listed successfully", "model": MaintenanceHistoryListResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Maintenance not found", "model": ErrorResponse},
        422: {"description": "Invalid pagination parameters", "model": ErrorResponse},
    },
)
def get_maintenance_history(
    maintenance_id: int,
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /maintenances/{maintenance_id}/history returning immutable status events."""
    return MaintenanceController.list_history(db, current_user, maintenance_id, limit, after_id)


@app.get(
    "/vehicles/{vehicle_id}/maintenance-history",
    tags=["Vehicles"],
    summary="List vehicle maintenance records history",
    response_model=MaintenanceListResponse,
    responses={
        200: {"description": "Vehicle maintenance history returned", "model": MaintenanceListResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Vehicle not found", "model": ErrorResponse},
        422: {"description": "Invalid query parameters", "model": ErrorResponse},
    },
)
def get_vehicle_maintenance_history(
    vehicle_id: int,
    status: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    admin: UserModel = Depends(AuthController.require_admin),
):
    """The system shall expose GET /vehicles/{vehicle_id}/maintenance-history as an admin-only endpoint."""
    return MaintenanceController.list_vehicle_maintenance_history(db, vehicle_id, status, type, limit, after_id)


@app.get(
    "/vehicles/{vehicle_id}/maintenance-availability",
    tags=["Vehicles"],
    summary="Check maintenance availability interval",
    response_model=MaintenanceListResponse,
    responses={
        200: {"description": "Active overlapping maintenance returned", "model": MaintenanceListResponse},
        400: {"description": "Invalid interval", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        404: {"description": "Vehicle not found", "model": ErrorResponse},
        422: {"description": "Invalid query syntax", "model": ErrorResponse},
    },
)
def check_maintenance_availability(
    vehicle_id: int,
    planned_start: datetime = Query(...),
    planned_end: datetime = Query(...),
    exclude_maintenance_id: Optional[int] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /vehicles/{vehicle_id}/maintenance-availability returning active overlapping records."""
    return MaintenanceController.check_maintenance_availability(
        db, vehicle_id, planned_start, planned_end, exclude_maintenance_id, limit, after_id
    )
