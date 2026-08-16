"""Backend abstraction so any client can drive the fleet locally or over HTTP.

The console should not *be* the fleet; it should be a client of it. This module
exposes one interface with two implementations:

- `RemoteBackend` talks to the Cloud Run control plane. This is the real
  deployment shape: one governed backend, many clients, one job store, one audit
  chain.
- `LocalBackend` runs the graph in-process, for development and offline demos.

Selection is by environment: set FLEET_API_URL to point any client at a deployed
control plane. Authentication is resolved in this order:

1. FLEET_API_TOKEN            - a bearer token you supply (e.g. from
                                `gcloud auth print-identity-token`)
2. Cloud Run metadata server  - service-to-service identity tokens, minted for
                                the target audience and refreshed automatically
3. FLEET_API_KEY              - the x-fleet-api-key shared secret
4. none                       - only valid for an unauthenticated service
"""

from __future__ import annotations

import os
import time
from typing import Any, Protocol

import requests

FLEET_API_URL = os.getenv("FLEET_API_URL", "").rstrip("/")
REQUEST_TIMEOUT = float(os.getenv("FLEET_API_TIMEOUT_SECONDS", "30"))
METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)


class FleetBackend(Protocol):
    """The operations every client surface needs."""

    mode: str
    description: str

    def submit_job(self, crn: str, data_room_path: str, submitted_by: str) -> str: ...
    def get_job(self, job_id: str) -> dict[str, Any] | None: ...
    def list_jobs(self, limit: int = 25) -> list[dict[str, Any]]: ...
    def cancel_job(self, job_id: str) -> dict[str, Any]: ...
    def fleet(self) -> dict[str, Any]: ...
    def audit(self, limit: int = 300, trace_id: str | None = None) -> list[dict[str, Any]]: ...
    def verify_audit(self) -> dict[str, Any]: ...
    def memory(self, crn: str) -> dict[str, Any]: ...
    def add_note(self, crn: str, note: str, author: str) -> dict[str, Any]: ...
    def supports_uploads(self) -> bool: ...


class LocalBackend:
    """Runs the fleet in this process. Development and offline use."""

    mode = "local"

    def __init__(self) -> None:
        import agent_identity
        import agent_registry
        import gateway
        import memory_bank
        import orchestrator
        import runtime
        import telemetry

        self._identity = agent_identity
        self._registry = agent_registry
        self._gateway = gateway
        self._memory = memory_bank
        self._runtime = runtime
        self._telemetry = telemetry

        telemetry.configure_telemetry()
        orchestrator.bootstrap_fleet()
        runtime.reconcile_interrupted_jobs()

    @property
    def description(self) -> str:
        return "In-process fleet (this machine)"

    def submit_job(self, crn: str, data_room_path: str, submitted_by: str = "dashboard") -> str:
        return self._runtime.submit_job(
            crn, data_room_path=data_room_path, submitted_by=submitted_by
        )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self._runtime.get_job(job_id)

    def list_jobs(self, limit: int = 25) -> list[dict[str, Any]]:
        return self._runtime.list_jobs(limit=limit)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._runtime.cancel_job(job_id)

    def fleet(self) -> dict[str, Any]:
        return {
            "agents": self._registry.list_agents(include_retired=True),
            "identities": self._identity.fleet_roster(),
            "tools": self._gateway.registered_tools(),
            "allowed_egress_hosts": list(self._gateway.allowed_hosts()),
            "runtime": self._runtime.fleet_stats(),
        }

    def audit(self, limit: int = 300, trace_id: str | None = None) -> list[dict[str, Any]]:
        return self._telemetry.read_audit(limit=limit, trace_id=trace_id)

    def verify_audit(self) -> dict[str, Any]:
        return self._telemetry.verify_audit_chain()

    def memory(self, crn: str) -> dict[str, Any]:
        return self._memory.recall(crn, actor="dashboard")

    def add_note(self, crn: str, note: str, author: str = "dashboard") -> dict[str, Any]:
        return self._memory.add_note(crn, note, author=author)

    def supports_uploads(self) -> bool:
        return True


class RemoteBackend:
    """Talks to the Cloud Run control plane over HTTP."""

    mode = "remote"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._token: str = ""
        self._token_expires_at: float = 0.0

    @property
    def description(self) -> str:
        return f"Cloud Run control plane ({self.base_url})"

    # -- authentication --------------------------------------------------
    def _metadata_token(self) -> str:
        """Mint an identity token for this service's own audience, on Cloud Run."""

        if self._token and time.time() < self._token_expires_at:
            return self._token
        try:
            response = requests.get(
                METADATA_IDENTITY_URL,
                params={"audience": self.base_url},
                headers={"Metadata-Flavor": "Google"},
                timeout=5,
            )
            if response.status_code == 200 and response.text.strip():
                self._token = response.text.strip()
                # Identity tokens last an hour; refresh well before expiry.
                self._token_expires_at = time.time() + 2700
                return self._token
        except requests.RequestException:
            pass
        return ""

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        supplied = os.getenv("FLEET_API_TOKEN", "").strip()
        token = supplied or self._metadata_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        api_key = os.getenv("FLEET_API_KEY", "").strip()
        if api_key:
            headers["x-fleet-api-key"] = api_key
        return headers

    # -- transport -------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = requests.request(
            method,
            f"{self.base_url}{path}",
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )
        if response.status_code == 401:
            raise PermissionError(
                "The control plane rejected this client. Supply FLEET_API_TOKEN "
                "(gcloud auth print-identity-token) or FLEET_API_KEY."
            )
        if response.status_code == 403:
            raise PermissionError(
                "Access to the control plane is forbidden. The caller needs the "
                "Cloud Run Invoker role on this service."
            )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    # -- operations ------------------------------------------------------
    def submit_job(self, crn: str, data_room_path: str, submitted_by: str = "dashboard") -> str:
        payload = {"crn": crn, "data_room_path": data_room_path, "submitted_by": submitted_by}
        return self._request("POST", "/jobs", json=payload)["job_id"]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self._request("GET", f"/jobs/{job_id}")

    def list_jobs(self, limit: int = 25) -> list[dict[str, Any]]:
        result = self._request("GET", "/jobs", params={"limit": limit}) or {}
        return result.get("jobs", [])

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._request("POST", f"/jobs/{job_id}/cancel") or {}

    def fleet(self) -> dict[str, Any]:
        return self._request("GET", "/fleet") or {}

    def audit(self, limit: int = 300, trace_id: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if trace_id:
            params["trace_id"] = trace_id
        result = self._request("GET", "/audit", params=params) or {}
        return result.get("records", [])

    def verify_audit(self) -> dict[str, Any]:
        return self._request("GET", "/audit/verify") or {}

    def memory(self, crn: str) -> dict[str, Any]:
        return self._request("GET", f"/memory/{crn}") or {}

    def add_note(self, crn: str, note: str, author: str = "dashboard") -> dict[str, Any]:
        return self._request("POST", f"/memory/{crn}/notes", json={"note": note, "author": author}) or {}

    def supports_uploads(self) -> bool:
        # The control plane reads data rooms from its own filesystem; uploading
        # into a remote container would need an object-store handoff.
        return False

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/readyz") or {}


def get_backend(api_url: str | None = None) -> FleetBackend:
    """Return a remote backend when a control plane URL is configured."""

    url = (api_url if api_url is not None else FLEET_API_URL).strip()
    if url:
        return RemoteBackend(url)
    return LocalBackend()
