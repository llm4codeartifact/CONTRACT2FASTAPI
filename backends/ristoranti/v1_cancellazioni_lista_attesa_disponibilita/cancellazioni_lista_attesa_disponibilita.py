"""
Cancellazioni, Lista d'Attesa e Disponibilita - Restaurant Management API
Case ID: 4
Variant: C: Cancellations, waitlist, and availability

Technical specifications:
- Python 3.10+, FastAPI, Pydantic v2, SQLAlchemy ORM, SQLite in-memory with StaticPool.
- Pure ASGI middleware for 10 MiB limit (413), JSON media type (415), invalid JSON (400), preserving 405 Method Not Allowed.
- MVC architecture: SQLAlchemy models, Pydantic schemas, static controllers, thin FastAPI routes.
- Seed data: user1 (id=1, user), user2 (id=2, user), admin (id=3, admin), tables, initial reservations/waitlist entries.
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
logger = logging.getLogger("cancellazioni_lista_attesa")

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
    BOOKED = "BOOKED"
    CANCELLED = "CANCELLED"


class WaitlistStatusEnum(str, Enum):
    WAITING = "WAITING"
    OFFERED = "OFFERED"
    ACCEPTED = "ACCEPTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


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
    zone = Column(String(20), nullable=False, index=True)
    active = Column(Boolean, default=True, nullable=False)
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)

    reservations = relationship("ReservationModel", back_populates="table")
    offered_waitlist_entries = relationship("WaitlistModel", back_populates="held_table")


class ReservationModel(Base):
    __tablename__ = "reservations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    table_id = Column(Integer, ForeignKey("tables.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    guest_name = Column(String(120), nullable=False)
    party_size = Column(Integer, nullable=False)
    start_at = Column(DateTime(timezone=True), nullable=False, index=True)
    end_at = Column(DateTime(timezone=True), nullable=False, index=True)
    status = Column(String(20), nullable=False, default=ReservationStatusEnum.BOOKED.value, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    cancellation_reason = Column(String(500), nullable=True)
    cancelled_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    table = relationship("TableModel", back_populates="reservations")
    user = relationship("UserModel", foreign_keys=[user_id])
    cancelled_by_user = relationship("UserModel", foreign_keys=[cancelled_by_user_id])


class WaitlistModel(Base):
    __tablename__ = "waitlist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    guest_name = Column(String(120), nullable=False)
    party_size = Column(Integer, nullable=False)
    start_at = Column(DateTime(timezone=True), nullable=False, index=True)
    end_at = Column(DateTime(timezone=True), nullable=False, index=True)
    status = Column(String(20), nullable=False, default=WaitlistStatusEnum.WAITING.value, index=True)
    held_table_id = Column(Integer, ForeignKey("tables.id"), nullable=True, index=True)
    offer_expires_at = Column(DateTime(timezone=True), nullable=True)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    user = relationship("UserModel")
    held_table = relationship("TableModel", back_populates="offered_waitlist_entries")


# -----------------------------------------------------------------------------
# Capacity Helper
# -----------------------------------------------------------------------------
def compute_table_max_demand(
    db: Session,
    table_id: int,
    start_at: datetime,
    end_at: datetime,
    exclude_reservation_id: Optional[int] = None,
    exclude_waitlist_id: Optional[int] = None,
) -> int:
    # Query BOOKED reservations overlapping [start_at, end_at)
    res_query = select(ReservationModel).where(
        ReservationModel.table_id == table_id,
        ReservationModel.status == ReservationStatusEnum.BOOKED.value,
        ReservationModel.start_at < end_at,
        ReservationModel.end_at > start_at,
    )
    if exclude_reservation_id:
        res_query = res_query.where(ReservationModel.id != exclude_reservation_id)
    booked_reservations = list(db.scalars(res_query).all())

    # Query OFFERED waitlist entries holding capacity on this table overlapping [start_at, end_at)
    wait_query = select(WaitlistModel).where(
        WaitlistModel.held_table_id == table_id,
        WaitlistModel.status == WaitlistStatusEnum.OFFERED.value,
        WaitlistModel.start_at < end_at,
        WaitlistModel.end_at > start_at,
    )
    if exclude_waitlist_id:
        wait_query = wait_query.where(WaitlistModel.id != exclude_waitlist_id)
    offered_entries = list(db.scalars(wait_query).all())

    # Collect time points to check overlap profile
    time_points = {start_at, end_at}
    for r in booked_reservations:
        r_start = r.start_at.replace(tzinfo=timezone.utc) if r.start_at.tzinfo is None else r.start_at
        r_end = r.end_at.replace(tzinfo=timezone.utc) if r.end_at.tzinfo is None else r.end_at
        if start_at < r_start < end_at:
            time_points.add(r_start)
        if start_at < r_end < end_at:
            time_points.add(r_end)
    for o in offered_entries:
        o_start = o.start_at.replace(tzinfo=timezone.utc) if o.start_at.tzinfo is None else o.start_at
        o_end = o.end_at.replace(tzinfo=timezone.utc) if o.end_at.tzinfo is None else o.end_at
        if start_at < o_start < end_at:
            time_points.add(o_start)
        if start_at < o_end < end_at:
            time_points.add(o_end)

    sorted_points = sorted(list(time_points))
    max_demand = 0

    # Evaluate demand in each sub-interval [p_i, p_{i+1})
    for i in range(len(sorted_points) - 1):
        mid_point = sorted_points[i]
        current_demand = 0
        for r in booked_reservations:
            r_start = r.start_at.replace(tzinfo=timezone.utc) if r.start_at.tzinfo is None else r.start_at
            r_end = r.end_at.replace(tzinfo=timezone.utc) if r.end_at.tzinfo is None else r.end_at
            if r_start <= mid_point < r_end:
                current_demand += r.party_size
        for o in offered_entries:
            o_start = o.start_at.replace(tzinfo=timezone.utc) if o.start_at.tzinfo is None else o.start_at
            o_end = o.end_at.replace(tzinfo=timezone.utc) if o.end_at.tzinfo is None else o.end_at
            if o_start <= mid_point < o_end:
                current_demand += o.party_size
        if current_demand > max_demand:
            max_demand = current_demand

    return max_demand


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
    zone: ZoneEnum
    active: bool = True


class TablePatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Optional[str] = Field(None, pattern=r"^[A-Z][A-Z0-9-]{0,11}$")
    seats: Optional[int] = Field(None, ge=1, le=20)
    zone: Optional[ZoneEnum] = None
    active: Optional[bool] = None

    @model_validator(mode="after")
    def check_non_empty(self) -> "TablePatchPayload":
        if self.code is None and self.seats is None and self.zone is None and self.active is None:
            raise ValueError("At least one field must be supplied for patch")
        return self


class TableOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    code: str
    seats: int
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


class ReservationCancelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cancelled_at: datetime
    cancellation_reason: str = Field(..., min_length=3, max_length=500)
    offer_expires_at: datetime


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
    cancelled_at: Optional[datetime] = None
    cancellation_reason: Optional[str] = None
    cancelled_by_user_id: Optional[int] = None


class ReservationListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[ReservationOut]
    next_after_id: Optional[int] = None


class WaitlistCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guest_name: str = Field(..., min_length=2, max_length=120)
    party_size: int = Field(..., ge=1, le=20)
    start_at: datetime
    end_at: datetime


class WaitlistAcceptPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accepted_at: datetime


class WaitlistExpirePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expired_at: datetime


class WaitlistOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    user_id: int
    guest_name: str
    party_size: int
    start_at: datetime
    end_at: datetime
    status: WaitlistStatusEnum
    held_table_id: Optional[int] = None
    offer_expires_at: Optional[datetime] = None
    accepted_at: Optional[datetime] = None
    created_at: datetime


class WaitlistListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[WaitlistOut]
    next_after_id: Optional[int] = None


class TableAvailabilityOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    table_id: int
    code: str
    seats: int
    zone: ZoneEnum
    available_seats: int


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

        # Seed Tables
        t1 = TableModel(id=1, code="T01", seats=4, zone=ZoneEnum.INDOOR.value, active=True)
        t2 = TableModel(id=2, code="T02", seats=2, zone=ZoneEnum.TERRACE.value, active=True)
        t3 = TableModel(id=3, code="T03", seats=6, zone=ZoneEnum.PRIVATE.value, active=True)
        db.add_all([t1, t2, t3])
        db.flush()

        # Seed one BOOKED reservation
        res1 = ReservationModel(
            id=1,
            table_id=1,
            user_id=1,
            guest_name="Mario Rossi",
            party_size=2,
            start_at=datetime(2026, 9, 10, 19, 0, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 10, 21, 0, 0, tzinfo=timezone.utc),
            status=ReservationStatusEnum.BOOKED.value,
            created_at=datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        db.add(res1)
        db.flush()

        # Seed one WAITING entry
        w1 = WaitlistModel(
            id=1,
            user_id=2,
            guest_name="Luigi Verdi",
            party_size=2,
            start_at=datetime(2026, 9, 10, 19, 30, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 10, 21, 0, 0, tzinfo=timezone.utc),
            status=WaitlistStatusEnum.WAITING.value,
            created_at=datetime(2026, 9, 2, 11, 0, 0, tzinfo=timezone.utc),
        )
        # Seed one OFFERED entry with explicit future expiry timestamp
        w2 = WaitlistModel(
            id=2,
            user_id=1,
            guest_name="Anna Neri",
            party_size=2,
            start_at=datetime(2026, 9, 10, 20, 0, 0, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 10, 21, 30, 0, tzinfo=timezone.utc),
            status=WaitlistStatusEnum.OFFERED.value,
            held_table_id=2,
            offer_expires_at=datetime(2026, 9, 10, 18, 0, 0, tzinfo=timezone.utc),
            created_at=datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc),
        )
        db.add_all([w1, w2])
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

        if path == "/health":
            await self.app(scope, receive, send)
            return

        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        # Check router route matches for 405 Method Not Allowed handling
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

        # Duplicate code check
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

        # Deactivation check with active holds
        if payload.active is False and table.active is True:
            has_booked = db.scalar(
                select(ReservationModel)
                .where(
                    ReservationModel.table_id == table_id,
                    ReservationModel.status == ReservationStatusEnum.BOOKED.value,
                )
                .limit(1)
            )
            has_offered = db.scalar(
                select(WaitlistModel)
                .where(
                    WaitlistModel.held_table_id == table_id,
                    WaitlistModel.status == WaitlistStatusEnum.OFFERED.value,
                )
                .limit(1)
            )
            if has_booked or has_offered:
                raise AppException(
                    409,
                    "Cannot deactivate table with active reservations or waitlist holds",
                    "Conflict",
                )
            table.active = False
        elif payload.active is True:
            table.active = True

        # Seats change check against active demand
        if payload.seats is not None and payload.seats != table.seats:
            # Check all overlapping active intervals for this table
            booked_res = list(
                db.scalars(
                    select(ReservationModel).where(
                        ReservationModel.table_id == table_id,
                        ReservationModel.status == ReservationStatusEnum.BOOKED.value,
                    )
                ).all()
            )
            offered_w = list(
                db.scalars(
                    select(WaitlistModel).where(
                        WaitlistModel.held_table_id == table_id,
                        WaitlistModel.status == WaitlistStatusEnum.OFFERED.value,
                    )
                ).all()
            )
            for r in booked_res:
                r_s = r.start_at.replace(tzinfo=timezone.utc) if r.start_at.tzinfo is None else r.start_at
                r_e = r.end_at.replace(tzinfo=timezone.utc) if r.end_at.tzinfo is None else r.end_at
                demand = compute_table_max_demand(db, table_id, r_s, r_e)
                if demand > payload.seats:
                    raise AppException(
                        409,
                        f"Cannot reduce table seats to {payload.seats}: active demand is {demand}",
                        "Conflict",
                    )
            for o in offered_w:
                o_s = o.start_at.replace(tzinfo=timezone.utc) if o.start_at.tzinfo is None else o.start_at
                o_e = o.end_at.replace(tzinfo=timezone.utc) if o.end_at.tzinfo is None else o.end_at
                demand = compute_table_max_demand(db, table_id, o_s, o_e)
                if demand > payload.seats:
                    raise AppException(
                        409,
                        f"Cannot reduce table seats to {payload.seats}: active demand is {demand}",
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

        # REQ-4-008 & REQ-4-016: A table cannot be deleted while any reservation references it
        has_reservation = db.scalar(
            select(ReservationModel).where(ReservationModel.table_id == table_id).limit(1)
        )
        if has_reservation:
            raise AppException(
                409, "Cannot delete table referenced by reservations", "Conflict"
            )

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
            # Overlap with [from, to)
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
                cancelled_at=r.cancelled_at.replace(tzinfo=timezone.utc) if r.cancelled_at and r.cancelled_at.tzinfo is None else r.cancelled_at,
                cancellation_reason=r.cancellation_reason,
                cancelled_by_user_id=r.cancelled_by_user_id,
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

        if payload.party_size > table.seats:
            raise AppException(409, f"Party size {payload.party_size} exceeds table capacity {table.seats}", "Conflict")

        current_max_demand = compute_table_max_demand(db, table.id, s_utc, e_utc)
        if current_max_demand + payload.party_size > table.seats:
            raise AppException(
                409,
                f"Insufficient capacity on table {table.id}: demand would be {current_max_demand + payload.party_size} > {table.seats}",
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
            status=ReservationStatusEnum.BOOKED.value,
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
    def cancel_reservation(
        db: Session, current_user: UserModel, reservation_id: int, payload: ReservationCancelPayload
    ) -> Tuple[ReservationModel, Optional[WaitlistModel]]:
        res = ReservationController.get_reservation(db, current_user, reservation_id)

        if res.status != ReservationStatusEnum.BOOKED.value:
            raise AppException(409, "Only BOOKED reservations can be cancelled", "Conflict")

        c_utc = normalize_utc(payload.cancelled_at)
        exp_utc = normalize_utc(payload.offer_expires_at)
        if exp_utc <= c_utc:
            raise AppException(422, "offer_expires_at must be strictly later than cancelled_at", "Unprocessable Entity")

        trimmed_reason = payload.cancellation_reason.strip()
        if len(trimmed_reason) < 3 or len(trimmed_reason) > 500:
            raise AppException(422, "cancellation_reason must be 3..500 trimmed characters", "Unprocessable Entity")

        # Cancel reservation
        res.status = ReservationStatusEnum.CANCELLED.value
        res.cancelled_at = c_utc
        res.cancellation_reason = trimmed_reason
        res.cancelled_by_user_id = current_user.id
        db.flush()

        table = TableController.get_table(db, res.table_id)

        # REQ-4-006: Offer the cancelled table capacity to the lowest-id matching WAITING entry that fits
        # Find WAITING entries ordered by id asc
        waiting_entries = list(
            db.scalars(
                select(WaitlistModel)
                .where(WaitlistModel.status == WaitlistStatusEnum.WAITING.value)
                .order_by(WaitlistModel.id.asc())
            ).all()
        )

        offered_entry = None
        for w in waiting_entries:
            w_s = w.start_at.replace(tzinfo=timezone.utc) if w.start_at.tzinfo is None else w.start_at
            w_e = w.end_at.replace(tzinfo=timezone.utc) if w.end_at.tzinfo is None else w.end_at

            # Must fit the table seats
            if w.party_size > table.seats:
                continue

            # Must overlap the cancelled reservation interval
            res_s = res.start_at.replace(tzinfo=timezone.utc) if res.start_at.tzinfo is None else res.start_at
            res_e = res.end_at.replace(tzinfo=timezone.utc) if res.end_at.tzinfo is None else res.end_at
            if max(w_s, res_s) >= min(w_e, res_e):
                continue

            # Check if capacity allows placing this entry on the table
            demand = compute_table_max_demand(db, table.id, w_s, w_e)
            if demand + w.party_size <= table.seats:
                # Matches! Promote to OFFERED
                w.status = WaitlistStatusEnum.OFFERED.value
                w.held_table_id = table.id
                w.offer_expires_at = exp_utc
                offered_entry = w
                db.flush()
                break

        db.commit()
        db.refresh(res)
        if offered_entry:
            db.refresh(offered_entry)
        return res, offered_entry


class WaitlistController:
    @staticmethod
    def list_waitlist(
        db: Session,
        current_user: UserModel,
        wait_status: Optional[WaitlistStatusEnum],
        from_time: Optional[datetime],
        to_time: Optional[datetime],
        limit: int,
        after_id: Optional[int],
    ) -> WaitlistListResponse:
        query = select(WaitlistModel)

        if current_user.role != RoleEnum.ADMIN.value:
            query = query.where(WaitlistModel.user_id == current_user.id)

        if wait_status is not None:
            query = query.where(WaitlistModel.status == wait_status.value)

        if from_time is not None and to_time is not None:
            f_utc = normalize_utc(from_time)
            t_utc = normalize_utc(to_time)
            if f_utc >= t_utc:
                raise AppException(422, "'from' must be strictly earlier than 'to'", "Unprocessable Entity")
            query = query.where(WaitlistModel.start_at < t_utc, WaitlistModel.end_at > f_utc)
        elif from_time is not None:
            f_utc = normalize_utc(from_time)
            query = query.where(WaitlistModel.end_at > f_utc)
        elif to_time is not None:
            t_utc = normalize_utc(to_time)
            query = query.where(WaitlistModel.start_at < t_utc)

        if after_id is not None and after_id > 0:
            query = query.where(WaitlistModel.id > after_id)

        query = query.order_by(WaitlistModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())

        next_after_id = None
        if len(records) > limit:
            next_after_id = records[limit - 1].id
            records = records[:limit]

        items = [
            WaitlistOut(
                id=w.id,
                user_id=w.user_id,
                guest_name=w.guest_name,
                party_size=w.party_size,
                start_at=w.start_at.replace(tzinfo=timezone.utc) if w.start_at.tzinfo is None else w.start_at,
                end_at=w.end_at.replace(tzinfo=timezone.utc) if w.end_at.tzinfo is None else w.end_at,
                status=WaitlistStatusEnum(w.status),
                held_table_id=w.held_table_id,
                offer_expires_at=w.offer_expires_at.replace(tzinfo=timezone.utc) if w.offer_expires_at and w.offer_expires_at.tzinfo is None else w.offer_expires_at,
                accepted_at=w.accepted_at.replace(tzinfo=timezone.utc) if w.accepted_at and w.accepted_at.tzinfo is None else w.accepted_at,
                created_at=w.created_at.replace(tzinfo=timezone.utc) if w.created_at.tzinfo is None else w.created_at,
            )
            for w in records
        ]
        return WaitlistListResponse(items=items, next_after_id=next_after_id)

    @staticmethod
    def create_waitlist_entry(
        db: Session, current_user: UserModel, payload: WaitlistCreatePayload
    ) -> WaitlistModel:
        s_utc, e_utc = validate_interval(payload.start_at, payload.end_at)
        trimmed_name = payload.guest_name.strip()
        if len(trimmed_name) < 2 or len(trimmed_name) > 120:
            raise AppException(422, "guest_name must be 2..120 trimmed characters", "Unprocessable Entity")

        now = datetime.now(timezone.utc)
        entry = WaitlistModel(
            user_id=current_user.id,
            guest_name=trimmed_name,
            party_size=payload.party_size,
            start_at=s_utc,
            end_at=e_utc,
            status=WaitlistStatusEnum.WAITING.value,
            created_at=now,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry

    @staticmethod
    def get_waitlist_entry(db: Session, current_user: UserModel, waitlist_id: int) -> WaitlistModel:
        if waitlist_id <= 0:
            raise AppException(404, "waitlist_id must be a positive integer", "Not Found")

        entry = db.scalar(select(WaitlistModel).where(WaitlistModel.id == waitlist_id))
        if not entry:
            raise AppException(404, f"Waitlist entry {waitlist_id} not found", "Not Found")

        if current_user.role != RoleEnum.ADMIN.value and entry.user_id != current_user.id:
            raise AppException(403, "Access denied to waitlist entry of another user", "Forbidden")

        return entry

    @staticmethod
    def cancel_waitlist_entry(db: Session, current_user: UserModel, waitlist_id: int):
        entry = WaitlistController.get_waitlist_entry(db, current_user, waitlist_id)

        if entry.status in (
            WaitlistStatusEnum.ACCEPTED.value,
            WaitlistStatusEnum.CANCELLED.value,
            WaitlistStatusEnum.EXPIRED.value,
        ):
            raise AppException(
                409, f"Cannot cancel waitlist entry in terminal status '{entry.status}'", "Conflict"
            )

        entry.status = WaitlistStatusEnum.CANCELLED.value
        entry.held_table_id = None
        entry.offer_expires_at = None
        db.commit()

    @staticmethod
    def accept_waitlist_entry(
        db: Session, current_user: UserModel, waitlist_id: int, payload: WaitlistAcceptPayload
    ) -> ReservationModel:
        entry = WaitlistController.get_waitlist_entry(db, current_user, waitlist_id)

        if entry.status != WaitlistStatusEnum.OFFERED.value:
            raise AppException(409, "Only OFFERED waitlist entries can be accepted", "Conflict")

        if not entry.held_table_id:
            raise AppException(404, "Held table missing for offered entry", "Not Found")

        table = TableController.get_table(db, entry.held_table_id)
        if not table.active:
            raise AppException(409, "Held table is no longer active", "Conflict")

        acc_utc = normalize_utc(payload.accepted_at)
        exp_utc = (
            entry.offer_expires_at.replace(tzinfo=timezone.utc)
            if entry.offer_expires_at.tzinfo is None
            else entry.offer_expires_at
        )
        if acc_utc > exp_utc:
            raise AppException(409, "Offer has expired and cannot be accepted", "Conflict")

        s_utc = entry.start_at.replace(tzinfo=timezone.utc) if entry.start_at.tzinfo is None else entry.start_at
        e_utc = entry.end_at.replace(tzinfo=timezone.utc) if entry.end_at.tzinfo is None else entry.end_at

        # Check capacity excluding this waitlist entry hold itself
        demand = compute_table_max_demand(db, table.id, s_utc, e_utc, exclude_waitlist_id=entry.id)
        if demand + entry.party_size > table.seats:
            raise AppException(
                409,
                f"Insufficient capacity on table {table.id} to accept reservation",
                "Conflict",
            )

        # Mark waitlist ACCEPTED
        entry.status = WaitlistStatusEnum.ACCEPTED.value
        entry.accepted_at = acc_utc
        db.flush()

        # Create BOOKED reservation
        res = ReservationModel(
            table_id=table.id,
            user_id=entry.user_id,
            guest_name=entry.guest_name,
            party_size=entry.party_size,
            start_at=s_utc,
            end_at=e_utc,
            status=ReservationStatusEnum.BOOKED.value,
            created_at=acc_utc,
        )
        db.add(res)
        db.commit()
        db.refresh(res)
        return res

    @staticmethod
    def expire_waitlist_entry(
        db: Session, current_user: UserModel, waitlist_id: int, payload: WaitlistExpirePayload
    ) -> WaitlistModel:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "Admin privileges required to expire waitlist entries", "Forbidden")

        entry = WaitlistController.get_waitlist_entry(db, current_user, waitlist_id)

        if entry.status != WaitlistStatusEnum.OFFERED.value:
            raise AppException(409, "Only OFFERED waitlist entries can be expired", "Conflict")

        exp_param_utc = normalize_utc(payload.expired_at)
        offer_exp_utc = (
            entry.offer_expires_at.replace(tzinfo=timezone.utc)
            if entry.offer_expires_at.tzinfo is None
            else entry.offer_expires_at
        )

        if exp_param_utc < offer_exp_utc:
            raise AppException(
                409,
                f"expired_at ({exp_param_utc.isoformat()}) must be at or after offer_expires_at ({offer_exp_utc.isoformat()})",
                "Conflict",
            )

        entry.status = WaitlistStatusEnum.EXPIRED.value
        entry.held_table_id = None
        db.commit()
        db.refresh(entry)
        return entry


class AvailabilityController:
    @staticmethod
    def get_availability(
        db: Session,
        start_at: datetime,
        end_at: datetime,
        party_size: int,
        zone: Optional[ZoneEnum],
    ) -> List[TableAvailabilityOut]:
        s_utc, e_utc = validate_interval(start_at, end_at)

        query = select(TableModel).where(
            TableModel.is_deleted == False,
            TableModel.active == True,
            TableModel.seats >= party_size,
        )
        if zone:
            query = query.where(TableModel.zone == zone.value)

        candidate_tables = list(db.scalars(query).all())
        results = []

        for table in candidate_tables:
            max_demand = compute_table_max_demand(db, table.id, s_utc, e_utc)
            available_seats = table.seats - max_demand
            if available_seats >= party_size:
                results.append(
                    TableAvailabilityOut(
                        table_id=table.id,
                        code=table.code,
                        seats=table.seats,
                        zone=ZoneEnum(table.zone),
                        available_seats=available_seats,
                    )
                )

        return results


# -----------------------------------------------------------------------------
# FastAPI App Initialization & Routes
# -----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    with SessionLocal() as db:
        seed_database(db)
    yield


app = FastAPI(
    title="Restaurant Management - Cancellations, Waitlist, and Availability API",
    version="1.0.0",
    description="RESTful API for restaurant reservations, waitlist queuing, automated offer transitions, and table availability.",
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
        seats=table.seats,
        zone=ZoneEnum(table.zone),
        active=table.active,
    )


@app.get(
    "/tables/{table_id}",
    response_model=TableOut,
    status_code=status.HTTP_200_OK,
    summary="Get table details",
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
    table_id: Optional[int] = Query(None, description="Optional table id filter"),
    status: Optional[ReservationStatusEnum] = Query(None, description="Filter by reservation status"),
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
    summary="Create reservation",
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
        cancelled_at=res.cancelled_at.replace(tzinfo=timezone.utc) if res.cancelled_at and res.cancelled_at.tzinfo is None else res.cancelled_at,
        cancellation_reason=res.cancellation_reason,
        cancelled_by_user_id=res.cancelled_by_user_id,
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
        cancelled_at=res.cancelled_at.replace(tzinfo=timezone.utc) if res.cancelled_at and res.cancelled_at.tzinfo is None else res.cancelled_at,
        cancellation_reason=res.cancellation_reason,
        cancelled_by_user_id=res.cancelled_by_user_id,
    )


@app.post(
    "/reservations/{reservation_id}/cancel",
    response_model=ReservationOut,
    status_code=status.HTTP_200_OK,
    summary="Cancel reservation and trigger waitlist offer",
    tags=["Reservations"],
)
def cancel_reservation(
    reservation_id: int,
    payload: ReservationCancelPayload,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    res, _ = ReservationController.cancel_reservation(db, current_user, reservation_id, payload)
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
        cancelled_at=res.cancelled_at.replace(tzinfo=timezone.utc) if res.cancelled_at and res.cancelled_at.tzinfo is None else res.cancelled_at,
        cancellation_reason=res.cancellation_reason,
        cancelled_by_user_id=res.cancelled_by_user_id,
    )


@app.get(
    "/waitlist",
    response_model=WaitlistListResponse,
    status_code=status.HTTP_200_OK,
    summary="List waitlist entries",
    tags=["Waitlist"],
)
def list_waitlist(
    status: Optional[WaitlistStatusEnum] = Query(None, description="Filter by status"),
    from_time: Optional[datetime] = Query(None, alias="from", description="Filter overlapping start"),
    to_time: Optional[datetime] = Query(None, alias="to", description="Filter overlapping end"),
    limit: int = Query(20, ge=1, le=100, description="Pagination limit"),
    after_id: Optional[int] = Query(None, gt=0, description="Pagination cursor"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return WaitlistController.list_waitlist(
        db, current_user, status, from_time, to_time, limit, after_id
    )


@app.post(
    "/waitlist",
    response_model=WaitlistOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create waitlist entry",
    tags=["Waitlist"],
)
def create_waitlist_entry(
    payload: WaitlistCreatePayload,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    entry = WaitlistController.create_waitlist_entry(db, current_user, payload)
    response.headers["Location"] = f"/waitlist/{entry.id}"
    return WaitlistOut(
        id=entry.id,
        user_id=entry.user_id,
        guest_name=entry.guest_name,
        party_size=entry.party_size,
        start_at=entry.start_at.replace(tzinfo=timezone.utc) if entry.start_at.tzinfo is None else entry.start_at,
        end_at=entry.end_at.replace(tzinfo=timezone.utc) if entry.end_at.tzinfo is None else entry.end_at,
        status=WaitlistStatusEnum(entry.status),
        held_table_id=entry.held_table_id,
        offer_expires_at=entry.offer_expires_at.replace(tzinfo=timezone.utc) if entry.offer_expires_at and entry.offer_expires_at.tzinfo is None else entry.offer_expires_at,
        accepted_at=entry.accepted_at.replace(tzinfo=timezone.utc) if entry.accepted_at and entry.accepted_at.tzinfo is None else entry.accepted_at,
        created_at=entry.created_at.replace(tzinfo=timezone.utc) if entry.created_at.tzinfo is None else entry.created_at,
    )


@app.get(
    "/waitlist/{waitlist_id}",
    response_model=WaitlistOut,
    status_code=status.HTTP_200_OK,
    summary="Get waitlist entry by id",
    tags=["Waitlist"],
)
def get_waitlist_entry(
    waitlist_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    entry = WaitlistController.get_waitlist_entry(db, current_user, waitlist_id)
    return WaitlistOut(
        id=entry.id,
        user_id=entry.user_id,
        guest_name=entry.guest_name,
        party_size=entry.party_size,
        start_at=entry.start_at.replace(tzinfo=timezone.utc) if entry.start_at.tzinfo is None else entry.start_at,
        end_at=entry.end_at.replace(tzinfo=timezone.utc) if entry.end_at.tzinfo is None else entry.end_at,
        status=WaitlistStatusEnum(entry.status),
        held_table_id=entry.held_table_id,
        offer_expires_at=entry.offer_expires_at.replace(tzinfo=timezone.utc) if entry.offer_expires_at and entry.offer_expires_at.tzinfo is None else entry.offer_expires_at,
        accepted_at=entry.accepted_at.replace(tzinfo=timezone.utc) if entry.accepted_at and entry.accepted_at.tzinfo is None else entry.accepted_at,
        created_at=entry.created_at.replace(tzinfo=timezone.utc) if entry.created_at.tzinfo is None else entry.created_at,
    )


@app.delete(
    "/waitlist/{waitlist_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cancel waitlist entry",
    tags=["Waitlist"],
)
def cancel_waitlist_entry(
    waitlist_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    WaitlistController.cancel_waitlist_entry(db, current_user, waitlist_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post(
    "/waitlist/{waitlist_id}/accept",
    response_model=ReservationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Accept offered waitlist entry into booked reservation",
    tags=["Waitlist"],
)
def accept_waitlist_entry(
    waitlist_id: int,
    payload: WaitlistAcceptPayload,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    res = WaitlistController.accept_waitlist_entry(db, current_user, waitlist_id, payload)
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
        cancelled_at=res.cancelled_at.replace(tzinfo=timezone.utc) if res.cancelled_at and res.cancelled_at.tzinfo is None else res.cancelled_at,
        cancellation_reason=res.cancellation_reason,
        cancelled_by_user_id=res.cancelled_by_user_id,
    )


@app.post(
    "/waitlist/{waitlist_id}/expire",
    response_model=WaitlistOut,
    status_code=status.HTTP_200_OK,
    summary="Expire offered waitlist entry (admin only)",
    tags=["Waitlist"],
)
def expire_waitlist_entry(
    waitlist_id: int,
    payload: WaitlistExpirePayload,
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    entry = WaitlistController.expire_waitlist_entry(db, current_user, waitlist_id, payload)
    return WaitlistOut(
        id=entry.id,
        user_id=entry.user_id,
        guest_name=entry.guest_name,
        party_size=entry.party_size,
        start_at=entry.start_at.replace(tzinfo=timezone.utc) if entry.start_at.tzinfo is None else entry.start_at,
        end_at=entry.end_at.replace(tzinfo=timezone.utc) if entry.end_at.tzinfo is None else entry.end_at,
        status=WaitlistStatusEnum(entry.status),
        held_table_id=entry.held_table_id,
        offer_expires_at=entry.offer_expires_at.replace(tzinfo=timezone.utc) if entry.offer_expires_at and entry.offer_expires_at.tzinfo is None else entry.offer_expires_at,
        accepted_at=entry.accepted_at.replace(tzinfo=timezone.utc) if entry.accepted_at and entry.accepted_at.tzinfo is None else entry.accepted_at,
        created_at=entry.created_at.replace(tzinfo=timezone.utc) if entry.created_at.tzinfo is None else entry.created_at,
    )


@app.get(
    "/availability",
    response_model=List[TableAvailabilityOut],
    status_code=status.HTTP_200_OK,
    summary="Check table availability for requested interval and party size",
    tags=["Availability"],
)
def get_availability(
    start_at: datetime = Query(..., description="Start UTC timestamp on 5-minute boundary"),
    end_at: datetime = Query(..., description="End UTC timestamp on 5-minute boundary"),
    party_size: int = Query(..., ge=1, le=20, description="Requested party size"),
    zone: Optional[ZoneEnum] = Query(None, description="Optional zone filter"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return AvailabilityController.get_availability(db, start_at, end_at, party_size, zone)
