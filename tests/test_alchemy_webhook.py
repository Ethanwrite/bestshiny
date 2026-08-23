from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from platform_shared import Settings
from production_domain.models import (
    AlchemyWebhookDelivery,
    OnchainPayment,
    OnchainPaymentIntent,
    User,
    Workspace,
    WorkspaceCreditLedgerEntry,
    WorkspaceWalletBinding,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from video_platform_api.container import build_container
from video_platform_api.main import create_app

SIGNING_KEY = "alchemy-webhook-test-signing-key"
WEBHOOK_ID = "wh_base_usdc_test"
TREASURY = "0x2222222222222222222222222222222222222222"
PAYER = "0x1111111111111111111111111111111111111111"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TX_HASH = "0x" + "a" * 64


def _container(tmp_path, *, crediting_enabled: bool = True, signing_key: str = SIGNING_KEY):
    return build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'alchemy.db'}",
            storage_root=tmp_path / "media",
            public_base_url="http://testserver",
            auth_required=False,
            platform_api_key="test-platform-key",
            deployment_environment="test",
            alchemy_webhook_signing_key=signing_key,
            alchemy_webhook_id=WEBHOOK_ID,
            alchemy_network="BASE_MAINNET",
            alchemy_treasury_address=TREASURY,
            alchemy_crediting_enabled=crediting_enabled,
            alchemy_usdc_microunits_per_credit=10_000,
        )
    )


def _seed_verified_wallet(container) -> str:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        user = User(
            email="alchemy-payer@example.com",
            display_name="Alchemy payer",
            password_hash="unused",
        )
        session.add(user)
        session.flush([user])
        workspace = Workspace(
            owner_user_id=user.id,
            name="Alchemy workspace",
            plan_tier="FREE",
            credit_balance=50,
        )
        session.add(workspace)
        session.flush([workspace])
        binding = WorkspaceWalletBinding(
                workspace_id=workspace.id,
                chain_id=8453,
                address=PAYER,
                status="VERIFIED",
                verified_by_user_id=user.id,
                verified_at=datetime.now(UTC),
                metadata_json={"proof": "test-fixture"},
            )
        session.add(binding)
        session.flush([binding])
        session.add(
            OnchainPaymentIntent(
                workspace_id=workspace.id,
                wallet_binding_id=binding.id,
                network="BASE_MAINNET",
                chain_id=8453,
                from_address=PAYER,
                to_address=TREASURY,
                token_address=USDC,
                raw_amount_microunits=1_000_000,
                credits=100,
                status="PENDING",
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
                metadata_json={},
            )
        )
        return workspace.id


def _payload(
    *,
    event_id: str = "whevt_base_usdc_1",
    removed: bool = False,
    token_address: str = USDC,
    to_address: str = TREASURY,
    network: str = "BASE_MAINNET",
    amount_microunits: int = 1_000_000,
) -> dict[str, object]:
    return {
        "webhookId": WEBHOOK_ID,
        "id": event_id,
        "createdAt": "2026-08-22T00:00:00Z",
        "type": "ADDRESS_ACTIVITY",
        "event": {
            "network": network,
            "activity": [
                {
                    "blockNum": "0x123",
                    "hash": TX_HASH,
                    "fromAddress": PAYER,
                    "toAddress": to_address,
                    "value": amount_microunits / 1_000_000,
                    "asset": "USDC",
                    "category": "token",
                    "rawContract": {
                        "rawValue": hex(amount_microunits),
                        "address": token_address,
                        "decimals": 6,
                    },
                    "log": {
                        "address": token_address,
                        "blockNumber": "0x123",
                        "transactionHash": TX_HASH,
                        "logIndex": "0x1",
                        "removed": removed,
                    },
                }
            ],
        },
    }


