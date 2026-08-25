"""Per-caller API keys: scoped, expiring, rate-limited, and attributable.

A single shared secret cannot answer the questions an operator actually has:
who called, what are they allowed to do, how much may they spend, and how do I
cut off one caller without cutting off everyone. This module replaces it.

Keys are **self-contained and signed**, not stored in a table. That is a
deliberate response to where this runs: Cloud Run gives the container an
ephemeral disk, so a key database would be lost on every redeploy and would not
be shared between instances. A signed key needs no storage, works across
instances, and survives deployment.

    ddd_v1.<payload>.<signature>

The payload carries the key id, the caller's name, its scopes, when it expires,
and its budgets. The signature is HMAC-SHA256 under the fleet signing key -- the
same key that secures agent identity tokens -- so a key cannot be forged without
the secret held in Secret Manager.

The trade-off this makes is revocation: nothing stored means nothing to delete.
It is handled by listing revoked key ids (see `revoked_ids`) and by keeping
lifetimes short. Choose a TTL you would be comfortable being stuck with.

Issue one with:

    python api_keys.py issue --name "judge-demo" --scopes audits:read --days 7
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
from typing import Iterable, Sequence

import agent_identity

PREFIX = "ddd_v1."

# -- scopes ---------------------------------------------------------------

SCOPE_AUDITS_READ = "audits:read"
SCOPE_AUDITS_WRITE = "audits:write"
SCOPE_MEMORY_WRITE = "memory:write"
SCOPE_GOVERNANCE_READ = "governance:read"
SCOPE_ADMIN = "admin"

ALL_SCOPES = (
    SCOPE_AUDITS_READ,
    SCOPE_AUDITS_WRITE,
    SCOPE_MEMORY_WRITE,
    SCOPE_GOVERNANCE_READ,
    SCOPE_ADMIN,
)

SCOPE_DESCRIPTIONS = {
    SCOPE_AUDITS_READ: "Read audits, reports, company search, and the agent registry.",
    SCOPE_AUDITS_WRITE: "Submit audits, upload documents, and cancel runs. Spends model quota.",
    SCOPE_MEMORY_WRITE: "Add operator notes that later audits will see.",
    SCOPE_GOVERNANCE_READ: "Read the audit trail and verify the hash chain.",
    SCOPE_ADMIN: "Everything, including scopes added in future.",
}

# A sensible read-only set, which is what a demo or a monitoring check needs.
READ_ONLY = (SCOPE_AUDITS_READ, SCOPE_GOVERNANCE_READ)

DEFAULT_TTL_DAYS = int(os.getenv("FLEET_KEY_DEFAULT_TTL_DAYS", "30"))
DEFAULT_REQUESTS_PER_HOUR = int(os.getenv("FLEET_KEY_DEFAULT_REQUESTS_PER_HOUR", "600"))
DEFAULT_AUDITS_PER_HOUR = int(os.getenv("FLEET_KEY_DEFAULT_AUDITS_PER_HOUR", "20"))


class InvalidKey(Exception):
    """The key is malformed, forged, expired, or revoked."""


@dataclass(frozen=True)
class Principal:
    """Who is calling, and what they may do."""

    key_id: str
    name: str
    scopes: frozenset[str]
    kind: str = "api_key"
    expires_at: int = 0
    requests_per_hour: int = 0
    audits_per_hour: int = 0
    attributes: dict[str, object] = field(default_factory=dict)

    def has_scope(self, scope: str) -> bool:
        return SCOPE_ADMIN in self.scopes or scope in self.scopes

    @property
    def actor(self) -> str:
        """How this caller appears in the audit trail."""

        return f"{self.kind}:{self.name}" if self.name else self.kind

    def describe(self) -> dict[str, object]:
        return {
            "key_id": self.key_id,
            "name": self.name,
            "kind": self.kind,
            "scopes": sorted(self.scopes),
            "expires_at": self.expires_at,
            "requests_per_hour": self.requests_per_hour,
            "audits_per_hour": self.audits_per_hour,
        }


# Callers that are not API keys still need an identity, so every code path in
# the service can ask the same questions rather than special-casing.

def console_principal() -> Principal:
    """A person signed into the console. Everything except administration."""

    return Principal(
        key_id="console",
        name="console",
        kind="console",
        scopes=frozenset({SCOPE_AUDITS_READ, SCOPE_AUDITS_WRITE, SCOPE_MEMORY_WRITE, SCOPE_GOVERNANCE_READ}),
        requests_per_hour=0,
        audits_per_hour=int(os.getenv("FLEET_CONSOLE_AUDITS_PER_HOUR", "30")),
    )


def legacy_principal() -> Principal:
    """The single shared FLEET_API_KEY.

    Kept working so existing deployments do not break, but it is unattributable
    and unrevokable-in-isolation. Issue named keys and retire it.
    """

    return Principal(
        key_id="legacy",
        name="shared-api-key",
        kind="legacy_key",
        scopes=frozenset(ALL_SCOPES),
    )


def open_principal() -> Principal:
    """An unauthenticated deployment. Correct locally, never in production."""

    return Principal(
        key_id="anonymous",
        name="anonymous",
        kind="open",
        scopes=frozenset(ALL_SCOPES),
    )


# -- encoding -------------------------------------------------------------


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(payload: str) -> str:
    signature = hmac.new(
        agent_identity._signing_key(), payload.encode("ascii"), hashlib.sha256
    ).digest()
    return _b64encode(signature)


# -- issuing --------------------------------------------------------------


def issue(
    name: str,
    scopes: Sequence[str] = READ_ONLY,
    *,
    ttl_days: int = DEFAULT_TTL_DAYS,
    requests_per_hour: int = DEFAULT_REQUESTS_PER_HOUR,
    audits_per_hour: int = DEFAULT_AUDITS_PER_HOUR,
    issued_by: str = "operator",
) -> tuple[str, Principal]:
    """Mint a key. Returns the secret and what it grants.

    The secret is shown once, here, because nothing stores it. Losing it means
    issuing another, which is the correct failure mode.
    """

    name = " ".join(str(name).split())
    if not name:
        raise ValueError("A key needs a name; it is how the caller appears in the audit trail.")

    unknown = sorted(set(scopes) - set(ALL_SCOPES))
    if unknown:
        raise ValueError(f"Unknown scope(s): {', '.join(unknown)}. Valid: {', '.join(ALL_SCOPES)}")
    if not scopes:
        raise ValueError("A key with no scopes can do nothing; pass at least one.")
    if ttl_days <= 0:
        raise ValueError("ttl_days must be positive. A key that never expires is a liability.")

    now = int(time.time())
    claims = {
        "kid": secrets.token_hex(6),
        "nam": name,
        "scp": sorted(set(scopes)),
        "iat": now,
        "exp": now + ttl_days * 86400,
        "rph": int(requests_per_hour),
        "aph": int(audits_per_hour),
        "iss": issued_by,
    }
    payload = _b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    key = f"{PREFIX}{payload}.{_sign(payload)}"
    return key, _principal_from_claims(claims)


def _principal_from_claims(claims: dict) -> Principal:
    return Principal(
        key_id=str(claims.get("kid", "")),
        name=str(claims.get("nam", "")),
        scopes=frozenset(claims.get("scp", [])),
        kind="api_key",
        expires_at=int(claims.get("exp", 0)),
        requests_per_hour=int(claims.get("rph", 0)),
        audits_per_hour=int(claims.get("aph", 0)),
        attributes={"issued_by": claims.get("iss", ""), "issued_at": claims.get("iat", 0)},
    )


# -- revocation -----------------------------------------------------------

REVOKED_FILE = Path(os.getenv("FLEET_STATE_DIR", ".fleet")) / "revoked_keys"


def revoked_ids() -> frozenset[str]:
    """Key ids that must be refused despite a valid signature.

    Read fresh on every check so revoking does not require a redeploy: set
    FLEET_REVOKED_KEY_IDS (a Secret Manager value works well), or write ids one
    per line into the state directory.
    """

    ids = {
        item.strip()
        for item in os.getenv("FLEET_REVOKED_KEY_IDS", "").replace("\n", ",").split(",")
        if item.strip()
    }
    try:
        if REVOKED_FILE.exists():
            ids.update(
                line.strip()
                for line in REVOKED_FILE.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            )
    except OSError:
        pass
    return frozenset(ids)


def revoke(key_id: str) -> Path:
    """Record a key id as revoked in the local state directory."""

    REVOKED_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = revoked_ids()
    if key_id not in existing:
        with REVOKED_FILE.open("a", encoding="utf-8") as handle:
            handle.write(f"{key_id}\n")
    return REVOKED_FILE


# -- verifying ------------------------------------------------------------


def verify(key: str, *, now: int | None = None) -> Principal:
    """Validate a key and return the caller it identifies.

    Raises InvalidKey for anything wrong. Order matters: the signature is
    checked before the claims are trusted for anything at all.
    """

    if not key or not key.startswith(PREFIX):
        raise InvalidKey("Not a fleet API key.")

    body = key[len(PREFIX) :]
    payload, separator, signature = body.partition(".")
    if not separator or not payload or not signature:
        raise InvalidKey("Malformed key.")

    if not hmac.compare_digest(signature, _sign(payload)):
        raise InvalidKey("Signature does not verify; the key was not issued by this fleet.")

    try:
        claims = json.loads(_b64decode(payload))
    except (ValueError, json.JSONDecodeError) as exc:
        raise InvalidKey("Key payload is not readable.") from exc
    if not isinstance(claims, dict):
        raise InvalidKey("Key payload is not an object.")

    moment = int(time.time()) if now is None else now
    expires_at = int(claims.get("exp", 0))
    if expires_at and moment >= expires_at:
        raise InvalidKey("This key has expired.")

    key_id = str(claims.get("kid", ""))
    if key_id in revoked_ids():
        raise InvalidKey("This key has been revoked.")

    return _principal_from_claims(claims)


def inspect(key: str) -> dict[str, object]:
    """Read a key's claims without validating expiry or revocation.

    For an operator working out what a key in a config file actually is. The
    signature is still checked, so this cannot be used to read a forged key.
    """

    body = key[len(PREFIX) :] if key.startswith(PREFIX) else key
    payload, _, signature = body.partition(".")
    if not hmac.compare_digest(signature, _sign(payload)):
        raise InvalidKey("Signature does not verify.")
    claims = json.loads(_b64decode(payload))
    now = int(time.time())
    return {
        **claims,
        "expired": bool(claims.get("exp")) and now >= int(claims["exp"]),
        "revoked": str(claims.get("kid", "")) in revoked_ids(),
        "expires_in_days": round((int(claims.get("exp", now)) - now) / 86400, 1),
    }


# -- command line ---------------------------------------------------------


def _main(argv: Iterable[str] | None = None) -> int:  # pragma: no cover - operator tool
    import argparse

    parser = argparse.ArgumentParser(
        prog="api_keys",
        description="Issue and inspect fleet API keys.",
        epilog="Scopes: " + "  ".join(f"{s}" for s in ALL_SCOPES),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("issue", help="mint a key")
    new.add_argument("--name", required=True, help="who the key is for; appears in the audit trail")
    new.add_argument(
        "--scopes",
        nargs="+",
        default=list(READ_ONLY),
        choices=ALL_SCOPES,
        help=f"default: {' '.join(READ_ONLY)}",
    )
    new.add_argument("--days", type=int, default=DEFAULT_TTL_DAYS)
    new.add_argument("--requests-per-hour", type=int, default=DEFAULT_REQUESTS_PER_HOUR)
    new.add_argument("--audits-per-hour", type=int, default=DEFAULT_AUDITS_PER_HOUR)

    show = sub.add_parser("inspect", help="decode a key")
    show.add_argument("key")

    check = sub.add_parser("verify", help="validate a key as the service would")
    check.add_argument("key")

    kill = sub.add_parser("revoke", help="revoke a key id")
    kill.add_argument("key_id")

    sub.add_parser("scopes", help="list the available scopes")

    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "issue":
        key, principal = issue(
            args.name,
            args.scopes,
            ttl_days=args.days,
            requests_per_hour=args.requests_per_hour,
            audits_per_hour=args.audits_per_hour,
        )
        print(key)
        print()
        print(f"  name     {principal.name}")
        print(f"  key id   {principal.key_id}")
        print(f"  scopes   {', '.join(sorted(principal.scopes))}")
        print(f"  expires  in {args.days} day(s)")
        print(f"  budget   {principal.requests_per_hour}/h requests, {principal.audits_per_hour}/h audits")
        print()
        print("Store it now: nothing on the server has a copy.")
        print("Send it as the x-fleet-api-key header, or FLEET_API_KEY for ddclient.")
        return 0

    if args.command == "inspect":
        print(json.dumps(inspect(args.key), indent=2))
        return 0

    if args.command == "verify":
        try:
            principal = verify(args.key)
        except InvalidKey as exc:
            print(f"REJECTED: {exc}")
            return 1
        print(json.dumps(principal.describe(), indent=2))
        return 0

    if args.command == "revoke":
        path = revoke(args.key_id)
        print(f"Revoked {args.key_id}; recorded in {path}")
        print("On Cloud Run, add it to FLEET_REVOKED_KEY_IDS instead - container disks do not persist.")
        return 0

    for scope in ALL_SCOPES:
        print(f"  {scope:<20} {SCOPE_DESCRIPTIONS[scope]}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
