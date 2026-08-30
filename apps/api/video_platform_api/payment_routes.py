from __future__ import annotations

from decimal import Decimal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from payment_core import (
    DePayAuthenticationError,
    DePayConfigurationError,
    DePayPayloadError,
    WalletPaymentConflict,
    WalletPaymentNotFound,
    WalletPaymentRejected,
)
from production_domain.models import (
    DePayCheckoutSession,
    OnchainPaymentIntent,
    Workspace,
    WorkspaceWalletBinding,
)
from pydantic import BaseModel, Field
from sqlalchemy import select

from .auth import AuthPrincipal, AuthService
from .container import Container


class WalletChallengeRequest(BaseModel):
    address: str = Field(min_length=42, max_length=42)
    chain_id: int = Field(gt=0)


class WalletVerifyRequest(BaseModel):
    challenge_id: str = Field(min_length=1, max_length=100)
    signature: str = Field(min_length=132, max_length=132)


class PaymentIntentRequest(BaseModel):
    wallet_binding_id: str = Field(min_length=1, max_length=100)
    amount_usdc: Decimal = Field(gt=0, max_digits=18, decimal_places=6)


class PaymentSubmitRequest(BaseModel):
    transaction_hash: str = Field(min_length=66, max_length=66)


class CheckoutRequest(BaseModel):
    """The whole of what a browser may choose: which package, for which workspace.

    Amount, currency and credits are server-owned and never accepted here.
    """

    workspace_id: str = Field(min_length=1, max_length=36)
    sku: str = Field(min_length=1, max_length=80)


class DePayCheckoutRequest(BaseModel):
    sku: str = Field(min_length=1, max_length=80)


def _binding_view(binding: WorkspaceWalletBinding) -> dict[str, object]:
    return {
        "id": binding.id,
        "workspace_id": binding.workspace_id,
        "chain_id": binding.chain_id,
        "address": binding.address,
        "status": binding.status,
        "verified_at": binding.verified_at,
        "revoked_at": binding.revoked_at,
    }


def _intent_view(intent: OnchainPaymentIntent) -> dict[str, object]:
    return {
        "id": intent.id,
        "workspace_id": intent.workspace_id,
        "wallet_binding_id": intent.wallet_binding_id,
        "network": intent.network,
        "chain_id": intent.chain_id,
        "from_address": intent.from_address,
        "to_address": intent.to_address,
        "token_address": intent.token_address,
        "raw_amount_microunits": intent.raw_amount_microunits,
        "amount_usdc": f"{Decimal(intent.raw_amount_microunits) / Decimal(1_000_000):f}",
        "sku": intent.sku,
        "currency": intent.currency,
        "pricing_version": intent.pricing_version,
        "provider": intent.provider,
        "credits": intent.credits,
        "status": intent.status,
        "transaction_hash": intent.transaction_hash,
        "expires_at": intent.expires_at,
        "submitted_at": intent.submitted_at,
        "paid_at": intent.paid_at,
    }


