from __future__ import annotations

from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi.testclient import TestClient
from platform_shared import Settings
from production_domain.models import OnchainPaymentIntent, WorkspaceWalletBinding
from sqlalchemy import select
from video_platform_api.container import build_container
from video_platform_api.main import create_app

TREASURY = "0x2222222222222222222222222222222222222222"


def _container(tmp_path):  # type: ignore[no-untyped-def]
    return build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'wallet.db'}",
            storage_root=tmp_path / "media",
            public_base_url="http://testserver",
            web_origins="http://testserver",
            auth_required=True,
            platform_api_key="test-platform-key",
            deployment_environment="test",
            alchemy_network="BASE_SEPOLIA",
            alchemy_treasury_address=TREASURY,
            alchemy_usdc_microunits_per_credit=10_000,
            reown_project_id="test-reown-project",
            legacy_wallet_payments_enabled=True,
        )
    )


def _registered_client(container) -> tuple[TestClient, str]:  # type: ignore[no-untyped-def]
    client = TestClient(create_app(container))
    response = client.post(
        "/api/auth/register",
        json={
            "email": "wallet-owner@example.com",
            "password": "correct-horse-battery-staple",
            "display_name": "Wallet owner",
            "workspace_name": "Wallet workspace",
        },
    )
    assert response.status_code == 201
    return client, response.json()["user"]["workspaces"][0]["id"]


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("ai_director_csrf") or ""}


def test_authenticated_owner_can_verify_wallet_and_create_payment_intent(tmp_path) -> None:
    container = _container(tmp_path)
    client, workspace_id = _registered_client(container)
    account = Account.create()

    config = client.get("/v1/payments/config")
    assert config.status_code == 200
    assert config.json()["chain_id"] == 84532
    assert config.json()["reown_configured"] is True

    challenge = client.post(
        f"/v1/workspaces/{workspace_id}/wallet-bindings/challenge",
        json={"address": account.address, "chain_id": 84532},
        headers={**_csrf(client), "Origin": "http://testserver"},
    )
    assert challenge.status_code == 200
    message = challenge.json()["message"]
    assert "This signature does not send a transaction" in message
    signature = "0x" + Account.sign_message(
        encode_defunct(text=message), account.key
    ).signature.hex()

    verified = client.post(
        f"/v1/workspaces/{workspace_id}/wallet-bindings/verify",
        json={"challenge_id": challenge.json()["challenge_id"], "signature": signature},
        headers=_csrf(client),
    )
    assert verified.status_code == 200
    assert verified.json()["address"] == account.address.lower()
    assert verified.json()["status"] == "VERIFIED"

    replay = client.post(
        f"/v1/workspaces/{workspace_id}/wallet-bindings/verify",
        json={"challenge_id": challenge.json()["challenge_id"], "signature": signature},
        headers=_csrf(client),
    )
    assert replay.status_code == 409

    intent = client.post(
        f"/v1/workspaces/{workspace_id}/payment-intents",
        json={"wallet_binding_id": verified.json()["id"], "amount_usdc": "0.01"},
        headers=_csrf(client),
    )
    assert intent.status_code == 200
    assert intent.json()["raw_amount_microunits"] == 10_000
    assert intent.json()["credits"] == 1
    assert intent.json()["to_address"] == TREASURY

    cancelled = client.post(
        f"/v1/workspaces/{workspace_id}/payment-intents/{intent.json()['id']}/cancel",
        headers=_csrf(client),
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"

    intent = client.post(
        f"/v1/workspaces/{workspace_id}/payment-intents",
        json={"wallet_binding_id": verified.json()["id"], "amount_usdc": "0.01"},
        headers=_csrf(client),
    )
    assert intent.status_code == 200

    transaction_hash = "0x" + "a" * 64
    submitted = client.post(
        f"/v1/workspaces/{workspace_id}/payment-intents/{intent.json()['id']}/submitted",
        json={"transaction_hash": transaction_hash},
        headers=_csrf(client),
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "SUBMITTED"
    assert submitted.json()["transaction_hash"] == transaction_hash

    cannot_cancel_submitted = client.post(
        f"/v1/workspaces/{workspace_id}/payment-intents/{intent.json()['id']}/cancel",
        headers=_csrf(client),
    )
    assert cannot_cancel_submitted.status_code == 409

    with container.database.session() as session:
        binding = session.scalar(select(WorkspaceWalletBinding))
        stored_intent = session.scalar(select(OnchainPaymentIntent))
        assert binding is not None and binding.verified_by_user_id is not None
        assert stored_intent is not None and stored_intent.wallet_binding_id == binding.id


def test_wallet_challenge_rejects_wrong_chain_and_wrong_signer(tmp_path) -> None:
    container = _container(tmp_path)
    client, workspace_id = _registered_client(container)
    account = Account.create()

    wrong_chain = client.post(
        f"/v1/workspaces/{workspace_id}/wallet-bindings/challenge",
        json={"address": account.address, "chain_id": 8453},
        headers={**_csrf(client), "Origin": "http://testserver"},
    )
    assert wrong_chain.status_code == 422

    challenge = client.post(
        f"/v1/workspaces/{workspace_id}/wallet-bindings/challenge",
        json={"address": account.address, "chain_id": 84532},
        headers={**_csrf(client), "Origin": "http://testserver"},
    )
    assert challenge.status_code == 200
    other = Account.create()
    signature = "0x" + Account.sign_message(
        encode_defunct(text=challenge.json()["message"]),
        other.key,
    ).signature.hex()
    rejected = client.post(
        f"/v1/workspaces/{workspace_id}/wallet-bindings/verify",
        json={"challenge_id": challenge.json()["challenge_id"], "signature": signature},
        headers=_csrf(client),
    )
    assert rejected.status_code == 422
    with container.database.session() as session:
        assert session.scalar(select(WorkspaceWalletBinding)) is None
