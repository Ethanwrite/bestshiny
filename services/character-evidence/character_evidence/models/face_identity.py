from __future__ import annotations

from pathlib import Path
from typing import Any

from .face_detector import DetectedFace


class SFaceIdentityEncoder:
    model_name = "SFace"
    model_version = "2021dec"

    def __init__(self, weights: Path):
        import cv2

        self.cv2 = cv2
        self.recognizer = cv2.FaceRecognizerSF.create(str(weights), "")

    def encode_aligned(self, image: Any, face: DetectedFace) -> Any:
        """YuNet five landmarks -> SFace alignCrop -> embedding."""

        import numpy as np

        aligned = self.recognizer.alignCrop(image, face.row)
        if aligned is None or aligned.size == 0:
            raise RuntimeError("SFace five-point alignment produced an empty face")
        feature = self.recognizer.feature(aligned).reshape(-1).astype("float32")
        norm = float(np.linalg.norm(feature))
        if not norm:
            raise RuntimeError("SFace produced a zero embedding")
        return feature / norm

    @staticmethod
    def cosine(left: Any, right: Any) -> float:
        import numpy as np

        return max(-1.0, min(1.0, float(np.dot(left, right))))
