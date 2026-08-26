from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from production_domain.models import (
    CostRecord,
    DecisionOutcomeRecord,
    DecisionRecord,
    Episode,
    GenerationCandidate,
    GenerationJob,
    LiveCanaryPermit,
    ModelDefinition,
    ModelExecutionRecord,
    Project,
    ProviderAccount,
    ProviderBillingEvidence,
    ProviderProjectBinding,
    QAResult,
    Scene,
    Shot,
    TimelineTransition,
)
from sqlalchemy import select
from video_platform_api.main import create_app


def _internal_headers(container) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {"Authorization": f"Bearer {container.settings.platform_api_key}"}


def _seed_production_evidence(container) -> dict[str, str]:  # type: ignore[no-untyped-def]
    with container.database.session() as session:
        definition = session.scalar(select(ModelDefinition).order_by(ModelDefinition.created_at))
        assert definition is not None
        project = Project(title="Evidence Project")
        foreign_project = Project(title="Foreign Project")
        session.add_all([project, foreign_project])
        session.flush()

        episode = Episode(project_id=project.id, title="Episode", episode_number=1)
        foreign_episode = Episode(
            project_id=foreign_project.id,
            title="Foreign Episode",
            episode_number=1,
        )
        session.add_all([episode, foreign_episode])
        session.flush()
        scene = Scene(episode_id=episode.id, sequence=1)
        foreign_scene = Scene(episode_id=foreign_episode.id, sequence=1)
        session.add_all([scene, foreign_scene])
        session.flush()
        source_shot = Shot(scene_id=scene.id, sequence=1, prompt="Source")
        target_shot = Shot(
            scene_id=scene.id,
            sequence=2,
            prompt="Target",
            previous_shot_id=None,
            downstream_state_stale=True,
            stale_reason="RECOMPUTE_REQUIRED",
        )
        foreign_shot = Shot(scene_id=foreign_scene.id, sequence=1, prompt="Foreign")
        session.add_all([source_shot, target_shot, foreign_shot])
        session.flush()
        target_shot.previous_shot_id = source_shot.id
        target_shot.stale_from_shot_id = source_shot.id

        account = ProviderAccount(
            provider="google_flow",
            account_identifier="evidence-flow@example.com",
            supported_models=["veo"],
        )
        session.add(account)
        session.flush()
        binding = ProviderProjectBinding(
            local_project_id=project.id,
            provider="google_flow",
            provider_account_id=account.id,
            provider_project_id="flow-evidence-project",
            status="READY",
            status_reason="FLOW_RAW_STATUS_SECRET",
            provisioning_token="FLOW_PROVISIONING_SECRET",
        )
        session.add(binding)

        job = GenerationJob(
            project_id=project.id,
            shot_id=target_shot.id,
            generation_type="video",
            provider="google_flow",
            model="veo",
            status="COMPLETED",
            request_json={"prompt": "PRIVATE_PROMPT_BODY"},
            provider_request_json={"raw_response": "PROVIDER_RAW_SECRET"},
            request_hash="a" * 64,
            provider_job_id="provider-job-visible-reference",
            account_id=account.id,
            provider_project_id="flow-evidence-project",
            submission_state="SENT",
            claim_token="CLAIM_TOKEN_SECRET",
            actual_cost=0.25,
        )
        foreign_job = GenerationJob(
            project_id=foreign_project.id,
            shot_id=foreign_shot.id,
            generation_type="video",
            provider="seedance",
            model="seedance-v1",
            status="COMPLETED",
            request_json={"prompt": "FOREIGN_PRIVATE_PROMPT"},
            provider_request_json={"raw_response": "FOREIGN_PROVIDER_RAW"},
            request_hash="b" * 64,
        )
        session.add_all([job, foreign_job])
        session.flush()
        target_shot.generation_job_id = job.id

        candidate = GenerationCandidate(
            shot_id=target_shot.id,
            attempt_number=1,
            generation_job_id=job.id,
            status="USER_REVIEW_REQUIRED",
        )
        session.add(candidate)
        session.flush()
        job.candidate_id = candidate.id
        qa = QAResult(
            candidate_id=candidate.id,
            profile="DIALOGUE",
            level_reached=2,
            decision="SOFT_FAIL",
            overall_score=0.71,
            character_score=0.7,
            hard_failures=["SEMANTIC_REVIEW_REQUIRED"],
            metrics_json={
                "evidence_source": "CHARACTER_EVIDENCE_PRODUCER_V1",
                "evidence_complete": True,
                "provider_raw": "QA_PROVIDER_RAW_SECRET",
                "character_evidence": {
                    "producer_run_id": "fixture-run",
                    "producer_version": "character-evidence-v1",
                    "character_id": "character-1",
                    "tracking_status": "TRACKING_UNCERTAIN",
                    "tracking_reason_codes": ["AMBIGUOUS_TRACK"],
                    "review_requirements": ["VLM_REVIEW_REQUIRED"],
                    "samples": [{"private_vector": [0.1, 0.2, 0.3]}] * 3,
                    "aggregate": {"average_identity": 0.7, "usable_samples": 3},
                    "threshold_profile": {
                        "profile_id": "dialogue-front-v1",
                        "version": "threshold-v1",
                        "identity_pass": 0.78,
                    },
                },
            },
            summary="QA_SUMMARY_PRIVATE_REASON",
        )
        session.add(qa)
        session.flush()
        candidate.qa_result_id = qa.id

        cost = CostRecord(
            project_id=project.id,
            shot_id=target_shot.id,
            candidate_id=candidate.id,
            generation_job_id=job.id,
            provider=job.provider,
            model=job.model,
            estimated_cost=0.2,
            actual_cost=0.25,
        )
        session.add(cost)
        session.flush()
        billing = ProviderBillingEvidence(
            generation_job_id=job.id,
            cost_record_id=cost.id,
            evidence_key="provider-completion-1",
            provider=job.provider,
            model=job.model,
            source="VERIFIED_PROVIDER",
            provider_reference="provider-invoice-visible-reference",
            actual_cost_usd=Decimal("0.250000"),
            estimated_cost_usd=Decimal("0.200000"),
            metadata_json={"raw_response": "BILLING_PROVIDER_RAW_SECRET"},
        )
        outcome = DecisionOutcomeRecord(
            project_id=project.id,
            shot_id=target_shot.id,
            candidate_id=candidate.id,
            generation_job_id=job.id,
            qa_result_id=qa.id,
            continuity_decision="HYBRID",
            generation_policy="TEXT_TO_VIDEO",
            provider=job.provider,
            model=job.model,
            shot_features_json={
                "sequence": 2,
                "shot_type": "DIALOGUE",
                "prompt_hash": "c" * 64,
                "raw_prompt": "OUTCOME_PRIVATE_PROMPT",
            },
            qa_result_json={"raw_provider": "OUTCOME_PROVIDER_RAW_SECRET"},
            user_outcome="REJECTED",
            accepted=False,
            estimated_cost_usd=Decimal("0.200000"),
            actual_cost_usd=Decimal("0.250000"),
            billing_source="VERIFIED_PROVIDER",
        )
        transition = TimelineTransition(
            project_id=project.id,
            source_shot_id=source_shot.id,
            target_shot_id=target_shot.id,
            transition_type="SCENE_CUT",
            branch_key="scene-cut:fixture",
            reconciliation_required=True,
            metadata_json={
                "propagation_semantics": "RESET_BOUNDARY",
                "spatial_state": "RESET",
                "raw_provider_response": "TIMELINE_PROVIDER_RAW_SECRET",
            },
        )
        executions = [
            ModelExecutionRecord(
                project_id=project.id,
                role="DIRECTOR",
                model_definition_id=definition.id,
                provider=definition.provider,
                provider_model_id=definition.provider_model_id,
                request_hash=str(index) * 64,
                latency_ms=10 + index,
                token_usage_json={"input_tokens": 12, "raw_response": "MODEL_USAGE_SECRET"},
                estimated_cost_usd=Decimal("0.010000"),
                cost_source="ESTIMATED",
                status="SUCCEEDED",
                metadata_json={
                    "capability": "chat",
                    "input_count": 1,
                    "raw_response": "MODEL_EXECUTION_RAW_SECRET",
                },
            )
            for index in (1, 2)
        ]
        session.add_all([billing, outcome, transition, *executions])
        session.flush()
        return {
            "project_id": project.id,
            "foreign_project_id": foreign_project.id,
            "job_id": job.id,
            "foreign_job_id": foreign_job.id,
            "source_shot_id": source_shot.id,
            "shot_id": target_shot.id,
            "foreign_shot_id": foreign_shot.id,
        }


