from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any

from platform_database import Database
from platform_shared import affected_rows
from production_domain.models import (
    AlchemyWebhookDelivery,
    OnchainPayment,
    OnchainPaymentIntent,
    Workspace,
    WorkspaceCreditLedgerEntry,
    WorkspaceWalletBinding,
    utcnow,
)
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

BASE_NETWORKS: dict[str, tuple[int, str]] = {
    "BASE_MAINNET": (8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"),
    "BASE_SEPOLIA": (84532, "0x036cbd53842c5426634e7929541ec2318f3dcf7e"),
}
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TRANSACTION_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")


class AlchemyWebhookError(RuntimeError):
    pass


class AlchemyWebhookConfigurationError(AlchemyWebhookError):
    pass


class AlchemyWebhookAuthenticationError(AlchemyWebhookError):
    pass


class AlchemyWebhookPayloadError(AlchemyWebhookError):
    pass


class AlchemyWebhookConflict(AlchemyWebhookError):
    pass


@dataclass(frozen=True)
class AlchemyWebhookResult:
    event_id: str
    replayed: bool
    result: str
    activity_count: int
    accepted_count: int
    credited_count: int
    ignored_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "event_id": self.event_id,
            "replayed": self.replayed,
            "result": self.result,
            "activity_count": self.activity_count,
            "accepted_count": self.accepted_count,
            "credited_count": self.credited_count,
            "ignored_count": self.ignored_count,
        }


@dataclass(frozen=True)
class _USDCTransfer:
    transaction_hash: str
    log_index: str
    block_number: str
    from_address: str
    to_address: str
    token_address: str
    raw_amount_microunits: int
    removed: bool


@dataclass(frozen=True)
class _TransferResult:
    status: str
    credited: bool = False


