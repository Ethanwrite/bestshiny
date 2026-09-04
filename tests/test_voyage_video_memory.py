"""Video memories are built from bounded frames, never from a video_url.

Voyage's multimodal endpoint documents text and image content, and migration
0071 prices exactly two things for ``voyage-multimodal-3.5``: text per 1M
tokens and image input per 1B pixels. Nothing prices a video and nothing in
this repo's vendor documentation describes sending one. Memory therefore
extracts a fixed, bounded strip of stills before it embeds, records which
frames it used, and — because the whole subsystem is advisory — degrades
instead of failing the business request that triggered the indexing.
"""

from __future__ import annotations

import io
import json
import subprocess
from typing import Any

import pytest
from entitlement_core import ModelRoleRuntime, WorkspaceModelResolver
from fastapi.testclient import TestClient
from memory_core import (
    DEGRADED_EMBEDDING_PROVIDER,
    MAX_CONTENT_FRAME_PIXELS,
    MAX_FRAME_BYTES,
    MAX_FRAME_EDGE_PIXELS,
    MAX_FRAMES_PER_CONTENT,
    VIDEO_FRAME_POSITIONS,
    BoundedVideoFrameSampler,
    EmbeddingVector,
    MemoryEmbeddingUnavailable,
    MemoryLayer,
    ModelRoleEmbeddingProvider,
    MultimodalContent,
    MultimodalMemoryEngine,
    ShotMemoryInput,
    voyage_content_pieces,
)
from PIL import Image
from platform_shared import Settings
from production_domain.models import DecisionRecord, GenerationJob, JobStatus, Project, ShotMemory
from provider_sdk import (
    EmbeddingCapability,
    ProviderCapability,
    ProviderCapabilityCatalog,
    ProviderError,
    ProviderTrustLevel,
)
from sqlalchemy import select
from video_platform_api.container import build_container
from video_platform_api.main import create_app
from voyage_provider import VoyageProvider

VIDEO_URL = "https://media.invalid/v1/storage/ab/abcdef.mp4"


# ------------------------------------------------------------------ fixtures


