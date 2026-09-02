from __future__ import annotations

from typing import Any

import pytest
from eth_abi import encode
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_utils import keccak, to_checksum_address
from fastapi.testclient import TestClient
from payment_core import BASE_NETWORKS, EIP3009RelayerService
from payment_core.eip3009 import EIP3009RPCError
from platform_shared import Settings
from production_domain.models import (
    EIP3009Authorization,
    OnchainPayment,
    PaymentOrder,
    Workspace,
    WorkspaceCreditLedgerEntry,
)
from sqlalchemy import select
from video_platform_api.container import build_container
from video_platform_api.main import create_app

TREASURY = "0x2222222222222222222222222222222222222222"
USDC = BASE_NETWORKS["BASE_MAINNET"][1]


class FakeBaseRPC:
    def __init__(self) -> None:
        self.sent_raw: list[str] = []
        self.receipt: dict[str, Any] | None = None
        self.send_failures = 0
        self.contract_wallets: set[str] = set()
        self.invalid_contract_wallets: set[str] = set()
        self.usdc_balances: dict[str, int] = {}

    def __call__(self, method: str, params: list[Any]) -> Any:
        if method == "eth_chainId":
            return "0x2105"
        if method == "eth_call":
            data = str(params[0].get("data") or "")
            if data == "0x" + keccak(text="DOMAIN_SEPARATOR()")[:4].hex():
                domain_typehash = keccak(
                    text=(
                        "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
                    )
                )
                return (
                    "0x"
                    + keccak(
                        encode(
                            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
                            [
                                domain_typehash,
                                keccak(text="USD Coin"),
                                keccak(text="2"),
                                8453,
                                to_checksum_address(USDC),
                            ],
                        )
                    ).hex()
                )
            if data.startswith("0x" + keccak(text="balanceOf(address)")[:4].hex()):
                payer = "0x" + data[-40:]
                return hex(self.usdc_balances.get(payer.lower(), 1_000_000_000))
            if data.startswith("0x" + keccak(text="isValidSignature(bytes32,bytes)")[:4].hex()):
                wallet = str(params[0].get("to") or "").lower()
                if wallet in self.invalid_contract_wallets:
                    return "0xffffffff" + "0" * 56
                return "0x1626ba7e" + "0" * 56
            return "0x" + "0" * 64
        if method == "eth_getCode":
            return "0x6001600055" if str(params[0]).lower() in self.contract_wallets else "0x"
        if method == "eth_getTransactionCount":
            return "0x7"
        if method == "eth_estimateGas":
            return "0x249f0"
        if method == "eth_getBlockByNumber":
            return {"baseFeePerGas": "0xf4240"}
        if method == "eth_maxPriorityFeePerGas":
            return "0xf4240"
        if method == "eth_getBalance":
            return "0xde0b6b3a7640000"
        if method == "eth_sendRawTransaction":
            raw = str(params[0])
            self.sent_raw.append(raw)
            if self.send_failures:
                self.send_failures -= 1
                raise EIP3009RPCError("RPC_UNAVAILABLE")
            return "0x" + keccak(bytes.fromhex(raw[2:])).hex()
        if method == "eth_getTransactionByHash":
            return None
        if method == "eth_getTransactionReceipt":
            return self.receipt
        if method == "eth_blockNumber":
            return "0x65"
        raise AssertionError(f"unexpected RPC method {method}")


def _container(tmp_path):  # type: ignore[no-untyped-def]
    relayer = Account.create("bestshiny-relayer")
    container = build_container(
        Settings(
            _env_file=None,
            database_url=f"sqlite:///{tmp_path / 'eip3009.db'}",
            storage_root=tmp_path / "media",
            public_base_url="http://testserver",
            web_origins="http://testserver",
            auth_required=True,
            platform_api_key="test-platform-key",
            deployment_environment="test",
            alchemy_network="BASE_MAINNET",
            alchemy_treasury_address=TREASURY,
            relayer_address=relayer.address,
            relayer_private_key=relayer.key.hex(),
            base_rpc_url="https://base-rpc.example.test",
            relayer_authorization_ttl_seconds=900,
        )
    )
    fake = FakeBaseRPC()
    container.eip3009_relayer._rpc = fake  # type: ignore[method-assign]
    return container, relayer, fake


