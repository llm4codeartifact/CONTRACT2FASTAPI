"""
Spostamento Colonne e Aggiornamenti Massivi - Kanban Project Management API
Case ID: 5
Variant: B: Column moves and bulk updates

Technical specifications:
- Python 3.10+, FastAPI, Pydantic v2, SQLAlchemy ORM, SQLite in-memory with StaticPool.
- Pure ASGI middleware for 10 MiB limit (413), JSON media type (415), invalid JSON (400), preserving 405 Method Not Allowed.
- MVC architecture: SQLAlchemy models, Pydantic schemas, static controllers, thin FastAPI routes.
- Seed data: user1 (id=1, user), user2 (id=2, user), admin (id=3, admin), projects, columns, and tickets for reorder tests.
- Standardized error format: {"detail": str, "status_code": int, "error_type": str}.
- GET /health reporting {"status": "ok", "database": "connected"}.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    select,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Session,
    relationship,
    sessionmaker,
)
from sqlalchemy.pool import StaticPool
from starlette.routing import Match
from starlette.types import ASGIApp, Receive, Scope, Send

# -----------------------------------------------------------------------------
# Logging Configuration
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("kanban_spostamento_colonne")

# -----------------------------------------------------------------------------
# Database Setup (SQLite in-memory with StaticPool)
# -----------------------------------------------------------------------------
DATABASE_URL = "sqlite:///:memory:"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


# -----------------------------------------------------------------------------
# Enums
# -----------------------------------------------------------------------------
class RoleEnum(str, Enum):
    USER = "user"
    ADMIN = "admin"


class PlacementEnum(str, Enum):
    TOP = "TOP"
    BOTTOM = "BOTTOM"


# -----------------------------------------------------------------------------
# SQLAlchemy Models
# -----------------------------------------------------------------------------
class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    role = Column(String(20), nullable=False)
    token = Column(String(100), unique=True, nullable=False)


class ProjectModel(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(10), unique=True, nullable=False, index=True)
    name = Column(String(120), nullable=False)
    creator_account_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)

    creator = relationship("UserModel")
    columns = relationship("ColumnModel", back_populates="project")
    tickets = relationship("TicketModel", back_populates="project")


class ColumnModel(Base):
    __tablename__ = "columns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    name = Column(String(80), nullable=False)
    position = Column(Integer, nullable=False, index=True)
    wip_limit = Column(Integer, default=0, nullable=False)
    active = Column(Boolean, default=True, nullable=False)
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)

    project = relationship("ProjectModel", back_populates="columns")
    tickets = relationship("TicketModel", back_populates="column")


class TicketModel(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    column_id = Column(Integer, ForeignKey("columns.id"), nullable=False, index=True)
    title = Column(String(160), nullable=False)
    description = Column(Text, nullable=False, default="")
    position = Column(Integer, nullable=False, index=True)

    project = relationship("ProjectModel", back_populates="tickets")
    column = relationship("ColumnModel", back_populates="tickets")


# -----------------------------------------------------------------------------
# Exceptions & Error Schemas
# -----------------------------------------------------------------------------
class AppException(Exception):
    def __init__(self, status_code: int, detail: str, error_type: str):
        self.status_code = status_code
        self.detail = detail
        self.error_type = error_type
        super().__init__(detail)


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: str
    status_code: int
    error_type: str


# -----------------------------------------------------------------------------
# Pydantic Schemas
# -----------------------------------------------------------------------------
class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str
    database: str


class ProjectCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(..., pattern=r"^[A-Z][A-Z0-9]{1,9}$")
    name: str = Field(..., min_length=3, max_length=120)


class ProjectPatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Optional[str] = Field(None, pattern=r"^[A-Z][A-Z0-9]{1,9}$")
    name: Optional[str] = Field(None, min_length=3, max_length=120)
    active: Optional[bool] = None

    @model_validator(mode="after")
    def check_non_empty(self) -> "ProjectPatchPayload":
        if self.key is None and self.name is None and self.active is None:
            raise ValueError("At least one field must be supplied for patch")
        return self


class ProjectOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    key: str
    name: str
    creator_account_id: int
    active: bool


class ProjectListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[ProjectOut]
    next_after_id: Optional[int] = None


class ColumnCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=2, max_length=80)
    position: int = Field(..., ge=1, le=1000)
    wip_limit: int = Field(default=0, ge=0, le=100)


class ColumnPatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Optional[str] = Field(None, min_length=2, max_length=80)
    position: Optional[int] = Field(None, ge=1, le=1000)
    wip_limit: Optional[int] = Field(None, ge=0, le=100)
    active: Optional[bool] = None

    @model_validator(mode="after")
    def check_non_empty(self) -> "ColumnPatchPayload":
        if self.name is None and self.position is None and self.wip_limit is None and self.active is None:
            raise ValueError("At least one field must be supplied for patch")
        return self


class ColumnOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    project_id: int
    name: str
    position: int
    wip_limit: int
    active: bool


class ColumnListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[ColumnOut]
    next_after_id: Optional[int] = None


class TicketCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int = Field(..., gt=0)
    column_id: int = Field(..., gt=0)
    title: str = Field(..., min_length=3, max_length=160)
    description: Optional[str] = Field(default="", max_length=4000)


class TicketMovePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_column_id: int = Field(..., gt=0)
    after_ticket_id: Optional[int] = Field(None, gt=0)


class BulkMovePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket_ids: List[int] = Field(..., min_length=1, max_length=100)
    target_column_id: int = Field(..., gt=0)
    placement: PlacementEnum

    @model_validator(mode="after")
    def check_distinct_ids(self) -> "BulkMovePayload":
        if len(self.ticket_ids) != len(set(self.ticket_ids)):
            raise ValueError("ticket_ids must contain distinct ticket ids")
        return self


class TicketOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    project_id: int
    column_id: int
    title: str
    description: str
    position: int


class TicketListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[TicketOut]
    next_after_id: Optional[int] = None


# -----------------------------------------------------------------------------
# Database Seed & Dependencies
# -----------------------------------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def seed_database(db: Session):
    Base.metadata.create_all(bind=engine)
    existing_users = db.scalar(select(UserModel).limit(1))
    if not existing_users:
        u1 = UserModel(
            id=1, username="user1", role=RoleEnum.USER.value, token="user1-token-secret-12345"
        )
        u2 = UserModel(
            id=2, username="user2", role=RoleEnum.USER.value, token="user2-token-secret-12345"
        )
        admin = UserModel(
            id=3, username="admin", role=RoleEnum.ADMIN.value, token="admin-token-secret-12345"
        )
        db.add_all([u1, u2, admin])
        db.flush()

        # Seed projects: User1 project and User2 project
        p1 = ProjectModel(
            id=1,
            key="KAN1",
            name="Project One",
            creator_account_id=1,
            active=True,
            is_deleted=False,
        )
        p2 = ProjectModel(
            id=2,
            key="KAN2",
            name="Project Two",
            creator_account_id=2,
            active=True,
            is_deleted=False,
        )
        db.add_all([p1, p2])
        db.flush()

        # Three active ordered columns in project 1
        c1 = ColumnModel(
            id=1, project_id=1, name="To Do", position=1, wip_limit=5, active=True, is_deleted=False
        )
        c2 = ColumnModel(
            id=2, project_id=1, name="Doing", position=2, wip_limit=3, active=True, is_deleted=False
        )
        c3 = ColumnModel(
            id=3, project_id=1, name="Done", position=3, wip_limit=0, active=True, is_deleted=False
        )
        db.add_all([c1, c2, c3])
        db.flush()

        # Tickets in multiple columns for reorder tests
        t1 = TicketModel(id=1, project_id=1, column_id=1, title="Task 1", description="", position=1)
        t2 = TicketModel(id=2, project_id=1, column_id=1, title="Task 2", description="", position=2)
        t3 = TicketModel(id=3, project_id=1, column_id=2, title="Task 3", description="", position=1)
        t4 = TicketModel(id=4, project_id=1, column_id=3, title="Task 4", description="", position=1)
        db.add_all([t1, t2, t3, t4])
        db.commit()


def get_current_user(
    authorization: Optional[str] = Header(None), db: Session = Depends(get_db)
) -> UserModel:
    if not authorization or not authorization.startswith("Bearer "):
        raise AppException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Bearer authorization header",
            error_type="Unauthorized",
        )
    token = authorization.split("Bearer ", 1)[1].strip()
    user = db.scalar(select(UserModel).where(UserModel.token == token))
    if not user:
        raise AppException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session token",
            error_type="Unauthorized",
        )
    return user


def require_admin(current_user: UserModel = Depends(get_current_user)) -> UserModel:
    if current_user.role != RoleEnum.ADMIN.value:
        raise AppException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required for this operation",
            error_type="Forbidden",
        )
    return current_user


# -----------------------------------------------------------------------------
# Pure ASGI Middleware
# -----------------------------------------------------------------------------
MAX_BODY_SIZE = 10 * 1024 * 1024


class JsonValidationASGIMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"].upper()
        path = scope["path"]

        if path == "/health":
            await self.app(scope, receive, send)
            return

        if method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        fastapi_app: FastAPI = scope.get("app")
        if fastapi_app and hasattr(fastapi_app, "router"):
            has_matching_path = False
            is_method_allowed = False
            for route in fastapi_app.router.routes:
                match, _ = route.matches(scope)
                if match == Match.FULL:
                    has_matching_path = True
                    is_method_allowed = True
                    break
                elif match == Match.PARTIAL:
                    has_matching_path = True

            if has_matching_path and not is_method_allowed:
                await self.app(scope, receive, send)
                return

        headers_dict = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope["headers"]}
        content_length_str = headers_dict.get("content-length")
        content_type = headers_dict.get("content-type", "")

        if content_length_str:
            try:
                cl = int(content_length_str)
                if cl > MAX_BODY_SIZE:
                    await self._send_json_error(
                        send,
                        413,
                        f"Request payload exceeds {MAX_BODY_SIZE} bytes limit",
                        "Payload Too Large",
                    )
                    return
            except ValueError:
                pass

        if method in ("POST", "PATCH", "PUT"):
            if not content_type.startswith("application/json"):
                await self._send_json_error(
                    send, 415, "Content-Type must be application/json", "Unsupported Media Type"
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

            if body:
                try:
                    json.loads(body.decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as err:
                    await self._send_json_error(
                        send, 400, f"Malformed JSON body: {str(err)}", "Bad Request"
                    )
                    return

            async def custom_receive():
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            await self.app(scope, custom_receive, send)
            return

        await self.app(scope, receive, send)

    async def _send_json_error(self, send: Send, status_code: int, detail: str, error_type: str):
        payload = json.dumps(
            {"detail": detail, "status_code": status_code, "error_type": error_type}
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("latin1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload, "more_body": False})


# -----------------------------------------------------------------------------
# Static Controllers
# -----------------------------------------------------------------------------
class HealthController:
    @staticmethod
    def get_health(db: Session) -> HealthResponse:
        try:
            db.execute(text("SELECT 1")).scalar()
            return HealthResponse(status="ok", database="connected")
        except Exception as e:
            logger.error(f"Health probe failed: {e}")
            raise AppException(503, "Local persistence probe failed", "Service Unavailable")


class ProjectController:
    @staticmethod
    def list_projects(
        db: Session,
        current_user: UserModel,
        active: Optional[bool],
        key_prefix: Optional[str],
        limit: int,
        after_id: Optional[int],
    ) -> ProjectListResponse:
        query = select(ProjectModel).where(ProjectModel.is_deleted == False)

        if current_user.role != RoleEnum.ADMIN.value:
            query = query.where(ProjectModel.creator_account_id == current_user.id)

        if active is not None:
            query = query.where(ProjectModel.active == active)

        if key_prefix:
            query = query.where(ProjectModel.key.startswith(key_prefix))

        if after_id is not None and after_id > 0:
            query = query.where(ProjectModel.id > after_id)

        query = query.order_by(ProjectModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())

        next_after_id = None
        if len(records) > limit:
            next_after_id = records[limit - 1].id
            records = records[:limit]

        items = [
            ProjectOut(
                id=p.id,
                key=p.key,
                name=p.name,
                creator_account_id=p.creator_account_id,
                active=p.active,
            )
            for p in records
        ]
        return ProjectListResponse(items=items, next_after_id=next_after_id)

    @staticmethod
    def create_project(
        db: Session, current_user: UserModel, payload: ProjectCreatePayload
    ) -> ProjectModel:
        existing = db.scalar(
            select(ProjectModel).where(ProjectModel.key == payload.key, ProjectModel.is_deleted == False)
        )
        if existing:
            raise AppException(409, f"Project key '{payload.key}' already exists", "Conflict")

        project = ProjectModel(
            key=payload.key,
            name=payload.name.strip(),
            creator_account_id=current_user.id,
            active=True,
            is_deleted=False,
        )
        db.add(project)
        db.commit()
        db.refresh(project)
        return project

    @staticmethod
    def get_project(db: Session, current_user: UserModel, project_id: int) -> ProjectModel:
        if project_id <= 0:
            raise AppException(404, "project_id must be a positive integer", "Not Found")

        project = db.scalar(
            select(ProjectModel).where(ProjectModel.id == project_id, ProjectModel.is_deleted == False)
        )
        if not project:
            raise AppException(404, f"Project {project_id} not found", "Not Found")

        if current_user.role != RoleEnum.ADMIN.value and project.creator_account_id != current_user.id:
            raise AppException(403, "Access denied to project owned by another user", "Forbidden")

        return project

    @staticmethod
    def patch_project(
        db: Session, current_user: UserModel, project_id: int, payload: ProjectPatchPayload
    ) -> ProjectModel:
        project = ProjectController.get_project(db, current_user, project_id)

        if payload.key is not None and payload.key != project.key:
            dup = db.scalar(
                select(ProjectModel).where(
                    ProjectModel.key == payload.key,
                    ProjectModel.id != project_id,
                    ProjectModel.is_deleted == False,
                )
            )
            if dup:
                raise AppException(409, f"Project key '{payload.key}' already exists", "Conflict")
            project.key = payload.key

        if payload.name is not None:
            project.name = payload.name.strip()

        if payload.active is False and project.active is True:
            # Check for active columns or tickets
            has_cols = db.scalar(
                select(ColumnModel)
                .where(
                    ColumnModel.project_id == project_id,
                    ColumnModel.active == True,
                    ColumnModel.is_deleted == False,
                )
                .limit(1)
            )
            has_tickets = db.scalar(select(TicketModel).where(TicketModel.project_id == project_id).limit(1))
            if has_cols or has_tickets:
                raise AppException(
                    409, "Cannot deactivate project with active columns or tickets", "Conflict"
                )
            project.active = False
        elif payload.active is True:
            project.active = True

        db.commit()
        db.refresh(project)
        return project

    @staticmethod
    def delete_project(db: Session, current_user: UserModel, project_id: int):
        project = ProjectController.get_project(db, current_user, project_id)

        # REQ-5-008: A project can be soft-deleted only when it has no active columns and no tickets
        has_cols = db.scalar(
            select(ColumnModel)
            .where(
                ColumnModel.project_id == project_id,
                ColumnModel.active == True,
                ColumnModel.is_deleted == False,
            )
            .limit(1)
        )
        has_tickets = db.scalar(select(TicketModel).where(TicketModel.project_id == project_id).limit(1))
        if has_cols or has_tickets:
            raise AppException(
                409, "Cannot delete project with active columns or tickets remaining", "Conflict"
            )

        project.is_deleted = True
        db.commit()


class ColumnController:
    @staticmethod
    def list_columns(
        db: Session,
        current_user: UserModel,
        project_id: int,
        active: Optional[bool],
        limit: int,
        after_id: Optional[int],
    ) -> ColumnListResponse:
        ProjectController.get_project(db, current_user, project_id)

        query = select(ColumnModel).where(
            ColumnModel.project_id == project_id, ColumnModel.is_deleted == False
        )

        if active is not None:
            query = query.where(ColumnModel.active == active)
        if after_id is not None and after_id > 0:
            query = query.where(ColumnModel.id > after_id)

        query = query.order_by(ColumnModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())

        next_after_id = None
        if len(records) > limit:
            next_after_id = records[limit - 1].id
            records = records[:limit]

        items = [
            ColumnOut(
                id=c.id,
                project_id=c.project_id,
                name=c.name,
                position=c.position,
                wip_limit=c.wip_limit,
                active=c.active,
            )
            for c in records
        ]
        return ColumnListResponse(items=items, next_after_id=next_after_id)

    @staticmethod
    def create_column(
        db: Session, current_user: UserModel, project_id: int, payload: ColumnCreatePayload
    ) -> ColumnModel:
        project = ProjectController.get_project(db, current_user, project_id)
        if not project.active:
            raise AppException(409, f"Project {project_id} is inactive", "Conflict")

        trimmed_name = payload.name.strip()

        # REQ-5-003: Active column positions and names are unique within a project
        dup_name = db.scalar(
            select(ColumnModel).where(
                ColumnModel.project_id == project_id,
                ColumnModel.name == trimmed_name,
                ColumnModel.active == True,
                ColumnModel.is_deleted == False,
            )
        )
        if dup_name:
            raise AppException(409, f"Column name '{trimmed_name}' already exists in project", "Conflict")

        # Get existing active columns ordered by position
        existing_cols = list(
            db.scalars(
                select(ColumnModel)
                .where(
                    ColumnModel.project_id == project_id,
                    ColumnModel.active == True,
                    ColumnModel.is_deleted == False,
                )
                .order_by(ColumnModel.position.asc())
            ).all()
        )

        max_pos = len(existing_cols) + 1
        if payload.position < 1 or payload.position > max_pos:
            raise AppException(
                409,
                f"Invalid insertion position {payload.position}: must be between 1 and {max_pos}",
                "Conflict",
            )

        # Shift existing columns at or after payload.position
        for c in existing_cols:
            if c.position >= payload.position:
                c.position += 1

        col = ColumnModel(
            project_id=project_id,
            name=trimmed_name,
            position=payload.position,
            wip_limit=payload.wip_limit,
            active=True,
            is_deleted=False,
        )
        db.add(col)
        db.commit()
        db.refresh(col)
        return col

    @staticmethod
    def get_column(db: Session, current_user: UserModel, column_id: int) -> ColumnModel:
        if column_id <= 0:
            raise AppException(404, "column_id must be a positive integer", "Not Found")

        col = db.scalar(
            select(ColumnModel).where(ColumnModel.id == column_id, ColumnModel.is_deleted == False)
        )
        if not col:
            raise AppException(404, f"Column {column_id} not found", "Not Found")

        ProjectController.get_project(db, current_user, col.project_id)
        return col

    @staticmethod
    def patch_column(
        db: Session, current_user: UserModel, column_id: int, payload: ColumnPatchPayload
    ) -> ColumnModel:
        col = ColumnController.get_column(db, current_user, column_id)

        if payload.name is not None and payload.name.strip() != col.name:
            trimmed = payload.name.strip()
            dup = db.scalar(
                select(ColumnModel).where(
                    ColumnModel.project_id == col.project_id,
                    ColumnModel.name == trimmed,
                    ColumnModel.id != column_id,
                    ColumnModel.active == True,
                    ColumnModel.is_deleted == False,
                )
            )
            if dup:
                raise AppException(409, f"Column name '{trimmed}' already exists", "Conflict")
            col.name = trimmed

        if payload.wip_limit is not None and payload.wip_limit != col.wip_limit:
            if payload.wip_limit > 0:
                ticket_count = len(col.tickets)
                if ticket_count > payload.wip_limit:
                    raise AppException(
                        409,
                        f"Cannot set wip_limit to {payload.wip_limit}: column contains {ticket_count} tickets",
                        "Conflict",
                    )
            col.wip_limit = payload.wip_limit

        if payload.active is not None:
            col.active = payload.active

        if payload.position is not None and payload.position != col.position:
            # Reorder positions
            cols = list(
                db.scalars(
                    select(ColumnModel)
                    .where(
                        ColumnModel.project_id == col.project_id,
                        ColumnModel.is_deleted == False,
                        ColumnModel.id != column_id,
                    )
                    .order_by(ColumnModel.position.asc())
                ).all()
            )
            max_pos = len(cols) + 1
            if payload.position < 1 or payload.position > max_pos:
                raise AppException(
                    409,
                    f"Invalid position {payload.position}: must be between 1 and {max_pos}",
                    "Conflict",
                )
            cols.insert(payload.position - 1, col)
            for idx, c in enumerate(cols, start=1):
                c.position = idx

        db.commit()
        db.refresh(col)
        return col

    @staticmethod
    def delete_column(db: Session, current_user: UserModel, column_id: int):
        col = ColumnController.get_column(db, current_user, column_id)

        # REQ-5-008: A column can be soft-deleted only when it contains no tickets
        has_tickets = db.scalar(select(TicketModel).where(TicketModel.column_id == column_id).limit(1))
        if has_tickets:
            raise AppException(409, "Cannot delete column containing tickets", "Conflict")

        col.is_deleted = True
        db.flush()

        # Renumber remaining active columns
        remaining_cols = list(
            db.scalars(
                select(ColumnModel)
                .where(
                    ColumnModel.project_id == col.project_id,
                    ColumnModel.is_deleted == False,
                )
                .order_by(ColumnModel.position.asc())
            ).all()
        )
        for idx, c in enumerate(remaining_cols, start=1):
            c.position = idx

        db.commit()


class TicketController:
    @staticmethod
    def list_tickets(
        db: Session,
        current_user: UserModel,
        project_id: Optional[int],
        column_id: Optional[int],
        title_prefix: Optional[str],
        limit: int,
        after_id: Optional[int],
    ) -> TicketListResponse:
        query = select(TicketModel)

        if project_id is not None:
            if project_id <= 0:
                raise AppException(404, "project_id must be a positive integer", "Not Found")
            ProjectController.get_project(db, current_user, project_id)
            query = query.where(TicketModel.project_id == project_id)
        else:
            if current_user.role != RoleEnum.ADMIN.value:
                query = query.join(ProjectModel).where(
                    ProjectModel.creator_account_id == current_user.id,
                    ProjectModel.is_deleted == False,
                )

        if column_id is not None:
            if column_id <= 0:
                raise AppException(404, "column_id must be a positive integer", "Not Found")
            col = db.scalar(
                select(ColumnModel).where(ColumnModel.id == column_id, ColumnModel.is_deleted == False)
            )
            if not col:
                raise AppException(404, f"Column {column_id} not found", "Not Found")
            ProjectController.get_project(db, current_user, col.project_id)
            query = query.where(TicketModel.column_id == column_id)

        if title_prefix:
            query = query.where(TicketModel.title.startswith(title_prefix))

        if after_id is not None and after_id > 0:
            query = query.where(TicketModel.id > after_id)

        query = query.order_by(TicketModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())

        next_after_id = None
        if len(records) > limit:
            next_after_id = records[limit - 1].id
            records = records[:limit]

        items = [
            TicketOut(
                id=t.id,
                project_id=t.project_id,
                column_id=t.column_id,
                title=t.title,
                description=t.description,
                position=t.position,
            )
            for t in records
        ]
        return TicketListResponse(items=items, next_after_id=next_after_id)

    @staticmethod
    def create_ticket(
        db: Session, current_user: UserModel, payload: TicketCreatePayload
    ) -> TicketModel:
        project = ProjectController.get_project(db, current_user, payload.project_id)
        if not project.active:
            raise AppException(409, f"Project {payload.project_id} is inactive", "Conflict")

        col = ColumnController.get_column(db, current_user, payload.column_id)
        if not col.active:
            raise AppException(409, f"Column {payload.column_id} is inactive", "Conflict")

        if col.project_id != payload.project_id:
            raise AppException(409, "Column does not belong to the specified project", "Conflict")

        # WIP check
        current_count = len(col.tickets)
        if col.wip_limit > 0 and current_count >= col.wip_limit:
            raise AppException(
                409, f"Column {col.id} has reached its WIP limit of {col.wip_limit}", "Conflict"
            )

        ticket = TicketModel(
            project_id=payload.project_id,
            column_id=payload.column_id,
            title=payload.title.strip(),
            description=payload.description.strip() if payload.description else "",
            position=current_count + 1,
        )
        db.add(ticket)
        db.commit()
        db.refresh(ticket)
        return ticket

    @staticmethod
    def get_ticket(db: Session, current_user: UserModel, ticket_id: int) -> TicketModel:
        if ticket_id <= 0:
            raise AppException(404, "ticket_id must be a positive integer", "Not Found")

        ticket = db.scalar(select(TicketModel).where(TicketModel.id == ticket_id))
        if not ticket:
            raise AppException(404, f"Ticket {ticket_id} not found", "Not Found")

        ProjectController.get_project(db, current_user, ticket.project_id)
        return ticket

    @staticmethod
    def move_ticket(
        db: Session, current_user: UserModel, ticket_id: int, payload: TicketMovePayload
    ) -> TicketModel:
        ticket = TicketController.get_ticket(db, current_user, ticket_id)
        target_col = ColumnController.get_column(db, current_user, payload.target_column_id)

        if target_col.project_id != ticket.project_id:
            raise AppException(409, "Target column belongs to a different project", "Conflict")

        # If moving to another column, check target WIP limit
        if target_col.id != ticket.column_id:
            if target_col.wip_limit > 0 and len(target_col.tickets) >= target_col.wip_limit:
                raise AppException(
                    409, f"Target column has reached its WIP limit of {target_col.wip_limit}", "Conflict"
                )

        # Target tickets excluding moving ticket
        target_tickets = list(
            db.scalars(
                select(TicketModel)
                .where(
                    TicketModel.column_id == target_col.id,
                    TicketModel.id != ticket_id,
                )
                .order_by(TicketModel.position.asc())
            ).all()
        )

        insert_index = 0
        if payload.after_ticket_id is not None:
            found_idx = None
            for idx, t in enumerate(target_tickets):
                if t.id == payload.after_ticket_id:
                    found_idx = idx
                    break
            if found_idx is None:
                raise AppException(
                    409,
                    f"after_ticket_id {payload.after_ticket_id} not found in target column",
                    "Conflict",
                )
            insert_index = found_idx + 1

        old_column_id = ticket.column_id
        ticket.column_id = target_col.id

        target_tickets.insert(insert_index, ticket)
        for idx, t in enumerate(target_tickets, start=1):
            t.position = idx

        # If moved between columns, renumber old column
        if old_column_id != target_col.id:
            old_col_tickets = list(
                db.scalars(
                    select(TicketModel)
                    .where(
                        TicketModel.column_id == old_column_id,
                        TicketModel.id != ticket_id,
                    )
                    .order_by(TicketModel.position.asc())
                ).all()
            )
            for idx, t in enumerate(old_col_tickets, start=1):
                t.position = idx

        db.commit()
        db.refresh(ticket)
        return ticket

    @staticmethod
    def bulk_move_tickets(
        db: Session, current_user: UserModel, project_id: int, payload: BulkMovePayload
    ) -> List[TicketModel]:
        ProjectController.get_project(db, current_user, project_id)
        target_col = ColumnController.get_column(db, current_user, payload.target_column_id)

        if target_col.project_id != project_id:
            raise AppException(409, "Target column belongs to a different project", "Conflict")

        tickets_map = {}
        for tid in payload.ticket_ids:
            t = db.scalar(select(TicketModel).where(TicketModel.id == tid))
            if not t:
                raise AppException(404, f"Ticket {tid} not found", "Not Found")
            if t.project_id != project_id:
                raise AppException(409, f"Ticket {tid} does not belong to project {project_id}", "Conflict")
            tickets_map[tid] = t

        ordered_requested_tickets = [tickets_map[tid] for tid in payload.ticket_ids]

        # Calculate new items joining target column from external columns
        incoming_count = sum(1 for t in ordered_requested_tickets if t.column_id != target_col.id)
        current_target_count = len(target_col.tickets)

        if target_col.wip_limit > 0 and (current_target_count + incoming_count) > target_col.wip_limit:
            raise AppException(
                409,
                f"Bulk move would exceed target column WIP limit ({current_target_count + incoming_count} > {target_col.wip_limit})",
                "Conflict",
            )

        requested_ids_set = set(payload.ticket_ids)
        existing_target_remaining = list(
            db.scalars(
                select(TicketModel)
                .where(
                    TicketModel.column_id == target_col.id,
                    ~TicketModel.id.in_(requested_ids_set),
                )
                .order_by(TicketModel.position.asc())
            ).all()
        )

        source_col_ids = {t.column_id for t in ordered_requested_tickets if t.column_id != target_col.id}

        for t in ordered_requested_tickets:
            t.column_id = target_col.id

        if payload.placement == PlacementEnum.TOP:
            final_target_order = ordered_requested_tickets + existing_target_remaining
        else:
            final_target_order = existing_target_remaining + ordered_requested_tickets

        for idx, t in enumerate(final_target_order, start=1):
            t.position = idx

        # Renumber affected source columns
        for sc_id in source_col_ids:
            sc_tickets = list(
                db.scalars(
                    select(TicketModel)
                    .where(
                        TicketModel.column_id == sc_id,
                        ~TicketModel.id.in_(requested_ids_set),
                    )
                    .order_by(TicketModel.position.asc())
                ).all()
            )
            for idx, t in enumerate(sc_tickets, start=1):
                t.position = idx

        db.commit()
        return ordered_requested_tickets


# -----------------------------------------------------------------------------
# FastAPI App Initialization & Routes
# -----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    with SessionLocal() as db:
        seed_database(db)
    yield


app = FastAPI(
    title="Kanban Project Management - Column Moves and Bulk Updates API",
    version="1.0.0",
    description="RESTful API for column ordering, WIP limit enforcement, ticket moves, and atomic bulk moves.",
    lifespan=lifespan,
)


@app.exception_handler(AppException)
async def app_exception_handler(request: Request, exc: AppException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "status_code": exc.status_code, "error_type": exc.error_type},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": str(exc),
            "status_code": status.HTTP_422_UNPROCESSABLE_ENTITY,
            "error_type": "Unprocessable Entity",
        },
    )


# Attach ASGI Middleware
app.add_middleware(JsonValidationASGIMiddleware)

# Immediate database init for TestClient
with SessionLocal() as _startup_db:
    seed_database(_startup_db)


# -----------------------------------------------------------------------------
# Endpoints
# -----------------------------------------------------------------------------
@app.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Health check probe",
    tags=["Health"],
    responses={503: {"description": "Service unavailable"}},
)
def get_health(db: Session = Depends(get_db)):
    return HealthController.get_health(db)


@app.get(
    "/projects",
    response_model=ProjectListResponse,
    status_code=status.HTTP_200_OK,
    summary="List projects",
    tags=["Projects"],
)
def list_projects(
    active: Optional[bool] = Query(None, description="Filter active status"),
    key_prefix: Optional[str] = Query(None, pattern=r"^[A-Z0-9]{1,10}$", description="Filter key prefix"),
    limit: int = Query(20, ge=1, le=100, description="Pagination limit"),
    after_id: Optional[int] = Query(None, gt=0, description="Pagination cursor"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ProjectController.list_projects(db, current_user, active, key_prefix, limit, after_id)


@app.post(
    "/projects",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create project",
    tags=["Projects"],
)
def create_project(
    payload: ProjectCreatePayload,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    proj = ProjectController.create_project(db, current_user, payload)
    response.headers["Location"] = f"/projects/{proj.id}"
    return ProjectOut(
        id=proj.id,
        key=proj.key,
        name=proj.name,
        creator_account_id=proj.creator_account_id,
        active=proj.active,
    )


@app.get(
    "/projects/{project_id}",
    response_model=ProjectOut,
    status_code=status.HTTP_200_OK,
    summary="Get project details",
    tags=["Projects"],
)
def get_project(
    project_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    proj = ProjectController.get_project(db, current_user, project_id)
    return ProjectOut(
        id=proj.id,
        key=proj.key,
        name=proj.name,
        creator_account_id=proj.creator_account_id,
        active=proj.active,
    )


@app.patch(
    "/projects/{project_id}",
    response_model=ProjectOut,
    status_code=status.HTTP_200_OK,
    summary="Patch project",
    tags=["Projects"],
)
def patch_project(
    project_id: int,
    payload: ProjectPatchPayload,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    proj = ProjectController.patch_project(db, current_user, project_id, payload)
    return ProjectOut(
        id=proj.id,
        key=proj.key,
        name=proj.name,
        creator_account_id=proj.creator_account_id,
        active=proj.active,
    )


@app.delete(
    "/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete project",
    tags=["Projects"],
)
def delete_project(
    project_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ProjectController.delete_project(db, current_user, project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
    "/projects/{project_id}/columns",
    response_model=ColumnListResponse,
    status_code=status.HTTP_200_OK,
    summary="List project columns",
    tags=["Columns"],
)
def list_columns(
    project_id: int,
    active: Optional[bool] = Query(None, description="Filter active status"),
    limit: int = Query(20, ge=1, le=100, description="Pagination limit"),
    after_id: Optional[int] = Query(None, gt=0, description="Pagination cursor"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ColumnController.list_columns(db, current_user, project_id, active, limit, after_id)


@app.post(
    "/projects/{project_id}/columns",
    response_model=ColumnOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create project column",
    tags=["Columns"],
)
def create_column(
    project_id: int,
    payload: ColumnCreatePayload,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    col = ColumnController.create_column(db, current_user, project_id, payload)
    response.headers["Location"] = f"/columns/{col.id}"
    return ColumnOut(
        id=col.id,
        project_id=col.project_id,
        name=col.name,
        position=col.position,
        wip_limit=col.wip_limit,
        active=col.active,
    )


@app.get(
    "/columns/{column_id}",
    response_model=ColumnOut,
    status_code=status.HTTP_200_OK,
    summary="Get column by id",
    tags=["Columns"],
)
def get_column(
    column_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    col = ColumnController.get_column(db, current_user, column_id)
    return ColumnOut(
        id=col.id,
        project_id=col.project_id,
        name=col.name,
        position=col.position,
        wip_limit=col.wip_limit,
        active=col.active,
    )


@app.patch(
    "/columns/{column_id}",
    response_model=ColumnOut,
    status_code=status.HTTP_200_OK,
    summary="Patch column",
    tags=["Columns"],
)
def patch_column(
    column_id: int,
    payload: ColumnPatchPayload,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    col = ColumnController.patch_column(db, current_user, column_id, payload)
    return ColumnOut(
        id=col.id,
        project_id=col.project_id,
        name=col.name,
        position=col.position,
        wip_limit=col.wip_limit,
        active=col.active,
    )


@app.delete(
    "/columns/{column_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete column",
    tags=["Columns"],
)
def delete_column(
    column_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ColumnController.delete_column(db, current_user, column_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
    "/tickets",
    response_model=TicketListResponse,
    status_code=status.HTTP_200_OK,
    summary="List tickets",
    tags=["Tickets"],
)
def list_tickets(
    project_id: Optional[int] = Query(None, description="Optional project filter"),
    column_id: Optional[int] = Query(None, description="Optional column filter"),
    title_prefix: Optional[str] = Query(None, description="Optional title prefix filter"),
    limit: int = Query(20, ge=1, le=100, description="Pagination limit"),
    after_id: Optional[int] = Query(None, gt=0, description="Pagination cursor"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return TicketController.list_tickets(
        db, current_user, project_id, column_id, title_prefix, limit, after_id
    )


@app.post(
    "/tickets",
    response_model=TicketOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create ticket at column bottom",
    tags=["Tickets"],
)
def create_ticket(
    payload: TicketCreatePayload,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = TicketController.create_ticket(db, current_user, payload)
    response.headers["Location"] = f"/tickets/{ticket.id}"
    return TicketOut(
        id=ticket.id,
        project_id=ticket.project_id,
        column_id=ticket.column_id,
        title=ticket.title,
        description=ticket.description,
        position=ticket.position,
    )


@app.get(
    "/tickets/{ticket_id}",
    response_model=TicketOut,
    status_code=status.HTTP_200_OK,
    summary="Get ticket by id",
    tags=["Tickets"],
)
def get_ticket(
    ticket_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = TicketController.get_ticket(db, current_user, ticket_id)
    return TicketOut(
        id=ticket.id,
        project_id=ticket.project_id,
        column_id=ticket.column_id,
        title=ticket.title,
        description=ticket.description,
        position=ticket.position,
    )


@app.post(
    "/tickets/{ticket_id}/move",
    response_model=TicketOut,
    status_code=status.HTTP_200_OK,
    summary="Move ticket atomically",
    tags=["Tickets"],
)
def move_ticket(
    ticket_id: int,
    payload: TicketMovePayload,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = TicketController.move_ticket(db, current_user, ticket_id, payload)
    return TicketOut(
        id=ticket.id,
        project_id=ticket.project_id,
        column_id=ticket.column_id,
        title=ticket.title,
        description=ticket.description,
        position=ticket.position,
    )


@app.post(
    "/projects/{project_id}/tickets/bulk-move",
    response_model=List[TicketOut],
    status_code=status.HTTP_200_OK,
    summary="Bulk move tickets to TOP or BOTTOM",
    tags=["Tickets"],
)
def bulk_move_tickets(
    project_id: int,
    payload: BulkMovePayload,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tickets = TicketController.bulk_move_tickets(db, current_user, project_id, payload)
    return [
        TicketOut(
            id=t.id,
            project_id=t.project_id,
            column_id=t.column_id,
            title=t.title,
            description=t.description,
            position=t.position,
        )
        for t in tickets
    ]
