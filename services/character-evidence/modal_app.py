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
        "git clone https://github.com/FoundationVision/ByteTrack.git /opt/ByteTrack",
        "git -C /opt/ByteTrack checkout d1bf0191adff59bc8fcfeaa0b33d3d1642552a99",
        # Before the install, so the installed distribution contains the tracker
        # rather than depending on the source tree being copied into afterwards.
        "cp -R /opt/ByteTrack/yolox/tracker /opt/YOLOX/yolox/tracker",
        # YOLOX's setup.py imports torch at build time, and PEP 517 build
        # isolation hides the torch installed above behind a fresh environment,
        # so the build dies on ModuleNotFoundError before compiling anything.
        #
        # This is a plain install rather than `-e`, and that is the fix. pip's
        # legacy editable path hands off to `setup.py develop`, which re-invokes
        # `pip install -e . --use-pep517 --no-deps` in a *new* process that
        # rebuilds an isolated environment; neither --no-build-isolation nor
        # PIP_NO_BUILD_ISOLATION survives that hop, both of which were tried.
        # A non-editable install calls the build backend in this environment,
        # where the pinned torch already is. `PYTHONPATH` still puts
        # /opt/YOLOX first, so imports resolve to the pinned source tree either
        # way -- the install is here for the build, not for the import path.
        "PIP_NO_BUILD_ISOLATION=1 python -m pip install --no-deps /opt/YOLOX",
        # Fail loudly here rather than at the first inference request.
        "python -c \"import torch, yolox; print('torch', torch.__version__, 'yolox ok')\"",
        # Public release asset, fetched the same way DINOv2 is. `gh release
        # download` needs GH_TOKEN even for a public repository and exited 4
        # asking for `gh auth login`, which a build container cannot do. The
        # pinned SHA-256 on the next line is what makes swapping the transport
        # safe: a wrong URL fails the checksum rather than shipping quietly.
        "curl --fail --location --output /models/yolox_s.pth "
        "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/"
        "0.1.1rc0/yolox_s.pth",
        "echo 'f55ded7181e1b0c13285c56e7790b8f0e8f8db590fe4edb37f0b7f345c913a30  "
        "/models/yolox_s.pth' | sha256sum -c -",
        # opencv_zoo ships these two only through git LFS, and that repository's
        # LFS budget is exhausted: both a full clone and a targeted `lfs pull`
        # die with "This repository exceeded its LFS budget", which belongs to
        # the upstream account and cannot be fixed from this build. GitHub's own
        # media endpoint serves the same objects at the same pinned commit. The
        # bytes were compared against the SHA-256 values below before this was
        # changed and match exactly -- content-addressed artifacts are what makes
        # a transport swap checkable rather than a leap of faith, and the
        # sha256sum lines that follow re-check it on every build.
        "curl --fail --location --output /models/face_detection_yunet_2026may.onnx "
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/"
        "47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_detection_yunet/"
        "face_detection_yunet_2026may.onnx",
        "curl --fail --location --output /models/face_recognition_sface_2021dec.onnx "
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/"
        "47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_recognition_sface/"
        "face_recognition_sface_2021dec.onnx",
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
    # `.env()` counts as a build step, so it has to come before the
    # `add_local_*` calls: Modal refuses an image that builds after adding local
    # files, because those are mounted at container start rather than baked in.
    .env(
        {
            "PYTHONPATH": "/opt/character_evidence:/opt/YOLOX",
            "CHARACTER_EVIDENCE_THRESHOLDS_PATH": "/opt/character_evidence/config/thresholds-v1.json",
        }
    )
    .add_local_dir("services/character-evidence", remote_path="/opt/character_evidence")
    .add_local_file(
        "config/character-evidence/thresholds-v1.json",
        remote_path="/opt/character_evidence/config/thresholds-v1.json",
    )
)

app = modal.App(APP_NAME)
secret = modal.Secret.from_name(SECRET_NAME)

#: Accepted job identities, persisted server-side by Modal across containers.
#: `Dict.put(..., skip_if_exists=True)` is the atomic claim: it returns False
#: when the key already exists (modal.Dict reference), so a re-POSTed
#: candidate_id is acknowledged without spawning a second GPU job.
JOBS_DICT_NAME = "bestshiny-character-evidence-jobs"

#: Callback envelopes whose delivery to BestShiny failed after the worker's
#: in-process retries. Drained by the scheduled redelivery below, so a
#: produced result survives BestShiny being briefly unreachable instead of
#: dying with one POST. (Queue partitions expire on `partition_ttl`; the
#: redelivery schedule runs far inside that window.)
OUTBOX_QUEUE_NAME = "bestshiny-character-evidence-outbox"
OUTBOX_PARTITION_TTL_SECONDS = 7 * 24 * 3600
OUTBOX_MAX_REDELIVERY_ATTEMPTS = 60


def _jobs_dict():  # type: ignore[no-untyped-def]
    return modal.Dict.from_name(JOBS_DICT_NAME, create_if_missing=True)


def _outbox_queue():  # type: ignore[no-untyped-def]
    return modal.Queue.from_name(OUTBOX_QUEUE_NAME, create_if_missing=True)


def _spool(item: dict) -> None:
    _outbox_queue().put(item, partition_ttl=OUTBOX_PARTITION_TTL_SECONDS)


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
        from character_evidence.api import deliver_or_spool, failure_envelope
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
        # Delivery failure spools the envelope rather than losing it: the
        # scheduled redelivery owns the long tail, and BestShiny's own
        # ACCEPTED-timeout scan covers the case where even that fails.
        deliver_or_spool(callback, _spool)


@app.function(
    image=image,
    min_containers=0,
    max_containers=1,
    scaledown_window=60,
    secrets=[secret],
)
@modal.asgi_app()
def https_api():
    import time

    from character_evidence.api import create_api

    jobs = _jobs_dict()

    def claim_job(job_id: str) -> bool:
        return bool(jobs.put(job_id, {"accepted_at": int(time.time())}, skip_if_exists=True))

    return create_api(lambda payload: CVWorker().analyze.spawn(payload), claim_job=claim_job)


@app.function(
    image=image,
    secrets=[secret],
    schedule=modal.Period(minutes=5),
    timeout=240,
)
def redeliver_callbacks() -> None:
    """Drain the callback outbox until BestShiny acknowledges each envelope.

    Non-blocking gets: an empty queue ends the run. An envelope that still
    cannot be delivered goes back on the queue with its attempt count; one
    that exhausts the budget moves to the 'dead' partition, where it stays
    visible for operators for the partition TTL instead of vanishing.
    """

    from character_evidence.api import deliver_callback
    from character_evidence.schemas import CallbackEnvelope

    queue = _outbox_queue()
    for _ in range(100):
        item = queue.get(block=False)
        if item is None:
            return
        envelope = CallbackEnvelope.model_validate(item["envelope"])
        try:
            deliver_callback(envelope)
        except RuntimeError:
            attempts = int(item.get("attempts", 0)) + 1
            item["attempts"] = attempts
            if attempts >= OUTBOX_MAX_REDELIVERY_ATTEMPTS:
                queue.put(
                    item, partition="dead", partition_ttl=OUTBOX_PARTITION_TTL_SECONDS
                )
            else:
                queue.put(item, partition_ttl=OUTBOX_PARTITION_TTL_SECONDS)
