"""The wallet pays into the workspace the open project is charged against, and
a lost /submit answer is never reported as "nothing was transferred".

Two 2026-09-06 audit findings, both in ``wallet.js``:

1. The wallet chose the first workspace the user owns or administers and the
   credits pill separately took the first workspace listed - neither the one
   the open project belongs to - so a member of several workspaces could top
   up A, generate in B, and read a balance from C. The wallet now binds to the
   project's workspace whenever the user is a member of it, follows every
   project switch, and says on the sheet who receives the credits.
2. When ``POST .../submit`` had been processed but its answer was lost, the
   page said "no USDC was transferred" and never asked the server about the
   order, so the user paid again. The signed order is now kept, the server is
   asked what became of it, a submission it holds is followed to settlement,
   one it never applied is resent with the same signature, and an unreachable
   server leaves the result pending - never "not paid".

The shipped functions are lifted out of ``wallet.js`` and executed in Node
against stubs, so what is asserted is the served source.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "apps" / "web"
WALLET_JS = (WEB / "wallet.js").read_text(encoding="utf-8")
APP_JS = (WEB / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (WEB / "index.html").read_text(encoding="utf-8")


def _function_source(name: str, source: str = WALLET_JS) -> str:
    for opener in (f"\nfunction {name}(", f"\nasync function {name}("):
        start = source.find(opener)
        if start != -1:
            break
    assert start != -1, f"the module no longer defines {name}()"
    start += 1
    depth = 0
    index = source.index("{", start)
    for position in range(index, len(source)):
        character = source[position]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[start : position + 1]
    raise AssertionError(f"{name}() is not brace-balanced")


def _const_source(name: str, source: str = WALLET_JS) -> str:
    """One top-level ``const NAME = …;`` - an object, an array or one line."""

    start = source.find(f"\nconst {name} = ")
    assert start != -1, f"the module no longer defines {name}"
    start += 1
    value_at = start + len(f"const {name} = ")
    if source[value_at] in "{[":
        opener, closer = source[value_at], "}" if source[value_at] == "{" else "]"
        depth = 0
        for position in range(value_at, len(source)):
            if source[position] == opener:
                depth += 1
            elif source[position] == closer:
                depth -= 1
                if depth == 0:
                    return source[start : source.index(";", position) + 1]
        raise AssertionError(f"{name} is not balanced")
    return source[start : source.index(";\n", value_at) + 1]


# Polling runs through one engine since the 2026-09-07 review; the functions
# that follow an order are lifted together with it.
POLL_ENGINE = "\n".join(
    [
        _const_source(name)
        for name in ("POLL_INTERVAL_MS", "POLL_RETRY_STEPS_MS", "POLL_MAX_FAILURES", "POLL_WAITING_COPY")
    ]
    + [_function_source(name) for name in ("stopPolling", "followOrder")]
)


def _run(tmp_path: Path, script: str) -> dict:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to execute the shipped front-end functions")
    path = tmp_path / "wallet-harness.mjs"
    path.write_text(script, encoding="utf-8")
    completed = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=60, check=False)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------
# 1. Which workspace a top-up credits.
# --------------------------------------------------------------------------
CHOOSE_HARNESS = """
%(choose)s
const paymentState = { billing: null };
const user = { workspaces: [
  { id: "w-owned", role: "OWNER", name: "Mine" },
  { id: "w-member", role: "MEMBER", name: "The studio" },
  { id: "w-admin", role: "ADMIN", name: "Client" },
] };
console.log(JSON.stringify({
  project_in_member_workspace: chooseWorkspace(user, "w-member")?.id,
  project_in_admin_workspace: chooseWorkspace(user, "w-admin")?.id,
  no_project: chooseWorkspace(user)?.id,
  project_in_a_workspace_the_user_left: chooseWorkspace(user, "w-elsewhere")?.id,
  signed_out: chooseWorkspace(null, "w-member"),
}));
"""


def test_the_wallet_binds_to_the_open_projects_workspace(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _run(tmp_path, CHOOSE_HARNESS % {"choose": _function_source("chooseWorkspace")})
    assert result["project_in_member_workspace"] == "w-member", (
        "membership is enough: that is where the credits are spent"
    )
    assert result["project_in_admin_workspace"] == "w-admin"
    assert result["no_project"] == "w-owned", "without a project, the old order: owned or administered first"
    assert result["project_in_a_workspace_the_user_left"] == "w-owned"
    assert result["signed_out"] is None


def test_the_credits_pill_reads_the_same_workspace() -> None:
    source = APP_JS[APP_JS.index("function currentWorkspaceId(") :]
    source = source[: source.index("\n}") + 2]
    assert "state.project?.workspace_id" in source
    load = APP_JS[APP_JS.index("async function loadCredits(") :]
    load = load[: load.index("\n}") + 2]
    assert "currentWorkspaceId()" in load
    assert "workspaces || [])[0]" not in load, "the first-listed workspace is no longer the pill's authority"


def test_every_project_switch_is_announced_to_the_wallet() -> None:
    select = APP_JS[APP_JS.index("async function selectProject(") :]
    select = select[: select.index("\nfunction ") ]
    assert "announceWorkspace(project.workspace_id)" in select
    assert '"ai-director:workspace-changed"' in APP_JS
    assert 'window.addEventListener("ai-director:workspace-changed"' in WALLET_JS
    assert "bindProjectWorkspace(" in WALLET_JS


def test_the_sheet_names_who_receives_the_credits() -> None:
    assert 'id="walletRecipient"' in INDEX_HTML
    facts = INDEX_HTML[INDEX_HTML.index('class="wallet-confirm-facts"') :][:800]
    assert "Credits go to" in facts
    render = _function_source("render")
    assert 'element("walletRecipient").textContent = workspaceLabel(paymentState.workspace)' in render
    success = _function_source("showPaymentSuccess")
    assert "workspaceLabel(paymentState.workspace)" in success


def test_a_balance_that_arrives_after_a_switch_is_not_painted(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = """
%(refresh)s
const paymentState = { workspace: { id: "w-a" }, billing: null };
const rendered = [];
function render() { rendered.push(paymentState.billing?.workspace_id); }
async function api() {
  paymentState.workspace = { id: "w-b" };
  return { workspace_id: "w-a", plan_tier: "PRO", credit_balance: 999 };
}
await refreshBilling();
console.log(JSON.stringify({ billing: paymentState.billing, rendered }));
"""
    result = _run(tmp_path, harness % {"refresh": _function_source("refreshBilling")})
    assert result["billing"] is None and result["rendered"] == []


# --------------------------------------------------------------------------
# 2. A lost /submit answer.
# --------------------------------------------------------------------------
RECOVERY_HARNESS = """
%(functions)s

