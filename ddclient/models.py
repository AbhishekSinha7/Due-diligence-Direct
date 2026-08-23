"""Typed views over the control plane's JSON.

Each model wraps the raw payload rather than replacing it. The fleet's schema
will grow, and a client that drops unknown keys silently loses evidence — so
every model keeps `.raw` and exposes the fields callers actually reach for.

Nothing here validates or recomputes. The numbers came from documents the
company filed at Companies House; this layer must not become a second opinion
about what they mean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"})

SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "CLEAR": 3}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


@dataclass(frozen=True)
class CompanySearchResult:
    """One hit from the register's company search."""

    company_number: str
    title: str
    company_status: str = ""
    company_type: str = ""
    date_of_creation: str = ""
    address_snippet: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "CompanySearchResult":
        return cls(
            company_number=str(payload.get("company_number", "")),
            title=str(payload.get("title", "")),
            company_status=str(payload.get("company_status", "") or ""),
            company_type=str(payload.get("company_type", "") or ""),
            date_of_creation=str(payload.get("date_of_creation", "") or ""),
            address_snippet=str(payload.get("address_snippet", "") or ""),
            raw=payload,
        )

    def __str__(self) -> str:  # pragma: no cover - convenience
        return f"{self.title} ({self.company_number})"


@dataclass(frozen=True)
class Finding:
    """A single graded finding from the legal or financial agent."""

    category: str
    severity: str
    finding: str
    evidentiary_quote: str = ""
    evidence_verified: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Finding":
        return cls(
            category=str(payload.get("category", "") or ""),
            severity=str(payload.get("severity", "") or "").upper(),
            finding=str(payload.get("finding", "") or ""),
            evidentiary_quote=str(payload.get("evidentiary_quote", "") or ""),
            evidence_verified=payload.get("evidence_verified"),
            raw=payload,
        )

    @property
    def is_material(self) -> bool:
        """HIGH or MEDIUM. What a deal team would actually act on."""

        return self.severity in {"HIGH", "MEDIUM"}


@dataclass(frozen=True)
class DebatePoint:
    """A disagreement between the legal and financial agents, and its resolution."""

    issue: str
    severity: str
    legal_view: str = ""
    financial_view: str = ""
    resolved_position: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DebatePoint":
        return cls(
            issue=str(payload.get("issue", "") or ""),
            severity=str(payload.get("severity", "") or "").upper(),
            legal_view=str(payload.get("legal_view", "") or ""),
            financial_view=str(payload.get("financial_view", "") or ""),
            resolved_position=str(payload.get("resolved_position", "") or ""),
            raw=payload,
        )


@dataclass(frozen=True)
class Exchange:
    """One typed message an agent sent another during the run."""

    seq: int
    sender: str
    recipient: str
    kind: str
    message: str
    timestamp: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Exchange":
        return cls(
            seq=int(payload.get("seq", 0) or 0),
            sender=str(payload.get("sender", "") or ""),
            recipient=str(payload.get("recipient", "") or ""),
            kind=str(payload.get("kind", "") or ""),
            message=str(payload.get("message", "") or ""),
            timestamp=str(payload.get("timestamp", "") or ""),
            attributes=_as_dict(payload.get("attributes")),
            raw=payload,
        )

    def __str__(self) -> str:  # pragma: no cover - convenience
        return f"{self.sender} -> {self.recipient}: {self.message}"


@dataclass(frozen=True)
class ModelCall:
    """One model invocation, with what it cost."""

    schema: str
    model: str
    prompt_tokens: int
    output_tokens: int
    latency_ms: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ModelCall":
        return cls(
            schema=str(payload.get("schema", "") or ""),
            model=str(payload.get("model", "") or ""),
            prompt_tokens=int(payload.get("prompt_tokens", 0) or 0),
            output_tokens=int(payload.get("output_tokens", 0) or 0),
            latency_ms=int(payload.get("latency_ms", 0) or 0),
        )


@dataclass(frozen=True)
class TokenUsage:
    """What the run cost in tokens, in total and per call."""

    calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    total_model_latency_ms: int = 0
    by_call: tuple[ModelCall, ...] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TokenUsage":
        payload = _as_dict(payload)
        return cls(
            calls=int(payload.get("calls", 0) or 0),
            prompt_tokens=int(payload.get("prompt_tokens", 0) or 0),
            output_tokens=int(payload.get("output_tokens", 0) or 0),
            total_tokens=int(payload.get("total_tokens", 0) or 0),
            total_model_latency_ms=int(payload.get("total_model_latency_ms", 0) or 0),
            by_call=tuple(
                ModelCall.from_payload(entry) for entry in _as_list(payload.get("by_call"))
            ),
        )


@dataclass(frozen=True)
class ReconciliationCheck:
    """One balance-sheet identity, and whether the filed accounts satisfy it."""

    identity: str
    formula: str
    expected: float | None
    reported: float | None
    difference: float | None
    consistent: bool

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ReconciliationCheck":
        return cls(
            identity=str(payload.get("identity", "") or ""),
            formula=str(payload.get("formula", "") or ""),
            expected=payload.get("expected"),
            reported=payload.get("reported"),
            difference=payload.get("difference"),
            consistent=bool(payload.get("consistent")),
        )


@dataclass(frozen=True)
class AccountsPeriod:
    """One accounting period parsed out of the filed iXBRL document."""

    period_end: str
    metrics: dict[str, Any]
    evidence: dict[str, Any]
    reconciliation: tuple[ReconciliationCheck, ...]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "AccountsPeriod":
        return cls(
            period_end=str(payload.get("period_end", "") or ""),
            metrics=_as_dict(payload.get("metrics")),
            evidence=_as_dict(payload.get("evidence")),
            reconciliation=tuple(
                ReconciliationCheck.from_payload(entry)
                for entry in _as_list(payload.get("reconciliation"))
            ),
            raw=payload,
        )

    @property
    def failed_checks(self) -> tuple[ReconciliationCheck, ...]:
        """Identities the filed accounts do not satisfy.

        A non-empty result means the company's own numbers do not add up, and
        any ratio derived from them was suppressed rather than reported.
        """

        return tuple(check for check in self.reconciliation if not check.consistent)

    @property
    def reconciles(self) -> bool:
        return not self.failed_checks


