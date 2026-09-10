"""Compatibility import for the unified Prompt Compiler implementation."""

from video_prompt_core.compiler import (
    ContinuityApprovalRequired,
    ContinuityGateSource,
    PromptCompilerResult,
    PromptCompilerService,
)

__all__ = [
    "ContinuityApprovalRequired",
    "ContinuityGateSource",
    "PromptCompilerResult",
    "PromptCompilerService",
]
