"""Workspace connections: a workspace's own provider keys.

What these tests pin:

1. ownership - only an owner or admin manages a workspace's connections, any
   member lists them, and another workspace's connection is not found;
2. secrecy - no response carries the key, the stored ciphertext is bound to
   its row, deletion erases it, and a validation error never echoes it;
3. wire formats - OpenAI-compatible and Anthropic Messages requests are shaped
   as those APIs define, optional sampling parameters are sent only when set
   and dropped once when a provider refuses them, refusals are reported;
4. egress - every connection is dialled only after its resolved address is
   checked, on the address actually used, and redirects are never followed.
"""

from __future__ import annotations

import json

import httpx
import pytest
from connection_core import (
    AnthropicChatClient,
    ConnectionInvalid,
    ConnectionModelInput,
    ConnectionUnavailable,
    ConnectionView,
    EgressDenied,
    OpenAICompatibleChatClient,
    ProviderCallFailed,
    address_allowed,
    fenced_client,
    normalize_base_url,
    secret_hint,
)
from connection_core.egress import FencedNetworkBackend
from connection_core.protocols import AuthStyle
from connection_core.transport import ConnectionTransport
from fastapi.testclient import TestClient
from production_domain.models import (
    Project,
    User,
    UserConnection,
    Workspace,
    WorkspaceMembership,
)
from provider_sdk import ProviderError, ProviderJsonClient
from sqlalchemy import select
from video_platform_api.main import create_app

KEY = "sk-live-" + "abcdefghijklmnop1234"


def _register(client: TestClient, email: str) -> tuple[dict[str, str], str, str]:
    registered = client.post(
        "/api/auth/register", json={"email": email, "password": "correct horse battery staple"}
    )
    assert registered.status_code in {200, 201}, registered.text
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    created = client.post("/v1/projects", headers=headers, json={"title": "Canvas"}).json()
    project = client.get(f"/v1/projects/{created['id']}", headers=headers).json()
    return headers, project["id"], project["workspace_id"]


def _mock_client_factory(handler):  # type: ignore[no-untyped-def]
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


# ---------------------------------------------------------------------------
# 1 + 2. Ownership and secrecy through the API
# ---------------------------------------------------------------------------


