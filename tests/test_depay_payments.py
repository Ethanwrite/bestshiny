from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from payment_core import DePayPaymentService, PaymentPackage
from platform_shared import Settings
from production_domain.models import (
    DePayCheckoutSession,
    DePayWebhookDelivery,
    OnchainPayment,
    PaymentOrder,
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
INTEGRATION_ID = "2655544b-edb1-4433-bfd6-4ece0d764ed6"

_DYNAMIC_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem_private(key: rsa.RSAPrivateKey) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _keys() -> tuple[rsa.RSAPrivateKey, str]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, public.decode()


def _container(tmp_path, public_key: str, dynamic_key: str | None = None):  # type: ignore[no-untyped-def]
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
            depay_integration_id=INTEGRATION_ID,
            depay_callback_public_key=public_key,
            depay_dynamic_config_private_key=(
                _pem_private(_DYNAMIC_PRIVATE) if dynamic_key is None else dynamic_key
            ),
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


def _create_checkout(
    client: TestClient,
    container,  # type: ignore[no-untyped-def]
    workspace_id: str,
    sku: str = "creator_50",
    **extra: object,
) -> tuple[dict, str, str]:
    response = client.post(
        "/v1/payments/checkout",
        json={"workspace_id": workspace_id, "sku": sku, **extra},
        headers=_csrf(client),
    )
    assert response.status_code == 201, response.text
    checkout = response.json()
    with container.database.session() as session:
        row = session.get(DePayCheckoutSession, checkout["id"])
        assert row is not None and row.payment_intent_id
        order_ref = row.payment_intent_id
    return checkout, checkout["checkout_token"], order_ref


def _callback_payload(
    token: str,
    order_ref: str,
    *,
    amount: str = "50.000000",
    transaction: str = TX_HASH,
    commitment: str = "confirmed",
    blockchain: str = "base",
    receiver: str = TREASURY,
    depay_token: str = USDC,
) -> dict[str, object]:
    return {
        "status": "success",
        "blockchain": blockchain,
        "transaction": transaction,
        "sender": PAYER,
        "receiver": receiver,
        "token": depay_token,
        "decimals": 6,
        "commitment": commitment,
        "confirmations": 1,
        "after_block": "123456",
        "amount": amount,
        "payload": {
            "link_id": INTEGRATION_ID,
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


def _post_dynamic_config(container, private, body: dict[str, object]):  # type: ignore[no-untyped-def]
    raw, signature = _signed(private, body)
    return TestClient(create_app(container)).post(
        "/v1/payments/depay/config",
        content=raw,
        headers={"content-type": "application/json", "x-signature": signature},
    )


def test_three_packages_are_server_owned_and_creator_50_is_recommended(tmp_path) -> None:
    _private, public = _keys()
    container = _container(tmp_path, public)
    client, _workspace_id = _registered(container)

    config = client.get("/v1/payments/config")
    assert config.status_code == 200
    body = config.json()
    assert body["checkout_provider"] == "DEPAY"
    assert body["depay_dynamic_configured"] is True
    assert body["depay_integration_id"] == INTEGRATION_ID
    assert body["payment_packages"] == [
        {"sku": "starter_20", "amount": "20.00", "currency": "USDC",
         "credits": 1_800, "recommended": False},
        {"sku": "creator_50", "amount": "50.00", "currency": "USDC",
         "credits": 5_000, "recommended": True},
        {"sku": "pro_100", "amount": "100.00", "currency": "USDC",
         "credits": 11_000, "recommended": False},
    ]
    # Bookkeeping the browser has no use for stays server-side.
    for plan in body["payment_packages"]:
        assert "pricing_version" not in plan and "provider" not in plan


def test_every_tier_credits_exactly_its_snapshot(tmp_path) -> None:
    for index, (sku, amount, credits) in enumerate(
        (("starter_20", "20.0", 1_800), ("creator_50", "50.0", 5_000), ("pro_100", "100.0", 11_000))
    ):
        private, public = _keys()
        container = _container(tmp_path / f"tier{index}", public)
        client, workspace_id = _registered(container)
        checkout, token, order_ref = _create_checkout(client, container, workspace_id, sku)
        assert checkout["sku"] == sku
        assert checkout["credits"] == credits

        response = _post_callback(
            container, private, _callback_payload(token, order_ref, amount=amount)
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"] == "CREDITED"
        assert response.json()["credits_granted"] == credits

        with container.database.session() as session:
            workspace = session.get(Workspace, workspace_id)
            order = session.scalar(select(PaymentOrder))
            ledger = list(session.scalars(select(WorkspaceCreditLedgerEntry)))
            assert workspace is not None
            assert (workspace.plan_tier, workspace.credit_balance) == ("PRO", credits + 50)
            assert order is not None and order.status == "PAID"
            assert order.sku == sku and order.provider == "DEPAY" and order.currency == "USDC"
            assert order.amount == Decimal(amount)
            assert order.pricing_version == "2026-08-30.v1"
            assert len(ledger) == 1
            assert ledger[0].metadata_json["sku"] == sku


def test_finalized_commitment_settles_like_confirmed(tmp_path) -> None:
    """Regression for the 2026-08-30 fulfillment failure.

    Treasury had the USDC and DePay's signed callback said so, but the service
    demanded the literal string "confirmed" and answered 400 to the stronger
    "finalized" commitment, thirteen times.
    """
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    _checkout, token, order_ref = _create_checkout(client, container, workspace_id)

    response = _post_callback(
        container,
        private,
        _callback_payload(token, order_ref, commitment="finalized"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "CREDITED"
    assert response.json()["credits_granted"] == 5_000

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        checkout_row = session.scalar(select(DePayCheckoutSession))
        assert workspace is not None and workspace.credit_balance == 5_050
        assert checkout_row is not None and checkout_row.status == "PAID"


def test_unrecognised_commitment_is_still_refused(tmp_path) -> None:
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    _checkout, token, order_ref = _create_checkout(client, container, workspace_id)

    response = _post_callback(
        container,
        private,
        _callback_payload(token, order_ref, commitment="pending"),
    )
    assert response.status_code == 400
    with container.database.session() as session:
        assert session.scalar(select(WorkspaceCreditLedgerEntry)) is None


def test_client_cannot_set_amount_or_credits(tmp_path) -> None:
    _private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)

    checkout, _token, order_ref = _create_checkout(
        client,
        container,
        workspace_id,
        "starter_20",
        amount="1",
        amount_usdc="1",
        credits=999_999,
    )
    assert checkout["amount_usdc"] == "20.00"
    assert checkout["credits"] == 1_800
    with container.database.session() as session:
        order = session.get(PaymentOrder, order_ref)
        assert order is not None
        assert order.raw_amount_microunits == 20_000_000 and order.credits == 1_800

    unknown = client.post(
        "/v1/payments/checkout",
        json={"workspace_id": workspace_id, "sku": "free_1000000"},
        headers=_csrf(client),
    )
    assert unknown.status_code == 422


def test_dynamic_config_returns_the_frozen_amount_and_never_settles(tmp_path) -> None:
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    _checkout, token, order_ref = _create_checkout(client, container, workspace_id, "pro_100")

    # The browser only ever holds the checkout token; the order id is ours.
    response = _post_dynamic_config(container, private, {"checkout_token": token})
    assert response.status_code == 200, response.text
    body = response.json()
    # DePay documents `amount` as a JSON number, not a string.
    assert body["accept"] == [
        {"blockchain": "base", "amount": 100, "token": USDC, "receiver": TREASURY}
    ]
    assert isinstance(json.loads(response.content)["accept"][0]["amount"], float)
    assert b" " not in response.content and b"\n" not in response.content
    assert body["payload"]["link_id"] == INTEGRATION_ID
    assert body["payload"]["injected"]["order_ref"] == order_ref

    signature = response.headers["x-signature"]
    _DYNAMIC_PRIVATE.public_key().verify(
        base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
        response.content,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=64),
        hashes.SHA256(),
    )

    with container.database.session() as session:
        order = session.get(PaymentOrder, order_ref)
        assert order is not None and order.status == "PENDING" and order.paid_at is None
        assert session.scalar(select(WorkspaceCreditLedgerEntry)) is None
        assert session.scalar(select(OnchainPayment)) is None
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None and workspace.credit_balance == 50


def test_dynamic_config_refuses_client_chosen_prices_and_bad_signatures(tmp_path) -> None:
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    _checkout, token, order_ref = _create_checkout(client, container, workspace_id)

    for tampered in (
        {"checkout_token": token, "amount": "1"},
        {"checkout_token": token, "amount_usdc": "1"},
        {"checkout_token": token, "credits": 100_000},
        {"checkout_token": token, "order_ref": "some-other-order"},
    ):
        assert _post_dynamic_config(container, private, tampered).status_code == 400

    raw, _signature = _signed(private, {"checkout_token": token})
    unsigned = TestClient(create_app(container)).post(
        "/v1/payments/depay/config",
        content=raw,
        headers={"x-signature": "not-a-signature"},
    )
    assert unsigned.status_code == 401

    unknown = _post_dynamic_config(container, private, {"checkout_token": "not-a-real-token"})
    assert unknown.status_code == 400


def test_duplicate_callbacks_produce_exactly_one_fulfillment(tmp_path) -> None:
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    _checkout, token, order_ref = _create_checkout(client, container, workspace_id)
    payload = _callback_payload(token, order_ref)

    first = _post_callback(container, private, payload)
    assert first.status_code == 200 and first.json()["replayed"] is False
    for _ in range(3):
        replay = _post_callback(container, private, payload)
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True
        assert replay.json()["result"] == "CREDITED"

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        ledger = list(session.scalars(select(WorkspaceCreditLedgerEntry)))
        deliveries = list(session.scalars(select(DePayWebhookDelivery)))
        payments = list(session.scalars(select(OnchainPayment)))
        assert workspace is not None and workspace.credit_balance == 5_050
        assert len(ledger) == 1 and len(deliveries) == 1 and len(payments) == 1


def test_historical_order_settles_against_its_own_snapshot_after_repricing(tmp_path) -> None:
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    _checkout, token, order_ref = _create_checkout(client, container, workspace_id)

    # The catalogue is repriced after the order was placed. The order keeps its
    # own terms: 50 USDC for 5,000 credits, not 80 USDC for 2,000.
    repriced = DePayPaymentService(
        container.database,
        payment_link_url=LINK_URL,
        integration_id=INTEGRATION_ID,
        legacy_link_id=LINK_ID,
        callback_public_key=public,
        dynamic_config_private_key=_pem_private(_DYNAMIC_PRIVATE),
        treasury_address=TREASURY,
        packages={
            "creator_50": PaymentPackage(
                sku="creator_50",
                amount=Decimal("80"),
                credits=2_000,
                pricing_version="2026-09-01.v2",
            )
        },
    )
    raw, signature = _signed(private, _callback_payload(token, order_ref))
    result = repriced.handle_callback(raw, signature)
    assert result.result == "CREDITED"
    assert result.credits_granted == 5_000

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        order = session.get(PaymentOrder, order_ref)
        ledger = session.scalar(select(WorkspaceCreditLedgerEntry))
        assert workspace is not None and workspace.credit_balance == 5_050
        assert order is not None and order.amount == Decimal("50")
        assert order.credits == 5_000 and order.pricing_version == "2026-08-30.v1"
        assert ledger is not None and ledger.metadata_json["pricing_version"] == "2026-08-30.v1"


def test_callback_refuses_wrong_amount_network_token_or_treasury(tmp_path) -> None:
    private, public = _keys()
    wrong_treasury = "0x3333333333333333333333333333333333333333"
    wrong_token = "0x4444444444444444444444444444444444444444"

    # A short amount is authenticated and on Base, so it reaches settlement and
    # is quarantined there rather than refused at the boundary.
    container = _container(tmp_path / "amount", public)
    client, workspace_id = _registered(container)
    _checkout, token, order_ref = _create_checkout(client, container, workspace_id)
    short = _post_callback(
        container, private, _callback_payload(token, order_ref, amount="49.000000")
    )
    assert short.status_code == 200
    assert short.json()["result"] == "RECONCILIATION_REQUIRED"
    assert short.json()["credits_granted"] == 0
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None and workspace.credit_balance == 50
        assert session.scalar(select(WorkspaceCreditLedgerEntry)) is None

    for index, payload_kwargs in enumerate(
        (
            {"blockchain": "ethereum"},
            {"receiver": wrong_treasury},
            {"depay_token": wrong_token},
        )
    ):
        rejected_container = _container(tmp_path / f"reject{index}", public)
        reject_client, reject_workspace = _registered(rejected_container)
        _c, reject_token, reject_ref = _create_checkout(
            reject_client, rejected_container, reject_workspace
        )
        response = _post_callback(
            rejected_container,
            private,
            _callback_payload(reject_token, reject_ref, **payload_kwargs),
        )
        assert response.status_code == 400, payload_kwargs
        with rejected_container.database.session() as session:
            assert session.scalar(select(WorkspaceCreditLedgerEntry)) is None


def test_callback_refuses_bad_signature_and_unknown_order(tmp_path) -> None:
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    _checkout, token, order_ref = _create_checkout(client, container, workspace_id)
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
    for payload in (
        _callback_payload("unknown-token", order_ref),
        _callback_payload(token, "pi-does-not-match"),
    ):
        raw, signature = _signed(private, payload)
        assert (
            webhook_client.post(
                "/v1/webhooks/depay",
                content=raw,
                headers={"x-signature": signature},
            ).status_code
            == 400
        )
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None and workspace.credit_balance == 50
        assert workspace.plan_tier == "FREE"
        assert session.scalar(select(WorkspaceCreditLedgerEntry)) is None


def test_second_purchase_tops_up_without_re_upgrading(tmp_path) -> None:
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    first, first_token, first_ref = _create_checkout(client, container, workspace_id, "starter_20")
    assert first["purchase_kind"] == "UPGRADE_PRO_AND_CREDITS"
    assert (
        _post_callback(
            container, private, _callback_payload(first_token, first_ref, amount="20.0")
        ).status_code
        == 200
    )

    second, second_token, second_ref = _create_checkout(client, container, workspace_id, "pro_100")
    assert second["purchase_kind"] == "TOP_UP_CREDITS"
    response = _post_callback(
        container,
        private,
        _callback_payload(
            second_token, second_ref, amount="100.0", transaction="0x" + "e" * 64
        ),
    )
    assert response.status_code == 200
    assert response.json()["credits_granted"] == 11_000
    assert response.json()["pro_activated"] is False

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        ledger = list(
            session.scalars(
                select(WorkspaceCreditLedgerEntry).order_by(WorkspaceCreditLedgerEntry.created_at)
            )
        )
        assert workspace is not None
        assert (workspace.plan_tier, workspace.credit_balance) == ("PRO", 12_850)
        assert len(ledger) == 2
        assert [entry.metadata_json["sku"] for entry in ledger] == ["starter_20", "pro_100"]


def test_checkout_fails_closed_without_callback_or_dynamic_config_keys(tmp_path) -> None:
    for index, (public_key, dynamic_key) in enumerate((("", None), (None, ""))):
        _private, generated = _keys()
        container = _container(
            tmp_path / f"closed{index}",
            generated if public_key is None else public_key,
            dynamic_key,
        )
        client, workspace_id = _registered(container)
        response = client.post(
            "/v1/payments/checkout",
            json={"workspace_id": workspace_id, "sku": "creator_50"},
            headers=_csrf(client),
        )
        assert response.status_code == 503
        assert "支付入口已关闭" in response.json()["detail"]
        with container.database.session() as session:
            assert session.scalar(select(PaymentOrder)) is None


def test_a_lapsed_window_does_not_strand_a_settled_payment(tmp_path) -> None:
    """DePay retries for ~21 days; our checkout window is 24 hours.

    The money is in Treasury and the order's terms are frozen, so a callback
    that arrives after the window closed still has to fulfill. Refusing it
    would leave a real payment needing manual repair — which is exactly the
    hole the 2026-08-30 incident sat in while the fix was written.
    """
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    checkout, token, order_ref = _create_checkout(client, container, workspace_id)

    lapsed = datetime.now(UTC) - timedelta(hours=6)
    with container.database.session() as session:
        session.get(DePayCheckoutSession, checkout["id"]).expires_at = lapsed
        session.get(PaymentOrder, order_ref).expires_at = lapsed

    response = _post_callback(
        container,
        private,
        _callback_payload(token, order_ref, commitment="finalized"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "CREDITED"
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None and workspace.credit_balance == 5_050
        assert len(list(session.scalars(select(WorkspaceCreditLedgerEntry)))) == 1


def test_a_legacy_order_without_a_package_still_settles_on_its_own_terms(tmp_path) -> None:
    """The order in flight when the bug was found predates SKUs entirely.

    Migration 0066 backfills it as `legacy_depay_fixed`, and settlement reads
    the row, so it credits its own 3,000 rather than any current package.
    """
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    checkout, token, order_ref = _create_checkout(client, container, workspace_id)

    with container.database.session() as session:
        order = session.get(PaymentOrder, order_ref)
        order.sku = "legacy_depay_fixed"
        order.pricing_version = "legacy_depay_v1"
        order.amount = Decimal("30")
        order.raw_amount_microunits = 30_000_000
        order.credits = 3_000

    response = _post_callback(
        container, private, _callback_payload(token, order_ref, amount="30.0")
    )
    assert response.status_code == 200, response.text
    assert response.json()["credits_granted"] == 3_000
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        ledger = session.scalar(select(WorkspaceCreditLedgerEntry))
        assert workspace is not None and workspace.credit_balance == 3_050
        assert ledger is not None and ledger.metadata_json["sku"] == "legacy_depay_fixed"


def test_a_transfer_alchemy_already_credited_is_not_credited_twice(tmp_path) -> None:
    """One transfer, one fulfillment — whichever plane observes it first.

    The reconciliation plane can adopt a Treasury transfer before DePay's
    callback lands. When it does, the callback must converge the status the
    browser polls without posting a second ledger entry, and without demoting
    a payment that is already good.
    """
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    checkout, token, order_ref = _create_checkout(client, container, workspace_id)

    # Stand in for the other plane: the transfer is on the books and credited.
    with container.database.session() as session:
        order = session.get(PaymentOrder, order_ref)
        payment = OnchainPayment(
            network="BASE_MAINNET",
            chain_id=8453,
            transaction_hash=TX_HASH,
            log_index="7",
            block_number="123456",
            from_address=PAYER,
            to_address=TREASURY,
            token_address=USDC,
            token_decimals=6,
            raw_amount_microunits=50_000_000,
            workspace_id=workspace_id,
            payment_intent_id=order_ref,
            provider_event_id="alchemy:reconciled",
            credits_granted=order.credits,
            status="CREDITED",
            metadata_json={"source": "ALCHEMY"},
        )
        session.add(payment)
        session.flush([payment])
        workspace = session.get(Workspace, workspace_id)
        session.add(
            WorkspaceCreditLedgerEntry(
                workspace_id=workspace_id,
                payment_id=payment.id,
                external_reference="alchemy:reconciled",
                entry_type="USDC_PURCHASE",
                direction="CREDIT",
                credits=order.credits,
                balance_before=workspace.credit_balance,
                balance_after=workspace.credit_balance + order.credits,
                currency="USDC",
                raw_amount_microunits=50_000_000,
                chain_id=8453,
                metadata_json={"source": "ALCHEMY"},
            )
        )
        workspace.credit_balance += order.credits

    response = _post_callback(container, private, _callback_payload(token, order_ref))
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "ALREADY_CREDITED"

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        payment = session.scalar(select(OnchainPayment))
        checkout_row = session.get(DePayCheckoutSession, checkout["id"])
        ledger = list(session.scalars(select(WorkspaceCreditLedgerEntry)))
        assert workspace is not None and workspace.credit_balance == 5_050
        assert len(ledger) == 1
        assert payment is not None and payment.status == "CREDITED"
        assert checkout_row is not None and checkout_row.status == "PAID"
        assert checkout_row.credits_granted == 5_000


def test_a_managed_integration_callback_shape_settles(tmp_path) -> None:
    """The widget's callback names the integration at the document root.

    The payment-link flow echoed `payload.link_id` back instead, and the two
    shapes have to settle identically — a body that identifies a *different*
    integration must not.
    """
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    _checkout, token, order_ref = _create_checkout(client, container, workspace_id)

    managed = _callback_payload(token, order_ref, commitment="finalized")
    managed["integration_id"] = INTEGRATION_ID
    managed["payload"] = {"injected": {"checkout_token": token, "order_ref": order_ref}}

    response = _post_callback(container, private, managed)
    assert response.status_code == 200, response.text
    assert response.json()["credits_granted"] == 5_000

    foreign_container = _container(tmp_path / "foreign", public)
    foreign_client, foreign_workspace = _registered(foreign_container)
    _c, foreign_token, foreign_ref = _create_checkout(
        foreign_client, foreign_container, foreign_workspace
    )
    foreign = _callback_payload(foreign_token, foreign_ref)
    foreign["integration_id"] = "11111111-2222-3333-4444-555555555555"
    foreign["payload"] = {"injected": {"checkout_token": foreign_token, "order_ref": foreign_ref}}
    assert _post_callback(foreign_container, private, foreign).status_code == 400
    with foreign_container.database.session() as session:
        assert session.scalar(select(WorkspaceCreditLedgerEntry)) is None


def test_the_signed_bytes_are_the_bytes_returned(tmp_path) -> None:
    """Sign the response body, not a re-serialization of it.

    A dict serialized twice can differ in key order or spacing, and DePay
    verifies the bytes it received.
    """
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    _checkout, token, _order_ref = _create_checkout(client, container, workspace_id, "starter_20")

    response = _post_dynamic_config(container, private, {"checkout_token": token})
    assert response.status_code == 200
    signature = response.headers["x-signature"]
    assert signature.endswith("=") or len(signature) % 4 == 0
    _DYNAMIC_PRIVATE.public_key().verify(
        base64.urlsafe_b64decode(signature),
        response.content,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=64),
        hashes.SHA256(),
    )


def test_a_callback_that_states_no_commitment_still_settles(tmp_path) -> None:
    """The shape production actually sends.

    DePay's real callback for this integration carries no `commitment` at all.
    Requiring one refused a signed, confirmed, exact-amount transfer to
    Treasury — the second day of the same outage. Settlement rests on the
    signature, `status`, the confirmation count and the frozen snapshot;
    `commitment` corroborates when stated and is absent otherwise.
    """
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    _checkout, token, order_ref = _create_checkout(client, container, workspace_id)

    payload = _callback_payload(token, order_ref)
    del payload["commitment"]

    response = _post_callback(container, private, payload)
    assert response.status_code == 200, response.text
    assert response.json()["result"] == "CREDITED"
    assert response.json()["credits_granted"] == 5_000
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        delivery = session.scalar(select(DePayWebhookDelivery))
        assert workspace is not None and workspace.credit_balance == 5_050
        # The receipt records that the level was unstated rather than claiming
        # a confirmation level DePay never gave.
        assert delivery is not None
        assert delivery.metadata_json["commitment"] == "unstated:1conf"


def test_a_null_commitment_is_treated_as_unstated(tmp_path) -> None:
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    _checkout, token, order_ref = _create_checkout(client, container, workspace_id)

    payload = _callback_payload(token, order_ref)
    payload["commitment"] = None
    assert _post_callback(container, private, payload).status_code == 200


def test_an_unconfirmed_payment_is_still_refused_either_way(tmp_path) -> None:
    """Silence is not a contradiction, but a contradiction is."""
    private, public = _keys()
    container = _container(tmp_path, public)
    client, workspace_id = _registered(container)
    _checkout, token, order_ref = _create_checkout(client, container, workspace_id)

    # A stated level we do not recognise is refused even with confirmations.
    stated = _callback_payload(token, order_ref, commitment="pending")
    assert _post_callback(container, private, stated).status_code == 400

    # No stated level and no confirmation is refused too — the fallback rests
    # on the confirmation count, so it must actually be there.
    silent = _callback_payload(token, order_ref)
    del silent["commitment"]
    silent["confirmations"] = 0
    assert _post_callback(container, private, silent).status_code == 400

    with container.database.session() as session:
        assert session.scalar(select(WorkspaceCreditLedgerEntry)) is None
        assert session.scalar(select(DePayWebhookDelivery)) is None
