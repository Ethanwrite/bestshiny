from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    DDL,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    false,
    text,
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


class AssetKind(StrEnum):
    """Logical production assets shared by manual and automated workflows."""

    CHARACTER = "CHARACTER"
    SCENE = "SCENE"
    PRODUCT = "PRODUCT"
    PROP = "PROP"
    WARDROBE = "WARDROBE"
    VEHICLE = "VEHICLE"
    CREATURE = "CREATURE"
    VOICE = "VOICE"
    STYLE = "STYLE"
    REFERENCE = "REFERENCE"


class AssetVersionStatus(StrEnum):
    DRAFT = "DRAFT"
    READY = "READY"
    REJECTED = "REJECTED"


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


class ProviderProjectBindingStatus(StrEnum):
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    MIGRATION_REQUIRED = "MIGRATION_REQUIRED"
    MIGRATING = "MIGRATING"
    DISABLED = "DISABLED"
    FAILED = "FAILED"


class FlowMigrationStatus(StrEnum):
    PLANNED = "PLANNED"
    USER_REVIEW_REQUIRED = "USER_REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    MIGRATING = "MIGRATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class FlowMigrationVerificationStatus(StrEnum):
    PENDING = "PENDING"
    USER_REVIEW_REQUIRED = "USER_REVIEW_REQUIRED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


class CredentialStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INVALID = "INVALID"
    ROTATION_REQUIRED = "ROTATION_REQUIRED"
    REVOKED = "REVOKED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class TimelineTransitionType(StrEnum):
    CONTINUOUS = "CONTINUOUS"
    SCENE_CUT = "SCENE_CUT"
    TIME_JUMP = "TIME_JUMP"
    FLASHBACK = "FLASHBACK"
    FLASH_FORWARD = "FLASH_FORWARD"
    MONTAGE = "MONTAGE"
    DREAM = "DREAM"
    LOCATION_CHANGE = "LOCATION_CHANGE"
    EXPLICIT_RESET = "EXPLICIT_RESET"


class CharacterStateProposalKind(StrEnum):
    INITIALIZE = "INITIALIZE"
    NARRATIVE = "NARRATIVE"
    EVIDENCE_DERIVED = "EVIDENCE_DERIVED"
    IDENTITY_REBASE = "IDENTITY_REBASE"


class CharacterStateProposalSource(StrEnum):
    RULES = "RULES"
    LLM = "LLM"
    HUMAN = "HUMAN"
    VISUAL_EVIDENCE = "VISUAL_EVIDENCE"


class CharacterStateValidationStage(StrEnum):
    POLICY = "POLICY"
    VISUAL = "VISUAL"
    HUMAN_OVERRIDE = "HUMAN_OVERRIDE"


class CharacterStateDecision(StrEnum):
    PASS = "PASS"
    REJECT = "REJECT"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class CharacterStateValidatorKind(StrEnum):
    RULE_ENGINE = "RULE_ENGINE"
    VLM = "VLM"
    HUMAN = "HUMAN"


class CharacterStateCommitActor(StrEnum):
    SYSTEM = "SYSTEM"
    HUMAN = "HUMAN"


class BillingEvidenceSource(StrEnum):
    VERIFIED_PROVIDER = "VERIFIED_PROVIDER"
    ESTIMATED = "ESTIMATED"
    RECONCILED_MANUAL = "RECONCILED_MANUAL"
    UNKNOWN = "UNKNOWN"


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


class WorkspaceMembership(Base, TimestampMixin):
    __tablename__ = "workspace_memberships"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_workspace_membership_user"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(40), default="VIEWER", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", nullable=False)


class AuthSession(Base, TimestampMixin):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent: Mapped[str] = mapped_column(String(500), default="", nullable=False)