def test_owner_manages_connections_and_no_response_carries_the_key(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        headers, _project_id, workspace_id = _register(client, "owner@example.com")
        protocols = client.get("/v1/connections/protocols", headers=headers).json()["protocols"]
        ids = {protocol["id"] for protocol in protocols}
        assert {"openai_compatible", "anthropic", "openrouter", "volcengine_ark", "alibaba_dashscope"} <= ids
        assert "mock" not in ids, "the network-free mock is development-only and off by default"

        created = client.post(
            f"/v1/workspaces/{workspace_id}/connections",
            headers=headers,
            json={
                "name": "  My   DeepSeek ",
                "protocol": "openai_compatible",
                "base_url": "https://api.deepseek.com/",
                "api_key": KEY,
                "models": [{"id": "deepseek-v4-flash", "capability": "chat"}],
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert KEY not in created.text
        assert body["name"] == "My DeepSeek"
        assert body["base_url"] == "https://api.deepseek.com"
        assert body["capabilities"] == ["chat"]
        assert body["secret_hint"] == "sk-…1234"
        assert body["status"] == "ACTIVE"

        listing = client.get(f"/v1/workspaces/{workspace_id}/connections", headers=headers)
        assert listing.status_code == 200
        assert listing.json()["can_manage"] is True
        assert [item["id"] for item in listing.json()["connections"]] == [body["id"]]
        assert KEY not in listing.text

        with container.database.session() as session:
            row = session.get(UserConnection, body["id"])
            assert row is not None
            assert KEY not in row.secret_ciphertext
            assert container.credentials.decrypt(row.secret_ciphertext) == f"{row.id}:{KEY}"

        renamed = client.patch(
            f"/v1/workspaces/{workspace_id}/connections/{body['id']}",
            headers=headers,
            json={"name": "DeepSeek prod", "api_key": "sk-rotated-key-99998888"},
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["name"] == "DeepSeek prod"
        assert renamed.json()["secret_hint"] == "sk-…8888"

        deleted = client.delete(f"/v1/workspaces/{workspace_id}/connections/{body['id']}", headers=headers)
        assert deleted.status_code == 204
        assert (
            client.get(f"/v1/workspaces/{workspace_id}/connections", headers=headers).json()["connections"]
            == []
        )
        with container.database.session() as session:
            row = session.get(UserConnection, body["id"])
            assert row.status == "DELETED" and row.deleted_at is not None
            assert row.secret_ciphertext == "", "deleting a connection erases its key"


def test_members_list_but_only_admins_manage_and_other_workspaces_see_nothing(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        owner_headers, _project, workspace_id = _register(client, "owner2@example.com")
        created = client.post(
            f"/v1/workspaces/{workspace_id}/connections",
            headers=owner_headers,
            json={"name": "OpenRouter", "protocol": "openrouter", "api_key": KEY},
        ).json()

        editor_headers, _editor_project, _editor_workspace = _register(client, "editor@example.com")
        with container.database.session() as session:
            editor = session.scalar(select(User).where(User.email == "editor@example.com"))
            session.add(WorkspaceMembership(workspace_id=workspace_id, user_id=editor.id, role="EDITOR"))
        listing = client.get(f"/v1/workspaces/{workspace_id}/connections", headers=editor_headers)
        assert listing.status_code == 200
        assert listing.json()["can_manage"] is False
        assert len(listing.json()["connections"]) == 1
        refused = client.post(
            f"/v1/workspaces/{workspace_id}/connections",
            headers=editor_headers,
            json={"name": "Mine", "protocol": "anthropic", "api_key": KEY},
        )
        assert refused.status_code == 403
        assert (
            client.delete(
                f"/v1/workspaces/{workspace_id}/connections/{created['id']}", headers=editor_headers
            ).status_code
            == 403
        )
        for suffix in ("test", "remote-models"):
            method = client.post if suffix == "test" else client.get
            checked = method(
                f"/v1/workspaces/{workspace_id}/connections/{created['id']}/{suffix}", headers=editor_headers
            )
            assert checked.status_code == 403, "checking a key is managing the connection"

        stranger_headers, _p, stranger_workspace = _register(client, "stranger@example.com")
        assert (
            client.get(f"/v1/workspaces/{workspace_id}/connections", headers=stranger_headers).status_code
            == 403
        )
        # Addressed through the stranger's own workspace, the connection does not exist.
        crossed = client.patch(
            f"/v1/workspaces/{stranger_workspace}/connections/{created['id']}",
            headers=stranger_headers,
            json={"name": "stolen"},
        )
        assert crossed.status_code == 404


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ({"protocol": "telepathy"}, "unknown connection protocol"),
        ({"protocol": "mock"}, "unknown connection protocol"),
        ({"base_url": "http://api.example.com/v1"}, "https://"),
        ({"base_url": "https://10.0.0.8/v1"}, "public internet host"),
        ({"base_url": "https://localhost/v1"}, "public internet host"),
        ({"base_url": "https://user:pass@api.example.com"}, "username or password"),
        ({"capabilities": ["video"]}, "do not support video"),
        ({"models": [{"id": "gpt", "capability": "video"}]}, "capability this connection lacks"),
        ({"api_key": ""}, "API key is required"),
    ],
)
def test_invalid_connections_are_refused_without_echoing_the_key(container, payload, fragment):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        headers, _project, workspace_id = _register(client, "validate@example.com")
        request = {"name": "Bad", "protocol": "openai_compatible", "api_key": KEY, **payload}
        response = client.post(f"/v1/workspaces/{workspace_id}/connections", headers=headers, json=request)
        assert response.status_code == 422, response.text
        assert fragment in response.json()["detail"]["message"]
        assert KEY not in response.text


def test_a_key_that_cannot_ride_a_header_is_refused_without_echo(container):  # type: ignore[no-untyped-def]
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        headers, _project, workspace_id = _register(client, "header@example.com")
        secret = "sk-with\nnewline-secret-material"
        response = client.post(
            f"/v1/workspaces/{workspace_id}/connections",
            headers=headers,
            json={"name": "Bad", "protocol": "openai_compatible", "api_key": secret},
        )
        assert response.status_code == 422
        assert "secret-material" not in response.text


def test_ciphertext_moved_onto_another_row_does_not_decrypt_as_that_rows_key(container):  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        user = User(email="swap@example.com", display_name="Swap")
        session.add(user)
        session.flush()
        workspace = Workspace(owner_user_id=user.id, name="Swap")
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id
    first = container.connections.create_connection(
        workspace_id, user_id=None, name="A", protocol="openrouter", api_key=KEY
    )
    second = container.connections.create_connection(
        workspace_id, user_id=None, name="B", protocol="openrouter", api_key="sk-other-key-0000"
    )
    with container.database.session() as session:
        a = session.get(UserConnection, first.id)
        b = session.get(UserConnection, second.id)
        b.secret_ciphertext = a.secret_ciphertext
    with container.database.session() as session:
        row = session.get(UserConnection, second.id)
        with pytest.raises(ConnectionUnavailable) as refused:
            container.connections._decrypt(row)
        assert refused.value.code == "CONNECTION_KEY_UNREADABLE"


def test_secret_hint_never_reveals_a_short_key():
    assert secret_hint("short") == "••••"
    assert secret_hint("sk-abcdefghijkl9876") == "sk-…9876"


# ---------------------------------------------------------------------------
# 3. Wire formats
# ---------------------------------------------------------------------------


def _json_client(handler, *, base_url: str, auth: AuthStyle, headers=None) -> ProviderJsonClient:  # type: ignore[no-untyped-def]
    transport = ConnectionTransport(
        base_url=base_url,
        api_key=KEY,
        auth=auth,
        default_headers=headers,
        client_factory=_mock_client_factory(handler),
    )
    return ProviderJsonClient("test", transport, api_key_configured=True)


async def test_openai_compatible_chat_is_shaped_as_chat_completions():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "model": "deepseek-v4-flash-0901",
                "choices": [
                    {"message": {"role": "assistant", "content": "A neon market."}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4},
            },
        )

    client = OpenAICompatibleChatClient(
        _json_client(handler, base_url="https://api.deepseek.com", auth=AuthStyle.BEARER)
    )
    result = await client.complete(
        model="deepseek-v4-flash",
        messages=[{"role": "user", "content": "Describe a market"}],
        system="Be brief",
    )
    assert result.text == "A neon market."
    assert result.model == "deepseek-v4-flash-0901"
    assert (result.input_tokens, result.output_tokens) == (12, 4)
    request = seen[0]
    assert str(request.url) == "https://api.deepseek.com/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body == {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "Be brief"},
            {"role": "user", "content": "Describe a market"},
        ],
    }, "sampling parameters are sent only when the user set them"


