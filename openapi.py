"""The OpenAPI description of the fleet control plane.

This is the contract other systems build against, so it is written by hand
rather than reflected out of the code: a generated spec documents whatever the
implementation happens to do, including its accidents. What stops it drifting is
`SpecificationTests`, which fails if a route exists without a spec entry or a
spec entry without a route.

Served at /openapi.json, rendered at /docs.
"""

from __future__ import annotations

import os
from typing import Any

import telemetry

def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _json(schema: dict[str, Any]) -> dict[str, Any]:
    return {"content": {"application/json": {"schema": schema}}}


def _error(description: str) -> dict[str, Any]:
    return {"description": description, **_json(_ref("Error"))}


CRN_PARAMETER = {
    "name": "crn",
    "in": "path",
    "required": True,
    "description": "Companies House company number.",
    "schema": {"type": "string", "minLength": 8, "maxLength": 8, "example": "03994971"},
}

JOB_ID_PARAMETER = {
    "name": "job_id",
    "in": "path",
    "required": True,
    "description": "Identifier returned when the audit was submitted.",
    "schema": {"type": "string", "example": "job-20260822T190123-a4a7a0"},
}


SCHEMAS: dict[str, Any] = {
    "Error": {
        "type": "object",
        "properties": {"error": {"type": "string", "description": "What went wrong."}},
        "required": ["error"],
    },
    "CompanySearchResult": {
        "type": "object",
        "properties": {
            "company_number": {"type": "string", "example": "03994971"},
            "title": {"type": "string", "example": "THIRD PARTY FORMATIONS LIMITED"},
            "company_status": {"type": "string", "example": "active"},
            "company_type": {"type": "string", "example": "ltd"},
            "date_of_creation": {"type": "string", "format": "date"},
            "address_snippet": {"type": "string"},
        },
    },
    "Finding": {
        "type": "object",
        "description": "One graded finding, with the evidence it rests on.",
        "properties": {
            "category": {"type": "string", "example": "Data Integrity"},
            "severity": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW", "CLEAR"]},
            "finding": {"type": "string"},
            "evidentiary_quote": {
                "type": "string",
                "description": (
                    "The source text or computed identity the finding cites. Checked "
                    "against the record before the report is released."
                ),
            },
            "evidence_verified": {
                "type": "boolean",
                "description": "False means the citation could not be grounded in the source.",
            },
        },
    },
    "ReconciliationCheck": {
        "type": "object",
        "description": (
            "One balance-sheet identity tested against the company's own filed figures. "
            "When `consistent` is false, ratios derived from it are suppressed rather "
            "than reported."
        ),
        "properties": {
            "identity": {"type": "string", "example": "working_capital"},
            "formula": {
                "type": "string",
                "example": "current_assets - creditors_within_one_year = net_current_assets",
            },
            "expected": {"type": "number", "example": -2182},
            "reported": {"type": "number", "example": 5558},
            "difference": {"type": "number", "example": -7740},
            "consistent": {"type": "boolean"},
        },
    },
    "Exchange": {
        "type": "object",
        "description": "A typed message one agent sent another during the run.",
        "properties": {
            "seq": {"type": "integer"},
            "timestamp": {"type": "string", "format": "date-time"},
            "sender": {"type": "string", "example": "orchestrator"},
            "recipient": {"type": "string", "example": "legal_risk"},
            "kind": {"type": "string", "example": "task_assignment"},
            "message": {"type": "string"},
            "attributes": {"type": "object", "additionalProperties": True},
            "trace_id": {"type": "string"},
            "span_id": {"type": "string"},
        },
    },
    "TokenUsage": {
        "type": "object",
        "description": "What the run cost, in total and per model call.",
        "properties": {
            "calls": {"type": "integer"},
            "prompt_tokens": {"type": "integer"},
            "output_tokens": {"type": "integer"},
            "total_tokens": {"type": "integer"},
            "total_model_latency_ms": {"type": "integer"},
            "by_call": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "schema": {"type": "string", "example": "legal_risks"},
                        "model": {"type": "string", "example": "gemini-3.5-flash"},
                        "prompt_tokens": {"type": "integer"},
                        "output_tokens": {"type": "integer"},
                        "latency_ms": {"type": "integer"},
                    },
                },
            },
        },
    },
    "Governance": {
        "type": "object",
        "description": "How the run was carried out, so the result can be audited.",
        "properties": {
            "trace_id": {"type": "string"},
            "analysis_mode": {
                "type": "string",
                "enum": ["model", "deterministic"],
                "description": "`deterministic` means at least one agent could not reach a model.",
            },
            "models_used": {"type": "array", "items": {"type": "string"}},
            "agent_models": {"type": "object", "additionalProperties": {"type": "string"}},
            "agents_on_deterministic_fallback": {"type": "array", "items": {"type": "string"}},
            "severity_counts": {"type": "object", "additionalProperties": {"type": "integer"}},
            "token_usage": _ref("TokenUsage"),
            "model_tiers": {"type": "object", "additionalProperties": True},
            "armor_verdict": {"type": "string", "enum": ["CLEAN", "SANITIZED", "BLOCKED"]},
            "armor_violations": {"type": "array", "items": {"type": "string"}},
            "documents_quarantined": {"type": "integer"},
            "unverified_citations": {"type": "integer"},
            "memory_written": {"type": "boolean"},
            "registry_versions": {"type": "object", "additionalProperties": {"type": "string"}},
        },
    },
    "Report": {
        "type": "object",
        "description": (
            "The completed audit. Every financial figure is parsed from the iXBRL "
            "accounts the company filed at Companies House; none is generated or estimated."
        ),
        "properties": {
            "crn": {"type": "string"},
            "run_id": {"type": "string"},
            "job_id": {"type": "string"},
            "trace_id": {"type": "string"},
            "raw_statutory_data": {
                "type": "object",
                "description": (
                    "One entry per Companies House endpoint, each wrapping its payload "
                    "in `{status, endpoint, data}` so a missing record is visible rather "
                    "than silently absent."
                ),
                "additionalProperties": True,
            },
            "accounts": {
                "type": "object",
                "description": "Filed accounts, parsed deterministically.",
                "properties": {
                    "status": {"type": "string", "example": "success"},
                    "filings_examined": {"type": "integer"},
                    "latest": {"type": "object", "additionalProperties": True},
                },
                "additionalProperties": True,
            },
            "data_room": {"type": "object", "additionalProperties": True},
            "memory": {"type": "object", "additionalProperties": True},
            "legal_risks": {
                "type": "object",
                "properties": {
                    "risks": {"type": "array", "items": _ref("Finding")},
                    "overall_legal_status": {"type": "string"},
                    "limitations": {"type": "array", "items": {"type": "string"}},
                    "model_used": {"type": "string"},
                    "prompt_tokens": {"type": "integer"},
                    "output_tokens": {"type": "integer"},
                },
            },
            "financial_analysis": {
                "type": "object",
                "properties": {
                    "findings": {"type": "array", "items": _ref("Finding")},
                    "accounts_status": {"type": "string"},
                    "overall_financial_status": {"type": "string"},
                    "limitations": {"type": "array", "items": {"type": "string"}},
                    "model_used": {"type": "string"},
                },
            },
            "debate_transcript": {
                "type": "object",
                "description": "Where the legal and financial agents disagreed, and how it resolved.",
                "properties": {
                    "points": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "issue": {"type": "string"},
                                "legal_view": {"type": "string"},
                                "financial_view": {"type": "string"},
                                "resolved_position": {"type": "string"},
                                "severity": {"type": "string"},
                            },
                        },
                    },
                    "risk_reward_summary": {"type": "string"},
                },
            },
            "red_flag_verdict": {
                "type": "object",
                "properties": {
                    "recommendation": {
                        "type": "string",
                        "enum": ["GREEN LIGHT", "PROCEED WITH CAUTION", "RED FLAG DEAL BREAKER"],
                    },
                    "executive_summary": {"type": "string"},
                    "top_risks": {"type": "array", "items": {"type": "string"}},
                    "required_human_review": {"type": "array", "items": {"type": "string"}},
                    "reliance_disclaimer": {"type": "string"},
                },
            },
            "governance": _ref("Governance"),
            "reasoning_chain": {"type": "array", "items": _ref("Exchange")},
        },
    },
    "JobEvent": {
        "type": "object",
        "description": (
            "A progress event. Entries whose `attributes.exchange` is true are "
            "inter-agent messages rather than pipeline stages."
        ),
        "properties": {
            "timestamp": {"type": "string", "format": "date-time"},
            "stage": {"type": "string", "example": "Legal agent"},
            "message": {"type": "string"},
            "attributes": {"type": "object", "additionalProperties": True},
        },
    },
    "Job": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "crn": {"type": "string"},
            "company_name": {
                "type": "string",
                "description": "The registered name, recorded when the audit succeeded.",
            },
            "status": {
                "type": "string",
                "enum": ["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"],
            },
            "data_room_path": {"type": "string"},
            "submitted_by": {"type": "string"},
            "created_at": {"type": "string", "format": "date-time"},
            "started_at": {"type": "string", "format": "date-time", "nullable": True},
            "finished_at": {"type": "string", "format": "date-time", "nullable": True},
            "trace_id": {"type": "string"},
            "events": {"type": "array", "items": _ref("JobEvent")},
            "summary": {
                "description": (
                    "A compact view of the finished report, present on every listing so a "
                    "table can show a verdict without downloading the full audit. Null "
                    "while the job is unfinished."
                ),
                "oneOf": [_ref("JobSummary"), {"type": "null"}],
            },
            "result": {
                "description": (
                    "The full report, once the job has succeeded. Null until then, and "
                    "absent entirely when the listing was requested with "
                    "`include_result=false`."
                ),
                "oneOf": [_ref("Report"), {"type": "null"}],
            },
            "error": {"type": "string", "nullable": True},
        },
    },
    "JobSummary": {
        "type": "object",
        "description": "What a listing needs from a finished audit.",
        "properties": {
            "recommendation": {
                "type": "string",
                "enum": ["GREEN LIGHT", "PROCEED WITH CAUTION", "RED FLAG DEAL BREAKER"],
            },
            "company_name": {"type": "string"},
            "company_status": {"type": "string"},
            "severity_counts": {"type": "object", "additionalProperties": {"type": "integer"}},
            "total_tokens": {"type": "integer"},
            "models_used": {"type": "array", "items": {"type": "string"}},
            "analysis_mode": {"type": "string"},
            "documents_quarantined": {"type": "integer"},
        },
    },
    "AuditRecord": {
        "type": "object",
        "description": "One link in the hash chain. Altering any record breaks verification.",
        "properties": {
            "timestamp": {"type": "string", "format": "date-time"},
            "action": {"type": "string", "example": "gateway.call"},
            "actor": {"type": "string", "example": "legal_risk"},
            "resource": {"type": "string"},
            "decision": {"type": "string", "enum": ["allow", "deny"]},
            "severity": {"type": "string", "example": "INFO"},
            "trace_id": {"type": "string"},
            "attributes": {"type": "object", "additionalProperties": True},
            "record_hash": {"type": "string"},
            "previous_hash": {"type": "string"},
        },
    },
}


