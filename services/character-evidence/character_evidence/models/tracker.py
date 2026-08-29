from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from .detector import PersonDetection


@dataclass(frozen=True)
class TrackedPerson:
    track_id: int
    box: tuple[float, float, float, float]
    confidence: float
    detection_confidence: float


def _iou(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    x1 = max(left[0], right[0])
    y1 = max(left[1], right[1])
    x2 = min(left[2], right[2])
    y2 = min(left[3], right[3])
    intersection = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


class ByteTrackTracker:
    model_name = "ByteTrack"
    model_version = "d1bf0191"

    def __init__(self, *, frame_rate: float):
        import numpy as np

        # This pinned upstream ByteTrack revision still references the removed
        # NumPy alias `np.float`. Keep the official source byte-for-byte (and
        # therefore hash-verifiable) while supplying its historical alias.
        if not hasattr(np, "float"):
            np.float = float  # type: ignore[attr-defined]
        from yolox.tracker.byte_tracker import BYTETracker

        args = SimpleNamespace(track_thresh=0.5, track_buffer=30, match_thresh=0.8, mot20=False)
        self.tracker = BYTETracker(args, frame_rate=max(1, round(frame_rate)))

    def update(
        self,
        detections: list[PersonDetection],
        *,
        image_shape: tuple[int, int],
    ) -> list[TrackedPerson]:
        import numpy as np

        observations = np.asarray(
            [[*item.box, item.confidence] for item in detections],
            dtype=np.float32,
        ).reshape((-1, 5))
        height, width = image_shape
        online: list[Any] = self.tracker.update(observations, (height, width), (height, width))
        results: list[TrackedPerson] = []
        for track in online:
            values = [float(value) for value in track.tlbr]
            box = (values[0], values[1], values[2], values[3])
            nearest = max(detections, key=lambda item: _iou(box, item.box), default=None)
            detection_confidence = nearest.confidence if nearest is not None else float(track.score)
            results.append(
                TrackedPerson(
                    track_id=int(track.track_id),
                    box=box,
                    confidence=max(0.0, min(1.0, float(track.score))),
                    detection_confidence=max(0.0, min(1.0, detection_confidence)),
                )
            )
        return results
