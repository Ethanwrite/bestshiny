from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from platform_shared import Settings
from production_domain.models import (
    DePayCheckoutSession,
    DePayWebhookDelivery,
    OnchainPayment,
    OnchainPaymentIntent,
    Workspace,
    WorkspaceCreditLedgerEntry,
)
from sqlalchemy import select
from video_platform_api.container import build_container
from video_platform_api.main import create_app

TREASURY = "0x2222222222222222222222222222222222222222"
PAYER = "0x1111111111111111111111111111111111111111"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TX_HASH = "0x" + "d" * 64
LINK_ID = "depay-link-test"
LINK_URL = f"https://link.depay.com/{LINK_ID}"


def _keys() -> tuple[rsa.RSAPrivateKey, str]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, public.decode()


def _container(tmp_path, public_key: str):  # type: ignore[no-untyped-def]
    return build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'depay.db'}",
            storage_root=tmp_path / "media",
            public_base_url="http://testserver",
            web_origins="http://testserver",
            auth_required=True,
            platform_api_key="test-platform-key",
            deployment_environment="test",
            alchemy_network="BASE_MAINNET",
            alchemy_treasury_address=TREASURY,
            depay_payment_link_url=LINK_URL,
            depay_link_id=LINK_ID,
            depay_callback_public_key=public_key,
            depay_offer_amount_usdc="30",
            depay_offer_credits=3_000,
        )
    )


def _registered(container) -> tuple[TestClient, str]:  # type: ignore[no-untyped-def]
    client = TestClient(create_app(container))
    response = client.post(
        "/api/auth/register",
        json={
            "email": "depay-owner@example.com",
            "password": "correct-horse-battery-staple",
            "display_name": "DePay owner",
            "workspace_name": "DePay workspace",
        },
    )
    assert response.status_code == 201
    return client, response.json()["user"]["workspaces"][0]["id"]


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("ai_director_csrf") or ""}


def _create_checkout(client: TestClient, workspace_id: str) -> tuple[dict, str, str]:
    response = client.post(
        f"/v1/workspaces/{workspace_id}/depay-checkouts",
        json={},
        headers=_csrf(client),
    )
    assert response.status_code == 201
    checkout = response.json()
    query = parse_qs(urlparse(checkout["checkout_url"]).query)
    assert "quantity" not in query
    assert query["payload[order_ref]"] == [checkout["payment_intent_id"]]
    return checkout, query["payload[checkout_token]"][0], query["payload[order_ref]"][0]


def _callback_payload(
    token: str,
    order_ref: str,
    *,
    amount: str = "30.000000",
    transaction: str = TX_HASH,
) -> dict[str, object]:
    return {
        "status": "success",
        "blockchain": "base",
        "transaction": transaction,
        "sender": PAYER,
        "receiver": TREASURY,
        "token": USDC,
        "decimals": 6,
        "commitment": "confirmed",
        "confirmations": 1,
        "after_block": "123456",
        "amount": amount,
        "payload": {
            "link_id": LINK_ID,
            "injected": {"checkout_token": token, "order_ref": order_ref},
        },
    }


def _signed(private: rsa.RSAPrivateKey, payload: dict[str, object]) -> tuple[bytes, str]:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = private.sign(
        raw,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=64),
        hashes.SHA256(),
    )
    return raw, base64.urlsafe_b64encode(signature).decode().rstrip("=")


def _post_callback(container, private, payload):  # type: ignore[no-untyped-def]
    raw, signature = _signed(private, payload)
    return TestClient(create_app(container)).post(
        "/v1/webhooks/depay",
        content=raw,
        headers={"content-type": "application/json", "x-signature": signature},
    )


def test_free_payment_atomically_activates_pro_and_grants_credits_once(tmp_path) -> None:
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)

    config = client.get("/v1/payments/config")
    assert config.status_code == 200
    assert config.json()["checkout_provider"] == "DEPAY"
    assert config.json()["legacy_wallet_payments_enabled"] is False
    assert config.json()["depay_checkout_configured"] is True
    assert config.json()["depay_offer"] == {
        "id": "pro_credits_fixed",
        "amount_usdc": "30.00",
        "credits": 3_000,
        "upgrade_plan": "PRO",
        "recurring": False,
    }
    legacy = client.post(
        f"/v1/workspaces/{workspace_id}/wallet-bindings/challenge",
        json={"address": PAYER, "chain_id": 8453},
        headers=_csrf(client),
    )
    assert legacy.status_code == 410

    checkout, token, order_ref = _create_checkout(client, workspace_id)
    assert checkout["expected_usdc"] == "30.00"
    assert checkout["expected_credits"] == 3_000
    assert checkout["purchase_kind"] == "UPGRADE_PRO_AND_CREDITS"

    response = _post_callback(container, private, _callback_payload(token, order_ref))
    assert response.status_code == 200
    assert response.json()["result"] == "CREDITED"
    assert response.json()["credits_granted"] == 3_000
    assert response.json()["plan_tier"] == "PRO"
    assert response.json()["pro_activated"] is True

    replay = _post_callback(container, private, _callback_payload(token, order_ref))
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        checkout_row = session.scalar(select(DePayCheckoutSession))
        intent = session.scalar(select(OnchainPaymentIntent))
        payment = session.scalar(select(OnchainPayment))
        ledger = list(session.scalars(select(WorkspaceCreditLedgerEntry)))
        deliveries = list(session.scalars(select(DePayWebhookDelivery)))
        assert workspace is not None
        assert (workspace.plan_tier, workspace.credit_balance) == ("PRO", 3_050)
        assert checkout_row is not None and checkout_row.status == "PAID"
        assert intent is not None and checkout_row.payment_intent_id == intent.id
        assert checkout_row.credits_granted == 3_000
        assert intent.status == "PAID"
        assert intent.wallet_binding_id is None and intent.raw_amount_microunits == 30_000_000
        assert payment is not None and payment.status == "CREDITED"
        assert payment.payment_intent_id == intent.id
        assert len(ledger) == 1
        assert ledger[0].metadata_json["plan_before"] == "FREE"
        assert ledger[0].metadata_json["plan_after"] == "PRO"
        assert ledger[0].metadata_json["pro_activated"] is True
        assert len(deliveries) == 1