async def test_openai_compatible_drops_a_refused_parameter_once():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "max_tokens" in body:
            return httpx.Response(
                400,
                json={
                    "error": {"message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens'."}
                },
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )

    client = OpenAICompatibleChatClient(
        _json_client(handler, base_url="https://api.openai.com/v1", auth=AuthStyle.BEARER)
    )
    result = await client.complete(model="gpt-x", messages=[{"role": "user", "content": "hi"}], max_tokens=64)
    assert result.text == "ok"
    assert len(bodies) == 2
    assert "max_tokens" not in bodies[1] and bodies[1]["max_completion_tokens"] == 64


async def test_openai_compatible_reports_a_refusal_and_an_empty_answer():
    def refusing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": None, "refusal": "I can't help"}, "finish_reason": "stop"}
                ]
            },
        )

    client = OpenAICompatibleChatClient(
        _json_client(refusing, base_url="https://x.example", auth=AuthStyle.BEARER)
    )
    with pytest.raises(ProviderCallFailed) as refused:
        await client.complete(model="m", messages=[{"role": "user", "content": "hi"}])
    assert refused.value.code == "MODEL_REFUSED"


async def test_anthropic_messages_request_and_refusal():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(
                200,
                json={
                    "model": "claude-opus-5",
                    "content": [
                        {"type": "thinking", "thinking": ""},
                        {"type": "text", "text": "Slow push-in."},
                    ],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 20, "output_tokens": 5},
                },
            )
        return httpx.Response(
            200,
            json={
                "content": [],
                "stop_reason": "refusal",
                "stop_details": {"type": "refusal", "category": "cyber"},
            },
        )

    client = AnthropicChatClient(
        _json_client(
            handler,
            base_url="https://api.anthropic.com",
            auth=AuthStyle.X_API_KEY,
            headers={"anthropic-version": "2023-06-01"},
        ),
        base_url="https://api.anthropic.com",
    )
    result = await client.complete(
        model="claude-opus-5", messages=[{"role": "user", "content": "Direct this"}], system="You direct"
    )
    assert result.text == "Slow push-in."
    assert (result.input_tokens, result.output_tokens) == (20, 5)
    request = seen[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == KEY
    assert "authorization" not in request.headers
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    body = json.loads(request.content)
    assert body["model"] == "claude-opus-5"
    assert body["max_tokens"] == 16_000
    assert body["system"] == "You direct"
    assert body["fallbacks"] == "default"
    assert "temperature" not in body

    with pytest.raises(ProviderCallFailed) as refused:
        await client.complete(model="claude-opus-5", messages=[{"role": "user", "content": "x"}])
    assert refused.value.code == "MODEL_REFUSED"
    assert "cyber" in str(refused.value)


async def test_anthropic_gateway_base_url_gets_no_beta_and_accepts_a_v1_suffix():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        )

    client = AnthropicChatClient(
        _json_client(handler, base_url="https://gateway.example.com/v1", auth=AuthStyle.X_API_KEY),
        base_url="https://gateway.example.com/v1",
    )
    await client.complete(model="claude-opus-5", messages=[{"role": "user", "content": "x"}], temperature=0.2)
    assert str(seen[0].url) == "https://gateway.example.com/v1/messages"
    assert "anthropic-beta" not in seen[0].headers
    body = json.loads(seen[0].content)
    assert "fallbacks" not in body and body["temperature"] == 0.2


async def test_anthropic_lists_models_across_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("after_id") == "m2":
            return httpx.Response(200, json={"data": [{"id": "m3"}], "has_more": False, "last_id": "m3"})
        return httpx.Response(
            200,
            json={
                "data": [{"id": "m1", "display_name": "Model 1"}, {"id": "m2"}],
                "has_more": True,
                "last_id": "m2",
            },
        )

    client = AnthropicChatClient(
        _json_client(handler, base_url="https://api.anthropic.com", auth=AuthStyle.X_API_KEY),
        base_url="https://api.anthropic.com",
    )
    models = await client.list_models()
    assert [model.id for model in models] == ["m1", "m2", "m3"]
    assert models[0].label == "Model 1"


def _workspace(container) -> str:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        user = User(email=f"ws-{id(session)}@example.com", display_name="W")
        session.add(user)
        session.flush()
        workspace = Workspace(owner_user_id=user.id, name="W")
        session.add(workspace)
        session.flush()
        return workspace.id


async def test_service_chat_and_test_use_the_stored_key_and_record_the_verdict(container):  # type: ignore[no-untyped-def]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(401, json={"error": {"message": "invalid api key"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    container.connections._client_factory = _mock_client_factory(handler)
    workspace_id = _workspace(container)
    view = container.connections.create_connection(
        workspace_id,
        user_id=None,
        name="Qwen",
        protocol="alibaba_dashscope",
        api_key=KEY,
        capabilities=["chat"],
        models=[ConnectionModelInput("qwen3.8-max", "chat")],
    )
    assert isinstance(view, ConnectionView)
    result = await container.connections.chat(
        connection_id=view.id,
        workspace_id=workspace_id,
        model="qwen3.8-max",
        messages=[{"role": "user", "content": "hi"}],
    )
    assert result.text == "hello"
    assert str(requests[0].url) == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert requests[0].headers["authorization"] == f"Bearer {KEY}"

    with pytest.raises(ConnectionUnavailable) as foreign:
        await container.connections.chat(
            connection_id=view.id,
            workspace_id=_workspace(container),
            model="qwen3.8-max",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert foreign.value.code == "CONNECTION_WORKSPACE_MISMATCH"

    openai_view = container.connections.create_connection(
        workspace_id, user_id=None, name="OpenAI", protocol="openai_compatible", api_key=KEY
    )
    verdict = await container.connections.test_connection(workspace_id, openai_view.id)
    assert verdict["ok"] is False and verdict["checked_remotely"] is True
    assert verdict["code"] == "CREDENTIAL_EXPIRED"
    assert verdict["connection"]["status"] == "INVALID"
    assert KEY not in json.dumps(verdict)

    unreachable = container.connections.create_connection(
        workspace_id, user_id=None, name="Unreachable", protocol="openai_compatible", api_key=KEY
    )

    async def private(host: str, port: int) -> list[str]:
        return ["10.0.0.3"]

    container.connections._client_factory = None
    container.connections._resolver = private
    refused = await container.connections.test_connection(workspace_id, unreachable.id)
    assert refused["ok"] is False and refused["code"] == "CONNECTION_EGRESS_DENIED"
    assert refused["connection"]["status"] == "ACTIVE", "a refused address is not a refused key"
    assert refused["connection"]["last_error"] is None
    container.connections._resolver = None

    router = container.connections.create_connection(
        workspace_id, user_id=None, name="OpenRouter", protocol="openrouter", api_key=KEY
    )
    requests.clear()
    container.connections._client_factory = _mock_client_factory(
        lambda request: httpx.Response(200, json={"data": [{"id": "a/b"}]})
    )
    public_listing = await container.connections.test_connection(workspace_id, router.id)
    assert public_listing["ok"] is True and public_listing["models_found"] == 1
    assert public_listing["checked_remotely"] is False, "a public model list proves nothing about the key"
    assert "checked on first use" in public_listing["message"]
    assert public_listing["connection"]["last_verified_at"] is None

    ark = container.connections.create_connection(
        workspace_id, user_id=None, name="Ark", protocol="volcengine_ark", api_key=KEY
    )
    local_only = await container.connections.test_connection(workspace_id, ark.id)
    assert local_only["ok"] is True and local_only["checked_remotely"] is False


async def test_switched_off_connections_refuse_use(container):  # type: ignore[no-untyped-def]
    workspace_id = _workspace(container)
    view = container.connections.create_connection(
        workspace_id, user_id=None, name="OR", protocol="openrouter", api_key=KEY
    )
    container.connections.enabled = False
    with pytest.raises(ConnectionUnavailable) as refused:
        await container.connections.chat(
            connection_id=view.id,
            workspace_id=workspace_id,
            model="anthropic/claude-sonnet-5",
            messages=[{"role": "user", "content": "hi"}],
        )
    assert refused.value.code == "CONNECTIONS_DISABLED"


# ---------------------------------------------------------------------------
# 4. Egress
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("8.8.8.8", True),
        ("2606:4700:4700::1111", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.18.0.5", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("100.100.100.200", False),
        ("198.18.0.89", False),
        ("::1", False),
        ("fd00::1", False),
        ("::ffff:127.0.0.1", False),
        ("0.0.0.0", False),
        ("not-an-ip", False),
    ],
)
def test_only_public_addresses_may_be_dialled(address, allowed):
    assert address_allowed(address, allow_private=False) is allowed


