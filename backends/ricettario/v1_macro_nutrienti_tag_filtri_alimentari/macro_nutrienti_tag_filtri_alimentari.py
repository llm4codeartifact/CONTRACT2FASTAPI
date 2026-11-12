"""
Recipe Management - Variant C: Macronutrients, tags, and dietary filters
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
from typing import Annotated, Any, Dict, List, Optional

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
logger = logging.getLogger("ricettario_variant_c")
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


class DietaryTypeEnum(str, enum.Enum):
    VEGAN = "VEGAN"
    VEGETARIAN = "VEGETARIAN"
    GLUTEN_FREE = "GLUTEN_FREE"
    DAIRY_FREE = "DAIRY_FREE"
    HIGH_PROTEIN = "HIGH_PROTEIN"


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
    calories_kcal = Column(Float, nullable=False)  # per 100g: 0..1000
    protein_g = Column(Float, nullable=False)      # per 100g: 0..100
    carbs_g = Column(Float, nullable=False)        # per 100g: 0..100
    fat_g = Column(Float, nullable=False)          # per 100g: 0..100
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
    recipe_tags = relationship("RecipeTagModel", back_populates="recipe", cascade="all, delete-orphan")


class RecipeComponentModel(Base):
    __tablename__ = "recipe_components"
    __table_args__ = (
        UniqueConstraint("recipe_id", "ingredient_id", name="uq_recipe_ingredient"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id"), nullable=False, index=True)
    ingredient_id = Column(Integer, ForeignKey("ingredients.id"), nullable=False, index=True)
    quantity_grams = Column(Float, nullable=False)  # 0.01..100000
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    recipe = relationship("RecipeModel", back_populates="components")
    ingredient = relationship("IngredientModel", back_populates="components")


class TagModel(Base):
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(40), unique=True, nullable=False, index=True)
    dietary_type = Column(String(30), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    recipe_tags = relationship("RecipeTagModel", back_populates="tag")


class RecipeTagModel(Base):
    __tablename__ = "recipe_tags"
    __table_args__ = (
        UniqueConstraint("recipe_id", "tag_id", name="uq_recipe_tag"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id"), nullable=False, index=True)
    tag_id = Column(Integer, ForeignKey("tags.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    recipe = relationship("RecipeModel", back_populates="recipe_tags")
    tag = relationship("TagModel", back_populates="recipe_tags")


# -----------------------------------------------------------------------------
# Pydantic Schemas (MVC: View / Data Transfer Objects)
# -----------------------------------------------------------------------------
class IngredientCreateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=120)
    calories_kcal: float = Field(..., ge=0, le=1000)
    protein_g: float = Field(..., ge=0, le=100)
    carbs_g: float = Field(..., ge=0, le=100)
    fat_g: float = Field(..., ge=0, le=100)


class IngredientPatchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    calories_kcal: Optional[float] = Field(default=None, ge=0, le=1000)
    protein_g: Optional[float] = Field(default=None, ge=0, le=100)
    carbs_g: Optional[float] = Field(default=None, ge=0, le=100)
    fat_g: Optional[float] = Field(default=None, ge=0, le=100)


class IngredientResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    calories_kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float
    created_at: datetime
    updated_at: datetime


class IngredientListResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[IngredientResponseSchema]
    limit: int
    next_after_id: Optional[int] = None


class RecipeComponentInputSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingredient_id: int = Field(..., gt=0)
    quantity_grams: float = Field(..., ge=0.01, le=100000.0)


class RecipeComponentResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    ingredient_id: int
    ingredient_name: str
    quantity_grams: float


class TagResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    dietary_type: DietaryTypeEnum
    created_at: datetime
    updated_at: datetime


class RecipeCreateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=3, max_length=160)
    instructions: str = Field(..., min_length=10, max_length=10000)
    servings: int = Field(default=1, ge=1, le=100)
    components: Optional[List[RecipeComponentInputSchema]] = None


class RecipePatchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=3, max_length=160)
    instructions: Optional[str] = Field(default=None, min_length=10, max_length=10000)
    servings: Optional[int] = Field(default=None, ge=1, le=100)
    components: Optional[List[RecipeComponentInputSchema]] = None


class RecipeResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    user_id: int
    title: str
    instructions: str
    servings: int
    components: List[RecipeComponentResponseSchema]
    tags: List[TagResponseSchema]
    created_at: datetime
    updated_at: datetime


class RecipeListResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[RecipeResponseSchema]
    limit: int
    next_after_id: Optional[int] = None


class TagCreateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=40)
    dietary_type: DietaryTypeEnum


class TagPatchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=2, max_length=40)
    dietary_type: Optional[DietaryTypeEnum] = None


class TagListResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[TagResponseSchema]
    limit: int
    next_after_id: Optional[int] = None


class RecipeAddTagSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag_id: int = Field(..., gt=0)


class ComponentNutritionDetailSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingredient_id: int
    ingredient_name: str
    quantity_grams: float
    calories_kcal: float
    protein_g: float
    carbs_g: float
    fat_g: float


class RecipeNutritionResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_id: int
    servings: int
    total_calories_kcal: float
    total_protein_g: float
    total_carbs_g: float
    total_fat_g: float
    per_serving_calories_kcal: float
    per_serving_protein_g: float
    per_serving_carbs_g: float
    per_serving_fat_g: float
    components: List[ComponentNutritionDetailSchema]


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
        ing_chicken = IngredientModel(
            name="Chicken Breast",
            calories_kcal=165.0,
            protein_g=31.0,
            carbs_g=0.0,
            fat_g=3.6,
        )
        ing_rice = IngredientModel(
            name="White Rice",
            calories_kcal=130.0,
            protein_g=2.7,
            carbs_g=28.0,
            fat_g=0.3,
        )
        ing_olive_oil = IngredientModel(
            name="Olive Oil",
            calories_kcal=884.0,
            protein_g=0.0,
            carbs_g=0.0,
            fat_g=100.0,
        )
        db.add_all([ing_chicken, ing_rice, ing_olive_oil])
        db.flush()

        # Seed tags
        tag_high_protein = TagModel(name="High Protein Meal", dietary_type=DietaryTypeEnum.HIGH_PROTEIN.value)
        tag_gluten_free = TagModel(name="Gluten Free", dietary_type=DietaryTypeEnum.GLUTEN_FREE.value)
        tag_vegan = TagModel(name="Plant Based", dietary_type=DietaryTypeEnum.VEGAN.value)
        db.add_all([tag_high_protein, tag_gluten_free, tag_vegan])
        db.flush()

        # Seed recipe
        recipe1 = RecipeModel(
            user_id=u1.id,
            title="Healthy Chicken and Rice",
            instructions="Boil rice, grill chicken with olive oil, mix and serve.",
            servings=2,
        )
        db.add(recipe1)
        db.flush()

        # Seed components
        comp1 = RecipeComponentModel(recipe_id=recipe1.id, ingredient_id=ing_chicken.id, quantity_grams=300.0)
        comp2 = RecipeComponentModel(recipe_id=recipe1.id, ingredient_id=ing_rice.id, quantity_grams=200.0)
        comp3 = RecipeComponentModel(recipe_id=recipe1.id, ingredient_id=ing_olive_oil.id, quantity_grams=10.0)
        db.add_all([comp1, comp2, comp3])

        # Seed recipe tag
        rt1 = RecipeTagModel(recipe_id=recipe1.id, tag_id=tag_high_protein.id)
        rt2 = RecipeTagModel(recipe_id=recipe1.id, tag_id=tag_gluten_free.id)
        db.add_all([rt1, rt2])

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

        if method in ("POST", "PATCH"):
            fastapi_app: FastAPI = scope.get("app")
            has_route_match = False
            if fastapi_app and hasattr(fastapi_app, "router"):
                for route in fastapi_app.router.routes:
                    match, _ = route.matches(scope)
                    if match == Match.FULL:
                        has_route_match = True
                        break

            if has_route_match and content_length_str not in ("", "0"):
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
class RecipeVariantCController:
    @staticmethod
    def _format_recipe(recipe: RecipeModel) -> RecipeResponseSchema:
        components = [
            RecipeComponentResponseSchema(
                id=c.id,
                ingredient_id=c.ingredient_id,
                ingredient_name=c.ingredient.name,
                quantity_grams=c.quantity_grams,
            )
            for c in recipe.components
        ]
        tags = [
            TagResponseSchema(
                id=rt.tag.id,
                name=rt.tag.name,
                dietary_type=DietaryTypeEnum(rt.tag.dietary_type),
                created_at=rt.tag.created_at,
                updated_at=rt.tag.updated_at,
            )
            for rt in recipe.recipe_tags
        ]
        return RecipeResponseSchema(
            id=recipe.id,
            user_id=recipe.user_id,
            title=recipe.title,
            instructions=recipe.instructions,
            servings=recipe.servings,
            components=components,
            tags=tags,
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
                    calories_kcal=ing.calories_kcal,
                    protein_g=ing.protein_g,
                    carbs_g=ing.carbs_g,
                    fat_g=ing.fat_g,
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

        ing = IngredientModel(
            name=payload.name,
            calories_kcal=payload.calories_kcal,
            protein_g=payload.protein_g,
            carbs_g=payload.carbs_g,
            fat_g=payload.fat_g,
        )
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

        if payload.calories_kcal is not None:
            ing.calories_kcal = payload.calories_kcal
        if payload.protein_g is not None:
            ing.protein_g = payload.protein_g
        if payload.carbs_g is not None:
            ing.carbs_g = payload.carbs_g
        if payload.fat_g is not None:
            ing.fat_g = payload.fat_g

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

        # REQ-10-005 & REQ-10-006: Ingredient cannot be deleted while referenced by recipes
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
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> RecipeListResponseSchema:
        query = db.query(RecipeModel)
        if after_id is not None:
            query = query.filter(RecipeModel.id > after_id)

        rows = query.order_by(RecipeModel.id.asc()).limit(limit + 1).all()
        has_more = len(rows) > limit
        items = rows[:limit]
        next_after_id = items[-1].id if has_more and items else None

        return RecipeListResponseSchema(
            items=[RecipeVariantCController._format_recipe(r) for r in items],
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
            for comp in payload.components:
                if comp.ingredient_id in seen_ingredients:
                    raise StarletteHTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Duplicate ingredient {comp.ingredient_id} in recipe components",
                    )
                seen_ingredients.add(comp.ingredient_id)
                ing = db.query(IngredientModel).filter(IngredientModel.id == comp.ingredient_id).first()
                if not ing:
                    raise StarletteHTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Ingredient {comp.ingredient_id} not found",
                    )
                rc = RecipeComponentModel(
                    recipe_id=recipe.id,
                    ingredient_id=comp.ingredient_id,
                    quantity_grams=comp.quantity_grams,
                )
                db.add(rc)

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
    def patch_recipe(
        db: Session,
        recipe_id: int,
        user: UserModel,
        payload: RecipePatchSchema,
    ) -> RecipeModel:
        recipe = db.query(RecipeModel).filter(RecipeModel.id == recipe_id).first()
        if not recipe:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recipe {recipe_id} not found",
            )

        if user.role != UserRole.ADMIN.value and recipe.user_id != user.id:
            raise StarletteHTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to update this recipe",
            )

        if payload.title is not None:
            recipe.title = payload.title
        if payload.instructions is not None:
            recipe.instructions = payload.instructions
        if payload.servings is not None:
            recipe.servings = payload.servings

        if payload.components is not None:
            # Replace components
            db.query(RecipeComponentModel).filter(RecipeComponentModel.recipe_id == recipe_id).delete()
            seen_ingredients = set()
            for comp in payload.components:
                if comp.ingredient_id in seen_ingredients:
                    raise StarletteHTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Duplicate ingredient {comp.ingredient_id} in recipe components",
                    )
                seen_ingredients.add(comp.ingredient_id)
                ing = db.query(IngredientModel).filter(IngredientModel.id == comp.ingredient_id).first()
                if not ing:
                    raise StarletteHTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Ingredient {comp.ingredient_id} not found",
                    )
                rc = RecipeComponentModel(
                    recipe_id=recipe.id,
                    ingredient_id=comp.ingredient_id,
                    quantity_grams=comp.quantity_grams,
                )
                db.add(rc)

        recipe.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(recipe)
        return recipe

    @staticmethod
    def delete_recipe(db: Session, recipe_id: int, user: UserModel) -> None:
        recipe = db.query(RecipeModel).filter(RecipeModel.id == recipe_id).first()
        if not recipe:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recipe {recipe_id} not found",
            )

        if user.role != UserRole.ADMIN.value and recipe.user_id != user.id:
            raise StarletteHTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to delete this recipe",
            )

        db.delete(recipe)
        db.commit()

    @staticmethod
    def list_tags(
        db: Session,
        dietary_type: Optional[DietaryTypeEnum] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> TagListResponseSchema:
        query = db.query(TagModel)
        if dietary_type is not None:
            query = query.filter(TagModel.dietary_type == dietary_type.value)
        if after_id is not None:
            query = query.filter(TagModel.id > after_id)

        rows = query.order_by(TagModel.id.asc()).limit(limit + 1).all()
        has_more = len(rows) > limit
        items = rows[:limit]
        next_after_id = items[-1].id if has_more and items else None

        return TagListResponseSchema(
            items=[
                TagResponseSchema(
                    id=t.id,
                    name=t.name,
                    dietary_type=DietaryTypeEnum(t.dietary_type),
                    created_at=t.created_at,
                    updated_at=t.updated_at,
                )
                for t in items
            ],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_tag(db: Session, payload: TagCreateSchema) -> TagModel:
        existing = db.query(TagModel).filter(TagModel.name == payload.name).first()
        if existing:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Tag with name '{payload.name}' already exists",
            )

        tag = TagModel(name=payload.name, dietary_type=payload.dietary_type.value)
        db.add(tag)
        db.commit()
        db.refresh(tag)
        return tag

    @staticmethod
    def get_tag(db: Session, tag_id: int) -> TagModel:
        tag = db.query(TagModel).filter(TagModel.id == tag_id).first()
        if not tag:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tag {tag_id} not found",
            )
        return tag

    @staticmethod
    def patch_tag(db: Session, tag_id: int, payload: TagPatchSchema) -> TagModel:
        tag = db.query(TagModel).filter(TagModel.id == tag_id).first()
        if not tag:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tag {tag_id} not found",
            )

        if payload.name is not None and payload.name != tag.name:
            existing = db.query(TagModel).filter(
                TagModel.name == payload.name,
                TagModel.id != tag_id,
            ).first()
            if existing:
                raise StarletteHTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Tag with name '{payload.name}' already exists",
                )
            tag.name = payload.name

        if payload.dietary_type is not None:
            tag.dietary_type = payload.dietary_type.value

        tag.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(tag)
        return tag

    @staticmethod
    def delete_tag(db: Session, tag_id: int) -> None:
        tag = db.query(TagModel).filter(TagModel.id == tag_id).first()
        if not tag:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tag {tag_id} not found",
            )

        # REQ-10-005: Tag deletion is prohibited while referenced by recipes
        referenced = db.query(RecipeTagModel).filter(RecipeTagModel.tag_id == tag_id).first() is not None
        if referenced:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot delete tag that is referenced by recipes",
            )

        db.delete(tag)
        db.commit()

    @staticmethod
    def add_tag_to_recipe(
        db: Session,
        recipe_id: int,
        user: UserModel,
        payload: RecipeAddTagSchema,
    ) -> RecipeModel:
        recipe = db.query(RecipeModel).filter(RecipeModel.id == recipe_id).first()
        if not recipe:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recipe {recipe_id} not found",
            )

        if user.role != UserRole.ADMIN.value and recipe.user_id != user.id:
            raise StarletteHTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to modify tags on this recipe",
            )

        tag = db.query(TagModel).filter(TagModel.id == payload.tag_id).first()
        if not tag:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tag {payload.tag_id} not found",
            )

        existing = db.query(RecipeTagModel).filter(
            RecipeTagModel.recipe_id == recipe_id,
            RecipeTagModel.tag_id == payload.tag_id,
        ).first()
        if existing:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Recipe already has tag {payload.tag_id}",
            )

        rt = RecipeTagModel(recipe_id=recipe_id, tag_id=payload.tag_id)
        db.add(rt)
        db.commit()
        db.refresh(recipe)
        return recipe

    @staticmethod
    def remove_tag_from_recipe(
        db: Session,
        recipe_id: int,
        tag_id: int,
        user: UserModel,
    ) -> None:
        recipe = db.query(RecipeModel).filter(RecipeModel.id == recipe_id).first()
        if not recipe:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recipe {recipe_id} not found",
            )

        if user.role != UserRole.ADMIN.value and recipe.user_id != user.id:
            raise StarletteHTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to modify tags on this recipe",
            )

        rt = db.query(RecipeTagModel).filter(
            RecipeTagModel.recipe_id == recipe_id,
            RecipeTagModel.tag_id == tag_id,
        ).first()
        if not rt:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tag {tag_id} is not associated with recipe {recipe_id}",
            )

        db.delete(rt)
        db.commit()

    @staticmethod
    def get_recipe_nutrition(db: Session, recipe_id: int) -> RecipeNutritionResponseSchema:
        recipe = db.query(RecipeModel).filter(RecipeModel.id == recipe_id).first()
        if not recipe:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Recipe {recipe_id} not found",
            )

        total_kcal = 0.0
        total_protein = 0.0
        total_carbs = 0.0
        total_fat = 0.0

        component_details: List[ComponentNutritionDetailSchema] = []
        for c in recipe.components:
            ratio = c.quantity_grams / 100.0
            c_kcal = round(c.ingredient.calories_kcal * ratio, 2)
            c_prot = round(c.ingredient.protein_g * ratio, 2)
            c_carb = round(c.ingredient.carbs_g * ratio, 2)
            c_fat = round(c.ingredient.fat_g * ratio, 2)

            total_kcal += c_kcal
            total_protein += c_prot
            total_carbs += c_carb
            total_fat += c_fat

            component_details.append(
                ComponentNutritionDetailSchema(
                    ingredient_id=c.ingredient_id,
                    ingredient_name=c.ingredient.name,
                    quantity_grams=c.quantity_grams,
                    calories_kcal=c_kcal,
                    protein_g=c_prot,
                    carbs_g=c_carb,
                    fat_g=c_fat,
                )
            )

        servings = recipe.servings if recipe.servings > 0 else 1
        per_serv_kcal = round(total_kcal / servings, 2)
        per_serv_prot = round(total_protein / servings, 2)
        per_serv_carb = round(total_carbs / servings, 2)
        per_serv_fat = round(total_fat / servings, 2)

        return RecipeNutritionResponseSchema(
            recipe_id=recipe.id,
            servings=servings,
            total_calories_kcal=round(total_kcal, 2),
            total_protein_g=round(total_protein, 2),
            total_carbs_g=round(total_carbs, 2),
            total_fat_g=round(total_fat, 2),
            per_serving_calories_kcal=per_serv_kcal,
            per_serving_protein_g=per_serv_prot,
            per_serving_carbs_g=per_serv_carb,
            per_serving_fat_g=per_serv_fat,
            components=component_details,
        )

    @staticmethod
    def search_recipes(
        db: Session,
        tag_ids: Optional[List[int]] = None,
        max_calories: Optional[float] = None,
        min_protein: Optional[float] = None,
        q: Optional[str] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> RecipeListResponseSchema:
        query = db.query(RecipeModel)
        if after_id is not None:
            query = query.filter(RecipeModel.id > after_id)

        all_recipes = query.order_by(RecipeModel.id.asc()).all()
        matched: List[RecipeModel] = []

        for r in all_recipes:
            # Filter q (literal search 1..80 in title or instructions)
            if q is not None and q.strip():
                search_term = q.strip().lower()
                if search_term not in r.title.lower() and search_term not in r.instructions.lower():
                    continue

            # Filter tag_ids (AND semantics: must contain all tag_ids)
            if tag_ids:
                r_tag_ids = {rt.tag_id for rt in r.recipe_tags}
                if not all(t_id in r_tag_ids for t_id in tag_ids):
                    continue

            # Calculate nutrition if needed for filters
            if max_calories is not None or min_protein is not None:
                total_kcal = 0.0
                total_prot = 0.0
                for c in r.components:
                    ratio = c.quantity_grams / 100.0
                    total_kcal += c.ingredient.calories_kcal * ratio
                    total_prot += c.ingredient.protein_g * ratio

                if max_calories is not None and total_kcal > max_calories:
                    continue
                if min_protein is not None and total_prot < min_protein:
                    continue

            matched.append(r)

        has_more = len(matched) > limit
        items = matched[:limit]
        next_after_id = items[-1].id if has_more and items else None

        return RecipeListResponseSchema(
            items=[RecipeVariantCController._format_recipe(r) for r in items],
            limit=limit,
            next_after_id=next_after_id,
        )


# -----------------------------------------------------------------------------
# FastAPI App Definition & Exception Handlers
# -----------------------------------------------------------------------------
app = FastAPI(
    title="Recipe Management - Variant C",
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
    return RecipeVariantCController.list_ingredients(db, limit, after_id)


@app.post("/ingredients", response_model=IngredientResponseSchema, status_code=status.HTTP_201_CREATED)
def create_ingredient(
    payload: IngredientCreateSchema,
    response: Response,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    ing = RecipeVariantCController.create_ingredient(db, payload)
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
    return RecipeVariantCController.get_ingredient(db, ingredient_id)


@app.patch("/ingredients/{ingredient_id}", response_model=IngredientResponseSchema, status_code=status.HTTP_200_OK)
def patch_ingredient(
    ingredient_id: int,
    payload: IngredientPatchSchema,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    if ingredient_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return RecipeVariantCController.patch_ingredient(db, ingredient_id, payload)


@app.delete("/ingredients/{ingredient_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_ingredient(
    ingredient_id: int,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    if ingredient_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    RecipeVariantCController.delete_ingredient(db, ingredient_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/recipes/search", response_model=RecipeListResponseSchema, status_code=status.HTTP_200_OK)
def search_recipes(
    tag_ids: Optional[List[Annotated[int, Field(gt=0)]]] = Query(None),
    max_calories: Optional[float] = Query(None, ge=0),
    min_protein: Optional[float] = Query(None, ge=0),
    q: Optional[str] = Query(None, min_length=1, max_length=80),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    return RecipeVariantCController.search_recipes(db, tag_ids, max_calories, min_protein, q, limit, after_id)


@app.get("/recipes", response_model=RecipeListResponseSchema, status_code=status.HTTP_200_OK)
def list_recipes(
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    return RecipeVariantCController.list_recipes(db, limit, after_id)


@app.post("/recipes", response_model=RecipeResponseSchema, status_code=status.HTTP_201_CREATED)
def create_recipe(
    payload: RecipeCreateSchema,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    recipe = RecipeVariantCController.create_recipe(db, current_user, payload)
    response.headers["Location"] = f"/recipes/{recipe.id}"
    return RecipeVariantCController._format_recipe(recipe)


@app.get("/recipes/{recipe_id}", response_model=RecipeResponseSchema, status_code=status.HTTP_200_OK)
def get_recipe(
    recipe_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if recipe_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    recipe = RecipeVariantCController.get_recipe(db, recipe_id)
    return RecipeVariantCController._format_recipe(recipe)


@app.patch("/recipes/{recipe_id}", response_model=RecipeResponseSchema, status_code=status.HTTP_200_OK)
def patch_recipe(
    recipe_id: int,
    payload: RecipePatchSchema,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if recipe_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    recipe = RecipeVariantCController.patch_recipe(db, recipe_id, current_user, payload)
    return RecipeVariantCController._format_recipe(recipe)


@app.delete("/recipes/{recipe_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_recipe(
    recipe_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if recipe_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    RecipeVariantCController.delete_recipe(db, recipe_id, current_user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/recipes/{recipe_id}/nutrition", response_model=RecipeNutritionResponseSchema, status_code=status.HTTP_200_OK)
def get_recipe_nutrition(
    recipe_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if recipe_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return RecipeVariantCController.get_recipe_nutrition(db, recipe_id)


@app.post("/recipes/{recipe_id}/tags", response_model=RecipeResponseSchema, status_code=status.HTTP_201_CREATED)
def add_tag_to_recipe(
    recipe_id: int,
    payload: RecipeAddTagSchema,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if recipe_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    recipe = RecipeVariantCController.add_tag_to_recipe(db, recipe_id, current_user, payload)
    response.headers["Location"] = f"/recipes/{recipe.id}/tags/{payload.tag_id}"
    return RecipeVariantCController._format_recipe(recipe)


@app.delete("/recipes/{recipe_id}/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_tag_from_recipe(
    recipe_id: int,
    tag_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if recipe_id <= 0 or tag_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    RecipeVariantCController.remove_tag_from_recipe(db, recipe_id, tag_id, current_user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/tags", response_model=TagListResponseSchema, status_code=status.HTTP_200_OK)
def list_tags(
    dietary_type: Optional[DietaryTypeEnum] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    return RecipeVariantCController.list_tags(db, dietary_type, limit, after_id)


@app.post("/tags", response_model=TagResponseSchema, status_code=status.HTTP_201_CREATED)
def create_tag(
    payload: TagCreateSchema,
    response: Response,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    tag = RecipeVariantCController.create_tag(db, payload)
    response.headers["Location"] = f"/tags/{tag.id}"
    return tag


@app.get("/tags/{tag_id}", response_model=TagResponseSchema, status_code=status.HTTP_200_OK)
def get_tag(
    tag_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if tag_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return RecipeVariantCController.get_tag(db, tag_id)


@app.patch("/tags/{tag_id}", response_model=TagResponseSchema, status_code=status.HTTP_200_OK)
def patch_tag(
    tag_id: int,
    payload: TagPatchSchema,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    if tag_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return RecipeVariantCController.patch_tag(db, tag_id, payload)


@app.delete("/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tag(
    tag_id: int,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    if tag_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    RecipeVariantCController.delete_tag(db, tag_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def custom_openapi() -> Dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema

    schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi
