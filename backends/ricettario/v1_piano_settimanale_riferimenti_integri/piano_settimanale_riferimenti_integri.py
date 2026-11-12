"""
Recipe Management - Variant B: Weekly plan and referential integrity
CASE_ID: 10

Compliant with:
- Python 3.10+, FastAPI, Pydantic v2, SQLAlchemy ORM, SQLite in-memory with StaticPool.
- MVC architecture: SQLAlchemy models, Pydantic v2 schemas, static controllers, thin FastAPI routes.
- Fully autonomous module exporting `app`.
- Rigorous permission checks, atomic transactions, standardized error responses.
"""

from __future__ import annotations

import datetime
import enum
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime as dt, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# -----------------------------------------------------------------------------
# Logging Configuration
# -----------------------------------------------------------------------------
logger = logging.getLogger("ricettario_variant_b")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(handler)

# -----------------------------------------------------------------------------
# Database Setup
# -----------------------------------------------------------------------------
DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

MAX_PAYLOAD_SIZE = 10 * 1024 * 1024  # 10 MiB


# -----------------------------------------------------------------------------
# Helper Security Utilities & Domain Validators
# -----------------------------------------------------------------------------
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# -----------------------------------------------------------------------------
# Enums
# -----------------------------------------------------------------------------
class UserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


class DayOfWeekEnum(str, enum.Enum):
    MONDAY = "MONDAY"
    TUESDAY = "TUESDAY"
    WEDNESDAY = "WEDNESDAY"
    THURSDAY = "THURSDAY"
    FRIDAY = "FRIDAY"
    SATURDAY = "SATURDAY"
    SUNDAY = "SUNDAY"


class MealTypeEnum(str, enum.Enum):
    BREAKFAST = "BREAKFAST"
    LUNCH = "LUNCH"
    DINNER = "DINNER"
    SNACK = "SNACK"


# -----------------------------------------------------------------------------
# SQLAlchemy Models (MVC: Model)
# -----------------------------------------------------------------------------
class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    role = Column(String(20), nullable=False, default=UserRole.USER.value)
    token_hash = Column(String(64), unique=True, nullable=False)
    token = Column(String(128), unique=True, nullable=False)

    meal_plans = relationship("MealPlanModel", back_populates="user", cascade="all, delete-orphan")


class RecipeModel(Base):
    __tablename__ = "recipes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(160), nullable=False, index=True)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    base_servings = Column(Float, nullable=False, default=1.0)
    created_at = Column(DateTime(timezone=True), default=lambda: dt.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: dt.now(timezone.utc),
        onupdate=lambda: dt.now(timezone.utc),
        nullable=False,
    )

    ingredients = relationship("RecipeIngredientItemModel", back_populates="recipe", cascade="all, delete-orphan")
    meal_entries = relationship("MealEntryModel", back_populates="recipe")


class RecipeIngredientItemModel(Base):
    __tablename__ = "recipe_ingredient_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id"), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    quantity = Column(Float, nullable=False)
    unit = Column(String(20), nullable=False)

    recipe = relationship("RecipeModel", back_populates="ingredients")


