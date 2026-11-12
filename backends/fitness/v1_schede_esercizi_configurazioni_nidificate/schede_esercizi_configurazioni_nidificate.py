"""
Fitness Management - Variant A: Workout plans, exercises, and nested configurations
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
logger = logging.getLogger("workout_plans")
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


# ============================================================================
# Enums
# ============================================================================
class RoleEnum(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


class ExerciseCategoryEnum(str, enum.Enum):
    CHEST = "CHEST"
    BACK = "BACK"
    LEGS = "LEGS"
    ARMS = "ARMS"
    SHOULDERS = "SHOULDERS"
    CORE = "CORE"
    CARDIO = "CARDIO"
    FULL_BODY = "FULL_BODY"


class PlanStatusEnum(str, enum.Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"


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
    category = Column(String(30), nullable=False, index=True)
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


class WorkoutPlanModel(Base):
    __tablename__ = "workout_plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    description = Column(String(500), nullable=True)
    status = Column(String(20), nullable=False, default=PlanStatusEnum.DRAFT.value)
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


class PlanBlockModel(Base):
    __tablename__ = "plan_blocks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(Integer, ForeignKey("workout_plans.id"), nullable=False, index=True)
    exercise_id = Column(Integer, ForeignKey("exercises.id"), nullable=False, index=True)
    position = Column(Integer, nullable=False)
    notes = Column(String(500), nullable=True)
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


class PlanBlockSetModel(Base):
    __tablename__ = "plan_block_sets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    block_id = Column(Integer, ForeignKey("plan_blocks.id"), nullable=False, index=True)
    set_number = Column(Integer, nullable=False)
    reps = Column(Integer, nullable=False)
    rest_seconds = Column(Integer, nullable=False)
    target_load_kg = Column(Float, nullable=True)


# ============================================================================
# Pydantic Schemas
# ============================================================================
class AppException(HTTPException):
    def __init__(self, status_code: int, detail: str, error_type: str = "Error"):
        super().__init__(status_code=status_code, detail=detail)
        self.error_type = error_type


# --- Nested Set Schema ---
class SetItemSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reps: int = Field(..., ge=1, le=100, description="Reps (1..100)")
    rest_seconds: int = Field(..., ge=0, le=900, description="Rest seconds (0..900)")
    target_load_kg: Optional[float] = Field(
        default=None, ge=0.0, le=500.0, description="Target load in kg (0..500)"
    )


class SetResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    set_number: int
    reps: int
    rest_seconds: int
    target_load_kg: Optional[float] = None


# --- Exercise Schemas ---
class ExerciseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., description="Unique exercise code")
    name: str = Field(..., description="Exercise name (2..120 chars)")
    category: ExerciseCategoryEnum = Field(..., description="Category enum")
    active: Optional[bool] = Field(default=True, description="Active status")

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
    category: Optional[ExerciseCategoryEnum] = None
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
    category: ExerciseCategoryEnum
    active: bool
    created_at: datetime
    updated_at: datetime


class ExerciseListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[ExerciseResponse]
    limit: int
    next_after_id: Optional[int] = None


# --- Workout Plan Schemas ---
class WorkoutPlanCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="Plan name (2..100 chars)")
    description: Optional[str] = Field(default=None, max_length=500)
    status: Optional[PlanStatusEnum] = Field(default=PlanStatusEnum.DRAFT)
    user_id: Optional[int] = Field(default=None, gt=0, description="Owner user ID (admin only)")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not (2 <= len(v) <= 100):
            raise ValueError("name must be between 2 and 100 characters")
        return v


class WorkoutPlanPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    description: Optional[str] = Field(default=None, max_length=500)
    status: Optional[PlanStatusEnum] = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (2 <= len(v) <= 100):
            raise ValueError("name must be between 2 and 100 characters")
        return v


class WorkoutPlanCloneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, description="Optional new name for cloned plan")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (2 <= len(v) <= 100):
            raise ValueError("name must be between 2 and 100 characters")
        return v


class WorkoutPlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    user_id: int
    name: str
    description: Optional[str]
    status: PlanStatusEnum
    created_at: datetime
    updated_at: datetime


class WorkoutPlanListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[WorkoutPlanResponse]
    limit: int
    next_after_id: Optional[int] = None


# --- Plan Block Schemas ---
class PlanBlockCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int = Field(..., gt=0, description="Workout plan ID")
    exercise_id: int = Field(..., gt=0, description="Exercise ID")
    position: Optional[int] = Field(default=None, ge=1, le=100, description="Position (1..100)")
    notes: Optional[str] = Field(default=None, max_length=500)
    sets: List[SetItemSchema] = Field(
        ..., min_length=1, max_length=10, description="Nested sets (1..10 sets)"
    )


class PlanBlockPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exercise_id: Optional[int] = Field(default=None, gt=0)
    position: Optional[int] = Field(default=None, ge=1, le=100)
    notes: Optional[str] = Field(default=None, max_length=500)
    sets: Optional[List[SetItemSchema]] = Field(default=None, min_length=1, max_length=10)


class PlanBlockMoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_position: int = Field(..., ge=1, le=100, description="Target position")


class PlanBlockResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    plan_id: int
    exercise_id: int
    position: int
    notes: Optional[str]
    sets: List[SetResponseSchema]
    created_at: datetime
    updated_at: datetime


class PlanBlockListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[PlanBlockResponse]
    limit: int
    next_after_id: Optional[int] = None


class ConfiguredBlockResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    position: int
    exercise: ExerciseResponse
    notes: Optional[str]
    sets: List[SetResponseSchema]


class WorkoutPlanConfigurationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    name: str
    description: Optional[str]
    status: PlanStatusEnum
    blocks: List[ConfiguredBlockResponse]


class ExercisePlanUsageItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    plan_name: str
    plan_status: PlanStatusEnum
    user_id: int
    block_id: int
    position: int


class ExercisePlanUsageListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[ExercisePlanUsageItem]
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

        # Users
        u1 = UserModel(id=1, username="user1", role=RoleEnum.USER.value, token="user1-token-secret-12345")
        u2 = UserModel(id=2, username="user2", role=RoleEnum.USER.value, token="user2-token-secret-12345")
        admin = UserModel(id=3, username="admin", role=RoleEnum.ADMIN.value, token="admin-token-secret-12345")
        db.add_all([u1, u2, admin])

        # Exercises
        e1 = ExerciseModel(id=1, code="BENCH_PRESS", name="Barbell Bench Press", category=ExerciseCategoryEnum.CHEST.value, active=True)
        e2 = ExerciseModel(id=2, code="BACK_SQUAT", name="Barbell Back Squat", category=ExerciseCategoryEnum.LEGS.value, active=True)
        e3 = ExerciseModel(id=3, code="DEADLIFT", name="Conventional Deadlift", category=ExerciseCategoryEnum.BACK.value, active=True)
        e4 = ExerciseModel(id=4, code="OVERHEAD_PRESS", name="Overhead Press", category=ExerciseCategoryEnum.SHOULDERS.value, active=True)
        e5 = ExerciseModel(id=5, code="BICEP_CURL", name="Barbell Bicep Curl", category=ExerciseCategoryEnum.ARMS.value, active=True)
        db.add_all([e1, e2, e3, e4, e5])

        # Workout Plans
        p1 = WorkoutPlanModel(id=1, user_id=1, name="User1 Push Day", description="Chest & Shoulders", status=PlanStatusEnum.PUBLISHED.value)
        p2 = WorkoutPlanModel(id=2, user_id=1, name="User1 Leg Day", description="Legs focus", status=PlanStatusEnum.DRAFT.value)
        p3 = WorkoutPlanModel(id=3, user_id=2, name="User2 Full Body", description="Full body routine", status=PlanStatusEnum.DRAFT.value)
        db.add_all([p1, p2, p3])

        # Plan Blocks & Sets
        # Block 1 in Plan 1: Bench press, pos 1
        b1 = PlanBlockModel(id=1, plan_id=1, exercise_id=1, position=1, notes="Warm up first")
        # Block 2 in Plan 1: OHP, pos 2
        b2 = PlanBlockModel(id=2, plan_id=1, exercise_id=4, position=2, notes="Strict form")
        # Block 3 in Plan 2: Squat, pos 1
        b3 = PlanBlockModel(id=3, plan_id=2, exercise_id=2, position=1, notes="Deep squats")
        db.add_all([b1, b2, b3])
        db.flush()

        s1_1 = PlanBlockSetModel(block_id=1, set_number=1, reps=10, rest_seconds=90, target_load_kg=60.0)
        s1_2 = PlanBlockSetModel(block_id=1, set_number=2, reps=8, rest_seconds=90, target_load_kg=70.0)
        s1_3 = PlanBlockSetModel(block_id=1, set_number=3, reps=6, rest_seconds=120, target_load_kg=80.0)

        s2_1 = PlanBlockSetModel(block_id=2, set_number=1, reps=10, rest_seconds=60, target_load_kg=40.0)
        s2_2 = PlanBlockSetModel(block_id=2, set_number=2, reps=8, rest_seconds=60, target_load_kg=45.0)

        s3_1 = PlanBlockSetModel(block_id=3, set_number=1, reps=10, rest_seconds=120, target_load_kg=90.0)
        s3_2 = PlanBlockSetModel(block_id=3, set_number=2, reps=10, rest_seconds=120, target_load_kg=90.0)

        db.add_all([s1_1, s1_2, s1_3, s2_1, s2_2, s3_1, s3_2])
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
        category: Optional[ExerciseCategoryEnum],
        active: Optional[bool],
        limit: int,
        after_id: Optional[int],
    ) -> ExerciseListResponse:
        query = select(ExerciseModel).where(ExerciseModel.is_deleted.is_(False))
        if category:
            query = query.where(ExerciseModel.category == category.value)
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
                f"Exercise with code {payload.code} already exists",
                "Conflict",
            )

        ex = ExerciseModel(
            code=payload.code,
            name=payload.name,
            category=payload.category.value,
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
        if payload.category is not None:
            ex.category = payload.category.value
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

        # Relation constraint: cannot delete if used in workout plans
        usages = db.execute(
            select(PlanBlockModel).where(PlanBlockModel.exercise_id == ex.id)
        ).scalars().all()
        if usages:
            raise AppException(
                status.HTTP_409_CONFLICT,
                "Cannot delete exercise currently used in workout plans",
                "Conflict",
            )

        ex.is_deleted = True
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def get_plan_usages(
        db: Session,
        current_user: UserModel,
        exercise_id: int,
        limit: int,
        after_id: Optional[int],
    ) -> ExercisePlanUsageListResponse:
        ex = ExerciseController.get_exercise(db, current_user, exercise_id)

        query = (
            select(PlanBlockModel, WorkoutPlanModel)
            .join(WorkoutPlanModel, PlanBlockModel.plan_id == WorkoutPlanModel.id)
            .where(
                PlanBlockModel.exercise_id == ex.id,
                WorkoutPlanModel.is_deleted.is_(False),
            )
        )

        if current_user.role != RoleEnum.ADMIN.value:
            query = query.where(WorkoutPlanModel.user_id == current_user.id)

        if after_id is not None:
            query = query.where(PlanBlockModel.id > after_id)

        query = query.order_by(PlanBlockModel.id.asc()).limit(limit + 1)
        results = db.execute(query).all()

        next_after_id = None
        if len(results) > limit:
            items = results[:limit]
            next_after_id = items[-1][0].id
        else:
            items = results

        usage_items = [
            ExercisePlanUsageItem(
                plan_id=plan.id,
                plan_name=plan.name,
                plan_status=PlanStatusEnum(plan.status),
                user_id=plan.user_id,
                block_id=block.id,
                position=block.position,
            )
            for block, plan in items
        ]

        return ExercisePlanUsageListResponse(
            items=usage_items,
            limit=limit,
            next_after_id=next_after_id,
        )


class WorkoutPlanController:
    @staticmethod
    def list_plans(
        db: Session,
        current_user: UserModel,
        user_id: Optional[int],
        plan_status: Optional[PlanStatusEnum],
        limit: int,
        after_id: Optional[int],
    ) -> WorkoutPlanListResponse:
        query = select(WorkoutPlanModel).where(WorkoutPlanModel.is_deleted.is_(False))

        if current_user.role != RoleEnum.ADMIN.value:
            query = query.where(WorkoutPlanModel.user_id == current_user.id)
        elif user_id is not None:
            query = query.where(WorkoutPlanModel.user_id == user_id)

        if plan_status:
            query = query.where(WorkoutPlanModel.status == plan_status.value)

        if after_id is not None:
            query = query.where(WorkoutPlanModel.id > after_id)

        query = query.order_by(WorkoutPlanModel.id.asc()).limit(limit + 1)
        results = db.execute(query).scalars().all()

        next_after_id = None
        if len(results) > limit:
            items = results[:limit]
            next_after_id = items[-1].id
        else:
            items = results

        return WorkoutPlanListResponse(
            items=[WorkoutPlanResponse.model_validate(it) for it in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_plan(
        db: Session,
        current_user: UserModel,
        payload: WorkoutPlanCreateRequest,
    ) -> WorkoutPlanModel:
        target_user_id = current_user.id
        if payload.user_id is not None:
            if current_user.role != RoleEnum.ADMIN.value and payload.user_id != current_user.id:
                raise AppException(
                    status.HTTP_403_FORBIDDEN,
                    "Users cannot create workout plans for other accounts",
                    "Forbidden",
                )
            target_user = db.execute(
                select(UserModel).where(UserModel.id == payload.user_id)
            ).scalar_one_or_none()
            if not target_user:
                raise AppException(status.HTTP_404_NOT_FOUND, "Target user not found", "Not Found")
            target_user_id = target_user.id

        plan = WorkoutPlanModel(
            user_id=target_user_id,
            name=payload.name,
            description=payload.description,
            status=payload.status.value if payload.status else PlanStatusEnum.DRAFT.value,
        )
        db.add(plan)
        try:
            db.commit()
            db.refresh(plan)
            return plan
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def get_plan(db: Session, current_user: UserModel, workoutplan_id: int) -> WorkoutPlanModel:
        if workoutplan_id <= 0:
            raise AppException(status.HTTP_404_NOT_FOUND, "Resource not found", "Not Found")

        plan = db.execute(
            select(WorkoutPlanModel).where(
                WorkoutPlanModel.id == workoutplan_id,
                WorkoutPlanModel.is_deleted.is_(False),
            )
        ).scalar_one_or_none()

        if not plan:
            raise AppException(status.HTTP_404_NOT_FOUND, "Workout plan not found", "Not Found")

        if current_user.role != RoleEnum.ADMIN.value and plan.user_id != current_user.id:
            raise AppException(status.HTTP_403_FORBIDDEN, "Access forbidden", "Forbidden")

        return plan

    @staticmethod
    def patch_plan(
        db: Session,
        current_user: UserModel,
        workoutplan_id: int,
        payload: WorkoutPlanPatchRequest,
    ) -> WorkoutPlanModel:
        plan = WorkoutPlanController.get_plan(db, current_user, workoutplan_id)

        # REQ-9-006: Published plans are immutable
        if plan.status == PlanStatusEnum.PUBLISHED.value:
            raise AppException(
                status.HTTP_409_CONFLICT,
                "Published plans are immutable; clone to create a mutable draft",
                "Conflict",
            )

        if payload.name is not None:
            plan.name = payload.name
        if payload.description is not None:
            plan.description = payload.description
        if payload.status is not None:
            plan.status = payload.status.value

        try:
            db.commit()
            db.refresh(plan)
            return plan
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def delete_plan(db: Session, current_user: UserModel, workoutplan_id: int) -> None:
        plan = WorkoutPlanController.get_plan(db, current_user, workoutplan_id)

        if plan.status == PlanStatusEnum.PUBLISHED.value:
            raise AppException(
                status.HTTP_409_CONFLICT,
                "Published plans are immutable and cannot be deleted; clone to create a draft",
                "Conflict",
            )

        plan.is_deleted = True
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def clone_plan(
        db: Session,
        current_user: UserModel,
        workoutplan_id: int,
        payload: WorkoutPlanCloneRequest,
    ) -> WorkoutPlanModel:
        source_plan = WorkoutPlanController.get_plan(db, current_user, workoutplan_id)

        cloned_name = payload.name or f"{source_plan.name} (Copy)"

        # Create cloned plan in DRAFT status
        cloned_plan = WorkoutPlanModel(
            user_id=current_user.id,
            name=cloned_name,
            description=source_plan.description,
            status=PlanStatusEnum.DRAFT.value,
        )
        db.add(cloned_plan)
        db.flush()

        # Copy blocks and sets
        source_blocks = db.execute(
            select(PlanBlockModel)
            .where(PlanBlockModel.plan_id == source_plan.id)
            .order_by(PlanBlockModel.position.asc())
        ).scalars().all()

        for sb in source_blocks:
            cb = PlanBlockModel(
                plan_id=cloned_plan.id,
                exercise_id=sb.exercise_id,
                position=sb.position,
                notes=sb.notes,
            )
            db.add(cb)
            db.flush()

            source_sets = db.execute(
                select(PlanBlockSetModel)
                .where(PlanBlockSetModel.block_id == sb.id)
                .order_by(PlanBlockSetModel.set_number.asc())
            ).scalars().all()

            for ss in source_sets:
                cs = PlanBlockSetModel(
                    block_id=cb.id,
                    set_number=ss.set_number,
                    reps=ss.reps,
                    rest_seconds=ss.rest_seconds,
                    target_load_kg=ss.target_load_kg,
                )
                db.add(cs)

        try:
            db.commit()
            db.refresh(cloned_plan)
            return cloned_plan
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def get_configuration(
        db: Session,
        current_user: UserModel,
        workoutplan_id: int,
    ) -> WorkoutPlanConfigurationResponse:
        plan = WorkoutPlanController.get_plan(db, current_user, workoutplan_id)

        blocks = db.execute(
            select(PlanBlockModel)
            .where(PlanBlockModel.plan_id == plan.id)
            .order_by(PlanBlockModel.position.asc())
        ).scalars().all()

        configured_blocks = []
        for b in blocks:
            ex = db.execute(
                select(ExerciseModel).where(ExerciseModel.id == b.exercise_id)
            ).scalar_one()

            sets = db.execute(
                select(PlanBlockSetModel)
                .where(PlanBlockSetModel.block_id == b.id)
                .order_by(PlanBlockSetModel.set_number.asc())
            ).scalars().all()

            configured_blocks.append(
                ConfiguredBlockResponse(
                    id=b.id,
                    position=b.position,
                    exercise=ExerciseResponse.model_validate(ex),
                    notes=b.notes,
                    sets=[SetResponseSchema.model_validate(s) for s in sets],
                )
            )

        return WorkoutPlanConfigurationResponse(
            plan_id=plan.id,
            name=plan.name,
            description=plan.description,
            status=PlanStatusEnum(plan.status),
            blocks=configured_blocks,
        )


class PlanBlockController:
    @staticmethod
    def list_blocks(
        db: Session,
        current_user: UserModel,
        plan_id: Optional[int],
        exercise_id: Optional[int],
        limit: int,
        after_id: Optional[int],
    ) -> PlanBlockListResponse:
        query = select(PlanBlockModel).join(WorkoutPlanModel)

        if current_user.role != RoleEnum.ADMIN.value:
            query = query.where(WorkoutPlanModel.user_id == current_user.id)

        if plan_id is not None:
            if plan_id <= 0:
                raise AppException(status.HTTP_404_NOT_FOUND, "Invalid plan_id", "Not Found")
            query = query.where(PlanBlockModel.plan_id == plan_id)

        if exercise_id is not None:
            if exercise_id <= 0:
                raise AppException(status.HTTP_404_NOT_FOUND, "Invalid exercise_id", "Not Found")
            query = query.where(PlanBlockModel.exercise_id == exercise_id)

        if after_id is not None:
            query = query.where(PlanBlockModel.id > after_id)

        query = query.order_by(PlanBlockModel.id.asc()).limit(limit + 1)
        results = db.execute(query).scalars().all()

        next_after_id = None
        if len(results) > limit:
            items = results[:limit]
            next_after_id = items[-1].id
        else:
            items = results

        block_responses = []
        for b in items:
            sets = db.execute(
                select(PlanBlockSetModel)
                .where(PlanBlockSetModel.block_id == b.id)
                .order_by(PlanBlockSetModel.set_number.asc())
            ).scalars().all()

            resp = PlanBlockResponse(
                id=b.id,
                plan_id=b.plan_id,
                exercise_id=b.exercise_id,
                position=b.position,
                notes=b.notes,
                sets=[SetResponseSchema.model_validate(s) for s in sets],
                created_at=b.created_at,
                updated_at=b.updated_at,
            )
            block_responses.append(resp)

        return PlanBlockListResponse(
            items=block_responses,
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_block(
        db: Session,
        current_user: UserModel,
        payload: PlanBlockCreateRequest,
    ) -> PlanBlockResponse:
        plan = WorkoutPlanController.get_plan(db, current_user, payload.plan_id)

        # REQ-9-006: Published plans are immutable
        if plan.status == PlanStatusEnum.PUBLISHED.value:
            raise AppException(
                status.HTTP_409_CONFLICT,
                "Published plans are immutable; cannot add blocks to published plan",
                "Conflict",
            )

        # Exercise exists and active
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

        # Existing blocks in plan
        existing_blocks = db.execute(
            select(PlanBlockModel).where(PlanBlockModel.plan_id == plan.id)
        ).scalars().all()

        target_position = payload.position
        if target_position is None:
            target_position = len(existing_blocks) + 1
        else:
            # Check unique position or shift
            dup = [b for b in existing_blocks if b.position == target_position]
            if dup:
                # Shift blocks from target_position upwards
                for b in existing_blocks:
                    if b.position >= target_position:
                        b.position += 1

        block = PlanBlockModel(
            plan_id=plan.id,
            exercise_id=ex.id,
            position=target_position,
            notes=payload.notes,
        )
        db.add(block)
        db.flush()

        # Add sets
        created_sets = []
        for idx, set_item in enumerate(payload.sets, start=1):
            s_obj = PlanBlockSetModel(
                block_id=block.id,
                set_number=idx,
                reps=set_item.reps,
                rest_seconds=set_item.rest_seconds,
                target_load_kg=set_item.target_load_kg,
            )
            db.add(s_obj)
            created_sets.append(s_obj)

        try:
            db.commit()
            db.refresh(block)
            for s in created_sets:
                db.refresh(s)

            return PlanBlockResponse(
                id=block.id,
                plan_id=block.plan_id,
                exercise_id=block.exercise_id,
                position=block.position,
                notes=block.notes,
                sets=[SetResponseSchema.model_validate(s) for s in created_sets],
                created_at=block.created_at,
                updated_at=block.updated_at,
            )
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def get_block(db: Session, current_user: UserModel, planblock_id: int) -> PlanBlockResponse:
        if planblock_id <= 0:
            raise AppException(status.HTTP_404_NOT_FOUND, "Resource not found", "Not Found")

        block = db.execute(
            select(PlanBlockModel).where(PlanBlockModel.id == planblock_id)
        ).scalar_one_or_none()
        if not block:
            raise AppException(status.HTTP_404_NOT_FOUND, "Plan block not found", "Not Found")

        plan = db.execute(
            select(WorkoutPlanModel).where(WorkoutPlanModel.id == block.plan_id)
        ).scalar_one()

        if current_user.role != RoleEnum.ADMIN.value and plan.user_id != current_user.id:
            raise AppException(status.HTTP_403_FORBIDDEN, "Access forbidden", "Forbidden")

        sets = db.execute(
            select(PlanBlockSetModel)
            .where(PlanBlockSetModel.block_id == block.id)
            .order_by(PlanBlockSetModel.set_number.asc())
        ).scalars().all()

        return PlanBlockResponse(
            id=block.id,
            plan_id=block.plan_id,
            exercise_id=block.exercise_id,
            position=block.position,
            notes=block.notes,
            sets=[SetResponseSchema.model_validate(s) for s in sets],
            created_at=block.created_at,
            updated_at=block.updated_at,
        )

    @staticmethod
    def patch_block(
        db: Session,
        current_user: UserModel,
        planblock_id: int,
        payload: PlanBlockPatchRequest,
    ) -> PlanBlockResponse:
        if planblock_id <= 0:
            raise AppException(status.HTTP_404_NOT_FOUND, "Resource not found", "Not Found")

        block = db.execute(
            select(PlanBlockModel).where(PlanBlockModel.id == planblock_id)
        ).scalar_one_or_none()
        if not block:
            raise AppException(status.HTTP_404_NOT_FOUND, "Plan block not found", "Not Found")

        plan = db.execute(
            select(WorkoutPlanModel).where(WorkoutPlanModel.id == block.plan_id)
        ).scalar_one()

        if current_user.role != RoleEnum.ADMIN.value and plan.user_id != current_user.id:
            raise AppException(status.HTTP_403_FORBIDDEN, "Access forbidden", "Forbidden")

        # REQ-9-006: Published plans are immutable
        if plan.status == PlanStatusEnum.PUBLISHED.value:
            raise AppException(
                status.HTTP_409_CONFLICT,
                "Published plans are immutable; cannot modify blocks of published plan",
                "Conflict",
            )

        if payload.exercise_id is not None:
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
            block.exercise_id = ex.id

        if payload.notes is not None:
            block.notes = payload.notes

        if payload.position is not None and payload.position != block.position:
            # Check collision
            collision = db.execute(
                select(PlanBlockModel).where(
                    PlanBlockModel.plan_id == plan.id,
                    PlanBlockModel.position == payload.position,
                    PlanBlockModel.id != block.id,
                )
            ).scalar_one_or_none()
            if collision:
                raise AppException(
                    status.HTTP_409_CONFLICT,
                    f"Position {payload.position} already occupied in plan; use /move endpoint to reorder",
                    "Conflict",
                )
            block.position = payload.position

        if payload.sets is not None:
            # Delete old sets and add new
            old_sets = db.execute(
                select(PlanBlockSetModel).where(PlanBlockSetModel.block_id == block.id)
            ).scalars().all()
            for os in old_sets:
                db.delete(os)

            for idx, set_item in enumerate(payload.sets, start=1):
                s_obj = PlanBlockSetModel(
                    block_id=block.id,
                    set_number=idx,
                    reps=set_item.reps,
                    rest_seconds=set_item.rest_seconds,
                    target_load_kg=set_item.target_load_kg,
                )
                db.add(s_obj)

        try:
            db.commit()
            db.refresh(block)
            sets = db.execute(
                select(PlanBlockSetModel)
                .where(PlanBlockSetModel.block_id == block.id)
                .order_by(PlanBlockSetModel.set_number.asc())
            ).scalars().all()

            return PlanBlockResponse(
                id=block.id,
                plan_id=block.plan_id,
                exercise_id=block.exercise_id,
                position=block.position,
                notes=block.notes,
                sets=[SetResponseSchema.model_validate(s) for s in sets],
                created_at=block.created_at,
                updated_at=block.updated_at,
            )
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def delete_block(db: Session, current_user: UserModel, planblock_id: int) -> None:
        if planblock_id <= 0:
            raise AppException(status.HTTP_404_NOT_FOUND, "Resource not found", "Not Found")

        block = db.execute(
            select(PlanBlockModel).where(PlanBlockModel.id == planblock_id)
        ).scalar_one_or_none()
        if not block:
            raise AppException(status.HTTP_404_NOT_FOUND, "Plan block not found", "Not Found")

        plan = db.execute(
            select(WorkoutPlanModel).where(WorkoutPlanModel.id == block.plan_id)
        ).scalar_one()

        if current_user.role != RoleEnum.ADMIN.value and plan.user_id != current_user.id:
            raise AppException(status.HTTP_403_FORBIDDEN, "Access forbidden", "Forbidden")

        # REQ-9-006: Published plans are immutable
        if plan.status == PlanStatusEnum.PUBLISHED.value:
            raise AppException(
                status.HTTP_409_CONFLICT,
                "Published plans are immutable; cannot delete blocks of published plan",
                "Conflict",
            )

        # Delete nested sets
        sets = db.execute(
            select(PlanBlockSetModel).where(PlanBlockSetModel.block_id == block.id)
        ).scalars().all()
        for s in sets:
            db.delete(s)

        deleted_pos = block.position
        db.delete(block)

        # Shift remaining blocks down to keep positions contiguous
        remaining = db.execute(
            select(PlanBlockModel).where(
                PlanBlockModel.plan_id == plan.id,
                PlanBlockModel.position > deleted_pos,
            )
        ).scalars().all()
        for r in remaining:
            r.position -= 1

        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")

    @staticmethod
    def move_block(
        db: Session,
        current_user: UserModel,
        planblock_id: int,
        payload: PlanBlockMoveRequest,
    ) -> PlanBlockResponse:
        if planblock_id <= 0:
            raise AppException(status.HTTP_404_NOT_FOUND, "Resource not found", "Not Found")

        block = db.execute(
            select(PlanBlockModel).where(PlanBlockModel.id == planblock_id)
        ).scalar_one_or_none()
        if not block:
            raise AppException(status.HTTP_404_NOT_FOUND, "Plan block not found", "Not Found")

        plan = db.execute(
            select(WorkoutPlanModel).where(WorkoutPlanModel.id == block.plan_id)
        ).scalar_one()

        if current_user.role != RoleEnum.ADMIN.value and plan.user_id != current_user.id:
            raise AppException(status.HTTP_403_FORBIDDEN, "Access forbidden", "Forbidden")

        # REQ-9-006: Published plans are immutable
        if plan.status == PlanStatusEnum.PUBLISHED.value:
            raise AppException(
                status.HTTP_409_CONFLICT,
                "Published plans are immutable; cannot move blocks of published plan",
                "Conflict",
            )

        all_blocks = db.execute(
            select(PlanBlockModel)
            .where(PlanBlockModel.plan_id == plan.id)
            .order_by(PlanBlockModel.position.asc())
        ).scalars().all()

        total = len(all_blocks)
        target_pos = payload.new_position
        if not (1 <= target_pos <= total):
            raise AppException(
                status.HTTP_400_BAD_REQUEST,
                f"new_position must be between 1 and {total}",
                "Bad Request",
            )

        current_pos = block.position
        if target_pos != current_pos:
            if target_pos > current_pos:
                # Moving down: shift items between current_pos+1 and target_pos down by 1
                for b in all_blocks:
                    if current_pos < b.position <= target_pos:
                        b.position -= 1
            else:
                # Moving up: shift items between target_pos and current_pos-1 up by 1
                for b in all_blocks:
                    if target_pos <= b.position < current_pos:
                        b.position += 1

            block.position = target_pos

        try:
            db.commit()
            db.refresh(block)
            sets = db.execute(
                select(PlanBlockSetModel)
                .where(PlanBlockSetModel.block_id == block.id)
                .order_by(PlanBlockSetModel.set_number.asc())
            ).scalars().all()

            return PlanBlockResponse(
                id=block.id,
                plan_id=block.plan_id,
                exercise_id=block.exercise_id,
                position=block.position,
                notes=block.notes,
                sets=[SetResponseSchema.model_validate(s) for s in sets],
                created_at=block.created_at,
                updated_at=block.updated_at,
            )
        except Exception:
            db.rollback()
            raise AppException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Database write failure")


# ============================================================================
# FastAPI App & Middleware
# ============================================================================
app = FastAPI(
    title="Workout Plans & Exercises API",
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
    category: Optional[ExerciseCategoryEnum] = Query(None),
    active: Optional[bool] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ExerciseController.list_exercises(db, current_user, category, active, limit, after_id)


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


# 4. GET /exercises/{exercise_id}/plan-usages (declare before /exercises/{exercise_id})
@app.get(
    "/exercises/{exercise_id}/plan-usages",
    tags=["Exercises"],
    response_model=ExercisePlanUsageListResponse,
)
def get_exercise_plan_usages(
    exercise_id: int,
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ExerciseController.get_plan_usages(db, current_user, exercise_id, limit, after_id)


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


# 8. GET /workout-plans
@app.get("/workout-plans", tags=["Workout Plans"], response_model=WorkoutPlanListResponse)
def list_workout_plans(
    user_id: Optional[int] = Query(None, gt=0),
    status: Optional[PlanStatusEnum] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return WorkoutPlanController.list_plans(db, current_user, user_id, status, limit, after_id)


# 9. POST /workout-plans
@app.post(
    "/workout-plans",
    tags=["Workout Plans"],
    response_model=WorkoutPlanResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_workout_plan(
    payload: WorkoutPlanCreateRequest,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    plan = WorkoutPlanController.create_plan(db, current_user, payload)
    response.headers["Location"] = f"/workout-plans/{plan.id}"
    return WorkoutPlanResponse.model_validate(plan)


# 10. POST /workout-plans/{workoutplan_id}/clone
@app.post(
    "/workout-plans/{workoutplan_id}/clone",
    tags=["Workout Plans"],
    response_model=WorkoutPlanResponse,
    status_code=status.HTTP_201_CREATED,
)
def clone_workout_plan(
    workoutplan_id: int,
    payload: WorkoutPlanCloneRequest,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cloned = WorkoutPlanController.clone_plan(db, current_user, workoutplan_id, payload)
    response.headers["Location"] = f"/workout-plans/{cloned.id}"
    return WorkoutPlanResponse.model_validate(cloned)


# 11. GET /workout-plans/{workoutplan_id}/configuration
@app.get(
    "/workout-plans/{workoutplan_id}/configuration",
    tags=["Workout Plans"],
    response_model=WorkoutPlanConfigurationResponse,
)
def get_workout_plan_configuration(
    workoutplan_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return WorkoutPlanController.get_configuration(db, current_user, workoutplan_id)


# 12. GET /workout-plans/{workoutplan_id}
@app.get("/workout-plans/{workoutplan_id}", tags=["Workout Plans"], response_model=WorkoutPlanResponse)
def get_workout_plan(
    workoutplan_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    plan = WorkoutPlanController.get_plan(db, current_user, workoutplan_id)
    return WorkoutPlanResponse.model_validate(plan)


# 13. PATCH /workout-plans/{workoutplan_id}
@app.patch("/workout-plans/{workoutplan_id}", tags=["Workout Plans"], response_model=WorkoutPlanResponse)
def patch_workout_plan(
    workoutplan_id: int,
    payload: WorkoutPlanPatchRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    plan = WorkoutPlanController.patch_plan(db, current_user, workoutplan_id, payload)
    return WorkoutPlanResponse.model_validate(plan)


# 14. DELETE /workout-plans/{workoutplan_id}
@app.delete(
    "/workout-plans/{workoutplan_id}",
    tags=["Workout Plans"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_workout_plan(
    workoutplan_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    WorkoutPlanController.delete_plan(db, current_user, workoutplan_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 15. GET /plan-blocks
@app.get("/plan-blocks", tags=["Plan Blocks"], response_model=PlanBlockListResponse)
def list_plan_blocks(
    plan_id: Optional[int] = Query(None, gt=0),
    exercise_id: Optional[int] = Query(None, gt=0),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return PlanBlockController.list_blocks(
        db, current_user, plan_id, exercise_id, limit, after_id
    )


# 16. POST /plan-blocks
@app.post(
    "/plan-blocks",
    tags=["Plan Blocks"],
    response_model=PlanBlockResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_plan_block(
    payload: PlanBlockCreateRequest,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    block = PlanBlockController.create_block(db, current_user, payload)
    response.headers["Location"] = f"/plan-blocks/{block.id}"
    return block


# 17. POST /plan-blocks/{planblock_id}/move
@app.post(
    "/plan-blocks/{planblock_id}/move",
    tags=["Plan Blocks"],
    response_model=PlanBlockResponse,
)
def move_plan_block(
    planblock_id: int,
    payload: PlanBlockMoveRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return PlanBlockController.move_block(db, current_user, planblock_id, payload)


# 18. GET /plan-blocks/{planblock_id}
@app.get("/plan-blocks/{planblock_id}", tags=["Plan Blocks"], response_model=PlanBlockResponse)
def get_plan_block(
    planblock_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return PlanBlockController.get_block(db, current_user, planblock_id)


# 19. PATCH /plan-blocks/{planblock_id}
@app.patch("/plan-blocks/{planblock_id}", tags=["Plan Blocks"], response_model=PlanBlockResponse)
def patch_plan_block(
    planblock_id: int,
    payload: PlanBlockPatchRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return PlanBlockController.patch_block(db, current_user, planblock_id, payload)


# 20. DELETE /plan-blocks/{planblock_id}
@app.delete(
    "/plan-blocks/{planblock_id}",
    tags=["Plan Blocks"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_plan_block(
    planblock_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    PlanBlockController.delete_block(db, current_user, planblock_id)
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
