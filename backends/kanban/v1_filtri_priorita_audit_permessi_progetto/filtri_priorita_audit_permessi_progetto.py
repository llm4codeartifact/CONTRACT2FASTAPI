"""
Filtri, Priorita, Audit e Permessi di Progetto - Kanban Project Management API
Case ID: 5
Variant: C: Filters, priorities, audit, and project permissions

Technical specifications:
- Python 3.10+, FastAPI, Pydantic v2, SQLAlchemy ORM, SQLite in-memory with StaticPool.
- Pure ASGI middleware for 10 MiB limit (413), JSON media type (415), invalid JSON (400), preserving 405 Method Not Allowed.
- MVC architecture: SQLAlchemy models, Pydantic schemas, static controllers, thin FastAPI routes.
- Seed data: user1 (id=1, user), user2 (id=2, user), admin (id=3, admin), active project with memberships and tickets.
- Standardized error format: {"detail": str, "status_code": int, "error_type": str}.
- GET /health reporting {"status": "ok", "database": "connected"}.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
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
logger = logging.getLogger("kanban_filtri_priorita")

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


class MembershipRoleEnum(str, Enum):
    VIEWER = "VIEWER"
    CONTRIBUTOR = "CONTRIBUTOR"
    MANAGER = "MANAGER"


class TicketStatusEnum(str, Enum):
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class PriorityEnum(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class AuditEventTypeEnum(str, Enum):
    PROJECT_CREATED = "PROJECT_CREATED"
    PROJECT_UPDATED = "PROJECT_UPDATED"
    PROJECT_DELETED = "PROJECT_DELETED"
    MEMBERSHIP_CREATED = "MEMBERSHIP_CREATED"
    MEMBERSHIP_UPDATED = "MEMBERSHIP_UPDATED"
    MEMBERSHIP_REMOVED = "MEMBERSHIP_REMOVED"
    TICKET_CREATED = "TICKET_CREATED"
    TICKET_UPDATED = "TICKET_UPDATED"


PRIORITY_WEIGHT = {
    PriorityEnum.P0.value: 0,
    PriorityEnum.P1.value: 1,
    PriorityEnum.P2.value: 2,
    PriorityEnum.P3.value: 3,
    PriorityEnum.P4.value: 4,
}


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
    description = Column(String(1000), nullable=False, default="")
    active = Column(Boolean, default=True, nullable=False)
    creator_account_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)

    creator = relationship("UserModel")
    memberships = relationship("MembershipModel", back_populates="project")
    tickets = relationship("TicketModel", back_populates="project")
    audit_events = relationship("AuditEventModel", back_populates="project")


class MembershipModel(Base):
    __tablename__ = "memberships"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String(20), nullable=False)
    active = Column(Boolean, default=True, nullable=False)

    project = relationship("ProjectModel", back_populates="memberships")
    account = relationship("UserModel")
    assigned_tickets = relationship("TicketModel", back_populates="assignee_membership")


class TicketModel(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    title = Column(String(160), nullable=False)
    description = Column(Text, nullable=False, default="")
    status = Column(String(20), nullable=False, default=TicketStatusEnum.TODO.value, index=True)
    priority = Column(String(5), nullable=False, default=PriorityEnum.P2.value, index=True)
    priority_order = Column(Integer, nullable=False, default=2, index=True)
    creator_account_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    assignee_membership_id = Column(Integer, ForeignKey("memberships.id"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)

    project = relationship("ProjectModel", back_populates="tickets")
    creator = relationship("UserModel")
    assignee_membership = relationship("MembershipModel", back_populates="assigned_tickets")


class AuditEventModel(Base):
    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor_account_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    event_type = Column(String(30), nullable=False, index=True)
    subject_type = Column(String(20), nullable=False)
    subject_id = Column(Integer, nullable=False, index=True)
    changed_fields_json = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    project = relationship("ProjectModel", back_populates="audit_events")
    actor = relationship("UserModel")


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
    description: Optional[str] = Field(default="", max_length=1000)


class ProjectPatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: Optional[str] = Field(None, pattern=r"^[A-Z][A-Z0-9]{1,9}$")
    name: Optional[str] = Field(None, min_length=3, max_length=120)
    description: Optional[str] = Field(None, max_length=1000)
    active: Optional[bool] = None

    @model_validator(mode="after")
    def check_non_empty(self) -> "ProjectPatchPayload":
        if self.key is None and self.name is None and self.description is None and self.active is None:
            raise ValueError("At least one field must be supplied for patch")
        return self


class ProjectOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    key: str
    name: str
    description: str
    active: bool
    creator_account_id: int


class ProjectListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[ProjectOut]
    next_after_id: Optional[int] = None


class MembershipCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: int = Field(..., gt=0)
    role: MembershipRoleEnum


class MembershipPatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Optional[MembershipRoleEnum] = None
    active: Optional[bool] = None

    @model_validator(mode="after")
    def check_non_empty(self) -> "MembershipPatchPayload":
        if self.role is None and self.active is None:
            raise ValueError("At least one field must be supplied for patch")
        return self


class MembershipOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    project_id: int
    account_id: int
    role: MembershipRoleEnum
    active: bool


class MembershipListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[MembershipOut]
    next_after_id: Optional[int] = None


class TicketCreatePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: int = Field(..., gt=0)
    title: str = Field(..., min_length=3, max_length=160)
    description: Optional[str] = Field(default="", max_length=4000)
    status: Optional[TicketStatusEnum] = TicketStatusEnum.TODO
    priority: PriorityEnum
    assignee_membership_id: Optional[int] = Field(None, gt=0)


class TicketPatchPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: Optional[str] = Field(None, min_length=3, max_length=160)
    description: Optional[str] = Field(None, max_length=4000)
    status: Optional[TicketStatusEnum] = None
    priority: Optional[PriorityEnum] = None
    assignee_membership_id: Optional[int] = Field(None, gt=0)

    @model_validator(mode="after")
    def check_non_empty(self) -> "TicketPatchPayload":
        if (
            self.title is None
            and self.description is None
            and self.status is None
            and self.priority is None
            and self.assignee_membership_id is None
        ):
            raise ValueError("At least one field must be supplied for patch")
        return self


class TicketOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    project_id: int
    title: str
    description: str
    status: TicketStatusEnum
    priority: PriorityEnum
    creator_account_id: int
    assignee_membership_id: Optional[int] = None
    created_at: datetime


class TicketListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[TicketOut]
    next_after_id: Optional[int] = None
    next_after_priority: Optional[PriorityEnum] = None


class AuditEventOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    actor_account_id: int
    project_id: int
    event_type: AuditEventTypeEnum
    subject_type: str
    subject_id: int
    changed_fields: List[str]
    created_at: datetime


class AuditEventListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[AuditEventOut]
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


def append_audit_event(
    db: Session,
    actor_id: int,
    project_id: int,
    event_type: AuditEventTypeEnum,
    subject_type: str,
    subject_id: int,
    changed_fields: List[str],
):
    ev = AuditEventModel(
        actor_account_id=actor_id,
        project_id=project_id,
        event_type=event_type.value,
        subject_type=subject_type,
        subject_id=subject_id,
        changed_fields_json=json.dumps(changed_fields),
        created_at=datetime.now(timezone.utc),
    )
    db.add(ev)


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

        # Seed active project
        proj = ProjectModel(
            id=1,
            key="ALPHA",
            name="Alpha Project",
            description="Seed project for Kanban tests",
            active=True,
            creator_account_id=1,
            is_deleted=False,
        )
        db.add(proj)
        db.flush()

        # Seed memberships: user1 (MANAGER), user2 (CONTRIBUTOR), admin (VIEWER)
        m1 = MembershipModel(
            id=1,
            project_id=1,
            account_id=1,
            role=MembershipRoleEnum.MANAGER.value,
            active=True,
        )
        m2 = MembershipModel(
            id=2,
            project_id=1,
            account_id=2,
            role=MembershipRoleEnum.CONTRIBUTOR.value,
            active=True,
        )
        m3 = MembershipModel(
            id=3,
            project_id=1,
            account_id=3,
            role=MembershipRoleEnum.VIEWER.value,
            active=True,
        )
        db.add_all([m1, m2, m3])
        db.flush()

        # Seed tickets of every priority
        now = datetime.now(timezone.utc)
        tickets = [
            TicketModel(
                id=1,
                project_id=1,
                title="Critical Production Bug",
                description="Fix immediately",
                status=TicketStatusEnum.TODO.value,
                priority=PriorityEnum.P0.value,
                priority_order=0,
                creator_account_id=1,
                assignee_membership_id=1,
                created_at=now,
            ),
            TicketModel(
                id=2,
                project_id=1,
                title="Major Performance Issue",
                description="Slow DB query",
                status=TicketStatusEnum.IN_PROGRESS.value,
                priority=PriorityEnum.P1.value,
                priority_order=1,
                creator_account_id=2,
                assignee_membership_id=2,
                created_at=now,
            ),
            TicketModel(
                id=3,
                project_id=1,
                title="Standard Feature Request",
                description="Add export button",
                status=TicketStatusEnum.TODO.value,
                priority=PriorityEnum.P2.value,
                priority_order=2,
                creator_account_id=1,
                assignee_membership_id=None,
                created_at=now,
            ),
            TicketModel(
                id=4,
                project_id=1,
                title="Minor UI Polish",
                description="Align labels",
                status=TicketStatusEnum.DONE.value,
                priority=PriorityEnum.P3.value,
                priority_order=3,
                creator_account_id=2,
                assignee_membership_id=2,
                created_at=now,
            ),
            TicketModel(
                id=5,
                project_id=1,
                title="Trivial Typos",
                description="Fix spelling in footer",
                status=TicketStatusEnum.CANCELLED.value,
                priority=PriorityEnum.P4.value,
                priority_order=4,
                creator_account_id=1,
                assignee_membership_id=None,
                created_at=now,
            ),
        ]
        db.add_all(tickets)
        db.flush()

        # Seed audit rows
        append_audit_event(
            db,
            1,
            1,
            AuditEventTypeEnum.PROJECT_CREATED,
            "PROJECT",
            1,
            ["key", "name", "description"],
        )
        append_audit_event(
            db, 1, 1, AuditEventTypeEnum.MEMBERSHIP_CREATED, "MEMBERSHIP", 1, ["role", "active"]
        )
        append_audit_event(
            db, 1, 1, AuditEventTypeEnum.MEMBERSHIP_CREATED, "MEMBERSHIP", 2, ["role", "active"]
        )
        append_audit_event(
            db, 1, 1, AuditEventTypeEnum.MEMBERSHIP_CREATED, "MEMBERSHIP", 3, ["role", "active"]
        )
        append_audit_event(
            db, 1, 1, AuditEventTypeEnum.TICKET_CREATED, "TICKET", 1, ["title", "priority", "status"]
        )
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
            query = query.join(
                MembershipModel,
                (MembershipModel.project_id == ProjectModel.id)
                & (MembershipModel.account_id == current_user.id)
                & (MembershipModel.active == True),
            )

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
                description=p.description,
                active=p.active,
                creator_account_id=p.creator_account_id,
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

        trimmed_name = payload.name.strip()
        trimmed_desc = payload.description.strip() if payload.description else ""

        project = ProjectModel(
            key=payload.key,
            name=trimmed_name,
            description=trimmed_desc,
            active=True,
            creator_account_id=current_user.id,
            is_deleted=False,
        )
        db.add(project)
        db.flush()

        # Atomically create creator MANAGER membership
        membership = MembershipModel(
            project_id=project.id,
            account_id=current_user.id,
            role=MembershipRoleEnum.MANAGER.value,
            active=True,
        )
        db.add(membership)
        db.flush()

        # Append audit event
        append_audit_event(
            db,
            current_user.id,
            project.id,
            AuditEventTypeEnum.PROJECT_CREATED,
            "PROJECT",
            project.id,
            ["key", "name", "description"],
        )

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

        if current_user.role != RoleEnum.ADMIN.value:
            mem = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.project_id == project_id,
                    MembershipModel.account_id == current_user.id,
                    MembershipModel.active == True,
                )
            )
            if not mem:
                raise AppException(403, "Access denied to non-member project", "Forbidden")

        return project

    @staticmethod
    def patch_project(
        db: Session, current_user: UserModel, project_id: int, payload: ProjectPatchPayload
    ) -> ProjectModel:
        project = ProjectController.get_project(db, current_user, project_id)

        # Must be MANAGER or admin
        if current_user.role != RoleEnum.ADMIN.value:
            mem = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.project_id == project_id,
                    MembershipModel.account_id == current_user.id,
                    MembershipModel.active == True,
                )
            )
            if not mem or mem.role != MembershipRoleEnum.MANAGER.value:
                raise AppException(403, "Manager privileges required to patch project", "Forbidden")

        changed_fields = []

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
            changed_fields.append("key")

        if payload.name is not None and payload.name.strip() != project.name:
            project.name = payload.name.strip()
            changed_fields.append("name")

        if payload.description is not None and payload.description.strip() != project.description:
            project.description = payload.description.strip()
            changed_fields.append("description")

        if payload.active is not None and payload.active != project.active:
            project.active = payload.active
            changed_fields.append("active")

        if changed_fields:
            append_audit_event(
                db,
                current_user.id,
                project.id,
                AuditEventTypeEnum.PROJECT_UPDATED,
                "PROJECT",
                project.id,
                changed_fields,
            )

        db.commit()
        db.refresh(project)
        return project

    @staticmethod
    def delete_project(db: Session, current_user: UserModel, project_id: int):
        project = ProjectController.get_project(db, current_user, project_id)

        if current_user.role != RoleEnum.ADMIN.value:
            mem = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.project_id == project_id,
                    MembershipModel.account_id == current_user.id,
                    MembershipModel.active == True,
                )
            )
            if not mem or mem.role != MembershipRoleEnum.MANAGER.value:
                raise AppException(403, "Manager privileges required to delete project", "Forbidden")

        # REQ-5-008: A project can be soft-deleted only when it has no tickets and no active non-creator memberships
        has_tickets = db.scalar(select(TicketModel).where(TicketModel.project_id == project_id).limit(1))
        if has_tickets:
            raise AppException(409, "Cannot delete project with existing tickets", "Conflict")

        has_other_active_members = db.scalar(
            select(MembershipModel)
            .where(
                MembershipModel.project_id == project_id,
                MembershipModel.account_id != project.creator_account_id,
                MembershipModel.active == True,
            )
            .limit(1)
        )
        if has_other_active_members:
            raise AppException(
                409, "Cannot delete project with active non-creator memberships", "Conflict"
            )

        project.is_deleted = True
        append_audit_event(
            db,
            current_user.id,
            project.id,
            AuditEventTypeEnum.PROJECT_DELETED,
            "PROJECT",
            project.id,
            ["is_deleted"],
        )

        db.commit()


class MembershipController:
    @staticmethod
    def list_memberships(
        db: Session,
        current_user: UserModel,
        project_id: int,
        role: Optional[MembershipRoleEnum],
        active: Optional[bool],
        limit: int,
        after_id: Optional[int],
    ) -> MembershipListResponse:
        ProjectController.get_project(db, current_user, project_id)

        query = select(MembershipModel).where(MembershipModel.project_id == project_id)

        if role is not None:
            query = query.where(MembershipModel.role == role.value)
        if active is not None:
            query = query.where(MembershipModel.active == active)
        if after_id is not None and after_id > 0:
            query = query.where(MembershipModel.id > after_id)

        query = query.order_by(MembershipModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())

        next_after_id = None
        if len(records) > limit:
            next_after_id = records[limit - 1].id
            records = records[:limit]

        items = [
            MembershipOut(
                id=m.id,
                project_id=m.project_id,
                account_id=m.account_id,
                role=MembershipRoleEnum(m.role),
                active=m.active,
            )
            for m in records
        ]
        return MembershipListResponse(items=items, next_after_id=next_after_id)

    @staticmethod
    def create_membership(
        db: Session,
        current_user: UserModel,
        project_id: int,
        payload: MembershipCreatePayload,
    ) -> MembershipModel:
        ProjectController.get_project(db, current_user, project_id)

        if current_user.role != RoleEnum.ADMIN.value:
            mem = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.project_id == project_id,
                    MembershipModel.account_id == current_user.id,
                    MembershipModel.active == True,
                )
            )
            if not mem or mem.role != MembershipRoleEnum.MANAGER.value:
                raise AppException(403, "Manager privileges required to add memberships", "Forbidden")

        account = db.scalar(select(UserModel).where(UserModel.id == payload.account_id))
        if not account:
            raise AppException(404, f"Account {payload.account_id} not found", "Not Found")

        existing = db.scalar(
            select(MembershipModel).where(
                MembershipModel.project_id == project_id,
                MembershipModel.account_id == payload.account_id,
            )
        )
        if existing:
            raise AppException(409, "Membership already exists for this account and project", "Conflict")

        membership = MembershipModel(
            project_id=project_id,
            account_id=payload.account_id,
            role=payload.role.value,
            active=True,
        )
        db.add(membership)
        db.flush()

        append_audit_event(
            db,
            current_user.id,
            project_id,
            AuditEventTypeEnum.MEMBERSHIP_CREATED,
            "MEMBERSHIP",
            membership.id,
            ["role", "active"],
        )

        db.commit()
        db.refresh(membership)
        return membership

    @staticmethod
    def patch_membership(
        db: Session,
        current_user: UserModel,
        project_id: int,
        membership_id: int,
        payload: MembershipPatchPayload,
    ) -> MembershipModel:
        ProjectController.get_project(db, current_user, project_id)

        if current_user.role != RoleEnum.ADMIN.value:
            caller_mem = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.project_id == project_id,
                    MembershipModel.account_id == current_user.id,
                    MembershipModel.active == True,
                )
            )
            if not caller_mem or caller_mem.role != MembershipRoleEnum.MANAGER.value:
                raise AppException(403, "Manager privileges required to edit memberships", "Forbidden")

        membership = db.scalar(
            select(MembershipModel).where(
                MembershipModel.id == membership_id, MembershipModel.project_id == project_id
            )
        )
        if not membership:
            raise AppException(404, f"Membership {membership_id} not found in project {project_id}", "Not Found")

        # Invariant: At least one active MANAGER must exist
        will_demote = payload.role is not None and payload.role != MembershipRoleEnum.MANAGER and membership.role == MembershipRoleEnum.MANAGER.value
        will_deactivate = payload.active is False and membership.active is True and membership.role == MembershipRoleEnum.MANAGER.value

        if will_demote or will_deactivate:
            active_managers = list(
                db.scalars(
                    select(MembershipModel).where(
                        MembershipModel.project_id == project_id,
                        MembershipModel.role == MembershipRoleEnum.MANAGER.value,
                        MembershipModel.active == True,
                        MembershipModel.id != membership_id,
                    )
                ).all()
            )
            if not active_managers:
                raise AppException(409, "Cannot demote or deactivate the final active MANAGER", "Conflict")

        changed_fields = []
        if payload.role is not None and payload.role.value != membership.role:
            membership.role = payload.role.value
            changed_fields.append("role")

        if payload.active is not None and payload.active != membership.active:
            membership.active = payload.active
            changed_fields.append("active")

        if changed_fields:
            append_audit_event(
                db,
                current_user.id,
                project_id,
                AuditEventTypeEnum.MEMBERSHIP_UPDATED,
                "MEMBERSHIP",
                membership.id,
                changed_fields,
            )

        db.commit()
        db.refresh(membership)
        return membership

    @staticmethod
    def delete_membership(
        db: Session,
        current_user: UserModel,
        project_id: int,
        membership_id: int,
    ):
        ProjectController.get_project(db, current_user, project_id)

        if current_user.role != RoleEnum.ADMIN.value:
            caller_mem = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.project_id == project_id,
                    MembershipModel.account_id == current_user.id,
                    MembershipModel.active == True,
                )
            )
            if not caller_mem or caller_mem.role != MembershipRoleEnum.MANAGER.value:
                raise AppException(403, "Manager privileges required to remove memberships", "Forbidden")

        membership = db.scalar(
            select(MembershipModel).where(
                MembershipModel.id == membership_id, MembershipModel.project_id == project_id
            )
        )
        if not membership:
            raise AppException(404, f"Membership {membership_id} not found in project {project_id}", "Not Found")

        if membership.role == MembershipRoleEnum.MANAGER.value and membership.active:
            active_managers = list(
                db.scalars(
                    select(MembershipModel).where(
                        MembershipModel.project_id == project_id,
                        MembershipModel.role == MembershipRoleEnum.MANAGER.value,
                        MembershipModel.active == True,
                        MembershipModel.id != membership_id,
                    )
                ).all()
            )
            if not active_managers:
                raise AppException(409, "Cannot remove the final active MANAGER", "Conflict")

        membership.active = False
        append_audit_event(
            db,
            current_user.id,
            project_id,
            AuditEventTypeEnum.MEMBERSHIP_REMOVED,
            "MEMBERSHIP",
            membership.id,
            ["active"],
        )

        db.commit()


class TicketController:
    @staticmethod
    def list_tickets(
        db: Session,
        current_user: UserModel,
        project_id: Optional[int],
        ticket_status: Optional[TicketStatusEnum],
        priority: Optional[PriorityEnum],
        assignee_membership_id: Optional[int],
        creator_account_id: Optional[int],
        q: Optional[str],
        priority_sort: bool,
        after_priority: Optional[PriorityEnum],
        after_id: Optional[int],
        limit: int,
    ) -> TicketListResponse:
        # Cursor validation for priority_sort
        if priority_sort:
            if (after_priority is None and after_id is not None) or (
                after_priority is not None and after_id is None
            ):
                raise AppException(
                    422,
                    "after_priority and after_id must be supplied together or omitted together",
                    "Unprocessable Entity",
                )

        if creator_account_id is not None and current_user.role != RoleEnum.ADMIN.value:
            raise AppException(403, "creator_account_id filter is permitted only for admins", "Forbidden")

        query = select(TicketModel)

        # Restrict to active-member projects for regular users
        if current_user.role != RoleEnum.ADMIN.value:
            user_proj_subq = select(MembershipModel.project_id).where(
                MembershipModel.account_id == current_user.id, MembershipModel.active == True
            )
            query = query.where(TicketModel.project_id.in_(user_proj_subq))

        if project_id is not None:
            if project_id <= 0:
                raise AppException(404, "project_id must be a positive integer", "Not Found")
            # Verify user membership if not admin
            if current_user.role != RoleEnum.ADMIN.value:
                mem = db.scalar(
                    select(MembershipModel).where(
                        MembershipModel.project_id == project_id,
                        MembershipModel.account_id == current_user.id,
                        MembershipModel.active == True,
                    )
                )
                if not mem:
                    raise AppException(403, "Access denied to non-member project", "Forbidden")
            query = query.where(TicketModel.project_id == project_id)

        if ticket_status is not None:
            query = query.where(TicketModel.status == ticket_status.value)

        if priority is not None:
            query = query.where(TicketModel.priority == priority.value)

        if assignee_membership_id is not None:
            if assignee_membership_id <= 0:
                raise AppException(404, "assignee_membership_id must be a positive integer", "Not Found")
            query = query.where(TicketModel.assignee_membership_id == assignee_membership_id)

        if creator_account_id is not None:
            if creator_account_id <= 0:
                raise AppException(404, "creator_account_id must be a positive integer", "Not Found")
            query = query.where(TicketModel.creator_account_id == creator_account_id)

        if q:
            trimmed_q = q.strip()
            if len(trimmed_q) < 1 or len(trimmed_q) > 80:
                raise AppException(422, "q filter must be 1..80 trimmed characters", "Unprocessable Entity")
            lower_q = f"%{trimmed_q.lower()}%"
            query = query.where(
                TicketModel.title.ilike(lower_q) | TicketModel.description.ilike(lower_q)
            )

        # Sorting and pagination
        if priority_sort:
            if after_priority is not None and after_id is not None:
                cursor_weight = PRIORITY_WEIGHT[after_priority.value]
                query = query.where(
                    (TicketModel.priority_order > cursor_weight)
                    | (
                        (TicketModel.priority_order == cursor_weight)
                        & (TicketModel.id > after_id)
                    )
                )
            query = query.order_by(TicketModel.priority_order.asc(), TicketModel.id.asc())
        else:
            if after_id is not None and after_id > 0:
                query = query.where(TicketModel.id > after_id)
            query = query.order_by(TicketModel.id.asc())

        query = query.limit(limit + 1)
        records = list(db.scalars(query).all())

        next_after_id = None
        next_after_priority = None
        if len(records) > limit:
            last_record = records[limit - 1]
            next_after_id = last_record.id
            if priority_sort:
                next_after_priority = PriorityEnum(last_record.priority)
            records = records[:limit]

        items = [
            TicketOut(
                id=t.id,
                project_id=t.project_id,
                title=t.title,
                description=t.description,
                status=TicketStatusEnum(t.status),
                priority=PriorityEnum(t.priority),
                creator_account_id=t.creator_account_id,
                assignee_membership_id=t.assignee_membership_id,
                created_at=t.created_at.replace(tzinfo=timezone.utc) if t.created_at.tzinfo is None else t.created_at,
            )
            for t in records
        ]
        return TicketListResponse(
            items=items, next_after_id=next_after_id, next_after_priority=next_after_priority
        )

    @staticmethod
    def create_ticket(
        db: Session, current_user: UserModel, payload: TicketCreatePayload
    ) -> TicketModel:
        project = ProjectController.get_project(db, current_user, payload.project_id)
        if not project.active:
            raise AppException(409, f"Project {payload.project_id} is inactive", "Conflict")

        # Permission: CONTRIBUTOR, MANAGER, or admin
        if current_user.role != RoleEnum.ADMIN.value:
            mem = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.project_id == payload.project_id,
                    MembershipModel.account_id == current_user.id,
                    MembershipModel.active == True,
                )
            )
            if not mem or mem.role == MembershipRoleEnum.VIEWER.value:
                raise AppException(403, "Contributor or Manager role required to create tickets", "Forbidden")

        # Validate assignee
        if payload.assignee_membership_id is not None:
            assignee = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.id == payload.assignee_membership_id,
                    MembershipModel.project_id == payload.project_id,
                )
            )
            if not assignee:
                raise AppException(404, f"Assignee membership {payload.assignee_membership_id} not found", "Not Found")
            if not assignee.active or assignee.role == MembershipRoleEnum.VIEWER.value:
                raise AppException(
                    409, "Assignee must be an active CONTRIBUTOR or MANAGER in this project", "Conflict"
                )

        now = datetime.now(timezone.utc)
        ticket = TicketModel(
            project_id=payload.project_id,
            title=payload.title.strip(),
            description=payload.description.strip() if payload.description else "",
            status=(payload.status or TicketStatusEnum.TODO).value,
            priority=payload.priority.value,
            priority_order=PRIORITY_WEIGHT[payload.priority.value],
            creator_account_id=current_user.id,
            assignee_membership_id=payload.assignee_membership_id,
            created_at=now,
        )
        db.add(ticket)
        db.flush()

        append_audit_event(
            db,
            current_user.id,
            payload.project_id,
            AuditEventTypeEnum.TICKET_CREATED,
            "TICKET",
            ticket.id,
            ["title", "priority", "status"],
        )

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

        # Check membership
        ProjectController.get_project(db, current_user, ticket.project_id)
        return ticket

    @staticmethod
    def patch_ticket(
        db: Session, current_user: UserModel, ticket_id: int, payload: TicketPatchPayload
    ) -> TicketModel:
        ticket = TicketController.get_ticket(db, current_user, ticket_id)

        # REQ-5-004: CONTRIBUTOR may edit tickets contributor created; MANAGER may edit any project ticket. Admin may edit all.
        if current_user.role != RoleEnum.ADMIN.value:
            mem = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.project_id == ticket.project_id,
                    MembershipModel.account_id == current_user.id,
                    MembershipModel.active == True,
                )
            )
            if not mem:
                raise AppException(403, "Access denied to non-member project", "Forbidden")
            if mem.role == MembershipRoleEnum.VIEWER.value:
                raise AppException(403, "Viewer cannot edit tickets", "Forbidden")
            if mem.role == MembershipRoleEnum.CONTRIBUTOR.value and ticket.creator_account_id != current_user.id:
                raise AppException(403, "Contributors may only edit tickets they created", "Forbidden")

        changed_fields = []

        if payload.assignee_membership_id is not None and payload.assignee_membership_id != ticket.assignee_membership_id:
            assignee = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.id == payload.assignee_membership_id,
                    MembershipModel.project_id == ticket.project_id,
                )
            )
            if not assignee:
                raise AppException(404, f"Assignee membership {payload.assignee_membership_id} not found", "Not Found")
            if not assignee.active or assignee.role == MembershipRoleEnum.VIEWER.value:
                raise AppException(
                    409, "Assignee must be an active CONTRIBUTOR or MANAGER in this project", "Conflict"
                )
            ticket.assignee_membership_id = payload.assignee_membership_id
            changed_fields.append("assignee_membership_id")

        if payload.title is not None and payload.title.strip() != ticket.title:
            ticket.title = payload.title.strip()
            changed_fields.append("title")

        if payload.description is not None and payload.description.strip() != ticket.description:
            ticket.description = payload.description.strip()
            changed_fields.append("description")

        if payload.status is not None and payload.status.value != ticket.status:
            ticket.status = payload.status.value
            changed_fields.append("status")

        if payload.priority is not None and payload.priority.value != ticket.priority:
            ticket.priority = payload.priority.value
            ticket.priority_order = PRIORITY_WEIGHT[payload.priority.value]
            changed_fields.append("priority")

        if changed_fields:
            append_audit_event(
                db,
                current_user.id,
                ticket.project_id,
                AuditEventTypeEnum.TICKET_UPDATED,
                "TICKET",
                ticket.id,
                changed_fields,
            )

        db.commit()
        db.refresh(ticket)
        return ticket


class AuditEventController:
    @staticmethod
    def list_audit_events(
        db: Session,
        current_user: UserModel,
        project_id: Optional[int],
        event_type: Optional[AuditEventTypeEnum],
        ticket_id: Optional[int],
        subject_id: Optional[int],
        limit: int,
        after_id: Optional[int],
    ) -> AuditEventListResponse:
        # Non-admin requires project_id
        if current_user.role != RoleEnum.ADMIN.value:
            if project_id is None:
                raise AppException(400, "project_id is required for non-admin users", "Bad Request")
            if project_id <= 0:
                raise AppException(404, "project_id must be a positive integer", "Not Found")

            mem = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.project_id == project_id,
                    MembershipModel.account_id == current_user.id,
                    MembershipModel.active == True,
                )
            )
            if not mem or mem.role != MembershipRoleEnum.MANAGER.value:
                raise AppException(403, "Manager privileges required to view audit events", "Forbidden")
        else:
            if project_id is not None and project_id <= 0:
                raise AppException(404, "project_id must be a positive integer", "Not Found")

        query = select(AuditEventModel)

        if project_id is not None:
            query = query.where(AuditEventModel.project_id == project_id)

        if event_type is not None:
            query = query.where(AuditEventModel.event_type == event_type.value)

        if ticket_id is not None:
            if ticket_id <= 0:
                raise AppException(404, "ticket_id must be a positive integer", "Not Found")
            query = query.where(
                AuditEventModel.subject_type == "TICKET",
                AuditEventModel.subject_id == ticket_id,
            )

        if subject_id is not None:
            if subject_id <= 0:
                raise AppException(404, "subject_id must be a positive integer", "Not Found")
            query = query.where(AuditEventModel.subject_id == subject_id)

        if after_id is not None and after_id > 0:
            query = query.where(AuditEventModel.id > after_id)

        query = query.order_by(AuditEventModel.id.asc()).limit(limit + 1)
        records = list(db.scalars(query).all())

        next_after_id = None
        if len(records) > limit:
            next_after_id = records[limit - 1].id
            records = records[:limit]

        items = [
            AuditEventOut(
                id=a.id,
                actor_account_id=a.actor_account_id,
                project_id=a.project_id,
                event_type=AuditEventTypeEnum(a.event_type),
                subject_type=a.subject_type,
                subject_id=a.subject_id,
                changed_fields=json.loads(a.changed_fields_json),
                created_at=a.created_at.replace(tzinfo=timezone.utc) if a.created_at.tzinfo is None else a.created_at,
            )
            for a in records
        ]
        return AuditEventListResponse(items=items, next_after_id=next_after_id)


# -----------------------------------------------------------------------------
# FastAPI App Initialization & Routes
# -----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    with SessionLocal() as db:
        seed_database(db)
    yield


app = FastAPI(
    title="Kanban Project Management - Filters, Priorities, Audit, and Permissions API",
    version="1.0.0",
    description="RESTful API for project access control, priority sorting, ticket workflows, and immutable audit logging.",
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
    summary="List accessible projects",
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
        description=proj.description,
        active=proj.active,
        creator_account_id=proj.creator_account_id,
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
        description=proj.description,
        active=proj.active,
        creator_account_id=proj.creator_account_id,
    )


@app.patch(
    "/projects/{project_id}",
    response_model=ProjectOut,
    status_code=status.HTTP_200_OK,
    summary="Patch project (MANAGER or Admin only)",
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
        description=proj.description,
        active=proj.active,
        creator_account_id=proj.creator_account_id,
    )


@app.delete(
    "/projects/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft-delete project (MANAGER or Admin only)",
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
    "/projects/{project_id}/memberships",
    response_model=MembershipListResponse,
    status_code=status.HTTP_200_OK,
    summary="List project memberships",
    tags=["Memberships"],
)
def list_memberships(
    project_id: int,
    role: Optional[MembershipRoleEnum] = Query(None, description="Filter by role"),
    active: Optional[bool] = Query(None, description="Filter by active status"),
    limit: int = Query(20, ge=1, le=100, description="Pagination limit"),
    after_id: Optional[int] = Query(None, gt=0, description="Pagination cursor"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return MembershipController.list_memberships(
        db, current_user, project_id, role, active, limit, after_id
    )


@app.post(
    "/projects/{project_id}/memberships",
    response_model=MembershipOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create project membership (MANAGER or Admin only)",
    tags=["Memberships"],
)
def create_membership(
    project_id: int,
    payload: MembershipCreatePayload,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    mem = MembershipController.create_membership(db, current_user, project_id, payload)
    response.headers["Location"] = f"/projects/{project_id}/memberships/{mem.id}"
    return MembershipOut(
        id=mem.id,
        project_id=mem.project_id,
        account_id=mem.account_id,
        role=MembershipRoleEnum(mem.role),
        active=mem.active,
    )


@app.patch(
    "/projects/{project_id}/memberships/{membership_id}",
    response_model=MembershipOut,
    status_code=status.HTTP_200_OK,
    summary="Patch membership (MANAGER or Admin only)",
    tags=["Memberships"],
)
def patch_membership(
    project_id: int,
    membership_id: int,
    payload: MembershipPatchPayload,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    mem = MembershipController.patch_membership(
        db, current_user, project_id, membership_id, payload
    )
    return MembershipOut(
        id=mem.id,
        project_id=mem.project_id,
        account_id=mem.account_id,
        role=MembershipRoleEnum(mem.role),
        active=mem.active,
    )


@app.delete(
    "/projects/{project_id}/memberships/{membership_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deactivate membership (MANAGER or Admin only)",
    tags=["Memberships"],
)
def delete_membership(
    project_id: int,
    membership_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    MembershipController.delete_membership(db, current_user, project_id, membership_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
    "/tickets",
    response_model=TicketListResponse,
    status_code=status.HTTP_200_OK,
    summary="List tickets with AND filters and optional priority sort",
    tags=["Tickets"],
)
def list_tickets(
    project_id: Optional[int] = Query(None, description="Optional project filter"),
    status: Optional[TicketStatusEnum] = Query(None, description="Filter status"),
    priority: Optional[PriorityEnum] = Query(None, description="Filter priority"),
    assignee_membership_id: Optional[int] = Query(None, description="Filter assignee"),
    creator_account_id: Optional[int] = Query(None, description="Admin-only filter creator"),
    q: Optional[str] = Query(None, description="Case-insensitive substring search in title/description"),
    priority_sort: bool = Query(False, description="Sort by priority ascending then id"),
    after_priority: Optional[PriorityEnum] = Query(None, description="Priority cursor when priority_sort=True"),
    after_id: Optional[int] = Query(None, gt=0, description="Id cursor"),
    limit: int = Query(20, ge=1, le=100, description="Pagination limit"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return TicketController.list_tickets(
        db,
        current_user,
        project_id,
        status,
        priority,
        assignee_membership_id,
        creator_account_id,
        q,
        priority_sort,
        after_priority,
        after_id,
        limit,
    )


@app.post(
    "/tickets",
    response_model=TicketOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create ticket (CONTRIBUTOR, MANAGER or Admin)",
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
        title=ticket.title,
        description=ticket.description,
        status=TicketStatusEnum(ticket.status),
        priority=PriorityEnum(ticket.priority),
        creator_account_id=ticket.creator_account_id,
        assignee_membership_id=ticket.assignee_membership_id,
        created_at=ticket.created_at.replace(tzinfo=timezone.utc) if ticket.created_at.tzinfo is None else ticket.created_at,
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
        title=ticket.title,
        description=ticket.description,
        status=TicketStatusEnum(ticket.status),
        priority=PriorityEnum(ticket.priority),
        creator_account_id=ticket.creator_account_id,
        assignee_membership_id=ticket.assignee_membership_id,
        created_at=ticket.created_at.replace(tzinfo=timezone.utc) if ticket.created_at.tzinfo is None else ticket.created_at,
    )


@app.patch(
    "/tickets/{ticket_id}",
    response_model=TicketOut,
    status_code=status.HTTP_200_OK,
    summary="Patch ticket",
    tags=["Tickets"],
)
def patch_ticket(
    ticket_id: int,
    payload: TicketPatchPayload,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = TicketController.patch_ticket(db, current_user, ticket_id, payload)
    return TicketOut(
        id=ticket.id,
        project_id=ticket.project_id,
        title=ticket.title,
        description=ticket.description,
        status=TicketStatusEnum(ticket.status),
        priority=PriorityEnum(ticket.priority),
        creator_account_id=ticket.creator_account_id,
        assignee_membership_id=ticket.assignee_membership_id,
        created_at=ticket.created_at.replace(tzinfo=timezone.utc) if ticket.created_at.tzinfo is None else ticket.created_at,
    )


@app.get(
    "/audit-events",
    response_model=AuditEventListResponse,
    status_code=status.HTTP_200_OK,
    summary="List immutable audit events (MANAGER or Admin only)",
    tags=["Audit"],
)
def list_audit_events(
    project_id: Optional[int] = Query(None, description="Required for non-admin"),
    event_type: Optional[AuditEventTypeEnum] = Query(None, description="Filter event type"),
    ticket_id: Optional[int] = Query(None, description="Filter ticket subject"),
    subject_id: Optional[int] = Query(None, description="Filter subject id"),
    limit: int = Query(20, ge=1, le=100, description="Pagination limit"),
    after_id: Optional[int] = Query(None, gt=0, description="Pagination cursor"),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return AuditEventController.list_audit_events(
        db, current_user, project_id, event_type, ticket_id, subject_id, limit, after_id
    )
