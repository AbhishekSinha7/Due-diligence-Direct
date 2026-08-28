"""Exceptions raised by the client.

Every failure mode the control plane can return gets its own type, because
callers act differently on each: an authentication failure needs a credential,
a policy denial needs a scope, and a failed job needs the operator to look at
the error text. A single generic exception would force string matching.
"""

from __future__ import annotations

from typing import Any


class FleetError(Exception):
    """Base class for every error this library raises."""


class TransportError(FleetError):
    """The control plane could not be reached at all."""


class APIError(FleetError):
    """The control plane returned an error response."""

    def __init__(self, message: str, status_code: int = 0, payload: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = super().__str__()
        return f"{base} (HTTP {self.status_code})" if self.status_code else base


class AuthenticationError(APIError):
    """No credential was supplied, or the one supplied was rejected.

    Supply one of: an access code, an API key, a bearer token, or run the client
    somewhere the Cloud Run metadata server can mint an identity token.
    """


class PolicyDenied(APIError):
    """The gateway refused the call: the caller lacks the scope or the tool."""


class NotFound(APIError):
    """The job, company, or memory record does not exist."""


class JobFailed(FleetError):
    """An audit reached a terminal state that was not success."""

    def __init__(self, job: Any) -> None:
        self.job = job
        detail = getattr(job, "error", None) or "no error detail was recorded"
        super().__init__(f"Audit {getattr(job, 'job_id', '?')} finished as {getattr(job, 'status', '?')}: {detail}")


class WaitTimeout(FleetError):
    """The audit did not finish inside the timeout.

    The job is still running server-side; this is a client-side give-up, not a
    cancellation. Call `cancel()` if you actually want it stopped.
    """

    def __init__(self, job: Any, waited: float) -> None:
        self.job = job
        self.waited = waited
        super().__init__(
            f"Audit {getattr(job, 'job_id', '?')} was still {getattr(job, 'status', '?')} "
            f"after {waited:.0f}s. It is still running on the fleet."
        )
