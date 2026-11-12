"""
Recipe Management - Variant A: Recipes, ingredients, and servings
CASE_ID: 10

Compliant with:
- Python 3.10+, FastAPI, Pydantic v2, SQLAlchemy ORM, SQLite in-memory with StaticPool.
- MVC architecture: SQLAlchemy models, Pydantic v2 schemas, static controllers, thin FastAPI routes.
- Fully autonomous module exporting `app`.
- Rigorous permission checks, atomic transactions, standardized error responses.
"""

from __future__ import annotations

import enum
import hashlib
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
logger = logging.getLogger("ricettario_variant_a")
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


class UnitEnum(str, enum.Enum):
    G = "G"
    ML = "ML"
    PIECE = "PIECE"
    TSP = "TSP"
    TBSP = "TBSP"


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

    recipes = relationship("RecipeModel", back_populates="user")


class IngredientModel(Base):
    __tablename__ = "ingredients"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(120), unique=True, nullable=False, index=True)
    unit = Column(String(20), nullable=False)  # G, ML, PIECE, TSP, TBSP
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    components = relationship("RecipeComponentModel", back_populates="ingredient")


class RecipeModel(Base):
    __tablename__ = "recipes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(160), nullable=False, index=True)
    instructions = Column(Text, nullable=False)
    servings = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user = relationship("UserModel", back_populates="recipes")
    components = relationship("RecipeComponentModel", back_populates="recipe", cascade="all, delete-orphan")


class RecipeComponentModel(Base):
    __tablename__ = "recipe_components"
    __table_args__ = (
        UniqueConstraint("recipe_id", "ingredient_id", name="uq_recipe_ingredient_a"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id"), nullable=False, index=True)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"), nullable=False, index=True)
    quantity = Column(Float, nullable=False)  # 0.01..100000
    unit = Column(String(20), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    recipe = relationship("RecipeModel", back_populates="components")
    ingredient = relationship("IngredientModel", back_populates="components")


# -----------------------------------------------------------------------------
# Pydantic Schemas (MVC: View / Data Transfer Objects)
# -----------------------------------------------------------------------------
class IngredientCreateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=120)
    unit: UnitEnum


class IngredientPatchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    unit: Optional[UnitEnum] = None


class IngredientResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    unit: UnitEnum
    created_at: datetime
    updated_at: datetime


class IngredientListResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[IngredientResponseSchema]
    limit: int
    next_after_id: Optional[int] = None


class RecipeComponentCreateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_id: int = Field(..., gt=0)
    ingredient_id: int = Field(..., gt=0)
    quantity: float = Field(..., ge=0.01, le=100000.0)
    unit: UnitEnum


class RecipeComponentPatchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: Optional[float] = Field(default=None, ge=0.01, le=100000.0)
    unit: Optional[UnitEnum] = None


class RecipeComponentResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    recipe_id: int
    ingredient_id: int
    ingredient_name: str
    quantity: float
    unit: UnitEnum
    created_at: datetime
    updated_at: datetime


class RecipeComponentListResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[RecipeComponentResponseSchema]
    limit: int
    next_after_id: Optional[int] = None


class RecipeComponentNestedInputSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingredient_id: int = Field(..., gt=0)
    quantity: float = Field(..., ge=0.01, le=100000.0)
    unit: UnitEnum


class RecipeCreateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=3, max_length=160)
    instructions: str = Field(..., min_length=10, max_length=10000)
    servings: int = Field(default=1, ge=1, le=100)
    components: Optional[List[RecipeComponentNestedInputSchema]] = None


class RecipePatchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=3, max_length=160)
    instructions: Optional[str] = Field(default=None, min_length=10, max_length=10000)
    servings: Optional[int] = Field(default=None, ge=1, le=100)


class RecipeResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    user_id: int
    title: str
    instructions: str
    servings: int
    components: List[RecipeComponentResponseSchema]
    created_at: datetime
    updated_at: datetime


class RecipeListResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[RecipeResponseSchema]
    limit: int
    next_after_id: Optional[int] = None


class ScaledComponentItemSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingredient_id: int
    ingredient_name: str
    original_quantity: float
    scaled_quantity: float
    unit: UnitEnum


class RecipeServingsCalculationResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_id: int
    title: str
    original_servings: int
    target_servings: int
    scaling_factor: float
    components: List[ScaledComponentItemSchema]


class ScaleRecipeRequestSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_servings: int = Field(..., ge=1, le=100)


class DuplicateRecipeRequestSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_title: Optional[str] = Field(default=None, min_length=3, max_length=160)
    target_servings: Optional[int] = Field(default=None, ge=1, le=100)


class RecipeUsageItemSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_id: int
    recipe_title: str
    component_quantity: float
    unit: UnitEnum


class RecipeUsageListResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingredient_id: int
    ingredient_name: str
    usages: List[RecipeUsageItemSchema]


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

        # Seed ingredients
        flour = IngredientModel(name="All-Purpose Flour", unit=UnitEnum.G.value)
        sugar = IngredientModel(name="Granulated Sugar", unit=UnitEnum.G.value)
        milk = IngredientModel(name="Whole Milk", unit=UnitEnum.ML.value)
        eggs = IngredientModel(name="Eggs", unit=UnitEnum.PIECE.value)
        vanilla = IngredientModel(name="Vanilla Extract", unit=UnitEnum.TSP.value)
        db.add_all([flour, sugar, milk, eggs, vanilla])
        db.flush()

        # Seed recipe
        pancake = RecipeModel(
            user_id=u1.id,
            title="Classic Fluffy Pancakes",
            instructions="Mix dry ingredients, add wet ingredients, cook on griddle until golden brown.",
            servings=4,
        )
        db.add(pancake)
        db.flush()

        # Seed components
        c1 = RecipeComponentModel(recipe_id=pancake.id, ingredient_id=flour.id, quantity=200.0, unit=UnitEnum.G.value)
        c2 = RecipeComponentModel(recipe_id=pancake.id, ingredient_id=sugar.id, quantity=30.0, unit=UnitEnum.G.value)
        c3 = RecipeComponentModel(recipe_id=pancake.id, ingredient_id=milk.id, quantity=250.0, unit=UnitEnum.ML.value)
        c4 = RecipeComponentModel(recipe_id=pancake.id, ingredient_id=eggs.id, quantity=2.0, unit=UnitEnum.PIECE.value)
        c5 = RecipeComponentModel(recipe_id=pancake.id, ingredient_id=vanilla.id, quantity=1.0, unit=UnitEnum.TSP.value)
        db.add_all([c1, c2, c3, c4, c5])

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
class RecipeVariantAController:
    @staticmethod
    def _format_component(c: RecipeComponentModel) -> RecipeComponentResponseSchema:
        return RecipeComponentResponseSchema(
            id=c.id,
            recipe_id=c.recipe_id,
            ingredient_id=c.ingredient_id,
            ingredient_name=c.ingredient.name,
            quantity=c.quantity,
            unit=UnitEnum(c.unit),
            created_at=c.created_at,
            updated_at=c.updated_at,
        )

    @staticmethod
    def _format_recipe(recipe: RecipeModel) -> RecipeResponseSchema:
        return RecipeResponseSchema(
            id=recipe.id,
            user_id=recipe.user_id,
            title=recipe.title,
            instructions=recipe.instructions,
            servings=recipe.servings,
            components=[RecipeVariantAController._format_component(c) for c in recipe.components],
            created_at=recipe.created_at,
            updated_at=recipe.updated_at,
        )

    @staticmethod
    def list_ingredients(
        db: Session,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> IngredientListResponseSchema:
        query = db.query(IngredientModel)
        if after_id is not None:
            query = query.filter(IngredientModel.id > after_id)

        rows = query.order_by(IngredientModel.id.asc()).limit(limit + 1).all()
        has_more = len(rows) > limit
        items = rows[:limit]
        next_after_id = items[-1].id if has_more and items else None

        return IngredientListResponseSchema(
            items=[
                IngredientResponseSchema(
                    id=ing.id,
                    name=ing.name,
                    unit=UnitEnum(ing.unit),
                    created_at=ing.created_at,
                    updated_at=ing.updated_at,
                )
                for ing in items
            ],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_ingredient(db: Session, payload: IngredientCreateSchema) -> IngredientModel:
        existing = db.query(IngredientModel).filter(IngredientModel.name == payload.name).first()
        if existing:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Ingredient with name '{payload.name}' already exists",
            )

        ing = IngredientModel(name=payload.name, unit=payload.unit.value)
        db.add(ing)
        db.commit()
        db.refresh(ing)
        return ing

    @staticmethod
    def get_ingredient(db: Session, ingredient_id: int) -> IngredientModel:
        ing = db.query(IngredientModel).filter(IngredientModel.id == ingredient_id).first()
        if not ing:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Ingredient {ingredient_id} not found",
            )
        return ing

    @staticmethod
    def patch_ingredient(db: Session, ingredient_id: int, payload: IngredientPatchSchema) -> IngredientModel:
        ing = db.query(IngredientModel).filter(IngredientModel.id == ingredient_id).first()
        if not ing:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Ingredient {ingredient_id} not found",
            )

        if payload.name is not None and payload.name != ing.name:
            existing = db.query(IngredientModel).filter(
                IngredientModel.name == payload.name,
                IngredientModel.id != ingredient_id,
            ).first()
            if existing:
                raise StarletteHTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Ingredient with name '{payload.name}' already exists",
                )
            ing.name = payload.name

        if payload.unit is not None and payload.unit.value != ing.unit:
            # Check if referenced components exist
            has_components = db.query(RecipeComponentModel).filter(
                RecipeComponentModel.ingredient_id == ingredient_id
            ).first() is not None
            if has_components:
                raise StarletteHTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot change unit of ingredient referenced by existing recipe components",
                )
            ing.unit = payload.unit.value

        ing.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(ing)
        return ing

    @staticmethod
    def delete_ingredient(db: Session, ingredient_id: int) -> None:
        ing = db.query(IngredientModel).filter(IngredientModel.id == ingredient_id).first()
        if not ing:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Ingredient {ingredient_id} not found",
            )

        # REQ-10-006: Ingredients cannot be deleted while components reference them
        referenced = db.query(RecipeComponentModel).filter(
            RecipeComponentModel.ingredient_id == ingredient_id
        ).first() is not None
        if referenced:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot delete ingredient that is referenced by recipe components",
            )

        db.delete(ing)
        db.commit()

    @staticmethod
    def list_recipes(
        db: Session,
        user: UserModel,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> RecipeListResponseSchema:
        query = db.query(RecipeModel)
        if user.role != UserRole.ADMIN.value:
            query = query.filter(RecipeModel.user_id == user.id)
        if after_id is not None:
            query = query.filter(RecipeModel.id > after_id)

        rows = query.order_by(RecipeModel.id.asc()).limit(limit + 1).all()
        has_more = len(rows) > limit
        items = rows[:limit]
        next_after_id = items[-1].id if has_more and items else None

        return RecipeListResponseSchema(
            items=[RecipeVariantAController._format_recipe(r) for r in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_recipe(db: Session, user: UserModel, payload: RecipeCreateSchema) -> RecipeModel:
        recipe = RecipeModel(
            user_id=user.id,
            title=payload.title,
            instructions=payload.instructions,
            servings=payload.servings,
        )
        db.add(recipe)
        db.flush()

        if payload.components:
            seen_ingredients = set()
            for c in payload.components:
                if c.ingredient_id in seen_ingredients:
                    raise StarletteHTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Duplicate ingredient {c.ingredient_id} in recipe components",
                    )
                seen_ingredients.add(c.ingredient_id)
                ing = db.query(IngredientModel).filter(IngredientModel.id == c.ingredient_id).first()
                if not ing:
                    raise StarletteHTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Ingredient {c.ingredient_id} not found",
                    )
                # REQ-10-004: matching unit
                if c.unit.value != ing.unit:
                    raise StarletteHTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=f"Component unit '{c.unit.value}' does not match ingredient unit '{ing.unit}'",
                    )
                comp = RecipeComponentModel(
                    recipe_id=recipe.id,
                    ingredient_id=c.ingredient_id,
                    quantity=c.quantity,
                    unit=c.unit.value,
                )
                db.add(comp)

        db.commit()
        db.refresh(recipe)
        return recipe

    @staticmethod
    def get_recipe(db: Session, recipe_id: int, user: UserModel) -> RecipeModel:
        recipe = db.query(RecipeModel).filter(RecipeModel.id == recipe_id).first()
        if not recipe:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recipe {recipe_id} not found",
            )

        if user.role != UserRole.ADMIN.value and recipe.user_id != user.id:
            raise StarletteHTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to access this recipe",
            )
        return recipe

    @staticmethod
    def patch_recipe(
        db: Session,
        recipe_id: int,
        user: UserModel,
        payload: RecipePatchSchema,
    ) -> RecipeModel:
        recipe = RecipeVariantAController.get_recipe(db, recipe_id, user)

        if payload.title is not None:
            recipe.title = payload.title
        if payload.instructions is not None:
            recipe.instructions = payload.instructions
        if payload.servings is not None:
            recipe.servings = payload.servings

        recipe.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(recipe)
        return recipe

    @staticmethod
    def delete_recipe(db: Session, recipe_id: int, user: UserModel) -> None:
        recipe = RecipeVariantAController.get_recipe(db, recipe_id, user)
        db.delete(recipe)
        db.commit()

    @staticmethod
    def list_recipe_components(
        db: Session,
        user: UserModel,
        recipe_id: Optional[int] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> RecipeComponentListResponseSchema:
        query = db.query(RecipeComponentModel).join(RecipeModel)
        if user.role != UserRole.ADMIN.value:
            query = query.filter(RecipeModel.user_id == user.id)

        if recipe_id is not None:
            query = query.filter(RecipeComponentModel.recipe_id == recipe_id)
        if after_id is not None:
            query = query.filter(RecipeComponentModel.id > after_id)

        rows = query.order_by(RecipeComponentModel.id.asc()).limit(limit + 1).all()
        has_more = len(rows) > limit
        items = rows[:limit]
        next_after_id = items[-1].id if has_more and items else None

        return RecipeComponentListResponseSchema(
            items=[RecipeVariantAController._format_component(c) for c in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_recipe_component(
        db: Session,
        user: UserModel,
        payload: RecipeComponentCreateSchema,
    ) -> RecipeComponentModel:
        recipe = RecipeVariantAController.get_recipe(db, payload.recipe_id, user)

        ing = db.query(IngredientModel).filter(IngredientModel.id == payload.ingredient_id).first()
        if not ing:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Ingredient {payload.ingredient_id} not found",
            )

        if payload.unit.value != ing.unit:
            raise StarletteHTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Component unit '{payload.unit.value}' does not match ingredient unit '{ing.unit}'",
            )

        existing = db.query(RecipeComponentModel).filter(
            RecipeComponentModel.recipe_id == payload.recipe_id,
            RecipeComponentModel.ingredient_id == payload.ingredient_id,
        ).first()
        if existing:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Component for ingredient {payload.ingredient_id} already exists in this recipe",
            )

        comp = RecipeComponentModel(
            recipe_id=payload.recipe_id,
            ingredient_id=payload.ingredient_id,
            quantity=payload.quantity,
            unit=payload.unit.value,
        )
        db.add(comp)
        db.commit()
        db.refresh(comp)
        return comp

    @staticmethod
    def get_recipe_component(db: Session, component_id: int, user: UserModel) -> RecipeComponentModel:
        comp = db.query(RecipeComponentModel).filter(RecipeComponentModel.id == component_id).first()
        if not comp:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recipe component {component_id} not found",
            )

        if user.role != UserRole.ADMIN.value and comp.recipe.user_id != user.id:
            raise StarletteHTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to access this recipe component",
            )
        return comp

    @staticmethod
    def patch_recipe_component(
        db: Session,
        component_id: int,
        user: UserModel,
        payload: RecipeComponentPatchSchema,
    ) -> RecipeComponentModel:
        comp = RecipeVariantAController.get_recipe_component(db, component_id, user)

        if payload.unit is not None:
            if payload.unit.value != comp.ingredient.unit:
                raise StarletteHTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Component unit '{payload.unit.value}' does not match ingredient unit '{comp.ingredient.unit}'",
                )
            comp.unit = payload.unit.value

        if payload.quantity is not None:
            comp.quantity = payload.quantity

        comp.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(comp)
        return comp

    @staticmethod
    def delete_recipe_component(db: Session, component_id: int, user: UserModel) -> None:
        comp = RecipeVariantAController.get_recipe_component(db, component_id, user)
        db.delete(comp)
        db.commit()

    @staticmethod
    def get_recipe_servings_calculation(
        db: Session,
        recipe_id: int,
        target_servings: int,
        user: UserModel,
    ) -> RecipeServingsCalculationResponseSchema:
        recipe = RecipeVariantAController.get_recipe(db, recipe_id, user)
        scale_ratio = target_servings / (recipe.servings if recipe.servings > 0 else 1)

        scaled_components = [
            ScaledComponentItemSchema(
                ingredient_id=c.ingredient_id,
                ingredient_name=c.ingredient.name,
                original_quantity=c.quantity,
                scaled_quantity=round(c.quantity * scale_ratio, 4),
                unit=UnitEnum(c.unit),
            )
            for c in recipe.components
        ]

        return RecipeServingsCalculationResponseSchema(
            recipe_id=recipe.id,
            title=recipe.title,
            original_servings=recipe.servings,
            target_servings=target_servings,
            scaling_factor=round(scale_ratio, 4),
            components=scaled_components,
        )

    @staticmethod
    def scale_recipe(
        db: Session,
        recipe_id: int,
        target_servings: int,
        user: UserModel,
    ) -> RecipeModel:
        recipe = RecipeVariantAController.get_recipe(db, recipe_id, user)

        if recipe.servings == target_servings:
            return recipe

        scale_ratio = target_servings / (recipe.servings if recipe.servings > 0 else 1)
        recipe.servings = target_servings

        for c in recipe.components:
            c.quantity = round(c.quantity * scale_ratio, 4)
            c.updated_at = datetime.now(timezone.utc)

        recipe.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(recipe)
        return recipe

    @staticmethod
    def duplicate_recipe(
        db: Session,
        recipe_id: int,
        user: UserModel,
        payload: DuplicateRecipeRequestSchema,
    ) -> RecipeModel:
        source_recipe = RecipeVariantAController.get_recipe(db, recipe_id, user)

        new_servings = payload.target_servings if payload.target_servings is not None else source_recipe.servings
        scale_ratio = new_servings / (source_recipe.servings if source_recipe.servings > 0 else 1)

        new_title = payload.new_title if payload.new_title is not None else f"Copy of {source_recipe.title}"

        new_recipe = RecipeModel(
            user_id=user.id,
            title=new_title,
            instructions=source_recipe.instructions,
            servings=new_servings,
        )
        db.add(new_recipe)
        db.flush()

        for c in source_recipe.components:
            scaled_qty = round(c.quantity * scale_ratio, 4)
            comp = RecipeComponentModel(
                recipe_id=new_recipe.id,
                ingredient_id=c.ingredient_id,
                quantity=scaled_qty,
                unit=c.unit,
            )
            db.add(comp)

        db.commit()
        db.refresh(new_recipe)
        return new_recipe

    @staticmethod
    def get_ingredient_usages(
        db: Session,
        ingredient_id: int,
        user: UserModel,
    ) -> RecipeUsageListResponseSchema:
        ing = RecipeVariantAController.get_ingredient(db, ingredient_id)

        query = db.query(RecipeComponentModel).join(RecipeModel).filter(RecipeComponentModel.ingredient_id == ingredient_id)
        if user.role != UserRole.ADMIN.value:
            query = query.filter(RecipeModel.user_id == user.id)

        rows = query.order_by(RecipeComponentModel.id.asc()).all()

        return RecipeUsageListResponseSchema(
            ingredient_id=ing.id,
            ingredient_name=ing.name,
            usages=[
                RecipeUsageItemSchema(
                    recipe_id=c.recipe_id,
                    recipe_title=c.recipe.title,
                    component_quantity=c.quantity,
                    unit=UnitEnum(c.unit),
                )
                for c in rows
            ],
        )


