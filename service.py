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
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

import agent_identity
import agent_registry
import gateway
import mcp_server
import memory_bank
import orchestrator
import runtime
import telemetry

API_KEY = os.getenv("FLEET_API_KEY", "").strip()


def _authorized(request: Request) -> bool:
    """Cloud Run IAM is the primary control; this is defence in depth."""

    if not API_KEY:
        return True
    presented = request.headers.get("x-fleet-api-key", "")
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        presented = presented or authorization[7:]
    return presented == API_KEY


def _guard(request: Request) -> JSONResponse | None:
    if _authorized(request):
        return None
    telemetry.audit(
        "service.request",
        actor="anonymous",
        resource=str(request.url.path),
        decision="deny",
        severity="WARN",
        attributes={"reason": "missing_or_invalid_api_key"},
    )
    return JSONResponse({"error": "unauthorized"}, status_code=401)


async def index(request: Request) -> JSONResponse:
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

    if (denied := _guard(request)) is not None:
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
    if (denied := _guard(request)) is not None:
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

    if (denied := _guard(request)) is not None:
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
    if (denied := _guard(request)) is not None:
        return denied
    try:
        payload: dict[str, Any] = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    try:
        query = mcp_server.CompanyQuery(crn=str(payload.get("crn", "")))
    except Exception as exc:
        return JSONResponse({"error": f"invalid crn: {exc}"}, status_code=400)

    # Documents are optional. Absent a path, the audit runs on statutory records
    # and filed accounts alone rather than scanning the server's working directory.
    job_id = runtime.submit_job(
        query.crn,
        data_room_path=str(payload.get("data_room_path", "") or ""),
        submitted_by=str(payload.get("submitted_by", "api")),
    )
    return JSONResponse({"job_id": job_id, "crn": query.crn, "status": runtime.STATUS_QUEUED}, status_code=202)


async def jobs(request: Request) -> JSONResponse:
    if (denied := _guard(request)) is not None:
        return denied
    limit = int(request.query_params.get("limit", "25"))
    crn = request.query_params.get("crn")
    return JSONResponse({"jobs": runtime.list_jobs(limit=limit, crn=crn)})


async def job_detail(request: Request) -> JSONResponse:
    if (denied := _guard(request)) is not None:
        return denied
    job = runtime.get_job(request.path_params["job_id"])
    if job is None:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return JSONResponse(job)


async def job_report_pdf(request: Request):
    """Download a finished audit as a PDF."""

    if (denied := _guard(request)) is not None:
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
    if (denied := _guard(request)) is not None:
        return denied
    try:
        return JSONResponse(runtime.cancel_job(request.path_params["job_id"]))
    except KeyError:
        return JSONResponse({"error": "job not found"}, status_code=404)


async def memory_view(request: Request) -> JSONResponse:
    if (denied := _guard(request)) is not None:
        return denied
    crn = request.path_params["crn"]
    return JSONResponse(memory_bank.recall(crn, actor="api"))


async def memory_note(request: Request) -> JSONResponse:
    if (denied := _guard(request)) is not None:
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
    if (denied := _guard(request)) is not None:
        return denied
    limit = int(request.query_params.get("limit", "200"))
    trace_id = request.query_params.get("trace_id")
    return JSONResponse({"records": telemetry.read_audit(limit=limit, trace_id=trace_id)})


async def audit_verify(request: Request) -> JSONResponse:
    if (denied := _guard(request)) is not None:
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
    routes=[
        Route("/", index),
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
