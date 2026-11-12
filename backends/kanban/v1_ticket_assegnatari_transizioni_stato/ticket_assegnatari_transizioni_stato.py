"""
Kanban Project Management - Variant A: Tickets, assignees, and state transitions
CASE_ID: 5

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
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Setup standard logging
logger = logging.getLogger("kanban_transitions")
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


class TicketStatusEnum(str, enum.Enum):
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    REVIEW = "REVIEW"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


# Allowed state transitions
ALLOWED_TRANSITIONS: Dict[str, set[str]] = {
    TicketStatusEnum.TODO.value: {
        TicketStatusEnum.IN_PROGRESS.value,
        TicketStatusEnum.CANCELLED.value,
    },
    TicketStatusEnum.IN_PROGRESS.value: {
        TicketStatusEnum.REVIEW.value,
        TicketStatusEnum.DONE.value,
        TicketStatusEnum.CANCELLED.value,
    },
    TicketStatusEnum.REVIEW.value: {
        TicketStatusEnum.IN_PROGRESS.value,
        TicketStatusEnum.DONE.value,
        TicketStatusEnum.CANCELLED.value,
    },
    TicketStatusEnum.DONE.value: set(),  # Terminal
    TicketStatusEnum.CANCELLED.value: set(),  # Terminal
}


# ============================================================================
# SQLAlchemy Models
# ============================================================================
class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    role = Column(String(20), nullable=False)  # user or admin
    token = Column(String(100), unique=True, nullable=False)


class ProjectModel(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(10), unique=True, nullable=False, index=True)
    name = Column(String(120), nullable=False)
    description = Column(String(1000), nullable=False, default="")
    creator_account_id = Column(Integer, ForeignKey("users.id"), nullable=False)
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


class MembershipModel(Base):
    __tablename__ = "memberships"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    active = Column(Boolean, default=True, nullable=False)
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

    __table_args__ = (
        UniqueConstraint("project_id", "account_id", name="uq_project_account"),
    )


class TicketModel(Base):
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False, index=True)
    title = Column(String(160), nullable=False)
    description = Column(String(4000), nullable=False, default="")
    estimated_points = Column(Integer, nullable=False, default=0)
    creator_account_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    assignee_membership_id = Column(
        Integer, ForeignKey("memberships.id"), nullable=True, index=True
    )
    status = Column(
        String(20), nullable=False, default=TicketStatusEnum.TODO.value, index=True
    )
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


class TicketTransitionModel(Base):
    __tablename__ = "ticket_transitions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id"), nullable=False, index=True)
    from_status = Column(String(20), nullable=False)
    to_status = Column(String(20), nullable=False)
    actor_account_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    transition_note = Column(String(500), nullable=False, default="")
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


# ============================================================================
# Pydantic Schemas
# ============================================================================
class AppException(HTTPException):
    def __init__(self, status_code: int, detail: str, error_type: str = "Error"):
        super().__init__(status_code=status_code, detail=detail)
        self.error_type = error_type


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., description="Unique uppercase project key")
    name: str = Field(..., description="Project name")
    description: Optional[str] = Field(default="", description="Project description")

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^[A-Z][A-Z0-9]{1,9}$", v):
            raise ValueError(
                "key must match ^[A-Z][A-Z0-9]{1,9}$ (uppercase starting with letter, length 2..10)"
            )
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not (3 <= len(v) <= 120):
            raise ValueError("name must be between 3 and 120 characters")
        return v

    @field_validator("description")
    @classmethod
    def validate_description(cls, v: Optional[str]) -> str:
        if v is None:
            return ""
        v = v.strip()
        if len(v) > 1000:
            raise ValueError("description must not exceed 1000 characters")
        return v


class ProjectPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    active: Optional[bool] = None

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not re.match(r"^[A-Z][A-Z0-9]{1,9}$", v):
            raise ValueError(
                "key must match ^[A-Z][A-Z0-9]{1,9}$ (uppercase starting with letter, length 2..10)"
            )
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (3 <= len(v) <= 120):
            raise ValueError("name must be between 3 and 120 characters")
        return v

    @field_validator("description")
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if len(v) > 1000:
            raise ValueError("description must not exceed 1000 characters")
        return v


class ProjectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    key: str
    name: str
    description: str
    creator_account_id: int
    active: bool
    created_at: datetime
    updated_at: datetime


class ProjectListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[ProjectResponse]
    limit: int
    next_after_id: Optional[int] = None


class MembershipCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: int = Field(..., gt=0, description="Target user account id")


class MembershipResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    project_id: int
    account_id: int
    active: bool
    created_at: datetime
    updated_at: datetime


class MembershipListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[MembershipResponse]
    limit: int
    next_after_id: Optional[int] = None


class TicketCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: int = Field(..., gt=0, description="Project ID")
    title: str = Field(..., description="Ticket title")
    description: Optional[str] = Field(default="", description="Ticket description")
    estimated_points: Optional[int] = Field(
        default=0, ge=0, le=21, description="Estimated story points"
    )

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        v = v.strip()
        if not (3 <= len(v) <= 160):
            raise ValueError("title must be between 3 and 160 characters")
        return v

    @field_validator("description")
    @classmethod
    def validate_description(cls, v: Optional[str]) -> str:
        if v is None:
            return ""
        v = v.strip()
        if len(v) > 4000:
            raise ValueError("description must not exceed 4000 characters")
        return v


class TicketPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = None
    description: Optional[str] = None
    estimated_points: Optional[int] = Field(default=None, ge=0, le=21)

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not (3 <= len(v) <= 160):
            raise ValueError("title must be between 3 and 160 characters")
        return v

    @field_validator("description")
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if len(v) > 4000:
            raise ValueError("description must not exceed 4000 characters")
        return v


class TicketAssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assignee_membership_id: Optional[int] = Field(
        None, description="Assignee membership id, or null to unassign"
    )

    @field_validator("assignee_membership_id")
    @classmethod
    def validate_assignee_id(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and v <= 0:
            raise ValueError("assignee_membership_id must be a positive integer")
        return v


class TicketTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_status: TicketStatusEnum = Field(..., description="Target status")
    transition_note: Optional[str] = Field(
        default="", description="Optional transition note"
    )

    @field_validator("transition_note")
    @classmethod
    def validate_note(cls, v: Optional[str]) -> str:
        if v is None:
            return ""
        v = v.strip()
        if len(v) > 500:
            raise ValueError("transition_note must not exceed 500 characters")
        return v


class TicketResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: int
    project_id: int
    title: str
    description: str
    estimated_points: int
    creator_account_id: int
    assignee_membership_id: Optional[int] = None
    status: TicketStatusEnum
    created_at: datetime
    updated_at: datetime


class TicketListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[TicketResponse]
    limit: int
    next_after_id: Optional[int] = None


# ============================================================================
# Seed Data Initialization
# ============================================================================
def init_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        existing = db.scalar(select(UserModel).limit(1))
        if not existing:
            # Users
            u1 = UserModel(
                id=1,
                username="user1",
                role=RoleEnum.USER.value,
                token="user1-token-secret-12345",
            )
            u2 = UserModel(
                id=2,
                username="user2",
                role=RoleEnum.USER.value,
                token="user2-token-secret-12345",
            )
            admin = UserModel(
                id=3,
                username="admin",
                role=RoleEnum.ADMIN.value,
                token="admin-token-secret-12345",
            )
            db.add_all([u1, u2, admin])
            db.flush()

            # Project
            p1 = ProjectModel(
                id=1,
                key="ALPHA",
                name="Alpha Project",
                description="Main project for development",
                creator_account_id=1,
                active=True,
                is_deleted=False,
            )
            db.add(p1)
            db.flush()

            # Memberships
            m1 = MembershipModel(id=1, project_id=1, account_id=1, active=True)
            m2 = MembershipModel(id=2, project_id=1, account_id=2, active=True)
            db.add_all([m1, m2])
            db.flush()

            # Tickets in all 5 states: TODO, IN_PROGRESS, REVIEW, DONE, CANCELLED
            t1 = TicketModel(
                id=1,
                project_id=1,
                title="Setup repository structure",
                description="Initial skeleton",
                estimated_points=3,
                creator_account_id=1,
                assignee_membership_id=1,
                status=TicketStatusEnum.TODO.value,
            )
            t2 = TicketModel(
                id=2,
                project_id=1,
                title="Database schema migrations",
                description="Create tables",
                estimated_points=5,
                creator_account_id=1,
                assignee_membership_id=2,
                status=TicketStatusEnum.IN_PROGRESS.value,
            )
            t3 = TicketModel(
                id=3,
                project_id=1,
                title="Authentication middleware",
                description="Verify tokens",
                estimated_points=8,
                creator_account_id=2,
                assignee_membership_id=1,
                status=TicketStatusEnum.REVIEW.value,
            )
            t4 = TicketModel(
                id=4,
                project_id=1,
                title="System architecture review",
                description="Review components",
                estimated_points=2,
                creator_account_id=1,
                assignee_membership_id=None,
                status=TicketStatusEnum.DONE.value,
            )
            t5 = TicketModel(
                id=5,
                project_id=1,
                title="Deprecated XML parser",
                description="Remove old parser",
                estimated_points=1,
                creator_account_id=2,
                assignee_membership_id=None,
                status=TicketStatusEnum.CANCELLED.value,
            )
            db.add_all([t1, t2, t3, t4, t5])
            db.flush()

            # Initial transitions
            tr1 = TicketTransitionModel(
                id=1,
                ticket_id=2,
                from_status=TicketStatusEnum.TODO.value,
                to_status=TicketStatusEnum.IN_PROGRESS.value,
                actor_account_id=1,
                transition_note="Started working",
            )
            tr2 = TicketTransitionModel(
                id=2,
                ticket_id=3,
                from_status=TicketStatusEnum.TODO.value,
                to_status=TicketStatusEnum.IN_PROGRESS.value,
                actor_account_id=2,
                transition_note="Starting work",
            )
            tr3 = TicketTransitionModel(
                id=3,
                ticket_id=3,
                from_status=TicketStatusEnum.IN_PROGRESS.value,
                to_status=TicketStatusEnum.REVIEW.value,
                actor_account_id=2,
                transition_note="Ready for review",
            )
            tr4 = TicketTransitionModel(
                id=4,
                ticket_id=4,
                from_status=TicketStatusEnum.TODO.value,
                to_status=TicketStatusEnum.IN_PROGRESS.value,
                actor_account_id=1,
                transition_note="Working",
            )
            tr5 = TicketTransitionModel(
                id=5,
                ticket_id=4,
                from_status=TicketStatusEnum.IN_PROGRESS.value,
                to_status=TicketStatusEnum.DONE.value,
                actor_account_id=1,
                transition_note="Done",
            )
            tr6 = TicketTransitionModel(
                id=6,
                ticket_id=5,
                from_status=TicketStatusEnum.TODO.value,
                to_status=TicketStatusEnum.CANCELLED.value,
                actor_account_id=2,
                transition_note="Cancelled legacy task",
            )
            db.add_all([tr1, tr2, tr3, tr4, tr5, tr6])
            db.commit()
    finally:
        db.close()


init_db()


# ============================================================================
# Dependency: DB Session & Current User
# ============================================================================
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(
    authorization: Optional[str] = Header(None), db: Session = Depends(get_db)
) -> UserModel:
    if not authorization or not authorization.startswith("Bearer "):
        raise AppException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header with Bearer token",
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


# ============================================================================
# Controllers (Static MVC)
# ============================================================================
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
            # User only sees projects where user has an active membership
            member_project_ids = select(MembershipModel.project_id).where(
                MembershipModel.account_id == current_user.id,
                MembershipModel.active == True,
            )
            query = query.where(ProjectModel.id.in_(member_project_ids))

        if active is not None:
            query = query.where(ProjectModel.active == active)

        if key_prefix is not None:
            if not re.match(r"^[A-Z0-9]{1,10}$", key_prefix):
                raise AppException(
                    400,
                    "key_prefix must be 1..10 uppercase alphanumeric characters",
                    "Bad Request",
                )
            query = query.where(ProjectModel.key.startswith(key_prefix))

        if after_id is not None:
            if after_id <= 0:
                raise AppException(400, "after_id must be positive", "Bad Request")
            query = query.where(ProjectModel.id > after_id)

        query = query.order_by(ProjectModel.id.asc()).limit(limit + 1)
        rows = list(db.scalars(query).all())

        next_after_id = None
        if len(rows) > limit:
            items = rows[:limit]
            next_after_id = items[-1].id
        else:
            items = rows

        return ProjectListResponse(
            items=[ProjectResponse.model_validate(p) for p in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_project(
        db: Session, current_user: UserModel, payload: ProjectCreateRequest
    ) -> ProjectModel:
        # Check duplicate key
        existing = db.scalar(
            select(ProjectModel).where(
                ProjectModel.key == payload.key, ProjectModel.is_deleted == False
            )
        )
        if existing:
            raise AppException(409, f"Project key '{payload.key}' already in use", "Conflict")

        project = ProjectModel(
            key=payload.key,
            name=payload.name,
            description=payload.description or "",
            creator_account_id=current_user.id,
            active=True,
            is_deleted=False,
        )
        db.add(project)
        db.flush()

        # Creator membership is automatically created and active
        creator_membership = MembershipModel(
            project_id=project.id,
            account_id=current_user.id,
            active=True,
        )
        db.add(creator_membership)
        db.commit()
        db.refresh(project)
        return project

    @staticmethod
    def get_project(db: Session, current_user: UserModel, project_id: int) -> ProjectModel:
        if project_id <= 0:
            raise AppException(404, f"Project {project_id} not found", "Not Found")

        project = db.scalar(
            select(ProjectModel).where(
                ProjectModel.id == project_id, ProjectModel.is_deleted == False
            )
        )
        if not project:
            raise AppException(404, f"Project {project_id} not found", "Not Found")

        if current_user.role != RoleEnum.ADMIN.value:
            # Check membership
            membership = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.project_id == project_id,
                    MembershipModel.account_id == current_user.id,
                    MembershipModel.active == True,
                )
            )
            if not membership:
                raise AppException(403, "Access to project is forbidden", "Forbidden")

        return project

    @staticmethod
    def patch_project(
        db: Session,
        current_user: UserModel,
        project_id: int,
        payload: ProjectPatchRequest,
    ) -> ProjectModel:
        if project_id <= 0:
            raise AppException(404, f"Project {project_id} not found", "Not Found")

        project = db.scalar(
            select(ProjectModel).where(
                ProjectModel.id == project_id, ProjectModel.is_deleted == False
            )
        )
        if not project:
            raise AppException(404, f"Project {project_id} not found", "Not Found")

        # Caller must be creator or admin
        if (
            current_user.role != RoleEnum.ADMIN.value
            and project.creator_account_id != current_user.id
        ):
            raise AppException(
                403, "Only the project creator or an admin may modify the project", "Forbidden"
            )

        fields_set = payload.model_dump(exclude_unset=True)
        if not fields_set:
            raise AppException(400, "At least one field must be updated", "Bad Request")

        if payload.key is not None and payload.key != project.key:
            duplicate = db.scalar(
                select(ProjectModel).where(
                    ProjectModel.key == payload.key,
                    ProjectModel.id != project_id,
                    ProjectModel.is_deleted == False,
                )
            )
            if duplicate:
                raise AppException(
                    409, f"Project key '{payload.key}' is already in use", "Conflict"
                )
            project.key = payload.key

        if payload.name is not None:
            project.name = payload.name

        if payload.description is not None:
            project.description = payload.description

        if payload.active is not None:
            if payload.active is False and project.active is True:
                # Deactivation check: active tickets (TODO, IN_PROGRESS, REVIEW)
                active_tickets_count = db.scalar(
                    select(TicketModel)
                    .where(
                        TicketModel.project_id == project_id,
                        TicketModel.status.in_(
                            [
                                TicketStatusEnum.TODO.value,
                                TicketStatusEnum.IN_PROGRESS.value,
                                TicketStatusEnum.REVIEW.value,
                            ]
                        ),
                    )
                    .limit(1)
                )
                if active_tickets_count:
                    raise AppException(
                        409,
                        "Cannot deactivate project while active tickets exist",
                        "Conflict",
                    )
            project.active = payload.active

        db.commit()
        db.refresh(project)
        return project

    @staticmethod
    def delete_project(db: Session, current_user: UserModel, project_id: int):
        if project_id <= 0:
            raise AppException(404, f"Project {project_id} not found", "Not Found")

        project = db.scalar(
            select(ProjectModel).where(
                ProjectModel.id == project_id, ProjectModel.is_deleted == False
            )
        )
        if not project:
            raise AppException(404, f"Project {project_id} not found", "Not Found")

        if (
            current_user.role != RoleEnum.ADMIN.value
            and project.creator_account_id != current_user.id
        ):
            raise AppException(
                403, "Only the project creator or an admin may delete the project", "Forbidden"
            )

        # REQ-5-008: A project may be soft-deleted only with no tickets and no non-creator memberships
        ticket_exists = db.scalar(
            select(TicketModel).where(TicketModel.project_id == project_id).limit(1)
        )
        if ticket_exists:
            raise AppException(
                409, "Cannot delete project while tickets exist in it", "Conflict"
            )

        non_creator_membership = db.scalar(
            select(MembershipModel)
            .where(
                MembershipModel.project_id == project_id,
                MembershipModel.account_id != project.creator_account_id,
            )
            .limit(1)
        )
        if non_creator_membership:
            raise AppException(
                409,
                "Cannot delete project while non-creator memberships exist in it",
                "Conflict",
            )

        project.is_deleted = True
        project.active = False
        db.commit()


class MembershipController:
    @staticmethod
    def list_memberships(
        db: Session,
        current_user: UserModel,
        project_id: int,
        active: Optional[bool],
        limit: int,
        after_id: Optional[int],
    ) -> MembershipListResponse:
        ProjectController.get_project(db, current_user, project_id)

        query = select(MembershipModel).where(MembershipModel.project_id == project_id)
        if active is not None:
            query = query.where(MembershipModel.active == active)

        if after_id is not None:
            if after_id <= 0:
                raise AppException(400, "after_id must be positive", "Bad Request")
            query = query.where(MembershipModel.id > after_id)

        query = query.order_by(MembershipModel.id.asc()).limit(limit + 1)
        rows = list(db.scalars(query).all())

        next_after_id = None
        if len(rows) > limit:
            items = rows[:limit]
            next_after_id = items[-1].id
        else:
            items = rows

        return MembershipListResponse(
            items=[MembershipResponse.model_validate(m) for m in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_membership(
        db: Session,
        current_user: UserModel,
        project_id: int,
        payload: MembershipCreateRequest,
    ) -> MembershipModel:
        if project_id <= 0:
            raise AppException(404, f"Project {project_id} not found", "Not Found")

        project = db.scalar(
            select(ProjectModel).where(
                ProjectModel.id == project_id,
                ProjectModel.is_deleted == False,
                ProjectModel.active == True,
            )
        )
        if not project:
            raise AppException(
                404, f"Active project {project_id} not found", "Not Found"
            )

        if (
            current_user.role != RoleEnum.ADMIN.value
            and project.creator_account_id != current_user.id
        ):
            raise AppException(
                403,
                "Only the project creator or an admin may add memberships",
                "Forbidden",
            )

        target_user = db.scalar(select(UserModel).where(UserModel.id == payload.account_id))
        if not target_user:
            raise AppException(404, f"Account {payload.account_id} not found", "Not Found")

        existing_membership = db.scalar(
            select(MembershipModel).where(
                MembershipModel.project_id == project_id,
                MembershipModel.account_id == payload.account_id,
            )
        )
        if existing_membership:
            raise AppException(
                409,
                f"Membership for account {payload.account_id} in project {project_id} already exists",
                "Conflict",
            )

        membership = MembershipModel(
            project_id=project_id,
            account_id=payload.account_id,
            active=True,
        )
        db.add(membership)
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
        if project_id <= 0 or membership_id <= 0:
            raise AppException(404, "Invalid project or membership id", "Not Found")

        project = db.scalar(
            select(ProjectModel).where(
                ProjectModel.id == project_id, ProjectModel.is_deleted == False
            )
        )
        if not project:
            raise AppException(404, f"Project {project_id} not found", "Not Found")

        if (
            current_user.role != RoleEnum.ADMIN.value
            and project.creator_account_id != current_user.id
        ):
            raise AppException(
                403,
                "Only the project creator or an admin may remove memberships",
                "Forbidden",
            )

        membership = db.scalar(
            select(MembershipModel).where(
                MembershipModel.id == membership_id,
                MembershipModel.project_id == project_id,
            )
        )
        if not membership:
            raise AppException(
                404,
                f"Membership {membership_id} not found in project {project_id}",
                "Not Found",
            )

        if not membership.active:
            raise AppException(409, "Membership is already deactivated", "Conflict")

        # Project creator membership cannot be removed
        if membership.account_id == project.creator_account_id:
            raise AppException(
                409,
                "The project creator has an active membership that cannot be removed while the project exists",
                "Conflict",
            )

        # REQ-5-005: An active membership with a TODO, IN_PROGRESS, or REVIEW assignment cannot be deactivated
        active_assignment = db.scalar(
            select(TicketModel)
            .where(
                TicketModel.assignee_membership_id == membership_id,
                TicketModel.status.in_(
                    [
                        TicketStatusEnum.TODO.value,
                        TicketStatusEnum.IN_PROGRESS.value,
                        TicketStatusEnum.REVIEW.value,
                    ]
                ),
            )
            .limit(1)
        )
        if active_assignment:
            raise AppException(
                409,
                "Cannot deactivate membership with active ticket assignments in TODO, IN_PROGRESS, or REVIEW",
                "Conflict",
            )

        membership.active = False
        db.commit()


class TicketController:
    @staticmethod
    def list_tickets(
        db: Session,
        current_user: UserModel,
        project_id: Optional[int],
        assignee_membership_id: Optional[int],
        status_val: Optional[TicketStatusEnum],
        creator_account_id: Optional[int],
        limit: int,
        after_id: Optional[int],
    ) -> TicketListResponse:
        # Creator filter permitted only to admins
        if creator_account_id is not None and current_user.role != RoleEnum.ADMIN.value:
            raise AppException(
                403, "Filtering by creator_account_id is permitted only to admins", "Forbidden"
            )

        query = select(TicketModel).join(
            ProjectModel, TicketModel.project_id == ProjectModel.id
        ).where(ProjectModel.is_deleted == False)

        if project_id is not None:
            if project_id <= 0:
                raise AppException(404, f"Project {project_id} not found", "Not Found")
            project = db.scalar(
                select(ProjectModel).where(
                    ProjectModel.id == project_id, ProjectModel.is_deleted == False
                )
            )
            if not project:
                raise AppException(404, f"Project {project_id} not found", "Not Found")

            if current_user.role != RoleEnum.ADMIN.value:
                membership = db.scalar(
                    select(MembershipModel).where(
                        MembershipModel.project_id == project_id,
                        MembershipModel.account_id == current_user.id,
                        MembershipModel.active == True,
                    )
                )
                if not membership:
                    raise AppException(
                        403, "Access to tickets for project is forbidden", "Forbidden"
                    )
            query = query.where(TicketModel.project_id == project_id)
        else:
            # If project_id not given, user is restricted to projects where user is active member
            if current_user.role != RoleEnum.ADMIN.value:
                member_project_ids = select(MembershipModel.project_id).where(
                    MembershipModel.account_id == current_user.id,
                    MembershipModel.active == True,
                )
                query = query.where(TicketModel.project_id.in_(member_project_ids))

        if assignee_membership_id is not None:
            if assignee_membership_id <= 0:
                raise AppException(404, "Assignee membership not found", "Not Found")
            assignee_exists = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.id == assignee_membership_id
                )
            )
            if not assignee_exists:
                raise AppException(404, "Assignee membership not found", "Not Found")
            query = query.where(TicketModel.assignee_membership_id == assignee_membership_id)

        if status_val is not None:
            query = query.where(TicketModel.status == status_val.value)

        if creator_account_id is not None:
            query = query.where(TicketModel.creator_account_id == creator_account_id)

        if after_id is not None:
            if after_id <= 0:
                raise AppException(400, "after_id must be positive", "Bad Request")
            query = query.where(TicketModel.id > after_id)

        query = query.order_by(TicketModel.id.asc()).limit(limit + 1)
        rows = list(db.scalars(query).all())

        next_after_id = None
        if len(rows) > limit:
            items = rows[:limit]
            next_after_id = items[-1].id
        else:
            items = rows

        return TicketListResponse(
            items=[TicketResponse.model_validate(t) for t in items],
            limit=limit,
            next_after_id=next_after_id,
        )

    @staticmethod
    def create_ticket(
        db: Session, current_user: UserModel, payload: TicketCreateRequest
    ) -> TicketModel:
        if payload.project_id <= 0:
            raise AppException(404, f"Project {payload.project_id} not found", "Not Found")

        project = db.scalar(
            select(ProjectModel).where(
                ProjectModel.id == payload.project_id,
                ProjectModel.is_deleted == False,
                ProjectModel.active == True,
            )
        )
        if not project:
            raise AppException(
                404, f"Active project {payload.project_id} not found", "Not Found"
            )

        if current_user.role != RoleEnum.ADMIN.value:
            mem = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.project_id == payload.project_id,
                    MembershipModel.account_id == current_user.id,
                    MembershipModel.active == True,
                )
            )
            if not mem:
                raise AppException(
                    403, "Active membership required to create tickets", "Forbidden"
                )

        ticket = TicketModel(
            project_id=payload.project_id,
            title=payload.title,
            description=payload.description or "",
            estimated_points=payload.estimated_points or 0,
            creator_account_id=current_user.id,
            assignee_membership_id=None,
            status=TicketStatusEnum.TODO.value,
        )
        db.add(ticket)
        db.commit()
        db.refresh(ticket)
        return ticket

    @staticmethod
    def get_ticket(db: Session, current_user: UserModel, ticket_id: int) -> TicketModel:
        if ticket_id <= 0:
            raise AppException(404, f"Ticket {ticket_id} not found", "Not Found")

        ticket = db.scalar(select(TicketModel).where(TicketModel.id == ticket_id))
        if not ticket:
            raise AppException(404, f"Ticket {ticket_id} not found", "Not Found")

        project = db.scalar(
            select(ProjectModel).where(
                ProjectModel.id == ticket.project_id, ProjectModel.is_deleted == False
            )
        )
        if not project:
            raise AppException(404, f"Project {ticket.project_id} not found", "Not Found")

        if current_user.role != RoleEnum.ADMIN.value:
            membership = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.project_id == ticket.project_id,
                    MembershipModel.account_id == current_user.id,
                    MembershipModel.active == True,
                )
            )
            if not membership:
                raise AppException(403, "Access to ticket is forbidden", "Forbidden")

        return ticket

    @staticmethod
    def patch_ticket(
        db: Session,
        current_user: UserModel,
        ticket_id: int,
        payload: TicketPatchRequest,
    ) -> TicketModel:
        if ticket_id <= 0:
            raise AppException(404, f"Ticket {ticket_id} not found", "Not Found")

        ticket = db.scalar(select(TicketModel).where(TicketModel.id == ticket_id))
        if not ticket:
            raise AppException(404, f"Ticket {ticket_id} not found", "Not Found")

        project = db.scalar(
            select(ProjectModel).where(
                ProjectModel.id == ticket.project_id, ProjectModel.is_deleted == False
            )
        )
        if not project:
            raise AppException(404, f"Project {ticket.project_id} not found", "Not Found")

        # Eligibility check: creator of ticket, assignee of ticket, project creator, admin
        is_eligible = False
        if current_user.role == RoleEnum.ADMIN.value:
            is_eligible = True
        elif project.creator_account_id == current_user.id:
            is_eligible = True
        elif ticket.creator_account_id == current_user.id:
            is_eligible = True
        elif ticket.assignee_membership_id is not None:
            assignee_membership = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.id == ticket.assignee_membership_id
                )
            )
            if assignee_membership and assignee_membership.account_id == current_user.id:
                is_eligible = True

        if not is_eligible:
            raise AppException(
                403, "Not authorized to update this ticket", "Forbidden"
            )

        fields_set = payload.model_dump(exclude_unset=True)
        if not fields_set:
            raise AppException(400, "At least one field must be updated", "Bad Request")

        if payload.title is not None:
            ticket.title = payload.title
        if payload.description is not None:
            ticket.description = payload.description
        if payload.estimated_points is not None:
            ticket.estimated_points = payload.estimated_points

        db.commit()
        db.refresh(ticket)
        return ticket

    @staticmethod
    def assign_ticket(
        db: Session,
        current_user: UserModel,
        ticket_id: int,
        payload: TicketAssignRequest,
    ) -> TicketModel:
        if ticket_id <= 0:
            raise AppException(404, f"Ticket {ticket_id} not found", "Not Found")

        ticket = db.scalar(select(TicketModel).where(TicketModel.id == ticket_id))
        if not ticket:
            raise AppException(404, f"Ticket {ticket_id} not found", "Not Found")

        project = db.scalar(
            select(ProjectModel).where(
                ProjectModel.id == ticket.project_id, ProjectModel.is_deleted == False
            )
        )
        if not project:
            raise AppException(404, f"Project {ticket.project_id} not found", "Not Found")

        # REQ-5-024: eligible actors are ticket creator, project creator, or admin
        if (
            current_user.role != RoleEnum.ADMIN.value
            and project.creator_account_id != current_user.id
            and ticket.creator_account_id != current_user.id
        ):
            raise AppException(
                403, "Only ticket creator, project creator, or admin may assign tickets", "Forbidden"
            )

        if payload.assignee_membership_id is None:
            ticket.assignee_membership_id = None
        else:
            target_membership = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.id == payload.assignee_membership_id
                )
            )
            if not target_membership:
                raise AppException(404, "Assignee membership not found", "Not Found")
            if target_membership.project_id != ticket.project_id:
                raise AppException(
                    409, "Assignee membership belongs to a different project", "Conflict"
                )
            if not target_membership.active:
                raise AppException(
                    409, "Assignee membership is inactive", "Conflict"
                )

            ticket.assignee_membership_id = target_membership.id

        db.commit()
        db.refresh(ticket)
        return ticket

    @staticmethod
    def transition_ticket(
        db: Session,
        current_user: UserModel,
        ticket_id: int,
        payload: TicketTransitionRequest,
    ) -> TicketModel:
        if ticket_id <= 0:
            raise AppException(404, f"Ticket {ticket_id} not found", "Not Found")

        ticket = db.scalar(select(TicketModel).where(TicketModel.id == ticket_id))
        if not ticket:
            raise AppException(404, f"Ticket {ticket_id} not found", "Not Found")

        project = db.scalar(
            select(ProjectModel).where(
                ProjectModel.id == ticket.project_id, ProjectModel.is_deleted == False
            )
        )
        if not project:
            raise AppException(404, f"Project {ticket.project_id} not found", "Not Found")

        # REQ-5-025: eligible actor: creator, assignee, project creator, or admin
        is_eligible = False
        if current_user.role == RoleEnum.ADMIN.value:
            is_eligible = True
        elif project.creator_account_id == current_user.id:
            is_eligible = True
        elif ticket.creator_account_id == current_user.id:
            is_eligible = True
        elif ticket.assignee_membership_id is not None:
            assignee_membership = db.scalar(
                select(MembershipModel).where(
                    MembershipModel.id == ticket.assignee_membership_id
                )
            )
            if assignee_membership and assignee_membership.account_id == current_user.id:
                is_eligible = True

        if not is_eligible:
            raise AppException(
                403, "Not authorized to transition this ticket", "Forbidden"
            )

        current_status = ticket.status
        target_status_val = payload.target_status.value

        allowed = ALLOWED_TRANSITIONS.get(current_status, set())
        if target_status_val not in allowed:
            raise AppException(
                409,
                f"Invalid state transition from '{current_status}' to '{target_status_val}'",
                "Conflict",
            )

        ticket.status = target_status_val

        # Record history
        tr = TicketTransitionModel(
            ticket_id=ticket.id,
            from_status=current_status,
            to_status=target_status_val,
            actor_account_id=current_user.id,
            transition_note=payload.transition_note or "",
        )
        db.add(tr)
        db.commit()
        db.refresh(ticket)
        return ticket


# ============================================================================
# FastAPI Application & Routes
# ============================================================================
app = FastAPI(
    title="Kanban Project Management - Tickets, Assignees, and State Transitions",
    version="1.0.0",
)


# ASGI Middleware
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

        # Check route matches to preserve 405 Method Not Allowed with Allow header
        from starlette.routing import Match

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

        headers_dict = {
            k.decode("latin1").lower(): v.decode("latin1")
            for k, v in scope["headers"]
        }
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

            # Replay buffered body
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


# 2. GET /projects
@app.get("/projects", tags=["Projects"], response_model=ProjectListResponse)
def list_projects(
    active: Optional[bool] = None,
    key_prefix: Optional[str] = Query(None, min_length=1, max_length=10),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return ProjectController.list_projects(
        db, current_user, active, key_prefix, limit, after_id
    )


# 3. POST /projects
@app.post(
    "/projects",
    tags=["Projects"],
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_project(
    payload: ProjectCreateRequest,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = ProjectController.create_project(db, current_user, payload)
    response.headers["Location"] = f"/projects/{project.id}"
    return ProjectResponse.model_validate(project)


# 4. GET /projects/{project_id}
@app.get("/projects/{project_id}", tags=["Projects"], response_model=ProjectResponse)
def get_project(
    project_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = ProjectController.get_project(db, current_user, project_id)
    return ProjectResponse.model_validate(project)


# 5. PATCH /projects/{project_id}
@app.patch("/projects/{project_id}", tags=["Projects"], response_model=ProjectResponse)
def patch_project(
    project_id: int,
    payload: ProjectPatchRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    project = ProjectController.patch_project(db, current_user, project_id, payload)
    return ProjectResponse.model_validate(project)


# 6. DELETE /projects/{project_id}
@app.delete(
    "/projects/{project_id}",
    tags=["Projects"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_project(
    project_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ProjectController.delete_project(db, current_user, project_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 7. GET /projects/{project_id}/memberships
@app.get(
    "/projects/{project_id}/memberships",
    tags=["Memberships"],
    response_model=MembershipListResponse,
)
def list_memberships(
    project_id: int,
    active: Optional[bool] = None,
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return MembershipController.list_memberships(
        db, current_user, project_id, active, limit, after_id
    )


# 8. POST /projects/{project_id}/memberships
@app.post(
    "/projects/{project_id}/memberships",
    tags=["Memberships"],
    response_model=MembershipResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_membership(
    project_id: int,
    payload: MembershipCreateRequest,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    membership = MembershipController.create_membership(
        db, current_user, project_id, payload
    )
    response.headers["Location"] = f"/projects/{project_id}/memberships/{membership.id}"
    return MembershipResponse.model_validate(membership)


# 9. DELETE /projects/{project_id}/memberships/{membership_id}
@app.delete(
    "/projects/{project_id}/memberships/{membership_id}",
    tags=["Memberships"],
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_membership(
    project_id: int,
    membership_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    MembershipController.delete_membership(db, current_user, project_id, membership_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 10. GET /tickets
@app.get("/tickets", tags=["Tickets"], response_model=TicketListResponse)
def list_tickets(
    project_id: Optional[int] = Query(None, gt=0),
    assignee_membership_id: Optional[int] = Query(None, gt=0),
    ticket_status: Optional[TicketStatusEnum] = Query(None, alias="status"),
    creator_account_id: Optional[int] = Query(None, gt=0),
    limit: int = Query(20, ge=1, le=100),
    after_id: Optional[int] = Query(None, gt=0),
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return TicketController.list_tickets(
        db,
        current_user,
        project_id,
        assignee_membership_id,
        ticket_status,
        creator_account_id,
        limit,
        after_id,
    )


# 11. POST /tickets
@app.post(
    "/tickets",
    tags=["Tickets"],
    response_model=TicketResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_ticket(
    payload: TicketCreateRequest,
    response: Response,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = TicketController.create_ticket(db, current_user, payload)
    response.headers["Location"] = f"/tickets/{ticket.id}"
    return TicketResponse.model_validate(ticket)


# 12. GET /tickets/{ticket_id}
@app.get("/tickets/{ticket_id}", tags=["Tickets"], response_model=TicketResponse)
def get_ticket(
    ticket_id: int,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = TicketController.get_ticket(db, current_user, ticket_id)
    return TicketResponse.model_validate(ticket)


# 13. PATCH /tickets/{ticket_id}
@app.patch("/tickets/{ticket_id}", tags=["Tickets"], response_model=TicketResponse)
def patch_ticket(
    ticket_id: int,
    payload: TicketPatchRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = TicketController.patch_ticket(db, current_user, ticket_id, payload)
    return TicketResponse.model_validate(ticket)


# 14. POST /tickets/{ticket_id}/assignee
@app.post("/tickets/{ticket_id}/assignee", tags=["Tickets"], response_model=TicketResponse)
def assign_ticket(
    ticket_id: int,
    payload: TicketAssignRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = TicketController.assign_ticket(db, current_user, ticket_id, payload)
    return TicketResponse.model_validate(ticket)


# 15. POST /tickets/{ticket_id}/transitions
@app.post(
    "/tickets/{ticket_id}/transitions",
    tags=["Tickets"],
    response_model=TicketResponse,
)
def transition_ticket(
    ticket_id: int,
    payload: TicketTransitionRequest,
    current_user: UserModel = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    ticket = TicketController.transition_ticket(db, current_user, ticket_id, payload)
    return TicketResponse.model_validate(ticket)
