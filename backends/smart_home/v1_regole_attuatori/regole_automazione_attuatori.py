"""
Automation Rules and Actuators Backend API
Domain: Smart home
Variant: Automation rules and actuators (smart-home-automation-rules-v1)
Architecture: MVC Pattern, FastAPI, Pydantic v2, SQLAlchemy ORM, SQLite In-Memory
"""

import enum
import hashlib
import http
import ipaddress
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, Query, Request, Response
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
    create_engine,
    func,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Match

# -----------------------------------------------------------------------------
# Logging Configuration
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("regole_automazione_attuatori_service")

# -----------------------------------------------------------------------------
# Database Setup
# -----------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///:memory:")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# -----------------------------------------------------------------------------
# Helper Security Utilities & Domain Validators
# -----------------------------------------------------------------------------
def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc, microsecond=0) if value.tzinfo is None else value.astimezone(timezone.utc).replace(microsecond=0)


def validate_safe_url(url_str: str) -> str:
    parsed = urlparse(url_str)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL scheme must be http or https")
    hostname = parsed.hostname
    if not hostname or hostname in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Host localhost or loopback is forbidden")
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified or ip.is_reserved:
            raise ValueError("Private and local IP addresses are forbidden")
    except ValueError as err:
        if "does not appear to be an IPv4 or IPv6 address" not in str(err):
            raise
    return url_str


# -----------------------------------------------------------------------------
# Enums
# -----------------------------------------------------------------------------
class ActuatorTypeEnum(str, enum.Enum):
    SWITCH = "SWITCH"
    DIMMER = "DIMMER"
    THERMOSTAT = "THERMOSTAT"


class SourceKindEnum(str, enum.Enum):
    TEMPERATURE = "TEMPERATURE"
    HUMIDITY = "HUMIDITY"
    MOTION = "MOTION"


class ComparatorEnum(str, enum.Enum):
    GT = "GT"
    GTE = "GTE"
    LT = "LT"
    LTE = "LTE"
    EQ = "EQ"


class CommandOriginEnum(str, enum.Enum):
    MANUAL = "MANUAL"
    RULE = "RULE"


class ExecutionOutcomeEnum(str, enum.Enum):
    APPLIED = "APPLIED"
    SUPPRESSED = "SUPPRESSED"


class UserRole(str, enum.Enum):
    USER = "user"
    ADMIN = "admin"


def validate_actuator_command_value(actuator_type: str, value: str) -> None:
    val_str = str(value).strip()
    if actuator_type == ActuatorTypeEnum.SWITCH.value:
        if val_str not in ("ON", "OFF"):
            raise ValueError("SWITCH command value must be 'ON' or 'OFF'")
    elif actuator_type == ActuatorTypeEnum.DIMMER.value:
        try:
            ival = int(val_str)
            if not (0 <= ival <= 100):
                raise ValueError()
        except Exception:
            raise ValueError("DIMMER command value must be integer 0..100")
    elif actuator_type == ActuatorTypeEnum.THERMOSTAT.value:
        try:
            fval = float(val_str)
            if not (5.0 <= fval <= 35.0):
                raise ValueError()
        except Exception:
            raise ValueError("THERMOSTAT command value must be decimal 5.0..35.0")
    else:
        raise ValueError(f"Unknown actuator type: {actuator_type}")


# -----------------------------------------------------------------------------
# SQLAlchemy Models (MVC: Model)
# -----------------------------------------------------------------------------
class UserModel(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=UserRole.USER.value)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    actuators: Mapped[List["ActuatorModel"]] = relationship("ActuatorModel", back_populates="owner")
    rules: Mapped[List["RuleModel"]] = relationship("RuleModel", back_populates="owner")
    events: Mapped[List["EventModel"]] = relationship("EventModel", back_populates="owner")


class ActuatorModel(Base):
    __tablename__ = "actuators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    serial: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)
    current_value: Mapped[str] = mapped_column(String(50), nullable=False)

    owner: Mapped[UserModel] = relationship("UserModel", back_populates="actuators")
    rule_actions: Mapped[List["RuleActionModel"]] = relationship("RuleActionModel", back_populates="actuator")
    commands: Mapped[List["CommandModel"]] = relationship("CommandModel", back_populates="actuator")


class RuleModel(Base):
    __tablename__ = "automation_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    comparator: Mapped[str] = mapped_column(String(10), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    owner: Mapped[UserModel] = relationship("UserModel", back_populates="rules")
    actions: Mapped[List["RuleActionModel"]] = relationship(
        "RuleActionModel",
        back_populates="rule",
        cascade="all, delete-orphan",
        order_by="RuleActionModel.order.asc()",
    )
    executions: Mapped[List["ExecutionModel"]] = relationship("ExecutionModel", back_populates="rule")


class RuleActionModel(Base):
    __tablename__ = "rule_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, ForeignKey("automation_rules.id"), nullable=False)
    actuator_id: Mapped[int] = mapped_column(Integer, ForeignKey("actuators.id"), nullable=False)
    order: Mapped[int] = mapped_column(Integer, nullable=False)
    command_value: Mapped[str] = mapped_column(String(50), nullable=False)

    rule: Mapped[RuleModel] = relationship("RuleModel", back_populates="actions")
    actuator: Mapped[ActuatorModel] = relationship("ActuatorModel", back_populates="rule_actions")


class EventModel(Base):
    __tablename__ = "automation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False)
    client_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    owner: Mapped[UserModel] = relationship("UserModel", back_populates="events")
    executions: Mapped[List["ExecutionModel"]] = relationship("ExecutionModel", back_populates="event")


class ExecutionModel(Base):
    __tablename__ = "automation_executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, ForeignKey("automation_rules.id"), nullable=False)
    event_id: Mapped[int] = mapped_column(Integer, ForeignKey("automation_events.id"), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    rule: Mapped[RuleModel] = relationship("RuleModel", back_populates="executions")
    event: Mapped[EventModel] = relationship("EventModel", back_populates="executions")
    commands: Mapped[List["CommandModel"]] = relationship("CommandModel", back_populates="execution")


class CommandModel(Base):
    __tablename__ = "actuator_commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actuator_id: Mapped[int] = mapped_column(Integer, ForeignKey("actuators.id"), nullable=False)
    origin: Mapped[str] = mapped_column(String(20), nullable=False)
    execution_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("automation_executions.id"), nullable=True)
    command_value: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="APPLIED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    actuator: Mapped[ActuatorModel] = relationship("ActuatorModel", back_populates="commands")
    execution: Mapped[Optional[ExecutionModel]] = relationship("ExecutionModel", back_populates="commands")


