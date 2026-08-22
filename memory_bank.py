"""Memory Bank: persistent, cross-session context for the fleet.

The fleet is long-lived, so a second audit of the same company must not start from
zero. The Memory Bank stores, per company:

- every completed audit (verdict, severity profile, run id, trace id),
- a compact fact sheet (status, charge count, insolvency cases, accounts state),
- operator notes written by a human reviewer.

On a later run the orchestrator recalls the fact sheet and computes a delta, so the
agents can reason about what changed since the last audit - for example a floating
charge registered between two runs. That temporal signal is impossible for a
single-shot chatbot and is the reason this layer exists.

Storage is SQLite by default. Set FLEET_MEMORY_DB to a mounted path for shared
deployments; the schema is intentionally small enough to port to Firestore.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import telemetry

STATE_DIR = Path(os.getenv("FLEET_STATE_DIR", ".fleet"))
MEMORY_DB = Path(os.getenv("FLEET_MEMORY_DB", str(STATE_DIR / "memory.db")))

TRACKED_FACTS = (
    "company_name",
    "company_status",
    "accounts_overdue",
    "accounts_next_due",
    "charge_count",
    "insolvency_cases",
    "psc_count",
    "active_officers",
    "net_assets",
    "accounts_period_end",
)


def _connect() -> sqlite3.Connection:
    MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(MEMORY_DB)
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS audit_memory (
            run_id TEXT PRIMARY KEY,
            crn TEXT NOT NULL,
            created_at TEXT NOT NULL,
            recommendation TEXT NOT NULL,
            executive_summary TEXT NOT NULL,
            severity_counts TEXT NOT NULL,
            facts TEXT NOT NULL,
            trace_id TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_audit_memory_crn ON audit_memory (crn, created_at);

        CREATE TABLE IF NOT EXISTS operator_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            crn TEXT NOT NULL,
            created_at TEXT NOT NULL,
            author TEXT NOT NULL,
            note TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_operator_notes_crn ON operator_notes (crn, created_at);
        """
    )
    connection.commit()
    return connection


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """Open, commit, and always close a memory connection."""

    connection = _connect()
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def extract_facts(bundle: dict[str, Any], accounts: dict[str, Any] | None = None) -> dict[str, Any]:
    """Reduce a statutory bundle and filed accounts to the fact sheet worth remembering."""

    def data(key: str) -> dict[str, Any]:
        record = bundle.get(key, {})
        payload = record.get("data", {}) if isinstance(record, dict) else {}
        return payload if isinstance(payload, dict) else {}

    profile = data("profile")
    profile_accounts = profile.get("accounts", {}) if isinstance(profile.get("accounts"), dict) else {}
    charges = data("charges")
    insolvency = data("insolvency")
    pscs = data("pscs")
    officers = data("officers")

    charge_items = charges.get("items", [])
    psc_items = pscs.get("items", [])
    insolvency_cases = insolvency.get("cases", [])

    derived = ((accounts or {}).get("latest") or {}).get("analysis", {}).get("derived", {})

    return {
        "company_name": profile.get("company_name", "unknown"),
        "company_status": profile.get("company_status", "unknown"),
        "accounts_overdue": bool(profile_accounts.get("overdue", False)),
        "accounts_next_due": profile_accounts.get("next_due", "unknown"),
        "net_assets": derived.get("net_assets"),
        "accounts_period_end": derived.get("latest_period_end"),
        "charge_count": int(charges.get("total_count", 0) or (len(charge_items) if isinstance(charge_items, list) else 0)),
        "insolvency_cases": len(insolvency_cases) if isinstance(insolvency_cases, list) else 0,
        "psc_count": int(pscs.get("total_results", 0) or (len(psc_items) if isinstance(psc_items, list) else 0)),
        "active_officers": int(officers.get("active_count", 0) or 0),
    }


def remember_audit(
    *,
    run_id: str,
    crn: str,
    recommendation: str,
    executive_summary: str,
    severity_counts: dict[str, int],
    facts: dict[str, Any],
    trace_id: str = "",
    actor: str = "synthesizer",
) -> dict[str, Any]:
    """Persist one completed audit so later sessions can compare against it."""

    record = {
        "run_id": run_id,
        "crn": crn,
        "created_at": _now(),
        "recommendation": recommendation,
        "executive_summary": executive_summary[:2000],
        "severity_counts": severity_counts,
        "facts": facts,
        "trace_id": trace_id,
    }

    with _db() as connection:
        connection.execute(
            """
            INSERT INTO audit_memory (
                run_id, crn, created_at, recommendation, executive_summary,
                severity_counts, facts, trace_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                recommendation = excluded.recommendation,
                executive_summary = excluded.executive_summary,
                severity_counts = excluded.severity_counts,
                facts = excluded.facts,
                trace_id = excluded.trace_id
            """,
            (
                run_id,
                crn,
                record["created_at"],
                recommendation,
                record["executive_summary"],
                json.dumps(severity_counts),
                json.dumps(facts, default=str),
                trace_id,
            ),
        )

    telemetry.audit(
        "memory.write",
        actor=actor,
        resource=f"memory://company/{crn}",
        decision="allow",
        attributes={"run_id": run_id, "recommendation": recommendation},
    )
    return record


