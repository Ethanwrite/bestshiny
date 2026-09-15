"""The gateway's view of a workspace's own provider connections.

A job with ``connection_id`` is paid by the workspace's provider account, so it
never touches the platform's wallet or spend fences: no credit reservation, no
spend authorization, no canary permit, no canary verdict. Everything else the
gateway guarantees for a job - the durable submission boundary, claims,
polling, media validation and storage, cancellation, restart recovery - is the
same code path as a platform job.

The resolver is an interface so the gateway does not import the connection
package (and through it, the provider adapters); ``build_container`` wires
``connection_core.UserConnectionService``, which satisfies it.
"""

from __future__ import annotations

from typing import Protocol

from provider_sdk import USER_CONNECTION_PROVIDER, GenerationProvider
from sqlalchemy.orm import Session


class UserConnectionResolver(Protocol):
    def assert_generation_target_in_session(
        self,
        session: Session,
        *,
        connection_id: str,
        project_id: str,
        media_type: str,
    ) -> None:
        """Refuse a job this connection cannot run (deleted, other workspace, capability, switched off)."""

    def generation_provider(
        self, connection_id: str, *, project_id: str, media_type: str
    ) -> GenerationProvider:
        """Build the provider that runs one job on the connection's own key."""

    def ensure_resources(self, connection_id: str) -> str:
        """Create or refresh the connection's scheduler capacity; return its account id."""


def is_connection_provider(provider: str | None) -> bool:
    return provider == USER_CONNECTION_PROVIDER


__all__ = ["UserConnectionResolver", "is_connection_provider"]
