"""Video references: validated before every provider call, adapted only safely.

The image half of the reference plane is covered in
``test_media_reference_plane.py``. This file covers what changes when the
asset is a video and the provider has declared
``ProviderReferenceConstraints.video``:

1. **Nothing unvalidated reaches a provider.** The original is ffprobed even
   when it fits every bound; a derived copy is re-probed after transcoding and
   stored only if it passes the same check.
2. **Adaptation never edits meaning.** Container, codec, resolution, frame
   rate, bitrate and bytes are transport facts and may be re-encoded.
   Duration and aspect ratio are content: trimming and cropping are refused
   with the specific unmet constraint, not performed by default.
3. **Derived copies are cached by source bytes, full constraints and
   transcoder version** — change any of the three and the provider gets a new
   copy instead of a stale one.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from media_service.renditions import (
    RenditionDerivationFailed,
    RenditionResolver,
    VideoReferenceUnadaptable,
)
from media_service.video_renditions import VideoReferenceTranscoder
from production_domain.models import MediaAsset, MediaRendition, MediaRenditionKind
from provider_sdk import ProviderReferenceConstraints, VideoReferenceConstraints
from sqlalchemy import select

# Bounds a well-behaved provider might declare: mp4/h264 only, 480px box,
# 30 fps, 5 seconds, 300 KB, 2 Mbps.
_VIDEO_BOUNDS = VideoReferenceConstraints(
    accepted_containers=frozenset({"video/mp4"}),
    preferred_container="video/mp4",
    accepted_codecs=frozenset({"h264"}),
    preferred_codec="h264",
    max_width=480,
    max_height=480,
    max_frame_rate=30.0,
    max_duration_seconds=5.0,
    max_bytes=300 * 1024,
    max_bitrate_bps=2_000_000,
)
_CONSTRAINTS = ProviderReferenceConstraints(video=_VIDEO_BOUNDS)


def _encode_fixture(
    path: Path,
    *,
    size: str,
    rate: int,
    duration: float,
    codec: str,
) -> None:
    encoder = {
        "h264": ["-c:v", "libx264", "-preset", "veryfast"],
        "vp9": ["-c:v", "libvpx-vp9", "-crf", "34", "-b:v", "0"],
    }[codec]
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={size}:rate={rate}:duration={duration}",
            "-pix_fmt",
            "yuv420p",
            *encoder,
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def video_fixtures(tmp_path_factory) -> dict[str, Path]:  # type: ignore[no-untyped-def]
    """Real encoded clips, built once per session; every test re-registers them."""

    root = tmp_path_factory.mktemp("video-fixtures")
    fixtures = {
        "conforming_mp4": root / "conforming.mp4",
        "oversized_webm": root / "oversized.webm",
        "square_mp4": root / "square.mp4",
    }
    # Inside every declared bound: h264/mp4, 320x240, 30 fps, 1 s.
    _encode_fixture(fixtures["conforming_mp4"], size="320x240", rate=30, duration=1, codec="h264")
    # Outside most of them: vp9/webm, 640 px wide, 60 fps, ~365 KB.
    _encode_fixture(fixtures["oversized_webm"], size="640x360", rate=60, duration=2, codec="vp9")
    # A different shape entirely, for the aspect-ratio refusal.
    _encode_fixture(fixtures["square_mp4"], size="240x240", rate=30, duration=1, codec="h264")
    return fixtures


def _register_video(container, project_id: str, path: Path, mime_type: str):  # type: ignore[no-untyped-def]
    with path.open("rb") as stream:
        return container.media.register(
            project_id,
            "VIDEO",
            stream,
            filename=path.name,
            mime_type=mime_type,
        )[0]


def _renditions(session) -> list[MediaRendition]:  # type: ignore[no-untyped-def]
    return list(session.scalars(select(MediaRendition)).all())


# --- 1. Validation gates every path ------------------------------------------


def test_image_constraint_keys_are_unchanged_by_the_video_extension() -> None:
    """Every image rendition cached before video bounds existed keeps its key."""

    constraints = ProviderReferenceConstraints(max_pixels=3840 * 2160, max_bytes=8 * 1024 * 1024)
    assert constraints.key() == (
        "px=8294400;bytes=8388608;fmt=image/jpeg+image/png+image/webp;pref=image/jpeg"
    )
    assert ";video[" in ProviderReferenceConstraints(video=_VIDEO_BOUNDS).key()


def test_video_stays_unadaptable_for_a_consumer_that_declared_no_video_bounds(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    video_fixtures: dict[str, Path],
) -> None:
    """No declared video bounds means nobody established that video is taken."""

    asset = _register_video(container, project.id, video_fixtures["conforming_mp4"], "video/mp4")
    resolver = RenditionResolver(container.storage)

    with container.database.session() as session, pytest.raises(RenditionDerivationFailed):
        resolver.resolve(
            session,
            session.get(MediaAsset, asset.id),
            ProviderReferenceConstraints(max_bytes=16),
        )


def test_an_original_inside_every_bound_is_validated_and_sent_as_is(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    video_fixtures: dict[str, Path],
) -> None:
    asset = _register_video(container, project.id, video_fixtures["conforming_mp4"], "video/mp4")
    resolver = RenditionResolver(container.storage)

    with container.database.session() as session:
        resolved = resolver.resolve(session, session.get(MediaAsset, asset.id), _CONSTRAINTS)
        assert _renditions(session) == []

    assert resolved.is_original is True
    assert resolved.derived is False
    assert resolved.storage_key == asset.storage_key


def test_a_video_outside_the_bounds_is_transcoded_revalidated_and_stored(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    video_fixtures: dict[str, Path],
    tmp_path: Path,
) -> None:
    source = video_fixtures["oversized_webm"]
    original_bytes = source.read_bytes()
    asset = _register_video(container, project.id, source, "video/webm")
    resolver = RenditionResolver(container.storage)

    with container.database.session() as session:
        resolved = resolver.resolve(session, session.get(MediaAsset, asset.id), _CONSTRAINTS)
        rows = _renditions(session)
        assert len(rows) == 1
        metadata = dict(rows[0].metadata_json)

    assert resolved.derived is True
    assert resolved.kind == MediaRenditionKind.PROVIDER_REFERENCE.value
    assert resolved.mime_type == "video/mp4"
    assert resolved.size_bytes <= _VIDEO_BOUNDS.max_bytes

    # The stored copy is judged by what ffprobe observes in it, not by what
    # the transcoder intended: re-probe the stored bytes independently.
    transcoder = VideoReferenceTranscoder()
    with container.storage.open(resolved.storage_key, "rb") as stream:
        derived_path = tmp_path / "derived-check.mp4"
        derived_path.write_bytes(stream.read())
    facts = transcoder.probe(derived_path)
    assert facts.codec == "h264"
    assert facts.width <= 480 and facts.height <= 480
    assert facts.frame_rate <= 30.0
    assert facts.size_bytes <= _VIDEO_BOUNDS.max_bytes
    assert not transcoder.violations(
        facts,
        container_mime_type="video/mp4",
        constraints=_VIDEO_BOUNDS,
        duration_slack_seconds=0.1,
    )
    # Scaling was uniform: the 16:9 source stayed 16:9 rather than being
    # cropped or padded into some other shape.
    assert abs(facts.width / facts.height - 640 / 360) < 0.02

    # The rendition records why it exists and how it was made.
    assert metadata["transcoder_version"] == transcoder.version
    assert metadata["resolver_version"] == RenditionResolver.version
    assert "VIDEO_CODEC_NOT_ACCEPTED" in metadata["adapted_violations"]
    assert metadata["output_probe"]["codec"] == "h264"

    # And the user's original is untouched.
    with container.storage.open(asset.storage_key, "rb") as stream:
        assert stream.read() == original_bytes


# --- 2. Cache identity: source bytes, full constraints, transcoder version ---


def test_a_derived_video_rendition_is_reused_rather_than_rebuilt(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    video_fixtures: dict[str, Path],
) -> None:
    asset = _register_video(container, project.id, video_fixtures["oversized_webm"], "video/webm")
    resolver = RenditionResolver(container.storage)

    with container.database.session() as session:
        first = resolver.resolve(session, session.get(MediaAsset, asset.id), _CONSTRAINTS)
    with container.database.session() as session:
        second = resolver.resolve(session, session.get(MediaAsset, asset.id), _CONSTRAINTS)
        assert len(_renditions(session)) == 1

    assert first.storage_key == second.storage_key


def test_changed_constraints_or_transcoder_version_derive_a_new_rendition(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    video_fixtures: dict[str, Path],
) -> None:
    asset = _register_video(container, project.id, video_fixtures["oversized_webm"], "video/webm")
    resolver = RenditionResolver(container.storage)

    with container.database.session() as session:
        resolver.resolve(session, session.get(MediaAsset, asset.id), _CONSTRAINTS)

    # The provider tightens one limit: same source, new copy.
    tightened = ProviderReferenceConstraints(
        video=replace(_VIDEO_BOUNDS, max_width=320, max_height=320)
    )
    with container.database.session() as session:
        resolver.resolve(session, session.get(MediaAsset, asset.id), tightened)
        assert len(_renditions(session)) == 2

    # The transcoder changes: same source, same bounds, new copy again —
    # a cached artefact of the old invocation must not impersonate the new one.
    resolver.video_transcoder.version = "video-reference-transcoder-v2-test"
    with container.database.session() as session:
        resolver.resolve(session, session.get(MediaAsset, asset.id), _CONSTRAINTS)
        assert len(_renditions(session)) == 3


# --- 3. Semantic edits are refused, naming the constraint ---------------------


def test_an_aspect_ratio_conflict_fails_closed_instead_of_cropping(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    video_fixtures: dict[str, Path],
) -> None:
    asset = _register_video(container, project.id, video_fixtures["square_mp4"], "video/mp4")
    resolver = RenditionResolver(container.storage)
    widescreen_only = ProviderReferenceConstraints(
        video=VideoReferenceConstraints(
            accepted_aspect_ratios=frozenset({"16:9"}),
            max_bytes=8 * 1024 * 1024,
        )
    )

    with container.database.session() as session:
        with pytest.raises(VideoReferenceUnadaptable) as failure:
            resolver.resolve(session, session.get(MediaAsset, asset.id), widescreen_only)
        assert _renditions(session) == []

    assert "VIDEO_ASPECT_RATIO_NOT_ACCEPTED" in failure.value.violations
    assert "manual crop" in str(failure.value)


def test_an_over_long_video_fails_closed_instead_of_being_trimmed(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    video_fixtures: dict[str, Path],
) -> None:
    """Duration is content. Even alongside adaptable gaps, nothing is derived."""

    asset = _register_video(container, project.id, video_fixtures["oversized_webm"], "video/webm")
    resolver = RenditionResolver(container.storage)
    one_second = ProviderReferenceConstraints(
        video=VideoReferenceConstraints(
            accepted_containers=frozenset({"video/mp4"}),
            accepted_codecs=frozenset({"h264"}),
            max_duration_seconds=1.0,
        )
    )

    with container.database.session() as session:
        with pytest.raises(VideoReferenceUnadaptable) as failure:
            resolver.resolve(session, session.get(MediaAsset, asset.id), one_second)
        assert _renditions(session) == []

    assert failure.value.violations == ("VIDEO_DURATION_EXCEEDS_LIMIT",)
    assert "manual trim" in str(failure.value)


def test_an_unmeetable_byte_bound_names_the_constraint_it_cannot_meet(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    video_fixtures: dict[str, Path],
) -> None:
    """10 KB for two seconds of video is below any usable bitrate: refuse, specifically."""

    asset = _register_video(container, project.id, video_fixtures["oversized_webm"], "video/webm")
    resolver = RenditionResolver(container.storage)
    starved = ProviderReferenceConstraints(
        video=VideoReferenceConstraints(
            accepted_containers=frozenset({"video/mp4"}),
            accepted_codecs=frozenset({"h264"}),
            max_bytes=10 * 1024,
        )
    )

    with container.database.session() as session:
        with pytest.raises(VideoReferenceUnadaptable) as failure:
            resolver.resolve(session, session.get(MediaAsset, asset.id), starved)
        assert _renditions(session) == []

    assert "VIDEO_BYTES_EXCEED_LIMIT" in failure.value.violations


# --- 4. Container-only gaps, and the provider-facing boundary -----------------


def test_a_container_only_gap_is_remuxed_without_re_encoding(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    video_fixtures: dict[str, Path],
) -> None:
    asset = _register_video(container, project.id, video_fixtures["conforming_mp4"], "video/mp4")
    resolver = RenditionResolver(container.storage)
    quicktime_only = ProviderReferenceConstraints(
        video=VideoReferenceConstraints(
            accepted_containers=frozenset({"video/quicktime"}),
            preferred_container="video/quicktime",
            accepted_codecs=frozenset({"h264"}),
            max_bytes=8 * 1024 * 1024,
        )
    )

    with container.database.session() as session:
        resolved = resolver.resolve(session, session.get(MediaAsset, asset.id), quicktime_only)
        rows = _renditions(session)
        assert len(rows) == 1
        assert rows[0].metadata_json["remuxed"] is True

    assert resolved.mime_type == "video/quicktime"
    # The streams were rewrapped, not re-encoded.
    assert resolved.width == 320 and resolved.height == 240


def test_reference_urls_hand_out_only_validated_video_renditions(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    video_fixtures: dict[str, Path],
) -> None:
    """The provider-facing URL points at the validated copy, never the raw original."""

    asset = _register_video(container, project.id, video_fixtures["oversized_webm"], "video/webm")

    reference = container.media.reference_url(
        asset.id,
        project_id=project.id,
        provider="test-video-provider",
        require_https=False,
        constraints=_CONSTRAINTS,
    )

    with container.database.session() as session:
        rows = _renditions(session)
        assert len(rows) == 1
        assert rows[0].storage_key in reference
    assert asset.storage_key not in reference


def test_an_unadaptable_video_fails_the_reference_url_with_the_unmet_constraint(
    container,  # type: ignore[no-untyped-def]
    project,  # type: ignore[no-untyped-def]
    video_fixtures: dict[str, Path],
) -> None:
    from media_service import ProviderReferenceUrlUnavailable

    asset = _register_video(container, project.id, video_fixtures["oversized_webm"], "video/webm")
    one_second = ProviderReferenceConstraints(
        video=VideoReferenceConstraints(max_duration_seconds=1.0)
    )

    with pytest.raises(ProviderReferenceUrlUnavailable, match="VIDEO_DURATION_EXCEEDS_LIMIT"):
        container.media.reference_url(
            asset.id,
            project_id=project.id,
            provider="test-video-provider",
            require_https=False,
            constraints=one_second,
        )
