"""Hand `live_canary.py` the local QA session it refuses to invent for itself.

`scripts/live_canary.py` needs a real workspace user and a project that user can
write to, and deliberately creates neither. This resolves both from what is
already in the local database.

It cannot hand back the token you are holding in the browser: `auth_sessions`
stores only `sha256(token)`, by design. What it does instead is mint a **fresh,
short-lived** session for the same user through the application's own
`AuthService`, so the canary runs as that user without anyone typing a password.

    uv run python scripts/canary_session.py                  # report only
    eval "$(uv run python scripts/canary_session.py --export)"
    uv run python scripts/canary_session.py --revoke <session-id>

Selection, when `--email` is not given, is deliberately narrow: the ACTIVE
workspace with the most credits whose owner already has a live session — and
only if that owner is an `@example.com` test account. A real address is never
auto-selected; name it with `--email` if you mean it. The chosen user, workspace,
project and balance are always printed, so the canary never runs against a
workspace you did not mean to spend.

The minted session expires in `--ttl-minutes` (default 120, matching the canary
permit window) rather than the application's 30-day default, and its id is
printed so you can revoke it the moment the run is done.

Prints the bearer token — that is its whole job — and no other secret. Do not
paste the output into a log, an issue, or a commit.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from platform_database import Database  # noqa: E402
from platform_shared import Settings  # noqa: E402
from production_domain.models import AuthSession, Project, User, Workspace  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from video_platform_api.auth import AuthService  # noqa: E402

TEST_ACCOUNT_SUFFIX = "@example.com"


def _say(mark: str, label: str, detail: str = "") -> None:
    print(f"  [{mark}] {label:22} {detail}", file=sys.stderr)


def _pick_owner(session, email: str | None) -> tuple[User, Workspace] | None:  # type: ignore[no-untyped-def]
    """The local QA user, or nothing — never a guess at a real person's account."""

    live_session_owners = (
        select(AuthSession.user_id)
        .where(AuthSession.revoked_at.is_(None), AuthSession.expires_at > datetime.now(UTC))
        .distinct()
    )
    statement = (
        select(User, Workspace)
        .join(Workspace, Workspace.owner_user_id == User.id)
        .where(User.status == "ACTIVE", Workspace.status == "ACTIVE")
        .order_by(Workspace.credit_balance.desc(), Workspace.created_at.desc())
    )
    if email:
        statement = statement.where(func.lower(User.email) == email.strip().lower())
    else:
        statement = statement.where(
            User.id.in_(live_session_owners),
            User.email.ilike(f"%{TEST_ACCOUNT_SUFFIX}"),
        )
    return session.execute(statement).first()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="use this account instead of auto-selecting the QA one")
    parser.add_argument("--project", help="use this project id instead of the workspace's newest")
    parser.add_argument("--ttl-minutes", type=int, default=120, help="how long the minted session lives")
    parser.add_argument("--export", action="store_true", help="print only the two export lines, for eval")
    parser.add_argument("--revoke", metavar="SESSION_ID", help="revoke a session this tool minted")
    args = parser.parse_args()

    settings = Settings()
    if not settings.platform_api_key.strip():
        _say("FAIL", "PLATFORM_API_KEY", "absent — this is operator tooling, not a login")
        return 1
    database = Database(settings.database_url)

    if args.revoke:
        with database.session() as session:
            record = session.get(AuthSession, args.revoke)
            if record is None:
                _say("FAIL", "revoke", f"no session {args.revoke}")
                return 1
            record.revoked_at = datetime.now(UTC)
        _say("ok  ", "revoked", args.revoke)
        return 0

    auth = AuthService(
        database,
        session_ttl_days=settings.auth_session_ttl_days,
        auth_required=settings.auth_required,
        deployment_environment=settings.deployment_environment,
    )

    with database.session() as session:
        found = _pick_owner(session, args.email)
        if found is None:
            _say("FAIL", "QA account", "none matched")
            print(
                "\n  Auto-selection only considers `@example.com` accounts that already hold a\n"
                "  live session, so a real address is never picked for you. Name one with\n"
                "  --email if that is what you intend.\n",
                file=sys.stderr,
            )
            return 1
        user, workspace = found
        if args.project:
            project = session.get(Project, args.project)
            if project is None or project.workspace_id != workspace.id:
                _say("FAIL", "project", f"{args.project} is not in workspace {workspace.id}")
                return 1
        else:
            project = session.scalars(
                select(Project)
                .where(Project.workspace_id == workspace.id, Project.status == "ACTIVE")
                .order_by(Project.created_at.desc())
                .limit(1)
            ).first()
            if project is None:
                _say("FAIL", "project", f"workspace {workspace.name!r} has no ACTIVE project")
                return 1

        issued = auth._issue(session, user, user_agent="live-canary-cli")  # noqa: SLF001
        minted = session.get(AuthSession, issued.principal.session_id)
        if minted is None:  # pragma: no cover - the row was just flushed.
            raise RuntimeError("minted session disappeared")
        # The application's default is 30 days. A token printed to a terminal
        # should not outlive the run it was printed for.
        minted.expires_at = datetime.now(UTC) + timedelta(minutes=max(1, args.ttl_minutes))
        token, session_id, expires_at = issued.token, minted.id, minted.expires_at

        _say("ok  ", "account", f"{user.email} ({user.platform_role})")
        _say("ok  ", "workspace", f"{workspace.name} · {workspace.plan_tier} · {workspace.credit_balance} CR")
        _say("ok  ", "project", f"{project.title} · {project.id}")
        _say("ok  ", "session", f"{session_id} · expires {expires_at:%Y-%m-%d %H:%M}Z")
        project_id = project.id

    print(f"export CANARY_ACCESS_TOKEN={token}")
    print(f"export CANARY_PROJECT_ID={project_id}")
    if not args.export:
        print(
            f"\n# Revoke it when the run is done:\n"
            f"#   uv run python scripts/canary_session.py --revoke {session_id}",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
