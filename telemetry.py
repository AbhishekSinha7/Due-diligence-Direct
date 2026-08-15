"""OpenTelemetry tracing and tamper-evident audit logging for the agent fleet.

Design goals for the Fortified Enterprise Fleet track:

- Every agent action runs inside an OpenTelemetry span with fleet attributes.
- Every governance decision (gateway allow/deny, guardrail verdict, memory write)
  is appended to an OTel-compliant audit log that carries the trace and span ids.
- The audit log is hash-chained so a demo can prove records were not edited.
- Nothing here is allowed to break the workflow: if the OTel SDK or an exporter is
  unavailable, tracing degrades to local ids and the run continues.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "duediligence-direct")
SERVICE_VERSION = os.getenv("FLEET_VERSION", "1.0.0")
TELEMETRY_DIR = Path(os.getenv("FLEET_TELEMETRY_DIR", "telemetry"))
AUDIT_LOG_PATH = TELEMETRY_DIR / "audit.jsonl"
SPAN_LOG_PATH = TELEMETRY_DIR / "spans.jsonl"

_LOCK = threading.Lock()
_LAST_AUDIT_HASH = "0" * 64
_CONFIGURED = False
_TRACER: Any = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _JsonlSpanExporter:
    """Writes finished spans as OTLP-shaped JSON lines for offline inspection."""

    def export(self, spans: Any) -> Any:  # pragma: no cover - exercised via SDK
        try:
            TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
            with SPAN_LOG_PATH.open("a", encoding="utf-8") as handle:
                for span in spans:
                    context = span.get_span_context()
                    handle.write(
                        json.dumps(
                            {
                                "name": span.name,
                                "trace_id": format(context.trace_id, "032x"),
                                "span_id": format(context.span_id, "016x"),
                                "parent_span_id": (
                                    format(span.parent.span_id, "016x") if span.parent else None
                                ),
                                "start_time_unix_nano": span.start_time,
                                "end_time_unix_nano": span.end_time,
                                "status": str(span.status.status_code),
                                "attributes": {k: str(v) for k, v in dict(span.attributes or {}).items()},
                                "resource": {
                                    "service.name": SERVICE_NAME,
                                    "service.version": SERVICE_VERSION,
                                },
                            }
                        )
                        + "\n"
                    )
        except Exception:
            return None
        return None

    def shutdown(self) -> None:  # pragma: no cover - SDK contract
        return None

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # pragma: no cover
        return True


def configure_telemetry() -> Any:
    """Configure the tracer once. Returns a tracer or None when the SDK is absent."""

    global _CONFIGURED, _TRACER
    if _CONFIGURED:
        return _TRACER

    _CONFIGURED = True
    TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    except ImportError:
        _TRACER = None
        return None

    resource = Resource.create(
        {
            "service.name": SERVICE_NAME,
            "service.version": SERVICE_VERSION,
            "deployment.environment": os.getenv("FLEET_ENVIRONMENT", "local"),
            "cloud.provider": "gcp",
            "cloud.platform": os.getenv("FLEET_CLOUD_PLATFORM", "gcp_cloud_run"),
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(SimpleSpanProcessor(_JsonlSpanExporter()))

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if endpoint:
        try:  # pragma: no cover - requires a live collector
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        except Exception:
            pass

    if os.getenv("FLEET_CLOUD_TRACE", "").lower() in {"1", "true", "yes"}:
        try:  # pragma: no cover - requires google-cloud-trace
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

            provider.add_span_processor(BatchSpanProcessor(CloudTraceSpanExporter()))
        except Exception:
            pass

    trace.set_tracer_provider(provider)
    _TRACER = trace.get_tracer(SERVICE_NAME, SERVICE_VERSION)
    return _TRACER


def current_ids() -> tuple[str, str]:
    """Return (trace_id, span_id) for the active span, or synthetic ids."""

    try:
        from opentelemetry import trace

        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            return format(context.trace_id, "032x"), format(context.span_id, "016x")
    except Exception:
        pass
    return uuid.uuid4().hex, uuid.uuid4().hex[:16]


@contextmanager
def agent_span(name: str, **attributes: Any) -> Iterator[dict[str, str]]:
    """Run a block inside an OTel span, yielding its trace and span ids."""

    tracer = configure_telemetry()
    clean = {f"fleet.{k}": ("" if v is None else str(v)) for k, v in attributes.items()}
    if tracer is None:
        trace_id, span_id = current_ids()
        yield {"trace_id": trace_id, "span_id": span_id}
        return

    with tracer.start_as_current_span(name, attributes=clean) as span:
        trace_id, span_id = current_ids()
        try:
            yield {"trace_id": trace_id, "span_id": span_id}
        except Exception as exc:
            try:
                span.record_exception(exc)
            except Exception:
                pass
            raise


def _chain_hash(previous: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(f"{previous}{body}".encode("utf-8")).hexdigest()


def _head_hash() -> str:
    """Read the hash of the last record on disk.

    The control plane, the CLI, and the dashboard can all append to the same log,
    so the chain head is owned by the file rather than by any one process. Only
    the tail of the file is read.
    """

    if not AUDIT_LOG_PATH.exists():
        return "0" * 64

    try:
        with AUDIT_LOG_PATH.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 8192))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return _LAST_AUDIT_HASH

    for line in reversed([line for line in tail.splitlines() if line.strip()]):
        try:
            return str(json.loads(line).get("record_hash", "0" * 64))
        except json.JSONDecodeError:
            continue
    return "0" * 64


def audit(
    action: str,
    *,
    actor: str = "system",
    resource: str = "",
    decision: str = "allow",
    severity: str = "INFO",
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a hash-chained audit record and return it."""

    global _LAST_AUDIT_HASH

    trace_id, span_id = current_ids()
    record = {
        "timestamp": _now(),
        "service_name": SERVICE_NAME,
        "service_version": SERVICE_VERSION,
        "severity_text": severity,
        "trace_id": trace_id,
        "span_id": span_id,
        "action": action,
        "actor": actor,
        "resource": resource,
        "decision": decision,
        "attributes": attributes or {},
    }

    with _LOCK:
        record["previous_hash"] = _head_hash()
        record["record_hash"] = _chain_hash(record["previous_hash"], record)
        _LAST_AUDIT_HASH = record["record_hash"]
        try:
            TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
            with AUDIT_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        except Exception:
            pass

    return record


def read_audit(limit: int = 200, trace_id: str | None = None) -> list[dict[str, Any]]:
    """Read the most recent audit records, newest last."""

    if not AUDIT_LOG_PATH.exists():
        return []

    records: list[dict[str, Any]] = []
    with AUDIT_LOG_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if trace_id and record.get("trace_id") != trace_id:
                continue
            records.append(record)
    return records[-limit:]


def verify_audit_chain() -> dict[str, Any]:
    """Recompute the hash chain to prove the audit log was not edited."""

    records = read_audit(limit=10**9)
    previous = "0" * 64
    for index, record in enumerate(records):
        stored_hash = record.get("record_hash", "")
        payload = {k: v for k, v in record.items() if k != "record_hash"}
        if record.get("previous_hash") != previous or _chain_hash(previous, payload) != stored_hash:
            return {"valid": False, "records": len(records), "broken_at": index}
        previous = stored_hash
    return {"valid": True, "records": len(records), "head_hash": previous}
