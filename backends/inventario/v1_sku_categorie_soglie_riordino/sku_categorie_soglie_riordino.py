"""
Inventory Management - Variant C: SKU, categories, thresholds, and replenishment
CASE_ID: 8

Compliant with:
- Python 3.10+, FastAPI, Pydantic v2, SQLAlchemy ORM, SQLite in-memory with StaticPool.
- MVC architecture: SQLAlchemy models, Pydantic v2 schemas, static controllers, thin FastAPI routes.
- Fully autonomous module exporting `app`.
- Rigorous permission checks, atomic transactions, standardized error responses.
"""

from __future__ import annotations

import enum
import hashlib
import http
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
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
)
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# -----------------------------------------------------------------------------
# Logging Configuration
# -----------------------------------------------------------------------------
logger = logging.getLogger("inventario_variant_c")
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


class CategoryModel(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(80), unique=True, nullable=False, index=True)
    parent_id = Column(Integer, ForeignKey("categories.id"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    parent = relationship("CategoryModel", remote_side=[id], backref="children")
    products = relationship("ProductModel", back_populates="category")


class ProductModel(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sku = Column(String(32), unique=True, nullable=False, index=True)
    name = Column(String(160), nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False, index=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    category = relationship("CategoryModel", back_populates="products")
    stock_levels = relationship("StockLevelModel", back_populates="product", cascade="all, delete-orphan")
    reorder_rules = relationship("ReorderRuleModel", back_populates="product", cascade="all, delete-orphan")


class StockLevelModel(Base):
    __tablename__ = "stock_levels"
    __table_args__ = (
        UniqueConstraint("product_id", "location_code", name="uq_stock_product_location"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    location_code = Column(String(64), nullable=False, index=True)
    quantity = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    product = relationship("ProductModel", back_populates="stock_levels")


class ReorderRuleModel(Base):
    __tablename__ = "reorder_rules"
    __table_args__ = (
        UniqueConstraint("product_id", "location_code", name="uq_rule_product_location"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    location_code = Column(String(64), nullable=False, index=True)
    reorder_point = Column(Integer, nullable=False)
    target_level = Column(Integer, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    is_acknowledged = Column(Boolean, nullable=False, default=False)
    acknowledged_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    product = relationship("ProductModel", back_populates="reorder_rules")


# -----------------------------------------------------------------------------
# Pydantic Schemas (MVC: View / Data Transfer Objects)
# -----------------------------------------------------------------------------
class CategoryCreateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=2, max_length=80)
    parent_id: Optional[int] = Field(default=None, gt=0)


class CategoryPatchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=2, max_length=80)
    parent_id: Optional[int] = Field(default=None, gt=0)


class CategoryResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    parent_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class CategoryListResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[CategoryResponseSchema]
    limit: int
    next_after_id: Optional[int] = None


class ProductCreateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: str = Field(..., min_length=3, max_length=32, pattern=r"^[A-Z0-9-]{3,32}$")
    name: str = Field(..., min_length=2, max_length=160)
    category_id: int = Field(..., gt=0)
    is_active: bool = Field(default=True)


class ProductPatchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: Optional[str] = Field(default=None, min_length=3, max_length=32, pattern=r"^[A-Z0-9-]{3,32}$")
    name: Optional[str] = Field(default=None, min_length=2, max_length=160)
    category_id: Optional[int] = Field(default=None, gt=0)
    is_active: Optional[bool] = None


class ProductResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    sku: str
    name: str
    category_id: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


class ProductListResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[ProductResponseSchema]
    limit: int
    next_after_id: Optional[int] = None


class ReorderRuleCreateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(..., gt=0)
    location_code: str = Field(..., min_length=2, max_length=64)
    reorder_point: int = Field(..., ge=0, le=1000000)
    target_level: int = Field(..., ge=1, le=2000000)
    is_active: bool = Field(default=True)

    @model_validator(mode="after")
    def validate_levels(self) -> ReorderRuleCreateSchema:
        if self.target_level <= self.reorder_point:
            raise ValueError("target_level must be strictly greater than reorder_point")
        return self


class ReorderRulePatchSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reorder_point: Optional[int] = Field(default=None, ge=0, le=1000000)
    target_level: Optional[int] = Field(default=None, ge=1, le=2000000)
    is_active: Optional[bool] = None


class ReorderRuleResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    product_id: int
    location_code: str
    reorder_point: int
    target_level: int
    is_active: bool
    is_acknowledged: bool
    acknowledged_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class ReorderRuleListResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[ReorderRuleResponseSchema]
    limit: int
    next_after_id: Optional[int] = None


class ReorderCandidateItemSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: int
    product_id: int
    sku: str
    product_name: str
    location_code: str
    current_stock: int
    reorder_point: int
    target_level: int
    suggested_order_quantity: int
    is_acknowledged: bool


class ReorderCandidateListResponseSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[ReorderCandidateItemSchema]
    limit: int
    next_after_id: Optional[int] = None


class LocationStockDetailSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    location_code: str
    quantity: int
    reorder_point: Optional[int] = None
    target_level: Optional[int] = None
    is_below_reorder_point: bool = False


class ProductStockSummarySchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int
    sku: str
    name: str
    is_active: bool
    total_stock: int
    locations: List[LocationStockDetailSchema]


class StockLevelUpdateSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: int = Field(..., gt=0)
    location_code: str = Field(..., min_length=2, max_length=64)
    quantity: int = Field(..., ge=0, le=10000000)


class ErrorResponseSchema(BaseModel):
    detail: Any


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

        users = [
            UserModel(
                username="user1",
                role=UserRole.USER.value,
                token=user1_token,
                token_hash=hash_token(user1_token),
            ),
            UserModel(
                username="user2",
                role=UserRole.USER.value,
                token=user2_token,
                token_hash=hash_token(user2_token),
            ),
            UserModel(
                username="admin",
                role=UserRole.ADMIN.value,
                token=admin_token,
                token_hash=hash_token(admin_token),
            ),
        ]
        db.add_all(users)
        db.commit()

        # Seed categories
        cat_electronics = CategoryModel(name="Electronics", parent_id=None)
        db.add(cat_electronics)
        db.flush()

        cat_audio = CategoryModel(name="Audio Devices", parent_id=cat_electronics.id)
        db.add(cat_audio)
        db.flush()

        # Seed products
        prod_headphone = ProductModel(
            sku="HEADPHONE-001",
            name="Wireless Noise Cancelling Headphones",
            category_id=cat_audio.id,
            is_active=True,
        )
        prod_speaker = ProductModel(
            sku="SPEAKER-001",
            name="Bluetooth Portable Speaker",
            category_id=cat_audio.id,
            is_active=True,
        )
        db.add_all([prod_headphone, prod_speaker])
        db.flush()

        # Seed stock levels
        stock1 = StockLevelModel(
            product_id=prod_headphone.id,
            location_code="MAIN-WAREHOUSE",
            quantity=15,
        )
        stock2 = StockLevelModel(
            product_id=prod_speaker.id,
            location_code="MAIN-WAREHOUSE",
            quantity=3,
        )
        db.add_all([stock1, stock2])
        db.flush()

        # Seed reorder rules
        rule1 = ReorderRuleModel(
            product_id=prod_headphone.id,
            location_code="MAIN-WAREHOUSE",
            reorder_point=10,
            target_level=50,
            is_active=True,
        )
        rule2 = ReorderRuleModel(
            product_id=prod_speaker.id,
            location_code="MAIN-WAREHOUSE",
            reorder_point=5,
            target_level=30,
            is_active=True,
        )
        db.add_all([rule1, rule2])
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

        if method in ("POST", "PATCH", "PUT") and content_length_str:
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
            # Check route match to avoid masking 404/405
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
class InventoryVariantCController:
    @staticmethod
    def _check_category_cycle(db: Session, category_id: int, new_parent_id: int) -> bool:
        """Returns True if setting category's parent to new_parent_id creates a cycle."""
        curr = new_parent_id
        visited = {category_id}
        while curr is not None:
            if curr in visited:
                return True
            visited.add(curr)
            parent = db.query(CategoryModel).filter(CategoryModel.id == curr).first()
            if not parent or parent.parent_id is None:
                break
            curr = parent.parent_id
        return False

    @staticmethod
    def list_categories(
        db: Session,
        parent_id: Optional[int] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> CategoryListResponseSchema:
        query = db.query(CategoryModel)
        if parent_id is not None:
            query = query.filter(CategoryModel.parent_id == parent_id)
        if after_id is not None:
            query = query.filter(CategoryModel.id > after_id)

        rows = query.order_by(CategoryModel.id.asc()).limit(limit + 1).all()
        has_more = len(rows) > limit
        items = rows[:limit]
        next_after_id = items[-1].id if has_more and items else None

        return CategoryListResponseSchema(
            items=[
                CategoryResponseSchema(
                    id=c.id,
                    name=c.name,
                    parent_id=c.parent_id,
                    created_at=c.created_at,
                    updated_at=c.updated_at,
                )
                for c in items
            ],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_category(db: Session, payload: CategoryCreateSchema) -> CategoryModel:
        if payload.parent_id is not None:
            parent = db.query(CategoryModel).filter(CategoryModel.id == payload.parent_id).first()
            if not parent:
                raise StarletteHTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Parent category {payload.parent_id} not found",
                )

        existing = db.query(CategoryModel).filter(CategoryModel.name == payload.name).first()
        if existing:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Category with name '{payload.name}' already exists",
            )

        cat = CategoryModel(name=payload.name, parent_id=payload.parent_id)
        db.add(cat)
        db.commit()
        db.refresh(cat)
        return cat

    @staticmethod
    def get_category(db: Session, category_id: int) -> CategoryModel:
        cat = db.query(CategoryModel).filter(CategoryModel.id == category_id).first()
        if not cat:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Category {category_id} not found",
            )
        return cat

    @staticmethod
    def patch_category(db: Session, category_id: int, payload: CategoryPatchSchema) -> CategoryModel:
        cat = db.query(CategoryModel).filter(CategoryModel.id == category_id).first()
        if not cat:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Category {category_id} not found",
            )

        if payload.name is not None and payload.name != cat.name:
            existing = db.query(CategoryModel).filter(
                CategoryModel.name == payload.name,
                CategoryModel.id != category_id,
            ).first()
            if existing:
                raise StarletteHTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Category with name '{payload.name}' already exists",
                )
            cat.name = payload.name

        if payload.parent_id is not None:
            if payload.parent_id == category_id:
                raise StarletteHTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A category cannot be its own parent",
                )
            parent = db.query(CategoryModel).filter(CategoryModel.id == payload.parent_id).first()
            if not parent:
                raise StarletteHTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Parent category {payload.parent_id} not found",
                )
            if InventoryVariantCController._check_category_cycle(db, category_id, payload.parent_id):
                raise StarletteHTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Parent category assignment creates a circular dependency",
                )
            cat.parent_id = payload.parent_id

        cat.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(cat)
        return cat

    @staticmethod
    def delete_category(db: Session, category_id: int) -> None:
        cat = db.query(CategoryModel).filter(CategoryModel.id == category_id).first()
        if not cat:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Category {category_id} not found",
            )

        # Check for children or products
        has_children = db.query(CategoryModel).filter(CategoryModel.parent_id == category_id).first() is not None
        if has_children:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot delete category that has child subcategories",
            )

        has_products = db.query(ProductModel).filter(ProductModel.category_id == category_id).first() is not None
        if has_products:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot delete category that is referenced by products",
            )

        db.delete(cat)
        db.commit()

    @staticmethod
    def list_products(
        db: Session,
        category_id: Optional[int] = None,
        is_active: Optional[bool] = None,
        sku: Optional[str] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> ProductListResponseSchema:
        query = db.query(ProductModel)
        if category_id is not None:
            query = query.filter(ProductModel.category_id == category_id)
        if is_active is not None:
            query = query.filter(ProductModel.is_active == is_active)
        if sku is not None:
            query = query.filter(ProductModel.sku == sku)
        if after_id is not None:
            query = query.filter(ProductModel.id > after_id)

        rows = query.order_by(ProductModel.id.asc()).limit(limit + 1).all()
        has_more = len(rows) > limit
        items = rows[:limit]
        next_after_id = items[-1].id if has_more and items else None

        return ProductListResponseSchema(
            items=[
                ProductResponseSchema(
                    id=p.id,
                    sku=p.sku,
                    name=p.name,
                    category_id=p.category_id,
                    is_active=p.is_active,
                    created_at=p.created_at,
                    updated_at=p.updated_at,
                )
                for p in items
            ],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_product(db: Session, payload: ProductCreateSchema) -> ProductModel:
        cat = db.query(CategoryModel).filter(CategoryModel.id == payload.category_id).first()
        if not cat:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Category {payload.category_id} not found",
            )

        existing = db.query(ProductModel).filter(ProductModel.sku == payload.sku).first()
        if existing:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Product with SKU '{payload.sku}' already exists",
            )

        prod = ProductModel(
            sku=payload.sku,
            name=payload.name,
            category_id=payload.category_id,
            is_active=payload.is_active,
        )
        db.add(prod)
        db.commit()
        db.refresh(prod)
        return prod

    @staticmethod
    def get_product(db: Session, product_id: int) -> ProductModel:
        prod = db.query(ProductModel).filter(ProductModel.id == product_id).first()
        if not prod:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product {product_id} not found",
            )
        return prod

    @staticmethod
    def patch_product(db: Session, product_id: int, payload: ProductPatchSchema) -> ProductModel:
        prod = db.query(ProductModel).filter(ProductModel.id == product_id).first()
        if not prod:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product {product_id} not found",
            )

        if payload.sku is not None and payload.sku != prod.sku:
            existing = db.query(ProductModel).filter(
                ProductModel.sku == payload.sku,
                ProductModel.id != product_id,
            ).first()
            if existing:
                raise StarletteHTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Product with SKU '{payload.sku}' already exists",
                )
            prod.sku = payload.sku

        if payload.name is not None:
            prod.name = payload.name

        if payload.category_id is not None and payload.category_id != prod.category_id:
            cat = db.query(CategoryModel).filter(CategoryModel.id == payload.category_id).first()
            if not cat:
                raise StarletteHTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Category {payload.category_id} not found",
                )
            prod.category_id = payload.category_id

        if payload.is_active is not None and payload.is_active != prod.is_active:
            if not payload.is_active:
                # REQ-8-006: Deactivating products is prohibited while active reorder rules exist
                has_active_rules = db.query(ReorderRuleModel).filter(
                    ReorderRuleModel.product_id == product_id,
                    ReorderRuleModel.is_active == True,
                ).first() is not None
                if has_active_rules:
                    raise StarletteHTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Deactivating product is prohibited while active reorder rules exist",
                    )
            prod.is_active = payload.is_active

        prod.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(prod)
        return prod

    @staticmethod
    def delete_product(db: Session, product_id: int) -> None:
        prod = db.query(ProductModel).filter(ProductModel.id == product_id).first()
        if not prod:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product {product_id} not found",
            )

        has_rules = db.query(ReorderRuleModel).filter(ReorderRuleModel.product_id == product_id).first() is not None
        if has_rules:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot delete product that has reorder rules configured",
            )

        db.delete(prod)
        db.commit()

    @staticmethod
    def deactivate_product(db: Session, product_id: int) -> ProductModel:
        prod = db.query(ProductModel).filter(ProductModel.id == product_id).first()
        if not prod:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product {product_id} not found",
            )

        if not prod.is_active:
            return prod

        # REQ-8-006: Check active reorder rules
        has_active_rules = db.query(ReorderRuleModel).filter(
            ReorderRuleModel.product_id == product_id,
            ReorderRuleModel.is_active == True,
        ).first() is not None
        if has_active_rules:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Deactivating product is prohibited while active reorder rules exist",
            )

        prod.is_active = False
        prod.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(prod)
        return prod

    @staticmethod
    def get_stock_summary(db: Session, product_id: int) -> ProductStockSummarySchema:
        prod = db.query(ProductModel).filter(ProductModel.id == product_id).first()
        if not prod:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product {product_id} not found",
            )

        stocks = db.query(StockLevelModel).filter(StockLevelModel.product_id == product_id).all()
        rules = {
            r.location_code: r
            for r in db.query(ReorderRuleModel).filter(ReorderRuleModel.product_id == product_id).all()
        }

        total_stock = sum(s.quantity for s in stocks)
        locations_detail: List[LocationStockDetailSchema] = []

        seen_locations = set()
        for s in stocks:
            seen_locations.add(s.location_code)
            rule = rules.get(s.location_code)
            r_point = rule.reorder_point if rule else None
            t_level = rule.target_level if rule else None
            below = (s.quantity <= r_point) if r_point is not None else False
            locations_detail.append(
                LocationStockDetailSchema(
                    location_code=s.location_code,
                    quantity=s.quantity,
                    reorder_point=r_point,
                    target_level=t_level,
                    is_below_reorder_point=below,
                )
            )

        # Include locations that have reorder rules even if stock record is not created
        for loc, rule in rules.items():
            if loc not in seen_locations:
                locations_detail.append(
                    LocationStockDetailSchema(
                        location_code=loc,
                        quantity=0,
                        reorder_point=rule.reorder_point,
                        target_level=rule.target_level,
                        is_below_reorder_point=True,
                    )
                )

        return ProductStockSummarySchema(
            product_id=prod.id,
            sku=prod.sku,
            name=prod.name,
            is_active=prod.is_active,
            total_stock=total_stock,
            locations=locations_detail,
        )

    @staticmethod
    def list_reorder_rules(
        db: Session,
        product_id: Optional[int] = None,
        location_code: Optional[str] = None,
        is_active: Optional[bool] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> ReorderRuleListResponseSchema:
        query = db.query(ReorderRuleModel)
        if product_id is not None:
            query = query.filter(ReorderRuleModel.product_id == product_id)
        if location_code is not None:
            query = query.filter(ReorderRuleModel.location_code == location_code)
        if is_active is not None:
            query = query.filter(ReorderRuleModel.is_active == is_active)
        if after_id is not None:
            query = query.filter(ReorderRuleModel.id > after_id)

        rows = query.order_by(ReorderRuleModel.id.asc()).limit(limit + 1).all()
        has_more = len(rows) > limit
        items = rows[:limit]
        next_after_id = items[-1].id if has_more and items else None

        return ReorderRuleListResponseSchema(
            items=[
                ReorderRuleResponseSchema(
                    id=r.id,
                    product_id=r.product_id,
                    location_code=r.location_code,
                    reorder_point=r.reorder_point,
                    target_level=r.target_level,
                    is_active=r.is_active,
                    is_acknowledged=r.is_acknowledged,
                    acknowledged_at=r.acknowledged_at,
                    created_at=r.created_at,
                    updated_at=r.updated_at,
                )
                for r in items
            ],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_reorder_rule(db: Session, payload: ReorderRuleCreateSchema) -> ReorderRuleModel:
        prod = db.query(ProductModel).filter(ProductModel.id == payload.product_id).first()
        if not prod:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product {payload.product_id} not found",
            )

        existing = db.query(ReorderRuleModel).filter(
            ReorderRuleModel.product_id == payload.product_id,
            ReorderRuleModel.location_code == payload.location_code,
        ).first()
        if existing:
            raise StarletteHTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Reorder rule for product {payload.product_id} at location '{payload.location_code}' already exists",
            )

        rule = ReorderRuleModel(
            product_id=payload.product_id,
            location_code=payload.location_code,
            reorder_point=payload.reorder_point,
            target_level=payload.target_level,
            is_active=payload.is_active,
            is_acknowledged=False,
            acknowledged_at=None,
        )
        db.add(rule)
        db.commit()
        db.refresh(rule)
        return rule

    @staticmethod
    def get_reorder_rule(db: Session, rule_id: int) -> ReorderRuleModel:
        rule = db.query(ReorderRuleModel).filter(ReorderRuleModel.id == rule_id).first()
        if not rule:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Reorder rule {rule_id} not found",
            )
        return rule

    @staticmethod
    def patch_reorder_rule(db: Session, rule_id: int, payload: ReorderRulePatchSchema) -> ReorderRuleModel:
        rule = db.query(ReorderRuleModel).filter(ReorderRuleModel.id == rule_id).first()
        if not rule:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Reorder rule {rule_id} not found",
            )

        new_point = payload.reorder_point if payload.reorder_point is not None else rule.reorder_point
        new_target = payload.target_level if payload.target_level is not None else rule.target_level

        if new_target <= new_point:
            raise StarletteHTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="target_level must be strictly greater than reorder_point",
            )

        if payload.reorder_point is not None:
            rule.reorder_point = payload.reorder_point
        if payload.target_level is not None:
            rule.target_level = payload.target_level
        if payload.is_active is not None:
            rule.is_active = payload.is_active

        rule.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(rule)
        return rule

    @staticmethod
    def delete_reorder_rule(db: Session, rule_id: int) -> None:
        rule = db.query(ReorderRuleModel).filter(ReorderRuleModel.id == rule_id).first()
        if not rule:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Reorder rule {rule_id} not found",
            )
        db.delete(rule)
        db.commit()

    @staticmethod
    def acknowledge_reorder_rule(db: Session, rule_id: int) -> ReorderRuleModel:
        rule = db.query(ReorderRuleModel).filter(ReorderRuleModel.id == rule_id).first()
        if not rule:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Reorder rule {rule_id} not found",
            )

        rule.is_acknowledged = True
        rule.acknowledged_at = datetime.now(timezone.utc)
        rule.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(rule)
        return rule

    @staticmethod
    def list_reorder_candidates(
        db: Session,
        location_code: Optional[str] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> ReorderCandidateListResponseSchema:
        # Candidate rule search
        query = db.query(ReorderRuleModel, ProductModel).join(
            ProductModel, ReorderRuleModel.product_id == ProductModel.id
        ).filter(
            ReorderRuleModel.is_active == True,
            ProductModel.is_active == True,
        )

        if location_code is not None:
            query = query.filter(ReorderRuleModel.location_code == location_code)
        if after_id is not None:
            query = query.filter(ReorderRuleModel.id > after_id)

        rows = query.order_by(ReorderRuleModel.id.asc()).all()

        candidates: List[ReorderCandidateItemSchema] = []
        for rule, prod in rows:
            stock = db.query(StockLevelModel).filter(
                StockLevelModel.product_id == prod.id,
                StockLevelModel.location_code == rule.location_code,
            ).first()
            current_qty = stock.quantity if stock else 0

            if current_qty <= rule.reorder_point:
                suggested = rule.target_level - current_qty
                candidates.append(
                    ReorderCandidateItemSchema(
                        rule_id=rule.id,
                        product_id=prod.id,
                        sku=prod.sku,
                        product_name=prod.name,
                        location_code=rule.location_code,
                        current_stock=current_qty,
                        reorder_point=rule.reorder_point,
                        target_level=rule.target_level,
                        suggested_order_quantity=suggested,
                        is_acknowledged=rule.is_acknowledged,
                    )
                )

        has_more = len(candidates) > limit
        items = candidates[:limit]
        next_after_id = items[-1].rule_id if has_more and items else None

        return ReorderCandidateListResponseSchema(
            items=items,
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def set_stock_level(db: Session, payload: StockLevelUpdateSchema) -> StockLevelModel:
        prod = db.query(ProductModel).filter(ProductModel.id == payload.product_id).first()
        if not prod:
            raise StarletteHTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Product {payload.product_id} not found",
            )

        stock = db.query(StockLevelModel).filter(
            StockLevelModel.product_id == payload.product_id,
            StockLevelModel.location_code == payload.location_code,
        ).first()

        if not stock:
            stock = StockLevelModel(
                product_id=payload.product_id,
                location_code=payload.location_code,
                quantity=payload.quantity,
            )
            db.add(stock)
        else:
            stock.quantity = payload.quantity
            stock.updated_at = datetime.now(timezone.utc)

        db.commit()
        db.refresh(stock)
        return stock


# -----------------------------------------------------------------------------
# FastAPI App Definition & Exception Handlers
# -----------------------------------------------------------------------------
app = FastAPI(
    title="Inventory Management - Variant C",
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
    responses={503: {"model": ErrorResponseSchema, "description": "Service Unavailable"}},
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


@app.get("/categories", response_model=CategoryListResponseSchema, status_code=status.HTTP_200_OK)
def list_categories(
    parent_id: Optional[int] = Query(None, gt=0),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    return InventoryVariantCController.list_categories(db, parent_id, limit, after_id)


@app.post("/categories", response_model=CategoryResponseSchema, status_code=status.HTTP_201_CREATED)
def create_category(
    payload: CategoryCreateSchema,
    response: Response,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    cat = InventoryVariantCController.create_category(db, payload)
    response.headers["Location"] = f"/categories/{cat.id}"
    return cat


@app.get("/categories/{category_id}", response_model=CategoryResponseSchema, status_code=status.HTTP_200_OK)
def get_category(
    category_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if category_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return InventoryVariantCController.get_category(db, category_id)


@app.patch("/categories/{category_id}", response_model=CategoryResponseSchema, status_code=status.HTTP_200_OK)
def patch_category(
    category_id: int,
    payload: CategoryPatchSchema,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    if category_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return InventoryVariantCController.patch_category(db, category_id, payload)


@app.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_category(
    category_id: int,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    if category_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    InventoryVariantCController.delete_category(db, category_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/products", response_model=ProductListResponseSchema, status_code=status.HTTP_200_OK)
def list_products(
    category_id: Optional[int] = Query(None, gt=0),
    is_active: Optional[bool] = Query(None),
    sku: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    return InventoryVariantCController.list_products(db, category_id, is_active, sku, limit, after_id)


@app.post("/products", response_model=ProductResponseSchema, status_code=status.HTTP_201_CREATED)
def create_product(
    payload: ProductCreateSchema,
    response: Response,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    prod = InventoryVariantCController.create_product(db, payload)
    response.headers["Location"] = f"/products/{prod.id}"
    return prod


@app.get("/products/{product_id}", response_model=ProductResponseSchema, status_code=status.HTTP_200_OK)
def get_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if product_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return InventoryVariantCController.get_product(db, product_id)


@app.patch("/products/{product_id}", response_model=ProductResponseSchema, status_code=status.HTTP_200_OK)
def patch_product(
    product_id: int,
    payload: ProductPatchSchema,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    if product_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return InventoryVariantCController.patch_product(db, product_id, payload)


@app.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    if product_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    InventoryVariantCController.delete_product(db, product_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/products/{product_id}/deactivate", response_model=ProductResponseSchema, status_code=status.HTTP_200_OK)
def deactivate_product(
    product_id: int,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    if product_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return InventoryVariantCController.deactivate_product(db, product_id)


@app.get("/products/{product_id}/stock-summary", response_model=ProductStockSummarySchema, status_code=status.HTTP_200_OK)
def get_product_stock_summary(
    product_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if product_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return InventoryVariantCController.get_stock_summary(db, product_id)


@app.get("/reorder-rules", response_model=ReorderRuleListResponseSchema, status_code=status.HTTP_200_OK)
def list_reorder_rules(
    product_id: Optional[int] = Query(None, gt=0),
    location_code: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    return InventoryVariantCController.list_reorder_rules(db, product_id, location_code, is_active, limit, after_id)


@app.post("/reorder-rules", response_model=ReorderRuleResponseSchema, status_code=status.HTTP_201_CREATED)
def create_reorder_rule(
    payload: ReorderRuleCreateSchema,
    response: Response,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    rule = InventoryVariantCController.create_reorder_rule(db, payload)
    response.headers["Location"] = f"/reorder-rules/{rule.id}"
    return rule


@app.get("/reorder-rules/{reorderrule_id}", response_model=ReorderRuleResponseSchema, status_code=status.HTTP_200_OK)
def get_reorder_rule(
    reorderrule_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    if reorderrule_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return InventoryVariantCController.get_reorder_rule(db, reorderrule_id)


@app.patch("/reorder-rules/{reorderrule_id}", response_model=ReorderRuleResponseSchema, status_code=status.HTTP_200_OK)
def patch_reorder_rule(
    reorderrule_id: int,
    payload: ReorderRulePatchSchema,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    if reorderrule_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return InventoryVariantCController.patch_reorder_rule(db, reorderrule_id, payload)


@app.delete("/reorder-rules/{reorderrule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_reorder_rule(
    reorderrule_id: int,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    if reorderrule_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    InventoryVariantCController.delete_reorder_rule(db, reorderrule_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/reorder-rules/{reorderrule_id}/acknowledge", response_model=ReorderRuleResponseSchema, status_code=status.HTTP_200_OK)
def acknowledge_reorder_rule(
    reorderrule_id: int,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    if reorderrule_id <= 0:
        raise StarletteHTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource id must be positive")
    return InventoryVariantCController.acknowledge_reorder_rule(db, reorderrule_id)


@app.get("/reorder-candidates", response_model=ReorderCandidateListResponseSchema, status_code=status.HTTP_200_OK)
def list_reorder_candidates(
    location_code: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(get_current_user),
):
    return InventoryVariantCController.list_reorder_candidates(db, location_code, limit, after_id)


@app.post("/stock-levels", status_code=status.HTTP_200_OK)
def set_stock_level(
    payload: StockLevelUpdateSchema,
    db: Session = Depends(get_db),
    current_admin: UserModel = Depends(require_admin),
):
    return InventoryVariantCController.set_stock_level(db, payload)
