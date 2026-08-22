"""Agent Registry: discovery and lifecycle management for the fleet.

Every agent publishes a versioned agent card before it can be dispatched. The
registry is the single source of truth for:

- what agents exist and what they can do (discovery),
- which version is active, deprecated, or retired (lifecycle),
- which scopes and tools each version is allowed to request (governance input).

Storage is SQLite so the registry survives restarts. Set FLEET_REGISTRY_DB to a
mounted volume (or Cloud SQL / Filestore path) for a shared fleet deployment.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import agent_identity
import telemetry

STATE_DIR = Path(os.getenv("FLEET_STATE_DIR", ".fleet"))
REGISTRY_DB = Path(os.getenv("FLEET_REGISTRY_DB", str(STATE_DIR / "registry.db")))

STATUS_ACTIVE = "ACTIVE"
STATUS_DEPRECATED = "DEPRECATED"
STATUS_RETIRED = "RETIRED"


class RegistryError(Exception):
    """Raised when a registry operation is rejected."""


def _connect() -> sqlite3.Connection:
    REGISTRY_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(REGISTRY_DB)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_cards (
            agent_id TEXT NOT NULL,
            version TEXT NOT NULL,
            display_name TEXT NOT NULL,
            description TEXT NOT NULL,
            capabilities TEXT NOT NULL,
            input_schema TEXT NOT NULL,
            output_schema TEXT NOT NULL,
            scopes TEXT NOT NULL,
            tools TEXT NOT NULL,
            model TEXT NOT NULL,
            status TEXT NOT NULL,
            published_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (agent_id, version)
        )
        """
    )
    connection.commit()
    return connection


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """Open, commit, and always close a registry connection."""

    connection = _connect()
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _version_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in version.split("."):
        digits = "".join(character for character in chunk if character.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def publish_agent(
    agent_id: str,
    *,
    version: str,
    description: str,
    capabilities: list[str],
    input_schema: str,
    output_schema: str,
    tools: list[str],
    model: str,
    display_name: str = "",
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    """Publish or update an agent card. Republishing the same version updates it."""

    identity = agent_identity.get_identity(agent_id)
    granted = list(scopes) if scopes is not None else list(identity.scopes)
    ungranted = [scope for scope in granted if scope not in identity.scopes]
    if ungranted:
        raise RegistryError(
            f"Cannot publish {agent_id}@{version}: identity does not hold scope(s) {', '.join(ungranted)}"
        )

    card = {
        "agent_id": agent_id,
        "version": version,
        "display_name": display_name or identity.display_name,
        "description": description,
        "capabilities": capabilities,
        "input_schema": input_schema,
        "output_schema": output_schema,
        "scopes": granted,
        "tools": tools,
        "model": model,
        "status": STATUS_ACTIVE,
    }

    with _db() as connection:
        existing = connection.execute(
            "SELECT published_at FROM agent_cards WHERE agent_id = ? AND version = ?",
            (agent_id, version),
        ).fetchone()
        published_at = existing["published_at"] if existing else _now()
        connection.execute(
            """
            INSERT INTO agent_cards (
                agent_id, version, display_name, description, capabilities,
                input_schema, output_schema, scopes, tools, model, status,
                published_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id, version) DO UPDATE SET
                display_name = excluded.display_name,
                description = excluded.description,
                capabilities = excluded.capabilities,
                input_schema = excluded.input_schema,
                output_schema = excluded.output_schema,
                scopes = excluded.scopes,
                tools = excluded.tools,
                model = excluded.model,
                status = excluded.status,
                updated_at = excluded.updated_at
            """,
            (
                agent_id,
                version,
                card["display_name"],
                description,
                json.dumps(capabilities),
                input_schema,
                output_schema,
                json.dumps(granted),
                json.dumps(tools),
                model,
                STATUS_ACTIVE,
                published_at,
                _now(),
            ),
        )

    telemetry.audit(
        "registry.publish",
        actor=identity.principal,
        resource=f"agent://{agent_id}@{version}",
        decision="allow",
        attributes={"capabilities": capabilities, "tools": tools, "model": model},
    )
    return card


def set_status(agent_id: str, version: str, status: str) -> dict[str, Any]:
    """Move a published version through its lifecycle."""

    if status not in {STATUS_ACTIVE, STATUS_DEPRECATED, STATUS_RETIRED}:
        raise RegistryError(f"Unsupported lifecycle status: {status}")

    with _db() as connection:
        cursor = connection.execute(
            "UPDATE agent_cards SET status = ?, updated_at = ? WHERE agent_id = ? AND version = ?",
            (status, _now(), agent_id, version),
        )
        if cursor.rowcount == 0:
            raise RegistryError(f"No published card for {agent_id}@{version}")

    telemetry.audit(
        "registry.lifecycle",
        actor="orchestrator",
        resource=f"agent://{agent_id}@{version}",
        decision=status,
    )
    return {"agent_id": agent_id, "version": version, "status": status}


def _row_to_card(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "agent_id": row["agent_id"],
        "version": row["version"],
        "display_name": row["display_name"],
        "description": row["description"],
        "capabilities": json.loads(row["capabilities"]),
        "input_schema": row["input_schema"],
        "output_schema": row["output_schema"],
        "scopes": json.loads(row["scopes"]),
        "tools": json.loads(row["tools"]),
        "model": row["model"],
        "status": row["status"],
        "published_at": row["published_at"],
        "updated_at": row["updated_at"],
    }


def list_agents(include_retired: bool = False) -> list[dict[str, Any]]:
    with _db() as connection:
        rows = connection.execute("SELECT * FROM agent_cards").fetchall()

    cards = [_row_to_card(row) for row in rows]
    if not include_retired:
        cards = [card for card in cards if card["status"] != STATUS_RETIRED]
    return sorted(cards, key=lambda card: (card["agent_id"], _version_key(card["version"])))


def resolve_agent(agent_id: str, version: str | None = None) -> dict[str, Any]:
    """Resolve the requested version, or the highest ACTIVE version by default."""

    with _db() as connection:
        if version:
            row = connection.execute(
                "SELECT * FROM agent_cards WHERE agent_id = ? AND version = ?",
                (agent_id, version),
            ).fetchone()
            if row is None:
                raise RegistryError(f"No registered card for {agent_id}@{version}")
            card = _row_to_card(row)
            if card["status"] == STATUS_RETIRED:
                raise RegistryError(f"{agent_id}@{version} is retired and cannot be dispatched.")
            return card

        rows = connection.execute(
            "SELECT * FROM agent_cards WHERE agent_id = ? AND status = ?",
            (agent_id, STATUS_ACTIVE),
        ).fetchall()

    if not rows:
        raise RegistryError(f"No ACTIVE version registered for agent {agent_id}")
    cards = [_row_to_card(row) for row in rows]
    return max(cards, key=lambda card: _version_key(card["version"]))


def find_by_capability(capability: str) -> list[dict[str, Any]]:
    return [
        card
        for card in list_agents()
        if capability in card["capabilities"] and card["status"] == STATUS_ACTIVE
    ]


FLEET_CARDS: list[dict[str, Any]] = [
    {
        "agent_id": "orchestrator",
        "version": "1.5.0",
        "description": "Resolves a company by name or number, plans the diligence run, ingests statutory records, filed accounts, and data room evidence, triages documents with an open model, then fans work out to specialist agents.",
        "capabilities": [
            "company_search",
            "planning",
            "statutory_ingestion",
            "accounts_ingestion",
            "data_room_ingestion",
            "document_triage",
            "semantic_clause_detection",
            "fan_out",
        ],
        "input_schema": "DueDiligenceRequest",
        "output_schema": "DueDiligenceState",
        "tools": [
            "search_companies",
            "collect_company_records",
            "analyze_statutory_accounts",
            "load_data_room",
            "gemma.classify_documents",
            "embedding.clause_scan",
        ],
    },
    {
        "agent_id": "legal_risk",
        "version": "1.1.0",
        "description": "Evaluates insolvency, registered charges, PSC control, and contract liabilities with evidence citations.",
        "capabilities": ["legal_risk_analysis", "evidence_citation"],
        "input_schema": "StatutoryBundle",
        "output_schema": "LegalAuditReport",
        "tools": ["gemini.generate_structured"],
    },
    {
        "agent_id": "financial_auditor",
        "version": "1.2.0",
        "description": "Interprets deterministically computed balance sheet metrics from filed iXBRL accounts, plus company standing and filing regularity.",
        "capabilities": ["financial_analysis", "accounts_interpretation", "filing_compliance"],
        "input_schema": "StatutoryBundle+AccountsMetrics",
        "output_schema": "FinancialAuditReport",
        "tools": ["gemini.generate_structured"],
    },
    {
        "agent_id": "debate",
        "version": "1.0.0",
        "description": "Runs the adversarial reconciliation between legal and financial positions and resolves conflicts conservatively.",
        "capabilities": ["conflict_resolution", "adversarial_review"],
        "input_schema": "LegalAuditReport+FinancialAuditReport",
        "output_schema": "DebateReport",
        "tools": ["gemini.generate_structured"],
    },
    {
        "agent_id": "runtime",
        "version": "1.0.0",
        "description": "Executes diligence jobs asynchronously and notifies external systems when one reaches a terminal state.",
        "capabilities": ["async_execution", "notification_dispatch"],
        "input_schema": "DueDiligenceRequest",
        "output_schema": "JobRecord",
        "tools": ["notify.dispatch"],
    },
    {
        "agent_id": "synthesizer",
        "version": "1.0.0",
        "description": "Compiles the Red Flag Report, deal recommendation, and reliance disclaimer, then writes fleet memory.",
        "capabilities": ["report_synthesis", "recommendation"],
        "input_schema": "DueDiligenceState",
        "output_schema": "DealReport",
        "tools": ["gemini.generate_structured", "memory_bank.write"],
    },
]


def bootstrap_registry(model: str | None = None) -> list[dict[str, Any]]:
    """Publish the built-in fleet cards. Safe to call on every startup."""

    resolved_model = model or os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
    published: list[dict[str, Any]] = []
    for card in FLEET_CARDS:
        published.append(
            publish_agent(
                card["agent_id"],
                version=card["version"],
                description=card["description"],
                capabilities=card["capabilities"],
                input_schema=card["input_schema"],
                output_schema=card["output_schema"],
                tools=card["tools"],
                model=resolved_model,
            )
        )
    return published
