"""
Overbooking e Prenotazioni Concorrenti - Restaurant Management API
Case ID: 4
Variant: B: Overbooking and concurrent reservations

Technical specifications:
- Python 3.10+, FastAPI, Pydantic v2, SQLAlchemy ORM, SQLite in-memory with StaticPool.
- Pure ASGI middleware for 10 MiB limit (413), JSON media type (415), invalid JSON (400), preserving 405 Method Not Allowed.
- MVC architecture: SQLAlchemy models, Pydantic schemas, static controllers, thin FastAPI routes.
- Seed data: user1 (id=1, user), user2 (id=2, user), admin (id=3, admin), tables with zero and nonzero overbook seats.
- Standardized error format: {"detail": str, "status_code": int, "error_type": str}.
- GET /health reporting {"status": "ok", "database": "connected"}.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    Boolean,
    Column,
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
logger = logging.getLogger("overbooking_prenotazioni")

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


class ReservationStatusEnum(str, Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    NO_SHOW = "NO_SHOW"


# -----------------------------------------------------------------------------
# Domain Invariant Helpers
# -----------------------------------------------------------------------------
MIN_DATETIME = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
MAX_DATETIME = datetime(2035, 12, 31, 23, 59, 59, tzinfo=timezone.utc)


def normalize_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise AppException(422, "Timestamp must be offset-aware UTC ISO-8601", "Unprocessable Entity")
    return dt.astimezone(timezone.utc)


def validate_timestamp_boundary(dt: datetime, field_name: str) -> datetime:
    dt_utc = normalize_utc(dt)
    if dt_utc < MIN_DATETIME or dt_utc > MAX_DATETIME:
        raise AppException(
            422,
            f"{field_name} must be within 2025-01-01T00:00:00Z and 2035-12-31T23:59:59Z",
            "Unprocessable Entity",
        )
    if dt_utc.minute % 5 != 0 or dt_utc.second != 0 or dt_utc.microsecond != 0:
        raise AppException(
            422,
            f"{field_name} must be on a 5-minute UTC boundary with second precision",
            "Unprocessable Entity",
        )
    return dt_utc


def validate_interval(start_at: datetime, end_at: datetime) -> Tuple[datetime, datetime]:
    s_utc = validate_timestamp_boundary(start_at, "start_at")
    e_utc = validate_timestamp_boundary(end_at, "end_at")
    if e_utc <= s_utc:
        raise AppException(422, "end_at must be strictly later than start_at", "Unprocessable Entity")
    duration_min = (e_utc - s_utc).total_seconds() / 60
    if duration_min < 30 or duration_min > 180:
        raise AppException(
            422,
            f"Reservation duration must be between 30 and 180 minutes (got {duration_min} minutes)",
            "Unprocessable Entity",
        )
    return s_utc, e_utc


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
    overbook_seats = Column(Integer, nullable=False, default=0)
    zone = Column(String(20), nullable=False, index=True)
    active = Column(Boolean, default=True, nullable=False)
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)

    reservations = relationship("ReservationModel", back_populates="table")


class ReservationModel(Base):
    __tablename__ = "reservations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_id = Column(Integer, ForeignKey("tables.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    guest_name = Column(String(120), nullable=False)
    party_size = Column(Integer, nullable=False)
    start_at = Column(DateTime(timezone=True), nullable=False, index=True)
    end_at = Column(DateTime(timezone=True), nullable=False, index=True)
    status = Column(String(20), nullable=False, default=ReservationStatusEnum.PENDING.value, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancellation_reason = Column(String(500), nullable=True)
    cancelled_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    no_show_at = Column(DateTime(timezone=True), nullable=True)
    no_show_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    table = relationship("TableModel", back_populates="reservations")
    user = relationship("UserModel", foreign_keys=[user_id])
    cancelled_by_user = relationship("UserModel", foreign_keys=[cancelled_by_user_id])
    no_show_by_user = relationship("UserModel", foreign_keys=[no_show_by_user_id])


# -----------------------------------------------------------------------------
# Capacity Helpers
# -----------------------------------------------------------------------------
def compute_table_demands(
    db: Session,
    table_id: int,
    start_at: datetime,
    end_at: datetime,
    exclude_reservation_id: Optional[int] = None,
) -> Tuple[int, int]:
    """
    Computes (max_active_demand, max_confirmed_demand) on [start_at, end_at).
    Active demand = PENDING + CONFIRMED.
    Confirmed demand = CONFIRMED.
    """
    query = select(ReservationModel).where(
        ReservationModel.table_id == table_id,
        ReservationModel.status.in_([ReservationStatusEnum.PENDING.value, ReservationStatusEnum.CONFIRMED.value]),
        ReservationModel.start_at < end_at,
        ReservationModel.end_at > start_at,
    )
    if exclude_reservation_id:
        query = query.where(ReservationModel.id != exclude_reservation_id)

    reservations = list(db.scalars(query).all())

    time_points = {start_at, end_at}
    for r in reservations:
        r_start = r.start_at.replace(tzinfo=timezone.utc) if r.start_at.tzinfo is None else r.start_at
        r_end = r.end_at.replace(tzinfo=timezone.utc) if r.end_at.tzinfo is None else r.end_at
        if start_at < r_start < end_at:
            time_points.add(r_start)
        if start_at < r_end < end_at:
            time_points.add(r_end)

    sorted_points = sorted(list(time_points))
    max_active = 0
    max_confirmed = 0

    for i in range(len(sorted_points) - 1):
        mid_point = sorted_points[i]
        curr_active = 0
        curr_confirmed = 0
        for r in reservations:
            r_start = r.start_at.replace(tzinfo=timezone.utc) if r.start_at.tzinfo is None else r.start_at
            r_end = r.end_at.replace(tzinfo=timezone.utc) if r.end_at.tzinfo is None else r.end_at
            if r_start <= mid_point < r_end:
                curr_active += r.party_size
                if r.status == ReservationStatusEnum.CONFIRMED.value:
                    curr_confirmed += r.party_size
        if curr_active > max_active:
            max_active = curr_active
        if curr_confirmed > max_confirmed:
            max_confirmed = curr_confirmed

    return max_active, max_confirmed


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
    seats: int = Field(..., ge=1, le=20)
    overbook_seats: int = Field(..., ge=0, le=8)
    zone: ZoneEnum
    active: bool = True


class TablePatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Optional[str] = Field(None, pattern=r"^[A-Z][A-Z0-9-]{0,11}$")
    seats: Optional[int] = Field(None, ge=1, le=20)
    overbook_seats: Optional[int] = Field(None, ge=0, le=8)
    zone: Optional[ZoneEnum] = None
    active: Optional[bool] = None

    @model_validator(mode="after")
    def check_non_empty(self) -> "TablePatchPayload":
        if (
            self.code is None
            and self.seats is None
            and self.overbook_seats is None
            and self.zone is None
            and self.active is None
        ):
            raise ValueError("At least one field must be supplied for patch")
        return self


class TableOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    code: str
    seats: int
    overbook_seats: int
    zone: ZoneEnum
    active: bool


class TableListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[TableOut]
    next_after_id: Optional[int] = None


class ReservationCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table_id: int = Field(..., gt=0)
    guest_name: str = Field(..., min_length=2, max_length=120)
    party_size: int = Field(..., ge=1, le=20)
    start_at: datetime
    end_at: datetime


class ReservationPatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guest_name: Optional[str] = Field(None, min_length=2, max_length=120)
    party_size: Optional[int] = Field(None, ge=1, le=20)
    table_id: Optional[int] = Field(None, gt=0)
    start_at: Optional[datetime] = None
    end_at: Optional[datetime] = None

    @model_validator(mode="after")
    def check_non_empty(self) -> "ReservationPatchPayload":
        if (
            self.guest_name is None
            and self.party_size is None
            and self.table_id is None
            and self.start_at is None
            and self.end_at is None
        ):
            raise ValueError("At least one field must be supplied for patch")
        return self


class ReservationConfirmPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed_at: datetime


class ReservationCancelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cancelled_at: datetime
    cancellation_reason: str = Field(..., min_length=3, max_length=500)


class ReservationNoShowPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    no_show_at: datetime


class ReservationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    table_id: int
    user_id: int
    guest_name: str
    party_size: int
    start_at: datetime
    end_at: datetime
    status: ReservationStatusEnum
    created_at: datetime
    confirmed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    cancellation_reason: Optional[str] = None
    cancelled_by_user_id: Optional[int] = None
    no_show_at: Optional[datetime] = None
    no_show_by_user_id: Optional[int] = None


class ReservationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[ReservationOut]
    next_after_id: Optional[int] = None


class TableAvailabilityOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table_id: int
    seats: int
    overbook_seats: int
    standard_remaining: int
    overbook_remaining: int
    can_book: bool


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

        # Seed tables covering zero, limited, and nonzero overbook_seats cases
        t1 = TableModel(
            id=1, code="T01", seats=4, overbook_seats=0, zone=ZoneEnum.INDOOR.value, active=True
        )
        t2 = TableModel(
            id=2, code="T02", seats=4, overbook_seats=2, zone=ZoneEnum.TERRACE.value, active=True
        )
        t3 = TableModel(
            id=3, code="T03", seats=6, overbook_seats=4, zone=ZoneEnum.PRIVATE.value, active=True
        )
        db.add_all([t1, t2, t3])
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
                seats=t.seats,
                overbook_seats=t.overbook_seats,
                zone=ZoneEnum(t.zone),
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
            seats=payload.seats,
            overbook_seats=payload.overbook_seats,
            zone=payload.zone.value,
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
            # Check for PENDING or CONFIRMED reservations
            has_active = db.scalar(
                select(ReservationModel)
                .where(
                    ReservationModel.table_id == table_id,
                    ReservationModel.status.in_([
                        ReservationStatusEnum.PENDING.value,
                        ReservationStatusEnum.CONFIRMED.value,
                    ]),
                )
                .limit(1)
            )
            if has_active:
                raise AppException(
                    409, "Cannot deactivate table with PENDING or CONFIRMED reservations", "Conflict"
                )
            table.active = False
        elif payload.active is True:
            table.active = True

        new_seats = payload.seats if payload.seats is not None else table.seats
        new_overbook = payload.overbook_seats if payload.overbook_seats is not None else table.overbook_seats

        if payload.seats is not None or payload.overbook_seats is not None:
            # Validate that capacity would not fall below active or confirmed demand
            active_res = list(
                db.scalars(
                    select(ReservationModel).where(
                        ReservationModel.table_id == table_id,
                        ReservationModel.status.in_([
                            ReservationStatusEnum.PENDING.value,
                            ReservationStatusEnum.CONFIRMED.value,
                        ]),
                    )
                ).all()
            )
            for r in active_res:
                r_s = r.start_at.replace(tzinfo=timezone.utc) if r.start_at.tzinfo is None else r.start_at
                r_e = r.end_at.replace(tzinfo=timezone.utc) if r.end_at.tzinfo is None else r.end_at
                act_dem, conf_dem = compute_table_demands(db, table_id, r_s, r_e)
                if act_dem > new_seats + new_overbook:
                    raise AppException(
                        409,
                        f"Active demand {act_dem} exceeds new total capacity {new_seats + new_overbook}",
                        "Conflict",
                    )
                if conf_dem > new_seats:
                    raise AppException(
                        409,
                        f"Confirmed demand {conf_dem} exceeds new standard seats {new_seats}",
                        "Conflict",
                    )

            table.seats = new_seats
            table.overbook_seats = new_overbook

        if payload.zone is not None:
            table.zone = payload.zone.value

        db.commit()
        db.refresh(table)
        return table

    @staticmethod
    def delete_table(db: Session, table_id: int):
        table = TableController.get_table(db, table_id)

        # REQ-4-009: table can be soft-deleted only with no reservations
        has_reservation = db.scalar(
            select(ReservationModel).where(ReservationModel.table_id == table_id).limit(1)
        )
        if has_reservation:
            raise AppException(409, "Cannot delete table referenced by reservation history", "Conflict")

        table.is_deleted = True
        db.commit()


class ReservationController:
    @staticmethod
    def list_reservations(
        db: Session,
        current_user: UserModel,
        table_id: Optional[int],
        res_status: Optional[ReservationStatusEnum],
        from_time: Optional[datetime],
        to_time: Optional[datetime],
        limit: int,
        after_id: Optional[int],
    ) -> ReservationListResponse:
        query = select(ReservationModel)

        if table_id is not None:
            if table_id <= 0:
                raise AppException(404, "table_id must be a positive integer", "Not Found")
            TableController.get_table(db, table_id)
            query = query.where(ReservationModel.table_id == table_id)

        if current_user.role != RoleEnum.ADMIN.value:
            query = query.where(ReservationModel.user_id == current_user.id)

        if res_status is not None:
            query = query.where(ReservationModel.status == res_status.value)

        if from_time is not None and to_time is not None:
            f_utc = normalize_utc(from_time)
            t_utc = normalize_utc(to_time)
            if f_utc >= t_utc:
                raise AppException(422, "'from' must be strictly earlier than 'to'", "Unprocessable Entity")
            query = query.where(ReservationModel.start_at < t_utc, ReservationModel.end_at > f_utc)
        elif from_time is not None:
            f_utc = normalize_utc(from_time)
            query = query.where(ReservationModel.end_at > f_utc)
        elif to_time is not None:
            t_utc = normalize_utc(to_time)
            query = query.where(ReservationModel.start_at < t_utc)

        if after_id is not None and after_id > 0:
            query = query.where(ReservationModel.id > after_id)

        query = query.order_by(ReservationModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())

        next_after_id = None
        if len(records) > limit:
            next_after_id = records[limit - 1].id
            records = records[:limit]

        items = [
            ReservationOut(
                id=r.id,
                table_id=r.table_id,
                user_id=r.user_id,
                guest_name=r.guest_name,
                party_size=r.party_size,
                start_at=r.start_at.replace(tzinfo=timezone.utc) if r.start_at.tzinfo is None else r.start_at,
                end_at=r.end_at.replace(tzinfo=timezone.utc) if r.end_at.tzinfo is None else r.end_at,
                status=ReservationStatusEnum(r.status),
                created_at=r.created_at.replace(tzinfo=timezone.utc) if r.created_at.tzinfo is None else r.created_at,
                confirmed_at=r.confirmed_at.replace(tzinfo=timezone.utc) if r.confirmed_at and r.confirmed_at.tzinfo is None else r.confirmed_at,
                cancelled_at=r.cancelled_at.replace(tzinfo=timezone.utc) if r.cancelled_at and r.cancelled_at.tzinfo is None else r.cancelled_at,
                cancellation_reason=r.cancellation_reason,
                cancelled_by_user_id=r.cancelled_by_user_id,
                no_show_at=r.no_show_at.replace(tzinfo=timezone.utc) if r.no_show_at and r.no_show_at.tzinfo is None else r.no_show_at,
                no_show_by_user_id=r.no_show_by_user_id,
            )
            for r in records
        ]
        return ReservationListResponse(items=items, next_after_id=next_after_id)

    @staticmethod
    def create_reservation(
        db: Session, current_user: UserModel, payload: ReservationCreatePayload
    ) -> ReservationModel:
        table = TableController.get_table(db, payload.table_id)
        if not table.active:
            raise AppException(409, f"Table {payload.table_id} is inactive", "Conflict")

        s_utc, e_utc = validate_interval(payload.start_at, payload.end_at)
        trimmed_name = payload.guest_name.strip()
        if len(trimmed_name) < 2 or len(trimmed_name) > 120:
            raise AppException(422, "guest_name must be 2..120 trimmed characters", "Unprocessable Entity")

        total_capacity = table.seats + table.overbook_seats
        if payload.party_size > total_capacity:
            raise AppException(
                409, f"party_size {payload.party_size} exceeds total table capacity {total_capacity}", "Conflict"
            )

        # REQ-4-004: Active booking demand (PENDING + CONFIRMED) must not exceed table.seats + table.overbook_seats
        act_dem, _ = compute_table_demands(db, table.id, s_utc, e_utc)
        if act_dem + payload.party_size > total_capacity:
            raise AppException(
                409,
                f"Active booking demand would be {act_dem + payload.party_size} > total capacity {total_capacity}",
                "Conflict",
            )

        now = datetime.now(timezone.utc)
        res = ReservationModel(
            table_id=table.id,
            user_id=current_user.id,
            guest_name=trimmed_name,
            party_size=payload.party_size,
            start_at=s_utc,
            end_at=e_utc,
            status=ReservationStatusEnum.PENDING.value,
            created_at=now,
        )
        db.add(res)
        db.commit()
        db.refresh(res)
        return res

    @staticmethod
    def get_reservation(
        db: Session, current_user: UserModel, reservation_id: int
    ) -> ReservationModel:
        if reservation_id <= 0:
            raise AppException(404, "reservation_id must be a positive integer", "Not Found")

        res = db.scalar(select(ReservationModel).where(ReservationModel.id == reservation_id))
        if not res:
            raise AppException(404, f"Reservation {reservation_id} not found", "Not Found")

        if current_user.role != RoleEnum.ADMIN.value and res.user_id != current_user.id:
            raise AppException(403, "Access denied to reservation of another user", "Forbidden")

        return res

    @staticmethod
    def patch_reservation(
        db: Session, current_user: UserModel, reservation_id: int, payload: ReservationPatchPayload
    ) -> ReservationModel:
        res = ReservationController.get_reservation(db, current_user, reservation_id)

        # REQ-4-020: permit PENDING only
        if res.status != ReservationStatusEnum.PENDING.value:
            raise AppException(409, "Only PENDING reservations can be edited", "Conflict")

        target_table_id = payload.table_id if payload.table_id is not None else res.table_id
        target_party_size = payload.party_size if payload.party_size is not None else res.party_size
        table = TableController.get_table(db, target_table_id)
        if not table.active:
            raise AppException(409, f"Target table {target_table_id} is inactive", "Conflict")

        s_input = payload.start_at if payload.start_at is not None else res.start_at.replace(tzinfo=timezone.utc)
        e_input = payload.end_at if payload.end_at is not None else res.end_at.replace(tzinfo=timezone.utc)
        s_utc, e_utc = validate_interval(s_input, e_input)

        total_capacity = table.seats + table.overbook_seats
        if target_party_size > total_capacity:
            raise AppException(
                409, f"party_size {target_party_size} exceeds total capacity {total_capacity}", "Conflict"
            )

        act_dem, _ = compute_table_demands(
            db, target_table_id, s_utc, e_utc, exclude_reservation_id=reservation_id
        )
        if act_dem + target_party_size > total_capacity:
            raise AppException(
                409,
                f"Active demand would be {act_dem + target_party_size} > total capacity {total_capacity}",
                "Conflict",
            )

        if payload.guest_name is not None:
            trimmed = payload.guest_name.strip()
            if len(trimmed) < 2 or len(trimmed) > 120:
                raise AppException(422, "guest_name must be 2..120 trimmed characters", "Unprocessable Entity")
            res.guest_name = trimmed

        res.table_id = target_table_id
        res.party_size = target_party_size
        res.start_at = s_utc
        res.end_at = e_utc

        db.commit()
        db.refresh(res)
        return res

    @staticmethod
    def confirm_reservation(
        db: Session, current_user: UserModel, reservation_id: int, payload: ReservationConfirmPayload
    ) -> ReservationModel:
        res = ReservationController.get_reservation(db, current_user, reservation_id)

        if res.status != ReservationStatusEnum.PENDING.value:
            raise AppException(409, "Only PENDING reservations can be confirmed", "Conflict")

        table = TableController.get_table(db, res.table_id)
        s_utc = res.start_at.replace(tzinfo=timezone.utc) if res.start_at.tzinfo is None else res.start_at
        e_utc = res.end_at.replace(tzinfo=timezone.utc) if res.end_at.tzinfo is None else res.end_at

        # REQ-4-005: Confirmed demand must not exceed table.seats
        _, conf_dem = compute_table_demands(
            db, table.id, s_utc, e_utc, exclude_reservation_id=reservation_id
        )
        if conf_dem + res.party_size > table.seats:
            raise AppException(
                409,
                f"Confirmed demand would be {conf_dem + res.party_size} > standard seats {table.seats}",
                "Conflict",
            )

        c_utc = normalize_utc(payload.confirmed_at)
        res.status = ReservationStatusEnum.CONFIRMED.value
        res.confirmed_at = c_utc

        db.commit()
        db.refresh(res)
        return res

    @staticmethod
    def cancel_reservation(
        db: Session, current_user: UserModel, reservation_id: int, payload: ReservationCancelPayload
    ) -> ReservationModel:
        res = ReservationController.get_reservation(db, current_user, reservation_id)

        if res.status not in (ReservationStatusEnum.PENDING.value, ReservationStatusEnum.CONFIRMED.value):
            raise AppException(409, f"Cannot cancel reservation in terminal status '{res.status}'", "Conflict")

        c_utc = normalize_utc(payload.cancelled_at)
        trimmed_reason = payload.cancellation_reason.strip()
        if len(trimmed_reason) < 3 or len(trimmed_reason) > 500:
            raise AppException(422, "cancellation_reason must be 3..500 trimmed characters", "Unprocessable Entity")

        res.status = ReservationStatusEnum.CANCELLED.value
        res.cancelled_at = c_utc
        res.cancellation_reason = trimmed_reason
        res.cancelled_by_user_id = current_user.id

        db.commit()
        db.refresh(res)
        return res

    @staticmethod
    def no_show_reservation(
        db: Session, current_user: UserModel, reservation_id: int, payload: ReservationNoShowPayload
    ) -> ReservationModel:
        res = ReservationController.get_reservation(db, current_user, reservation_id)

        if res.status not in (ReservationStatusEnum.PENDING.value, ReservationStatusEnum.CONFIRMED.value):
            raise AppException(409, f"Cannot mark no-show for reservation in terminal status '{res.status}'", "Conflict")

        ns_utc = normalize_utc(payload.no_show_at)
        res.status = ReservationStatusEnum.NO_SHOW.value
        res.no_show_at = ns_utc
        res.no_show_by_user_id = current_user.id

        db.commit()
        db.refresh(res)
        return res


class AvailabilityController:
    @staticmethod
    def get_availability(
        db: Session, table_id: int, start_at: datetime, end_at: datetime, party_size: int
    ) -> TableAvailabilityOut:
        table = TableController.get_table(db, table_id)
        if not table.active:
            raise AppException(409, f"Table {table_id} is inactive", "Conflict")

        s_utc, e_utc = validate_interval(start_at, end_at)
        act_dem, _ = compute_table_demands(db, table.id, s_utc, e_utc)

        total_capacity = table.seats + table.overbook_seats
        standard_remaining = max(0, table.seats - act_dem)
        overbook_remaining = max(0, total_capacity - act_dem)
        can_book = overbook_remaining >= party_size

        return TableAvailabilityOut(
            table_id=table.id,
            seats=table.seats,
            overbook_seats=table.overbook_seats,
            standard_remaining=standard_remaining,
            overbook_remaining=overbook_remaining,
            can_book=can_book,
        )

    @staticmethod
    def get_conflicts(
        db: Session,
        table_id: int,
        start_at: datetime,
        end_at: datetime,
        exclude_reservation_id: Optional[int],
        limit: int,
        after_id: Optional[int],
    ) -> ReservationListResponse:
        TableController.get_table(db, table_id)
        s_utc, e_utc = validate_interval(start_at, end_at)

        query = select(ReservationModel).where(
            ReservationModel.table_id == table_id,
            ReservationModel.status.in_([
                ReservationStatusEnum.PENDING.value,
                ReservationStatusEnum.CONFIRMED.value,
            ]),
            ReservationModel.start_at < e_utc,
            ReservationModel.end_at > s_utc,
        )

        if exclude_reservation_id is not None:
            if exclude_reservation_id <= 0:
                raise AppException(404, "exclude_reservation_id must be a positive integer", "Not Found")
            query = query.where(ReservationModel.id != exclude_reservation_id)

        if after_id is not None and after_id > 0:
            query = query.where(ReservationModel.id > after_id)

        query = query.order_by(ReservationModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())

        next_after_id = None
        if len(records) > limit:
            next_after_id = records[limit - 1].id
            records = records[:limit]

        items = [
            ReservationOut(
                id=r.id,
                table_id=r.table_id,
                user_id=r.user_id,
                guest_name=r.guest_name,
                party_size=r.party_size,
                start_at=r.start_at.replace(tzinfo=timezone.utc) if r.start_at.tzinfo is None else r.start_at,
                end_at=r.end_at.replace(tzinfo=timezone.utc) if r.end_at.tzinfo is None else r.end_at,
                status=ReservationStatusEnum(r.status),
                created_at=r.created_at.replace(tzinfo=timezone.utc) if r.created_at.tzinfo is None else r.created_at,
                confirmed_at=r.confirmed_at.replace(tzinfo=timezone.utc) if r.confirmed_at and r.confirmed_at.tzinfo is None else r.confirmed_at,
                cancelled_at=r.cancelled_at.replace(tzinfo=timezone.utc) if r.cancelled_at and r.cancelled_at.tzinfo is None else r.cancelled_at,
                cancellation_reason=r.cancellation_reason,
                cancelled_by_user_id=r.cancelled_by_user_id,
                no_show_at=r.no_show_at.replace(tzinfo=timezone.utc) if r.no_show_at and r.no_show_at.tzinfo is None else r.no_show_at,
                no_show_by_user_id=r.no_show_by_user_id,
            )
            for r in records
        ]
        return ReservationListResponse(items=items, next_after_id=next_after_id)


# -----------------------------------------------------------------------------
# FastAPI App Initialization & Routes
# -----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    with SessionLocal() as db:
        seed_database(db)
    yield


app = FastAPI(
    title="Restaurant Management - Overbooking and Concurrent Reservations API",
    version="1.0.0",
    description="RESTful API for table overbooking limits, concurrent reservation locking, confirmations, and no-shows.",
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
    after_id: Optional[int] = Query(None, gt=0, description="Pagination cursor"),
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
        seats=table.seats,
        overbook_seats=table.overbook_seats,
        zone=ZoneEnum(table.zone),
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
        seats=table.seats,
        overbook_seats=table.overbook_seats,
        zone=ZoneEnum(table.zone),
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
        seats=table.seats,
        overbook_seats=table.overbook_seats,
        zone=ZoneEnum(table.zone),
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
    "/reservations",
    response_model=ReservationListResponse,
    status_code=status.HTTP_200_OK,
    summary="List reservations",
    tags=["Reservations"],
)
def list_reservations(
    table_id: Optional[int] = Query(None, description="Optional table filter"),
    status: Optional[ReservationStatusEnum] = Query(None, description="Filter by status"),
    from_time: Optional[datetime] = Query(None, alias="from", description="Filter overlapping start"),
    to_time: Optional[datetime] = Query(None, alias="to", description="Filter overlapping end"),
    limit: int = Query(20, ge=1, le=100, description="Pagination limit"),
    after_id: Optional[int] = Query(None, gt=0, description="Pagination cursor"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ReservationController.list_reservations(
        db, current_user, table_id, status, from_time, to_time, limit, after_id
    )


@app.post(
    "/reservations",
    response_model=ReservationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create reservation (PENDING)",
    tags=["Reservations"],
)
def create_reservation(
    payload: ReservationCreatePayload,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    res = ReservationController.create_reservation(db, current_user, payload)
    response.headers["Location"] = f"/reservations/{res.id}"
    return ReservationOut(
        id=res.id,
        table_id=res.table_id,
        user_id=res.user_id,
        guest_name=res.guest_name,
        party_size=res.party_size,
        start_at=res.start_at.replace(tzinfo=timezone.utc) if res.start_at.tzinfo is None else res.start_at,
        end_at=res.end_at.replace(tzinfo=timezone.utc) if res.end_at.tzinfo is None else res.end_at,
        status=ReservationStatusEnum(res.status),
        created_at=res.created_at.replace(tzinfo=timezone.utc) if res.created_at.tzinfo is None else res.created_at,
        confirmed_at=res.confirmed_at.replace(tzinfo=timezone.utc) if res.confirmed_at and res.confirmed_at.tzinfo is None else res.confirmed_at,
        cancelled_at=res.cancelled_at.replace(tzinfo=timezone.utc) if res.cancelled_at and res.cancelled_at.tzinfo is None else res.cancelled_at,
        cancellation_reason=res.cancellation_reason,
        cancelled_by_user_id=res.cancelled_by_user_id,
        no_show_at=res.no_show_at.replace(tzinfo=timezone.utc) if res.no_show_at and res.no_show_at.tzinfo is None else res.no_show_at,
        no_show_by_user_id=res.no_show_by_user_id,
    )


@app.get(
    "/reservations/{reservation_id}",
    response_model=ReservationOut,
    status_code=status.HTTP_200_OK,
    summary="Get reservation by id",
    tags=["Reservations"],
)
def get_reservation(
    reservation_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    res = ReservationController.get_reservation(db, current_user, reservation_id)
    return ReservationOut(
        id=res.id,
        table_id=res.table_id,
        user_id=res.user_id,
        guest_name=res.guest_name,
        party_size=res.party_size,
        start_at=res.start_at.replace(tzinfo=timezone.utc) if res.start_at.tzinfo is None else res.start_at,
        end_at=res.end_at.replace(tzinfo=timezone.utc) if res.end_at.tzinfo is None else res.end_at,
        status=ReservationStatusEnum(res.status),
        created_at=res.created_at.replace(tzinfo=timezone.utc) if res.created_at.tzinfo is None else res.created_at,
        confirmed_at=res.confirmed_at.replace(tzinfo=timezone.utc) if res.confirmed_at and res.confirmed_at.tzinfo is None else res.confirmed_at,
        cancelled_at=res.cancelled_at.replace(tzinfo=timezone.utc) if res.cancelled_at and res.cancelled_at.tzinfo is None else res.cancelled_at,
        cancellation_reason=res.cancellation_reason,
        cancelled_by_user_id=res.cancelled_by_user_id,
        no_show_at=res.no_show_at.replace(tzinfo=timezone.utc) if res.no_show_at and res.no_show_at.tzinfo is None else res.no_show_at,
        no_show_by_user_id=res.no_show_by_user_id,
    )


@app.patch(
    "/reservations/{reservation_id}",
    response_model=ReservationOut,
    status_code=status.HTTP_200_OK,
    summary="Patch reservation while PENDING",
    tags=["Reservations"],
)
def patch_reservation(
    reservation_id: int,
    payload: ReservationPatchPayload,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    res = ReservationController.patch_reservation(db, current_user, reservation_id, payload)
    return ReservationOut(
        id=res.id,
        table_id=res.table_id,
        user_id=res.user_id,
        guest_name=res.guest_name,
        party_size=res.party_size,
        start_at=res.start_at.replace(tzinfo=timezone.utc) if res.start_at.tzinfo is None else res.start_at,
        end_at=res.end_at.replace(tzinfo=timezone.utc) if res.end_at.tzinfo is None else res.end_at,
        status=ReservationStatusEnum(res.status),
        created_at=res.created_at.replace(tzinfo=timezone.utc) if res.created_at.tzinfo is None else res.created_at,
        confirmed_at=res.confirmed_at.replace(tzinfo=timezone.utc) if res.confirmed_at and res.confirmed_at.tzinfo is None else res.confirmed_at,
        cancelled_at=res.cancelled_at.replace(tzinfo=timezone.utc) if res.cancelled_at and res.cancelled_at.tzinfo is None else res.cancelled_at,
        cancellation_reason=res.cancellation_reason,
        cancelled_by_user_id=res.cancelled_by_user_id,
        no_show_at=res.no_show_at.replace(tzinfo=timezone.utc) if res.no_show_at and res.no_show_at.tzinfo is None else res.no_show_at,
        no_show_by_user_id=res.no_show_by_user_id,
    )


@app.post(
    "/reservations/{reservation_id}/confirm",
    response_model=ReservationOut,
    status_code=status.HTTP_200_OK,
    summary="Confirm PENDING reservation",
    tags=["Reservations"],
)
def confirm_reservation(
    reservation_id: int,
    payload: ReservationConfirmPayload,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    res = ReservationController.confirm_reservation(db, current_user, reservation_id, payload)
    return ReservationOut(
        id=res.id,
        table_id=res.table_id,
        user_id=res.user_id,
        guest_name=res.guest_name,
        party_size=res.party_size,
        start_at=res.start_at.replace(tzinfo=timezone.utc) if res.start_at.tzinfo is None else res.start_at,
        end_at=res.end_at.replace(tzinfo=timezone.utc) if res.end_at.tzinfo is None else res.end_at,
        status=ReservationStatusEnum(res.status),
        created_at=res.created_at.replace(tzinfo=timezone.utc) if res.created_at.tzinfo is None else res.created_at,
        confirmed_at=res.confirmed_at.replace(tzinfo=timezone.utc) if res.confirmed_at and res.confirmed_at.tzinfo is None else res.confirmed_at,
        cancelled_at=res.cancelled_at.replace(tzinfo=timezone.utc) if res.cancelled_at and res.cancelled_at.tzinfo is None else res.cancelled_at,
        cancellation_reason=res.cancellation_reason,
        cancelled_by_user_id=res.cancelled_by_user_id,
        no_show_at=res.no_show_at.replace(tzinfo=timezone.utc) if res.no_show_at and res.no_show_at.tzinfo is None else res.no_show_at,
        no_show_by_user_id=res.no_show_by_user_id,
    )


@app.post(
    "/reservations/{reservation_id}/cancel",
    response_model=ReservationOut,
    status_code=status.HTTP_200_OK,
    summary="Cancel reservation",
    tags=["Reservations"],
)
def cancel_reservation(
    reservation_id: int,
    payload: ReservationCancelPayload,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    res = ReservationController.cancel_reservation(db, current_user, reservation_id, payload)
    return ReservationOut(
        id=res.id,
        table_id=res.table_id,
        user_id=res.user_id,
        guest_name=res.guest_name,
        party_size=res.party_size,
        start_at=res.start_at.replace(tzinfo=timezone.utc) if res.start_at.tzinfo is None else res.start_at,
        end_at=res.end_at.replace(tzinfo=timezone.utc) if res.end_at.tzinfo is None else res.end_at,
        status=ReservationStatusEnum(res.status),
        created_at=res.created_at.replace(tzinfo=timezone.utc) if res.created_at.tzinfo is None else res.created_at,
        confirmed_at=res.confirmed_at.replace(tzinfo=timezone.utc) if res.confirmed_at and res.confirmed_at.tzinfo is None else res.confirmed_at,
        cancelled_at=res.cancelled_at.replace(tzinfo=timezone.utc) if res.cancelled_at and res.cancelled_at.tzinfo is None else res.cancelled_at,
        cancellation_reason=res.cancellation_reason,
        cancelled_by_user_id=res.cancelled_by_user_id,
        no_show_at=res.no_show_at.replace(tzinfo=timezone.utc) if res.no_show_at and res.no_show_at.tzinfo is None else res.no_show_at,
        no_show_by_user_id=res.no_show_by_user_id,
    )


@app.post(
    "/reservations/{reservation_id}/no-show",
    response_model=ReservationOut,
    status_code=status.HTTP_200_OK,
    summary="Mark reservation NO_SHOW",
    tags=["Reservations"],
)
def no_show_reservation(
    reservation_id: int,
    payload: ReservationNoShowPayload,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    res = ReservationController.no_show_reservation(db, current_user, reservation_id, payload)
    return ReservationOut(
        id=res.id,
        table_id=res.table_id,
        user_id=res.user_id,
        guest_name=res.guest_name,
        party_size=res.party_size,
        start_at=res.start_at.replace(tzinfo=timezone.utc) if res.start_at.tzinfo is None else res.start_at,
        end_at=res.end_at.replace(tzinfo=timezone.utc) if res.end_at.tzinfo is None else res.end_at,
        status=ReservationStatusEnum(res.status),
        created_at=res.created_at.replace(tzinfo=timezone.utc) if res.created_at.tzinfo is None else res.created_at,
        confirmed_at=res.confirmed_at.replace(tzinfo=timezone.utc) if res.confirmed_at and res.confirmed_at.tzinfo is None else res.confirmed_at,
        cancelled_at=res.cancelled_at.replace(tzinfo=timezone.utc) if res.cancelled_at and res.cancelled_at.tzinfo is None else res.cancelled_at,
        cancellation_reason=res.cancellation_reason,
        cancelled_by_user_id=res.cancelled_by_user_id,
        no_show_at=res.no_show_at.replace(tzinfo=timezone.utc) if res.no_show_at and res.no_show_at.tzinfo is None else res.no_show_at,
        no_show_by_user_id=res.no_show_by_user_id,
    )


@app.get(
    "/availability",
    response_model=TableAvailabilityOut,
    status_code=status.HTTP_200_OK,
    summary="Calculate table standard and overbook remaining capacity",
    tags=["Availability"],
)
def get_availability(
    table_id: int = Query(..., gt=0, description="Positive table id"),
    start_at: datetime = Query(..., description="Start UTC timestamp on 5-minute boundary"),
    end_at: datetime = Query(..., description="End UTC timestamp on 5-minute boundary"),
    party_size: int = Query(..., ge=1, le=20, description="Party size"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return AvailabilityController.get_availability(db, table_id, start_at, end_at, party_size)


@app.get(
    "/tables/{table_id}/reservation-conflicts",
    response_model=ReservationListResponse,
    status_code=status.HTTP_200_OK,
    summary="List reservation conflicts for table and interval (admin only)",
    tags=["Tables"],
)
def get_reservation_conflicts(
    table_id: int,
    start_at: datetime = Query(..., description="Start UTC timestamp on 5-minute boundary"),
    end_at: datetime = Query(..., description="End UTC timestamp on 5-minute boundary"),
    exclude_reservation_id: Optional[int] = Query(None, gt=0, description="Optional reservation id to exclude"),
    limit: int = Query(20, ge=1, le=100, description="Pagination limit"),
    after_id: Optional[int] = Query(None, gt=0, description="Pagination cursor"),
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return AvailabilityController.get_conflicts(
        db, table_id, start_at, end_at, exclude_reservation_id, limit, after_id
    )
