from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from narrative_core import AuthoritativeTimelineStateEngine
from platform_database import Database
from platform_shared import affected_rows
from production_domain.models import (
    CandidateStatus,
    Character,
    CharacterIdentityVersion,
    CharacterStateCommit,
    CharacterStateCommitActor,
    CharacterStateDecision,
    CharacterStateDelta,
    CharacterStateHead,
    CharacterStateProposalKind,
    CharacterStateProposalSource,
    CharacterStateValidation,
    CharacterStateValidationStage,
    CharacterStateValidatorKind,
    CharacterStateVersion,
    DecisionRecord,
    GenerationCandidate,
    ModelExecutionRecord,
    QAResult,
    Shot,
    ShotStatus,
    TimelineState,
    TimelineTransition,
)
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .branches import assert_branch_writable_in_session

_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_MISSING = object()
_PATCH_FORMAT = "JSON_PATCH_V1"
_STATE_SCHEMA_VERSION = "character-narrative-state-v1"
_POLICY_VERSION = "character-state-policy-v1"
_DEFAULT_SCOPE = "main"
_VISUAL_CONFIDENCE_THRESHOLD = 0.75
_PROPOSAL_SET_HASH_KEY = "character_state_proposal_set_hash"
_MAX_STATE_BYTES = 256 * 1024
_MAX_STATE_NODES = 5_000
_MAX_STATE_DEPTH = 12
_MAX_CONTINUITY_CONSTRAINTS = 200

_FORBIDDEN_IDENTITY_PATHS = (
    "identity",
    "canonical_asset_id",
    "identity_embedding_id",
    "face",
    "body_proportions",
    "canonical_hair",
    "canonical_outfit",
    "appearance.face",
    "appearance.hair",
    "appearance.body",
    "appearance.body_proportions",
    "appearance.canonical_hair",
    "appearance.canonical_outfit",
    "appearance.outfit.type",
    "appearance.outfit.design",
    "appearance.outfit.color",
)
_VISUAL_ROOTS = (
    "appearance.injury",
    "appearance.outfit",
    "appearance.contamination",
    "appearance.wetness",
    "props",
    "narrative_state.location",
    "narrative_state.time_of_day",
    "narrative_state.lighting",
    "narrative_state.props_in_hand",
)
_ALLOWED_EVIDENCE_AUTHORITIES = {"FACT_OBSERVATION"}


class CharacterStateError(RuntimeError):
    pass


class CharacterStatePolicyViolation(CharacterStateError):
    pass


class CharacterStateEvidenceRequired(CharacterStateError):
    pass


class CharacterStateConflict(CharacterStateError):
    pass


@dataclass(frozen=True)
class CharacterStateValidationSummary:
    decision: str
    delta_ids: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class CharacterStatePreview:
    target_state: dict[str, Any]
    normalized_patch: list[dict[str, Any]]
    changed_paths: tuple[str, ...]
    required_visual_paths: tuple[str, ...]


def canonical_json_hash(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CharacterStatePolicyViolation("character state must be finite canonical JSON") from exc
    return hashlib.sha256(payload).hexdigest()


def _validate_state_resource(value: Any, *, label: str) -> None:
    """Bound user/planner JSON before recursive traversal, hashing, or persistence."""

    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > _MAX_STATE_NODES:
            raise CharacterStatePolicyViolation(f"{label} exceeds the state node limit")
        if depth > _MAX_STATE_DEPTH:
            raise CharacterStatePolicyViolation(f"{label} exceeds the state depth limit")
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str) or not key or len(key) > 160:
                    raise CharacterStatePolicyViolation(f"{label} contains an invalid object key")
                stack.append((item, depth + 1))
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif current is not None and not isinstance(current, (str, int, float, bool)):
            raise CharacterStatePolicyViolation(f"{label} contains a non-JSON value")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CharacterStatePolicyViolation(f"{label} must be finite JSON") from exc
    if len(encoded) > _MAX_STATE_BYTES:
        raise CharacterStatePolicyViolation(f"{label} exceeds the state byte limit")


def _identity_fingerprint(identity: CharacterIdentityVersion) -> str:
    return canonical_json_hash(
        {
            "id": identity.id,
            "version": identity.version,
            "master_asset_id": identity.master_asset_id,
            "front_asset_id": identity.front_asset_id,
            "left_profile_asset_id": identity.left_profile_asset_id,
            "right_profile_asset_id": identity.right_profile_asset_id,
            "three_quarter_left_asset_id": identity.three_quarter_left_asset_id,
            "three_quarter_right_asset_id": identity.three_quarter_right_asset_id,
            "full_body_asset_id": identity.full_body_asset_id,
            "hair_signature": identity.hair_signature,
            "costume_signature": identity.costume_signature,
        }
    )


def _state_hash(
    *,
    project_id: str,
    character_id: str,
    timeline_scope_key: str,
    version: int,
    identity_version_id: str,
    identity_fingerprint: str,
    previous_state_hash: str | None,
    narrative_state: dict[str, Any],
) -> str:
    return canonical_json_hash(
        {
            "schema_version": _STATE_SCHEMA_VERSION,
            "project_id": project_id,
            "character_id": character_id,
            "timeline_scope_key": timeline_scope_key,
            "version": version,
            "identity_version_id": identity_version_id,
            "identity_fingerprint": identity_fingerprint,
            "previous_state_hash": previous_state_hash,
            "narrative_state": narrative_state,
        }
    )


def _normalized_path(raw: Any) -> str:
    if not isinstance(raw, str):
        raise CharacterStatePolicyViolation("state delta path must be a string")
    path = raw.strip().strip(".")
    parts = path.split(".") if path else []
    if not parts or len(parts) > 10 or len(path) > 320 or any(not _PATH_SEGMENT.match(p) for p in parts):
        raise CharacterStatePolicyViolation(f"invalid character state path: {raw!r}")
    return ".".join(parts)


def _json_pointer(path: str) -> str:
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in path.split("."))


def _identity_path(path: str) -> bool:
    return any(
        path == item or path.startswith(f"{item}.") or item.startswith(f"{path}.")
        for item in _FORBIDDEN_IDENTITY_PATHS
    )


def _visual_path(path: str) -> bool:
    return any(
        path == item or path.startswith(f"{item}.") or item.startswith(f"{path}.") for item in _VISUAL_ROOTS
    )


def _get_path(state: dict[str, Any], path: str) -> Any:
    current: Any = state
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _parent_for_path(state: dict[str, Any], path: str) -> tuple[dict[str, Any], str]:
    parts = path.split(".")
    current: Any = state
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current or not isinstance(current[part], dict):
            raise CharacterStatePolicyViolation(f"state delta parent does not exist: {path}")
        current = current[part]
    if not isinstance(current, dict):
        raise CharacterStatePolicyViolation(f"state delta parent is not an object: {path}")
    return current, parts[-1]


def _walk_paths(value: Any, prefix: str = "") -> list[str]:
    if not isinstance(value, dict):
        return [prefix] if prefix else []
    paths: list[str] = []
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        paths.extend(_walk_paths(item, path) if isinstance(item, dict) else [path])
    return paths


def _changed_leaf_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    """Return the concrete leaf paths whose values differ between two JSON objects."""

    if before is not _MISSING and after is not _MISSING and before == after:
        return []
    if isinstance(before, dict) or isinstance(after, dict):
        before_items = before if isinstance(before, dict) else {}
        after_items = after if isinstance(after, dict) else {}
        changed: list[str] = []
        for key in sorted(set(before_items) | set(after_items), key=str):
            path = f"{prefix}.{key}" if prefix else str(key)
            changed.extend(
                _changed_leaf_paths(
                    before_items.get(key, _MISSING),
                    after_items.get(key, _MISSING),
                    path,
                )
            )
        return changed or ([prefix] if prefix else [])
    return [prefix] if prefix else []


