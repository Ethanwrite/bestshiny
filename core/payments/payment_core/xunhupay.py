from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urljoin, urlparse

import httpx
from platform_database import Database
from platform_shared import affected_rows
from production_domain.models import (
    PaymentOrder,
    Workspace,
    WorkspaceCreditLedgerEntry,
    XunhuPayCheckoutSession,
    XunhuPaySettlement,
    new_id,
    utcnow,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .catalog import XUNHUPAY_PACKAGES, PaymentPackage

logger = logging.getLogger(__name__)

_MERCHANT_ORDER = re.compile(r"^[0-9A-Za-z_*-]{1,32}$")
_PROVIDER_ID = re.compile(r"^[0-9A-Za-z_*-]{1,64}$")


class XunhuPayError(RuntimeError):
    pass


class XunhuPayConfigurationError(XunhuPayError):
    pass


class XunhuPayAuthenticationError(XunhuPayError):
    pass


class XunhuPayPayloadError(XunhuPayError):
    pass


class XunhuPayConflict(XunhuPayError):
    pass


class XunhuPayGatewayError(XunhuPayError):
    pass


@dataclass(frozen=True)
class XunhuPayCheckoutResult:
    checkout_id: str
    order_id: str
    plan_id: str
    amount_cny: str
    credits: int
    currency: str
    provider: str
    purchase_kind: str
    checkout_url: str | None
    qrcode_url: str | None
    expires_at: datetime


@dataclass(frozen=True)
class XunhuPayNotificationResult:
    transaction_id: str
    replayed: bool
    result: str
    credits_granted: int
    plan_tier: str | None = None
    pro_activated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "transaction_id": self.transaction_id,
            "replayed": self.replayed,
            "result": self.result,
            "credits_granted": self.credits_granted,
            "plan_tier": self.plan_tier,
            "pro_activated": self.pro_activated,
        }


