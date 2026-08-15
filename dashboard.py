"""Fleet console for DueDiligence Direct.

The dashboard is a client of the Agent Runtime, not the workflow itself: it submits
a background job and then renders live stage events, governance decisions, the
audit trail, the registry, and cross-session memory while the fleet works.
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st
from pydantic import ValidationError

import agent_identity
import agent_registry
import data_room_loader
import gateway
import mcp_server
import memory_bank
import orchestrator
import runtime
import telemetry

st.set_page_config(page_title="DueDiligence Direct - Fleet Console", layout="wide")

DATA_ROOM_CHOICES = {
    "Sample data room (clean)": "sample_data_room",
    "Hostile data room (Model Armor demo)": "sample_data_room_hostile",
    "Local data_room/": "data_room",
}

SEVERITY_COLORS = {"HIGH": "#b91c1c", "MEDIUM": "#b45309", "LOW": "#2563eb", "CLEAR": "#15803d"}
RECOMMENDATION_COLORS = {
    "GREEN LIGHT": "#15803d",
    "PROCEED WITH CAUTION": "#b45309",
    "RED FLAG DEAL BREAKER": "#b91c1c",
}


@st.cache_resource
def bootstrap() -> dict:
    telemetry.configure_telemetry()
    fleet = orchestrator.bootstrap_fleet()
    runtime.reconcile_interrupted_jobs()
    return fleet


bootstrap()


def badge(text: str, color: str, size: int = 12) -> str:
    return (
        f"<span style='background:{color};color:white;border-radius:4px;"
        f"padding:3px 9px;font-size:{size}px;font-weight:600'>{text}</span>"
    )


def severity_badge(severity: str) -> str:
    severity = str(severity or "UNKNOWN").upper()
    return badge(severity, SEVERITY_COLORS.get(severity, "#475569"))


def evidence_rows(state: dict) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in state.get("legal_risks", {}).get("risks", []):
        rows.append(
            {
                "Agent": "Legal",
                "Category": item.get("category", ""),
                "Severity": item.get("severity", ""),
                "Verified": "yes" if item.get("evidence_verified", True) else "no",
                "Finding": item.get("finding", ""),
                "Evidence": item.get("evidentiary_quote", ""),
            }
        )
    for item in state.get("financial_analysis", {}).get("findings", []):
        rows.append(
            {
                "Agent": "Financial",
                "Category": item.get("category", ""),
                "Severity": item.get("severity", ""),
                "Verified": "yes" if item.get("evidence_verified", True) else "no",
                "Finding": item.get("finding", ""),
                "Evidence": item.get("evidentiary_quote", ""),
            }
        )
    for item in state.get("debate_transcript", {}).get("points", []):
        rows.append(
            {
                "Agent": "Debate",
                "Category": item.get("issue", ""),
                "Severity": item.get("severity", ""),
                "Verified": "-",
                "Finding": item.get("resolved_position", ""),
                "Evidence": f"Legal: {item.get('legal_view', '')} | Financial: {item.get('financial_view', '')}",
            }
        )
    return rows


def severity_counts(state: dict) -> dict[str, int]:
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "CLEAR": 0}
    for row in evidence_rows(state):
        severity = row.get("Severity", "").upper()
        if severity in counts:
            counts[severity] += 1
    return counts


def render_findings(items: list[dict], quote_key: str, title_key: str) -> None:
    for item in items:
        with st.container(border=True):
            verified = item.get("evidence_verified", True)
            suffix = "" if verified else "  " + badge("UNVERIFIED CITATION", "#7c2d12", 11)
            st.markdown(
                f"{severity_badge(item.get('severity'))} **{item.get(title_key, '')}**{suffix}",
                unsafe_allow_html=True,
            )
            st.write(item.get("finding", item.get("resolved_position", "")))
            st.caption(item.get(quote_key, ""))
            if item.get("evidence_note"):
                st.caption(f"Grounding audit: {item['evidence_note']}")


def render_accounts(accounts: dict) -> None:
    """Show the figures taken from the company's own iXBRL filing."""

    if not accounts:
        st.info("No filed accounts were ingested for this run.")
        return
    if accounts.get("status") != "success":
        st.warning(
            f"Filed accounts could not be analyzed deterministically (status: {accounts.get('status')}). "
            f"{accounts.get('message', '')}"
        )

    latest = accounts.get("latest") or {}
    analysis = latest.get("analysis", {})
    derived = analysis.get("derived", {})

    if analysis:
        st.caption(
            f"Source: {latest.get('description', 'accounts filing')} filed {latest.get('filing_date')} "
            f"for period ending {derived.get('latest_period_end')} - "
            f"{analysis.get('fact_count', 0)} tagged figures parsed from iXBRL."
        )
        if latest.get("document_url"):
            st.caption(f"Document: {latest['document_url']}")

        headline = st.columns(4)
        net_assets = derived.get("net_assets")
        headline[0].metric(
            "Net assets",
            f"{net_assets:,.0f}" if isinstance(net_assets, (int, float)) else "not tagged",
            (
                f"{derived['net_assets_change']:+,.0f} YoY"
                if isinstance(derived.get("net_assets_change"), (int, float))
                else None
            ),
        )
        ratio = derived.get("current_ratio")
        headline[1].metric(
            "Current ratio",
            f"{ratio:.2f}" if isinstance(ratio, (int, float)) else "not derivable",
            "unreliable" if derived.get("current_ratio_reliable") is False else None,
        )
        runway = derived.get("cash_runway_months")
        headline[2].metric(
            "Cash runway",
            f"{runway:.1f} months" if isinstance(runway, (int, float)) else "not derivable",
        )
        headline[3].metric(
            "Internally consistent",
            "yes" if derived.get("internally_consistent", True) else "no",
        )

        st.markdown("#### Balance sheet by period")
        rows = []
        for period in analysis.get("periods", []):
            row = {"period_end": period["period_end"]}
            row.update(
                {
                    key: f"{value:,.0f}" if isinstance(value, (int, float)) else value
                    for key, value in period["metrics"].items()
                }
            )
            rows.append(row)
        st.dataframe(rows, hide_index=True, width="stretch")

        st.markdown("#### Accounting identity checks")
        st.caption(
            "Small-company filings are self-tagged. Ratios are only published when the "
            "filing's own figures reconcile."
        )
        st.dataframe(
            analysis.get("periods", [{}])[0].get("reconciliation", []),
            hide_index=True,
            width="stretch",
        )

        signals = analysis.get("signals", [])
        if signals:
            st.markdown("#### Deterministic signals")
            for signal in signals:
                with st.container(border=True):
                    st.markdown(
                        f"{severity_badge(signal['severity'])} **{signal['code']}**",
                        unsafe_allow_html=True,
                    )
                    st.write(signal["detail"])
                    st.caption(signal["evidence"])

        with st.expander("Tag-level evidence"):
            st.json(
                {
                    period["period_end"]: period.get("evidence", {})
                    for period in analysis.get("periods", [])
                }
            )

    st.markdown("#### Filings examined")
    st.dataframe(
        [
            {
                "filing_date": filing.get("filing_date"),
                "description": filing.get("description"),
                "status": filing.get("status"),
                "formats": ", ".join(filing.get("available_formats", []) or []),
                "period_end": (filing.get("analysis", {}).get("derived", {}) or {}).get("latest_period_end"),
            }
            for filing in accounts.get("filings", [])
        ]
        or [{"filing_date": "-", "description": "none", "status": "-", "formats": "-", "period_end": "-"}],
        hide_index=True,
        width="stretch",
    )


