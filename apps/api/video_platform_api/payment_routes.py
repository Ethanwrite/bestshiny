from __future__ import annotations

from decimal import Decimal
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from payment_core import (
    DePayAuthenticationError,
    DePayConfigurationError,
    DePayPayloadError,
    EIP3009ConfigurationError,
    EIP3009Conflict,
    EIP3009NotFound,
    EIP3009Rejected,
    EIP3009RPCError,
    WalletPaymentConflict,
    WalletPaymentNotFound,
    WalletPaymentRejected,
    XunhuPayAuthenticationError,
    XunhuPayConfigurationError,
    XunhuPayConflict,
    XunhuPayGatewayError,
    XunhuPayPayloadError,
)
from production_domain.models import (
    DePayCheckoutSession,
    OnchainPaymentIntent,
    Workspace,
    WorkspaceWalletBinding,
    XunhuPayCheckoutSession,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator
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

    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(min_length=1, max_length=36)
    provider: Literal["depay", "xunhupay"] = "depay"
    plan_id: str | None = Field(default=None, min_length=1, max_length=80)
    # Compatibility for older BestShiny clients. New clients send `plan_id`;
    # either spelling selects only a server-owned catalogue row.
    sku: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def validate_plan(self) -> CheckoutRequest:
        if self.plan_id and self.sku and self.plan_id != self.sku:
            raise ValueError("plan_id and sku must match")
        if not self.plan_id and not self.sku:
            raise ValueError("plan_id is required")
        return self

    @property
    def selected_plan_id(self) -> str:
        return self.plan_id or self.sku or ""


class DePayCheckoutRequest(BaseModel):
    sku: str = Field(min_length=1, max_length=80)


class RelayedCheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(min_length=1, max_length=36)
    sku: str = Field(min_length=1, max_length=80)
    from_address: str = Field(min_length=42, max_length=42)


class RelayedAuthorizationSubmitRequest(BaseModel):
    signature: str = Field(min_length=4, max_length=16_386)


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
            raise HTTPException(410, "Legacy wallet payments are disabled; use a fixed payment pack")

    @app.get("/v1/payments/config")
    def payment_config(principal: AuthPrincipal = Depends(auth.current_user)):  # noqa: B008
        del principal
        settings = container.settings
        service = container.wallet_payments
        depay = container.depay_payments
        xunhupay = container.xunhupay_payments
        relayer = container.eip3009_relayer
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
            "checkout_provider": ("EIP3009_RELAYER" if relayer.configured else "DEPAY"),
            "relayed_usdc_configured": relayer.configured,
            "gas_sponsored": relayer.configured,
            "relayer_address": relayer.relayer_address if relayer.configured else "",
            "depay_checkout_configured": depay.checkout_configured,
            "depay_callback_configured": depay.callback_configured,
            "depay_dynamic_configured": depay.dynamic_configured,
            "depay_integration_id": depay.integration_id,
            "payment_packages": (relayer.package_views() if relayer.configured else depay.package_views()),
            "xunhupay_configured": xunhupay.configured,
            "xunhupay_packages": xunhupay.package_views(),
            "payment_methods": [
                {
                    "provider": "xunhupay",
                    "label": "WeChat Pay",
                    "detail": "XunHuPay",
                    "configured": xunhupay.configured,
                },
                {
                    "provider": "depay",
                    "label": "USDC",
                    "detail": "DePay",
                    "configured": bool(relayer.configured or depay.dynamic_configured),
                },
            ],
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
                raise HTTPException(404, "Workspace not found")
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

    @app.get("/v1/workspaces/{workspace_id}/payments/history")
    def payment_history(
        workspace_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),  # noqa: B008
    ):
        auth.require_workspace(principal, workspace_id)
        with container.database.session() as session:
            orders = list(
                session.scalars(
                    select(OnchainPaymentIntent)
                    .where(
                        OnchainPaymentIntent.workspace_id == workspace_id,
                        OnchainPaymentIntent.provider.in_(
                            ("DEPAY", "EIP3009_RELAYER", "XUNHUPAY")
                        ),
                    )
                    .order_by(OnchainPaymentIntent.created_at.desc())
                    .limit(20)
                )
            )
            return {
                "items": [
                    {
                        "id": order.id,
                        "plan_id": order.sku,
                        "provider": (
                            "xunhupay" if order.provider == "XUNHUPAY" else "depay"
                        ),
                        "amount": f"{Decimal(order.amount):.2f}",
                        "currency": order.currency,
                        "credits": order.credits,
                        "status": order.status,
                        "created_at": order.created_at,
                        "paid_at": order.paid_at,
                    }
                    for order in orders
                ]
            }

    def _open_checkout(workspace_id: str, sku: str, principal: AuthPrincipal):
        auth.require_workspace(principal, workspace_id)
        if principal.development_bypass:
            raise HTTPException(403, "DePay top-ups require a signed-in account")
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
        if body.provider == "depay":
            return _open_checkout(body.workspace_id, body.selected_plan_id, principal)
        auth.require_workspace(principal, body.workspace_id)
        if principal.development_bypass:
            raise HTTPException(403, "WeChat Pay top-ups require a signed-in account")
        try:
            checkout = container.xunhupay_payments.create_checkout(
                workspace_id=body.workspace_id,
                user_id=principal.user_id,
                plan_id=body.selected_plan_id,
            )
        except XunhuPayConfigurationError as exc:
            raise HTTPException(503, str(exc)) from exc
        except XunhuPayPayloadError as exc:
            raise HTTPException(422, str(exc)) from exc
        except XunhuPayAuthenticationError as exc:
            raise HTTPException(502, str(exc)) from exc
        except XunhuPayGatewayError as exc:
            raise HTTPException(502, str(exc)) from exc
        except XunhuPayConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "id": checkout.checkout_id,
            "provider": "xunhupay",
            "plan_id": checkout.plan_id,
            "amount": checkout.amount_cny,
            "currency": checkout.currency,
            "credits": checkout.credits,
            "purchase_kind": checkout.purchase_kind,
            "url": checkout.checkout_url,
            "url_qrcode": checkout.qrcode_url,
            "expires_at": checkout.expires_at,
        }

    @app.post("/v1/payments/xunhupay/notify")
    async def receive_xunhupay_notification(request: Request):
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            raise HTTPException(415, "XunHuPay notifications must use form encoding")
        raw_body = await request.body()
        try:
            container.xunhupay_payments.handle_notification(raw_body)
        except XunhuPayConfigurationError as exc:
            raise HTTPException(503, str(exc)) from exc
        except XunhuPayAuthenticationError as exc:
            raise HTTPException(401, str(exc)) from exc
        except XunhuPayPayloadError as exc:
            raise HTTPException(400, str(exc)) from exc
        except XunhuPayConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        # XunHuPay retries unless the response body is exactly this token.
        return Response(content="success", media_type="text/plain")

    def _relayer_error(exc: Exception) -> HTTPException:
        if isinstance(exc, EIP3009ConfigurationError):
            return HTTPException(503, str(exc))
        if isinstance(exc, EIP3009NotFound):
            return HTTPException(404, str(exc))
        if isinstance(exc, EIP3009Conflict):
            return HTTPException(409, str(exc))
        if isinstance(exc, EIP3009Rejected):
            return HTTPException(422, str(exc))
        if isinstance(exc, EIP3009RPCError):
            return HTTPException(503, {"code": exc.code, "message": str(exc)})
        return HTTPException(500, "An unknown Base USDC relayer error occurred")

    @app.post("/v1/payments/relayed-checkout", status_code=201)
    def create_relayed_checkout(
        body: RelayedCheckoutRequest,
        principal: AuthPrincipal = Depends(auth.current_user),  # noqa: B008
    ):
        auth.require_workspace(principal, body.workspace_id)
        if principal.development_bypass:
            raise HTTPException(403, "Gas-sponsored top-ups require a signed-in account")
        try:
            checkout = container.eip3009_relayer.prepare_checkout(
                workspace_id=body.workspace_id,
                user_id=principal.user_id,
                sku=body.sku,
                from_address=body.from_address,
            )
        except (
            EIP3009ConfigurationError,
            EIP3009Conflict,
            EIP3009Rejected,
            EIP3009RPCError,
        ) as exc:
            raise _relayer_error(exc) from exc
        return {
            "id": checkout.authorization_id,
            "payment_intent_id": checkout.payment_intent_id,
            "sku": checkout.sku,
            "amount_usdc": checkout.amount_usdc,
            "currency": "USDC",
            "credits": checkout.credits,
            "purchase_kind": checkout.purchase_kind,
            "typed_data": checkout.typed_data,
            "expires_at": checkout.expires_at,
            "gas_sponsored": True,
        }

    @app.post("/v1/workspaces/{workspace_id}/relayed-authorizations/{authorization_id}/submit")
    def submit_relayed_authorization(
        workspace_id: str,
        authorization_id: str,
        body: RelayedAuthorizationSubmitRequest,
        principal: AuthPrincipal = Depends(auth.current_user),  # noqa: B008
    ):
        auth.require_workspace(principal, workspace_id)
        try:
            result = container.eip3009_relayer.submit_authorization(
                workspace_id=workspace_id,
                user_id=principal.user_id,
                authorization_id=authorization_id,
                signature=body.signature,
            )
        except (
            EIP3009ConfigurationError,
            EIP3009Conflict,
            EIP3009NotFound,
            EIP3009Rejected,
            EIP3009RPCError,
        ) as exc:
            raise _relayer_error(exc) from exc
        return result.as_dict()

    @app.post("/v1/workspaces/{workspace_id}/relayed-authorizations/{authorization_id}/reconcile")
    def reconcile_relayed_authorization(
        workspace_id: str,
        authorization_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),  # noqa: B008
    ):
        auth.require_workspace(principal, workspace_id)
        try:
            result = container.eip3009_relayer.reconcile(
                workspace_id=workspace_id,
                user_id=principal.user_id,
                authorization_id=authorization_id,
            )
        except (
            EIP3009ConfigurationError,
            EIP3009Conflict,
            EIP3009NotFound,
            EIP3009Rejected,
            EIP3009RPCError,
        ) as exc:
            raise _relayer_error(exc) from exc
        return result.as_dict()

    @app.get("/v1/workspaces/{workspace_id}/relayed-authorizations/{authorization_id}")
    def get_relayed_authorization(
        workspace_id: str,
        authorization_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),  # noqa: B008
    ):
        auth.require_workspace(principal, workspace_id)
        try:
            result = container.eip3009_relayer.get_authorization(
                workspace_id=workspace_id,
                user_id=principal.user_id,
                authorization_id=authorization_id,
            )
        except EIP3009NotFound as exc:
            raise _relayer_error(exc) from exc
        return result.as_dict()

    @app.post("/v1/workspaces/{workspace_id}/relayed-authorizations/{authorization_id}/cancel")
    def cancel_relayed_authorization(
        workspace_id: str,
        authorization_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),  # noqa: B008
    ):
        auth.require_workspace(principal, workspace_id)
        try:
            result = container.eip3009_relayer.cancel(
                workspace_id=workspace_id,
                user_id=principal.user_id,
                authorization_id=authorization_id,
            )
        except (EIP3009Conflict, EIP3009NotFound) as exc:
            raise _relayer_error(exc) from exc
        return result.as_dict()

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
                raise HTTPException(404, "DePay checkout not found")
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
                    f"{Decimal(intent.raw_amount_microunits) / Decimal(1_000_000):.2f}" if intent else None
                ),
                "credits": intent.credits if intent else None,
                "purchase_kind": (intent.metadata_json or {}).get("purchase_kind") if intent else None,
                "credits_granted": checkout.credits_granted,
                "expires_at": checkout.expires_at,
                "paid_at": checkout.paid_at,
            }

    @app.get("/v1/workspaces/{workspace_id}/xunhupay-checkouts/{checkout_id}")
    def get_xunhupay_checkout(
        workspace_id: str,
        checkout_id: str,
        principal: AuthPrincipal = Depends(auth.current_user),  # noqa: B008
    ):
        auth.require_workspace(principal, workspace_id)
        with container.database.session() as session:
            checkout = session.scalar(
                select(XunhuPayCheckoutSession).where(
                    XunhuPayCheckoutSession.id == checkout_id,
                    XunhuPayCheckoutSession.workspace_id == workspace_id,
                )
            )
            if checkout is None:
                raise HTTPException(404, "XunHuPay checkout not found")
            order = session.get(OnchainPaymentIntent, checkout.payment_order_id)
            return {
                "id": checkout.id,
                "provider": "xunhupay",
                "status": checkout.status,
                "plan_id": order.sku if order else None,
                "amount": f"{Decimal(order.amount):.2f}" if order else None,
                "currency": order.currency if order else "CNY",
                "credits": order.credits if order else None,
                "purchase_kind": (order.metadata_json or {}).get("purchase_kind") if order else None,
                "credits_granted": checkout.credits_granted,
                "url": checkout.checkout_url,
                "url_qrcode": checkout.qrcode_url,
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
            raise HTTPException(403, "Wallet binding requires a signed-in account")
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
            raise HTTPException(403, "Wallet binding requires a signed-in account")
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
            raise HTTPException(422, "USDC amounts support at most 6 decimal places")
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
                raise HTTPException(404, "Payment request not found")
            return _intent_view(intent)