def _normalize_constraints(state: dict[str, Any]) -> None:
    constraints = state.get("continuity_constraints", [])
    if constraints is None:
        state["continuity_constraints"] = []
        return
    if not isinstance(constraints, list):
        raise CharacterStatePolicyViolation("continuity_constraints must be an array")
    if len(constraints) > _MAX_CONTINUITY_CONSTRAINTS:
        raise CharacterStatePolicyViolation("continuity_constraints exceeds the item limit")
    normalized: list[dict[str, Any]] = []
    constraint_ids: set[str] = set()
    for index, raw in enumerate(constraints):
        if not isinstance(raw, dict):
            raise CharacterStatePolicyViolation("continuity constraints must be objects")
        allowed = {
            "id",
            "path",
            "rule",
            "value",
            "release_scene_sequence",
            "evidence_required",
            "description",
        }
        if set(raw) - allowed:
            raise CharacterStatePolicyViolation("continuity constraint contains unsupported fields")
        path = _normalized_path(raw.get("path"))
        if _identity_path(path):
            raise CharacterStatePolicyViolation("identity belongs to the immutable identity layer")
        rule = str(raw.get("rule", "")).upper()
        if rule not in {"MUST_EQUAL", "MUST_EXIST", "LOCK_UNTIL_SCENE"}:
            raise CharacterStatePolicyViolation(f"unsupported continuity rule: {rule}")
        constraint_id = str(raw.get("id") or f"constraint-{index + 1}")
        if constraint_id in constraint_ids:
            raise CharacterStatePolicyViolation(f"duplicate continuity constraint id: {constraint_id}")
        constraint_ids.add(constraint_id)
        item: dict[str, Any] = {
            "id": constraint_id,
            "path": path,
            "rule": rule,
            "evidence_required": bool(raw.get("evidence_required", True)),
        }
        if raw.get("description"):
            item["description"] = str(raw["description"])[:500]
        if rule in {"MUST_EQUAL", "LOCK_UNTIL_SCENE"}:
            if "value" not in raw:
                raise CharacterStatePolicyViolation(f"{rule} requires a value")
            item["value"] = deepcopy(raw["value"])
        if rule == "LOCK_UNTIL_SCENE":
            release = raw.get("release_scene_sequence")
            if isinstance(release, bool) or not isinstance(release, int) or release < 1:
                raise CharacterStatePolicyViolation(
                    "LOCK_UNTIL_SCENE requires a positive release_scene_sequence"
                )
            item["release_scene_sequence"] = release
        normalized.append(item)
    state["continuity_constraints"] = normalized


