"""Agent Identity: per-agent workload identities and short-lived signed tokens.

Each agent in the fleet is a distinct principal with its own scopes. Agents never
call a tool directly; they mint a short-lived token and present it to the Gateway,
which verifies the signature, the expiry, and the scope before any egress happens.

Signing key resolution order:
1. FLEET_SIGNING_KEY environment variable (inject via Secret Manager in Cloud Run).
2. A locally generated development key persisted under the fleet state directory.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STATE_DIR = Path(os.getenv("FLEET_STATE_DIR", ".fleet"))
DEV_KEY_PATH = STATE_DIR / "dev_signing_key"
DEFAULT_TOKEN_TTL_SECONDS = int(os.getenv("FLEET_TOKEN_TTL_SECONDS", "300"))

# Scopes are coarse capabilities the Gateway understands.
SCOPE_STATUTORY_READ = "companies_house.read"
SCOPE_DATA_ROOM_READ = "data_room.read"
SCOPE_MODEL_INVOKE = "model.invoke"
SCOPE_MEMORY_READ = "memory.read"
SCOPE_MEMORY_WRITE = "memory.write"
SCOPE_REGISTRY_WRITE = "registry.write"


class IdentityError(Exception):
    """Raised when a token cannot be minted or verified."""


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str
    display_name: str
    version: str
    scopes: tuple[str, ...]
    service_account: str = field(default="")

    @property
    def principal(self) -> str:
        return f"{self.agent_id}@{self.version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "version": self.version,
            "scopes": list(self.scopes),
            "service_account": self.service_account
            or os.getenv("FLEET_SERVICE_ACCOUNT", "local-development"),
            "principal": self.principal,
        }


FLEET_IDENTITIES: dict[str, AgentIdentity] = {
    "orchestrator": AgentIdentity(
        agent_id="orchestrator",
        display_name="Orchestrator Agent",
        version="1.3.0",
        scopes=(
            SCOPE_STATUTORY_READ,
            SCOPE_DATA_ROOM_READ,
            SCOPE_MEMORY_READ,
            SCOPE_MEMORY_WRITE,
            SCOPE_REGISTRY_WRITE,
        ),
    ),
    "legal_risk": AgentIdentity(
        agent_id="legal_risk",
        display_name="Legal Risk Agent",
        version="1.1.0",
        scopes=(SCOPE_MODEL_INVOKE, SCOPE_MEMORY_READ),
    ),
    "financial_auditor": AgentIdentity(
        agent_id="financial_auditor",
        display_name="Financial Auditor Agent",
        version="1.2.0",
        scopes=(SCOPE_MODEL_INVOKE, SCOPE_MEMORY_READ),
    ),
    "debate": AgentIdentity(
        agent_id="debate",
        display_name="Debate Agent",
        version="1.0.0",
        scopes=(SCOPE_MODEL_INVOKE,),
    ),
    "synthesizer": AgentIdentity(
        agent_id="synthesizer",
        display_name="Synthesizer Agent",
        version="1.0.0",
        scopes=(SCOPE_MODEL_INVOKE, SCOPE_MEMORY_WRITE),
    ),
}


def get_identity(agent_id: str) -> AgentIdentity:
    try:
        return FLEET_IDENTITIES[agent_id]
    except KeyError as exc:
        raise IdentityError(f"Unknown agent identity: {agent_id}") from exc


def _signing_key() -> bytes:
    configured = os.getenv("FLEET_SIGNING_KEY", "").strip()
    if configured:
        return configured.encode("utf-8")

    if DEV_KEY_PATH.exists():
        return DEV_KEY_PATH.read_bytes()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    DEV_KEY_PATH.write_bytes(key)
    return key


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def mint_token(
    agent_id: str,
    *,
    audience: str,
    scopes: list[str] | None = None,
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
) -> str:
    """Mint a short-lived signed token for one agent to call one audience."""

    identity = get_identity(agent_id)
    requested = tuple(scopes) if scopes else identity.scopes
    missing = [scope for scope in requested if scope not in identity.scopes]
    if missing:
        raise IdentityError(f"{identity.principal} is not granted scope(s): {', '.join(missing)}")

    issued_at = int(time.time())
    claims = {
        "sub": identity.principal,
        "agent_id": identity.agent_id,
        "ver": identity.version,
        "aud": audience,
        "scopes": list(requested),
        "iat": issued_at,
        "exp": issued_at + max(1, ttl_seconds),
        "jti": secrets.token_hex(8),
        "sa": identity.service_account or os.getenv("FLEET_SERVICE_ACCOUNT", "local-development"),
    }
    payload = _b64(json.dumps(claims, sort_keys=True).encode("utf-8"))
    signature = hmac.new(_signing_key(), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_b64(signature)}"


def verify_token(token: str, *, audience: str, required_scope: str) -> dict[str, Any]:
    """Verify signature, expiry, audience, and scope. Raises IdentityError on failure."""

    try:
        payload, signature = token.split(".", 1)
    except ValueError as exc:
        raise IdentityError("Malformed agent token.") from exc

    expected = hmac.new(_signing_key(), payload.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _unb64(signature)):
        raise IdentityError("Agent token signature is invalid.")

    claims = json.loads(_unb64(payload).decode("utf-8"))
    if claims.get("exp", 0) <= int(time.time()):
        raise IdentityError("Agent token has expired.")
    if claims.get("aud") != audience:
        raise IdentityError(f"Agent token audience mismatch: {claims.get('aud')} != {audience}")
    if required_scope not in claims.get("scopes", []):
        raise IdentityError(f"{claims.get('sub')} lacks required scope {required_scope}.")
    return claims


def fleet_roster() -> list[dict[str, Any]]:
    return [identity.to_dict() for identity in FLEET_IDENTITIES.values()]
