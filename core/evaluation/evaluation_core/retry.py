from __future__ import annotations

from .schemas import EvaluationDecision, EvaluationResult, RetryPlan


class RetryEngine:
    """Bounded repair policy. It returns a plan and never submits a paid job itself."""

    version = "retry-engine-v1"

    def __init__(self, max_auto_retries: int = 2):
        if max_auto_retries < 0 or max_auto_retries > 5:
            raise ValueError("max_auto_retries must be between 0 and 5")
        self.max_auto_retries = max_auto_retries

    def plan(
        self,
        evaluation: EvaluationResult,
        *,
        attempt_number: int,
        current_provider: str,
        current_model: str,
        alternatives: list[tuple[str, str]] | None = None,
        references_already_strengthened: bool = False,
    ) -> RetryPlan:
        if evaluation.decision == EvaluationDecision.ACCEPT:
            return RetryPlan(
                action=EvaluationDecision.ACCEPT,
                attempt_number=attempt_number,
                terminal=True,
                reasons=["evaluation accepted"],
            )
        if evaluation.decision == EvaluationDecision.REJECT or attempt_number >= self.max_auto_retries:
            return RetryPlan(
                action=EvaluationDecision.REJECT,
                attempt_number=attempt_number,
                terminal=True,
                reasons=[*evaluation.retry_reasons, "automatic retry limit reached or evidence unavailable"],
            )

        next_attempt = attempt_number + 1
        alternatives = [item for item in alternatives or [] if item != (current_provider, current_model)]
        if evaluation.decision == EvaluationDecision.SWITCH_MODEL and alternatives:
            provider, model = alternatives[0]
            return RetryPlan(
                action=EvaluationDecision.SWITCH_MODEL,
                attempt_number=next_attempt,
                terminal=False,
                next_provider=provider,
                next_model=model,
                prompt_patch=evaluation.retry_patch,
                inject_stronger_references=not references_already_strengthened,
                reasons=evaluation.retry_reasons,
            )
        if evaluation.decision == EvaluationDecision.RETRY_REWRITE_PROMPT:
            return RetryPlan(
                action=EvaluationDecision.RETRY_REWRITE_PROMPT,
                attempt_number=next_attempt,
                terminal=False,
                next_provider=current_provider,
                next_model=current_model,
                prompt_patch=evaluation.retry_patch,
                inject_stronger_references=not references_already_strengthened,
                reasons=evaluation.retry_reasons,
            )
        if not references_already_strengthened:
            return RetryPlan(
                action=EvaluationDecision.RETRY_SAME_MODEL,
                attempt_number=next_attempt,
                terminal=False,
                next_provider=current_provider,
                next_model=current_model,
                inject_stronger_references=True,
                reasons=evaluation.retry_reasons,
            )
        if alternatives:
            provider, model = alternatives[0]
            return RetryPlan(
                action=EvaluationDecision.SWITCH_MODEL,
                attempt_number=next_attempt,
                terminal=False,
                next_provider=provider,
                next_model=model,
                reasons=evaluation.retry_reasons,
            )
        return RetryPlan(
            action=EvaluationDecision.RETRY_SAME_MODEL,
            attempt_number=next_attempt,
            terminal=False,
            next_provider=current_provider,
            next_model=current_model,
            reasons=evaluation.retry_reasons,
        )
