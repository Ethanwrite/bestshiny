from __future__ import annotations


class UserConnectionError(RuntimeError):
    """Base for connection failures that carry a stable, user-facing code."""

    code = "CONNECTION_ERROR"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code

    def as_detail(self) -> dict[str, str]:
        return {"message": str(self), "reason_code": self.code}


class ConnectionNotFound(LookupError):
    """No live connection with that id in the caller's workspace."""


class ConnectionInvalid(UserConnectionError, ValueError):
    """The request describes a connection that cannot exist (bad URL, protocol, model)."""

    code = "CONNECTION_INVALID"


class ConnectionUnavailable(UserConnectionError):
    """The connection exists but cannot serve this call (deleted, disabled, wrong capability)."""

    code = "CONNECTION_UNAVAILABLE"


class EgressDenied(PermissionError):
    """An outbound connection would reach an address the platform must not dial.

    Raised where the socket is opened, on the address actually resolved, so a
    user-supplied host cannot be pointed at the platform's own network - the
    database, the object store, the cloud metadata service - by DNS, a literal
    address or a rebinding answer.
    """


class ResponseTooLarge(EgressDenied):
    """The provider's response body exceeded the connection's byte cap.

    Unlike a refused connection this happens after the request was sent, so a
    generation submission that hits it must be treated as possibly submitted.
    """


class ProviderCallFailed(UserConnectionError):
    """The provider answered with an error, or could not be reached."""

    code = "PROVIDER_CALL_FAILED"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
        super().__init__(message, code=code)
        self.status_code = status_code


__all__ = [
    "UserConnectionError",
    "ConnectionInvalid",
    "ConnectionNotFound",
    "ConnectionUnavailable",
    "EgressDenied",
    "ProviderCallFailed",
    "ResponseTooLarge",
]
