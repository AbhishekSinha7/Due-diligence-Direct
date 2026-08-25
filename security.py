"""Edge controls for the HTTP surface.

Cloud Run IAM is the primary access control and should stay that way: these are
the defences that survive someone deciding to make the service public, or a
credential leaking. Each one is cheap, and none of them is a substitute for IAM.

What lives here:

- `is_secure_request` - the truth about the scheme behind a TLS-terminating
  proxy, which `request.url.scheme` does not tell you.
- `SecurityHeadersMiddleware` - CSP and friends.
- `SignInThrottle` - makes guessing the console access code expensive.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Deque

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# ---------------------------------------------------------------- scheme

def is_secure_request(request: Request) -> bool:
    """True when the browser's connection is HTTPS.

    On Cloud Run, TLS terminates at the front end and the container is spoken to
    over plain HTTP, so `request.url.scheme` is "http" on a perfectly secure
    request. Trusting it marks session cookies as non-Secure, which lets them
    travel over a downgraded connection. The forwarded header is authoritative
    here because only the platform can set it — the container is not reachable
    directly.
    """

    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        # A comma-separated chain lists the original client protocol first.
        return forwarded.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


def client_fingerprint(request: Request) -> str:
    """A best-effort caller identity for throttling.

    `x-forwarded-for` is client-controlled in general, but on Cloud Run the front
    end appends the real address and the container cannot be reached around it,
    so the last entry is trustworthy there. Falls back to the socket address.
    """

    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------- headers

# 'unsafe-inline' for styles only: the console uses style attributes and the API
# reference injects a stylesheet at runtime. Scripts stay strict — the bundle
# contains no eval, no Function constructor, and no workers, so 'self' is enough.
#
# connect-src 'self' is load-bearing beyond the usual: the vendored reference
# ships a default request proxy at proxy.scalar.com, and this is what guarantees
# a "Try it" request cannot leave for a third party carrying a credential.
CSP = (
    "default-src 'self'; "
    "base-uri 'none'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "worker-src 'none'"
)

BASE_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "same-origin",
    "cross-origin-opener-policy": "same-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}

HSTS = "max-age=31536000; includeSubDomains"

# The console is a single-page app whose JavaScript and the API it calls are
# deployed together and must not drift apart. Without this, a browser may serve a
# cached script against a newer API and fail in ways that look like a broken
# feature rather than a stale asset. "no-cache" still allows caching -- it
# requires revalidation, so an unchanged file costs a 304 and no body.
REVALIDATE = "no-cache, must-revalidate"


def _is_console_asset(path: str) -> bool:
    """Whether this path serves the console itself rather than data."""

    return path in {"/", "/docs"} or path.startswith("/static/")


class SecurityHeadersMiddleware:
    """Attach security headers to every response.

    HSTS is only sent over HTTPS: promising it on a plain-HTTP local run would
    pin a developer's browser to a scheme their machine does not serve.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        secure = is_secure_request(request)

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in BASE_HEADERS.items():
                    headers.setdefault(name, value)
                headers.setdefault("content-security-policy", CSP)
                if secure:
                    headers.setdefault("strict-transport-security", HSTS)
                if _is_console_asset(scope.get("path", "")):
                    headers["cache-control"] = REVALIDATE
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ---------------------------------------------------------------- throttle


class SignInThrottle:
    """Sliding-window limiter for console sign-in attempts.

    An access code is a short shared secret, so unlimited guessing is the whole
    attack. This caps failures per caller and, separately, in total — the second
    limit is what makes a distributed guessing run expensive rather than free.

    Honest about its limits: state is per process. Cloud Run may run several
    instances, so a determined attacker gets a multiple of these budgets. It
    raises the cost of guessing by orders of magnitude; it is not a replacement
    for IAM, Cloud Armor, or a long random code.
    """

    def __init__(
        self,
        max_per_caller: int | None = None,
        max_global: int | None = None,
        window_seconds: int | None = None,
    ) -> None:
        self.max_per_caller = max_per_caller or int(os.getenv("FLEET_SIGNIN_MAX_PER_CALLER", "10"))
        self.max_global = max_global or int(os.getenv("FLEET_SIGNIN_MAX_GLOBAL", "100"))
        self.window = window_seconds or int(os.getenv("FLEET_SIGNIN_WINDOW_SECONDS", "900"))
        self._per_caller: dict[str, Deque[float]] = {}
        self._global: Deque[float] = deque()
        self._lock = threading.Lock()

    @staticmethod
    def _prune(bucket: Deque[float], cutoff: float) -> None:
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def retry_after(self, caller: str) -> int:
        """Seconds the caller must wait, or 0 if an attempt is allowed now."""

        now = time.time()
        cutoff = now - self.window
        with self._lock:
            self._prune(self._global, cutoff)
            if len(self._global) >= self.max_global:
                return max(1, int(self._global[0] + self.window - now))

            bucket = self._per_caller.get(caller)
            if bucket is None:
                return 0
            self._prune(bucket, cutoff)
            if len(bucket) >= self.max_per_caller:
                return max(1, int(bucket[0] + self.window - now))
        return 0

    def record_failure(self, caller: str) -> None:
        now = time.time()
        with self._lock:
            self._per_caller.setdefault(caller, deque()).append(now)
            self._global.append(now)

    def record_success(self, caller: str) -> None:
        """Clear the caller's budget. A correct code proves they are not guessing."""

        with self._lock:
            self._per_caller.pop(caller, None)

    def reset(self) -> None:
        with self._lock:
            self._per_caller.clear()
            self._global.clear()


class RateLimiter:
    """Sliding-window request budgets, keyed by caller.

    Per process, like `SignInThrottle`: with several Cloud Run instances a
    caller's real budget is a multiple of what is configured here. It exists to
    bound accidental runaway and casual abuse -- notably model spend -- not to
    be an authorisation boundary. Cloud Armor is the answer if you need one.
    """

    def __init__(self, window_seconds: int = 3600) -> None:
        self.window = window_seconds
        self._buckets: dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, caller: str, limit: int) -> int:
        """Seconds to wait, or 0 if a request is allowed. A limit of 0 is unlimited."""

        if limit <= 0:
            return 0
        now = time.time()
        cutoff = now - self.window
        with self._lock:
            bucket = self._buckets.setdefault(caller, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                return max(1, int(bucket[0] + self.window - now))
            bucket.append(now)
        return 0

    def used(self, caller: str) -> int:
        now = time.time()
        cutoff = now - self.window
        with self._lock:
            bucket = self._buckets.get(caller)
            if not bucket:
                return 0
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            return len(bucket)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
