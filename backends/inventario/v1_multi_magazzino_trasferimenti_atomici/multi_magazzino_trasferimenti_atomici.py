"""
Inventory Management - Variant B: Multi-warehouse and atomic transfers
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
    or_,
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
logger = logging.getLogger("inventario_variant_b")
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


class TransferStatusEnum(str, enum.Enum):
    DRAFT = "DRAFT"
    DISPATCHED = "DISPATCHED"
    RECEIVED = "RECEIVED"
    CANCELLED = "CANCELLED"


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


class WarehouseModel(Base):
    __tablename__ = "warehouses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(20), unique=True, nullable=False, index=True)
    name = Column(String(120), nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    is_deleted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    stock_levels = relationship("StockLevelModel", back_populates="warehouse")


class ProductModel(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sku = Column(String(32), unique=True, nullable=False, index=True)
    name = Column(String(160), nullable=False)
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
    transfers = relationship("TransferModel", back_populates="product")


class StockLevelModel(Base):
    __tablename__ = "stock_levels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    quantity = Column(Float, nullable=False, default=0.0)
    in_transit_quantity = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    warehouse = relationship("WarehouseModel", back_populates="stock_levels", foreign_keys=[warehouse_id])
    product = relationship("ProductModel", back_populates="stock_levels", foreign_keys=[product_id])


class TransferModel(Base):
    __tablename__ = "transfers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=False, index=True)
    dest_warehouse_id = Column(Integer, ForeignKey("warehouses.id"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    quantity = Column(Float, nullable=False)
    status = Column(String(20), nullable=False, default=TransferStatusEnum.DRAFT.value, index=True)
    dispatched_at = Column(DateTime(timezone=True), nullable=True)
    received_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    source_warehouse = relationship("WarehouseModel", foreign_keys=[source_warehouse_id])
    dest_warehouse = relationship("WarehouseModel", foreign_keys=[dest_warehouse_id])
    product = relationship("ProductModel", back_populates="transfers", foreign_keys=[product_id])


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


# Warehouse Schemas
class WarehouseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(..., min_length=2, max_length=20)
    name: str = Field(..., min_length=2, max_length=120)
    active: Optional[bool] = True

    @field_validator("code", "name")
    @classmethod
    def strip_strings(cls, v: str) -> str:
        s = v.strip()
        if len(s) < 2:
            raise ValueError("Field length must be at least 2 characters after trimming")
        return s


class WarehousePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Optional[str] = Field(None, min_length=2, max_length=20)
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    active: Optional[bool] = None

    @field_validator("code", "name")
    @classmethod
    def strip_strings(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if len(s) < 2:
            raise ValueError("Field length must be at least 2 characters after trimming")
        return s

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("code", "name", "active"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class WarehouseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    code: str
    name: str
    active: bool
    created_at: str
    updated_at: str


class WarehouseListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[WarehouseResponse]
    limit: int
    next_after_id: Optional[int]


# Product Schemas
class ProductCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str = Field(..., min_length=2, max_length=32)
    name: str = Field(..., min_length=2, max_length=160)
    active: Optional[bool] = True

    @field_validator("sku", "name")
    @classmethod
    def strip_strings(cls, v: str) -> str:
        s = v.strip()
        if len(s) < 2:
            raise ValueError("Field length must be at least 2 characters after trimming")
        return s


class ProductPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: Optional[str] = Field(None, min_length=2, max_length=32)
    name: Optional[str] = Field(None, min_length=2, max_length=160)
    active: Optional[bool] = None

    @field_validator("sku", "name")
    @classmethod
    def strip_strings(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = v.strip()
        if len(s) < 2:
            raise ValueError("Field length must be at least 2 characters after trimming")
        return s

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("sku", "name", "active"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        return self


class ProductResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    sku: str
    name: str
    active: bool
    created_at: str
    updated_at: str


class ProductListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[ProductResponse]
    limit: int
    next_after_id: Optional[int]


# Transfer Schemas
class TransferCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_warehouse_id: int = Field(..., gt=0)
    dest_warehouse_id: int = Field(..., gt=0)
    product_id: int = Field(..., gt=0)
    quantity: float = Field(..., ge=0.001, le=1000000.0)

    @model_validator(mode="after")
    def validate_warehouses(self):
        if self.source_warehouse_id == self.dest_warehouse_id:
            raise ValueError("source_warehouse_id and dest_warehouse_id must be distinct")
        return self


class TransferPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_warehouse_id: Optional[int] = Field(None, gt=0)
    dest_warehouse_id: Optional[int] = Field(None, gt=0)
    product_id: Optional[int] = Field(None, gt=0)
    quantity: Optional[float] = Field(None, ge=0.001, le=1000000.0)

    @model_validator(mode="after")
    def validate_non_empty(self):
        if not self.model_fields_set:
            raise ValueError("At least one field must be provided in PATCH")
        for f in ("source_warehouse_id", "dest_warehouse_id", "product_id", "quantity"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} cannot be null")
        if self.source_warehouse_id is not None and self.dest_warehouse_id is not None:
            if self.source_warehouse_id == self.dest_warehouse_id:
                raise ValueError("source_warehouse_id and dest_warehouse_id must be distinct")
        return self


class TransferResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    source_warehouse_id: int
    dest_warehouse_id: int
    product_id: int
    quantity: float
    status: TransferStatusEnum
    dispatched_at: Optional[str]
    received_at: Optional[str]
    cancelled_at: Optional[str]
    created_at: str
    updated_at: str


class TransferListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[TransferResponse]
    limit: int
    next_after_id: Optional[int]


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
class WarehouseController:
    @classmethod
    def serialize(cls, w: WarehouseModel) -> WarehouseResponse:
        return WarehouseResponse(
            id=w.id,
            code=w.code,
            name=w.name,
            active=w.active,
            created_at=w.created_at.isoformat(),
            updated_at=w.updated_at.isoformat(),
        )

    @classmethod
    def get_by_id(cls, db: Session, warehouse_id: int) -> WarehouseModel:
        if warehouse_id <= 0:
            raise AppException(404, "Warehouse not found", "Not Found")
        wh = db.scalar(select(WarehouseModel).where(WarehouseModel.id == warehouse_id, WarehouseModel.is_deleted == False))
        if not wh:
            raise AppException(404, "Warehouse not found", "Not Found")
        return wh

    @classmethod
    def list_warehouses(
        cls,
        db: Session,
        code: Optional[str] = None,
        active: Optional[bool] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> WarehouseListResponse:
        query = select(WarehouseModel).where(WarehouseModel.is_deleted == False)
        if code is not None:
            query = query.where(WarehouseModel.code.ilike(f"%{code.strip()}%"))
        if active is not None:
            query = query.where(WarehouseModel.active == active)
        if after_id is not None:
            query = query.where(WarehouseModel.id > after_id)

        query = query.order_by(WarehouseModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return WarehouseListResponse(
            items=[cls.serialize(r) for r in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def create_warehouse(cls, db: Session, current_user: UserModel, payload: WarehouseCreateRequest) -> WarehouseModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins manage warehouses", "Forbidden")

        if db.scalar(select(WarehouseModel).where(WarehouseModel.code == payload.code, WarehouseModel.is_deleted == False)):
            raise AppException(409, "Warehouse with this code already exists", "Conflict")

        wh = WarehouseModel(
            code=payload.code,
            name=payload.name,
            active=payload.active if payload.active is not None else True,
            is_deleted=False,
        )
        db.add(wh)
        try:
            db.commit()
            db.refresh(wh)
            return wh
        except Exception:
            db.rollback()
            raise AppException(500, "Database error creating warehouse", "Internal Server Error")

    @classmethod
    def patch_warehouse(cls, db: Session, current_user: UserModel, warehouse_id: int, payload: WarehousePatchRequest) -> WarehouseModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins manage warehouses", "Forbidden")

        wh = cls.get_by_id(db, warehouse_id)

        if payload.code is not None and payload.code != wh.code:
            if db.scalar(select(WarehouseModel).where(WarehouseModel.code == payload.code, WarehouseModel.id != wh.id, WarehouseModel.is_deleted == False)):
                raise AppException(409, "Warehouse with this code already exists", "Conflict")
            wh.code = payload.code

        if payload.name is not None:
            wh.name = payload.name

        if payload.active is not None:
            wh.active = payload.active

        try:
            db.commit()
            db.refresh(wh)
            return wh
        except Exception:
            db.rollback()
            raise AppException(500, "Database error updating warehouse", "Internal Server Error")

    @classmethod
    def delete_warehouse(cls, db: Session, current_user: UserModel, warehouse_id: int) -> None:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins manage warehouses", "Forbidden")

        wh = cls.get_by_id(db, warehouse_id)

        # Check stock > 0
        has_stock = db.scalar(
            select(StockLevelModel).where(
                StockLevelModel.warehouse_id == wh.id,
                or_(StockLevelModel.quantity > 0, StockLevelModel.in_transit_quantity > 0),
            ).limit(1)
        )
        if has_stock:
            raise AppException(409, "Cannot delete warehouse with existing stock or in-transit stock", "Conflict")

        # Check active transfers
        has_transfers = db.scalar(
            select(TransferModel).where(
                or_(TransferModel.source_warehouse_id == wh.id, TransferModel.dest_warehouse_id == wh.id),
                TransferModel.status.in_([TransferStatusEnum.DRAFT.value, TransferStatusEnum.DISPATCHED.value]),
            ).limit(1)
        )
        if has_transfers:
            raise AppException(409, "Cannot delete warehouse with active pending transfers", "Conflict")

        wh.is_deleted = True
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(500, "Database error deleting warehouse", "Internal Server Error")


class ProductController:
    @classmethod
    def serialize(cls, p: ProductModel) -> ProductResponse:
        return ProductResponse(
            id=p.id,
            sku=p.sku,
            name=p.name,
            active=p.active,
            created_at=p.created_at.isoformat(),
            updated_at=p.updated_at.isoformat(),
        )

    @classmethod
    def get_by_id(cls, db: Session, product_id: int) -> ProductModel:
        if product_id <= 0:
            raise AppException(404, "Product not found", "Not Found")
        prod = db.scalar(select(ProductModel).where(ProductModel.id == product_id, ProductModel.is_deleted == False))
        if not prod:
            raise AppException(404, "Product not found", "Not Found")
        return prod

    @classmethod
    def list_products(
        cls,
        db: Session,
        sku: Optional[str] = None,
        name: Optional[str] = None,
        active: Optional[bool] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> ProductListResponse:
        query = select(ProductModel).where(ProductModel.is_deleted == False)
        if sku is not None:
            query = query.where(ProductModel.sku.ilike(f"%{sku.strip()}%"))
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
            raise AppException(403, "Admins manage products", "Forbidden")

        if db.scalar(select(ProductModel).where(ProductModel.sku == payload.sku, ProductModel.is_deleted == False)):
            raise AppException(409, "Product with this SKU already exists", "Conflict")

        prod = ProductModel(
            sku=payload.sku,
            name=payload.name,
            active=payload.active if payload.active is not None else True,
            is_deleted=False,
        )
        db.add(prod)
        try:
            db.commit()
            db.refresh(prod)
            return prod
        except Exception:
            db.rollback()
            raise AppException(500, "Database error creating product", "Internal Server Error")

    @classmethod
    def patch_product(cls, db: Session, current_user: UserModel, product_id: int, payload: ProductPatchRequest) -> ProductModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins manage products", "Forbidden")

        prod = cls.get_by_id(db, product_id)

        if payload.sku is not None and payload.sku != prod.sku:
            if db.scalar(select(ProductModel).where(ProductModel.sku == payload.sku, ProductModel.id != prod.id, ProductModel.is_deleted == False)):
                raise AppException(409, "Product with this SKU already exists", "Conflict")
            prod.sku = payload.sku

        if payload.name is not None:
            prod.name = payload.name

        if payload.active is not None:
            prod.active = payload.active

        try:
            db.commit()
            db.refresh(prod)
            return prod
        except Exception:
            db.rollback()
            raise AppException(500, "Database error updating product", "Internal Server Error")

    @classmethod
    def delete_product(cls, db: Session, current_user: UserModel, product_id: int) -> None:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins manage products", "Forbidden")

        prod = cls.get_by_id(db, product_id)

        has_stock = db.scalar(
            select(StockLevelModel).where(
                StockLevelModel.product_id == prod.id,
                or_(StockLevelModel.quantity > 0, StockLevelModel.in_transit_quantity > 0),
            ).limit(1)
        )
        if has_stock:
            raise AppException(409, "Cannot delete product with existing inventory stock", "Conflict")

        has_transfers = db.scalar(
            select(TransferModel).where(
                TransferModel.product_id == prod.id,
                TransferModel.status.in_([TransferStatusEnum.DRAFT.value, TransferStatusEnum.DISPATCHED.value]),
            ).limit(1)
        )
        if has_transfers:
            raise AppException(409, "Cannot delete product with active pending transfers", "Conflict")

        prod.is_deleted = True
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(500, "Database error deleting product", "Internal Server Error")


class TransferController:
    @classmethod
    def serialize(cls, t: TransferModel) -> TransferResponse:
        return TransferResponse(
            id=t.id,
            source_warehouse_id=t.source_warehouse_id,
            dest_warehouse_id=t.dest_warehouse_id,
            product_id=t.product_id,
            quantity=t.quantity,
            status=TransferStatusEnum(t.status),
            dispatched_at=t.dispatched_at.isoformat() if t.dispatched_at else None,
            received_at=t.received_at.isoformat() if t.received_at else None,
            cancelled_at=t.cancelled_at.isoformat() if t.cancelled_at else None,
            created_at=t.created_at.isoformat(),
            updated_at=t.updated_at.isoformat(),
        )

    @classmethod
    def get_by_id(cls, db: Session, transfer_id: int) -> TransferModel:
        if transfer_id <= 0:
            raise AppException(404, "Transfer not found", "Not Found")
        transfer = db.scalar(select(TransferModel).where(TransferModel.id == transfer_id))
        if not transfer:
            raise AppException(404, "Transfer not found", "Not Found")
        return transfer

    @classmethod
    def list_transfers(
        cls,
        db: Session,
        source_warehouse_id: Optional[int] = None,
        dest_warehouse_id: Optional[int] = None,
        product_id: Optional[int] = None,
        status: Optional[TransferStatusEnum] = None,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> TransferListResponse:
        query = select(TransferModel)
        if source_warehouse_id is not None:
            query = query.where(TransferModel.source_warehouse_id == source_warehouse_id)
        if dest_warehouse_id is not None:
            query = query.where(TransferModel.dest_warehouse_id == dest_warehouse_id)
        if product_id is not None:
            query = query.where(TransferModel.product_id == product_id)
        if status is not None:
            query = query.where(TransferModel.status == status.value)
        if after_id is not None:
            query = query.where(TransferModel.id > after_id)

        query = query.order_by(TransferModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return TransferListResponse(
            items=[cls.serialize(r) for r in items],
            limit=limit,
            next_after_id=next_id,
        )

    @classmethod
    def create_transfer(cls, db: Session, current_user: UserModel, payload: TransferCreateRequest) -> TransferModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins manage transfers", "Forbidden")

        src = WarehouseController.get_by_id(db, payload.source_warehouse_id)
        dst = WarehouseController.get_by_id(db, payload.dest_warehouse_id)
        prod = ProductController.get_by_id(db, payload.product_id)

        if not src.active or not dst.active:
            raise AppException(409, "Both warehouses must be active", "Conflict")
        if not prod.active:
            raise AppException(409, "Product must be active", "Conflict")

        transfer = TransferModel(
            source_warehouse_id=src.id,
            dest_warehouse_id=dst.id,
            product_id=prod.id,
            quantity=payload.quantity,
            status=TransferStatusEnum.DRAFT.value,
        )
        db.add(transfer)
        try:
            db.commit()
            db.refresh(transfer)
            return transfer
        except Exception:
            db.rollback()
            raise AppException(500, "Database error creating transfer", "Internal Server Error")

    @classmethod
    def patch_transfer(cls, db: Session, current_user: UserModel, transfer_id: int, payload: TransferPatchRequest) -> TransferModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins manage transfers", "Forbidden")

        transfer = cls.get_by_id(db, transfer_id)
        if transfer.status != TransferStatusEnum.DRAFT.value:
            raise AppException(409, "Only DRAFT transfers can be modified", "Conflict")

        src_id = payload.source_warehouse_id or transfer.source_warehouse_id
        dst_id = payload.dest_warehouse_id or transfer.dest_warehouse_id
        if src_id == dst_id:
            raise AppException(422, "Source and destination warehouses must be distinct", "Unprocessable Entity")

        if payload.source_warehouse_id is not None:
            src = WarehouseController.get_by_id(db, payload.source_warehouse_id)
            if not src.active:
                raise AppException(409, "Source warehouse must be active", "Conflict")
            transfer.source_warehouse_id = src.id

        if payload.dest_warehouse_id is not None:
            dst = WarehouseController.get_by_id(db, payload.dest_warehouse_id)
            if not dst.active:
                raise AppException(409, "Destination warehouse must be active", "Conflict")
            transfer.dest_warehouse_id = dst.id

        if payload.product_id is not None:
            prod = ProductController.get_by_id(db, payload.product_id)
            if not prod.active:
                raise AppException(409, "Product must be active", "Conflict")
            transfer.product_id = prod.id

        if payload.quantity is not None:
            transfer.quantity = payload.quantity

        try:
            db.commit()
            db.refresh(transfer)
            return transfer
        except Exception:
            db.rollback()
            raise AppException(500, "Database error updating transfer", "Internal Server Error")

    @classmethod
    def delete_transfer(cls, db: Session, current_user: UserModel, transfer_id: int) -> None:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins manage transfers", "Forbidden")

        transfer = cls.get_by_id(db, transfer_id)
        if transfer.status != TransferStatusEnum.DRAFT.value:
            raise AppException(409, "Only DRAFT transfers can be deleted", "Conflict")

        db.delete(transfer)
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise AppException(500, "Database error deleting transfer", "Internal Server Error")

    @classmethod
    def dispatch_transfer(cls, db: Session, current_user: UserModel, transfer_id: int) -> TransferModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins dispatch transfers", "Forbidden")

        transfer = cls.get_by_id(db, transfer_id)

        # Idempotence: if already DISPATCHED, return 200
        if transfer.status == TransferStatusEnum.DISPATCHED.value:
            return transfer

        if transfer.status != TransferStatusEnum.DRAFT.value:
            raise AppException(409, f"Cannot dispatch transfer in status {transfer.status}", "Conflict")

        # REQ-8-005: Dispatch subtracts source and adds in-transit atomically; source never negative
        src_stock = db.scalar(
            select(StockLevelModel).where(
                StockLevelModel.warehouse_id == transfer.source_warehouse_id,
                StockLevelModel.product_id == transfer.product_id,
            )
        )
        if not src_stock or src_stock.quantity < transfer.quantity:
            raise AppException(409, "Insufficient stock at source warehouse for dispatch", "Conflict")

        src_stock.quantity -= transfer.quantity
        src_stock.in_transit_quantity += transfer.quantity

        transfer.status = TransferStatusEnum.DISPATCHED.value
        transfer.dispatched_at = datetime.now(timezone.utc)

        try:
            db.commit()
            db.refresh(transfer)
            return transfer
        except Exception:
            db.rollback()
            raise AppException(500, "Database error dispatching transfer", "Internal Server Error")

    @classmethod
    def receive_transfer(cls, db: Session, current_user: UserModel, transfer_id: int) -> TransferModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins receive transfers", "Forbidden")

        transfer = cls.get_by_id(db, transfer_id)

        # Idempotence: if already RECEIVED, return 200
        if transfer.status == TransferStatusEnum.RECEIVED.value:
            return transfer

        # REQ-8-006: Receive transfers in-transit to destination atomically
        if transfer.status != TransferStatusEnum.DISPATCHED.value:
            raise AppException(409, f"Cannot receive transfer in status {transfer.status}", "Conflict")

        src_stock = db.scalar(
            select(StockLevelModel).where(
                StockLevelModel.warehouse_id == transfer.source_warehouse_id,
                StockLevelModel.product_id == transfer.product_id,
            )
        )
        if not src_stock or src_stock.in_transit_quantity < transfer.quantity:
            raise AppException(409, "Inconsistent in-transit inventory quantity", "Conflict")

        src_stock.in_transit_quantity -= transfer.quantity

        dst_stock = db.scalar(
            select(StockLevelModel).where(
                StockLevelModel.warehouse_id == transfer.dest_warehouse_id,
                StockLevelModel.product_id == transfer.product_id,
            )
        )
        if not dst_stock:
            dst_stock = StockLevelModel(
                warehouse_id=transfer.dest_warehouse_id,
                product_id=transfer.product_id,
                quantity=0.0,
                in_transit_quantity=0.0,
            )
            db.add(dst_stock)
            db.flush()

        dst_stock.quantity += transfer.quantity

        transfer.status = TransferStatusEnum.RECEIVED.value
        transfer.received_at = datetime.now(timezone.utc)

        try:
            db.commit()
            db.refresh(transfer)
            return transfer
        except Exception:
            db.rollback()
            raise AppException(500, "Database error receiving transfer", "Internal Server Error")

    @classmethod
    def cancel_transfer(cls, db: Session, current_user: UserModel, transfer_id: int) -> TransferModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Admins cancel transfers", "Forbidden")

        transfer = cls.get_by_id(db, transfer_id)

        # Idempotence: if already CANCELLED, return 200
        if transfer.status == TransferStatusEnum.CANCELLED.value:
            return transfer

        if transfer.status not in (TransferStatusEnum.DRAFT.value, TransferStatusEnum.DISPATCHED.value):
            raise AppException(409, f"Cannot cancel transfer in status {transfer.status}", "Conflict")

        # If dispatched, restore stock at source from in-transit
        if transfer.status == TransferStatusEnum.DISPATCHED.value:
            src_stock = db.scalar(
                select(StockLevelModel).where(
                    StockLevelModel.warehouse_id == transfer.source_warehouse_id,
                    StockLevelModel.product_id == transfer.product_id,
                )
            )
            if src_stock:
                src_stock.in_transit_quantity = max(0.0, src_stock.in_transit_quantity - transfer.quantity)
                src_stock.quantity += transfer.quantity

        transfer.status = TransferStatusEnum.CANCELLED.value
        transfer.cancelled_at = datetime.now(timezone.utc)

        try:
            db.commit()
            db.refresh(transfer)
            return transfer
        except Exception:
            db.rollback()
            raise AppException(500, "Database error cancelling transfer", "Internal Server Error")

    @classmethod
    def list_conflicts(
        cls,
        db: Session,
        warehouse_id: int,
        limit: int = 20,
        after_id: Optional[int] = None,
    ) -> TransferListResponse:
        WarehouseController.get_by_id(db, warehouse_id)

        query = select(TransferModel).where(
            TransferModel.source_warehouse_id == warehouse_id,
            TransferModel.status.in_([TransferStatusEnum.DRAFT.value, TransferStatusEnum.DISPATCHED.value]),
        )
        if after_id is not None:
            query = query.where(TransferModel.id > after_id)

        query = query.order_by(TransferModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return TransferListResponse(
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

    w1 = WarehouseModel(id=1, code="WH-MILANO", name="Milano Central Depot", active=True, is_deleted=False)
    w2 = WarehouseModel(id=2, code="WH-ROMA", name="Roma Hub", active=True, is_deleted=False)
    w3 = WarehouseModel(id=3, code="WH-TORINO", name="Torino Auxiliary", active=False, is_deleted=False)
    db.add_all([w1, w2, w3])
    db.flush()

    p1 = ProductModel(id=1, sku="SKU-STEEL-001", name="Stainless Steel Beam", active=True, is_deleted=False)
    p2 = ProductModel(id=2, sku="SKU-ALUM-002", name="Aluminium Sheet", active=True, is_deleted=False)
    db.add_all([p1, p2])
    db.flush()

    s1 = StockLevelModel(id=1, warehouse_id=1, product_id=1, quantity=500.0, in_transit_quantity=0.0)
    s2 = StockLevelModel(id=2, warehouse_id=2, product_id=1, quantity=100.0, in_transit_quantity=0.0)
    s3 = StockLevelModel(id=3, warehouse_id=1, product_id=2, quantity=200.0, in_transit_quantity=0.0)
    db.add_all([s1, s2, s3])
    db.flush()

    t1 = TransferModel(
        id=1,
        source_warehouse_id=1,
        dest_warehouse_id=2,
        product_id=1,
        quantity=50.0,
        status=TransferStatusEnum.DRAFT.value,
    )
    db.add(t1)
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
    title="Inventory Management - Multi-Warehouse & Atomic Transfers API",
    version="1.0.0",
    description="Deterministic RESTful API for warehouses, products, and multi-warehouse atomic stock transfers.",
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
# Endpoints (20 Specified Endpoints for Variant B)
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


# 2. GET /warehouses
@app.get(
    "/warehouses",
    tags=["Warehouses"],
    response_model=WarehouseListResponse,
)
def list_warehouses(
    code: Optional[str] = Query(None),
    active: Optional[bool] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return WarehouseController.list_warehouses(db, code, active, limit, after_id)


# 3. POST /warehouses
@app.post(
    "/warehouses",
    tags=["Warehouses"],
    status_code=status.HTTP_201_CREATED,
    response_model=WarehouseResponse,
)
def create_warehouse(
    payload: WarehouseCreateRequest,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    wh = WarehouseController.create_warehouse(db, current_user, payload)
    response.headers["Location"] = f"/warehouses/{wh.id}"
    return WarehouseController.serialize(wh)


# 4. GET /warehouses/{warehouse_id}
@app.get(
    "/warehouses/{warehouse_id}",
    tags=["Warehouses"],
    response_model=WarehouseResponse,
)
def get_warehouse(
    warehouse_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    wh = WarehouseController.get_by_id(db, warehouse_id)
    return WarehouseController.serialize(wh)


# 5. PATCH /warehouses/{warehouse_id}
@app.patch(
    "/warehouses/{warehouse_id}",
    tags=["Warehouses"],
    response_model=WarehouseResponse,
)
def patch_warehouse(
    warehouse_id: int,
    payload: WarehousePatchRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    wh = WarehouseController.patch_warehouse(db, current_user, warehouse_id, payload)
    return WarehouseController.serialize(wh)


# 6. DELETE /warehouses/{warehouse_id}
@app.delete(
    "/warehouses/{warehouse_id}",
    tags=["Warehouses"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_warehouse(
    warehouse_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    WarehouseController.delete_warehouse(db, current_user, warehouse_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 7. GET /products
@app.get(
    "/products",
    tags=["Products"],
    response_model=ProductListResponse,
)
def list_products(
    sku: Optional[str] = Query(None),
    name: Optional[str] = Query(None),
    active: Optional[bool] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return ProductController.list_products(db, sku, name, active, limit, after_id)


# 8. POST /products
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


# 9. GET /products/{product_id}
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


# 10. PATCH /products/{product_id}
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


# 11. DELETE /products/{product_id}
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


# 12. GET /transfers
@app.get(
    "/transfers",
    tags=["Transfers"],
    response_model=TransferListResponse,
)
def list_transfers(
    source_warehouse_id: Optional[int] = Query(None, gt=0),
    dest_warehouse_id: Optional[int] = Query(None, gt=0),
    product_id: Optional[int] = Query(None, gt=0),
    status: Optional[TransferStatusEnum] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return TransferController.list_transfers(db, source_warehouse_id, dest_warehouse_id, product_id, status, limit, after_id)


# 13. POST /transfers
@app.post(
    "/transfers",
    tags=["Transfers"],
    status_code=status.HTTP_201_CREATED,
    response_model=TransferResponse,
)
def create_transfer(
    payload: TransferCreateRequest,
    response: Response,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    tr = TransferController.create_transfer(db, current_user, payload)
    response.headers["Location"] = f"/transfers/{tr.id}"
    return TransferController.serialize(tr)


# 14. GET /transfers/{transfer_id}
@app.get(
    "/transfers/{transfer_id}",
    tags=["Transfers"],
    response_model=TransferResponse,
)
def get_transfer(
    transfer_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    tr = TransferController.get_by_id(db, transfer_id)
    return TransferController.serialize(tr)


# 15. PATCH /transfers/{transfer_id}
@app.patch(
    "/transfers/{transfer_id}",
    tags=["Transfers"],
    response_model=TransferResponse,
)
def patch_transfer(
    transfer_id: int,
    payload: TransferPatchRequest,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    tr = TransferController.patch_transfer(db, current_user, transfer_id, payload)
    return TransferController.serialize(tr)


# 16. DELETE /transfers/{transfer_id}
@app.delete(
    "/transfers/{transfer_id}",
    tags=["Transfers"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_transfer(
    transfer_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    TransferController.delete_transfer(db, current_user, transfer_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 17. POST /transfers/{transfer_id}/dispatch
@app.post(
    "/transfers/{transfer_id}/dispatch",
    tags=["Transfers"],
    response_model=TransferResponse,
)
def dispatch_transfer(
    transfer_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    tr = TransferController.dispatch_transfer(db, current_user, transfer_id)
    return TransferController.serialize(tr)


# 18. POST /transfers/{transfer_id}/receive
@app.post(
    "/transfers/{transfer_id}/receive",
    tags=["Transfers"],
    response_model=TransferResponse,
)
def receive_transfer(
    transfer_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    tr = TransferController.receive_transfer(db, current_user, transfer_id)
    return TransferController.serialize(tr)


# 19. POST /transfers/{transfer_id}/cancel
@app.post(
    "/transfers/{transfer_id}/cancel",
    tags=["Transfers"],
    response_model=TransferResponse,
)
def cancel_transfer(
    transfer_id: int,
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    tr = TransferController.cancel_transfer(db, current_user, transfer_id)
    return TransferController.serialize(tr)


# 20. GET /warehouses/{warehouse_id}/transfer-conflicts
@app.get(
    "/warehouses/{warehouse_id}/transfer-conflicts",
    tags=["Warehouses"],
    response_model=TransferListResponse,
)
def get_warehouse_transfer_conflicts(
    warehouse_id: int,
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(AuthController.get_current_user),
    db: Session = Depends(get_db),
):
    return TransferController.list_conflicts(db, warehouse_id, limit, after_id)


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