const paymentState = {
  workspace: { id: "w1" }, checkout: { id: "auth-1", sku: "starter_20" }, pendingSubmission: null,
};
const messages = [];
const polls = [];
function setMessage(message = "", error = "") { messages.push({ message, error }); }
function pollRelayedAuthorization(id) { polls.push(id); }
const PAYMENT_FAILURE = {
  EXPIRED: "This payment expired before it was confirmed.",
  CANCELLED: "This payment was cancelled.",
  FAILED: "This payment did not go through.",
  RECONCILIATION_REQUIRED: "We could not confirm this payment automatically.",
};
const failureReason = (status) => PAYMENT_FAILURE[status] || "This payment did not complete.";
const paymentFailureMessage = (status) => `${failureReason(status)} No credits were added.`
  + " If money left your wallet, contact us and we will restore it.";
const lookup = %(lookup)s;
const calls = [];
async function api(path, options = {}) {
  calls.push(`${options.method || "GET"} ${path}`);
  if (lookup === "unreachable") throw new Error("Failed to fetch");
  return { id: "auth-1", status: lookup, transaction_hash: null, credits_granted: 0 };
}
const failure = new Error(%(reason)s);
if (%(status)s) failure.status = %(status)s;
const outcome = await recoverRelayedSubmission(paymentState.checkout, "0xsig", failure);
console.log(JSON.stringify({ outcome, messages, polls, calls, pending: paymentState.pendingSubmission }));
"""


def _recover(
    tmp_path: Path, lookup: str, *, reason: str = "Failed to fetch", status: int | None = None
) -> dict:
    resend = (
        'const RESEND_MESSAGE = "The signed authorization was not submitted;'
        ' nothing has left your wallet. Press Confirm to send it again — no new signature is needed.";'
    )
    functions = "\n".join([resend, _function_source("recoverRelayedSubmission")])
    return _run(
        tmp_path,
        RECOVERY_HARNESS
        % {
            "functions": functions,
            "lookup": json.dumps(lookup),
            "reason": json.dumps(reason),
            "status": json.dumps(status),
        },
    )


def _all_text(result: dict) -> str:
    return " ".join(f"{item['message']} {item['error']}" for item in result["messages"])


@pytest.mark.parametrize("held", ["SUBMITTING", "SUBMITTED", "CONFIRMED"])
def test_a_submission_the_server_holds_is_followed_not_denied(tmp_path, held) -> None:  # type: ignore[no-untyped-def]
    result = _recover(tmp_path, held)
    assert result["outcome"] == held
    assert result["calls"] == ["GET /v1/workspaces/w1/relayed-authorizations/auth-1"], (
        "the original order is asked about first"
    )
    assert result["polls"] == ["auth-1"], "and then followed to settlement"
    assert "no USDC was transferred" not in _all_text(result)
    assert "do not pay again" in _all_text(result)
    assert result["pending"] is None


def test_a_submission_the_server_never_applied_is_kept_for_resending(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _recover(tmp_path, "PENDING")
    assert result["outcome"] == "PENDING"
    assert result["polls"] == []
    assert result["pending"] == {"checkoutId": "auth-1", "signature": "0xsig"}
    text = _all_text(result)
    assert "not submitted" in text and "no new signature is needed" in text
    assert "Failed to fetch" in text, "the reason is shown, not hidden"


def test_a_conflict_from_the_server_is_never_answered_with_a_resend(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """409 is the relayer refusing the order as it stands - among its reasons,
    a USDC authorization already used on chain, where money did move."""

    result = _recover(
        tmp_path, "PENDING", reason="This USDC authorization nonce has already been used", status=409
    )
    assert result["outcome"] == "PENDING"
    assert result["pending"] is None, "resending cannot help and must not be offered"
    text = _all_text(result)
    assert "already been used" in text
    assert "If money left your wallet, contact us" in text
    assert "nothing has left your wallet" not in text


def test_an_unreachable_server_leaves_the_result_pending(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _recover(tmp_path, "unreachable")
    assert result["outcome"] == "PENDING_CONFIRMATION"
    text = _all_text(result)
    assert "pending confirmation" in text.lower()
    assert "auth-1" in text, "the order id is on the page"
    assert "Do not pay again" in text
    assert "no USDC was transferred" not in text
    assert result["polls"] == ["auth-1"], "it keeps asking"
    assert result["pending"] == {"checkoutId": "auth-1", "signature": "0xsig"}


@pytest.mark.parametrize("status", ["EXPIRED", "CANCELLED", "FAILED", "RECONCILIATION_REQUIRED"])
def test_a_settled_failure_is_reported_as_the_server_says(tmp_path, status) -> None:  # type: ignore[no-untyped-def]
    result = _recover(tmp_path, status)
    assert result["outcome"] == status
    assert "No credits were added" in _all_text(result)
    assert result["pending"] is None


def test_the_checkout_flow_asks_before_claiming_anything_after_signing() -> None:
    source = _function_source("createRelayedCheckout")
    # Once a signature exists, the only path out of a failure is the recovery.
    assert "if (signature && checkout?.id) {" in source
    assert "await recoverRelayedSubmission(checkout, signature, error);" in source
    # The "nothing was transferred" sentence is reachable only before signing.
    before, after = source.split("recoverRelayedSubmission(checkout, signature, error)")
    assert "no USDC was transferred" not in before
    assert "no USDC was transferred" in after


def test_a_kept_submission_is_resent_with_the_same_signature(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = """
%(functions)s
const paymentState = {
  workspace: { id: "w1" }, checkout: { id: "auth-1", sku: "starter_20" },
  pendingSubmission: { checkoutId: "auth-1", signature: "0xsig" }, busy: false,
};
const calls = [];
const messages = [];
function selectedPackage() { return { sku: "starter_20" }; }
function setBusy() {}
function setMessage(message = "", error = "") { messages.push({ message, error }); }
function clearWalletConnectQr() {}
function pollRelayedAuthorization(id) { calls.push(`poll ${id}`); }
function workspaceLabel() { return "Mine"; }
function connectBaseWallet() { throw new Error("must not reconnect the wallet for a resend"); }
async function api(path, options = {}) {
  calls.push(`${options.method || "GET"} ${path} ${options.body || ""}`);
  return { status: "SUBMITTED" };
}
await createRelayedCheckout("qr");
console.log(JSON.stringify({ calls, pending: paymentState.pendingSubmission, messages }));
"""
    functions = "\n".join(
        _function_source(name) for name in ("createRelayedCheckout", "submitRelayedAuthorization")
    )
    result = _run(tmp_path, harness % {"functions": functions})
    assert result["calls"] == [
        'POST /v1/workspaces/w1/relayed-authorizations/auth-1/submit {"signature":"0xsig"}',
        "poll auth-1",
    ], "the same order, the same signature, no new checkout and no new signature"
    assert result["pending"] is None


def test_sign_out_forgets_the_previous_accounts_wallet(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = """
%(functions)s
const nodes = new Proxy({}, {
  get(target, id) {
    if (!(id in target)) {
      target[id] = { textContent: "old", hidden: false, replaceChildren() {}, classList: { remove() {} } };
    }
    return target[id];
  },
});
const element = (id) => nodes[id];
const paymentState = {
  user: { id: "alice" }, workspace: { id: "w1" }, projectWorkspaceId: null, billing: { credit_balance: 5 },
  checkout: { id: "c" }, pendingSubmission: { checkoutId: "c", signature: "0x" }, walletAccount: "0xabc",
  pollTimer: 1, unmountWidget: null, config: {},
};
function chooseWorkspace() { return null; }
function discardWidget() {}
function clearWalletConnectQr() {}
function clearXunhuPayQr() {}
function setMessage() {}
function resetWalletView() {}
function render() {}
const window = { clearTimeout() {} };
paymentState.pollGeneration = 0;
await initializeForUser(null);
console.log(JSON.stringify({
  state: {
    user: paymentState.user, workspace: paymentState.workspace, billing: paymentState.billing,
    checkout: paymentState.checkout, pending: paymentState.pendingSubmission,
    account: paymentState.walletAccount,
  },
  receipt: ["walletSuccessPlan", "walletSuccessAmount", "walletSuccessCredits", "walletSuccessBalance"]
    .map((id) => nodes[id].textContent),
  pill: nodes.creditsAmount.textContent,
}));
"""
    result = _run(tmp_path, harness % {
        "functions": "\n".join(_function_source(name) for name in ("initializeForUser", "stopPolling")),
    })
    assert result["state"] == {
        "user": None, "workspace": None, "billing": None, "checkout": None, "pending": None, "account": "",
    }
    assert result["receipt"] == ["—", "—", "— Credits", "—"]
    assert result["pill"] == "—"


def test_a_poll_that_finds_the_order_unsubmitted_hands_it_back_to_the_user(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """After an outage the poll reaches the server and learns the order is still
    PENDING: the kept signature is the way to finish, not another 3-second poll."""

    harness = """
