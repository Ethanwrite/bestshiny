from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PersonDetection:
    box: tuple[float, float, float, float]
    confidence: float


class YOLOXPersonDetector:
    model_name = "YOLOX-s"
    model_version = "0.1.1rc0"

    def __init__(self, weights: Path, *, confidence: float = 0.30, nms: float = 0.45):
        import torch
        from yolox.data.data_augment import ValTransform
        from yolox.exp import get_exp
        from yolox.utils import postprocess

        self.torch = torch
        self.postprocess = postprocess
        self.preprocess = ValTransform(legacy=False)
        self.exp = get_exp(exp_name="yolox-s")
        self.confidence = confidence
        self.nms = nms
        self.device = torch.device("cuda")
        model = self.exp.get_model()
        checkpoint: dict[str, Any] = torch.load(weights, map_location="cpu", weights_only=False)
        state = checkpoint.get("model")
        if not isinstance(state, dict):
            raise RuntimeError("YOLOX-s checkpoint does not contain the official model state")
        model.load_state_dict(state)
        self.model = model.eval().to(self.device)

    def warmup(self) -> None:
        torch = self.torch
        height, width = self.exp.test_size
        with torch.inference_mode():
            self.model(torch.zeros((1, 3, height, width), device=self.device))

    def detect(self, image: Any) -> list[PersonDetection]:
        torch = self.torch
        height, width = image.shape[:2]
        prepared, _ = self.preprocess(image, None, self.exp.test_size)
        ratio = min(self.exp.test_size[0] / height, self.exp.test_size[1] / width)
        tensor = torch.from_numpy(prepared).unsqueeze(0).float().to(self.device)
        with torch.inference_mode():
            output = self.model(tensor)
            predictions = self.postprocess(
                output,
                self.exp.num_classes,
                self.confidence,
                self.nms,
                class_agnostic=True,
            )[0]
        if predictions is None:
            return []
        predictions = predictions.detach().cpu()
        results: list[PersonDetection] = []
        for row in predictions:
            if int(row[6].item()) != 0:  # COCO person class only
                continue
            x1, y1, x2, y2 = (float(value) / ratio for value in row[:4])
            x1, x2 = sorted((max(0.0, min(width, x1)), max(0.0, min(width, x2))))
            y1, y2 = sorted((max(0.0, min(height, y1)), max(0.0, min(height, y2))))
            if x2 - x1 < 2 or y2 - y1 < 2:
                continue
            confidence = float(row[4] * row[5])
            results.append(PersonDetection((x1, y1, x2, y2), confidence))
        return results