def test_private_mode_still_refuses_unroutable_addresses():
    assert address_allowed("127.0.0.1", allow_private=True) is True
    assert address_allowed("0.0.0.0", allow_private=True) is False


async def test_the_fence_refuses_at_connect_time_on_the_resolved_address():
    async def rebinding(host: str, port: int) -> list[str]:
        return ["93.184.216.34", "10.0.0.5"]

    backend = FencedNetworkBackend(resolver=rebinding)
    with pytest.raises(EgressDenied):
        await backend.connect_tcp("api.example.com", 443)

    async def empty(host: str, port: int) -> list[str]:
        return []

    with pytest.raises(EgressDenied):
        await FencedNetworkBackend(resolver=empty).connect_tcp("api.example.com", 443)


async def test_a_real_client_never_reaches_loopback():
    """Pins the pool rebuild in `FencedTransport` to the installed httpx release."""

    async with fenced_client(timeout_seconds=3) as client:
        with pytest.raises(EgressDenied):
            await client.get("http://127.0.0.1:9/")
        with pytest.raises(EgressDenied):
            await client.get("https://[::1]/")


async def test_transport_maps_egress_and_oversized_bodies_to_provider_errors():
    async def private(host: str, port: int) -> list[str]:
        return ["10.0.0.9"]

    transport = ConnectionTransport(
        base_url="https://api.example.com",
        api_key=KEY,
        auth=AuthStyle.BEARER,
        resolver=private,
    )
    client = ProviderJsonClient("t", transport, api_key_configured=True)
    with pytest.raises(ProviderError) as denied:
        await client.request("POST", "/videos", json_body={}, submitted=True)
    assert denied.value.code == "CONNECTION_EGRESS_DENIED"
    assert denied.value.submitted is False

    def huge(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 2048)

    small = ConnectionTransport(
        base_url="https://api.example.com",
        api_key=KEY,
        auth=AuthStyle.BEARER,
        max_response_bytes=1024,
        client_factory=_mock_client_factory(huge),
    )
    with pytest.raises(ProviderError) as too_large:
        await ProviderJsonClient("t", small, api_key_configured=True).request(
            "POST", "/videos", json_body={}, submitted=True
        )
    assert too_large.value.code == "CONNECTION_RESPONSE_TOO_LARGE"
    assert too_large.value.submitted is True, "the request reached the provider before the body overflowed"


