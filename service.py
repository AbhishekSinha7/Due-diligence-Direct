"""Fleet control plane: the HTTP surface deployed to Google Cloud Run.

This is what makes the fleet institutional rather than a laptop script. A client
submits a diligence job and gets a job id back immediately; the run continues in
the Agent Runtime on the server. The same service exposes the registry, the audit
trail, the memory bank, and health endpoints for Cloud Run probes.

Run locally:
    python -m uvicorn service:app --port 8080

Endpoints:
    GET  /healthz                 liveness probe
    GET  /readyz                  readiness probe with dependency status
    GET  /fleet                   registry cards, identities, tools, runtime stats
    POST /jobs                    {"crn": "...", "data_room_path": "..."} -> job id
    GET  /jobs                    recent jobs
    GET  /jobs/{job_id}           job status, stage events, and final report
    POST /jobs/{job_id}/cancel    cooperative cancellation
    GET  /memory/{crn}            prior audits, notes, and tracked-fact deltas
    POST /memory/{crn}/notes      {"note": "...", "author": "..."}
    GET  /audit                   recent audit records (?trace_id= to filter)
    GET  /audit/verify            recompute the audit hash chain
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import agent_identity
import agent_registry
import api_keys
import gateway
import mcp_server
import memory_bank
import orchestrator
import runtime
import security
import telemetry

API_KEY = os.getenv("FLEET_API_KEY", "").strip()
CONSOLE_ACCESS_CODE = os.getenv("FLEET_CONSOLE_ACCESS_CODE", "").strip()
CONSOLE_COOKIE = "fleet_console"
WEB_ROOT = Path(__file__).parent / "web"

# Bounds runaway model spend: a signed-in caller can still queue work, but not
# an unbounded amount of it.
MAX_PENDING_JOBS = int(os.getenv("FLEET_MAX_PENDING_JOBS", "25"))
MAX_PAGE_SIZE = int(os.getenv("FLEET_MAX_PAGE_SIZE", "100"))

SIGNIN_THROTTLE = security.SignInThrottle()
REQUEST_LIMITER = security.RateLimiter()
AUDIT_LIMITER = security.RateLimiter()


def _console_token() -> str:
    """A cookie value derived from the access code, so the code itself never
    travels back to the browser and cannot be read out of storage."""

    material = f"console:{CONSOLE_ACCESS_CODE}".encode("utf-8")
    return hmac.new(agent_identity._signing_key(), material, hashlib.sha256).hexdigest()


def _presented_secret(request: Request) -> str:
    """The credential the caller sent, from either accepted header."""

    presented = request.headers.get("x-fleet-api-key", "")
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        presented = presented or authorization[7:]
    return presented


def _principal(request: Request) -> api_keys.Principal | None:
    """Identify the caller, or None if they are not authenticated.

    Four routes in, in order of specificity: an issued per-caller key, the legacy
    shared secret, a console session cookie, and finally an unconfigured
    deployment which is open by design for local development.
    """

    presented = _presented_secret(request)
    if presented:
        if presented.startswith(api_keys.PREFIX):
            try:
                return api_keys.verify(presented)
            except api_keys.InvalidKey:
                return None
        # A Cloud Run identity token also arrives as a bearer credential. IAM has
        # already validated it before the request reaches this process, so it is
        # only meaningful here when it matches the shared secret.
        if API_KEY and hmac.compare_digest(presented, API_KEY):
            return api_keys.legacy_principal()

    if CONSOLE_ACCESS_CODE:
        cookie = request.cookies.get(CONSOLE_COOKIE, "")
        if cookie and hmac.compare_digest(cookie, _console_token()):
            return api_keys.console_principal()

    if not API_KEY and not CONSOLE_ACCESS_CODE:
        return api_keys.open_principal()

    return None


def _authorized(request: Request) -> bool:
    """Whether the caller is authenticated at all. Retained for compatibility."""

    return _principal(request) is not None


def _deny(request: Request, reason: str, status: int, message: str, actor: str = "anonymous", **headers: str):
    telemetry.audit(
        "service.request",
        actor=actor,
        resource=str(request.url.path),
        decision="deny",
        severity="WARN",
        attributes={"reason": reason},
    )
    return JSONResponse({"error": message}, status_code=status, headers=headers or None)


def _guard(request: Request, scope: str | None = None) -> JSONResponse | None:
    """Authenticate, authorise, and meter one request.

    Returns a response to send instead of handling the request, or None to
    proceed. On success the caller is left on `request.state.principal` so
    handlers can attribute what they do.
    """

    principal = _principal(request)
    if principal is None:
        return _deny(request, "missing_or_invalid_credential", 401, "unauthorized")

    if scope and not principal.has_scope(scope):
        return _deny(
            request,
            "insufficient_scope",
            403,
            f"This key lacks the {scope} scope.",
            actor=principal.actor,
        )

    wait = REQUEST_LIMITER.check(principal.key_id, principal.requests_per_hour)
    if wait:
        return _deny(
            request,
            "rate_limited",
            429,
            f"Request budget exhausted. Try again in {wait} seconds.",
            actor=principal.actor,
            **{"retry-after": str(wait)},
        )

    request.state.principal = principal
    return None


def _caller(request: Request) -> api_keys.Principal:
    """The principal established by `_guard`."""

    return getattr(request.state, "principal", None) or api_keys.open_principal()


async def root(request: Request):
    """The console for people, the API index for machines.

    A browser asks for text/html and gets the single-page console; curl and every
    other client keeps the documented JSON index at exactly the same URL.
    """

    if "text/html" in request.headers.get("accept", ""):
        return FileResponse(WEB_ROOT / "index.html")
    return await api_index(request)


async def api_docs(request: Request):
    """The rendered API reference. Ungated: a contract nobody can read is not one."""

    return FileResponse(WEB_ROOT / "docs.html")


async def openapi_spec(request: Request) -> JSONResponse:
    """The machine-readable contract, pinned to whatever URL served it.

    Pinning matters: the reference's playground sends real requests, and a
    hardcoded server URL would point a reader's experiments at the wrong fleet.
    """

    import openapi

    origin = os.getenv("FLEET_PUBLIC_URL", "").strip()
    if not origin:
        forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("host", "")
        origin = f"{forwarded_proto}://{host}" if host else ""
    return JSONResponse(openapi.build_spec(origin))


async def whoami(request: Request) -> JSONResponse:
    """What the presented credential is and what it grants.

    Anything holding a key needs a way to answer "what am I allowed to do"
    without discovering it through a sequence of 403s.
    """

    principal = _principal(request)
    if principal is None:
        return JSONResponse({"authenticated": False}, status_code=401)
    return JSONResponse(
        {
            "authenticated": True,
            **principal.describe(),
            "requests_used_this_hour": REQUEST_LIMITER.used(principal.key_id),
            "audits_used_this_hour": AUDIT_LIMITER.used(principal.key_id),
        }
    )


async def console_session(request: Request) -> JSONResponse:
    """Exchange the shared access code for a session cookie.

    Unauthenticated by necessity: this is the endpoint that grants access. It is
    the only one, and it hands back nothing but a cookie.
    """

    if request.method == "GET":
        return JSONResponse(
            {
                "locked": bool(CONSOLE_ACCESS_CODE or API_KEY),
                "authenticated": _authorized(request),
            }
        )

    if not CONSOLE_ACCESS_CODE:
        return JSONResponse({"error": "console sign-in is not configured"}, status_code=404)

    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    caller = security.client_fingerprint(request)
    wait = SIGNIN_THROTTLE.retry_after(caller)
    if wait:
        telemetry.audit(
            "console.signin",
            actor=caller,
            resource="console://session",
            decision="deny",
            severity="WARN",
            attributes={"reason": "rate_limited", "retry_after_seconds": wait},
        )
        return JSONResponse(
            {"error": f"Too many sign-in attempts. Try again in {wait} seconds."},
            status_code=429,
            headers={"retry-after": str(wait)},
        )

    supplied = str(payload.get("code", ""))
    if not hmac.compare_digest(supplied, CONSOLE_ACCESS_CODE):
        SIGNIN_THROTTLE.record_failure(caller)
        telemetry.audit(
            "console.signin",
            actor=caller,
            resource="console://session",
            decision="deny",
            severity="WARN",
            attributes={"reason": "bad_access_code"},
        )
        return JSONResponse({"error": "access code not recognised"}, status_code=401)

    SIGNIN_THROTTLE.record_success(caller)
    telemetry.audit(
        "console.signin",
        actor="console",
        resource="console://session",
        decision="allow",
    )
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        CONSOLE_COOKIE,
        _console_token(),
        max_age=12 * 60 * 60,
        httponly=True,
        samesite="strict",
        # Behind Cloud Run's TLS termination the request looks like plain HTTP,
        # so the forwarded header decides this rather than request.url.scheme.
        secure=security.is_secure_request(request),
        path="/",
    )
    return response


async def api_index(request: Request) -> JSONResponse:
    """Service index. Safe to expose: it describes the API, never the data."""

    agents = agent_registry.list_agents()
    return JSONResponse(
        {
            "service": telemetry.SERVICE_NAME,
            "description": (
                "Governed multi-agent M&A due diligence fleet over UK Companies House "
                "statutory records and filed iXBRL accounts."
            ),
            "version": telemetry.SERVICE_VERSION,
            "environment": os.getenv("FLEET_ENVIRONMENT", "local"),
            "region": os.getenv("FLEET_DATA_REGION", "unset"),
            "model": os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
            "agents_registered": len(agents),
            "agents": [f"{card['agent_id']}@{card['version']}" for card in agents],
            "endpoints": {
                "GET /": "the operator console (HTML) or this index (JSON)",
                "GET /api/whoami": "what the presented credential grants",
                "GET /docs": "API reference and playground",
                "GET /openapi.json": "the OpenAPI 3.1 contract",
                "GET /healthz": "liveness probe",
                "GET /readyz": "readiness and dependency status",
                "GET /fleet": "agent cards, identities, tool policies, runtime stats",
                "GET /companies/search?q=": "find a company by name -> company numbers",
                "POST /data-rooms": "upload deal documents -> data_room_path",
                "POST /jobs": "submit an audit: {\"crn\": \"03994971\"} -> job id",
                "GET /jobs": "recent jobs",
                "GET /jobs/{job_id}": "status, stage events, final report",
                "GET /jobs/{job_id}/report.pdf": "download the Red Flag Report as a PDF",
                "POST /jobs/{job_id}/cancel": "cooperative cancellation",
                "GET /memory/{crn}": "prior audits, notes, tracked-fact deltas",
                "POST /memory/{crn}/notes": "add an operator note",
                "GET /audit": "audit records (?trace_id= to filter)",
                "GET /audit/verify": "recompute the audit hash chain",
            },
            "source": "https://github.com/AbhishekSinha7/Due-diligence-Direct",
        }
    )


async def healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": telemetry.SERVICE_NAME})


async def readyz(request: Request) -> JSONResponse:
    agents = agent_registry.list_agents()
    return JSONResponse(
        {
            "status": "ready" if agents else "degraded",
            "registered_agents": len(agents),
            "model_candidates": list(orchestrator.MODEL_CANDIDATES),
            "companies_house_configured": bool(os.getenv("COMPANIES_HOUSE_API_KEY", "").strip()),
            "model_configured": bool(
                os.getenv("GEMINI_API_KEY", "").strip()
                or os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in {"1", "true", "yes"}
            ),
            "runtime": runtime.fleet_stats(),
        }
    )


async def search_companies(request: Request) -> JSONResponse:
    """Resolve a company by name. Routed through the gateway like any other tool."""

    if (denied := _guard(request, api_keys.SCOPE_AUDITS_READ)) is not None:
        return denied
    query = request.query_params.get("q", "")
    limit = int(request.query_params.get("limit", "10"))
    try:
        return JSONResponse(
            gateway.call("orchestrator", "search_companies", query=query, limit=limit)
        )
    except gateway.PolicyViolation as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)


async def fleet(request: Request) -> JSONResponse:
    if (denied := _guard(request, api_keys.SCOPE_AUDITS_READ)) is not None:
        return denied
    return JSONResponse(
        {
            "agents": agent_registry.list_agents(),
            "identities": agent_identity.fleet_roster(),
            "tools": gateway.registered_tools(),
            "allowed_egress_hosts": list(gateway.allowed_hosts()),
            "runtime": runtime.fleet_stats(),
        }
    )


MAX_UPLOAD_BYTES = int(os.getenv("FLEET_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))
MAX_UPLOAD_FILES = int(os.getenv("FLEET_MAX_UPLOAD_FILES", "25"))
UPLOAD_ROOT = Path(os.getenv("FLEET_UPLOAD_ROOT", "data_room/uploads"))
ALLOWED_UPLOAD_EXTENSIONS = {".csv", ".md", ".pdf", ".txt"}


async def upload_data_room(request: Request) -> JSONResponse:
    """Accept deal documents and return the data room path to audit against.

    Files are written to a per-upload folder on the fleet's own disk, which is
    what the ingestion tool reads. Uploads are untrusted by definition, so this
    endpoint only stores them; Model Armor screens the contents at ingestion.
    """

    if (denied := _guard(request, api_keys.SCOPE_AUDITS_WRITE)) is not None:
        return denied
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return JSONResponse({"error": "files must be a non-empty list"}, status_code=400)
    if len(files) > MAX_UPLOAD_FILES:
        return JSONResponse(
            {"error": f"at most {MAX_UPLOAD_FILES} files per upload"}, status_code=413
        )

    room_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    root = UPLOAD_ROOT / room_id
    stored: list[dict[str, Any]] = []
    total_bytes = 0

    for entry in files:
        if not isinstance(entry, dict):
            return JSONResponse({"error": "each file must be an object"}, status_code=400)

        # Strip any path component: an upload must never choose its own location.
        name = Path(str(entry.get("name", ""))).name
        if not name:
            return JSONResponse({"error": "each file needs a name"}, status_code=400)
        suffix = Path(name).suffix.lower()
        if suffix not in ALLOWED_UPLOAD_EXTENSIONS:
            return JSONResponse(
                {"error": f"unsupported file type {suffix or name}"}, status_code=415
            )

        try:
            content = base64.b64decode(str(entry.get("content_base64", "")), validate=True)
        except (binascii.Error, ValueError):
            return JSONResponse({"error": f"{name}: content_base64 is not valid base64"}, status_code=400)

        total_bytes += len(content)
        if total_bytes > MAX_UPLOAD_BYTES:
            return JSONResponse(
                {"error": f"upload exceeds {MAX_UPLOAD_BYTES} bytes"}, status_code=413
            )

        root.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content)
        stored.append({"file_name": name, "bytes": len(content)})

    telemetry.audit(
        "data_room.upload",
        actor=str(payload.get("submitted_by", "api")),
        resource=f"data_room://{room_id}",
        decision="allow",
        attributes={"files": len(stored), "bytes": total_bytes},
    )
    return JSONResponse(
        {"data_room_path": str(root).replace("\\", "/"), "room_id": room_id, "files": stored},
        status_code=201,
    )


async def submit(request: Request) -> JSONResponse:
    if (denied := _guard(request, api_keys.SCOPE_AUDITS_WRITE)) is not None:
        return denied
    try:
        payload: dict[str, Any] = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    try:
        query = mcp_server.CompanyQuery(crn=str(payload.get("crn", "")))
    except Exception as exc:
        return JSONResponse({"error": f"invalid crn: {exc}"}, status_code=400)

    caller = _caller(request)
    wait = AUDIT_LIMITER.check(caller.key_id, caller.audits_per_hour)
    if wait:
        return _deny(
            request,
            "audit_budget_exhausted",
            429,
            f"This caller's audit budget is spent. Try again in {wait} seconds.",
            actor=caller.actor,
            **{"retry-after": str(wait)},
        )

    stats = runtime.fleet_stats()
    counts = stats.get("counts", {}) if isinstance(stats, dict) else {}
    pending = int(counts.get(runtime.STATUS_QUEUED, 0)) + int(counts.get(runtime.STATUS_RUNNING, 0))
    if pending >= MAX_PENDING_JOBS:
        telemetry.audit(
            "job.submit",
            actor=str(payload.get("submitted_by", "api")),
            resource=f"company://{query.crn}",
            decision="deny",
            severity="WARN",
            attributes={"reason": "pending_job_limit", "pending": pending},
        )
        return JSONResponse(
            {"error": f"{pending} audits are already queued or running; try again shortly."},
            status_code=429,
            headers={"retry-after": "60"},
        )

    # Documents are optional. Absent a path, the audit runs on statutory records
    # and filed accounts alone rather than scanning the server's working directory.
    job_id = runtime.submit_job(
        query.crn,
        data_room_path=str(payload.get("data_room_path", "") or ""),
        submitted_by=f'{caller.actor}/{payload.get("submitted_by", "api")}',
    )
    return JSONResponse({"job_id": job_id, "crn": query.crn, "status": runtime.STATUS_QUEUED}, status_code=202)


async def jobs(request: Request) -> JSONResponse:
    if (denied := _guard(request, api_keys.SCOPE_AUDITS_READ)) is not None:
        return denied
    params = request.query_params
    # Capped: a page is for reading, and an uncapped one is a way to make the
    # service do unbounded work on request.
    limit = max(1, min(int(params.get("limit", "25")), MAX_PAGE_SIZE))
    offset = max(0, int(params.get("offset", "0")))
    crn = params.get("crn")
    status = params.get("status")
    query = params.get("q")
    if status and status.upper() not in runtime.ALL_STATUSES:
        return JSONResponse(
            {"error": f"unknown status; expected one of {', '.join(sorted(runtime.ALL_STATUSES))}"},
            status_code=400,
        )

    # Defaults to true so the published contract keeps its meaning; list views
    # should pass false and fetch a full report only when one is opened.
    include_result = params.get("include_result", "true").lower() not in {"false", "0", "no"}

    return JSONResponse(
        {
            "jobs": runtime.list_jobs(
                limit=limit,
                crn=crn,
                status=status,
                offset=offset,
                include_result=include_result,
                query=query,
            ),
            "total": runtime.count_jobs(crn=crn, status=status, query=query),
            "limit": limit,
            "offset": offset,
        }
    )


async def job_detail(request: Request) -> JSONResponse:
    if (denied := _guard(request, api_keys.SCOPE_AUDITS_READ)) is not None:
        return denied
    job = runtime.get_job(request.path_params["job_id"])
    if job is None:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return JSONResponse(job)


async def job_report_pdf(request: Request):
    """Download a finished audit as a PDF."""

    if (denied := _guard(request, api_keys.SCOPE_AUDITS_READ)) is not None:
        return denied
    job = runtime.get_job(request.path_params["job_id"])
    if job is None:
        return JSONResponse({"error": "job not found"}, status_code=404)
    if job["status"] != runtime.STATUS_SUCCEEDED or not job.get("result"):
        return JSONResponse(
            {"error": f"job is {job['status']}; no report to export"}, status_code=409
        )

    import report_export

    state = job["result"]
    return Response(
        content=report_export.build_pdf(state),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{report_export.suggested_filename(state)}"'
        },
    )


async def job_cancel(request: Request) -> JSONResponse:
    if (denied := _guard(request, api_keys.SCOPE_AUDITS_WRITE)) is not None:
        return denied
    try:
        return JSONResponse(runtime.cancel_job(request.path_params["job_id"]))
    except KeyError:
        return JSONResponse({"error": "job not found"}, status_code=404)


async def memory_view(request: Request) -> JSONResponse:
    if (denied := _guard(request, api_keys.SCOPE_AUDITS_READ)) is not None:
        return denied
    crn = request.path_params["crn"]
    return JSONResponse(memory_bank.recall(crn, actor="api"))


async def memory_note(request: Request) -> JSONResponse:
    if (denied := _guard(request, api_keys.SCOPE_MEMORY_WRITE)) is not None:
        return denied
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    note = str(payload.get("note", "")).strip()
    if not note:
        return JSONResponse({"error": "note is required"}, status_code=400)
    return JSONResponse(
        memory_bank.add_note(
            request.path_params["crn"], note, author=str(payload.get("author", "operator"))
        ),
        status_code=201,
    )


async def audit_records(request: Request) -> JSONResponse:
    if (denied := _guard(request, api_keys.SCOPE_GOVERNANCE_READ)) is not None:
        return denied
    limit = int(request.query_params.get("limit", "200"))
    trace_id = request.query_params.get("trace_id")
    return JSONResponse({"records": telemetry.read_audit(limit=limit, trace_id=trace_id)})


async def audit_verify(request: Request) -> JSONResponse:
    if (denied := _guard(request, api_keys.SCOPE_GOVERNANCE_READ)) is not None:
        return denied
    return JSONResponse(telemetry.verify_audit_chain())


@contextlib.asynccontextmanager
async def lifespan(application: Starlette):
    """Publish agent cards and reconcile stale jobs before serving traffic."""

    telemetry.configure_telemetry()
    orchestrator.bootstrap_fleet()
    reconciled = runtime.reconcile_interrupted_jobs()
    telemetry.audit(
        "service.startup",
        actor="control-plane",
        resource="service://fleet",
        decision="allow",
        attributes={"interrupted_jobs_reconciled": reconciled},
    )
    yield
    telemetry.audit(
        "service.shutdown", actor="control-plane", resource="service://fleet", decision="allow"
    )


app = Starlette(
    debug=False,
    lifespan=lifespan,
    # The vendored API reference is several megabytes uncompressed, and audit
    # payloads are highly repetitive JSON.
    middleware=[
        Middleware(security.SecurityHeadersMiddleware),
        Middleware(GZipMiddleware, minimum_size=1024),
    ],
    routes=[
        Route("/", root),
        Route("/api", api_index),
        Route("/api/session", console_session, methods=["GET", "POST"]),
        Route("/api/whoami", whoami),
        Route("/docs", api_docs),
        Route("/openapi.json", openapi_spec),
        Mount("/static", StaticFiles(directory=str(WEB_ROOT)), name="static"),
        Route("/healthz", healthz),
        Route("/readyz", readyz),
        Route("/fleet", fleet),
        Route("/companies/search", search_companies),
        Route("/data-rooms", upload_data_room, methods=["POST"]),
        Route("/jobs", submit, methods=["POST"]),
        Route("/jobs", jobs, methods=["GET"]),
        Route("/jobs/{job_id}", job_detail),
        Route("/jobs/{job_id}/report.pdf", job_report_pdf),
        Route("/jobs/{job_id}/cancel", job_cancel, methods=["POST"]),
        Route("/memory/{crn}", memory_view),
        Route("/memory/{crn}/notes", memory_note, methods=["POST"]),
        Route("/audit", audit_records),
        Route("/audit/verify", audit_verify),
    ],
)


if __name__ == "__main__":  # pragma: no cover - local convenience
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
