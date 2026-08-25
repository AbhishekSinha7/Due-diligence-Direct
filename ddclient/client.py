"""The DueDiligence Direct API client.

The fleet is a governed service, not a library: work is submitted, runs
server-side under agent identities, and is recorded in one audit chain. This
client is a thin, typed way to drive that service — it never runs an agent
locally, and it never reinterprets what the fleet returns.

    from ddclient import DueDiligenceClient

    with DueDiligenceClient("https://fleet.example.run.app", access_code="...") as fleet:
        report = fleet.run("03994971", on_event=print)
        print(report.recommendation)
"""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

import requests

from .errors import (
    APIError,
    AuthenticationError,
    JobFailed,
    NotFound,
    PolicyDenied,
    TransportError,
    WaitTimeout,
)
from .models import CompanySearchResult, Job, JobPage, Report

METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)

DEFAULT_TIMEOUT = 30.0
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_WAIT_TIMEOUT = 900.0

ALLOWED_UPLOAD_SUFFIXES = frozenset({".csv", ".md", ".pdf", ".txt"})

EventCallback = Callable[[dict[str, Any]], None]


class DueDiligenceClient:
    """A client for one fleet control plane.

    Authentication is resolved once, in this order:

    1. `access_code`   - exchanged for a session cookie, as the console does
    2. `token`         - a bearer token you supply, or FLEET_API_TOKEN
    3. metadata server - a Cloud Run identity token, minted for this audience
    4. `api_key`       - the x-fleet-api-key shared secret, or FLEET_API_KEY
    5. none            - valid only for an unauthenticated service

    The client holds a `requests.Session`, so it keeps the session cookie and
    reuses connections. Use it as a context manager, or call `close()`.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        access_code: str | None = None,
        api_key: str | None = None,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = "ddclient/1.0",
    ) -> None:
        url = (base_url if base_url is not None else os.getenv("FLEET_API_URL", "")).strip()
        if not url:
            raise ValueError(
                "A control plane URL is required. Pass base_url, or set FLEET_API_URL."
            )
        self.base_url = url.rstrip("/")
        self.timeout = timeout

        self._api_key = (api_key if api_key is not None else os.getenv("FLEET_API_KEY", "")).strip()
        self._token = (token if token is not None else os.getenv("FLEET_API_TOKEN", "")).strip()
        self._metadata_token = ""
        self._metadata_expires_at = 0.0

        self._session = requests.Session()
        self._session.headers.update({"user-agent": user_agent, "accept": "application/json"})

        code = (
            access_code if access_code is not None else os.getenv("FLEET_CONSOLE_ACCESS_CODE", "")
        ).strip()
        if code:
            self.sign_in(code)

    # -- lifecycle -------------------------------------------------------
    def __enter__(self) -> "DueDiligenceClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()

    def __repr__(self) -> str:  # pragma: no cover - convenience
        return f"<DueDiligenceClient {self.base_url}>"

    # -- authentication --------------------------------------------------
    def sign_in(self, access_code: str) -> None:
        """Exchange the shared access code for a session cookie."""

        self._request("POST", "/api/session", json={"code": access_code})

    def _identity_token(self) -> str:
        """Mint a Cloud Run identity token, when running on Google Cloud."""

        if self._metadata_token and time.time() < self._metadata_expires_at:
            return self._metadata_token
        try:
            response = requests.get(
                METADATA_IDENTITY_URL,
                params={"audience": self.base_url},
                headers={"Metadata-Flavor": "Google"},
                timeout=5,
            )
            if response.status_code == 200 and response.text.strip():
                self._metadata_token = response.text.strip()
                # Identity tokens last an hour; refresh well before expiry.
                self._metadata_expires_at = time.time() + 2700
                return self._metadata_token
        except requests.RequestException:
            pass
        return ""

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        token = self._token or self._identity_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self._api_key:
            headers["x-fleet-api-key"] = self._api_key
        return headers

    # -- transport -------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = self._auth_headers()
        headers.update(kwargs.pop("headers", {}) or {})
        try:
            response = self._session.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                timeout=kwargs.pop("timeout", self.timeout),
                **kwargs,
            )
        except requests.RequestException as exc:
            raise TransportError(f"Could not reach {self.base_url}: {exc}") from exc
        return self._unwrap(response)

    @staticmethod
    def _unwrap(response: requests.Response) -> Any:
        if response.status_code == 204:
            return None

        payload: Any = None
        if response.content:
            try:
                payload = response.json()
            except ValueError:
                payload = {"error": response.text[:500]}

        if response.ok:
            return payload

        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("error") or payload.get("message") or "")
        message = message or f"{response.reason or 'Request failed'}"

        if response.status_code == 401:
            raise AuthenticationError(
                "The control plane rejected this client. Supply an access code, an API key, "
                "or a bearer token.",
                401,
                payload,
            )
        if response.status_code == 403:
            raise PolicyDenied(message, 403, payload)
        if response.status_code == 404:
            raise NotFound(message or "Not found", 404, payload)
        raise APIError(message, response.status_code, payload)

    # -- service ---------------------------------------------------------
    def index(self) -> dict[str, Any]:
        """The service index: version, environment, and every endpoint."""

        return self._request("GET", "/api")

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/healthz")

    def ready(self) -> dict[str, Any]:
        """Readiness, including whether credentials and models are configured."""

        return self._request("GET", "/readyz")

    def whoami(self) -> dict[str, Any]:
        """What this client's credential is and what it grants.

        Name, key id, scopes, expiry, and how much of the hourly budget is
        spent. Worth calling before anything else: discovering your scopes
        through a sequence of 403s is a poor way to learn them.
        """

        return self._request("GET", "/api/whoami")

    def fleet(self) -> dict[str, Any]:
        """Agent cards, identities, tool policies, and runtime statistics."""

        return self._request("GET", "/fleet")

    # -- companies -------------------------------------------------------
    def search_companies(self, query: str, limit: int = 10) -> list[CompanySearchResult]:
        """Find a company on the register by name."""

        payload = self._request(
            "GET", "/companies/search", params={"q": query, "limit": limit}
        )
        results = (payload or {}).get("results") or []
        return [CompanySearchResult.from_payload(item) for item in results if isinstance(item, dict)]

    def find_company(self, query: str) -> CompanySearchResult | None:
        """The single best match for a name, or None.

        Convenience for scripts that know the company by name but not number.
        Returns None rather than guessing when the register has no match.
        """

        results = self.search_companies(query, limit=1)
        return results[0] if results else None

    # -- data rooms ------------------------------------------------------
    def upload_documents(
        self, documents: Iterable[str | Path | tuple[str, bytes]]
    ) -> str:
        """Upload deal documents and return the data room path to audit against.

        Accepts paths or explicit `(name, bytes)` pairs. Documents are untrusted
        by definition: the fleet screens them at ingestion, and this method does
        not pre-process them beyond checking the extension the API will accept.
        """

        files: list[dict[str, str]] = []
        for item in documents:
            if isinstance(item, tuple):
                name, content = item
                name = Path(str(name)).name
            else:
                path = Path(item)
                if not path.is_file():
                    raise FileNotFoundError(f"No such document: {path}")
                name, content = path.name, path.read_bytes()

            suffix = Path(name).suffix.lower()
            if suffix not in ALLOWED_UPLOAD_SUFFIXES:
                raise ValueError(
                    f"{name}: the fleet accepts "
                    f"{', '.join(sorted(ALLOWED_UPLOAD_SUFFIXES))} documents"
                )
            files.append(
                {"name": name, "content_base64": base64.b64encode(content).decode("ascii")}
            )

        if not files:
            raise ValueError("No documents were supplied.")

        payload = self._request(
            "POST", "/data-rooms", json={"files": files, "submitted_by": "ddclient"}
        )
        return str((payload or {}).get("data_room_path", ""))

    # -- jobs ------------------------------------------------------------
    def submit(
        self,
        crn: str,
        *,
        documents: Sequence[str | Path | tuple[str, bytes]] | None = None,
        data_room_path: str = "",
        submitted_by: str = "ddclient",
    ) -> Job:
        """Queue an audit and return immediately.

        The run continues on the fleet whether or not this process stays alive.
        Pass `documents` to upload deal papers first, or `data_room_path` to
        reuse a data room already on the fleet.
        """

        if documents:
            if data_room_path:
                raise ValueError("Pass documents or data_room_path, not both.")
            data_room_path = self.upload_documents(documents)

        payload = self._request(
            "POST",
            "/jobs",
            json={
                "crn": str(crn).strip().upper(),
                "data_room_path": data_room_path,
                "submitted_by": submitted_by,
            },
        )
        return Job(payload or {})

    def get_job(self, job_id: str) -> Job:
        """Fetch a job's current status, events, and report."""

        return Job(self._request("GET", f"/jobs/{job_id}") or {})

    def list_jobs(
        self,
        limit: int = 25,
        crn: str | None = None,
        *,
        status: str | None = None,
        offset: int = 0,
        include_result: bool = True,
        query: str | None = None,
    ) -> list[Job]:
        """One page of audits, newest first.

        `query` matches the audited company's name or number, for looking through
        a history. `crn` is an exact filter. `include_result=False` returns a
        compact `summary` instead of the full report on each job, which is what
        you want for anything listing rather than reading. Use `job_page` if you
        also need the total.
        """

        return list(
            self.job_page(
                limit=limit,
                crn=crn,
                status=status,
                offset=offset,
                include_result=include_result,
                query=query,
            ).jobs
        )

    def job_page(
        self,
        limit: int = 25,
        crn: str | None = None,
        *,
        status: str | None = None,
        offset: int = 0,
        include_result: bool = True,
        query: str | None = None,
    ) -> JobPage:
        """A page of audits together with the total number that match."""

        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if crn:
            params["crn"] = crn
        if status:
            params["status"] = status
        if query:
            params["q"] = query
        if not include_result:
            params["include_result"] = "false"
        payload = self._request("GET", "/jobs", params=params) or {}
        jobs = tuple(Job(item) for item in payload.get("jobs", []) if isinstance(item, dict))
        return JobPage(
            jobs=jobs,
            total=int(payload.get("total", len(jobs))),
            limit=int(payload.get("limit", limit)),
            offset=int(payload.get("offset", offset)),
        )

    def iter_jobs(
        self,
        crn: str | None = None,
        *,
        status: str | None = None,
        page_size: int = 50,
        include_result: bool = False,
        max_jobs: int | None = None,
        query: str | None = None,
    ) -> Iterator[Job]:
        """Every audit that matches, fetched a page at a time.

        Defaults to summaries because walking a full history while pulling down
        every complete report is rarely what anyone means.
        """

        offset = 0
        seen = 0
        while True:
            page = self.job_page(
                limit=page_size,
                crn=crn,
                status=status,
                offset=offset,
                include_result=include_result,
                query=query,
            )
            if not page.jobs:
                return
            for job in page.jobs:
                yield job
                seen += 1
                if max_jobs is not None and seen >= max_jobs:
                    return
            if not page.has_more:
                return
            offset = page.next_offset

    def count_jobs(
        self, crn: str | None = None, *, status: str | None = None, query: str | None = None
    ) -> int:
        """How many audits match, without fetching them."""

        return self.job_page(
            limit=1, crn=crn, status=status, query=query, include_result=False
        ).total

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Ask the fleet to stop a run at its next stage boundary."""

        return self._request("POST", f"/jobs/{job_id}/cancel") or {}

    def wait_for(
        self,
        job_id: str,
        *,
        timeout: float = DEFAULT_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        on_event: EventCallback | None = None,
    ) -> Job:
        """Poll until the job reaches a terminal state.

        `on_event` is called once per new event as it appears, so a caller can
        show progress without writing its own polling loop. Raising WaitTimeout
        does not stop the run — it is still executing on the fleet.
        """

        deadline = time.monotonic() + timeout
        seen = 0
        job = self.get_job(job_id)
        while True:
            events = job.events
            if on_event is not None and len(events) > seen:
                for event in events[seen:]:
                    on_event(event)
            seen = len(events)

            if job.is_terminal:
                return job

            waited = timeout - (deadline - time.monotonic())
            if time.monotonic() >= deadline:
                raise WaitTimeout(job, waited)

            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
            job = self.get_job(job_id)

    def run(
        self,
        crn: str,
        *,
        documents: Sequence[str | Path | tuple[str, bytes]] | None = None,
        data_room_path: str = "",
        submitted_by: str = "ddclient",
        timeout: float = DEFAULT_WAIT_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        on_event: EventCallback | None = None,
        raise_on_failure: bool = True,
    ) -> Report:
        """Submit an audit, wait for it, and return the finished report.

        The one-call path for scripts. For anything that should survive the
        caller exiting, use `submit()` and come back with `get_job()` later.
        """

        job = self.submit(
            crn, documents=documents, data_room_path=data_room_path, submitted_by=submitted_by
        )
        finished = self.wait_for(
            job.job_id, timeout=timeout, poll_interval=poll_interval, on_event=on_event
        )
        report = finished.report
        if report is None:
            if raise_on_failure:
                raise JobFailed(finished)
            return Report({})
        return report

    # -- reports ---------------------------------------------------------
    def report_pdf(self, job_id: str) -> bytes:
        """The Red Flag Report as a PDF, rendered by the fleet."""

        headers = self._auth_headers()
        headers["accept"] = "application/pdf"
        try:
            response = self._session.get(
                f"{self.base_url}/jobs/{job_id}/report.pdf",
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise TransportError(f"Could not reach {self.base_url}: {exc}") from exc
        if not response.ok:
            self._unwrap(response)  # raises the right error type
        return response.content

    def save_report_pdf(self, job_id: str, path: str | Path) -> Path:
        """Write the PDF to disk and return where it landed."""

        destination = Path(path)
        if destination.is_dir():
            destination = destination / f"red-flag-report-{job_id}.pdf"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.report_pdf(job_id))
        return destination

    # -- memory ----------------------------------------------------------
    def memory(self, crn: str) -> dict[str, Any]:
        """What the fleet remembers about a company between audits."""

        return self._request("GET", f"/memory/{crn}") or {}

    def add_note(self, crn: str, note: str, author: str = "ddclient") -> dict[str, Any]:
        """Attach an operator note that later audits will see."""

        return self._request(
            "POST", f"/memory/{crn}/notes", json={"note": note, "author": author}
        ) or {}

    # -- audit trail -----------------------------------------------------
    def audit_records(
        self, limit: int = 200, trace_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Audit log records, newest last. Filter by trace to follow one run."""

        params: dict[str, Any] = {"limit": limit}
        if trace_id:
            params["trace_id"] = trace_id
        payload = self._request("GET", "/audit", params=params) or {}
        return [item for item in payload.get("records", []) if isinstance(item, dict)]

    def verify_audit_chain(self) -> dict[str, Any]:
        """Recompute the hash chain. `{"valid": true}` means nothing was altered."""

        return self._request("GET", "/audit/verify") or {}
