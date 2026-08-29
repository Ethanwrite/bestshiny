from __future__ import annotations

import modal

APP_NAME = "bestshiny-character-evidence"
SECRET_NAME = "bestshiny-character-evidence-secrets"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install(
        "build-essential",
        "curl",
        "ffmpeg",
        "gh",
        "git",
        "git-lfs",
        "libgl1",
        "libglib2.0-0",
    )
    .uv_pip_install(
        "cython==3.1.3",
        "cython-bbox==0.1.5",
        "fastapi==0.128.0",
        "httpx==0.28.1",
        "lap==0.5.12",
        "loguru==0.7.3",
        "numpy==2.2.6",
        "ninja==1.13.0",
        "opencv-python-headless==4.12.0.88",
        "pillow==12.0.0",
        "pydantic==2.12.5",
        "pycocotools==2.0.10",
        "scikit-image==0.25.2",
        "scipy==1.16.1",
        "tabulate==0.9.0",
        "thop==0.1.1.post2209072238",
        "torch==2.8.0",
        "torchvision==0.23.0",
        "tqdm==4.67.1",
    )
    .run_commands(
        "mkdir -p /models /opt",
        "git clone https://github.com/Megvii-BaseDetection/YOLOX.git /opt/YOLOX",
        "git -C /opt/YOLOX checkout e1052df71842031413f6030723c3607b839c80ce",
        "python -m pip install --no-deps -e /opt/YOLOX",
        "git clone https://github.com/FoundationVision/ByteTrack.git /opt/ByteTrack",
        "git -C /opt/ByteTrack checkout d1bf0191adff59bc8fcfeaa0b33d3d1642552a99",
        "cp -R /opt/ByteTrack/yolox/tracker /opt/YOLOX/yolox/tracker",
        "gh release download 0.1.1rc0 -R Megvii-BaseDetection/YOLOX -p yolox_s.pth -D /models",
        "echo 'f55ded7181e1b0c13285c56e7790b8f0e8f8db590fe4edb37f0b7f345c913a30  "
        "/models/yolox_s.pth' | sha256sum -c -",
        "git clone https://github.com/opencv/opencv_zoo.git /opt/opencv_zoo",
        "git -C /opt/opencv_zoo checkout 47534e27c9851bb1128ccc0102f1145e27f23f98",
        "git -C /opt/opencv_zoo lfs pull --include='models/face_detection_yunet/"
        "face_detection_yunet_2026may.onnx,models/face_recognition_sface/"
        "face_recognition_sface_2021dec.onnx'",
        "cp /opt/opencv_zoo/models/face_detection_yunet/face_detection_yunet_2026may.onnx /models/",
        "cp /opt/opencv_zoo/models/face_recognition_sface/face_recognition_sface_2021dec.onnx /models/",
        "echo 'ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0  "
        "/models/face_detection_yunet_2026may.onnx' | sha256sum -c -",
        "echo '0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79  "
        "/models/face_recognition_sface_2021dec.onnx' | sha256sum -c -",
        "git clone https://github.com/facebookresearch/dinov2.git /opt/dinov2",
        "git -C /opt/dinov2 checkout 7764ea0f912e53c92e82eb78a2a1631e92725fc8",
        "curl --fail --location --output /models/dinov2_vitb14_pretrain.pth "
        "https://dl.fbaipublicfiles.com/dinov2/dinov2_vitb14/"
        "dinov2_vitb14_pretrain.pth",
        "echo '0b8b82f85de91b424aded121c7e1dcc2b7bc6d0adeea651bf73a13307fad8c73  "
        "/models/dinov2_vitb14_pretrain.pth' | sha256sum -c -",
        "echo '7cecdcdd7998103969a4ba1772f4c9fb5560fd5eef05ca03e0d2df28346ca50b  "
        "/opt/YOLOX/yolox/tracker/byte_tracker.py' | sha256sum -c -",
    )
    .add_local_dir("services/character-evidence", remote_path="/opt/character_evidence")
    .add_local_file(
        "config/character-evidence/thresholds-v1.json",
        remote_path="/opt/character_evidence/config/thresholds-v1.json",
    )
    .env(
        {
            "PYTHONPATH": "/opt/character_evidence:/opt/YOLOX",
            "CHARACTER_EVIDENCE_THRESHOLDS_PATH": "/opt/character_evidence/config/thresholds-v1.json",
        }
    )
)

app = modal.App(APP_NAME)
secret = modal.Secret.from_name(SECRET_NAME)


@app.cls(
    image=image,
    gpu="T4",
    min_containers=0,
    max_containers=1,
    scaledown_window=60,
    timeout=3600,
    secrets=[secret],
)
class CVWorker:
    @modal.enter()
    def load(self) -> None:
        from character_evidence.pipeline import CharacterEvidencePipeline

        self.pipeline = CharacterEvidencePipeline()

    @modal.method()
    def analyze(self, payload: dict) -> None:
        from character_evidence.api import deliver_callback, failure_envelope
        from character_evidence.schemas import AnalyzeRequest, CallbackEnvelope

        try:
            request = AnalyzeRequest.model_validate(payload)
            reports = self.pipeline.analyze(request)
            callback = CallbackEnvelope(
                job_id=request.job_id,
                project_id=request.project_id,
                shot_id=request.shot_id,
                status="SUCCEEDED",
                reports=reports,
            )
        except Exception as exc:
            callback = failure_envelope(payload, exc)
        deliver_callback(callback)


@app.function(
    image=image,
    min_containers=0,
    max_containers=1,
    scaledown_window=60,
    secrets=[secret],
)
@modal.asgi_app()
def https_api():
    from character_evidence.api import create_api

    return create_api(lambda payload: CVWorker().analyze.spawn(payload))
