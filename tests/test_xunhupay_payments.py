from __future__ import annotations

import json
from urllib.parse import urlencode

import httpx
from fastapi.testclient import TestClient
from payment_core import XunhuPayPaymentService
from platform_shared import Settings
from production_domain.models import (
    PaymentOrder,
    Workspace,
    WorkspaceCreditLedgerEntry,
    XunhuPayCheckoutSession,
    XunhuPaySettlement,
)
from sqlalchemy import select
from video_platform_api.container import build_container
from video_platform_api.main import create_app

APP_ID = "xunhupay-test-app"
APP_SECRET = "xunhupay-test-secret"
GATEWAY_URL = "https://api.xunhupay.test/payment/do.html"


def _container(tmp_path):  # type: ignore[no-untyped-def]
    container = build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'xunhupay.db'}",
            storage_root=tmp_path / "media",
            public_base_url="https://api.bestshiny.test",
            web_origins="http://testserver",
            auth_required=True,
            platform_api_key="test-platform-key",
            deployment_environment="test",
            xunhupay_app_id=APP_ID,
            xunhupay_app_secret=APP_SECRET,
            xunhupay_gateway_url=GATEWAY_URL,
            xunhupay_notify_url="https://api.bestshiny.test/v1/payments/xunhupay/notify",
            xunhupay_return_url="https://bestshiny.test/app",
        )
    )

    def gateway(request: httpx.Request) -> httpx.Response:
        assert request.url == GATEWAY_URL
        payload = json.loads(request.content)
        assert payload["total_fee"] in {"140.00", "450.00", "700.00"}
        assert payload["hash"] == XunhuPayPaymentService.generate_hash(payload, APP_SECRET)
        response: dict[str, object] = {
            "openid": f"gateway-{payload['trade_order_id']}",
            "url": "https://api.xunhupay.test/mobile/pay",
            "url_qrcode": "https://api.xunhupay.test/qr/pay.png",
            "errcode": 0,
            "errmsg": "success!",
        }
        response["hash"] = XunhuPayPaymentService.generate_hash(response, APP_SECRET)
        return httpx.Response(200, json=response)

    container.xunhupay_payments._client = httpx.Client(  # noqa: SLF001
        transport=httpx.MockTransport(gateway)
    )
    return container


def _registered(container) -> tuple[TestClient, str]:  # type: ignore[no-untyped-def]
    client = TestClient(create_app(container))
    response = client.post(
        "/api/auth/register",
        json={
            "email": "xunhupay-owner@example.com",
            "password": "correct-horse-battery-staple",
            "display_name": "XunHuPay owner",
            "workspace_name": "XunHuPay workspace",
        },
    )
    assert response.status_code == 201
    return client, response.json()["user"]["workspaces"][0]["id"]


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("ai_director_csrf") or ""}