# -----------------------------------------------------------------------------
# FastAPI App Definition & Exception Handlers
# -----------------------------------------------------------------------------
app = FastAPI(
    title="Recipe Management - Variant A",
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


@app.get("/ingredients", response_model=IngredientListResponseSchema, status_code=status.HTTP_200_OK)
def list_ingredients(
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    return RecipeVariantAController.list_ingredients(db, limit, after_id)


@app.post("/ingredients", response_model=IngredientResponseSchema, status_code=status.HTTP_201_CREATED)
def create_ingredient(
    payload: IngredientCreateSchema,
    response: Response,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    ing = RecipeVariantAController.create_ingredient(db, payload)
    response.headers["Location"] = f"/ingredients/{ing.id}"
    return ing


@app.get("/ingredients/{ingredient_id}", response_model=IngredientResponseSchema, status_code=status.HTTP_200_OK)
def get_ingredient(
    ingredient_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if ingredient_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return RecipeVariantAController.get_ingredient(db, ingredient_id)


@app.patch("/ingredients/{ingredient_id}", response_model=IngredientResponseSchema, status_code=status.HTTP_200_OK)
def patch_ingredient(
    ingredient_id: int,
    payload: IngredientPatchSchema,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    if ingredient_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return RecipeVariantAController.patch_ingredient(db, ingredient_id, payload)


@app.delete("/ingredients/{ingredient_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ingredient(
    ingredient_id: int,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    if ingredient_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    RecipeVariantAController.delete_ingredient(db, ingredient_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/ingredients/{ingredient_id}/recipe-usages", response_model=RecipeUsageListResponseSchema, status_code=status.HTTP_200_OK)
def get_ingredient_usages(
    ingredient_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if ingredient_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return RecipeVariantAController.get_ingredient_usages(db, ingredient_id, current_user)


@app.get("/recipes", response_model=RecipeListResponseSchema, status_code=status.HTTP_200_OK)
def list_recipes(
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    return RecipeVariantAController.list_recipes(db, current_user, limit, after_id)


@app.post("/recipes", response_model=RecipeResponseSchema, status_code=status.HTTP_201_CREATED)
def create_recipe(
    payload: RecipeCreateSchema,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    recipe = RecipeVariantAController.create_recipe(db, current_user, payload)
    response.headers["Location"] = f"/recipes/{recipe.id}"
    return RecipeVariantAController._format_recipe(recipe)


@app.get("/recipes/{recipe_id}", response_model=RecipeResponseSchema, status_code=status.HTTP_200_OK)
def get_recipe(
    recipe_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if recipe_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    recipe = RecipeVariantAController.get_recipe(db, recipe_id, current_user)
    return RecipeVariantAController._format_recipe(recipe)


@app.patch("/recipes/{recipe_id}", response_model=RecipeResponseSchema, status_code=status.HTTP_200_OK)
def patch_recipe(
    recipe_id: int,
    payload: RecipePatchSchema,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if recipe_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    recipe = RecipeVariantAController.patch_recipe(db, recipe_id, current_user, payload)
    return RecipeVariantAController._format_recipe(recipe)


@app.delete("/recipes/{recipe_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_recipe(
    recipe_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if recipe_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    RecipeVariantAController.delete_recipe(db, recipe_id, current_user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/recipes/{recipe_id}/servings", response_model=RecipeServingsCalculationResponseSchema, status_code=status.HTTP_200_OK)
def get_recipe_servings_calculation(
    recipe_id: int,
    target_servings: int = Query(..., ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if recipe_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return RecipeVariantAController.get_recipe_servings_calculation(db, recipe_id, target_servings, current_user)


@app.post("/recipes/{recipe_id}/scale", response_model=RecipeResponseSchema, status_code=status.HTTP_200_OK)
def scale_recipe(
    recipe_id: int,
    payload: ScaleRecipeRequestSchema,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if recipe_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    recipe = RecipeVariantAController.scale_recipe(db, recipe_id, payload.target_servings, current_user)
    return RecipeVariantAController._format_recipe(recipe)


@app.post("/recipes/{recipe_id}/duplicate", response_model=RecipeResponseSchema, status_code=status.HTTP_201_CREATED)
def duplicate_recipe(
    recipe_id: int,
    payload: DuplicateRecipeRequestSchema,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if recipe_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    new_recipe = RecipeVariantAController.duplicate_recipe(db, recipe_id, current_user, payload)
    response.headers["Location"] = f"/recipes/{new_recipe.id}"
    return RecipeVariantAController._format_recipe(new_recipe)


@app.get("/recipe-components", response_model=RecipeComponentListResponseSchema, status_code=status.HTTP_200_OK)
def list_recipe_components(
    recipe_id: Optional[int] = Query(None, gt=0),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    return RecipeVariantAController.list_recipe_components(db, current_user, recipe_id, limit, after_id)


@app.post("/recipe-components", response_model=RecipeComponentResponseSchema, status_code=status.HTTP_201_CREATED)
def create_recipe_component(
    payload: RecipeComponentCreateSchema,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    comp = RecipeVariantAController.create_recipe_component(db, current_user, payload)
    response.headers["Location"] = f"/recipe-components/{comp.id}"
    return RecipeVariantAController._format_component(comp)


@app.get("/recipe-components/{recipecomponent_id}", response_model=RecipeComponentResponseSchema, status_code=status.HTTP_200_OK)
def get_recipe_component(
    recipecomponent_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if recipecomponent_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    comp = RecipeVariantAController.get_recipe_component(db, recipecomponent_id, current_user)
    return RecipeVariantAController._format_component(comp)


@app.patch("/recipe-components/{recipecomponent_id}", response_model=RecipeComponentResponseSchema, status_code=status.HTTP_200_OK)
def patch_recipe_component(
    recipecomponent_id: int,
    payload: RecipeComponentPatchSchema,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if recipecomponent_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    comp = RecipeVariantAController.patch_recipe_component(db, recipecomponent_id, current_user, payload)
    return RecipeVariantAController._format_component(comp)


@app.delete("/recipe-components/{recipecomponent_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_recipe_component(
    recipecomponent_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if recipecomponent_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    RecipeVariantAController.delete_recipe_component(db, recipecomponent_id, current_user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def custom_openapi() -> Dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi
