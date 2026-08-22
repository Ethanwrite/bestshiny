from __future__ import annotations

from asset_registry_core import assert_canonical_media_provenance
from platform_database import Database
from production_domain.models import (
    Character,
    CharacterIdentityVersion,
    CharacterStateHead,
    CharacterStateVersion,
    MediaAsset,
    TimelineState,
)
from sqlalchemy import func, select


class IdentityLocked(RuntimeError):
    pass


class CharacterIdentityService:
    """Creates immutable identity versions from user-selected canonical assets."""

    def __init__(self, database: Database):
        self.database = database

    def create_character(
        self, project_id: str, name: str, description: str = "", canonical_facts: dict | None = None
    ) -> Character:
        with self.database.session() as session:
            character = Character(
                project_id=project_id,
                name=name,
                description=description,
                canonical_facts=canonical_facts or {},
            )
            session.add(character)
            session.flush()
            return character

    def confirm_identity(
        self,
        character_id: str,
        master_asset_id: str,
        *,
        references: dict[str, str | None] | None = None,
        hair_signature: str = "",
        costume_signature: str = "",
    ) -> CharacterIdentityVersion:
        references = references or {}
        with self.database.session() as session:
            character = session.scalar(
                select(Character)
                .where(Character.id == character_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            asset = session.get(MediaAsset, master_asset_id)
            if not character or not asset:
                raise LookupError("character or master asset not found")
            if asset.project_id != character.project_id:
                raise ValueError("master asset belongs to a different project")
            identity_assets = [asset]
            for role, reference_asset_id in references.items():
                if not reference_asset_id:
                    continue
                reference_asset = session.get(MediaAsset, reference_asset_id)
                if not reference_asset:
                    raise LookupError(f"character reference asset not found: {role}")
                if reference_asset.project_id != character.project_id:
                    raise ValueError(f"character reference asset belongs to a different project: {role}")
                identity_assets.append(reference_asset)
            # Canonical identity is itself a canonical write. Validate immutable
            # origin provenance before creating a version or mutating any pointer,
            # status, or media ownership field. A later logical-asset promotion is
            # defense in depth, not the first trust boundary.
            assert_canonical_media_provenance(session, identity_assets)
            version = (
                int(
                    session.scalar(
                        select(func.coalesce(func.max(CharacterIdentityVersion.version), 0)).where(
                            CharacterIdentityVersion.character_id == character.id
                        )
                    )
                    or 0
                )
                + 1
            )
            identity = CharacterIdentityVersion(
                character_id=character.id,
                version=version,
                master_asset_id=master_asset_id,
                front_asset_id=references.get("front_asset_id"),
                left_profile_asset_id=references.get("left_profile_asset_id"),
                right_profile_asset_id=references.get("right_profile_asset_id"),
                three_quarter_left_asset_id=references.get("three_quarter_left_asset_id"),
                three_quarter_right_asset_id=references.get("three_quarter_right_asset_id"),
                full_body_asset_id=references.get("full_body_asset_id"),
                hair_signature=hair_signature,
                costume_signature=costume_signature,
                status="LOCKED",
            )
            session.add(identity)
            session.flush()
            character.current_identity_version_id = identity.id
            character.status = "CONFIRMED"
            asset.character_id = character.id
            session.flush()
            return identity

    def update_locked_version(self, identity_version_id: str, changes: dict) -> None:
        del changes
        with self.database.session() as session:
            identity = session.get(CharacterIdentityVersion, identity_version_id)
            if not identity:
                raise LookupError("identity version not found")
            raise IdentityLocked("confirmed identity versions are immutable; create a new version")

    def binding(
        self,
        character_id: str,
        *,
        project_id: str | None = None,
        timeline_state_id: str | None = None,
        timeline_scope_key: str = "main",
    ) -> dict:
        with self.database.session() as session:
            character = session.get(Character, character_id)
            if not character or (project_id is not None and character.project_id != project_id):
                raise LookupError("character not found in project")
            if not character.current_identity_version_id:
                raise LookupError("character has no confirmed identity")
            state_version = None
            if timeline_state_id:
                timeline_state = session.get(TimelineState, timeline_state_id)
                if timeline_state is None or timeline_state.project_id != character.project_id:
                    raise LookupError("timeline state not found in character project")
                state_ref = (timeline_state.state_json.get("character_state_refs") or {}).get(character.id)
                if isinstance(state_ref, dict) and state_ref.get("state_version_id"):
                    state_version = session.get(CharacterStateVersion, state_ref["state_version_id"])
                    if (
                        state_version is None
                        or state_version.character_id != character.id
                        or state_version.project_id != character.project_id
                        or state_ref.get("state_hash") != state_version.state_hash
                        or state_ref.get("version") != state_version.version
                        or state_ref.get("timeline_scope_key") != state_version.timeline_scope_key
                        or state_ref.get("identity_version_id") != state_version.identity_version_id
                    ):
                        raise LookupError("timeline character-state reference is invalid")
            elif state_version is None:
                head = session.scalar(
                    select(CharacterStateHead).where(
                        CharacterStateHead.project_id == character.project_id,
                        CharacterStateHead.character_id == character.id,
                        CharacterStateHead.timeline_scope_key == timeline_scope_key,
                    )
                )
                state_version = session.get(CharacterStateVersion, head.state_version_id) if head else None
            identity_version_id = (
                state_version.identity_version_id
                if state_version is not None
                else character.current_identity_version_id
            )
            identity = session.get(CharacterIdentityVersion, identity_version_id)
            if identity is None or identity.character_id != character.id or identity.status != "LOCKED":
                raise LookupError("character identity binding is invalid")
            result = {
                "character_id": character.id,
                "identity_version_id": identity.id,
                "version": identity.version,
                "canonical_assets": [
                    asset_id
                    for asset_id in [
                        identity.master_asset_id,
                        identity.front_asset_id,
                        identity.left_profile_asset_id,
                        identity.right_profile_asset_id,
                        identity.three_quarter_left_asset_id,
                        identity.three_quarter_right_asset_id,
                        identity.full_body_asset_id,
                    ]
                    if asset_id
                ],
                "hair_signature": identity.hair_signature,
                "costume_signature": identity.costume_signature,
                "provider_bindings": identity.provider_bindings_json,
            }
            if state_version is not None:
                result.update(
                    {
                        "narrative_state_version_id": state_version.id,
                        "narrative_state_version": state_version.version,
                        "narrative_state_hash": state_version.state_hash,
                        "timeline_scope_key": state_version.timeline_scope_key,
                        "narrative_state": state_version.narrative_state_json,
                        "continuity_constraints": state_version.narrative_state_json.get(
                            "continuity_constraints", []
                        ),
                    }
                )
            return result
