from email.message import Message

from scripts.verify_object_storage import _cors_preflight


class _Response:
    def __init__(self, headers: dict[str, str]):
        self.headers = Message()
        for name, value in headers.items():
            self.headers[name] = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_cors_preflight_accepts_exact_origin_method_and_signed_headers(monkeypatch):
    monkeypatch.setattr(
        "scripts.verify_object_storage.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(
            {
                "Access-Control-Allow-Origin": "https://studio.example",
                "Access-Control-Allow-Methods": "GET, PUT, HEAD",
                "Access-Control-Allow-Headers": "content-type, x-amz-checksum-sha256",
            }
        ),
    )

    allowed, detail = _cors_preflight(
        "https://bucket.example/object?signature=secret",
        origin="https://studio.example",
        upload_headers={"Content-Type": "image/png", "x-amz-checksum-sha256": "digest"},
    )

    assert allowed is True
    assert detail == "allowed"


def test_cors_preflight_rejects_wrong_origin(monkeypatch):
    monkeypatch.setattr(
        "scripts.verify_object_storage.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(
            {
                "Access-Control-Allow-Origin": "https://admin.example",
                "Access-Control-Allow-Methods": "PUT",
                "Access-Control-Allow-Headers": "*",
            }
        ),
    )

    allowed, detail = _cors_preflight(
        "https://bucket.example/object?signature=secret",
        origin="https://studio.example",
        upload_headers={"Content-Type": "image/png"},
    )

    assert allowed is False
    assert detail == "missing origin"


def test_cors_preflight_rejects_missing_signed_header(monkeypatch):
    monkeypatch.setattr(
        "scripts.verify_object_storage.urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(
            {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "PUT",
                "Access-Control-Allow-Headers": "content-type",
            }
        ),
    )

    allowed, detail = _cors_preflight(
        "https://bucket.example/object?signature=secret",
        origin="https://studio.example",
        upload_headers={"Content-Type": "image/png", "x-amz-checksum-sha256": "digest"},
    )

    assert allowed is False
    assert detail == "missing headers content-type,x-amz-checksum-sha256"