class Report:
    """A finished audit.

    Wraps the orchestrator's final state. Accessors are read-only views; the
    complete payload stays available at `.raw` so nothing is lost to this layer.
    """

    def __init__(self, state: dict[str, Any]) -> None:
        self.raw = _as_dict(state)

    # -- identity --------------------------------------------------------
    @property
    def crn(self) -> str:
        return str(self.raw.get("crn", "") or "")

    @property
    def run_id(self) -> str:
        return str(self.raw.get("run_id", "") or "")

    @property
    def trace_id(self) -> str:
        return str(self.raw.get("trace_id", "") or "")

    # -- verdict ---------------------------------------------------------
    @property
    def verdict(self) -> dict[str, Any]:
        return _as_dict(self.raw.get("red_flag_verdict"))

    @property
    def recommendation(self) -> str:
        """GREEN LIGHT, PROCEED WITH CAUTION, or RED FLAG DEAL BREAKER."""

        return str(self.verdict.get("recommendation", "") or "UNKNOWN")

    @property
    def executive_summary(self) -> str:
        return str(self.verdict.get("executive_summary", "") or "")

    @property
    def top_risks(self) -> list[str]:
        return [str(item) for item in _as_list(self.verdict.get("top_risks"))]

    @property
    def required_human_review(self) -> list[str]:
        return [str(item) for item in _as_list(self.verdict.get("required_human_review"))]

    @property
    def reliance_disclaimer(self) -> str:
        return str(self.verdict.get("reliance_disclaimer", "") or "")

    # -- findings --------------------------------------------------------
    @property
    def legal_findings(self) -> list[Finding]:
        agent = _as_dict(self.raw.get("legal_risks"))
        return [Finding.from_payload(_as_dict(item)) for item in _as_list(agent.get("risks"))]

    @property
    def financial_findings(self) -> list[Finding]:
        agent = _as_dict(self.raw.get("financial_analysis"))
        return [Finding.from_payload(_as_dict(item)) for item in _as_list(agent.get("findings"))]

    @property
    def findings(self) -> list[Finding]:
        """Every graded finding, most severe first."""

        combined = self.legal_findings + self.financial_findings
        return sorted(combined, key=lambda item: SEVERITY_ORDER.get(item.severity, 9))

    def findings_at(self, *severities: str) -> list[Finding]:
        """Findings matching the given severities, e.g. `findings_at("HIGH")`."""

        wanted = {str(level).upper() for level in severities}
        return [item for item in self.findings if item.severity in wanted]

    @property
    def material_findings(self) -> list[Finding]:
        return self.findings_at("HIGH", "MEDIUM")

    @property
    def debate_points(self) -> list[DebatePoint]:
        transcript = _as_dict(self.raw.get("debate_transcript"))
        return [
            DebatePoint.from_payload(_as_dict(item)) for item in _as_list(transcript.get("points"))
        ]

    @property
    def reasoning_chain(self) -> list[Exchange]:
        return [
            Exchange.from_payload(_as_dict(item))
            for item in _as_list(self.raw.get("reasoning_chain"))
        ]

    # -- statutory record -------------------------------------------------
    def _endpoint(self, name: str) -> dict[str, Any]:
        record = _as_dict(_as_dict(self.raw.get("raw_statutory_data")).get(name))
        return _as_dict(record.get("data"))

    @property
    def company(self) -> dict[str, Any]:
        """The Companies House company profile, as filed."""

        return self._endpoint("profile")

    @property
    def company_name(self) -> str:
        return str(self.company.get("company_name", "") or "")

    @property
    def company_status(self) -> str:
        return str(self.company.get("company_status", "") or "")

    @property
    def officers(self) -> list[dict[str, Any]]:
        return [_as_dict(item) for item in _as_list(self._endpoint("officers").get("items"))]

    @property
    def pscs(self) -> list[dict[str, Any]]:
        return [_as_dict(item) for item in _as_list(self._endpoint("pscs").get("items"))]

    @property
    def charges(self) -> list[dict[str, Any]]:
        return [_as_dict(item) for item in _as_list(self._endpoint("charges").get("items"))]

    @property
    def insolvency_cases(self) -> list[dict[str, Any]]:
        return [_as_dict(item) for item in _as_list(self._endpoint("insolvency").get("cases"))]

    @property
    def filings(self) -> list[dict[str, Any]]:
        return [_as_dict(item) for item in _as_list(self._endpoint("filings").get("items"))]

    # -- filed accounts ---------------------------------------------------
    @property
    def accounts(self) -> dict[str, Any]:
        return _as_dict(self.raw.get("accounts"))

    @property
    def accounts_available(self) -> bool:
        return self.accounts.get("status") == "success"

    @property
    def accounts_analysis(self) -> dict[str, Any]:
        return _as_dict(_as_dict(self.accounts.get("latest")).get("analysis"))

    @property
    def accounts_document_url(self) -> str:
        """The Companies House document the figures were parsed from."""

        return str(_as_dict(self.accounts.get("latest")).get("document_url", "") or "")

    @property
    def periods(self) -> list[AccountsPeriod]:
        return [
            AccountsPeriod.from_payload(_as_dict(item))
            for item in _as_list(self.accounts_analysis.get("periods"))
        ]

    @property
    def reconciliation_failures(self) -> list[ReconciliationCheck]:
        """Every balance-sheet identity the filed accounts fail, across periods."""

        return [check for period in self.periods for check in period.failed_checks]

    # -- governance -------------------------------------------------------
    @property
    def governance(self) -> dict[str, Any]:
        return _as_dict(self.raw.get("governance"))

    @property
    def token_usage(self) -> TokenUsage:
        return TokenUsage.from_payload(self.governance.get("token_usage"))

    @property
    def severity_counts(self) -> dict[str, int]:
        counts = _as_dict(self.governance.get("severity_counts"))
        return {key: int(value or 0) for key, value in counts.items()}

    @property
    def models_used(self) -> list[str]:
        return [str(item) for item in _as_list(self.governance.get("models_used"))]

    @property
    def used_deterministic_fallback(self) -> bool:
        """True when at least one agent could not reach a model.

        The run still completes with explicit limitations, but a caller relying
        on model reasoning should know it did not happen.
        """

        return bool(_as_list(self.governance.get("agents_on_deterministic_fallback")))

    @property
    def unverified_citations(self) -> int:
        return int(self.governance.get("unverified_citations", 0) or 0)

    @property
    def documents_quarantined(self) -> int:
        return int(self.governance.get("documents_quarantined", 0) or 0)

    @property
    def memory(self) -> dict[str, Any]:
        return _as_dict(self.raw.get("memory"))

    @property
    def changes_since_last_audit(self) -> list[dict[str, Any]]:
        return [_as_dict(item) for item in _as_list(self.memory.get("changes_since_last_audit"))]

    def __repr__(self) -> str:  # pragma: no cover - convenience
        return f"<Report {self.crn} {self.recommendation!r} findings={len(self.findings)}>"


