"""
Fitness Management - Variant B: Sessions, sets, and one-repetition maximum
CASE_ID: 9

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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
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
logger = logging.getLogger("workout_sessions")
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


class SessionStatusEnum(str, enum.Enum):
    OPEN = "OPEN"
    COMPLETED = "COMPLETED"


# ============================================================================
# SQLAlchemy Models
# ============================================================================
class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    role = Column(String(20), nullable=False)
    token = Column(String(100), unique=True, nullable=False)


class ExerciseModel(Base):
    __tablename__ = "exercises"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(120), nullable=False)
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


class WorkoutSessionModel(Base):
    __tablename__ = "workout_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(20), nullable=False, default=SessionStatusEnum.OPEN.value)
    notes = Column(String(500), nullable=True)
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


class PerformedSetModel(Base):
    __tablename__ = "performed_sets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("workout_sessions.id"), nullable=False, index=True)
    exercise_id = Column(Integer, ForeignKey("exercises.id"), nullable=False, index=True)
    set_order = Column(Integer, nullable=False)
    reps = Column(Integer, nullable=False)
    load_kg = Column(Float, nullable=False)
    rpe = Column(Float, nullable=True)
    is_void = Column(Boolean, default=False, nullable=False)
    void_reason = Column(String(500), nullable=True)
    calculated_1rm = Column(Float, nullable=True)
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
# Helpers & Formula
# ============================================================================
def to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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


def calculate_epley_1rm(load_kg: float, reps: int) -> Optional[float]:
    """
    REQ-9-005: One RM uses Epley load_kg times (1 + reps/30) only for reps 1..20.
    """
    if not (1 <= reps <= 20):
        return None
    if load_kg <= 0:
        return 0.0
    return round(load_kg * (1.0 + reps / 30.0), 2)


# ============================================================================
# Pydantic Schemas
# ============================================================================
class AppException(HTTPException):
    def __init__(self, status_code: int, detail: str, error_type: str = "Error"):
        super().__init__(status_code=status_code, detail=detail)
        self.error_type = error_type


# --- Exercise Schemas ---
class ExerciseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., description="Unique exercise code (2..50 chars)")
    name: str = Field(..., description="Exercise name (2..120 chars)")
    active: Optional[bool] = Field(default=True)

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: str) -> str:
        v = v.strip().upper()
        if not (2 <= len(v) <= 50):
            raise ValueError("code must be between 2 and 50 characters")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not (2 <= len(v) <= 120):
            raise ValueError("name must be between 2 and 120 characters")
        return v


class ExercisePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Optional[str] = None
    name: Optional[str] = None
    active: Optional[bool] = None

    @field_validator("code")
    @classmethod
    def validate_code(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().upper()
        if not (2 <= len(v) <= 50):
            raise ValueError("code must be between 2 and 50 characters")
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (2 <= len(v) <= 120):
            raise ValueError("name must be between 2 and 120 characters")
        return v


class ExerciseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    code: str
    name: str
    active: bool
    created_at: datetime
    updated_at: datetime


class ExerciseListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[ExerciseResponse]
    limit: int
    next_after_id: Optional[int] = None


# --- Workout Session Schemas ---
class WorkoutSessionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    started_at: datetime = Field(..., description="UTC start timestamp (2025..2035)")
    notes: Optional[str] = Field(default=None, max_length=500)
    user_id: Optional[int] = Field(default=None, gt=0, description="Owner user ID (admin only)")

    @field_validator("started_at", mode="before")
    @classmethod
    def validate_started_at(cls, v: Any) -> datetime:
        return parse_and_validate_timestamp(v)


class WorkoutSessionPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    notes: Optional[str] = Field(default=None, max_length=500)

    @field_validator("started_at", "ended_at", mode="before")
    @classmethod
    def validate_timestamps(cls, v: Any) -> Optional[datetime]:
        if v is None:
            return None
        return parse_and_validate_timestamp(v)


class WorkoutSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    user_id: int
    started_at: datetime
    ended_at: Optional[datetime]
    status: SessionStatusEnum
    notes: Optional[str]
    created_at: datetime
    updated_at: datetime


class WorkoutSessionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[WorkoutSessionResponse]
    limit: int
    next_after_id: Optional[int] = None


# --- Performed Set Schemas ---
class PerformedSetCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: int = Field(..., gt=0, description="Workout session ID")
    exercise_id: int = Field(..., gt=0, description="Exercise ID")
    reps: int = Field(..., ge=1, le=100, description="Reps (1..100)")
    load_kg: float = Field(..., ge=0.0, le=500.0, description="Load in kg (0..500)")
    set_order: Optional[int] = Field(default=None, ge=1, le=100)
    rpe: Optional[float] = Field(default=None, ge=1.0, le=10.0)


class PerformedSetPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reps: Optional[int] = Field(default=None, ge=1, le=100)
    load_kg: Optional[float] = Field(default=None, ge=0.0, le=500.0)
    set_order: Optional[int] = Field(default=None, ge=1, le=100)
    rpe: Optional[float] = Field(default=None, ge=1.0, le=10.0)


class PerformedSetVoidRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: Optional[str] = Field(default=None, max_length=500)


class PerformedSetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    session_id: int
    exercise_id: int
    set_order: int
    reps: int
    load_kg: float
    rpe: Optional[float]
    is_void: bool
    void_reason: Optional[str]
    calculated_1rm: Optional[float]
    created_at: datetime
    updated_at: datetime


class PerformedSetListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[PerformedSetResponse]
    limit: int
    next_after_id: Optional[int] = None


# --- Volume & 1RM Schemas ---
class ExerciseVolumeBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exercise_id: int
    exercise_name: str
    volume_kg: float
    sets_count: int
    reps_count: int


class SessionVolumeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: int
    total_volume_kg: float
    total_sets: int
    total_reps: int
    exercise_breakdown: List[ExerciseVolumeBreakdown]


class OneRepMaxResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exercise_id: int
    exercise_code: str
    exercise_name: str
    user_id: int
    max_1rm: Optional[float]
    based_on_set_id: Optional[int]
    load_kg: Optional[float]
    reps: Optional[int]
    achieved_at: Optional[datetime]


# ============================================================================
# Seed Deterministic Data
# ============================================================================
def init_db():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        if db.execute(select(UserModel)).first():
            return

        # Seed Users
        u1 = UserModel(id=1, username="user1", role=RoleEnum.USER.value, token="user1-token-secret-12345")
        u2 = UserModel(id=2, username="user2", role=RoleEnum.USER.value, token="user2-token-secret-12345")
        admin = UserModel(id=3, username="admin", role=RoleEnum.ADMIN.value, token="admin-token-secret-12345")
        db.add_all([u1, u2, admin])

        # Seed Exercises
        e1 = ExerciseModel(id=1, code="BENCH_PRESS", name="Barbell Bench Press", active=True)
        e2 = ExerciseModel(id=2, code="BACK_SQUAT", name="Barbell Back Squat", active=True)
        e3 = ExerciseModel(id=3, code="DEADLIFT", name="Conventional Deadlift", active=True)
        db.add_all([e1, e2, e3])

        # Seed Sessions
        s1 = WorkoutSessionModel(
            id=1,
            user_id=1,
            started_at=datetime(2025, 5, 1, 10, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2025, 5, 1, 11, 30, 0, tzinfo=timezone.utc),
            status=SessionStatusEnum.COMPLETED.value,
            notes="Chest & Squats",
        )
        s2 = WorkoutSessionModel(
            id=2,
            user_id=1,
            started_at=datetime(2025, 5, 3, 15, 0, 0, tzinfo=timezone.utc),
            ended_at=None,
            status=SessionStatusEnum.OPEN.value,
            notes="Afternoon Bench session",
        )
        s3 = WorkoutSessionModel(
            id=3,
            user_id=2,
            started_at=datetime(2025, 5, 2, 18, 0, 0, tzinfo=timezone.utc),
            ended_at=None,
            status=SessionStatusEnum.OPEN.value,
            notes="User2 Deadlift start",
        )
        db.add_all([s1, s2, s3])

        # Seed Sets
        # Session 1:
        # Set 1: 5 reps @ 100kg -> 100 * (1 + 5/30) = 116.67
        set1 = PerformedSetModel(
            id=1,
            session_id=1,
            exercise_id=1,
            set_order=1,
            reps=5,
            load_kg=100.0,
            calculated_1rm=116.67,
            is_void=False,
        )
        # Set 2: 3 reps @ 110kg -> 110 * (1 + 3/30) = 121.0
        set2 = PerformedSetModel(
            id=2,
            session_id=1,
            exercise_id=1,
            set_order=2,
            reps=3,
            load_kg=110.0,
            calculated_1rm=121.0,
            is_void=False,
        )
        # Set 3: 8 reps @ 120kg -> 120 * (1 + 8/30) = 152.0
        set3 = PerformedSetModel(
            id=3,
            session_id=1,
            exercise_id=2,
            set_order=3,
            reps=8,
            load_kg=120.0,
            calculated_1rm=152.0,
            is_void=False,
        )
        # Session 2:
        # Set 4: 10 reps @ 80kg -> 80 * (1 + 10/30) = 106.67
        set4 = PerformedSetModel(
            id=4,
            session_id=2,
            exercise_id=1,
            set_order=1,
            reps=10,
            load_kg=80.0,
            calculated_1rm=106.67,
            is_void=False,
        )
        db.add_all([set1, set2, set3, set4])
        db.commit()


init_db()


# ============================================================================
# Dependencies
# ============================================================================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> UserModel:
    if not authorization:
        raise AppException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing Authorization header",
            "Unauthorized",
        )
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AppException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid Authorization scheme, expected Bearer <token>",
            "Unauthorized",
        )
    token = parts[1]
    user = db.execute(select(UserModel).where(UserModel.token == token)).scalar_one_or_none()
    if not user:
        raise AppException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or expired token",
            "Unauthorized",
        )
    return user


def require_admin(current_user: UserModel = Depends(get_current_user)) -> UserModel:
    if current_user.role != RoleEnum.ADMIN.value:
        raise AppException(
            status.HTTP_403_FORBIDDEN,
            "Admin required",
            "Forbidden",
        )
    return current_user


# ============================================================================
# Controllers
# ============================================================================
class ExerciseController:
    @staticmethod
    def list_exercises(
        db: Session,
        current_user: UserModel,
        active: Optional[bool],
        limit: int,
        after_id: Optional[int],
    ) -> ExerciseListResponse:
        query = select(ExerciseModel).where(ExerciseModel.is_deleted.is_(False))
        if active is not None:
            query = query.where(ExerciseModel.active == active)
        if after_id is not None:
            query = query.where(ExerciseModel.id > after_id)

        query = query.order_by(ExerciseModel.id.asc()).limit(limit + 1)
        results = db.execute(query).scalars().all()

        next_after_id = None
        if len(results) > limit:
            items = results[:limit]
            next_after_id = items[-1].id
        else:
            items = results

        return ExerciseListResponse(
            items=[ExerciseResponse.model_validate(it) for it in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_exercise(
        db: Session,
        current_user: UserModel,
        payload: ExerciseCreateRequest,
    ) -> ExerciseModel:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(status.HTTP_403_FORBIDDEN, "Admin required", "Forbidden")

        existing = db.execute(
            select(ExerciseModel).where(ExerciseModel.code == payload.code)
        ).scalar_one_or_none()
        if existing:
            raise AppException(
                status.HTTP_409_CONFLICT,
                f"Exercise code {payload.code} already exists",
                "Conflict",
            )

        ex = ExerciseModel(
            code=payload.code,
            name=payload.name,
            active=payload.active if payload.active is not None else True,
        )
        db.add(ex)
        try:
            db.commit()
            db.refresh(ex)
            return ex
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def get_exercise(db: Session, current_user: UserModel, exercise_id: int) -> ExerciseModel:
        if exercise_id <= 0:
            raise AppException(status.HTTP_404_NOT_FOUND, "Resource not found", "Not Found")

        ex = db.execute(
            select(ExerciseModel).where(
                ExerciseModel.id == exercise_id,
                ExerciseModel.is_deleted.is_(False),
            )
        ).scalar_one_or_none()
        if not ex:
            raise AppException(status.HTTP_404_NOT_FOUND, "Exercise not found", "Not Found")
        return ex

    @staticmethod
    def patch_exercise(
        db: Session,
        current_user: UserModel,
        exercise_id: int,
        payload: ExercisePatchRequest,
    ) -> ExerciseModel:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(status.HTTP_403_FORBIDDEN, "Admin required", "Forbidden")

        ex = ExerciseController.get_exercise(db, current_user, exercise_id)

        if payload.code is not None and payload.code != ex.code:
            dup = db.execute(
                select(ExerciseModel).where(
                    ExerciseModel.code == payload.code,
                    ExerciseModel.id != ex.id,
                )
            ).scalar_one_or_none()
            if dup:
                raise AppException(
                    status.HTTP_409_CONFLICT,
                    f"Exercise code {payload.code} already exists",
                    "Conflict",
                )
            ex.code = payload.code

        if payload.name is not None:
            ex.name = payload.name
        if payload.active is not None:
            ex.active = payload.active

        try:
            db.commit()
            db.refresh(ex)
            return ex
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def delete_exercise(db: Session, current_user: UserModel, exercise_id: int) -> None:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(status.HTTP_403_FORBIDDEN, "Admin required", "Forbidden")

        ex = ExerciseController.get_exercise(db, current_user, exercise_id)

        # Relation constraint: cannot delete if referenced in performed sets
        used = db.execute(
            select(PerformedSetModel).where(PerformedSetModel.exercise_id == ex.id)
        ).scalars().all()
        if used:
            raise AppException(
                status.HTTP_409_CONFLICT,
                "Cannot delete exercise referenced in performed sets",
                "Conflict",
            )

        ex.is_deleted = True
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def calculate_one_rm(
        db: Session,
        current_user: UserModel,
        exercise_id: int,
        user_id: Optional[int],
    ) -> OneRepMaxResponse:
        ex = ExerciseController.get_exercise(db, current_user, exercise_id)

        target_uid = current_user.id
        if user_id is not None:
            if current_user.role != RoleEnum.ADMIN.value and user_id != current_user.id:
                raise AppException(
                    status.HTTP_403_FORBIDDEN,
                    "Users can only check their own 1RM",
                    "Forbidden",
                )
            target_uid = user_id

        # Query all non-void sets for this exercise performed by user
        query = (
            select(PerformedSetModel, WorkoutSessionModel)
            .join(WorkoutSessionModel, PerformedSetModel.session_id == WorkoutSessionModel.id)
            .where(
                PerformedSetModel.exercise_id == ex.id,
                PerformedSetModel.is_void.is_(False),
                WorkoutSessionModel.user_id == target_uid,
                WorkoutSessionModel.is_deleted.is_(False),
                PerformedSetModel.reps >= 1,
                PerformedSetModel.reps <= 20,
            )
        )
        rows = db.execute(query).all()

        max_1rm = None
        best_set = None
        achieved_at = None

        for p_set, sess in rows:
            est_1rm = p_set.calculated_1rm
            if est_1rm is None:
                est_1rm = calculate_epley_1rm(p_set.load_kg, p_set.reps)

            if est_1rm is not None:
                if max_1rm is None or est_1rm > max_1rm:
                    max_1rm = est_1rm
                    best_set = p_set
                    achieved_at = sess.started_at

        return OneRepMaxResponse(
            exercise_id=ex.id,
            exercise_code=ex.code,
            exercise_name=ex.name,
            user_id=target_uid,
            max_1rm=max_1rm,
            based_on_set_id=best_set.id if best_set else None,
            load_kg=best_set.load_kg if best_set else None,
            reps=best_set.reps if best_set else None,
            achieved_at=achieved_at,
        )


class WorkoutSessionController:
    @staticmethod
    def list_sessions(
        db: Session,
        current_user: UserModel,
        user_id: Optional[int],
        session_status: Optional[SessionStatusEnum],
        limit: int,
        after_id: Optional[int],
    ) -> WorkoutSessionListResponse:
        query = select(WorkoutSessionModel).where(WorkoutSessionModel.is_deleted.is_(False))

        if current_user.role != RoleEnum.ADMIN.value:
            query = query.where(WorkoutSessionModel.user_id == current_user.id)
        elif user_id is not None:
            query = query.where(WorkoutSessionModel.user_id == user_id)

        if session_status:
            query = query.where(WorkoutSessionModel.status == session_status.value)

        if after_id is not None:
            query = query.where(WorkoutSessionModel.id > after_id)

        query = query.order_by(WorkoutSessionModel.id.asc()).limit(limit + 1)
        results = db.execute(query).scalars().all()

        next_after_id = None
        if len(results) > limit:
            items = results[:limit]
            next_after_id = items[-1].id
        else:
            items = results

        return WorkoutSessionListResponse(
            items=[WorkoutSessionResponse.model_validate(it) for it in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_session(
        db: Session,
        current_user: UserModel,
        payload: WorkoutSessionCreateRequest,
    ) -> WorkoutSessionModel:
        target_user_id = current_user.id
        if payload.user_id is not None:
            if current_user.role != RoleEnum.ADMIN.value and payload.user_id != current_user.id:
                raise AppException(
                    status.HTTP_403_FORBIDDEN,
                    "Users cannot create sessions for other accounts",
                    "Forbidden",
                )
            target_user = db.execute(
                select(UserModel).where(UserModel.id == payload.user_id)
            ).scalar_one_or_none()
            if not target_user:
                raise AppException(status.HTTP_404_NOT_FOUND, "Target user not found", "Not Found")
            target_user_id = target_user.id

        session = WorkoutSessionModel(
            user_id=target_user_id,
            started_at=payload.started_at,
            notes=payload.notes,
            status=SessionStatusEnum.OPEN.value,
        )
        db.add(session)
        try:
            db.commit()
            db.refresh(session)
            return session
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def get_session(db: Session, current_user: UserModel, workoutsession_id: int) -> WorkoutSessionModel:
        if workoutsession_id <= 0:
            raise AppException(status.HTTP_404_NOT_FOUND, "Resource not found", "Not Found")

        session = db.execute(
            select(WorkoutSessionModel).where(
                WorkoutSessionModel.id == workoutsession_id,
                WorkoutSessionModel.is_deleted.is_(False),
            )
        ).scalar_one_or_none()

        if not session:
            raise AppException(status.HTTP_404_NOT_FOUND, "Workout session not found", "Not Found")

        if current_user.role != RoleEnum.ADMIN.value and session.user_id != current_user.id:
            raise AppException(status.HTTP_403_FORBIDDEN, "Access forbidden", "Forbidden")

        return session

    @staticmethod
    def patch_session(
        db: Session,
        current_user: UserModel,
        workoutsession_id: int,
        payload: WorkoutSessionPatchRequest,
    ) -> WorkoutSessionModel:
        session = WorkoutSessionController.get_session(db, current_user, workoutsession_id)

        # REQ-9-006: Completed sessions cannot change
        if session.status == SessionStatusEnum.COMPLETED.value:
            raise AppException(
                status.HTTP_409_CONFLICT,
                "Completed sessions cannot be modified",
                "Conflict",
            )

        new_started = to_utc(payload.started_at or session.started_at)
        new_ended = to_utc(payload.ended_at or session.ended_at)
        if new_ended and new_started > new_ended:
            raise AppException(
                status.HTTP_400_BAD_REQUEST,
                "started_at must be less than or equal to ended_at",
                "Bad Request",
            )

        if payload.started_at is not None:
            session.started_at = payload.started_at
        if payload.ended_at is not None:
            session.ended_at = payload.ended_at
        if payload.notes is not None:
            session.notes = payload.notes

        try:
            db.commit()
            db.refresh(session)
            return session
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def delete_session(db: Session, current_user: UserModel, workoutsession_id: int) -> None:
        session = WorkoutSessionController.get_session(db, current_user, workoutsession_id)

        if session.status == SessionStatusEnum.COMPLETED.value:
            raise AppException(
                status.HTTP_409_CONFLICT,
                "Completed sessions cannot be deleted; retained as history",
                "Conflict",
            )

        session.is_deleted = True
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def complete_session(
        db: Session,
        current_user: UserModel,
        workoutsession_id: int,
    ) -> WorkoutSessionModel:
        session = WorkoutSessionController.get_session(db, current_user, workoutsession_id)

        if session.status == SessionStatusEnum.COMPLETED.value:
            # Idempotent completion
            return session

        session.status = SessionStatusEnum.COMPLETED.value
        if session.ended_at is None:
            session.ended_at = datetime.now(timezone.utc)

        try:
            db.commit()
            db.refresh(session)
            return session
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def calculate_volume(
        db: Session,
        current_user: UserModel,
        workoutsession_id: int,
    ) -> SessionVolumeResponse:
        session = WorkoutSessionController.get_session(db, current_user, workoutsession_id)

        sets = db.execute(
            select(PerformedSetModel)
            .where(
                PerformedSetModel.session_id == session.id,
                PerformedSetModel.is_void.is_(False),
            )
            .order_by(PerformedSetModel.set_order.asc())
        ).scalars().all()

        total_volume = 0.0
        total_reps = 0
        exercise_map: Dict[int, Dict[str, Any]] = {}

        for s in sets:
            set_vol = round(s.load_kg * s.reps, 2)
            total_volume += set_vol
            total_reps += s.reps

            if s.exercise_id not in exercise_map:
                ex = db.execute(
                    select(ExerciseModel).where(ExerciseModel.id == s.exercise_id)
                ).scalar_one()
                exercise_map[s.exercise_id] = {
                    "exercise_name": ex.name,
                    "volume_kg": 0.0,
                    "sets_count": 0,
                    "reps_count": 0,
                }

            exercise_map[s.exercise_id]["volume_kg"] += set_vol
            exercise_map[s.exercise_id]["sets_count"] += 1
            exercise_map[s.exercise_id]["reps_count"] += s.reps

        breakdown = [
            ExerciseVolumeBreakdown(
                exercise_id=eid,
                exercise_name=data["exercise_name"],
                volume_kg=round(data["volume_kg"], 2),
                sets_count=data["sets_count"],
                reps_count=data["reps_count"],
            )
            for eid, data in exercise_map.items()
        ]

        return SessionVolumeResponse(
            session_id=session.id,
            total_volume_kg=round(total_volume, 2),
            total_sets=len(sets),
            total_reps=total_reps,
            exercise_breakdown=breakdown,
        )


class PerformedSetController:
    @staticmethod
    def list_sets(
        db: Session,
        current_user: UserModel,
        session_id: Optional[int],
        exercise_id: Optional[int],
        is_void: Optional[bool],
        limit: int,
        after_id: Optional[int],
    ) -> PerformedSetListResponse:
        query = select(PerformedSetModel).join(WorkoutSessionModel)

        if current_user.role != RoleEnum.ADMIN.value:
            query = query.where(WorkoutSessionModel.user_id == current_user.id)

        if session_id is not None:
            if session_id <= 0:
                raise AppException(status.HTTP_404_NOT_FOUND, "Invalid session_id", "Not Found")
            query = query.where(PerformedSetModel.session_id == session_id)

        if exercise_id is not None:
            if exercise_id <= 0:
                raise AppException(status.HTTP_404_NOT_FOUND, "Invalid exercise_id", "Not Found")
            query = query.where(PerformedSetModel.exercise_id == exercise_id)

        if is_void is not None:
            query = query.where(PerformedSetModel.is_void == is_void)

        if after_id is not None:
            query = query.where(PerformedSetModel.id > after_id)

        query = query.order_by(PerformedSetModel.id.asc()).limit(limit + 1)
        results = db.execute(query).scalars().all()

        next_after_id = None
        if len(results) > limit:
            items = results[:limit]
            next_after_id = items[-1].id
        else:
            items = results

        return PerformedSetListResponse(
            items=[PerformedSetResponse.model_validate(it) for it in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_set(
        db: Session,
        current_user: UserModel,
        payload: PerformedSetCreateRequest,
    ) -> PerformedSetModel:
        session = WorkoutSessionController.get_session(db, current_user, payload.session_id)

        # REQ-9-006: Completed sessions cannot add sets
        if session.status == SessionStatusEnum.COMPLETED.value:
            raise AppException(
                status.HTTP_409_CONFLICT,
                "Cannot add sets to a completed session",
                "Conflict",
            )

        ex = db.execute(
            select(ExerciseModel).where(
                ExerciseModel.id == payload.exercise_id,
                ExerciseModel.is_deleted.is_(False),
            )
        ).scalar_one_or_none()
        if not ex:
            raise AppException(status.HTTP_404_NOT_FOUND, "Exercise not found", "Not Found")
        if not ex.active:
            raise AppException(status.HTTP_400_BAD_REQUEST, "Exercise is not active", "Bad Request")

        # Set order determination
        existing_count = db.execute(
            select(PerformedSetModel).where(PerformedSetModel.session_id == session.id)
        ).scalars().all()
        target_order = payload.set_order if payload.set_order is not None else len(existing_count) + 1

        calculated_1rm = calculate_epley_1rm(payload.load_kg, payload.reps)

        set_obj = PerformedSetModel(
            session_id=session.id,
            exercise_id=ex.id,
            set_order=target_order,
            reps=payload.reps,
            load_kg=payload.load_kg,
            rpe=payload.rpe,
            is_void=False,
            calculated_1rm=calculated_1rm,
        )
        db.add(set_obj)
        try:
            db.commit()
            db.refresh(set_obj)
            return set_obj
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def get_set(db: Session, current_user: UserModel, performedset_id: int) -> PerformedSetModel:
        if performedset_id <= 0:
            raise AppException(status.HTTP_404_NOT_FOUND, "Resource not found", "Not Found")

        s = db.execute(
            select(PerformedSetModel).where(PerformedSetModel.id == performedset_id)
        ).scalar_one_or_none()
        if not s:
            raise AppException(status.HTTP_404_NOT_FOUND, "Performed set not found", "Not Found")

        session = db.execute(
            select(WorkoutSessionModel).where(WorkoutSessionModel.id == s.session_id)
        ).scalar_one()

        if current_user.role != RoleEnum.ADMIN.value and session.user_id != current_user.id:
            raise AppException(status.HTTP_403_FORBIDDEN, "Access forbidden", "Forbidden")

        return s

    @staticmethod
    def patch_set(
        db: Session,
        current_user: UserModel,
        performedset_id: int,
        payload: PerformedSetPatchRequest,
    ) -> PerformedSetModel:
        s = PerformedSetController.get_set(db, current_user, performedset_id)
        session = db.execute(
            select(WorkoutSessionModel).where(WorkoutSessionModel.id == s.session_id)
        ).scalar_one()

        # REQ-9-006: Completed sessions cannot change sets
        if session.status == SessionStatusEnum.COMPLETED.value:
            raise AppException(
                status.HTTP_409_CONFLICT,
                "Cannot modify sets of a completed session",
                "Conflict",
            )

        if payload.reps is not None:
            s.reps = payload.reps
        if payload.load_kg is not None:
            s.load_kg = payload.load_kg
        if payload.set_order is not None:
            s.set_order = payload.set_order
        if payload.rpe is not None:
            s.rpe = payload.rpe

        s.calculated_1rm = calculate_epley_1rm(s.load_kg, s.reps)

        try:
            db.commit()
            db.refresh(s)
            return s
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def delete_set(db: Session, current_user: UserModel, performedset_id: int) -> None:
        s = PerformedSetController.get_set(db, current_user, performedset_id)
        session = db.execute(
            select(WorkoutSessionModel).where(WorkoutSessionModel.id == s.session_id)
        ).scalar_one()

        if session.status == SessionStatusEnum.COMPLETED.value:
            raise AppException(
                status.HTTP_409_CONFLICT,
                "Cannot delete sets of a completed session",
                "Conflict",
            )

        db.delete(s)
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def void_set(
        db: Session,
        current_user: UserModel,
        performedset_id: int,
        payload: PerformedSetVoidRequest,
    ) -> PerformedSetModel:
        s = PerformedSetController.get_set(db, current_user, performedset_id)
        session = db.execute(
            select(WorkoutSessionModel).where(WorkoutSessionModel.id == s.session_id)
        ).scalar_one()

        if session.status == SessionStatusEnum.COMPLETED.value:
            raise AppException(
                status.HTTP_409_CONFLICT,
                "Cannot void sets of a completed session",
                "Conflict",
            )

        s.is_void = True
        if payload.reason is not None:
            s.void_reason = payload.reason

        try:
            db.commit()
            db.refresh(s)
            return s
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")


# ============================================================================
# FastAPI App & Middleware
# ============================================================================
app = FastAPI(
    title="Workout Sessions & 1RM Calculation API",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)


class JsonValidationASGIMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"].upper()
        if method in ("POST", "PATCH"):
            headers = dict(scope.get("headers", []))
            content_type = headers.get(b"content-type", b"").decode("latin1").lower()

            if headers.get(b"content-length", b"0") != b"0" and not content_type.startswith("application/json"):
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


# ============================================================================
# Routes
# ============================================================================
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


# 2. GET /exercises
@app.get("/exercises", tags=["Exercises"], response_model=ExerciseListResponse)
def list_exercises(
    active: Optional[bool] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ExerciseController.list_exercises(db, current_user, active, limit, after_id)


# 3. POST /exercises
@app.post(
    "/exercises",
    tags=["Exercises"],
    response_model=ExerciseResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_exercise(
    payload: ExerciseCreateRequest,
    response: Response,
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ex = ExerciseController.create_exercise(db, current_user, payload)
    response.headers["Location"] = f"/exercises/{ex.id}"
    return ExerciseResponse.model_validate(ex)


# 4. GET /exercises/{exercise_id}/one-rm (declare before /exercises/{exercise_id})
@app.get(
    "/exercises/{exercise_id}/one-rm",
    tags=["Exercises"],
    response_model=OneRepMaxResponse,
)
def get_exercise_one_rm(
    exercise_id: int,
    user_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ExerciseController.calculate_one_rm(db, current_user, exercise_id, user_id)


# 5. GET /exercises/{exercise_id}
@app.get("/exercises/{exercise_id}", tags=["Exercises"], response_model=ExerciseResponse)
def get_exercise(
    exercise_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ex = ExerciseController.get_exercise(db, current_user, exercise_id)
    return ExerciseResponse.model_validate(ex)


# 6. PATCH /exercises/{exercise_id}
@app.patch("/exercises/{exercise_id}", tags=["Exercises"], response_model=ExerciseResponse)
def patch_exercise(
    exercise_id: int,
    payload: ExercisePatchRequest,
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ex = ExerciseController.patch_exercise(db, current_user, exercise_id, payload)
    return ExerciseResponse.model_validate(ex)


# 7. DELETE /exercises/{exercise_id}
@app.delete(
    "/exercises/{exercise_id}",
    tags=["Exercises"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_exercise(
    exercise_id: int,
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ExerciseController.delete_exercise(db, current_user, exercise_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 8. GET /workout-sessions
@app.get("/workout-sessions", tags=["Workout Sessions"], response_model=WorkoutSessionListResponse)
def list_workout_sessions(
    user_id: Optional[int] = Query(None, gt=0),
    status: Optional[SessionStatusEnum] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return WorkoutSessionController.list_sessions(db, current_user, user_id, status, limit, after_id)


# 9. POST /workout-sessions
@app.post(
    "/workout-sessions",
    tags=["Workout Sessions"],
    response_model=WorkoutSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_workout_session(
    payload: WorkoutSessionCreateRequest,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sess = WorkoutSessionController.create_session(db, current_user, payload)
    response.headers["Location"] = f"/workout-sessions/{sess.id}"
    return WorkoutSessionResponse.model_validate(sess)


# 10. POST /workout-sessions/{workoutsession_id}/complete
@app.post(
    "/workout-sessions/{workoutsession_id}/complete",
    tags=["Workout Sessions"],
    response_model=WorkoutSessionResponse,
)
def complete_workout_session(
    workoutsession_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sess = WorkoutSessionController.complete_session(db, current_user, workoutsession_id)
    return WorkoutSessionResponse.model_validate(sess)


# 11. GET /workout-sessions/{workoutsession_id}/volume
@app.get(
    "/workout-sessions/{workoutsession_id}/volume",
    tags=["Workout Sessions"],
    response_model=SessionVolumeResponse,
)
def get_workout_session_volume(
    workoutsession_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return WorkoutSessionController.calculate_volume(db, current_user, workoutsession_id)


# 12. GET /workout-sessions/{workoutsession_id}
@app.get(
    "/workout-sessions/{workoutsession_id}",
    tags=["Workout Sessions"],
    response_model=WorkoutSessionResponse,
)
def get_workout_session(
    workoutsession_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sess = WorkoutSessionController.get_session(db, current_user, workoutsession_id)
    return WorkoutSessionResponse.model_validate(sess)


# 13. PATCH /workout-sessions/{workoutsession_id}
@app.patch(
    "/workout-sessions/{workoutsession_id}",
    tags=["Workout Sessions"],
    response_model=WorkoutSessionResponse,
)
def patch_workout_session(
    workoutsession_id: int,
    payload: WorkoutSessionPatchRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sess = WorkoutSessionController.patch_session(db, current_user, workoutsession_id, payload)
    return WorkoutSessionResponse.model_validate(sess)


# 14. DELETE /workout-sessions/{workoutsession_id}
@app.delete(
    "/workout-sessions/{workoutsession_id}",
    tags=["Workout Sessions"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_workout_session(
    workoutsession_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    WorkoutSessionController.delete_session(db, current_user, workoutsession_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 15. GET /performed-sets
@app.get("/performed-sets", tags=["Performed Sets"], response_model=PerformedSetListResponse)
def list_performed_sets(
    session_id: Optional[int] = Query(None, gt=0),
    exercise_id: Optional[int] = Query(None, gt=0),
    is_void: Optional[bool] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return PerformedSetController.list_sets(
        db, current_user, session_id, exercise_id, is_void, limit, after_id
    )


# 16. POST /performed-sets
@app.post(
    "/performed-sets",
    tags=["Performed Sets"],
    response_model=PerformedSetResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_performed_set(
    payload: PerformedSetCreateRequest,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p_set = PerformedSetController.create_set(db, current_user, payload)
    response.headers["Location"] = f"/performed-sets/{p_set.id}"
    return PerformedSetResponse.model_validate(p_set)


# 17. POST /performed-sets/{performedset_id}/void
@app.post(
    "/performed-sets/{performedset_id}/void",
    tags=["Performed Sets"],
    response_model=PerformedSetResponse,
)
def void_performed_set(
    performedset_id: int,
    payload: PerformedSetVoidRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p_set = PerformedSetController.void_set(db, current_user, performedset_id, payload)
    return PerformedSetResponse.model_validate(p_set)


# 18. GET /performed-sets/{performedset_id}
@app.get("/performed-sets/{performedset_id}", tags=["Performed Sets"], response_model=PerformedSetResponse)
def get_performed_set(
    performedset_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p_set = PerformedSetController.get_set(db, current_user, performedset_id)
    return PerformedSetResponse.model_validate(p_set)


# 19. PATCH /performed-sets/{performedset_id}
@app.patch("/performed-sets/{performedset_id}", tags=["Performed Sets"], response_model=PerformedSetResponse)
def patch_performed_set(
    performedset_id: int,
    payload: PerformedSetPatchRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p_set = PerformedSetController.patch_set(db, current_user, performedset_id, payload)
    return PerformedSetResponse.model_validate(p_set)


# 20. DELETE /performed-sets/{performedset_id}
@app.delete(
    "/performed-sets/{performedset_id}",
    tags=["Performed Sets"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_performed_set(
    performedset_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    PerformedSetController.delete_set(db, current_user, performedset_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
    )
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi
