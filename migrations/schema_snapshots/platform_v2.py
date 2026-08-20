"""Frozen SQLAlchemy schema at revision 0002_director_platform.

This file is intentionally independent from production_domain.models so historical
revisions remain reproducible as the application domain evolves. The four legacy
timeline embedding columns are frozen as JSON; no pgvector extension is required.
Source model: git commit 94f13ef92b7de76d077107140364a33e07fd61ca.
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
    CHARACTER_MASTER = "CHARACTER_MASTER"
    LOCATION_MASTER = "LOCATION_MASTER"
    PROP_MASTER = "PROP_MASTER"
    KEYFRAME = "KEYFRAME"


class JobStatus(StrEnum):
    NEW = "NEW"
    RESERVED = "RESERVED"
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
    HARD_CONTINUITY = "HARD_CONTINUITY"
    HYBRID = "HYBRID"
    RE_ANCHOR = "RE_ANCHOR"


class ShotStatus(StrEnum):
    DRAFT = "DRAFT"
    PLANNED = "PLANNED"
    READY = "READY"
    QUEUED = "QUEUED"
    GENERATING = "GENERATING"
    VALIDATING = "VALIDATING"
    REPAIRING = "REPAIRING"
    REGENERATING = "REGENERATING"
    USER_REVIEW_REQUIRED = "USER_REVIEW_REQUIRED"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"


class CandidateStatus(StrEnum):
    CREATED = "CREATED"
    GENERATING = "GENERATING"
    VALIDATING = "VALIDATING"
    PASSED = "PASSED"
    SOFT_FAILED = "SOFT_FAILED"
    HARD_FAILED = "HARD_FAILED"
    USER_REVIEW_REQUIRED = "USER_REVIEW_REQUIRED"
    COMMITTED = "COMMITTED"
    REJECTED = "REJECTED"


class QADecision(StrEnum):
    PASS = "PASS"
    SOFT_FAIL = "SOFT_FAIL"
    HARD_FAIL = "HARD_FAIL"
    USER_REVIEW_REQUIRED = "USER_REVIEW_REQUIRED"


class GenerationPolicy(StrEnum):
    TEXT_TO_VIDEO = "TEXT_TO_VIDEO"
    IMAGE_TO_VIDEO = "IMAGE_TO_VIDEO"
    CONTINUE_I2V = "CONTINUE_I2V"
    CONTINUE_V2V = "CONTINUE_V2V"
    HYBRID_REFERENCE = "HYBRID_REFERENCE"
    REANCHOR_CHARACTER = "REANCHOR_CHARACTER"
    REANCHOR_SCENE = "REANCHOR_SCENE"
    REANCHOR_FULL = "REANCHOR_FULL"
    START_END_FRAME = "START_END_FRAME"
    REFERENCE_TO_VIDEO = "REFERENCE_TO_VIDEO"


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


class User(Base, TimestampMixin):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    password_hash: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", nullable=False)


class Workspace(Base, TimestampMixin):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", nullable=False)


class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str | None] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", nullable=False)
    default_aspect_ratio: Mapped[str] = mapped_column(String(20), default="9:16", nullable=False)
    default_provider: Mapped[str] = mapped_column(String(80), default="google_flow", nullable=False)
    default_language: Mapped[str] = mapped_column(String(30), default="zh-CN", nullable=False)
    episodes: Mapped[list[Episode]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Episode(Base, TimestampMixin):
    __tablename__ = "episodes"
    __table_args__ = (UniqueConstraint("project_id", "episode_number", name="uq_episode_number"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    episode_number: Mapped[int] = mapped_column(Integer, nullable=False)
    script_source: Mapped[str] = mapped_column(Text, default="", nullable=False)
    script_structured: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
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
    time_context: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    scene_description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    world_state_id: Mapped[str | None] = mapped_column(ForeignKey("timeline_states.id"))
    lighting_preset_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(40), default="PLANNED", nullable=False)
    episode: Mapped[Episode] = relationship(back_populates="scenes")
    shots: Mapped[list[Shot]] = relationship(back_populates="scene", cascade="all, delete-orphan")


class Shot(Base, TimestampMixin):
    __tablename__ = "shots"
    __table_args__ = (UniqueConstraint("scene_id", "sequence", name="uq_shot_sequence"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scene_id: Mapped[str] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    shot_type: Mapped[str] = mapped_column(String(60), default="MEDIUM", nullable=False)
    duration: Mapped[float] = mapped_column(Float, default=8.0, nullable=False)
    user_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    compiled_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    negative_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    provider: Mapped[str] = mapped_column(String(80), default="google_flow", nullable=False)
    model: Mapped[str] = mapped_column(String(120), default="veo", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="DRAFT", nullable=False)
    previous_shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), nullable=True)
    next_shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), nullable=True)
    input_state_id: Mapped[str | None] = mapped_column(ForeignKey("timeline_states.id"))
    output_state_id: Mapped[str | None] = mapped_column(ForeignKey("timeline_states.id"))
    camera_state_id: Mapped[str | None] = mapped_column(String(36))
    lighting_state_id: Mapped[str | None] = mapped_column(String(36))
    blocking_state_id: Mapped[str | None] = mapped_column(String(36))
    start_frame_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"), nullable=True)
    end_frame_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"), nullable=True)
    output_video_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"), nullable=True)
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"), nullable=True)
    committed_candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_candidates.id"), nullable=True
    )
    continuity_policy: Mapped[str] = mapped_column(String(60), default="HYBRID", nullable=False)
    generation_policy: Mapped[str] = mapped_column(
        String(60), default=GenerationPolicy.TEXT_TO_VIDEO.value, nullable=False
    )
    preferred_provider: Mapped[str] = mapped_column(String(80), default="google_flow", nullable=False)
    preferred_model: Mapped[str] = mapped_column(String(120), default="veo", nullable=False)
    continuity_mode: Mapped[str] = mapped_column(
        String(50), default=ContinuityMode.NONE.value, nullable=False
    )
    scene: Mapped[Scene] = relationship(back_populates="shots")


class TimelineState(Base, TimestampMixin):
    __tablename__ = "timeline_states"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    episode_id: Mapped[str | None] = mapped_column(ForeignKey("episodes.id"), index=True)
    scene_id: Mapped[str | None] = mapped_column(ForeignKey("scenes.id"), index=True)
    shot_id: Mapped[str | None] = mapped_column(String(36), index=True)
    previous_state_id: Mapped[str | None] = mapped_column(ForeignKey("timeline_states.id"))
    state_kind: Mapped[str] = mapped_column(String(40), default="SHOT_INPUT", nullable=False)
    state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    semantic_embedding: Mapped[list[float] | None] = mapped_column(JSON())
    visual_embedding: Mapped[list[float] | None] = mapped_column(JSON())
    camera_embedding: Mapped[list[float] | None] = mapped_column(JSON())
    character_track_embedding: Mapped[list[float] | None] = mapped_column(JSON())


class ShotStateSnapshot(Base, TimestampMixin):
    __tablename__ = "shot_state_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shot_id: Mapped[str] = mapped_column(ForeignKey("shots.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(String(36), index=True)
    timeline_state_id: Mapped[str] = mapped_column(ForeignKey("timeline_states.id"), index=True)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class NarrativeEvent(Base, TimestampMixin):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    episode_id: Mapped[str] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), index=True)
    actor_id: Mapped[str | None] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(240), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(36))
    object_id: Mapped[str | None] = mapped_column(String(36))
    dialogue: Mapped[str] = mapped_column(Text, default="", nullable=False)
    preconditions: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    effects: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    timeline_position: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    source_text: Mapped[str] = mapped_column(Text, default="", nullable=False)


class Character(Base, TimestampMixin):
    __tablename__ = "characters"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    canonical_facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="DRAFT", nullable=False)
    current_identity_version_id: Mapped[str | None] = mapped_column(String(36), index=True)


class CharacterIdentityVersion(Base, TimestampMixin):
    __tablename__ = "character_identity_versions"
    __table_args__ = (UniqueConstraint("character_id", "version", name="uq_character_identity_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    master_asset_id: Mapped[str] = mapped_column(ForeignKey("media_assets.id"), nullable=False)
    front_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    left_profile_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    right_profile_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    three_quarter_left_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    three_quarter_right_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    full_body_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    face_embedding: Mapped[list[float] | None] = mapped_column(JSON)
    appearance_embedding: Mapped[list[float] | None] = mapped_column(JSON)
    hair_signature: Mapped[str] = mapped_column(Text, default="", nullable=False)
    costume_signature: Mapped[str] = mapped_column(Text, default="", nullable=False)
    provider_bindings_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="LOCKED", nullable=False)
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Location(Base, TimestampMixin):
    __tablename__ = "locations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    canonical_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class Prop(Base, TimestampMixin):
    __tablename__ = "props"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    canonical_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class GenerationCandidate(Base, TimestampMixin):
    __tablename__ = "generation_candidates"
    __table_args__ = (UniqueConstraint("shot_id", "attempt_number", name="uq_candidate_attempt"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    shot_id: Mapped[str] = mapped_column(ForeignKey("shots.id", ondelete="CASCADE"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    generation_job_id: Mapped[str | None] = mapped_column(String(36), index=True)
    output_asset_id: Mapped[str | None] = mapped_column(String(36), index=True)
    qa_result_id: Mapped[str | None] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(
        String(40), default=CandidateStatus.CREATED.value, index=True, nullable=False
    )
    accepted_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


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
    parent_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"), index=True)
    generation_candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_candidates.id"), index=True
    )
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
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("generation_candidates.id"), index=True)
    generation_type: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default=JobStatus.NEW.value, index=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    provider_request_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy: Mapped[str] = mapped_column(String(60), default=GenerationPolicy.TEXT_TO_VIDEO.value)
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
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cost_estimate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    actual_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


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


class ProviderProjectBinding(Base, TimestampMixin):
    __tablename__ = "provider_projects"
    __table_args__ = (
        UniqueConstraint("local_project_id", "provider", "provider_account_id", name="uq_provider_project"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    local_project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    provider_account_id: Mapped[str] = mapped_column(ForeignKey("provider_accounts.id"), index=True)
    provider_project_id: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="READY", nullable=False)


class ProviderCharacterBinding(Base, TimestampMixin):
    __tablename__ = "provider_character_bindings"
    __table_args__ = (
        UniqueConstraint(
            "character_identity_version_id", "provider", "provider_account_id", name="uq_provider_character"
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    character_identity_version_id: Mapped[str] = mapped_column(
        ForeignKey("character_identity_versions.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    provider_account_id: Mapped[str] = mapped_column(ForeignKey("provider_accounts.id"), index=True)
    binding_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="READY", nullable=False)


class ProviderInstructionBinding(Base, TimestampMixin):
    __tablename__ = "provider_instruction_bindings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    provider_account_id: Mapped[str] = mapped_column(ForeignKey("provider_accounts.id"), index=True)
    instruction_name: Mapped[str] = mapped_column(String(240), nullable=False)
    provider_instruction_id: Mapped[str] = mapped_column(String(500), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="READY", nullable=False)


class QAResult(Base, TimestampMixin):
    __tablename__ = "qa_results"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("generation_candidates.id", ondelete="CASCADE"), index=True
    )
    profile: Mapped[str] = mapped_column(String(80), default="DIALOGUE", nullable=False)
    level_reached: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    decision: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    overall_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    character_score: Mapped[float | None] = mapped_column(Float)
    scene_score: Mapped[float | None] = mapped_column(Float)
    composition_score: Mapped[float | None] = mapped_column(Float)
    action_score: Mapped[float | None] = mapped_column(Float)
    camera_score: Mapped[float | None] = mapped_column(Float)
    lighting_score: Mapped[float | None] = mapped_column(Float)
    narrative_score: Mapped[float | None] = mapped_column(Float)
    hard_failures: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)


class CostRecord(Base, TimestampMixin):
    __tablename__ = "cost_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("generation_candidates.id"), index=True)
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    duration: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    resolution: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    credits: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    actual_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    retry_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    wasted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class DecisionRecord(Base):
    __tablename__ = "decision_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    decision_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    input_features: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    selected_action: Mapped[str] = mapped_column(String(160), nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    model_version: Mapped[str] = mapped_column(String(80), default="rules-v1", nullable=False)
    policy_version: Mapped[str] = mapped_column(String(80), default="v1", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class Skill(Base, TimestampMixin):
    __tablename__ = "skills"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    category: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", nullable=False)


class SkillVersion(Base, TimestampMixin):
    __tablename__ = "skill_versions"
    __table_args__ = (UniqueConstraint("skill_id", "version", name="uq_skill_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id", ondelete="CASCADE"), index=True)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    input_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    compatible_tasks: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    compatible_models: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    dependencies: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", nullable=False)


class PromptCompilation(Base, TimestampMixin):
    __tablename__ = "prompt_compilations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    user_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    compiled_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(80), default="v1", nullable=False)
    skill_versions: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    diff_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


# Migration revisions import only this stable metadata object.
metadata = Base.metadata