def normalize_initial_state(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CharacterStatePolicyViolation("narrative state must be an object")
    _validate_state_resource(value, label="narrative state")
    state = deepcopy(value)
    for path in _walk_paths(state):
        if _identity_path(path):
            raise CharacterStatePolicyViolation(
                f"immutable identity path cannot enter narrative state: {path}"
            )
    _normalize_constraints(state)
    canonical_json_hash(state)
    return state


def normalize_and_apply_patch(
    base_state: dict[str, Any], patch_json: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    _validate_state_resource(base_state, label="base narrative state")
    _validate_state_resource(patch_json, label="state delta")
    if not isinstance(patch_json, dict) or set(patch_json) != {"operations"}:
        raise CharacterStatePolicyViolation("state delta must contain only an operations array")
    raw_operations = patch_json.get("operations")
    if not isinstance(raw_operations, list) or not raw_operations or len(raw_operations) > 100:
        raise CharacterStatePolicyViolation("state delta operations must be a non-empty bounded array")
    target = deepcopy(base_state)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_operations:
        if not isinstance(raw, dict):
            raise CharacterStatePolicyViolation("state delta operations must be objects")
        if set(raw) - {"op", "path", "from", "to"}:
            raise CharacterStatePolicyViolation("state delta operation contains unsupported fields")
        operation = str(raw.get("op", "")).upper()
        if operation not in {"ADD", "REPLACE", "REMOVE"}:
            raise CharacterStatePolicyViolation(f"unsupported state delta operation: {operation}")
        path = _normalized_path(raw.get("path"))
        if path in seen:
            raise CharacterStatePolicyViolation(f"state delta changes a path more than once: {path}")
        seen.add(path)
        if _identity_path(path):
            raise CharacterStatePolicyViolation(f"immutable identity path cannot change: {path}")
        if path == "continuity_constraints" or path.startswith("continuity_constraints."):
            raise CharacterStatePolicyViolation("narrative deltas cannot rewrite continuity policy")
        if operation == "REMOVE" and _visual_path(path):
            raise CharacterStatePolicyViolation(
                "visual narrative state cannot be removed; replace it with an explicit state"
            )
        current = _get_path(target, path)
        parent, leaf = _parent_for_path(target, path)
        if operation == "ADD":
            if current is not _MISSING or "to" not in raw or "from" in raw:
                raise CharacterStatePolicyViolation(f"ADD precondition failed: {path}")
            parent[leaf] = deepcopy(raw["to"])
            normalized.append(
                {
                    "op": "add",
                    "path": _json_pointer(path),
                    "value": deepcopy(raw["to"]),
                }
            )
        elif operation == "REPLACE":
            if current is _MISSING or "from" not in raw or "to" not in raw or current != raw["from"]:
                raise CharacterStatePolicyViolation(f"REPLACE base value mismatch: {path}")
            parent[leaf] = deepcopy(raw["to"])
            normalized.append(
                {
                    "op": "replace",
                    "path": _json_pointer(path),
                    "value": deepcopy(raw["to"]),
                }
            )
        else:
            if current is _MISSING or "from" not in raw or "to" in raw or current != raw["from"]:
                raise CharacterStatePolicyViolation(f"REMOVE base value mismatch: {path}")
            del parent[leaf]
            normalized.append({"op": "remove", "path": _json_pointer(path)})
    _validate_state_resource(target, label="target narrative state")
    canonical_json_hash(target)
    return target, normalized, _changed_leaf_paths(base_state, target)


def _enforce_continuity_constraints(
    base_state: dict[str, Any], target_state: dict[str, Any], scene_sequence: int
) -> list[str]:
    violations: list[str] = []
    for constraint in base_state.get("continuity_constraints", []):
        path = constraint["path"]
        value = _get_path(target_state, path)
        rule = constraint["rule"]
        if rule == "MUST_EXIST" and value is _MISSING:
            violations.append(f"STATE_REQUIRED:{path}")
        elif rule == "MUST_EQUAL" and (value is _MISSING or value != constraint.get("value")):
            violations.append(f"STATE_LOCK_MISMATCH:{path}")
        elif (
            rule == "LOCK_UNTIL_SCENE"
            and scene_sequence < int(constraint["release_scene_sequence"])
            and (value is _MISSING or value != constraint.get("value"))
        ):
            violations.append(f"STATE_LOCK_ACTIVE:{path}")
    return violations


def _active_visual_constraint_paths(state: dict[str, Any], scene_sequence: int) -> set[str]:
    paths: set[str] = set()
    for constraint in state.get("continuity_constraints", []):
        if not constraint.get("evidence_required", True):
            continue
        path = str(constraint.get("path", ""))
        if not _visual_path(path):
            continue
        if constraint.get("rule") == "LOCK_UNTIL_SCENE" and scene_sequence >= int(
            constraint["release_scene_sequence"]
        ):
            continue
        paths.add(path)
    return paths


def required_visual_state_paths(
    state: dict[str, Any],
    *,
    scene_sequence: int,
    changed_paths: tuple[str, ...] | list[str] = (),
) -> tuple[str, ...]:
    required = {path for path in changed_paths if _visual_path(path)}
    required.update(_active_visual_constraint_paths(state, scene_sequence))
    return tuple(sorted(required))


def preview_character_state_transition(
    base_state: dict[str, Any],
    patch_json: dict[str, Any],
    *,
    scene_sequence: int,
) -> CharacterStatePreview:
    """Build the exact generation target without mutating authoritative state."""

    target_state, normalized_patch, changed_paths = normalize_and_apply_patch(base_state, patch_json)
    violations = _enforce_continuity_constraints(base_state, target_state, scene_sequence)
    if violations:
        raise CharacterStatePolicyViolation("; ".join(violations))
    return CharacterStatePreview(
        target_state=target_state,
        normalized_patch=normalized_patch,
        changed_paths=tuple(changed_paths),
        required_visual_paths=required_visual_state_paths(
            target_state,
            scene_sequence=scene_sequence,
            changed_paths=changed_paths,
        ),
    )


def _required_visual_paths(delta: CharacterStateDelta, scene_sequence: int) -> list[str]:
    return list(
        required_visual_state_paths(
            delta.proposed_state_json,
            scene_sequence=scene_sequence,
            changed_paths=delta.changed_paths_json,
        )
    )


class PersistentCharacterStateService:
    version = "persistent-character-state-v1"

    def __init__(self, database: Database):
        self.database = database
        self.timeline = AuthoritativeTimelineStateEngine(database)

    @staticmethod
    def _project_for_shot(session: Session, shot: Shot) -> str:
        return str(shot.scene.episode.project_id)

    @staticmethod
    def _scope_for_shot(session: Session, shot: Shot, character_id: str) -> str:
        transition = session.scalar(
            select(TimelineTransition).where(TimelineTransition.target_shot_id == shot.id)
        )
        if transition is not None and transition.branch_key:
            return str(transition.branch_key)
        if shot.input_state_id:
            input_state = session.get(TimelineState, shot.input_state_id)
            if input_state:
                ref = (input_state.state_json.get("character_state_refs") or {}).get(character_id)
                if isinstance(ref, dict) and ref.get("timeline_scope_key"):
                    return str(ref["timeline_scope_key"])
        return _DEFAULT_SCOPE

    @staticmethod
    def _head(
        session: Session,
        *,
        project_id: str,
        character_id: str,
        timeline_scope_key: str,
        lock: bool = False,
    ) -> CharacterStateHead | None:
        statement = select(CharacterStateHead).where(
            CharacterStateHead.project_id == project_id,
            CharacterStateHead.character_id == character_id,
            CharacterStateHead.timeline_scope_key == timeline_scope_key,
        )
        if lock:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return session.scalar(statement)

    def current(
        self,
        project_id: str,
        character_id: str,
        *,
        timeline_scope_key: str = _DEFAULT_SCOPE,
    ) -> CharacterStateVersion | None:
        with self.database.session() as session:
            character = session.get(Character, character_id)
            if character is None or character.project_id != project_id:
                raise LookupError("character not found in project")
            head = self._head(
                session,
                project_id=project_id,
                character_id=character_id,
                timeline_scope_key=timeline_scope_key,
            )
            return session.get(CharacterStateVersion, head.state_version_id) if head else None

    @staticmethod
    def _identity(session: Session, character: Character) -> CharacterIdentityVersion:
        if not character.current_identity_version_id:
            raise CharacterStatePolicyViolation("character identity must be confirmed first")
        identity = session.get(CharacterIdentityVersion, character.current_identity_version_id)
        if identity is None or identity.character_id != character.id or identity.status != "LOCKED":
            raise CharacterStatePolicyViolation("character identity binding is invalid or unlocked")
        return identity

    @staticmethod
    def _candidate_proposal_rows(
        session: Session,
        candidate_id: str,
    ) -> list[CharacterStateDelta]:
        rows = list(
            session.scalars(
                select(CharacterStateDelta)
                .where(
                    CharacterStateDelta.candidate_id == candidate_id,
                    CharacterStateDelta.proposal_kind != CharacterStateProposalKind.INITIALIZE.value,
                )
                .order_by(CharacterStateDelta.character_id, CharacterStateDelta.id)
            )
        )
        character_ids = [row.character_id for row in rows]
        if len(character_ids) != len(set(character_ids)):
            raise CharacterStateConflict(
                "candidate has more than one state proposal for a character; regenerate"
            )
        return rows

    @classmethod
    def _candidate_proposal_set_hash(
        cls,
        session: Session,
        candidate_id: str,
    ) -> str | None:
        rows = cls._candidate_proposal_rows(session, candidate_id)
        if not rows:
            return None
        return canonical_json_hash(
            [
                {
                    "id": row.id,
                    "character_id": row.character_id,
                    "timeline_scope_key": row.timeline_scope_key,
                    "base_state_version_id": row.base_state_version_id,
                    "identity_version_id": row.identity_version_id,
                    "target_version": row.target_version,
                    "base_state_hash": row.base_state_hash,
                    "target_state_hash": row.target_state_hash,
                    "patch_json": row.patch_json,
                    "changed_paths_json": row.changed_paths_json,
                    "input_timeline_state_id": row.input_timeline_state_id,
                    "input_timeline_state_hash": row.input_timeline_state_hash,
                    "planned_output_timeline_state_id": row.planned_output_timeline_state_id,
                    "planned_output_timeline_state_hash": row.planned_output_timeline_state_hash,
                    "source_kind": row.source_kind,
                    "model_execution_record_id": row.model_execution_record_id,
                    "proposed_by_user_id": row.proposed_by_user_id,
                }
                for row in rows
            ]
        )

    @classmethod
    def _assert_candidate_proposal_set_frozen(
        cls,
        session: Session,
        candidate: GenerationCandidate,
    ) -> None:
        computed = cls._candidate_proposal_set_hash(session, candidate.id)
        recorded = (candidate.metadata_json or {}).get(_PROPOSAL_SET_HASH_KEY)
        if computed != recorded:
            raise CharacterStateConflict("candidate state proposal set changed after generation planning")

    def initialize_from_committed_candidate(
        self,
        *,
        project_id: str,
        character_id: str,
        shot_id: str,
        candidate_id: str,
        narrative_state: dict[str, Any],
        timeline_scope_key: str = _DEFAULT_SCOPE,
        committed_by_user_id: str | None = None,
        reason: str,
    ) -> CharacterStateVersion:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("state initialization reason is required")
        if not committed_by_user_id:
            raise CharacterStatePolicyViolation(
                "state initialization requires an authenticated human confirmer"
            )
        state = normalize_initial_state(narrative_state)
        with self.database.session() as session:
            character = session.scalar(
                select(Character)
                .where(Character.id == character_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            shot = session.scalar(
                select(Shot)
                .where(Shot.id == shot_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            candidate = session.get(GenerationCandidate, candidate_id)
            if character is None or character.project_id != project_id:
                raise LookupError("character not found in project")
            if shot is None or self._project_for_shot(session, shot) != project_id:
                raise LookupError("shot not found in project")
            if (
                candidate is None
                or candidate.shot_id != shot.id
                or candidate.status != CandidateStatus.COMMITTED.value
                or shot.status != ShotStatus.COMMITTED.value
                or shot.committed_candidate_id != candidate.id
            ):
                raise CharacterStatePolicyViolation(
                    "initial narrative state requires the shot's committed candidate"
                )
            if not shot.input_state_id or not shot.output_state_id:
                raise CharacterStatePolicyViolation("source shot has incomplete timeline state")
            resolved_scope = self._scope_for_shot(session, shot, character.id)
            if timeline_scope_key != resolved_scope:
                raise CharacterStatePolicyViolation(
                    "timeline scope does not match the source shot's authoritative branch"
                )
            assert_branch_writable_in_session(
                session, project_id=project_id, scope_key=timeline_scope_key
            )
            baseline_violations = _enforce_continuity_constraints(
                state,
                state,
                shot.scene.sequence,
            )
            if baseline_violations:
                raise CharacterStatePolicyViolation("; ".join(baseline_violations))
            identity = self._identity(session, character)
            identity_hash = _identity_fingerprint(identity)
            target_hash = _state_hash(
                project_id=project_id,
                character_id=character.id,
                timeline_scope_key=timeline_scope_key,
                version=1,
                identity_version_id=identity.id,
                identity_fingerprint=identity_hash,
                previous_state_hash=None,
                narrative_state=state,
            )
            existing_head = self._head(
                session,
                project_id=project_id,
                character_id=character.id,
                timeline_scope_key=timeline_scope_key,
                lock=True,
            )
            if existing_head is not None:
                existing_version = session.get(CharacterStateVersion, existing_head.state_version_id)
                existing_commit = session.scalar(
                    select(CharacterStateCommit).where(
                        CharacterStateCommit.to_state_version_id == existing_head.state_version_id
                    )
                )
                if (
                    existing_version is not None
                    and existing_commit is not None
                    and existing_version.version == 1
                    and existing_version.source_shot_id == shot.id
                    and existing_version.source_candidate_id == candidate.id
                    and existing_version.identity_version_id == identity.id
                    and existing_version.state_hash == target_hash
                    and existing_version.narrative_state_json == state
                    and existing_commit.committed_by_user_id == committed_by_user_id
                    and existing_commit.reason == normalized_reason
                ):
                    return existing_version
                raise CharacterStateConflict("character narrative state is already initialized")
            patch = [
                {
                    "op": "add",
                    "path": _json_pointer(path),
                    "value": deepcopy(_get_path(state, path)),
                }
                for path in _walk_paths(state)
            ]
            delta = CharacterStateDelta(
                project_id=project_id,
                character_id=character.id,
                timeline_scope_key=timeline_scope_key,
                shot_id=shot.id,
                candidate_id=candidate.id,
                base_state_version_id=None,
                identity_version_id=identity.id,
                input_timeline_state_id=shot.input_state_id,
                planned_output_timeline_state_id=shot.output_state_id,
                proposal_revision=1,
                proposal_kind=CharacterStateProposalKind.INITIALIZE.value,
                source_kind=CharacterStateProposalSource.HUMAN.value,
                patch_format=_PATCH_FORMAT,
                patch_json=patch,
                changed_paths_json=_walk_paths(state),
                proposed_state_json=state,
                base_state_hash=None,
                target_state_hash=target_hash,
                input_timeline_state_hash=canonical_json_hash(
                    session.get(TimelineState, shot.input_state_id).state_json
                ),
                planned_output_timeline_state_hash=canonical_json_hash(
                    session.get(TimelineState, shot.output_state_id).state_json
                ),
                target_version=1,
                state_schema_version=_STATE_SCHEMA_VERSION,
                policy_version=_POLICY_VERSION,
                proposed_by_user_id=committed_by_user_id,
                idempotency_key=f"initialize:{character.id}:{timeline_scope_key}",
            )
            session.add(delta)
            session.flush()
            policy = self._add_validation(
                session,
                delta=delta,
                stage=CharacterStateValidationStage.POLICY.value,
                decision=CharacterStateDecision.PASS.value,
                validator_kind=CharacterStateValidatorKind.RULE_ENGINE.value,
                validated_target_hash=target_hash,
                evidence={"source": "EXPLICIT_BASELINE_INITIALIZATION"},
                violations=[],
            )
            visual = self._add_validation(
                session,
                delta=delta,
                stage=CharacterStateValidationStage.VISUAL.value,
                decision=CharacterStateDecision.PASS.value,
                validator_kind=CharacterStateValidatorKind.HUMAN.value,
                validated_target_hash=target_hash,
                evidence={"source": "HUMAN_CONFIRMED_COMMITTED_BASELINE", "reason": normalized_reason},
                violations=[],
                qa_result_id=candidate.qa_result_id,
                validated_by_user_id=committed_by_user_id,
                evidence_asset_id=candidate.output_asset_id,
            )
            human = self._add_validation(
                session,
                delta=delta,
                stage=CharacterStateValidationStage.HUMAN_OVERRIDE.value,
                decision=CharacterStateDecision.PASS.value,
                validator_kind=CharacterStateValidatorKind.HUMAN.value,
                validated_target_hash=target_hash,
                evidence={"explicit_confirmation": True, "reason": normalized_reason},
                violations=[],
                qa_result_id=candidate.qa_result_id,
                validated_by_user_id=committed_by_user_id,
            )
            version = CharacterStateVersion(
                project_id=project_id,
                character_id=character.id,
                timeline_scope_key=timeline_scope_key,
                version=1,
                previous_state_version_id=None,
                identity_version_id=identity.id,
                source_shot_id=shot.id,
                source_candidate_id=candidate.id,
                state_schema_version=_STATE_SCHEMA_VERSION,
                narrative_state_json=state,
                identity_fingerprint=identity_hash,
                previous_state_hash=None,
                state_hash=target_hash,
            )
            session.add(version)
            session.flush()
            commit = self._add_commit(
                session,
                delta=delta,
                version=version,
                from_version=None,
                policy=policy,
                visual=visual,
                human=human,
                expected_head_version=0,
                actor=CharacterStateCommitActor.HUMAN.value,
                committed_by_user_id=committed_by_user_id,
                reason=normalized_reason,
            )
            head = CharacterStateHead(
                project_id=project_id,
                character_id=character.id,
                timeline_scope_key=timeline_scope_key,
                state_version_id=version.id,
                lock_version=1,
            )
            session.add(head)
            session.flush()
            output_state = self._write_timeline_ref(session, shot, version)
            self.timeline.propagate(session, shot, output_state)
            session.add(
                DecisionRecord(
                    project_id=project_id,
                    shot_id=shot.id,
                    decision_type="CHARACTER_STATE_INITIALIZED",
                    input_features={
                        "character_id": character.id,
                        "candidate_id": candidate.id,
                        "state_hash": target_hash,
                        "timeline_scope_key": timeline_scope_key,
                    },
                    selected_action="COMMIT_STATE_V1",
                    reason_codes=["EXPLICIT_COMMITTED_BASELINE"],
                    model_version=self.version,
                    policy_version=_POLICY_VERSION,
                )
            )
            session.flush()
            del commit
            return version

    def propose_for_candidate_in_session(
        self,
        session: Session,
        *,
        candidate: GenerationCandidate,
        character_id: str,
        base_state_version_id: str,
        patch_json: dict[str, Any],
        idempotency_key: str,
        source_kind: str = CharacterStateProposalSource.RULES.value,
        proposed_by_user_id: str | None = None,
        model_execution_record_id: str | None = None,
    ) -> CharacterStateDelta:
        try:
            normalized_source = CharacterStateProposalSource(source_kind).value
        except ValueError as exc:
            raise CharacterStatePolicyViolation("unsupported state proposal source") from exc
        if normalized_source == CharacterStateProposalSource.HUMAN.value and not proposed_by_user_id:
            raise CharacterStatePolicyViolation("human state proposals require an authenticated user")
        if (
            normalized_source
            in {
                CharacterStateProposalSource.LLM.value,
                CharacterStateProposalSource.VISUAL_EVIDENCE.value,
            }
            and not model_execution_record_id
        ):
            raise CharacterStatePolicyViolation("model-derived state proposals require execution provenance")
        shot = session.get(Shot, candidate.shot_id)
        character = session.get(Character, character_id)
        if shot is None or character is None:
            raise LookupError("candidate shot or character not found")
        project_id = self._project_for_shot(session, shot)
        if character.project_id != project_id:
            raise CharacterStatePolicyViolation("character and candidate belong to different projects")
        if not shot.input_state_id or not shot.output_state_id:
            raise CharacterStatePolicyViolation("candidate shot has incomplete timeline state")
        scope = self._scope_for_shot(session, shot, character.id)
        # A merged, retired or abandoned branch keeps its history readable
        # and accepts no new state; only ACTIVE (or lifecycle-unknown legacy)
        # branches may take a proposal.
        assert_branch_writable_in_session(session, project_id=project_id, scope_key=scope)
        existing = session.scalar(
            select(CharacterStateDelta).where(
                CharacterStateDelta.project_id == project_id,
                CharacterStateDelta.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            replay_base = (
                session.get(CharacterStateVersion, existing.base_state_version_id)
                if existing.base_state_version_id
                else None
            )
            if replay_base is None:
                raise CharacterStateConflict("state delta replay lost its base version")
            replay_preview = preview_character_state_transition(
                replay_base.narrative_state_json,
                patch_json,
                scene_sequence=shot.scene.sequence,
            )
            replay_hash = _state_hash(
                project_id=project_id,
                character_id=character.id,
                timeline_scope_key=scope,
                version=existing.target_version,
                identity_version_id=replay_base.identity_version_id,
                identity_fingerprint=replay_base.identity_fingerprint,
                previous_state_hash=replay_base.state_hash,
                narrative_state=replay_preview.target_state,
            )
            if (
                existing.candidate_id != candidate.id
                or existing.character_id != character_id
                or existing.timeline_scope_key != scope
                or existing.base_state_version_id != base_state_version_id
                or existing.source_kind != normalized_source
                or existing.proposed_by_user_id != proposed_by_user_id
                or existing.model_execution_record_id != model_execution_record_id
                or existing.patch_json != replay_preview.normalized_patch
                or existing.target_state_hash != replay_hash
            ):
                raise CharacterStateConflict(
                    "state delta idempotency key was reused with a different proposal"
                )
            self._assert_candidate_proposal_set_frozen(session, candidate)
            return existing
        if (
            candidate.status != CandidateStatus.CREATED.value
            or candidate.generation_job_id is not None
            or candidate.output_asset_id is not None
            or candidate.qa_result_id is not None
        ):
            raise CharacterStateConflict(
                "state proposals freeze when generation is dispatched; create a new candidate"
            )
        prior_for_character = session.scalar(
            select(CharacterStateDelta).where(
                CharacterStateDelta.candidate_id == candidate.id,
                CharacterStateDelta.character_id == character.id,
                CharacterStateDelta.proposal_kind != CharacterStateProposalKind.INITIALIZE.value,
            )
        )
        if prior_for_character is not None:
            raise CharacterStateConflict("candidate already has a frozen state proposal for this character")
        input_state = session.get(TimelineState, shot.input_state_id)
        output_state = session.get(TimelineState, shot.output_state_id)
        input_ref = (
            (input_state.state_json.get("character_state_refs") or {}).get(character.id)
            if input_state is not None
            else None
        )
        if not isinstance(input_ref, dict) or input_ref.get("state_version_id") != base_state_version_id:
            raise CharacterStateConflict(
                "shot input does not explicitly select the proposed character-state base"
            )
        base = session.get(CharacterStateVersion, base_state_version_id)
        if base is None or base.character_id != character.id or base.project_id != project_id:
            raise CharacterStateConflict("authoritative character state is missing")
        head = self._head(
            session,
            project_id=project_id,
            character_id=character.id,
            timeline_scope_key=scope,
            lock=True,
        )
        branch_fork = head is None and base.timeline_scope_key != scope
        if head is not None and head.state_version_id != base_state_version_id:
            raise CharacterStateConflict("state delta base is not the authoritative branch head")
        if head is None and not branch_fork:
            raise CharacterStateConflict("state delta base is not the authoritative branch head")
        if branch_fork:
            transition = session.scalar(
                select(TimelineTransition).where(TimelineTransition.target_shot_id == shot.id)
            )
            if transition is None or transition.branch_key != scope:
                raise CharacterStateConflict(
                    "a new character-state branch requires an explicit branch transition"
                )
        expected_input_ref = {
            "state_version_id": base.id,
            "version": base.version,
            "state_hash": base.state_hash,
            "timeline_scope_key": base.timeline_scope_key,
            "identity_version_id": base.identity_version_id,
        }
        if (
            input_state is None
            or output_state is None
            or not isinstance(input_ref, dict)
            or any(input_ref.get(key) != value for key, value in expected_input_ref.items())
        ):
            raise CharacterStateConflict(
                "shot input does not reference the selected immutable character-state base"
            )
        identity = self._identity(session, character)
        if (
            identity.id != base.identity_version_id
            or _identity_fingerprint(identity) != base.identity_fingerprint
        ):
            raise CharacterStateConflict(
                "character identity changed; narrative state requires explicit rebase"
            )
        preview = preview_character_state_transition(
            base.narrative_state_json,
            patch_json,
            scene_sequence=shot.scene.sequence,
        )
        target_state = preview.target_state
        normalized_patch = preview.normalized_patch
        changed_paths = list(preview.changed_paths)
        violations: list[str] = []
        target_version = 1 if branch_fork else base.version + 1
        target_hash = _state_hash(
            project_id=project_id,
            character_id=character.id,
            timeline_scope_key=scope,
            version=target_version,
            identity_version_id=identity.id,
            identity_fingerprint=base.identity_fingerprint,
            previous_state_hash=base.state_hash,
            narrative_state=target_state,
        )
        revision = 1
        delta = CharacterStateDelta(
            project_id=project_id,
            character_id=character.id,
            timeline_scope_key=scope,
            shot_id=shot.id,
            candidate_id=candidate.id,
            base_state_version_id=base.id,
            identity_version_id=identity.id,
            input_timeline_state_id=input_state.id,
            planned_output_timeline_state_id=output_state.id,
            proposal_revision=revision,
            proposal_kind=CharacterStateProposalKind.NARRATIVE.value,
            source_kind=normalized_source,
            patch_format=_PATCH_FORMAT,
            patch_json=normalized_patch,
            changed_paths_json=changed_paths,
            proposed_state_json=target_state,
            base_state_hash=base.state_hash,
            target_state_hash=target_hash,
            input_timeline_state_hash=canonical_json_hash(input_state.state_json),
            planned_output_timeline_state_hash=canonical_json_hash(output_state.state_json),
            target_version=target_version,
            state_schema_version=_STATE_SCHEMA_VERSION,
            policy_version=_POLICY_VERSION,
            model_execution_record_id=model_execution_record_id,
            proposed_by_user_id=proposed_by_user_id,
            idempotency_key=idempotency_key,
        )
        session.add(delta)
        session.flush()
        self._add_validation(
            session,
            delta=delta,
            stage=CharacterStateValidationStage.POLICY.value,
            decision=(
                CharacterStateDecision.REJECT.value if violations else CharacterStateDecision.PASS.value
            ),
            validator_kind=CharacterStateValidatorKind.RULE_ENGINE.value,
            validated_target_hash=target_hash,
            evidence={
                "scene_sequence": shot.scene.sequence,
                "changed_paths": changed_paths,
                "identity_layer_unchanged": True,
            },
            violations=violations,
        )
        if violations:
            raise CharacterStatePolicyViolation("; ".join(violations))
        proposal_hash = self._candidate_proposal_set_hash(session, candidate.id)
        if proposal_hash is None:  # pragma: no cover - the flushed delta guarantees a set.
            raise CharacterStateConflict("candidate state proposal set was not persisted")
        candidate.metadata_json = {
            **(candidate.metadata_json or {}),
            _PROPOSAL_SET_HASH_KEY: proposal_hash,
        }
        session.flush()
        return delta

    @staticmethod
    def _add_validation(
        session: Session,
        *,
        delta: CharacterStateDelta,
        stage: str,
        decision: str,
        validator_kind: str,
        validated_target_hash: str,
        evidence: dict[str, Any],
        violations: list[str],
        qa_result_id: str | None = None,
        validated_by_user_id: str | None = None,
        model_execution_record_id: str | None = None,
        evidence_asset_id: str | None = None,
    ) -> CharacterStateValidation:
        evidence_hash = canonical_json_hash(evidence)
        existing = session.scalar(
            select(CharacterStateValidation).where(
                CharacterStateValidation.state_delta_id == delta.id,
                CharacterStateValidation.stage == stage,
                CharacterStateValidation.evidence_hash == evidence_hash,
            )
        )
        if existing is not None:
            return existing
        attempt = (
            int(
                session.scalar(
                    select(func.coalesce(func.max(CharacterStateValidation.attempt), 0)).where(
                        CharacterStateValidation.state_delta_id == delta.id,
                        CharacterStateValidation.stage == stage,
                    )
                )
                or 0
            )
            + 1
        )
        validation = CharacterStateValidation(
            project_id=delta.project_id,
            state_delta_id=delta.id,
            stage=stage,
            attempt=attempt,
            decision=decision,
            validator_kind=validator_kind,
            model_execution_record_id=model_execution_record_id,
            qa_result_id=qa_result_id,
            evidence_asset_id=evidence_asset_id,
            validated_target_hash=validated_target_hash,
            evidence_hash=evidence_hash,
            observed_state_json=evidence.get("observed_state", {}),
            evidence_json=evidence,
            violations_json=[{"code": code} for code in violations],
            policy_version=_POLICY_VERSION,
            validated_by_user_id=validated_by_user_id,
        )
        session.add(validation)
        session.flush()
        return validation

    @staticmethod
    def _qa_state_evidence(qa: QAResult) -> dict[str, Any]:
        metrics = qa.metrics_json or {}
        evidence = metrics.get("character_state_evidence")
        if isinstance(evidence, dict):
            return evidence
        prior = metrics.get("prior_metrics")
        if isinstance(prior, dict) and isinstance(prior.get("character_state_evidence"), dict):
            return prior["character_state_evidence"]
        return {}

    def validate_candidate(self, candidate_id: str, qa_result_id: str) -> CharacterStateValidationSummary:
        with self.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            qa = session.get(QAResult, qa_result_id)
            if candidate is None or qa is None or qa.candidate_id != candidate.id:
                raise LookupError("candidate or QA result not found")
            self._assert_candidate_proposal_set_frozen(session, candidate)
            shot = session.get(Shot, candidate.shot_id)
            if shot is None:
                raise LookupError("candidate shot not found")
            deltas = list(
                session.scalars(
                    select(CharacterStateDelta)
                    .where(CharacterStateDelta.candidate_id == candidate.id)
                    .order_by(CharacterStateDelta.character_id, CharacterStateDelta.proposal_revision.desc())
                )
            )
            latest: dict[str, CharacterStateDelta] = {}
            for delta in deltas:
                latest.setdefault(delta.character_id, delta)
            if not latest:
                return CharacterStateValidationSummary(CharacterStateDecision.PASS.value, (), ())
            evidence_by_character = self._qa_state_evidence(qa)
            aggregate = CharacterStateDecision.PASS.value
            reasons: list[str] = []
            for character_id, delta in latest.items():
                raw = evidence_by_character.get(character_id, {})
                execution_id = raw.get("model_execution_record_id") if isinstance(raw, dict) else None
                execution = session.get(ModelExecutionRecord, execution_id) if execution_id else None
                trusted_vlm_provenance = bool(
                    execution is not None
                    and execution.project_id == delta.project_id
                    and execution.role == "VLM_REVIEWER"
                    and execution.status == "SUCCEEDED"
                    and execution.metadata_json.get("evidence_purpose") == "CHARACTER_STATE_FACT_OBSERVATION"
                    and execution.metadata_json.get("evidence_asset_id") == candidate.output_asset_id
                    and "voyage" not in f"{execution.provider} {execution.provider_model_id}".casefold()
                )
                decision, violations, observed = self._evaluate_visual_evidence(
                    delta,
                    raw,
                    scene_sequence=shot.scene.sequence,
                    expected_evidence_asset_id=candidate.output_asset_id,
                    trusted_vlm_provenance=trusted_vlm_provenance,
                )
                has_vlm_provenance = bool(
                    isinstance(raw, dict)
                    and str(raw.get("authority_level", "")).upper() == "FACT_OBSERVATION"
                    and trusted_vlm_provenance
                )
                self._add_validation(
                    session,
                    delta=delta,
                    stage=CharacterStateValidationStage.VISUAL.value,
                    decision=decision,
                    validator_kind=(
                        CharacterStateValidatorKind.VLM.value
                        if has_vlm_provenance
                        else CharacterStateValidatorKind.RULE_ENGINE.value
                    ),
                    validated_target_hash=delta.target_state_hash,
                    evidence={**raw, "observed_state": observed},
                    violations=violations,
                    qa_result_id=qa.id,
                    model_execution_record_id=(
                        str(raw["model_execution_record_id"]) if has_vlm_provenance else None
                    ),
                    evidence_asset_id=(
                        str(raw["evidence_asset_id"])
                        if isinstance(raw, dict) and raw.get("evidence_asset_id") == candidate.output_asset_id
                        else None
                    ),
                )
                reasons.extend(violations)
                if decision == CharacterStateDecision.REJECT.value:
                    aggregate = CharacterStateDecision.REJECT.value
                elif (
                    decision == CharacterStateDecision.REVIEW_REQUIRED.value
                    and aggregate != CharacterStateDecision.REJECT.value
                ):
                    aggregate = CharacterStateDecision.REVIEW_REQUIRED.value
            return CharacterStateValidationSummary(
                aggregate,
                tuple(delta.id for delta in latest.values()),
                tuple(sorted(set(reasons))),
            )

    @staticmethod
    def _evaluate_visual_evidence(
        delta: CharacterStateDelta,
        raw: Any,
        *,
        scene_sequence: int,
        expected_evidence_asset_id: str | None,
        trusted_vlm_provenance: bool,
    ) -> tuple[str, list[str], dict[str, Any]]:
        required = _required_visual_paths(delta, scene_sequence)
        if not required:
            return CharacterStateDecision.PASS.value, [], {}
        if not isinstance(raw, dict):
            return (
                CharacterStateDecision.REVIEW_REQUIRED.value,
                [f"STATE_EVIDENCE_MISSING:{path}" for path in required],
                {},
            )
        authority = str(raw.get("authority_level", "")).upper()
        source = str(raw.get("source", "")).upper()
        if authority not in _ALLOWED_EVIDENCE_AUTHORITIES or "VOYAGE" in source:
            return (
                CharacterStateDecision.REVIEW_REQUIRED.value,
                ["ADVISORY_EMBEDDING_CANNOT_VALIDATE_STATE"],
                {},
            )
        if not trusted_vlm_provenance:
            return (
                CharacterStateDecision.REVIEW_REQUIRED.value,
                ["STATE_EVIDENCE_MODEL_PROVENANCE_INVALID"],
                {},
            )
        if expected_evidence_asset_id is None or raw.get("evidence_asset_id") != expected_evidence_asset_id:
            return (
                CharacterStateDecision.REVIEW_REQUIRED.value,
                ["STATE_EVIDENCE_ASSET_MISMATCH"],
                {},
            )
        observations = raw.get("observations")
        if not isinstance(observations, list):
            return (
                CharacterStateDecision.REVIEW_REQUIRED.value,
                [f"STATE_EVIDENCE_MISSING:{path}" for path in required],
                {},
            )
        by_path: dict[str, dict[str, Any]] = {}
        for item in observations:
            if not isinstance(item, dict):
                continue
            try:
                path = _normalized_path(item.get("path"))
            except CharacterStatePolicyViolation:
                continue
            by_path[path] = item
        violations: list[str] = []
        observed_state: dict[str, Any] = {}
        rejected = False
        for path in required:
            observation = by_path.get(path)
            if observation is None:
                violations.append(f"STATE_EVIDENCE_MISSING:{path}")
                continue
            raw_confidence = observation.get("confidence")
            if (
                isinstance(raw_confidence, bool)
                or not isinstance(raw_confidence, (int, float))
                or not 0 <= float(raw_confidence) <= 1
            ):
                violations.append(f"STATE_EVIDENCE_INVALID_CONFIDENCE:{path}")
                continue
            observed = observation.get("value")
            observed_state[path] = observed
            expected = _get_path(delta.proposed_state_json, path)
            if float(raw_confidence) < _VISUAL_CONFIDENCE_THRESHOLD:
                violations.append(f"STATE_EVIDENCE_LOW_CONFIDENCE:{path}")
            elif expected is _MISSING or observed != expected:
                violations.append(f"STATE_EVIDENCE_MISMATCH:{path}")
                rejected = True
        if rejected:
            decision = CharacterStateDecision.REJECT.value
        elif violations:
            decision = CharacterStateDecision.REVIEW_REQUIRED.value
        else:
            decision = CharacterStateDecision.PASS.value
        return decision, violations, observed_state

    @staticmethod
    def _latest_validation(session: Session, delta_id: str, stage: str) -> CharacterStateValidation | None:
        return session.scalar(
            select(CharacterStateValidation)
            .where(
                CharacterStateValidation.state_delta_id == delta_id,
                CharacterStateValidation.stage == stage,
            )
            .order_by(CharacterStateValidation.attempt.desc())
        )

    @staticmethod
    def _add_commit(
        session: Session,
        *,
        delta: CharacterStateDelta,
        version: CharacterStateVersion,
        from_version: CharacterStateVersion | None,
        policy: CharacterStateValidation,
        visual: CharacterStateValidation,
        human: CharacterStateValidation | None,
        expected_head_version: int,
        actor: str,
        committed_by_user_id: str | None,
        reason: str,
    ) -> CharacterStateCommit:
        payload = {
            "project_id": delta.project_id,
            "character_id": delta.character_id,
            "timeline_scope_key": delta.timeline_scope_key,
            "shot_id": delta.shot_id,
            "candidate_id": delta.candidate_id,
            "delta_id": delta.id,
            "from_state_version_id": from_version.id if from_version else None,
            "to_state_version_id": version.id,
            "target_state_hash": version.state_hash,
            "policy_validation_id": policy.id,
            "visual_validation_id": visual.id,
            "human_validation_id": human.id if human else None,
            "expected_head_version": expected_head_version,
            "actor": actor,
            "reason": reason,
        }
        commit = CharacterStateCommit(
            project_id=delta.project_id,
            character_id=delta.character_id,
            timeline_scope_key=delta.timeline_scope_key,
            shot_id=delta.shot_id,
            candidate_id=delta.candidate_id,
            state_delta_id=delta.id,
            from_state_version_id=from_version.id if from_version else None,
            to_state_version_id=version.id,
            policy_validation_id=policy.id,
            visual_validation_id=visual.id,
            human_validation_id=human.id if human else None,
            expected_head_version=expected_head_version,
            commit_actor=actor,
            committed_by_user_id=committed_by_user_id,
            reason=reason,
            commit_hash=canonical_json_hash(payload),
        )
        session.add(commit)
        session.flush()
        return commit

    @staticmethod
    def _write_timeline_ref(
        session: Session,
        shot: Shot,
        version: CharacterStateVersion,
    ) -> TimelineState:
        output_state = session.get(TimelineState, shot.output_state_id) if shot.output_state_id else None
        if output_state is None:
            raise CharacterStateConflict("shot output timeline state is missing")
        payload = deepcopy(output_state.state_json)
        refs = dict(payload.get("character_state_refs") or {})
        refs[version.character_id] = {
            "state_version_id": version.id,
            "version": version.version,
            "state_hash": version.state_hash,
            "timeline_scope_key": version.timeline_scope_key,
            "identity_version_id": version.identity_version_id,
        }
        payload["character_state_refs"] = refs
        characters = dict(payload.get("characters") or {})
        character_payload = dict(characters.get(version.character_id) or {})
        character_payload["character_id"] = version.character_id
        character_payload["narrative_state"] = deepcopy(version.narrative_state_json)
        character_payload["narrative_state_version_id"] = version.id
        characters[version.character_id] = character_payload
        payload["characters"] = characters
        output_state.state_json = payload
        return output_state

    def commit_candidate_in_session(
        self,
        session: Session,
        *,
        candidate: GenerationCandidate,
        shot: Shot,
        qa: QAResult,
        output_state: TimelineState,
        committed_by_user_id: str | None,
    ) -> list[CharacterStateVersion]:
        self._assert_candidate_proposal_set_frozen(session, candidate)
        deltas = list(
            session.scalars(
                select(CharacterStateDelta)
                .where(CharacterStateDelta.candidate_id == candidate.id)
                .order_by(CharacterStateDelta.character_id, CharacterStateDelta.proposal_revision.desc())
            )
        )
        latest: dict[str, CharacterStateDelta] = {}
        for delta in deltas:
            latest.setdefault(delta.character_id, delta)
        committed: list[CharacterStateVersion] = []
        if latest:
            locked_input = session.scalar(
                select(TimelineState)
                .where(TimelineState.id == shot.input_state_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            locked_output = session.scalar(
                select(TimelineState)
                .where(TimelineState.id == shot.output_state_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            input_hash = canonical_json_hash(locked_input.state_json) if locked_input else None
            output_hash = canonical_json_hash(locked_output.state_json) if locked_output else None
            for delta in latest.values():
                if (
                    candidate.shot_id != delta.shot_id
                    or shot.id != delta.shot_id
                    or locked_input is None
                    or locked_output is None
                    or output_state.id != locked_output.id
                    or locked_input.id != delta.input_timeline_state_id
                    or locked_output.id != delta.planned_output_timeline_state_id
                    or locked_input.project_id != delta.project_id
                    or locked_output.project_id != delta.project_id
                    or locked_input.state_kind != "SHOT_INPUT"
                    or locked_output.state_kind != "SHOT_OUTPUT"
                    or locked_input.shot_id not in {None, shot.id}
                    or locked_output.shot_id not in {None, shot.id}
                ):
                    raise CharacterStateConflict("state delta timeline ownership fence changed")
                if (
                    input_hash != delta.input_timeline_state_hash
                    or output_hash != delta.planned_output_timeline_state_hash
                ):
                    raise CharacterStateConflict("timeline state changed after this candidate was planned")
        for delta in latest.values():
            replay = session.scalar(
                select(CharacterStateCommit).where(CharacterStateCommit.state_delta_id == delta.id)
            )
            if replay is not None:
                version = session.get(CharacterStateVersion, replay.to_state_version_id)
                if version is None:
                    raise CharacterStateConflict("character state commit lost its target version")
                self._write_timeline_ref(session, shot, version)
                committed.append(version)
                continue
            policy = self._latest_validation(session, delta.id, CharacterStateValidationStage.POLICY.value)
            visual = self._latest_validation(session, delta.id, CharacterStateValidationStage.VISUAL.value)
            if policy is None or policy.decision != CharacterStateDecision.PASS.value:
                raise CharacterStatePolicyViolation("state delta has no passing policy validation")
            if visual is None:
                raise CharacterStateEvidenceRequired("state delta has no visual validation")
            human: CharacterStateValidation | None = None
            if visual.decision == CharacterStateDecision.REVIEW_REQUIRED.value:
                metrics = qa.metrics_json or {}
                if (
                    qa.profile != "HUMAN_REVIEW"
                    or metrics.get("source") != "USER_EXPLICIT_CONFIRMATION"
                    or not metrics.get("explicit_confirmation")
                ):
                    raise CharacterStateEvidenceRequired("state delta requires explicit human review")
                human = self._add_validation(
                    session,
                    delta=delta,
                    stage=CharacterStateValidationStage.HUMAN_OVERRIDE.value,
                    decision=CharacterStateDecision.PASS.value,
                    validator_kind=CharacterStateValidatorKind.HUMAN.value,
                    validated_target_hash=delta.target_state_hash,
                    evidence={
                        "source": "USER_EXPLICIT_CONFIRMATION",
                        "reason": metrics.get("reason"),
                        "qa_result_id": qa.id,
                    },
                    violations=[],
                    qa_result_id=qa.id,
                    validated_by_user_id=committed_by_user_id,
                )
            elif visual.decision != CharacterStateDecision.PASS.value:
                raise CharacterStatePolicyViolation("state delta visual evidence was rejected")
            head = self._head(
                session,
                project_id=delta.project_id,
                character_id=delta.character_id,
                timeline_scope_key=delta.timeline_scope_key,
                lock=True,
            )
            base = (
                session.get(CharacterStateVersion, delta.base_state_version_id)
                if delta.base_state_version_id
                else None
            )
            branch_fork = bool(
                head is None
                and base is not None
                and base.timeline_scope_key != delta.timeline_scope_key
                and delta.target_version == 1
            )
            if head is not None and head.state_version_id != delta.base_state_version_id:
                raise CharacterStateConflict("character state changed after this candidate was planned")
            if head is None and not branch_fork:
                raise CharacterStateConflict("character state changed after this candidate was planned")
            if (
                base is None
                or base.state_hash != delta.base_state_hash
                or base.identity_version_id != delta.identity_version_id
                or (
                    branch_fork
                    and delta.target_version != 1
                    or not branch_fork
                    and delta.target_version != base.version + 1
                )
            ):
                raise CharacterStateConflict("character state base hash/version no longer matches")
            if branch_fork:
                transition = session.scalar(
                    select(TimelineTransition).where(TimelineTransition.target_shot_id == shot.id)
                )
                if transition is None or transition.branch_key != delta.timeline_scope_key:
                    raise CharacterStateConflict("character state branch fork is no longer valid")
            expected_hash = _state_hash(
                project_id=delta.project_id,
                character_id=delta.character_id,
                timeline_scope_key=delta.timeline_scope_key,
                version=delta.target_version,
                identity_version_id=delta.identity_version_id,
                identity_fingerprint=base.identity_fingerprint,
                previous_state_hash=base.state_hash,
                narrative_state=delta.proposed_state_json,
            )
            if expected_hash != delta.target_state_hash:
                raise CharacterStateConflict("state delta target hash is not reproducible")
            version = CharacterStateVersion(
                project_id=delta.project_id,
                character_id=delta.character_id,
                timeline_scope_key=delta.timeline_scope_key,
                version=delta.target_version,
                previous_state_version_id=base.id,
                identity_version_id=delta.identity_version_id,
                source_shot_id=shot.id,
                source_candidate_id=candidate.id,
                state_schema_version=_STATE_SCHEMA_VERSION,
                narrative_state_json=deepcopy(delta.proposed_state_json),
                identity_fingerprint=base.identity_fingerprint,
                previous_state_hash=base.state_hash,
                state_hash=delta.target_state_hash,
            )
            session.add(version)
            session.flush()
            expected_head_lock = head.lock_version if head is not None else 0
            self._add_commit(
                session,
                delta=delta,
                version=version,
                from_version=base,
                policy=policy,
                visual=visual,
                human=human,
                expected_head_version=expected_head_lock,
                actor=(
                    CharacterStateCommitActor.HUMAN.value
                    if committed_by_user_id
                    else CharacterStateCommitActor.SYSTEM.value
                ),
                committed_by_user_id=committed_by_user_id,
                reason="candidate committed after policy and evidence validation",
            )
            if head is None:
                session.add(
                    CharacterStateHead(
                        project_id=delta.project_id,
                        character_id=delta.character_id,
                        timeline_scope_key=delta.timeline_scope_key,
                        state_version_id=version.id,
                        lock_version=1,
                    )
                )
                session.flush()
            else:
                advanced = session.execute(
                    update(CharacterStateHead)
                    .where(
                        CharacterStateHead.id == head.id,
                        CharacterStateHead.state_version_id == base.id,
                        CharacterStateHead.lock_version == expected_head_lock,
                    )
                    .values(state_version_id=version.id, lock_version=expected_head_lock + 1)
                    .execution_options(synchronize_session=False)
                )
                if affected_rows(advanced) != 1:
                    raise CharacterStateConflict("another candidate advanced the character state first")
            self._write_timeline_ref(session, shot, version)
            session.add(
                DecisionRecord(
                    project_id=delta.project_id,
                    shot_id=shot.id,
                    decision_type="CHARACTER_STATE_COMMIT",
                    input_features={
                        "character_id": delta.character_id,
                        "candidate_id": candidate.id,
                        "from_version": base.version,
                        "to_version": version.version,
                        "changed_paths": delta.changed_paths_json,
                        "base_state_hash": base.state_hash,
                        "target_state_hash": version.state_hash,
                        "branch_fork": branch_fork,
                        "timeline_scope_key": delta.timeline_scope_key,
                    },
                    selected_action="COMMIT_STATE_VERSION",
                    reason_codes=[
                        "POLICY_PASS",
                        "VISUAL_OR_HUMAN_EVIDENCE_PASS",
                        *(["BRANCH_HEAD_CREATED"] if branch_fork else []),
                    ],
                    model_version=self.version,
                    policy_version=_POLICY_VERSION,
                )
            )
            committed.append(version)
        self._carry_forward_context(
            session,
            candidate,
            shot,
            changed_character_ids=set(latest),
        )
        return committed

    @staticmethod
    def _carry_forward_context(
        session: Session,
        candidate: GenerationCandidate,
        shot: Shot,
        *,
        changed_character_ids: set[str],
    ) -> None:
        context = (candidate.metadata_json or {}).get("character_state_context", [])
        output = session.get(TimelineState, shot.output_state_id) if shot.output_state_id else None
        input_state = session.get(TimelineState, shot.input_state_id) if shot.input_state_id else None
        if output is None or input_state is None or not isinstance(context, list):
            return
        project_id = str(shot.scene.episode.project_id)
        if output.project_id != project_id or input_state.project_id != project_id:
            raise CharacterStateConflict("candidate timeline context belongs to another project")
        input_refs = input_state.state_json.get("character_state_refs") or {}
        if not isinstance(input_refs, dict):
            raise CharacterStateConflict("candidate input character-state refs are invalid")
        payload = deepcopy(output.state_json)
        refs = dict(payload.get("character_state_refs") or {})
        characters = dict(payload.get("characters") or {})
        transition = session.scalar(
            select(TimelineTransition).where(TimelineTransition.target_shot_id == shot.id)
        )
        branch_scope = str(transition.branch_key) if transition and transition.branch_key else None
        for item in context:
            if not isinstance(item, dict):
                continue
            character_id = item.get("character_id")
            version_id = item.get("narrative_state_version_id")
            if not character_id or not version_id:
                continue
            if character_id in changed_character_ids:
                if not isinstance(refs.get(character_id), dict):
                    raise CharacterStateConflict("committed character state is missing from shot output")
                continue
            version = session.get(CharacterStateVersion, version_id)
            character = session.get(Character, character_id)
            input_ref = input_refs.get(character_id)
            expected_ref = {
                "state_version_id": version_id,
                "version": item.get("narrative_state_version"),
                "state_hash": item.get("narrative_state_hash"),
                "timeline_scope_key": item.get("timeline_scope_key"),
                "identity_version_id": version.identity_version_id if version else None,
            }
            scope = item.get("timeline_scope_key")
            if branch_scope is not None and scope != branch_scope:
                if (
                    version is None
                    or character is None
                    or character.project_id != project_id
                    or version.project_id != project_id
                    or version.character_id != character_id
                    or version.version != item.get("narrative_state_version")
                    or version.state_hash != item.get("narrative_state_hash")
                    or version.timeline_scope_key != scope
                    or not isinstance(input_ref, dict)
                    or any(input_ref.get(key) != value for key, value in expected_ref.items())
                ):
                    raise CharacterStateConflict("candidate branch source context is invalid")
                # Never leak a main/historical head into a new branch. A changed
                # character gets branch v1 above; unchanged characters require an
                # explicit committed baseline before they can propagate further.
                refs.pop(character_id, None)
                characters.pop(character_id, None)
                continue
            head = (
                session.scalar(
                    select(CharacterStateHead)
                    .where(
                        CharacterStateHead.project_id == project_id,
                        CharacterStateHead.character_id == character_id,
                        CharacterStateHead.timeline_scope_key == scope,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if scope
                else None
            )
            if (
                version is None
                or character is None
                or character.project_id != project_id
                or version.project_id != project_id
                or version.character_id != character_id
                or version.version != item.get("narrative_state_version")
                or version.state_hash != item.get("narrative_state_hash")
                or version.timeline_scope_key != scope
                or head is None
                or head.state_version_id != version.id
                or not isinstance(input_ref, dict)
                or any(input_ref.get(key) != value for key, value in expected_ref.items())
            ):
                raise CharacterStateConflict("candidate state context is no longer valid")
            refs[character_id] = {
                "state_version_id": version.id,
                "version": version.version,
                "state_hash": version.state_hash,
                "timeline_scope_key": version.timeline_scope_key,
                "identity_version_id": version.identity_version_id,
            }
            row = dict(characters.get(character_id) or {})
            row["character_id"] = character_id
            row["narrative_state"] = deepcopy(version.narrative_state_json)
            row["narrative_state_version_id"] = version.id
            characters[character_id] = row
        payload["character_state_refs"] = refs
        payload["characters"] = characters
        output.state_json = payload

    def transition_view(self, candidate_id: str) -> list[dict[str, Any]]:
        with self.database.session() as session:
            candidate = session.get(GenerationCandidate, candidate_id)
            if candidate is None:
                raise LookupError("candidate not found")
            rows = list(
                session.scalars(
                    select(CharacterStateDelta)
                    .where(CharacterStateDelta.candidate_id == candidate.id)
                    .order_by(CharacterStateDelta.character_id, CharacterStateDelta.proposal_revision)
                )
            )
            result: list[dict[str, Any]] = []
            for delta in rows:
                validations = list(
                    session.scalars(
                        select(CharacterStateValidation)
                        .where(CharacterStateValidation.state_delta_id == delta.id)
                        .order_by(CharacterStateValidation.stage, CharacterStateValidation.attempt)
                    )
                )
                commit = session.scalar(
                    select(CharacterStateCommit).where(CharacterStateCommit.state_delta_id == delta.id)
                )
                result.append(
                    {
                        "id": delta.id,
                        "character_id": delta.character_id,
                        "timeline_scope_key": delta.timeline_scope_key,
                        "base_state_version_id": delta.base_state_version_id,
                        "target_version": delta.target_version,
                        "base_state_hash": delta.base_state_hash,
                        "target_state_hash": delta.target_state_hash,
                        "patch": delta.patch_json,
                        "changed_paths": delta.changed_paths_json,
                        "validations": [
                            {
                                "id": item.id,
                                "stage": item.stage,
                                "attempt": item.attempt,
                                "decision": item.decision,
                                "validator_kind": item.validator_kind,
                                "violations": item.violations_json,
                            }
                            for item in validations
                        ],
                        "committed_state_version_id": (
                            commit.to_state_version_id if commit is not None else None
                        ),
                    }
                )
            return result


__all__ = [
    "CharacterStateConflict",
    "CharacterStateError",
    "CharacterStateEvidenceRequired",
    "CharacterStatePolicyViolation",
    "CharacterStatePreview",
    "CharacterStateValidationSummary",
    "PersistentCharacterStateService",
    "canonical_json_hash",
    "normalize_and_apply_patch",
    "normalize_initial_state",
    "preview_character_state_transition",
    "required_visual_state_paths",
]
