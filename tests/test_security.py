"""Tests for the edge controls on the HTTP surface.

Each of these covers a defect that was present and demonstrated, not a
hypothetical: a session cookie that lost its Secure flag behind Cloud Run's TLS
termination, an access code that could be guessed without limit, missing
response headers, and a documentation bundle willing to route credentials
through a third-party proxy.
"""

from __future__ import annotations

import importlib
import os
import re
import unittest
from pathlib import Path

import security

REPO_ROOT = Path(__file__).resolve().parents[1]


class EnvironmentOverride:
    def __init__(self, **values: str):
        self.values = values
        self.previous: dict[str, str | None] = {}

    def __enter__(self):
        for key, value in self.values.items():
            self.previous[key] = os.environ.get(key)
            os.environ[key] = value
        import service

        importlib.reload(service)
        return service

    def __exit__(self, *exc_info):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import service

        importlib.reload(service)


class SchemeDetectionTests(unittest.TestCase):
    """Cloud Run terminates TLS, so the container sees plain HTTP."""

    @staticmethod
    def _request(headers: dict[str, str], scheme: str = "http"):
        from starlette.requests import Request

        return Request(
            {
                "type": "http",
                "scheme": scheme,
                "path": "/",
                "query_string": b"",
                "server": ("fleet.example.run.app", 443),
                "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
                "client": ("10.0.0.1", 1234),
            }
        )

    def test_forwarded_header_is_authoritative(self):
        self.assertTrue(security.is_secure_request(self._request({"x-forwarded-proto": "https"})))
        self.assertFalse(security.is_secure_request(self._request({"x-forwarded-proto": "http"})))

    def test_forwarded_chain_uses_the_client_protocol(self):
        request = self._request({"x-forwarded-proto": "https,http"})
        self.assertTrue(security.is_secure_request(request))

    def test_falls_back_to_the_connection_scheme(self):
        self.assertTrue(security.is_secure_request(self._request({}, scheme="https")))
        self.assertFalse(security.is_secure_request(self._request({}, scheme="http")))

    def test_client_fingerprint_prefers_the_forwarded_address(self):
        request = self._request({"x-forwarded-for": "1.2.3.4, 10.0.0.5"})
        self.assertEqual(security.client_fingerprint(request), "10.0.0.5")
        self.assertEqual(security.client_fingerprint(self._request({})), "10.0.0.1")


class SessionCookieTests(unittest.TestCase):
    def test_cookie_is_marked_secure_behind_a_terminating_proxy(self):
        from starlette.testclient import TestClient

        with EnvironmentOverride(
            FLEET_CONSOLE_ACCESS_CODE="open-sesame", FLEET_API_KEY=""
        ) as service:
            with TestClient(service.app) as client:
                response = client.post(
                    "/api/session",
                    json={"code": "open-sesame"},
                    headers={"x-forwarded-proto": "https"},
                )
                cookie = response.headers.get("set-cookie", "").lower()
                self.assertIn("secure", cookie)
                self.assertIn("httponly", cookie)
                self.assertIn("samesite=strict", cookie)

    def test_cookie_is_not_marked_secure_on_plain_local_http(self):
        """Marking it Secure locally would stop the cookie working at all."""

        from starlette.testclient import TestClient

        with EnvironmentOverride(
            FLEET_CONSOLE_ACCESS_CODE="open-sesame", FLEET_API_KEY=""
        ) as service:
            with TestClient(service.app) as client:
                response = client.post("/api/session", json={"code": "open-sesame"})
                self.assertNotIn("secure", response.headers.get("set-cookie", "").lower())


class ThrottleTests(unittest.TestCase):
    def test_per_caller_budget_is_enforced_then_expires(self):
        throttle = security.SignInThrottle(max_per_caller=3, max_global=100, window_seconds=900)
        for _ in range(3):
            self.assertEqual(throttle.retry_after("1.2.3.4"), 0)
            throttle.record_failure("1.2.3.4")
        self.assertGreater(throttle.retry_after("1.2.3.4"), 0)
        # A different caller is unaffected.
        self.assertEqual(throttle.retry_after("5.6.7.8"), 0)

    def test_a_correct_code_clears_the_budget(self):
        throttle = security.SignInThrottle(max_per_caller=2, max_global=100)
        throttle.record_failure("1.2.3.4")
        throttle.record_failure("1.2.3.4")
        self.assertGreater(throttle.retry_after("1.2.3.4"), 0)
        throttle.record_success("1.2.3.4")
        self.assertEqual(throttle.retry_after("1.2.3.4"), 0)

    def test_global_budget_limits_distributed_guessing(self):
        """Rotating source addresses must not buy unlimited attempts."""

        throttle = security.SignInThrottle(max_per_caller=100, max_global=5)
        for index in range(5):
            throttle.record_failure(f"10.0.0.{index}")
        self.assertGreater(throttle.retry_after("10.0.0.99"), 0)

    def test_expired_attempts_stop_counting(self):
        throttle = security.SignInThrottle(max_per_caller=2, max_global=100, window_seconds=1)
        throttle.record_failure("1.2.3.4")
        throttle.record_failure("1.2.3.4")
        self.assertGreater(throttle.retry_after("1.2.3.4"), 0)
        # Rewind the recorded attempts past the window rather than sleeping.
        with throttle._lock:
            throttle._per_caller["1.2.3.4"] = type(throttle._per_caller["1.2.3.4"])(
                [t - 10 for t in throttle._per_caller["1.2.3.4"]]
            )
            throttle._global = type(throttle._global)([t - 10 for t in throttle._global])
        self.assertEqual(throttle.retry_after("1.2.3.4"), 0)


