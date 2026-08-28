"""Tests for per-caller API keys.

The point of these keys is that one caller can be identified, limited, and cut
off without touching anyone else. Each test below is one of those properties.
"""

from __future__ import annotations

import time
import unittest

import api_keys
import security
from api_keys import InvalidKey
from tests.test_security import EnvironmentOverride


class IssueAndVerifyTests(unittest.TestCase):
    def test_round_trip_carries_the_caller_and_its_scopes(self):
        key, issued = api_keys.issue(
            "judge-demo", [api_keys.SCOPE_AUDITS_READ], ttl_days=7, requests_per_hour=100
        )
        self.assertTrue(key.startswith(api_keys.PREFIX))

        principal = api_keys.verify(key)
        self.assertEqual(principal.name, "judge-demo")
        self.assertEqual(principal.key_id, issued.key_id)
        self.assertEqual(principal.scopes, frozenset({api_keys.SCOPE_AUDITS_READ}))
        self.assertEqual(principal.requests_per_hour, 100)
        self.assertTrue(principal.has_scope(api_keys.SCOPE_AUDITS_READ))
        self.assertFalse(principal.has_scope(api_keys.SCOPE_AUDITS_WRITE))

    def test_admin_scope_implies_the_others(self):
        key, _ = api_keys.issue("ops", [api_keys.SCOPE_ADMIN])
        principal = api_keys.verify(key)
        for scope in api_keys.ALL_SCOPES:
            self.assertTrue(principal.has_scope(scope), scope)

    def test_a_tampered_key_is_refused(self):
        """The whole design rests on this: claims are not trusted before the signature."""

        key, _ = api_keys.issue("reader", [api_keys.SCOPE_AUDITS_READ])
        payload, _, signature = key[len(api_keys.PREFIX) :].partition(".")

        import base64
        import json

        claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
        claims["scp"] = [api_keys.SCOPE_ADMIN]
        forged_payload = (
            base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
            .decode()
            .rstrip("=")
        )
        forged = f"{api_keys.PREFIX}{forged_payload}.{signature}"

        with self.assertRaises(InvalidKey):
            api_keys.verify(forged)

    def test_malformed_input_is_refused_without_raising_anything_else(self):
        for candidate in ["", "nonsense", api_keys.PREFIX, api_keys.PREFIX + "a.b", "Bearer x"]:
            with self.assertRaises(InvalidKey):
                api_keys.verify(candidate)

    def test_an_expired_key_stops_working(self):
        key, _ = api_keys.issue("temporary", [api_keys.SCOPE_AUDITS_READ], ttl_days=1)
        api_keys.verify(key)  # valid now
        with self.assertRaises(InvalidKey) as caught:
            api_keys.verify(key, now=int(time.time()) + 2 * 86400)
        self.assertIn("expired", str(caught.exception).lower())

    def test_issuing_rejects_unusable_configurations(self):
        with self.assertRaises(ValueError):
            api_keys.issue("", [api_keys.SCOPE_AUDITS_READ])
        with self.assertRaises(ValueError):
            api_keys.issue("nobody", [])
        with self.assertRaises(ValueError):
            api_keys.issue("typo", ["audits:reed"])
        with self.assertRaises(ValueError):
            api_keys.issue("forever", [api_keys.SCOPE_AUDITS_READ], ttl_days=0)


class RevocationTests(unittest.TestCase):
    def test_a_revoked_key_id_is_refused_despite_a_valid_signature(self):
        key, issued = api_keys.issue("leaked", [api_keys.SCOPE_AUDITS_READ])
        api_keys.verify(key)
        with EnvironmentOverride(FLEET_REVOKED_KEY_IDS=issued.key_id):
            with self.assertRaises(InvalidKey) as caught:
                api_keys.verify(key)
            self.assertIn("revoked", str(caught.exception).lower())

    def test_revoking_one_key_leaves_others_working(self):
        doomed_key, doomed = api_keys.issue("doomed", [api_keys.SCOPE_AUDITS_READ])
        kept_key, _ = api_keys.issue("kept", [api_keys.SCOPE_AUDITS_READ])
        with EnvironmentOverride(FLEET_REVOKED_KEY_IDS=doomed.key_id):
            with self.assertRaises(InvalidKey):
                api_keys.verify(doomed_key)
            self.assertEqual(api_keys.verify(kept_key).name, "kept")


class RateLimiterTests(unittest.TestCase):
    def test_budget_is_per_caller(self):
        limiter = security.RateLimiter(window_seconds=3600)
        for _ in range(3):
            self.assertEqual(limiter.check("key-a", 3), 0)
        self.assertGreater(limiter.check("key-a", 3), 0)
        self.assertEqual(limiter.check("key-b", 3), 0)

    def test_zero_means_unlimited(self):
        limiter = security.RateLimiter()
        for _ in range(100):
            self.assertEqual(limiter.check("unmetered", 0), 0)

    def test_usage_is_reportable(self):
        limiter = security.RateLimiter()
        for _ in range(4):
            limiter.check("key-a", 10)
        self.assertEqual(limiter.used("key-a"), 4)