def _paths() -> dict[str, Any]:
    return {
        "/": {
            "get": {
                "tags": ["Service"],
                "summary": "Console or service index",
                "description": (
                    "Content-negotiated. `Accept: text/html` returns the operator "
                    "console; anything else returns the JSON index, which is also "
                    "always available at `/api`."
                ),
                "security": [],
                "responses": {
                    "200": {"description": "The console, or the service index.", **_json({"type": "object"})}
                },
            }
        },
        "/api": {
            "get": {
                "tags": ["Service"],
                "summary": "Service index",
                "description": "Version, environment, registered agents, and every endpoint.",
                "security": [],
                "responses": {"200": {"description": "Service index.", **_json({"type": "object"})}},
            }
        },
        "/api/session": {
            "get": {
                "tags": ["Service"],
                "summary": "Whether the console is locked",
                "security": [],
                "responses": {
                    "200": {
                        "description": "Lock and sign-in state.",
                        **_json(
                            {
                                "type": "object",
                                "properties": {
                                    "locked": {"type": "boolean"},
                                    "authenticated": {"type": "boolean"},
                                },
                            }
                        ),
                    }
                },
            },
            "post": {
                "tags": ["Service"],
                "summary": "Sign the console in",
                "description": (
                    "Exchanges the shared access code for an HttpOnly, SameSite=strict "
                    "session cookie. The cookie carries an HMAC of the code, never the "
                    "code itself. Machines should use the API key or a bearer token instead."
                ),
                "security": [],
                "requestBody": {
                    "required": True,
                    **_json(
                        {
                            "type": "object",
                            "properties": {"code": {"type": "string"}},
                            "required": ["code"],
                        }
                    ),
                },
                "responses": {
                    "200": {
                        "description": "Signed in; the session cookie is set.",
                        **_json({"type": "object", "properties": {"authenticated": {"type": "boolean"}}}),
                    },
                    "401": _error("The access code was not recognised."),
                    "404": _error("Console sign-in is not configured on this deployment."),
                },
            },
        },
        "/api/whoami": {
            "get": {
                "tags": ["Service"],
                "summary": "What the presented credential grants",
                "description": (
                    "Returns the caller's name, scopes, expiry, and how much of their "
                    "hourly budget is spent. Use it to check a key rather than "
                    "discovering its limits through a sequence of 403s."
                ),
                "responses": {
                    "200": {
                        "description": "The authenticated caller.",
                        **_json(
                            {
                                "type": "object",
                                "properties": {
                                    "authenticated": {"type": "boolean"},
                                    "name": {"type": "string"},
                                    "key_id": {"type": "string"},
                                    "kind": {
                                        "type": "string",
                                        "enum": ["api_key", "console", "legacy_key", "open"],
                                    },
                                    "scopes": {"type": "array", "items": {"type": "string"}},
                                    "expires_at": {"type": "integer"},
                                    "requests_per_hour": {"type": "integer"},
                                    "audits_per_hour": {"type": "integer"},
                                    "requests_used_this_hour": {"type": "integer"},
                                    "audits_used_this_hour": {"type": "integer"},
                                },
                            }
                        ),
                    },
                    "401": _error("No valid credential was presented."),
                },
            }
        },
        "/healthz": {
            "get": {
                "tags": ["Service"],
                "summary": "Liveness probe",
                "security": [],
                "responses": {"200": {"description": "The process is up.", **_json({"type": "object"})}},
            }
        },
        "/readyz": {
            "get": {
                "tags": ["Service"],
                "summary": "Readiness and dependency status",
                "description": "Reports whether credentials and models are configured, and runtime load.",
                "security": [],
                "responses": {"200": {"description": "Readiness report.", **_json({"type": "object"})}},
            }
        },
        "/fleet": {
            "get": {
                "tags": ["Governance"],
                "summary": "Agent registry, identities, and tool policies",
                "description": (
                    "Every agent's published card, the scopes its identity holds, the "
                    "gateway's tool policies, and the egress allowlist."
                ),
                "responses": {
                    "200": {"description": "The fleet roster.", **_json({"type": "object"})},
                    "401": _error("Not authenticated."),
                },
            }
        },
        "/companies/search": {
            "get": {
                "tags": ["Companies"],
                "summary": "Find a company by name",
                "description": "Searches the live Companies House register. Routed through the gateway like any other tool.",
                "parameters": [
                    {
                        "name": "q",
                        "in": "query",
                        "required": True,
                        "description": "At least two characters.",
                        "schema": {"type": "string", "example": "third party formations"},
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "Matching companies.",
                        **_json(
                            {
                                "type": "object",
                                "properties": {
                                    "status": {"type": "string"},
                                    "results": {
                                        "type": "array",
                                        "items": _ref("CompanySearchResult"),
                                    },
                                },
                            }
                        ),
                    },
                    "401": _error("Not authenticated."),
                    "403": _error("The gateway refused the call."),
                },
            }
        },
        "/data-rooms": {
            "post": {
                "tags": ["Audits"],
                "summary": "Upload deal documents",
                "description": (
                    "Stores documents and returns the data room path to audit against. "
                    "Uploads are untrusted by definition: this endpoint only stores them, "
                    "and Model Armor screens the contents at ingestion."
                ),
                "requestBody": {
                    "required": True,
                    **_json(
                        {
                            "type": "object",
                            "properties": {
                                "files": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": 25,
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "name": {
                                                "type": "string",
                                                "description": ".csv, .md, .pdf or .txt. Any path component is stripped.",
                                                "example": "services-agreement.md",
                                            },
                                            "content_base64": {"type": "string"},
                                        },
                                        "required": ["name", "content_base64"],
                                    },
                                },
                                "submitted_by": {"type": "string"},
                            },
                            "required": ["files"],
                        }
                    ),
                },
                "responses": {
                    "201": {
                        "description": "Data room created.",
                        **_json(
                            {
                                "type": "object",
                                "properties": {
                                    "data_room_path": {"type": "string"},
                                    "room_id": {"type": "string"},
                                    "files": {"type": "array", "items": {"type": "object"}},
                                },
                            }
                        ),
                    },
                    "400": _error("Malformed body, or a file without a name."),
                    "401": _error("Not authenticated."),
                    "413": _error("Too many files, or the upload exceeded the size limit."),
                    "415": _error("Unsupported file type."),
                },
            }
        },
        "/jobs": {
            "get": {
                "tags": ["Audits"],
                "summary": "List audits",
                "description": (
                    "The full audit history, newest first, with `total` so a client can "
                    "page. Pass `include_result=false` for anything rendering a list: it "
                    "returns a compact `summary` per job instead of the complete report, "
                    "which is the difference between a few kilobytes and several megabytes."
                ),
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "description": "Page size. Capped server-side.",
                        "schema": {"type": "integer", "default": 25, "minimum": 1, "maximum": 100},
                    },
                    {
                        "name": "offset",
                        "in": "query",
                        "description": "How many to skip, for paging.",
                        "schema": {"type": "integer", "default": 0, "minimum": 0},
                    },
                    {
                        "name": "crn",
                        "in": "query",
                        "description": "Only audits of this company number, matched exactly.",
                        "schema": {"type": "string", "example": "03994971"},
                    },
                    {
                        "name": "q",
                        "in": "query",
                        "description": (
                            "Match the audited company's registered name or number. For "
                            "looking through a history, where the operator knows one or "
                            "the other."
                        ),
                        "schema": {"type": "string", "example": "formations"},
                    },
                    {
                        "name": "status",
                        "in": "query",
                        "schema": {
                            "type": "string",
                            "enum": [
                                "QUEUED", "RUNNING", "SUCCEEDED",
                                "FAILED", "CANCELLED", "INTERRUPTED",
                            ],
                        },
                    },
                    {
                        "name": "include_result",
                        "in": "query",
                        "description": (
                            "Include the full report on every job. Defaults to true for "
                            "compatibility; pass false when listing."
                        ),
                        "schema": {"type": "boolean", "default": True},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "A page of audits, newest first.",
                        **_json(
                            {
                                "type": "object",
                                "properties": {
                                    "jobs": {"type": "array", "items": _ref("Job")},
                                    "total": {
                                        "type": "integer",
                                        "description": "How many audits match, ignoring paging.",
                                    },
                                    "limit": {"type": "integer"},
                                    "offset": {"type": "integer"},
                                },
                            }
                        ),
                    },
                    "400": _error("Unknown status filter."),
                    "401": _error("Not authenticated."),
                },
            },
            "post": {
                "tags": ["Audits"],
                "summary": "Submit an audit",
                "description": (
                    "Queues the audit and returns immediately. The run continues in the "
                    "fleet's runtime whether or not the caller stays connected. Omit "
                    "`data_room_path` to audit statutory records and filed accounts alone."
                ),
                "requestBody": {
                    "required": True,
                    **_json(
                        {
                            "type": "object",
                            "properties": {
                                "crn": {"type": "string", "example": "03994971"},
                                "data_room_path": {
                                    "type": "string",
                                    "description": "A path returned by POST /data-rooms.",
                                },
                                "submitted_by": {"type": "string"},
                            },
                            "required": ["crn"],
                        }
                    ),
                },
                "responses": {
                    "202": {
                        "description": "Audit queued.",
                        **_json(
                            {
                                "type": "object",
                                "properties": {
                                    "job_id": {"type": "string"},
                                    "crn": {"type": "string"},
                                    "status": {"type": "string", "example": "QUEUED"},
                                },
                            }
                        ),
                    },
                    "400": _error("Malformed body or invalid company number."),
                    "401": _error("Not authenticated."),
                },
            },
        },
        "/jobs/{job_id}": {
            "get": {
                "tags": ["Audits"],
                "summary": "Status, progress events, and the report",
                "description": (
                    "Poll this while a job runs: `events` grows as the fleet works, and "
                    "`result` is populated once the status is SUCCEEDED."
                ),
                "parameters": [JOB_ID_PARAMETER],
                "responses": {
                    "200": {"description": "The job.", **_json(_ref("Job"))},
                    "401": _error("Not authenticated."),
                    "404": _error("No such job."),
                },
            }
        },
        "/jobs/{job_id}/report.pdf": {
            "get": {
                "tags": ["Audits"],
                "summary": "Download the Red Flag Report as a PDF",
                "parameters": [JOB_ID_PARAMETER],
                "responses": {
                    "200": {
                        "description": "The report.",
                        "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}},
                    },
                    "401": _error("Not authenticated."),
                    "404": _error("No such job."),
                    "409": _error("The job has not succeeded, so there is no report to export."),
                },
            }
        },
        "/jobs/{job_id}/cancel": {
            "post": {
                "tags": ["Audits"],
                "summary": "Cancel a running audit",
                "description": "Cooperative: the run stops at its next stage boundary.",
                "parameters": [JOB_ID_PARAMETER],
                "responses": {
                    "200": {"description": "Cancellation recorded.", **_json({"type": "object"})},
                    "401": _error("Not authenticated."),
                    "404": _error("No such job."),
                },
            }
        },
        "/memory/{crn}": {
            "get": {
                "tags": ["Memory"],
                "summary": "What the fleet remembers about a company",
                "description": "Prior audits, operator notes, tracked facts, and what changed since the last audit.",
                "parameters": [CRN_PARAMETER],
                "responses": {
                    "200": {"description": "The memory record.", **_json({"type": "object"})},
                    "401": _error("Not authenticated."),
                },
            }
        },
        "/memory/{crn}/notes": {
            "post": {
                "tags": ["Memory"],
                "summary": "Add an operator note",
                "description": "Notes are shown to later audits of the same company.",
                "parameters": [CRN_PARAMETER],
                "requestBody": {
                    "required": True,
                    **_json(
                        {
                            "type": "object",
                            "properties": {
                                "note": {"type": "string"},
                                "author": {"type": "string"},
                            },
                            "required": ["note"],
                        }
                    ),
                },
                "responses": {
                    "201": {"description": "Note recorded.", **_json({"type": "object"})},
                    "400": _error("The note was empty."),
                    "401": _error("Not authenticated."),
                },
            }
        },
        "/audit": {
            "get": {
                "tags": ["Governance"],
                "summary": "Audit trail records",
                "description": "Every tool call, policy decision, and model exchange. Filter by trace to follow one run.",
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 200}},
                    {"name": "trace_id", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {
                        "description": "Audit records.",
                        **_json(
                            {
                                "type": "object",
                                "properties": {
                                    "records": {"type": "array", "items": _ref("AuditRecord")}
                                },
                            }
                        ),
                    },
                    "401": _error("Not authenticated."),
                },
            }
        },
        "/audit/verify": {
            "get": {
                "tags": ["Governance"],
                "summary": "Verify the audit hash chain",
                "description": (
                    "Recomputes every hash. `valid: false` means a record was altered or "
                    "removed after it was written."
                ),
                "responses": {
                    "200": {
                        "description": "Verification result.",
                        **_json(
                            {
                                "type": "object",
                                "properties": {
                                    "valid": {"type": "boolean"},
                                    "records": {"type": "integer"},
                                    "head_hash": {"type": "string"},
                                },
                            }
                        ),
                    },
                    "401": _error("Not authenticated."),
                },
            }
        },
    }


REQUIRED_SCOPES = {
    ("/companies/search", "get"): "audits:read",
    ("/fleet", "get"): "audits:read",
    ("/data-rooms", "post"): "audits:write",
    ("/jobs", "get"): "audits:read",
    ("/jobs", "post"): "audits:write",
    ("/jobs/{job_id}", "get"): "audits:read",
    ("/jobs/{job_id}/report.pdf", "get"): "audits:read",
    ("/jobs/{job_id}/cancel", "post"): "audits:write",
    ("/memory/{crn}", "get"): "audits:read",
    ("/memory/{crn}/notes", "post"): "memory:write",
    ("/audit", "get"): "governance:read",
    ("/audit/verify", "get"): "governance:read",
}


def _annotate_scopes(paths: dict[str, Any]) -> None:
    """Record each operation's required scope, and the errors metering can raise.

    Done here rather than by hand so the documented scope cannot disagree with
    REQUIRED_SCOPES, and so no guarded operation forgets its 403 or 429.
    """

    for (path, method), scope in REQUIRED_SCOPES.items():
        operation = paths[path][method]
        operation["x-required-scope"] = scope
        operation["description"] = (
            operation.get("description", "").rstrip()
            + f"\n\n**Requires the `{scope}` scope.**"
        )
        operation["responses"].setdefault(
            "403", _error(f"The key lacks the {scope} scope.")
        )
        operation["responses"].setdefault(
            "429", _error("Request or audit budget exhausted; see the Retry-After header.")
        )