def _raw_and_signature(payload: dict[str, object]) -> tuple[bytes, str]:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(SIGNING_KEY.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return raw, signature


def _post(client: TestClient, payload: dict[str, object]):
    raw, signature = _raw_and_signature(payload)
    return client.post(
        "/v1/webhooks/alchemy",
        content=raw,
        headers={
            "content-type": "application/json",
            "x-alchemy-signature": signature,
        },
    )


def test_alchemy_webhook_requires_configuration_and_valid_raw_body_signature(
    tmp_path,
) -> None:
    unconfigured = _container(tmp_path / "unconfigured", signing_key="")
    raw, signature = _raw_and_signature(_payload())
    response = TestClient(create_app(unconfigured)).post(
        "/v1/webhooks/alchemy",
        content=raw,
        headers={"x-alchemy-signature": signature},
    )
    assert response.status_code == 503

    configured = _container(tmp_path / "configured")
    client = TestClient(create_app(configured))
    assert client.post("/v1/webhooks/alchemy", content=raw).status_code == 401
    assert (
        client.post(
            "/v1/webhooks/alchemy",
            content=raw + b" ",
            headers={"x-alchemy-signature": signature},
        ).status_code
        == 401
    )


def test_valid_base_native_usdc_delivery_credits_verified_wallet_once(tmp_path) -> None:
    container = _container(tmp_path)
    workspace_id = _seed_verified_wallet(container)
    client = TestClient(create_app(container))

    response = _post(client, _payload())
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "event_id": "whevt_base_usdc_1",
        "replayed": False,
        "result": "PROCESSED",
        "activity_count": 1,
        "accepted_count": 1,
        "credited_count": 1,
        "ignored_count": 0,
    }

    replay = _post(client, _payload())
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    duplicate_activity = _post(client, _payload(event_id="whevt_base_usdc_duplicate_activity"))
    assert duplicate_activity.status_code == 200
    assert duplicate_activity.json()["replayed"] is False
    assert duplicate_activity.json()["credited_count"] == 0
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        payment = session.scalar(select(OnchainPayment))
        intent = session.scalar(select(OnchainPaymentIntent))
        ledger = list(session.scalars(select(WorkspaceCreditLedgerEntry)))
        deliveries = list(session.scalars(select(AlchemyWebhookDelivery)))
        assert workspace is not None and workspace.credit_balance == 150
        assert payment is not None and payment.status == "CREDITED"
        assert payment.raw_amount_microunits == 1_000_000
        assert payment.credits_granted == 100
        assert payment.payment_intent_id == intent.id if intent else False
        assert intent is not None and intent.status == "PAID"
        assert intent.transaction_hash == TX_HASH
        assert len(ledger) == 1
        assert ledger[0].direction == "CREDIT"
        assert ledger[0].balance_before == 50
        assert ledger[0].balance_after == 150
        assert len(deliveries) == 2


def test_alchemy_dashboard_test_url_event_is_authenticated_and_acknowledged(tmp_path) -> None:
    container = _container(tmp_path)
    payload: dict[str, object] = {
        "webhookId": WEBHOOK_ID,
        "id": "whevt_dashboard_test_url",
        "createdAt": "2026-08-22T00:00:00Z",
        "type": "ADDRESS_ACTIVITY",
        "event": {"eventDetails": "Alchemy Test URL sample"},
    }

    response = _post(TestClient(create_app(container)), payload)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "event_id": "whevt_dashboard_test_url",
        "replayed": False,
        "result": "TEST_EVENT",
        "activity_count": 0,
        "accepted_count": 0,
        "credited_count": 0,
        "ignored_count": 0,
    }
    with container.database.session() as session:
        delivery = session.scalar(select(AlchemyWebhookDelivery))
        assert delivery is not None
        assert delivery.result == "TEST_EVENT"