def _png(width: int, height: int) -> bytes:
    """A PNG with enough detail that JPEG re-encoding is a real measurement."""

    image = Image.new("RGB", (width, height), (30, 90, 150))
    for x in range(0, width, 7):
        for y in range(0, height, 11):
            image.putpixel((x, y), (x % 256, y % 256, (x + y) % 256))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeFfmpeg:
    """A recorded ffprobe/ffmpeg pair: no binary, no network, no video file."""

    def __init__(self, *, duration: float = 10.0, width: int = 1920, height: int = 1080):
        self.duration = duration
        self.frame = _png(width, height)
        self.commands: list[list[str]] = []

    def __call__(self, args: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
        del timeout
        self.commands.append(list(args))
        if args[0] == "ffprobe":
            payload = json.dumps({"format": {"duration": str(self.duration)}}).encode("utf-8")
            return subprocess.CompletedProcess(args, 0, payload, b"")
        return subprocess.CompletedProcess(args, 0, self.frame, b"")

    def seeks(self) -> list[float]:
        return [
            float(args[args.index("-ss") + 1]) for args in self.commands if args[0] == "ffmpeg"
        ]

    def inputs(self) -> list[str]:
        return [args[args.index("-i") + 1] for args in self.commands if args[0] == "ffmpeg"]


def _missing_ffmpeg(args: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    del timeout
    raise FileNotFoundError(args[0])


class _RecordingEmbeddingCapability(EmbeddingCapability):
    trust_level = ProviderTrustLevel.PRODUCTION
    configured = True

    def __init__(self) -> None:
        self.inputs: list[Any] = []

    async def create_embeddings(
        self,
        *,
        model: str,
        inputs: str | list[str] | list[dict[str, Any]],
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del model
        self.inputs.append(inputs)
        dimension = int((parameters or {}).get("dimensions", 256))
        return {
            "data": [{"embedding": [1.0] * dimension}],
            "usage": {"text_tokens": 6, "image_pixels": 262_144, "video_pixels": 0},
        }


class _BrokenEmbeddingProvider:
    """An embedding backend that is simply down."""

    @property
    def provenance(self) -> Any:
        raise MemoryEmbeddingUnavailable("vector memory is unavailable")

    def embed(self, content: MultimodalContent, *, input_type: str) -> list[float]:
        del content, input_type
        raise MemoryEmbeddingUnavailable("vector memory is unavailable")

    def embed_with_provenance(
        self,
        content: MultimodalContent,
        *,
        input_type: str,
        project_id: str,
    ) -> EmbeddingVector:
        del content, input_type, project_id
        raise MemoryEmbeddingUnavailable("vector memory is unavailable")


def _voyage_runtime(container, capability: EmbeddingCapability) -> ModelRoleRuntime:  # type: ignore[no-untyped-def]
    providers = ProviderCapabilityCatalog()
    providers.register("voyage", capability, {ProviderCapability.EMBEDDINGS.value})
    return ModelRoleRuntime(
        container.database,
        WorkspaceModelResolver(container.database, container.model_infrastructure),
        providers,
        provider_mode="mock",
    )


def _voyage_memory(container, ffmpeg: _FakeFfmpeg, capability: EmbeddingCapability):  # type: ignore[no-untyped-def]
    return ModelRoleEmbeddingProvider(
        _voyage_runtime(container, capability),
        dimension=256,
        frame_sampler=BoundedVideoFrameSampler(runner=ffmpeg),
    )


@pytest.fixture
def https_container(tmp_path, database_url):  # type: ignore[no-untyped-def]
    """A container whose media URLs are HTTPS, as object storage issues them.

    The promote route only indexes a media URL it can hand to a provider, and
    the default test container serves plain HTTP.
    """

    settings = Settings(
        _env_file=None,
        database_url=database_url,
        storage_root=tmp_path / "media",
        public_base_url="https://testserver",
        flow_project_id="flow-project-test",
        worker_heartbeat_timeout_seconds=1,
        browser_command_timeout_seconds=2,
        auth_required=False,
        platform_api_key="test-platform-key",
        deployment_environment="test",
        local_reference_signing_key="test-reference-signing-key",
    )
    built = build_container(settings)
    try:
        yield built
    finally:
        built.database.engine.dispose()


@pytest.fixture
def https_project(https_container):  # type: ignore[no-untyped-def]
    with https_container.database.session() as session:
        item = Project(title="Rooftop Episode")
        session.add(item)
        session.flush()
        return item


def _completed_video_job(container, project_id: str):  # type: ignore[no-untyped-def]
    media, _created = container.media.register(
        project_id,
        "VIDEO",
        io.BytesIO(b"not-a-real-mp4-but-a-real-row"),
        filename="clip.mp4",
        mime_type="video/mp4",
    )
    with container.database.session() as session:
        job = GenerationJob(
            project_id=project_id,
            generation_type="video",
            provider="google_flow",
            model="NARWHAL",
            status=JobStatus.COMPLETED.value,
            request_json={"prompt": "a lantern rising over the rooftop"},
            request_hash="d" * 64,
            output_asset_id=media.id,
        )
        session.add(job)
        session.flush()
        return job.id, media


# ----------------------------------------------------------- frame extraction


def test_frame_sampler_takes_a_bounded_strip_at_fixed_positions() -> None:
    ffmpeg = _FakeFfmpeg(duration=10.0, width=1920, height=1080)
    sampler = BoundedVideoFrameSampler(runner=ffmpeg)

    frames, reasons = sampler.sample(VIDEO_URL)

    assert reasons == ()
    assert len(frames) == len(VIDEO_FRAME_POSITIONS)
    assert tuple(frame.normalized_position for frame in frames) == VIDEO_FRAME_POSITIONS
    # Fixed positions of a 10s clip, so a re-index of the same video compares
    # with the memory it replaces instead of sampling somewhere else.
    assert [frame.timestamp_seconds for frame in frames] == [0.5, 3.5, 6.5, 9.5]
    assert ffmpeg.seeks() == [0.5, 3.5, 6.5, 9.5]
    assert ffmpeg.inputs() == [VIDEO_URL] * len(VIDEO_FRAME_POSITIONS)
    for frame in frames:
        assert max(frame.width, frame.height) <= MAX_FRAME_EDGE_PIXELS
        assert frame.byte_length <= MAX_FRAME_BYTES
        assert frame.data_uri.startswith("data:image/jpeg;base64,")
        assert frame.source_video_url == VIDEO_URL


def test_frame_sampler_degrades_rather_than_raising_when_it_cannot_sample() -> None:
    # No ffmpeg on this host at all.
    frames, reasons = BoundedVideoFrameSampler(runner=_missing_ffmpeg).sample(VIDEO_URL)
    assert frames == () and reasons == ("FFMPEG_UNAVAILABLE",)

    # A decoder that returns nothing for every seek.
    def empty(args: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
        del timeout
        if args[0] == "ffprobe":
            return subprocess.CompletedProcess(args, 0, b'{"format": {"duration": "4.0"}}', b"")
        return subprocess.CompletedProcess(args, 1, b"", b"decode error")

    frames, reasons = BoundedVideoFrameSampler(runner=empty).sample(VIDEO_URL)
    assert frames == () and reasons == ("FRAME_EXTRACTION_FAILED",)

    # A source that is not object storage never reaches a subprocess: ffmpeg
    # would happily open file:// or redirect into a private network.
    called = _FakeFfmpeg()
    frames, reasons = BoundedVideoFrameSampler(runner=called).sample("file:///etc/passwd")
    assert frames == () and reasons == ("UNSUPPORTED_VIDEO_SOURCE",)
    assert called.commands == []


def test_frame_budget_bounds_a_content_carrying_several_videos() -> None:
    ffmpeg = _FakeFfmpeg(duration=8.0, width=640, height=360)
    content = MultimodalContent(
        text="four clips",
        video_urls=[f"{VIDEO_URL}?v={index}" for index in range(4)],
    )

    pieces, lineage = voyage_content_pieces(content, sampler=BoundedVideoFrameSampler(runner=ffmpeg))

    assert len(lineage.frames) == MAX_FRAMES_PER_CONTENT
    assert lineage.total_pixels <= MAX_CONTENT_FRAME_PIXELS
    assert "FRAME_BUDGET_EXHAUSTED" in lineage.reason_codes
    # One text piece plus the frames that fit; four videos do not buy sixteen.
    assert len(pieces) == MAX_FRAMES_PER_CONTENT + 1


# --------------------------------------------------------- the Voyage payload


def test_voyage_receives_frames_as_images_and_never_a_video_url(container, project) -> None:  # type: ignore[no-untyped-def]
    capability = _RecordingEmbeddingCapability()
    ffmpeg = _FakeFfmpeg()
    adapter = _voyage_memory(container, ffmpeg, capability)

    embedded = adapter.embed_with_provenance(
        MultimodalContent(text="rooftop lantern", video_urls=[VIDEO_URL]),
        input_type="document",
        project_id=project.id,
    )

    sent = capability.inputs[0]
    kinds = [piece["type"] for piece in sent[0]["content"]]
    assert kinds == ["text", "image_url", "image_url", "image_url", "image_url"]
    assert all(
        piece["image_url"].startswith("data:image/jpeg;base64,")
        for piece in sent[0]["content"]
        if piece["type"] == "image_url"
    )
    # The whole point of workstream 13: not "no video_url in the type field"
    # but no video URL anywhere in what crosses the boundary.
    payload = json.dumps(sent)
    assert "video_url" not in payload
    assert VIDEO_URL not in payload
    lineage = embedded.provenance.video_frame_lineage
    assert lineage is not None
    assert lineage.status.value == "EXTRACTED"
    assert [frame.timestamp_seconds for frame in lineage.frames] == [0.5, 3.5, 6.5, 9.5]


def test_a_video_that_yields_no_frames_is_never_embedded_as_text(container, project) -> None:  # type: ignore[no-untyped-def]
    capability = _RecordingEmbeddingCapability()
    adapter = ModelRoleEmbeddingProvider(
        _voyage_runtime(container, capability),
        dimension=256,
        frame_sampler=BoundedVideoFrameSampler(runner=_missing_ffmpeg),
    )

    with pytest.raises(MemoryEmbeddingUnavailable, match="FFMPEG_UNAVAILABLE"):
        adapter.embed_with_provenance(
            MultimodalContent(text="rooftop lantern", video_urls=[VIDEO_URL]),
            input_type="document",
            project_id=project.id,
        )

    # Embedding the caption alone would have produced a vector that claims to
    # stand for a clip nothing ever looked at.
    assert capability.inputs == []


async def test_the_voyage_adapter_refuses_a_video_piece_outright() -> None:
    provider = VoyageProvider(api_key="test-key")

    with pytest.raises(ProviderError, match="not video") as raised:
        await provider.create_embeddings(
            model="voyage-multimodal-3.5",
            inputs=[{"content": [{"type": "video_url", "video_url": VIDEO_URL}]}],
        )

    assert raised.value.code == "INVALID_REQUEST"


# ------------------------------------------------------------------- lineage


def test_indexing_a_video_records_which_frames_it_was_built_from(container, project) -> None:  # type: ignore[no-untyped-def]
    capability = _RecordingEmbeddingCapability()
    memory_engine = MultimodalMemoryEngine(
        container.database,
        _voyage_memory(container, _FakeFfmpeg(), capability),
        enabled=True,
    )

    memory = memory_engine.index(
        ShotMemoryInput(
            project_id=project.id,
            layer=MemoryLayer.EPISODIC,
            memory_type="ASSET_VERSION",
            content=MultimodalContent(text="rooftop lantern", video_urls=[VIDEO_URL]),
        )
    )

    lineage = memory.metadata_json["video_frame_lineage"]
    assert lineage["status"] == "EXTRACTED"
    assert lineage["sampler_version"] == BoundedVideoFrameSampler.version
    assert lineage["source_video_urls"] == [VIDEO_URL]
    assert [frame["normalized_position"] for frame in lineage["frames"]] == list(VIDEO_FRAME_POSITIONS)
    assert [frame["timestamp_seconds"] for frame in lineage["frames"]] == [0.5, 3.5, 6.5, 9.5]
    assert {frame["source_video_url"] for frame in lineage["frames"]} == {VIDEO_URL}
    assert lineage["total_pixels"] <= MAX_CONTENT_FRAME_PIXELS
    assert memory.embedding_provider == "voyage"
    # The clip itself stays on the row; the lineage says which moments of it
    # the vector actually represents.
    assert memory.video_urls == [VIDEO_URL]


def test_confirming_a_video_creation_indexes_it_through_the_frame_path(
    https_container,
    https_project,
) -> None:  # type: ignore[no-untyped-def]
    capability = _RecordingEmbeddingCapability()
    ffmpeg = _FakeFfmpeg()
    https_container.memory.embeddings = _voyage_memory(https_container, ffmpeg, capability)
    https_container.feature_flags.set("voyage_memory", True, project_id=https_project.id)
    job_id, media = _completed_video_job(https_container, https_project.id)

    with TestClient(create_app(https_container)) as client:
        response = client.post(
            f"/api/generations/{job_id}/promote",
            json={"asset_type": "SCENE", "name": "Rooftop", "promote_to_canonical": True},
        )

    assert response.status_code == 200, response.text
    assert response.json()["canonical"] is True
    memory_id = response.json()["memory_id"]
    assert memory_id
    with https_container.database.session() as session:
        memory = session.get(ShotMemory, memory_id)
        assert memory is not None
        assert memory.video_urls == [media.public_url]
        assert memory.metadata_json["video_frame_lineage"]["status"] == "EXTRACTED"
        assert len(memory.metadata_json["video_frame_lineage"]["frames"]) == len(VIDEO_FRAME_POSITIONS)
    assert ffmpeg.inputs() == [media.public_url] * len(VIDEO_FRAME_POSITIONS)
    assert "video_url" not in json.dumps(capability.inputs)


# --------------------------------------------------------------- degradation


def test_indexing_failure_is_recorded_and_leaves_a_structurally_retrievable_row(
    container,
    project,
) -> None:  # type: ignore[no-untyped-def]
    memory_engine = MultimodalMemoryEngine(container.database, _BrokenEmbeddingProvider(), enabled=True)

    memory = memory_engine.index(
        ShotMemoryInput(
            project_id=project.id,
            layer=MemoryLayer.EPISODIC,
            memory_type="ASSET_VERSION",
            content=MultimodalContent(text="rooftop lantern", video_urls=[VIDEO_URL]),
        )
    )

    assert memory.embedding == []
    assert memory.embedding_dimension == 0
    assert memory.embedding_provider == DEGRADED_EMBEDDING_PROVIDER
    assert memory.metadata_json["vector_degraded"] is True
    with container.database.session() as session:
        record = session.scalar(
            select(DecisionRecord).where(
                DecisionRecord.project_id == project.id,
                DecisionRecord.decision_type == "MEMORY_VECTOR_DEGRADED",
            )
        )
        assert record is not None
        assert record.selected_action == "STRUCTURED_TIMELINE_ONLY"


def test_a_saved_creation_survives_an_embedding_outage(https_container, https_project) -> None:  # type: ignore[no-untyped-def]
    https_container.memory.embeddings = _BrokenEmbeddingProvider()
    https_container.feature_flags.set("voyage_memory", True, project_id=https_project.id)
    job_id, _media = _completed_video_job(https_container, https_project.id)

    with TestClient(create_app(https_container)) as client:
        response = client.post(
            f"/api/generations/{job_id}/promote",
            json={"asset_type": "SCENE", "name": "Rooftop", "promote_to_canonical": True},
        )

    # The version exists and the promotion stands: memory is advisory and gets
    # no vote on whether the user's creation was saved.
    assert response.status_code == 200, response.text
    assert response.json()["canonical"] is True
    assert response.json()["version"]["id"]
    with https_container.database.session() as session:
        memory = session.get(ShotMemory, response.json()["memory_id"])
        assert memory is not None and memory.embedding_provider == DEGRADED_EMBEDDING_PROVIDER
        assert (
            session.scalar(
                select(DecisionRecord).where(
                    DecisionRecord.decision_type == "MEMORY_VECTOR_DEGRADED",
                )
            )
            is not None
        )


def test_the_promote_route_holds_the_boundary_even_if_indexing_raises(
    https_container,
    https_project,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    def unavailable(_value: ShotMemoryInput) -> ShotMemory:
        raise MemoryEmbeddingUnavailable("vector memory is unavailable")

    monkeypatch.setattr(https_container.memory, "index", unavailable)
    https_container.feature_flags.set("voyage_memory", True, project_id=https_project.id)
    job_id, _media = _completed_video_job(https_container, https_project.id)

    with TestClient(create_app(https_container)) as client:
        response = client.post(
            f"/api/generations/{job_id}/promote",
            json={"asset_type": "SCENE", "name": "Rooftop"},
        )

    assert response.status_code == 200, response.text
    assert response.json()["memory_id"] is None
    assert response.json()["version"]["id"]