class MealPlanModel(Base):
    __tablename__ = "meal_plans"
    __table_args__ = (
        UniqueConstraint("user_id", "week_start_date", name="uq_user_week_start"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    week_start_date = Column(Date, nullable=False, index=True)
    note = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: dt.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: dt.now(timezone.utc),
        onupdate=lambda: dt.now(timezone.utc),
        nullable=False,
    )

    user = relationship("UserModel", back_populates="meal_plans")
    entries = relationship("MealEntryModel", back_populates="plan", cascade="all, delete-orphan")


class MealEntryModel(Base):
    __tablename__ = "meal_entries"
    __table_args__ = (
        UniqueConstraint("plan_id", "day", "meal", name="uq_plan_day_meal"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(Integer, ForeignKey("meal_plans.id"), nullable=False, index=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id"), nullable=False, index=True)
    day = Column(String(20), nullable=False)
    meal = Column(String(20), nullable=False)
    servings = Column(Float, nullable=False)  # 0.5..20
    created_at = Column(DateTime(timezone=True), default=lambda: dt.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: dt.now(timezone.utc),
        onupdate=lambda: dt.now(timezone.utc),
        nullable=False,
    )

    plan = relationship("MealPlanModel", back_populates="entries")
    recipe = relationship("RecipeModel", back_populates="meal_entries")


# -----------------------------------------------------------------------------
# Pydantic Schemas (MVC: View / Data Transfer Objects)
# -----------------------------------------------------------------------------
class RecipeIngredientInputSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=120)
    quantity: float = Field(..., gt=0)
    unit: str = Field(..., min_length=1, max_length=20)


class RecipeIngredientResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    quantity: float
    unit: str


class RecipeCreateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=3, max_length=160)
    description: Optional[str] = Field(default=None, max_length=2000)
    is_active: bool = Field(default=True)
    base_servings: float = Field(default=1.0, ge=0.1, le=100.0)
    ingredients: Optional[List[RecipeIngredientInputSchema]] = None


class RecipePatchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=3, max_length=160)
    description: Optional[str] = Field(default=None, max_length=2000)
    is_active: Optional[bool] = None
    base_servings: Optional[float] = Field(default=None, ge=0.1, le=100.0)


class RecipeResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    title: str
    description: Optional[str] = None
    is_active: bool
    base_servings: float
    ingredients: List[RecipeIngredientResponseSchema]
    created_at: dt
    updated_at: dt


class RecipeListResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[RecipeResponseSchema]
    limit: int
    next_after_id: Optional[int] = None


class MealPlanCreateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    week_start_date: date
    note: Optional[str] = Field(default=None, max_length=255)

    @field_validator("week_start_date")
    @classmethod
    def validate_week_start(cls, v: date) -> date:
        if v.weekday() != 0:
            raise ValueError("week_start_date must be a Monday")
        if not (2025 <= v.year <= 2035):
            raise ValueError("week_start_date year must be between 2025 and 2035")
        return v


class MealPlanPatchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: Optional[str] = Field(default=None, max_length=255)


class MealPlanResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    user_id: int
    week_start_date: date
    note: Optional[str] = None
    created_at: dt
    updated_at: dt


class MealPlanListResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[MealPlanResponseSchema]
    limit: int
    next_after_id: Optional[int] = None


class MealEntryCreateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int = Field(..., gt=0)
    recipe_id: int = Field(..., gt=0)
    day: DayOfWeekEnum
    meal: MealTypeEnum
    servings: float = Field(..., ge=0.5, le=20.0)


class MealEntryPatchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_id: Optional[int] = Field(default=None, gt=0)
    day: Optional[DayOfWeekEnum] = None
    meal: Optional[MealTypeEnum] = None
    servings: Optional[float] = Field(default=None, ge=0.5, le=20.0)


class MealEntryResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    plan_id: int
    recipe_id: int
    recipe_title: str
    day: DayOfWeekEnum
    meal: MealTypeEnum
    servings: float
    created_at: dt
    updated_at: dt


class MealEntryListResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[MealEntryResponseSchema]
    limit: int
    next_after_id: Optional[int] = None


class ShoppingListItemSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    total_quantity: float
    unit: str


class ShoppingListResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: int
    week_start_date: date
    items: List[ShoppingListItemSchema]


class CopyWeekSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_week_start_date: date

    @field_validator("target_week_start_date")
    @classmethod
    def validate_week_start(cls, v: date) -> date:
        if v.weekday() != 0:
            raise ValueError("target_week_start_date must be a Monday")
        if not (2025 <= v.year <= 2035):
            raise ValueError("target_week_start_date year must be between 2025 and 2035")
        return v


class MoveMealEntrySchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_day: DayOfWeekEnum
    target_meal: MealTypeEnum


class PlanUsageItemSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_id: int
    plan_id: int
    user_id: int
    week_start_date: date
    day: DayOfWeekEnum
    meal: MealTypeEnum
    servings: float


class PlanUsageListResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_id: int
    items: List[PlanUsageItemSchema]


# -----------------------------------------------------------------------------
# Database Dependency & Seed Data
# -----------------------------------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def seed_database():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(UserModel).first() is not None:
            return

        user1_token = os.environ.get("API_USER_TOKEN", "user1-token-secret-12345")
        user2_token = os.environ.get("API_SECOND_USER_TOKEN", "user2-token-secret-12345")
        admin_token = os.environ.get("API_ADMIN_TOKEN", "admin-token-secret-12345")

        u1 = UserModel(
            username="user1",
            role=UserRole.USER.value,
            token=user1_token,
            token_hash=hash_token(user1_token),
        )
        u2 = UserModel(
            username="user2",
            role=UserRole.USER.value,
            token=user2_token,
            token_hash=hash_token(user2_token),
        )
        adm = UserModel(
            username="admin",
            role=UserRole.ADMIN.value,
            token=admin_token,
            token_hash=hash_token(admin_token),
        )
        db.add_all([u1, u2, adm])
        db.flush()

        # Seed recipes
        r1 = RecipeModel(
            title="Oatmeal with Blueberries",
            description="Classic morning oatmeal with fresh blueberries",
            is_active=True,
            base_servings=1.0,
        )
        r2 = RecipeModel(
            title="Grilled Salmon Bowl",
            description="Grilled salmon with quinoa and greens",
            is_active=True,
            base_servings=1.0,
        )
        db.add_all([r1, r2])
        db.flush()

        # Seed recipe ingredients
        ing1 = RecipeIngredientItemModel(recipe_id=r1.id, name="Rolled Oats", quantity=80.0, unit="G")
        ing2 = RecipeIngredientItemModel(recipe_id=r1.id, name="Blueberries", quantity=50.0, unit="G")
        ing3 = RecipeIngredientItemModel(recipe_id=r2.id, name="Salmon Fillet", quantity=200.0, unit="G")
        ing4 = RecipeIngredientItemModel(recipe_id=r2.id, name="Quinoa", quantity=100.0, unit="G")
        db.add_all([ing1, ing2, ing3, ing4])
        db.flush()

        # Seed meal plan for user1 (Monday, 2025-09-01)
        plan1 = MealPlanModel(
            user_id=u1.id,
            week_start_date=date(2025, 9, 1),
            note="September week 1 meal plan",
        )
        db.add(plan1)
        db.flush()

        # Seed meal entry
        entry1 = MealEntryModel(
            plan_id=plan1.id,
            recipe_id=r1.id,
            day=DayOfWeekEnum.MONDAY.value,
            meal=MealTypeEnum.BREAKFAST.value,
            servings=1.0,
        )
        entry2 = MealEntryModel(
            plan_id=plan1.id,
            recipe_id=r2.id,
            day=DayOfWeekEnum.MONDAY.value,
            meal=MealTypeEnum.LUNCH.value,
            servings=2.0,
        )
        db.add_all([entry1, entry2])

        db.commit()
        logger.info("Database seeded successfully.")
    except Exception as e:
        db.rollback()
        logger.error(f"Error seeding database: {e}")
        raise
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed_database()
    yield


# -----------------------------------------------------------------------------
# Security & Authentication Dependency
# -----------------------------------------------------------------------------
def get_current_user(
    authorization: Optional[str] = Header(None, alias="Authorization"),
    db: Session = Depends(get_db),
) -> UserModel:
    if not authorization:
        raise StarletteHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
        )

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise StarletteHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format. Must be Bearer <token>",
        )

    raw_token = parts[1]
    thash = hash_token(raw_token)
    user = db.query(UserModel).filter(UserModel.token_hash == thash).first()
    if not user:
        raise StarletteHTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )
    return user