def test_production_evidence_is_internal_only_project_scoped_and_redacted(container) -> None:  # type: ignore[no-untyped-def]
    ids = _seed_production_evidence(container)
    path = "/internal/production-evidence"
    params = {
        "project_id": ids["project_id"],
        "job_id": ids["job_id"],
        "shot_id": ids["shot_id"],
    }
    with TestClient(create_app(container)) as client:
        assert client.get(path, params=params).status_code == 401
        assert (
            client.get(path, params=params, headers={"Authorization": "Bearer ordinary-user"}).status_code
            == 401
        )
        response = client.get(path, params=params, headers=_internal_headers(container))

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["scope"]["project_id"] == ids["project_id"]
    assert payload["scope"]["effective_shot_id"] == ids["shot_id"]
    assert payload["scope"]["model_execution_linkage"] == "PROJECT_ONLY"
    assert payload["shot_state"] == {
        "id": ids["shot_id"],
        "downstream_state_stale": True,
        "stale_reason": "RECOMPUTE_REQUIRED",
        "stale_from_shot_id": ids["source_shot_id"],
    }
    assert {item["id"] for item in payload["provider_jobs"]} == {ids["job_id"]}
    assert len(payload["model_executions"]) == 2
    assert len(payload["provider_billing_evidence"]) == 1
    assert len(payload["provider_billing_evidence"][0]["provider_reference_fingerprint"]) == 64
    assert len(payload["cost_records"]) == 1
    assert len(payload["flow_bindings"]) == 1
    assert len(payload["qa_evidence"]) == 1
    assert payload["qa_evidence"][0]["evidence"]["character"]["sample_count"] == 3
    assert len(payload["decision_outcomes"]) == 1
    assert len(payload["timeline_transitions"]) == 1
    assert payload["timeline_transitions"][0]["metadata"] == {
        "propagation_semantics": "RESET_BOUNDARY",
        "spatial_state": "RESET",
    }

    serialized = json.dumps(payload, ensure_ascii=False)
    for secret in (
        "PRIVATE_PROMPT_BODY",
        "PROVIDER_RAW_SECRET",
        "CLAIM_TOKEN_SECRET",
        "FLOW_PROVISIONING_SECRET",
        "QA_PROVIDER_RAW_SECRET",
        "BILLING_PROVIDER_RAW_SECRET",
        "provider-invoice-visible-reference",
        "FLOW_RAW_STATUS_SECRET",
        "QA_SUMMARY_PRIVATE_REASON",
        "OUTCOME_PRIVATE_PROMPT",
        "OUTCOME_PROVIDER_RAW_SECRET",
        "TIMELINE_PROVIDER_RAW_SECRET",
        "MODEL_USAGE_SECRET",
        "MODEL_EXECUTION_RAW_SECRET",
        "private_vector",
    ):
        assert secret not in serialized


def test_production_evidence_rejects_cross_project_job_and_shot_filters(container) -> None:  # type: ignore[no-untyped-def]
    ids = _seed_production_evidence(container)
    path = "/internal/production-evidence"
    headers = _internal_headers(container)
    with TestClient(create_app(container)) as client:
        foreign_job = client.get(
            path,
            headers=headers,
            params={"project_id": ids["project_id"], "job_id": ids["foreign_job_id"]},
        )
        foreign_shot = client.get(
            path,
            headers=headers,
            params={"project_id": ids["project_id"], "shot_id": ids["foreign_shot_id"]},
        )
        mismatched_local_scope = client.get(
            path,
            headers=headers,
            params={
                "project_id": ids["project_id"],
                "job_id": ids["job_id"],
                "shot_id": ids["source_shot_id"],
            },
        )

    assert foreign_job.status_code == 404
    assert foreign_shot.status_code == 404
    assert mismatched_local_scope.status_code == 404


def test_production_evidence_requires_project_scope_and_enforces_limit_cap(container) -> None:  # type: ignore[no-untyped-def]
    ids = _seed_production_evidence(container)
    path = "/internal/production-evidence"
    headers = _internal_headers(container)
    with TestClient(create_app(container)) as client:
        assert client.get(path, headers=headers).status_code == 422
        assert (
            client.get(
                path,
                headers=headers,
                params={"project_id": ids["project_id"], "limit": 101},
            ).status_code
            == 422
        )
        limited = client.get(
            path,
            headers=headers,
            params={"project_id": ids["project_id"], "limit": 1},
        )

    assert limited.status_code == 200, limited.text
    payload = limited.json()
    assert payload["scope"]["limit_per_collection"] == 1
    for collection in (
        "model_executions",
        "provider_jobs",
        "provider_billing_evidence",
        "cost_records",
        "flow_bindings",
        "qa_evidence",
        "decision_outcomes",
        "timeline_transitions",
    ):
        assert len(payload[collection]) <= 1


def test_live_canary_permit_api_is_internal_only_and_rejects_spoofed_fields(container) -> None:  # type: ignore[no-untyped-def]
    path = "/internal/live-canary-permits"
    internal_headers = {
        **_internal_headers(container),
        "Idempotency-Key": "spoof-boundary-test",
    }
    body = {
        "provider": "offline-fixture",
        "model": "fixture-model-v1",
        "max_requests": 2,
        "max_cost_usd": "1.250000",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
        "purpose": "offline authorization boundary test",
        "explicit_confirmation": True,
    }
    with TestClient(create_app(container)) as client:
        assert client.post(path, json=body).status_code == 401
        assert (
            client.post(
                path,
                json=body,
                headers={"Authorization": "Bearer ordinary-user"},
            ).status_code
            == 401
        )
        spoofed = client.post(
            path,
            headers=internal_headers,
            json={
                **body,
                "status": "ACTIVE",
                "used_requests": 0,
                "reserved_cost_usd": "0",
                "actual_cost_usd": "0",
                "metadata_json": {"provider_key": "must-not-be-accepted"},
                "api_key": "must-not-be-accepted",
            },
        )
        unconfirmed = client.post(
            path,
            headers=internal_headers,
            json={**body, "explicit_confirmation": False},
        )
        naive_expiry = client.post(
            path,
            headers=internal_headers,
            json={
                **body,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=20)).replace(tzinfo=None).isoformat(),
            },
        )
        missing_idempotency_key = client.post(
            path,
            headers=_internal_headers(container),
            json=body,
        )

    assert spoofed.status_code == 422
    assert unconfirmed.status_code == 422
    assert naive_expiry.status_code == 422
    assert missing_idempotency_key.status_code == 400
    with container.database.session() as session:
        assert list(session.scalars(select(LiveCanaryPermit))) == []
        assert (
            list(
                session.scalars(
                    select(DecisionRecord).where(DecisionRecord.decision_type == "LIVE_CANARY_PERMIT_CREATED")
                )
            )
            == []
        )