class XunhuPayPaymentService:
    """Create server-priced CNY orders and settle only authenticated notifications."""

    max_body_bytes = 65_536

    def __init__(
        self,
        database: Database,
        *,
        app_id: str,
        app_secret: str,
        gateway_url: str,
        public_base_url: str,
        notify_url: str = "",
        return_url: str = "",
        packages: Mapping[str, PaymentPackage] | None = None,
        checkout_ttl_minutes: int = 30,
        timeout_seconds: int = 15,
        client: httpx.Client | None = None,
    ) -> None:
        self.database = database
        self.app_id = app_id.strip()
        self.app_secret = app_secret.strip()
        self.gateway_url = gateway_url.strip()
        self.notify_url = notify_url.strip() or urljoin(
            public_base_url.rstrip("/") + "/", "v1/payments/xunhupay/notify"
        )
        self.return_url = return_url.strip()
        self.checkout_ttl = timedelta(minutes=max(5, min(checkout_ttl_minutes, 1_440)))
        self._packages = dict(packages if packages is not None else XUNHUPAY_PACKAGES)
        if any(
            plan_id != package.sku
            or package.provider != "XUNHUPAY"
            or package.currency != "CNY"
            for plan_id, package in self._packages.items()
        ):
            raise ValueError("XunHuPay package registry is invalid")
        self._client = client or httpx.Client(timeout=max(2, min(timeout_seconds, 60)))

    @property
    def configured(self) -> bool:
        return bool(
            self.app_id
            and self.app_secret
            and self._is_https_url(self.gateway_url)
            and self._is_https_url(self.notify_url)
            and (not self.return_url or self._is_https_url(self.return_url))
        )

    def package_views(self) -> list[dict[str, object]]:
        return [package.as_public_dict() for package in self._packages.values()]

    def create_checkout(
        self,
        *,
        workspace_id: str,
        user_id: str,
        plan_id: str,
    ) -> XunhuPayCheckoutResult:
        package = self._packages.get(plan_id)
        if package is None:
            raise XunhuPayPayloadError("Unknown or unavailable payment pack")
        if not self.configured:
            raise XunhuPayConfigurationError("XunHuPay is not fully configured")

        now = utcnow()
        expires_at = now + self.checkout_ttl
        trade_order_id = secrets.token_hex(16)
        with self.database.session() as session:
            workspace = session.scalar(
                select(Workspace).where(Workspace.id == workspace_id).with_for_update()
            )
            if workspace is None or workspace.status != "ACTIVE":
                raise XunhuPayPayloadError("Workspace not found or unavailable")
            if workspace.plan_tier not in {"FREE", "PRO"}:
                raise XunhuPayPayloadError(
                    f"The {workspace.plan_tier} plan does not support this payment method"
                )
            purchase_kind = (
                "UPGRADE_PRO_AND_CREDITS" if workspace.plan_tier == "FREE" else "TOP_UP_CREDITS"
            )
            order = PaymentOrder(
                workspace_id=workspace.id,
                wallet_binding_id=None,
                network="XUNHUPAY",
                chain_id=None,
                from_address=None,
                to_address=None,
                token_address=None,
                sku=package.sku,
                amount=package.amount,
                currency=package.currency,
                raw_amount_microunits=package.raw_amount_microunits,
                credits=package.credits,
                pricing_version=package.pricing_version,
                provider=package.provider,
                status="PENDING",
                expires_at=expires_at,
                metadata_json={
                    "purchase_kind": purchase_kind,
                    "plan_at_creation": workspace.plan_tier,
                    "recurring": False,
                    "xunhupay_app_id": self.app_id,
                },
            )
            session.add(order)
            session.flush([order])
            checkout = XunhuPayCheckoutSession(
                workspace_id=workspace.id,
                user_id=user_id,
                payment_order_id=order.id,
                trade_order_id=trade_order_id,
                credits_granted=0,
                status="PENDING",
                expires_at=expires_at,
                metadata_json={"plan_id": package.sku, "payment_order_id": order.id},
            )
            session.add(checkout)
            session.flush([checkout])
            checkout_id = checkout.id
            order_id = order.id

        request_payload = self._checkout_payload(package, trade_order_id, now)
        try:
            response = self._client.post(self.gateway_url, json=request_payload)
            response.raise_for_status()
            gateway_payload = response.json()
            if not isinstance(gateway_payload, dict):
                raise ValueError("response is not an object")
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            self._mark_checkout(checkout_id, "RECONCILIATION_REQUIRED")
            raise XunhuPayGatewayError(
                "The XunHuPay order result is uncertain; do not pay and create a new order"
            ) from exc

        try:
            self._verify_hash(gateway_payload)
        except XunhuPayAuthenticationError:
            self._mark_checkout(checkout_id, "RECONCILIATION_REQUIRED")
            raise

        errcode = gateway_payload.get("errcode")
        if isinstance(errcode, bool) or str(errcode) != "0":
            self._mark_checkout(checkout_id, "CANCELLED")
            logger.warning("XunHuPay checkout refused: errcode=%r", errcode)
            raise XunhuPayGatewayError("XunHuPay could not create the order; try again later")

        try:
            checkout_url = self._optional_https_url(gateway_payload.get("url"))
            qrcode_url = self._optional_https_url(gateway_payload.get("url_qrcode"))
        except XunhuPayGatewayError:
            self._mark_checkout(checkout_id, "RECONCILIATION_REQUIRED")
            raise
        if not checkout_url and not qrcode_url:
            self._mark_checkout(checkout_id, "RECONCILIATION_REQUIRED")
            raise XunhuPayGatewayError("XunHuPay did not return a usable payment URL")
        gateway_order_id = str(
            gateway_payload.get("openid")
            or gateway_payload.get("orderid")
            or gateway_payload.get("oderid")
            or ""
        ).strip()[:64]
        with self.database.session() as session:
            checkout = session.scalar(
                select(XunhuPayCheckoutSession)
                .where(XunhuPayCheckoutSession.id == checkout_id)
                .with_for_update()
            )
            if checkout is None or checkout.status != "PENDING":
                raise XunhuPayConflict("The XunHuPay order changed while checkout was being created")
            checkout.gateway_order_id = gateway_order_id or None
            checkout.checkout_url = checkout_url
            checkout.qrcode_url = qrcode_url
            session.flush([checkout])

        return XunhuPayCheckoutResult(
            checkout_id=checkout_id,
            order_id=order_id,
            plan_id=package.sku,
            amount_cny=f"{package.amount:.2f}",
            credits=package.credits,
            currency=package.currency,
            provider=package.provider,
            purchase_kind=purchase_kind,
            checkout_url=checkout_url,
            qrcode_url=qrcode_url,
            expires_at=expires_at,
        )

    def handle_notification(self, raw_body: bytes) -> XunhuPayNotificationResult:
        payload = self._parse_form(raw_body)
        self._verify_hash(payload)
        if payload.get("appid") != self.app_id:
            raise XunhuPayAuthenticationError("XunHuPay notification appid does not match")
        if payload.get("status") != "OD":
            raise XunhuPayPayloadError("XunHuPay notification is not in paid status")

        trade_order_id = str(payload.get("trade_order_id") or "")
        transaction_id = str(payload.get("transaction_id") or "")
        open_order_id = str(payload.get("open_order_id") or "")
        if not _MERCHANT_ORDER.fullmatch(trade_order_id):
            raise XunhuPayPayloadError("Invalid XunHuPay merchant order ID")
        if not _PROVIDER_ID.fullmatch(transaction_id) or not _PROVIDER_ID.fullmatch(open_order_id):
            raise XunhuPayPayloadError("Invalid XunHuPay transaction identity")
        amount = self._money(payload.get("total_fee"))
        payload_hash = hashlib.sha256(raw_body).hexdigest()

        for attempt in range(2):
            try:
                with self.database.session() as session:
                    return self._apply_notification(
                        session,
                        payload,
                        trade_order_id=trade_order_id,
                        transaction_id=transaction_id,
                        open_order_id=open_order_id,
                        amount=amount,
                        payload_hash=payload_hash,
                    )
            except IntegrityError as exc:
                if attempt == 1:
                    raise XunhuPayConflict(
                        "Concurrent XunHuPay notifications could not be reconciled"
                    ) from exc
        raise AssertionError("unreachable")

    def _apply_notification(
        self,
        session: Session,
        payload: dict[str, str],
        *,
        trade_order_id: str,
        transaction_id: str,
        open_order_id: str,
        amount: Decimal,
        payload_hash: str,
    ) -> XunhuPayNotificationResult:
        checkout = session.scalar(
            select(XunhuPayCheckoutSession)
            .where(XunhuPayCheckoutSession.trade_order_id == trade_order_id)
            .with_for_update()
        )
        if checkout is None:
            raise XunhuPayPayloadError("XunHuPay merchant order ID not found")
        order = session.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.id == checkout.payment_order_id)
            .with_for_update()
        )
        if order is None or order.workspace_id != checkout.workspace_id:
            raise XunhuPayPayloadError("XunHuPay payment order is unavailable")

        existing = session.scalar(
            select(XunhuPaySettlement).where(
                (XunhuPaySettlement.transaction_id == transaction_id)
                | (XunhuPaySettlement.payment_order_id == order.id)
            )
        )
        if existing is not None:
            if (
                existing.payment_order_id != order.id
                or existing.transaction_id != transaction_id
                or existing.open_order_id != open_order_id
                or existing.amount != amount
                or existing.currency != "CNY"
            ):
                raise XunhuPayConflict(
                    "XunHuPay transaction is already bound to different payment evidence"
                )
            return XunhuPayNotificationResult(
                transaction_id=transaction_id,
                replayed=True,
                result="ALREADY_CREDITED" if existing.status == "CREDITED" else existing.status,
                credits_granted=existing.credits_granted,
                plan_tier=(existing.metadata_json or {}).get("plan_after"),
                pro_activated=bool((existing.metadata_json or {}).get("pro_activated")),
            )

        invalid_order = (
            order.provider != "XUNHUPAY"
            or order.currency != "CNY"
            or order.network != "XUNHUPAY"
            or order.amount != amount
            or order.raw_amount_microunits != self._microunits(amount)
            or (order.metadata_json or {}).get("xunhupay_app_id") != self.app_id
            or checkout.status not in {"PENDING", "RECONCILIATION_REQUIRED"}
            or order.status not in {"PENDING", "RECONCILIATION_REQUIRED"}
        )
        settlement = XunhuPaySettlement(
            id=new_id(),
            checkout_session_id=checkout.id,
            payment_order_id=order.id,
            transaction_id=transaction_id,
            open_order_id=open_order_id,
            amount=amount,
            currency="CNY",
            payload_hash=payload_hash,
            credits_granted=0,
            status="RECONCILIATION_REQUIRED" if invalid_order else "CREDITED",
            metadata_json={
                "source": "XUNHUPAY_SIGNED_NOTIFICATION",
                "status": payload.get("status"),
                "order_title": payload.get("order_title"),
                "notification_time": payload.get("time"),
                "plan_id": order.sku,
                "pricing_version": order.pricing_version,
            },
        )
        if invalid_order:
            checkout.status = "RECONCILIATION_REQUIRED"
            order.status = "RECONCILIATION_REQUIRED"
            session.add(settlement)
            session.flush([checkout, order, settlement])
            return XunhuPayNotificationResult(
                transaction_id=transaction_id,
                replayed=False,
                result="RECONCILIATION_REQUIRED",
                credits_granted=0,
            )

        workspace = session.scalar(
            select(Workspace).where(Workspace.id == checkout.workspace_id).with_for_update()
        )
        if (
            workspace is None
            or workspace.status != "ACTIVE"
            or workspace.plan_tier not in {"FREE", "PRO"}
        ):
            checkout.status = "RECONCILIATION_REQUIRED"
            order.status = "RECONCILIATION_REQUIRED"
            settlement.status = "RECONCILIATION_REQUIRED"
            session.add(settlement)
            session.flush([checkout, order, settlement])
            return XunhuPayNotificationResult(
                transaction_id=transaction_id,
                replayed=False,
                result="RECONCILIATION_REQUIRED",
                credits_granted=0,
            )

        credits = order.credits
        balance_before = workspace.credit_balance
        plan_before = workspace.plan_tier
        plan_after = "PRO" if plan_before == "FREE" else plan_before
        pro_activated = plan_before == "FREE"
        applied = session.execute(
            update(Workspace)
            .where(
                Workspace.id == workspace.id,
                Workspace.status == "ACTIVE",
                Workspace.plan_tier == plan_before,
            )
            .values(
                credit_balance=Workspace.credit_balance + credits,
                plan_tier=plan_after,
            )
        )
        if affected_rows(applied) != 1:
            raise XunhuPayConflict("Workspace changed while applying the XunHuPay purchase")
        session.expire(workspace)
        session.refresh(workspace, ["credit_balance", "plan_tier"])

        external_reference = f"xunhupay:{transaction_id}"
        settlement.credits_granted = credits
        settlement.metadata_json = {
            **dict(settlement.metadata_json or {}),
            "plan_before": plan_before,
            "plan_after": workspace.plan_tier,
            "pro_activated": pro_activated,
        }
        # This table is append-only. Insert its final form before the ledger
        # row that references it; never INSERT then UPDATE settlement evidence.
        session.add(settlement)
        session.flush([settlement])
        ledger = WorkspaceCreditLedgerEntry(
            workspace_id=workspace.id,
            payment_id=None,
            xunhupay_settlement_id=settlement.id,
            external_reference=external_reference,
            entry_type="CNY_PURCHASE",
            direction="CREDIT",
            credits=credits,
            balance_before=balance_before,
            balance_after=workspace.credit_balance,
            currency="CNY",
            raw_amount_microunits=self._microunits(amount),
            chain_id=None,
            metadata_json={
                "source": "XUNHUPAY_SIGNED_NOTIFICATION",
                "payment_order_id": order.id,
                "checkout_session_id": checkout.id,
                "transaction_id": transaction_id,
                "open_order_id": open_order_id,
                "plan_id": order.sku,
                "pricing_version": order.pricing_version,
                "provider": order.provider,
                "purchase_kind": (order.metadata_json or {}).get("purchase_kind"),
                "plan_before": plan_before,
                "plan_after": workspace.plan_tier,
                "pro_activated": pro_activated,
                "recurring": False,
            },
        )
        session.add(ledger)
        now = utcnow()
        checkout.status = "PAID"
        checkout.credits_granted = credits
        checkout.paid_at = now
        order.status = "PAID"
        order.paid_at = now
        session.flush([checkout, order, ledger])
        return XunhuPayNotificationResult(
            transaction_id=transaction_id,
            replayed=False,
            result="CREDITED",
            credits_granted=credits,
            plan_tier=workspace.plan_tier,
            pro_activated=pro_activated,
        )

    def _checkout_payload(
        self,
        package: PaymentPackage,
        trade_order_id: str,
        now: datetime,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "version": "1.1",
            "appid": self.app_id,
            "trade_order_id": trade_order_id,
            "total_fee": f"{package.amount:.2f}",
            "title": f"BestShiny {package.credits} Credits",
            "time": int(now.timestamp()),
            "notify_url": self.notify_url,
            "plugins": "BestShiny",
            "nonce_str": secrets.token_hex(16),
        }
        if self.return_url:
            if not self._is_https_url(self.return_url):
                raise XunhuPayConfigurationError("XUNHUPAY_RETURN_URL must use HTTPS")
            payload["return_url"] = self.return_url
        payload["hash"] = self.generate_hash(payload, self.app_secret)
        return payload

    def _mark_checkout(self, checkout_id: str, status: str) -> None:
        with self.database.session() as session:
            checkout = session.scalar(
                select(XunhuPayCheckoutSession)
                .where(XunhuPayCheckoutSession.id == checkout_id)
                .with_for_update()
            )
            if checkout is None or checkout.status == "PAID":
                return
            checkout.status = status
            order = session.get(PaymentOrder, checkout.payment_order_id)
            if order is not None and order.status != "PAID":
                order.status = status
            session.flush([item for item in (checkout, order) if item is not None])

    def _verify_hash(self, payload: Mapping[str, object]) -> None:
        if not self.app_secret:
            raise XunhuPayConfigurationError("XUNHUPAY_APP_SECRET is not configured")
        supplied = str(payload.get("hash") or "").strip().lower()
        expected = self.generate_hash(payload, self.app_secret)
        if not re.fullmatch(r"[0-9a-f]{32}", supplied) or not hmac.compare_digest(
            supplied, expected
        ):
            raise XunhuPayAuthenticationError("Invalid XunHuPay signature")

    @staticmethod
    def generate_hash(payload: Mapping[str, object], app_secret: str) -> str:
        fields = []
        for key in sorted(payload):
            value = payload[key]
            if key == "hash" or value is None or value == "":
                continue
            fields.append(f"{key}={value}")
        return hashlib.md5(("&".join(fields) + app_secret).encode("utf-8")).hexdigest()  # noqa: S324

    @classmethod
    def _parse_form(cls, raw_body: bytes) -> dict[str, str]:
        if len(raw_body) > cls.max_body_bytes:
            raise XunhuPayPayloadError("XunHuPay notification body is too large")
        try:
            pairs = parse_qsl(
                raw_body.decode("utf-8"),
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=50,
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise XunhuPayPayloadError("XunHuPay notification must be a valid form") from exc
        payload: dict[str, str] = {}
        for key, value in pairs:
            if key in payload:
                raise XunhuPayPayloadError("XunHuPay notification contains duplicate fields")
            payload[key] = value
        return payload

    @staticmethod
    def _money(value: object) -> Decimal:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise XunhuPayPayloadError("Invalid XunHuPay amount") from exc
        if (
            not amount.is_finite()
            or amount <= 0
            or amount * Decimal(100) != (amount * Decimal(100)).to_integral_value()
        ):
            raise XunhuPayPayloadError("XunHuPay amount must be exact to one cent")
        return amount.quantize(Decimal("0.01"))

    @staticmethod
    def _microunits(amount: Decimal) -> int:
        return int(amount * Decimal(1_000_000))

    @staticmethod
    def _is_https_url(value: str) -> bool:
        parsed = urlparse(value)
        return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username

    @classmethod
    def _optional_https_url(cls, value: object) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        if not cls._is_https_url(text):
            raise XunhuPayGatewayError("XunHuPay returned an unsafe payment URL")
        return text
