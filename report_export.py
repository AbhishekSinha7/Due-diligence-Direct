"""Render a completed audit as a PDF a deal team can file.

The Red Flag Report is the artefact that leaves the system, so it carries what a
reviewer needs to check the work: the verdict, the evidence behind every finding
with its citation, the figures taken from the filed accounts, the governance
record (models used, guardrail verdicts, unverified citations), and the reliance
disclaimer.

Deliberately plain: no images, no external fonts, no network. It renders the same
whether the run used a model or the deterministic engine.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

SEVERITY_COLORS = {
    "HIGH": colors.HexColor("#b91c1c"),
    "MEDIUM": colors.HexColor("#b45309"),
    "LOW": colors.HexColor("#2563eb"),
    "CLEAR": colors.HexColor("#15803d"),
}
VERDICT_COLORS = {
    "GREEN LIGHT": colors.HexColor("#15803d"),
    "PROCEED WITH CAUTION": colors.HexColor("#b45309"),
    "RED FLAG DEAL BREAKER": colors.HexColor("#b91c1c"),
}


def _clean(value: Any, limit: int = 4000) -> str:
    """Make text safe for the PDF's core fonts and for XML markup."""

    text = str(value if value is not None else "")
    # Core PDF fonts are latin-1; emoji and other astral characters would raise.
    text = text.encode("latin-1", "replace").decode("latin-1")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", parent=base["Title"], fontSize=20, spaceAfter=2, alignment=TA_LEFT
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"], fontSize=9, textColor=colors.HexColor("#475569")
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"], fontSize=12, spaceBefore=12, spaceAfter=4
        ),
        "body": ParagraphStyle("body", parent=base["Normal"], fontSize=9.5, leading=13),
        "small": ParagraphStyle(
            "small", parent=base["Normal"], fontSize=8, textColor=colors.HexColor("#475569")
        ),
        "cell": ParagraphStyle("cell", parent=base["Normal"], fontSize=8, leading=10),
    }


def _table(rows: list[list[Any]], widths: list[float], style_extra: list[tuple] | None = None) -> Table:
    table = Table(rows, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e2e8f0")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f8fafc")]),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
            + (style_extra or [])
        )
    )
    return table


def _findings_table(items: list[dict[str, Any]], styles: dict[str, ParagraphStyle]) -> Table | Paragraph:
    if not items:
        return Paragraph("No findings recorded.", styles["body"])

    rows: list[list[Any]] = [["Severity", "Category", "Finding", "Evidence", "Verified"]]
    severity_rows: list[tuple] = []
    for index, item in enumerate(items, start=1):
        severity = str(item.get("severity", "")).upper()
        rows.append(
            [
                Paragraph(_clean(severity), styles["cell"]),
                Paragraph(_clean(item.get("category"), 120), styles["cell"]),
                Paragraph(_clean(item.get("finding"), 600), styles["cell"]),
                Paragraph(_clean(item.get("evidentiary_quote"), 400), styles["cell"]),
                Paragraph("yes" if item.get("evidence_verified", True) else "NO", styles["cell"]),
            ]
        )
        if severity in SEVERITY_COLORS:
            severity_rows.append(("TEXTCOLOR", (0, index), (0, index), SEVERITY_COLORS[severity]))
            severity_rows.append(("FONTNAME", (0, index), (0, index), "Helvetica-Bold"))

    return _table(rows, [16 * mm, 30 * mm, 62 * mm, 52 * mm, 14 * mm], severity_rows)


def build_pdf(state: dict[str, Any]) -> bytes:
    """Render a finished run's state into a PDF document."""

    styles = _styles()
    report = state.get("red_flag_verdict") or {}
    governance = state.get("governance") or {}
    memory = state.get("memory") or {}
    facts = memory.get("current_facts") or {}
    accounts = state.get("accounts") or {}
    latest = (accounts.get("latest") or {}).get("analysis", {})
    derived = latest.get("derived", {})

    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"Red Flag Report {state.get('crn', '')}",
        author="DueDiligence Direct",
    )

    story: list[Any] = []
    company = facts.get("company_name") or state.get("crn", "Unknown company")
    story.append(Paragraph(_clean(company), styles["title"]))
    story.append(
        Paragraph(
            f"Company number {_clean(state.get('crn'))} &middot; Red Flag Report &middot; "
            f"generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            styles["subtitle"],
        )
    )
    story.append(Spacer(1, 8))

    verdict = str(report.get("recommendation", "UNKNOWN"))
    verdict_table = Table([[Paragraph(f"<b>{_clean(verdict)}</b>", styles["body"])]], colWidths=[174 * mm])
    verdict_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), VERDICT_COLORS.get(verdict, colors.HexColor("#475569"))),
                ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    story.append(verdict_table)
    story.append(Spacer(1, 10))

    story.append(Paragraph("Executive summary", styles["h2"]))
    story.append(Paragraph(_clean(report.get("executive_summary")), styles["body"]))

    top_risks = report.get("top_risks") or []
    if top_risks:
        story.append(Paragraph("Top risks", styles["h2"]))
        for risk in top_risks:
            story.append(Paragraph(f"&bull; {_clean(risk, 700)}", styles["body"]))

    review = report.get("required_human_review") or []
    if review:
        story.append(Paragraph("Requires human review", styles["h2"]))
        for item in review:
            story.append(Paragraph(f"&bull; {_clean(item, 700)}", styles["body"]))

    # --- statutory snapshot -------------------------------------------------
    story.append(Paragraph("Statutory position", styles["h2"]))
    snapshot = [
        ["Status", _clean(facts.get("company_status", "unknown"))],
        ["Registered charges", _clean(facts.get("charge_count", "-"))],
        ["Insolvency cases", _clean(facts.get("insolvency_cases", "-"))],
        ["Active officers", _clean(facts.get("active_officers", "-"))],
        ["Persons with significant control", _clean(facts.get("psc_count", "-"))],
        ["Accounts overdue", _clean(facts.get("accounts_overdue", "-"))],
        ["Accounts next due", _clean(facts.get("accounts_next_due", "-"))],
    ]
    story.append(_table([["Fact", "Value"]] + snapshot, [60 * mm, 114 * mm]))

    # --- filed accounts -----------------------------------------------------
    if derived:
        story.append(Paragraph("Filed accounts", styles["h2"]))
        story.append(
            Paragraph(
                f"Source: {_clean((accounts.get('latest') or {}).get('description', 'accounts filing'))}, "
                f"filed {_clean((accounts.get('latest') or {}).get('filing_date'))}, period ending "
                f"{_clean(derived.get('latest_period_end'))}. Figures are extracted from the filing's "
                f"iXBRL tags and computed deterministically.",
                styles["small"],
            )
        )
        story.append(Spacer(1, 4))
        rows = [["Metric", "Value"]]
        for label, key in (
            ("Net assets", "net_assets"),
            ("Net assets change (YoY)", "net_assets_change"),
            ("Current ratio", "current_ratio"),
            ("Cash runway (months)", "cash_runway_months"),
            ("Internally consistent", "internally_consistent"),
        ):
            if derived.get(key) is not None:
                rows.append([label, _clean(derived.get(key))])
        story.append(_table(rows, [60 * mm, 114 * mm]))
        if derived.get("internally_consistent") is False:
            story.append(Spacer(1, 3))
            story.append(
                Paragraph(
                    "This filing fails one or more balance sheet identity checks. Ratios that "
                    "depend on the affected figures are withheld and require manual verification.",
                    styles["small"],
                )
            )

    story.append(PageBreak())

    # --- evidence -----------------------------------------------------------
    story.append(Paragraph("Legal findings", styles["h2"]))
    story.append(_findings_table((state.get("legal_risks") or {}).get("risks", []), styles))

    story.append(Paragraph("Financial findings", styles["h2"]))
    story.append(_findings_table((state.get("financial_analysis") or {}).get("findings", []), styles))

    debate_points = (state.get("debate_transcript") or {}).get("points", [])
    if debate_points:
        story.append(Paragraph("Reconciliation", styles["h2"]))
        rows = [["Severity", "Issue", "Legal view", "Financial view", "Resolved position"]]
        for point in debate_points:
            rows.append(
                [
                    Paragraph(_clean(point.get("severity")), styles["cell"]),
                    Paragraph(_clean(point.get("issue"), 160), styles["cell"]),
                    Paragraph(_clean(point.get("legal_view"), 300), styles["cell"]),
                    Paragraph(_clean(point.get("financial_view"), 300), styles["cell"]),
                    Paragraph(_clean(point.get("resolved_position"), 300), styles["cell"]),
                ]
            )
        story.append(_table(rows, [16 * mm, 32 * mm, 42 * mm, 42 * mm, 42 * mm]))

    # --- governance ---------------------------------------------------------
    story.append(Paragraph("Governance record", styles["h2"]))
    usage = governance.get("token_usage") or {}
    rows = [
        ["Trace id", _clean(governance.get("trace_id"))],
        ["Analysis mode", _clean(governance.get("analysis_mode"))],
        ["Models used", _clean(", ".join(governance.get("models_used") or []))],
        ["Agent versions", _clean(
            ", ".join(f"{k}@{v}" for k, v in (governance.get("registry_versions") or {}).items())
        )],
        ["Documents quarantined", _clean(governance.get("documents_quarantined", 0))],
        ["Unverified citations", _clean(governance.get("unverified_citations", 0))],
        ["Output guardrail", _clean(governance.get("armor_verdict"))],
    ]
    if usage:
        rows.append(
            [
                "Tokens",
                _clean(
                    f"{usage.get('total_tokens', 0):,} total "
                    f"({usage.get('prompt_tokens', 0):,} prompt + {usage.get('output_tokens', 0):,} output) "
                    f"across {usage.get('calls', 0)} model call(s)"
                ),
            ]
        )
    story.append(_table([["Item", "Value"]] + rows, [45 * mm, 129 * mm]))

    chain = state.get("reasoning_chain") or []
    if chain:
        story.append(Paragraph("Agent reasoning chain", styles["h2"]))
        rows = [["#", "From", "To", "Type", "Message"]]
        for entry in chain:
            rows.append(
                [
                    Paragraph(_clean(entry.get("seq")), styles["cell"]),
                    Paragraph(_clean(entry.get("sender")), styles["cell"]),
                    Paragraph(_clean(entry.get("recipient")), styles["cell"]),
                    Paragraph(_clean(entry.get("kind")), styles["cell"]),
                    Paragraph(_clean(entry.get("message"), 400), styles["cell"]),
                ]
            )
        story.append(_table(rows, [8 * mm, 26 * mm, 26 * mm, 24 * mm, 90 * mm]))

    errors = state.get("ingestion_errors") or []
    if errors:
        story.append(Paragraph("Ingestion and governance events", styles["h2"]))
        for item in errors[:20]:
            story.append(Paragraph(f"&bull; {_clean(item, 300)}", styles["small"]))

    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#cbd5e1")))
    story.append(Spacer(1, 4))
    story.append(
        KeepTogether(
            Paragraph(
                _clean(
                    report.get("reliance_disclaimer")
                    or "AI-generated due diligence support only. Not legal, financial, tax, or "
                    "investment advice."
                ),
                styles["small"],
            )
        )
    )

    document.build(story)
    return buffer.getvalue()


def suggested_filename(state: dict[str, Any]) -> str:
    facts = (state.get("memory") or {}).get("current_facts") or {}
    name = re.sub(r"[^A-Za-z0-9]+", "-", str(facts.get("company_name") or state.get("crn", "report")))
    return f"red-flag-report-{name.strip('-').lower()}-{state.get('crn', '')}.pdf"