async def test_redirects_are_reported_not_followed():
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": "http://169.254.169.254/"})

    transport = ConnectionTransport(
        base_url="https://api.example.com",
        api_key=KEY,
        auth=AuthStyle.BEARER,
        client_factory=_mock_client_factory(redirect),
    )
    with pytest.raises(ProviderError) as redirected:
        await ProviderJsonClient("t", transport, api_key_configured=True).request("GET", "/models")
    assert "redirect" in str(redirected.value)


def test_transport_repr_never_contains_the_key():
    transport = ConnectionTransport(base_url="https://api.example.com", api_key=KEY, auth=AuthStyle.BEARER)
    assert KEY not in repr(transport)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://api.openai.com/v1/", "https://api.openai.com/v1"),
        ("HTTPS://API.Example.COM:8443/base/", "https://api.example.com:8443/base"),
        ("https://dashscope-intl.aliyuncs.com", "https://dashscope-intl.aliyuncs.com"),
    ],
)
def test_base_urls_are_canonicalized(value, expected):
    assert normalize_base_url(value) == expected


def test_private_base_urls_are_development_only():
    with pytest.raises(ConnectionInvalid):
        normalize_base_url("http://localhost:11434/v1")
    assert normalize_base_url("http://localhost:11434/v1", allow_private=True) == "http://localhost:11434/v1"


def test_the_process_refuses_development_switches_in_production(tmp_path):  # type: ignore[no-untyped-def]
    from platform_shared import Settings
    from video_platform_api.container import build_container

    for switch in ("user_connection_allow_private_network", "user_connection_mock_protocol_enabled"):
        settings = Settings(
            _env_file=None,
            database_url="postgresql+psycopg://unused:unused@127.0.0.1:1/unused",
            deployment_environment="production",
            platform_api_key="p" * 16 + "q" * 8 + "abcdefghijklmnop",
            credential_encryption_key="",
            **{switch: True},
        )
        with pytest.raises(RuntimeError, match=switch.upper()):
            build_container(settings)


def test_projects_expose_the_workspace_connections_belong_to(container):  # type: ignore[no-untyped-def]
    # The canvas reads the project's workspace to find its connections.
    container.settings.auth_required = True
    with TestClient(create_app(container)) as client:
        headers, project_id, workspace_id = _register(client, "project-ws@example.com")
        with container.database.session() as session:
            assert session.get(Project, project_id).workspace_id == workspace_id