@dataclass(frozen=True)
class JobPage:
    """One page of audit history, and how much more there is."""

    jobs: tuple["Job", ...]
    total: int
    limit: int
    offset: int

    def __iter__(self):
        return iter(self.jobs)

    def __len__(self) -> int:
        return len(self.jobs)

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.jobs) < self.total

    @property
    def next_offset(self) -> int:
        return self.offset + len(self.jobs)


class Job:
    """An audit submitted to the fleet's runtime.

    A job is a handle, not a result: it exists from the moment it is queued, and
    carries a report only once it has succeeded.
    """

    def __init__(self, payload: dict[str, Any]) -> None:
        self.raw = _as_dict(payload)

    @property
    def job_id(self) -> str:
        return str(self.raw.get("job_id", "") or "")

    @property
    def crn(self) -> str:
        return str(self.raw.get("crn", "") or "")

    @property
    def status(self) -> str:
        return str(self.raw.get("status", "") or "")

    @property
    def trace_id(self) -> str:
        return str(self.raw.get("trace_id", "") or "")

    @property
    def error(self) -> str:
        return str(self.raw.get("error", "") or "")

    @property
    def submitted_by(self) -> str:
        return str(self.raw.get("submitted_by", "") or "")

    @property
    def created_at(self) -> str:
        return str(self.raw.get("created_at", "") or "")

    @property
    def finished_at(self) -> str:
        return str(self.raw.get("finished_at", "") or "")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def succeeded(self) -> bool:
        return self.status == "SUCCEEDED"

    @property
    def events(self) -> list[dict[str, Any]]:
        return [_as_dict(item) for item in _as_list(self.raw.get("events"))]

    @property
    def stage_events(self) -> list[dict[str, Any]]:
        """Progress events, excluding the inter-agent messages."""

        return [event for event in self.events if not _as_dict(event.get("attributes")).get("exchange")]

    @property
    def exchanges(self) -> list[Exchange]:
        """Inter-agent messages recorded so far, live while the job runs."""

        out: list[Exchange] = []
        for event in self.events:
            attributes = _as_dict(event.get("attributes"))
            if not attributes.get("exchange"):
                continue
            out.append(
                Exchange.from_payload(
                    {
                        "seq": len(out) + 1,
                        "sender": attributes.get("sender", "agent"),
                        "recipient": attributes.get("recipient", "agent"),
                        "kind": attributes.get("kind", "message"),
                        "message": event.get("message", ""),
                        "timestamp": event.get("timestamp", ""),
                        "attributes": attributes,
                    }
                )
            )
        return out

    @property
    def summary(self) -> dict[str, Any]:
        """The compact result view returned by list endpoints.

        Present whether or not the full report was fetched, so a listing can
        show a verdict without downloading every audit in full.
        """

        return _as_dict(self.raw.get("summary"))

    @property
    def recommendation(self) -> str:
        """The verdict, from the full report if present, else the summary."""

        report = self.report
        if report is not None:
            return report.recommendation
        return str(self.summary.get("recommendation", "") or "")

    @property
    def company_name(self) -> str:
        report = self.report
        if report is not None and report.company_name:
            return report.company_name
        return str(self.summary.get("company_name", "") or "")

    @property
    def duration_seconds(self) -> float | None:
        """How long the run took, or None if it has not finished."""

        from datetime import datetime

        if not self.raw.get("started_at") or not self.raw.get("finished_at"):
            return None
        try:
            started = datetime.fromisoformat(str(self.raw["started_at"]))
            finished = datetime.fromisoformat(str(self.raw["finished_at"]))
        except ValueError:
            return None
        return (finished - started).total_seconds()

    @property
    def report(self) -> Report | None:
        """The finished audit, or None while the job is still in flight."""

        result = self.raw.get("result")
        return Report(result) if isinstance(result, dict) and result else None

    def __iter__(self) -> Iterator[dict[str, Any]]:  # pragma: no cover - convenience
        return iter(self.events)

    def __repr__(self) -> str:  # pragma: no cover - convenience
        return f"<Job {self.job_id} {self.crn} {self.status}>"
