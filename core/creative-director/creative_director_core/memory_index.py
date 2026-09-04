"""What a locked visual bible is worth remembering as, and how it is described.

Separate from the service because it is a pure projection: given a locked
bible and its lineage, it says which canonical artefacts should enter the
advisory vector memory and what text describes each one. Nothing here embeds,
writes or decides authority - every entry is ADVISORY by construction, and the
outbox is what actually queues it.
"""

from __future__ import annotations

from typing import Any

from production_domain.models import (
    AssetVersion,
    Character,
    CharacterIdentityVersion,
    CreativeSession,
    VisualBibleVersion,
)


def bible_memories(
    session: Any,
    row: CreativeSession,
    bible: VisualBibleVersion,
    lineage: dict[str, Any],
) -> list[dict[str, Any]]:
    """One entry per canonical artefact the lock produced.

    Character identities, the locked style plate, and the canonical scene,
    product and prop assets. The idempotency key names the artefact, not the
    attempt, so re-locking the same bible queues nothing new.
    """

    entries: list[dict[str, Any]] = []
    title = (row.title or "").strip()
    for anchor_key, identity in (lineage.get("identities") or {}).items():
        if not isinstance(identity, dict):
            continue
        version_id = identity.get("identity_version_id")
        if not version_id:
            continue
        version = session.get(CharacterIdentityVersion, version_id)
        character = session.get(Character, identity.get("character_id"))
        name = character.name if character is not None else anchor_key
        entries.append(
            {
                "idempotency_key": f"memory:identity:{version_id}",
                "source": "VISUAL_BIBLE_LOCK",
                "memory_type": "CHARACTER_IDENTITY",
                "text": f"{name} canonical identity, visual bible v{bible.version}"
                + (f" of {title}" if title else ""),
                "media_asset_ids": [
                    item
                    for item in (
                        [version.master_asset_id, version.front_asset_id] if version else []
                    )
                    if item
                ],
                # The logical asset id as well as the character id: the one
                # production consumer filters retrieval on the canonical asset
                # ids it is asking about, so a memory without one is dropped
                # before it can help a shot.
                "entity_ids": [
                    item
                    for item in [
                        identity.get("logical_asset_id"),
                        identity.get("character_id"),
                    ]
                    if item
                ],
                "asset_version_ids": [
                    item for item in [identity.get("logical_asset_version_id")] if item
                ],
                "metadata": {
                    "creative_session_id": row.id,
                    "visual_bible_id": bible.id,
                    "anchor_key": anchor_key,
                    "character_identity_version_id": version_id,
                },
            }
        )
    style_version_id = lineage.get("style_version_id")
    if style_version_id:
        style_version = session.get(AssetVersion, style_version_id)
        entries.append(
            {
                "idempotency_key": f"memory:style:{style_version_id}",
                "source": "VISUAL_BIBLE_LOCK",
                "memory_type": "STYLE",
                "text": f"locked visual style, visual bible v{bible.version}"
                + (f" of {title}" if title else ""),
                "media_asset_ids": [
                    item
                    for item in [style_version.primary_media_asset_id if style_version else None]
                    if item
                ],
                "entity_ids": [
                    item
                    for item in [
                        lineage.get("style_asset_id")
                        or (style_version.asset_id if style_version else None)
                    ]
                    if item
                ],
                "asset_version_ids": [style_version_id],
                "metadata": {
                    "creative_session_id": row.id,
                    "visual_bible_id": bible.id,
                    "style_lock_id": lineage.get("style_lock_id"),
                },
            }
        )
    for anchor_key, asset in (lineage.get("assets") or {}).items():
        if not isinstance(asset, dict) or not asset.get("asset_version_id"):
            continue
        kind = str(asset.get("kind") or "REFERENCE")
        subject = str(asset.get("subject") or anchor_key)
        entries.append(
            {
                "idempotency_key": f"memory:asset:{asset['asset_version_id']}",
                "source": "VISUAL_BIBLE_LOCK",
                "memory_type": kind,
                "text": f"{kind.lower()} {subject}, visual bible v{bible.version}"
                + (f" of {title}" if title else ""),
                "media_asset_ids": [
                    item for item in [asset.get("media_asset_id")] if item
                ],
                "entity_ids": [item for item in [asset.get("asset_id")] if item],
                "asset_version_ids": [asset["asset_version_id"]],
                "metadata": {
                    "creative_session_id": row.id,
                    "visual_bible_id": bible.id,
                    "anchor_key": anchor_key,
                    "asset_id": asset.get("asset_id"),
                },
            }
        )
    return entries


__all__ = ["bible_memories"]