def require_admin(user: UserModel = Depends(get_current_user)) -> UserModel:
    if user.role != UserRole.ADMIN.value:
        raise StarletteHTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operation requires admin privileges",
        )
    return user


# -----------------------------------------------------------------------------
# Pure ASGI Middleware
# -----------------------------------------------------------------------------
class JsonValidationASGIMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"].upper()
        headers = dict(scope.get("headers", []))
        content_type = headers.get(b"content-type", b"").decode("latin-1")
        content_length_str = headers.get(b"content-length", b"").decode("latin-1")

        if content_length_str:
            try:
                if int(content_length_str) > MAX_PAYLOAD_SIZE:
                    response = JSONResponse(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        content={"detail": "Payload size exceeds maximum allowed limit of 10 MiB"},
                    )
                    await response(scope, receive, send)
                    return
            except ValueError:
                pass

        if method in ("POST", "PATCH", "PUT"):
            fastapi_app: FastAPI = scope.get("app")
            has_route_match = False
            if fastapi_app and hasattr(fastapi_app, "router"):
                for route in fastapi_app.router.routes:
                    match, _ = route.matches(scope)
                    if match == Match.FULL:
                        has_route_match = True
                        break

            if has_route_match:
                if not content_type or not content_type.lower().startswith("application/json"):
                    response = JSONResponse(
                        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                        content={"detail": "Unsupported media type. Content-Type must be application/json"},
                    )
                    await response(scope, receive, send)
                    return

                body_chunks = []
                bytes_received = 0
                while True:
                    message = await receive()
                    if message["type"] == "http.request":
                        body = message.get("body", b"")
                        bytes_received += len(body)
                        if bytes_received > MAX_PAYLOAD_SIZE:
                            response = JSONResponse(
                                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                content={"detail": "Payload size exceeds maximum allowed limit of 10 MiB"},
                            )
                            await response(scope, receive, send)
                            return
                        body_chunks.append(body)
                        if not message.get("more_body", False):
                            break

                full_body = b"".join(body_chunks)
                if full_body:
                    try:
                        json.loads(full_body.decode("utf-8"))
                    except Exception:
                        response = JSONResponse(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            content={"detail": "Malformed or invalid JSON payload"},
                        )
                        await response(scope, receive, send)
                        return

                body_sent = False

                async def custom_receive() -> Message:
                    nonlocal body_sent
                    if not body_sent:
                        body_sent = True
                        return {
                            "type": "http.request",
                            "body": full_body,
                            "more_body": False,
                        }
                    return {"type": "http.request", "body": b"", "more_body": False}

                await self.app(scope, custom_receive, send)
                return

        await self.app(scope, receive, send)


