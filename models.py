import uuid
from datetime import datetime

from sqlalchemy import Column, String, Boolean, DateTime, Integer, Text, JSON, ForeignKey
from sqlalchemy.orm import DeclarativeBase, relationship, Mapped, mapped_column
from sqlalchemy.dialects.postgresql import UUID


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    pass_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    tos_accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Relationships
    tenant = relationship("Tenant", back_populates="user", uselist=False)
    github_link = relationship("GitHubLink", back_populates="user", uselist=False)
    build_jobs = relationship("BuildJob", back_populates="user")


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), unique=True, nullable=False)
    subdomain: Mapped[str] = mapped_column(String(63), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="requested")
    api_secret_enc: Mapped[str] = mapped_column(Text, nullable=True)
    mongo_user: Mapped[str] = mapped_column(String(50), nullable=True)
    mongo_db: Mapped[str] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="tenant")


class GitHubLink(Base):
    __tablename__ = "github_links"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    gh_login: Mapped[str] = mapped_column(String(100), nullable=False)
    access_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[str] = mapped_column(String(200), default="")
    linked_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user = relationship("User", back_populates="github_link")


class BuildJob(Base):
    __tablename__ = "build_jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    gh_repo: Mapped[str] = mapped_column(String(255), nullable=False)
    workflow_run_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    aaps_version: Mapped[str] = mapped_column(String(20), nullable=False)
    build_variant: Mapped[str] = mapped_column(String(20), default="fullRelease")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user = relationship("User", back_populates="build_jobs")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
