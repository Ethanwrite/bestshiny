from __future__ import annotations

from typing import Any, cast

from sqlalchemy.engine import CursorResult, Result


def affected_rows(result: Result[Any]) -> int:
    """Return a DML rowcount while preserving SQLAlchemy's typed Result API."""

    return cast(CursorResult[Any], result).rowcount
