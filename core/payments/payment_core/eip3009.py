from __future__ import annotations

import hashlib
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
from eth_abi import encode
from eth_account import Account
from eth_account.messages import SignableMessage, encode_typed_data
from eth_utils import keccak, to_checksum_address
from platform_database import Database
from platform_shared import affected_rows
from production_domain.models import (
    EIP3009Authorization,
    OnchainPayment,
    PaymentOrder,
    RelayerAccountState,
    Workspace,
    WorkspaceCreditLedgerEntry,
    utcnow,
)
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from .alchemy import BASE_NETWORKS
from .catalog import PAYMENT_PACKAGES, PaymentPackage

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SIGNATURE = re.compile(r"^0x[0-9a-fA-F]{130}$")
_TRANSACTION_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_NONCE = re.compile(r"^0x[0-9a-fA-F]{64}$")

_TRANSFER_AUTHORIZATION_FUNCTION = (
    "transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)"
)
_AUTHORIZATION_STATE_FUNCTION = "authorizationState(address,bytes32)"
_TRANSFER_EVENT = "Transfer(address,address,uint256)"
_AUTHORIZATION_USED_EVENT = "AuthorizationUsed(address,bytes32)"


class EIP3009Error(RuntimeError):
    pass


class EIP3009ConfigurationError(EIP3009Error):
    pass


class EIP3009Rejected(EIP3009Error):
    pass


class EIP3009NotFound(EIP3009Error):
    pass


class EIP3009Conflict(EIP3009Error):
    pass


class EIP3009RPCError(EIP3009Error):
    def __init__(self, code: str, detail: str = "Base RPC request failed") -> None:
        super().__init__(detail)
        self.code = code


@dataclass(frozen=True)
class EIP3009CheckoutResult:
    authorization_id: str
    payment_intent_id: str
    sku: str
    amount_usdc: str
    credits: int
    purchase_kind: str
    typed_data: dict[str, Any]
    expires_at: datetime


@dataclass(frozen=True)
class EIP3009AuthorizationResult:
    authorization_id: str
    status: str
    transaction_hash: str | None
    credits_granted: int
    plan_tier: str | None = None
    pro_activated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.authorization_id,
            "status": self.status,
            "transaction_hash": self.transaction_hash,
            "credits_granted": self.credits_granted,
            "plan_tier": self.plan_tier,
            "pro_activated": self.pro_activated,
        }


@dataclass(frozen=True)
class EIP3009SweepResult:
    expired: int = 0
    confirmed: int = 0
    pending: int = 0
    failed: int = 0


class EIP3009RelayerService:
    """Relay user-signed Base USDC transfers and fulfill their snapshotted orders."""

    network = "BASE_MAINNET"
    token_name = "USD Coin"
    token_version = "2"
    token_decimals = 6

    def __init__(
        self,
        database: Database,
        *,
        relayer_address: str,
        relayer_private_key: str,
        rpc_url: str,
        treasury_address: str,
        authorization_ttl_seconds: int = 900,
        min_confirmations: int = 1,
        rpc_timeout_seconds: int = 15,
        max_gas_limit: int = 200_000,
        max_fee_per_gas_wei: int = 5_000_000_000,
        packages: dict[str, PaymentPackage] | None = None,
    ) -> None:
        self.database = database
        self.chain_id, self.usdc_contract = BASE_NETWORKS[self.network]
        self.relayer_address = self._normalize_address(relayer_address)
        self.treasury_address = self._normalize_address(treasury_address)
        self.relayer_private_key = relayer_private_key.strip()
        self.rpc_url = rpc_url.strip()
        self.authorization_ttl = timedelta(seconds=max(120, min(int(authorization_ttl_seconds), 3_600)))
        self.min_confirmations = max(1, min(int(min_confirmations), 100))
        self.max_gas_limit = max(80_000, min(int(max_gas_limit), 1_000_000))
        self.max_fee_per_gas_wei = max(1_000_000, int(max_fee_per_gas_wei))
        self._packages = dict(packages if packages is not None else PAYMENT_PACKAGES)
        self._submission_lock = threading.Lock()
        self._client = httpx.Client(timeout=max(2, min(int(rpc_timeout_seconds), 60)))
        self._account = None

        relayer_parts = (
            bool(relayer_address.strip()),
            bool(self.relayer_private_key),
            bool(self.rpc_url),
        )
        if any(relayer_parts) and not all(relayer_parts):
            raise ValueError(
                "RELAYER_ADDRESS, RELAYER_PRIVATE_KEY and BASE_RPC_URL must be configured together"
            )
        if relayer_address.strip() and not self.relayer_address:
            raise ValueError("RELAYER_ADDRESS must be a valid EVM address")
        if treasury_address.strip() and not self.treasury_address:
            raise ValueError("EIP-3009 treasury address must be a valid EVM address")
        if self.relayer_private_key:
            try:
                account = Account.from_key(self.relayer_private_key)
            except (ValueError, TypeError) as exc:
                raise ValueError("RELAYER_PRIVATE_KEY must be a 32-byte EVM private key") from exc
            if self.relayer_address and account.address.lower() != self.relayer_address:
                raise ValueError("RELAYER_PRIVATE_KEY does not match RELAYER_ADDRESS")
            self._account = account
        if self.rpc_url:
            parsed = urlparse(self.rpc_url)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("BASE_RPC_URL must be an HTTPS URL")
        if any(sku != package.sku for sku, package in self._packages.items()):
            raise ValueError("EIP-3009 package registry keys must match package SKUs")

    @property
    def configured(self) -> bool:
        return bool(
            self.relayer_address
            and self.relayer_private_key
            and self._account is not None
            and self.rpc_url
            and self.treasury_address
        )

    def package_views(self) -> list[dict[str, object]]:
        return [package.as_public_dict() for package in self._packages.values()]

    def prepare_checkout(
        self,
        *,
        workspace_id: str,
        user_id: str,
        sku: str,
        from_address: str,
    ) -> EIP3009CheckoutResult:
        self._require_configured()
        payer = self._normalize_address(from_address)
        if not payer:
            raise EIP3009Rejected("付款钱包地址无效")
        package = self._packages.get(sku)
        if package is None:
            raise EIP3009Rejected("未知或不可用的支付套餐")
        now = utcnow()
        valid_after = max(0, int(now.timestamp()) - 30)
        valid_before = int((now + self.authorization_ttl).timestamp())
        nonce = "0x" + secrets.token_hex(32)
        expires_at = datetime.fromtimestamp(valid_before, tz=UTC)

        with self.database.session() as session:
            workspace = session.scalar(
                select(Workspace).where(Workspace.id == workspace_id).with_for_update()
            )
            if workspace is None or workspace.status != "ACTIVE":
                raise EIP3009Rejected("工作空间不存在或不可用")
            if workspace.plan_tier not in {"FREE", "PRO"}:
                raise EIP3009Rejected(f"当前套餐 {workspace.plan_tier} 不支持该支付入口")
            purchase_kind = "UPGRADE_PRO_AND_CREDITS" if workspace.plan_tier == "FREE" else "TOP_UP_CREDITS"
            order = PaymentOrder(
                workspace_id=workspace.id,
                wallet_binding_id=None,
                network=self.network,
                chain_id=self.chain_id,
                from_address=payer,
                to_address=self.treasury_address,
                token_address=self.usdc_contract,
                sku=package.sku,
                amount=package.amount,
                currency=package.currency,
                raw_amount_microunits=package.raw_amount_microunits,
                credits=package.credits,
                pricing_version=package.pricing_version,
                provider="EIP3009_RELAYER",
                status="PENDING",
                expires_at=expires_at,
                metadata_json={
                    "purchase_kind": purchase_kind,
                    "plan_at_creation": workspace.plan_tier,
                    "recurring": False,
                    "gas_sponsored": True,
                },
            )
            session.add(order)
            session.flush([order])
            typed_data = self._typed_data(
                payer=payer,
                value=order.raw_amount_microunits,
                valid_after=valid_after,
                valid_before=valid_before,
                nonce=nonce,
            )
            authorization = EIP3009Authorization(
                workspace_id=workspace.id,
                user_id=user_id,
                payment_intent_id=order.id,
                chain_id=self.chain_id,
                token_address=self.usdc_contract,
                from_address=payer,
                to_address=self.treasury_address,
                value_microunits=order.raw_amount_microunits,
                valid_after=valid_after,
                valid_before=valid_before,
                nonce=nonce,
                typed_data_hash=self._message_hash(self._signable_message(typed_data)).hex(),
                relayer_address=self.relayer_address,
                status="PENDING",
                attempt_count=0,
                metadata_json={
                    "sku": order.sku,
                    "pricing_version": order.pricing_version,
                },
            )
            session.add(authorization)
            session.flush([authorization])
            return EIP3009CheckoutResult(
                authorization_id=authorization.id,
                payment_intent_id=order.id,
                sku=order.sku,
                amount_usdc=f"{order.amount:.2f}",
                credits=order.credits,
                purchase_kind=purchase_kind,
                typed_data=typed_data,
                expires_at=expires_at,
            )

    def submit_authorization(
        self,
        *,
        workspace_id: str,
        user_id: str,
        authorization_id: str,
        signature: str,
    ) -> EIP3009AuthorizationResult:
        self._require_configured()
        if not _SIGNATURE.fullmatch(signature):
            raise EIP3009Rejected("EIP-712 签名格式无效")
        should_broadcast = False
        with self._submission_lock, self.database.session() as session:
            self._lock_relayer(session)
            authorization, order = self._load_owned(
                session, workspace_id, user_id, authorization_id, lock=True
            )
            if authorization.status in {"SUBMITTED", "CONFIRMED"}:
                return self._result(authorization, order)
            if authorization.status == "SUBMITTING":
                should_broadcast = True
            else:
                now_epoch = int(utcnow().timestamp())
                if authorization.valid_before <= now_epoch:
                    authorization.status = "EXPIRED"
                    order.status = "EXPIRED"
                    return self._result(authorization, order)
                if authorization.status != "PENDING" or order.status != "PENDING":
                    raise EIP3009Conflict("支付授权当前不可提交")
                typed_data = self._typed_data_from_row(authorization)
                signable = self._signable_message(typed_data)
                digest = self._message_hash(signable).hex()
                if digest != authorization.typed_data_hash:
                    raise EIP3009Conflict("支付授权快照已改变")
                try:
                    recovered = Account.recover_message(signable, signature=signature).lower()
                except (ValueError, TypeError) as exc:
                    raise EIP3009Rejected("EIP-712 签名无法恢复付款钱包") from exc
                if recovered != authorization.from_address:
                    raise EIP3009Rejected("EIP-712 签名钱包与订单不匹配")
                authorization.signature_hash = hashlib.sha256(bytes.fromhex(signature[2:])).hexdigest()
                authorization.attempt_count += 1
                self._require_chain()
                if self._authorization_used(authorization):
                    raise EIP3009Conflict("该 USDC authorization nonce 已被使用")
                raw_transaction, tx_hash, relayer_nonce = self._prepare_transaction(authorization, signature)
                # Persist the exact signed transaction before touching the
                # network. If the process dies after broadcasting, a retry can
                # derive the same hash, locate it, or safely re-broadcast the
                # same bytes without spending a second nonce.
                authorization.raw_transaction = raw_transaction
                authorization.transaction_hash = tx_hash
                authorization.relayer_nonce = relayer_nonce
                authorization.status = "SUBMITTING"
                authorization.submitted_at = utcnow()
                authorization.last_error_code = None
                order.transaction_hash = tx_hash
                order.status = "SUBMITTED"
                order.submitted_at = authorization.submitted_at
                state = session.get(RelayerAccountState, self.relayer_address)
                if state is not None:
                    state.last_submitted_nonce = relayer_nonce
                should_broadcast = True
        if not should_broadcast:
            raise AssertionError("relayer submission produced no work")
        return self._ensure_broadcast(
            workspace_id=workspace_id,
            user_id=user_id,
            authorization_id=authorization_id,
        )

    def reconcile(
        self,
        *,
        workspace_id: str,
        user_id: str,
        authorization_id: str,
    ) -> EIP3009AuthorizationResult:
        self._require_configured()
        with self.database.session() as session:
            authorization, order = self._load_owned(
                session, workspace_id, user_id, authorization_id, lock=False
            )
            if authorization.status == "SUBMITTING":
                pass
            elif authorization.status != "SUBMITTED" or not authorization.transaction_hash:
                return self._result(authorization, order)
            tx_hash = authorization.transaction_hash

        if authorization.status == "SUBMITTING":
            broadcast = self._ensure_broadcast(
                workspace_id=workspace_id,
                user_id=user_id,
                authorization_id=authorization_id,
            )
            if broadcast.status != "SUBMITTED":
                return broadcast

        receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            return EIP3009AuthorizationResult(authorization_id, "SUBMITTED", tx_hash, 0)
        if not isinstance(receipt, dict) or str(receipt.get("transactionHash") or "").lower() != tx_hash:
            raise EIP3009RPCError("INVALID_RECEIPT", "Base RPC returned an invalid receipt")
        if self._parse_quantity(receipt.get("status")) != 1:
            with self.database.session() as session:
                authorization, order = self._load_owned(
                    session, workspace_id, user_id, authorization_id, lock=True
                )
                if authorization.status == "SUBMITTED":
                    authorization.status = "FAILED"
                    authorization.last_error_code = "TRANSACTION_REVERTED"
                    order.status = "CANCELLED"
                return self._result(authorization, order)
        block_number = self._parse_quantity(receipt.get("blockNumber"))
        latest = self._parse_quantity(self._rpc("eth_blockNumber", []))
        if latest - block_number + 1 < self.min_confirmations:
            return EIP3009AuthorizationResult(authorization_id, "SUBMITTED", tx_hash, 0)
        try:
            transfer = self._validated_transfer(receipt, authorization)
        except EIP3009Conflict:
            with self.database.session() as session:
                authorization, order = self._load_owned(
                    session, workspace_id, user_id, authorization_id, lock=True
                )
                if authorization.status == "SUBMITTED":
                    authorization.status = "RECONCILIATION_REQUIRED"
                    authorization.last_error_code = "RECEIPT_EVIDENCE_MISMATCH"
                    order.status = "RECONCILIATION_REQUIRED"
                return self._result(authorization, order)
        return self._settle(
            workspace_id=workspace_id,
            user_id=user_id,
            authorization_id=authorization_id,
            receipt=receipt,
            transfer=transfer,
        )

    def get_authorization(
        self,
        *,
        workspace_id: str,
        user_id: str,
        authorization_id: str,
    ) -> EIP3009AuthorizationResult:
        with self.database.session() as session:
            authorization, order = self._load_owned(
                session, workspace_id, user_id, authorization_id, lock=False
            )
            return self._result(authorization, order)

    def sweep(self, *, limit: int = 50) -> EIP3009SweepResult:
        """Expire unsigned orders and reconcile submitted transactions without a browser."""

        bounded = max(1, min(int(limit), 500))
        now_epoch = int(utcnow().timestamp())
        expired = 0
        with self.database.session() as session:
            expired_ids = list(
                session.scalars(
                    select(EIP3009Authorization.id)
                    .where(
                        EIP3009Authorization.status == "PENDING",
                        EIP3009Authorization.valid_before <= now_epoch,
                    )
                    .order_by(EIP3009Authorization.valid_before)
                    .limit(bounded)
                )
            )
        for authorization_id in expired_ids:
            with self.database.session() as session:
                authorization = session.scalar(
                    select(EIP3009Authorization)
                    .where(EIP3009Authorization.id == authorization_id)
                    .with_for_update()
                )
                if (
                    authorization is None
                    or authorization.status != "PENDING"
                    or authorization.valid_before > now_epoch
                ):
                    continue
                order = session.get(PaymentOrder, authorization.payment_intent_id)
                authorization.status = "EXPIRED"
                authorization.last_error_code = "AUTHORIZATION_EXPIRED"
                if order is not None and order.status == "PENDING":
                    order.status = "EXPIRED"
                expired += 1

        with self.database.session() as session:
            due = list(
                session.scalars(
                    select(EIP3009Authorization)
                    .where(EIP3009Authorization.status.in_(("SUBMITTING", "SUBMITTED")))
                    .order_by(EIP3009Authorization.created_at)
                    .limit(bounded)
                )
            )
            targets = [(item.workspace_id, item.user_id, item.id) for item in due]
        confirmed = 0
        pending = 0
        failed = 0
        for workspace_id, user_id, authorization_id in targets:
            try:
                result = self.reconcile(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    authorization_id=authorization_id,
                )
            except EIP3009Error:
                failed += 1
                continue
            if result.status == "CONFIRMED":
                confirmed += 1
            elif result.status in {"FAILED", "EXPIRED", "RECONCILIATION_REQUIRED"}:
                failed += 1
            else:
                pending += 1
        return EIP3009SweepResult(
            expired=expired,
            confirmed=confirmed,
            pending=pending,
            failed=failed,
        )

    def cancel(
        self,
        *,
        workspace_id: str,
        user_id: str,
        authorization_id: str,
    ) -> EIP3009AuthorizationResult:
        with self.database.session() as session:
            authorization, order = self._load_owned(
                session, workspace_id, user_id, authorization_id, lock=True
            )
            if authorization.status == "PENDING" and order.status == "PENDING":
                authorization.status = "CANCELLED"
                authorization.last_error_code = "USER_CANCELLED"
                order.status = "CANCELLED"
            elif authorization.status != "CANCELLED":
                raise EIP3009Conflict("已提交的链上授权不能取消")
            return self._result(authorization, order)

    def _settle(
        self,
        *,
        workspace_id: str,
        user_id: str,
        authorization_id: str,
        receipt: dict[str, Any],
        transfer: dict[str, Any],
    ) -> EIP3009AuthorizationResult:
        with self.database.session() as session:
            authorization, order = self._load_owned(
                session, workspace_id, user_id, authorization_id, lock=True
            )
            if authorization.status == "CONFIRMED":
                return self._result(authorization, order)
            if authorization.status != "SUBMITTED" or order.status != "SUBMITTED":
                raise EIP3009Conflict("支付授权状态无法结算")
            payment = session.scalar(
                select(OnchainPayment).where(
                    OnchainPayment.network == self.network,
                    OnchainPayment.transaction_hash == authorization.transaction_hash,
                    OnchainPayment.log_index == transfer["log_index"],
                )
            )
            if payment is None:
                payment = OnchainPayment(
                    network=self.network,
                    chain_id=self.chain_id,
                    transaction_hash=authorization.transaction_hash,
                    log_index=transfer["log_index"],
                    block_number=transfer["block_number"],
                    from_address=authorization.from_address,
                    to_address=authorization.to_address,
                    token_address=self.usdc_contract,
                    token_decimals=self.token_decimals,
                    raw_amount_microunits=authorization.value_microunits,
                    workspace_id=authorization.workspace_id,
                    wallet_binding_id=None,
                    payment_intent_id=order.id,
                    provider_event_id=f"eip3009:{self.network}:{authorization.transaction_hash}",
                    credits_granted=0,
                    status="RECEIVED",
                    metadata_json={
                        "source": "EIP3009_RELAYER",
                        "authorization_id": authorization.id,
                        "relayer_address": authorization.relayer_address,
                    },
                )
                session.add(payment)
                session.flush([payment])
            identity_conflict = (
                payment.from_address != authorization.from_address
                or payment.to_address != authorization.to_address
                or payment.token_address != authorization.token_address
                or payment.raw_amount_microunits != authorization.value_microunits
                or payment.workspace_id not in {None, authorization.workspace_id}
                or payment.payment_intent_id not in {None, order.id}
                or payment.status in {"REMOVED", "RECONCILIATION_REQUIRED"}
            )
            if identity_conflict:
                authorization.status = "RECONCILIATION_REQUIRED"
                order.status = "RECONCILIATION_REQUIRED"
                payment.status = "RECONCILIATION_REQUIRED"
                return self._result(authorization, order)
            # Alchemy may observe the Transfer before the browser asks us to
            # reconcile the relayer receipt. In that race it creates an
            # UNMATCHED canonical payment because EIP-3009 orders intentionally
            # have no wallet binding. Adopt that exact chain fact instead of
            # creating a second payment or quarantining a valid one.
            payment.workspace_id = authorization.workspace_id
            payment.payment_intent_id = order.id
            payment.provider_event_id = f"eip3009:{self.network}:{authorization.transaction_hash}"
            payment.metadata_json = {
                **dict(payment.metadata_json or {}),
                "source": "EIP3009_RELAYER",
                "authorization_id": authorization.id,
                "relayer_address": authorization.relayer_address,
            }
            if (
                payment.workspace_id != authorization.workspace_id
                or payment.payment_intent_id != order.id
                or payment.raw_amount_microunits != order.raw_amount_microunits
            ):
                authorization.status = "RECONCILIATION_REQUIRED"
                order.status = "RECONCILIATION_REQUIRED"
                payment.status = "RECONCILIATION_REQUIRED"
                return self._result(authorization, order)
            existing = session.scalar(
                select(WorkspaceCreditLedgerEntry).where(
                    WorkspaceCreditLedgerEntry.payment_id == payment.id,
                    WorkspaceCreditLedgerEntry.entry_type == "USDC_PURCHASE",
                )
            )
            if existing is None:
                workspace = session.scalar(
                    select(Workspace).where(Workspace.id == workspace_id).with_for_update()
                )
                if (
                    workspace is None
                    or workspace.status != "ACTIVE"
                    or workspace.plan_tier not in {"FREE", "PRO"}
                ):
                    authorization.status = "RECONCILIATION_REQUIRED"
                    order.status = "RECONCILIATION_REQUIRED"
                    payment.status = "RECONCILIATION_REQUIRED"
                    return self._result(authorization, order)
                credits = order.credits
                before = workspace.credit_balance
                plan_before = workspace.plan_tier
                plan_after = "PRO" if plan_before == "FREE" else plan_before
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
                    raise EIP3009Conflict("workspace changed while applying relayed purchase")
                session.expire(workspace)
                session.refresh(workspace, ["credit_balance", "plan_tier"])
                gas_used = self._parse_quantity(receipt.get("gasUsed"))
                gas_price = self._parse_quantity(receipt.get("effectiveGasPrice"))
                ledger = WorkspaceCreditLedgerEntry(
                    workspace_id=workspace.id,
                    payment_id=payment.id,
                    external_reference=(
                        f"eip3009:{self.network}:{authorization.transaction_hash}:{transfer['log_index']}"
                    ),
                    entry_type="USDC_PURCHASE",
                    direction="CREDIT",
                    credits=credits,
                    balance_before=before,
                    balance_after=workspace.credit_balance,
                    currency=order.currency,
                    raw_amount_microunits=order.raw_amount_microunits,
                    chain_id=self.chain_id,
                    metadata_json={
                        "source": "EIP3009_RELAYER",
                        "authorization_id": authorization.id,
                        "payment_order_id": order.id,
                        "sku": order.sku,
                        "pricing_version": order.pricing_version,
                        "plan_before": plan_before,
                        "plan_after": workspace.plan_tier,
                        "pro_activated": plan_before == "FREE",
                        "relayer_address": authorization.relayer_address,
                        "relayer_gas_used": gas_used,
                        "relayer_effective_gas_price_wei": gas_price,
                        "relayer_gas_cost_wei": gas_used * gas_price,
                        "gas_sponsored": True,
                    },
                )
                session.add(ledger)
                payment.credits_granted = credits
                payment.status = "CREDITED"
                result = EIP3009AuthorizationResult(
                    authorization.id,
                    "CONFIRMED",
                    authorization.transaction_hash,
                    credits,
                    plan_tier=workspace.plan_tier,
                    pro_activated=plan_before == "FREE",
                )
            else:
                payment.status = "CREDITED"
                payment.credits_granted = existing.credits
                result = EIP3009AuthorizationResult(
                    authorization.id,
                    "CONFIRMED",
                    authorization.transaction_hash,
                    existing.credits,
                )
            authorization.status = "CONFIRMED"
            authorization.confirmed_at = utcnow()
            authorization.last_error_code = None
            order.status = "PAID"
            order.paid_at = authorization.confirmed_at
            return result

    def _prepare_transaction(
        self, authorization: EIP3009Authorization, signature: str
    ) -> tuple[str, str, int]:
        calldata = self._transfer_calldata(authorization, signature)
        nonce = self._parse_quantity(self._rpc("eth_getTransactionCount", [self.relayer_address, "pending"]))
        estimate = self._parse_quantity(
            self._rpc(
                "eth_estimateGas",
                [
                    {
                        "from": self.relayer_address,
                        "to": self.usdc_contract,
                        "data": calldata,
                        "value": "0x0",
                    }
                ],
            )
        )
        gas = min(self.max_gas_limit, max(80_000, (estimate * 120 + 99) // 100))
        if estimate > self.max_gas_limit:
            raise EIP3009RPCError("GAS_LIMIT_EXCEEDED", "授权交易需要的 Gas 超出平台上限")
        latest = self._rpc("eth_getBlockByNumber", ["latest", False])
        if not isinstance(latest, dict):
            raise EIP3009RPCError("INVALID_LATEST_BLOCK")
        base_fee = self._parse_quantity(latest.get("baseFeePerGas"))
        try:
            priority = self._parse_quantity(self._rpc("eth_maxPriorityFeePerGas", []))
        except EIP3009RPCError:
            priority = 1_000_000
        max_fee = base_fee * 2 + priority
        if max_fee > self.max_fee_per_gas_wei:
            raise EIP3009RPCError("GAS_PRICE_TOO_HIGH", "Base Gas 价格超出平台代付上限")
        relayer_balance = self._parse_quantity(self._rpc("eth_getBalance", [self.relayer_address, "latest"]))
        if relayer_balance < gas * max_fee:
            raise EIP3009RPCError("RELAYER_BALANCE_LOW", "Relayer 的 Base ETH 不足以代付本次 Gas")
        transaction = {
            "chainId": self.chain_id,
            "nonce": nonce,
            "to": to_checksum_address(self.usdc_contract),
            "value": 0,
            "data": calldata,
            "gas": gas,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority,
            "type": 2,
        }
        signed = Account.sign_transaction(transaction, self.relayer_private_key)
        expected = "0x" + signed.hash.hex()
        raw_transaction = "0x" + signed.raw_transaction.hex()
        return raw_transaction, expected, nonce

    def _ensure_broadcast(
        self,
        *,
        workspace_id: str,
        user_id: str,
        authorization_id: str,
    ) -> EIP3009AuthorizationResult:
        with self.database.session() as session:
            authorization, order = self._load_owned(
                session, workspace_id, user_id, authorization_id, lock=False
            )
            if authorization.status in {"SUBMITTED", "CONFIRMED"}:
                return self._result(authorization, order)
            if (
                authorization.status != "SUBMITTING"
                or not authorization.transaction_hash
                or not authorization.raw_transaction
            ):
                raise EIP3009Conflict("Relayer transaction is not prepared")
            tx_hash = authorization.transaction_hash
            raw_transaction = authorization.raw_transaction
            expired = authorization.valid_before <= int(utcnow().timestamp())
        if expired:
            try:
                receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
                known = self._rpc("eth_getTransactionByHash", [tx_hash])
            except EIP3009RPCError:
                receipt = known = None
            if not isinstance(receipt, dict) and not isinstance(known, dict):
                with self.database.session() as session:
                    authorization, order = self._load_owned(
                        session, workspace_id, user_id, authorization_id, lock=True
                    )
                    if authorization.status == "SUBMITTING":
                        authorization.status = "EXPIRED"
                        authorization.last_error_code = "AUTHORIZATION_EXPIRED_BEFORE_BROADCAST"
                        if order.status == "SUBMITTED":
                            order.status = "EXPIRED"
                    return self._result(authorization, order)
        try:
            sent = str(self._rpc("eth_sendRawTransaction", [raw_transaction])).lower()
            if sent != tx_hash:
                raise EIP3009RPCError("TRANSACTION_HASH_MISMATCH")
        except EIP3009RPCError as exc:
            try:
                known = self._rpc("eth_getTransactionByHash", [tx_hash])
            except EIP3009RPCError:
                known = None
            if not isinstance(known, dict):
                with self.database.session() as session:
                    authorization, _order = self._load_owned(
                        session, workspace_id, user_id, authorization_id, lock=True
                    )
                    authorization.last_error_code = exc.code
                raise
        with self.database.session() as session:
            authorization, order = self._load_owned(
                session, workspace_id, user_id, authorization_id, lock=True
            )
            if authorization.status == "SUBMITTING":
                authorization.status = "SUBMITTED"
                authorization.last_error_code = None
            return self._result(authorization, order)

    def _validated_transfer(
        self,
        receipt: dict[str, Any],
        authorization: EIP3009Authorization,
    ) -> dict[str, str]:
        if self._normalize_address(str(receipt.get("to") or "")) != self.usdc_contract:
            raise EIP3009Conflict("Relayer receipt target is not Base USDC")
        transfer_topic = "0x" + keccak(text=_TRANSFER_EVENT).hex()
        authorization_topic = "0x" + keccak(text=_AUTHORIZATION_USED_EVENT).hex()
        expected_from = "0x" + authorization.from_address[2:].rjust(64, "0")
        expected_to = "0x" + authorization.to_address[2:].rjust(64, "0")
        expected_nonce = authorization.nonce.lower()
        matches: list[dict[str, Any]] = []
        auth_used = False
        for item in receipt.get("logs") or []:
            if (
                not isinstance(item, dict)
                or self._normalize_address(str(item.get("address") or "")) != self.usdc_contract
            ):
                continue
            topics = [str(value).lower() for value in (item.get("topics") or [])]
            if (
                len(topics) >= 3
                and topics[0] == transfer_topic
                and topics[1] == expected_from
                and topics[2] == expected_to
                and self._parse_quantity(item.get("data")) == authorization.value_microunits
            ):
                matches.append(item)
            if (
                len(topics) >= 3
                and topics[0] == authorization_topic
                and topics[1] == expected_from
                and topics[2] == expected_nonce
            ):
                auth_used = True
        if len(matches) != 1 or not auth_used:
            raise EIP3009Conflict("Relayer receipt does not prove the exact USDC authorization")
        item = matches[0]
        return {
            "log_index": str(item.get("logIndex") or "").lower(),
            "block_number": str(receipt.get("blockNumber") or "").lower(),
        }

    def _authorization_used(self, authorization: EIP3009Authorization) -> bool:
        selector = keccak(text=_AUTHORIZATION_STATE_FUNCTION)[:4]
        data = selector + encode(
            ["address", "bytes32"],
            [to_checksum_address(authorization.from_address), bytes.fromhex(authorization.nonce[2:])],
        )
        result = str(
            self._rpc(
                "eth_call",
                [{"to": self.usdc_contract, "data": "0x" + data.hex()}, "latest"],
            )
        )
        return self._parse_quantity(result) != 0

    def _transfer_calldata(self, authorization: EIP3009Authorization, signature: str) -> str:
        raw = bytes.fromhex(signature[2:])
        r = raw[:32]
        s = raw[32:64]
        v = raw[64]
        if v in {0, 1}:
            v += 27
        if v not in {27, 28}:
            raise EIP3009Rejected("EIP-712 签名 recovery id 无效")
        selector = keccak(text=_TRANSFER_AUTHORIZATION_FUNCTION)[:4]
        arguments = encode(
            [
                "address",
                "address",
                "uint256",
                "uint256",
                "uint256",
                "bytes32",
                "uint8",
                "bytes32",
                "bytes32",
            ],
            [
                to_checksum_address(authorization.from_address),
                to_checksum_address(authorization.to_address),
                authorization.value_microunits,
                authorization.valid_after,
                authorization.valid_before,
                bytes.fromhex(authorization.nonce[2:]),
                v,
                r,
                s,
            ],
        )
        return "0x" + (selector + arguments).hex()

    def _typed_data(
        self,
        *,
        payer: str,
        value: int,
        valid_after: int,
        valid_before: int,
        nonce: str,
    ) -> dict[str, Any]:
        return {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "TransferWithAuthorization": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "validAfter", "type": "uint256"},
                    {"name": "validBefore", "type": "uint256"},
                    {"name": "nonce", "type": "bytes32"},
                ],
            },
            "primaryType": "TransferWithAuthorization",
            "domain": {
                "name": self.token_name,
                "version": self.token_version,
                "chainId": self.chain_id,
                "verifyingContract": self.usdc_contract,
            },
            "message": {
                "from": payer,
                "to": self.treasury_address,
                "value": value,
                "validAfter": valid_after,
                "validBefore": valid_before,
                "nonce": nonce,
            },
        }

    def _typed_data_from_row(self, authorization: EIP3009Authorization) -> dict[str, Any]:
        return self._typed_data(
            payer=authorization.from_address,
            value=authorization.value_microunits,
            valid_after=authorization.valid_after,
            valid_before=authorization.valid_before,
            nonce=authorization.nonce,
        )

    @staticmethod
    def _signable_message(typed_data: dict[str, Any]) -> SignableMessage:
        return encode_typed_data(full_message=typed_data)

    @staticmethod
    def _message_hash(message: SignableMessage) -> bytes:
        return keccak(b"\x19" + message.version + message.header + message.body)

    def _load_owned(
        self,
        session: Session,
        workspace_id: str,
        user_id: str,
        authorization_id: str,
        *,
        lock: bool,
    ) -> tuple[EIP3009Authorization, PaymentOrder]:
        statement = select(EIP3009Authorization).where(
            EIP3009Authorization.id == authorization_id,
            EIP3009Authorization.workspace_id == workspace_id,
            EIP3009Authorization.user_id == user_id,
        )
        if lock:
            statement = statement.with_for_update()
        authorization = session.scalar(statement)
        if authorization is None:
            raise EIP3009NotFound("支付授权不存在")
        order_statement = select(PaymentOrder).where(
            PaymentOrder.id == authorization.payment_intent_id,
            PaymentOrder.workspace_id == workspace_id,
        )
        if lock:
            order_statement = order_statement.with_for_update()
        order = session.scalar(order_statement)
        if order is None:
            raise EIP3009Conflict("支付订单不存在")
        return authorization, order

    def _lock_relayer(self, session: Session) -> None:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            lock_id = int.from_bytes(
                hashlib.sha256(self.relayer_address.encode()).digest()[:8], "big", signed=True
            )
            session.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": lock_id})
        state = session.scalar(
            select(RelayerAccountState)
            .where(RelayerAccountState.address == self.relayer_address)
            .with_for_update()
        )
        if state is None:
            state = RelayerAccountState(
                address=self.relayer_address,
                chain_id=self.chain_id,
                last_submitted_nonce=None,
                metadata_json={},
            )
            session.add(state)
            session.flush([state])

    def _require_chain(self) -> None:
        if self._parse_quantity(self._rpc("eth_chainId", [])) != self.chain_id:
            raise EIP3009RPCError("WRONG_CHAIN", "BASE_RPC_URL is not Base Mainnet")
        selector = "0x" + keccak(text="DOMAIN_SEPARATOR()")[:4].hex()
        onchain = str(
            self._rpc(
                "eth_call",
                [{"to": self.usdc_contract, "data": selector}, "latest"],
            )
        ).lower()
        domain_typehash = keccak(
            text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
        )
        expected = (
            "0x"
            + keccak(
                encode(
                    ["bytes32", "bytes32", "bytes32", "uint256", "address"],
                    [
                        domain_typehash,
                        keccak(text=self.token_name),
                        keccak(text=self.token_version),
                        self.chain_id,
                        to_checksum_address(self.usdc_contract),
                    ],
                )
            ).hex()
        )
        if onchain != expected:
            raise EIP3009RPCError("USDC_DOMAIN_MISMATCH", "Base USDC EIP-712 domain does not match the build")

    def _rpc(self, method: str, params: list[Any]) -> Any:
        try:
            response = self._client.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise EIP3009RPCError("RPC_UNAVAILABLE") from exc
        if not isinstance(payload, dict) or payload.get("error") is not None or "result" not in payload:
            raise EIP3009RPCError("RPC_REJECTED")
        return payload["result"]

    def _require_configured(self) -> None:
        if not self.configured:
            raise EIP3009ConfigurationError("Base USDC Relayer 尚未配置")

    @staticmethod
    def _normalize_address(value: str) -> str:
        stripped = value.strip()
        return stripped.lower() if _EVM_ADDRESS.fullmatch(stripped) else ""

    @staticmethod
    def _parse_quantity(value: object) -> int:
        if isinstance(value, bool):
            raise EIP3009RPCError("INVALID_QUANTITY")
        if isinstance(value, int):
            result = value
        elif isinstance(value, str):
            stripped = value.strip().lower()
            try:
                result = int(stripped, 16 if stripped.startswith("0x") else 10)
            except ValueError as exc:
                raise EIP3009RPCError("INVALID_QUANTITY") from exc
        else:
            raise EIP3009RPCError("INVALID_QUANTITY")
        if result < 0:
            raise EIP3009RPCError("INVALID_QUANTITY")
        return result

    @staticmethod
    def _result(
        authorization: EIP3009Authorization,
        order: PaymentOrder,
    ) -> EIP3009AuthorizationResult:
        return EIP3009AuthorizationResult(
            authorization.id,
            authorization.status,
            authorization.transaction_hash,
            order.credits if authorization.status == "CONFIRMED" else 0,
        )
