"""Outbound notification when a diligence job reaches a terminal state.

A run takes minutes and nobody should have to watch it. When a job finishes,
fails, or is cancelled, the fleet posts a compact summary to a configured
webhook - a Slack or Google Chat incoming webhook, or any endpoint that accepts
JSON.

The destination is fixed configuration (`FLEET_NOTIFY_WEBHOOK`), never taken from
a request, so there is no user-controlled URL to point the fleet at. Dispatch is
routed through the gateway under the runtime's own identity, and a notification
failure is recorded but never fails the audit that produced it.
"""

from __future__ import annotations

import os
from typing import Any

import requests

WEBHOOK_URL = os.getenv("FLEET_NOTIFY_WEBHOOK", "").strip()
NOTIFY_TIMEOUT_SECONDS = float(os.getenv("FLEET_NOTIFY_TIMEOUT_SECONDS", "10"))
CONSOLE_URL = os.getenv("FLEET_CONSOLE_URL", "").strip()

VERDICT_EMOJI = {
    "GREEN LIGHT": "🟢",
    "PROCEED WITH CAUTION": "🟠",
    "RED FLAG DEAL BREAKER": "🔴",
}


def build_payload(job: dict[str, Any]) -> dict[str, Any]:
    """Summarise a finished job for an external recipient."""

    result = job.get("result") or {}
    report = result.get("red_flag_verdict") or {}
    governance = result.get("governance") or {}
    facts = (result.get("memory") or {}).get("current_facts") or {}
    recommendation = report.get("recommendation", "")

    summary = (
        f"{VERDICT_EMOJI.get(recommendation, '⚪')} {recommendation or job.get('status')} - "
        f"{facts.get('company_name') or job.get('crn')}"
    )

    return {
        "event": "diligence.job.finished",
        "status": job.get("status"),
        "job_id": job.get("job_id"),
        "crn": job.get("crn"),
        "company_name": facts.get("company_name"),
        "recommendation": recommendation,
        "summary": summary,
        "text": summary,  # Slack and Google Chat both render a top-level `text`
        "executive_summary": (report.get("executive_summary") or "")[:600],
        "top_risks": (report.get("top_risks") or [])[:5],
        "severity_counts": governance.get("severity_counts", {}),
        "analysis_mode": governance.get("analysis_mode"),
        "trace_id": job.get("trace_id"),
        "error": job.get("error"),
        "console_url": CONSOLE_URL or None,
        "submitted_by": job.get("submitted_by"),
        "finished_at": job.get("finished_at"),
    }


def dispatch(job: dict[str, Any]) -> dict[str, Any]:
    """Post the summary to the configured webhook. Gateway tool handler."""

    if not WEBHOOK_URL:
        return {"status": "disabled", "message": "FLEET_NOTIFY_WEBHOOK is not configured."}

    payload = build_payload(job)
    try:
        response = requests.post(WEBHOOK_URL, json=payload, timeout=NOTIFY_TIMEOUT_SECONDS)
    except requests.Timeout:
        return {"status": "timeout", "message": "Notification endpoint timed out."}
    except requests.RequestException as exc:
        return {"status": "network_error", "message": str(exc)[:200]}

    if 200 <= response.status_code < 300:
        return {"status": "success", "code": response.status_code, "recommendation": payload["recommendation"]}
    return {"status": "error", "code": response.status_code, "error": response.text[:200]}