def test_pro_payment_uses_same_offer_and_only_tops_up_credits(tmp_path) -> None:
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    first, first_token, first_ref = _create_checkout(client, workspace_id)
    assert first["purchase_kind"] == "UPGRADE_PRO_AND_CREDITS"
    assert _post_callback(container, private, _callback_payload(first_token, first_ref)).status_code == 200

    second, second_token, second_ref = _create_checkout(client, workspace_id)
    assert second["purchase_kind"] == "TOP_UP_CREDITS"
    response = _post_callback(
        container,
        private,
        _callback_payload(second_token, second_ref, transaction="0x" + "e" * 64),
    )
    assert response.status_code == 200
    assert response.json()["credits_granted"] == 3_000
    assert response.json()["plan_tier"] == "PRO"
    assert response.json()["pro_activated"] is False

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        ledger = list(
            session.scalars(
                select(WorkspaceCreditLedgerEntry).order_by(WorkspaceCreditLedgerEntry.created_at)
            )
        )
        assert workspace is not None
        assert (workspace.plan_tier, workspace.credit_balance) == ("PRO", 6_050)
        assert len(ledger) == 2
        assert ledger[1].credits == 3_000
        assert ledger[1].metadata_json["plan_before"] == "PRO"
        assert ledger[1].metadata_json["plan_after"] == "PRO"
        assert ledger[1].metadata_json["pro_activated"] is False


def test_depay_does_not_infer_business_entitlements_from_paid_amount(tmp_path) -> None:
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    _checkout, token, order_ref = _create_checkout(client, workspace_id)

    response = _post_callback(
        container,
        private,
        _callback_payload(token, order_ref, amount="29.000000"),
    )
    assert response.status_code == 200
    assert response.json()["result"] == "RECONCILIATION_REQUIRED"
    assert response.json()["credits_granted"] == 0
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        intent = session.scalar(select(OnchainPaymentIntent))
        assert workspace is not None
        assert (workspace.plan_tier, workspace.credit_balance) == ("FREE", 50)
        assert intent is not None and intent.status == "RECONCILIATION_REQUIRED"
        assert session.scalar(select(WorkspaceCreditLedgerEntry)) is None


def test_depay_callback_rejects_bad_signature_and_unknown_order(tmp_path) -> None:
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    _checkout, token, order_ref = _create_checkout(client, workspace_id)
    webhook_client = TestClient(create_app(container))

    bad_raw, _ = _signed(private, _callback_payload(token, order_ref))
    assert (
        webhook_client.post(
            "/v1/webhooks/depay",
            content=bad_raw,
            headers={"x-signature": "not-a-signature"},
        ).status_code
        == 401
    )
    unknown_raw, unknown_signature = _signed(private, _callback_payload("unknown-token", order_ref))
    assert (
        webhook_client.post(
            "/v1/webhooks/depay",
            content=unknown_raw,
            headers={"x-signature": unknown_signature},
        ).status_code
        == 400
    )
    mismatch_raw, mismatch_signature = _signed(
        private,
        _callback_payload(token, "pi-does-not-match"),
    )
    assert (
        webhook_client.post(
            "/v1/webhooks/depay",
            content=mismatch_raw,
            headers={"x-signature": mismatch_signature},
        ).status_code
        == 400
    )
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None and workspace.credit_balance == 50
        assert workspace.plan_tier == "FREE"
        assert session.scalar(select(WorkspaceCreditLedgerEntry)) is None


def test_depay_checkout_fails_closed_without_signed_callback_key(tmp_path) -> None:
    container = _container(tmp_path, "")
    client, workspace_id = _registered(container)
    response = client.post(
        f"/v1/workspaces/{workspace_id}/depay-checkouts",
        json={},
        headers=_csrf(client),
    )
    assert response.status_code == 503
    assert "支付入口已关闭" in response.json()["detail"]
    with container.database.session() as session:
        assert session.scalar(select(OnchainPaymentIntent)) is None
