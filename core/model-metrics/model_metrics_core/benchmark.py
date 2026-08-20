from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from model_registry_core import ShotRequirements
from platform_database import Database
from production_domain.models import ModelBenchmarkResult
from sqlalchemy import select


@dataclass(frozen=True)
class BenchmarkCase:
    key: str
    label: str
    requirements: ShotRequirements
    dimensions: tuple[str, ...]


class ModelBenchmarkSuite:
    version = "visual-production-benchmark-v1"

    cases = (
        BenchmarkCase(
            "portrait_consistency",
            "Portrait identity consistency",
            ShotRequirements(requires_character_consistency=True, requires_reference_images=True),
            ("character_consistency",),
        ),
        BenchmarkCase(
            "profile_preservation",
            "Profile identity and orientation",
            ShotRequirements(
                requires_character_consistency=True,
                requires_end_frame_profile=True,
                forbid_camera_gaze=True,
            ),
            ("character_consistency", "camera_control"),
        ),
        BenchmarkCase(
            "rear_view_ending",
            "Rear-view ending without camera gaze",
            ShotRequirements(requires_rear_view_ending=True, forbid_camera_gaze=True),
            ("character_consistency", "camera_control"),
        ),
        BenchmarkCase(
            "two_character_dialogue",
            "Two-character Chinese dialogue",
            ShotRequirements(
                profile="dialogue",
                characters=2,
                requires_multi_character=True,
                requires_dialogue=True,
                requires_chinese_dialogue=True,
            ),
            ("dialogue", "chinese_dialogue", "multi_character"),
        ),
        BenchmarkCase(
            "running",
            "Physical running action",
            ShotRequirements(
                profile="action",
                requires_complex_action=True,
                requires_physical_plausibility=True,
            ),
            ("complex_motion", "physical_plausibility"),
        ),
        BenchmarkCase(
            "object_interaction",
            "Character-object interaction",
            ShotRequirements(profile="action", requires_complex_action=True),
            ("complex_motion", "product_fidelity"),
        ),
        BenchmarkCase(
            "camera_orbit",
            "Single camera orbit",
            ShotRequirements(requires_camera_control=True),
            ("camera_control",),
        ),
        BenchmarkCase(
            "chinese_text",
            "Chinese text rendering",
            ShotRequirements(requires_text_rendering=True),
            ("text_rendering",),
        ),
        BenchmarkCase(
            "product_logo",
            "Product and logo preservation",
            ShotRequirements(profile="commercial_hero", product_fidelity_priority=1),
            ("product_fidelity", "visual_quality"),
        ),
        BenchmarkCase(
            "night_rain",
            "Night lighting and rain",
            ShotRequirements(profile="commercial_hero", visual_quality_priority=1),
            ("lighting", "visual_quality"),
        ),
        BenchmarkCase(
            "long_take",
            "Long continuous take",
            ShotRequirements(duration=30),
            ("long_form", "scene_consistency"),
        ),
        BenchmarkCase(
            "multi_character_blocking",
            "Multi-character blocking",
            ShotRequirements(characters=3, requires_multi_character=True, requires_camera_control=True),
            ("multi_character", "camera_control"),
        ),
    )

    def __init__(self, database: Database):
        self.database = database

    def manifest(self) -> list[dict[str, Any]]:
        return [
            {
                "key": case.key,
                "label": case.label,
                "requirements": case.requirements.model_dump(),
                "dimensions": list(case.dimensions),
            }
            for case in self.cases
        ]

    def record(
        self,
        *,
        provider: str,
        model_id: str,
        model_version: str,
        case_key: str,
        scores: dict[str, float],
        passed: bool,
        evidence_asset_ids: list[str] | None = None,
    ) -> ModelBenchmarkResult:
        case = next((item for item in self.cases if item.key == case_key), None)
        if not case:
            raise KeyError(f"unknown benchmark case: {case_key}")
        normalized = {
            dimension: max(0.0, min(1.0, float(scores.get(dimension, 0.0)))) for dimension in case.dimensions
        }
        with self.database.session() as session:
            record = ModelBenchmarkResult(
                suite_version=self.version,
                case_key=case.key,
                provider=provider,
                model_id=model_id,
                model_version=model_version,
                passed=passed,
                scores_json=normalized,
                evidence_asset_ids=evidence_asset_ids or [],
            )
            session.add(record)
            session.flush()
            return record

    def adjustments(self) -> dict[str, dict[str, float]]:
        with self.database.session() as session:
            records = list(
                session.scalars(
                    select(ModelBenchmarkResult).where(ModelBenchmarkResult.suite_version == self.version)
                )
            )
        totals: dict[str, dict[str, list[float]]] = {}
        for record in records:
            key = f"{record.provider}:{record.model_id}"
            model_totals = totals.setdefault(key, {})
            for dimension, score in record.scores_json.items():
                model_totals.setdefault(dimension, []).append(float(score))
        return {
            key: {dimension: sum(scores) / len(scores) for dimension, scores in dimensions.items()}
            for key, dimensions in totals.items()
        }
