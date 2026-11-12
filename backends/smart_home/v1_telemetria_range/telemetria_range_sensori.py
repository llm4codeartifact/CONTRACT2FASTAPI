"""
Telemetria e Range Sensori - Smart Home API
Case ID: 2
Variant: Telemetry and sensor ranges

Technical specifications:
- Python 3.10+, FastAPI, Pydantic v2, SQLAlchemy ORM, SQLite in-memory with StaticPool.
- Pure ASGI middleware for 10 MiB limit (413), JSON media type (415), invalid JSON (400), preserving 405 Method Not Allowed.
- MVC architecture: SQLAlchemy models, Pydantic schemas, static controllers, thin FastAPI routes.
- Seed data: user1 (id=1, user), user2 (id=2, user), admin (id=3, admin).
- Standardized error format: {"detail": str, "status_code": int, "error_type": str}.
- GET /health reporting {"status": "ok", "database": "connected"}.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Tuple

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    select,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Session,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool
from starlette.routing import Match
from starlette.types import ASGIApp, Receive, Scope, Send

# -----------------------------------------------------------------------------
# Logging Configuration
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("telemetria_range_sensori")

# -----------------------------------------------------------------------------
# Database Setup (SQLite in-memory with StaticPool)
# -----------------------------------------------------------------------------
DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


# -----------------------------------------------------------------------------
# Enums
# -----------------------------------------------------------------------------
class SensorTypeEnum(str, Enum):
    TEMPERATURE = "TEMPERATURE"
    HUMIDITY = "HUMIDITY"
    CO2 = "CO2"
    ILLUMINANCE = "ILLUMINANCE"


class DirectionEnum(str, Enum):
    BELOW = "BELOW"
    ABOVE = "ABOVE"


class RoleEnum(str, Enum):
    USER = "user"
    ADMIN = "admin"


PHYSICAL_RANGES = {
    SensorTypeEnum.TEMPERATURE: (-50.0, 150.0),
    SensorTypeEnum.HUMIDITY: (0.0, 100.0),
    SensorTypeEnum.CO2: (0.0, 5000.0),
    SensorTypeEnum.ILLUMINANCE: (0.0, 200000.0),
}


def validate_physical_bound(sensor_type: SensorTypeEnum, val: float, field_name: str = "value"):
    min_val, max_val = PHYSICAL_RANGES[sensor_type]
    if val < min_val or val > max_val:
        raise AppException(
            422,
            f"{field_name} {val} is outside physical range [{min_val}, {max_val}] for {sensor_type.value}",
            "Unprocessable Entity",
        )


# -----------------------------------------------------------------------------
# SQLAlchemy Models
# -----------------------------------------------------------------------------
class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    role = Column(String(20), nullable=False)
    token = Column(String(100), unique=True, nullable=False)

    sensors = relationship("SensorModel", back_populates="owner")


class SensorModel(Base):
    __tablename__ = "sensors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    serial = Column(String(32), unique=True, nullable=False, index=True)
    name = Column(String(80), nullable=False)
    type = Column(String(20), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    owner = relationship("UserModel", back_populates="sensors")
    threshold_policy = relationship(
        "ThresholdPolicyModel",
        uselist=False,
        back_populates="sensor",
        cascade="all, delete-orphan",
    )
    readings = relationship("ReadingModel", back_populates="sensor")
    alerts = relationship("AlertModel", back_populates="sensor")


class ThresholdPolicyModel(Base):
    __tablename__ = "threshold_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sensor_id = Column(Integer, ForeignKey("sensors.id"), unique=True, nullable=False, index=True)
    lower_bound = Column(Float, nullable=True)
    upper_bound = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    sensor = relationship("SensorModel", back_populates="threshold_policy")


class ReadingModel(Base):
    __tablename__ = "readings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sensor_id = Column(Integer, ForeignKey("sensors.id"), nullable=False, index=True)
    value = Column(Float, nullable=False)
    captured_at = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    sensor = relationship("SensorModel", back_populates="readings")
    alerts = relationship("AlertModel", back_populates="reading")

    __table_args__ = (
        UniqueConstraint("sensor_id", "captured_at", name="uq_reading_sensor_captured_at"),
    )


class AlertModel(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sensor_id = Column(Integer, ForeignKey("sensors.id"), nullable=False, index=True)
    reading_id = Column(Integer, ForeignKey("readings.id"), nullable=False, index=True)
    direction = Column(String(10), nullable=False)
    reading_value = Column(Float, nullable=False)
    threshold_lower = Column(Float, nullable=True)
    threshold_upper = Column(Float, nullable=True)
    resolved = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    sensor = relationship("SensorModel", back_populates="alerts")
    reading = relationship("ReadingModel", back_populates="alerts")


# -----------------------------------------------------------------------------
# Exceptions & Error Schemas
# -----------------------------------------------------------------------------
class AppException(Exception):
    def __init__(self, status_code: int, detail: str, error_type: str):
        self.status_code = status_code
        self.detail = detail
        self.error_type = error_type
        super().__init__(detail)


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: str
    status_code: int
    error_type: str


# -----------------------------------------------------------------------------
# Pydantic Schemas
# -----------------------------------------------------------------------------
class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    database: str


class SensorCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: int = Field(..., gt=0)
    serial: str = Field(..., pattern=r"^[A-Z0-9-]{6,32}$")
    name: str = Field(..., min_length=1, max_length=80)
    type: SensorTypeEnum


class SensorPatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    owner_id: Optional[int] = Field(None, gt=0)

    @model_validator(mode="after")
    def check_at_least_one(self) -> "SensorPatchPayload":
        if self.name is None and self.owner_id is None:
            raise ValueError("At least one of name or owner_id must be provided")
        return self


class ThresholdPolicyCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None

    @model_validator(mode="after")
    def check_bounds(self) -> "ThresholdPolicyCreatePayload":
        if self.lower_bound is None and self.upper_bound is None:
            raise ValueError("At least one of lower_bound or upper_bound must be provided")
        if (
            self.lower_bound is not None
            and self.upper_bound is not None
            and self.lower_bound >= self.upper_bound
        ):
            raise ValueError("lower_bound must be strictly less than upper_bound")
        return self


class ThresholdPolicyPatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None

    @model_validator(mode="after")
    def check_non_empty(self) -> "ThresholdPolicyPatchPayload":
        if self.lower_bound is None and self.upper_bound is None:
            raise ValueError("At least one of lower_bound or upper_bound must be supplied")
        return self


class ReadingCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float
    captured_at: datetime


class ThresholdPolicySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None
    created_at: datetime
    updated_at: datetime


class SensorDetailOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    owner_id: int
    serial: str
    name: str
    type: SensorTypeEnum
    created_at: datetime
    threshold_policy: Optional[ThresholdPolicySummary] = None


class SensorListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[SensorDetailOut]
    next_after_id: Optional[int] = None


class ThresholdPolicyOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    sensor_id: int
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None
    created_at: datetime
    updated_at: datetime


class ReadingOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    sensor_id: int
    value: float
    captured_at: datetime
    created_at: datetime


class ReadingListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[ReadingOut]
    next_after_id: Optional[int] = None


class TelemetryRangeOut(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    sensor_id: int
    from_time: datetime = Field(..., alias="from")
    to_time: datetime = Field(..., alias="to")
    count: int
    min: Optional[float] = None
    max: Optional[float] = None
    average: Optional[float] = None
    readings: List[ReadingOut]


class AlertOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    sensor_id: int
    reading_id: int
    direction: DirectionEnum
    reading_value: float
    threshold_lower: Optional[float] = None
    threshold_upper: Optional[float] = None
    resolved: bool
    created_at: datetime


class AlertListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[AlertOut]
    next_after_id: Optional[int] = None


# -----------------------------------------------------------------------------
# Database Seed & Dependencies
# -----------------------------------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def seed_database(db: Session):
    Base.metadata.create_all(bind=engine)
    existing_users = db.scalar(select(UserModel).limit(1))
    if not existing_users:
        u1 = UserModel(
            id=1, username="user1", role=RoleEnum.USER.value, token="user1-token-secret-12345"
        )
        u2 = UserModel(
            id=2, username="user2", role=RoleEnum.USER.value, token="user2-token-secret-12345"
        )
        admin = UserModel(
            id=3, username="admin", role=RoleEnum.ADMIN.value, token="admin-token-secret-12345"
        )
        db.add_all([u1, u2, admin])
        db.commit()


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def get_current_user(
    authorization: Optional[str] = Header(None), db: Session = Depends(get_db)
) -> UserModel:
    if not authorization or not authorization.startswith("Bearer "):
        raise AppException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Bearer authorization header",
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


def require_admin(current_user: UserModel = Depends(get_current_user)) -> UserModel:
    if current_user.role != RoleEnum.ADMIN.value:
        raise AppException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required for this operation",
            error_type="Forbidden",
        )
    return current_user


# -----------------------------------------------------------------------------
# Pure ASGI Middleware
# -----------------------------------------------------------------------------
MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MiB


class JsonValidationASGIMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"].upper()
        path = scope["path"]

        # Health endpoint bypasses validation
        if path == "/health":
            await self.app(scope, receive, send)
            return

        # Pass OPTIONS immediately
        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        # Check route match in router to allow 405 Method Not Allowed handling
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

        # Read headers
        headers_dict = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope["headers"]}
        content_length_str = headers_dict.get("content-length")
        content_type = headers_dict.get("content-type", "")

        # 413 check via content-length header
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

        # Methods that expect bodies
        if method in ("POST", "PATCH", "PUT"):
            # Check Media-Type: must be application/json
            if not content_type.startswith("application/json"):
                await self._send_json_error(
                    send, 415, "Content-Type must be application/json", "Unsupported Media Type"
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

            # Validate valid JSON syntax
            if body:
                try:
                    json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as err:
                    await self._send_json_error(
                        send, 400, f"Malformed JSON body: {str(err)}", "Bad Request"
                    )
                    return

            # Replay buffered body
            async def custom_receive():
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            await self.app(scope, custom_receive, send)
            return

        # For GET, DELETE, HEAD
        await self.app(scope, receive, send)

    async def _send_json_error(self, send: Send, status_code: int, detail: str, error_type: str):
        payload = json.dumps(
            {"detail": detail, "status_code": status_code, "error_type": error_type}
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("latin1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload, "more_body": False})


# -----------------------------------------------------------------------------
# Static Controllers
# -----------------------------------------------------------------------------
class HealthController:
    @staticmethod
    def get_health(db: Session) -> HealthResponse:
        try:
            db.execute(text("SELECT 1")).scalar()
            return HealthResponse(status="ok", database="connected")
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            raise AppException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Local persistence probe failed",
                "Service Unavailable",
            )


class SensorController:
    @staticmethod
    def build_sensor_detail(sensor: SensorModel) -> SensorDetailOut:
        policy_summary = None
        if sensor.threshold_policy:
            p = sensor.threshold_policy
            created_at = p.created_at.replace(tzinfo=timezone.utc) if p.created_at.tzinfo is None else p.created_at
            updated_at = p.updated_at.replace(tzinfo=timezone.utc) if p.updated_at.tzinfo is None else p.updated_at
            policy_summary = ThresholdPolicySummary(
                id=p.id,
                lower_bound=p.lower_bound,
                upper_bound=p.upper_bound,
                created_at=created_at,
                updated_at=updated_at,
            )
        s_created_at = sensor.created_at.replace(tzinfo=timezone.utc) if sensor.created_at.tzinfo is None else sensor.created_at
        return SensorDetailOut(
            id=sensor.id,
            owner_id=sensor.owner_id,
            serial=sensor.serial,
            name=sensor.name,
            type=SensorTypeEnum(sensor.type),
            created_at=s_created_at,
            threshold_policy=policy_summary,
        )

    @staticmethod
    def list_sensors(
        db: Session,
        current_user: UserModel,
        sensor_type: Optional[SensorTypeEnum],
        serial_prefix: Optional[str],
        owner_id: Optional[int],
        limit: int,
        after_id: Optional[int],
    ) -> SensorListResponse:
        query = select(SensorModel)

        # Authorization and owner filtering
        if current_user.role != RoleEnum.ADMIN.value:
            if owner_id is not None and owner_id != current_user.id:
                raise AppException(403, "Users may only access their own sensors", "Forbidden")
            query = query.where(SensorModel.owner_id == current_user.id)
        else:
            if owner_id is not None:
                if owner_id <= 0:
                    raise AppException(404, "owner_id must be a positive integer", "Not Found")
                owner_exists = db.scalar(select(UserModel).where(UserModel.id == owner_id))
                if not owner_exists:
                    raise AppException(404, f"Owner user {owner_id} not found", "Not Found")
                query = query.where(SensorModel.owner_id == owner_id)

        if sensor_type:
            query = query.where(SensorModel.type == sensor_type.value)

        if serial_prefix:
            query = query.where(SensorModel.serial.startswith(serial_prefix))

        if after_id is not None and after_id > 0:
            query = query.where(SensorModel.id > after_id)

        query = query.order_by(SensorModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())

        next_after_id = None
        if len(records) > limit:
            next_after_id = records[limit - 1].id
            records = records[:limit]

        items = [SensorController.build_sensor_detail(s) for s in records]
        return SensorListResponse(items=items, next_after_id=next_after_id)

    @staticmethod
    def create_sensor(db: Session, current_user: UserModel, payload: SensorCreatePayload) -> SensorModel:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Admin privileges required to create sensors", "Forbidden")

        if payload.owner_id <= 0:
            raise AppException(404, "owner_id must be a positive integer", "Not Found")

        owner = db.scalar(select(UserModel).where(UserModel.id == payload.owner_id))
        if not owner:
            raise AppException(404, f"Owner user {payload.owner_id} not found", "Not Found")

        existing_serial = db.scalar(select(SensorModel).where(SensorModel.serial == payload.serial))
        if existing_serial:
            raise AppException(409, f"Serial '{payload.serial}' already exists", "Conflict")

        now = utc_now()
        sensor = SensorModel(
            owner_id=payload.owner_id,
            serial=payload.serial,
            name=payload.name,
            type=payload.type.value,
            created_at=now,
        )
        db.add(sensor)
        db.commit()
        db.refresh(sensor)
        return sensor

    @staticmethod
    def get_sensor(db: Session, current_user: UserModel, sensor_id: int) -> SensorModel:
        if sensor_id <= 0:
            raise AppException(404, "sensor_id must be a positive integer", "Not Found")

        sensor = db.scalar(select(SensorModel).where(SensorModel.id == sensor_id))
        if not sensor:
            raise AppException(404, f"Sensor {sensor_id} not found", "Not Found")

        if current_user.role != RoleEnum.ADMIN.value and sensor.owner_id != current_user.id:
            raise AppException(403, "Access denied to sensor owned by another user", "Forbidden")

        return sensor

    @staticmethod
    def patch_sensor(
        db: Session, current_user: UserModel, sensor_id: int, payload: SensorPatchPayload
    ) -> SensorModel:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Admin privileges required to patch sensors", "Forbidden")

        if sensor_id <= 0:
            raise AppException(404, "sensor_id must be a positive integer", "Not Found")

        sensor = db.scalar(select(SensorModel).where(SensorModel.id == sensor_id))
        if not sensor:
            raise AppException(404, f"Sensor {sensor_id} not found", "Not Found")

        if payload.owner_id is not None:
            if payload.owner_id <= 0:
                raise AppException(404, "owner_id must be a positive integer", "Not Found")
            new_owner = db.scalar(select(UserModel).where(UserModel.id == payload.owner_id))
            if not new_owner:
                raise AppException(404, f"Owner user {payload.owner_id} not found", "Not Found")
            sensor.owner_id = payload.owner_id

        if payload.name is not None:
            sensor.name = payload.name

        db.commit()
        db.refresh(sensor)
        return sensor

    @staticmethod
    def delete_sensor(db: Session, current_user: UserModel, sensor_id: int):
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Admin privileges required to delete sensors", "Forbidden")

        if sensor_id <= 0:
            raise AppException(404, "sensor_id must be a positive integer", "Not Found")

        sensor = db.scalar(select(SensorModel).where(SensorModel.id == sensor_id))
        if not sensor:
            raise AppException(404, f"Sensor {sensor_id} not found", "Not Found")

        # REQ-2-008: Sensor deletion is forbidden while any reading, threshold policy, or alert refers to it
        if sensor.threshold_policy is not None or len(sensor.readings) > 0 or len(sensor.alerts) > 0:
            raise AppException(
                409,
                "Cannot delete sensor with existing readings, threshold policy, or alerts",
                "Conflict",
            )

        db.delete(sensor)
        db.commit()


class ThresholdPolicyController:
    @staticmethod
    def get_threshold(
        db: Session, current_user: UserModel, sensor_id: int
    ) -> Optional[ThresholdPolicyModel]:
        sensor = SensorController.get_sensor(db, current_user, sensor_id)
        return sensor.threshold_policy

    @staticmethod
    def create_threshold(
        db: Session, current_user: UserModel, sensor_id: int, payload: ThresholdPolicyCreatePayload
    ) -> ThresholdPolicyModel:
        sensor = SensorController.get_sensor(db, current_user, sensor_id)

        if sensor.threshold_policy is not None:
            raise AppException(
                409, f"Active threshold policy already exists for sensor {sensor_id}", "Conflict"
            )

        sensor_type = SensorTypeEnum(sensor.type)
        if payload.lower_bound is not None:
            validate_physical_bound(sensor_type, payload.lower_bound, "lower_bound")
        if payload.upper_bound is not None:
            validate_physical_bound(sensor_type, payload.upper_bound, "upper_bound")

        now = utc_now()
        policy = ThresholdPolicyModel(
            sensor_id=sensor_id,
            lower_bound=payload.lower_bound,
            upper_bound=payload.upper_bound,
            created_at=now,
            updated_at=now,
        )
        db.add(policy)
        db.commit()
        db.refresh(policy)
        return policy

    @staticmethod
    def patch_threshold(
        db: Session, current_user: UserModel, sensor_id: int, payload: ThresholdPolicyPatchPayload
    ) -> ThresholdPolicyModel:
        sensor = SensorController.get_sensor(db, current_user, sensor_id)
        policy = sensor.threshold_policy
        if not policy:
            raise AppException(404, f"No active threshold policy found for sensor {sensor_id}", "Not Found")

        sensor_type = SensorTypeEnum(sensor.type)

        new_lower = payload.lower_bound if payload.lower_bound is not None else policy.lower_bound
        new_upper = payload.upper_bound if payload.upper_bound is not None else policy.upper_bound

        if new_lower is None and new_upper is None:
            raise AppException(422, "At least one bound must remain present in policy", "Unprocessable Entity")

        if new_lower is not None:
            validate_physical_bound(sensor_type, new_lower, "lower_bound")
        if new_upper is not None:
            validate_physical_bound(sensor_type, new_upper, "upper_bound")

        if new_lower is not None and new_upper is not None and new_lower >= new_upper:
            raise AppException(
                422, "lower_bound must be strictly less than upper_bound", "Unprocessable Entity"
            )

        policy.lower_bound = new_lower
        policy.upper_bound = new_upper
        policy.updated_at = utc_now()
        db.commit()
        db.refresh(policy)
        return policy

    @staticmethod
    def delete_threshold(db: Session, current_user: UserModel, sensor_id: int):
        sensor = SensorController.get_sensor(db, current_user, sensor_id)
        policy = sensor.threshold_policy
        if not policy:
            raise AppException(404, f"No active threshold policy to delete for sensor {sensor_id}", "Not Found")

        db.delete(policy)
        db.commit()


class ReadingController:
    @staticmethod
    def create_reading(
        db: Session, current_user: UserModel, sensor_id: int, payload: ReadingCreatePayload
    ) -> Tuple[ReadingModel, Optional[AlertModel]]:
        sensor = SensorController.get_sensor(db, current_user, sensor_id)
        sensor_type = SensorTypeEnum(sensor.type)

        # REQ-2-002: Validate reading against physical range
        validate_physical_bound(sensor_type, payload.value, "value")

        # REQ-2-003 & REQ-2-004: captured_at is UTC with second precision and unique per sensor
        captured_dt = (
            payload.captured_at.astimezone(timezone.utc).replace(microsecond=0)
            if payload.captured_at.tzinfo
            else payload.captured_at.replace(tzinfo=timezone.utc, microsecond=0)
        )

        existing_reading = db.scalar(
            select(ReadingModel).where(
                ReadingModel.sensor_id == sensor_id,
                ReadingModel.captured_at == captured_dt,
            )
        )
        if existing_reading:
            raise AppException(
                409,
                f"Reading at captured_at timestamp '{captured_dt.isoformat()}' already exists for sensor {sensor_id}",
                "Conflict",
            )

        now = utc_now()
        reading = ReadingModel(
            sensor_id=sensor_id,
            value=payload.value,
            captured_at=captured_dt,
            created_at=now,
        )
        db.add(reading)
        db.flush()

        # REQ-2-006: Evaluate threshold policy atomically and create alert if breached
        alert = None
        policy = sensor.threshold_policy
        if policy:
            alert_dir = None
            if policy.lower_bound is not None and payload.value < policy.lower_bound:
                alert_dir = DirectionEnum.BELOW.value
            elif policy.upper_bound is not None and payload.value > policy.upper_bound:
                alert_dir = DirectionEnum.ABOVE.value

            if alert_dir:
                alert = AlertModel(
                    sensor_id=sensor_id,
                    reading_id=reading.id,
                    direction=alert_dir,
                    reading_value=payload.value,
                    threshold_lower=policy.lower_bound,
                    threshold_upper=policy.upper_bound,
                    resolved=False,
                    created_at=now,
                )
                db.add(alert)
                db.flush()

        db.commit()
        db.refresh(reading)
        if alert:
            db.refresh(alert)
        return reading, alert

    @staticmethod
    def list_readings(
        db: Session,
        current_user: UserModel,
        sensor_id: int,
        from_time: Optional[datetime],
        to_time: Optional[datetime],
        limit: int,
        after_id: Optional[int],
    ) -> ReadingListResponse:
        SensorController.get_sensor(db, current_user, sensor_id)

        query = select(ReadingModel).where(ReadingModel.sensor_id == sensor_id)

        if from_time is not None and to_time is not None:
            f_utc = from_time.astimezone(timezone.utc) if from_time.tzinfo else from_time.replace(tzinfo=timezone.utc)
            t_utc = to_time.astimezone(timezone.utc) if to_time.tzinfo else to_time.replace(tzinfo=timezone.utc)
            if f_utc >= t_utc:
                raise AppException(422, "'from' must be strictly earlier than 'to'", "Unprocessable Entity")
            if (t_utc - f_utc).total_seconds() > 31 * 86400:
                raise AppException(422, "Interval between from and to cannot exceed 31 days", "Unprocessable Entity")
            query = query.where(ReadingModel.captured_at >= f_utc, ReadingModel.captured_at < t_utc)
        elif from_time is not None:
            f_utc = from_time.astimezone(timezone.utc) if from_time.tzinfo else from_time.replace(tzinfo=timezone.utc)
            query = query.where(ReadingModel.captured_at >= f_utc)
        elif to_time is not None:
            t_utc = to_time.astimezone(timezone.utc) if to_time.tzinfo else to_time.replace(tzinfo=timezone.utc)
            query = query.where(ReadingModel.captured_at < t_utc)

        if after_id is not None and after_id > 0:
            query = query.where(ReadingModel.id > after_id)

        query = query.order_by(ReadingModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())

        next_after_id = None
        if len(records) > limit:
            next_after_id = records[limit - 1].id
            records = records[:limit]

        items = [
            ReadingOut(
                id=r.id,
                sensor_id=r.sensor_id,
                value=r.value,
                captured_at=r.captured_at.replace(tzinfo=timezone.utc) if r.captured_at.tzinfo is None else r.captured_at,
                created_at=r.created_at.replace(tzinfo=timezone.utc) if r.created_at.tzinfo is None else r.created_at,
            )
            for r in records
        ]
        return ReadingListResponse(items=items, next_after_id=next_after_id)

    @staticmethod
    def get_reading(db: Session, current_user: UserModel, reading_id: int) -> ReadingModel:
        if reading_id <= 0:
            raise AppException(404, "reading_id must be a positive integer", "Not Found")

        reading = db.scalar(select(ReadingModel).where(ReadingModel.id == reading_id))
        if not reading:
            raise AppException(404, f"Reading {reading_id} not found", "Not Found")

        if current_user.role != RoleEnum.ADMIN.value and reading.sensor.owner_id != current_user.id:
            raise AppException(403, "Access denied to reading for another user's sensor", "Forbidden")

        return reading

    @staticmethod
    def get_latest_reading(
        db: Session, current_user: UserModel, sensor_id: int
    ) -> Optional[ReadingOut]:
        SensorController.get_sensor(db, current_user, sensor_id)

        latest = db.scalar(
            select(ReadingModel)
            .where(ReadingModel.sensor_id == sensor_id)
            .order_by(ReadingModel.captured_at.desc(), ReadingModel.id.desc())
            .limit(1)
        )
        if not latest:
            return None

        c_at = latest.captured_at.replace(tzinfo=timezone.utc) if latest.captured_at.tzinfo is None else latest.captured_at
        cr_at = latest.created_at.replace(tzinfo=timezone.utc) if latest.created_at.tzinfo is None else latest.created_at
        return ReadingOut(
            id=latest.id,
            sensor_id=latest.sensor_id,
            value=latest.value,
            captured_at=c_at,
            created_at=cr_at,
        )

    @staticmethod
    def get_range(
        db: Session,
        current_user: UserModel,
        sensor_id: int,
        from_time: datetime,
        to_time: datetime,
    ) -> TelemetryRangeOut:
        SensorController.get_sensor(db, current_user, sensor_id)

        f_utc = from_time.astimezone(timezone.utc) if from_time.tzinfo else from_time.replace(tzinfo=timezone.utc)
        t_utc = to_time.astimezone(timezone.utc) if to_time.tzinfo else to_time.replace(tzinfo=timezone.utc)

        if f_utc >= t_utc:
            raise AppException(422, "'from' must be strictly earlier than 'to'", "Unprocessable Entity")

        if (t_utc - f_utc).total_seconds() > 31 * 86400:
            raise AppException(422, "Telemetry range interval cannot exceed 31 days", "Unprocessable Entity")

        # Query readings in [from, to) ordered by captured_at ascending, then id ascending
        records = list(
            db.scalars(
                select(ReadingModel)
                .where(
                    ReadingModel.sensor_id == sensor_id,
                    ReadingModel.captured_at >= f_utc,
                    ReadingModel.captured_at < t_utc,
                )
                .order_by(ReadingModel.captured_at.asc(), ReadingModel.id.asc())
            ).all()
        )

        count = len(records)
        min_val = None
        max_val = None
        avg_val = None

        if count > 0:
            vals = [r.value for r in records]
            min_val = min(vals)
            max_val = max(vals)
            avg_val = round(sum(vals) / count, 4)

        readings_out = [
            ReadingOut(
                id=r.id,
                sensor_id=r.sensor_id,
                value=r.value,
                captured_at=r.captured_at.replace(tzinfo=timezone.utc) if r.captured_at.tzinfo is None else r.captured_at,
                created_at=r.created_at.replace(tzinfo=timezone.utc) if r.created_at.tzinfo is None else r.created_at,
            )
            for r in records
        ]

        return TelemetryRangeOut(
            sensor_id=sensor_id,
            from_time=f_utc,
            to_time=t_utc,
            count=count,
            min=min_val,
            max=max_val,
            average=avg_val,
            readings=readings_out,
        )


class AlertController:
    @staticmethod
    def list_alerts(
        db: Session,
        current_user: UserModel,
        sensor_id: Optional[int],
        direction: Optional[DirectionEnum],
        resolved: Optional[bool],
        limit: int,
        after_id: Optional[int],
    ) -> AlertListResponse:
        query = select(AlertModel)

        if sensor_id is not None:
            if sensor_id <= 0:
                raise AppException(404, "sensor_id must be a positive integer", "Not Found")
            sensor = db.scalar(select(SensorModel).where(SensorModel.id == sensor_id))
            if not sensor:
                raise AppException(404, f"Sensor {sensor_id} not found", "Not Found")
            if current_user.role != RoleEnum.ADMIN.value and sensor.owner_id != current_user.id:
                raise AppException(403, "Access denied to alerts for another user's sensor", "Forbidden")
            query = query.where(AlertModel.sensor_id == sensor_id)
        else:
            if current_user.role != RoleEnum.ADMIN.value:
                query = query.join(SensorModel).where(SensorModel.owner_id == current_user.id)

        if direction:
            query = query.where(AlertModel.direction == direction.value)

        if resolved is not None:
            query = query.where(AlertModel.resolved == resolved)

        if after_id is not None and after_id > 0:
            query = query.where(AlertModel.id > after_id)

        query = query.order_by(AlertModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())

        next_after_id = None
        if len(records) > limit:
            next_after_id = records[limit - 1].id
            records = records[:limit]

        items = [
            AlertOut(
                id=a.id,
                sensor_id=a.sensor_id,
                reading_id=a.reading_id,
                direction=DirectionEnum(a.direction),
                reading_value=a.reading_value,
                threshold_lower=a.threshold_lower,
                threshold_upper=a.threshold_upper,
                resolved=a.resolved,
                created_at=a.created_at.replace(tzinfo=timezone.utc) if a.created_at.tzinfo is None else a.created_at,
            )
            for a in records
        ]
        return AlertListResponse(items=items, next_after_id=next_after_id)


# -----------------------------------------------------------------------------
# FastAPI App Initialization & Routes
# -----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    with SessionLocal() as db:
        seed_database(db)
    yield


app = FastAPI(
    title="Smart Home - Telemetry and Sensor Ranges API",
    version="1.0.0",
    description="RESTful API for smart home telemetry, threshold policies, range aggregations, and alerts.",
    lifespan=lifespan,
)

# Exception handlers
@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "status_code": exc.status_code, "error_type": exc.error_type},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": str(exc),
            "status_code": status.HTTP_422_UNPROCESSABLE_ENTITY,
            "error_type": "Unprocessable Entity",
        },
    )


# Attach ASGI Middleware
app.add_middleware(JsonValidationASGIMiddleware)

# Immediate database init for TestClient
with SessionLocal() as _startup_db:
    seed_database(_startup_db)


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------
@app.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Health check probe",
    tags=["Health"],
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Local persistence unavailable",
            "model": ErrorResponse,
        }
    },
)
def get_health(db: Session = Depends(get_db)):
    return HealthController.get_health(db)


@app.get(
    "/sensors",
    response_model=SensorListResponse,
    status_code=status.HTTP_200_OK,
    summary="List accessible sensors",
    tags=["Sensors"],
)
def list_sensors(
    type: Optional[SensorTypeEnum] = Query(None, description="Optional sensor type filter"),
    serial_prefix: Optional[str] = Query(
        None, pattern=r"^[A-Z0-9-]{1,32}$", description="Optional serial prefix filter"
    ),
    owner_id: Optional[int] = Query(None, description="Admin-only filter by owner user id"),
    limit: int = Query(20, ge=1, le=100, description="Page limit 1..100"),
    after_id: Optional[int] = Query(None, gt=0, description="Cursor for pagination"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return SensorController.list_sensors(
        db, current_user, type, serial_prefix, owner_id, limit, after_id
    )


@app.post(
    "/sensors",
    response_model=SensorDetailOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create sensor (admin only)",
    tags=["Sensors"],
)
def create_sensor(
    payload: SensorCreatePayload,
    response: Response,
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    sensor = SensorController.create_sensor(db, current_user, payload)
    response.headers["Location"] = f"/sensors/{sensor.id}"
    return SensorController.build_sensor_detail(sensor)


@app.get(
    "/sensors/{sensor_id}",
    response_model=SensorDetailOut,
    status_code=status.HTTP_200_OK,
    summary="Get sensor details and policy summary",
    tags=["Sensors"],
)
def get_sensor(
    sensor_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sensor = SensorController.get_sensor(db, current_user, sensor_id)
    return SensorController.build_sensor_detail(sensor)


@app.patch(
    "/sensors/{sensor_id}",
    response_model=SensorDetailOut,
    status_code=status.HTTP_200_OK,
    summary="Patch sensor metadata (admin only)",
    tags=["Sensors"],
)
def patch_sensor(
    sensor_id: int,
    payload: SensorPatchPayload,
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    sensor = SensorController.patch_sensor(db, current_user, sensor_id, payload)
    return SensorController.build_sensor_detail(sensor)


@app.delete(
    "/sensors/{sensor_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete sensor (admin only)",
    tags=["Sensors"],
)
def delete_sensor(
    sensor_id: int,
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    SensorController.delete_sensor(db, current_user, sensor_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
    "/sensors/{sensor_id}/thresholds",
    response_model=Optional[ThresholdPolicyOut],
    status_code=status.HTTP_200_OK,
    summary="Get active threshold policy for sensor",
    tags=["Thresholds"],
)
def get_threshold(
    sensor_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    policy = ThresholdPolicyController.get_threshold(db, current_user, sensor_id)
    if not policy:
        return None
    c_at = policy.created_at.replace(tzinfo=timezone.utc) if policy.created_at.tzinfo is None else policy.created_at
    u_at = policy.updated_at.replace(tzinfo=timezone.utc) if policy.updated_at.tzinfo is None else policy.updated_at
    return ThresholdPolicyOut(
        id=policy.id,
        sensor_id=policy.sensor_id,
        lower_bound=policy.lower_bound,
        upper_bound=policy.upper_bound,
        created_at=c_at,
        updated_at=u_at,
    )


@app.post(
    "/sensors/{sensor_id}/thresholds",
    response_model=ThresholdPolicyOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create active threshold policy for sensor",
    tags=["Thresholds"],
)
def create_threshold(
    sensor_id: int,
    payload: ThresholdPolicyCreatePayload,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    policy = ThresholdPolicyController.create_threshold(db, current_user, sensor_id, payload)
    response.headers["Location"] = f"/sensors/{sensor_id}/thresholds"
    c_at = policy.created_at.replace(tzinfo=timezone.utc) if policy.created_at.tzinfo is None else policy.created_at
    u_at = policy.updated_at.replace(tzinfo=timezone.utc) if policy.updated_at.tzinfo is None else policy.updated_at
    return ThresholdPolicyOut(
        id=policy.id,
        sensor_id=policy.sensor_id,
        lower_bound=policy.lower_bound,
        upper_bound=policy.upper_bound,
        created_at=c_at,
        updated_at=u_at,
    )


@app.patch(
    "/sensors/{sensor_id}/thresholds",
    response_model=ThresholdPolicyOut,
    status_code=status.HTTP_200_OK,
    summary="Patch active threshold policy bounds",
    tags=["Thresholds"],
)
def patch_threshold(
    sensor_id: int,
    payload: ThresholdPolicyPatchPayload,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    policy = ThresholdPolicyController.patch_threshold(db, current_user, sensor_id, payload)
    c_at = policy.created_at.replace(tzinfo=timezone.utc) if policy.created_at.tzinfo is None else policy.created_at
    u_at = policy.updated_at.replace(tzinfo=timezone.utc) if policy.updated_at.tzinfo is None else policy.updated_at
    return ThresholdPolicyOut(
        id=policy.id,
        sensor_id=policy.sensor_id,
        lower_bound=policy.lower_bound,
        upper_bound=policy.upper_bound,
        created_at=c_at,
        updated_at=u_at,
    )


@app.delete(
    "/sensors/{sensor_id}/thresholds",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete active threshold policy",
    tags=["Thresholds"],
)
def delete_threshold(
    sensor_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ThresholdPolicyController.delete_threshold(db, current_user, sensor_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/sensors/{sensor_id}/readings",
    response_model=ReadingOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create sensor telemetry reading and evaluate policy alerts",
    tags=["Readings"],
)
def create_reading(
    sensor_id: int,
    payload: ReadingCreatePayload,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    reading, _ = ReadingController.create_reading(db, current_user, sensor_id, payload)
    response.headers["Location"] = f"/readings/{reading.id}"
    c_at = reading.captured_at.replace(tzinfo=timezone.utc) if reading.captured_at.tzinfo is None else reading.captured_at
    cr_at = reading.created_at.replace(tzinfo=timezone.utc) if reading.created_at.tzinfo is None else reading.created_at
    return ReadingOut(
        id=reading.id,
        sensor_id=reading.sensor_id,
        value=reading.value,
        captured_at=c_at,
        created_at=cr_at,
    )


@app.get(
    "/sensors/{sensor_id}/readings",
    response_model=ReadingListResponse,
    status_code=status.HTTP_200_OK,
    summary="List sensor readings with time filters and pagination",
    tags=["Readings"],
)
def list_readings(
    sensor_id: int,
    from_time: Optional[datetime] = Query(None, alias="from", description="Optional inclusive start UTC timestamp"),
    to_time: Optional[datetime] = Query(None, alias="to", description="Optional exclusive end UTC timestamp"),
    limit: int = Query(20, ge=1, le=100, description="Page limit 1..100"),
    after_id: Optional[int] = Query(None, gt=0, description="Cursor for pagination"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ReadingController.list_readings(
        db, current_user, sensor_id, from_time, to_time, limit, after_id
    )


@app.get(
    "/readings/{reading_id}",
    response_model=ReadingOut,
    status_code=status.HTTP_200_OK,
    summary="Get immutable reading by id",
    tags=["Readings"],
)
def get_reading(
    reading_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    reading = ReadingController.get_reading(db, current_user, reading_id)
    c_at = reading.captured_at.replace(tzinfo=timezone.utc) if reading.captured_at.tzinfo is None else reading.captured_at
    cr_at = reading.created_at.replace(tzinfo=timezone.utc) if reading.created_at.tzinfo is None else reading.created_at
    return ReadingOut(
        id=reading.id,
        sensor_id=reading.sensor_id,
        value=reading.value,
        captured_at=c_at,
        created_at=cr_at,
    )


@app.get(
    "/sensors/{sensor_id}/latest",
    response_model=Optional[ReadingOut],
    status_code=status.HTTP_200_OK,
    summary="Get latest reading for sensor",
    tags=["Readings"],
)
def get_latest_reading(
    sensor_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ReadingController.get_latest_reading(db, current_user, sensor_id)


@app.get(
    "/sensors/{sensor_id}/range",
    response_model=TelemetryRangeOut,
    status_code=status.HTTP_200_OK,
    summary="Get telemetry aggregation in half-open interval [from, to)",
    tags=["Readings"],
)
def get_range(
    sensor_id: int,
    from_time: datetime = Query(..., alias="from", description="Required inclusive start UTC timestamp"),
    to_time: datetime = Query(..., alias="to", description="Required exclusive end UTC timestamp"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ReadingController.get_range(db, current_user, sensor_id, from_time, to_time)


@app.get(
    "/alerts",
    response_model=AlertListResponse,
    status_code=status.HTTP_200_OK,
    summary="List alerts with optional filters and pagination",
    tags=["Alerts"],
)
def list_alerts(
    sensor_id: Optional[int] = Query(None, description="Optional sensor filter"),
    direction: Optional[DirectionEnum] = Query(None, description="Optional direction filter"),
    resolved: Optional[bool] = Query(None, description="Optional resolved filter"),
    limit: int = Query(20, ge=1, le=100, description="Page limit 1..100"),
    after_id: Optional[int] = Query(None, gt=0, description="Cursor for pagination"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return AlertController.list_alerts(
        db, current_user, sensor_id, direction, resolved, limit, after_id
    )
