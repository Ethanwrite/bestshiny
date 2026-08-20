"""Frozen SQLAlchemy schema at revision 0001_platform_v1.

This file is independent from production_domain.models so revision 0001 always
creates the schema originally released at commit a281e91f673cd429fc2c6c929189bb29cc6b7254.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class AssetType(StrEnum):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"
    REFERENCE = "REFERENCE"
    CHARACTER_REFERENCE = "CHARACTER_REFERENCE"
    LOCATION_REFERENCE = "LOCATION_REFERENCE"
    PROP_REFERENCE = "PROP_REFERENCE"
    START_FRAME = "START_FRAME"
    END_FRAME = "END_FRAME"
    GENERATED_FRAME = "GENERATED_FRAME"


class JobStatus(StrEnum):
    NEW = "NEW"
    QUEUED = "QUEUED"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    WORKER_NEEDS_USER_ACTION = "WORKER_NEEDS_USER_ACTION"


class AccountStatus(StrEnum):
    READY = "READY"
    BUSY = "BUSY"
    COOLDOWN = "COOLDOWN"
    DISABLED = "DISABLED"
    EXPIRED = "EXPIRED"


class WorkerStatus(StrEnum):
    READY = "READY"
    BUSY = "BUSY"
    OFFLINE = "OFFLINE"
    NEEDS_USER_ACTION = "NEEDS_USER_ACTION"


class ContinuityMode(StrEnum):
    NONE = "NONE"
    PREVIOUS_END_FRAME = "PREVIOUS_END_FRAME"
    REFERENCE_FRAME = "REFERENCE_FRAME"
    START_END_FRAME = "START_END_FRAME"
    PROVIDER_CONTINUATION = "PROVIDER_CONTINUATION"


class RetryCategory(StrEnum):
    TRANSIENT_NETWORK = "TRANSIENT_NETWORK"
    WORKER_DISCONNECT = "WORKER_DISCONNECT"
    RATE_LIMIT = "RATE_LIMIT"
    CREDENTIAL_EXPIRED = "CREDENTIAL_EXPIRED"
    PROVIDER_BUSY = "PROVIDER_BUSY"
    INVALID_REQUEST = "INVALID_REQUEST"
    CONTENT_REJECTED = "CONTENT_REJECTED"
    PERMANENT_ERROR = "PERMANENT_ERROR"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", nullable=False)
    episodes: Mapped[list[Episode]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Episode(Base, TimestampMixin):
    __tablename__ = "episodes"
    __table_args__ = (UniqueConstraint("project_id", "episode_number", name="uq_episode_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="DRAFT", nullable=False)
    project: Mapped[Project] = relationship(back_populates="episodes")
    scenes: Mapped[list[Scene]] = relationship(back_populates="episode", cascade="all, delete-orphan")


class Scene(Base, TimestampMixin):
    __tablename__ = "scenes"
    __table_args__ = (UniqueConstraint("episode_id", "sequence", name="uq_scene_sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), index=True)
    location_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    episode: Mapped[Episode] = relationship(back_populates="scenes")
    shots: Mapped[list[Shot]] = relationship(back_populates="scene", cascade="all, delete-orphan")


class Shot(Base, TimestampMixin):
    __tablename__ = "shots"
    __table_args__ = (UniqueConstraint("scene_id", "sequence", name="uq_shot_sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scene_id: Mapped[str] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    duration: Mapped[float] = mapped_column(Float, default=8.0, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    negative_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    provider: Mapped[str] = mapped_column(String(80), default="google_flow", nullable=False)
    model: Mapped[str] = mapped_column(String(120), default="veo", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="DRAFT", nullable=False)
    previous_shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), nullable=True)
    start_frame_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"), nullable=True)
    end_frame_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"), nullable=True)
    output_video_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"), nullable=True)
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"), nullable=True)
    continuity_mode: Mapped[str] = mapped_column(
        String(50), default=ContinuityMode.NONE.value, nullable=False
    )
    scene: Mapped[Scene] = relationship(back_populates="shots")


class MediaAsset(Base, TimestampMixin):
    __tablename__ = "media_assets"
    __table_args__ = (
        UniqueConstraint("project_id", "sha256", "asset_type", name="uq_asset_project_hash_type"),
        Index("ix_asset_provider_media", "provider", "provider_media_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    asset_type: Mapped[str] = mapped_column(String(50), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    local_path: Mapped[str | None] = mapped_column(String(1000))
    public_url: Mapped[str | None] = mapped_column(String(2000))
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration: Mapped[float | None] = mapped_column(Float)
    provider: Mapped[str | None] = mapped_column(String(80))
    provider_media_id: Mapped[str | None] = mapped_column(String(500))
    character_id: Mapped[str | None] = mapped_column(String(36))
    scene_id: Mapped[str | None] = mapped_column(ForeignKey("scenes.id"))
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ProviderCredential(Base, TimestampMixin):
    __tablename__ = "provider_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ProviderAccount(Base, TimestampMixin):
    __tablename__ = "provider_accounts"
    __table_args__ = (UniqueConstraint("provider", "account_identifier", name="uq_provider_account"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    account_identifier: Mapped[str] = mapped_column(String(320), nullable=False)
    tier: Mapped[str] = mapped_column(String(50), default="FREE", nullable=False)
    credits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=AccountStatus.READY.value, index=True, nullable=False
    )
    image_capacity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    video_capacity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    image_inflight: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    video_inflight: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pending_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    credential_id: Mapped[str | None] = mapped_column(ForeignKey("provider_credentials.id"))
    proxy_id: Mapped[str | None] = mapped_column(String(36))
    worker_id: Mapped[str | None] = mapped_column(String(36), index=True)
    supported_models: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class BrowserWorker(Base, TimestampMixin):
    __tablename__ = "browser_workers"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("provider_accounts.id"), index=True)
    connection_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=WorkerStatus.READY.value, index=True, nullable=False
    )
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    credits: Mapped[int | None] = mapped_column(Integer)
    current_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_jobs: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class GenerationJob(Base, TimestampMixin):
    __tablename__ = "generation_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    generation_type: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default=JobStatus.NEW.value, index=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_job_id: Mapped[str | None] = mapped_column(String(500), index=True)
    account_id: Mapped[str | None] = mapped_column(ForeignKey("provider_accounts.id"), index=True)
    worker_id: Mapped[str | None] = mapped_column(ForeignKey("browser_workers.id"), index=True)
    output_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    retry_category: Mapped[str | None] = mapped_column(String(60))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submission_state: Mapped[str] = mapped_column(String(40), default="NOT_SENT", nullable=False)
    safe_to_retry: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GenerationIdempotency(Base, TimestampMixin):
    __tablename__ = "generation_idempotency"
    __table_args__ = (UniqueConstraint("key", name="uq_generation_idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    key: Mapped[str] = mapped_column(String(250), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_job_id: Mapped[str] = mapped_column(ForeignKey("generation_jobs.id"), nullable=False)
    provider_job_id: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(40), default="PROCESSING", nullable=False)
    result_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))


class GenerationEvent(Base):
    __tablename__ = "generation_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_job_id: Mapped[str] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class WorkerCommand(Base, TimestampMixin):
    __tablename__ = "worker_commands"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    worker_id: Mapped[str] = mapped_column(ForeignKey("browser_workers.id", ondelete="CASCADE"), index=True)
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    message_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True, nullable=False)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaProviderBinding(Base, TimestampMixin):
    __tablename__ = "media_provider_bindings"
    __table_args__ = (
        UniqueConstraint("asset_id", "provider", "account_id", name="uq_asset_provider_account"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("media_assets.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    account_id: Mapped[str] = mapped_column(ForeignKey("provider_accounts.id"), index=True)
    provider_media_id: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="READY", nullable=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# Migration revisions import only this stable metadata object.
metadata = Base.metadata