@pytest.mark.parametrize(
    ("payload"),
    [
        _payload(token_address="0x3333333333333333333333333333333333333333"),
        _payload(to_address="0x4444444444444444444444444444444444444444"),
        _payload(network="ETH_MAINNET"),
    ],
)
def test_alchemy_webhook_ignores_non_native_usdc_or_wrong_network(tmp_path, payload) -> None:  # type: ignore[no-untyped-def]
    container = _container(tmp_path)
    workspace_id = _seed_verified_wallet(container)
    response = _post(TestClient(create_app(container)), payload)
    assert response.status_code == 200
    assert response.json()["credited_count"] == 0
    assert response.json()["ignored_count"] == 1
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None and workspace.credit_balance == 50
        assert session.scalar(select(OnchainPayment)) is None


def test_unmatched_wallet_is_recorded_without_credit(tmp_path) -> None:
    container = _container(tmp_path)
    response = _post(TestClient(create_app(container)), _payload())
    assert response.status_code == 200
    assert response.json()["accepted_count"] == 1
    assert response.json()["credited_count"] == 0
    with container.database.session() as session:
        payment = session.scalar(select(OnchainPayment))
        assert payment is not None and payment.status == "UNMATCHED"
        assert payment.workspace_id is None
        assert session.scalar(select(WorkspaceCreditLedgerEntry)) is None


def test_verified_wallet_without_payment_intent_requires_reconciliation(tmp_path) -> None:
    container = _container(tmp_path)
    workspace_id = _seed_verified_wallet(container)
    with container.database.session() as session:
        intent = session.scalar(select(OnchainPaymentIntent))
        assert intent is not None
        session.delete(intent)

    response = _post(TestClient(create_app(container)), _payload())

    assert response.status_code == 200
    assert response.json()["credited_count"] == 0
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        payment = session.scalar(select(OnchainPayment))
        assert workspace is not None and workspace.credit_balance == 50
        assert payment is not None and payment.status == "RECONCILIATION_REQUIRED"
        assert payment.payment_intent_id is None
        assert session.scalar(select(WorkspaceCreditLedgerEntry)) is None


def test_chain_reorg_posts_append_only_reversal_without_negative_balance(tmp_path) -> None:
    container = _container(tmp_path)
    workspace_id = _seed_verified_wallet(container)
    client = TestClient(create_app(container))
    assert _post(client, _payload()).status_code == 200

    removed = _payload(event_id="whevt_base_usdc_reorg", removed=True)
    response = _post(client, removed)
    assert response.status_code == 200
    assert response.json()["accepted_count"] == 1
    assert response.json()["credited_count"] == 0
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        payment = session.scalar(select(OnchainPayment))
        entries = list(
            session.scalars(
                select(WorkspaceCreditLedgerEntry).order_by(WorkspaceCreditLedgerEntry.created_at)
            )
        )
        assert workspace is not None and workspace.credit_balance == 50
        assert payment is not None and payment.status == "REMOVED"
        intent = session.scalar(select(OnchainPaymentIntent))
        assert intent is not None and intent.status == "RECONCILIATION_REQUIRED"
        assert [item.direction for item in entries] == ["CREDIT", "DEBIT"]
        assert entries[1].related_entry_id == entries[0].id

    with pytest.raises(IntegrityError, match="append-only"):
        with container.database.session() as session:
            entry = session.scalar(select(WorkspaceCreditLedgerEntry))
            assert entry is not None
            entry.credits = 99


def test_reorg_requires_reconciliation_if_credits_were_already_spent(tmp_path) -> None:
    container = _container(tmp_path)
    workspace_id = _seed_verified_wallet(container)
    client = TestClient(create_app(container))
    assert _post(client, _payload()).status_code == 200
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.credit_balance = 25

    response = _post(client, _payload(event_id="whevt_spent_reorg", removed=True))
    assert response.status_code == 200
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        payment = session.scalar(select(OnchainPayment))
        assert workspace is not None and workspace.credit_balance == 25
        assert payment is not None and payment.status == "RECONCILIATION_REQUIRED"
        assert len(list(session.scalars(select(WorkspaceCreditLedgerEntry)))) == 1
