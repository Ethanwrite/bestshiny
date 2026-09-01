from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
from collections.abc import Mapping
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
    PaymentOrder,
    Workspace,
    WorkspaceCreditLedgerEntry,
    utcnow,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .alchemy import BASE_NETWORKS
from .catalog import PAYMENT_PACKAGES, PaymentPackage

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TRANSACTION_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
# DePay documents exactly two commitment levels, and `finalized` is the
# stronger of the two: it is sent for high-value payments and for integrations
# configured to wait past a single confirmation. Demanding the literal string
# "confirmed" therefore rejected the *safer* callback — the whole of the
# 2026-08-30 fulfillment failure, where 13 signed callbacks for a real
# Treasury-settled payment were answered 400.
_SETTLED_COMMITMENTS = frozenset({"confirmed", "finalized"})
# Where DePay may carry the integration/link identity. Managed integrations
# put it at the document root; the payment-link flow echoes back the payload
# object we injected.
_INTEGRATION_ID_PATHS = (
    ("integration_id",),
    ("link_id",),
    ("payload", "link_id"),
    ("payload", "integration_id"),
)
_PRICE_FIELDS = frozenset({"amount", "amount_usdc", "credits"})

logger = logging.getLogger(__name__)


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
    order_id: str
    integration_id: str
    checkout_token: str
    checkout_url: str
    sku: str
    expected_usdc: str
    expected_credits: int
    currency: str
    pricing_version: str
    provider: str
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
    """Create snapshotted orders and finalize them only from signed DePay callbacks."""

    max_body_bytes = 1_048_576

    def __init__(
        self,
        database: Database,
        *,
        payment_link_url: str = "",
        integration_id: str,
        legacy_link_id: str = "",
        callback_public_key: str,
        dynamic_config_private_key: str = "",
        treasury_address: str,
        packages: Mapping[str, PaymentPackage] | None = None,
        max_provider_fee_bps: int = 200,
        checkout_ttl_minutes: int = 1_440,
    ) -> None:
        self.database = database
        self.payment_link_url = payment_link_url.strip()
        self.integration_id = integration_id.strip()
        self.legacy_link_id = legacy_link_id.strip()
        self.callback_public_key = callback_public_key.replace("\\n", "\n").strip()
        self.dynamic_config_private_key = dynamic_config_private_key.replace("\\n", "\n").strip()
        self.network = "BASE_MAINNET"
        self.chain_id, self.usdc_contract = BASE_NETWORKS[self.network]
        self.treasury_address = self._normalize_address(treasury_address)
        if treasury_address.strip() and not self.treasury_address:
            raise ValueError("DePay treasury address must be a valid EVM address")
        self._packages = dict(packages if packages is not None else PAYMENT_PACKAGES)
        if any(sku != package.sku for sku, package in self._packages.items()):
            raise ValueError("DePay package registry keys must match package SKUs")
        if not 0 <= max_provider_fee_bps <= 1_000:
            raise ValueError("DePay provider fee allowance must be between 0 and 10 percent")
        self.max_provider_fee_bps = max_provider_fee_bps
        self.checkout_ttl = timedelta(minutes=max(15, min(checkout_ttl_minutes, 10_080)))

    @property
    def checkout_configured(self) -> bool:
        return bool(self.integration_id and self.treasury_address)

    @property
    def callback_configured(self) -> bool:
        return self.checkout_configured and bool(self.callback_public_key)

    @property
    def dynamic_configured(self) -> bool:
        return self.callback_configured and bool(self.dynamic_config_private_key)

    def package_views(self) -> list[dict[str, object]]:
        return [package.as_public_dict() for package in self._packages.values()]

    def create_checkout(
        self,
        *,
        workspace_id: str,
        user_id: str,
        sku: str,
    ) -> DePayCheckoutResult:
        package = self._packages.get(sku)
        if package is None:
            raise DePayPayloadError("未知或不可用的支付套餐")
        if not self.dynamic_configured:
            raise DePayConfigurationError("DePay 动态配置或签名密钥尚未配置，支付入口已关闭")
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
            if workspace.plan_tier not in {"FREE", "PRO"}:
                raise DePayPayloadError(f"当前套餐 {workspace.plan_tier} 不支持该支付入口")
            purchase_kind = (
                "UPGRADE_PRO_AND_CREDITS"
                if workspace.plan_tier == "FREE"
                else "TOP_UP_CREDITS"
            )
            order = PaymentOrder(
                workspace_id=workspace.id,
                wallet_binding_id=None,
                network=self.network,
                chain_id=self.chain_id,
                from_address=None,
                to_address=self.treasury_address,
                token_address=self.usdc_contract,
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
                },
            )
            session.add(order)
            session.flush([order])
            checkout = DePayCheckoutSession(
                workspace_id=workspace_id,
                user_id=user_id,
                payment_intent_id=order.id,
                token_hash=token_hash,
                requested_quantity=1,
                credits_granted=0,
                status="PENDING",
                expires_at=expires_at,
                metadata_json={
                    "integration_id": self.integration_id,
                    "network": self.network,
                    "order_id": order.id,
                    "sku": package.sku,
                },
            )
            session.add(checkout)
            session.flush([checkout])
            return DePayCheckoutResult(
                checkout_id=checkout.id,
                order_id=order.id,
                integration_id=self.integration_id,
                checkout_token=token,
                checkout_url=self._checkout_url(token, order.id),
                sku=package.sku,
                expected_usdc=f"{package.amount:.2f}",
                expected_credits=package.credits,
                currency=package.currency,
                pricing_version=package.pricing_version,
                provider=package.provider,
                purchase_kind=purchase_kind,
                expires_at=expires_at,
            )

    def dynamic_configuration(
        self,
        raw_body: bytes,
        signature: str | None,
    ) -> tuple[bytes, str]:
        """Return signed widget config from an existing immutable order snapshot.

        This endpoint reads. It never settles, never marks an order paid and
        never issues credits — those belong to the signed callback alone.
        """
        self._verify_signature(raw_body, signature)
        if not self.dynamic_config_private_key:
            raise DePayConfigurationError("DEPAY_DYNAMIC_CONFIG_PRIVATE_KEY is not configured")
        payload = self._parse_payload(raw_body)
        if _PRICE_FIELDS.intersection(payload):
            raise DePayPayloadError("DePay dynamic configuration does not accept price fields")
        # The checkout token is the whole binding. `order_ref` is optional and
        # only cross-checked when present, so the browser never has to be told
        # the order's identifier to start a payment.
        checkout_token = payload.get("checkout_token")
        order_ref = payload.get("order_ref")
        if not isinstance(checkout_token, str) or not checkout_token or len(checkout_token) > 200:
            raise DePayPayloadError("DePay dynamic configuration is missing checkout token")
        if order_ref is not None and (not isinstance(order_ref, str) or len(order_ref) > 100):
            raise DePayPayloadError("DePay dynamic configuration order_ref is invalid")
        with self.database.session() as session:
            checkout = session.scalar(
                select(DePayCheckoutSession).where(
                    DePayCheckoutSession.token_hash
                    == hashlib.sha256(checkout_token.encode()).hexdigest()
                )
            )
            if (
                checkout is None
                or not checkout.payment_intent_id
                or (order_ref is not None and checkout.payment_intent_id != order_ref)
                or checkout.status != "PENDING"
                or self._utc(checkout.expires_at) <= utcnow()
            ):
                raise DePayPayloadError("DePay payment order is unavailable")
            order = session.get(PaymentOrder, checkout.payment_intent_id)
            if order is None or order.workspace_id != checkout.workspace_id:
                raise DePayPayloadError("DePay payment order is unavailable")
            if (
                order.provider != "DEPAY"
                or order.currency != "USDC"
                or order.status != "PENDING"
                or order.network != self.network
                or order.chain_id != self.chain_id
                or order.token_address != self.usdc_contract
                or order.to_address != self.treasury_address
                or self._amount_to_microunits(order.amount) != order.raw_amount_microunits
            ):
                raise DePayPayloadError("DePay payment order snapshot is invalid")
            # DePay documents `amount` as a JSON number, so send one. This is
            # only what the widget *asks* the payer for: the amount we accept
            # is `raw_amount_microunits` on the order, and settlement compares
            # against that integer, so a rounded quote cannot buy credits.
            body = json.dumps(
                {
                    "accept": [
                        {
                            "blockchain": "base",
                            "amount": float(order.amount.normalize()),
                            "token": self.usdc_contract,
                            "receiver": self.treasury_address,
                        }
                    ],
                    "payload": {
                        "link_id": self.integration_id,
                        "injected": {
                            "order_ref": order.id,
                            "checkout_token": checkout_token,
                        },
                    },
                },
                separators=(",", ":"),
            ).encode()
        return body, self._sign_dynamic_configuration(body)

    def handle_callback(
        self,
        raw_body: bytes,
        signature: str | None,
    ) -> DePayWebhookResult:
        self._verify_signature(raw_body, signature)
        payload = self._parse_payload(raw_body)
        try:
            return self._settle_verified_callback(payload, raw_body)
        except DePayPayloadError as exc:
            # A refusal here is a signed callback we would not honour, which is
            # either our bug or a contract change. Say which fields drove it:
            # reconstructing that from an HTTP status and a response length
            # cost two days once already.
            logger.warning("DePay callback refused (%s) — %s", exc, self._fingerprint(payload))
            raise

    def _settle_verified_callback(
        self,
        payload: dict[str, Any],
        raw_body: bytes,
    ) -> DePayWebhookResult:
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
                        if existing.result != "RECONCILIATION_REQUIRED":
                            if existing.payload_hash != payload_hash:
                                raise DePayConflict(
                                    "DePay transaction was replayed with a different callback body"
                                )
                            return self._delivery_result(existing, replayed=True)
                        # Quarantine is a state to recover from, not a verdict.
                        # Memoizing it stranded a real payment for good: every
                        # later delivery replayed the failure instead of
                        # retrying it, and because a replay answers 200 DePay
                        # stopped retrying altogether.
                        #
                        # The receipt itself is immutable — the table is
                        # append-only in the database, not merely by
                        # convention — so re-run settlement and leave the
                        # original outcome standing as evidence of what
                        # happened first. The ledger entry is what records the
                        # recovery, and its unique external_reference plus the
                        # existing-purchase check are what keep it exactly-once.
                        # A re-sent body may legitimately differ from the one
                        # that was quarantined, so it is re-validated in full
                        # rather than compared against the stored hash.
                        return self._apply_callback(
                            session,
                            payload,
                            transfer,
                            event_key=event_key,
                            payload_hash=payload_hash,
                            record_receipt=False,
                        )
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

    def _fingerprint(self, payload: dict[str, Any]) -> str:
        """Non-secret fields of a callback, for a refusal log line.

        Deliberately omits the injected payload: it carries the checkout token,
        which is a bearer credential. Everything here is either public chain
        data or a field DePay states about its own delivery.
        """
        declared = {
            value
            for value in (self._payload_path(payload, path) for path in _INTEGRATION_ID_PATHS)
            if value
        }
        known = {value for value in (self.integration_id, self.legacy_link_id) if value}
        return (
            f"status={payload.get('status')!r} blockchain={payload.get('blockchain')!r} "
            f"commitment={payload.get('commitment')!r} confirmations={payload.get('confirmations')!r} "
            f"decimals={payload.get('decimals')!r} amount={payload.get('amount')!r} "
            f"transaction={payload.get('transaction')!r} "
            f"integration_declared={sorted(declared)} integration_matches={bool(declared & known)} "
            f"top_level_keys={sorted(payload)}"
        )

    def _apply_callback(
        self,
        session: Session,
        payload: dict[str, Any],
        transfer: dict[str, Any],
        *,
        event_key: str,
        payload_hash: str,
        record_receipt: bool = True,
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
        order = session.scalar(
            select(PaymentOrder)
            .where(PaymentOrder.id == checkout.payment_intent_id)
            .with_for_update()
        )
        if order is None or order.workspace_id != checkout.workspace_id:
            raise DePayPayloadError("DePay payment order is unavailable")

        payment = self._find_or_create_payment(session, checkout, transfer, event_key)
        order_transaction_conflict = order.transaction_hash not in {None, transfer["transaction"]}
        if not order_transaction_conflict:
            order.from_address = transfer["sender"]
            order.transaction_hash = transfer["transaction"]
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
        # This transfer already bought its credits — reconciled by Alchemy
        # first, or handed to us twice under two event keys. `_find_or_create_
        # payment` has already refused a payment belonging to another workspace
        # or order, so this is the same business fulfillment and must stay one:
        # converge the projection the browser polls, post nothing.
        already_credited = payment.status == "CREDITED" or existing_purchase is not None
        # The commercial terms come from the frozen snapshot, never from the
        # live catalogue and never from the amount DePay reports. A lapsed
        # checkout window is deliberately *not* grounds to refuse: the money is
        # already in Treasury, the terms cannot drift, and the alternative is
        # stranding a settled payment behind manual repair.
        invalid_order = (
            order.provider != "DEPAY"
            or order.currency != "USDC"
            or order.network != self.network
            or order.chain_id != self.chain_id
            or order.token_address != self.usdc_contract
            or order.to_address != self.treasury_address
            or not self._covers_order(order.raw_amount_microunits, transfer["raw_amount_microunits"])
            or self._amount_to_microunits(order.amount) != order.raw_amount_microunits
            or order_transaction_conflict
            or checkout.status not in {"PENDING", "PAID", "RECONCILIATION_REQUIRED"}
            or order.status not in {"PENDING", "PAID", "RECONCILIATION_REQUIRED"}
        )
        if already_credited and not invalid_order:
            credits = payment.credits_granted
            result = "ALREADY_CREDITED"
            checkout.status = "PAID"
            checkout.credits_granted = credits
            checkout.payment_id = payment.id
            checkout.paid_at = checkout.paid_at or now
            order.status = "PAID"
            order.paid_at = order.paid_at or now
        elif invalid_order:
            checkout.status = "RECONCILIATION_REQUIRED"
            order.status = "RECONCILIATION_REQUIRED"
            payment.status = "RECONCILIATION_REQUIRED"
            result = "RECONCILIATION_REQUIRED"
        else:
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
                payment.status = "RECONCILIATION_REQUIRED"
                result = "RECONCILIATION_REQUIRED"
            else:
                credits = order.credits
                balance_before = workspace.credit_balance
                plan_before = workspace.plan_tier
                plan_tier = "PRO" if plan_before == "FREE" else plan_before
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
                    currency=order.currency,
                    raw_amount_microunits=transfer["raw_amount_microunits"],
                    chain_id=self.chain_id,
                    metadata_json={
                        "source": "DEPAY_SIGNED_CALLBACK",
                        "integration_id": self.integration_id,
                        "checkout_session_id": checkout.id,
                        "payment_order_id": order.id,
                        "ordered_microunits": order.raw_amount_microunits,
                        "received_microunits": transfer["raw_amount_microunits"],
                        "provider_fee_microunits": (
                            order.raw_amount_microunits - transfer["raw_amount_microunits"]
                        ),
                        "sku": order.sku,
                        "pricing_version": order.pricing_version,
                        "provider": order.provider,
                        "purchase_kind": order.metadata_json.get("purchase_kind"),
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
                payment.payment_intent_id = order.id
                order.status = "PAID"
                order.paid_at = now

        receipt = {
            "commitment": transfer["commitment"],
            "confirmations": transfer["confirmations"],
            "credits_granted": credits,
            "payment_order_id": order.id,
            "ordered_microunits": order.raw_amount_microunits,
            "received_microunits": transfer["raw_amount_microunits"],
            "sku": order.sku,
            "pricing_version": order.pricing_version,
            "plan_tier": plan_tier,
            "pro_activated": pro_activated,
        }
        written = [payment, order, checkout]
        if record_receipt:
            delivery = DePayWebhookDelivery(
                event_key=event_key,
                payload_hash=payload_hash,
                link_id=self.integration_id,
                checkout_session_id=checkout.id,
                payment_id=payment.id,
                result=result,
                metadata_json=receipt,
            )
            session.add(delivery)
            written.append(delivery)
        session.flush(written)
        return DePayWebhookResult(
            event_key,
            False,
            result,
            credits,
            plan_tier=plan_tier,
            pro_activated=pro_activated,
        )

    def _covers_order(self, ordered: int, received: int) -> bool:
        """Does what reached Treasury settle an order priced at `ordered`?

        DePay deducts its fee before forwarding, so Treasury nets less than the
        buyer was charged — 29.55 against a 30.00 order, exactly 1.5%, on the
        2026-08-30 payment. What the customer agreed to pay is the commercial
        fact; the provider's cut is cost of sale. So accept a shortfall up to
        the configured fee allowance and no further: a materially short payment
        is still a mismatch, and an overpayment settles the order it names.
        """
        allowance = (ordered * self.max_provider_fee_bps + 9_999) // 10_000
        return received >= ordered - allowance

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
                "depay_integration_id": self.integration_id,
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
                "depay_integration_id": self.integration_id,
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
        confirmations = payload.get("confirmations")
        if isinstance(confirmations, bool) or not isinstance(confirmations, int) or confirmations < 1:
            raise DePayPayloadError("DePay payment has no confirmation")
        # `commitment` is corroboration, not the proof. The proof is the
        # signature, `status == success`, at least one block confirmation, and
        # a transfer of the exact snapshot amount to Treasury in canonical
        # USDC. DePay does not put this field in every callback shape, and
        # demanding it kept a genuinely settled payment out of the ledger for
        # two days. So: honour a level it *does* state, and fall back to the
        # confirmation count when it states none. A level we do not recognise
        # is still refused — silence is not the same as a contradiction.
        commitment = str(payload.get("commitment") or "").lower()
        if commitment and commitment not in _SETTLED_COMMITMENTS:
            raise DePayPayloadError(f"DePay commitment level is not settled: {commitment}")
        raw_amount = self._amount_to_microunits(payload.get("amount"))
        self._require_integration(payload)
        return {
            "transaction": transaction,
            "sender": sender,
            "raw_amount_microunits": raw_amount,
            "commitment": commitment or f"unstated:{confirmations}conf",
            "confirmations": confirmations,
            "after_block": payload.get("after_block"),
        }

    def _require_integration(self, payload: dict[str, Any]) -> None:
        """Reject a callback that names a *different* integration than ours.

        The RSA-PSS signature already proves DePay sent the body, and the order
        binding proves which purchase it settles; this only stops one of our
        own other integrations from being mistaken for this one. A body that
        carries no integration identity at all is therefore accepted on the
        signature — refusing it would strand real money over a field DePay
        does not promise on every flow.
        """
        declared = {
            value
            for value in (self._payload_path(payload, path) for path in _INTEGRATION_ID_PATHS)
            if value
        }
        known = {value for value in (self.integration_id, self.legacy_link_id) if value}
        if declared and not declared.intersection(known):
            raise DePayPayloadError("DePay integration id does not match configuration")

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

    def _sign_dynamic_configuration(self, response_body: bytes) -> str:
        try:
            key = serialization.load_pem_private_key(
                self.dynamic_config_private_key.encode(),
                password=None,
            )
            if not isinstance(key, rsa.RSAPrivateKey):
                raise TypeError("not an RSA private key")
            signature = key.sign(
                response_body,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=64),
                hashes.SHA256(),
            )
        except (ValueError, TypeError) as exc:
            raise DePayConfigurationError("DEPAY_DYNAMIC_CONFIG_PRIVATE_KEY is invalid") from exc
        # Padding kept: DePay's own example encodes the signature without
        # stripping it, and a strict decoder accepts canonical base64 where it
        # may refuse the trimmed form. Incoming signatures are re-padded before
        # verification, so we are lenient inbound and canonical outbound.
        return base64.urlsafe_b64encode(signature).decode()

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
    def _payload_path(payload: dict[str, Any], path: tuple[str, ...]) -> str:
        cursor: Any = payload
        for key in path:
            if not isinstance(cursor, dict):
                return ""
            cursor = cursor.get(key)
        return str(cursor) if isinstance(cursor, str) else ""

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
