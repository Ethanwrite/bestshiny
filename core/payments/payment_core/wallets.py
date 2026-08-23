from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from eth_account import Account
from eth_account.messages import encode_defunct
from platform_database import Database
from production_domain.models import (
    OnchainPaymentIntent,
    WalletBindingChallenge,
    WorkspaceWalletBinding,
    utcnow,
)
from sqlalchemy import select

from .alchemy import BASE_NETWORKS

_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TRANSACTION_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")


class WalletPaymentError(RuntimeError):
    pass


class WalletPaymentConflict(WalletPaymentError):
    pass


class WalletPaymentRejected(WalletPaymentError):
    pass


class WalletPaymentNotFound(WalletPaymentError):
    pass


@dataclass(frozen=True)
class WalletChallengeResult:
    challenge_id: str
    address: str
    chain_id: int
    message: str
    expires_at: datetime


class WalletPaymentService:
    """Own wallet proofs and server-priced Base USDC purchase intents."""

    def __init__(
        self,
        database: Database,
        *,
        network: str,
        treasury_address: str,
        usdc_microunits_per_credit: int,
        challenge_ttl_seconds: int = 300,
        intent_ttl_minutes: int = 30,
    ) -> None:
        if network not in BASE_NETWORKS:
            raise ValueError(f"unsupported wallet payment network: {network}")
        self.database = database
        self.network = network
        self.chain_id, self.usdc_contract = BASE_NETWORKS[network]
        self.treasury_address = self.normalize_address(treasury_address)
        if treasury_address.strip() and not self.treasury_address:
            raise ValueError("wallet payment treasury address must be a valid EVM address")
        if usdc_microunits_per_credit < 1:
            raise ValueError("USDC microunits per credit must be positive")
        self.usdc_microunits_per_credit = usdc_microunits_per_credit
        self.challenge_ttl = timedelta(seconds=max(60, min(challenge_ttl_seconds, 900)))
        self.intent_ttl = timedelta(minutes=max(5, min(intent_ttl_minutes, 120)))

    def issue_challenge(
        self,
        *,
        workspace_id: str,
        user_id: str,
        address: str,
        chain_id: int,
        origin: str,
    ) -> WalletChallengeResult:
        normalized = self.normalize_address(address)
        if not normalized:
            raise WalletPaymentRejected("钱包地址无效")
        if chain_id != self.chain_id:
            raise WalletPaymentRejected(f"钱包必须切换到 {self.network}")
        normalized_origin = origin.strip().rstrip("/")[:500]
        if not normalized_origin.startswith(("https://", "http://")):
            raise WalletPaymentRejected("钱包签名来源无效")
        now = utcnow()
        expires_at = now + self.challenge_ttl
        nonce = secrets.token_urlsafe(24)
        message = self._challenge_message(
            origin=normalized_origin,
            workspace_id=workspace_id,
            address=normalized,
            chain_id=chain_id,
            nonce=nonce,
            issued_at=now,
            expires_at=expires_at,
        )
        with self.database.session() as session:
            existing = session.scalar(
                select(WorkspaceWalletBinding).where(
                    WorkspaceWalletBinding.chain_id == chain_id,
                    WorkspaceWalletBinding.address == normalized,
                )
            )
            if existing is not None and existing.workspace_id != workspace_id:
                raise WalletPaymentConflict("该钱包已绑定到其他工作空间")
            challenge = WalletBindingChallenge(
                workspace_id=workspace_id,
                user_id=user_id,
                chain_id=chain_id,
                address=normalized,
                nonce_hash=hashlib.sha256(nonce.encode()).hexdigest(),
                message=message,
                message_hash=hashlib.sha256(message.encode()).hexdigest(),
                expires_at=expires_at,
            )
            session.add(challenge)
            session.flush([challenge])
            return WalletChallengeResult(
                challenge_id=challenge.id,
                address=normalized,
                chain_id=chain_id,
                message=message,
                expires_at=expires_at,
            )

    def verify_challenge(
        self,
        *,
        workspace_id: str,
        user_id: str,
        challenge_id: str,
        signature: str,
    ) -> WorkspaceWalletBinding:
        if not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature.strip()):
            raise WalletPaymentRejected("钱包签名格式无效")
        now = utcnow()
        with self.database.session() as session:
            challenge = session.scalar(
                select(WalletBindingChallenge)
                .where(
                    WalletBindingChallenge.id == challenge_id,
                    WalletBindingChallenge.workspace_id == workspace_id,
                    WalletBindingChallenge.user_id == user_id,
                )
                .with_for_update()
            )
            if challenge is None:
                raise WalletPaymentNotFound("钱包验证请求不存在")
            if challenge.consumed_at is not None:
                raise WalletPaymentConflict("钱包验证请求已使用")
            if self._utc(challenge.expires_at) <= now:
                raise WalletPaymentRejected("钱包验证请求已过期")
            if hashlib.sha256(challenge.message.encode()).hexdigest() != challenge.message_hash:
                raise WalletPaymentConflict("钱包验证消息完整性检查失败")
            try:
                recovered = Account.recover_message(
                    encode_defunct(text=challenge.message),
                    signature=signature.strip(),
                ).lower()
            except Exception as exc:  # eth-account exposes backend-specific recovery errors
                raise WalletPaymentRejected("钱包签名无法验证") from exc
            if not secrets.compare_digest(recovered, challenge.address):
                raise WalletPaymentRejected("签名钱包与待绑定地址不一致")
            binding = session.scalar(
                select(WorkspaceWalletBinding)
                .where(
                    WorkspaceWalletBinding.chain_id == challenge.chain_id,
                    WorkspaceWalletBinding.address == challenge.address,
                )
                .with_for_update()
            )
            if binding is not None and binding.workspace_id != workspace_id:
                raise WalletPaymentConflict("该钱包已绑定到其他工作空间")
            if binding is None:
                binding = WorkspaceWalletBinding(
                    workspace_id=workspace_id,
                    chain_id=challenge.chain_id,
                    address=challenge.address,
                    status="VERIFIED",
                    verified_by_user_id=user_id,
                    verified_at=now,
                    metadata_json={},
                )
                session.add(binding)
            else:
                binding.status = "VERIFIED"
                binding.verified_by_user_id = user_id
                binding.verified_at = now
                binding.revoked_at = None
            binding.metadata_json = {
                **dict(binding.metadata_json or {}),
                "challenge_id": challenge.id,
                "verification": "EIP191_PERSONAL_SIGN",
                "message_hash": challenge.message_hash,
            }
            challenge.consumed_at = now
            session.flush([binding])
            return binding

    def create_intent(
        self,
        *,
        workspace_id: str,
        binding_id: str,
        raw_amount_microunits: int,
    ) -> OnchainPaymentIntent:
        if not self.treasury_address:
            raise WalletPaymentRejected("收款钱包尚未配置")
        credits, remainder = divmod(raw_amount_microunits, self.usdc_microunits_per_credit)
        if remainder or credits < 1:
            raise WalletPaymentRejected("USDC 金额不能准确换算为整数积分")
        if raw_amount_microunits > 1_000_000_000_000:
            raise WalletPaymentRejected("单笔 USDC 支付金额超出限制")
        now = utcnow()
        with self.database.session() as session:
            binding = session.scalar(
                select(WorkspaceWalletBinding).where(
                    WorkspaceWalletBinding.id == binding_id,
                    WorkspaceWalletBinding.workspace_id == workspace_id,
                    WorkspaceWalletBinding.chain_id == self.chain_id,
                    WorkspaceWalletBinding.status == "VERIFIED",
                )
            )
            if binding is None:
                raise WalletPaymentRejected("请先验证付款钱包")
            for pending in session.scalars(
                select(OnchainPaymentIntent).where(
                    OnchainPaymentIntent.wallet_binding_id == binding.id,
                    OnchainPaymentIntent.status == "PENDING",
                )
            ):
                pending.status = "CANCELLED"
            intent = OnchainPaymentIntent(
                workspace_id=workspace_id,
                wallet_binding_id=binding.id,
                network=self.network,
                chain_id=self.chain_id,
                from_address=binding.address,
                to_address=self.treasury_address,
                token_address=self.usdc_contract,
                raw_amount_microunits=raw_amount_microunits,
                credits=credits,
                status="PENDING",
                expires_at=now + self.intent_ttl,
                metadata_json={"microunits_per_credit": self.usdc_microunits_per_credit},
            )
            session.add(intent)
            session.flush([intent])
            return intent

    def submit_intent(
        self,
        *,
        workspace_id: str,
        intent_id: str,
        transaction_hash: str,
    ) -> OnchainPaymentIntent:
        normalized_hash = transaction_hash.strip().lower()
        if not _TRANSACTION_HASH.fullmatch(normalized_hash):
            raise WalletPaymentRejected("交易哈希格式无效")
        now = utcnow()
        with self.database.session() as session:
            intent = session.scalar(
                select(OnchainPaymentIntent)
                .where(
                    OnchainPaymentIntent.id == intent_id,
                    OnchainPaymentIntent.workspace_id == workspace_id,
                )
                .with_for_update()
            )
            if intent is None:
                raise WalletPaymentNotFound("支付请求不存在")
            if intent.status == "PAID":
                if intent.transaction_hash != normalized_hash:
                    raise WalletPaymentConflict("支付请求已由其他交易完成")
                return intent
            if intent.status not in {"PENDING", "SUBMITTED"}:
                raise WalletPaymentConflict("支付请求当前不可提交")
            if self._utc(intent.expires_at) <= now:
                raise WalletPaymentRejected("支付请求已过期")
            if intent.transaction_hash and intent.transaction_hash != normalized_hash:
                raise WalletPaymentConflict("支付请求已关联其他交易")
            intent.transaction_hash = normalized_hash
            intent.status = "SUBMITTED"
            intent.submitted_at = now
            session.flush([intent])
            return intent

    def cancel_intent(
        self,
        *,
        workspace_id: str,
        intent_id: str,
    ) -> OnchainPaymentIntent:
        """Cancel an intent only while no transaction has been submitted."""

        with self.database.session() as session:
            intent = session.scalar(
                select(OnchainPaymentIntent)
                .where(
                    OnchainPaymentIntent.id == intent_id,
                    OnchainPaymentIntent.workspace_id == workspace_id,
                )
                .with_for_update()
            )
            if intent is None:
                raise WalletPaymentNotFound("支付请求不存在")
            if intent.status == "CANCELLED":
                return intent
            if intent.status != "PENDING" or intent.transaction_hash:
                raise WalletPaymentConflict("已提交链上交易的支付请求不能取消")
            intent.status = "CANCELLED"
            session.flush([intent])
            return intent

    @staticmethod
    def normalize_address(value: str) -> str:
        stripped = value.strip()
        return stripped.lower() if _EVM_ADDRESS.fullmatch(stripped) else ""

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _challenge_message(
        *,
        origin: str,
        workspace_id: str,
        address: str,
        chain_id: int,
        nonce: str,
        issued_at: datetime,
        expires_at: datetime,
    ) -> str:
        return "\n".join(
            (
                "AI Director requests wallet ownership verification.",
                "",
                f"Origin: {origin}",
                f"Workspace ID: {workspace_id}",
                f"Address: {address}",
                f"Chain ID: {chain_id}",
                f"Nonce: {nonce}",
                f"Issued At: {issued_at.isoformat()}",
                f"Expiration Time: {expires_at.isoformat()}",
                "",
                "This signature does not send a transaction or spend funds.",
            )
        )