# -----------------------------------------------------------------------------
# Custom App Exception
# -----------------------------------------------------------------------------
class AppException(Exception):
    def __init__(
        self,
        status_code: int,
        detail: str,
        error_type: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
    ):
        self.status_code = status_code
        self.detail = detail
        self.headers = headers or {}
        if error_type is None:
            self.error_type = (
                http.HTTPStatus(status_code).phrase
                if status_code in http.HTTPStatus._value2member_map_
                else "Error"
            )
        else:
            self.error_type = error_type
        super().__init__(detail)


# -----------------------------------------------------------------------------
# Pydantic v2 Schemas (MVC: View/Schema)
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


# Actuator Schemas
class ActuatorCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner_id: int = Field(..., gt=0)
    serial: str = Field(..., pattern=r"^[A-Z0-9-]{6,32}$")
    name: str = Field(..., min_length=1, max_length=80)
    type: ActuatorTypeEnum
    initial_value: str = Field(..., min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_initial_value(self):
        validate_actuator_command_value(self.type.value, self.initial_value)
        return self


class ActuatorPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    owner_id: Optional[int] = Field(None, gt=0)

    @model_validator(mode="after")
    def validate_patch(self):
        if not self.model_fields_set:
            raise ValueError("Patch payload cannot be empty")
        for field_name in ("name", "owner_id"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null")
        return self


class ActuatorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    owner_id: int
    serial: str
    name: str
    type: ActuatorTypeEnum
    current_value: str


class ActuatorListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[ActuatorResponse]
    next_after_id: Optional[int]


# Command Schemas
class ManualCommandCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(..., min_length=1, max_length=50)


class CommandResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    actuator_id: int
    origin: CommandOriginEnum
    execution_id: Optional[int]
    command_value: str
    status: str
    created_at: datetime


class CommandListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[CommandResponse]
    next_after_id: Optional[int]


# Rule Action Schemas
class RuleActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order: int = Field(..., ge=1, le=5)
    actuator_id: int = Field(..., gt=0)
    command_value: str = Field(..., min_length=1, max_length=50)


class RuleActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    rule_id: int
    actuator_id: int
    order: int
    command_value: str


# Rule Schemas
class RuleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=3, max_length=80)
    source_kind: SourceKindEnum
    comparator: ComparatorEnum
    threshold: float = Field(..., ge=-50.0, le=5000.0)
    cooldown_seconds: int = Field(..., ge=0, le=86400)
    actions: List[RuleActionInput] = Field(..., min_length=1, max_length=5)
    owner_id: Optional[int] = Field(None, gt=0)

    @model_validator(mode="after")
    def validate_actions(self):
        orders = [a.order for a in self.actions]
        if len(orders) != len(set(orders)):
            raise ValueError("Action orders must be distinct")
        actuators = [a.actuator_id for a in self.actions]
        if len(actuators) != len(set(actuators)):
            raise ValueError("Target actuators in actions must be distinct")
        return self


class RuleReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=3, max_length=80)
    source_kind: SourceKindEnum
    comparator: ComparatorEnum
    threshold: float = Field(..., ge=-50.0, le=5000.0)
    cooldown_seconds: int = Field(..., ge=0, le=86400)
    enabled: bool = True
    actions: List[RuleActionInput] = Field(..., min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_actions(self):
        orders = [a.order for a in self.actions]
        if len(orders) != len(set(orders)):
            raise ValueError("Action orders must be distinct")
        actuators = [a.actuator_id for a in self.actions]
        if len(actuators) != len(set(actuators)):
            raise ValueError("Target actuators in actions must be distinct")
        return self


class RuleResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    owner_id: int
    name: str
    enabled: bool
    source_kind: SourceKindEnum
    comparator: ComparatorEnum
    threshold: float
    cooldown_seconds: int
    actions: List[RuleActionResponse]


class RuleListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[RuleResponse]
    next_after_id: Optional[int]


# Event Schemas
class EventCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_event_id: str = Field(..., pattern=r"^[A-Za-z0-9-]{8,64}$")
    source_kind: SourceKindEnum
    value: float = Field(..., ge=-50.0, le=5000.0)
    occurred_at: datetime
    owner_id: Optional[int] = Field(None, gt=0)

    @field_validator("occurred_at")
    @classmethod
    def validate_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("occurred_at must be a timezone-aware UTC datetime")
        return v.astimezone(timezone.utc).replace(microsecond=0)


class EventResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    owner_id: int
    client_event_id: str
    source_kind: SourceKindEnum
    value: float
    occurred_at: datetime


# Execution Schemas
class ExecutionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    rule_id: int
    event_id: int
    outcome: ExecutionOutcomeEnum
    executed_at: datetime


class ExecutionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[ExecutionResponse]
    next_after_id: Optional[int]


# -----------------------------------------------------------------------------
# Static Controllers (MVC: Controller)
# -----------------------------------------------------------------------------
class AuthController:
    @staticmethod
    def get_current_user(
        authorization: Optional[str] = Header(None),
        db: Session = Depends(get_db),
    ) -> UserModel:
        if not authorization or not authorization.startswith("Bearer "):
            raise AppException(401, "Missing or invalid Bearer authorization header", "Unauthorized")
        token = authorization[7:].strip()
        if not token:
            raise AppException(401, "Missing session token", "Unauthorized")
        thash = hash_token(token)
        user = db.scalar(select(UserModel).where(UserModel.token_hash == thash))
        if not user:
            raise AppException(401, "Invalid or expired session token", "Unauthorized")
        return user

    @staticmethod
    def require_admin(
        current_user: UserModel = Depends(get_current_user),
    ) -> UserModel:
        if current_user.role != UserRole.ADMIN.value:
            raise AppException(403, "Administrator privilege required", "Forbidden")
        return current_user


class ActuatorController:
    @staticmethod
    def serialize_actuator(a: ActuatorModel) -> ActuatorResponse:
        return ActuatorResponse(
            id=a.id,
            owner_id=a.owner_id,
            serial=a.serial,
            name=a.name,
            type=ActuatorTypeEnum(a.type),
            current_value=a.current_value,
        )

    @classmethod
    def list_actuators(
        cls,
        db: Session,
        current_user: UserModel,
        type_filter: Optional[ActuatorTypeEnum],
        serial_prefix: Optional[str],
        owner_id: Optional[int],
        limit: int,
        after_id: Optional[int],
    ) -> ActuatorListResponse:
        if after_id is not None and after_id <= 0:
            raise AppException(422, "after_id must be a positive integer", "Unprocessable Entity")

        query = select(ActuatorModel)

        if owner_id is not None:
            if owner_id <= 0:
                raise AppException(404, "owner_id must be a positive integer", "Not Found")
            if current_user.role == UserRole.USER.value and owner_id != current_user.id:
                raise AppException(403, "Users cannot list actuators of other owners", "Forbidden")
            owner = db.scalar(select(UserModel).where(UserModel.id == owner_id))
            if not owner:
                raise AppException(404, f"Owner {owner_id} not found", "Not Found")
            query = query.where(ActuatorModel.owner_id == owner_id)
        else:
            if current_user.role == UserRole.USER.value:
                query = query.where(ActuatorModel.owner_id == current_user.id)

        if type_filter is not None:
            query = query.where(ActuatorModel.type == type_filter.value)

        if serial_prefix is not None:
            norm_prefix = serial_prefix.strip().upper()
            if not (1 <= len(norm_prefix) <= 32):
                raise AppException(422, "serial_prefix length must be 1..32", "Unprocessable Entity")
            query = query.where(ActuatorModel.serial.startswith(norm_prefix))

        if after_id is not None:
            query = query.where(ActuatorModel.id > after_id)

        query = query.order_by(ActuatorModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return ActuatorListResponse(
            items=[cls.serialize_actuator(a) for a in items],
            next_after_id=next_id,
        )

    @classmethod
    def get_actuator_by_id(cls, db: Session, current_user: UserModel, actuator_id: int) -> ActuatorModel:
        if actuator_id <= 0:
            raise AppException(404, "Actuator id must be a positive integer", "Not Found")
        actuator = db.scalar(select(ActuatorModel).where(ActuatorModel.id == actuator_id))
        if not actuator:
            raise AppException(404, f"Actuator {actuator_id} not found", "Not Found")
        if current_user.role != UserRole.ADMIN.value and actuator.owner_id != current_user.id:
            raise AppException(403, "Access denied to actuator owned by another user", "Forbidden")
        return actuator

    @classmethod
    def create_actuator(cls, db: Session, payload: ActuatorCreateRequest) -> ActuatorModel:
        if payload.owner_id <= 0:
            raise AppException(404, "owner_id must be a positive integer", "Not Found")
        owner = db.scalar(select(UserModel).where(UserModel.id == payload.owner_id))
        if not owner:
            raise AppException(404, f"Owner {payload.owner_id} not found", "Not Found")

        try:
            existing = db.scalar(select(ActuatorModel).where(ActuatorModel.serial == payload.serial))
            if existing:
                raise AppException(409, f"Actuator with serial '{payload.serial}' already exists", "Conflict")

            actuator = ActuatorModel(
                owner_id=payload.owner_id,
                serial=payload.serial,
                name=payload.name,
                type=payload.type.value,
                current_value=payload.initial_value,
            )
            db.add(actuator)
            db.commit()
            db.refresh(actuator)
            return actuator
        except AppException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error("Failed to create actuator: %s", exc)
            raise AppException(500, "Failed to create actuator", "Internal Server Error")

    @classmethod
    def patch_actuator(
        cls,
        db: Session,
        actuator_id: int,
        payload: ActuatorPatchRequest,
    ) -> ActuatorModel:
        if actuator_id <= 0:
            raise AppException(404, "Actuator id must be a positive integer", "Not Found")
        actuator = db.scalar(select(ActuatorModel).where(ActuatorModel.id == actuator_id))
        if not actuator:
            raise AppException(404, f"Actuator {actuator_id} not found", "Not Found")

        data = payload.model_dump(exclude_unset=True)
        if "owner_id" in data:
            new_owner_id = data["owner_id"]
            if new_owner_id <= 0:
                raise AppException(404, "owner_id must be a positive integer", "Not Found")
            owner = db.scalar(select(UserModel).where(UserModel.id == new_owner_id))
            if not owner:
                raise AppException(404, f"Owner user {new_owner_id} not found", "Not Found")
            actuator.owner_id = new_owner_id

        if "name" in data:
            actuator.name = data["name"]

        try:
            db.commit()
            db.refresh(actuator)
            return actuator
        except Exception as exc:
            db.rollback()
            logger.error("Failed to patch actuator: %s", exc)
            raise AppException(500, "Failed to patch actuator", "Internal Server Error")

    @classmethod
    def delete_actuator(cls, db: Session, actuator_id: int) -> None:
        if actuator_id <= 0:
            raise AppException(404, "Actuator id must be a positive integer", "Not Found")
        actuator = db.scalar(select(ActuatorModel).where(ActuatorModel.id == actuator_id))
        if not actuator:
            raise AppException(404, f"Actuator {actuator_id} not found", "Not Found")

        action_ref = db.scalar(select(func.count(RuleActionModel.id)).where(RuleActionModel.actuator_id == actuator.id)) or 0
        cmd_ref = db.scalar(select(func.count(CommandModel.id)).where(CommandModel.actuator_id == actuator.id)) or 0
        if action_ref > 0 or cmd_ref > 0:
            raise AppException(
                409,
                "Cannot delete actuator referenced by rule action or command history under REQ-2-009",
                "Conflict",
            )

        try:
            db.delete(actuator)
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.error("Failed to delete actuator: %s", exc)
            raise AppException(500, "Failed to delete actuator", "Internal Server Error")

    @classmethod
    def create_manual_command(
        cls,
        db: Session,
        current_user: UserModel,
        actuator_id: int,
        payload: ManualCommandCreateRequest,
    ) -> CommandModel:
        actuator = cls.get_actuator_by_id(db, current_user, actuator_id)
        try:
            validate_actuator_command_value(actuator.type, payload.value)
        except ValueError as val_err:
            raise AppException(422, str(val_err), "Unprocessable Entity")

        try:
            cmd = CommandModel(
                actuator_id=actuator.id,
                origin=CommandOriginEnum.MANUAL.value,
                execution_id=None,
                command_value=payload.value,
                status="APPLIED",
                created_at=utc_now(),
            )
            actuator.current_value = payload.value
            db.add(cmd)
            db.commit()
            db.refresh(cmd)
            return cmd
        except Exception as exc:
            db.rollback()
            logger.error("Failed to create manual command: %s", exc)
            raise AppException(500, "Failed to create manual command", "Internal Server Error")

    @classmethod
    def list_commands(
        cls,
        db: Session,
        current_user: UserModel,
        actuator_id: int,
        origin: Optional[CommandOriginEnum],
        limit: int,
        after_id: Optional[int],
    ) -> CommandListResponse:
        actuator = cls.get_actuator_by_id(db, current_user, actuator_id)
        if after_id is not None and after_id <= 0:
            raise AppException(422, "after_id must be a positive integer", "Unprocessable Entity")

        query = select(CommandModel).where(CommandModel.actuator_id == actuator.id)
        if origin is not None:
            query = query.where(CommandModel.origin == origin.value)
        if after_id is not None:
            query = query.where(CommandModel.id > after_id)

        query = query.order_by(CommandModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return CommandListResponse(
            items=[
                CommandResponse(
                    id=c.id,
                    actuator_id=c.actuator_id,
                    origin=CommandOriginEnum(c.origin),
                    execution_id=c.execution_id,
                    command_value=c.command_value,
                    status=c.status,
                    created_at=as_utc(c.created_at),
                )
                for c in items
            ],
            next_after_id=next_id,
        )


class RuleController:
    @staticmethod
    def serialize_rule(r: RuleModel) -> RuleResponse:
        return RuleResponse(
            id=r.id,
            owner_id=r.owner_id,
            name=r.name,
            enabled=r.enabled,
            source_kind=SourceKindEnum(r.source_kind),
            comparator=ComparatorEnum(r.comparator),
            threshold=r.threshold,
            cooldown_seconds=r.cooldown_seconds,
            actions=[
                RuleActionResponse(
                    id=a.id,
                    rule_id=a.rule_id,
                    actuator_id=a.actuator_id,
                    order=a.order,
                    command_value=a.command_value,
                )
                for a in sorted(r.actions, key=lambda x: x.order)
            ],
        )

    @classmethod
    def list_rules(
        cls,
        db: Session,
        current_user: UserModel,
        enabled: Optional[bool],
        source_kind: Optional[SourceKindEnum],
        owner_id: Optional[int],
        limit: int,
        after_id: Optional[int],
    ) -> RuleListResponse:
        if after_id is not None and after_id <= 0:
            raise AppException(422, "after_id must be a positive integer", "Unprocessable Entity")

        query = select(RuleModel)

        if owner_id is not None:
            if owner_id <= 0:
                raise AppException(404, "owner_id must be a positive integer", "Not Found")
            if current_user.role == UserRole.USER.value and owner_id != current_user.id:
                raise AppException(403, "Users cannot list rules of other owners", "Forbidden")
            owner = db.scalar(select(UserModel).where(UserModel.id == owner_id))
            if not owner:
                raise AppException(404, f"Owner {owner_id} not found", "Not Found")
            query = query.where(RuleModel.owner_id == owner_id)
        else:
            if current_user.role == UserRole.USER.value:
                query = query.where(RuleModel.owner_id == current_user.id)

        if enabled is not None:
            query = query.where(RuleModel.enabled == enabled)
        if source_kind is not None:
            query = query.where(RuleModel.source_kind == source_kind.value)
        if after_id is not None:
            query = query.where(RuleModel.id > after_id)

        query = query.order_by(RuleModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return RuleListResponse(
            items=[cls.serialize_rule(r) for r in items],
            next_after_id=next_id,
        )

    @classmethod
    def get_rule_by_id(cls, db: Session, current_user: UserModel, rule_id: int) -> RuleModel:
        if rule_id <= 0:
            raise AppException(404, "Rule id must be a positive integer", "Not Found")
        rule = db.scalar(select(RuleModel).where(RuleModel.id == rule_id))
        if not rule:
            raise AppException(404, f"Rule {rule_id} not found", "Not Found")
        if current_user.role != UserRole.ADMIN.value and rule.owner_id != current_user.id:
            raise AppException(403, "Access denied to rule owned by another user", "Forbidden")
        return rule

    @classmethod
    def create_rule(
        cls,
        db: Session,
        current_user: UserModel,
        payload: RuleCreateRequest,
    ) -> RuleModel:
        if payload.owner_id is not None:
            if current_user.role != UserRole.ADMIN.value:
                if payload.owner_id != current_user.id:
                    raise AppException(403, "Users cannot assign rules to other owners", "Forbidden")
                target_owner_id = current_user.id
            else:
                if payload.owner_id <= 0:
                    raise AppException(404, "owner_id must be a positive integer", "Not Found")
                owner = db.scalar(select(UserModel).where(UserModel.id == payload.owner_id))
                if not owner:
                    raise AppException(404, f"Owner {payload.owner_id} not found", "Not Found")
                target_owner_id = payload.owner_id
        else:
            target_owner_id = current_user.id

        # Verify duplicate rule name per owner
        existing_name = db.scalar(
            select(RuleModel).where(
                RuleModel.owner_id == target_owner_id,
                RuleModel.name == payload.name,
            )
        )
        if existing_name:
            raise AppException(409, f"Rule with name '{payload.name}' already exists for this owner", "Conflict")

        # Verify action actuators exist, same owner, valid command
        for a in payload.actions:
            actuator = db.scalar(select(ActuatorModel).where(ActuatorModel.id == a.actuator_id))
            if not actuator:
                raise AppException(404, f"Actuator {a.actuator_id} not found", "Not Found")
            if actuator.owner_id != target_owner_id:
                raise AppException(
                    422,
                    f"Actuator {a.actuator_id} belongs to another user; all actions must target same-owner actuators",
                    "Unprocessable Entity",
                )
            try:
                validate_actuator_command_value(actuator.type, a.command_value)
            except ValueError as err:
                raise AppException(422, f"Action for actuator {a.actuator_id}: {err}", "Unprocessable Entity")

        try:
            rule = RuleModel(
                owner_id=target_owner_id,
                name=payload.name,
                enabled=True,
                source_kind=payload.source_kind.value,
                comparator=payload.comparator.value,
                threshold=payload.threshold,
                cooldown_seconds=payload.cooldown_seconds,
            )
            db.add(rule)
            db.flush()

            for a in payload.actions:
                action = RuleActionModel(
                    rule_id=rule.id,
                    actuator_id=a.actuator_id,
                    order=a.order,
                    command_value=a.command_value,
                )
                db.add(action)

            db.commit()
            db.refresh(rule)
            return rule
        except AppException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error("Failed to create rule: %s", exc)
            raise AppException(500, "Failed to create rule", "Internal Server Error")

    @classmethod
    def replace_rule(
        cls,
        db: Session,
        current_user: UserModel,
        rule_id: int,
        payload: RuleReplaceRequest,
    ) -> RuleModel:
        rule = cls.get_rule_by_id(db, current_user, rule_id)

        # Check duplicate name for same owner
        existing = db.scalar(
            select(RuleModel).where(
                RuleModel.owner_id == rule.owner_id,
                RuleModel.name == payload.name,
                RuleModel.id != rule.id,
            )
        )
        if existing:
            raise AppException(409, f"Rule with name '{payload.name}' already exists for this owner", "Conflict")

        # Validate actuators
        for a in payload.actions:
            actuator = db.scalar(select(ActuatorModel).where(ActuatorModel.id == a.actuator_id))
            if not actuator:
                raise AppException(404, f"Actuator {a.actuator_id} not found", "Not Found")
            if actuator.owner_id != rule.owner_id:
                raise AppException(
                    422,
                    f"Actuator {a.actuator_id} belongs to another user",
                    "Unprocessable Entity",
                )
            try:
                validate_actuator_command_value(actuator.type, a.command_value)
            except ValueError as err:
                raise AppException(422, f"Action for actuator {a.actuator_id}: {err}", "Unprocessable Entity")

        try:
            rule.name = payload.name
            rule.source_kind = payload.source_kind.value
            rule.comparator = payload.comparator.value
            rule.threshold = payload.threshold
            rule.cooldown_seconds = payload.cooldown_seconds
            rule.enabled = payload.enabled

            # Replace actions
            for old_action in list(rule.actions):
                db.delete(old_action)
            db.flush()

            for a in payload.actions:
                new_action = RuleActionModel(
                    rule_id=rule.id,
                    actuator_id=a.actuator_id,
                    order=a.order,
                    command_value=a.command_value,
                )
                db.add(new_action)

            db.commit()
            db.refresh(rule)
            return rule
        except AppException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error("Failed to replace rule: %s", exc)
            raise AppException(500, "Failed to replace rule", "Internal Server Error")

    @classmethod
    def delete_rule(cls, db: Session, current_user: UserModel, rule_id: int) -> None:
        rule = cls.get_rule_by_id(db, current_user, rule_id)

        has_executions = (db.scalar(select(func.count(ExecutionModel.id)).where(ExecutionModel.rule_id == rule.id)) or 0) > 0
        if has_executions:
            raise AppException(409, "Cannot delete an executed rule to preserve audit history under REQ-2-009", "Conflict")

        try:
            db.delete(rule)
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.error("Failed to delete rule: %s", exc)
            raise AppException(500, "Failed to delete rule", "Internal Server Error")

    @classmethod
    def set_enabled(cls, db: Session, current_user: UserModel, rule_id: int, enabled: bool) -> RuleModel:
        rule = cls.get_rule_by_id(db, current_user, rule_id)
        rule.enabled = enabled
        try:
            db.commit()
            db.refresh(rule)
            return rule
        except Exception as exc:
            db.rollback()
            logger.error("Failed to update rule status: %s", exc)
            raise AppException(500, "Failed to update rule status", "Internal Server Error")


class AutomationEventController:
    @classmethod
    def create_and_evaluate_event(
        cls,
        db: Session,
        current_user: UserModel,
        payload: EventCreateRequest,
    ) -> tuple[EventModel, bool]:
        if payload.owner_id is not None:
            if current_user.role != UserRole.ADMIN.value:
                if payload.owner_id != current_user.id:
                    raise AppException(403, "Users cannot submit events for other owners", "Forbidden")
                target_owner_id = current_user.id
            else:
                if payload.owner_id <= 0:
                    raise AppException(404, "owner_id must be a positive integer", "Not Found")
                owner = db.scalar(select(UserModel).where(UserModel.id == payload.owner_id))
                if not owner:
                    raise AppException(404, f"Owner user {payload.owner_id} not found", "Not Found")
                target_owner_id = payload.owner_id
        else:
            target_owner_id = current_user.id

        # Check existing client_event_id for owner
        existing = db.scalar(
            select(EventModel).where(
                EventModel.owner_id == target_owner_id,
                EventModel.client_event_id == payload.client_event_id,
            )
        )
        if existing:
            # Check exact equality
            existing_occ = (
                existing.occurred_at.replace(tzinfo=timezone.utc)
                if existing.occurred_at.tzinfo is None
                else existing.occurred_at
            )
            is_identical = (
                existing.source_kind == payload.source_kind.value
                and abs(existing.value - payload.value) < 1e-6
                and existing_occ == payload.occurred_at
            )
            if is_identical:
                # Idempotent replay: return existing event, is_new=False
                return existing, False
            else:
                raise AppException(
                    409,
                    f"client_event_id '{payload.client_event_id}' already exists with differing payload",
                    "Conflict",
                )

        try:
            event = EventModel(
                owner_id=target_owner_id,
                client_event_id=payload.client_event_id,
                source_kind=payload.source_kind.value,
                value=payload.value,
                occurred_at=payload.occurred_at,
            )
            db.add(event)
            db.flush()

            # Query enabled matching rules for owner ordered by rule id asc
            rules = list(
                db.scalars(
                    select(RuleModel)
                    .where(
                        RuleModel.owner_id == target_owner_id,
                        RuleModel.enabled == True,
                        RuleModel.source_kind == payload.source_kind.value,
                    )
                    .order_by(RuleModel.id.asc())
                ).all()
            )

            for rule in rules:
                # Check condition match
                val = payload.value
                thresh = rule.threshold
                matched = False
                if rule.comparator == ComparatorEnum.GT.value:
                    matched = val > thresh
                elif rule.comparator == ComparatorEnum.GTE.value:
                    matched = val >= thresh
                elif rule.comparator == ComparatorEnum.LT.value:
                    matched = val < thresh
                elif rule.comparator == ComparatorEnum.LTE.value:
                    matched = val <= thresh
                elif rule.comparator == ComparatorEnum.EQ.value:
                    matched = abs(val - thresh) < 1e-6

                if not matched:
                    continue

                # Check cooldown: last APPLIED execution of this rule
                last_exec = db.scalar(
                    select(ExecutionModel)
                    .where(
                        ExecutionModel.rule_id == rule.id,
                        ExecutionModel.outcome == ExecutionOutcomeEnum.APPLIED.value,
                    )
                    .order_by(ExecutionModel.executed_at.desc(), ExecutionModel.id.desc())
                    .limit(1)
                )

                outcome = ExecutionOutcomeEnum.APPLIED.value
                if last_exec:
                    last_dt = (
                        last_exec.executed_at.replace(tzinfo=timezone.utc)
                        if last_exec.executed_at.tzinfo is None
                        else last_exec.executed_at
                    )
                    occ_dt = (
                        payload.occurred_at.replace(tzinfo=timezone.utc)
                        if payload.occurred_at.tzinfo is None
                        else payload.occurred_at
                    )
                    elapsed = (occ_dt - last_dt).total_seconds()
                    if elapsed < rule.cooldown_seconds:
                        outcome = ExecutionOutcomeEnum.SUPPRESSED.value

                exec_rec = ExecutionModel(
                    rule_id=rule.id,
                    event_id=event.id,
                    outcome=outcome,
                    executed_at=payload.occurred_at,
                )
                db.add(exec_rec)
                db.flush()

                if outcome == ExecutionOutcomeEnum.APPLIED.value:
                    for action in sorted(rule.actions, key=lambda a: a.order):
                        cmd = CommandModel(
                            actuator_id=action.actuator_id,
                            origin=CommandOriginEnum.RULE.value,
                            execution_id=exec_rec.id,
                            command_value=action.command_value,
                            status="APPLIED",
                            created_at=payload.occurred_at,
                        )
                        db.add(cmd)
                        action.actuator.current_value = action.command_value

            db.commit()
            db.refresh(event)
            return event, True
        except AppException:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            logger.error("Failed to process automation event: %s", exc)
            raise AppException(500, "Failed to process automation event", "Internal Server Error")


class ExecutionController:
    @classmethod
    def list_executions(
        cls,
        db: Session,
        current_user: UserModel,
        rule_id: Optional[int],
        outcome: Optional[ExecutionOutcomeEnum],
        client_event_id: Optional[str],
        limit: int,
        after_id: Optional[int],
    ) -> ExecutionListResponse:
        if after_id is not None and after_id <= 0:
            raise AppException(422, "after_id must be a positive integer", "Unprocessable Entity")

        query = select(ExecutionModel).join(ExecutionModel.rule)

        if current_user.role == UserRole.USER.value:
            query = query.where(RuleModel.owner_id == current_user.id)

        if rule_id is not None:
            if rule_id <= 0:
                raise AppException(404, "rule_id must be a positive integer", "Not Found")
            rule = db.scalar(select(RuleModel).where(RuleModel.id == rule_id))
            if not rule:
                raise AppException(404, f"Rule {rule_id} not found", "Not Found")
            if current_user.role != UserRole.ADMIN.value and rule.owner_id != current_user.id:
                raise AppException(403, "Access denied to executions of rule owned by another user", "Forbidden")
            query = query.where(ExecutionModel.rule_id == rule_id)

        if outcome is not None:
            query = query.where(ExecutionModel.outcome == outcome.value)

        if client_event_id is not None:
            query = query.join(ExecutionModel.event).where(EventModel.client_event_id == client_event_id)

        if after_id is not None:
            query = query.where(ExecutionModel.id > after_id)

        query = query.order_by(ExecutionModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())
        has_more = len(records) > limit
        items = records[:limit]
        next_id = items[-1].id if (has_more and items) else None

        return ExecutionListResponse(
            items=[
                ExecutionResponse(
                    id=e.id,
                    rule_id=e.rule_id,
                    event_id=e.event_id,
                    outcome=ExecutionOutcomeEnum(e.outcome),
                    executed_at=as_utc(e.executed_at),
                )
                for e in items
            ],
            next_after_id=next_id,
        )


# -----------------------------------------------------------------------------
# Pure ASGI Content-Type & Size Enforcement Middleware
# -----------------------------------------------------------------------------
class JsonValidationASGIMiddleware:
    def __init__(self, app_instance, routes):
        self.app = app_instance
        self.routes = routes

    async def __call__(self, scope, receive, send):
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
        max_size = 10 * 1024 * 1024  # 10 MiB limit

        while True:
            message = await receive()
            chunk = message.get("body", b"")
            total_len += len(chunk)
            if total_len > max_size:
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
# Seed Data Initialization
# -----------------------------------------------------------------------------
def seed_database(db: Session):
    Base.metadata.create_all(bind=engine)
    if db.scalar(select(UserModel).limit(1)):
        return

    admin_token = os.getenv("API_ADMIN_TOKEN", "admin-token-secret-12345")
    user1_token = os.getenv("API_USER_TOKEN", "user1-token-secret-12345")
    user2_token = os.getenv("API_SECOND_USER_TOKEN", "user2-token-secret-12345")

    u1 = UserModel(
        id=1,
        username="user1",
        role=UserRole.USER.value,
        token_hash=hash_token(user1_token),
    )
    u2 = UserModel(
        id=2,
        username="user2",
        role=UserRole.USER.value,
        token_hash=hash_token(user2_token),
    )
    u3 = UserModel(
        id=3,
        username="admin",
        role=UserRole.ADMIN.value,
        token_hash=hash_token(admin_token),
    )
    db.add_all([u1, u2, u3])
    db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    with SessionLocal() as db:
        seed_database(db)
    yield


with SessionLocal() as _startup_db:
    seed_database(_startup_db)


# -----------------------------------------------------------------------------
# FastAPI Application & Routes (MVC: View/Routing)
# -----------------------------------------------------------------------------
app = FastAPI(
    title="Smart Home - Automation Rules & Actuators API",
    version="1.0.0",
    description="Deterministic RESTful API for automation rules, event evaluation, cooldown enforcement, and actuator command execution.",
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
    logger.error("Unhandled internal exception: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "status_code": 500, "error_type": "Internal Server Error"},
    )


# -----------------------------------------------------------------------------
# Endpoints (17 Specified Endpoints)
# -----------------------------------------------------------------------------

# REQ-2-011: GET /health
@app.get(
    "/health",
    tags=["Health"],
    summary="Health check probe",
    response_model=HealthResponse,
    responses={
        200: {"description": "Service operational and database connected", "model": HealthResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def get_health(db: Session = Depends(get_db)):
    """The system shall expose GET /health returning deterministic local persistence health."""
    try:
        result = db.scalar(select(1))
        if result != 1:
            raise AppException(503, "Database probe failed", "Service Unavailable")
    except Exception:
        raise AppException(503, "Database connection probe failed", "Service Unavailable")
    return HealthResponse(status="ok", database="connected")


# REQ-2-012: GET /actuators
@app.get(
    "/actuators",
    tags=["Actuators"],
    summary="List actuators",
    response_model=ActuatorListResponse,
    responses={
        200: {"description": "Actuators listed successfully", "model": ActuatorListResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def list_actuators(
    type: Optional[ActuatorTypeEnum] = Query(None),
    serial_prefix: Optional[str] = Query(None),
    owner_id: Optional[int] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /actuators accepting optional type, prefix, and owner_id."""
    return ActuatorController.list_actuators(db, current_user, type, serial_prefix, owner_id, limit, after_id)


# REQ-2-013: POST /actuators
@app.post(
    "/actuators",
    status_code=201,
    tags=["Actuators"],
    summary="Create a new actuator",
    response_model=ActuatorResponse,
    responses={
        201: {"description": "Actuator created successfully", "model": ActuatorResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        409: {"description": "Conflict", "model": ErrorResponse},
        415: {"description": "Unsupported Media Type", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def create_actuator(
    payload: ActuatorCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
    admin_user: UserModel = Depends(AuthController.require_admin),
):
    """The system shall expose POST /actuators as an admin-only endpoint returning 201 with Location."""
    actuator = ActuatorController.create_actuator(db, payload)
    response.headers["Location"] = f"/actuators/{actuator.id}"
    return ActuatorController.serialize_actuator(actuator)


# REQ-2-014: GET /actuators/{actuator_id}
@app.get(
    "/actuators/{actuator_id}",
    tags=["Actuators"],
    summary="Get actuator details",
    response_model=ActuatorResponse,
    responses={
        200: {"description": "Actuator details retrieved successfully", "model": ActuatorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def get_actuator(
    actuator_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /actuators/{actuator_id} returning actuator metadata and current_value."""
    actuator = ActuatorController.get_actuator_by_id(db, current_user, actuator_id)
    return ActuatorController.serialize_actuator(actuator)


# REQ-2-015: PATCH /actuators/{actuator_id}
@app.patch(
    "/actuators/{actuator_id}",
    tags=["Actuators"],
    summary="Update actuator metadata",
    response_model=ActuatorResponse,
    responses={
        200: {"description": "Actuator updated successfully", "model": ActuatorResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        415: {"description": "Unsupported Media Type", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def patch_actuator(
    actuator_id: int,
    payload: ActuatorPatchRequest,
    db: Session = Depends(get_db),
    admin_user: UserModel = Depends(AuthController.require_admin),
):
    """The system shall expose PATCH /actuators/{actuator_id} as an admin-only endpoint."""
    actuator = ActuatorController.patch_actuator(db, actuator_id, payload)
    return ActuatorController.serialize_actuator(actuator)


# REQ-2-016: DELETE /actuators/{actuator_id}
@app.delete(
    "/actuators/{actuator_id}",
    status_code=204,
    tags=["Actuators"],
    summary="Delete an actuator",
    responses={
        204: {"description": "Actuator deleted successfully"},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        409: {"description": "Conflict", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def delete_actuator(
    actuator_id: int,
    db: Session = Depends(get_db),
    admin_user: UserModel = Depends(AuthController.require_admin),
):
    """The system shall expose DELETE /actuators/{actuator_id} deleting unreferenced actuators only."""
    ActuatorController.delete_actuator(db, actuator_id)
    return Response(status_code=204)


# REQ-2-017: POST /actuators/{actuator_id}/commands
@app.post(
    "/actuators/{actuator_id}/commands",
    status_code=201,
    tags=["Commands"],
    summary="Send manual command to actuator",
    response_model=CommandResponse,
    responses={
        201: {"description": "Manual command created successfully", "model": CommandResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        415: {"description": "Unsupported Media Type", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def create_manual_command(
    actuator_id: int,
    payload: ManualCommandCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose POST /actuators/{actuator_id}/commands creating an immutable MANUAL command."""
    cmd = ActuatorController.create_manual_command(db, current_user, actuator_id, payload)
    response.headers["Location"] = f"/actuators/{actuator_id}/commands/{cmd.id}"
    return CommandResponse(
        id=cmd.id,
        actuator_id=cmd.actuator_id,
        origin=CommandOriginEnum(cmd.origin),
        execution_id=cmd.execution_id,
        command_value=cmd.command_value,
        status=cmd.status,
        created_at=as_utc(cmd.created_at),
    )


# REQ-2-018: GET /actuators/{actuator_id}/commands
@app.get(
    "/actuators/{actuator_id}/commands",
    tags=["Commands"],
    summary="List actuator command history",
    response_model=CommandListResponse,
    responses={
        200: {"description": "Commands listed successfully", "model": CommandListResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def list_actuator_commands(
    actuator_id: int,
    origin: Optional[CommandOriginEnum] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /actuators/{actuator_id}/commands with optional origin filtering."""
    return ActuatorController.list_commands(db, current_user, actuator_id, origin, limit, after_id)


# REQ-2-019: GET /automation-rules
@app.get(
    "/automation-rules",
    tags=["Automation Rules"],
    summary="List automation rules",
    response_model=RuleListResponse,
    responses={
        200: {"description": "Rules listed successfully", "model": RuleListResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def list_rules(
    enabled: Optional[bool] = Query(None),
    source_kind: Optional[SourceKindEnum] = Query(None),
    owner_id: Optional[int] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /automation-rules with optional filters and stable pagination."""
    return RuleController.list_rules(db, current_user, enabled, source_kind, owner_id, limit, after_id)


# REQ-2-020: POST /automation-rules
@app.post(
    "/automation-rules",
    status_code=201,
    tags=["Automation Rules"],
    summary="Create a new automation rule",
    response_model=RuleResponse,
    responses={
        201: {"description": "Rule created successfully", "model": RuleResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        409: {"description": "Conflict", "model": ErrorResponse},
        415: {"description": "Unsupported Media Type", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def create_rule(
    payload: RuleCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose POST /automation-rules creating rule and action definitions."""
    rule = RuleController.create_rule(db, current_user, payload)
    response.headers["Location"] = f"/automation-rules/{rule.id}"
    return RuleController.serialize_rule(rule)


# REQ-2-021: GET /automation-rules/{rule_id}
@app.get(
    "/automation-rules/{rule_id}",
    tags=["Automation Rules"],
    summary="Get automation rule details",
    response_model=RuleResponse,
    responses={
        200: {"description": "Rule retrieved successfully", "model": RuleResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def get_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /automation-rules/{rule_id} returning rule and ordered actions."""
    rule = RuleController.get_rule_by_id(db, current_user, rule_id)
    return RuleController.serialize_rule(rule)


# REQ-2-022: PUT /automation-rules/{rule_id}
@app.put(
    "/automation-rules/{rule_id}",
    tags=["Automation Rules"],
    summary="Replace an automation rule",
    response_model=RuleResponse,
    responses={
        200: {"description": "Rule replaced successfully", "model": RuleResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        409: {"description": "Conflict", "model": ErrorResponse},
        415: {"description": "Unsupported Media Type", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def replace_rule(
    rule_id: int,
    payload: RuleReplaceRequest,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose PUT /automation-rules/{rule_id} replacing rule definition and actions."""
    rule = RuleController.replace_rule(db, current_user, rule_id, payload)
    return RuleController.serialize_rule(rule)


# REQ-2-023: DELETE /automation-rules/{rule_id}
@app.delete(
    "/automation-rules/{rule_id}",
    status_code=204,
    tags=["Automation Rules"],
    summary="Delete an unexecuted rule",
    responses={
        204: {"description": "Rule deleted successfully"},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        409: {"description": "Conflict", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def delete_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose DELETE /automation-rules/{rule_id} deleting only an unexecuted rule."""
    RuleController.delete_rule(db, current_user, rule_id)
    return Response(status_code=204)


# REQ-2-024: POST /automation-rules/{rule_id}/enable
@app.post(
    "/automation-rules/{rule_id}/enable",
    tags=["Automation Rules"],
    summary="Enable an automation rule",
    response_model=RuleResponse,
    responses={
        200: {"description": "Rule enabled successfully", "model": RuleResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def enable_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose POST /automation-rules/{rule_id}/enable setting enabled true."""
    rule = RuleController.set_enabled(db, current_user, rule_id, True)
    return RuleController.serialize_rule(rule)


# REQ-2-025: POST /automation-rules/{rule_id}/disable
@app.post(
    "/automation-rules/{rule_id}/disable",
    tags=["Automation Rules"],
    summary="Disable an automation rule",
    response_model=RuleResponse,
    responses={
        200: {"description": "Rule disabled successfully", "model": RuleResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def disable_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose POST /automation-rules/{rule_id}/disable setting enabled false."""
    rule = RuleController.set_enabled(db, current_user, rule_id, False)
    return RuleController.serialize_rule(rule)


# REQ-2-026: POST /automation-events
@app.post(
    "/automation-events",
    tags=["Automation Events"],
    summary="Post automation event for rule evaluation",
    response_model=EventResponse,
    responses={
        200: {"description": "Duplicate identical event replayed", "model": EventResponse},
        201: {"description": "Event created and evaluated", "model": EventResponse},
        400: {"description": "Bad Request", "model": ErrorResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        409: {"description": "Conflict", "model": ErrorResponse},
        415: {"description": "Unsupported Media Type", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def create_automation_event(
    payload: EventCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose POST /automation-events creating an event and evaluating matching rules."""
    event, is_new = AutomationEventController.create_and_evaluate_event(db, current_user, payload)
    if is_new:
        response.status_code = 201
        response.headers["Location"] = f"/automation-events/{event.id}"
    else:
        response.status_code = 200
        response.headers["Location"] = f"/automation-events/{event.id}"
    return EventResponse(
        id=event.id,
        owner_id=event.owner_id,
        client_event_id=event.client_event_id,
        source_kind=SourceKindEnum(event.source_kind),
        value=event.value,
        occurred_at=as_utc(event.occurred_at),
    )


# REQ-2-027: GET /automation-executions
@app.get(
    "/automation-executions",
    tags=["Automation Executions"],
    summary="List automation executions",
    response_model=ExecutionListResponse,
    responses={
        200: {"description": "Executions listed successfully", "model": ExecutionListResponse},
        401: {"description": "Unauthorized", "model": ErrorResponse},
        403: {"description": "Forbidden", "model": ErrorResponse},
        404: {"description": "Not Found", "model": ErrorResponse},
        422: {"description": "Unprocessable Entity", "model": ErrorResponse},
        500: {"description": "Internal Server Error", "model": ErrorResponse},
        503: {"description": "Service Unavailable", "model": ErrorResponse},
    },
)
def list_executions(
    rule_id: Optional[int] = Query(None),
    outcome: Optional[ExecutionOutcomeEnum] = Query(None),
    client_event_id: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    db: Session = Depends(get_db),
    current_user: UserModel = Depends(AuthController.get_current_user),
):
    """The system shall expose GET /automation-executions accepting optional rule_id, outcome, client_event_id."""
    return ExecutionController.list_executions(db, current_user, rule_id, outcome, client_event_id, limit, after_id)
