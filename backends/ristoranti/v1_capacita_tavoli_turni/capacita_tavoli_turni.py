"""
Capacita Tavoli e Turni di Servizio - Restaurant Management API
Case ID: 4
Variant: A: Table capacity and service turns

Technical specifications:
- Python 3.10+, FastAPI, Pydantic v2, SQLAlchemy ORM, SQLite in-memory with StaticPool.
- Pure ASGI middleware for 10 MiB limit (413), JSON media type (415), invalid JSON (400), preserving 405 Method Not Allowed.
- MVC architecture: SQLAlchemy models, Pydantic schemas, static controllers, thin FastAPI routes.
- Seed data: user1 (id=1, user), user2 (id=2, user), admin (id=3, admin), tables, and shifts.
- Standardized error format: {"detail": str, "status_code": int, "error_type": str}.
- GET /health reporting {"status": "ok", "database": "connected"}.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
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
logger = logging.getLogger("capacita_tavoli_turni")

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
class RoleEnum(str, Enum):
    USER = "user"
    ADMIN = "admin"


class ZoneEnum(str, Enum):
    INDOOR = "INDOOR"
    TERRACE = "TERRACE"
    BAR = "BAR"
    PRIVATE = "PRIVATE"


class ServiceKindEnum(str, Enum):
    BREAKFAST = "BREAKFAST"
    LUNCH = "LUNCH"
    DINNER = "DINNER"


class SeatingStatusEnum(str, Enum):
    BOOKED = "BOOKED"
    SEATED = "SEATED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


# -----------------------------------------------------------------------------
# Domain Invariant Helpers
# -----------------------------------------------------------------------------
MIN_DATE = date(2025, 1, 1)
MAX_DATE = date(2035, 12, 31)
MIN_DATETIME = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
MAX_DATETIME = datetime(2035, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


def normalize_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise AppException(422, "Timestamp must be offset-aware UTC ISO-8601", "Unprocessable Entity")
    return dt.astimezone(timezone.utc)


def validate_date_range(d: date, field_name: str):
    if d < MIN_DATE or d > MAX_DATE:
        raise AppException(
            422, f"{field_name} must be between 2025-01-01 and 2035-12-31", "Unprocessable Entity"
        )


def validate_datetime_range(dt: datetime, field_name: str) -> datetime:
    dt_utc = normalize_utc(dt)
    if dt_utc < MIN_DATETIME or dt_utc > MAX_DATETIME:
        raise AppException(
            422,
            f"{field_name} must be within 2025-01-01T00:00:00Z and 2035-12-31T23:59:59Z",
            "Unprocessable Entity",
        )
    return dt_utc


# -----------------------------------------------------------------------------
# SQLAlchemy Models
# -----------------------------------------------------------------------------
class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    role = Column(String(20), nullable=False)
    token = Column(String(100), unique=True, nullable=False)


class TableModel(Base):
    __tablename__ = "tables"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(12), unique=True, nullable=False, index=True)
    seats = Column(Integer, nullable=False)
    zone = Column(String(20), nullable=False, index=True)
    active = Column(Boolean, default=True, nullable=False)
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)

    seatings = relationship("SeatingModel", back_populates="table")


class ServiceShiftModel(Base):
    __tablename__ = "service_shifts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    service_date = Column(Date, nullable=False, index=True)
    service_kind = Column(String(20), nullable=False, index=True)
    start_at = Column(DateTime(timezone=True), nullable=False)
    end_at = Column(DateTime(timezone=True), nullable=False)
    turn_count = Column(Integer, nullable=False)
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)

    seatings = relationship("SeatingModel", back_populates="shift")


class SeatingModel(Base):
    __tablename__ = "seatings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_id = Column(Integer, ForeignKey("tables.id"), nullable=False, index=True)
    shift_id = Column(Integer, ForeignKey("service_shifts.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    turn_number = Column(Integer, nullable=False)
    party_size = Column(Integer, nullable=False)
    guest_name = Column(String(120), nullable=False)
    status = Column(String(20), nullable=False, default=SeatingStatusEnum.BOOKED.value, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    table = relationship("TableModel", back_populates="seatings")
    shift = relationship("ServiceShiftModel", back_populates="seatings")
    user = relationship("UserModel", foreign_keys=[user_id])
    cancelled_by_user = relationship("UserModel", foreign_keys=[cancelled_by_user_id])


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


class TableCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(..., pattern=r"^[A-Z][A-Z0-9-]{0,11}$")
    zone: ZoneEnum
    seats: int = Field(..., ge=1, le=20)
    active: bool = True


class TablePatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Optional[str] = Field(None, pattern=r"^[A-Z][A-Z0-9-]{0,11}$")
    zone: Optional[ZoneEnum] = None
    seats: Optional[int] = Field(None, ge=1, le=20)
    active: Optional[bool] = None

    @model_validator(mode="after")
    def check_non_empty(self) -> "TablePatchPayload":
        if self.code is None and self.zone is None and self.seats is None and self.active is None:
            raise ValueError("At least one field must be supplied for patch")
        return self


class TableOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    code: str
    zone: ZoneEnum
    seats: int
    active: bool


class TableListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[TableOut]
    next_after_id: Optional[int] = None


class ShiftCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_date: date
    service_kind: ServiceKindEnum
    start_at: datetime
    end_at: datetime
    turn_count: int = Field(..., ge=1, le=4)


class ShiftPatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None
    turn_count: Optional[int] = Field(None, ge=1, le=4)

    @model_validator(mode="after")
    def check_non_empty(self) -> "ShiftPatchPayload":
        if self.start_at is None and self.end_at is None and self.turn_count is None:
            raise ValueError("At least one field must be supplied for patch")
        return self


class ShiftOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    service_date: date
    service_kind: ServiceKindEnum
    start_at: datetime
    end_at: datetime
    turn_count: int


class ShiftListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[ShiftOut]
    next_after_id: Optional[int] = None


class SeatingCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table_id: int = Field(..., gt=0)
    shift_id: int = Field(..., gt=0)
    turn_number: int = Field(..., ge=1, le=4)
    party_size: int = Field(..., ge=1, le=20)
    guest_name: str = Field(..., min_length=2, max_length=120)


class SeatingPatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guest_name: Optional[str] = Field(None, min_length=2, max_length=120)
    party_size: Optional[int] = Field(None, ge=1, le=20)
    table_id: Optional[int] = Field(None, gt=0)
    shift_id: Optional[int] = Field(None, gt=0)
    turn_number: Optional[int] = Field(None, ge=1, le=4)
    status: Optional[SeatingStatusEnum] = None

    @model_validator(mode="after")
    def check_non_empty(self) -> "SeatingPatchPayload":
        if (
            self.guest_name is None
            and self.party_size is None
            and self.table_id is None
            and self.shift_id is None
            and self.turn_number is None
            and self.status is None
        ):
            raise ValueError("At least one field must be supplied for patch")
        return self


class SeatingCancelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cancelled_at: datetime


class SeatingOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    table_id: int
    shift_id: int
    user_id: int
    turn_number: int
    party_size: int
    guest_name: str
    status: SeatingStatusEnum
    created_at: datetime
    cancelled_at: Optional[datetime] = None
    cancelled_by_user_id: Optional[int] = None


class SeatingListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[SeatingOut]
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
        db.flush()

        t1 = TableModel(id=1, code="T01", zone=ZoneEnum.INDOOR.value, seats=2, active=True)
        t2 = TableModel(id=2, code="T02", zone=ZoneEnum.TERRACE.value, seats=4, active=True)
        t3 = TableModel(id=3, code="T03", zone=ZoneEnum.PRIVATE.value, seats=6, active=True)
        db.add_all([t1, t2, t3])
        db.flush()

        s1 = ServiceShiftModel(
            id=1,
            service_date=date(2026, 9, 10),
            service_kind=ServiceKindEnum.LUNCH.value,
            start_at=datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 10, 15, 0, 0, tzinfo=timezone.utc),
            turn_count=2,
        )
        s2 = ServiceShiftModel(
            id=2,
            service_date=date(2026, 9, 10),
            service_kind=ServiceKindEnum.DINNER.value,
            start_at=datetime(2026, 9, 10, 19, 0, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 10, 23, 0, 0, tzinfo=timezone.utc),
            turn_count=3,
        )
        db.add_all([s1, s2])
        db.commit()


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
MAX_BODY_SIZE = 10 * 1024 * 1024


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

        headers_dict = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope["headers"]}
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

            if body:
                try:
                    json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as err:
                    await self._send_json_error(
                        send, 400, f"Malformed JSON body: {str(err)}", "Bad Request"
                    )
                    return

            async def custom_receive():
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            await self.app(scope, custom_receive, send)
            return

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
            logger.error(f"Health probe failed: {e}")
            raise AppException(503, "Local persistence probe failed", "Service Unavailable")


class TableController:
    @staticmethod
    def list_tables(
        db: Session,
        zone: Optional[ZoneEnum],
        min_seats: Optional[int],
        active: Optional[bool],
        limit: int,
        after_id: Optional[int],
    ) -> TableListResponse:
        query = select(TableModel).where(TableModel.is_deleted == False)

        if zone is not None:
            query = query.where(TableModel.zone == zone.value)
        if min_seats is not None:
            query = query.where(TableModel.seats >= min_seats)
        if active is not None:
            query = query.where(TableModel.active == active)
        if after_id is not None and after_id > 0:
            query = query.where(TableModel.id > after_id)

        query = query.order_by(TableModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())

        next_after_id = None
        if len(records) > limit:
            next_after_id = records[limit - 1].id
            records = records[:limit]

        items = [
            TableOut(
                id=t.id,
                code=t.code,
                zone=ZoneEnum(t.zone),
                seats=t.seats,
                active=t.active,
            )
            for t in records
        ]
        return TableListResponse(items=items, next_after_id=next_after_id)

    @staticmethod
    def create_table(db: Session, payload: TableCreatePayload) -> TableModel:
        existing = db.scalar(
            select(TableModel).where(TableModel.code == payload.code, TableModel.is_deleted == False)
        )
        if existing:
            raise AppException(409, f"Table code '{payload.code}' already exists", "Conflict")

        table = TableModel(
            code=payload.code,
            zone=payload.zone.value,
            seats=payload.seats,
            active=payload.active,
            is_deleted=False,
        )
        db.add(table)
        db.commit()
        db.refresh(table)
        return table

    @staticmethod
    def get_table(db: Session, table_id: int) -> TableModel:
        if table_id <= 0:
            raise AppException(404, "table_id must be a positive integer", "Not Found")
        table = db.scalar(
            select(TableModel).where(TableModel.id == table_id, TableModel.is_deleted == False)
        )
        if not table:
            raise AppException(404, f"Table {table_id} not found", "Not Found")
        return table

    @staticmethod
    def patch_table(db: Session, table_id: int, payload: TablePatchPayload) -> TableModel:
        table = TableController.get_table(db, table_id)

        if payload.code is not None and payload.code != table.code:
            dup = db.scalar(
                select(TableModel).where(
                    TableModel.code == payload.code,
                    TableModel.id != table_id,
                    TableModel.is_deleted == False,
                )
            )
            if dup:
                raise AppException(409, f"Table code '{payload.code}' already exists", "Conflict")
            table.code = payload.code

        if payload.active is False and table.active is True:
            # Check for active seatings (BOOKED or SEATED)
            has_active_seating = db.scalar(
                select(SeatingModel)
                .where(
                    SeatingModel.table_id == table_id,
                    SeatingModel.status.in_([SeatingStatusEnum.BOOKED.value, SeatingStatusEnum.SEATED.value]),
                )
                .limit(1)
            )
            if has_active_seating:
                raise AppException(
                    409, "Cannot deactivate table with BOOKED or SEATED seatings", "Conflict"
                )
            table.active = False
        elif payload.active is True:
            table.active = True

        if payload.seats is not None and payload.seats != table.seats:
            # Check if active seating exceeds new seats
            max_party = db.scalar(
                select(SeatingModel.party_size)
                .where(
                    SeatingModel.table_id == table_id,
                    SeatingModel.status.in_([SeatingStatusEnum.BOOKED.value, SeatingStatusEnum.SEATED.value]),
                )
                .order_by(SeatingModel.party_size.desc())
                .limit(1)
            )
            if max_party and max_party > payload.seats:
                raise AppException(
                    409,
                    f"Cannot reduce seats to {payload.seats}: active seating has party_size {max_party}",
                    "Conflict",
                )
            table.seats = payload.seats

        if payload.zone is not None:
            table.zone = payload.zone.value

        db.commit()
        db.refresh(table)
        return table

    @staticmethod
    def delete_table(db: Session, table_id: int):
        table = TableController.get_table(db, table_id)

        # REQ-4-007: table can be soft-deleted only with no seating of any status
        has_seating = db.scalar(select(SeatingModel).where(SeatingModel.table_id == table_id).limit(1))
        if has_seating:
            raise AppException(409, "Cannot delete table referenced by seating history", "Conflict")

        table.is_deleted = True
        db.commit()


class ShiftController:
    @staticmethod
    def list_shifts(
        db: Session,
        service_date: Optional[date],
        service_kind: Optional[ServiceKindEnum],
        date_from: Optional[date],
        date_to: Optional[date],
        limit: int,
        after_id: Optional[int],
    ) -> ShiftListResponse:
        query = select(ServiceShiftModel).where(ServiceShiftModel.is_deleted == False)

        if service_date is not None:
            query = query.where(ServiceShiftModel.service_date == service_date)
        if service_kind is not None:
            query = query.where(ServiceShiftModel.service_kind == service_kind.value)

        if date_from is not None and date_to is not None:
            if date_from > date_to:
                raise AppException(422, "date_from must be before or equal to date_to", "Unprocessable Entity")
            query = query.where(
                ServiceShiftModel.service_date >= date_from,
                ServiceShiftModel.service_date <= date_to,
            )
        elif date_from is not None:
            query = query.where(ServiceShiftModel.service_date >= date_from)
        elif date_to is not None:
            query = query.where(ServiceShiftModel.service_date <= date_to)

        if after_id is not None and after_id > 0:
            query = query.where(ServiceShiftModel.id > after_id)

        query = query.order_by(ServiceShiftModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())

        next_after_id = None
        if len(records) > limit:
            next_after_id = records[limit - 1].id
            records = records[:limit]

        items = [
            ShiftOut(
                id=s.id,
                service_date=s.service_date,
                service_kind=ServiceKindEnum(s.service_kind),
                start_at=s.start_at.replace(tzinfo=timezone.utc) if s.start_at.tzinfo is None else s.start_at,
                end_at=s.end_at.replace(tzinfo=timezone.utc) if s.end_at.tzinfo is None else s.end_at,
                turn_count=s.turn_count,
            )
            for s in records
        ]
        return ShiftListResponse(items=items, next_after_id=next_after_id)

    @staticmethod
    def create_shift(db: Session, payload: ShiftCreatePayload) -> ServiceShiftModel:
        validate_date_range(payload.service_date, "service_date")
        s_utc = validate_datetime_range(payload.start_at, "start_at")
        e_utc = validate_datetime_range(payload.end_at, "end_at")

        if s_utc.date() != payload.service_date or e_utc.date() != payload.service_date:
            raise AppException(
                422, "start_at and end_at must be on the same service_date", "Unprocessable Entity"
            )
        if e_utc <= s_utc:
            raise AppException(422, "end_at must be strictly later than start_at", "Unprocessable Entity")

        # REQ-4-003: A service_date and service_kind pair is unique
        existing = db.scalar(
            select(ServiceShiftModel).where(
                ServiceShiftModel.service_date == payload.service_date,
                ServiceShiftModel.service_kind == payload.service_kind.value,
                ServiceShiftModel.is_deleted == False,
            )
        )
        if existing:
            raise AppException(
                409,
                f"Shift for date '{payload.service_date}' and kind '{payload.service_kind.value}' already exists",
                "Conflict",
            )

        shift = ServiceShiftModel(
            service_date=payload.service_date,
            service_kind=payload.service_kind.value,
            start_at=s_utc,
            end_at=e_utc,
            turn_count=payload.turn_count,
            is_deleted=False,
        )
        db.add(shift)
        db.commit()
        db.refresh(shift)
        return shift

    @staticmethod
    def get_shift(db: Session, shift_id: int) -> ServiceShiftModel:
        if shift_id <= 0:
            raise AppException(404, "shift_id must be a positive integer", "Not Found")
        shift = db.scalar(
            select(ServiceShiftModel).where(
                ServiceShiftModel.id == shift_id, ServiceShiftModel.is_deleted == False
            )
        )
        if not shift:
            raise AppException(404, f"Service shift {shift_id} not found", "Not Found")
        return shift

    @staticmethod
    def patch_shift(db: Session, shift_id: int, payload: ShiftPatchPayload) -> ServiceShiftModel:
        shift = ShiftController.get_shift(db, shift_id)

        # REQ-4-020: atomically update only a shift with no seating records
        has_seating = db.scalar(select(SeatingModel).where(SeatingModel.shift_id == shift_id).limit(1))
        if has_seating:
            raise AppException(409, "Cannot patch shift referenced by seatings", "Conflict")

        s_utc = validate_datetime_range(payload.start_at, "start_at") if payload.start_at else shift.start_at
        e_utc = validate_datetime_range(payload.end_at, "end_at") if payload.end_at else shift.end_at

        if s_utc.date() != shift.service_date or e_utc.date() != shift.service_date:
            raise AppException(
                422, "start_at and end_at must be on the shift service_date", "Unprocessable Entity"
            )
        if e_utc <= s_utc:
            raise AppException(422, "end_at must be strictly later than start_at", "Unprocessable Entity")

        if payload.start_at is not None:
            shift.start_at = s_utc
        if payload.end_at is not None:
            shift.end_at = e_utc
        if payload.turn_count is not None:
            shift.turn_count = payload.turn_count

        db.commit()
        db.refresh(shift)
        return shift

    @staticmethod
    def delete_shift(db: Session, shift_id: int):
        shift = ShiftController.get_shift(db, shift_id)

        # REQ-4-007 & REQ-4-021: soft-delete only with no seating records
        has_seating = db.scalar(select(SeatingModel).where(SeatingModel.shift_id == shift_id).limit(1))
        if has_seating:
            raise AppException(409, "Cannot delete shift referenced by seating records", "Conflict")

        shift.is_deleted = True
        db.commit()


class SeatingController:
    @staticmethod
    def list_seatings(
        db: Session,
        current_user: UserModel,
        table_id: Optional[int],
        shift_id: Optional[int],
        seating_status: Optional[SeatingStatusEnum],
        service_date: Optional[date],
        limit: int,
        after_id: Optional[int],
    ) -> SeatingListResponse:
        query = select(SeatingModel)

        if table_id is not None:
            if table_id <= 0:
                raise AppException(404, "table_id must be a positive integer", "Not Found")
            TableController.get_table(db, table_id)
            query = query.where(SeatingModel.table_id == table_id)

        if shift_id is not None:
            if shift_id <= 0:
                raise AppException(404, "shift_id must be a positive integer", "Not Found")
            ShiftController.get_shift(db, shift_id)
            query = query.where(SeatingModel.shift_id == shift_id)

        if current_user.role != RoleEnum.ADMIN.value:
            query = query.where(SeatingModel.user_id == current_user.id)

        if seating_status is not None:
            query = query.where(SeatingModel.status == seating_status.value)

        if service_date is not None:
            query = query.join(ServiceShiftModel).where(ServiceShiftModel.service_date == service_date)

        if after_id is not None and after_id > 0:
            query = query.where(SeatingModel.id > after_id)

        query = query.order_by(SeatingModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())

        next_after_id = None
        if len(records) > limit:
            next_after_id = records[limit - 1].id
            records = records[:limit]

        items = [
            SeatingOut(
                id=s.id,
                table_id=s.table_id,
                shift_id=s.shift_id,
                user_id=s.user_id,
                turn_number=s.turn_number,
                party_size=s.party_size,
                guest_name=s.guest_name,
                status=SeatingStatusEnum(s.status),
                created_at=s.created_at.replace(tzinfo=timezone.utc) if s.created_at.tzinfo is None else s.created_at,
                cancelled_at=s.cancelled_at.replace(tzinfo=timezone.utc) if s.cancelled_at and s.cancelled_at.tzinfo is None else s.cancelled_at,
                cancelled_by_user_id=s.cancelled_by_user_id,
            )
            for s in records
        ]
        return SeatingListResponse(items=items, next_after_id=next_after_id)

    @staticmethod
    def create_seating(
        db: Session, current_user: UserModel, payload: SeatingCreatePayload
    ) -> SeatingModel:
        table = TableController.get_table(db, payload.table_id)
        if not table.active:
            raise AppException(409, f"Table {payload.table_id} is inactive", "Conflict")

        shift = ShiftController.get_shift(db, payload.shift_id)

        if payload.turn_number < 1 or payload.turn_number > shift.turn_count:
            raise AppException(
                409,
                f"turn_number {payload.turn_number} exceeds shift turn count {shift.turn_count}",
                "Conflict",
            )

        if payload.party_size > table.seats:
            raise AppException(
                409,
                f"party_size {payload.party_size} exceeds table capacity {table.seats}",
                "Conflict",
            )

        trimmed_name = payload.guest_name.strip()
        if len(trimmed_name) < 2 or len(trimmed_name) > 120:
            raise AppException(422, "guest_name must be 2..120 trimmed characters", "Unprocessable Entity")

        # REQ-4-005: For each non-cancelled seating, table_id, shift_id, and turn_number are unique
        existing = db.scalar(
            select(SeatingModel).where(
                SeatingModel.table_id == payload.table_id,
                SeatingModel.shift_id == payload.shift_id,
                SeatingModel.turn_number == payload.turn_number,
                SeatingModel.status != SeatingStatusEnum.CANCELLED.value,
            )
        )
        if existing:
            raise AppException(
                409,
                f"Table {payload.table_id} is already booked for shift {payload.shift_id} turn {payload.turn_number}",
                "Conflict",
            )

        now = datetime.now(timezone.utc)
        seating = SeatingModel(
            table_id=payload.table_id,
            shift_id=payload.shift_id,
            user_id=current_user.id,
            turn_number=payload.turn_number,
            party_size=payload.party_size,
            guest_name=trimmed_name,
            status=SeatingStatusEnum.BOOKED.value,
            created_at=now,
        )
        db.add(seating)
        db.commit()
        db.refresh(seating)
        return seating

    @staticmethod
    def get_seating(db: Session, current_user: UserModel, seating_id: int) -> SeatingModel:
        if seating_id <= 0:
            raise AppException(404, "seating_id must be a positive integer", "Not Found")

        seating = db.scalar(select(SeatingModel).where(SeatingModel.id == seating_id))
        if not seating:
            raise AppException(404, f"Seating {seating_id} not found", "Not Found")

        if current_user.role != RoleEnum.ADMIN.value and seating.user_id != current_user.id:
            raise AppException(403, "Access denied to seating of another user", "Forbidden")

        return seating

    @staticmethod
    def patch_seating(
        db: Session, current_user: UserModel, seating_id: int, payload: SeatingPatchPayload
    ) -> SeatingModel:
        seating = SeatingController.get_seating(db, current_user, seating_id)

        # REQ-4-025: permit only BOOKED seatings
        if seating.status != SeatingStatusEnum.BOOKED.value:
            raise AppException(
                409, f"Only BOOKED seatings can be patched (current status is '{seating.status}')", "Conflict"
            )

        target_table_id = payload.table_id if payload.table_id is not None else seating.table_id
        target_shift_id = payload.shift_id if payload.shift_id is not None else seating.shift_id
        target_turn_number = payload.turn_number if payload.turn_number is not None else seating.turn_number
        target_party_size = payload.party_size if payload.party_size is not None else seating.party_size

        table = TableController.get_table(db, target_table_id)
        if not table.active:
            raise AppException(409, f"Target table {target_table_id} is inactive", "Conflict")

        shift = ShiftController.get_shift(db, target_shift_id)

        if target_turn_number < 1 or target_turn_number > shift.turn_count:
            raise AppException(
                409,
                f"turn_number {target_turn_number} exceeds shift turn count {shift.turn_count}",
                "Conflict",
            )

        if target_party_size > table.seats:
            raise AppException(
                409,
                f"party_size {target_party_size} exceeds table capacity {table.seats}",
                "Conflict",
            )

        # Uniqueness check for target slot if slot changed
        if (
            target_table_id != seating.table_id
            or target_shift_id != seating.shift_id
            or target_turn_number != seating.turn_number
        ):
            dup = db.scalar(
                select(SeatingModel).where(
                    SeatingModel.table_id == target_table_id,
                    SeatingModel.shift_id == target_shift_id,
                    SeatingModel.turn_number == target_turn_number,
                    SeatingModel.id != seating_id,
                    SeatingModel.status != SeatingStatusEnum.CANCELLED.value,
                )
            )
            if dup:
                raise AppException(
                    409,
                    f"Target slot (table {target_table_id}, shift {target_shift_id}, turn {target_turn_number}) is already occupied",
                    "Conflict",
                )

        if payload.guest_name is not None:
            trimmed = payload.guest_name.strip()
            if len(trimmed) < 2 or len(trimmed) > 120:
                raise AppException(422, "guest_name must be 2..120 trimmed characters", "Unprocessable Entity")
            seating.guest_name = trimmed

        if payload.status is not None:
            # REQ-4-006: BOOKED can transition to SEATED or CANCELLED
            if payload.status == SeatingStatusEnum.SEATED:
                seating.status = SeatingStatusEnum.SEATED.value
            elif payload.status == SeatingStatusEnum.CANCELLED:
                seating.status = SeatingStatusEnum.CANCELLED.value
                seating.cancelled_at = datetime.now(timezone.utc)
                seating.cancelled_by_user_id = current_user.id
            else:
                raise AppException(
                    409, f"Invalid seating status transition from BOOKED to {payload.status.value}", "Conflict"
                )

        seating.table_id = target_table_id
        seating.shift_id = target_shift_id
        seating.turn_number = target_turn_number
        seating.party_size = target_party_size

        db.commit()
        db.refresh(seating)
        return seating

    @staticmethod
    def cancel_seating(
        db: Session, current_user: UserModel, seating_id: int, payload: SeatingCancelPayload
    ) -> SeatingModel:
        seating = SeatingController.get_seating(db, current_user, seating_id)

        # REQ-4-006 & REQ-4-026: permit BOOKED or SEATED transitions only
        if seating.status not in (SeatingStatusEnum.BOOKED.value, SeatingStatusEnum.SEATED.value):
            raise AppException(
                409,
                f"Cannot cancel seating in terminal status '{seating.status}'",
                "Conflict",
            )

        c_utc = normalize_utc(payload.cancelled_at)
        seating.status = SeatingStatusEnum.CANCELLED.value
        seating.cancelled_at = c_utc
        seating.cancelled_by_user_id = current_user.id

        db.commit()
        db.refresh(seating)
        return seating


# -----------------------------------------------------------------------------
# FastAPI App Initialization & Routes
# -----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    with SessionLocal() as db:
        seed_database(db)
    yield


app = FastAPI(
    title="Restaurant Management - Table Capacity and Service Turns API",
    version="1.0.0",
    description="RESTful API for restaurant tables, service shifts, turns, and seating lifecycle.",
    lifespan=lifespan,
)


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
    responses={503: {"description": "Service unavailable"}},
)
def get_health(db: Session = Depends(get_db)):
    return HealthController.get_health(db)


@app.get(
    "/tables",
    response_model=TableListResponse,
    status_code=status.HTTP_200_OK,
    summary="List tables",
    tags=["Tables"],
)
def list_tables(
    zone: Optional[ZoneEnum] = Query(None, description="Filter by zone"),
    min_seats: Optional[int] = Query(None, ge=1, le=20, description="Filter by minimum seats"),
    active: Optional[bool] = Query(None, description="Filter by active status"),
    limit: int = Query(20, ge=1, le=100, description="Pagination limit"),
    after_id: Optional[int] = Query(None, gt=0, description="Cursor for pagination"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return TableController.list_tables(db, zone, min_seats, active, limit, after_id)


@app.post(
    "/tables",
    response_model=TableOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create table (admin only)",
    tags=["Tables"],
)
def create_table(
    payload: TableCreatePayload,
    response: Response,
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    table = TableController.create_table(db, payload)
    response.headers["Location"] = f"/tables/{table.id}"
    return TableOut(
        id=table.id,
        code=table.code,
        zone=ZoneEnum(table.zone),
        seats=table.seats,
        active=table.active,
    )


@app.get(
    "/tables/{table_id}",
    response_model=TableOut,
    status_code=status.HTTP_200_OK,
    summary="Get table by id",
    tags=["Tables"],
)
def get_table(
    table_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    table = TableController.get_table(db, table_id)
    return TableOut(
        id=table.id,
        code=table.code,
        zone=ZoneEnum(table.zone),
        seats=table.seats,
        active=table.active,
    )


@app.patch(
    "/tables/{table_id}",
    response_model=TableOut,
    status_code=status.HTTP_200_OK,
    summary="Patch table metadata (admin only)",
    tags=["Tables"],
)
def patch_table(
    table_id: int,
    payload: TablePatchPayload,
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    table = TableController.patch_table(db, table_id, payload)
    return TableOut(
        id=table.id,
        code=table.code,
        zone=ZoneEnum(table.zone),
        seats=table.seats,
        active=table.active,
    )


@app.delete(
    "/tables/{table_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete table (admin only)",
    tags=["Tables"],
)
def delete_table(
    table_id: int,
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    TableController.delete_table(db, table_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
    "/service-shifts",
    response_model=ShiftListResponse,
    status_code=status.HTTP_200_OK,
    summary="List service shifts",
    tags=["Shifts"],
)
def list_shifts(
    service_date: Optional[date] = Query(None, description="Filter by service date"),
    service_kind: Optional[ServiceKindEnum] = Query(None, description="Filter by kind"),
    date_from: Optional[date] = Query(None, description="Filter date from (inclusive)"),
    date_to: Optional[date] = Query(None, description="Filter date to (inclusive)"),
    limit: int = Query(20, ge=1, le=100, description="Pagination limit"),
    after_id: Optional[int] = Query(None, gt=0, description="Pagination cursor"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ShiftController.list_shifts(
        db, service_date, service_kind, date_from, date_to, limit, after_id
    )


@app.post(
    "/service-shifts",
    response_model=ShiftOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create service shift (admin only)",
    tags=["Shifts"],
)
def create_shift(
    payload: ShiftCreatePayload,
    response: Response,
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    shift = ShiftController.create_shift(db, payload)
    response.headers["Location"] = f"/service-shifts/{shift.id}"
    return ShiftOut(
        id=shift.id,
        service_date=shift.service_date,
        service_kind=ServiceKindEnum(shift.service_kind),
        start_at=shift.start_at.replace(tzinfo=timezone.utc) if shift.start_at.tzinfo is None else shift.start_at,
        end_at=shift.end_at.replace(tzinfo=timezone.utc) if shift.end_at.tzinfo is None else shift.end_at,
        turn_count=shift.turn_count,
    )


@app.get(
    "/service-shifts/{shift_id}",
    response_model=ShiftOut,
    status_code=status.HTTP_200_OK,
    summary="Get service shift by id",
    tags=["Shifts"],
)
def get_shift(
    shift_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    shift = ShiftController.get_shift(db, shift_id)
    return ShiftOut(
        id=shift.id,
        service_date=shift.service_date,
        service_kind=ServiceKindEnum(shift.service_kind),
        start_at=shift.start_at.replace(tzinfo=timezone.utc) if shift.start_at.tzinfo is None else shift.start_at,
        end_at=shift.end_at.replace(tzinfo=timezone.utc) if shift.end_at.tzinfo is None else shift.end_at,
        turn_count=shift.turn_count,
    )


@app.patch(
    "/service-shifts/{shift_id}",
    response_model=ShiftOut,
    status_code=status.HTTP_200_OK,
    summary="Patch service shift (admin only)",
    tags=["Shifts"],
)
def patch_shift(
    shift_id: int,
    payload: ShiftPatchPayload,
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    shift = ShiftController.patch_shift(db, shift_id, payload)
    return ShiftOut(
        id=shift.id,
        service_date=shift.service_date,
        service_kind=ServiceKindEnum(shift.service_kind),
        start_at=shift.start_at.replace(tzinfo=timezone.utc) if shift.start_at.tzinfo is None else shift.start_at,
        end_at=shift.end_at.replace(tzinfo=timezone.utc) if shift.end_at.tzinfo is None else shift.end_at,
        turn_count=shift.turn_count,
    )


@app.delete(
    "/service-shifts/{shift_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete service shift (admin only)",
    tags=["Shifts"],
)
def delete_shift(
    shift_id: int,
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ShiftController.delete_shift(db, shift_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
    "/seatings",
    response_model=SeatingListResponse,
    status_code=status.HTTP_200_OK,
    summary="List seatings",
    tags=["Seatings"],
)
def list_seatings(
    table_id: Optional[int] = Query(None, description="Filter by table id"),
    shift_id: Optional[int] = Query(None, description="Filter by shift id"),
    status: Optional[SeatingStatusEnum] = Query(None, description="Filter by seating status"),
    service_date: Optional[date] = Query(None, description="Filter by service date"),
    limit: int = Query(20, ge=1, le=100, description="Pagination limit"),
    after_id: Optional[int] = Query(None, gt=0, description="Pagination cursor"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return SeatingController.list_seatings(
        db, current_user, table_id, shift_id, status, service_date, limit, after_id
    )


@app.post(
    "/seatings",
    response_model=SeatingOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create seating",
    tags=["Seatings"],
)
def create_seating(
    payload: SeatingCreatePayload,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    seating = SeatingController.create_seating(db, current_user, payload)
    response.headers["Location"] = f"/seatings/{seating.id}"
    return SeatingOut(
        id=seating.id,
        table_id=seating.table_id,
        shift_id=seating.shift_id,
        user_id=seating.user_id,
        turn_number=seating.turn_number,
        party_size=seating.party_size,
        guest_name=seating.guest_name,
        status=SeatingStatusEnum(seating.status),
        created_at=seating.created_at.replace(tzinfo=timezone.utc) if seating.created_at.tzinfo is None else seating.created_at,
        cancelled_at=seating.cancelled_at.replace(tzinfo=timezone.utc) if seating.cancelled_at and seating.cancelled_at.tzinfo is None else seating.cancelled_at,
        cancelled_by_user_id=seating.cancelled_by_user_id,
    )

@app.get(
    "/seatings/{seating_id}",
    response_model=SeatingOut,
    status_code=status.HTTP_200_OK,
    summary="Get seating by id",
    tags=["Seatings"],
)
def get_seating(
    seating_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    seating = SeatingController.get_seating(db, current_user, seating_id)
    return SeatingOut(
        id=seating.id,
        table_id=seating.table_id,
        shift_id=seating.shift_id,
        user_id=seating.user_id,
        turn_number=seating.turn_number,
        party_size=seating.party_size,
        guest_name=seating.guest_name,
        status=SeatingStatusEnum(seating.status),
        created_at=seating.created_at.replace(tzinfo=timezone.utc) if seating.created_at.tzinfo is None else seating.created_at,
        cancelled_at=seating.cancelled_at.replace(tzinfo=timezone.utc) if seating.cancelled_at and seating.cancelled_at.tzinfo is None else seating.cancelled_at,
        cancelled_by_user_id=seating.cancelled_by_user_id,
    )


@app.patch(
    "/seatings/{seating_id}",
    response_model=SeatingOut,
    status_code=status.HTTP_200_OK,
    summary="Patch seating",
    tags=["Seatings"],
)
def patch_seating(
    seating_id: int,
    payload: SeatingPatchPayload,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    seating = SeatingController.patch_seating(db, current_user, seating_id, payload)
    return SeatingOut(
        id=seating.id,
        table_id=seating.table_id,
        shift_id=seating.shift_id,
        user_id=seating.user_id,
        turn_number=seating.turn_number,
        party_size=seating.party_size,
        guest_name=seating.guest_name,
        status=SeatingStatusEnum(seating.status),
        created_at=seating.created_at.replace(tzinfo=timezone.utc) if seating.created_at.tzinfo is None else seating.created_at,
        cancelled_at=seating.cancelled_at.replace(tzinfo=timezone.utc) if seating.cancelled_at and seating.cancelled_at.tzinfo is None else seating.cancelled_at,
        cancelled_by_user_id=seating.cancelled_by_user_id,
    )


@app.post(
    "/seatings/{seating_id}/cancel",
    response_model=SeatingOut,
    status_code=status.HTTP_200_OK,
    summary="Cancel seating",
    tags=["Seatings"],
)
def cancel_seating(
    seating_id: int,
    payload: SeatingCancelPayload,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    seating = SeatingController.cancel_seating(db, current_user, seating_id, payload)
    return SeatingOut(
        id=seating.id,
        table_id=seating.table_id,
        shift_id=seating.shift_id,
        user_id=seating.user_id,
        turn_number=seating.turn_number,
        party_size=seating.party_size,
        guest_name=seating.guest_name,
        status=SeatingStatusEnum(seating.status),
        created_at=seating.created_at.replace(tzinfo=timezone.utc) if seating.created_at.tzinfo is None else seating.created_at,
        cancelled_at=seating.cancelled_at.replace(tzinfo=timezone.utc) if seating.cancelled_at and seating.cancelled_at.tzinfo is None else seating.cancelled_at,
        cancelled_by_user_id=seating.cancelled_by_user_id,
    )