def build_spec(server_url: str = "") -> dict[str, Any]:
    """The OpenAPI document. `server_url` pins the playground to this deployment."""

    servers = [{"url": server_url or "/", "description": "This deployment"}]
    paths = _paths()
    _annotate_scopes(paths)
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "DueDiligence Direct",
            "version": telemetry.SERVICE_VERSION,
            "summary": "Governed multi-agent M&A due diligence over the UK statutory register.",
            "description": (
                "A fleet of six governed agents reads a company's Companies House record "
                "and its own filed iXBRL accounts, argues the findings out between "
                "themselves, and returns a recommendation with the evidence attached.\n\n"
                "**Every financial figure is parsed from documents the company filed at "
                "Companies House.** Nothing is generated, estimated, or substituted. When "
                "filed accounts fail a balance-sheet identity check, ratios derived from "
                "them are suppressed rather than reported.\n\n"
                "This is AI-generated diligence support, not legal or financial advice. "
                "Verify source records before relying on any finding.\n\n"
                "### Authentication\n\n"
                "Machines send `x-fleet-api-key`, or a bearer token (on Google Cloud, a "
                "Cloud Run identity token). People sign the console in at "
                "`POST /api/session` and carry a session cookie. A deployment with "
                "neither `FLEET_API_KEY` nor `FLEET_CONSOLE_ACCESS_CODE` set is open.\n\n"
                "### Clients\n\n"
                "A Python client and CLI ship with the service: see `docs/CLIENT.md`, or "
                "`pip install requests` and `from ddclient import DueDiligenceClient`."
            ),
            "license": {"name": "MIT"},
        },
        "servers": servers,
        "tags": [
            {"name": "Audits", "description": "Submit, watch, and export due diligence audits."},
            {"name": "Companies", "description": "Resolve a company on the statutory register."},
            {"name": "Memory", "description": "What the fleet remembers between audits."},
            {
                "name": "Governance",
                "description": "The agent registry and the tamper-evident audit trail.",
            },
            {"name": "Service", "description": "Index, health, and console sign-in."},
        ],
        "components": {
            "schemas": SCHEMAS,
            "securitySchemes": {
                "apiKey": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "x-fleet-api-key",
                    "description": (
                        "A per-caller key issued with `python api_keys.py issue`. Scoped, "
                        "expiring, and rate limited. The legacy shared FLEET_API_KEY is "
                        "still accepted but is unattributable and cannot be revoked on "
                        "its own."
                    ),
                },
                "bearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "A Cloud Run identity token, or any token accepted as FLEET_API_KEY.",
                },
                "consoleSession": {
                    "type": "apiKey",
                    "in": "cookie",
                    "name": "fleet_console",
                    "description": "Set by POST /api/session. HttpOnly, so the playground uses it automatically once signed in.",
                },
            },
        },
        "security": [{"apiKey": []}, {"bearer": []}, {"consoleSession": []}],
        "paths": paths,
        "externalDocs": {
            "description": "Source and architecture",
            "url": os.getenv(
                "FLEET_SOURCE_URL", "https://github.com/AbhishekSinha7/Due-diligence-Direct"
            ),
        },
    }
