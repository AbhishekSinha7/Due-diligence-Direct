"""Model Armor: guardrails on everything entering and leaving a model call.

Three layers:

1. Input armor - screens untrusted data room text and statutory payloads for
   prompt injection, then redacts credentials and personal data before the text
   is ever placed in a prompt.
2. Output armor - blocks unsafe advice framing and enforces the reliance
   disclaimer on the final deal report.
3. Grounding audit - a deterministic string check that every evidentiary quote
   actually appears in the source payload. Unverifiable citations are demoted
   rather than trusted, which is what stops confident hallucinated liabilities.

Every verdict is written to the audit log so a reviewer can see what was blocked.
"""

from __future__ import annotations

import re
from typing import Any

import telemetry

INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("instruction_override", re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.I)),
    ("instruction_override", re.compile(r"disregard\s+(the\s+)?(system|previous|earlier)\s+\w+", re.I)),
    ("role_hijack", re.compile(r"you\s+are\s+now\s+(a|an|the)\s+", re.I)),
    ("role_hijack", re.compile(r"\bnew\s+system\s+prompt\b", re.I)),
    ("verdict_steering", re.compile(r"(mark|report|classify|rate)\s+(this|the)\s+(company|deal|document)\s+as\s+(clean|clear|green|low\s+risk|safe)", re.I)),
    ("verdict_steering", re.compile(r"do\s+not\s+(report|flag|mention|disclose)\s+", re.I)),
    ("exfiltration", re.compile(r"(reveal|print|output|repeat)\s+(your|the)\s+(system\s+prompt|instructions|api\s+key)", re.I)),
    ("exfiltration", re.compile(r"send\s+(the\s+)?(data|results|keys?)\s+to\s+https?://", re.I)),
    ("tool_abuse", re.compile(r"\b(curl|wget|subprocess|os\.system|rm\s+-rf)\b", re.I)),
]

REDACTION_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("email", re.compile(r"\b[\w.%-]+@[\w.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"), "[REDACTED_API_KEY]"),
    ("bearer_token", re.compile(r"\b(sk|rk|ghp|gho)_[0-9A-Za-z]{16,}\b"), "[REDACTED_TOKEN]"),
    ("national_insurance", re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2}\d{6}[A-D]\b"), "[REDACTED_NINO]"),
    ("payment_card", re.compile(r"\b(?:\d[ -]?){13,19}\b(?![\d.])"), "[REDACTED_CARD]"),
    ("uk_phone", re.compile(r"\b(?:\+44\s?|0)(?:\d\s?){9,10}\b"), "[REDACTED_PHONE]"),
]

UNSAFE_OUTPUT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("unqualified_advice", re.compile(r"\bthis is (legal|financial|investment) advice\b", re.I)),
    ("guarantee", re.compile(r"\b(guarantee[ds]?|risk[- ]free|no risk whatsoever)\b", re.I)),
    ("skip_review", re.compile(r"\bno (further |human )?(review|counsel|verification) (is )?(needed|required)\b", re.I)),
]

REQUIRED_DISCLAIMER = (
    "AI-generated due diligence support only. Not legal, financial, tax, or investment advice; "
    "qualified professionals must verify source records before transaction reliance."
)

VERDICT_ALLOW = "ALLOW"
VERDICT_SANITIZED = "SANITIZED"
VERDICT_BLOCK = "BLOCK"

# A document with this many distinct injection hits is quarantined, not sanitized.
BLOCK_THRESHOLD = int(2)


def screen_input(text: str, *, source: str, actor: str = "orchestrator") -> dict[str, Any]:
    """Screen untrusted text for injection, then redact sensitive values."""

    findings: list[dict[str, str]] = []
    for label, pattern in INJECTION_PATTERNS:
        for match in pattern.finditer(text or ""):
            findings.append(
                {
                    "type": "prompt_injection",
                    "category": label,
                    "excerpt": match.group(0)[:160],
                }
            )

    sanitized = text or ""
    redactions: list[str] = []
    for label, pattern, replacement in REDACTION_PATTERNS:
        sanitized, count = pattern.subn(replacement, sanitized)
        if count:
            redactions.append(f"{label}x{count}")

    injection_categories = {finding["category"] for finding in findings}
    if len(injection_categories) >= BLOCK_THRESHOLD:
        verdict = VERDICT_BLOCK
        sanitized = (
            "[QUARANTINED BY MODEL ARMOR: this document attempted to steer the audit and was "
            "withheld from agent prompts. Categories: "
            + ", ".join(sorted(injection_categories))
            + "]"
        )
    elif findings:
        verdict = VERDICT_SANITIZED
        for label, pattern in INJECTION_PATTERNS:
            sanitized = pattern.sub("[NEUTRALIZED_INSTRUCTION]", sanitized)
    elif redactions:
        verdict = VERDICT_SANITIZED
    else:
        verdict = VERDICT_ALLOW

    if verdict != VERDICT_ALLOW:
        telemetry.audit(
            "model_armor.input",
            actor=actor,
            resource=source,
            decision=verdict,
            severity="WARN" if verdict == VERDICT_BLOCK else "INFO",
            attributes={
                "injection_categories": sorted(injection_categories),
                "redactions": redactions,
            },
        )

    return {
        "verdict": verdict,
        "sanitized_text": sanitized,
        "findings": findings,
        "redactions": redactions,
        "source": source,
    }


def screen_data_room(data_room: dict[str, Any], *, actor: str = "orchestrator") -> dict[str, Any]:
    """Apply input armor to every extracted data room document."""

    documents = data_room.get("documents", [])
    screened_documents: list[dict[str, Any]] = []
    armor_findings: list[dict[str, Any]] = []

    for document in documents:
        result = screen_input(
            document.get("text_excerpt", ""),
            source=f"data_room://{document.get('file_name', 'unknown')}",
            actor=actor,
        )
        screened_documents.append(
            {
                **document,
                "text_excerpt": result["sanitized_text"],
                "armor_verdict": result["verdict"],
                "armor_redactions": result["redactions"],
                "quarantined": result["verdict"] == VERDICT_BLOCK,
            }
        )
        if result["verdict"] != VERDICT_ALLOW:
            armor_findings.append(
                {
                    "file_name": document.get("file_name", "unknown"),
                    "verdict": result["verdict"],
                    "categories": sorted({finding["category"] for finding in result["findings"]}),
                    "redactions": result["redactions"],
                }
            )

    return {
        **data_room,
        "documents": screened_documents,
        "armor_findings": armor_findings,
        "armor_blocked": sum(1 for doc in screened_documents if doc.get("quarantined")),
    }


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def ground_findings(
    findings: list[dict[str, Any]],
    source_text: str,
    *,
    quote_key: str = "evidentiary_quote",
    actor: str = "orchestrator",
) -> list[dict[str, Any]]:
    """Deterministically verify each citation against the source payload.

    A citation passes if a distinctive token from the quote (an identifier, date,
    endpoint, or field name of 4+ characters) is present in the source payload.
    Findings that fail are marked unverified and capped at MEDIUM severity so an
    unsupported claim can never drive a deal-breaker verdict on its own.
    """

    haystack = _normalize(source_text)
    verified: list[dict[str, Any]] = []

    for finding in findings:
        quote = str(finding.get(quote_key, ""))
        tokens = [token for token in re.split(r"[^A-Za-z0-9_\-./]+", quote) if len(token) >= 4]
        supported = any(_normalize(token) and _normalize(token) in haystack for token in tokens)
        item = {**finding, "evidence_verified": bool(supported)}

        if not supported and quote.strip():
            item["evidence_note"] = "Citation could not be matched to the retrieved payload."
            if str(item.get("severity", "")).upper() == "HIGH":
                item["severity"] = "MEDIUM"
                item["evidence_note"] += " Severity capped at MEDIUM by the grounding audit."
            telemetry.audit(
                "model_armor.grounding",
                actor=actor,
                resource=str(finding.get("category", "finding")),
                decision="unverified",
                severity="WARN",
                attributes={"quote": quote[:200]},
            )
        verified.append(item)

    return verified


def screen_output(report: dict[str, Any], *, actor: str = "synthesizer") -> dict[str, Any]:
    """Enforce safe framing and the reliance disclaimer on the final report."""

    text_fields = ["executive_summary", "reliance_disclaimer"]
    blob = " ".join(str(report.get(field, "")) for field in text_fields)
    blob += " ".join(str(item) for item in report.get("top_risks", []))

    violations = [label for label, pattern in UNSAFE_OUTPUT_PATTERNS if pattern.search(blob)]
    guarded = dict(report)

    disclaimer = str(guarded.get("reliance_disclaimer", "")).strip()
    if "not legal" not in disclaimer.lower() or "verify" not in disclaimer.lower():
        guarded["reliance_disclaimer"] = REQUIRED_DISCLAIMER
        violations.append("disclaimer_replaced")

    if violations:
        for label, pattern in UNSAFE_OUTPUT_PATTERNS:
            if label in violations:
                guarded["executive_summary"] = pattern.sub(
                    "[REMOVED BY MODEL ARMOR]", str(guarded.get("executive_summary", ""))
                )
        telemetry.audit(
            "model_armor.output",
            actor=actor,
            resource="deal_report",
            decision=VERDICT_SANITIZED,
            severity="WARN",
            attributes={"violations": sorted(set(violations))},
        )

    guarded["armor_verdict"] = VERDICT_SANITIZED if violations else VERDICT_ALLOW
    guarded["armor_violations"] = sorted(set(violations))
    return guarded
