"""
Temporal Conflicts Among Trips, Drivers, and Vehicles Backend API
Domain: Corporate fleet management
Variant: Temporal conflicts among trips, drivers, and vehicles (fleet-temporal-conflict-v1)
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
from datetime import datetime, timedelta, timezone
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
logger = logging.getLogger("temporal_conflicts_service")

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


def as_utc(value: datetime) -> datetime:
    """Normalize SQLite's naive datetimes before domain comparisons."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
class VehicleStatusEnum(str, enum.Enum):
    AVAILABLE = "AVAILABLE"
    ASSIGNED = "ASSIGNED"
    OUT_OF_SERVICE = "OUT_OF_SERVICE"


class TripStatusEnum(str, enum.Enum):
    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


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


class DriverModel(Base):
    __tablename__ = "drivers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    trips: Mapped[List["TripModel"]] = relationship("TripModel", back_populates="driver")


class VehicleModel(Base):
    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    license_plate: Mapped[str] = mapped_column(String(12), unique=True, nullable=False)
    manufacturer: Mapped[str] = mapped_column(String(60), nullable=False)
    model: Mapped[str] = mapped_column(String(60), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=VehicleStatusEnum.AVAILABLE.value)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    trips: Mapped[List["TripModel"]] = relationship("TripModel", back_populates="vehicle")


class TripModel(Base):
    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    driver_id: Mapped[int] = mapped_column(Integer, ForeignKey("drivers.id"), nullable=False)
    vehicle_id: Mapped[int] = mapped_column(Integer, ForeignKey("vehicles.id"), nullable=False)
    requester_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    purpose: Mapped[str] = mapped_column(String(250), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=TripStatusEnum.PLANNED.value)
    planned_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    planned_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actual_start: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    actual_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancellation_reason: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    driver: Mapped[DriverModel] = relationship("DriverModel", back_populates="trips")
    vehicle: Mapped[VehicleModel] = relationship("VehicleModel", back_populates="trips")
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


class DriverCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_code: str = Field(..., pattern=r"^[A-Z0-9-]{3,20}$")
    full_name: str = Field(..., min_length=3, max_length=120)
    active: bool = True

    @field_validator("employee_code")
    @classmethod
    def normalize_code(cls, v: str) -> str:
        return v.strip().upper()


class DriverResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    employee_code: str
    full_name: str
    active: bool


class DriverListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[DriverResponse]
    next_after_id: Optional[int]


class TripCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    driver_id: int = Field(..., gt=0)
    vehicle_id: int = Field(..., gt=0)
    purpose: str = Field(..., min_length=3, max_length=250)
    planned_start: datetime
    planned_end: datetime


class TripPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: Optional[str] = Field(None, min_length=3, max_length=250)
    driver_id: Optional[int] = Field(None, gt=0)
    vehicle_id: Optional[int] = Field(None, gt=0)
    planned_start: Optional[datetime] = None
    planned_end: Optional[datetime] = None

    @model_validator(mode="after")
    def reject_null_for_non_nullable_fields(self):
        for field_name in ("purpose", "driver_id", "vehicle_id", "planned_start", "planned_end"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class TripStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actual_start: datetime


class TripCompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actual_end: datetime


class TripCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cancelled_at: datetime
    cancellation_reason: str = Field(..., min_length=3, max_length=500)


class TripResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    driver_id: int
    vehicle_id: int
    requester_id: int
    purpose: str
    status: TripStatusEnum
    planned_start: datetime
    planned_end: datetime
    actual_start: Optional[datetime]
    actual_end: Optional[datetime]
    cancelled_at: Optional[datetime]
    cancellation_reason: Optional[str]


class TripListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[TripResponse]
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


class DriverController:
    @staticmethod
    def list_drivers(
        db: Session,
        active: Optional[bool],
        employee_code_prefix: Optional[str],
        limit: int,
        after_id: Optional[int],
    ) -> DriverListResponse:
        query = select(DriverModel)
        if after_id is not None:
            if after_id <= 0:
                raise AppException(422, "after_id must be a positive integer", "Unprocessable Entity")
            query = query.where(DriverModel.id > after_id)

        if active is not None:
            query = query.where(DriverModel.active == active)

        if employee_code_prefix is not None:
            norm_prefix = employee_code_prefix.strip().upper()
            if not (1 <= len(norm_prefix) <= 20):
                raise AppException(400, "employee_code_prefix length must be between 1 and 20", "Bad Request")
            query = query.where(DriverModel.employee_code.like(f"{norm_prefix}%"))

        query = query.order_by(DriverModel.id.asc()).limit(limit + 1)
        drivers = list(db.scalars(query).all())
        has_more = len(drivers) > limit
        items = drivers[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return DriverListResponse(
            items=[
                DriverResponse(
                    id=d.id,
                    employee_code=d.employee_code,
                    full_name=d.full_name,
                    active=d.active,
                )
                for d in items
            ],
            next_after_id=next_id,
        )

    @staticmethod
    def create_driver(db: Session, payload: DriverCreateRequest) -> DriverModel:
        if db.scalar(select(DriverModel).where(DriverModel.employee_code == payload.employee_code)):
            raise AppException(409, f"Driver with employee code '{payload.employee_code}' already exists", "Conflict")

        driver = DriverModel(
            employee_code=payload.employee_code,
            full_name=payload.full_name,
            active=payload.active,
        )
        db.add(driver)
        db.commit()
        db.refresh(driver)
        return driver

    @staticmethod
    def get_driver_by_id(db: Session, driver_id: int) -> DriverModel:
        if driver_id <= 0:
            raise AppException(404, "Driver id must be positive", "Not Found")
        driver = db.scalar(select(DriverModel).where(DriverModel.id == driver_id))
        if not driver:
            raise AppException(404, f"Driver {driver_id} not found", "Not Found")
        return driver


class ConflictChecker:
    @staticmethod
    def check_intervals_overlap(s1: datetime, e1: datetime, s2: datetime, e2: datetime) -> bool:
        # Touching endpoints do not overlap: intervals sharing an endpoint without overlap are permitted
        return max(s1, s2) < min(e1, e2)

    @classmethod
    def check_driver_conflict(
        cls,
        db: Session,
        driver_id: int,
        start: datetime,
        end: datetime,
        exclude_trip_id: Optional[int] = None,
    ):
        query = select(TripModel).where(
            TripModel.driver_id == driver_id,
            TripModel.status.in_([TripStatusEnum.PLANNED.value, TripStatusEnum.IN_PROGRESS.value]),
        )
        if exclude_trip_id:
            query = query.where(TripModel.id != exclude_trip_id)
        active_trips = db.scalars(query).all()

        for t in active_trips:
            t_s = t.actual_start or t.planned_start
            if t_s.tzinfo is None:
                t_s = t_s.replace(tzinfo=timezone.utc)
            t_e = t.planned_end
            if t_e.tzinfo is None:
                t_e = t_e.replace(tzinfo=timezone.utc)
            if cls.check_intervals_overlap(start, end, t_s, t_e):
                raise AppException(409, f"Temporal conflict: driver is assigned to active trip {t.id}", "Conflict")

    @classmethod
    def check_vehicle_conflict(
        cls,
        db: Session,
        vehicle_id: int,
        start: datetime,
        end: datetime,
        exclude_trip_id: Optional[int] = None,
    ):
        query = select(TripModel).where(
            TripModel.vehicle_id == vehicle_id,
            TripModel.status.in_([TripStatusEnum.PLANNED.value, TripStatusEnum.IN_PROGRESS.value]),
        )
        if exclude_trip_id:
            query = query.where(TripModel.id != exclude_trip_id)
        active_trips = db.scalars(query).all()

        for t in active_trips:
            t_s = t.actual_start or t.planned_start
            if t_s.tzinfo is None:
                t_s = t_s.replace(tzinfo=timezone.utc)
            t_e = t.planned_end
            if t_e.tzinfo is None:
                t_e = t_e.replace(tzinfo=timezone.utc)
            if cls.check_intervals_overlap(start, end, t_s, t_e):
                raise AppException(409, f"Temporal conflict: vehicle is assigned to active trip {t.id}", "Conflict")


class TripController:
    @classmethod
    def list_trips(
        cls,
        db: Session,
        current_user: UserModel,
        driver_id: Optional[int],
        vehicle_id: Optional[int],
        status: Optional[str],
        from_dt: Optional[datetime],
        to_dt: Optional[datetime],
        limit: int,
        after_id: Optional[int],
    ) -> TripListResponse:
        query = select(TripModel)
        if current_user.role != UserRole.ADMIN.value:
            query = query.where(TripModel.requester_id == current_user.id)

        if driver_id is not None:
            if driver_id <= 0:
                raise AppException(404, "driver_id must be positive", "Not Found")
            if not db.scalar(select(DriverModel.id).where(DriverModel.id == driver_id)):
                raise AppException(404, f"Driver {driver_id} not found", "Not Found")
            query = query.where(TripModel.driver_id == driver_id)

        if vehicle_id is not None:
            if vehicle_id <= 0:
                raise AppException(404, "vehicle_id must be positive", "Not Found")
            if not db.scalar(select(VehicleModel.id).where(VehicleModel.id == vehicle_id)):
                raise AppException(404, f"Vehicle {vehicle_id} not found", "Not Found")
            query = query.where(TripModel.vehicle_id == vehicle_id)

        if status is not None:
            try:
                st_enum = TripStatusEnum(status)
                query = query.where(TripModel.status == st_enum.value)
            except ValueError:
                raise AppException(400, f"Invalid trip status: {status}", "Bad Request")

        if from_dt and to_dt:
            if from_dt > to_dt:
                raise AppException(400, "'from' date cannot be later than 'to' date", "Bad Request")
        if from_dt:
            query = query.where(TripModel.planned_start >= from_dt)
        if to_dt:
            query = query.where(TripModel.planned_end <= to_dt)

        if after_id is not None:
            if after_id <= 0:
                raise AppException(422, "after_id must be a positive integer", "Unprocessable Entity")
            query = query.where(TripModel.id > after_id)

        query = query.order_by(TripModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return TripListResponse(
            items=[cls.serialize_trip(t) for t in items],
            next_after_id=next_id,
        )

    @classmethod
    def create_trip(
        cls,
        db: Session,
        current_user: UserModel,
        payload: TripCreateRequest,
    ) -> TripModel:
        if payload.driver_id <= 0:
            raise AppException(404, "driver_id must be positive", "Not Found")
        if payload.vehicle_id <= 0:
            raise AppException(404, "vehicle_id must be positive", "Not Found")

        driver = db.scalar(select(DriverModel).where(DriverModel.id == payload.driver_id))
        if not driver:
            raise AppException(404, f"Driver {payload.driver_id} not found", "Not Found")
        if not driver.active:
            raise AppException(409, f"Driver {payload.driver_id} is inactive", "Conflict")

        vehicle = db.scalar(select(VehicleModel).where(VehicleModel.id == payload.vehicle_id))
        if not vehicle:
            raise AppException(404, f"Vehicle {payload.vehicle_id} not found", "Not Found")
        if not vehicle.active:
            raise AppException(409, f"Vehicle {payload.vehicle_id} is inactive", "Conflict")
        if vehicle.status == VehicleStatusEnum.OUT_OF_SERVICE.value:
            raise AppException(409, f"Vehicle {payload.vehicle_id} is OUT_OF_SERVICE", "Conflict")

        start = payload.planned_start
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end = payload.planned_end
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        if end <= start:
            raise AppException(400, "planned_end must be strictly later than planned_start", "Bad Request")

        ConflictChecker.check_driver_conflict(db, driver.id, start, end)
        ConflictChecker.check_vehicle_conflict(db, vehicle.id, start, end)

        trip = TripModel(
            driver_id=driver.id,
            vehicle_id=vehicle.id,
            requester_id=current_user.id,
            purpose=payload.purpose,
            status=TripStatusEnum.PLANNED.value,
            planned_start=start.replace(microsecond=0),
            planned_end=end.replace(microsecond=0),
        )
        db.add(trip)
        db.commit()
        db.refresh(trip)
        return trip

    @classmethod
    def get_trip_by_id(
        cls,
        db: Session,
        current_user: UserModel,
        trip_id: int,
    ) -> TripModel:
        if trip_id <= 0:
            raise AppException(404, "Trip id must be positive", "Not Found")
        t = db.scalar(select(TripModel).where(TripModel.id == trip_id))
        if not t:
            raise AppException(404, f"Trip {trip_id} not found", "Not Found")
        if current_user.role != UserRole.ADMIN.value and t.requester_id != current_user.id:
            raise AppException(403, "Access denied to trip owned by another user", "Forbidden")
        return t

    @classmethod
    def patch_trip(
        cls,
        db: Session,
        current_user: UserModel,
        trip_id: int,
        payload: TripPatchRequest,
    ) -> TripModel:
        trip = cls.get_trip_by_id(db, current_user, trip_id)
        if trip.status != TripStatusEnum.PLANNED.value:
            raise AppException(409, f"Only PLANNED trips can be modified; current status is {trip.status}", "Conflict")

        data = payload.model_dump(exclude_unset=True)
        if not data:
            raise AppException(400, "Patch payload cannot be empty", "Bad Request")

        new_driver_id = data.get("driver_id", trip.driver_id)
        new_vehicle_id = data.get("vehicle_id", trip.vehicle_id)

        if "driver_id" in data:
            driver = db.scalar(select(DriverModel).where(DriverModel.id == new_driver_id))
            if not driver:
                raise AppException(404, f"Driver {new_driver_id} not found", "Not Found")
            if not driver.active:
                raise AppException(409, f"Driver {new_driver_id} is inactive", "Conflict")

        if "vehicle_id" in data:
            vehicle = db.scalar(select(VehicleModel).where(VehicleModel.id == new_vehicle_id))
            if not vehicle:
                raise AppException(404, f"Vehicle {new_vehicle_id} not found", "Not Found")
            if not vehicle.active:
                raise AppException(409, f"Vehicle {new_vehicle_id} is inactive", "Conflict")
            if vehicle.status == VehicleStatusEnum.OUT_OF_SERVICE.value:
                raise AppException(409, f"Vehicle {new_vehicle_id} is OUT_OF_SERVICE", "Conflict")

        new_start = data.get("planned_start", trip.planned_start)
        if new_start.tzinfo is None:
            new_start = new_start.replace(tzinfo=timezone.utc)
        new_end = data.get("planned_end", trip.planned_end)
        if new_end.tzinfo is None:
            new_end = new_end.replace(tzinfo=timezone.utc)

        if new_end <= new_start:
            raise AppException(400, "planned_end must be strictly later than planned_start", "Bad Request")

        ConflictChecker.check_driver_conflict(db, new_driver_id, new_start, new_end, exclude_trip_id=trip.id)
        ConflictChecker.check_vehicle_conflict(db, new_vehicle_id, new_start, new_end, exclude_trip_id=trip.id)

        if "purpose" in data:
            trip.purpose = data["purpose"]
        trip.driver_id = new_driver_id
        trip.vehicle_id = new_vehicle_id
        trip.planned_start = new_start.replace(microsecond=0)
        trip.planned_end = new_end.replace(microsecond=0)

        db.commit()
        db.refresh(trip)
        return trip

    @classmethod
    def start_trip(
        cls,
        db: Session,
        current_user: UserModel,
        trip_id: int,
        payload: TripStartRequest,
    ) -> TripModel:
        trip = cls.get_trip_by_id(db, current_user, trip_id)
        if trip.status != TripStatusEnum.PLANNED.value:
            raise AppException(409, f"Cannot start trip in status {trip.status}", "Conflict")

        start_dt = payload.actual_start
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        plan_start = trip.planned_start
        if plan_start.tzinfo is None:
            plan_start = plan_start.replace(tzinfo=timezone.utc)

        if start_dt < plan_start:
            raise AppException(409, "actual_start cannot precede planned_start", "Conflict")

        vehicle = db.scalar(select(VehicleModel).where(VehicleModel.id == trip.vehicle_id))
        if not vehicle or vehicle.status != VehicleStatusEnum.AVAILABLE.value:
            current_v_status = vehicle.status if vehicle else "UNKNOWN"
            raise AppException(409, f"Vehicle must be AVAILABLE to start trip; currently {current_v_status}", "Conflict")

        plan_end = trip.planned_end
        if plan_end.tzinfo is None:
            plan_end = plan_end.replace(tzinfo=timezone.utc)

        ConflictChecker.check_driver_conflict(db, trip.driver_id, start_dt, plan_end, exclude_trip_id=trip.id)
        ConflictChecker.check_vehicle_conflict(db, trip.vehicle_id, start_dt, plan_end, exclude_trip_id=trip.id)

        trip.status = TripStatusEnum.IN_PROGRESS.value
        trip.actual_start = start_dt.replace(microsecond=0)
        vehicle.status = VehicleStatusEnum.ASSIGNED.value

        db.commit()
        db.refresh(trip)
        return trip

    @classmethod
    def complete_trip(
        cls,
        db: Session,
        current_user: UserModel,
        trip_id: int,
        payload: TripCompleteRequest,
    ) -> TripModel:
        trip = cls.get_trip_by_id(db, current_user, trip_id)
        if trip.status != TripStatusEnum.IN_PROGRESS.value:
            raise AppException(409, f"Cannot complete trip in status {trip.status}", "Conflict")

        end_dt = payload.actual_end
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        act_start = trip.actual_start or trip.planned_start
        if act_start.tzinfo is None:
            act_start = act_start.replace(tzinfo=timezone.utc)

        if end_dt < act_start:
            raise AppException(409, "actual_end cannot precede actual_start", "Conflict")

        plan_end = trip.planned_end
        if plan_end.tzinfo is None:
            plan_end = plan_end.replace(tzinfo=timezone.utc)
        max_end = plan_end + timedelta(hours=24)

        if end_dt > max_end:
            raise AppException(409, "actual_end cannot exceed planned_end plus 24 hours", "Conflict")

        trip.status = TripStatusEnum.COMPLETED.value
        trip.actual_end = end_dt.replace(microsecond=0)

        vehicle = db.scalar(select(VehicleModel).where(VehicleModel.id == trip.vehicle_id))
        other_active_trips = db.scalars(
            select(TripModel).where(
                TripModel.vehicle_id == trip.vehicle_id,
                TripModel.id != trip.id,
                TripModel.status.in_([TripStatusEnum.PLANNED.value, TripStatusEnum.IN_PROGRESS.value]),
            )
        ).first()

        if vehicle and not other_active_trips:
            vehicle.status = VehicleStatusEnum.AVAILABLE.value

        db.commit()
        db.refresh(trip)
        return trip

    @classmethod
    def cancel_trip(
        cls,
        db: Session,
        current_user: UserModel,
        trip_id: int,
        payload: TripCancelRequest,
    ) -> TripModel:
        trip = cls.get_trip_by_id(db, current_user, trip_id)
        if trip.status in (TripStatusEnum.COMPLETED.value, TripStatusEnum.CANCELLED.value):
            raise AppException(409, f"Cannot cancel terminal trip in status {trip.status}", "Conflict")

        canc_dt = payload.cancelled_at
        if canc_dt.tzinfo is None:
            canc_dt = canc_dt.replace(tzinfo=timezone.utc)

        trip.status = TripStatusEnum.CANCELLED.value
        trip.cancelled_at = canc_dt.replace(microsecond=0)
        trip.cancellation_reason = payload.cancellation_reason

        vehicle = db.scalar(select(VehicleModel).where(VehicleModel.id == trip.vehicle_id))
        other_active_trips = db.scalars(
            select(TripModel).where(
                TripModel.vehicle_id == trip.vehicle_id,
                TripModel.id != trip.id,
                TripModel.status.in_([TripStatusEnum.PLANNED.value, TripStatusEnum.IN_PROGRESS.value]),
            )
        ).first()

        if vehicle and not other_active_trips:
            vehicle.status = VehicleStatusEnum.AVAILABLE.value

        db.commit()
        db.refresh(trip)
        return trip

    @staticmethod
    def serialize_trip(t: TripModel) -> TripResponse:
        return TripResponse(
            id=t.id,
            driver_id=t.driver_id,
            vehicle_id=t.vehicle_id,
            requester_id=t.requester_id,
            purpose=t.purpose,
            status=TripStatusEnum(t.status),
            planned_start=t.planned_start,
            planned_end=t.planned_end,
            actual_start=t.actual_start,
            actual_end=t.actual_end,
            cancelled_at=t.cancelled_at,
            cancellation_reason=t.cancellation_reason,
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

    d1 = DriverModel(employee_code="EMP-001", full_name="Mario Rossi", active=True)
    d2 = DriverModel(employee_code="EMP-002", full_name="Luigi Bianchi", active=True)
    db.add_all([d1, d2])
    db.flush()

    v1 = VehicleModel(
        license_plate="FL-001-AA",
        manufacturer="Fiat",
        model="Ducato",
        year=2022,
        status=VehicleStatusEnum.AVAILABLE.value,
        active=True,
    )
    v2 = VehicleModel(
        license_plate="FL-002-BB",
        manufacturer="Iveco",
        model="Daily",
        year=2023,
        status=VehicleStatusEnum.AVAILABLE.value,
        active=True,
    )
    db.add_all([v1, v2])
    db.flush()

    t1 = TripModel(
        driver_id=d1.id,
        vehicle_id=v1.id,
        requester_id=user_1.id,
        purpose="Delivery to Northern Hub",
        status=TripStatusEnum.PLANNED.value,
        planned_start=datetime(2026, 9, 10, 8, 0, 0, tzinfo=timezone.utc),
        planned_end=datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc),
    )
    db.add(t1)
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
    title="Corporate Fleet Management - Temporal Conflicts API",
    version="1.0.0",
    description="Deterministic RESTful API for driver, vehicle, and trip scheduling with temporal conflict verification.",
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
    summary="Service health probe",
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
    "/drivers",
    tags=["Drivers"],
    summary="List drivers",
    response_model=DriverListResponse,
    responses={
        200: {"description": "Drivers listed successfully", "model": DriverListResponse},
        400: {"description": "Semantic query error", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        422: {"description": "Invalid parameter syntax", "model": ErrorResponse},
    },
)
def list_drivers(
    active: Optional[bool] = Query(None, description="Filter by active status"),
    employee_code_prefix: Optional[str] = Query(None, description="Filter by code prefix (1..20 chars)"),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    admin: UserModel = Depends(AuthController.require_admin),
):
    """The system shall expose GET /drivers as an admin-only endpoint accepting optional filters."""
    return DriverController.list_drivers(db, active, employee_code_prefix, limit, after_id)


@app.post(
    "/drivers",
    tags=["Drivers"],
    summary="Create a driver",
    status_code=201,
    response_model=DriverResponse,
    responses={
        201: {"description": "Driver created successfully", "model": DriverResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        409: {"description": "Employee code conflict", "model": ErrorResponse},
        415: {"description": "Unsupported media type", "model": ErrorResponse},
        422: {"description": "Malformed body", "model": ErrorResponse},
    },
)
def create_driver(
    payload: DriverCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
    admin: UserModel = Depends(AuthController.require_admin),
):
    """The system shall expose POST /drivers as an admin-only endpoint creating unique driver."""
    d = DriverController.create_driver(db, payload)
    response.headers["Location"] = f"/drivers/{d.id}"
    return DriverResponse(
        id=d.id,
        employee_code=d.employee_code,
        full_name=d.full_name,
        active=d.active,
    )


@app.get(
    "/trips",
    tags=["Trips"],
    summary="List trips",
    response_model=TripListResponse,
    responses={
        200: {"description": "Trips listed successfully", "model": TripListResponse},
        400: {"description": "Invalid filter parameters", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        404: {"description": "Driver or vehicle not found", "model": ErrorResponse},
        422: {"description": "Invalid query syntax", "model": ErrorResponse},
    },
)
def list_trips(
    driver_id: Optional[int] = Query(None),
    vehicle_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    from_dt: Optional[datetime] = Query(None, alias="from"),
    to_dt: Optional[datetime] = Query(None, alias="to"),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /trips restricting standard users to requester-owned trips."""
    return TripController.list_trips(db, current_user, driver_id, vehicle_id, status, from_dt, to_dt, limit, after_id)


@app.post(
    "/trips",
    tags=["Trips"],
    summary="Create a planned trip",
    status_code=201,
    response_model=TripResponse,
    responses={
        201: {"description": "Trip planned successfully", "model": TripResponse},
        400: {"description": "Semantic validation error", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        404: {"description": "Driver or vehicle not found", "model": ErrorResponse},
        409: {"description": "Driver or vehicle temporal conflict or inactive", "model": ErrorResponse},
        415: {"description": "Unsupported media type", "model": ErrorResponse},
        422: {"description": "Malformed body", "model": ErrorResponse},
    },
)
def create_trip(
    payload: TripCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose POST /trips validating driver and vehicle non-overlap before creating PLANNED trip."""
    trip = TripController.create_trip(db, current_user, payload)
    response.headers["Location"] = f"/trips/{trip.id}"
    return TripController.serialize_trip(trip)


@app.get(
    "/trips/{trip_id}",
    tags=["Trips"],
    summary="Get trip details",
    response_model=TripResponse,
    responses={
        200: {"description": "Trip retrieved successfully", "model": TripResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Trip not found", "model": ErrorResponse},
    },
)
def get_trip(
    trip_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /trips/{trip_id} returning trip only to requester or admin."""
    trip = TripController.get_trip_by_id(db, current_user, trip_id)
    return TripController.serialize_trip(trip)


@app.patch(
    "/trips/{trip_id}",
    tags=["Trips"],
    summary="Update planned trip",
    response_model=TripResponse,
    responses={
        200: {"description": "Trip updated successfully", "model": TripResponse},
        400: {"description": "Invalid values or empty patch", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Trip not found", "model": ErrorResponse},
        409: {"description": "Trip not PLANNED or conflict detected", "model": ErrorResponse},
        415: {"description": "Unsupported media type", "model": ErrorResponse},
        422: {"description": "Unprocessable payload", "model": ErrorResponse},
    },
)
def patch_trip(
    trip_id: int,
    payload: TripPatchRequest,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose PATCH /trips/{trip_id} permitting changes only to PLANNED trips without conflicts."""
    trip = TripController.patch_trip(db, current_user, trip_id, payload)
    return TripController.serialize_trip(trip)


@app.post(
    "/trips/{trip_id}/start",
    tags=["Trips"],
    summary="Start planned trip",
    response_model=TripResponse,
    responses={
        200: {"description": "Trip started successfully", "model": TripResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Trip not found", "model": ErrorResponse},
        409: {"description": "Trip not PLANNED, timestamp invalid, or vehicle not AVAILABLE", "model": ErrorResponse},
        415: {"description": "Unsupported media type", "model": ErrorResponse},
        422: {"description": "Malformed body", "model": ErrorResponse},
    },
)
def start_trip(
    trip_id: int,
    payload: TripStartRequest,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose POST /trips/{trip_id}/start moving trip to IN_PROGRESS and vehicle to ASSIGNED."""
    trip = TripController.start_trip(db, current_user, trip_id, payload)
    return TripController.serialize_trip(trip)


@app.post(
    "/trips/{trip_id}/complete",
    tags=["Trips"],
    summary="Complete in-progress trip",
    response_model=TripResponse,
    responses={
        200: {"description": "Trip completed successfully", "model": TripResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Trip not found", "model": ErrorResponse},
        409: {"description": "Trip not IN_PROGRESS or invalid actual_end", "model": ErrorResponse},
        415: {"description": "Unsupported media type", "model": ErrorResponse},
        422: {"description": "Malformed body", "model": ErrorResponse},
    },
)
def complete_trip(
    trip_id: int,
    payload: TripCompleteRequest,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose POST /trips/{trip_id}/complete moving trip to COMPLETED and vehicle to AVAILABLE."""
    trip = TripController.complete_trip(db, current_user, trip_id, payload)
    return TripController.serialize_trip(trip)


@app.post(
    "/trips/{trip_id}/cancel",
    tags=["Trips"],
    summary="Cancel planned or in-progress trip",
    response_model=TripResponse,
    responses={
        200: {"description": "Trip cancelled successfully", "model": TripResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Trip not found", "model": ErrorResponse},
        409: {"description": "Trip already terminal", "model": ErrorResponse},
        415: {"description": "Unsupported media type", "model": ErrorResponse},
        422: {"description": "Malformed body", "model": ErrorResponse},
    },
)
def cancel_trip(
    trip_id: int,
    payload: TripCancelRequest,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose POST /trips/{trip_id}/cancel retaining immutable cancellation history."""
    trip = TripController.cancel_trip(db, current_user, trip_id, payload)
    return TripController.serialize_trip(trip)


@app.get(
    "/drivers/{driver_id}/trip-conflicts",
    tags=["Drivers"],
    summary="Detect driver trip conflicts",
    response_model=TripListResponse,
    responses={
        200: {"description": "Conflicting trips returned", "model": TripListResponse},
        400: {"description": "Invalid time interval", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Driver not found", "model": ErrorResponse},
        422: {"description": "Invalid query syntax", "model": ErrorResponse},
    },
)
def get_driver_trip_conflicts(
    driver_id: int,
    planned_start: datetime = Query(...),
    planned_end: datetime = Query(...),
    exclude_trip_id: Optional[int] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    admin: UserModel = Depends(AuthController.require_admin),
):
    """The system shall expose GET /drivers/{driver_id}/trip-conflicts returning active overlapping trips."""
    DriverController.get_driver_by_id(db, driver_id)
    planned_start = as_utc(planned_start)
    planned_end = as_utc(planned_end)

    if planned_end <= planned_start:
        raise AppException(400, "planned_end must be strictly later than planned_start", "Bad Request")

    query = select(TripModel).where(
        TripModel.driver_id == driver_id,
        TripModel.status.in_([TripStatusEnum.PLANNED.value, TripStatusEnum.IN_PROGRESS.value]),
    )
    if exclude_trip_id:
        if exclude_trip_id <= 0:
            raise AppException(400, "exclude_trip_id must be positive", "Bad Request")
        query = query.where(TripModel.id != exclude_trip_id)

    if after_id is not None:
        if after_id <= 0:
            raise AppException(422, "after_id must be positive", "Unprocessable Entity")
        query = query.where(TripModel.id > after_id)

    query = query.order_by(TripModel.id.asc())
    candidates = list(db.scalars(query).all())

    conflicts = [
        t for t in candidates
        if ConflictChecker.check_intervals_overlap(
            planned_start, planned_end,
            as_utc(t.actual_start or t.planned_start), as_utc(t.planned_end)
        )
    ]
    has_more = len(conflicts) > limit
    items = conflicts[:limit]
    next_id = items[-1].id if (has_more and items) else None

    return TripListResponse(
        items=[TripController.serialize_trip(t) for t in items],
        next_after_id=next_id,
    )


@app.get(
    "/vehicles/{vehicle_id}/trip-conflicts",
    tags=["Vehicles"],
    summary="Detect vehicle trip conflicts",
    response_model=TripListResponse,
    responses={
        200: {"description": "Conflicting trips returned", "model": TripListResponse},
        400: {"description": "Invalid time interval", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Vehicle not found", "model": ErrorResponse},
        422: {"description": "Invalid query syntax", "model": ErrorResponse},
    },
)
def get_vehicle_trip_conflicts(
    vehicle_id: int,
    planned_start: datetime = Query(...),
    planned_end: datetime = Query(...),
    exclude_trip_id: Optional[int] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    admin: UserModel = Depends(AuthController.require_admin),
):
    """The system shall expose GET /vehicles/{vehicle_id}/trip-conflicts returning active overlapping trips."""
    if vehicle_id <= 0:
        raise AppException(404, "Vehicle id must be positive", "Not Found")
    vehicle = db.scalar(select(VehicleModel).where(VehicleModel.id == vehicle_id))
    if not vehicle:
        raise AppException(404, f"Vehicle {vehicle_id} not found", "Not Found")

    planned_start = as_utc(planned_start)
    planned_end = as_utc(planned_end)

    if planned_end <= planned_start:
        raise AppException(400, "planned_end must be strictly later than planned_start", "Bad Request")

    query = select(TripModel).where(
        TripModel.vehicle_id == vehicle_id,
        TripModel.status.in_([TripStatusEnum.PLANNED.value, TripStatusEnum.IN_PROGRESS.value]),
    )
    if exclude_trip_id:
        if exclude_trip_id <= 0:
            raise AppException(400, "exclude_trip_id must be positive", "Bad Request")
        query = query.where(TripModel.id != exclude_trip_id)

    if after_id is not None:
        if after_id <= 0:
            raise AppException(422, "after_id must be positive", "Unprocessable Entity")
        query = query.where(TripModel.id > after_id)

    query = query.order_by(TripModel.id.asc())
    candidates = list(db.scalars(query).all())

    conflicts = [
        t for t in candidates
        if ConflictChecker.check_intervals_overlap(
            planned_start, planned_end,
            as_utc(t.actual_start or t.planned_start), as_utc(t.planned_end)
        )
    ]
    has_more = len(conflicts) > limit
    items = conflicts[:limit]
    next_id = items[-1].id if (has_more and items) else None

    return TripListResponse(
        items=[TripController.serialize_trip(t) for t in items],
        next_after_id=next_id,
    )
