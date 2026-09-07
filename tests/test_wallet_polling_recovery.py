"""Following an order to settlement survives a dropped request, and stops when the sheet closes.

Two 2026-09-07 review findings in ``wallet.js``:

1. the DePay status check stopped for good on one network error - the
   exception branch logged and scheduled nothing - so a customer who had
   paid never saw the credits arrive;
2. closing the sheet only cleared the timer: a request already in flight
   scheduled the next round on return, and an order or account switched
   under it could be painted with a stale answer.

Polling now runs through one engine (``followOrder``): a tick that throws is
retried under a growing delay and, after enough failures in a row, parks
behind a "Check payment status again" button that resumes the same order;
every poll belongs to a generation that closing, signing out or starting
another order bumps, and a tick consults it after every await. Close, "Start
creating" and Escape all end at the dialog's close event.

The shipped functions are lifted out of ``wallet.js`` by name and executed in
Node against stubs, so what is asserted is the served source.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "apps" / "web"
WALLET_JS = (WEB / "wallet.js").read_text(encoding="utf-8")
INDEX_HTML = (WEB / "index.html").read_text(encoding="utf-8")


def _balanced(source: str, start: int, opener: str, closer: str) -> int:
    depth = 0
    for position in range(start, len(source)):
        character = source[position]
        if character == opener:
            depth += 1
        elif character == closer:
            depth -= 1
            if depth == 0:
                return position + 1
    raise AssertionError("unbalanced source")


def _function_source(name: str, source: str = WALLET_JS) -> str:
    for opener in (f"\nfunction {name}(", f"\nasync function {name}("):
        start = source.find(opener)
        if start != -1:
            break
    assert start != -1, f"the module no longer defines {name}()"
    start += 1
    parameters_end = _balanced(source, source.index("(", start), "(", ")")
    return source[start : _balanced(source, source.index("{", parameters_end), "{", "}")]


def _const_source(name: str, source: str = WALLET_JS) -> str:
    start = source.find(f"\nconst {name} = ")
    assert start != -1, f"the module no longer defines {name}"
    start += 1
    value_at = start + len(f"const {name} = ")
    if source[value_at] in "{[":
        closer = "}" if source[value_at] == "{" else "]"
        end = source.index(";", _balanced(source, value_at, source[value_at], closer)) + 1
    else:
        end = source.index(";\n", value_at) + 1
    return source[start:end]


ENGINE = "\n".join(
    [
        _const_source(name)
        for name in ("POLL_INTERVAL_MS", "POLL_RETRY_STEPS_MS", "POLL_MAX_FAILURES", "POLL_WAITING_COPY")
    ]
    + [_function_source(name) for name in ("stopPolling", "followOrder", "resumeStalledPoll")]
)


def _run(tmp_path: Path, script: str) -> dict:
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node is required to execute the shipped front-end functions")
    path = tmp_path / "harness.mjs"
    path.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        [node, str(path)], capture_output=True, text=True, timeout=60, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


PRELUDE = """
%(engine)s
const paymentState = { pollGeneration: 0, poll: null, stalledPoll: null, pollTimer: null };
const timers = [];
const scheduled = [];
const messages = [];
const recheck = { hidden: true };
const window = {
  setTimeout(fn, ms) { timers.push(ms); scheduled.push(fn); return timers.length; },
  clearTimeout() {},
};
const element = () => recheck;
function setMessage(message = "", error = "") { messages.push({ message, error }); }
"""


def test_a_failed_status_check_is_retried_under_backoff_not_abandoned(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = PRELUDE + """
let attempts = 0;
async function tick() { attempts += 1; throw new Error("offline"); }
await followOrder("depay", "chk-1", tick);
console.log(JSON.stringify({
  timers, messages, attempts, following: paymentState.poll?.id,
  failures: paymentState.poll?.failures, recheck: recheck.hidden,
}));
"""
    result = _run(tmp_path, harness % {"engine": ENGINE})
    assert result["attempts"] == 1
    assert result["timers"] == [5000], "the next check is scheduled, later than the normal cadence"
    assert result["messages"] == [{"message": "Waiting for the payment to be confirmed…", "error": "offline"}]
    assert result["following"] == "chk-1" and result["failures"] == 1
    assert result["recheck"] is True, "no manual entry while the poll still runs itself"


def test_after_enough_failures_the_poll_parks_and_check_again_resumes_it(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = PRELUDE + """
let attempts = 0;
let outcome = "offline";
async function tick() {
  attempts += 1;
  if (outcome === "offline") throw new Error("offline");
  return false;
}
await followOrder("depay", "chk-1", tick);
while (scheduled.length) { const next = scheduled.shift(); await next(); }
const parked = {
  timers: [...timers], attempts, poll: paymentState.poll, stalled: paymentState.stalledPoll?.id,
  recheck: recheck.hidden, last: messages[messages.length - 1],
};
outcome = "paid";
resumeStalledPoll();
await new Promise((resolve) => setTimeout(resolve, 0));
console.log(JSON.stringify({
  parked,
  resumed: { attempts, stalled: paymentState.stalledPoll, poll: paymentState.poll, recheck: recheck.hidden },
}));
"""
    result = _run(tmp_path, harness % {"engine": ENGINE})
    parked = result["parked"]
    assert parked["timers"] == [5000, 8000, 13000, 20000, 30000, 30000]
    assert parked["attempts"] == 7
    assert parked["poll"] is None and parked["stalled"] == "chk-1"
    assert parked["recheck"] is False, "the manual way back is offered once the poll parks"
    assert "Do not pay again" in parked["last"]["message"] and "chk-1" in parked["last"]["message"]
    resumed = result["resumed"]
    assert resumed["attempts"] == 8, "Check again runs the same order's check"
    assert resumed["stalled"] is None and resumed["poll"] is None and resumed["recheck"] is True


def test_a_check_in_flight_when_the_sheet_closes_drops_its_answer(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = PRELUDE + """
let release;
let sideEffects = 0;
async function tick(id, current) {
  await new Promise((resolve) => { release = resolve; });
  if (!current()) return false;
  sideEffects += 1;
  return true;
}
const started = followOrder("depay", "chk-2", tick);
await new Promise((resolve) => setTimeout(resolve, 0));
stopPolling();
release();
await started;
console.log(JSON.stringify({
  timers, sideEffects, poll: paymentState.poll, generation: paymentState.pollGeneration,
}));
"""
    result = _run(tmp_path, harness % {"engine": ENGINE})
    assert result["timers"] == [], "the late answer schedules nothing"
    assert result["sideEffects"] == 0
    assert result["poll"] is None
    assert result["generation"] == 2


def test_a_newer_order_supersedes_the_one_being_followed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    harness = PRELUDE + """
const seen = [];
const releases = {};
async function tick(id, current) {
  await new Promise((resolve) => { releases[id] = resolve; });
  if (!current()) return false;
  seen.push(id);
  return true;
}
const first = followOrder("depay", "old", tick);
await new Promise((resolve) => setTimeout(resolve, 0));
const second = followOrder("depay", "new", tick);
await new Promise((resolve) => setTimeout(resolve, 0));
releases.old();
releases.new();
await first;
await second;
console.log(JSON.stringify({ seen, timers, following: paymentState.poll?.id }));
"""
    result = _run(tmp_path, harness % {"engine": ENGINE})
    assert result["seen"] == ["new"]
    assert result["timers"] == [3000]
    assert result["following"] == "new"


CHECKOUT_TICK = """
%(engine)s
%(tick)s
const paymentState = {
  workspace: { id: "w1" }, billing: { credit_balance: 5, plan_tier: "PRO" },
  pollGeneration: 0, poll: null, stalledPoll: null, pollTimer: null,
};
const calls = [];
const window = {
  setTimeout() { return 1; }, clearTimeout() {},
  dispatchEvent(event) { calls.push(`event:${event.type}`); },
};
class CustomEvent { constructor(type, init) { this.type = type; this.detail = init.detail; } }
const element = () => ({ hidden: true });
let status = "PENDING";
async function api(path) { calls.push(path); return { id: "chk-1", status }; }
async function refreshBilling() { calls.push("refreshBilling"); }
function showPaymentSuccess(payment) { calls.push(`success:${payment.status}`); }
function finishWidget() { calls.push("finishWidget"); }
function setMessage(message = "", error = "") { calls.push(`message:${message}|${error}`); }
function paymentFailureMessage(value) { return `failed:${value}`; }
const pending = await pollCheckoutOnce("chk-1", () => true);
status = "PAID";
const paid = await pollCheckoutOnce("chk-1", () => true);
const superseded = await pollCheckoutOnce("chk-1", () => false);
status = "EXPIRED";
const expired = await pollCheckoutOnce("chk-1", () => true);
console.log(JSON.stringify({ pending, paid, superseded, expired, calls }));
"""


def test_the_depay_check_reports_whether_the_order_still_needs_following(tmp_path) -> None:  # type: ignore[no-untyped-def]
    result = _run(tmp_path, CHECKOUT_TICK % {"engine": ENGINE, "tick": _function_source("pollCheckoutOnce")})
    assert result["pending"] is True
    assert result["paid"] is False and result["superseded"] is False and result["expired"] is False
    assert result["calls"] == [
        "/v1/workspaces/w1/depay-checkouts/chk-1",
        "/v1/workspaces/w1/depay-checkouts/chk-1",
        "refreshBilling",
        "event:ai-director:plan-changed",
        "success:PAID",
        # Superseded before its answer landed: the order is read, nothing is painted.
        "/v1/workspaces/w1/depay-checkouts/chk-1",
        "/v1/workspaces/w1/depay-checkouts/chk-1",
        "finishWidget",
        "message:|failed:EXPIRED",
    ]


def test_every_order_kind_runs_through_the_engine() -> None:
    for name in ("pollCheckout", "pollXunhuPayCheckout", "pollRelayedAuthorization"):
        source = _function_source(name)
        assert "followOrder(" in source, name
    for name in ("pollCheckoutOnce", "pollXunhuPayCheckoutOnce", "pollRelayedAuthorizationOnce"):
        source = _function_source(name)
        assert "if (!current()) return false;" in source, name
        assert "setTimeout" not in source, "a tick never schedules; the engine does"


def test_close_start_creating_and_escape_all_stop_the_poll_the_same_way() -> None:
    assert 'element("closeWalletBtn").addEventListener("click", closeWalletSheet);' in WALLET_JS
    assert 'element("walletContinueBtn").addEventListener("click", closeWalletSheet);' in WALLET_JS
    close = WALLET_JS[WALLET_JS.index('element("walletDialog").addEventListener("close"') :][:600]
    assert "stopPolling();" in close
    assert "paymentState.widgetHandoff" in close, "the close made for the DePay window keeps the poll"
    checkout = _function_source("createCheckout")
    assert "paymentState.widgetHandoff = true;\n      dialog.close();" in checkout
    assert "stopPolling();" in _function_source("initializeForUser"), "sign-out ends the previous poll"
    assert "paymentState.poll || paymentState.stalledPoll" in _function_source("bindProjectWorkspace")
    assert 'id="walletRecheckBtn"' in INDEX_HTML
    assert 'element("walletRecheckBtn").addEventListener("click", resumeStalledPoll);' in WALLET_JS