# -----------------------------------------------------------------------------
# Static Controllers (MVC: Controller)
# -----------------------------------------------------------------------------
class RecipeVariantBController:
    @staticmethod
    def _format_recipe(recipe: RecipeModel) -> RecipeResponseSchema:
        return RecipeResponseSchema(
            id=recipe.id,
            title=recipe.title,
            description=recipe.description,
            is_active=recipe.is_active,
            base_servings=recipe.base_servings,
            ingredients=[
                RecipeIngredientResponseSchema(
                    id=ing.id,
                    name=ing.name,
                    quantity=ing.quantity,
                    unit=ing.unit,
                )
                for ing in recipe.ingredients
            ],
            created_at=recipe.created_at,
            updated_at=recipe.updated_at,
        )

    @staticmethod
    def list_recipes(
        db: Session,
        is_active: Optional[bool] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> RecipeListResponseSchema:
        query = db.query(RecipeModel)
        if is_active is not None:
            query = query.filter(RecipeModel.is_active == is_active)
        if after_id is not None:
            query = query.filter(RecipeModel.id > after_id)

        rows = query.order_by(RecipeModel.id.asc()).limit(limit + 1).all()
        has_more = len(rows) > limit
        items = rows[:limit]
        next_after_id = items[-1].id if has_more and items else None

        return RecipeListResponseSchema(
            items=[RecipeVariantBController._format_recipe(r) for r in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_recipe(db: Session, payload: RecipeCreateSchema) -> RecipeModel:
        recipe = RecipeModel(
            title=payload.title,
            description=payload.description,
            is_active=payload.is_active,
            base_servings=payload.base_servings,
        )
        db.add(recipe)
        db.flush()

        if payload.ingredients:
            for item in payload.ingredients:
                ing = RecipeIngredientItemModel(
                    recipe_id=recipe.id,
                    name=item.name,
                    quantity=item.quantity,
                    unit=item.unit,
                )
                db.add(ing)

        db.commit()
        db.refresh(recipe)
        return recipe

    @staticmethod
    def get_recipe(db: Session, recipe_id: int) -> RecipeModel:
        recipe = db.query(RecipeModel).filter(RecipeModel.id == recipe_id).first()
        if not recipe:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recipe {recipe_id} not found",
            )
        return recipe

    @staticmethod
    def patch_recipe(db: Session, recipe_id: int, payload: RecipePatchSchema) -> RecipeModel:
        recipe = db.query(RecipeModel).filter(RecipeModel.id == recipe_id).first()
        if not recipe:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recipe {recipe_id} not found",
            )

        if payload.title is not None:
            recipe.title = payload.title
        if payload.description is not None:
            recipe.description = payload.description
        if payload.is_active is not None:
            recipe.is_active = payload.is_active
        if payload.base_servings is not None:
            recipe.base_servings = payload.base_servings

        recipe.updated_at = dt.now(timezone.utc)
        db.commit()
        db.refresh(recipe)
        return recipe

    @staticmethod
    def delete_recipe(db: Session, recipe_id: int) -> None:
        recipe = db.query(RecipeModel).filter(RecipeModel.id == recipe_id).first()
        if not recipe:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recipe {recipe_id} not found",
            )

        # REQ-10-006: Recipes cannot be deleted while plan entries reference them; deletion never cascades
        has_entries = db.query(MealEntryModel).filter(MealEntryModel.recipe_id == recipe_id).first() is not None
        if has_entries:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot delete recipe while it is referenced by meal plan entries",
            )

        db.delete(recipe)
        db.commit()

    @staticmethod
    def list_meal_plans(
        db: Session,
        user: UserModel,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> MealPlanListResponseSchema:
        query = db.query(MealPlanModel)
        if user.role != UserRole.ADMIN.value:
            query = query.filter(MealPlanModel.user_id == user.id)
        if after_id is not None:
            query = query.filter(MealPlanModel.id > after_id)

        rows = query.order_by(MealPlanModel.id.asc()).limit(limit + 1).all()
        has_more = len(rows) > limit
        items = rows[:limit]
        next_after_id = items[-1].id if has_more and items else None

        return MealPlanListResponseSchema(
            items=[
                MealPlanResponseSchema(
                    id=p.id,
                    user_id=p.user_id,
                    week_start_date=p.week_start_date,
                    note=p.note,
                    created_at=p.created_at,
                    updated_at=p.updated_at,
                )
                for p in items
            ],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_meal_plan(db: Session, user: UserModel, payload: MealPlanCreateSchema) -> MealPlanModel:
        existing = db.query(MealPlanModel).filter(
            MealPlanModel.user_id == user.id,
            MealPlanModel.week_start_date == payload.week_start_date,
        ).first()
        if existing:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Meal plan for week starting {payload.week_start_date} already exists for this user",
            )

        plan = MealPlanModel(
            user_id=user.id,
            week_start_date=payload.week_start_date,
            note=payload.note,
        )
        db.add(plan)
        db.commit()
        db.refresh(plan)
        return plan

    @staticmethod
    def get_meal_plan(db: Session, plan_id: int, user: UserModel) -> MealPlanModel:
        plan = db.query(MealPlanModel).filter(MealPlanModel.id == plan_id).first()
        if not plan:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Meal plan {plan_id} not found",
            )

        if user.role != UserRole.ADMIN.value and plan.user_id != user.id:
            raise StarletteHTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to access this meal plan",
            )
        return plan

    @staticmethod
    def patch_meal_plan(
        db: Session,
        plan_id: int,
        user: UserModel,
        payload: MealPlanPatchSchema,
    ) -> MealPlanModel:
        plan = RecipeVariantBController.get_meal_plan(db, plan_id, user)
        if payload.note is not None:
            plan.note = payload.note
        plan.updated_at = dt.now(timezone.utc)
        db.commit()
        db.refresh(plan)
        return plan

    @staticmethod
    def delete_meal_plan(db: Session, plan_id: int, user: UserModel) -> None:
        plan = RecipeVariantBController.get_meal_plan(db, plan_id, user)
        db.delete(plan)
        db.commit()

    @staticmethod
    def list_meal_entries(
        db: Session,
        user: UserModel,
        plan_id: Optional[int] = None,
        day: Optional[DayOfWeekEnum] = None,
        meal: Optional[MealTypeEnum] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> MealEntryListResponseSchema:
        query = db.query(MealEntryModel).join(MealPlanModel)
        if user.role != UserRole.ADMIN.value:
            query = query.filter(MealPlanModel.user_id == user.id)

        if plan_id is not None:
            query = query.filter(MealEntryModel.plan_id == plan_id)
        if day is not None:
            query = query.filter(MealEntryModel.day == day.value)
        if meal is not None:
            query = query.filter(MealEntryModel.meal == meal.value)
        if after_id is not None:
            query = query.filter(MealEntryModel.id > after_id)

        rows = query.order_by(MealEntryModel.id.asc()).limit(limit + 1).all()
        has_more = len(rows) > limit
        items = rows[:limit]
        next_after_id = items[-1].id if has_more and items else None

        return MealEntryListResponseSchema(
            items=[
                MealEntryResponseSchema(
                    id=e.id,
                    plan_id=e.plan_id,
                    recipe_id=e.recipe_id,
                    recipe_title=e.recipe.title,
                    day=DayOfWeekEnum(e.day),
                    meal=MealTypeEnum(e.meal),
                    servings=e.servings,
                    created_at=e.created_at,
                    updated_at=e.updated_at,
                )
                for e in items
            ],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_meal_entry(db: Session, user: UserModel, payload: MealEntryCreateSchema) -> MealEntryModel:
        plan = RecipeVariantBController.get_meal_plan(db, payload.plan_id, user)

        recipe = db.query(RecipeModel).filter(RecipeModel.id == payload.recipe_id).first()
        if not recipe:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recipe {payload.recipe_id} not found",
            )

        if not recipe.is_active:
            raise StarletteHTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Recipe {payload.recipe_id} is inactive and cannot be added to a meal plan",
            )

        # REQ-10-005: Plan-day-meal combination is unique
        existing = db.query(MealEntryModel).filter(
            MealEntryModel.plan_id == payload.plan_id,
            MealEntryModel.day == payload.day.value,
            MealEntryModel.meal == payload.meal.value,
        ).first()
        if existing:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Meal entry for {payload.day.value} {payload.meal.value} already exists in this plan",
            )

        entry = MealEntryModel(
            plan_id=payload.plan_id,
            recipe_id=payload.recipe_id,
            day=payload.day.value,
            meal=payload.meal.value,
            servings=payload.servings,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry

    @staticmethod
    def get_meal_entry(db: Session, entry_id: int, user: UserModel) -> MealEntryModel:
        entry = db.query(MealEntryModel).filter(MealEntryModel.id == entry_id).first()
        if not entry:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Meal entry {entry_id} not found",
            )

        if user.role != UserRole.ADMIN.value and entry.plan.user_id != user.id:
            raise StarletteHTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to access this meal entry",
            )
        return entry

    @staticmethod
    def patch_meal_entry(
        db: Session,
        entry_id: int,
        user: UserModel,
        payload: MealEntryPatchSchema,
    ) -> MealEntryModel:
        entry = RecipeVariantBController.get_meal_entry(db, entry_id, user)

        if payload.recipe_id is not None and payload.recipe_id != entry.recipe_id:
            rec = db.query(RecipeModel).filter(RecipeModel.id == payload.recipe_id).first()
            if not rec:
                raise StarletteHTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Recipe {payload.recipe_id} not found",
                )
            if not rec.is_active:
                raise StarletteHTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Recipe {payload.recipe_id} is inactive",
                )
            entry.recipe_id = payload.recipe_id

        target_day = payload.day.value if payload.day is not None else entry.day
        target_meal = payload.meal.value if payload.meal is not None else entry.meal

        if target_day != entry.day or target_meal != entry.meal:
            existing = db.query(MealEntryModel).filter(
                MealEntryModel.plan_id == entry.plan_id,
                MealEntryModel.day == target_day,
                MealEntryModel.meal == target_meal,
                MealEntryModel.id != entry_id,
            ).first()
            if existing:
                raise StarletteHTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Meal entry for {target_day} {target_meal} already exists in this plan",
                )
            entry.day = target_day
            entry.meal = target_meal

        if payload.servings is not None:
            entry.servings = payload.servings

        entry.updated_at = dt.now(timezone.utc)
        db.commit()
        db.refresh(entry)
        return entry

    @staticmethod
    def delete_meal_entry(db: Session, entry_id: int, user: UserModel) -> None:
        entry = RecipeVariantBController.get_meal_entry(db, entry_id, user)
        db.delete(entry)
        db.commit()

    @staticmethod
    def get_shopping_list(db: Session, plan_id: int, user: UserModel) -> ShoppingListResponseSchema:
        plan = RecipeVariantBController.get_meal_plan(db, plan_id, user)
        aggregated: Dict[tuple[str, str], float] = {}

        for entry in plan.entries:
            ratio = entry.servings / (entry.recipe.base_servings if entry.recipe.base_servings > 0 else 1.0)
            for ing in entry.recipe.ingredients:
                key = (ing.name.strip().title(), ing.unit.strip().upper())
                aggregated[key] = aggregated.get(key, 0.0) + (ing.quantity * ratio)

        items = [
            ShoppingListItemSchema(
                name=name,
                total_quantity=round(qty, 2),
                unit=unit,
            )
            for (name, unit), qty in sorted(aggregated.items())
        ]

        return ShoppingListResponseSchema(
            plan_id=plan.id,
            week_start_date=plan.week_start_date,
            items=items,
        )

    @staticmethod
    def copy_week(
        db: Session,
        plan_id: int,
        user: UserModel,
        payload: CopyWeekSchema,
    ) -> MealPlanModel:
        source_plan = RecipeVariantBController.get_meal_plan(db, plan_id, user)

        # Check if user already has a plan for target week
        existing = db.query(MealPlanModel).filter(
            MealPlanModel.user_id == user.id,
            MealPlanModel.week_start_date == payload.target_week_start_date,
        ).first()
        if existing:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Meal plan for week starting {payload.target_week_start_date} already exists for this user",
            )

        new_plan = MealPlanModel(
            user_id=user.id,
            week_start_date=payload.target_week_start_date,
            note=f"Copied from week {source_plan.week_start_date}",
        )
        db.add(new_plan)
        db.flush()

        # Copy entries
        for e in source_plan.entries:
            copy_entry = MealEntryModel(
                plan_id=new_plan.id,
                recipe_id=e.recipe_id,
                day=e.day,
                meal=e.meal,
                servings=e.servings,
            )
            db.add(copy_entry)

        db.commit()
        db.refresh(new_plan)
        return new_plan

    @staticmethod
    def get_plan_usages(db: Session, recipe_id: int, user: UserModel) -> PlanUsageListResponseSchema:
        recipe = db.query(RecipeModel).filter(RecipeModel.id == recipe_id).first()
        if not recipe:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recipe {recipe_id} not found",
            )

        query = db.query(MealEntryModel).join(MealPlanModel).filter(MealEntryModel.recipe_id == recipe_id)
        if user.role != UserRole.ADMIN.value:
            query = query.filter(MealPlanModel.user_id == user.id)

        rows = query.order_by(MealEntryModel.id.asc()).all()

        return PlanUsageListResponseSchema(
            recipe_id=recipe.id,
            items=[
                PlanUsageItemSchema(
                    entry_id=e.id,
                    plan_id=e.plan_id,
                    user_id=e.plan.user_id,
                    week_start_date=e.plan.week_start_date,
                    day=DayOfWeekEnum(e.day),
                    meal=MealTypeEnum(e.meal),
                    servings=e.servings,
                )
                for e in rows
            ],
        )

    @staticmethod
    def move_meal_entry(
        db: Session,
        entry_id: int,
        user: UserModel,
        payload: MoveMealEntrySchema,
    ) -> MealEntryModel:
        entry = RecipeVariantBController.get_meal_entry(db, entry_id, user)

        if entry.day == payload.target_day.value and entry.meal == payload.target_meal.value:
            return entry

        # Check conflict
        conflict = db.query(MealEntryModel).filter(
            MealEntryModel.plan_id == entry.plan_id,
            MealEntryModel.day == payload.target_day.value,
            MealEntryModel.meal == payload.target_meal.value,
            MealEntryModel.id != entry.id,
        ).first()
        if conflict:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Target slot {payload.target_day.value} {payload.target_meal.value} already occupied",
            )

        entry.day = payload.target_day.value
        entry.meal = payload.target_meal.value
        entry.updated_at = dt.now(timezone.utc)
        db.commit()
        db.refresh(entry)
        return entry