def history(crn: str, limit: int = 5) -> list[dict[str, Any]]:
    with _db() as connection:
        rows = connection.execute(
            "SELECT * FROM audit_memory WHERE crn = ? ORDER BY created_at DESC LIMIT ?",
            (crn, limit),
        ).fetchall()

    return [
        {
            "run_id": row["run_id"],
            "crn": row["crn"],
            "created_at": row["created_at"],
            "recommendation": row["recommendation"],
            "executive_summary": row["executive_summary"],
            "severity_counts": json.loads(row["severity_counts"]),
            "facts": json.loads(row["facts"]),
            "trace_id": row["trace_id"],
        }
        for row in rows
    ]


def add_note(crn: str, note: str, author: str = "operator") -> dict[str, Any]:
    with _db() as connection:
        connection.execute(
            "INSERT INTO operator_notes (crn, created_at, author, note) VALUES (?, ?, ?, ?)",
            (crn, _now(), author, note),
        )
    telemetry.audit(
        "memory.note",
        actor=author,
        resource=f"memory://company/{crn}",
        decision="allow",
        attributes={"chars": len(note)},
    )
    return {"crn": crn, "author": author, "note": note}


def notes(crn: str, limit: int = 20) -> list[dict[str, Any]]:
    with _db() as connection:
        rows = connection.execute(
            "SELECT created_at, author, note FROM operator_notes WHERE crn = ? ORDER BY created_at DESC LIMIT ?",
            (crn, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def diff_facts(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the material changes between two fact sheets."""

    changes: list[dict[str, Any]] = []
    for key in TRACKED_FACTS:
        before = previous.get(key)
        after = current.get(key)
        if before == after or before is None:
            continue

        significance = "INFO"
        if key in {"charge_count", "insolvency_cases"} and isinstance(after, int) and isinstance(before, int):
            significance = "HIGH" if after > before else "INFO"
        elif key == "accounts_overdue" and after and not before:
            significance = "HIGH"
        elif key == "company_status":
            significance = "HIGH"
        elif key == "net_assets" and isinstance(before, (int, float)) and isinstance(after, (int, float)):
            # A newly filed balance sheet that goes negative, or halves, is material.
            if after < 0 <= before:
                significance = "HIGH"
            elif before > 0 and after < before * 0.5:
                significance = "HIGH"
            elif after < before:
                significance = "MEDIUM"

        changes.append({"fact": key, "previous": before, "current": after, "significance": significance})
    return changes


def recall(crn: str, current_facts: dict[str, Any] | None = None, *, actor: str = "orchestrator") -> dict[str, Any]:
    """Load prior context for a company and diff it against the current run."""

    past = history(crn)
    operator_notes = notes(crn)
    changes = diff_facts(past[0]["facts"], current_facts or {}) if past and current_facts else []

    telemetry.audit(
        "memory.read",
        actor=actor,
        resource=f"memory://company/{crn}",
        decision="allow",
        attributes={"prior_audits": len(past), "changes_detected": len(changes)},
    )

    return {
        "crn": crn,
        "prior_audits": past,
        "operator_notes": operator_notes,
        "changes_since_last_audit": changes,
        "is_first_audit": not past,
    }


def prompt_context(memory: dict[str, Any], max_chars: int = 2500) -> str:
    """Render recalled memory as a compact block for agent prompts."""

    if memory.get("is_first_audit"):
        return "FLEET MEMORY: no prior audit exists for this company. Treat every finding as a new baseline."

    lines = ["FLEET MEMORY (persistent across sessions):"]
    for entry in memory.get("prior_audits", [])[:3]:
        lines.append(
            f"- {entry['created_at']}: verdict {entry['recommendation']}; facts {json.dumps(entry['facts'], default=str)}"
        )

    changes = memory.get("changes_since_last_audit", [])
    if changes:
        lines.append("CHANGES SINCE LAST AUDIT (treat HIGH significance changes as priority findings):")
        for change in changes:
            lines.append(
                f"- {change['fact']}: {change['previous']} -> {change['current']} [{change['significance']}]"
            )
    else:
        lines.append("No tracked statutory facts changed since the last audit.")

    for note in memory.get("operator_notes", [])[:3]:
        lines.append(f"- operator note ({note['author']}): {note['note']}")

    return "\n".join(lines)[:max_chars]
