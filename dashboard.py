"""Fleet console for DueDiligence Direct.

The dashboard is a client of the Agent Runtime, not the workflow itself: it submits
a background job and then renders live stage events, governance decisions, the
audit trail, the registry, and cross-session memory while the fleet works.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import streamlit as st
from pydantic import ValidationError

import fleet_client
import mcp_server

st.set_page_config(page_title="DueDiligence Direct - Fleet Console", layout="wide")


def _require_access_code() -> None:
    """Gate the console when it is published publicly.

    Cloud Run IAM is the right control for machine callers, but a browser cannot
    present an identity token. When FLEET_CONSOLE_ACCESS_CODE is set the console
    asks for it before rendering anything; unset, the console is open, which is
    what you want on a laptop. This bounds who can spend model quota, and the
    gateway's per-agent quota bounds how much any one session can spend.
    """

    expected = os.getenv("FLEET_CONSOLE_ACCESS_CODE", "").strip()
    if not expected or st.session_state.get("access_granted"):
        return

    st.title("DueDiligence Direct")
    st.caption("Governed multi-agent M&A diligence fleet. Enter the access code to continue.")
    with st.form("access"):
        supplied = st.text_input("Access code", type="password")
        if st.form_submit_button("Enter", type="primary"):
            if supplied == expected:
                st.session_state["access_granted"] = True
                st.rerun()
            else:
                st.error("That code is not recognised.")
    st.stop()


_require_access_code()

SEVERITY_COLORS = {"HIGH": "#b91c1c", "MEDIUM": "#b45309", "LOW": "#2563eb", "CLEAR": "#15803d"}
RECOMMENDATION_COLORS = {
    "GREEN LIGHT": "#15803d",
    "PROCEED WITH CAUTION": "#b45309",
    "RED FLAG DEAL BREAKER": "#b91c1c",
}


# Terminal job states, mirrored here so the console needs no runtime import.
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}


@st.cache_resource
def get_backend(api_url: str, fingerprint: float):
    """Connect to a control plane, or run the fleet locally when none is set.

    The backend is cached because building a local one publishes agent cards and
    reconciles jobs, and the live panel reruns every two seconds. `fingerprint` is
    the client module's mtime, so editing the client invalidates the cache instead
    of leaving a stale object with missing methods behind.
    """

    return fleet_client.get_backend(api_url)


def _client_fingerprint() -> float:
    try:
        return os.path.getmtime(fleet_client.__file__)
    except OSError:
        return 0.0


backend = get_backend(fleet_client.FLEET_API_URL, _client_fingerprint())


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


AGENT_AVATARS = {
    "orchestrator": "🧭",
    "legal_risk": "⚖️",
    "financial_auditor": "📊",
    "debate": "⚔️",
    "synthesizer": "📝",
    "memory_bank": "🧠",
    "operator": "👤",
}

KIND_LABELS = {
    "task_assignment": "assigns work to",
    "finding_report": "reports to",
    "challenge": "challenges",
    "rebuttal": "rebuts to",
    "resolution": "resolves for",
    "context_recall": "recalls for",
    "verdict": "delivers verdict to",
}


def render_conversation(entries: list[dict]) -> None:
    """Render the inter-agent reasoning chain as a conversation."""

    if not entries:
        st.info(
            "The agent conversation appears here: task assignments, findings, challenges, "
            "rebuttals, and the final verdict."
        )
        return

    for entry in entries:
        sender = entry.get("sender", "unknown")
        recipient = entry.get("recipient", "unknown")
        kind = entry.get("kind", "message")
        with st.chat_message(sender, avatar=AGENT_AVATARS.get(sender, "🤖")):
            st.markdown(
                f"**{sender}** {KIND_LABELS.get(kind, 'messages')} **{recipient}** "
                f"{badge(kind.replace('_', ' '), '#475569', 11)}",
                unsafe_allow_html=True,
            )
            st.write(entry.get("message", ""))
            attributes = {
                key: value
                for key, value in (entry.get("attributes") or {}).items()
                if value not in (None, "", [])
            }
            details = []
            if attributes.get("model"):
                details.append(f"model {attributes['model']}")
            if attributes.get("latency_ms"):
                details.append(f"{attributes['latency_ms']} ms")
            if attributes.get("prompt_tokens") or attributes.get("output_tokens"):
                details.append(
                    f"{attributes.get('prompt_tokens', 0):,} in / "
                    f"{attributes.get('output_tokens', 0):,} out tokens"
                )
            if attributes.get("findings") is not None:
                details.append(f"{attributes['findings']} finding(s)")
            if attributes.get("unverified_citations"):
                details.append(f"{attributes['unverified_citations']} unverified citation(s)")
            if details:
                st.caption(" · ".join(details))


STATUS_COLORS = {
    "active": "#15803d",
    "dissolved": "#475569",
    "liquidation": "#b91c1c",
    "administration": "#b45309",
    "receivership": "#b45309",
    "converted-closed": "#475569",
}


@st.dialog("Find a company", width="large")
def company_search_dialog() -> None:
    """Search the register by name and pick the company to audit."""

    st.caption(
        "Search UK Companies House by name. Selecting a result fills in its company "
        "number, so you never need to know the number in advance."
    )
    query = st.text_input(
        "Company name", value=st.session_state.get("last_search", ""), placeholder="e.g. Monzo Bank"
    )
    limit = st.slider("Results", min_value=5, max_value=30, value=10, step=5)

    if st.button("Search", type="primary") and query.strip():
        st.session_state["last_search"] = query
        try:
            st.session_state["search_results"] = backend.search_companies(query, limit)
        except Exception as exc:
            st.session_state["search_results"] = {"status": "error", "message": str(exc), "results": []}

    results = st.session_state.get("search_results") or {}
    if results.get("status") == "success":
        found = results.get("results", [])
        st.caption(
            f"{results.get('total_results', len(found))} match(es) on the register; showing {len(found)}."
        )
        for item in found:
            number = item.get("company_number", "")
            with st.container(border=True):
                left, right = st.columns([4, 1])
                status = str(item.get("company_status", "unknown"))
                left.markdown(
                    f"**{item.get('title', 'Unknown')}** "
                    f"{badge(status.upper(), STATUS_COLORS.get(status, '#475569'), 11)}",
                    unsafe_allow_html=True,
                )
                left.caption(
                    f"{number} · incorporated {item.get('date_of_creation', 'unknown')} · "
                    f"{item.get('company_type', '')}"
                )
                left.caption(item.get("address_snippet", ""))
                if right.button("Audit this", key=f"pick-{number}"):
                    st.session_state["selected_crn"] = number
                    st.session_state["selected_name"] = item.get("title", "")
                    st.rerun()
    elif results:
        st.error(results.get("message") or f"Search failed ({results.get('status')}).")


def _payload(bundle: dict, key: str) -> dict:
    record = bundle.get(key, {}) or {}
    data = record.get("data", {}) if isinstance(record, dict) else {}
    return data if isinstance(data, dict) else {}


def _address(address: dict | None) -> str:
    if not isinstance(address, dict):
        return ""
    parts = [
        address.get("premises"),
        address.get("address_line_1"),
        address.get("address_line_2"),
        address.get("locality"),
        address.get("region"),
        address.get("postal_code"),
        address.get("country"),
    ]
    return ", ".join(str(part) for part in parts if part)


def render_company(bundle: dict) -> None:
    """Everything the register holds: profile, officers, PSCs, charges, filings."""

    if not bundle:
        st.info("No statutory records were retrieved for this run.")
        return

    profile = _payload(bundle, "profile")
    accounts = profile.get("accounts", {}) if isinstance(profile.get("accounts"), dict) else {}
    confirmation = (
        profile.get("confirmation_statement", {})
        if isinstance(profile.get("confirmation_statement"), dict)
        else {}
    )

    st.markdown(f"### {profile.get('company_name', 'Unknown company')}")
    top = st.columns(4)
    top[0].metric("Company number", profile.get("company_number", "-"))
    top[1].metric("Status", str(profile.get("company_status", "unknown")).title())
    top[2].metric("Incorporated", profile.get("date_of_creation", "-"))
    top[3].metric("Type", str(profile.get("type", "-")).upper())

    left, right = st.columns(2)
    with left:
        st.markdown("**Registered office**")
        st.write(_address(profile.get("registered_office_address")) or "not recorded")
        if profile.get("registered_office_is_in_dispute"):
            st.warning("Registered office address is in dispute.")
        st.markdown("**SIC codes**")
        st.write(", ".join(profile.get("sic_codes", []) or []) or "not recorded")
        st.markdown("**Jurisdiction**")
        st.write(profile.get("jurisdiction", "-"))
    with right:
        st.markdown("**Accounts**")
        st.write(
            f"Next due: {accounts.get('next_due', '-')} · Overdue: {accounts.get('overdue', '-')} · "
            f"Last made up to: {(accounts.get('last_accounts') or {}).get('made_up_to', '-')}"
        )
        st.markdown("**Confirmation statement**")
        st.write(
            f"Next due: {confirmation.get('next_due', '-')} · Overdue: {confirmation.get('overdue', '-')}"
        )
        st.markdown("**Register flags**")
        st.write(
            f"has_charges={profile.get('has_charges', False)} · "
            f"has_insolvency_history={profile.get('has_insolvency_history', False)} · "
            f"has_been_liquidated={profile.get('has_been_liquidated', False)}"
        )

    previous_names = profile.get("previous_company_names") or []
    st.markdown("#### Previous company names")
    if previous_names:
        st.dataframe(
            [
                {
                    "name": entry.get("name"),
                    "effective_from": entry.get("effective_from"),
                    "ceased_on": entry.get("ceased_on"),
                }
                for entry in previous_names
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("The company has never traded under a different registered name.")

    officers = _payload(bundle, "officers")
    officer_items = officers.get("items", []) or []
    st.markdown(
        f"#### Officers ({officers.get('active_count', 0)} active, "
        f"{officers.get('resigned_count', 0)} resigned)"
    )
    if officer_items:
        st.dataframe(
            [
                {
                    "name": item.get("name"),
                    "role": item.get("officer_role"),
                    "status": "resigned" if item.get("resigned_on") else "active",
                    "appointed_on": item.get("appointed_on"),
                    "resigned_on": item.get("resigned_on", ""),
                    "nationality": item.get("nationality", ""),
                    "occupation": item.get("occupation", ""),
                    "country_of_residence": item.get("country_of_residence", ""),
                    "born": (
                        f"{(item.get('date_of_birth') or {}).get('month', '')}/"
                        f"{(item.get('date_of_birth') or {}).get('year', '')}"
                        if item.get("date_of_birth")
                        else ""
                    ),
                }
                for item in officer_items
                if isinstance(item, dict)
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption(f"No officer records returned (endpoint status: {bundle.get('officers', {}).get('status')}).")

    pscs = _payload(bundle, "pscs")
    psc_items = pscs.get("items", []) or []
    st.markdown(f"#### Persons with significant control ({len(psc_items)})")
    if psc_items:
        st.dataframe(
            [
                {
                    "name": item.get("name"),
                    "kind": item.get("kind"),
                    "notified_on": item.get("notified_on"),
                    "ceased_on": item.get("ceased_on", ""),
                    "nature_of_control": "; ".join(item.get("natures_of_control", []) or []),
                    "nationality": item.get("nationality", ""),
                    "country_of_residence": item.get("country_of_residence", ""),
                }
                for item in psc_items
                if isinstance(item, dict)
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption(
            f"No PSC records returned (endpoint status: {bundle.get('pscs', {}).get('status')}). "
            "A company with no identified PSC is itself a KYB question."
        )

    charges = _payload(bundle, "charges")
    charge_items = charges.get("items", []) or []
    st.markdown(
        f"#### Charges and mortgages ({charges.get('total_count', len(charge_items))} total, "
        f"{charges.get('unfiltered_count', '')} unfiltered)".replace(", unfiltered", "")
    )
    if charge_items:
        st.dataframe(
            [
                {
                    "charge_code": item.get("charge_code") or item.get("id"),
                    "status": item.get("status"),
                    "created_on": item.get("created_on"),
                    "delivered_on": item.get("delivered_on"),
                    "satisfied_on": item.get("satisfied_on", ""),
                    "classification": (item.get("classification") or {}).get("description", ""),
                    "persons_entitled": "; ".join(
                        entry.get("name", "") for entry in (item.get("persons_entitled") or [])
                    ),
                }
                for item in charge_items
                if isinstance(item, dict)
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("No registered charges, debentures, or mortgages.")

    insolvency = _payload(bundle, "insolvency")
    cases = insolvency.get("cases", []) or []
    st.markdown(f"#### Insolvency ({len(cases)} case(s))")
    if cases:
        st.dataframe(
            [
                {
                    "type": case.get("type"),
                    "number": case.get("number", ""),
                    "dates": "; ".join(
                        f"{d.get('type')}={d.get('date')}" for d in (case.get("dates") or [])
                    ),
                    "practitioners": "; ".join(
                        p.get("name", "") for p in (case.get("practitioners") or [])
                    ),
                }
                for case in cases
                if isinstance(case, dict)
            ],
            hide_index=True,
            width="stretch",
        )
    else:
        st.caption("No insolvency cases on the register.")

    filings = _payload(bundle, "filings")
    filing_items = filings.get("items", []) or []
    st.markdown(f"#### Recent filings ({len(filing_items)})")
    if filing_items:
        st.dataframe(
            [
                {
                    "date": item.get("date"),
                    "type": item.get("type"),
                    "category": item.get("category"),
                    "description": item.get("description"),
                }
                for item in filing_items
                if isinstance(item, dict)
            ],
            hide_index=True,
            width="stretch",
        )

    with st.expander("Raw Companies House payload (every endpoint)"):
        st.json(bundle)


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

    verdict_column, pdf_column, json_column = st.columns([3, 1, 1])
    verdict_column.markdown(
        badge(report.get("recommendation", "UNKNOWN"), RECOMMENDATION_COLORS.get(report.get("recommendation", ""), "#475569"), 15),
        unsafe_allow_html=True,
    )

    # Export lives beside the verdict: the report is the thing people came for.
    try:
        import report_export

        pdf_column.download_button(
            "⬇ PDF report",
            data=report_export.build_pdf(state),
            file_name=report_export.suggested_filename(state),
            mime="application/pdf",
            width="stretch",
            type="primary",
        )
    except Exception as exc:
        pdf_column.error(f"PDF unavailable: {exc}")

    json_column.download_button(
        "⬇ Full run (JSON)",
        data=json.dumps(state, indent=2, default=str),
        file_name=f"run-{state.get('crn', 'audit')}-{state.get('run_id', '')}.json",
        mime="application/json",
        width="stretch",
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
            "Company",
            "Agent conversation",
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
        render_company(state.get("raw_statutory_data", {}))

    with tabs[2]:
        st.caption(
            "Every message passed between agents during this run, in order. Each entry is also "
            "an event on the OpenTelemetry span for this trace."
        )
        render_conversation(state.get("reasoning_chain", []))

    with tabs[3]:
        render_accounts(state.get("accounts", {}))

    with tabs[4]:
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
        usage = governance.get("token_usage") or {}
        if usage:
            st.markdown("**Model usage**")
            usage_columns = st.columns(4)
            usage_columns[0].metric("Model calls", usage.get("calls", 0))
            usage_columns[1].metric("Prompt tokens", f"{usage.get('prompt_tokens', 0):,}")
            usage_columns[2].metric("Output tokens", f"{usage.get('output_tokens', 0):,}")
            usage_columns[3].metric("Total tokens", f"{usage.get('total_tokens', 0):,}")
            by_call = usage.get("by_call") or []
            if by_call:
                st.dataframe(
                    [
                        {
                            "call": call.get("schema"),
                            "model": call.get("model"),
                            "prompt_tokens": call.get("prompt_tokens"),
                            "output_tokens": call.get("output_tokens"),
                            "total_tokens": call.get("prompt_tokens", 0) + call.get("output_tokens", 0),
                            "latency_ms": call.get("latency_ms"),
                        }
                        for call in by_call
                    ],
                    hide_index=True,
                    width="stretch",
                )
                st.caption(
                    f"Total model latency {usage.get('total_model_latency_ms', 0):,} ms across "
                    f"{usage.get('calls', 0)} call(s)."
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
            records = backend.audit(limit=500, trace_id=trace_id)
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

    with tabs[5]:
        rows = evidence_rows(state)
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch")
        else:
            st.info("No evidence rows were produced.")
        with st.expander("Raw Companies House payload"):
            st.json(state.get("raw_statutory_data", {}))

    with tabs[6]:
        render_findings(state.get("legal_risks", {}).get("risks", []), "evidentiary_quote", "category")
        for limitation in state.get("legal_risks", {}).get("limitations", []):
            st.caption(f"Limitation: {limitation}")

    with tabs[7]:
        render_findings(
            state.get("financial_analysis", {}).get("findings", []), "evidentiary_quote", "category"
        )
        for limitation in state.get("financial_analysis", {}).get("limitations", []):
            st.caption(f"Limitation: {limitation}")

    with tabs[8]:
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

    with tabs[9]:
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

    with tabs[10]:
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
    job = backend.get_job(job_id)
    if job is None:
        st.error(f"Job {job_id} not found.")
        return

    header, control = st.columns([4, 1])
    header.markdown(
        f"**Job {job['job_id']}** for CRN {job['crn']} - status {job['status']}",
    )
    if job["status"] not in TERMINAL_STATUSES:
        if control.button("Cancel job", key=f"cancel-{job_id}"):
            backend.cancel_job(job_id)
        st.progress(min(len(job["events"]) / 16, 0.95), text="Fleet working in the background")

    # Stage events say what the fleet is doing; exchanges say what the agents are
    # saying to each other. Both stream live, so keep them side by side.
    events = job["events"]
    exchanges = [
        {
            "sender": (event.get("attributes") or {}).get("sender", "agent"),
            "recipient": (event.get("attributes") or {}).get("recipient", "agent"),
            "kind": (event.get("attributes") or {}).get("kind", "message"),
            "message": event.get("message", ""),
            "attributes": event.get("attributes") or {},
        }
        for event in events
        if (event.get("attributes") or {}).get("exchange")
    ]

    stage_column, chat_column = st.columns([1, 1])
    with stage_column:
        st.markdown("**Pipeline stages**")
        for event in events:
            if (event.get("attributes") or {}).get("exchange"):
                continue
            st.write(f"`{event['timestamp'][11:19]}` **{event['stage']}** - {event['message']}")
    with chat_column:
        st.markdown("**Agent conversation**")
        render_conversation(exchanges)

    # Announce the transition once, the first time this client sees it finish.
    seen = st.session_state.setdefault("announced_jobs", {})
    if job["status"] in TERMINAL_STATUSES and seen.get(job_id) != job["status"]:
        seen[job_id] = job["status"]
        if job["status"] == "SUCCEEDED":
            verdict = ((job.get("result") or {}).get("red_flag_verdict") or {}).get(
                "recommendation", "complete"
            )
            st.toast(f"Audit finished for {job['crn']}: {verdict}", icon="✅")
        elif job["status"] == "FAILED":
            st.toast(f"Audit failed for {job['crn']}", icon="🚨")
        else:
            st.toast(f"Audit {job['status'].lower()} for {job['crn']}", icon="⚠️")

    if job["status"] == "SUCCEEDED" and job["result"]:
        report = (job["result"].get("red_flag_verdict") or {})
        elapsed = ""
        if job.get("started_at") and job.get("finished_at"):
            try:
                from datetime import datetime

                delta = datetime.fromisoformat(job["finished_at"]) - datetime.fromisoformat(
                    job["started_at"]
                )
                elapsed = f" in {delta.total_seconds():.0f}s"
            except ValueError:
                elapsed = ""
        st.success(
            f"Audit complete{elapsed} — verdict: {report.get('recommendation', 'unknown')}"
        )
        render_report(job["result"])
    elif job["status"] == "FAILED":
        st.error(job["error"] or "Job failed")
    elif job["status"] == "CANCELLED":
        st.warning("Job cancelled by operator")
    elif job["status"] == "INTERRUPTED":
        st.warning("Job was interrupted by a process restart")


st.title("DueDiligence Direct")
st.caption(
    "Governed multi-agent M&A diligence fleet - Companies House statutory data, "
    "Gemini reasoning, and enterprise controls on every call."
)

with st.sidebar:
    st.subheader("Run an audit")
    if st.button("🔍 Search by company name", width="stretch"):
        company_search_dialog()

    crn = st.text_input(
        "Company number",
        value=st.session_state.get("selected_crn", "03994971"),
        max_chars=8,
        help="Type it directly, or use the search above if you only know the name.",
    )
    if st.session_state.get("selected_name"):
        st.caption(f"Selected: {st.session_state['selected_name']}")
    uploaded_files = st.file_uploader(
        "Deal documents (optional)",
        type=["csv", "md", "pdf", "txt"],
        accept_multiple_files=True,
        help=(
            "Contracts, side letters, and other data room material. Uploads are sent to the "
            "fleet and screened by Model Armor before any agent reads them. Statutory records "
            "and filed accounts are always audited; financial figures never come from these "
            "documents."
        ),
    )
    st.caption(
        "No documents? The audit still runs on Companies House statutory records and the "
        "company's filed accounts."
    )
    run_clicked = st.button("Submit to Agent Runtime", type="primary", width="stretch")

    st.divider()
    st.subheader("Backend")
    try:
        fleet_info = backend.fleet()
        backend_error = ""
    except Exception as exc:
        fleet_info = {}
        backend_error = str(exc)

    st.markdown(
        badge(
            "CLOUD RUN" if backend.mode == "remote" else "LOCAL",
            "#1d4ed8" if backend.mode == "remote" else "#475569",
        ),
        unsafe_allow_html=True,
    )
    st.caption(backend.description)
    if backend_error:
        st.error(backend_error)

    stats = fleet_info.get("runtime", {"workers": 0, "total": 0, "in_flight": 0})
    st.metric("Jobs in flight", stats.get("in_flight", 0))
    st.metric("Jobs recorded", stats.get("total", 0))
    st.caption(f"Runtime workers: {stats.get('workers', 0)}")

if run_clicked:
    try:
        query = mcp_server.CompanyQuery(crn=crn)
    except ValidationError as exc:
        st.error(exc.errors()[0]["msg"])
        st.stop()

    # No picker: either the operator uploads documents, or the audit is statutory-only.
    data_room_path = ""
    if uploaded_files:
        try:
            data_room_path = backend.upload_data_room(
                [(item.name, item.getvalue()) for item in uploaded_files]
            )
            st.toast(f"Uploaded {len(uploaded_files)} document(s) to the fleet")
        except Exception as exc:
            st.error(f"Upload failed: {exc}")
            st.stop()

    st.session_state["job_id"] = backend.submit_job(
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
        recent = backend.list_jobs(limit=10)
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
            for card in fleet_info.get("agents", [])
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
            for identity in fleet_info.get("identities", [])
        ],
        hide_index=True,
        width="stretch",
    )
    right.markdown("#### Gateway tool policies")
    right.dataframe(fleet_info.get("tools", []), hide_index=True, width="stretch")
    right.caption(f"Allowed egress hosts: {', '.join(fleet_info.get('allowed_egress_hosts', []))}")

with audit_tab:
    verification = backend.verify_audit()
    if verification["valid"]:
        st.success(
            f"Audit chain verified across {verification['records']} record(s). "
            f"Head hash {verification.get('head_hash', '')[:16]}..."
        )
    else:
        st.error(f"Audit chain broken at record {verification.get('broken_at')}")

    records = backend.audit(limit=300)
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
    # Span files live on the fleet's own disk, so they are only readable locally.
    span_log = Path(os.getenv("FLEET_TELEMETRY_DIR", "telemetry")) / "spans.jsonl"
    if backend.mode == "remote":
        st.caption(
            "OpenTelemetry spans for this fleet are exported to Cloud Trace; look up a run by "
            "its trace id there."
        )
    elif span_log.exists():
        with st.expander("Latest OpenTelemetry spans"):
            lines = span_log.read_text(encoding="utf-8").strip().splitlines()[-25:]
            st.json([json.loads(line) for line in lines if line.strip()])

with memory_tab:
    lookup_crn = st.text_input("Company number to recall", value=crn, max_chars=8, key="memory-crn")
    if lookup_crn:
        memory = backend.memory(lookup_crn)
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
            backend.add_note(lookup_crn, new_note.strip(), author="dashboard")
            st.rerun()
