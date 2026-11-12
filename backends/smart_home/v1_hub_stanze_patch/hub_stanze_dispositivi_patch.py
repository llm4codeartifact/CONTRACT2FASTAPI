"""
Hubs, Rooms, Devices, and Concurrent PATCH Backend API
Domain: Smart home
Variant: Hubs, rooms, devices, and concurrent PATCH (smart-home-hub-rooms-patch-v1)
Architecture: MVC Pattern, FastAPI, Pydantic v2, SQLAlchemy ORM, SQLite In-Memory
"""

import enum
import hashlib
import http
import ipaddress
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    create_engine,
    func,
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
logger = logging.getLogger("hub_stanze_dispositivi_patch_service")

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
# Helper Security Utilities & Domain Validators
# -----------------------------------------------------------------------------
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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
class TimezoneEnum(str, enum.Enum):
    EUROPE_ROME = "Europe/Rome"
    EUROPE_LONDON = "Europe/London"
    UTC = "UTC"
    AMERICA_NEW_YORK = "America/New_York"
    ASIA_TOKYO = "Asia/Tokyo"


class DeviceTypeEnum(str, enum.Enum):
    SENSOR = "SENSOR"
    LIGHT = "LIGHT"
    SWITCH = "SWITCH"
    THERMOSTAT = "THERMOSTAT"
    LOCK = "LOCK"


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

    hubs: Mapped[List["HubModel"]] = relationship("HubModel", back_populates="owner")


class HubModel(Base):
    __tablename__ = "hubs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    serial: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    timezone: Mapped[str] = mapped_column(String(50), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    owner: Mapped[UserModel] = relationship("UserModel", back_populates="hubs")
    rooms: Mapped[List["RoomModel"]] = relationship("RoomModel", back_populates="hub")
    devices: Mapped[List["DeviceModel"]] = relationship("DeviceModel", back_populates="hub")


class RoomModel(Base):
    __tablename__ = "rooms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hub_id: Mapped[int] = mapped_column(Integer, ForeignKey("hubs.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    floor: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    hub: Mapped[HubModel] = relationship("HubModel", back_populates="rooms")
    devices: Mapped[List["DeviceModel"]] = relationship("DeviceModel", back_populates="room")


class DeviceModel(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hub_id: Mapped[int] = mapped_column(Integer, ForeignKey("hubs.id"), nullable=False)
    room_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("rooms.id"), nullable=True)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    address: Mapped[str] = mapped_column(String(12), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    hub: Mapped[HubModel] = relationship("HubModel", back_populates="devices")
    room: Mapped[Optional[RoomModel]] = relationship("RoomModel", back_populates="devices")


# -----------------------------------------------------------------------------
# Custom App Exception
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


# Hub Schemas
class HubCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    serial: str = Field(..., pattern=r"^[A-Z0-9-]{8,32}$")
    name: str = Field(..., min_length=1, max_length=80)
    timezone: TimezoneEnum
    owner_id: Optional[int] = Field(None, gt=0)


class HubPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    timezone: Optional[TimezoneEnum] = None

    @model_validator(mode="after")
    def validate_patch(self):
        if not self.model_fields_set:
            raise ValueError("Patch payload cannot be empty")
        for field_name in ("name", "timezone"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class HubResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    owner_id: int
    serial: str
    name: str
    timezone: TimezoneEnum
    revision: int
    etag: str


class HubListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[HubResponse]
    next_after_id: Optional[int]


# Room Schemas
class RoomCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hub_id: int = Field(..., gt=0)
    name: str = Field(..., min_length=1, max_length=60)
    floor: int = Field(..., ge=-5, le=200)


class RoomPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=60)
    floor: Optional[int] = Field(None, ge=-5, le=200)

    @model_validator(mode="after")
    def validate_patch(self):
        if not self.model_fields_set:
            raise ValueError("Patch payload cannot be empty")
        for field_name in ("name", "floor"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class RoomResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    hub_id: int
    name: str
    floor: int
    revision: int
    etag: str


class RoomListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[RoomResponse]
    next_after_id: Optional[int]


# Device Schemas
class DeviceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hub_id: int = Field(..., gt=0)
    room_id: Optional[int] = Field(None, gt=0)
    label: str = Field(..., min_length=1, max_length=80)
    address: str = Field(..., pattern=r"^[A-F0-9]{12}$")
    type: DeviceTypeEnum


class DevicePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: Optional[str] = Field(None, min_length=1, max_length=80)
    room_id: Optional[int] = None
    type: Optional[DeviceTypeEnum] = None

    @model_validator(mode="after")
    def validate_patch(self):
        if not self.model_fields_set:
            raise ValueError("Patch payload cannot be empty")
        for field_name in ("label", "type"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        if "room_id" in self.model_fields_set and self.room_id is not None and self.room_id <= 0:
            raise ValueError("room_id must be a positive integer or null")
        return self


class DeviceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    hub_id: int
    room_id: Optional[int]
    label: str
    address: str
    type: DeviceTypeEnum
    revision: int
    etag: str


class DeviceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[DeviceResponse]
    next_after_id: Optional[int]


# Topology Schema
class HubTopologyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hub: HubResponse
    rooms: List[RoomResponse]
    devices: List[DeviceResponse]


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
        if not token:
            raise AppException(401, "Missing session token", "Unauthorized")
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


def parse_and_validate_if_match(if_match: Optional[str]) -> int:
    if if_match is None or not if_match.strip():
        raise AppException(422, "If-Match header is required for PATCH", "Unprocessable Entity")
    raw = if_match.strip()
    match = re.fullmatch(r'"([1-9][0-9]*)"', raw)
    if not match:
        raise AppException(
            422,
            "Malformed If-Match header; must be a quoted strong ETag with a positive integer",
            "Unprocessable Entity",
        )
    return int(match.group(1))


class HubController:
    @staticmethod
    def serialize_hub(hub: HubModel) -> HubResponse:
        return HubResponse(
            id=hub.id,
            owner_id=hub.owner_id,
            serial=hub.serial,
            name=hub.name,
            timezone=TimezoneEnum(hub.timezone),
            revision=hub.revision,
            etag=f'"{hub.revision}"',
        )

    @classmethod
    def list_hubs(
        cls,
        db: Session,
        current_user: UserModel,
        owner_id: Optional[int],
        timezone: Optional[TimezoneEnum],
        limit: int,
        after_id: Optional[int],
    ) -> HubListResponse:
        if after_id is not None and after_id <= 0:
            raise AppException(422, "after_id must be a positive integer", "Unprocessable Entity")

        query = select(HubModel)

        if owner_id is not None:
            if owner_id <= 0:
                raise AppException(404, "owner_id must be a positive integer", "Not Found")
            if current_user.role == UserRole.USER.value and owner_id != current_user.id:
                raise AppException(403, "Users cannot list hubs of other owners", "Forbidden")
            owner = db.scalar(select(UserModel).where(UserModel.id == owner_id))
            if not owner:
                raise AppException(404, f"Owner {owner_id} not found", "Not Found")
            query = query.where(HubModel.owner_id == owner_id)
        else:
            if current_user.role == UserRole.USER.value:
                query = query.where(HubModel.owner_id == current_user.id)

        if timezone is not None:
            query = query.where(HubModel.timezone == timezone.value)

        if after_id is not None:
            query = query.where(HubModel.id > after_id)

        query = query.order_by(HubModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return HubListResponse(
            items=[cls.serialize_hub(h) for h in items],
            next_after_id=next_id,
        )

    @classmethod
    def get_hub_by_id(cls, db: Session, current_user: UserModel, hub_id: int) -> HubModel:
        if hub_id <= 0:
            raise AppException(404, "Hub id must be a positive integer", "Not Found")
        hub = db.scalar(select(HubModel).where(HubModel.id == hub_id))
        if not hub:
            raise AppException(404, f"Hub {hub_id} not found", "Not Found")
        if current_user.role != UserRole.ADMIN.value and hub.owner_id != current_user.id:
            raise AppException(403, "Access denied to hub owned by another user", "Forbidden")
        return hub

    @classmethod
    def create_hub(
        cls,
        db: Session,
        current_user: UserModel,
        payload: HubCreateRequest,
    ) -> HubModel:
        if payload.owner_id is not None:
            if current_user.role != UserRole.ADMIN.value:
                if payload.owner_id != current_user.id:
                    raise AppException(403, "Users cannot assign hubs to other owners", "Forbidden")
                target_owner_id = current_user.id
            else:
                if payload.owner_id <= 0:
                    raise AppException(404, "owner_id must be a positive integer", "Not Found")
                owner = db.scalar(select(UserModel).where(UserModel.id == payload.owner_id))
                if not owner:
                    raise AppException(404, f"Owner {payload.owner_id} not found", "Not Found")
                target_owner_id = payload.owner_id
        else:
            target_owner_id = current_user.id

        try:
            existing = db.scalar(select(HubModel).where(HubModel.serial == payload.serial))
            if existing:
                raise AppException(409, f"Hub with serial '{payload.serial}' already exists", "Conflict")

            hub = HubModel(
                owner_id=target_owner_id,
                serial=payload.serial,
                name=payload.name,
                timezone=payload.timezone.value,
                revision=1,
            )
            db.add(hub)
            db.commit()
            db.refresh(hub)
            return hub
        except AppException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error("Failed to create hub: %s", exc)
            raise AppException(500, "Failed to create hub", "Internal Server Error")

    @classmethod
    def patch_hub(
        cls,
        db: Session,
        current_user: UserModel,
        hub_id: int,
        payload: HubPatchRequest,
        if_match: Optional[str],
    ) -> HubModel:
        expected_revision = parse_and_validate_if_match(if_match)
        hub = cls.get_hub_by_id(db, current_user, hub_id)

        try:
            if expected_revision != hub.revision:
                raise AppException(
                    409,
                    f"Stale ETag: current revision is {hub.revision}",
                    "Conflict",
                    headers={"ETag": f'"{hub.revision}"'},
                )

            data = payload.model_dump(exclude_unset=True)
            if "name" in data:
                hub.name = data["name"]
            if "timezone" in data:
                hub.timezone = (
                    data["timezone"].value
                    if hasattr(data["timezone"], "value")
                    else str(data["timezone"])
                )

            hub.revision += 1
            db.commit()
            db.refresh(hub)
            return hub
        except AppException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error("Failed to patch hub: %s", exc)
            raise AppException(500, "Failed to patch hub", "Internal Server Error")

    @classmethod
    def delete_hub(cls, db: Session, current_user: UserModel, hub_id: int) -> None:
        hub = cls.get_hub_by_id(db, current_user, hub_id)
        try:
            room_count = db.scalar(select(func.count(RoomModel.id)).where(RoomModel.hub_id == hub.id)) or 0
            device_count = db.scalar(select(func.count(DeviceModel.id)).where(DeviceModel.hub_id == hub.id)) or 0
            if room_count > 0 or device_count > 0:
                raise AppException(
                    409,
                    "Cannot delete hub with associated rooms or devices under REQ-2-008",
                    "Conflict",
                )

            db.delete(hub)
            db.commit()
        except AppException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error("Failed to delete hub: %s", exc)
            raise AppException(500, "Failed to delete hub", "Internal Server Error")


class RoomController:
    @staticmethod
    def serialize_room(room: RoomModel) -> RoomResponse:
        return RoomResponse(
            id=room.id,
            hub_id=room.hub_id,
            name=room.name,
            floor=room.floor,
            revision=room.revision,
            etag=f'"{room.revision}"',
        )

    @classmethod
    def list_rooms(
        cls,
        db: Session,
        current_user: UserModel,
        hub_id: Optional[int],
        floor: Optional[int],
        limit: int,
        after_id: Optional[int],
    ) -> RoomListResponse:
        if after_id is not None and after_id <= 0:
            raise AppException(422, "after_id must be a positive integer", "Unprocessable Entity")

        query = select(RoomModel)

        if hub_id is not None:
            if hub_id <= 0:
                raise AppException(404, "hub_id must be a positive integer", "Not Found")
            hub = db.scalar(select(HubModel).where(HubModel.id == hub_id))
            if not hub:
                raise AppException(404, f"Hub {hub_id} not found", "Not Found")
            if current_user.role != UserRole.ADMIN.value and hub.owner_id != current_user.id:
                raise AppException(403, "Access denied to rooms in hub owned by another user", "Forbidden")
            query = query.where(RoomModel.hub_id == hub_id)
        else:
            if current_user.role == UserRole.USER.value:
                query = query.join(RoomModel.hub).where(HubModel.owner_id == current_user.id)

        if floor is not None:
            query = query.where(RoomModel.floor == floor)

        if after_id is not None:
            query = query.where(RoomModel.id > after_id)

        query = query.order_by(RoomModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return RoomListResponse(
            items=[cls.serialize_room(r) for r in items],
            next_after_id=next_id,
        )

    @classmethod
    def get_room_by_id(cls, db: Session, current_user: UserModel, room_id: int) -> RoomModel:
        if room_id <= 0:
            raise AppException(404, "Room id must be a positive integer", "Not Found")
        room = db.scalar(select(RoomModel).where(RoomModel.id == room_id))
        if not room:
            raise AppException(404, f"Room {room_id} not found", "Not Found")
        if current_user.role != UserRole.ADMIN.value and room.hub.owner_id != current_user.id:
            raise AppException(403, "Access denied to room in hub owned by another user", "Forbidden")
        return room

    @classmethod
    def create_room(
        cls,
        db: Session,
        current_user: UserModel,
        payload: RoomCreateRequest,
    ) -> RoomModel:
        if payload.hub_id <= 0:
            raise AppException(404, "hub_id must be a positive integer", "Not Found")
        hub = db.scalar(select(HubModel).where(HubModel.id == payload.hub_id))
        if not hub:
            raise AppException(404, f"Hub {payload.hub_id} not found", "Not Found")
        if current_user.role != UserRole.ADMIN.value and hub.owner_id != current_user.id:
            raise AppException(403, "Access denied: cannot create room in hub owned by another user", "Forbidden")

        try:
            existing = db.scalar(
                select(RoomModel).where(
                    RoomModel.hub_id == hub.id,
                    func.lower(RoomModel.name) == payload.name.lower(),
                )
            )
            if existing:
                raise AppException(409, f"Room with name '{payload.name}' already exists in this hub", "Conflict")

            room = RoomModel(
                hub_id=hub.id,
                name=payload.name,
                floor=payload.floor,
                revision=1,
            )
            db.add(room)
            db.commit()
            db.refresh(room)
            return room
        except AppException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error("Failed to create room: %s", exc)
            raise AppException(500, "Failed to create room", "Internal Server Error")

    @classmethod
    def patch_room(
        cls,
        db: Session,
        current_user: UserModel,
        room_id: int,
        payload: RoomPatchRequest,
        if_match: Optional[str],
    ) -> RoomModel:
        expected_revision = parse_and_validate_if_match(if_match)
        room = cls.get_room_by_id(db, current_user, room_id)

        try:
            if expected_revision != room.revision:
                raise AppException(
                    409,
                    f"Stale ETag: current revision is {room.revision}",
                    "Conflict",
                    headers={"ETag": f'"{room.revision}"'},
                )

            data = payload.model_dump(exclude_unset=True)
            if "name" in data:
                new_name = data["name"]
                existing = db.scalar(
                    select(RoomModel).where(
                        RoomModel.hub_id == room.hub_id,
                        func.lower(RoomModel.name) == new_name.lower(),
                        RoomModel.id != room.id,
                    )
                )
                if existing:
                    raise AppException(409, f"Room with name '{new_name}' already exists in this hub", "Conflict")
                room.name = new_name

            if "floor" in data:
                room.floor = data["floor"]

            room.revision += 1
            db.commit()
            db.refresh(room)
            return room
        except AppException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error("Failed to patch room: %s", exc)
            raise AppException(500, "Failed to patch room", "Internal Server Error")

    @classmethod
    def delete_room(cls, db: Session, current_user: UserModel, room_id: int) -> None:
        room = cls.get_room_by_id(db, current_user, room_id)
        try:
            device_count = db.scalar(select(func.count(DeviceModel.id)).where(DeviceModel.room_id == room.id)) or 0
            if device_count > 0:
                raise AppException(
                    409,
                    "Cannot delete room containing devices under REQ-2-008",
                    "Conflict",
                )

            db.delete(room)
            db.commit()
        except AppException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error("Failed to delete room: %s", exc)
            raise AppException(500, "Failed to delete room", "Internal Server Error")


class DeviceController:
    @staticmethod
    def serialize_device(device: DeviceModel) -> DeviceResponse:
        return DeviceResponse(
            id=device.id,
            hub_id=device.hub_id,
            room_id=device.room_id,
            label=device.label,
            address=device.address,
            type=DeviceTypeEnum(device.type),
            revision=device.revision,
            etag=f'"{device.revision}"',
        )

    @classmethod
    def list_devices(
        cls,
        db: Session,
        current_user: UserModel,
        hub_id: Optional[int],
        room_id: Optional[int],
        type_filter: Optional[DeviceTypeEnum],
        unassigned: Optional[bool],
        limit: int,
        after_id: Optional[int],
    ) -> DeviceListResponse:
        if after_id is not None and after_id <= 0:
            raise AppException(422, "after_id must be a positive integer", "Unprocessable Entity")

        query = select(DeviceModel)

        if hub_id is not None:
            if hub_id <= 0:
                raise AppException(404, "hub_id must be a positive integer", "Not Found")
            hub = db.scalar(select(HubModel).where(HubModel.id == hub_id))
            if not hub:
                raise AppException(404, f"Hub {hub_id} not found", "Not Found")
            if current_user.role != UserRole.ADMIN.value and hub.owner_id != current_user.id:
                raise AppException(403, "Access denied to devices in hub owned by another user", "Forbidden")
            query = query.where(DeviceModel.hub_id == hub_id)
        else:
            if current_user.role == UserRole.USER.value:
                query = query.join(DeviceModel.hub).where(HubModel.owner_id == current_user.id)

        if room_id is not None:
            if room_id <= 0:
                raise AppException(404, "room_id must be a positive integer", "Not Found")
            room = db.scalar(select(RoomModel).where(RoomModel.id == room_id))
            if not room:
                raise AppException(404, f"Room {room_id} not found", "Not Found")
            if current_user.role != UserRole.ADMIN.value and room.hub.owner_id != current_user.id:
                raise AppException(403, "Access denied to devices in room owned by another user", "Forbidden")
            if hub_id is not None and room.hub_id != hub_id:
                raise AppException(404, "Room does not belong to specified hub", "Not Found")
            query = query.where(DeviceModel.room_id == room_id)

        if type_filter is not None:
            query = query.where(DeviceModel.type == type_filter.value)

        if unassigned is True:
            query = query.where(DeviceModel.room_id.is_(None))
        elif unassigned is False:
            query = query.where(DeviceModel.room_id.is_not(None))

        if after_id is not None:
            query = query.where(DeviceModel.id > after_id)

        query = query.order_by(DeviceModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return DeviceListResponse(
            items=[cls.serialize_device(d) for d in items],
            next_after_id=next_id,
        )

    @classmethod
    def get_device_by_id(cls, db: Session, current_user: UserModel, device_id: int) -> DeviceModel:
        if device_id <= 0:
            raise AppException(404, "Device id must be a positive integer", "Not Found")
        device = db.scalar(select(DeviceModel).where(DeviceModel.id == device_id))
        if not device:
            raise AppException(404, f"Device {device_id} not found", "Not Found")
        if current_user.role != UserRole.ADMIN.value and device.hub.owner_id != current_user.id:
            raise AppException(403, "Access denied to device in hub owned by another user", "Forbidden")
        return device

    @classmethod
    def create_device(
        cls,
        db: Session,
        current_user: UserModel,
        payload: DeviceCreateRequest,
    ) -> DeviceModel:
        if payload.hub_id <= 0:
            raise AppException(404, "hub_id must be a positive integer", "Not Found")
        hub = db.scalar(select(HubModel).where(HubModel.id == payload.hub_id))
        if not hub:
            raise AppException(404, f"Hub {payload.hub_id} not found", "Not Found")
        if current_user.role != UserRole.ADMIN.value and hub.owner_id != current_user.id:
            raise AppException(403, "Access denied: cannot add device to hub owned by another user", "Forbidden")

        if payload.room_id is not None:
            if payload.room_id <= 0:
                raise AppException(404, "room_id must be a positive integer", "Not Found")
            room = db.scalar(select(RoomModel).where(RoomModel.id == payload.room_id))
            if not room:
                raise AppException(404, f"Room {payload.room_id} not found", "Not Found")
            if room.hub_id != hub.id:
                raise AppException(409, "Specified room does not belong to the device's hub", "Conflict")
            room_count = db.scalar(select(func.count(DeviceModel.id)).where(DeviceModel.room_id == room.id)) or 0
            if room_count >= 32:
                raise AppException(409, "Room capacity limit reached (maximum 32 devices per room)", "Conflict")

        hub_count = db.scalar(select(func.count(DeviceModel.id)).where(DeviceModel.hub_id == hub.id)) or 0
        if hub_count >= 64:
            raise AppException(409, "Hub capacity limit reached (maximum 64 devices per hub)", "Conflict")

        try:
            existing_label = db.scalar(
                select(DeviceModel).where(
                    DeviceModel.hub_id == hub.id,
                    DeviceModel.label == payload.label,
                )
            )
            if existing_label:
                raise AppException(409, f"Device with label '{payload.label}' already exists in this hub", "Conflict")

            existing_addr = db.scalar(
                select(DeviceModel).where(
                    DeviceModel.hub_id == hub.id,
                    DeviceModel.address == payload.address,
                )
            )
            if existing_addr:
                raise AppException(409, f"Device with address '{payload.address}' already exists in this hub", "Conflict")

            device = DeviceModel(
                hub_id=hub.id,
                room_id=payload.room_id,
                label=payload.label,
                address=payload.address,
                type=payload.type.value,
                revision=1,
            )
            db.add(device)
            db.commit()
            db.refresh(device)
            return device
        except AppException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error("Failed to create device: %s", exc)
            raise AppException(500, "Failed to create device", "Internal Server Error")

    @classmethod
    def patch_device(
        cls,
        db: Session,
        current_user: UserModel,
        device_id: int,
        payload: DevicePatchRequest,
        if_match: Optional[str],
    ) -> DeviceModel:
        expected_revision = parse_and_validate_if_match(if_match)
        device = cls.get_device_by_id(db, current_user, device_id)

        try:
            if expected_revision != device.revision:
                raise AppException(
                    409,
                    f"Stale ETag: current revision is {device.revision}",
                    "Conflict",
                    headers={"ETag": f'"{device.revision}"'},
                )

            data = payload.model_dump(exclude_unset=True)

            if "label" in data:
                new_label = data["label"]
                if new_label != device.label:
                    existing = db.scalar(
                        select(DeviceModel).where(
                            DeviceModel.hub_id == device.hub_id,
                            DeviceModel.label == new_label,
                            DeviceModel.id != device.id,
                        )
                    )
                    if existing:
                        raise AppException(
                            409,
                            f"Device with label '{new_label}' already exists in this hub",
                            "Conflict",
                        )
                device.label = new_label

            if "room_id" in data:
                new_room_id = data["room_id"]
                if new_room_id is None:
                    device.room_id = None
                else:
                    if new_room_id <= 0:
                        raise AppException(404, "room_id must be a positive integer", "Not Found")
                    room = db.scalar(select(RoomModel).where(RoomModel.id == new_room_id))
                    if not room:
                        raise AppException(404, f"Room {new_room_id} not found", "Not Found")
                    if room.hub_id != device.hub_id:
                        raise AppException(409, "Target room does not belong to the device's hub", "Conflict")
                    if new_room_id != device.room_id:
                        target_room_count = (
                            db.scalar(select(func.count(DeviceModel.id)).where(DeviceModel.room_id == new_room_id)) or 0
                        )
                        if target_room_count >= 32:
                            raise AppException(
                                409,
                                "Target room capacity limit reached (maximum 32 devices per room)",
                                "Conflict",
                            )
                    device.room_id = new_room_id

            if "type" in data:
                device.type = (
                    data["type"].value
                    if hasattr(data["type"], "value")
                    else str(data["type"])
                )

            device.revision += 1
            db.commit()
            db.refresh(device)
            return device
        except AppException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error("Failed to patch device: %s", exc)
            raise AppException(500, "Failed to patch device", "Internal Server Error")

    @classmethod
    def delete_device(cls, db: Session, current_user: UserModel, device_id: int) -> None:
        device = cls.get_device_by_id(db, current_user, device_id)
        try:
            db.delete(device)
            db.commit()
        except AppException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error("Failed to delete device: %s", exc)
            raise AppException(500, "Failed to delete device", "Internal Server Error")


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

    admin_token = os.getenv("API_ADMIN_TOKEN", "admin-token-secret-12345")
    user1_token = os.getenv("API_USER_TOKEN", "user1-token-secret-12345")
    user2_token = os.getenv("API_SECOND_USER_TOKEN", "user2-token-secret-12345")

    # Deterministic local users exist with ids 1 and 2 in role user and id 3 in role admin
    u1 = UserModel(
        id=1,
        username="user1",
        role=UserRole.USER.value,
        token_hash=hash_token(user1_token),
    )
    u2 = UserModel(
        id=2,
        username="user2",
        role=UserRole.USER.value,
        token_hash=hash_token(user2_token),
    )
    u3 = UserModel(
        id=3,
        username="admin",
        role=UserRole.ADMIN.value,
        token_hash=hash_token(admin_token),
    )
    db.add_all([u1, u2, u3])
    db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    with SessionLocal() as db:
        seed_database(db)
    yield


# Immediate initialization for test harnesses and startup
with SessionLocal() as _startup_db:
    seed_database(_startup_db)


# -----------------------------------------------------------------------------
# FastAPI Application & Routes (MVC: View/Routing)
# -----------------------------------------------------------------------------
app = FastAPI(
    title="Smart Home - Hubs, Rooms, Devices & Concurrent PATCH API",
    version="1.0.0",
    description="Deterministic RESTful API for smart home topology, room assignments, and optimistic concurrency via strong ETags.",
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
    logger.error("Unhandled internal exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "status_code": 500, "error_type": "Internal Server Error"},
    )


# -----------------------------------------------------------------------------
# Endpoints (17 Specified Endpoints)
# -----------------------------------------------------------------------------

# REQ-2-011: GET /health
@app.get(
    "/health",
    tags=["Health"],
    summary="Health check probe",
    response_model=HealthResponse,
    responses={
        200: {"description": "Service operational and database connected", "model": HealthResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def get_health(db: Session = Depends(get_db)):
    """The system shall expose GET /health as a public endpoint returning deterministic persistence health."""
    try:
        result = db.scalar(select(1))
        if result != 1:
            raise AppException(503, "Database connection probe failed", "Service Unavailable")
    except Exception:
        raise AppException(503, "Database connection probe failed", "Service Unavailable")
    return HealthResponse(status="ok", database="connected")


# REQ-2-012: GET /hubs
@app.get(
    "/hubs",
    tags=["Hubs"],
    summary="List accessible hubs",
    response_model=HubListResponse,
    responses={
        200: {"description": "Hubs listed successfully", "model": HubListResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def list_hubs(
    owner_id: Optional[int] = Query(None),
    timezone: Optional[TimezoneEnum] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /hubs as an authenticated user-or-admin endpoint with stable pagination."""
    return HubController.list_hubs(db, current_user, owner_id, timezone, limit, after_id)


# REQ-2-013: POST /hubs
@app.post(
    "/hubs",
    status_code=201,
    tags=["Hubs"],
    summary="Create a new hub",
    response_model=HubResponse,
    responses={
        201: {"description": "Hub created successfully", "model": HubResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        409: {"description": "Conflict", "model": ErrorResponse},
        415: {"description": "Unsupported Media Type", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def create_hub(
    payload: HubCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose POST /hubs creating a hub at revision 1 returning 201 with Location and ETag."""
    hub = HubController.create_hub(db, current_user, payload)
    response.headers["Location"] = f"/hubs/{hub.id}"
    response.headers["ETag"] = f'"{hub.revision}"'
    return HubController.serialize_hub(hub)


# REQ-2-014: GET /hubs/{hub_id}
@app.get(
    "/hubs/{hub_id}",
    tags=["Hubs"],
    summary="Get hub details",
    response_model=HubResponse,
    responses={
        200: {"description": "Hub retrieved successfully", "model": HubResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def get_hub(
    hub_id: int,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /hubs/{hub_id} returning the hub with current revision ETag."""
    hub = HubController.get_hub_by_id(db, current_user, hub_id)
    response.headers["ETag"] = f'"{hub.revision}"'
    return HubController.serialize_hub(hub)


# REQ-2-015: PATCH /hubs/{hub_id}
@app.patch(
    "/hubs/{hub_id}",
    tags=["Hubs"],
    summary="Update hub details with optimistic concurrency",
    response_model=HubResponse,
    responses={
        200: {"description": "Hub updated successfully", "model": HubResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        409: {"description": "Conflict", "model": ErrorResponse},
        415: {"description": "Unsupported Media Type", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def patch_hub(
    hub_id: int,
    payload: HubPatchRequest,
    response: Response,
    if_match: Optional[str] = Header(None, alias="If-Match"),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose PATCH /hubs/{hub_id} updating supplied fields and incrementing revision."""
    hub = HubController.patch_hub(db, current_user, hub_id, payload, if_match)
    response.headers["ETag"] = f'"{hub.revision}"'
    return HubController.serialize_hub(hub)


# REQ-2-016: DELETE /hubs/{hub_id}
@app.delete(
    "/hubs/{hub_id}",
    status_code=204,
    tags=["Hubs"],
    summary="Delete an empty hub",
    responses={
        204: {"description": "Hub deleted successfully"},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        409: {"description": "Conflict", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def delete_hub(
    hub_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose DELETE /hubs/{hub_id} deleting only a hub with no rooms and no devices."""
    HubController.delete_hub(db, current_user, hub_id)
    return Response(status_code=204)


# REQ-2-017: GET /rooms
@app.get(
    "/rooms",
    tags=["Rooms"],
    summary="List rooms",
    response_model=RoomListResponse,
    responses={
        200: {"description": "Rooms listed successfully", "model": RoomListResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def list_rooms(
    hub_id: Optional[int] = Query(None),
    floor: Optional[int] = Query(None, ge=-5, le=200),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /rooms with optional parent hub and floor filtering."""
    return RoomController.list_rooms(db, current_user, hub_id, floor, limit, after_id)


# REQ-2-018: POST /rooms
@app.post(
    "/rooms",
    status_code=201,
    tags=["Rooms"],
    summary="Create a new room",
    response_model=RoomResponse,
    responses={
        201: {"description": "Room created successfully", "model": RoomResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        409: {"description": "Conflict", "model": ErrorResponse},
        415: {"description": "Unsupported Media Type", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def create_room(
    payload: RoomCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose POST /rooms creating a room in a hub returning 201 with Location and ETag."""
    room = RoomController.create_room(db, current_user, payload)
    response.headers["Location"] = f"/rooms/{room.id}"
    response.headers["ETag"] = f'"{room.revision}"'
    return RoomController.serialize_room(room)


# REQ-2-019: GET /rooms/{room_id}
@app.get(
    "/rooms/{room_id}",
    tags=["Rooms"],
    summary="Get room details",
    response_model=RoomResponse,
    responses={
        200: {"description": "Room retrieved successfully", "model": RoomResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def get_room(
    room_id: int,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /rooms/{room_id} returning the room with current revision ETag."""
    room = RoomController.get_room_by_id(db, current_user, room_id)
    response.headers["ETag"] = f'"{room.revision}"'
    return RoomController.serialize_room(room)


# REQ-2-020: PATCH /rooms/{room_id}
@app.patch(
    "/rooms/{room_id}",
    tags=["Rooms"],
    summary="Update room details with optimistic concurrency",
    response_model=RoomResponse,
    responses={
        200: {"description": "Room updated successfully", "model": RoomResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        409: {"description": "Conflict", "model": ErrorResponse},
        415: {"description": "Unsupported Media Type", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def patch_room(
    room_id: int,
    payload: RoomPatchRequest,
    response: Response,
    if_match: Optional[str] = Header(None, alias="If-Match"),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose PATCH /rooms/{room_id} updating supplied fields and incrementing revision."""
    room = RoomController.patch_room(db, current_user, room_id, payload, if_match)
    response.headers["ETag"] = f'"{room.revision}"'
    return RoomController.serialize_room(room)


# REQ-2-021: DELETE /rooms/{room_id}
@app.delete(
    "/rooms/{room_id}",
    status_code=204,
    tags=["Rooms"],
    summary="Delete an empty room",
    responses={
        204: {"description": "Room deleted successfully"},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        409: {"description": "Conflict", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def delete_room(
    room_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose DELETE /rooms/{room_id} deleting only an empty room with no devices."""
    RoomController.delete_room(db, current_user, room_id)
    return Response(status_code=204)


# REQ-2-022: GET /devices
@app.get(
    "/devices",
    tags=["Devices"],
    summary="List devices",
    response_model=DeviceListResponse,
    responses={
        200: {"description": "Devices listed successfully", "model": DeviceListResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def list_devices(
    hub_id: Optional[int] = Query(None),
    room_id: Optional[int] = Query(None),
    type: Optional[DeviceTypeEnum] = Query(None),
    unassigned: Optional[bool] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /devices accepting optional filters and stable pagination."""
    return DeviceController.list_devices(db, current_user, hub_id, room_id, type, unassigned, limit, after_id)


# REQ-2-023: POST /devices
@app.post(
    "/devices",
    status_code=201,
    tags=["Devices"],
    summary="Create a new device",
    response_model=DeviceResponse,
    responses={
        201: {"description": "Device created successfully", "model": DeviceResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        409: {"description": "Conflict", "model": ErrorResponse},
        415: {"description": "Unsupported Media Type", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def create_device(
    payload: DeviceCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose POST /devices validating capacity and uniqueness, returning 201 with Location and ETag."""
    device = DeviceController.create_device(db, current_user, payload)
    response.headers["Location"] = f"/devices/{device.id}"
    response.headers["ETag"] = f'"{device.revision}"'
    return DeviceController.serialize_device(device)


# REQ-2-024: GET /devices/{device_id}
@app.get(
    "/devices/{device_id}",
    tags=["Devices"],
    summary="Get device details",
    response_model=DeviceResponse,
    responses={
        200: {"description": "Device retrieved successfully", "model": DeviceResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def get_device(
    device_id: int,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /devices/{device_id} returning device details with current revision ETag."""
    device = DeviceController.get_device_by_id(db, current_user, device_id)
    response.headers["ETag"] = f'"{device.revision}"'
    return DeviceController.serialize_device(device)


# REQ-2-025: PATCH /devices/{device_id}
@app.patch(
    "/devices/{device_id}",
    tags=["Devices"],
    summary="Update device details with optimistic concurrency",
    response_model=DeviceResponse,
    responses={
        200: {"description": "Device updated successfully", "model": DeviceResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        409: {"description": "Conflict", "model": ErrorResponse},
        415: {"description": "Unsupported Media Type", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def patch_device(
    device_id: int,
    payload: DevicePatchRequest,
    response: Response,
    if_match: Optional[str] = Header(None, alias="If-Match"),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose PATCH /devices/{device_id} updating supplied fields and incrementing revision."""
    device = DeviceController.patch_device(db, current_user, device_id, payload, if_match)
    response.headers["ETag"] = f'"{device.revision}"'
    return DeviceController.serialize_device(device)


# REQ-2-026: DELETE /devices/{device_id}
@app.delete(
    "/devices/{device_id}",
    status_code=204,
    tags=["Devices"],
    summary="Delete a device",
    responses={
        204: {"description": "Device deleted successfully"},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def delete_device(
    device_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose DELETE /devices/{device_id} removing only that device without cascade."""
    DeviceController.delete_device(db, current_user, device_id)
    return Response(status_code=204)


# REQ-2-027: GET /hubs/{hub_id}/topology
@app.get(
    "/hubs/{hub_id}/topology",
    tags=["Hubs"],
    summary="Get full hub topology",
    response_model=HubTopologyResponse,
    responses={
        200: {"description": "Hub topology retrieved successfully", "model": HubTopologyResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def get_hub_topology(
    hub_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /hubs/{hub_id}/topology returning the hub, rooms, and devices in acyclic order."""
    hub = HubController.get_hub_by_id(db, current_user, hub_id)
    rooms = list(db.scalars(select(RoomModel).where(RoomModel.hub_id == hub.id).order_by(RoomModel.id.asc())).all())
    devices = list(db.scalars(select(DeviceModel).where(DeviceModel.hub_id == hub.id).order_by(DeviceModel.id.asc())).all())
    return HubTopologyResponse(
        hub=HubController.serialize_hub(hub),
        rooms=[RoomController.serialize_room(r) for r in rooms],
        devices=[DeviceController.serialize_device(d) for d in devices],
    )
