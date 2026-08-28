"""DueDiligence Direct — client library for the governed diligence fleet.

The fleet runs as a service: audits are submitted, executed server-side under
agent identities, and recorded in a single hash-chained audit trail. This
library is the supported way to drive it from Python.

    from ddclient import DueDiligenceClient

    with DueDiligenceClient("https://fleet.example.run.app", access_code="...") as fleet:
        report = fleet.run("03994971")
        print(report.recommendation)
        for finding in report.material_findings:
            print(f"[{finding.severity}] {finding.category}: {finding.finding}")

Every figure in a report is parsed from documents the company filed at
Companies House. Nothing in this library generates, estimates, or substitutes
financial data, and a report that fails its balance-sheet reconciliation says so
rather than reporting ratios derived from numbers that do not add up.
"""

from .client import DueDiligenceClient
from .errors import (
    APIError,
    AuthenticationError,
    FleetError,
    JobFailed,
    NotFound,
    PolicyDenied,
    TransportError,
    WaitTimeout,
)
from .models import (
    AccountsPeriod,
    CompanySearchResult,
    DebatePoint,
    Exchange,
    Finding,
    Job,
    JobPage,
    ModelCall,
    ReconciliationCheck,
    Report,
    TokenUsage,
)

__version__ = "1.0.0"

__all__ = [
    "DueDiligenceClient",
    # errors
    "FleetError",
    "TransportError",
    "APIError",
    "AuthenticationError",
    "PolicyDenied",
    "NotFound",
    "JobFailed",
    "WaitTimeout",
    # models
    "Job",
    "JobPage",
    "Report",
    "Finding",
    "DebatePoint",
    "Exchange",
    "ModelCall",
    "TokenUsage",
    "ReconciliationCheck",
    "AccountsPeriod",
    "CompanySearchResult",
    "__version__",
]
