"""Agent Gateway: the single policy enforcement point for the fleet.

No agent touches a tool, an API, or the model directly. Every call is routed
through `gateway.call()`, which enforces, in order:

1. Identity   - the caller presents a short-lived signed token (agent_identity).
2. Lifecycle  - the caller's version is resolved in the registry and must be ACTIVE.
3. Capability - the tool must be declared on the caller's published agent card.
4. Scope      - the tool's required scope must be in the token.
5. Egress     - outbound hosts must be on the allowlist.
6. Quota      - a per-agent token bucket bounds call volume.
7. Resilience - transient failures are retried with backoff and then surfaced.

Allow and deny decisions are both audited, so the demo can show a denied call as
clearly as a successful one.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import agent_identity
import agent_registry
import telemetry

AUDIENCE = "fleet-gateway"

DEFAULT_ALLOWED_HOSTS = (
    "api.company-information.service.gov.uk",
    "document-api.company-information.service.gov.uk",
    "generativelanguage.googleapis.com",
    "aiplatform.googleapis.com",
)

DEFAULT_QUOTA_PER_MINUTE = int(os.getenv("FLEET_GATEWAY_QUOTA_PER_MINUTE", "60"))
MAX_ATTEMPTS = int(os.getenv("FLEET_GATEWAY_MAX_ATTEMPTS", "3"))
RETRY_BASE_DELAY_SECONDS = float(os.getenv("FLEET_GATEWAY_RETRY_DELAY", "0.5"))

# Statuses that are worth retrying because they are transient, not semantic.
RETRYABLE_STATUSES = {"timeout", "network_error"}


class PolicyViolation(Exception):
    """Raised when the gateway refuses a call."""


@dataclass
class ToolPolicy:
    name: str
    required_scope: str
    handler: Callable[..., Any]
    egress_hosts: tuple[str, ...] = ()
    description: str = ""


_TOOL_POLICIES: dict[str, ToolPolicy] = {}
_QUOTA_STATE: dict[str, list[float]] = {}


def register_tool(policy: ToolPolicy) -> None:
    _TOOL_POLICIES[policy.name] = policy


def registered_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": policy.name,
            "required_scope": policy.required_scope,
            "egress_hosts": list(policy.egress_hosts),
            "description": policy.description,
        }
        for policy in sorted(_TOOL_POLICIES.values(), key=lambda item: item.name)
    ]


def allowed_hosts() -> tuple[str, ...]:
    configured = os.getenv("FLEET_ALLOWED_EGRESS_HOSTS", "").strip()
    if configured:
        return tuple(host.strip() for host in configured.split(",") if host.strip())
    return DEFAULT_ALLOWED_HOSTS


def _check_egress(policy: ToolPolicy) -> None:
    permitted = allowed_hosts()
    for host in policy.egress_hosts:
        hostname = urlparse(host if "//" in host else f"https://{host}").hostname or host
        if hostname not in permitted:
            raise PolicyViolation(
                f"Egress to {hostname} is not on the fleet allowlist for tool {policy.name}."
            )


def _check_quota(principal: str) -> None:
    window_start = time.time() - 60.0
    calls = [timestamp for timestamp in _QUOTA_STATE.get(principal, []) if timestamp >= window_start]
    if len(calls) >= DEFAULT_QUOTA_PER_MINUTE:
        _QUOTA_STATE[principal] = calls
        raise PolicyViolation(
            f"{principal} exceeded the fleet quota of {DEFAULT_QUOTA_PER_MINUTE} calls/minute."
        )
    calls.append(time.time())
    _QUOTA_STATE[principal] = calls


def reset_quota() -> None:
    _QUOTA_STATE.clear()


def _check_capability(agent_id: str, tool_name: str) -> dict[str, Any]:
    card = agent_registry.resolve_agent(agent_id)
    if card["status"] != agent_registry.STATUS_ACTIVE:
        raise PolicyViolation(f"{agent_id}@{card['version']} is {card['status']} and cannot call tools.")
    if tool_name not in card["tools"]:
        raise PolicyViolation(
            f"{agent_id}@{card['version']} has not published capability for tool {tool_name}."
        )
    return card


def call(agent_id: str, tool_name: str, /, **kwargs: Any) -> Any:
    """Invoke a tool on behalf of an agent, subject to full fleet policy."""

    policy = _TOOL_POLICIES.get(tool_name)
    if policy is None:
        telemetry.audit(
            "gateway.call",
            actor=agent_id,
            resource=f"tool://{tool_name}",
            decision="deny",
            severity="ERROR",
            attributes={"reason": "unregistered_tool"},
        )
        raise PolicyViolation(f"Tool {tool_name} is not registered with the gateway.")

    with telemetry.agent_span(
        f"gateway.{tool_name}",
        agent_id=agent_id,
        tool=tool_name,
        required_scope=policy.required_scope,
    ):
        try:
            token = agent_identity.mint_token(
                agent_id, audience=AUDIENCE, scopes=[policy.required_scope]
            )
            claims = agent_identity.verify_token(
                token, audience=AUDIENCE, required_scope=policy.required_scope
            )
            card = _check_capability(agent_id, tool_name)
            _check_egress(policy)
            _check_quota(claims["sub"])
        except (agent_identity.IdentityError, agent_registry.RegistryError, PolicyViolation) as exc:
            telemetry.audit(
                "gateway.call",
                actor=agent_id,
                resource=f"tool://{tool_name}",
                decision="deny",
                severity="ERROR",
                attributes={"reason": exc.__class__.__name__, "message": str(exc)},
            )
            raise PolicyViolation(str(exc)) from exc

        last_error: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                result = policy.handler(**kwargs)
            except Exception as exc:
                last_error = exc
                if attempt == MAX_ATTEMPTS:
                    break
                time.sleep(RETRY_BASE_DELAY_SECONDS * attempt)
                continue

            status = result.get("status") if isinstance(result, dict) else None
            if status in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS:
                telemetry.audit(
                    "gateway.retry",
                    actor=claims["sub"],
                    resource=f"tool://{tool_name}",
                    decision="retry",
                    severity="WARN",
                    attributes={"attempt": attempt, "status": status},
                )
                time.sleep(RETRY_BASE_DELAY_SECONDS * attempt)
                continue

            telemetry.audit(
                "gateway.call",
                actor=claims["sub"],
                resource=f"tool://{tool_name}",
                decision="allow",
                attributes={
                    "agent_version": card["version"],
                    "scope": policy.required_scope,
                    "attempts": attempt,
                    "result_status": status or "ok",
                },
            )
            return result

        telemetry.audit(
            "gateway.call",
            actor=claims["sub"],
            resource=f"tool://{tool_name}",
            decision="error",
            severity="ERROR",
            attributes={
                "attempts": MAX_ATTEMPTS,
                "error": last_error.__class__.__name__ if last_error else "exhausted_retries",
            },
        )
        if last_error is not None:
            raise last_error
        return {"status": "error", "message": f"{tool_name} exhausted {MAX_ATTEMPTS} attempts."}


def bootstrap_tools() -> None:
    """Register the fleet's tools. Imports are local to avoid circular imports."""

    import data_room_loader
    import mcp_server

    register_tool(
        ToolPolicy(
            name="collect_company_records",
            required_scope=agent_identity.SCOPE_STATUTORY_READ,
            handler=lambda crn: mcp_server.collect_company_records(mcp_server.CompanyQuery(crn=crn)),
            egress_hosts=("api.company-information.service.gov.uk",),
            description="Collect the Companies House statutory record bundle for one CRN.",
        )
    )
    register_tool(
        ToolPolicy(
            name="analyze_statutory_accounts",
            required_scope=agent_identity.SCOPE_STATUTORY_READ,
            handler=lambda crn, max_filings=2: mcp_server.analyze_statutory_accounts(
                mcp_server.CompanyQuery(crn=crn), max_filings=max_filings
            ),
            egress_hosts=(
                "api.company-information.service.gov.uk",
                "document-api.company-information.service.gov.uk",
            ),
            description="Download filed iXBRL accounts and compute balance sheet metrics deterministically.",
        )
    )
    register_tool(
        ToolPolicy(
            name="load_data_room",
            required_scope=agent_identity.SCOPE_DATA_ROOM_READ,
            handler=lambda path: data_room_loader.load_data_room(path),
            description="Extract text from local deal documents in the data room folder.",
        )
    )
    register_tool(
        ToolPolicy(
            name="memory_bank.write",
            required_scope=agent_identity.SCOPE_MEMORY_WRITE,
            handler=lambda **payload: __import__("memory_bank").remember_audit(**payload),
            description="Persist a completed audit into the cross-session Memory Bank.",
        )
    )