class AlchemyUSDCWebhookService:
    """Authenticate Alchemy Address Activity deliveries and post Base USDC purchases."""

    max_body_bytes = 1_048_576
    max_activities = 1_000

    def __init__(
        self,
        database: Database,
        *,
        signing_key: str,
        webhook_id: str,
        network: str,
        treasury_address: str,
        crediting_enabled: bool,
        usdc_microunits_per_credit: int,
    ) -> None:
        if network not in BASE_NETWORKS:
            raise ValueError(f"unsupported Alchemy Base network: {network}")
        if usdc_microunits_per_credit < 1:
            raise ValueError("USDC microunits per credit must be positive")
        self.database = database
        self.signing_key = signing_key
        self.webhook_id = webhook_id.strip()
        self.network = network
        self.chain_id, self.usdc_contract = BASE_NETWORKS[network]
        self.treasury_address = self._normalize_optional_address(treasury_address)
        if treasury_address.strip() and not self.treasury_address:
            raise ValueError("Alchemy treasury address must be a valid EVM address")
        if crediting_enabled and not self.treasury_address:
            raise ValueError("Alchemy crediting requires a treasury address")
        self.crediting_enabled = crediting_enabled
        self.usdc_microunits_per_credit = usdc_microunits_per_credit

    def handle(self, raw_body: bytes, signature: str | None) -> AlchemyWebhookResult:
        self._verify_signature(raw_body, signature)
        payload = self._parse_payload(raw_body)
        payload_hash = hashlib.sha256(raw_body).hexdigest()
        event_id = self._required_text(payload, "id", max_length=160)

        for attempt in range(2):
            try:
                with self.database.session() as session:
                    existing = session.scalar(
                        select(AlchemyWebhookDelivery).where(
                            AlchemyWebhookDelivery.provider_event_id == event_id
                        )
                    )
                    if existing is not None:
                        if existing.payload_hash != payload_hash:
                            raise AlchemyWebhookConflict(
                                "Alchemy event id was replayed with a different payload"
                            )
                        return self._delivery_result(existing, replayed=True)
                    return self._process_delivery(session, payload, payload_hash)
            except IntegrityError as exc:
                if attempt == 1:
                    raise AlchemyWebhookConflict(
                        "concurrent Alchemy delivery could not be reconciled"
                    ) from exc
        raise AssertionError("unreachable")

    def _verify_signature(self, raw_body: bytes, signature: str | None) -> None:
        if not self.signing_key:
            raise AlchemyWebhookConfigurationError("ALCHEMY_WEBHOOK_SIGNING_KEY is not configured")
        if len(raw_body) > self.max_body_bytes:
            raise AlchemyWebhookPayloadError("Alchemy webhook body is too large")
        supplied = (signature or "").strip().lower()
        if len(supplied) != 64 or any(character not in "0123456789abcdef" for character in supplied):
            raise AlchemyWebhookAuthenticationError("invalid Alchemy webhook signature")
        expected = hmac.new(self.signing_key.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise AlchemyWebhookAuthenticationError("invalid Alchemy webhook signature")

    def _parse_payload(self, raw_body: bytes) -> dict[str, Any]:
        try:
            value = json.loads(raw_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AlchemyWebhookPayloadError("Alchemy webhook body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise AlchemyWebhookPayloadError("Alchemy webhook body must be a JSON object")
        return value

    def _process_delivery(
        self,
        session: Session,
        payload: dict[str, Any],
        payload_hash: str,
    ) -> AlchemyWebhookResult:
        event_id = self._required_text(payload, "id", max_length=160)
        webhook_id = self._required_text(payload, "webhookId", max_length=160)
        webhook_type = self._required_text(payload, "type", max_length=80)
        if self.webhook_id and webhook_id != self.webhook_id:
            raise AlchemyWebhookAuthenticationError("Alchemy webhook id does not match configuration")
        event = payload.get("event")
        if not isinstance(event, dict):
            raise AlchemyWebhookPayloadError("Alchemy webhook event must be an object")
        is_test_event = (
            "network" not in event
            and "activity" not in event
            and "eventDetails" in event
        )
        network = (
            self.network
            if is_test_event
            else self._required_text(event, "network", max_length=80)
        )
        activities = [] if is_test_event else event.get("activity", [])
        if not isinstance(activities, list):
            raise AlchemyWebhookPayloadError("Alchemy webhook activity must be a list")
        if len(activities) > self.max_activities:
            raise AlchemyWebhookPayloadError("Alchemy webhook contains too many activities")

        accepted = 0
        credited = 0
        status_counts: dict[str, int] = {}
        if not is_test_event and webhook_type == "ADDRESS_ACTIVITY" and network == self.network:
            for activity in activities:
                transfer = self._extract_usdc_transfer(activity)
                if transfer is None:
                    continue
                accepted += 1
                transfer_result = self._apply_transfer(session, event_id, transfer)
                status_counts[transfer_result.status] = status_counts.get(transfer_result.status, 0) + 1
                if transfer_result.credited:
                    credited += 1

        ignored = len(activities) - accepted
        result = "TEST_EVENT" if is_test_event else "PROCESSED"
        if not is_test_event and webhook_type != "ADDRESS_ACTIVITY":
            result = "IGNORED_TYPE"
        elif not is_test_event and network != self.network:
            result = "IGNORED_NETWORK"
        delivery = AlchemyWebhookDelivery(
            provider_event_id=event_id,
            webhook_id=webhook_id,
            webhook_type=webhook_type,
            network=network,
            payload_hash=payload_hash,
            activity_count=len(activities),
            accepted_count=accepted,
            credited_count=credited,
            ignored_count=ignored,
            result=result,
            metadata_json={"payment_status_counts": status_counts},
        )
        session.add(delivery)
        session.flush([delivery])
        return self._delivery_result(delivery, replayed=False)

    def _extract_usdc_transfer(self, activity: object) -> _USDCTransfer | None:
        if not isinstance(activity, dict) or str(activity.get("category") or "").lower() not in {
            "erc20",
            "token",
        }:
            return None
        raw_contract = activity.get("rawContract")
        if not isinstance(raw_contract, dict):
            return None
        token_address = self._normalize_optional_address(str(raw_contract.get("address") or ""))
        to_address = self._normalize_optional_address(str(activity.get("toAddress") or ""))
        from_address = self._normalize_optional_address(str(activity.get("fromAddress") or ""))
        if (
            not token_address
            or token_address != self.usdc_contract
            or not self.treasury_address
            or to_address != self.treasury_address
            or not from_address
        ):
            return None
        decimals = raw_contract.get("decimals")
        try:
            decimals_value = self._parse_uint(decimals)
        except ValueError:
            return None
        if decimals_value != 6:
            return None
        transaction_hash = str(activity.get("hash") or "").lower()
        if not _TRANSACTION_HASH.fullmatch(transaction_hash):
            return None
        log = activity.get("log")
        if not isinstance(log, dict):
            return None
        log_address = self._normalize_optional_address(str(log.get("address") or ""))
        log_transaction_hash = str(log.get("transactionHash") or "").lower()
        if log_address != token_address or log_transaction_hash != transaction_hash:
            return None
        log_index = str(log.get("logIndex") or "").lower()
        if not self._valid_hex_quantity(log_index):
            return None
        block_number = str(activity.get("blockNum") or log.get("blockNumber") or "").lower()
        if not self._valid_hex_quantity(block_number):
            return None
        raw_value = raw_contract.get("rawValue")
        try:
            amount = self._parse_uint(raw_value)
        except ValueError:
            return None
        if amount < 1 or amount > 9_223_372_036_854_775_807:
            return None
        removed = log.get("removed", False)
        if not isinstance(removed, bool):
            return None
        return _USDCTransfer(
            transaction_hash=transaction_hash,
            log_index=log_index,
            block_number=block_number,
            from_address=from_address,
            to_address=to_address,
            token_address=token_address,
            raw_amount_microunits=amount,
            removed=removed,
        )

    def _apply_transfer(
        self,
        session: Session,
        provider_event_id: str,
        transfer: _USDCTransfer,
    ) -> _TransferResult:
        payment = session.scalar(
            select(OnchainPayment).where(
                OnchainPayment.network == self.network,
                OnchainPayment.transaction_hash == transfer.transaction_hash,
                OnchainPayment.log_index == transfer.log_index,
            )
        )
        if payment is None:
            depay_matches = list(
                session.scalars(
                    select(OnchainPayment).where(
                        OnchainPayment.network == self.network,
                        OnchainPayment.transaction_hash == transfer.transaction_hash,
                        OnchainPayment.to_address == transfer.to_address,
                        OnchainPayment.token_address == transfer.token_address,
                        OnchainPayment.raw_amount_microunits
                        == transfer.raw_amount_microunits,
                        OnchainPayment.provider_event_id.like("depay:%"),
                    )
                )
            )
            if len(depay_matches) == 1:
                payment = depay_matches[0]
                if payment.log_index == "depay":
                    payment.log_index = transfer.log_index
        if payment is not None:
            if (
                payment.from_address != transfer.from_address
                or payment.to_address != transfer.to_address
                or payment.token_address != transfer.token_address
                or payment.raw_amount_microunits != transfer.raw_amount_microunits
            ):
                raise AlchemyWebhookConflict("on-chain payment identity changed across deliveries")
            if transfer.removed:
                return self._remove_payment(session, payment, provider_event_id)
            if payment.status == "REMOVED":
                payment.status = "RECONCILIATION_REQUIRED"
                payment.provider_event_id = provider_event_id
                payment.metadata_json = {
                    **dict(payment.metadata_json or {}),
                    "reappeared_after_removal": True,
                }
            return _TransferResult(payment.status)

        binding = session.scalar(
            select(WorkspaceWalletBinding).where(
                WorkspaceWalletBinding.chain_id == self.chain_id,
                WorkspaceWalletBinding.address == transfer.from_address,
                WorkspaceWalletBinding.status == "VERIFIED",
            )
        )
        intent = self._match_payment_intent(session, binding, transfer)
        payment = OnchainPayment(
            network=self.network,
            chain_id=self.chain_id,
            transaction_hash=transfer.transaction_hash,
            log_index=transfer.log_index,
            block_number=transfer.block_number,
            from_address=transfer.from_address,
            to_address=transfer.to_address,
            token_address=transfer.token_address,
            token_decimals=6,
            raw_amount_microunits=transfer.raw_amount_microunits,
            workspace_id=(binding.workspace_id if binding else None),
            wallet_binding_id=(binding.id if binding else None),
            payment_intent_id=(intent.id if intent else None),
            provider_event_id=provider_event_id,
            credits_granted=0,
            status=(
                "REMOVED"
                if transfer.removed
                else "UNMATCHED"
                if binding is None
                else "RECEIVED"
                if intent is not None
                else "RECONCILIATION_REQUIRED"
            ),
            removed_at=(utcnow() if transfer.removed else None),
            metadata_json={
                "crediting_enabled": self.crediting_enabled,
                "intent_matched": intent is not None,
            },
        )
        session.add(payment)
        session.flush([payment])
        if intent is not None:
            if transfer.removed:
                intent.status = "RECONCILIATION_REQUIRED"
            else:
                intent.transaction_hash = transfer.transaction_hash
                intent.status = "PAID"
                intent.paid_at = utcnow()
        if transfer.removed or binding is None or intent is None or not self.crediting_enabled:
            return _TransferResult(payment.status)

        credits = intent.credits
        workspace = session.get(Workspace, binding.workspace_id)
        if workspace is None or workspace.status != "ACTIVE":
            payment.status = "RECONCILIATION_REQUIRED"
            return _TransferResult(payment.status)
        balance_before = workspace.credit_balance
        applied = session.execute(
            update(Workspace)
            .where(Workspace.id == workspace.id, Workspace.status == "ACTIVE")
            .values(credit_balance=Workspace.credit_balance + credits)
        )
        if affected_rows(applied) != 1:
            raise AlchemyWebhookConflict("workspace changed while posting USDC credits")
        session.expire(workspace)
        session.refresh(workspace, ["credit_balance"])
        ledger = WorkspaceCreditLedgerEntry(
            workspace_id=workspace.id,
            payment_id=payment.id,
            external_reference=self._ledger_reference(transfer),
            entry_type="USDC_PURCHASE",
            direction="CREDIT",
            credits=credits,
            balance_before=balance_before,
            balance_after=workspace.credit_balance,
            currency="USDC",
            raw_amount_microunits=transfer.raw_amount_microunits,
            chain_id=self.chain_id,
            metadata_json={
                "network": self.network,
                "transaction_hash": transfer.transaction_hash,
                "log_index": transfer.log_index,
                "microunits_per_credit": self.usdc_microunits_per_credit,
                "payment_intent_id": intent.id,
            },
        )
        session.add(ledger)
        session.flush([ledger])
        payment.status = "CREDITED"
        payment.credits_granted = credits
        return _TransferResult(payment.status, credited=True)

    def _match_payment_intent(
        self,
        session: Session,
        binding: WorkspaceWalletBinding | None,
        transfer: _USDCTransfer,
    ) -> OnchainPaymentIntent | None:
        if binding is None:
            return None
        submitted = session.scalar(
            select(OnchainPaymentIntent).where(
                OnchainPaymentIntent.transaction_hash == transfer.transaction_hash,
                OnchainPaymentIntent.status.in_(("PENDING", "SUBMITTED")),
            )
        )
        if submitted is not None:
            if self._intent_matches_transfer(submitted, binding, transfer):
                return submitted
            return None
        candidates = list(
            session.scalars(
                select(OnchainPaymentIntent)
                .where(
                    OnchainPaymentIntent.wallet_binding_id == binding.id,
                    OnchainPaymentIntent.network == self.network,
                    OnchainPaymentIntent.chain_id == self.chain_id,
                    OnchainPaymentIntent.from_address == transfer.from_address,
                    OnchainPaymentIntent.to_address == transfer.to_address,
                    OnchainPaymentIntent.token_address == transfer.token_address,
                    OnchainPaymentIntent.raw_amount_microunits
                    == transfer.raw_amount_microunits,
                    OnchainPaymentIntent.status == "PENDING",
                    OnchainPaymentIntent.expires_at > utcnow(),
                )
                .order_by(OnchainPaymentIntent.created_at.desc())
                .limit(2)
            )
        )
        return candidates[0] if len(candidates) == 1 else None

    def _intent_matches_transfer(
        self,
        intent: OnchainPaymentIntent,
        binding: WorkspaceWalletBinding,
        transfer: _USDCTransfer,
    ) -> bool:
        return (
            intent.workspace_id == binding.workspace_id
            and intent.wallet_binding_id == binding.id
            and intent.network == self.network
            and intent.chain_id == self.chain_id
            and intent.from_address == transfer.from_address
            and intent.to_address == transfer.to_address
            and intent.token_address == transfer.token_address
            and intent.raw_amount_microunits == transfer.raw_amount_microunits
        )

    def _remove_payment(
        self,
        session: Session,
        payment: OnchainPayment,
        provider_event_id: str,
    ) -> _TransferResult:
        if payment.status == "REMOVED":
            return _TransferResult(payment.status)
        payment.provider_event_id = provider_event_id
        payment.removed_at = utcnow()
        if payment.payment_intent_id:
            intent = session.get(OnchainPaymentIntent, payment.payment_intent_id)
            if intent is not None:
                intent.status = "RECONCILIATION_REQUIRED"
        purchase = session.scalar(
            select(WorkspaceCreditLedgerEntry).where(
                WorkspaceCreditLedgerEntry.payment_id == payment.id,
                WorkspaceCreditLedgerEntry.entry_type == "USDC_PURCHASE",
            )
        )
        if purchase is None:
            payment.status = "REMOVED"
            return _TransferResult(payment.status)
        reversal = session.scalar(
            select(WorkspaceCreditLedgerEntry).where(
                WorkspaceCreditLedgerEntry.external_reference == f"{purchase.external_reference}:reorg"
            )
        )
        if reversal is not None:
            payment.status = "REMOVED"
            return _TransferResult(payment.status)
        debited = session.execute(
            update(Workspace)
            .where(
                Workspace.id == purchase.workspace_id,
                Workspace.credit_balance >= purchase.credits,
            )
            .values(credit_balance=Workspace.credit_balance - purchase.credits)
        )
        if affected_rows(debited) != 1:
            payment.status = "RECONCILIATION_REQUIRED"
            payment.metadata_json = {
                **dict(payment.metadata_json or {}),
                "reorg_reversal_blocked": "INSUFFICIENT_AVAILABLE_CREDITS",
            }
            return _TransferResult(payment.status)
        workspace = session.get(Workspace, purchase.workspace_id)
        if workspace is None:
            raise AlchemyWebhookConflict("workspace disappeared during USDC reorg reversal")
        session.refresh(workspace, ["credit_balance"])
        reversal = WorkspaceCreditLedgerEntry(
            workspace_id=purchase.workspace_id,
            payment_id=payment.id,
            related_entry_id=purchase.id,
            external_reference=f"{purchase.external_reference}:reorg",
            entry_type="USDC_REORG_REVERSAL",
            direction="DEBIT",
            credits=purchase.credits,
            balance_before=workspace.credit_balance + purchase.credits,
            balance_after=workspace.credit_balance,
            currency="USDC",
            raw_amount_microunits=purchase.raw_amount_microunits,
            chain_id=purchase.chain_id,
            metadata_json={"provider_event_id": provider_event_id},
        )
        session.add(reversal)
        session.flush([reversal])
        payment.status = "REMOVED"
        return _TransferResult(payment.status)

    def _ledger_reference(self, transfer: _USDCTransfer) -> str:
        return f"alchemy:{self.network}:{transfer.transaction_hash}:{transfer.log_index}"

    @staticmethod
    def _parse_uint(value: object) -> int:
        if isinstance(value, bool):
            raise ValueError("boolean is not an amount")
        if isinstance(value, int):
            result = value
        elif isinstance(value, str):
            stripped = value.strip().lower()
            result = int(stripped, 16 if stripped.startswith("0x") else 10)
        else:
            raise ValueError("unsupported amount")
        if result < 0:
            raise ValueError("amount cannot be negative")
        return result

    @staticmethod
    def _valid_hex_quantity(value: str) -> bool:
        if not value.startswith("0x") or len(value) < 3:
            return False
        return all(character in "0123456789abcdef" for character in value[2:])

    @staticmethod
    def _normalize_optional_address(value: str) -> str:
        stripped = value.strip()
        if not stripped or not _EVM_ADDRESS.fullmatch(stripped):
            return ""
        return stripped.lower()

    @staticmethod
    def _required_text(value: dict[str, Any], key: str, *, max_length: int) -> str:
        item = value.get(key)
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > max_length:
            raise AlchemyWebhookPayloadError(f"Alchemy webhook field {key} is invalid")
        return item.strip()

    @staticmethod
    def _delivery_result(
        delivery: AlchemyWebhookDelivery,
        *,
        replayed: bool,
    ) -> AlchemyWebhookResult:
        return AlchemyWebhookResult(
            event_id=delivery.provider_event_id,
            replayed=replayed,
            result=delivery.result,
            activity_count=delivery.activity_count,
            accepted_count=delivery.accepted_count,
            credited_count=delivery.credited_count,
            ignored_count=delivery.ignored_count,
        )