class Workspace(Base, TimestampMixin):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint("credit_balance >= 0", name="ck_workspace_credit_balance"),
        CheckConstraint("max_storage_bytes > 0", name="ck_workspace_max_storage_positive"),
        CheckConstraint("used_storage_bytes >= 0", name="ck_workspace_storage_used_nonnegative"),
        CheckConstraint("reserved_storage_bytes >= 0", name="ck_workspace_storage_reserved_nonnegative"),
        CheckConstraint(
            "used_storage_bytes + reserved_storage_bytes <= max_storage_bytes",
            name="ck_workspace_storage_capacity",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", nullable=False)
    plan_tier: Mapped[str] = mapped_column(String(40), default="FREE", nullable=False)
    credit_balance: Mapped[int] = mapped_column(Integer, default=50, nullable=False)
    max_storage_bytes: Mapped[int] = mapped_column(
        BigInteger,
        default=5 * 1024 * 1024 * 1024,
        nullable=False,
    )
    used_storage_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reserved_storage_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class WorkspaceCreditEntry(Base, TimestampMixin):
    """Current state of one server-priced generation credit reservation."""

    __tablename__ = "workspace_credit_entries"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_credit_entry_project_key"),
        UniqueConstraint("generation_job_id", name="uq_credit_entry_generation_job"),
        CheckConstraint("credits > 0", name="ck_workspace_credit_entry_positive"),
        CheckConstraint("balance_after >= 0", name="ck_workspace_credit_entry_balance"),
        CheckConstraint("settled_credits >= 0", name="ck_credit_entry_settled_nonnegative"),
        CheckConstraint("refunded_credits >= 0", name="ck_credit_entry_refunded_nonnegative"),
        CheckConstraint(
            "settled_credits + refunded_credits <= credits",
            name="ck_credit_entry_allocation",
        ),
        CheckConstraint("version > 0", name="ck_credit_entry_version_positive"),
        CheckConstraint(
            "status IN ('RESERVED', 'SETTLED', 'REFUNDED', 'RECONCILIATION_REQUIRED')",
            name="ck_credit_entry_status",
        ),
        CheckConstraint(
            "(status = 'RESERVED' AND settled_credits = 0 AND refunded_credits = 0 "
            "AND settled_at IS NULL AND refunded_at IS NULL "
            "AND reconciliation_required_at IS NULL) OR "
            "(status = 'RECONCILIATION_REQUIRED' AND settled_credits = 0 "
            "AND refunded_credits = 0 AND settled_at IS NULL AND refunded_at IS NULL "
            "AND reconciliation_required_at IS NOT NULL "
            "AND reconciliation_reason IS NOT NULL) OR "
            "(status = 'SETTLED' AND settled_credits = credits AND refunded_credits = 0 "
            "AND settled_at IS NOT NULL AND refunded_at IS NULL) OR "
            "(status = 'REFUNDED' AND settled_credits = 0 AND refunded_credits = credits "
            "AND refunded_at IS NOT NULL AND settled_at IS NULL)",
            name="ck_credit_entry_state_allocation",
        ),
        Index("ix_workspace_credit_entries_status", "status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    generation_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL")
    )
    idempotency_key: Mapped[str] = mapped_column(String(250), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    settled_credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    refunded_credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="RESERVED", nullable=False)
    reason: Mapped[str] = mapped_column(String(120), default="GENERATION_RESERVED", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciliation_required_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciliation_reason: Mapped[str | None] = mapped_column(String(240))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class WorkspaceCreditEvent(Base):
    """Append-only audit fact emitted for every wallet lifecycle transition."""

    __tablename__ = "workspace_credit_events"
    __table_args__ = (
        UniqueConstraint("credit_entry_id", "event_key", name="uq_credit_event_entry_key"),
        CheckConstraint("credits >= 0", name="ck_credit_event_credits_nonnegative"),
        CheckConstraint("balance_after >= 0", name="ck_credit_event_balance_nonnegative"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    credit_entry_id: Mapped[str] = mapped_column(
        ForeignKey("workspace_credit_entries.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    generation_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="SET NULL"), index=True
    )
    event_key: Mapped[str] = mapped_column(String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    balance_delta: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(240), nullable=False)
    actor_type: Mapped[str] = mapped_column(
        String(80), default="SYSTEM", server_default="SYSTEM", nullable=False
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class LegacyWorkspaceClaim(Base, TimestampMixin):
    """Append-only audit record for an explicit legacy-data ownership transfer."""

    __tablename__ = "legacy_workspace_claims"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_legacy_workspace_claim_idempotency"),
        UniqueConstraint("legacy_user_id", name="uq_legacy_workspace_claim_legacy_user"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    legacy_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    target_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    actor_type: Mapped[str] = mapped_column(String(80), nullable=False)
    workspace_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    project_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="COMPLETED", nullable=False)


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
    downstream_state_stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stale_reason: Mapped[str | None] = mapped_column(String(240))
    stale_from_shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
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
    semantic_embedding: Mapped[list[float] | None] = mapped_column(Vector(16).with_variant(JSON(), "sqlite"))
    visual_embedding: Mapped[list[float] | None] = mapped_column(Vector(16).with_variant(JSON(), "sqlite"))
    camera_embedding: Mapped[list[float] | None] = mapped_column(Vector(16).with_variant(JSON(), "sqlite"))
    character_track_embedding: Mapped[list[float] | None] = mapped_column(
        Vector(16).with_variant(JSON(), "sqlite")
    )


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
    __table_args__ = (UniqueConstraint("id", "project_id", name="uq_characters_id_project"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    canonical_facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="DRAFT", nullable=False)
    current_identity_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("character_identity_versions.id", ondelete="SET NULL"), index=True
    )


class CharacterIdentityVersion(Base, TimestampMixin):
    __tablename__ = "character_identity_versions"
    __table_args__ = (
        UniqueConstraint("character_id", "version", name="uq_character_identity_version"),
        UniqueConstraint("id", "character_id", name="uq_character_identity_id_character"),
        CheckConstraint("version > 0", name="ck_character_identity_version_positive"),
    )
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


class CharacterStateVersion(Base, TimestampMixin):
    """Immutable, fully materialized narrative state for one timeline scope."""

    __tablename__ = "character_state_versions"
    __table_args__ = (
        UniqueConstraint(
            "character_id",
            "timeline_scope_key",
            "version",
            name="uq_character_state_version_scope_number",
        ),
        UniqueConstraint("id", "character_id", name="uq_character_state_version_id_character"),
        CheckConstraint("version > 0", name="ck_character_state_version_positive"),
        CheckConstraint(
            "length(timeline_scope_key) > 0",
            name="ck_character_state_version_scope_nonempty",
        ),
        CheckConstraint(
            "length(identity_fingerprint) = 64 AND length(state_hash) = 64",
            name="ck_character_state_version_hash_lengths",
        ),
        CheckConstraint(
            "(previous_state_version_id IS NULL AND previous_state_hash IS NULL) OR "
            "(previous_state_version_id IS NOT NULL AND previous_state_hash IS NOT NULL "
            "AND length(previous_state_hash) = 64)",
            name="ck_character_state_version_previous_hash",
        ),
        CheckConstraint(
            "previous_state_version_id IS NULL OR previous_state_version_id != id",
            name="ck_character_state_version_not_self_parent",
        ),
        CheckConstraint(
            "(source_shot_id IS NULL AND source_candidate_id IS NULL) OR "
            "(source_shot_id IS NOT NULL AND source_candidate_id IS NOT NULL)",
            name="ck_character_state_version_source_pair",
        ),
        ForeignKeyConstraint(
            ["character_id", "project_id"],
            ["characters.id", "characters.project_id"],
            name="fk_character_state_version_character_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["previous_state_version_id", "character_id"],
            ["character_state_versions.id", "character_state_versions.character_id"],
            name="fk_character_state_version_previous_character",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["identity_version_id", "character_id"],
            ["character_identity_versions.id", "character_identity_versions.character_id"],
            name="fk_character_state_version_identity_character",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_character_state_version_scope",
            "project_id",
            "character_id",
            "timeline_scope_key",
            "version",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    character_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    timeline_scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_state_version_id: Mapped[str | None] = mapped_column(String(36), index=True)
    identity_version_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    source_shot_id: Mapped[str | None] = mapped_column(
        ForeignKey("shots.id", ondelete="RESTRICT"), index=True
    )
    source_candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_candidates.id", ondelete="RESTRICT"), index=True
    )
    state_schema_version: Mapped[str] = mapped_column(
        String(80), default="character-state-v1", nullable=False
    )
    narrative_state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    identity_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_state_hash: Mapped[str | None] = mapped_column(String(64))
    state_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)


class CharacterStateDelta(Base, TimestampMixin):
    """Immutable proposal; superseding a proposal always creates a new row."""

    __tablename__ = "character_state_deltas"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key", name="uq_character_state_delta_project_key"),
        UniqueConstraint(
            "candidate_id",
            "character_id",
            "proposal_revision",
            name="uq_character_state_delta_candidate_revision",
        ),
        CheckConstraint("proposal_revision > 0", name="ck_character_state_delta_revision_positive"),
        CheckConstraint("target_version > 0", name="ck_character_state_delta_target_positive"),
        CheckConstraint("length(timeline_scope_key) > 0", name="ck_character_state_delta_scope_nonempty"),
        CheckConstraint("patch_format = 'JSON_PATCH_V1'", name="ck_character_state_delta_patch_format"),
        CheckConstraint(
            "proposal_kind IN ('INITIALIZE', 'NARRATIVE', 'EVIDENCE_DERIVED', 'IDENTITY_REBASE')",
            name="ck_character_state_delta_proposal_kind",
        ),
        CheckConstraint(
            "source_kind IN ('RULES', 'LLM', 'HUMAN', 'VISUAL_EVIDENCE')",
            name="ck_character_state_delta_source_kind",
        ),
        CheckConstraint(
            "length(target_state_hash) = 64 AND length(input_timeline_state_hash) = 64 "
            "AND length(planned_output_timeline_state_hash) = 64",
            name="ck_character_state_delta_hash_lengths",
        ),
        CheckConstraint(
            "(proposal_kind = 'INITIALIZE' AND base_state_version_id IS NULL "
            "AND base_state_hash IS NULL) OR "
            "(proposal_kind != 'INITIALIZE' AND base_state_version_id IS NOT NULL "
            "AND base_state_hash IS NOT NULL AND length(base_state_hash) = 64)",
            name="ck_character_state_delta_base_contract",
        ),
        CheckConstraint(
            "supersedes_delta_id IS NULL OR supersedes_delta_id != id",
            name="ck_character_state_delta_not_self_supersede",
        ),
        CheckConstraint(
            "source_kind NOT IN ('LLM', 'VISUAL_EVIDENCE') OR model_execution_record_id IS NOT NULL",
            name="ck_character_state_delta_model_provenance",
        ),
        CheckConstraint(
            "source_kind != 'HUMAN' OR proposed_by_user_id IS NOT NULL",
            name="ck_character_state_delta_human_provenance",
        ),
        ForeignKeyConstraint(
            ["character_id", "project_id"],
            ["characters.id", "characters.project_id"],
            name="fk_character_state_delta_character_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["base_state_version_id", "character_id"],
            ["character_state_versions.id", "character_state_versions.character_id"],
            name="fk_character_state_delta_base_character",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["identity_version_id", "character_id"],
            ["character_identity_versions.id", "character_identity_versions.character_id"],
            name="fk_character_state_delta_identity_character",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_character_state_delta_scope",
            "project_id",
            "character_id",
            "timeline_scope_key",
            "created_at",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    character_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    timeline_scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    shot_id: Mapped[str] = mapped_column(
        ForeignKey("shots.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("generation_candidates.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    base_state_version_id: Mapped[str | None] = mapped_column(String(36), index=True)
    identity_version_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    input_timeline_state_id: Mapped[str] = mapped_column(
        ForeignKey("timeline_states.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    planned_output_timeline_state_id: Mapped[str] = mapped_column(
        ForeignKey("timeline_states.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    proposal_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    supersedes_delta_id: Mapped[str | None] = mapped_column(
        ForeignKey("character_state_deltas.id", ondelete="RESTRICT"), index=True
    )
    proposal_kind: Mapped[str] = mapped_column(
        String(40), default=CharacterStateProposalKind.NARRATIVE.value, nullable=False
    )
    source_kind: Mapped[str] = mapped_column(
        String(40), default=CharacterStateProposalSource.RULES.value, nullable=False
    )
    patch_format: Mapped[str] = mapped_column(String(40), default="JSON_PATCH_V1", nullable=False)
    patch_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    changed_paths_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    proposed_state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    base_state_hash: Mapped[str | None] = mapped_column(String(64))
    target_state_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    input_timeline_state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    planned_output_timeline_state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    target_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state_schema_version: Mapped[str] = mapped_column(
        String(80), default="character-state-v1", nullable=False
    )
    policy_version: Mapped[str] = mapped_column(
        String(80), default="character-state-policy-v1", nullable=False
    )
    model_execution_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_execution_records.id", ondelete="RESTRICT"), index=True
    )
    proposed_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)


class CharacterStateValidation(Base, TimestampMixin):
    """Immutable policy, visual, or human evidence about one proposed delta."""

    __tablename__ = "character_state_validations"
    __table_args__ = (
        UniqueConstraint(
            "state_delta_id",
            "stage",
            "attempt",
            name="uq_character_state_validation_stage_attempt",
        ),
        CheckConstraint("attempt > 0", name="ck_character_state_validation_attempt_positive"),
        CheckConstraint(
            "stage IN ('POLICY', 'VISUAL', 'HUMAN_OVERRIDE')",
            name="ck_character_state_validation_stage",
        ),
        CheckConstraint(
            "decision IN ('PASS', 'REJECT', 'REVIEW_REQUIRED')",
            name="ck_character_state_validation_decision",
        ),
        CheckConstraint(
            "validator_kind IN ('RULE_ENGINE', 'VLM', 'HUMAN')",
            name="ck_character_state_validation_validator",
        ),
        CheckConstraint(
            "length(validated_target_hash) = 64 AND length(evidence_hash) = 64",
            name="ck_character_state_validation_hash_lengths",
        ),
        CheckConstraint(
            "validator_kind != 'VLM' OR model_execution_record_id IS NOT NULL",
            name="ck_character_state_validation_model_provenance",
        ),
        CheckConstraint(
            "validator_kind != 'HUMAN' OR validated_by_user_id IS NOT NULL",
            name="ck_character_state_validation_human_provenance",
        ),
        CheckConstraint(
            "stage != 'POLICY' OR validator_kind = 'RULE_ENGINE'",
            name="ck_character_state_validation_policy_rules",
        ),
        CheckConstraint(
            "stage != 'HUMAN_OVERRIDE' OR validator_kind = 'HUMAN'",
            name="ck_character_state_validation_override_human",
        ),
        Index("ix_character_state_validation_delta", "state_delta_id", "stage", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    state_delta_id: Mapped[str] = mapped_column(
        ForeignKey("character_state_deltas.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    decision: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    validator_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    model_execution_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("model_execution_records.id", ondelete="RESTRICT"), index=True
    )
    qa_result_id: Mapped[str | None] = mapped_column(
        ForeignKey("qa_results.id", ondelete="RESTRICT"), index=True
    )
    evidence_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT"), index=True
    )
    validated_target_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    violations_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    policy_version: Mapped[str] = mapped_column(
        String(80), default="character-state-policy-v1", nullable=False
    )
    validated_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )


class CharacterStateCommit(Base, TimestampMixin):
    """Append-only adoption record connecting validated evidence to a new state."""

    __tablename__ = "character_state_commits"
    __table_args__ = (
        UniqueConstraint("state_delta_id", name="uq_character_state_commit_delta"),
        UniqueConstraint("to_state_version_id", name="uq_character_state_commit_to_version"),
        UniqueConstraint(
            "candidate_id", "character_id", name="uq_character_state_commit_candidate_character"
        ),
        CheckConstraint("expected_head_version >= 0", name="ck_character_state_commit_head_nonnegative"),
        CheckConstraint(
            "from_state_version_id IS NULL OR from_state_version_id != to_state_version_id",
            name="ck_character_state_commit_distinct_versions",
        ),
        CheckConstraint("length(commit_hash) = 64", name="ck_character_state_commit_hash_length"),
        CheckConstraint("length(trim(reason)) > 0", name="ck_character_state_commit_reason_nonempty"),
        CheckConstraint(
            "commit_actor IN ('SYSTEM', 'HUMAN')",
            name="ck_character_state_commit_actor",
        ),
        CheckConstraint(
            "commit_actor != 'HUMAN' OR committed_by_user_id IS NOT NULL",
            name="ck_character_state_commit_human_provenance",
        ),
        ForeignKeyConstraint(
            ["character_id", "project_id"],
            ["characters.id", "characters.project_id"],
            name="fk_character_state_commit_character_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["from_state_version_id", "character_id"],
            ["character_state_versions.id", "character_state_versions.character_id"],
            name="fk_character_state_commit_from_character",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["to_state_version_id", "character_id"],
            ["character_state_versions.id", "character_state_versions.character_id"],
            name="fk_character_state_commit_to_character",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_character_state_commit_scope",
            "project_id",
            "character_id",
            "timeline_scope_key",
            "created_at",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    character_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    timeline_scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    shot_id: Mapped[str] = mapped_column(
        ForeignKey("shots.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("generation_candidates.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    state_delta_id: Mapped[str] = mapped_column(
        ForeignKey("character_state_deltas.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    from_state_version_id: Mapped[str | None] = mapped_column(String(36), index=True)
    to_state_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    policy_validation_id: Mapped[str] = mapped_column(
        ForeignKey("character_state_validations.id", ondelete="RESTRICT"), nullable=False
    )
    visual_validation_id: Mapped[str] = mapped_column(
        ForeignKey("character_state_validations.id", ondelete="RESTRICT"), nullable=False
    )
    human_validation_id: Mapped[str | None] = mapped_column(
        ForeignKey("character_state_validations.id", ondelete="RESTRICT")
    )
    expected_head_version: Mapped[int] = mapped_column(Integer, nullable=False)
    commit_actor: Mapped[str] = mapped_column(
        String(40), default=CharacterStateCommitActor.SYSTEM.value, nullable=False
    )
    committed_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    commit_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class CharacterStateHead(Base, TimestampMixin):
    """Mutable CAS projection; version and commit rows remain authoritative."""

    __tablename__ = "character_state_heads"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "character_id",
            "timeline_scope_key",
            name="uq_character_state_head_scope",
        ),
        CheckConstraint("lock_version > 0", name="ck_character_state_head_version_positive"),
        CheckConstraint("length(timeline_scope_key) > 0", name="ck_character_state_head_scope_nonempty"),
        ForeignKeyConstraint(
            ["character_id", "project_id"],
            ["characters.id", "characters.project_id"],
            name="fk_character_state_head_character_project",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["state_version_id", "character_id"],
            ["character_state_versions.id", "character_state_versions.character_id"],
            name="fk_character_state_head_version_character",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_character_state_head_scope",
            "project_id",
            "character_id",
            "timeline_scope_key",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    character_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    timeline_scope_key: Mapped[str] = mapped_column(String(160), nullable=False)
    state_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False)


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
        UniqueConstraint(
            "project_id",
            "sha256",
            "asset_type",
            "lineage_key",
            name="uq_media_asset_lineage_hash",
        ),
        Index("ix_asset_provider_media", "provider", "provider_media_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    asset_type: Mapped[str] = mapped_column(String(50), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    lineage_key: Mapped[str] = mapped_column(String(500), default="shared", nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    local_path: Mapped[str | None] = mapped_column(String(1000))
    public_url: Mapped[str | None] = mapped_column(String(2000))
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
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


class Asset(Base, TimestampMixin):
    """A logical, versioned production asset rather than a single media file."""

    __tablename__ = "assets"
    __table_args__ = (Index("ix_assets_project_kind_status", "project_id", "asset_type", "status"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    asset_type: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    canonical_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    canonical_version_id: Mapped[str | None] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", index=True, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)


class AssetVersion(Base, TimestampMixin):
    """Immutable version payload; becoming canonical always requires an explicit promotion."""

    __tablename__ = "asset_versions"
    __table_args__ = (UniqueConstraint("asset_id", "version", name="uq_asset_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(240), default="", nullable=False)
    primary_media_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT"), index=True
    )
    parent_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("asset_versions.id", ondelete="SET NULL"), index=True
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    continuity_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    embedding_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="USER_UPLOAD", index=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=AssetVersionStatus.READY.value, index=True, nullable=False
    )
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)


class AssetVersionMedia(Base, TimestampMixin):
    """Role-labelled media belonging to a logical asset version."""

    __tablename__ = "asset_version_media"
    __table_args__ = (
        UniqueConstraint("asset_version_id", "media_asset_id", "role", name="uq_asset_version_media_role"),
        Index("ix_asset_version_media_version_role", "asset_version_id", "role"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_version_id: Mapped[str] = mapped_column(
        ForeignKey("asset_versions.id", ondelete="CASCADE"), index=True
    )
    media_asset_id: Mapped[str] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT"), index=True
    )
    role: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class AssetCanonicalPromotion(Base, TimestampMixin):
    """Append-only audit record for an explicit canonical-version change."""

    __tablename__ = "asset_canonical_promotions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    from_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("asset_versions.id", ondelete="SET NULL"), index=True
    )
    to_version_id: Mapped[str] = mapped_column(
        ForeignKey("asset_versions.id", ondelete="RESTRICT"), index=True
    )
    promoted_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


def _install_asset_registry_integrity_ddl() -> None:
    """Install DB-side immutability guards for both create_all and migrations.

    Production databases receive equivalent DDL from migration 0008. These table
    events keep ephemeral ``Base.metadata.create_all`` databases honest as well.
    Physical deletion of a logical asset is intentionally rejected while it owns
    versions or promotion history; callers must archive assets instead.
    """

    anchor = AssetCanonicalPromotion.__table__
    sqlite_statements = (
        """CREATE TRIGGER IF NOT EXISTS trg_assets_canonical_same_asset_insert
        BEFORE INSERT ON assets WHEN NEW.canonical_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM asset_versions
            WHERE id = NEW.canonical_version_id AND asset_id = NEW.id
        ) BEGIN SELECT RAISE(ABORT, 'canonical version must belong to the same asset'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_assets_canonical_same_asset_update
        BEFORE UPDATE OF id, canonical_version_id ON assets
        WHEN NEW.canonical_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM asset_versions
            WHERE id = NEW.canonical_version_id AND asset_id = NEW.id
        ) BEGIN SELECT RAISE(ABORT, 'canonical version must belong to the same asset'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_assets_canonical_requires_promotion_update
        BEFORE UPDATE OF canonical_version_id ON assets
        WHEN NOT (NEW.canonical_version_id IS OLD.canonical_version_id) AND (
            NEW.canonical_version_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM asset_canonical_promotions
                WHERE asset_id = NEW.id
                  AND to_version_id = NEW.canonical_version_id
                  AND from_version_id IS OLD.canonical_version_id
                  AND created_at >= OLD.updated_at
            )
        ) BEGIN SELECT RAISE(ABORT, 'canonical change requires a fresh promotion record'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_asset_versions_parent_same_asset_insert
        BEFORE INSERT ON asset_versions WHEN NEW.parent_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM asset_versions
            WHERE id = NEW.parent_version_id AND asset_id = NEW.asset_id
        ) BEGIN SELECT RAISE(ABORT, 'parent version must belong to the same asset'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_asset_promotions_versions_same_asset_insert
        BEFORE INSERT ON asset_canonical_promotions WHEN NOT EXISTS (
            SELECT 1 FROM asset_versions WHERE id = NEW.to_version_id AND asset_id = NEW.asset_id
        ) OR (NEW.from_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM asset_versions WHERE id = NEW.from_version_id AND asset_id = NEW.asset_id
        )) BEGIN SELECT RAISE(ABORT, 'promotion versions must belong to the same asset'); END""",
    )
    for statement in sqlite_statements:
        event.listen(anchor, "after_create", DDL(statement).execute_if(dialect="sqlite"))

    protected_table_names = (
        "asset_versions",
        "asset_version_media",
        "asset_canonical_promotions",
    )
    for table_name in protected_table_names:
        for operation in ("UPDATE", "DELETE"):
            trigger_name = f"trg_{table_name}_append_only_{operation.lower()}"
            message = f"{table_name} is append-only"
            statement = (
                f"CREATE TRIGGER IF NOT EXISTS {trigger_name} BEFORE {operation} ON {table_name} "
                f"BEGIN SELECT RAISE(ABORT, '{message}'); END"
            )
            event.listen(anchor, "after_create", DDL(statement).execute_if(dialect="sqlite"))

    postgres_statements = (
        """CREATE OR REPLACE FUNCTION enforce_asset_registry_consistency()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_TABLE_NAME = 'assets' THEN
                IF NEW.canonical_version_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM asset_versions
                    WHERE id = NEW.canonical_version_id AND asset_id = NEW.id
                ) THEN RAISE EXCEPTION 'canonical version must belong to the same asset'; END IF;
                IF TG_OP = 'UPDATE'
                   AND NEW.canonical_version_id IS DISTINCT FROM OLD.canonical_version_id AND (
                    NEW.canonical_version_id IS NULL OR NOT EXISTS (
                        SELECT 1 FROM asset_canonical_promotions
                        WHERE asset_id = NEW.id
                          AND to_version_id = NEW.canonical_version_id
                          AND from_version_id IS NOT DISTINCT FROM OLD.canonical_version_id
                          AND created_at >= OLD.updated_at
                    )
                ) THEN RAISE EXCEPTION 'canonical change requires a fresh promotion record'; END IF;
            ELSIF TG_TABLE_NAME = 'asset_versions' THEN
                IF NEW.parent_version_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM asset_versions
                    WHERE id = NEW.parent_version_id AND asset_id = NEW.asset_id
                ) THEN RAISE EXCEPTION 'parent version must belong to the same asset'; END IF;
            ELSIF TG_TABLE_NAME = 'asset_canonical_promotions' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM asset_versions
                    WHERE id = NEW.to_version_id AND asset_id = NEW.asset_id
                ) OR (NEW.from_version_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM asset_versions
                    WHERE id = NEW.from_version_id AND asset_id = NEW.asset_id
                )) THEN RAISE EXCEPTION 'promotion versions must belong to the same asset'; END IF;
            END IF;
            RETURN NEW;
        END; $$""",
        """CREATE OR REPLACE FUNCTION enforce_asset_registry_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '%% is append-only', TG_TABLE_NAME
            USING ERRCODE = '23000';
            RETURN OLD;
        END; $$""",
        """CREATE TRIGGER trg_assets_canonical_same_asset
        BEFORE INSERT OR UPDATE OF id, canonical_version_id ON assets FOR EACH ROW
        EXECUTE FUNCTION enforce_asset_registry_consistency()""",
        """CREATE TRIGGER trg_asset_versions_parent_same_asset
        BEFORE INSERT ON asset_versions FOR EACH ROW
        EXECUTE FUNCTION enforce_asset_registry_consistency()""",
        """CREATE TRIGGER trg_asset_promotions_versions_same_asset
        BEFORE INSERT ON asset_canonical_promotions FOR EACH ROW
        EXECUTE FUNCTION enforce_asset_registry_consistency()""",
    )
    for statement in postgres_statements:
        event.listen(anchor, "after_create", DDL(statement).execute_if(dialect="postgresql"))
    for table_name in protected_table_names:
        statement = (
            f"CREATE TRIGGER trg_{table_name}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table_name} FOR EACH ROW "
            "EXECUTE FUNCTION enforce_asset_registry_append_only()"
        )
        event.listen(anchor, "after_create", DDL(statement).execute_if(dialect="postgresql"))


_install_asset_registry_integrity_ddl()


def _install_shot_lineage_ddl() -> None:
    sqlite_check = """(
        NEW.previous_shot_id = NEW.id OR NOT EXISTS (
            SELECT 1
            FROM shots AS previous
            JOIN scenes AS previous_scene ON previous_scene.id = previous.scene_id
            JOIN episodes AS previous_episode ON previous_episode.id = previous_scene.episode_id
            JOIN scenes AS current_scene ON current_scene.id = NEW.scene_id
            JOIN episodes AS current_episode ON current_episode.id = current_scene.episode_id
            WHERE previous.id = NEW.previous_shot_id
              AND previous_episode.project_id = current_episode.project_id
        )
    )"""
    for operation, suffix in (("INSERT", "insert"), ("UPDATE OF scene_id, previous_shot_id", "update")):
        event.listen(
            Shot.__table__,
            "after_create",
            DDL(
                f"""CREATE TRIGGER IF NOT EXISTS trg_shots_previous_same_project_{suffix}
                BEFORE {operation} ON shots
                WHEN NEW.previous_shot_id IS NOT NULL AND {sqlite_check}
                BEGIN SELECT RAISE(ABORT, 'previous shot must belong to the same project'); END"""
            ).execute_if(dialect="sqlite"),
        )

    event.listen(
        Shot.__table__,
        "after_create",
        DDL(
            """CREATE OR REPLACE FUNCTION enforce_shot_previous_same_project()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.previous_shot_id IS NOT NULL AND (
                    NEW.previous_shot_id = NEW.id OR NOT EXISTS (
                        SELECT 1
                        FROM shots AS previous
                        JOIN scenes AS previous_scene ON previous_scene.id = previous.scene_id
                        JOIN episodes AS previous_episode ON previous_episode.id = previous_scene.episode_id
                        JOIN scenes AS current_scene ON current_scene.id = NEW.scene_id
                        JOIN episodes AS current_episode ON current_episode.id = current_scene.episode_id
                        WHERE previous.id = NEW.previous_shot_id
                          AND previous_episode.project_id = current_episode.project_id
                    )
                ) THEN
                    RAISE EXCEPTION 'previous shot must belong to the same project'
                    USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END; $$"""
        ).execute_if(dialect="postgresql"),
    )
    event.listen(
        Shot.__table__,
        "after_create",
        DDL(
            """CREATE TRIGGER trg_shots_previous_same_project
            BEFORE INSERT OR UPDATE OF scene_id, previous_shot_id ON shots
            FOR EACH ROW EXECUTE FUNCTION enforce_shot_previous_same_project()"""
        ).execute_if(dialect="postgresql"),
    )


_install_shot_lineage_ddl()


class ProviderCredential(Base, TimestampMixin):
    __tablename__ = "provider_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(40), default=CredentialStatus.ACTIVE.value, index=True, nullable=False
    )
    status_reason: Mapped[str | None] = mapped_column(String(240))
    redacted_fingerprint: Mapped[str | None] = mapped_column(String(80))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class WorkerAccessCredential(Base, TimestampMixin):
    """Revocable, worker-scoped credential; only the token digest is persisted."""

    __tablename__ = "worker_access_credentials"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_worker_access_credentials_token_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    worker_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkerSocketTicket(Base, TimestampMixin):
    """Short-lived, one-use WebSocket bootstrap ticket derived from a worker credential."""

    __tablename__ = "worker_socket_tickets"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_worker_socket_tickets_token_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    credential_id: Mapped[str] = mapped_column(
        ForeignKey("worker_access_credentials.id", ondelete="CASCADE"), index=True, nullable=False
    )
    worker_id: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class GenerationJob(Base, TimestampMixin):
    __tablename__ = "generation_jobs"
    __table_args__ = (
        CheckConstraint("quoted_credits >= 0", name="ck_generation_job_quoted_credits"),
        CheckConstraint(
            "provider != 'google_flow' OR provider_job_id IS NULL "
            "OR status IN ('COMPLETED', 'CANCELLED', 'FAILED') "
            "OR (account_id IS NOT NULL AND provider_project_id IS NOT NULL)",
            name="ck_generation_flow_poll_identity",
        ),
        Index(
            "uq_generation_flow_poll_identity",
            "provider",
            "account_id",
            "provider_project_id",
            "provider_job_id",
            unique=True,
            sqlite_where=text("provider = 'google_flow' AND provider_job_id IS NOT NULL"),
            postgresql_where=text("provider = 'google_flow' AND provider_job_id IS NOT NULL"),
        ),
    )
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
    provider_project_id: Mapped[str | None] = mapped_column(String(500), index=True)
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
    claim_token: Mapped[str | None] = mapped_column(String(64), index=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    reservation_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cost_estimate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    actual_cost: Mapped[float | None] = mapped_column(Float)
    workspace_credit_required: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    quoted_credits: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)


class GenerationIdempotency(Base, TimestampMixin):
    __tablename__ = "generation_idempotency"
    __table_args__ = (UniqueConstraint("project_id", "key", name="uq_generation_idempotency_project_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
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
    claim_connection_id: Mapped[str | None] = mapped_column(String(100), index=True)
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
    provider_media_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="READY", nullable=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    upload_claim_token: Mapped[str | None] = mapped_column(String(36), index=True)
    upload_claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    upload_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderProjectBinding(Base, TimestampMixin):
    __tablename__ = "provider_projects"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PROVISIONING', 'READY', 'DEGRADED', 'MIGRATION_REQUIRED', "
            "'MIGRATING', 'DISABLED', 'FAILED')",
            name="ck_provider_project_status",
        ),
        CheckConstraint("version > 0", name="ck_provider_project_version"),
        CheckConstraint(
            "status IN ('PROVISIONING', 'MIGRATION_REQUIRED', 'FAILED') OR provider_project_id IS NOT NULL",
            name="ck_provider_project_remote_id",
        ),
        Index(
            "uq_flow_active_local_project",
            "local_project_id",
            "provider",
            unique=True,
            sqlite_where=text(
                "provider = 'google_flow' AND status IN "
                "('PROVISIONING', 'READY', 'DEGRADED', 'MIGRATION_REQUIRED', 'MIGRATING')"
            ),
            postgresql_where=text(
                "provider = 'google_flow' AND status IN "
                "('PROVISIONING', 'READY', 'DEGRADED', 'MIGRATION_REQUIRED', 'MIGRATING')"
            ),
        ),
        Index(
            "uq_non_flow_provider_project_account",
            "local_project_id",
            "provider",
            "provider_account_id",
            unique=True,
            sqlite_where=text("provider != 'google_flow'"),
            postgresql_where=text("provider != 'google_flow'"),
        ),
        Index(
            "uq_flow_remote_project_owner",
            "provider",
            "provider_project_id",
            unique=True,
            sqlite_where=text("provider = 'google_flow' AND provider_project_id IS NOT NULL"),
            postgresql_where=text("provider = 'google_flow' AND provider_project_id IS NOT NULL"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    local_project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    provider_account_id: Mapped[str] = mapped_column(ForeignKey("provider_accounts.id"), index=True)
    provider_project_id: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(
        String(40), default=ProviderProjectBindingStatus.READY.value, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    status_reason: Mapped[str | None] = mapped_column(String(240))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    migration_required_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provisioning_token: Mapped[str | None] = mapped_column(String(64), index=True)
    provisioning_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class FlowMigrationPlan(Base, TimestampMixin):
    __tablename__ = "flow_migration_plans"
    __table_args__ = (
        CheckConstraint(
            "migration_status IN ('PLANNED', 'USER_REVIEW_REQUIRED', 'APPROVED', "
            "'MIGRATING', 'COMPLETED', 'FAILED', 'CANCELLED')",
            name="ck_flow_migration_status",
        ),
        CheckConstraint(
            "verification_status IN ('PENDING', 'USER_REVIEW_REQUIRED', 'VERIFIED', 'FAILED')",
            name="ck_flow_migration_verification",
        ),
        Index(
            "uq_flow_migration_active_binding",
            "source_binding_id",
            unique=True,
            sqlite_where=text(
                "migration_status IN ('PLANNED', 'USER_REVIEW_REQUIRED', 'APPROVED', 'MIGRATING')"
            ),
            postgresql_where=text(
                "migration_status IN ('PLANNED', 'USER_REVIEW_REQUIRED', 'APPROVED', 'MIGRATING')"
            ),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_binding_id: Mapped[str] = mapped_column(
        ForeignKey("provider_projects.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    local_project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_account_id: Mapped[str] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    target_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("provider_accounts.id", ondelete="RESTRICT"), index=True
    )
    source_project_id: Mapped[str | None] = mapped_column(String(500))
    target_project_id: Mapped[str | None] = mapped_column(String(500))
    characters_json: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    instructions_json: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    assets_json: Mapped[list[Any]] = mapped_column(JSON, default=list, nullable=False)
    migration_status: Mapped[str] = mapped_column(
        String(40), default=FlowMigrationStatus.USER_REVIEW_REQUIRED.value, index=True, nullable=False
    )
    verification_status: Mapped[str] = mapped_column(
        String(40),
        default=FlowMigrationVerificationStatus.USER_REVIEW_REQUIRED.value,
        nullable=False,
    )
    trigger_reason: Mapped[str] = mapped_column(String(240), nullable=False)


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
    __table_args__ = (Index("ix_cost_records_generation_job_id", "generation_job_id", unique=True),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    candidate_id: Mapped[str | None] = mapped_column(ForeignKey("generation_candidates.id"), index=True)
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"))
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    duration: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    resolution: Mapped[str] = mapped_column(String(40), default="", nullable=False)
    credits: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    actual_cost: Mapped[float | None] = mapped_column(Float)
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


class PromptRevision(Base, TimestampMixin):
    __tablename__ = "prompt_revisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), index=True)
    mode: Mapped[str] = mapped_column(String(40), default="IMAGE", index=True, nullable=False)
    original_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    corrected_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    detected_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    reference_asset_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    preserved_constraints: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    editable_variables: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    changes_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    corrector_version: Mapped[str] = mapped_column(String(80), nullable=False)


class FeatureFlag(Base, TimestampMixin):
    __tablename__ = "feature_flags"
    __table_args__ = (UniqueConstraint("name", "scope_key", name="uq_feature_flag_name_scope_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    scope_key: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ShotMemory(Base, TimestampMixin):
    __tablename__ = "shot_memories"
    __table_args__ = (Index("ix_shot_memories_scope", "project_id", "layer", "scene_id", "memory_type"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    layer: Mapped[str] = mapped_column(String(8), index=True, nullable=False)
    memory_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    text_content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    image_urls: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    video_urls: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    entity_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    scene_id: Mapped[str | None] = mapped_column(ForeignKey("scenes.id"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    asset_version_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    temporal_position: Mapped[float | None] = mapped_column(Float)
    canonical: Mapped[bool] = mapped_column(Boolean, default=False, index=True, nullable=False)
    # JSON keeps Matryoshka output dimension configurable. A production PostgreSQL
    # deployment may add a matching pgvector expression/index without changing this contract.
    embedding: Mapped[list[float]] = mapped_column(JSON, default=list, nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(Integer, default=512, nullable=False)
    embedding_provider: Mapped[str] = mapped_column(String(80), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(120), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class EvaluationResult(Base, TimestampMixin):
    __tablename__ = "evaluation_results"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    generated_asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"))
    decision: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    overall_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    critical_failure: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scores_json: Mapped[dict[str, float]] = mapped_column(JSON, default=dict, nullable=False)
    checks_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    retry_reasons: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    retry_patch: Mapped[str] = mapped_column(Text, default="", nullable=False)
    evidence_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    evaluator_version: Mapped[str] = mapped_column(String(80), nullable=False)
    judge_provider: Mapped[str] = mapped_column(String(80), default="none", nullable=False)
    judge_model: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    provider: Mapped[str] = mapped_column(String(80), default="", index=True, nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), default="", index=True, nullable=False)


class ModelDefinition(Base, TimestampMixin):
    """Provider-neutral model catalogue entry used by business-layer roles."""

    __tablename__ = "model_definitions"
    __table_args__ = (
        UniqueConstraint("logical_name", name="uq_model_definitions_logical_name"),
        UniqueConstraint(
            "provider",
            "provider_model_id",
            "modality",
            name="uq_model_definitions_provider_model_modality",
        ),
        Index("ix_model_definitions_provider_enabled", "provider", "enabled"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    logical_name: Mapped[str] = mapped_column(String(160), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    modality: Mapped[str] = mapped_column(String(50), nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    quality_tier: Mapped[str] = mapped_column(String(40), default="STANDARD", nullable=False)
    cost_class: Mapped[str] = mapped_column(String(40), default="STANDARD", nullable=False)
    provider_trust_level: Mapped[str] = mapped_column(String(40), default="STANDARD", nullable=False)
    criticality_allowed: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    live_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    context_window: Mapped[int | None] = mapped_column(Integer)
    max_duration: Mapped[float | None] = mapped_column(Float)
    supported_aspect_ratios: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ModelCapabilityProfile(Base, TimestampMixin):
    """Authoritative, persisted capability and manual-quality profile for one model."""

    __tablename__ = "model_capability_profiles"
    __table_args__ = (
        CheckConstraint("max_reference_images >= 0", name="ck_model_capability_max_references"),
        CheckConstraint(
            "min_duration IS NULL OR min_duration > 0",
            name="ck_model_capability_min_duration",
        ),
        CheckConstraint(
            "max_duration IS NULL OR max_duration > 0",
            name="ck_model_capability_max_duration",
        ),
        CheckConstraint(
            "min_duration IS NULL OR max_duration IS NULL OR min_duration <= max_duration",
            name="ck_model_capability_duration_range",
        ),
        CheckConstraint(
            "physics_prior >= 0 AND physics_prior <= 1 "
            "AND identity_prior >= 0 AND identity_prior <= 1 "
            "AND camera_prior >= 0 AND camera_prior <= 1 "
            "AND render_prior >= 0 AND render_prior <= 1 "
            "AND action_prior >= 0 AND action_prior <= 1 "
            "AND dialogue_prior >= 0 AND dialogue_prior <= 1 "
            "AND text_render_prior >= 0 AND text_render_prior <= 1",
            name="ck_model_capability_manual_priors",
        ),
        Index("ix_model_capability_profiles_source", "source"),
    )
    model_definition_id: Mapped[str] = mapped_column(
        ForeignKey("model_definitions.id", ondelete="CASCADE"), primary_key=True
    )
    profile_version: Mapped[str] = mapped_column(String(80), default="1", nullable=False)
    confidence_level: Mapped[str] = mapped_column(String(40), default="initial", nullable=False)
    supported_operations: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    supports_image_generation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_video_generation: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_t2v: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_i2v: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_v2v: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_reference_image: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_multi_reference: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_start_frame: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_end_frame: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_start_end: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_character_reference: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_video_extension: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_camera_instruction: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_audio: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    supports_text_rendering: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_reference_images: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    min_duration: Mapped[float | None] = mapped_column(Float)
    max_duration: Mapped[float | None] = mapped_column(Float)
    supported_aspect_ratios: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    supported_resolutions: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    physics_prior: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    identity_prior: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    camera_prior: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    render_prior: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    action_prior: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    dialogue_prior: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    text_render_prior: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="MANUAL_PRIOR", nullable=False)


class ModelRoleBinding(Base, TimestampMixin):
    """Configurable role-to-model binding, optionally scoped to a plan tier."""

    __tablename__ = "model_role_bindings"
    __table_args__ = (
        UniqueConstraint(
            "role",
            "plan_tier",
            "model_definition_id",
            name="uq_model_role_binding_scope_model",
        ),
        Index(
            "ix_model_role_binding_lookup",
            "role",
            "plan_tier",
            "enabled",
            "priority",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    role: Mapped[str] = mapped_column(String(80), nullable=False)
    plan_tier: Mapped[str] = mapped_column(String(40), default="ALL", nullable=False)
    model_definition_id: Mapped[str] = mapped_column(
        ForeignKey("model_definitions.id", ondelete="CASCADE"), nullable=False
    )
    binding_kind: Mapped[str] = mapped_column(String(40), default="PRIMARY", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ProviderBudget(Base, TimestampMixin):
    """Durable provider-level spend ceiling used by low-trust edge routing."""

    __tablename__ = "provider_budgets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    credit_budget_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    actual_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"), nullable=False)
    reserved_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"), nullable=False)
    routing_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ProviderBudgetUsage(Base, TimestampMixin):
    """Exactly-once reserve/settle audit for a provider task."""

    __tablename__ = "provider_budget_usages"
    __table_args__ = (
        UniqueConstraint("provider", "task_id", name="uq_provider_budget_usage_task"),
        Index("ix_provider_budget_usage_lookup", "provider", "status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    budget_id: Mapped[str] = mapped_column(
        ForeignKey("provider_budgets.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    task_id: Mapped[str] = mapped_column(String(200), nullable=False)
    task_role: Mapped[str] = mapped_column(String(100), nullable=False)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    remaining_budget_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="RESERVED", nullable=False)


class ModelMetric(Base, TimestampMixin):
    __tablename__ = "model_metrics"
    __table_args__ = (
        UniqueConstraint("generation_job_id", "metric_name", name="uq_model_metric_job_name"),
        Index("ix_model_metrics_model_name", "provider", "model_id", "metric_name"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    model_version: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    metric_name: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    value: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ModelBenchmarkResult(Base, TimestampMixin):
    __tablename__ = "model_benchmark_results"
    __table_args__ = (Index("ix_benchmark_model_case", "provider", "model_id", "case_key", "suite_version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    suite_version: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    case_key: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    model_version: Mapped[str] = mapped_column(String(80), nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scores_json: Mapped[dict[str, float]] = mapped_column(JSON, default=dict, nullable=False)
    evidence_asset_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)


class ProductionTrace(Base, TimestampMixin):
    __tablename__ = "production_traces"
    __table_args__ = (Index("ix_production_traces_trace_id", "trace_id", unique=True),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), index=True, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    context_asset_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    retrieved_memory_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    router_scores_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    generation_latency: Mapped[float | None] = mapped_column(Float)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    actual_cost: Mapped[float | None] = mapped_column(Float)
    evaluation_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    retry_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ModelExecutionRecord(Base):
    """Immutable evidence for every role-runtime provider attempt."""

    __tablename__ = "model_execution_records"
    __table_args__ = (
        CheckConstraint("latency_ms >= 0", name="ck_model_execution_latency_nonnegative"),
        CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
            name="ck_model_execution_estimated_cost_nonnegative",
        ),
        CheckConstraint(
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0",
            name="ck_model_execution_actual_cost_nonnegative",
        ),
        Index("ix_model_execution_project_role_created", "project_id", "role", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    role: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model_definition_id: Mapped[str] = mapped_column(
        ForeignKey("model_definitions.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    provider_model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    token_usage_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    cost_source: Mapped[str] = mapped_column(
        String(40), default=BillingEvidenceSource.UNKNOWN.value, nullable=False
    )
    status: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(120))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class EmbeddingEvidence(Base):
    """Vector provenance without persisting the vector in an audit/log table."""

    __tablename__ = "embedding_evidence"
    __table_args__ = (
        CheckConstraint("embedding_dimension > 0", name="ck_embedding_evidence_dimension_positive"),
        CheckConstraint("latency_ms >= 0", name="ck_embedding_evidence_latency_nonnegative"),
        UniqueConstraint(
            "model_execution_record_id",
            "asset_id",
            "input_hash",
            name="uq_embedding_evidence_execution_input",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"), index=True)
    model_definition_id: Mapped[str] = mapped_column(
        ForeignKey("model_definitions.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    model_execution_record_id: Mapped[str] = mapped_column(
        ForeignKey("model_execution_records.id", ondelete="CASCADE"), index=True, nullable=False
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class ProviderBillingEvidence(Base, TimestampMixin):
    """Provider cost/credit fact with an explicit trust source."""

    __tablename__ = "provider_billing_evidence"
    __table_args__ = (
        UniqueConstraint("generation_job_id", "evidence_key", name="uq_billing_evidence_job_key"),
        CheckConstraint(
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0",
            name="ck_billing_evidence_actual_nonnegative",
        ),
        CheckConstraint(
            "estimated_cost_usd IS NULL OR estimated_cost_usd >= 0",
            name="ck_billing_evidence_estimated_nonnegative",
        ),
        CheckConstraint(
            "provider_credits IS NULL OR provider_credits >= 0",
            name="ck_billing_evidence_credits_nonnegative",
        ),
        Index("ix_billing_evidence_provider_model", "provider", "model", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_job_id: Mapped[str] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    cost_record_id: Mapped[str | None] = mapped_column(ForeignKey("cost_records.id"), index=True)
    evidence_key: Mapped[str] = mapped_column(String(160), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(
        String(40), default=BillingEvidenceSource.UNKNOWN.value, index=True, nullable=False
    )
    provider_reference: Mapped[str | None] = mapped_column(String(500))
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    provider_credits: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DecisionOutcomeRecord(Base):
    """Durable training/evaluation join across decision, attempt, QA, cost, and user outcome."""

    __tablename__ = "decision_outcome_records"
    __table_args__ = (
        UniqueConstraint("candidate_id", name="uq_decision_outcome_candidate"),
        Index("ix_decision_outcome_provider_model", "provider", "model", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    shot_id: Mapped[str] = mapped_column(ForeignKey("shots.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("generation_candidates.id", ondelete="CASCADE"), nullable=False
    )
    generation_job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id"), index=True)
    qa_result_id: Mapped[str | None] = mapped_column(ForeignKey("qa_results.id"), index=True)
    continuity_decision: Mapped[str] = mapped_column(String(80), nullable=False)
    generation_policy: Mapped[str] = mapped_column(String(80), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    shot_features_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    qa_result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    user_outcome: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    billing_source: Mapped[str] = mapped_column(
        String(40), default=BillingEvidenceSource.UNKNOWN.value, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class TimelineTransition(Base, TimestampMixin):
    __tablename__ = "timeline_transitions"
    __table_args__ = (
        UniqueConstraint("target_shot_id", name="uq_timeline_transition_target_shot"),
        Index("ix_timeline_transition_project_type", "project_id", "transition_type"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_shot_id: Mapped[str | None] = mapped_column(ForeignKey("shots.id"), index=True)
    target_shot_id: Mapped[str] = mapped_column(
        ForeignKey("shots.id", ondelete="CASCADE"), index=True, nullable=False
    )
    transition_type: Mapped[str] = mapped_column(
        String(40), default=TimelineTransitionType.CONTINUOUS.value, nullable=False
    )
    branch_key: Mapped[str | None] = mapped_column(String(120))
    reconciliation_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class LiveCanaryPermit(Base, TimestampMixin):
    __tablename__ = "live_canary_permits"
    __table_args__ = (
        CheckConstraint("max_requests > 0", name="ck_live_canary_max_requests_positive"),
        CheckConstraint("max_cost_usd > 0", name="ck_live_canary_max_cost_positive"),
        CheckConstraint("used_requests >= 0", name="ck_live_canary_used_requests_nonnegative"),
        CheckConstraint("reserved_cost_usd >= 0", name="ck_live_canary_reserved_cost_nonnegative"),
        CheckConstraint("actual_cost_usd >= 0", name="ck_live_canary_actual_cost_nonnegative"),
        Index("ix_live_canary_lookup", "provider", "model", "status", "expires_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    max_requests: Mapped[int] = mapped_column(Integer, nullable=False)
    max_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    used_requests: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"), nullable=False)
    actual_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal("0"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    purpose: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ACTIVE", index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class LiveCanaryUsage(Base, TimestampMixin):
    __tablename__ = "live_canary_usages"
    __table_args__ = (
        UniqueConstraint("permit_id", "idempotency_key", name="uq_live_canary_usage_key"),
        CheckConstraint("estimated_cost_usd >= 0", name="ck_live_canary_usage_estimated_nonnegative"),
        CheckConstraint(
            "actual_cost_usd IS NULL OR actual_cost_usd >= 0",
            name="ck_live_canary_usage_actual_nonnegative",
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    permit_id: Mapped[str] = mapped_column(
        ForeignKey("live_canary_permits.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    status: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    evidence_reference: Mapped[str | None] = mapped_column(String(500))


class RunAPIBenchmark(Base, TimestampMixin):
    __tablename__ = "runapi_benchmarks"
    __table_args__ = (UniqueConstraint("task_id", name="uq_runapi_benchmark_task"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(String(200), nullable=False)
    task_type: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_quality: Mapped[float | None] = mapped_column(Float)
    fact_lock_pass: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    fallback_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    actual_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    user_acceptance: Mapped[bool | None] = mapped_column(Boolean)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class StorageReservation(Base, TimestampMixin):
    __tablename__ = "storage_reservations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_storage_reservation_key"),
        CheckConstraint("reserved_bytes > 0", name="ck_storage_reservation_bytes_positive"),
        Index("ix_storage_reservation_status", "workspace_id", "status"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="RESERVED", nullable=False)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"), index=True)
    storage_key: Mapped[str | None] = mapped_column(String(1000))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    __table_args__ = (UniqueConstraint("token_hash", name="uq_password_reset_token_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_ip_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True, nullable=False
    )


class AuthLoginThrottle(Base, TimestampMixin):
    __tablename__ = "auth_login_throttles"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


def _install_character_state_integrity_ddl() -> None:
    """Install the create-all equivalent of migration 0028's state ledger guards."""

    # Metadata-level DDL runs only after every table is available.  This is
    # required because the head/commit guards deliberately inspect each other.
    anchor = Base.metadata
    protected_tables = (
        "character_identity_versions",
        "character_state_versions",
        "character_state_deltas",
        "character_state_validations",
        "character_state_commits",
    )
    for table_name in protected_tables:
        for operation in ("UPDATE", "DELETE"):
            trigger_name = f"trg_{table_name}_immutable_{operation.lower()}"
            event.listen(
                anchor,
                "after_create",
                DDL(
                    f"CREATE TRIGGER IF NOT EXISTS {trigger_name} BEFORE {operation} "
                    f"ON {table_name} BEGIN SELECT RAISE(ABORT, '{table_name} is append-only'); END"
                ).execute_if(dialect="sqlite"),
            )

    sqlite_statements = (
        """CREATE TRIGGER IF NOT EXISTS trg_character_identity_pointer_insert
        BEFORE INSERT ON characters WHEN NEW.current_identity_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM character_identity_versions AS identity
            WHERE identity.id = NEW.current_identity_version_id
              AND identity.character_id = NEW.id AND identity.status = 'LOCKED'
        ) BEGIN SELECT RAISE(ABORT, 'current identity must be a locked version of the character'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_identity_pointer_update
        BEFORE UPDATE OF id, current_identity_version_id ON characters
        WHEN NEW.current_identity_version_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM character_identity_versions AS identity
            WHERE identity.id = NEW.current_identity_version_id
              AND identity.character_id = NEW.id AND identity.status = 'LOCKED'
        ) BEGIN SELECT RAISE(ABORT, 'current identity must be a locked version of the character'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_canonical_facts_frozen
        BEFORE UPDATE OF canonical_facts ON characters
        WHEN OLD.current_identity_version_id IS NOT NULL
          AND NEW.canonical_facts IS NOT OLD.canonical_facts
        BEGIN SELECT RAISE(ABORT, 'confirmed canonical facts are immutable'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_state_version_consistency
        BEFORE INSERT ON character_state_versions WHEN
            json_type(NEW.narrative_state_json) != 'object'
            OR json_type(NEW.narrative_state_json, '$.identity') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.canonical_identity') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.face') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.body_proportions') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.canonical_hair') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.canonical_outfit') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.identity_embedding_id') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.canonical_asset_id') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.face') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.hair') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.body') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.body_proportions') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.canonical_hair') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.canonical_outfit') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.outfit.type') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.outfit.design') IS NOT NULL
            OR json_type(NEW.narrative_state_json, '$.appearance.outfit.color') IS NOT NULL
            OR NOT EXISTS (
                SELECT 1 FROM character_identity_versions AS identity
                WHERE identity.id = NEW.identity_version_id
                  AND identity.character_id = NEW.character_id AND identity.status = 'LOCKED'
            )
            OR (NEW.previous_state_version_id IS NULL AND NEW.version != 1)
            OR (NEW.previous_state_version_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM character_state_versions AS previous
                WHERE previous.id = NEW.previous_state_version_id
                  AND previous.character_id = NEW.character_id
                  AND previous.state_hash = NEW.previous_state_hash
                  AND ((previous.timeline_scope_key = NEW.timeline_scope_key
                        AND NEW.version = previous.version + 1)
                       OR (previous.timeline_scope_key != NEW.timeline_scope_key AND NEW.version = 1))
            ))
            OR (NEW.source_candidate_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM generation_candidates AS candidate
                JOIN shots AS shot ON shot.id = candidate.shot_id
                JOIN scenes AS scene ON scene.id = shot.scene_id
                JOIN episodes AS episode ON episode.id = scene.episode_id
                WHERE candidate.id = NEW.source_candidate_id
                  AND shot.id = NEW.source_shot_id AND episode.project_id = NEW.project_id
            ))
        BEGIN SELECT RAISE(ABORT, 'character state version is inconsistent'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_state_delta_consistency
        BEFORE INSERT ON character_state_deltas WHEN
            json_type(NEW.patch_json) != 'array'
            OR json_type(NEW.changed_paths_json) != 'array'
            OR json_type(NEW.proposed_state_json) != 'object'
            OR json_type(NEW.proposed_state_json, '$.identity') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.canonical_identity') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.face') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.body_proportions') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.canonical_hair') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.canonical_outfit') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.identity_embedding_id') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.canonical_asset_id') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.face') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.hair') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.body') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.body_proportions') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.canonical_hair') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.canonical_outfit') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.outfit.type') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.outfit.design') IS NOT NULL
            OR json_type(NEW.proposed_state_json, '$.appearance.outfit.color') IS NOT NULL
            OR EXISTS (
                SELECT 1 FROM json_each(NEW.patch_json) AS patch
                WHERE CASE
                   WHEN patch.type != 'object' THEN 1
                   WHEN json_type(patch.value, '$.path') IS NOT 'text' THEN 1
                   WHEN json_extract(patch.value, '$.path') = ''
                   OR json_extract(patch.value, '$.path') = '/'
                   OR json_extract(patch.value, '$.path') NOT LIKE '/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance'
                   OR json_extract(patch.value, '$.path') = '/appearance/outfit'
                   OR json_extract(patch.value, '$.path') = '/identity'
                   OR json_extract(patch.value, '$.path') LIKE '/identity/%%'
                   OR json_extract(patch.value, '$.path') = '/canonical_identity'
                   OR json_extract(patch.value, '$.path') LIKE '/canonical_identity/%%'
                   OR json_extract(patch.value, '$.path') = '/face'
                   OR json_extract(patch.value, '$.path') LIKE '/face/%%'
                   OR json_extract(patch.value, '$.path') = '/body_proportions'
                   OR json_extract(patch.value, '$.path') LIKE '/body_proportions/%%'
                   OR json_extract(patch.value, '$.path') = '/canonical_hair'
                   OR json_extract(patch.value, '$.path') LIKE '/canonical_hair/%%'
                   OR json_extract(patch.value, '$.path') = '/canonical_outfit'
                   OR json_extract(patch.value, '$.path') LIKE '/canonical_outfit/%%'
                   OR json_extract(patch.value, '$.path') = '/identity_embedding_id'
                   OR json_extract(patch.value, '$.path') LIKE '/identity_embedding_id/%%'
                   OR json_extract(patch.value, '$.path') = '/canonical_asset_id'
                   OR json_extract(patch.value, '$.path') LIKE '/canonical_asset_id/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/face'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/face/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/hair'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/hair/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/body'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/body/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/body_proportions'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/body_proportions/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/canonical_hair'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/canonical_hair/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/canonical_outfit'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/canonical_outfit/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/outfit/type'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/outfit/type/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/outfit/design'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/outfit/design/%%'
                   OR json_extract(patch.value, '$.path') = '/appearance/outfit/color'
                   OR json_extract(patch.value, '$.path') LIKE '/appearance/outfit/color/%%'
                   THEN 1 ELSE 0 END
            )
            OR NOT EXISTS (
                SELECT 1 FROM generation_candidates AS candidate
                JOIN shots AS shot ON shot.id = candidate.shot_id
                JOIN scenes AS scene ON scene.id = shot.scene_id
                JOIN episodes AS episode ON episode.id = scene.episode_id
                WHERE candidate.id = NEW.candidate_id AND shot.id = NEW.shot_id
                  AND episode.project_id = NEW.project_id
            )
            OR NOT EXISTS (
                SELECT 1 FROM timeline_states AS input_state
                WHERE input_state.id = NEW.input_timeline_state_id
                  AND input_state.project_id = NEW.project_id
                  AND input_state.state_kind = 'SHOT_INPUT'
                  AND (input_state.shot_id IS NULL OR input_state.shot_id = NEW.shot_id)
            )
            OR NOT EXISTS (
                SELECT 1 FROM timeline_states AS output_state
                WHERE output_state.id = NEW.planned_output_timeline_state_id
                  AND output_state.project_id = NEW.project_id
                  AND output_state.state_kind = 'SHOT_OUTPUT'
                  AND (output_state.shot_id IS NULL OR output_state.shot_id = NEW.shot_id)
            )
            OR NOT EXISTS (
                SELECT 1 FROM shots AS shot WHERE shot.id = NEW.shot_id
                  AND shot.input_state_id = NEW.input_timeline_state_id
                  AND shot.output_state_id = NEW.planned_output_timeline_state_id
            )
            OR (NEW.proposal_kind NOT IN ('INITIALIZE', 'IDENTITY_REBASE') AND NOT EXISTS (
                SELECT 1 FROM character_state_versions AS base
                WHERE base.id = NEW.base_state_version_id
                  AND base.identity_version_id = NEW.identity_version_id
                  AND base.state_hash = NEW.base_state_hash
            ))
            OR (NEW.supersedes_delta_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM character_state_deltas AS prior
                WHERE prior.id = NEW.supersedes_delta_id
                  AND prior.project_id = NEW.project_id
                  AND prior.character_id = NEW.character_id
                  AND prior.candidate_id = NEW.candidate_id
                  AND prior.proposal_revision < NEW.proposal_revision
            ))
            OR (NEW.model_execution_record_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM model_execution_records AS execution
                WHERE execution.id = NEW.model_execution_record_id
                  AND execution.project_id = NEW.project_id
            ))
        BEGIN SELECT RAISE(ABORT, 'character state delta is inconsistent'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_state_validation_consistency
        BEFORE INSERT ON character_state_validations WHEN
            json_type(NEW.observed_state_json) != 'object'
            OR json_type(NEW.evidence_json) != 'object'
            OR json_type(NEW.violations_json) != 'array'
            OR NOT EXISTS (
                SELECT 1 FROM character_state_deltas AS delta
                WHERE delta.id = NEW.state_delta_id AND delta.project_id = NEW.project_id
                  AND delta.target_state_hash = NEW.validated_target_hash
            )
            OR (NEW.qa_result_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM qa_results AS qa
                JOIN character_state_deltas AS delta ON delta.id = NEW.state_delta_id
                WHERE qa.id = NEW.qa_result_id AND qa.candidate_id = delta.candidate_id
            ))
            OR (NEW.evidence_asset_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM media_assets AS asset
                WHERE asset.id = NEW.evidence_asset_id AND asset.project_id = NEW.project_id
            ))
            OR (NEW.model_execution_record_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM model_execution_records AS execution
                WHERE execution.id = NEW.model_execution_record_id
                  AND execution.project_id = NEW.project_id
            ))
        BEGIN SELECT RAISE(ABORT, 'character state validation is inconsistent'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_state_commit_consistency
        BEFORE INSERT ON character_state_commits WHEN
            NOT EXISTS (
                SELECT 1 FROM character_state_deltas AS delta
                WHERE delta.id = NEW.state_delta_id AND delta.project_id = NEW.project_id
                  AND delta.character_id = NEW.character_id
                  AND delta.timeline_scope_key = NEW.timeline_scope_key
                  AND delta.shot_id = NEW.shot_id AND delta.candidate_id = NEW.candidate_id
                  AND delta.base_state_version_id IS NEW.from_state_version_id
            )
            OR NOT EXISTS (
                SELECT 1 FROM character_state_deltas AS delta
                JOIN character_state_versions AS target ON target.id = NEW.to_state_version_id
                WHERE delta.id = NEW.state_delta_id AND target.project_id = NEW.project_id
                  AND target.character_id = NEW.character_id
                  AND target.timeline_scope_key = NEW.timeline_scope_key
                  AND target.version = delta.target_version
                  AND target.previous_state_version_id IS NEW.from_state_version_id
                  AND target.identity_version_id = delta.identity_version_id
                  AND target.source_shot_id = NEW.shot_id
                  AND target.source_candidate_id = NEW.candidate_id
                  AND target.state_hash = delta.target_state_hash
            )
            OR NOT EXISTS (
                SELECT 1 FROM character_state_validations AS validation
                WHERE validation.id = NEW.policy_validation_id
                  AND validation.state_delta_id = NEW.state_delta_id
                  AND validation.stage = 'POLICY' AND validation.decision = 'PASS'
            )
            OR NOT EXISTS (
                SELECT 1 FROM character_state_validations AS visual
                WHERE visual.id = NEW.visual_validation_id
                  AND visual.state_delta_id = NEW.state_delta_id
                  AND visual.stage = 'VISUAL'
                  AND (visual.decision = 'PASS' OR (
                      visual.decision = 'REVIEW_REQUIRED'
                      AND NEW.human_validation_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM character_state_validations AS human
                          WHERE human.id = NEW.human_validation_id
                            AND human.state_delta_id = NEW.state_delta_id
                            AND human.stage = 'HUMAN_OVERRIDE' AND human.decision = 'PASS'
                      )
                  ))
            )
            OR (NEW.human_validation_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM character_state_validations AS validation
                WHERE validation.id = NEW.human_validation_id
                  AND validation.state_delta_id = NEW.state_delta_id
                  AND validation.stage = 'HUMAN_OVERRIDE' AND validation.decision = 'PASS'
            ))
            OR NOT EXISTS (
                SELECT 1 FROM generation_candidates AS candidate
                JOIN shots AS shot ON shot.id = candidate.shot_id
                WHERE candidate.id = NEW.candidate_id AND candidate.status = 'COMMITTED'
                  AND shot.id = NEW.shot_id AND shot.committed_candidate_id = NEW.candidate_id
            )
            OR ((SELECT COUNT(*) FROM character_state_heads AS head
                 WHERE head.project_id = NEW.project_id AND head.character_id = NEW.character_id
                   AND head.timeline_scope_key = NEW.timeline_scope_key) = 0
                AND NEW.expected_head_version != 0)
            OR ((SELECT COUNT(*) FROM character_state_heads AS head
                 WHERE head.project_id = NEW.project_id AND head.character_id = NEW.character_id
                   AND head.timeline_scope_key = NEW.timeline_scope_key) > 0
                AND NOT EXISTS (
                    SELECT 1 FROM character_state_heads AS head
                    WHERE head.project_id = NEW.project_id AND head.character_id = NEW.character_id
                      AND head.timeline_scope_key = NEW.timeline_scope_key
                      AND head.state_version_id IS NEW.from_state_version_id
                      AND head.lock_version = NEW.expected_head_version
                ))
        BEGIN SELECT RAISE(ABORT, 'character state commit is inconsistent'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_state_head_insert
        BEFORE INSERT ON character_state_heads WHEN
            NEW.lock_version != 1 OR NOT EXISTS (
                SELECT 1 FROM character_state_versions AS version
                JOIN character_state_commits AS commit_row
                  ON commit_row.to_state_version_id = version.id
                WHERE version.id = NEW.state_version_id
                  AND version.project_id = NEW.project_id
                  AND version.character_id = NEW.character_id
                  AND version.timeline_scope_key = NEW.timeline_scope_key
                  AND version.version = 1
                  AND commit_row.expected_head_version = 0
            )
        BEGIN SELECT RAISE(ABORT, 'character state head requires an initial commit'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_state_head_update
        BEFORE UPDATE ON character_state_heads WHEN
            NEW.id != OLD.id OR NEW.project_id != OLD.project_id
            OR NEW.character_id != OLD.character_id
            OR NEW.timeline_scope_key != OLD.timeline_scope_key
            OR NEW.lock_version != OLD.lock_version + 1
            OR NOT EXISTS (
                SELECT 1 FROM character_state_versions AS version
                JOIN character_state_commits AS commit_row
                  ON commit_row.to_state_version_id = version.id
                WHERE version.id = NEW.state_version_id
                  AND version.project_id = NEW.project_id
                  AND version.character_id = NEW.character_id
                  AND version.timeline_scope_key = NEW.timeline_scope_key
                  AND version.version = NEW.lock_version
                  AND commit_row.from_state_version_id = OLD.state_version_id
                  AND commit_row.expected_head_version = OLD.lock_version
            )
        BEGIN SELECT RAISE(ABORT, 'character state head update requires a fresh commit'); END""",
        """CREATE TRIGGER IF NOT EXISTS trg_character_state_head_delete
        BEFORE DELETE ON character_state_heads
        BEGIN SELECT RAISE(ABORT, 'character state heads cannot be deleted'); END""",
    )
    for statement in sqlite_statements:
        event.listen(anchor, "after_create", DDL(statement).execute_if(dialect="sqlite"))

    postgres_statements = (
        """CREATE OR REPLACE FUNCTION enforce_character_state_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '%% is append-only', TG_TABLE_NAME USING ERRCODE = '23000';
            RETURN OLD;
        END; $$""",
        """CREATE OR REPLACE FUNCTION enforce_character_identity_boundary()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.current_identity_version_id IS NOT NULL
               AND NEW.canonical_facts::jsonb IS DISTINCT FROM OLD.canonical_facts::jsonb THEN
                RAISE EXCEPTION 'confirmed canonical facts are immutable' USING ERRCODE = '23514';
            END IF;
            IF NEW.current_identity_version_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM character_identity_versions AS identity
                WHERE identity.id = NEW.current_identity_version_id
                  AND identity.character_id = NEW.id AND identity.status = 'LOCKED'
            ) THEN
                RAISE EXCEPTION 'current identity must be a locked version of the character'
                USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END; $$""",
        """CREATE OR REPLACE FUNCTION enforce_character_state_consistency()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE head_count integer;
        BEGIN
            IF TG_TABLE_NAME = 'character_state_versions' THEN
                IF json_typeof(NEW.narrative_state_json) <> 'object'
                   OR NEW.narrative_state_json::jsonb ?| ARRAY[
                       'identity', 'canonical_identity', 'face', 'body_proportions',
                       'canonical_hair', 'canonical_outfit', 'identity_embedding_id',
                       'canonical_asset_id'
                   ]
                   OR NEW.narrative_state_json::jsonb #> '{appearance,face}' IS NOT NULL
                   OR NEW.narrative_state_json::jsonb #> '{appearance,hair}' IS NOT NULL
                   OR NEW.narrative_state_json::jsonb #> '{appearance,body}' IS NOT NULL
                   OR NEW.narrative_state_json::jsonb #> '{appearance,body_proportions}' IS NOT NULL
                   OR NEW.narrative_state_json::jsonb #> '{appearance,canonical_hair}' IS NOT NULL
                   OR NEW.narrative_state_json::jsonb #> '{appearance,canonical_outfit}' IS NOT NULL
                   OR NEW.narrative_state_json::jsonb #> '{appearance,outfit,type}' IS NOT NULL
                   OR NEW.narrative_state_json::jsonb #> '{appearance,outfit,design}' IS NOT NULL
                   OR NEW.narrative_state_json::jsonb #> '{appearance,outfit,color}' IS NOT NULL
                   OR NOT EXISTS (
                       SELECT 1 FROM character_identity_versions AS identity
                       WHERE identity.id = NEW.identity_version_id
                         AND identity.character_id = NEW.character_id AND identity.status = 'LOCKED'
                   )
                   OR (NEW.previous_state_version_id IS NULL AND NEW.version <> 1)
                   OR (NEW.previous_state_version_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM character_state_versions AS previous
                       WHERE previous.id = NEW.previous_state_version_id
                         AND previous.character_id = NEW.character_id
                         AND previous.state_hash = NEW.previous_state_hash
                         AND ((previous.timeline_scope_key = NEW.timeline_scope_key
                               AND NEW.version = previous.version + 1)
                              OR (previous.timeline_scope_key <> NEW.timeline_scope_key
                                  AND NEW.version = 1))
                   ))
                   OR (NEW.source_candidate_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM generation_candidates AS candidate
                       JOIN shots AS shot ON shot.id = candidate.shot_id
                       JOIN scenes AS scene ON scene.id = shot.scene_id
                       JOIN episodes AS episode ON episode.id = scene.episode_id
                       WHERE candidate.id = NEW.source_candidate_id
                         AND shot.id = NEW.source_shot_id AND episode.project_id = NEW.project_id
                   )) THEN
                    RAISE EXCEPTION 'character state version is inconsistent' USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'character_state_deltas' THEN
                IF json_typeof(NEW.patch_json) <> 'array'
                   OR json_typeof(NEW.changed_paths_json) <> 'array'
                   OR json_typeof(NEW.proposed_state_json) <> 'object'
                   OR NEW.proposed_state_json::jsonb ?| ARRAY[
                       'identity', 'canonical_identity', 'face', 'body_proportions',
                       'canonical_hair', 'canonical_outfit', 'identity_embedding_id',
                       'canonical_asset_id'
                   ]
                   OR NEW.proposed_state_json::jsonb #> '{appearance,face}' IS NOT NULL
                   OR NEW.proposed_state_json::jsonb #> '{appearance,hair}' IS NOT NULL
                   OR NEW.proposed_state_json::jsonb #> '{appearance,body}' IS NOT NULL
                   OR NEW.proposed_state_json::jsonb #> '{appearance,body_proportions}' IS NOT NULL
                   OR NEW.proposed_state_json::jsonb #> '{appearance,canonical_hair}' IS NOT NULL
                   OR NEW.proposed_state_json::jsonb #> '{appearance,canonical_outfit}' IS NOT NULL
                   OR NEW.proposed_state_json::jsonb #> '{appearance,outfit,type}' IS NOT NULL
                   OR NEW.proposed_state_json::jsonb #> '{appearance,outfit,design}' IS NOT NULL
                   OR NEW.proposed_state_json::jsonb #> '{appearance,outfit,color}' IS NOT NULL
                   OR EXISTS (
                       SELECT 1 FROM json_array_elements(NEW.patch_json) AS patch
                       WHERE json_typeof(patch) <> 'object'
                           OR COALESCE(json_typeof(patch->'path'), '') <> 'string'
                           OR COALESCE(patch->>'path', '') IN ('', '/', '/appearance', '/appearance/outfit')
                           OR COALESCE(patch->>'path', '') !~ '^/'
                           OR COALESCE(patch->>'path', '') ~
                           '^/(identity|canonical_identity|face|body_proportions|canonical_hair|canonical_outfit|identity_embedding_id|canonical_asset_id)(/|$)'
                           OR COALESCE(patch->>'path', '') ~
                           '^/appearance/(face|hair|body|body_proportions|canonical_hair|canonical_outfit)(/|$)'
                           OR COALESCE(patch->>'path', '') ~
                           '^/appearance/outfit/(type|design|color)(/|$)'
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM generation_candidates AS candidate
                       JOIN shots AS shot ON shot.id = candidate.shot_id
                       JOIN scenes AS scene ON scene.id = shot.scene_id
                       JOIN episodes AS episode ON episode.id = scene.episode_id
                       WHERE candidate.id = NEW.candidate_id AND shot.id = NEW.shot_id
                         AND episode.project_id = NEW.project_id
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM timeline_states AS input_state
                       WHERE input_state.id = NEW.input_timeline_state_id
                         AND input_state.project_id = NEW.project_id
                         AND input_state.state_kind = 'SHOT_INPUT'
                         AND (input_state.shot_id IS NULL OR input_state.shot_id = NEW.shot_id)
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM timeline_states AS output_state
                       WHERE output_state.id = NEW.planned_output_timeline_state_id
                         AND output_state.project_id = NEW.project_id
                         AND output_state.state_kind = 'SHOT_OUTPUT'
                         AND (output_state.shot_id IS NULL OR output_state.shot_id = NEW.shot_id)
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM shots AS shot WHERE shot.id = NEW.shot_id
                         AND shot.input_state_id = NEW.input_timeline_state_id
                         AND shot.output_state_id = NEW.planned_output_timeline_state_id
                   )
                   OR (NEW.proposal_kind NOT IN ('INITIALIZE', 'IDENTITY_REBASE') AND NOT EXISTS (
                       SELECT 1 FROM character_state_versions AS base
                       WHERE base.id = NEW.base_state_version_id
                         AND base.identity_version_id = NEW.identity_version_id
                         AND base.state_hash = NEW.base_state_hash
                   ))
                   OR (NEW.supersedes_delta_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM character_state_deltas AS prior
                       WHERE prior.id = NEW.supersedes_delta_id
                         AND prior.project_id = NEW.project_id
                         AND prior.character_id = NEW.character_id
                         AND prior.candidate_id = NEW.candidate_id
                         AND prior.proposal_revision < NEW.proposal_revision
                   ))
                   OR (NEW.model_execution_record_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM model_execution_records AS execution
                       WHERE execution.id = NEW.model_execution_record_id
                         AND execution.project_id = NEW.project_id
                   )) THEN
                    RAISE EXCEPTION 'character state delta is inconsistent' USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'character_state_validations' THEN
                IF json_typeof(NEW.observed_state_json) <> 'object'
                   OR json_typeof(NEW.evidence_json) <> 'object'
                   OR json_typeof(NEW.violations_json) <> 'array'
                   OR NOT EXISTS (
                       SELECT 1 FROM character_state_deltas AS delta
                       WHERE delta.id = NEW.state_delta_id AND delta.project_id = NEW.project_id
                         AND delta.target_state_hash = NEW.validated_target_hash
                   )
                   OR (NEW.qa_result_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM qa_results AS qa
                       JOIN character_state_deltas AS delta ON delta.id = NEW.state_delta_id
                       WHERE qa.id = NEW.qa_result_id AND qa.candidate_id = delta.candidate_id
                   ))
                   OR (NEW.evidence_asset_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM media_assets AS asset
                       WHERE asset.id = NEW.evidence_asset_id AND asset.project_id = NEW.project_id
                   ))
                   OR (NEW.model_execution_record_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM model_execution_records AS execution
                       WHERE execution.id = NEW.model_execution_record_id
                         AND execution.project_id = NEW.project_id
                   )) THEN
                    RAISE EXCEPTION 'character state validation is inconsistent' USING ERRCODE = '23514';
                END IF;
            ELSIF TG_TABLE_NAME = 'character_state_commits' THEN
                IF NOT EXISTS (
                       SELECT 1 FROM character_state_deltas AS delta
                       WHERE delta.id = NEW.state_delta_id AND delta.project_id = NEW.project_id
                         AND delta.character_id = NEW.character_id
                         AND delta.timeline_scope_key = NEW.timeline_scope_key
                         AND delta.shot_id = NEW.shot_id AND delta.candidate_id = NEW.candidate_id
                         AND delta.base_state_version_id IS NOT DISTINCT FROM NEW.from_state_version_id
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM character_state_deltas AS delta
                       JOIN character_state_versions AS target ON target.id = NEW.to_state_version_id
                       WHERE delta.id = NEW.state_delta_id AND target.project_id = NEW.project_id
                         AND target.character_id = NEW.character_id
                         AND target.timeline_scope_key = NEW.timeline_scope_key
                         AND target.version = delta.target_version
                         AND target.previous_state_version_id IS NOT DISTINCT FROM NEW.from_state_version_id
                         AND target.identity_version_id = delta.identity_version_id
                         AND target.source_shot_id = NEW.shot_id
                         AND target.source_candidate_id = NEW.candidate_id
                         AND target.state_hash = delta.target_state_hash
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM character_state_validations AS validation
                       WHERE validation.id = NEW.policy_validation_id
                         AND validation.state_delta_id = NEW.state_delta_id
                         AND validation.stage = 'POLICY' AND validation.decision = 'PASS'
                   )
                   OR NOT EXISTS (
                       SELECT 1 FROM character_state_validations AS visual
                       WHERE visual.id = NEW.visual_validation_id
                         AND visual.state_delta_id = NEW.state_delta_id
                         AND visual.stage = 'VISUAL'
                         AND (visual.decision = 'PASS' OR (
                             visual.decision = 'REVIEW_REQUIRED'
                             AND NEW.human_validation_id IS NOT NULL
                             AND EXISTS (
                                 SELECT 1 FROM character_state_validations AS human
                                 WHERE human.id = NEW.human_validation_id
                                   AND human.state_delta_id = NEW.state_delta_id
                                   AND human.stage = 'HUMAN_OVERRIDE' AND human.decision = 'PASS'
                             )
                         ))
                   )
                   OR (NEW.human_validation_id IS NOT NULL AND NOT EXISTS (
                       SELECT 1 FROM character_state_validations AS validation
                       WHERE validation.id = NEW.human_validation_id
                         AND validation.state_delta_id = NEW.state_delta_id
                         AND validation.stage = 'HUMAN_OVERRIDE' AND validation.decision = 'PASS'
                   ))
                   OR NOT EXISTS (
                       SELECT 1 FROM generation_candidates AS candidate
                       JOIN shots AS shot ON shot.id = candidate.shot_id
                       WHERE candidate.id = NEW.candidate_id AND candidate.status = 'COMMITTED'
                         AND shot.id = NEW.shot_id AND shot.committed_candidate_id = NEW.candidate_id
                   ) THEN
                    RAISE EXCEPTION 'character state commit is inconsistent' USING ERRCODE = '23514';
                END IF;
                SELECT COUNT(*) INTO head_count FROM character_state_heads AS head
                WHERE head.project_id = NEW.project_id AND head.character_id = NEW.character_id
                  AND head.timeline_scope_key = NEW.timeline_scope_key;
                IF (head_count = 0 AND NEW.expected_head_version <> 0)
                   OR (head_count > 0 AND NOT EXISTS (
                       SELECT 1 FROM character_state_heads AS head
                       WHERE head.project_id = NEW.project_id AND head.character_id = NEW.character_id
                         AND head.timeline_scope_key = NEW.timeline_scope_key
                         AND head.state_version_id IS NOT DISTINCT FROM NEW.from_state_version_id
                         AND head.lock_version = NEW.expected_head_version
                   )) THEN
                    RAISE EXCEPTION 'character state commit head fence is stale' USING ERRCODE = '40001';
                END IF;
            ELSIF TG_TABLE_NAME = 'character_state_heads' THEN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'character state heads cannot be deleted' USING ERRCODE = '23000';
                ELSIF TG_OP = 'INSERT' THEN
                    IF NEW.lock_version <> 1 OR NOT EXISTS (
                        SELECT 1 FROM character_state_versions AS version
                        JOIN character_state_commits AS commit_row
                          ON commit_row.to_state_version_id = version.id
                        WHERE version.id = NEW.state_version_id
                          AND version.project_id = NEW.project_id
                          AND version.character_id = NEW.character_id
                          AND version.timeline_scope_key = NEW.timeline_scope_key
                          AND version.version = 1 AND commit_row.expected_head_version = 0
                    ) THEN
                        RAISE EXCEPTION 'character state head requires an initial commit'
                        USING ERRCODE = '23514';
                    END IF;
                ELSIF NEW.id IS DISTINCT FROM OLD.id
                   OR NEW.project_id IS DISTINCT FROM OLD.project_id
                   OR NEW.character_id IS DISTINCT FROM OLD.character_id
                   OR NEW.timeline_scope_key IS DISTINCT FROM OLD.timeline_scope_key
                   OR NEW.lock_version <> OLD.lock_version + 1
                   OR NOT EXISTS (
                       SELECT 1 FROM character_state_versions AS version
                       JOIN character_state_commits AS commit_row
                         ON commit_row.to_state_version_id = version.id
                       WHERE version.id = NEW.state_version_id
                         AND version.project_id = NEW.project_id
                         AND version.character_id = NEW.character_id
                         AND version.timeline_scope_key = NEW.timeline_scope_key
                         AND version.version = NEW.lock_version
                         AND commit_row.from_state_version_id = OLD.state_version_id
                         AND commit_row.expected_head_version = OLD.lock_version
                   ) THEN
                    RAISE EXCEPTION 'character state head update requires a fresh commit'
                    USING ERRCODE = '40001';
                END IF;
            END IF;
            RETURN NEW;
        END; $$""",
        """CREATE OR REPLACE TRIGGER trg_character_identity_boundary
        BEFORE INSERT OR UPDATE OF id, current_identity_version_id, canonical_facts ON characters
        FOR EACH ROW EXECUTE FUNCTION enforce_character_identity_boundary()""",
    )
    for statement in postgres_statements:
        event.listen(anchor, "after_create", DDL(statement).execute_if(dialect="postgresql"))
    for table_name in protected_tables:
        event.listen(
            anchor,
            "after_create",
            DDL(
                f"CREATE OR REPLACE TRIGGER trg_{table_name}_immutable "
                f"BEFORE UPDATE OR DELETE ON {table_name} "
                "FOR EACH ROW EXECUTE FUNCTION enforce_character_state_append_only()"
            ).execute_if(dialect="postgresql"),
        )
    for table_name in (
        "character_state_versions",
        "character_state_deltas",
        "character_state_validations",
        "character_state_commits",
    ):
        event.listen(
            anchor,
            "after_create",
            DDL(
                f"CREATE OR REPLACE TRIGGER trg_{table_name}_consistency BEFORE INSERT ON {table_name} "
                "FOR EACH ROW EXECUTE FUNCTION enforce_character_state_consistency()"
            ).execute_if(dialect="postgresql"),
        )
    event.listen(
        anchor,
        "after_create",
        DDL(
            "CREATE OR REPLACE TRIGGER trg_character_state_heads_consistency "
            "BEFORE INSERT OR UPDATE OR DELETE "
            "ON character_state_heads FOR EACH ROW EXECUTE FUNCTION enforce_character_state_consistency()"
        ).execute_if(dialect="postgresql"),
    )


_install_character_state_integrity_ddl()
