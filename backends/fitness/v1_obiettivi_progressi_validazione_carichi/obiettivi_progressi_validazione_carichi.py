"""
Fitness Management - Variant C: Goals, progress, and load validation
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
logger = logging.getLogger("fitness_goals")
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


class ExperienceLevelEnum(str, enum.Enum):
    BEGINNER = "BEGINNER"
    INTERMEDIATE = "INTERMEDIATE"
    ADVANCED = "ADVANCED"


class GoalMetricEnum(str, enum.Enum):
    WEIGHT = "WEIGHT"
    BODY_FAT = "BODY_FAT"
    STRENGTH = "STRENGTH"
    DISTANCE = "DISTANCE"
    SESSIONS = "SESSIONS"


class GoalStatusEnum(str, enum.Enum):
    OPEN = "OPEN"
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
    experience_level = Column(
        String(20), nullable=False, default=ExperienceLevelEnum.BEGINNER.value
    )
    token = Column(String(100), unique=True, nullable=False)


class LoadLimitModel(Base):
    __tablename__ = "load_limits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    exercise = Column(String(100), nullable=False, index=True)
    experience_level = Column(String(20), nullable=False, index=True)
    max_kg = Column(Float, nullable=False)
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


class FitnessGoalModel(Base):
    __tablename__ = "fitness_goals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(120), nullable=False)
    metric = Column(String(20), nullable=False)
    target_value = Column(Float, nullable=False)
    current_value = Column(Float, nullable=False, default=0.0)
    exercise = Column(String(100), nullable=True)  # especially for STRENGTH metric
    start_date = Column(DateTime(timezone=True), nullable=False)
    end_date = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False, default=GoalStatusEnum.OPEN.value)
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


class ProgressEventModel(Base):
    __tablename__ = "progress_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    goal_id = Column(Integer, ForeignKey("fitness_goals.id"), nullable=False, index=True)
    value = Column(Float, nullable=False)
    event_date = Column(DateTime(timezone=True), nullable=False)
    exercise = Column(String(100), nullable=True)
    is_validated = Column(Boolean, default=True, nullable=False)
    notes = Column(String(500), nullable=True)
    validated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    validated_at = Column(DateTime(timezone=True), nullable=True)
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


def to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --- Load Limits Schemas ---
class LoadLimitCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exercise: str = Field(..., description="Exercise identifier/name")
    experience_level: ExperienceLevelEnum = Field(..., description="Target experience level")
    max_kg: float = Field(..., ge=0.0, le=500.0, description="Max allowed load in kg (0..500)")

    @field_validator("exercise")
    @classmethod
    def validate_exercise(cls, v: str) -> str:
        v = v.strip()
        if not (2 <= len(v) <= 100):
            raise ValueError("exercise must be between 2 and 100 characters")
        return v


class LoadLimitPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exercise: Optional[str] = None
    experience_level: Optional[ExperienceLevelEnum] = None
    max_kg: Optional[float] = Field(default=None, ge=0.0, le=500.0)

    @field_validator("exercise")
    @classmethod
    def validate_exercise(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (2 <= len(v) <= 100):
            raise ValueError("exercise must be between 2 and 100 characters")
        return v


class LoadLimitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    exercise: str
    experience_level: ExperienceLevelEnum
    max_kg: float
    created_at: datetime
    updated_at: datetime


class LoadLimitListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[LoadLimitResponse]
    limit: int
    next_after_id: Optional[int] = None


class LoadValidationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exercise: str
    experience_level: ExperienceLevelEnum
    load_kg: float
    max_kg: Optional[float]
    is_valid: bool
    exceeds_limit: bool
    requires_admin_validation: bool


# --- Fitness Goals Schemas ---
class FitnessGoalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., description="Goal title (2..120 chars)")
    metric: GoalMetricEnum = Field(..., description="Goal metric")
    target_value: float = Field(..., ge=0.0, le=1000000.0, description="Target value (0..1000000)")
    start_date: datetime = Field(..., description="Start date (UTC 2025..2035)")
    end_date: datetime = Field(..., description="End date (UTC 2025..2035)")
    exercise: Optional[str] = Field(default=None, description="Exercise name/code (for STRENGTH)")
    user_id: Optional[int] = Field(default=None, gt=0, description="User ID (admin only)")

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        v = v.strip()
        if not (2 <= len(v) <= 120):
            raise ValueError("title must be between 2 and 120 characters")
        return v

    @field_validator("exercise")
    @classmethod
    def validate_exercise(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (2 <= len(v) <= 100):
            raise ValueError("exercise must be between 2 and 100 characters")
        return v

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def validate_dates(cls, v: Any) -> datetime:
        return parse_and_validate_timestamp(v)


class FitnessGoalPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    target_value: Optional[float] = Field(default=None, ge=0.0, le=1000000.0)
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    status: Optional[GoalStatusEnum] = None
    exercise: Optional[str] = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (2 <= len(v) <= 120):
            raise ValueError("title must be between 2 and 120 characters")
        return v

    @field_validator("exercise")
    @classmethod
    def validate_exercise(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (2 <= len(v) <= 100):
            raise ValueError("exercise must be between 2 and 100 characters")
        return v

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def validate_dates(cls, v: Any) -> Optional[datetime]:
        if v is None:
            return None
        return parse_and_validate_timestamp(v)


class FitnessGoalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    user_id: int
    title: str
    metric: GoalMetricEnum
    target_value: float
    current_value: float
    exercise: Optional[str]
    start_date: datetime
    end_date: datetime
    status: GoalStatusEnum
    is_deleted: bool
    created_at: datetime
    updated_at: datetime


class FitnessGoalListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[FitnessGoalResponse]
    limit: int
    next_after_id: Optional[int] = None


# --- Progress Events Schemas ---
class ProgressEventCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: int = Field(..., gt=0, description="Referenced Goal ID")
    value: float = Field(..., ge=0.0, le=1000000.0, description="Progress value (0..1000000)")
    event_date: datetime = Field(..., description="Event timestamp (UTC 2025..2035)")
    exercise: Optional[str] = Field(default=None, description="Exercise identifier (optional override)")
    notes: Optional[str] = Field(default=None, max_length=500, description="Optional notes")
    allow_pending: Optional[bool] = Field(
        default=False,
        description="Allow creating unvalidated event if exceeding load limit",
    )

    @field_validator("event_date", mode="before")
    @classmethod
    def validate_date(cls, v: Any) -> datetime:
        return parse_and_validate_timestamp(v)

    @field_validator("exercise")
    @classmethod
    def validate_exercise(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (2 <= len(v) <= 100):
            raise ValueError("exercise must be between 2 and 100 characters")
        return v


class ProgressEventPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Optional[float] = Field(default=None, ge=0.0, le=1000000.0)
    event_date: Optional[datetime] = None
    exercise: Optional[str] = None
    notes: Optional[str] = Field(default=None, max_length=500)

    @field_validator("event_date", mode="before")
    @classmethod
    def validate_date(cls, v: Any) -> Optional[datetime]:
        if v is None:
            return None
        return parse_and_validate_timestamp(v)

    @field_validator("exercise")
    @classmethod
    def validate_exercise(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (2 <= len(v) <= 100):
            raise ValueError("exercise must be between 2 and 100 characters")
        return v


class ProgressEventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    goal_id: int
    value: float
    event_date: datetime
    exercise: Optional[str]
    is_validated: bool
    notes: Optional[str]
    validated_by: Optional[int]
    validated_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class ProgressEventListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[ProgressEventResponse]
    limit: int
    next_after_id: Optional[int] = None


class GoalProgressResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: int
    title: str
    metric: GoalMetricEnum
    target_value: float
    current_value: float
    progress_percentage: float
    status: GoalStatusEnum
    exercise: Optional[str]
    start_date: datetime
    end_date: datetime
    total_events: int
    latest_event_date: Optional[datetime]
    events: List[ProgressEventResponse]


class PersonalRecordItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    metric: GoalMetricEnum
    exercise: Optional[str]
    record_value: float
    achieved_at: datetime
    goal_id: int
    progress_event_id: int


class PersonalRecordsListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[PersonalRecordItem]
    limit: int
    next_after_id: Optional[int] = None


# ============================================================================
# Seed Deterministic Data
# ============================================================================
def init_db():
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        if db.execute(select(UserModel)).first():
            return

        # Seed Users
        user1 = UserModel(
            id=1,
            username="user1",
            role=RoleEnum.USER.value,
            experience_level=ExperienceLevelEnum.BEGINNER.value,
            token="user1-token-secret-12345",
        )
        user2 = UserModel(
            id=2,
            username="user2",
            role=RoleEnum.USER.value,
            experience_level=ExperienceLevelEnum.INTERMEDIATE.value,
            token="user2-token-secret-12345",
        )
        admin = UserModel(
            id=3,
            username="admin",
            role=RoleEnum.ADMIN.value,
            experience_level=ExperienceLevelEnum.ADVANCED.value,
            token="admin-token-secret-12345",
        )
        db.add_all([user1, user2, admin])

        # Seed Load Limits
        limits = [
            LoadLimitModel(
                id=1,
                exercise="bench_press",
                experience_level=ExperienceLevelEnum.BEGINNER.value,
                max_kg=60.0,
            ),
            LoadLimitModel(
                id=2,
                exercise="bench_press",
                experience_level=ExperienceLevelEnum.INTERMEDIATE.value,
                max_kg=100.0,
            ),
            LoadLimitModel(
                id=3,
                exercise="bench_press",
                experience_level=ExperienceLevelEnum.ADVANCED.value,
                max_kg=150.0,
            ),
            LoadLimitModel(
                id=4,
                exercise="squat",
                experience_level=ExperienceLevelEnum.BEGINNER.value,
                max_kg=80.0,
            ),
            LoadLimitModel(
                id=5,
                exercise="squat",
                experience_level=ExperienceLevelEnum.INTERMEDIATE.value,
                max_kg=140.0,
            ),
            LoadLimitModel(
                id=6,
                exercise="deadlift",
                experience_level=ExperienceLevelEnum.BEGINNER.value,
                max_kg=100.0,
            ),
        ]
        db.add_all(limits)

        # Seed Goals
        g1 = FitnessGoalModel(
            id=1,
            user_id=1,
            title="User1 Bench Goal",
            metric=GoalMetricEnum.STRENGTH.value,
            target_value=80.0,
            current_value=50.0,
            exercise="bench_press",
            start_date=datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            end_date=datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc),
            status=GoalStatusEnum.OPEN.value,
        )
        g2 = FitnessGoalModel(
            id=2,
            user_id=2,
            title="User2 Running Goal",
            metric=GoalMetricEnum.DISTANCE.value,
            target_value=500.0,
            current_value=120.0,
            exercise=None,
            start_date=datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            end_date=datetime(2025, 6, 30, 23, 59, 59, tzinfo=timezone.utc),
            status=GoalStatusEnum.OPEN.value,
        )
        db.add_all([g1, g2])

        # Seed Progress Events
        e1 = ProgressEventModel(
            id=1,
            goal_id=1,
            value=50.0,
            event_date=datetime(2025, 2, 1, 10, 0, 0, tzinfo=timezone.utc),
            exercise="bench_press",
            is_validated=True,
            notes="Initial test",
            validated_by=3,
            validated_at=datetime(2025, 2, 1, 10, 5, 0, tzinfo=timezone.utc),
        )
        e2 = ProgressEventModel(
            id=2,
            goal_id=2,
            value=120.0,
            event_date=datetime(2025, 2, 15, 9, 0, 0, tzinfo=timezone.utc),
            exercise=None,
            is_validated=True,
            notes="Morning run",
            validated_by=3,
            validated_at=datetime(2025, 2, 15, 9, 5, 0, tzinfo=timezone.utc),
        )
        db.add_all([e1, e2])
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
            "Operation restricted to administrators",
            "Forbidden",
        )
    return current_user


# ============================================================================
# Controllers
# ============================================================================
class LoadLimitController:
    @staticmethod
    def list_limits(
        db: Session,
        current_user: UserModel,
        exercise: Optional[str],
        experience_level: Optional[ExperienceLevelEnum],
        limit: int,
        after_id: Optional[int],
    ) -> LoadLimitListResponse:
        query = select(LoadLimitModel)
        if exercise:
            query = query.where(LoadLimitModel.exercise == exercise)
        if experience_level:
            query = query.where(LoadLimitModel.experience_level == experience_level.value)
        if after_id is not None:
            query = query.where(LoadLimitModel.id > after_id)

        query = query.order_by(LoadLimitModel.id.asc()).limit(limit + 1)
        results = db.execute(query).scalars().all()

        next_after_id = None
        if len(results) > limit:
            items = results[:limit]
            next_after_id = items[-1].id
        else:
            items = results

        return LoadLimitListResponse(
            items=[LoadLimitResponse.model_validate(it) for it in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_limit(
        db: Session,
        current_user: UserModel,
        payload: LoadLimitCreateRequest,
    ) -> LoadLimitModel:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(status.HTTP_403_FORBIDDEN, "Admin required", "Forbidden")

        existing = db.execute(
            select(LoadLimitModel).where(
                LoadLimitModel.exercise == payload.exercise,
                LoadLimitModel.experience_level == payload.experience_level.value,
            )
        ).scalar_one_or_none()
        if existing:
            raise AppException(
                status.HTTP_409_CONFLICT,
                f"Load limit already exists for {payload.exercise} ({payload.experience_level.value})",
                "Conflict",
            )

        limit_obj = LoadLimitModel(
            exercise=payload.exercise,
            experience_level=payload.experience_level.value,
            max_kg=payload.max_kg,
        )
        db.add(limit_obj)
        try:
            db.commit()
            db.refresh(limit_obj)
            return limit_obj
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def get_limit(db: Session, current_user: UserModel, loadlimit_id: int) -> LoadLimitModel:
        if loadlimit_id <= 0:
            raise AppException(status.HTTP_404_NOT_FOUND, "Resource not found", "Not Found")

        limit_obj = db.execute(
            select(LoadLimitModel).where(LoadLimitModel.id == loadlimit_id)
        ).scalar_one_or_none()
        if not limit_obj:
            raise AppException(status.HTTP_404_NOT_FOUND, "Load limit not found", "Not Found")
        return limit_obj

    @staticmethod
    def patch_limit(
        db: Session,
        current_user: UserModel,
        loadlimit_id: int,
        payload: LoadLimitPatchRequest,
    ) -> LoadLimitModel:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(status.HTTP_403_FORBIDDEN, "Admin required", "Forbidden")

        limit_obj = LoadLimitController.get_limit(db, current_user, loadlimit_id)

        target_exercise = payload.exercise if payload.exercise is not None else limit_obj.exercise
        target_level = (
            payload.experience_level.value
            if payload.experience_level is not None
            else limit_obj.experience_level
        )

        if (
            target_exercise != limit_obj.exercise
            or target_level != limit_obj.experience_level
        ):
            dup = db.execute(
                select(LoadLimitModel).where(
                    LoadLimitModel.exercise == target_exercise,
                    LoadLimitModel.experience_level == target_level,
                    LoadLimitModel.id != limit_obj.id,
                )
            ).scalar_one_or_none()
            if dup:
                raise AppException(
                    status.HTTP_409_CONFLICT,
                    f"Load limit already exists for {target_exercise} ({target_level})",
                    "Conflict",
                )

        if payload.exercise is not None:
            limit_obj.exercise = payload.exercise
        if payload.experience_level is not None:
            limit_obj.experience_level = payload.experience_level.value
        if payload.max_kg is not None:
            limit_obj.max_kg = payload.max_kg

        try:
            db.commit()
            db.refresh(limit_obj)
            return limit_obj
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def delete_limit(db: Session, current_user: UserModel, loadlimit_id: int) -> None:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(status.HTTP_403_FORBIDDEN, "Admin required", "Forbidden")

        limit_obj = LoadLimitController.get_limit(db, current_user, loadlimit_id)
        db.delete(limit_obj)
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def validate_load(
        db: Session,
        current_user: UserModel,
        exercise: str,
        experience_level: ExperienceLevelEnum,
        load_kg: float,
    ) -> LoadValidationResponse:
        limit_obj = db.execute(
            select(LoadLimitModel).where(
                LoadLimitModel.exercise == exercise,
                LoadLimitModel.experience_level == experience_level.value,
            )
        ).scalar_one_or_none()

        if limit_obj:
            exceeds = load_kg > limit_obj.max_kg
            return LoadValidationResponse(
                exercise=exercise,
                experience_level=experience_level,
                load_kg=load_kg,
                max_kg=limit_obj.max_kg,
                is_valid=not exceeds,
                exceeds_limit=exceeds,
                requires_admin_validation=exceeds,
            )
        else:
            return LoadValidationResponse(
                exercise=exercise,
                experience_level=experience_level,
                load_kg=load_kg,
                max_kg=None,
                is_valid=True,
                exceeds_limit=False,
                requires_admin_validation=False,
            )


class FitnessGoalController:
    @staticmethod
    def list_goals(
        db: Session,
        current_user: UserModel,
        user_id: Optional[int],
        metric: Optional[GoalMetricEnum],
        goal_status: Optional[GoalStatusEnum],
        limit: int,
        after_id: Optional[int],
    ) -> FitnessGoalListResponse:
        query = select(FitnessGoalModel).where(FitnessGoalModel.is_deleted.is_(False))

        if current_user.role != RoleEnum.ADMIN.value:
            query = query.where(FitnessGoalModel.user_id == current_user.id)
        elif user_id is not None:
            query = query.where(FitnessGoalModel.user_id == user_id)

        if metric:
            query = query.where(FitnessGoalModel.metric == metric.value)
        if goal_status:
            query = query.where(FitnessGoalModel.status == goal_status.value)
        if after_id is not None:
            query = query.where(FitnessGoalModel.id > after_id)

        query = query.order_by(FitnessGoalModel.id.asc()).limit(limit + 1)
        results = db.execute(query).scalars().all()

        next_after_id = None
        if len(results) > limit:
            items = results[:limit]
            next_after_id = items[-1].id
        else:
            items = results

        return FitnessGoalListResponse(
            items=[FitnessGoalResponse.model_validate(it) for it in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_goal(
        db: Session,
        current_user: UserModel,
        payload: FitnessGoalCreateRequest,
    ) -> FitnessGoalModel:
        if payload.start_date > payload.end_date:
            raise AppException(
                status.HTTP_400_BAD_REQUEST,
                "start_date must be less than or equal to end_date",
                "Bad Request",
            )

        target_user_id = current_user.id
        if payload.user_id is not None:
            if current_user.role != RoleEnum.ADMIN.value and payload.user_id != current_user.id:
                raise AppException(
                    status.HTTP_403_FORBIDDEN,
                    "Users cannot create goals for other accounts",
                    "Forbidden",
                )
            target_user = db.execute(
                select(UserModel).where(UserModel.id == payload.user_id)
            ).scalar_one_or_none()
            if not target_user:
                raise AppException(status.HTTP_404_NOT_FOUND, "Target user not found", "Not Found")
            target_user_id = target_user.id

        goal = FitnessGoalModel(
            user_id=target_user_id,
            title=payload.title,
            metric=payload.metric.value,
            target_value=payload.target_value,
            current_value=0.0,
            exercise=payload.exercise,
            start_date=payload.start_date,
            end_date=payload.end_date,
            status=GoalStatusEnum.OPEN.value,
        )
        db.add(goal)
        try:
            db.commit()
            db.refresh(goal)
            return goal
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def get_goal(db: Session, current_user: UserModel, fitnessgoal_id: int) -> FitnessGoalModel:
        if fitnessgoal_id <= 0:
            raise AppException(status.HTTP_404_NOT_FOUND, "Resource not found", "Not Found")

        goal = db.execute(
            select(FitnessGoalModel).where(
                FitnessGoalModel.id == fitnessgoal_id,
                FitnessGoalModel.is_deleted.is_(False),
            )
        ).scalar_one_or_none()

        if not goal:
            raise AppException(status.HTTP_404_NOT_FOUND, "Goal not found", "Not Found")

        if current_user.role != RoleEnum.ADMIN.value and goal.user_id != current_user.id:
            raise AppException(status.HTTP_403_FORBIDDEN, "Access forbidden", "Forbidden")

        return goal

    @staticmethod
    def patch_goal(
        db: Session,
        current_user: UserModel,
        fitnessgoal_id: int,
        payload: FitnessGoalPatchRequest,
    ) -> FitnessGoalModel:
        goal = FitnessGoalController.get_goal(db, current_user, fitnessgoal_id)

        new_start = to_utc(payload.start_date or goal.start_date)
        new_end = to_utc(payload.end_date or goal.end_date)
        if new_start > new_end:
            raise AppException(
                status.HTTP_400_BAD_REQUEST,
                "start_date must be less than or equal to end_date",
                "Bad Request",
            )

        if payload.title is not None:
            goal.title = payload.title
        if payload.target_value is not None:
            goal.target_value = payload.target_value
        if payload.start_date is not None:
            goal.start_date = payload.start_date
        if payload.end_date is not None:
            goal.end_date = payload.end_date
        if payload.status is not None:
            goal.status = payload.status.value
        if payload.exercise is not None:
            goal.exercise = payload.exercise

        try:
            db.commit()
            db.refresh(goal)
            return goal
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def delete_goal(db: Session, current_user: UserModel, fitnessgoal_id: int) -> None:
        goal = FitnessGoalController.get_goal(db, current_user, fitnessgoal_id)

        # Relation constraint: if goal has recorded progress events, cannot delete to retain history
        event_count = db.execute(
            select(ProgressEventModel).where(ProgressEventModel.goal_id == goal.id)
        ).scalars().all()
        if event_count:
            raise AppException(
                status.HTTP_409_CONFLICT,
                "Cannot delete goal with recorded progress events; goal history is retained",
                "Conflict",
            )

        goal.is_deleted = True
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def get_goal_progress(
        db: Session,
        current_user: UserModel,
        fitnessgoal_id: int,
    ) -> GoalProgressResponse:
        goal = FitnessGoalController.get_goal(db, current_user, fitnessgoal_id)

        events = db.execute(
            select(ProgressEventModel)
            .where(ProgressEventModel.goal_id == goal.id)
            .order_by(ProgressEventModel.event_date.asc())
        ).scalars().all()

        validated_events = [e for e in events if e.is_validated]

        # Calculate progress current value
        current_val = 0.0
        if validated_events:
            if goal.metric == GoalMetricEnum.STRENGTH.value:
                current_val = max(e.value for e in validated_events)
            elif goal.metric in (
                GoalMetricEnum.DISTANCE.value,
                GoalMetricEnum.SESSIONS.value,
            ):
                current_val = sum(e.value for e in validated_events)
            else:  # WEIGHT, BODY_FAT: latest reading
                current_val = validated_events[-1].value

        pct = 0.0
        if goal.target_value > 0:
            pct = min(100.0, round((current_val / goal.target_value) * 100.0, 2))

        latest_dt = validated_events[-1].event_date if validated_events else None

        return GoalProgressResponse(
            goal_id=goal.id,
            title=goal.title,
            metric=GoalMetricEnum(goal.metric),
            target_value=goal.target_value,
            current_value=current_val,
            progress_percentage=pct,
            status=GoalStatusEnum(goal.status),
            exercise=goal.exercise,
            start_date=goal.start_date,
            end_date=goal.end_date,
            total_events=len(events),
            latest_event_date=latest_dt,
            events=[ProgressEventResponse.model_validate(e) for e in events],
        )


class ProgressEventController:
    @staticmethod
    def list_events(
        db: Session,
        current_user: UserModel,
        goal_id: Optional[int],
        is_validated: Optional[bool],
        limit: int,
        after_id: Optional[int],
    ) -> ProgressEventListResponse:
        query = select(ProgressEventModel).join(FitnessGoalModel)

        if current_user.role != RoleEnum.ADMIN.value:
            query = query.where(FitnessGoalModel.user_id == current_user.id)

        if goal_id is not None:
            if goal_id <= 0:
                raise AppException(status.HTTP_404_NOT_FOUND, "Invalid goal_id", "Not Found")
            query = query.where(ProgressEventModel.goal_id == goal_id)

        if is_validated is not None:
            query = query.where(ProgressEventModel.is_validated == is_validated)

        if after_id is not None:
            query = query.where(ProgressEventModel.id > after_id)

        query = query.order_by(ProgressEventModel.id.asc()).limit(limit + 1)
        results = db.execute(query).scalars().all()

        next_after_id = None
        if len(results) > limit:
            items = results[:limit]
            next_after_id = items[-1].id
        else:
            items = results

        return ProgressEventListResponse(
            items=[ProgressEventResponse.model_validate(it) for it in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_event(
        db: Session,
        current_user: UserModel,
        payload: ProgressEventCreateRequest,
    ) -> ProgressEventModel:
        if payload.goal_id <= 0:
            raise AppException(status.HTTP_404_NOT_FOUND, "Goal not found", "Not Found")

        goal = db.execute(
            select(FitnessGoalModel).where(
                FitnessGoalModel.id == payload.goal_id,
                FitnessGoalModel.is_deleted.is_(False),
            )
        ).scalar_one_or_none()

        if not goal:
            raise AppException(status.HTTP_404_NOT_FOUND, "Goal not found", "Not Found")

        if current_user.role != RoleEnum.ADMIN.value and goal.user_id != current_user.id:
            raise AppException(status.HTTP_403_FORBIDDEN, "Access forbidden", "Forbidden")

        # REQ-9-006: Closed goals cannot accept new progress events
        if goal.status in (GoalStatusEnum.COMPLETED.value, GoalStatusEnum.CANCELLED.value):
            raise AppException(
                status.HTTP_409_CONFLICT,
                f"Cannot add progress events to a {goal.status} goal",
                "Conflict",
            )

        # REQ-9-003: event_date in goal interval
        if not (to_utc(goal.start_date) <= to_utc(payload.event_date) <= to_utc(goal.end_date)):
            raise AppException(
                status.HTTP_400_BAD_REQUEST,
                f"event_date {payload.event_date.isoformat()} outside goal interval "
                f"[{goal.start_date.isoformat()} - {goal.end_date.isoformat()}]",
                "Bad Request",
            )

        exercise_to_check = payload.exercise or goal.exercise
        owner_user = db.execute(
            select(UserModel).where(UserModel.id == goal.user_id)
        ).scalar_one()

        is_validated = True
        validated_by = current_user.id if current_user.role == RoleEnum.ADMIN.value else None
        validated_at = datetime.now(timezone.utc) if current_user.role == RoleEnum.ADMIN.value else None

        # REQ-9-005: Strength events exceeding a limit are rejected unless explicitly validated by admin
        if goal.metric == GoalMetricEnum.STRENGTH.value or exercise_to_check:
            if exercise_to_check:
                limit_obj = db.execute(
                    select(LoadLimitModel).where(
                        LoadLimitModel.exercise == exercise_to_check,
                        LoadLimitModel.experience_level == owner_user.experience_level,
                    )
                ).scalar_one_or_none()

                if limit_obj and payload.value > limit_obj.max_kg:
                    if current_user.role == RoleEnum.ADMIN.value:
                        is_validated = True
                    else:
                        if payload.allow_pending:
                            is_validated = False
                        else:
                            raise AppException(
                                status.HTTP_409_CONFLICT,
                                f"Strength load {payload.value} kg exceeds load limit of {limit_obj.max_kg} kg "
                                f"for {exercise_to_check} ({owner_user.experience_level}). Admin validation required.",
                                "Conflict",
                            )

        event = ProgressEventModel(
            goal_id=goal.id,
            value=payload.value,
            event_date=payload.event_date,
            exercise=exercise_to_check,
            is_validated=is_validated,
            notes=payload.notes,
            validated_by=validated_by,
            validated_at=validated_at,
        )
        db.add(event)

        # Update goal current value if validated
        if is_validated:
            if goal.metric == GoalMetricEnum.STRENGTH.value:
                goal.current_value = max(goal.current_value, payload.value)
            elif goal.metric in (
                GoalMetricEnum.DISTANCE.value,
                GoalMetricEnum.SESSIONS.value,
            ):
                goal.current_value += payload.value
            else:
                goal.current_value = payload.value

        try:
            db.commit()
            db.refresh(event)
            return event
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def get_event(db: Session, current_user: UserModel, progressevent_id: int) -> ProgressEventModel:
        if progressevent_id <= 0:
            raise AppException(status.HTTP_404_NOT_FOUND, "Resource not found", "Not Found")

        event = db.execute(
            select(ProgressEventModel).where(ProgressEventModel.id == progressevent_id)
        ).scalar_one_or_none()

        if not event:
            raise AppException(status.HTTP_404_NOT_FOUND, "Progress event not found", "Not Found")

        goal = db.execute(
            select(FitnessGoalModel).where(FitnessGoalModel.id == event.goal_id)
        ).scalar_one_or_none()

        if current_user.role != RoleEnum.ADMIN.value and goal and goal.user_id != current_user.id:
            raise AppException(status.HTTP_403_FORBIDDEN, "Access forbidden", "Forbidden")

        return event

    @staticmethod
    def patch_event(
        db: Session,
        current_user: UserModel,
        progressevent_id: int,
        payload: ProgressEventPatchRequest,
    ) -> ProgressEventModel:
        event = ProgressEventController.get_event(db, current_user, progressevent_id)
        goal = db.execute(
            select(FitnessGoalModel).where(FitnessGoalModel.id == event.goal_id)
        ).scalar_one()

        if goal.status in (GoalStatusEnum.COMPLETED.value, GoalStatusEnum.CANCELLED.value):
            raise AppException(
                status.HTTP_409_CONFLICT,
                f"Cannot modify progress events of a {goal.status} goal",
                "Conflict",
            )

        if payload.event_date is not None:
            if not (to_utc(goal.start_date) <= to_utc(payload.event_date) <= to_utc(goal.end_date)):
                raise AppException(
                    status.HTTP_400_BAD_REQUEST,
                    f"event_date {payload.event_date.isoformat()} outside goal interval",
                    "Bad Request",
                )
            event.event_date = payload.event_date

        if payload.exercise is not None:
            event.exercise = payload.exercise

        if payload.notes is not None:
            event.notes = payload.notes

        if payload.value is not None:
            # Check limit if value changed
            exercise_to_check = event.exercise or goal.exercise
            owner_user = db.execute(
                select(UserModel).where(UserModel.id == goal.user_id)
            ).scalar_one()

            if (goal.metric == GoalMetricEnum.STRENGTH.value or exercise_to_check) and exercise_to_check:
                limit_obj = db.execute(
                    select(LoadLimitModel).where(
                        LoadLimitModel.exercise == exercise_to_check,
                        LoadLimitModel.experience_level == owner_user.experience_level,
                    )
                ).scalar_one_or_none()

                if limit_obj and payload.value > limit_obj.max_kg:
                    if current_user.role != RoleEnum.ADMIN.value:
                        raise AppException(
                            status.HTTP_409_CONFLICT,
                            f"Load {payload.value} kg exceeds limit of {limit_obj.max_kg} kg",
                            "Conflict",
                        )
            event.value = payload.value

        try:
            db.commit()
            db.refresh(event)
            return event
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def delete_event(db: Session, current_user: UserModel, progressevent_id: int) -> None:
        event = ProgressEventController.get_event(db, current_user, progressevent_id)
        goal = db.execute(
            select(FitnessGoalModel).where(FitnessGoalModel.id == event.goal_id)
        ).scalar_one()

        # REQ-9-006: Cannot delete events of closed goals
        if goal.status in (GoalStatusEnum.COMPLETED.value, GoalStatusEnum.CANCELLED.value):
            raise AppException(
                status.HTTP_409_CONFLICT,
                "Cannot delete progress events for closed goals; progress history is retained",
                "Conflict",
            )

        db.delete(event)
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def validate_event(
        db: Session,
        current_user: UserModel,
        progressevent_id: int,
    ) -> ProgressEventModel:
        if current_user.role != RoleEnum.ADMIN.value:
            raise AppException(
                status.HTTP_403_FORBIDDEN,
                "Only administrators can validate progress events",
                "Forbidden",
            )

        if progressevent_id <= 0:
            raise AppException(status.HTTP_404_NOT_FOUND, "Resource not found", "Not Found")

        event = db.execute(
            select(ProgressEventModel).where(ProgressEventModel.id == progressevent_id)
        ).scalar_one_or_none()
        if not event:
            raise AppException(status.HTTP_404_NOT_FOUND, "Progress event not found", "Not Found")

        event.is_validated = True
        event.validated_by = current_user.id
        event.validated_at = datetime.now(timezone.utc)

        goal = db.execute(
            select(FitnessGoalModel).where(FitnessGoalModel.id == event.goal_id)
        ).scalar_one_or_none()
        if goal:
            if goal.metric == GoalMetricEnum.STRENGTH.value:
                goal.current_value = max(goal.current_value, event.value)
            elif goal.metric in (
                GoalMetricEnum.DISTANCE.value,
                GoalMetricEnum.SESSIONS.value,
            ):
                goal.current_value += event.value
            else:
                goal.current_value = event.value

        try:
            db.commit()
            db.refresh(event)
            return event
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def get_personal_records(
        db: Session,
        current_user: UserModel,
        user_id: Optional[int],
        metric: Optional[GoalMetricEnum],
        exercise: Optional[str],
        limit: int,
        after_id: Optional[int],
    ) -> PersonalRecordsListResponse:
        query = (
            select(ProgressEventModel, FitnessGoalModel)
            .join(FitnessGoalModel, ProgressEventModel.goal_id == FitnessGoalModel.id)
            .where(
                ProgressEventModel.is_validated.is_(True),
                FitnessGoalModel.is_deleted.is_(False),
            )
        )

        if current_user.role != RoleEnum.ADMIN.value:
            query = query.where(FitnessGoalModel.user_id == current_user.id)
        elif user_id is not None:
            query = query.where(FitnessGoalModel.user_id == user_id)

        if metric:
            query = query.where(FitnessGoalModel.metric == metric.value)
        if exercise:
            query = query.where(
                (ProgressEventModel.exercise == exercise)
                | (FitnessGoalModel.exercise == exercise)
            )

        query = query.order_by(ProgressEventModel.id.asc())
        rows = db.execute(query).all()

        # Aggregate records: best value per (user_id, metric, exercise)
        best_records: Dict[tuple, tuple[ProgressEventModel, FitnessGoalModel]] = {}
        for ev, g in rows:
            ex_key = ev.exercise or g.exercise or "general"
            key = (g.user_id, g.metric, ex_key)
            if key not in best_records:
                best_records[key] = (ev, g)
            else:
                curr_ev, curr_g = best_records[key]
                if g.metric in (GoalMetricEnum.WEIGHT.value, GoalMetricEnum.BODY_FAT.value):
                    if ev.value < curr_ev.value:
                        best_records[key] = (ev, g)
                else:
                    if ev.value > curr_ev.value:
                        best_records[key] = (ev, g)

        record_items: List[PersonalRecordItem] = []
        for (uid, m_str, ex_k), (ev, g) in sorted(
            best_records.items(), key=lambda x: x[1][0].id
        ):
            if after_id is not None and ev.id <= after_id:
                continue
            record_items.append(
                PersonalRecordItem(
                    user_id=g.user_id,
                    metric=GoalMetricEnum(g.metric),
                    exercise=ev.exercise or g.exercise,
                    record_value=ev.value,
                    achieved_at=ev.event_date,
                    goal_id=g.id,
                    progress_event_id=ev.id,
                )
            )

        next_after_id = None
        if len(record_items) > limit:
            items = record_items[:limit]
            next_after_id = items[-1].progress_event_id
        else:
            items = record_items

        return PersonalRecordsListResponse(
            items=items,
            limit=limit,
            next_after_id=next_after_id,
        )


# ============================================================================
# FastAPI App & Middleware
# ============================================================================
app = FastAPI(
    title="Fitness Goals & Load Validation API",
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


@app.on_event("startup")
def on_startup():
    init_db()


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


# 2. GET /fitness-goals
@app.get("/fitness-goals", tags=["Fitness Goals"], response_model=FitnessGoalListResponse)
def list_fitness_goals(
    user_id: Optional[int] = Query(None, gt=0),
    metric: Optional[GoalMetricEnum] = Query(None),
    status: Optional[GoalStatusEnum] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return FitnessGoalController.list_goals(
        db, current_user, user_id, metric, status, limit, after_id
    )


# 3. POST /fitness-goals
@app.post(
    "/fitness-goals",
    tags=["Fitness Goals"],
    response_model=FitnessGoalResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_fitness_goal(
    payload: FitnessGoalCreateRequest,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    goal = FitnessGoalController.create_goal(db, current_user, payload)
    response.headers["Location"] = f"/fitness-goals/{goal.id}"
    return FitnessGoalResponse.model_validate(goal)


# 4. GET /fitness-goals/{fitnessgoal_id}
@app.get("/fitness-goals/{fitnessgoal_id}", tags=["Fitness Goals"], response_model=FitnessGoalResponse)
def get_fitness_goal(
    fitnessgoal_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    goal = FitnessGoalController.get_goal(db, current_user, fitnessgoal_id)
    return FitnessGoalResponse.model_validate(goal)


# 5. PATCH /fitness-goals/{fitnessgoal_id}
@app.patch("/fitness-goals/{fitnessgoal_id}", tags=["Fitness Goals"], response_model=FitnessGoalResponse)
def patch_fitness_goal(
    fitnessgoal_id: int,
    payload: FitnessGoalPatchRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    goal = FitnessGoalController.patch_goal(db, current_user, fitnessgoal_id, payload)
    return FitnessGoalResponse.model_validate(goal)


# 6. DELETE /fitness-goals/{fitnessgoal_id}
@app.delete(
    "/fitness-goals/{fitnessgoal_id}",
    tags=["Fitness Goals"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_fitness_goal(
    fitnessgoal_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    FitnessGoalController.delete_goal(db, current_user, fitnessgoal_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 7. GET /progress-events
@app.get("/progress-events", tags=["Progress Events"], response_model=ProgressEventListResponse)
def list_progress_events(
    goal_id: Optional[int] = Query(None, gt=0),
    is_validated: Optional[bool] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ProgressEventController.list_events(
        db, current_user, goal_id, is_validated, limit, after_id
    )


# 8. POST /progress-events
@app.post(
    "/progress-events",
    tags=["Progress Events"],
    response_model=ProgressEventResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_progress_event(
    payload: ProgressEventCreateRequest,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    event = ProgressEventController.create_event(db, current_user, payload)
    response.headers["Location"] = f"/progress-events/{event.id}"
    return ProgressEventResponse.model_validate(event)


# 9. GET /progress-events/{progressevent_id}
@app.get("/progress-events/{progressevent_id}", tags=["Progress Events"], response_model=ProgressEventResponse)
def get_progress_event(
    progressevent_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    event = ProgressEventController.get_event(db, current_user, progressevent_id)
    return ProgressEventResponse.model_validate(event)


# 10. PATCH /progress-events/{progressevent_id}
@app.patch("/progress-events/{progressevent_id}", tags=["Progress Events"], response_model=ProgressEventResponse)
def patch_progress_event(
    progressevent_id: int,
    payload: ProgressEventPatchRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    event = ProgressEventController.patch_event(db, current_user, progressevent_id, payload)
    return ProgressEventResponse.model_validate(event)


# 11. DELETE /progress-events/{progressevent_id}
@app.delete(
    "/progress-events/{progressevent_id}",
    tags=["Progress Events"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_progress_event(
    progressevent_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ProgressEventController.delete_event(db, current_user, progressevent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 12. GET /load-limits
@app.get("/load-limits", tags=["Load Limits"], response_model=LoadLimitListResponse)
def list_load_limits(
    exercise: Optional[str] = Query(None),
    experience_level: Optional[ExperienceLevelEnum] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return LoadLimitController.list_limits(
        db, current_user, exercise, experience_level, limit, after_id
    )


# 13. POST /load-limits
@app.post(
    "/load-limits",
    tags=["Load Limits"],
    response_model=LoadLimitResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_load_limit(
    payload: LoadLimitCreateRequest,
    response: Response,
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    limit_obj = LoadLimitController.create_limit(db, current_user, payload)
    response.headers["Location"] = f"/load-limits/{limit_obj.id}"
    return LoadLimitResponse.model_validate(limit_obj)


# 14. GET /load-limits/validate
@app.get("/load-limits/validate", tags=["Load Limits"], response_model=LoadValidationResponse)
def validate_load(
    exercise: str = Query(..., min_length=2, max_length=100),
    experience_level: ExperienceLevelEnum = Query(...),
    load_kg: float = Query(..., ge=0.0, le=500.0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return LoadLimitController.validate_load(
        db, current_user, exercise, experience_level, load_kg
    )


# 15. GET /load-limits/{loadlimit_id}
@app.get("/load-limits/{loadlimit_id}", tags=["Load Limits"], response_model=LoadLimitResponse)
def get_load_limit(
    loadlimit_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    limit_obj = LoadLimitController.get_limit(db, current_user, loadlimit_id)
    return LoadLimitResponse.model_validate(limit_obj)


# 16. PATCH /load-limits/{loadlimit_id}
@app.patch("/load-limits/{loadlimit_id}", tags=["Load Limits"], response_model=LoadLimitResponse)
def patch_load_limit(
    loadlimit_id: int,
    payload: LoadLimitPatchRequest,
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    limit_obj = LoadLimitController.patch_limit(db, current_user, loadlimit_id, payload)
    return LoadLimitResponse.model_validate(limit_obj)


# 17. DELETE /load-limits/{loadlimit_id}
@app.delete(
    "/load-limits/{loadlimit_id}",
    tags=["Load Limits"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_load_limit(
    loadlimit_id: int,
    current_user: UserModel = Depends(require_admin),
    db: Session = Depends(get_db),
):
    LoadLimitController.delete_limit(db, current_user, loadlimit_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 18. GET /fitness-goals/{fitnessgoal_id}/progress
@app.get(
    "/fitness-goals/{fitnessgoal_id}/progress",
    tags=["Fitness Goals"],
    response_model=GoalProgressResponse,
)
def get_goal_progress(
    fitnessgoal_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return FitnessGoalController.get_goal_progress(db, current_user, fitnessgoal_id)


# 19. POST /progress-events/{progressevent_id}/validate
@app.post(
    "/progress-events/{progressevent_id}/validate",
    tags=["Progress Events"],
    response_model=ProgressEventResponse,
)
def validate_progress_event(
    progressevent_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    event = ProgressEventController.validate_event(db, current_user, progressevent_id)
    return ProgressEventResponse.model_validate(event)


# 20. GET /personal-records
@app.get("/personal-records", tags=["Personal Records"], response_model=PersonalRecordsListResponse)
def get_personal_records(
    user_id: Optional[int] = Query(None, gt=0),
    metric: Optional[GoalMetricEnum] = Query(None),
    exercise: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ProgressEventController.get_personal_records(
        db, current_user, user_id, metric, exercise, limit, after_id
    )


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