class SignInRateLimitTests(unittest.TestCase):
    def test_guessing_the_access_code_is_cut_off(self):
        from starlette.testclient import TestClient

        with EnvironmentOverride(
            FLEET_CONSOLE_ACCESS_CODE="open-sesame",
            FLEET_API_KEY="",
            FLEET_SIGNIN_MAX_PER_CALLER="5",
            FLEET_SIGNIN_MAX_GLOBAL="1000",
        ) as service:
            service.SIGNIN_THROTTLE.reset()
            with TestClient(service.app) as client:
                statuses = [
                    client.post("/api/session", json={"code": f"guess-{index}"}).status_code
                    for index in range(12)
                ]
            self.assertEqual(statuses[:5], [401] * 5)
            self.assertTrue(all(code == 429 for code in statuses[5:]), statuses)

    def test_a_throttled_response_says_when_to_retry(self):
        from starlette.testclient import TestClient

        with EnvironmentOverride(
            FLEET_CONSOLE_ACCESS_CODE="open-sesame",
            FLEET_API_KEY="",
            FLEET_SIGNIN_MAX_PER_CALLER="1",
        ) as service:
            service.SIGNIN_THROTTLE.reset()
            with TestClient(service.app) as client:
                client.post("/api/session", json={"code": "wrong"})
                blocked = client.post("/api/session", json={"code": "wrong"})
            self.assertEqual(blocked.status_code, 429)
            self.assertTrue(blocked.headers.get("retry-after"))


class SecurityHeaderTests(unittest.TestCase):
    def setUp(self):
        from starlette.testclient import TestClient

        import service

        self.service = service
        self.client = TestClient(service.app)

    def test_every_response_carries_the_baseline_headers(self):
        with self.client as client:
            for path in ("/", "/docs", "/openapi.json", "/healthz"):
                response = client.get(path, headers={"accept": "text/html"})
                self.assertEqual(response.headers.get("x-content-type-options"), "nosniff", path)
                self.assertEqual(response.headers.get("x-frame-options"), "DENY", path)
                self.assertTrue(response.headers.get("content-security-policy"), path)
                self.assertTrue(response.headers.get("referrer-policy"), path)

    def test_policy_blocks_the_things_that_matter(self):
        with self.client as client:
            policy = client.get("/").headers["content-security-policy"]
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertIn("object-src 'none'", policy)
        self.assertIn("base-uri 'none'", policy)
        # Scripts must not be loadable from anywhere but this origin.
        self.assertIn("script-src 'self'", policy)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", policy)
        # This is what stops a playground request reaching proxy.scalar.com.
        self.assertIn("connect-src 'self'", policy)

    def test_hsts_only_over_https(self):
        with self.client as client:
            plain = client.get("/healthz")
            self.assertIsNone(plain.headers.get("strict-transport-security"))
            forwarded = client.get("/healthz", headers={"x-forwarded-proto": "https"})
            self.assertIn("max-age=", forwarded.headers.get("strict-transport-security", ""))


class DocumentationBundleTests(unittest.TestCase):
    def test_the_reference_disables_the_third_party_request_proxy(self):
        page = (REPO_ROOT / "web" / "docs.html").read_text(encoding="utf-8")
        self.assertIn('"proxyUrl":""', page.replace(" ", ""))

    def test_the_reference_has_no_inline_script(self):
        """An inline script would force 'unsafe-inline' into script-src."""

        page = (REPO_ROOT / "web" / "docs.html").read_text(encoding="utf-8")
        for block in re.findall(r"<script\b[^>]*>(.*?)</script>", page, re.S):
            self.assertEqual(block.strip(), "", "inline script found in docs.html")


class SubmissionLimitTests(unittest.TestCase):
    def test_queued_work_is_capped(self):
        from starlette.testclient import TestClient

        import runtime

        with EnvironmentOverride(FLEET_MAX_PENDING_JOBS="0", FLEET_API_KEY="") as service:
            original = runtime.fleet_stats
            runtime.fleet_stats = lambda: {"counts": {runtime.STATUS_QUEUED: 5}}
            try:
                with TestClient(service.app) as client:
                    response = client.post("/jobs", json={"crn": "03994971"})
                self.assertEqual(response.status_code, 429)
                self.assertTrue(response.headers.get("retry-after"))
            finally:
                runtime.fleet_stats = original


if __name__ == "__main__":
    unittest.main()


class AssetCachingTests(unittest.TestCase):
    """A cached script running against a newer API looks like a broken feature.

    This is not hypothetical: the audit-history view appeared blank because a
    browser held an older app.js whose router did not know the view existed.
    """

    def setUp(self):
        from starlette.testclient import TestClient

        import service

        self.client = TestClient(service.app)

    def test_console_assets_must_be_revalidated(self):
        with self.client as client:
            for path in ("/", "/docs", "/static/app.js", "/static/styles.css"):
                response = client.get(path, headers={"accept": "text/html"})
                self.assertEqual(response.status_code, 200, path)
                self.assertIn(
                    "no-cache", response.headers.get("cache-control", ""), path
                )

    def test_api_responses_are_not_given_the_asset_policy(self):
        """Data endpoints set their own caching; this policy is for the app shell."""

        with self.client as client:
            response = client.get("/healthz")
        self.assertNotIn("must-revalidate", response.headers.get("cache-control", ""))

    def test_revalidation_still_permits_a_304(self):
        """no-cache means revalidate, not never store: an unchanged asset is cheap."""

        with self.client as client:
            first = client.get("/static/styles.css")
            etag = first.headers.get("etag")
            self.assertTrue(etag, "static assets should carry an ETag to revalidate against")
            second = client.get("/static/styles.css", headers={"if-none-match": etag})
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.content, b"")