def _checkout(client: TestClient, workspace_id: str, plan_id: str = "creator_50") -> dict:
    response = client.post(
        "/v1/payments/checkout",
        json={
            "workspace_id": workspace_id,
            "provider": "xunhupay",
            "plan_id": plan_id,
        },
        headers=_csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _notification(
    container,  # type: ignore[no-untyped-def]
    *,
    total_fee: str,
    transaction_id: str = "transaction-001",
    open_order_id: str = "open-order-001",
) -> dict[str, str]:
    with container.database.session() as session:
        checkout = session.scalar(select(XunhuPayCheckoutSession))
        assert checkout is not None
        trade_order_id = checkout.trade_order_id
    payload = {
        "trade_order_id": trade_order_id,
        "total_fee": total_fee,
        "transaction_id": transaction_id,
        "open_order_id": open_order_id,
        "order_title": "BestShiny Credits",
        "status": "OD",
        "plugins": "BestShiny",
        "appid": APP_ID,
        "time": "1788336000",
        "nonce_str": "notification-001",
    }
    payload["hash"] = XunhuPayPaymentService.generate_hash(payload, APP_SECRET)
    return payload


def _post_notification(client: TestClient, payload: dict[str, str]):
    return client.post(
        "/v1/payments/xunhupay/notify",
        content=urlencode(payload),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )


def test_checkout_uses_only_server_owned_plan_price_and_credits(tmp_path) -> None:
    container = _container(tmp_path)
    client, workspace_id = _registered(container)

    config = client.get("/v1/payments/config")
    assert config.status_code == 200
    assert config.json()["xunhupay_configured"] is True
    assert config.json()["xunhupay_packages"] == [
        {"sku": "starter_20", "amount": "140.00", "currency": "CNY", "credits": 1_800,
         "recommended": False},
        {"sku": "creator_50", "amount": "450.00", "currency": "CNY", "credits": 6_000,
         "recommended": True},
        {"sku": "pro_100", "amount": "700.00", "currency": "CNY", "credits": 11_000,
         "recommended": False},
    ]

    forged = client.post(
        "/v1/payments/checkout",
        json={
            "workspace_id": workspace_id,
            "provider": "xunhupay",
            "plan_id": "creator_50",
            "amount": "0.01",
            "credits": 999_999,
        },
        headers=_csrf(client),
    )
    assert forged.status_code == 422

    checkout = _checkout(client, workspace_id)
    assert (checkout["amount"], checkout["currency"], checkout["credits"]) == (
        "450.00",
        "CNY",
        6_000,
    )
    assert "secret" not in json.dumps(checkout).lower()
    with container.database.session() as session:
        order = session.scalar(select(PaymentOrder))
        workspace = session.get(Workspace, workspace_id)
        assert order is not None and workspace is not None
        assert (order.provider, order.amount, order.credits, order.status) == (
            "XUNHUPAY",
            450,
            6_000,
            "PENDING",
        )
        assert workspace.credit_balance == 50


def test_signed_notification_posts_once_through_unified_credit_ledger(tmp_path) -> None:
    container = _container(tmp_path)
    client, workspace_id = _registered(container)
    _checkout(client, workspace_id, "starter_20")
    payload = _notification(container, total_fee="140")

    first = _post_notification(client, payload)
    second = _post_notification(client, payload)
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.text == second.text == "success"

    history = client.get(f"/v1/workspaces/{workspace_id}/payments/history")
    assert history.status_code == 200
    assert history.json()["items"] == [
        {
            "id": history.json()["items"][0]["id"],
            "plan_id": "starter_20",
            "provider": "xunhupay",
            "amount": "140.00",
            "currency": "CNY",
            "credits": 1_800,
            "status": "PAID",
            "created_at": history.json()["items"][0]["created_at"],
            "paid_at": history.json()["items"][0]["paid_at"],
        }
    ]

    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        order = session.scalar(select(PaymentOrder))
        checkout = session.scalar(select(XunhuPayCheckoutSession))
        settlements = list(session.scalars(select(XunhuPaySettlement)))
        ledger = list(session.scalars(select(WorkspaceCreditLedgerEntry)))
        assert workspace is not None and order is not None and checkout is not None
        assert (workspace.plan_tier, workspace.credit_balance) == ("PRO", 1_850)
        assert order.status == checkout.status == "PAID"
        assert len(settlements) == len(ledger) == 1
        assert settlements[0].status == "CREDITED"
        assert ledger[0].entry_type == "CNY_PURCHASE"
        assert ledger[0].payment_id is None
        assert ledger[0].xunhupay_settlement_id == settlements[0].id
        assert ledger[0].credits == 1_800


def test_bad_signature_and_amount_mismatch_never_credit_workspace(tmp_path) -> None:
    container = _container(tmp_path)
    client, workspace_id = _registered(container)
    _checkout(client, workspace_id)
    payload = _notification(container, total_fee="0.01")

    forged = dict(payload)
    forged["hash"] = "0" * 32
    rejected = _post_notification(client, forged)
    assert rejected.status_code == 401

    quarantined = _post_notification(client, payload)
    assert quarantined.status_code == 200
    assert quarantined.text == "success"
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        order = session.scalar(select(PaymentOrder))
        checkout = session.scalar(select(XunhuPayCheckoutSession))
        settlement = session.scalar(select(XunhuPaySettlement))
        assert workspace is not None and order is not None and checkout is not None
        assert workspace.credit_balance == 50
        assert order.status == checkout.status == "RECONCILIATION_REQUIRED"
        assert settlement is not None and settlement.status == "RECONCILIATION_REQUIRED"
        assert session.scalar(select(WorkspaceCreditLedgerEntry)) is None


def test_notification_requires_form_encoding_and_paid_status(tmp_path) -> None:
    container = _container(tmp_path)
    client, workspace_id = _registered(container)
    _checkout(client, workspace_id)
    payload = _notification(container, total_fee="450.00")

    wrong_content_type = client.post(
        "/v1/payments/xunhupay/notify",
        json=payload,
    )
    assert wrong_content_type.status_code == 415

    payload["status"] = "CD"
    payload["hash"] = XunhuPayPaymentService.generate_hash(payload, APP_SECRET)
    refunded = _post_notification(client, payload)
    assert refunded.status_code == 400
    with container.database.session() as session:
        assert session.scalar(select(XunhuPaySettlement)) is None
        assert session.scalar(select(WorkspaceCreditLedgerEntry)) is None
