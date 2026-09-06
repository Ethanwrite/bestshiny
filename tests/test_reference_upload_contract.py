"""One reference upload allowlist, front and back.

The server accepts PNG, JPEG and WebP for a reference image and refuses a
filename/MIME pair that disagrees. The web app used to gate on ``image/*``
while its own copy named PNG/JPG/WebP, so a GIF or HEIC passed every client
check and died at the API behind a one-sentence error. The app now carries the
server's table verbatim; these tests keep the two from drifting apart and pin
that the server's refusal names the real reason.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from platform_shared.media_validation import _IMAGE_TYPES
from video_platform_api import create_app

WEB = Path(__file__).resolve().parents[1] / "apps" / "web"


def _frontend_types() -> dict[str, str]:
    source = (WEB / "app.js").read_text(encoding="utf-8")
    block = re.search(r"const REFERENCE_IMAGE_TYPES = \{(.*?)\};", source, re.S)
    assert block, "REFERENCE_IMAGE_TYPES is the web app's copy of the server allowlist"
    return dict(re.findall(r'"(\.[a-z0-9]+)":\s*"([a-z]+/[a-z0-9.+-]+)"', block.group(1)))


def test_the_web_app_carries_the_servers_reference_allowlist() -> None:
    expected = {extension: mime for extension, (mime, _format) in _IMAGE_TYPES.items()}
    assert _frontend_types() == expected


def test_the_file_picker_filter_names_exactly_the_accepted_types() -> None:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    picker = re.search(r'<input id="passengerReference" type="file" accept="([^"]+)"', html)
    assert picker, "the reference picker declares an accept filter"
    accepted = set(picker.group(1).split(","))
    extensions = set(_IMAGE_TYPES)
    mimes = {mime for mime, _format in _IMAGE_TYPES.values()}
    assert accepted == extensions | mimes


def _gif_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 200, 10)).save(buffer, format="GIF")
    return buffer.getvalue()


def test_a_refused_upload_answers_with_the_reason_not_a_generic_sentence(container, project) -> None:  # type: ignore[no-untyped-def]
    with TestClient(create_app(container)) as client:
        gif = client.post(
            "/v1/assets",
            data={"project_id": project.id, "asset_type": "REFERENCE"},
            files={"file": ("sticker.gif", io.BytesIO(_gif_bytes()), "image/gif")},
        )
        assert gif.status_code == 415
        assert "PNG, JPEG, WebP" in gif.json()["detail"]

        forged = client.post(
            "/v1/assets",
            data={"project_id": project.id, "asset_type": "REFERENCE"},
            files={"file": ("sticker.png", io.BytesIO(_gif_bytes()), "image/png")},
        )
        assert forged.status_code == 415
        assert "do not match" in forged.json()["detail"]

        mismatched = client.post(
            "/v1/assets",
            data={"project_id": project.id, "asset_type": "REFERENCE"},
            files={"file": ("plate.png", io.BytesIO(_gif_bytes()), "image/webp")},
        )
        assert mismatched.status_code == 415
        assert "does not match its filename" in mismatched.json()["detail"]