class ScopeEnforcementTests(unittest.TestCase):
    """The service must actually refuse work a key is not scoped for."""

    def _client(self, service):
        from starlette.testclient import TestClient

        return TestClient(service.app)

    def test_a_read_only_key_cannot_spend_model_quota(self):
        key, _ = api_keys.issue("read-only", list(api_keys.READ_ONLY), ttl_days=1)
        with EnvironmentOverride(FLEET_API_KEY="unused-shared", FLEET_CONSOLE_ACCESS_CODE="") as service:
            service.REQUEST_LIMITER.reset()
            with self._client(service) as client:
                headers = {"x-fleet-api-key": key}
                self.assertEqual(client.get("/jobs", headers=headers).status_code, 200)
                self.assertEqual(client.get("/audit/verify", headers=headers).status_code, 200)

                blocked = client.post("/jobs", json={"crn": "03994971"}, headers=headers)
                self.assertEqual(blocked.status_code, 403)
                self.assertIn("audits:write", blocked.json()["error"])

                notes = client.post(
                    "/memory/03994971/notes", json={"note": "x"}, headers=headers
                )
                self.assertEqual(notes.status_code, 403)

    def test_a_write_key_without_governance_cannot_read_the_audit_trail(self):
        key, _ = api_keys.issue(
            "submitter", [api_keys.SCOPE_AUDITS_READ, api_keys.SCOPE_AUDITS_WRITE], ttl_days=1
        )
        with EnvironmentOverride(FLEET_API_KEY="unused-shared", FLEET_CONSOLE_ACCESS_CODE="") as service:
            service.REQUEST_LIMITER.reset()
            with self._client(service) as client:
                headers = {"x-fleet-api-key": key}
                self.assertEqual(client.get("/jobs", headers=headers).status_code, 200)
                self.assertEqual(client.get("/audit", headers=headers).status_code, 403)

    def test_an_invalid_or_expired_key_is_unauthenticated_not_merely_unauthorised(self):
        with EnvironmentOverride(FLEET_API_KEY="unused-shared", FLEET_CONSOLE_ACCESS_CODE="") as service:
            with self._client(service) as client:
                for candidate in [api_keys.PREFIX + "forged.signature", "not-a-key"]:
                    response = client.get("/jobs", headers={"x-fleet-api-key": candidate})
                    self.assertEqual(response.status_code, 401, candidate)

    def test_whoami_reports_what_the_key_grants(self):
        key, issued = api_keys.issue("integration", [api_keys.SCOPE_AUDITS_READ], ttl_days=3)
        with EnvironmentOverride(FLEET_API_KEY="unused-shared", FLEET_CONSOLE_ACCESS_CODE="") as service:
            service.REQUEST_LIMITER.reset()
            with self._client(service) as client:
                body = client.get("/api/whoami", headers={"x-fleet-api-key": key}).json()
        self.assertTrue(body["authenticated"])
        self.assertEqual(body["name"], "integration")
        self.assertEqual(body["key_id"], issued.key_id)
        self.assertEqual(body["scopes"], [api_keys.SCOPE_AUDITS_READ])
        self.assertIn("audits_used_this_hour", body)

    def test_whoami_refuses_an_anonymous_caller_on_a_locked_service(self):
        with EnvironmentOverride(FLEET_API_KEY="unused-shared", FLEET_CONSOLE_ACCESS_CODE="") as service:
            with self._client(service) as client:
                self.assertEqual(client.get("/api/whoami").status_code, 401)

    def test_the_legacy_shared_secret_still_works(self):
        """Existing deployments must not break when keys are introduced."""

        with EnvironmentOverride(FLEET_API_KEY="legacy-secret", FLEET_CONSOLE_ACCESS_CODE="") as service:
            service.REQUEST_LIMITER.reset()
            with self._client(service) as client:
                body = client.get("/api/whoami", headers={"x-fleet-api-key": "legacy-secret"}).json()
        self.assertEqual(body["kind"], "legacy_key")


class RequestBudgetTests(unittest.TestCase):
    def test_exhausting_the_request_budget_returns_429_with_retry_after(self):
        from starlette.testclient import TestClient

        key, _ = api_keys.issue(
            "tiny-budget", [api_keys.SCOPE_AUDITS_READ], ttl_days=1, requests_per_hour=3
        )
        with EnvironmentOverride(FLEET_API_KEY="unused-shared", FLEET_CONSOLE_ACCESS_CODE="") as service:
            service.REQUEST_LIMITER.reset()
            with TestClient(service.app) as client:
                headers = {"x-fleet-api-key": key}
                statuses = [client.get("/jobs", headers=headers).status_code for _ in range(5)]
                blocked = client.get("/jobs", headers=headers)
        self.assertEqual(statuses[:3], [200, 200, 200])
        self.assertEqual(statuses[3:], [429, 429])
        self.assertTrue(blocked.headers.get("retry-after"))


class CommandLineTests(unittest.TestCase):
    def test_issue_and_verify_through_the_cli(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(
                api_keys._main(["issue", "--name", "cli-test", "--scopes", "audits:read", "--days", "2"]),
                0,
            )
        key = buffer.getvalue().splitlines()[0].strip()
        self.assertTrue(key.startswith(api_keys.PREFIX))
        self.assertEqual(api_keys.verify(key).name, "cli-test")

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(api_keys._main(["verify", key]), 0)
            self.assertEqual(api_keys._main(["verify", "rubbish"]), 1)


if __name__ == "__main__":
    unittest.main()
