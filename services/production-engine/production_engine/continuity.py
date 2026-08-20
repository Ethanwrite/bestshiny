from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Protocol

from media_service import MediaRegistry
from platform_database import Database
from production_domain.models import AssetType, ContinuityMode, Episode, MediaAsset, Scene, Shot
from sqlalchemy import select


class FrameQualityDetector(Protocol):
    def usable(self, image_path: Path) -> bool: ...


class AcceptAnyFrame:
    def usable(self, image_path: Path) -> bool:
        return image_path.exists() and image_path.stat().st_size > 128


class EndFrameExtractor:
    def __init__(
        self, quality_detector: FrameQualityDetector | None = None, safe_offset_seconds: float = 0.15
    ):
        self.quality_detector = quality_detector or AcceptAnyFrame()
        self.safe_offset_seconds = max(0.05, safe_offset_seconds)

    @staticmethod
    def duration(path: Path) -> float:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return float(result.stdout.strip())

    def extract(self, video_path: Path, output_path: Path) -> Path:
        duration = self.duration(video_path)
        candidates = [max(0, duration - offset) for offset in (self.safe_offset_seconds, 0.35, 0.6, 1.0)]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        for timestamp in candidates:
            try:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-ss",
                        f"{timestamp:.3f}",
                        "-i",
                        str(video_path),
                        "-frames:v",
                        "1",
                        "-q:v",
                        "2",
                        str(output_path),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                if self.quality_detector.usable(output_path):
                    return output_path
            except (OSError, subprocess.SubprocessError) as exc:
                last_error = exc
        raise RuntimeError(f"could not extract a usable end frame: {last_error or 'quality rejected'}")


class ShotContinuityService:
    def __init__(self, database: Database, media: MediaRegistry, extractor: EndFrameExtractor | None = None):
        self.database = database
        self.media = media
        self.extractor = extractor or EndFrameExtractor()

    @staticmethod
    def _project_id(session, shot: Shot) -> str:  # type: ignore[no-untyped-def]
        scene = session.get(Scene, shot.scene_id)
        episode = session.get(Episode, scene.episode_id) if scene else None
        if not episode:
            raise LookupError("shot project could not be resolved")
        return episode.project_id

    def attach_previous_end_frame(self, shot_id: str) -> str | None:
        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            if (
                not shot
                or shot.continuity_mode != ContinuityMode.PREVIOUS_END_FRAME.value
                or not shot.previous_shot_id
            ):
                return None
            previous = session.get(Shot, shot.previous_shot_id)
            if not previous or not previous.end_frame_asset_id:
                return None
            project_id = self._project_id(session, shot)
            if self._project_id(session, previous) != project_id:
                raise LookupError("previous shot must belong to the same project")
            end_frame = session.get(MediaAsset, previous.end_frame_asset_id)
            if not end_frame or end_frame.project_id != project_id:
                raise LookupError("previous end frame must belong to the shot project")
            shot.start_frame_asset_id = previous.end_frame_asset_id
            return shot.start_frame_asset_id

    def extract_end_frame(self, shot_id: str, video_asset_id: str) -> MediaAsset:
        """Extract/register an end frame without mutating canonical shot state."""

        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            video = session.get(MediaAsset, video_asset_id)
            if not shot or not video or not video.local_path:
                raise LookupError("shot or local output video is missing")
            project_id = self._project_id(session, shot)
            if video.project_id != project_id:
                raise LookupError("output video must belong to the shot project")
            video_path = Path(video.local_path)
        with tempfile.TemporaryDirectory(prefix="end-frame-") as temp_dir:
            frame_path = self.extractor.extract(video_path, Path(temp_dir) / "end-frame.jpg")
            with frame_path.open("rb") as stream:
                end_frame, _ = self.media.register(
                    project_id,
                    AssetType.END_FRAME.value,
                    stream,
                    filename=f"shot-{shot_id}-end.jpg",
                    mime_type="image/jpeg",
                    shot_id=shot_id,
                    parent_asset_id=video_asset_id,
                    generation_candidate_id=video.generation_candidate_id,
                    metadata={
                        "source_video_asset_id": video_asset_id,
                        "safe_offset_seconds": self.extractor.safe_offset_seconds,
                    },
                )
        return end_frame

    def chain_existing_end_frame(
        self,
        session,  # type: ignore[no-untyped-def]
        shot: Shot,
        video: MediaAsset,
        end_frame: MediaAsset,
    ) -> None:
        """Apply continuity links inside the caller's canonical commit transaction."""

        project_id = self._project_id(session, shot)
        if video.project_id != project_id or end_frame.project_id != project_id:
            raise LookupError("output video and end frame must belong to the shot project")
        if end_frame.parent_asset_id != video.id or end_frame.shot_id != shot.id:
            raise LookupError("end frame lineage does not match the committed shot output")
        shot.output_video_asset_id = video.id
        shot.end_frame_asset_id = end_frame.id
        next_shot = session.scalar(
            select(Shot)
            .join(Scene, Shot.scene_id == Scene.id)
            .join(Episode, Scene.episode_id == Episode.id)
            .where(Shot.previous_shot_id == shot.id, Episode.project_id == project_id)
            .order_by(Scene.sequence, Shot.sequence)
        )
        if next_shot and next_shot.continuity_mode in {
            ContinuityMode.PREVIOUS_END_FRAME.value,
            ContinuityMode.HARD_CONTINUITY.value,
        }:
            next_shot.start_frame_asset_id = end_frame.id

    def extract_and_chain(self, shot_id: str, video_asset_id: str) -> MediaAsset:
        """Compatibility helper for explicit continuity recovery outside candidate commit."""

        end_frame = self.extract_end_frame(shot_id, video_asset_id)
        with self.database.session() as session:
            shot = session.get(Shot, shot_id)
            video = session.get(MediaAsset, video_asset_id)
            current_end_frame = session.get(MediaAsset, end_frame.id)
            if not shot or not video or not current_end_frame:
                raise LookupError("shot output disappeared before continuity could be chained")
            self.chain_existing_end_frame(session, shot, video, current_end_frame)
            session.flush()
            return session.get(MediaAsset, end_frame.id)
