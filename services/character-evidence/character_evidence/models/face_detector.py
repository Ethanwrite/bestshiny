from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DetectedFace:
    row: Any
    box: tuple[float, float, float, float]
    landmarks: tuple[tuple[float, float], ...]
    confidence: float


class YuNetFaceDetector:
    model_name = "YuNet"
    model_version = "2026may"

    def __init__(self, weights: Path, *, score_threshold: float = 0.8):
        import cv2

        self.cv2 = cv2
        self.detector = cv2.FaceDetectorYN.create(
            str(weights),
            "",
            (320, 320),
            score_threshold,
            0.3,
            5000,
        )

    def detect(self, image: Any) -> list[DetectedFace]:
        height, width = image.shape[:2]
        self.detector.setInputSize((width, height))
        _, raw_faces = self.detector.detect(image)
        if raw_faces is None:
            return []
        faces: list[DetectedFace] = []
        for row in raw_faces:
            x, y, w, h = (float(value) for value in row[:4])
            box = (
                max(0.0, x),
                max(0.0, y),
                min(float(width), x + w),
                min(float(height), y + h),
            )
            landmarks = tuple(
                (float(row[index]), float(row[index + 1])) for index in range(4, 14, 2)
            )
            faces.append(DetectedFace(row.copy(), box, landmarks, float(row[-1])))
        return faces
