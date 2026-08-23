from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from platform_database import Database
from platform_shared import affected_rows
from production_domain.models import (
    DePayCheckoutSession,
    DePayWebhookDelivery,
    OnchainPayment,
    OnchainPaymentIntent,
    Workspace,
    WorkspaceCreditLedgerEntry,
    utcnow,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .alchemy import BASE_NETWORKS

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TRANSACTION_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")


class DePayError(RuntimeError):
    pass


class DePayConfigurationError(DePayError):
    pass


class DePayAuthenticationError(DePayError):
    pass


class DePayPayloadError(DePayError):
    pass


class DePayConflict(DePayError):
    pass


@dataclass(frozen=True)
class DePayCheckoutResult:
    checkout_id: str
    payment_intent_id: str
    checkout_url: str
    expected_usdc: str
    expected_credits: int
    purchase_kind: str
    expires_at: datetime


@dataclass(frozen=True)
class DePayWebhookResult:
    event_key: str
    replayed: bool
    result: str
    credits_granted: int
    plan_tier: str | None = None
    pro_activated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "event_key": self.event_key,
            "replayed": self.replayed,
            "result": self.result,
            "credits_granted": self.credits_granted,
            "plan_tier": self.plan_tier,
            "pro_activated": self.pro_activated,
        }