def test_live_canary_permit_api_creates_audited_offline_permit_and_lists_usage(
    container,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    path = "/internal/live-canary-permits"

    def forbid_provider_resolution(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("permit administration must not resolve or call a provider")

    monkeypatch.setattr(container.providers, "get", forbid_provider_resolution)
    body = {
        "provider": "offline-fixture",
        "model": "fixture-model-v1",
        "max_requests": 3,
        "max_cost_usd": "2.750000",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
        "purpose": "one explicitly bounded offline canary",
        "explicit_confirmation": True,
    }
    internal_headers = {
        **_internal_headers(container),
        "Idempotency-Key": "offline-create-permit-1",
    }
    with TestClient(create_app(container)) as client:
        created = client.post(path, headers=internal_headers, json=body)
        replayed = client.post(path, headers=internal_headers, json=body)
        conflict = client.post(
            path,
            headers=internal_headers,
            json={**body, "purpose": "different authorization facts"},
        )

    assert created.status_code == 201, created.text
    created_payload = created.json()
    assert created_payload["replayed"] is False
    assert replayed.status_code == 201, replayed.text
    assert replayed.json()["id"] == created_payload["id"]
    assert replayed.json()["audit_decision_id"] == created_payload["audit_decision_id"]
    assert replayed.json()["replayed"] is True
    assert conflict.status_code == 409
    permit_id = created_payload["id"]
    audit_id = created_payload["audit_decision_id"]
    assert created_payload["provider"] == "offline-fixture"
    assert created_payload["model"] == "fixture-model-v1"
    assert created_payload["max_requests"] == 3
    assert created_payload["used_requests"] == 0
    assert created_payload["reserved_cost_usd"] in {"0", "0.000000"}
    assert created_payload["actual_cost_usd"] in {"0", "0.000000"}
    assert created_payload["usage_status"] == "UNUSED"
    assert created_payload["usage_statuses"] == {
        "RESERVED": 0,
        "UNCERTAIN": 0,
        "SETTLED": 0,
        "RELEASED": 0,
    }

    reservation = container.live_canary.reserve(
        permit_id,
        provider="offline-fixture",
        model="fixture-model-v1",
        estimated_cost_usd="0.250000",
        idempotency_key="offline-fixture-operation",
    )
    container.live_canary.mark_uncertain(
        reservation.usage_id,
        evidence_reference="offline-boundary-fixture",
    )

    with TestClient(create_app(container)) as client:
        assert client.get(path, params={"permit_id": permit_id}).status_code == 401
        assert (
            client.get(
                path,
                headers=_internal_headers(container),
                params={"limit": 101},
            ).status_code
            == 422
        )
        listed = client.get(
            path,
            headers=_internal_headers(container),
            params={"permit_id": permit_id, "provider": "offline-fixture", "status": "ACTIVE"},
        )

    assert listed.status_code == 200, listed.text
    payload = listed.json()
    assert payload["limit"] == 50
    assert len(payload["permits"]) == 1
    permit_view = payload["permits"][0]
    assert permit_view["id"] == permit_id
    assert permit_view["used_requests"] == 1
    assert permit_view["reserved_cost_usd"] == "0.250000"
    assert permit_view["actual_cost_usd"] == "0.000000"
    assert permit_view["usage_status"] == "RECONCILIATION_REQUIRED"
    assert permit_view["usage_statuses"]["UNCERTAIN"] == 1
    assert permit_view["requires_reconciliation"] is True

    serialized = json.dumps({"created": created_payload, "listed": payload})
    for forbidden in (
        container.settings.platform_api_key,
        "must-not-be-accepted",
        "provider_key",
        "metadata_json",
        "idempotency_key",
        "evidence_reference",
        "offline-boundary-fixture",
        "offline-create-permit-1",
    ):
        assert forbidden not in serialized

    with container.database.session() as session:
        stored = session.get(LiveCanaryPermit, permit_id)
        audit = session.get(DecisionRecord, audit_id)
        assert stored is not None
        assert audit is not None
        assert audit.project_id is None
        assert audit.shot_id is None
        assert audit.decision_type == "LIVE_CANARY_PERMIT_CREATED"
        assert audit.input_features["permit_id"] == permit_id
        assert audit.input_features["server_actor"] == "PLATFORM_API_KEY"
        assert audit.input_features["explicit_confirmation"] is True
        assert len(audit.input_features["idempotency_key_hash"]) == 64
        assert audit.input_features["idempotency_key_hash"] != "offline-create-permit-1"
        assert audit.selected_action == "CREATE_LIVE_CANARY_PERMIT"
        assert (
            len(
                list(
                    session.scalars(
                        select(DecisionRecord).where(
                            DecisionRecord.decision_type == "LIVE_CANARY_PERMIT_CREATED"
                        )
                    )
                )
            )
            == 1
        )


def test_live_canary_usage_reconciliation_frees_the_hold_and_audits_the_finding(
    container,  # type: ignore[no-untyped-def]
) -> None:
    """An UNCERTAIN usage that never reached the provider settles at zero.

    The hold exists because a crossed boundary looks the same from here whether
    it was billed or refused. Once an operator has read the provider's console,
    keeping the estimate reserved is no longer caution — it spends the audit's
    global ceiling on an attempt that cost nothing.
    """

    expires_at = datetime.now(UTC) + timedelta(hours=2)
    permit, _, _ = container.live_canary.create_authorized(
        provider="offline-fixture",
        model="fixture-model-v1",
        max_requests=1,
        max_cost_usd="0.500000",
        expires_at=expires_at,
        purpose="Offline reconciliation fixture",
        explicit_confirmation=True,
        actor_type="PLATFORM_API_KEY",
        idempotency_key="offline-reconcile-permit-1",
    )
    reservation = container.live_canary.reserve(
        permit.id,
        provider="offline-fixture",
        model="fixture-model-v1",
        estimated_cost_usd="0.500000",
        idempotency_key="offline-reconcile-operation",
    )
    container.live_canary.mark_uncertain(
        reservation.usage_id,
        evidence_reference="offline-boundary-fixture",
    )

    path = f"/internal/live-canary-usages/{reservation.usage_id}/reconcile"
    body = {
        "action": "CONFIRM_PROVIDER_NOT_CREATED",
        "reason": "provider refused before creating a job",
        "explicit_confirmation": True,
        "evidence_reference": "offline-console-read",
    }
    headers = {**_internal_headers(container), "Idempotency-Key": "offline-reconcile-1"}
    with TestClient(create_app(container)) as client:
        assert client.post(path, json=body).status_code == 401
        missing_key = client.post(path, headers=_internal_headers(container), json=body)
        priced = client.post(
            path,
            headers={**_internal_headers(container), "Idempotency-Key": "offline-reconcile-priced"},
            json={**body, "actual_cost_usd": "0.010000"},
        )
        settled = client.post(path, headers=headers, json=body)
        replayed = client.post(path, headers=headers, json=body)
        conflict = client.post(
            path,
            headers=headers,
            json={**body, "action": "SETTLE_ACTUAL_COST", "actual_cost_usd": "0.010000"},
        )
        repeated = client.post(
            path,
            headers={**_internal_headers(container), "Idempotency-Key": "offline-reconcile-2"},
            json=body,
        )

    assert missing_key.status_code == 400
    # Zero is the whole claim of this action; a cost alongside it is a contradiction.
    assert priced.status_code == 422, priced.text
    assert settled.status_code == 200, settled.text
    payload = settled.json()
    assert payload["usage_status"] == "SETTLED"
    assert payload["replayed"] is False
    assert payload["permit"]["reserved_cost_usd"] == "0.000000"
    assert payload["permit"]["actual_cost_usd"] == "0.000000"
    # The attempt still happened, so it still counts against the request ceiling.
    assert payload["permit"]["used_requests"] == 1
    assert payload["permit"]["requires_reconciliation"] is False
    assert payload["permit"]["usage_status"] == "SETTLED"
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    assert replayed.json()["audit_decision_id"] == payload["audit_decision_id"]
    assert conflict.status_code == 409
    # A second, different key finds nothing left to reconcile.
    assert repeated.status_code == 409

    serialized = json.dumps(payload)
    for forbidden in (
        container.settings.platform_api_key,
        "offline-reconcile-1",
        "offline-console-read",
    ):
        assert forbidden not in serialized

    with container.database.session() as session:
        audit = session.get(DecisionRecord, payload["audit_decision_id"])
        assert audit is not None
        assert audit.decision_type == "LIVE_CANARY_USAGE_RECONCILED"
        assert audit.selected_action == "CONFIRM_PROVIDER_NOT_CREATED"
        assert audit.input_features["usage_id"] == reservation.usage_id
        assert audit.input_features["actual_cost_usd"] == "0.000000"
        assert audit.input_features["previous_status"] == "UNCERTAIN"
        assert audit.input_features["server_actor"] == "PLATFORM_API_KEY"
        assert len(audit.input_features["idempotency_key_hash"]) == 64
        assert audit.input_features["idempotency_key_hash"] != "offline-reconcile-1"