const RESEND_MESSAGE = "resend";
%(functions)s
const paymentState = {
  workspace: { id: "w1" }, pendingSubmission: { checkoutId: "auth-1", signature: "0xsig" },
  pollTimer: null, billing: null, pollGeneration: 0, poll: null, stalledPoll: null,
};
const messages = [];
const timers = [];
const element = () => ({ hidden: true });
const window = { clearTimeout() {}, setTimeout(fn, ms) { timers.push(ms); return 1; }, dispatchEvent() {} };
function setMessage(message = "", error = "") { messages.push({ message, error }); }
function paymentFailureMessage(status) { return `failed:${status}`; }
async function refreshBilling() {}
function showPaymentSuccess() {}
async function api() { return { status: "PENDING" }; }
await pollRelayedAuthorization("auth-1");
console.log(JSON.stringify({ messages, timers, pending: paymentState.pendingSubmission }));
"""
    result = _run(tmp_path, harness % {
        "functions": POLL_ENGINE + "\n" + "\n".join(
            _function_source(name) for name in ("pollRelayedAuthorization", "pollRelayedAuthorizationOnce")
        ),
    })
    assert result["timers"] == [], "polling stops"
    assert result["messages"] == [{"message": "resend", "error": ""}]
    assert result["pending"] == {"checkoutId": "auth-1", "signature": "0xsig"}