def register_payment_routes(app: FastAPI, container: Container, auth: AuthService) -> None:
    def require_legacy_wallet_payments() -> None:
        if not container.settings.legacy_wallet_payments_enabled:
            raise HTTPException(410, "旧钱包支付入口已停用，请使用固定 DePay Offer")

    @app.get("/v1/payments/config")
    def payment_config(principal: AuthPrincipal = Depends(auth.current_user)):  # noqa: B008
        del principal
        settings = container.settings
        service = container.wallet_payments
        depay = container.depay_payments
        return {
            "reown_project_id": settings.reown_project_id,
            "reown_configured": bool(settings.reown_project_id.strip()),
            "legacy_wallet_payments_enabled": settings.legacy_wallet_payments_enabled,
            "payment_configured": bool(service.treasury_address),
            "network": service.network,
            "chain_id": service.chain_id,
            "treasury_address": service.treasury_address,
            "usdc_contract": service.usdc_contract,
            "usdc_microunits_per_credit": service.usdc_microunits_per_credit,
            "crediting_enabled": settings.alchemy_crediting_enabled,
            "checkout_provider": "DEPAY",
            "depay_checkout_configured": depay.checkout_configured,
            "depay_callback_configured": depay.callback_configured,
            "depay_dynamic_configured": depay.dynamic_configured,
            "depay_integration_id": depay.integration_id,
            "payment_packages": depay.package_views(),
        }

    @app.get("/v1/workspaces/{workspace_id}/billing")
    def billing_overview(
        workspace_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),  # noqa: B008
    ):
        role = auth.require_workspace(principal, workspace_id)
        with container.database.session() as session:
            workspace = session.get(Workspace, workspace_id)
            if workspace is None:
                raise HTTPException(404, "工作空间不存在")
            bindings = list(
                session.scalars(
                    select(WorkspaceWalletBinding)
                    .where(WorkspaceWalletBinding.workspace_id == workspace_id)
                    .order_by(WorkspaceWalletBinding.created_at.desc())
                )
            )
            return {
                "workspace_id": workspace.id,
                "role": role,
                "plan_tier": workspace.plan_tier,
                "credit_balance": workspace.credit_balance,
                "wallet_bindings": [_binding_view(binding) for binding in bindings],
            }

    def _open_checkout(workspace_id: str, sku: str, principal: AuthPrincipal):
        auth.require_workspace(principal, workspace_id)
        if principal.development_bypass:
            raise HTTPException(403, "DePay 充值需要真实登录账户")
        try:
            checkout = container.depay_payments.create_checkout(
                workspace_id=workspace_id,
                user_id=principal.user_id,
                sku=sku,
            )
        except DePayConfigurationError as exc:
            raise HTTPException(503, str(exc)) from exc
        except DePayPayloadError as exc:
            raise HTTPException(422, str(exc)) from exc
        # `checkout_token` is the bearer the widget hands back through Dynamic
        # Configuration, so it goes to the buyer's own browser and nowhere
        # else. Nothing here names the provider's internal settlement objects.
        return {
            "id": checkout.checkout_id,
            "integration_id": checkout.integration_id,
            "checkout_token": checkout.checkout_token,
            "sku": checkout.sku,
            "amount_usdc": checkout.expected_usdc,
            "currency": checkout.currency,
            "credits": checkout.expected_credits,
            "purchase_kind": checkout.purchase_kind,
            "expires_at": checkout.expires_at,
        }

    @app.post("/v1/payments/checkout", status_code=201)
    def create_checkout(
        body: CheckoutRequest,
        principal: AuthPrincipal = Depends(auth.current_user),  # noqa: B008
    ):
        return _open_checkout(body.workspace_id, body.sku, principal)

    @app.post("/v1/payments/depay/config")
    async def depay_dynamic_configuration(request: Request):
        """Price one existing order for the DePay widget. Reads only.

        No settlement, no order state change and no credits happen here — the
        signed callback at `/v1/webhooks/depay` is the only path that posts to
        the ledger.
        """
        raw_body = await request.body()
        try:
            body, signature = container.depay_payments.dynamic_configuration(
                raw_body,
                request.headers.get("x-signature"),
            )
        except DePayConfigurationError as exc:
            raise HTTPException(503, str(exc)) from exc
        except DePayAuthenticationError as exc:
            raise HTTPException(401, str(exc)) from exc
        except DePayPayloadError as exc:
            raise HTTPException(400, str(exc)) from exc
        return Response(
            content=body,
            media_type="application/json",
            headers={"x-signature": signature},
        )

    @app.post("/v1/workspaces/{workspace_id}/depay-checkouts", status_code=201)
    def create_depay_checkout(
        workspace_id: str,
        body: DePayCheckoutRequest,
        principal: AuthPrincipal = Depends(auth.current_user),  # noqa: B008
    ):
        return _open_checkout(workspace_id, body.sku, principal)

    @app.get("/v1/workspaces/{workspace_id}/depay-checkouts/{checkout_id}")
    def get_depay_checkout(
        workspace_id: str,
        checkout_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),  # noqa: B008
    ):
        auth.require_workspace(principal, workspace_id)
        with container.database.session() as session:
            checkout = session.scalar(
                select(DePayCheckoutSession).where(
                    DePayCheckoutSession.id == checkout_id,
                    DePayCheckoutSession.workspace_id == workspace_id,
                )
            )
            if checkout is None:
                raise HTTPException(404, "DePay 充值会话不存在")
            intent = (
                session.get(OnchainPaymentIntent, checkout.payment_intent_id)
                if checkout.payment_intent_id
                else None
            )
            # The authoritative fulfillment state the browser polls. Deliberately
            # free of order_ref, pricing_version, token addresses and settlement
            # identifiers: an ordinary buyer needs the outcome, not the plumbing.
            return {
                "id": checkout.id,
                "status": checkout.status,
                "sku": intent.sku if intent else None,
                "amount_usdc": (
                    f"{Decimal(intent.raw_amount_microunits) / Decimal(1_000_000):.2f}"
                    if intent
                    else None
                ),
                "credits": intent.credits if intent else None,
                "purchase_kind": (intent.metadata_json or {}).get("purchase_kind") if intent else None,
                "credits_granted": checkout.credits_granted,
                "expires_at": checkout.expires_at,
                "paid_at": checkout.paid_at,
            }

    @app.post("/v1/workspaces/{workspace_id}/wallet-bindings/challenge")
    def issue_wallet_challenge(
        workspace_id: str,
        body: WalletChallengeRequest,
        request: Request,
        principal: AuthPrincipal = Depends(auth.current_user),  # noqa: B008
    ):
        require_legacy_wallet_payments()
        auth.require_workspace(principal, workspace_id, admin=True)
        if principal.development_bypass:
            raise HTTPException(403, "钱包绑定需要真实登录账户")
        origin = request.headers.get("origin") or container.settings.public_base_url
        try:
            challenge = container.wallet_payments.issue_challenge(
                workspace_id=workspace_id,
                user_id=principal.user_id,
                address=body.address,
                chain_id=body.chain_id,
                origin=origin,
            )
        except WalletPaymentConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except WalletPaymentRejected as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "challenge_id": challenge.challenge_id,
            "address": challenge.address,
            "chain_id": challenge.chain_id,
            "message": challenge.message,
            "expires_at": challenge.expires_at,
        }

    @app.post("/v1/workspaces/{workspace_id}/wallet-bindings/verify")
    def verify_wallet_binding(
        workspace_id: str,
        body: WalletVerifyRequest,
        principal: AuthPrincipal = Depends(auth.current_user),  # noqa: B008
    ):
        require_legacy_wallet_payments()
        auth.require_workspace(principal, workspace_id, admin=True)
        if principal.development_bypass:
            raise HTTPException(403, "钱包绑定需要真实登录账户")
        try:
            binding = container.wallet_payments.verify_challenge(
                workspace_id=workspace_id,
                user_id=principal.user_id,
                challenge_id=body.challenge_id,
                signature=body.signature,
            )
        except WalletPaymentNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except WalletPaymentConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except WalletPaymentRejected as exc:
            raise HTTPException(422, str(exc)) from exc
        return _binding_view(binding)

    @app.post("/v1/workspaces/{workspace_id}/payment-intents")
    def create_payment_intent(
        workspace_id: str,
        body: PaymentIntentRequest,
        principal: AuthPrincipal = Depends(auth.current_user),  # noqa: B008
    ):
        require_legacy_wallet_payments()
        auth.require_workspace(principal, workspace_id, admin=True)
        microunits_decimal = body.amount_usdc * Decimal(1_000_000)
        if microunits_decimal != microunits_decimal.to_integral_value():
            raise HTTPException(422, "USDC 金额最多支持 6 位小数")
        try:
            intent = container.wallet_payments.create_intent(
                workspace_id=workspace_id,
                binding_id=body.wallet_binding_id,
                raw_amount_microunits=int(microunits_decimal),
            )
        except WalletPaymentRejected as exc:
            raise HTTPException(422, str(exc)) from exc
        return _intent_view(intent)

    @app.post("/v1/workspaces/{workspace_id}/payment-intents/{intent_id}/submitted")
    def submit_payment_intent(
        workspace_id: str,
        intent_id: str,
        body: PaymentSubmitRequest,
        principal: AuthPrincipal = Depends(auth.current_user),  # noqa: B008
    ):
        require_legacy_wallet_payments()
        auth.require_workspace(principal, workspace_id, admin=True)
        try:
            intent = container.wallet_payments.submit_intent(
                workspace_id=workspace_id,
                intent_id=intent_id,
                transaction_hash=body.transaction_hash,
            )
        except WalletPaymentNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except WalletPaymentConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except WalletPaymentRejected as exc:
            raise HTTPException(422, str(exc)) from exc
        return _intent_view(intent)

    @app.post("/v1/workspaces/{workspace_id}/payment-intents/{intent_id}/cancel")
    def cancel_payment_intent(
        workspace_id: str,
        intent_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),  # noqa: B008
    ):
        require_legacy_wallet_payments()
        auth.require_workspace(principal, workspace_id, admin=True)
        try:
            intent = container.wallet_payments.cancel_intent(
                workspace_id=workspace_id,
                intent_id=intent_id,
            )
        except WalletPaymentNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except WalletPaymentConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return _intent_view(intent)

    @app.get("/v1/workspaces/{workspace_id}/payment-intents/{intent_id}")
    def get_payment_intent(
        workspace_id: str,
        intent_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),  # noqa: B008
    ):
        require_legacy_wallet_payments()
        auth.require_workspace(principal, workspace_id)
        with container.database.session() as session:
            intent = session.scalar(
                select(OnchainPaymentIntent).where(
                    OnchainPaymentIntent.id == intent_id,
                    OnchainPaymentIntent.workspace_id == workspace_id,
                )
            )
            if intent is None:
                raise HTTPException(404, "支付请求不存在")
            return _intent_view(intent)