class DePayPaymentService:
    """Sell one server-owned offer through a shared fixed-amount DePay link."""

    max_body_bytes = 1_048_576

    def __init__(
        self,
        database: Database,
        *,
        payment_link_url: str,
        link_id: str,
        callback_public_key: str,
        treasury_address: str,
        offer_amount_usdc: Decimal,
        offer_credits: int,
        upgrade_plan_tier: str = "PRO",
        checkout_ttl_minutes: int = 1_440,
    ) -> None:
        self.database = database
        self.payment_link_url = payment_link_url.strip()
        self.link_id = link_id.strip()
        self.callback_public_key = callback_public_key.replace("\\n", "\n").strip()
        self.network = "BASE_MAINNET"
        self.chain_id, self.usdc_contract = BASE_NETWORKS[self.network]
        self.treasury_address = self._normalize_address(treasury_address)
        if treasury_address.strip() and not self.treasury_address:
            raise ValueError("DePay treasury address must be a valid EVM address")
        self.offer_amount_microunits = self._amount_to_microunits(offer_amount_usdc)
        self.offer_amount_usdc = Decimal(self.offer_amount_microunits) / Decimal(1_000_000)
        if offer_credits < 1:
            raise ValueError("DePay offer credits must be positive")
        if upgrade_plan_tier != "PRO":
            raise ValueError("DePay fixed offer currently supports only permanent PRO activation")
        self.offer_credits = offer_credits
        self.upgrade_plan_tier = upgrade_plan_tier
        self.checkout_ttl = timedelta(minutes=max(15, min(checkout_ttl_minutes, 10_080)))

    @property
    def checkout_configured(self) -> bool:
        parsed = urlparse(self.payment_link_url)
        return bool(
            parsed.scheme == "https"
            and parsed.netloc.endswith("depay.com")
            and self.link_id
            and parsed.path.rstrip("/").endswith(f"/{self.link_id}")
            and self.treasury_address
        )

    @property
    def callback_configured(self) -> bool:
        return self.checkout_configured and bool(self.callback_public_key)

    def offer_view(self) -> dict[str, object]:
        return {
            "id": "pro_credits_fixed",
            "amount_usdc": f"{self.offer_amount_usdc:.2f}",
            "credits": self.offer_credits,
            "upgrade_plan": self.upgrade_plan_tier,
            "recurring": False,
        }

    def create_checkout(
        self,
        *,
        workspace_id: str,
        user_id: str,
    ) -> DePayCheckoutResult:
        if not self.checkout_configured:
            raise DePayConfigurationError("DePay 收款链接尚未配置")
        if not self.callback_configured:
            raise DePayConfigurationError("DePay 签名回调尚未配置，支付入口已关闭")
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = utcnow()
        expires_at = now + self.checkout_ttl
        with self.database.session() as session:
            workspace = session.scalar(
                select(Workspace).where(Workspace.id == workspace_id).with_for_update()
            )
            if workspace is None or workspace.status != "ACTIVE":
                raise DePayPayloadError("工作空间不存在或不可用")
            if workspace.plan_tier not in {"FREE", self.upgrade_plan_tier}:
                raise DePayPayloadError(f"当前套餐 {workspace.plan_tier} 不支持该支付入口")
            purchase_kind = (
                "UPGRADE_PRO_AND_CREDITS"
                if workspace.plan_tier == "FREE"
                else "TOP_UP_CREDITS"
            )
            intent = OnchainPaymentIntent(
                workspace_id=workspace.id,
                wallet_binding_id=None,
                network=self.network,
                chain_id=self.chain_id,
                from_address=None,
                to_address=self.treasury_address,
                token_address=self.usdc_contract,
                raw_amount_microunits=self.offer_amount_microunits,
                credits=self.offer_credits,
                status="PENDING",
                expires_at=expires_at,
                metadata_json={
                    "provider": "DEPAY",
                    "purchase_kind": purchase_kind,
                    "plan_at_creation": workspace.plan_tier,
                    "upgrade_plan": self.upgrade_plan_tier,
                    "recurring": False,
                },
            )
            session.add(intent)
            session.flush([intent])
            checkout = DePayCheckoutSession(
                workspace_id=workspace_id,
                user_id=user_id,
                payment_intent_id=intent.id,
                token_hash=token_hash,
                requested_quantity=1,
                credits_granted=0,
                status="PENDING",
                expires_at=expires_at,
                metadata_json={
                    "link_id": self.link_id,
                    "network": self.network,
                    "fixed_amount_usdc": f"{self.offer_amount_usdc:.2f}",
                    "payment_intent_id": intent.id,
                },
            )
            session.add(checkout)
            session.flush([checkout])
            return DePayCheckoutResult(
                checkout_id=checkout.id,
                payment_intent_id=intent.id,
                checkout_url=self._checkout_url(token, intent.id),
                expected_usdc=f"{self.offer_amount_usdc:.2f}",
                expected_credits=self.offer_credits,
                purchase_kind=purchase_kind,
                expires_at=expires_at,
            )

    def handle_callback(
        self,
        raw_body: bytes,
        signature: str | None,
    ) -> DePayWebhookResult:
        self._verify_signature(raw_body, signature)
        payload = self._parse_payload(raw_body)
        transfer = self._validate_transfer(payload)
        event_key = f"depay:{self.network}:{transfer['transaction']}"
        payload_hash = hashlib.sha256(raw_body).hexdigest()
        for attempt in range(2):
            try:
                with self.database.session() as session:
                    existing = session.scalar(
                        select(DePayWebhookDelivery).where(DePayWebhookDelivery.event_key == event_key)
                    )
                    if existing is not None:
                        if existing.payload_hash != payload_hash:
                            raise DePayConflict(
                                "DePay transaction was replayed with a different callback body"
                            )
                        return self._delivery_result(existing, replayed=True)
                    return self._apply_callback(
                        session,
                        payload,
                        transfer,
                        event_key=event_key,
                        payload_hash=payload_hash,
                    )
            except IntegrityError as exc:
                if attempt == 1:
                    raise DePayConflict("concurrent DePay callback could not be reconciled") from exc
        raise AssertionError("unreachable")

    def _apply_callback(
        self,
        session: Session,
        payload: dict[str, Any],
        transfer: dict[str, Any],
        *,
        event_key: str,
        payload_hash: str,
    ) -> DePayWebhookResult:
        injected = self._injected_payload(payload)
        checkout_token = injected.get("checkout_token")
        order_ref = injected.get("order_ref")
        if not isinstance(checkout_token, str) or len(checkout_token) > 200:
            raise DePayPayloadError("DePay callback is missing checkout token")
        if not isinstance(order_ref, str) or len(order_ref) > 100:
            raise DePayPayloadError("DePay callback is missing order_ref")
        checkout = session.scalar(
            select(DePayCheckoutSession)
            .where(DePayCheckoutSession.token_hash == hashlib.sha256(checkout_token.encode()).hexdigest())
            .with_for_update()
        )
        if checkout is None:
            raise DePayPayloadError("DePay checkout token is unknown")
        if not checkout.payment_intent_id or checkout.payment_intent_id != order_ref:
            raise DePayPayloadError("DePay order_ref does not match checkout")
        intent = session.scalar(
            select(OnchainPaymentIntent)
            .where(OnchainPaymentIntent.id == checkout.payment_intent_id)
            .with_for_update()
        )
        if intent is None or intent.workspace_id != checkout.workspace_id:
            raise DePayPayloadError("DePay payment intent is unavailable")

        payment = self._find_or_create_payment(session, checkout, transfer, event_key)
        intent_transaction_conflict = intent.transaction_hash not in {None, transfer["transaction"]}
        if not intent_transaction_conflict:
            intent.from_address = transfer["sender"]
            intent.transaction_hash = transfer["transaction"]
        now = utcnow()
        result = "CREDITED"
        credits = 0
        plan_tier: str | None = None
        pro_activated = False
        existing_purchase = session.scalar(
            select(WorkspaceCreditLedgerEntry.id).where(
                WorkspaceCreditLedgerEntry.payment_id == payment.id,
                WorkspaceCreditLedgerEntry.entry_type == "USDC_PURCHASE",
            )
        )
        invalid_intent = (
            checkout.status != "PENDING"
            or intent.status != "PENDING"
            or self._utc(checkout.expires_at) <= now
            or self._utc(intent.expires_at) <= now
            or intent.raw_amount_microunits != transfer["raw_amount_microunits"]
            or intent.credits != self.offer_credits
            or intent_transaction_conflict
            or payment.status == "CREDITED"
            or existing_purchase is not None
        )
        if invalid_intent:
            checkout.status = "RECONCILIATION_REQUIRED"
            intent.status = "RECONCILIATION_REQUIRED"
            payment.status = "RECONCILIATION_REQUIRED"
            result = "RECONCILIATION_REQUIRED"
        else:
            workspace = session.scalar(
                select(Workspace).where(Workspace.id == checkout.workspace_id).with_for_update()
            )
            if (
                workspace is None
                or workspace.status != "ACTIVE"
                or workspace.plan_tier not in {"FREE", self.upgrade_plan_tier}
            ):
                checkout.status = "RECONCILIATION_REQUIRED"
                intent.status = "RECONCILIATION_REQUIRED"
                payment.status = "RECONCILIATION_REQUIRED"
                result = "RECONCILIATION_REQUIRED"
            else:
                credits = intent.credits
                balance_before = workspace.credit_balance
                plan_before = workspace.plan_tier
                plan_tier = self.upgrade_plan_tier if plan_before == "FREE" else plan_before
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
                        plan_tier=plan_tier,
                    )
                )
                if affected_rows(applied) != 1:
                    raise DePayConflict("workspace changed while applying DePay purchase")
                session.expire(workspace)
                session.refresh(workspace, ["credit_balance", "plan_tier"])
                ledger = WorkspaceCreditLedgerEntry(
                    workspace_id=workspace.id,
                    payment_id=payment.id,
                    external_reference=event_key,
                    entry_type="USDC_PURCHASE",
                    direction="CREDIT",
                    credits=credits,
                    balance_before=balance_before,
                    balance_after=workspace.credit_balance,
                    currency="USDC",
                    raw_amount_microunits=transfer["raw_amount_microunits"],
                    chain_id=self.chain_id,
                    metadata_json={
                        "source": "DEPAY_SIGNED_CALLBACK",
                        "link_id": self.link_id,
                        "checkout_session_id": checkout.id,
                        "payment_intent_id": intent.id,
                        "purchase_kind": intent.metadata_json.get("purchase_kind"),
                        "plan_before": plan_before,
                        "plan_after": workspace.plan_tier,
                        "pro_activated": pro_activated,
                        "recurring": False,
                    },
                )
                session.add(ledger)
                checkout.status = "PAID"
                checkout.credits_granted = credits
                checkout.payment_id = payment.id
                checkout.paid_at = now
                payment.status = "CREDITED"
                payment.credits_granted = credits
                payment.payment_intent_id = intent.id
                intent.status = "PAID"
                intent.paid_at = now

        delivery = DePayWebhookDelivery(
            event_key=event_key,
            payload_hash=payload_hash,
            link_id=self.link_id,
            checkout_session_id=checkout.id,
            payment_id=payment.id,
            result=result,
            metadata_json={
                "commitment": transfer["commitment"],
                "confirmations": transfer["confirmations"],
                "credits_granted": credits,
                "payment_intent_id": intent.id,
                "plan_tier": plan_tier,
                "pro_activated": pro_activated,
            },
        )
        session.add(delivery)
        session.flush([payment, intent, checkout, delivery])
        return DePayWebhookResult(
            event_key,
            False,
            result,
            credits,
            plan_tier=plan_tier,
            pro_activated=pro_activated,
        )

    def _find_or_create_payment(
        self,
        session: Session,
        checkout: DePayCheckoutSession,
        transfer: dict[str, Any],
        event_key: str,
    ) -> OnchainPayment:
        payment = session.scalar(
            select(OnchainPayment).where(
                OnchainPayment.network == self.network,
                OnchainPayment.transaction_hash == transfer["transaction"],
                OnchainPayment.to_address == self.treasury_address,
                OnchainPayment.token_address == self.usdc_contract,
            )
        )
        if payment is not None:
            if payment.raw_amount_microunits != transfer["raw_amount_microunits"]:
                raise DePayConflict("DePay payment amount conflicts with existing chain evidence")
            if payment.workspace_id not in {None, checkout.workspace_id}:
                raise DePayConflict("DePay payment is already assigned to another workspace")
            if payment.payment_intent_id not in {None, checkout.payment_intent_id}:
                raise DePayConflict("DePay payment is already assigned to another payment intent")
            payment.workspace_id = checkout.workspace_id
            payment.payment_intent_id = checkout.payment_intent_id
            payment.provider_event_id = event_key
            payment.metadata_json = {
                **dict(payment.metadata_json or {}),
                "depay_checkout_session_id": checkout.id,
                "depay_link_id": self.link_id,
            }
            return payment
        payment = OnchainPayment(
            network=self.network,
            chain_id=self.chain_id,
            transaction_hash=transfer["transaction"],
            log_index="depay",
            block_number=str(transfer.get("after_block") or "0"),
            from_address=transfer["sender"],
            to_address=self.treasury_address,
            token_address=self.usdc_contract,
            token_decimals=6,
            raw_amount_microunits=transfer["raw_amount_microunits"],
            workspace_id=checkout.workspace_id,
            wallet_binding_id=None,
            payment_intent_id=checkout.payment_intent_id,
            provider_event_id=event_key,
            credits_granted=0,
            status="RECEIVED",
            metadata_json={
                "source": "DEPAY_SIGNED_CALLBACK",
                "depay_checkout_session_id": checkout.id,
                "depay_link_id": self.link_id,
            },
        )
        session.add(payment)
        session.flush([payment])
        return payment

    def _validate_transfer(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("status") != "success":
            raise DePayPayloadError("DePay callback is not a successful payment")
        if str(payload.get("blockchain") or "").lower() != "base":
            raise DePayPayloadError("DePay callback is not on Base")
        transaction = str(payload.get("transaction") or "").lower()
        sender = self._normalize_address(str(payload.get("sender") or ""))
        receiver = self._normalize_address(str(payload.get("receiver") or ""))
        token = self._normalize_address(str(payload.get("token") or ""))
        if not _TRANSACTION_HASH.fullmatch(transaction) or not sender:
            raise DePayPayloadError("DePay transaction identity is invalid")
        if receiver != self.treasury_address or token != self.usdc_contract:
            raise DePayPayloadError("DePay receiver or token does not match configuration")
        if payload.get("decimals") != 6:
            raise DePayPayloadError("DePay token decimals do not match Native USDC")
        if payload.get("commitment") != "confirmed":
            raise DePayPayloadError("DePay payment is not confirmed")
        confirmations = payload.get("confirmations")
        if isinstance(confirmations, bool) or not isinstance(confirmations, int) or confirmations < 1:
            raise DePayPayloadError("DePay payment has no confirmation")
        raw_amount = self._amount_to_microunits(payload.get("amount"))
        link_id = self._payload_link_id(payload)
        if link_id != self.link_id:
            raise DePayPayloadError("DePay link id does not match configuration")
        return {
            "transaction": transaction,
            "sender": sender,
            "raw_amount_microunits": raw_amount,
            "commitment": "confirmed",
            "confirmations": confirmations,
            "after_block": payload.get("after_block"),
        }

    def _verify_signature(self, raw_body: bytes, signature: str | None) -> None:
        if not self.callback_public_key:
            raise DePayConfigurationError("DEPAY_CALLBACK_PUBLIC_KEY is not configured")
        if len(raw_body) > self.max_body_bytes:
            raise DePayPayloadError("DePay callback body is too large")
        supplied = (signature or "").strip()
        try:
            decoded = base64.urlsafe_b64decode(supplied + "=" * (-len(supplied) % 4))
            key = serialization.load_pem_public_key(self.callback_public_key.encode())
            if not isinstance(key, rsa.RSAPublicKey):
                raise TypeError("not an RSA public key")
            key.verify(
                decoded,
                raw_body,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=64),
                hashes.SHA256(),
            )
        except (ValueError, TypeError, InvalidSignature) as exc:
            raise DePayAuthenticationError("invalid DePay callback signature") from exc

    @staticmethod
    def _parse_payload(raw_body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DePayPayloadError("DePay callback body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise DePayPayloadError("DePay callback body must be a JSON object")
        return payload

    @staticmethod
    def _injected_payload(payload: dict[str, Any]) -> dict[str, Any]:
        payment_payload = payload.get("payload")
        if not isinstance(payment_payload, dict):
            return {}
        injected = payment_payload.get("injected")
        return injected if isinstance(injected, dict) else {}

    @staticmethod
    def _payload_link_id(payload: dict[str, Any]) -> str:
        payment_payload = payload.get("payload")
        return str(payment_payload.get("link_id") or "") if isinstance(payment_payload, dict) else ""

    def _checkout_url(self, token: str, order_ref: str) -> str:
        parsed = urlparse(self.payment_link_url)
        query = [
            item
            for item in parse_qsl(parsed.query, keep_blank_values=True)
            if item[0] not in {"quantity", "payload[order_ref]", "payload[checkout_token]"}
        ]
        query.extend(
            (
                ("payload[order_ref]", order_ref),
                ("payload[checkout_token]", token),
            )
        )
        return urlunparse(parsed._replace(query=urlencode(query)))

    @staticmethod
    def _amount_to_microunits(value: object) -> int:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise DePayPayloadError("DePay amount is invalid") from exc
        raw = amount * Decimal(1_000_000)
        if (
            not amount.is_finite()
            or amount <= 0
            or raw != raw.to_integral_value()
            or raw > 9_223_372_036_854_775_807
        ):
            raise DePayPayloadError("DePay amount is outside Native USDC precision")
        return int(raw)

    @staticmethod
    def _normalize_address(value: str) -> str:
        stripped = value.strip()
        return stripped.lower() if _EVM_ADDRESS.fullmatch(stripped) else ""

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _delivery_result(
        delivery: DePayWebhookDelivery,
        *,
        replayed: bool,
    ) -> DePayWebhookResult:
        return DePayWebhookResult(
            delivery.event_key,
            replayed,
            delivery.result,
            int((delivery.metadata_json or {}).get("credits_granted") or 0),
            plan_tier=(delivery.metadata_json or {}).get("plan_tier"),
            pro_activated=bool((delivery.metadata_json or {}).get("pro_activated")),
        )
