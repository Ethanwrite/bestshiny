# Live provider tests

Tests in this directory are isolated from the ordinary suite. Every test that can contact an external provider
must use `@pytest.mark.live_provider`. Pytest skips that marker unless the operator also supplies the explicit
`--run-live-provider` switch.

The switch is only a test-selection gate. It does not enable paid traffic. A live test must still satisfy the
runtime three-part gate through an approved secret-management environment:

```text
PROVIDER_MODE=live
ALLOW_LIVE_PROVIDER_CALLS=true
LIVE_PROVIDER_CONFIRMATION=I_UNDERSTAND_THIS_COSTS_MONEY
```

RunAPI additionally requires `ALLOW_RUNAPI_EDGE_CALLS=true`, an allowed Edge/Temporary task and available budget.
Never put credentials in this directory, pytest parameters, shell history, logs, fixtures or reports.

## These tests read the environment, not `.env`

`.env` is loaded by `Settings`, which is pydantic's own file read — it never reaches `os.environ`. These tests
read `os.environ` directly, so a variable that lives only in `.env` is **absent** here. The failure mode is
quiet: the fixture calls `pytest.skip`, the run reports success, and nothing was verified. Check the summary
line for `skipped` before believing a live run proved anything.

Export what the selected test needs into the shell that runs pytest. Do not commit an export, and do not echo
one into a log.

## What is here

| Test | Cost | What it establishes |
| --- | --- | --- |
| `test_wan_video_live.py::test_the_reviewed_model_ids_are_the_ones_that_get_posted` | free, no socket | The model ID, `media[]` shape and framing parameters this environment would actually post, per mode |
| `test_wan_video_live.py::test_rejected_shots_never_reach_the_provider` | free, no socket | Every fail-closed rule holds with no request made |
| `test_wan_video_live.py::test_smallest_t2v_generation_reaches_a_terminal_state` | **billed** | Submit, poll and artefact parsing against the real DashScope async protocol |
| `test_openrouter_image_live.py::test_capability_descriptor_matches_the_reviewed_envelope` | free `GET` | The published limits still match the envelope compiled into the adapter |
| `test_openrouter_image_live.py::test_smallest_approved_generation_returns_decodable_image_bytes` | **billed**, ~USD 0.01 | Request body and response parsing against the real service |

Only Wan **T2V** is testable without object storage. I2V and R2V each carry a reference the provider fetches
itself, and with `S3_*` unset there is no URL Alibaba can reach. Run `scripts/preflight_live.py` first — it
reports exactly that, makes no network call and prints no secret.

A typical invocation, cheapest first:

```text
pytest --run-live-provider -m live_provider tests/live/test_wan_video_live.py -k "not smallest_t2v"
```

Live tests must use the smallest approved non-canonical fixture, record latency/cost/failure evidence, and must
not promote output to a canonical asset or committed production timeline. They are excluded from default CI and
must never fall back from a missing fixture into a network call. Construct provider settings and clients inside
test fixtures or test functions, after pytest has applied the live-test isolation fixture; do not create them at
module import time.
