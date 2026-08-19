from __future__ import annotations

from platform_contracts import GenerationRequest
from platform_database import Database
from production_domain.models import Shot


class ProductionEngine:
    """Provider-neutral conversion from an approved Shot into a generation request."""

    def __init__(self, database: Database):
        self.database = database

    def request_for_shot(self, shot_id: str, idempotency_key: str) -> GenerationRequest:
        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            if not shot:
                raise LookupError(f"shot not found: {shot_id}")
            project_id = shot.scene.episode.project_id
            return GenerationRequest(
                project_id=project_id,
                shot_id=shot.id,
                type="video",
                provider=shot.provider,
                model=shot.model,
                prompt=shot.prompt,
                negative_prompt=shot.negative_prompt,
                duration=shot.duration,
                start_frame_asset_id=shot.start_frame_asset_id,
                end_frame_asset_id=shot.end_frame_asset_id,
                generation_policy=shot.generation_policy,
                idempotency_key=idempotency_key,
            )