# -----------------------------------------------------------------------------
# FastAPI App Definition & Exception Handlers
# -----------------------------------------------------------------------------
app = FastAPI(
    title="Recipe Management - Variant B",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)
app.add_middleware(JsonValidationASGIMiddleware)


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": "Request validation error", "errors": jsonable_encoder(exc.errors())},
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.error(f"Internal server error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.get(
    "/health",
    status_code=status.HTTP_200_OK,
    responses={503: {"description": "Service Unavailable"}},
)
def get_health(db: Session = Depends(get_db)):
    try:
        db.execute(select(1))
        return {"status": "healthy"}
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable"},
        )


@app.get("/recipes", response_model=RecipeListResponseSchema, status_code=status.HTTP_200_OK)
def list_recipes(
    is_active: Optional[bool] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    return RecipeVariantBController.list_recipes(db, is_active, limit, after_id)


@app.post("/recipes", response_model=RecipeResponseSchema, status_code=status.HTTP_201_CREATED)
def create_recipe(
    payload: RecipeCreateSchema,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    recipe = RecipeVariantBController.create_recipe(db, payload)
    response.headers["Location"] = f"/recipes/{recipe.id}"
    return RecipeVariantBController._format_recipe(recipe)


@app.get("/recipes/{recipe_id}", response_model=RecipeResponseSchema, status_code=status.HTTP_200_OK)
def get_recipe(
    recipe_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if recipe_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    recipe = RecipeVariantBController.get_recipe(db, recipe_id)
    return RecipeVariantBController._format_recipe(recipe)


@app.patch("/recipes/{recipe_id}", response_model=RecipeResponseSchema, status_code=status.HTTP_200_OK)
def patch_recipe(
    recipe_id: int,
    payload: RecipePatchSchema,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    if recipe_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    recipe = RecipeVariantBController.patch_recipe(db, recipe_id, payload)
    return RecipeVariantBController._format_recipe(recipe)


@app.delete("/recipes/{recipe_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_recipe(
    recipe_id: int,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    if recipe_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    RecipeVariantBController.delete_recipe(db, recipe_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/recipes/{recipe_id}/plan-usages", response_model=PlanUsageListResponseSchema, status_code=status.HTTP_200_OK)
def get_recipe_plan_usages(
    recipe_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if recipe_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return RecipeVariantBController.get_plan_usages(db, recipe_id, current_user)


@app.get("/meal-plans", response_model=MealPlanListResponseSchema, status_code=status.HTTP_200_OK)
def list_meal_plans(
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    return RecipeVariantBController.list_meal_plans(db, current_user, limit, after_id)


@app.post("/meal-plans", response_model=MealPlanResponseSchema, status_code=status.HTTP_201_CREATED)
def create_meal_plan(
    payload: MealPlanCreateSchema,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    plan = RecipeVariantBController.create_meal_plan(db, current_user, payload)
    response.headers["Location"] = f"/meal-plans/{plan.id}"
    return plan


@app.get("/meal-plans/{mealplan_id}", response_model=MealPlanResponseSchema, status_code=status.HTTP_200_OK)
def get_meal_plan(
    mealplan_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if mealplan_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return RecipeVariantBController.get_meal_plan(db, mealplan_id, current_user)


@app.patch("/meal-plans/{mealplan_id}", response_model=MealPlanResponseSchema, status_code=status.HTTP_200_OK)
def patch_meal_plan(
    mealplan_id: int,
    payload: MealPlanPatchSchema,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if mealplan_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return RecipeVariantBController.patch_meal_plan(db, mealplan_id, current_user, payload)


@app.delete("/meal-plans/{mealplan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_meal_plan(
    mealplan_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if mealplan_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    RecipeVariantBController.delete_meal_plan(db, mealplan_id, current_user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/meal-plans/{mealplan_id}/shopping-list", response_model=ShoppingListResponseSchema, status_code=status.HTTP_200_OK)
def get_shopping_list(
    mealplan_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if mealplan_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return RecipeVariantBController.get_shopping_list(db, mealplan_id, current_user)


@app.post("/meal-plans/{mealplan_id}/copy-week", response_model=MealPlanResponseSchema, status_code=status.HTTP_201_CREATED)
def copy_week(
    mealplan_id: int,
    payload: CopyWeekSchema,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if mealplan_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    new_plan = RecipeVariantBController.copy_week(db, mealplan_id, current_user, payload)
    response.headers["Location"] = f"/meal-plans/{new_plan.id}"
    return new_plan


@app.get("/meal-entries", response_model=MealEntryListResponseSchema, status_code=status.HTTP_200_OK)
def list_meal_entries(
    plan_id: Optional[int] = Query(None, gt=0),
    day: Optional[DayOfWeekEnum] = Query(None),
    meal: Optional[MealTypeEnum] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    return RecipeVariantBController.list_meal_entries(db, current_user, plan_id, day, meal, limit, after_id)


@app.post("/meal-entries", response_model=MealEntryResponseSchema, status_code=status.HTTP_201_CREATED)
def create_meal_entry(
    payload: MealEntryCreateSchema,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    entry = RecipeVariantBController.create_meal_entry(db, current_user, payload)
    response.headers["Location"] = f"/meal-entries/{entry.id}"
    return MealEntryResponseSchema(
        id=entry.id,
        plan_id=entry.plan_id,
        recipe_id=entry.recipe_id,
        recipe_title=entry.recipe.title,
        day=DayOfWeekEnum(entry.day),
        meal=MealTypeEnum(entry.meal),
        servings=entry.servings,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


@app.get("/meal-entries/{mealentry_id}", response_model=MealEntryResponseSchema, status_code=status.HTTP_200_OK)
def get_meal_entry(
    mealentry_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if mealentry_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    entry = RecipeVariantBController.get_meal_entry(db, mealentry_id, current_user)
    return MealEntryResponseSchema(
        id=entry.id,
        plan_id=entry.plan_id,
        recipe_id=entry.recipe_id,
        recipe_title=entry.recipe.title,
        day=DayOfWeekEnum(entry.day),
        meal=MealTypeEnum(entry.meal),
        servings=entry.servings,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


@app.patch("/meal-entries/{mealentry_id}", response_model=MealEntryResponseSchema, status_code=status.HTTP_200_OK)
def patch_meal_entry(
    mealentry_id: int,
    payload: MealEntryPatchSchema,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if mealentry_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    entry = RecipeVariantBController.patch_meal_entry(db, mealentry_id, current_user, payload)
    return MealEntryResponseSchema(
        id=entry.id,
        plan_id=entry.plan_id,
        recipe_id=entry.recipe_id,
        recipe_title=entry.recipe.title,
        day=DayOfWeekEnum(entry.day),
        meal=MealTypeEnum(entry.meal),
        servings=entry.servings,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


@app.delete("/meal-entries/{mealentry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_meal_entry(
    mealentry_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if mealentry_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    RecipeVariantBController.delete_meal_entry(db, mealentry_id, current_user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/meal-entries/{mealentry_id}/move", response_model=MealEntryResponseSchema, status_code=status.HTTP_200_OK)
def move_meal_entry(
    mealentry_id: int,
    payload: MoveMealEntrySchema,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if mealentry_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    entry = RecipeVariantBController.move_meal_entry(db, mealentry_id, current_user, payload)
    return MealEntryResponseSchema(
        id=entry.id,
        plan_id=entry.plan_id,
        recipe_id=entry.recipe_id,
        recipe_title=entry.recipe.title,
        day=DayOfWeekEnum(entry.day),
        meal=MealTypeEnum(entry.meal),
        servings=entry.servings,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


def custom_openapi() -> Dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi
