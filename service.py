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

import contextlib
import json
import os
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
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

    job_id = runtime.submit_job(
        query.crn,
        data_room_path=str(payload.get("data_room_path", "data_room")),
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
        Route("/healthz", healthz),
        Route("/readyz", readyz),
        Route("/fleet", fleet),
        Route("/jobs", submit, methods=["POST"]),
        Route("/jobs", jobs, methods=["GET"]),
        Route("/jobs/{job_id}", job_detail),
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
