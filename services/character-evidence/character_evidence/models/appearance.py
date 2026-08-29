from __future__ import annotations

from pathlib import Path
from typing import Any


class DINOv2AppearanceEncoder:
    model_name = "DINOv2-base"
    model_version = "dinov2_vitb14"

    def __init__(self, source: Path, weights: Path):
        import torch
        from torchvision import transforms

        self.torch = torch
        self.device = torch.device("cuda")
        self.model = torch.hub.load(
            str(source),
            "dinov2_vitb14",
            source="local",
            pretrained=False,
        )
        state = torch.load(weights, map_location="cpu", weights_only=True)
        if not isinstance(state, dict):
            raise RuntimeError("DINOv2-base checkpoint is not an official state dictionary")
        self.model.load_state_dict(state, strict=True)
        self.model = self.model.eval().to(self.device)
        self.transform = transforms.Compose(
            [
                transforms.Resize(256),
                transforms.CenterCrop(224),
                transforms.ToTensor(),
                transforms.Normalize(
                    mean=(0.485, 0.456, 0.406),
                    std=(0.229, 0.224, 0.225),
                ),
            ]
        )

    def warmup(self) -> None:
        torch = self.torch
        with torch.inference_mode():
            self.model(torch.zeros((1, 3, 224, 224), device=self.device))

    def encode(self, bgr_image: Any) -> Any:
        import cv2
        import numpy as np
        from PIL import Image

        rgb = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2RGB)
        tensor = self.transform(Image.fromarray(rgb)).unsqueeze(0).to(self.device)
        with self.torch.inference_mode():
            feature = self.model(tensor).detach().float().cpu().numpy().reshape(-1)
        norm = float(np.linalg.norm(feature))
        if not norm:
            raise RuntimeError("DINOv2 produced a zero embedding")
        return feature / norm

    @staticmethod
    def cosine(left: Any, right: Any) -> float:
        import numpy as np

        return max(-1.0, min(1.0, float(np.dot(left, right))))