def _registered(container) -> tuple[TestClient, str]:  # type: ignore[no-untyped-def]
    client = TestClient(create_app(container))
    response = client.post(
        "/api/auth/register",
        json={
            "email": "relayed-owner@example.com",
            "password": "correct-horse-battery-staple",
            "display_name": "Relayed owner",
            "workspace_name": "Relayed workspace",
        },
    )
    assert response.status_code == 201
    return client, response.json()["user"]["workspaces"][0]["id"]


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("ai_director_csrf") or ""}


def _checkout(client: TestClient, workspace_id: str, payer) -> dict:  # type: ignore[no-untyped-def]
    response = client.post(
        "/v1/payments/relayed-checkout",
        json={
            "workspace_id": workspace_id,
            "sku": "starter_20",
            "from_address": payer.address,
        },
        headers=_csrf(client),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _signature(payer, typed_data: dict[str, Any]) -> str:  # type: ignore[no-untyped-def]
    signed = Account.sign_message(encode_typed_data(full_message=typed_data), payer.key)
    return "0x" + bytes(signed.signature).hex()


def _receipt(checkout: dict, tx_hash: str, *, amount: int | None = None) -> dict[str, Any]:
    message = checkout["typed_data"]["message"]
    payer = str(message["from"]).lower()
    treasury = str(message["to"]).lower()
    value = int(message["value"] if amount is None else amount)
    from_topic = "0x" + payer[2:].rjust(64, "0")
    to_topic = "0x" + treasury[2:].rjust(64, "0")
    return {
        "transactionHash": tx_hash,
        "to": USDC,
        "status": "0x1",
        "blockNumber": "0x64",
        "gasUsed": "0x249f0",
        "effectiveGasPrice": "0x1e8480",
        "logs": [
            {
                "address": USDC,
                "logIndex": "0x0",
                "topics": [
                    "0x" + keccak(text="AuthorizationUsed(address,bytes32)").hex(),
                    from_topic,
                    str(message["nonce"]).lower(),
                ],
                "data": "0x",
            },
            {
                "address": USDC,
                "logIndex": "0x1",
                "topics": [
                    "0x" + keccak(text="Transfer(address,address,uint256)").hex(),
                    from_topic,
                    to_topic,
                ],
                "data": hex(value),
            },
        ],
    }


def test_relayer_pays_gas_and_exact_usdc_purchase_fulfills_once(tmp_path) -> None:
    container, relayer, fake = _container(tmp_path)
    client, workspace_id = _registered(container)
    payer = Account.create("bestshiny-payer")

    config = client.get("/v1/payments/config")
    assert config.status_code == 200
    assert config.json()["checkout_provider"] == "EIP3009_RELAYER"
    assert config.json()["gas_sponsored"] is True

    checkout = _checkout(client, workspace_id, payer)
    assert checkout["amount_usdc"] == "20.00"
    assert checkout["credits"] == 1_800
    assert checkout["gas_sponsored"] is True
    assert checkout["typed_data"]["domain"] == {
        "name": "USD Coin",
        "version": "2",
        "chainId": 8453,
        "verifyingContract": USDC,
    }
    submit = client.post(
        f"/v1/workspaces/{workspace_id}/relayed-authorizations/{checkout['id']}/submit",
        json={"signature": _signature(payer, checkout["typed_data"])},
        headers=_csrf(client),
    )
    assert submit.status_code == 200, submit.text
    assert submit.json()["status"] == "SUBMITTED"
    assert len(fake.sent_raw) == 1
    assert Account.recover_transaction(fake.sent_raw[0]).lower() == relayer.address.lower()

    tx_hash = submit.json()["transaction_hash"]
    fake.receipt = _receipt(checkout, tx_hash)
    confirmed = client.post(
        f"/v1/workspaces/{workspace_id}/relayed-authorizations/{checkout['id']}/reconcile",
        json={},
        headers=_csrf(client),
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "CONFIRMED"
    assert confirmed.json()["credits_granted"] == 1_800

    replay = client.post(
        f"/v1/workspaces/{workspace_id}/relayed-authorizations/{checkout['id']}/reconcile",
        json={},
        headers=_csrf(client),
    )
    assert replay.status_code == 200
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        order = session.scalar(select(PaymentOrder))
        authorization = session.scalar(select(EIP3009Authorization))
        payment = session.scalar(select(OnchainPayment))
        ledger = list(session.scalars(select(WorkspaceCreditLedgerEntry)))
        assert workspace is not None
        assert (workspace.plan_tier, workspace.credit_balance) == ("PRO", 1_850)
        assert order is not None and order.status == "PAID"
        assert order.provider == "EIP3009_RELAYER"
        assert authorization is not None and authorization.status == "CONFIRMED"
        assert payment is not None and payment.status == "CREDITED"
        assert len(ledger) == 1
        assert ledger[0].metadata_json["gas_sponsored"] is True
        assert ledger[0].metadata_json["relayer_address"] == relayer.address.lower()


def test_wrong_wallet_signature_is_rejected_before_any_relayer_transaction(tmp_path) -> None:
    container, _relayer, fake = _container(tmp_path)
    client, workspace_id = _registered(container)
    payer = Account.create("payer")
    attacker = Account.create("attacker")
    checkout = _checkout(client, workspace_id, payer)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/relayed-authorizations/{checkout['id']}/submit",
        json={"signature": _signature(attacker, checkout["typed_data"])},
        headers=_csrf(client),
    )
    assert response.status_code == 422
    assert fake.sent_raw == []
    with container.database.session() as session:
        assert session.scalar(select(PaymentOrder)).status == "PENDING"
        assert session.scalar(select(EIP3009Authorization)).signature_hash is None


def test_insufficient_usdc_is_rejected_before_requesting_a_signature(tmp_path) -> None:
    container, _relayer, fake = _container(tmp_path)
    client, workspace_id = _registered(container)
    payer = Account.create("underfunded-payer")
    fake.usdc_balances[payer.address.lower()] = 545_686

    response = client.post(
        "/v1/payments/relayed-checkout",
        json={
            "workspace_id": workspace_id,
            "sku": "starter_20",
            "from_address": payer.address,
        },
        headers=_csrf(client),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Base USDC 余额不足：当前连接钱包有 0.545686 USDC，需要 20 USDC"
    )
    with container.database.session() as session:
        assert session.scalar(select(PaymentOrder)) is None
        assert session.scalar(select(EIP3009Authorization)) is None


def test_balance_is_checked_again_before_relayer_broadcast(tmp_path) -> None:
    container, _relayer, fake = _container(tmp_path)
    client, workspace_id = _registered(container)
    payer = Account.create("payer-spent-after-checkout")
    checkout = _checkout(client, workspace_id, payer)
    fake.usdc_balances[payer.address.lower()] = 0

    response = client.post(
        f"/v1/workspaces/{workspace_id}/relayed-authorizations/{checkout['id']}/submit",
        json={"signature": _signature(payer, checkout["typed_data"])},
        headers=_csrf(client),
    )

    assert response.status_code == 422
    assert "当前连接钱包有 0 USDC，需要 20 USDC" in response.json()["detail"]
    assert fake.sent_raw == []
    with container.database.session() as session:
        assert session.scalar(select(PaymentOrder)).status == "PENDING"
        assert session.scalar(select(EIP3009Authorization)).status == "PENDING"


def test_deployed_erc1271_smart_wallet_signature_is_relayed(tmp_path) -> None:
    container, _relayer, fake = _container(tmp_path)
    client, workspace_id = _registered(container)
    smart_wallet = "0x3333333333333333333333333333333333333333"
    fake.contract_wallets.add(smart_wallet)
    checkout = _checkout(client, workspace_id, type("Wallet", (), {"address": smart_wallet})())

    response = client.post(
        f"/v1/workspaces/{workspace_id}/relayed-authorizations/{checkout['id']}/submit",
        json={"signature": "0x" + "ab" * 96},
        headers=_csrf(client),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "SUBMITTED"
    assert len(fake.sent_raw) == 1


def test_invalid_erc1271_signature_is_rejected_before_broadcast(tmp_path) -> None:
    container, _relayer, fake = _container(tmp_path)
    client, workspace_id = _registered(container)
    smart_wallet = "0x4444444444444444444444444444444444444444"
    fake.contract_wallets.add(smart_wallet)
    fake.invalid_contract_wallets.add(smart_wallet)
    checkout = _checkout(client, workspace_id, type("Wallet", (), {"address": smart_wallet})())

    response = client.post(
        f"/v1/workspaces/{workspace_id}/relayed-authorizations/{checkout['id']}/submit",
        json={"signature": "0x" + "cd" * 96},
        headers=_csrf(client),
    )
    assert response.status_code == 422
    assert fake.sent_raw == []


def test_duplicate_submit_reuses_the_same_transaction_without_spending_gas_twice(tmp_path) -> None:
    container, _relayer, fake = _container(tmp_path)
    client, workspace_id = _registered(container)
    payer = Account.create("payer")
    checkout = _checkout(client, workspace_id, payer)
    signature = _signature(payer, checkout["typed_data"])
    path = f"/v1/workspaces/{workspace_id}/relayed-authorizations/{checkout['id']}/submit"

    first = client.post(path, json={"signature": signature}, headers=_csrf(client))
    second = client.post(path, json={"signature": signature}, headers=_csrf(client))
    assert first.status_code == second.status_code == 200
    assert first.json()["transaction_hash"] == second.json()["transaction_hash"]
    assert len(fake.sent_raw) == 1


def test_uncertain_broadcast_is_durably_retried_with_identical_transaction_bytes(tmp_path) -> None:
    container, _relayer, fake = _container(tmp_path)
    client, workspace_id = _registered(container)
    payer = Account.create("payer")
    checkout = _checkout(client, workspace_id, payer)
    signature = _signature(payer, checkout["typed_data"])
    path = f"/v1/workspaces/{workspace_id}/relayed-authorizations/{checkout['id']}/submit"
    fake.send_failures = 1

    uncertain = client.post(path, json={"signature": signature}, headers=_csrf(client))
    assert uncertain.status_code == 503
    with container.database.session() as session:
        authorization = session.scalar(select(EIP3009Authorization))
        assert authorization is not None and authorization.status == "SUBMITTING"
        assert authorization.raw_transaction == fake.sent_raw[0]
        expected_hash = authorization.transaction_hash

    recovered = client.post(path, json={"signature": signature}, headers=_csrf(client))
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "SUBMITTED"
    assert recovered.json()["transaction_hash"] == expected_hash
    assert fake.sent_raw == [fake.sent_raw[0], fake.sent_raw[0]]


def test_uncertain_transaction_is_not_rebroadcast_after_authorization_expires(tmp_path) -> None:
    container, _relayer, fake = _container(tmp_path)
    client, workspace_id = _registered(container)
    payer = Account.create("payer")
    checkout = _checkout(client, workspace_id, payer)
    signature = _signature(payer, checkout["typed_data"])
    path = f"/v1/workspaces/{workspace_id}/relayed-authorizations/{checkout['id']}/submit"
    fake.send_failures = 1
    assert client.post(path, json={"signature": signature}, headers=_csrf(client)).status_code == 503
    with container.database.session() as session:
        authorization = session.scalar(select(EIP3009Authorization))
        assert authorization is not None
        authorization.valid_after = 0
        authorization.valid_before = 1

    expired = client.post(path, json={"signature": signature}, headers=_csrf(client))
    assert expired.status_code == 200
    assert expired.json()["status"] == "EXPIRED"
    assert len(fake.sent_raw) == 1


def test_wrong_receipt_amount_never_posts_credits(tmp_path) -> None:
    container, _relayer, fake = _container(tmp_path)
    client, workspace_id = _registered(container)
    payer = Account.create("payer")
    checkout = _checkout(client, workspace_id, payer)
    submit = client.post(
        f"/v1/workspaces/{workspace_id}/relayed-authorizations/{checkout['id']}/submit",
        json={"signature": _signature(payer, checkout["typed_data"])},
        headers=_csrf(client),
    )
    fake.receipt = _receipt(checkout, submit.json()["transaction_hash"], amount=19_999_999)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/relayed-authorizations/{checkout['id']}/reconcile",
        json={},
        headers=_csrf(client),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "RECONCILIATION_REQUIRED"
    with container.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None and workspace.credit_balance == 50
        assert session.scalar(select(WorkspaceCreditLedgerEntry)) is None


def test_alchemy_observation_arriving_first_is_adopted_without_double_credit(tmp_path) -> None:
    container, _relayer, fake = _container(tmp_path)
    client, workspace_id = _registered(container)
    payer = Account.create("payer")
    checkout = _checkout(client, workspace_id, payer)
    submit = client.post(
        f"/v1/workspaces/{workspace_id}/relayed-authorizations/{checkout['id']}/submit",
        json={"signature": _signature(payer, checkout["typed_data"])},
        headers=_csrf(client),
    )
    tx_hash = submit.json()["transaction_hash"]
    fake.receipt = _receipt(checkout, tx_hash)
    with container.database.session() as session:
        session.add(
            OnchainPayment(
                network="BASE_MAINNET",
                chain_id=8453,
                transaction_hash=tx_hash,
                log_index="0x1",
                block_number="0x64",
                from_address=payer.address.lower(),
                to_address=TREASURY,
                token_address=USDC,
                token_decimals=6,
                raw_amount_microunits=20_000_000,
                workspace_id=None,
                wallet_binding_id=None,
                payment_intent_id=None,
                provider_event_id="alchemy:first",
                credits_granted=0,
                status="UNMATCHED",
                metadata_json={"source": "ALCHEMY"},
            )
        )

    response = client.post(
        f"/v1/workspaces/{workspace_id}/relayed-authorizations/{checkout['id']}/reconcile",
        json={},
        headers=_csrf(client),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "CONFIRMED"
    with container.database.session() as session:
        payments = list(session.scalars(select(OnchainPayment)))
        ledger = list(session.scalars(select(WorkspaceCreditLedgerEntry)))
        assert len(payments) == len(ledger) == 1
        assert payments[0].workspace_id == workspace_id
        assert payments[0].status == "CREDITED"


def test_user_can_cancel_an_unsigned_authorization(tmp_path) -> None:
    container, _relayer, fake = _container(tmp_path)
    client, workspace_id = _registered(container)
    payer = Account.create("payer")
    checkout = _checkout(client, workspace_id, payer)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/relayed-authorizations/{checkout['id']}/cancel",
        json={},
        headers=_csrf(client),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert fake.sent_raw == []
    with container.database.session() as session:
        assert session.scalar(select(PaymentOrder)).status == "CANCELLED"


def test_worker_sweep_confirms_without_browser_polling(tmp_path) -> None:
    container, _relayer, fake = _container(tmp_path)
    client, workspace_id = _registered(container)
    payer = Account.create("payer")
    checkout = _checkout(client, workspace_id, payer)
    submit = client.post(
        f"/v1/workspaces/{workspace_id}/relayed-authorizations/{checkout['id']}/submit",
        json={"signature": _signature(payer, checkout["typed_data"])},
        headers=_csrf(client),
    )
    fake.receipt = _receipt(checkout, submit.json()["transaction_hash"])

    result = container.eip3009_relayer.sweep(limit=50)
    assert result.confirmed == 1
    with container.database.session() as session:
        assert session.scalar(select(PaymentOrder)).status == "PAID"
        assert session.scalar(select(WorkspaceCreditLedgerEntry)).credits == 1_800


def test_private_key_must_match_the_configured_relayer_address(tmp_path) -> None:
    account = Account.create("relayer")
    with pytest.raises(ValueError, match="does not match"):
        EIP3009RelayerService(
            database=_container(tmp_path)[0].database,
            relayer_address="0x1111111111111111111111111111111111111111",
            relayer_private_key=account.key.hex(),
            rpc_url="https://base-rpc.example.test",
            treasury_address=TREASURY,
        )
