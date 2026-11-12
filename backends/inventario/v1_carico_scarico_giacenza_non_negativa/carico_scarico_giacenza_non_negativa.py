"""
Inventory Management - Variant A: Stock intake, issue, and non-negative inventory
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
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
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
logger = logging.getLogger("inventario_variant_a")
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


class MovementTypeEnum(str, enum.Enum):
    INTAKE = "INTAKE"
    ISSUE = "ISSUE"
    ADJUSTMENT = "ADJUSTMENT"
    REVERSAL = "REVERSAL"


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


class ProductModel(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(160), unique=True, nullable=False, index=True)
    active = Column(Boolean, nullable=False, default=True)
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    stock_levels = relationship("StockLevelModel", back_populates="product")
    movements = relationship("StockMovementModel", back_populates="product")


class StockLevelModel(Base):
    __tablename__ = "stock_levels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    location = Column(String(80), nullable=False, index=True)
    quantity = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    product = relationship("ProductModel", back_populates="stock_levels")


class StockMovementModel(Base):
    __tablename__ = "stock_movements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    location = Column(String(80), nullable=False, index=True)
    movement_type = Column(String(20), nullable=False, index=True)
    quantity = Column(Float, nullable=False)
    reference = Column(String(160), nullable=True)
    is_reversed = Column(Boolean, nullable=False, default=False)
    reversed_movement_id = Column(Integer, ForeignKey("stock_movements.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    product = relationship("ProductModel", back_populates="movements")


# -----------------------------------------------------------------------------
# Custom Domain Exceptions
# -----------------------------------------------------------------------------
class AppException(Exception):
    def __init__(self, status_code: int, detail: str, error_type: str, headers: Optional[Dict[str, str]] = None):
        self.status_code = status_code
        self.detail = detail
        self.error_type = error_type
        self.headers = headers or {}
        super().__init__(detail)


# -----------------------------------------------------------------------------
# Pydantic Schemas (MVC: View / Data Transfer Objects)
# -----------------------------------------------------------------------------
class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: str
    status_code: int
    error_type: str


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    database: str


# Product Schemas
class ProductCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=2, max_length=160)
    active: Optional[bool] = True

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: str) -> str:
        s = v.strip()
        if len(s) < 2:
            raise ValueError("name must have at least 2 characters")
        return s


class ProductPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=2, max_length=160)
    active: Optional[bool] = None

    @field_validator("name")
    @classmethod
    def strip_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if len(s) < 2:
            raise ValueError("name must have at least 2 characters")
        return s

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("name", "active"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class ProductResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    name: str
    active: bool
    created_at: str
    updated_at: str


class ProductListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[ProductResponse]
    limit: int
    next_after_id: Optional[int]


# Stock Level Schemas
class StockLevelCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int = Field(..., gt=0)
    location: str = Field(..., min_length=2, max_length=80)
    quantity: Optional[float] = Field(0.0, ge=0.0, le=1000000.0)

    @field_validator("location")
    @classmethod
    def strip_location(cls, v: str) -> str:
        s = v.strip()
        if len(s) < 2:
            raise ValueError("location must have at least 2 characters")
        return s


class StockLevelPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location: Optional[str] = Field(None, min_length=2, max_length=80)
    quantity: Optional[float] = Field(None, ge=0.0, le=1000000.0)

    @field_validator("location")
    @classmethod
    def strip_location(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if len(s) < 2:
            raise ValueError("location must have at least 2 characters")
        return s

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("location", "quantity"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class StockLevelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    product_id: int
    location: str
    quantity: float
    created_at: str
    updated_at: str


class StockLevelListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[StockLevelResponse]
    limit: int
    next_after_id: Optional[int]


# Stock Movement Schemas
class StockMovementCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int = Field(..., gt=0)
    location: str = Field(..., min_length=2, max_length=80)
    movement_type: MovementTypeEnum
    quantity: float = Field(..., ge=0.001, le=1000000.0)
    reference: Optional[str] = Field(None, max_length=160)

    @field_validator("location")
    @classmethod
    def strip_location(cls, v: str) -> str:
        s = v.strip()
        if len(s) < 2:
            raise ValueError("location must have at least 2 characters")
        return s

    @model_validator(mode="after")
    def validate_movement_type(self):
        if self.movement_type == MovementTypeEnum.REVERSAL:
            raise ValueError("REVERSAL movements cannot be created directly; use the /reverse endpoint")
        return self


class StockMovementPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: Optional[str] = Field(None, max_length=160)

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        return self


class StockMovementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    product_id: int
    location: str
    movement_type: MovementTypeEnum
    quantity: float
    reference: Optional[str]
    is_reversed: bool
    reversed_movement_id: Optional[int]
    created_at: str


class StockMovementListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[StockMovementResponse]
    limit: int
    next_after_id: Optional[int]


class ReconcileStockRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actual_quantity: float = Field(..., ge=0.0, le=1000000.0)


class AvailabilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_id: int
    location: str
    available_quantity: float


# -----------------------------------------------------------------------------
# Database Dependency & Auth (MVC: Controller)
# -----------------------------------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class AuthController:
    @staticmethod
    def get_current_user(
        authorization: Optional[str] = Header(None),
        db: Session = Depends(get_db),
    ) -> UserModel:
        if not authorization:
            raise AppException(401, "Authorization header required", "Unauthorized")
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise AppException(401, "Invalid authorization scheme, Bearer required", "Unauthorized")

        raw_token = parts[1]
        token_hashed = hash_token(raw_token)
        user = db.scalar(select(UserModel).where(UserModel.token_hash == token_hashed))
        if not user:
            raise AppException(401, "Invalid or expired token", "Unauthorized")
        return user


# -----------------------------------------------------------------------------
# Business Controllers (MVC: Controller)
# -----------------------------------------------------------------------------
class ProductController:
    @classmethod
    def serialize(cls, p: ProductModel) -> ProductResponse:
        return ProductResponse(
            id=p.id,
            name=p.name,
            active=p.active,
            created_at=p.created_at.isoformat(),
            updated_at=p.updated_at.isoformat(),
        )

    @classmethod
    def get_by_id(cls, db: Session, product_id: int) -> ProductModel:
        if product_id <= 0:
            raise AppException(404, "Product not found", "Not Found")
        product = db.scalar(select(ProductModel).where(ProductModel.id == product_id, ProductModel.is_deleted == False))
        if not product:
            raise AppException(404, "Product not found", "Not Found")
        return product

    @classmethod
    def list_products(
        cls,
        db: Session,
        name: Optional[str] = None,
        active: Optional[bool] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> ProductListResponse:
        query = select(ProductModel).where(ProductModel.is_deleted == False)
        if name is not None:
            query = query.where(ProductModel.name.ilike(f"%{name.strip()}%"))
        if active is not None:
            query = query.where(ProductModel.active == active)
        if after_id is not None:
            query = query.where(ProductModel.id > after_id)

        query = query.order_by(ProductModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return ProductListResponse(
            items=[cls.serialize(r) for r in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def create_product(cls, db: Session, current_user: UserModel, payload: ProductCreateRequest) -> ProductModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins create products", "Forbidden")

        if db.scalar(select(ProductModel).where(ProductModel.name == payload.name, ProductModel.is_deleted == False)):
            raise AppException(409, "Product with this name already exists", "Conflict")

        product = ProductModel(
            name=payload.name,
            active=payload.active if payload.active is not None else True,
            is_deleted=False,
        )
        db.add(product)
        try:
            db.commit()
            db.refresh(product)
            return product
        except Exception:
            db.rollback()
            raise AppException(500, "Database error creating product", "Internal Server Error")

    @classmethod
    def patch_product(cls, db: Session, current_user: UserModel, product_id: int, payload: ProductPatchRequest) -> ProductModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins update products", "Forbidden")

        product = cls.get_by_id(db, product_id)

        if payload.name is not None and payload.name != product.name:
            if db.scalar(select(ProductModel).where(ProductModel.name == payload.name, ProductModel.id != product.id, ProductModel.is_deleted == False)):
                raise AppException(409, "Product with this name already exists", "Conflict")
            product.name = payload.name

        if payload.active is not None:
            product.active = payload.active

        try:
            db.commit()
            db.refresh(product)
            return product
        except Exception:
            db.rollback()
            raise AppException(500, "Database error updating product", "Internal Server Error")

    @classmethod
    def delete_product(cls, db: Session, current_user: UserModel, product_id: int) -> None:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins delete products", "Forbidden")

        product = cls.get_by_id(db, product_id)

        # Relation constraints: cannot delete if active stock > 0
        has_positive_stock = db.scalar(
            select(StockLevelModel).where(
                StockLevelModel.product_id == product.id,
                StockLevelModel.quantity > 0,
            ).limit(1)
        )
        if has_positive_stock:
            raise AppException(409, "Cannot delete product with existing non-zero stock", "Conflict")

        product.is_deleted = True
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(500, "Database error deleting product", "Internal Server Error")


class StockLevelController:
    @classmethod
    def serialize(cls, s: StockLevelModel) -> StockLevelResponse:
        return StockLevelResponse(
            id=s.id,
            product_id=s.product_id,
            location=s.location,
            quantity=s.quantity,
            created_at=s.created_at.isoformat(),
            updated_at=s.updated_at.isoformat(),
        )

    @classmethod
    def get_by_id(cls, db: Session, stocklevel_id: int) -> StockLevelModel:
        if stocklevel_id <= 0:
            raise AppException(404, "Stock level not found", "Not Found")
        sl = db.scalar(select(StockLevelModel).where(StockLevelModel.id == stocklevel_id))
        if not sl:
            raise AppException(404, "Stock level not found", "Not Found")
        return sl

    @classmethod
    def list_stock_levels(
        cls,
        db: Session,
        product_id: Optional[int] = None,
        location: Optional[str] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> StockLevelListResponse:
        query = select(StockLevelModel)
        if product_id is not None:
            query = query.where(StockLevelModel.product_id == product_id)
        if location is not None:
            query = query.where(StockLevelModel.location == location.strip())
        if after_id is not None:
            query = query.where(StockLevelModel.id > after_id)

        query = query.order_by(StockLevelModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return StockLevelListResponse(
            items=[cls.serialize(r) for r in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def create_stock_level(cls, db: Session, current_user: UserModel, payload: StockLevelCreateRequest) -> StockLevelModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins create stock levels", "Forbidden")

        ProductController.get_by_id(db, payload.product_id)

        # Check unique (product_id, location)
        existing = db.scalar(
            select(StockLevelModel).where(
                StockLevelModel.product_id == payload.product_id,
                StockLevelModel.location == payload.location,
            )
        )
        if existing:
            raise AppException(409, "Stock level for this product and location already exists", "Conflict")

        stock = StockLevelModel(
            product_id=payload.product_id,
            location=payload.location,
            quantity=payload.quantity if payload.quantity is not None else 0.0,
        )
        db.add(stock)
        try:
            db.commit()
            db.refresh(stock)
            return stock
        except Exception:
            db.rollback()
            raise AppException(500, "Database error creating stock level", "Internal Server Error")

    @classmethod
    def patch_stock_level(cls, db: Session, current_user: UserModel, stocklevel_id: int, payload: StockLevelPatchRequest) -> StockLevelModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins update stock levels", "Forbidden")

        stock = cls.get_by_id(db, stocklevel_id)

        if payload.location is not None and payload.location != stock.location:
            dup = db.scalar(
                select(StockLevelModel).where(
                    StockLevelModel.product_id == stock.product_id,
                    StockLevelModel.location == payload.location,
                    StockLevelModel.id != stock.id,
                )
            )
            if dup:
                raise AppException(409, "Stock level for target location already exists", "Conflict")
            stock.location = payload.location

        if payload.quantity is not None:
            if payload.quantity < 0:
                raise AppException(409, "Stock quantity cannot be negative", "Conflict")
            stock.quantity = payload.quantity

        try:
            db.commit()
            db.refresh(stock)
            return stock
        except Exception:
            db.rollback()
            raise AppException(500, "Database error updating stock level", "Internal Server Error")

    @classmethod
    def delete_stock_level(cls, db: Session, current_user: UserModel, stocklevel_id: int) -> None:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins delete stock levels", "Forbidden")

        stock = cls.get_by_id(db, stocklevel_id)
        if stock.quantity > 0:
            raise AppException(409, "Cannot delete stock level with quantity greater than zero", "Conflict")

        db.delete(stock)
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(500, "Database error deleting stock level", "Internal Server Error")

    @classmethod
    def reconcile_stock(cls, db: Session, current_user: UserModel, stocklevel_id: int, payload: ReconcileStockRequest) -> StockLevelModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins reconcile stock", "Forbidden")

        stock = cls.get_by_id(db, stocklevel_id)
        current_qty = stock.quantity
        diff = payload.actual_quantity - current_qty

        # Idempotence: if already reconciled to actual_quantity
        if current_qty == payload.actual_quantity:
            return stock

        stock.quantity = payload.actual_quantity

        movement = StockMovementModel(
            product_id=stock.product_id,
            location=stock.location,
            movement_type=MovementTypeEnum.ADJUSTMENT.value,
            quantity=abs(diff),
            reference=f"Stock reconciliation from {current_qty} to {payload.actual_quantity}",
            is_reversed=False,
        )
        db.add(movement)

        try:
            db.commit()
            db.refresh(stock)
            return stock
        except Exception:
            db.rollback()
            raise AppException(500, "Database error reconciling stock", "Internal Server Error")


class StockMovementController:
    @classmethod
    def serialize(cls, m: StockMovementModel) -> StockMovementResponse:
        return StockMovementResponse(
            id=m.id,
            product_id=m.product_id,
            location=m.location,
            movement_type=MovementTypeEnum(m.movement_type),
            quantity=m.quantity,
            reference=m.reference,
            is_reversed=m.is_reversed,
            reversed_movement_id=m.reversed_movement_id,
            created_at=m.created_at.isoformat(),
        )

    @classmethod
    def get_by_id(cls, db: Session, stockmovement_id: int) -> StockMovementModel:
        if stockmovement_id <= 0:
            raise AppException(404, "Stock movement not found", "Not Found")
        mov = db.scalar(select(StockMovementModel).where(StockMovementModel.id == stockmovement_id))
        if not mov:
            raise AppException(404, "Stock movement not found", "Not Found")
        return mov

    @classmethod
    def list_movements(
        cls,
        db: Session,
        product_id: Optional[int] = None,
        location: Optional[str] = None,
        movement_type: Optional[MovementTypeEnum] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> StockMovementListResponse:
        query = select(StockMovementModel)
        if product_id is not None:
            query = query.where(StockMovementModel.product_id == product_id)
        if location is not None:
            query = query.where(StockMovementModel.location == location.strip())
        if movement_type is not None:
            query = query.where(StockMovementModel.movement_type == movement_type.value)
        if after_id is not None:
            query = query.where(StockMovementModel.id > after_id)

        query = query.order_by(StockMovementModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return StockMovementListResponse(
            items=[cls.serialize(r) for r in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def create_movement(cls, db: Session, current_user: UserModel, payload: StockMovementCreateRequest) -> StockMovementModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins create movements", "Forbidden")

        product = ProductController.get_by_id(db, payload.product_id)
        if not product.active:
            raise AppException(409, "Cannot create movement for inactive product", "Conflict")

        stock = db.scalar(
            select(StockLevelModel).where(
                StockLevelModel.product_id == payload.product_id,
                StockLevelModel.location == payload.location,
            )
        )
        if not stock:
            stock = StockLevelModel(
                product_id=payload.product_id,
                location=payload.location,
                quantity=0.0,
            )
            db.add(stock)
            db.flush()

        # REQ-8-005: ISSUE and negative adjustment cannot make stock negative
        if payload.movement_type == MovementTypeEnum.INTAKE:
            stock.quantity += payload.quantity
        elif payload.movement_type == MovementTypeEnum.ISSUE:
            if stock.quantity < payload.quantity:
                raise AppException(409, "Insufficient stock for issue operation", "Conflict")
            stock.quantity -= payload.quantity
        elif payload.movement_type == MovementTypeEnum.ADJUSTMENT:
            # Default adjustment is positive intake unless indicated
            stock.quantity += payload.quantity

        movement = StockMovementModel(
            product_id=payload.product_id,
            location=payload.location,
            movement_type=payload.movement_type.value,
            quantity=payload.quantity,
            reference=payload.reference,
            is_reversed=False,
        )
        db.add(movement)

        try:
            db.commit()
            db.refresh(movement)
            return movement
        except Exception:
            db.rollback()
            raise AppException(500, "Database error creating movement", "Internal Server Error")

    @classmethod
    def patch_movement(cls, db: Session, current_user: UserModel, stockmovement_id: int, payload: StockMovementPatchRequest) -> StockMovementModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins update movements", "Forbidden")

        movement = cls.get_by_id(db, stockmovement_id)
        if payload.reference is not None:
            movement.reference = payload.reference

        try:
            db.commit()
            db.refresh(movement)
            return movement
        except Exception:
            db.rollback()
            raise AppException(500, "Database error updating movement", "Internal Server Error")

    @classmethod
    def delete_movement(cls, db: Session, current_user: UserModel, stockmovement_id: int) -> None:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins delete movements", "Forbidden")

        movement = cls.get_by_id(db, stockmovement_id)
        if movement.is_reversed:
            raise AppException(409, "Cannot delete an already reversed movement", "Conflict")

        # To preserve inventory balance, movements cannot simply be hard-deleted without audit
        db.delete(movement)
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(500, "Database error deleting movement", "Internal Server Error")

    @classmethod
    def reverse_movement(cls, db: Session, current_user: UserModel, stockmovement_id: int) -> StockMovementModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins reverse movements", "Forbidden")

        orig = cls.get_by_id(db, stockmovement_id)

        # REQ-8-006: Reversal is immutable history and exactly offsets one unreversed movement
        if orig.is_reversed:
            # Idempotence check: check if already reversed by a reversal record
            reversal_rec = db.scalar(select(StockMovementModel).where(StockMovementModel.reversed_movement_id == orig.id))
            if reversal_rec:
                return reversal_rec
            raise AppException(409, "Movement is already reversed", "Conflict")

        if orig.movement_type == MovementTypeEnum.REVERSAL.value:
            raise AppException(409, "Cannot reverse a reversal movement", "Conflict")

        stock = db.scalar(
            select(StockLevelModel).where(
                StockLevelModel.product_id == orig.product_id,
                StockLevelModel.location == orig.location,
            )
        )
        if not stock:
            raise AppException(404, "Associated stock level not found", "Not Found")

        # Offsetting logic
        if orig.movement_type == MovementTypeEnum.INTAKE.value:
            if stock.quantity < orig.quantity:
                raise AppException(409, "Reversing intake would cause negative stock", "Conflict")
            stock.quantity -= orig.quantity
        elif orig.movement_type == MovementTypeEnum.ISSUE.value:
            stock.quantity += orig.quantity
        elif orig.movement_type == MovementTypeEnum.ADJUSTMENT.value:
            if stock.quantity < orig.quantity:
                raise AppException(409, "Reversing adjustment would cause negative stock", "Conflict")
            stock.quantity -= orig.quantity

        orig.is_reversed = True

        reversal = StockMovementModel(
            product_id=orig.product_id,
            location=orig.location,
            movement_type=MovementTypeEnum.REVERSAL.value,
            quantity=orig.quantity,
            reference=f"Reversal of movement #{orig.id}",
            is_reversed=False,
            reversed_movement_id=orig.id,
        )
        db.add(reversal)

        try:
            db.commit()
            db.refresh(reversal)
            return reversal
        except Exception:
            db.rollback()
            raise AppException(500, "Database error reversing movement", "Internal Server Error")

    @classmethod
    def get_ledger(cls, db: Session, product_id: int, limit: int = 20, after_id: Optional[int] = None) -> StockMovementListResponse:
        ProductController.get_by_id(db, product_id)
        query = select(StockMovementModel).where(StockMovementModel.product_id == product_id)
        if after_id is not None:
            query = query.where(StockMovementModel.id > after_id)

        query = query.order_by(StockMovementModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return StockMovementListResponse(
            items=[cls.serialize(r) for r in items],
            limit=limit,
            next_after_id=next_id,
        )


# -----------------------------------------------------------------------------
# Seed Data Initialization
# -----------------------------------------------------------------------------
def seed_database(db: Session):
    Base.metadata.create_all(bind=engine)
    if db.scalar(select(UserModel).limit(1)):
        return

    admin_token = os.getenv("API_ADMIN_TOKEN", "admin-token-secret-12345")
    user1_token = os.getenv("API_USER_TOKEN", "user1-token-secret-12345")
    user2_token = os.getenv("API_SECOND_USER_TOKEN", "user2-token-secret-12345")

    u1 = UserModel(id=1, username="user1", role=UserRole.USER.value, token_hash=hash_token(user1_token), token=user1_token)
    u2 = UserModel(id=2, username="user2", role=UserRole.USER.value, token_hash=hash_token(user2_token), token=user2_token)
    u3 = UserModel(id=3, username="admin", role=UserRole.ADMIN.value, token_hash=hash_token(admin_token), token=admin_token)
    db.add_all([u1, u2, u3])
    db.flush()

    p1 = ProductModel(id=1, name="Standard Widget A", active=True, is_deleted=False)
    p2 = ProductModel(id=2, name="Heavy Duty Widget B", active=True, is_deleted=False)
    p3 = ProductModel(id=3, name="Legacy Component C", active=False, is_deleted=False)
    db.add_all([p1, p2, p3])
    db.flush()

    s1 = StockLevelModel(id=1, product_id=1, location="MAIN-A1", quantity=100.0)
    s2 = StockLevelModel(id=2, product_id=1, location="OVERFLOW-B2", quantity=50.0)
    s3 = StockLevelModel(id=3, product_id=2, location="MAIN-A1", quantity=30.0)
    db.add_all([s1, s2, s3])
    db.flush()

    m1 = StockMovementModel(
        id=1,
        product_id=1,
        location="MAIN-A1",
        movement_type=MovementTypeEnum.INTAKE.value,
        quantity=100.0,
        reference="Initial Stock Intake",
        is_reversed=False,
    )
    db.add(m1)
    db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    with SessionLocal() as db:
        seed_database(db)
    yield


with SessionLocal() as _startup_db:
    seed_database(_startup_db)


# -----------------------------------------------------------------------------
# Pure ASGI Middleware for JSON & Content-Type Validation
# -----------------------------------------------------------------------------
class JsonValidationASGIMiddleware:
    def __init__(self, app_instance: ASGIApp, routes: list):
        self.app = app_instance
        self.routes = routes

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        if method not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return

        has_full_match = False
        for route in self.routes:
            match_status, _ = route.matches(scope)
            if match_status == Match.FULL:
                has_full_match = True
                break

        if not has_full_match:
            await self.app(scope, receive, send)
            return

        body_chunks = []
        total_len = 0

        while True:
            message = await receive()
            chunk = message.get("body", b"")
            total_len += len(chunk)
            if total_len > MAX_PAYLOAD_SIZE:
                err_body = json.dumps({
                    "detail": "Payload too large; maximum permitted is 10 MiB",
                    "status_code": 413,
                    "error_type": "Payload Too Large",
                }).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(err_body)).encode("ascii")),
                    ],
                })
                await send({"type": "http.response.body", "body": err_body, "more_body": False})
                return
            body_chunks.append(chunk)
            if not message.get("more_body", False):
                break

        full_body = b"".join(body_chunks)
        if full_body:
            headers = dict(scope.get("headers", []))
            content_type = headers.get(b"content-type", b"").decode("latin-1").lower()
            if not (content_type == "application/json" or content_type.startswith("application/json;")):
                err_body = json.dumps({
                    "detail": "Media type must be application/json",
                    "status_code": 415,
                    "error_type": "Unsupported Media Type",
                }).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 415,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(err_body)).encode("ascii")),
                    ],
                })
                await send({"type": "http.response.body", "body": err_body, "more_body": False})
                return
            try:
                json.loads(full_body.decode("utf-8"))
            except Exception:
                err_body = json.dumps({
                    "detail": "Malformed JSON or invalid UTF-8 payload",
                    "status_code": 400,
                    "error_type": "Bad Request",
                }).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(err_body)).encode("ascii")),
                    ],
                })
                await send({"type": "http.response.body", "body": err_body, "more_body": False})
                return

        body_sent = False

        async def replay_receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": full_body, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)


# -----------------------------------------------------------------------------
# FastAPI Application & Routes (MVC: View/Routing)
# -----------------------------------------------------------------------------
app = FastAPI(
    title="Inventory Management - Stock Intake, Issue & Non-Negative Stock API",
    version="1.0.0",
    description="Deterministic RESTful API for managing products, stock levels, stock movements, and non-negative inventory balances.",
    lifespan=lifespan,
)

app.add_middleware(JsonValidationASGIMiddleware, routes=app.routes)


# -----------------------------------------------------------------------------
# Exception Handlers
# -----------------------------------------------------------------------------
@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "status_code": exc.status_code, "error_type": exc.error_type},
        headers=exc.headers,
    )


@app.exception_handler(StarletteHTTPException)
async def starlette_exception_handler(request: Request, exc: StarletteHTTPException):
    status_phrase = (
        http.HTTPStatus(exc.status_code).phrase
        if exc.status_code in http.HTTPStatus._value2member_map_
        else "HTTP Exception"
    )
    headers = getattr(exc, "headers", None) or {}
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc.detail), "status_code": exc.status_code, "error_type": status_phrase},
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": str(exc.errors()), "status_code": 422, "error_type": "Unprocessable Entity"},
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "status_code": 500, "error_type": "Internal Server Error"},
    )


# -----------------------------------------------------------------------------
# Endpoints (20 Specified Endpoints for Variant A)
# -----------------------------------------------------------------------------

# 1. GET /health
@app.get(
    "/health",
    tags=["Health"],
    response_model=HealthResponse,
    responses={503: {"model": ErrorResponse, "description": "Service Unavailable"}},
)
def get_health(db: Session = Depends(get_db)):
    try:
        db.execute(select(1))
        return HealthResponse(status="ok", database="connected")
    except Exception as ex:
        logger.error("Health probe failed: %s", ex)
        raise AppException(503, "Database connectivity probe failed", "Service Unavailable")


# 2. GET /products
@app.get(
    "/products",
    tags=["Products"],
    response_model=ProductListResponse,
)
def list_products(
    name: Optional[str] = Query(None),
    active: Optional[bool] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return ProductController.list_products(db, name, active, limit, after_id)


# 3. POST /products
@app.post(
    "/products",
    tags=["Products"],
    status_code=status.HTTP_201_CREATED,
    response_model=ProductResponse,
)
def create_product(
    payload: ProductCreateRequest,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    prod = ProductController.create_product(db, current_user, payload)
    response.headers["Location"] = f"/products/{prod.id}"
    return ProductController.serialize(prod)


# 4. GET /products/{product_id}
@app.get(
    "/products/{product_id}",
    tags=["Products"],
    response_model=ProductResponse,
)
def get_product(
    product_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    prod = ProductController.get_by_id(db, product_id)
    return ProductController.serialize(prod)


# 5. PATCH /products/{product_id}
@app.patch(
    "/products/{product_id}",
    tags=["Products"],
    response_model=ProductResponse,
)
def patch_product(
    product_id: int,
    payload: ProductPatchRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    prod = ProductController.patch_product(db, current_user, product_id, payload)
    return ProductController.serialize(prod)


# 6. DELETE /products/{product_id}
@app.delete(
    "/products/{product_id}",
    tags=["Products"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_product(
    product_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    ProductController.delete_product(db, current_user, product_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 7. GET /stock-levels
@app.get(
    "/stock-levels",
    tags=["Stock Levels"],
    response_model=StockLevelListResponse,
)
def list_stock_levels(
    product_id: Optional[int] = Query(None, gt=0),
    location: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return StockLevelController.list_stock_levels(db, product_id, location, limit, after_id)


# 8. POST /stock-levels
@app.post(
    "/stock-levels",
    tags=["Stock Levels"],
    status_code=status.HTTP_201_CREATED,
    response_model=StockLevelResponse,
)
def create_stock_level(
    payload: StockLevelCreateRequest,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    sl = StockLevelController.create_stock_level(db, current_user, payload)
    response.headers["Location"] = f"/stock-levels/{sl.id}"
    return StockLevelController.serialize(sl)


# 9. GET /stock-levels/{stocklevel_id}
@app.get(
    "/stock-levels/{stocklevel_id}",
    tags=["Stock Levels"],
    response_model=StockLevelResponse,
)
def get_stock_level(
    stocklevel_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    sl = StockLevelController.get_by_id(db, stocklevel_id)
    return StockLevelController.serialize(sl)


# 10. PATCH /stock-levels/{stocklevel_id}
@app.patch(
    "/stock-levels/{stocklevel_id}",
    tags=["Stock Levels"],
    response_model=StockLevelResponse,
)
def patch_stock_level(
    stocklevel_id: int,
    payload: StockLevelPatchRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    sl = StockLevelController.patch_stock_level(db, current_user, stocklevel_id, payload)
    return StockLevelController.serialize(sl)


# 11. DELETE /stock-levels/{stocklevel_id}
@app.delete(
    "/stock-levels/{stocklevel_id}",
    tags=["Stock Levels"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_stock_level(
    stocklevel_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    StockLevelController.delete_stock_level(db, current_user, stocklevel_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 12. GET /stock-movements
@app.get(
    "/stock-movements",
    tags=["Stock Movements"],
    response_model=StockMovementListResponse,
)
def list_stock_movements(
    product_id: Optional[int] = Query(None, gt=0),
    location: Optional[str] = Query(None),
    movement_type: Optional[MovementTypeEnum] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return StockMovementController.list_movements(db, product_id, location, movement_type, limit, after_id)


# 13. POST /stock-movements
@app.post(
    "/stock-movements",
    tags=["Stock Movements"],
    status_code=status.HTTP_201_CREATED,
    response_model=StockMovementResponse,
)
def create_stock_movement(
    payload: StockMovementCreateRequest,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    mov = StockMovementController.create_movement(db, current_user, payload)
    response.headers["Location"] = f"/stock-movements/{mov.id}"
    return StockMovementController.serialize(mov)


# 14. GET /stock-movements/{stockmovement_id}
@app.get(
    "/stock-movements/{stockmovement_id}",
    tags=["Stock Movements"],
    response_model=StockMovementResponse,
)
def get_stock_movement(
    stockmovement_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    mov = StockMovementController.get_by_id(db, stockmovement_id)
    return StockMovementController.serialize(mov)


# 15. PATCH /stock-movements/{stockmovement_id}
@app.patch(
    "/stock-movements/{stockmovement_id}",
    tags=["Stock Movements"],
    response_model=StockMovementResponse,
)
def patch_stock_movement(
    stockmovement_id: int,
    payload: StockMovementPatchRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    mov = StockMovementController.patch_movement(db, current_user, stockmovement_id, payload)
    return StockMovementController.serialize(mov)


# 16. DELETE /stock-movements/{stockmovement_id}
@app.delete(
    "/stock-movements/{stockmovement_id}",
    tags=["Stock Movements"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_stock_movement(
    stockmovement_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    StockMovementController.delete_movement(db, current_user, stockmovement_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 17. POST /stock-movements/{stockmovement_id}/reverse
@app.post(
    "/stock-movements/{stockmovement_id}/reverse",
    tags=["Stock Movements"],
    response_model=StockMovementResponse,
)
def reverse_stock_movement(
    stockmovement_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    reversal = StockMovementController.reverse_movement(db, current_user, stockmovement_id)
    return StockMovementController.serialize(reversal)


# 18. GET /products/{product_id}/stock-ledger
@app.get(
    "/products/{product_id}/stock-ledger",
    tags=["Products"],
    response_model=StockMovementListResponse,
)
def get_product_stock_ledger(
    product_id: int,
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return StockMovementController.get_ledger(db, product_id, limit, after_id)


# 19. POST /stock-levels/{stocklevel_id}/reconcile
@app.post(
    "/stock-levels/{stocklevel_id}/reconcile",
    tags=["Stock Levels"],
    response_model=StockLevelResponse,
)
def reconcile_stock_level(
    stocklevel_id: int,
    payload: ReconcileStockRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    sl = StockLevelController.reconcile_stock(db, current_user, stocklevel_id, payload)
    return StockLevelController.serialize(sl)


# 20. GET /stock/availability
@app.get(
    "/stock/availability",
    tags=["Stock Levels"],
    response_model=AvailabilityResponse,
)
def get_stock_availability(
    product_id: int = Query(..., gt=0),
    location: str = Query(..., min_length=2, max_length=80),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    ProductController.get_by_id(db, product_id)
    stock = db.scalar(
        select(StockLevelModel).where(
            StockLevelModel.product_id == product_id,
            StockLevelModel.location == location.strip(),
        )
    )
    available_qty = stock.quantity if stock else 0.0
    return AvailabilityResponse(
        product_id=product_id,
        location=location.strip(),
        available_quantity=available_qty,
    )


# -----------------------------------------------------------------------------
# Custom OpenAPI Export
# -----------------------------------------------------------------------------
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi
