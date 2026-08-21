# Live provider tests

There are no executable live-provider tests in this directory yet.

Tests in this directory are isolated from the ordinary test suite. Every test that can contact an external
provider must use `@pytest.mark.live_provider`. Pytest skips that marker unless the operator also supplies the
explicit `--run-live-provider` switch.

The switch is only a test-selection gate. It does not enable paid traffic. A live test must still satisfy the
runtime three-part gate through an approved secret-management environment:

```text
PROVIDER_MODE=live
ALLOW_LIVE_PROVIDER_CALLS=true
LIVE_PROVIDER_CONFIRMATION=I_UNDERSTAND_THIS_COSTS_MONEY
```

RunAPI additionally requires `ALLOW_RUNAPI_EDGE_CALLS=true`, an allowed Edge/Temporary task and available budget.
Never put credentials in this directory, pytest parameters, shell history, logs, fixtures or reports.

Run live tests only after explicit provider-specific approval, credential rotation, budget confirmation and a
review of the exact test selection. A typical invocation is:

```text
pytest --run-live-provider -m live_provider tests/live
```

Live tests must use the smallest approved non-canonical fixture, record latency/cost/failure evidence, and must
not promote output to a canonical asset or committed production timeline. They are excluded from default CI and
must never fall back from a missing fixture into a network call. Construct provider settings and clients inside
test fixtures or test functions, after pytest has applied the live-test isolation fixture; do not create them at
module import time.