def render_report(state: dict) -> None:
    report = state.get("red_flag_verdict", {})
    governance = state.get("governance", {})
    counts = severity_counts(state)

    st.markdown(
        badge(report.get("recommendation", "UNKNOWN"), RECOMMENDATION_COLORS.get(report.get("recommendation", ""), "#475569"), 15),
        unsafe_allow_html=True,
    )
    st.subheader("Executive report")
    st.write(report.get("executive_summary", ""))

    columns = st.columns(6)
    columns[0].metric("High", counts["HIGH"])
    columns[1].metric("Medium", counts["MEDIUM"])
    columns[2].metric("Low", counts["LOW"])
    columns[3].metric("Clear", counts["CLEAR"])
    columns[4].metric("Docs quarantined", governance.get("documents_quarantined", 0))
    columns[5].metric("Unverified citations", governance.get("unverified_citations", 0))

    tabs = st.tabs(
        [
            "Report",
            "Filed accounts",
            "Governance",
            "Evidence",
            "Legal",
            "Financial",
            "Debate",
            "Data room",
            "Memory",
        ]
    )

    with tabs[0]:
        st.markdown("#### Top risks")
        for risk in report.get("top_risks", []):
            st.write(f"- {risk}")
        st.markdown("#### Requires human review")
        for item in report.get("required_human_review", []):
            st.write(f"- {item}")
        st.caption(report.get("reliance_disclaimer", ""))
        if state.get("artifact_path"):
            st.code(state["artifact_path"])

    with tabs[1]:
        render_accounts(state.get("accounts", {}))

    with tabs[2]:
        left, right = st.columns(2)
        left.markdown("**Execution identity and models**")
        left.json(
            {
                "trace_id": governance.get("trace_id"),
                "models_used": governance.get("models_used"),
                "model_errors": governance.get("model_errors"),
                "registry_versions": governance.get("registry_versions"),
            }
        )
        right.markdown("**Guardrails**")
        right.json(
            {
                "output_armor_verdict": governance.get("armor_verdict"),
                "output_armor_violations": governance.get("armor_violations"),
                "documents_quarantined": governance.get("documents_quarantined"),
                "unverified_citations": governance.get("unverified_citations"),
                "memory_written": governance.get("memory_written"),
            }
        )
        armor_findings = state.get("data_room", {}).get("armor_findings", [])
        if armor_findings:
            st.markdown("**Model Armor input findings**")
            st.dataframe(armor_findings, hide_index=True, width="stretch")
        if state.get("ingestion_errors"):
            st.markdown("**Ingestion and governance events**")
            for item in state["ingestion_errors"]:
                st.write(f"- {item}")
        trace_id = governance.get("trace_id")
        if trace_id:
            st.markdown("**Audit records for this run**")
            records = telemetry.read_audit(limit=500, trace_id=trace_id)
            st.dataframe(
                [
                    {
                        "time": record["timestamp"],
                        "action": record["action"],
                        "actor": record["actor"],
                        "resource": record["resource"],
                        "decision": record["decision"],
                    }
                    for record in records
                ],
                hide_index=True,
                width="stretch",
            )

    with tabs[3]:
        rows = evidence_rows(state)
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch")
        else:
            st.info("No evidence rows were produced.")
        with st.expander("Raw Companies House payload"):
            st.json(state.get("raw_statutory_data", {}))

    with tabs[4]:
        render_findings(state.get("legal_risks", {}).get("risks", []), "evidentiary_quote", "category")
        for limitation in state.get("legal_risks", {}).get("limitations", []):
            st.caption(f"Limitation: {limitation}")

    with tabs[5]:
        render_findings(
            state.get("financial_analysis", {}).get("findings", []), "evidentiary_quote", "category"
        )
        for limitation in state.get("financial_analysis", {}).get("limitations", []):
            st.caption(f"Limitation: {limitation}")

    with tabs[6]:
        st.write(state.get("debate_transcript", {}).get("risk_reward_summary", ""))
        for point in state.get("debate_transcript", {}).get("points", []):
            with st.container(border=True):
                st.markdown(
                    f"{severity_badge(point.get('severity'))} **{point.get('issue', '')}**",
                    unsafe_allow_html=True,
                )
                st.write(point.get("resolved_position", ""))
                st.caption(f"Legal: {point.get('legal_view', '')}")
                st.caption(f"Financial: {point.get('financial_view', '')}")

    with tabs[7]:
        documents = state.get("data_room", {}).get("documents", [])
        if not documents:
            st.info(state.get("data_room", {}).get("message", "No data room documents were loaded."))
        for document in documents:
            with st.container(border=True):
                header, meta = st.columns([2, 1])
                quarantined = document.get("quarantined")
                header.markdown(
                    f"**{document.get('file_name', 'Unknown file')}** "
                    + (badge("QUARANTINED", "#b91c1c", 11) if quarantined else ""),
                    unsafe_allow_html=True,
                )
                header.caption(document.get("path", ""))
                meta.metric("Classification", str(document.get("classification", "unknown")).title())
                st.text_area(
                    "Text passed to agents (post-armor)",
                    value=document.get("text_excerpt", "")[:1600],
                    height=140,
                    disabled=True,
                    key=f"excerpt-{document.get('path', document.get('file_name', 'doc'))}",
                )
                if document.get("armor_redactions"):
                    st.caption(f"Redacted: {', '.join(document['armor_redactions'])}")

    with tabs[8]:
        memory = state.get("memory", {})
        if memory.get("is_first_audit"):
            st.info("This was the first audit of this company; a baseline has now been stored.")
        st.markdown("**Tracked facts this run**")
        st.json(memory.get("current_facts", {}))
        changes = memory.get("changes_since_last_audit", [])
        if changes:
            st.markdown("**Changes since the previous audit**")
            st.dataframe(changes, hide_index=True, width="stretch")
        st.markdown("**Prior audits**")
        st.dataframe(
            [
                {
                    "created_at": entry["created_at"],
                    "recommendation": entry["recommendation"],
                    "trace_id": entry["trace_id"],
                }
                for entry in memory.get("prior_audits", [])
            ]
            or [{"created_at": "-", "recommendation": "none", "trace_id": "-"}],
            hide_index=True,
            width="stretch",
        )


@st.fragment(run_every=2)
def render_live_job(job_id: str) -> None:
    job = runtime.get_job(job_id)
    if job is None:
        st.error(f"Job {job_id} not found.")
        return

    header, control = st.columns([4, 1])
    header.markdown(
        f"**Job {job['job_id']}** for CRN {job['crn']} - status {job['status']}",
    )
    if job["status"] not in runtime.TERMINAL_STATUSES:
        if control.button("Cancel job", key=f"cancel-{job_id}"):
            runtime.cancel_job(job_id)
        st.progress(min(len(job["events"]) / 10, 0.95), text="Fleet working in the background")

    for event in job["events"]:
        st.write(f"`{event['timestamp']}` **{event['stage']}** - {event['message']}")

    if job["status"] == runtime.STATUS_SUCCEEDED and job["result"]:
        st.success("Audit complete")
        render_report(job["result"])
    elif job["status"] == runtime.STATUS_FAILED:
        st.error(job["error"] or "Job failed")
    elif job["status"] == runtime.STATUS_CANCELLED:
        st.warning("Job cancelled by operator")
    elif job["status"] == runtime.STATUS_INTERRUPTED:
        st.warning("Job was interrupted by a process restart")


st.title("DueDiligence Direct")
st.caption(
    "Governed multi-agent M&A diligence fleet - Companies House statutory data, "
    "Gemini reasoning, and enterprise controls on every call."
)

with st.sidebar:
    st.subheader("Run an audit")
    crn = st.text_input("Company number", value="03994971", max_chars=8)
    data_room_label = st.selectbox("Data room", list(DATA_ROOM_CHOICES))
    uploaded_files = st.file_uploader(
        "Or upload deal documents", type=["csv", "md", "pdf", "txt"], accept_multiple_files=True
    )
    run_clicked = st.button("Submit to Agent Runtime", type="primary", width="stretch")

    st.divider()
    st.subheader("Fleet")
    stats = runtime.fleet_stats()
    st.metric("Jobs in flight", stats["in_flight"])
    st.metric("Jobs recorded", stats["total"])
    st.caption(f"Runtime workers: {stats['workers']}")

if run_clicked:
    try:
        query = mcp_server.CompanyQuery(crn=crn)
    except ValidationError as exc:
        st.error(exc.errors()[0]["msg"])
        st.stop()

    data_room_path = DATA_ROOM_CHOICES[data_room_label]
    if uploaded_files:
        saved = data_room_loader.save_uploaded_files(uploaded_files)
        data_room_path = "data_room/uploads"
        st.toast(f"Saved {len(saved)} uploaded file(s)")

    st.session_state["job_id"] = runtime.submit_job(
        query.crn, data_room_path=data_room_path, submitted_by="dashboard"
    )

console, fleet_tab, audit_tab, memory_tab = st.tabs(
    ["Live audit", "Agent registry", "Audit trail", "Memory bank"]
)

with console:
    active_job = st.session_state.get("job_id")
    if active_job:
        render_live_job(active_job)
    else:
        st.info("Submit an audit from the sidebar. The run executes in the background runtime.")
        recent = runtime.list_jobs(limit=10)
        if recent:
            st.markdown("**Recent jobs**")
            st.dataframe(
                [
                    {
                        "job_id": job["job_id"],
                        "crn": job["crn"],
                        "status": job["status"],
                        "created_at": job["created_at"],
                    }
                    for job in recent
                ],
                hide_index=True,
                width="stretch",
            )

with fleet_tab:
    st.markdown("#### Published agent cards")
    st.dataframe(
        [
            {
                "agent": card["agent_id"],
                "version": card["version"],
                "status": card["status"],
                "capabilities": ", ".join(card["capabilities"]),
                "tools": ", ".join(card["tools"]),
                "model": card["model"],
            }
            for card in agent_registry.list_agents(include_retired=True)
        ],
        hide_index=True,
        width="stretch",
    )
    left, right = st.columns(2)
    left.markdown("#### Agent identities and scopes")
    left.dataframe(
        [
            {
                "principal": identity["principal"],
                "scopes": ", ".join(identity["scopes"]),
                "service_account": identity["service_account"],
            }
            for identity in agent_identity.fleet_roster()
        ],
        hide_index=True,
        width="stretch",
    )
    right.markdown("#### Gateway tool policies")
    right.dataframe(gateway.registered_tools(), hide_index=True, width="stretch")
    right.caption(f"Allowed egress hosts: {', '.join(gateway.allowed_hosts())}")

with audit_tab:
    verification = telemetry.verify_audit_chain()
    if verification["valid"]:
        st.success(
            f"Audit chain verified across {verification['records']} record(s). "
            f"Head hash {verification.get('head_hash', '')[:16]}..."
        )
    else:
        st.error(f"Audit chain broken at record {verification.get('broken_at')}")

    records = telemetry.read_audit(limit=300)
    st.dataframe(
        [
            {
                "time": record["timestamp"],
                "severity": record["severity_text"],
                "action": record["action"],
                "actor": record["actor"],
                "resource": record["resource"],
                "decision": record["decision"],
                "trace_id": record["trace_id"][:12],
            }
            for record in reversed(records)
        ],
        hide_index=True,
        width="stretch",
        height=420,
    )
    span_log = Path(telemetry.SPAN_LOG_PATH)
    if span_log.exists():
        with st.expander("Latest OpenTelemetry spans"):
            lines = span_log.read_text(encoding="utf-8").strip().splitlines()[-25:]
            st.json([json.loads(line) for line in lines if line.strip()])

with memory_tab:
    lookup_crn = st.text_input("Company number to recall", value=crn, max_chars=8, key="memory-crn")
    if lookup_crn:
        memory = memory_bank.recall(lookup_crn, actor="dashboard")
        st.markdown("#### Prior audits")
        st.dataframe(
            [
                {
                    "created_at": entry["created_at"],
                    "recommendation": entry["recommendation"],
                    "high": entry["severity_counts"].get("HIGH", 0),
                    "medium": entry["severity_counts"].get("MEDIUM", 0),
                    "trace_id": entry["trace_id"][:12],
                }
                for entry in memory["prior_audits"]
            ]
            or [{"created_at": "-", "recommendation": "no history", "high": 0, "medium": 0, "trace_id": "-"}],
            hide_index=True,
            width="stretch",
        )
        st.markdown("#### Operator notes")
        for note in memory["operator_notes"]:
            st.write(f"`{note['created_at']}` **{note['author']}**: {note['note']}")
        new_note = st.text_area("Add an operator note (persists across sessions)", key="memory-note")
        if st.button("Save note") and new_note.strip():
            memory_bank.add_note(lookup_crn, new_note.strip(), author="dashboard")
            st.rerun()
