"""Audit a company in about ten lines, using the client library.

    pip install requests
    set FLEET_API_URL=https://your-service.run.app
    set FLEET_API_KEY=ddd_v1....
    python examples/quickstart.py 03994971

Submitting an audit spends model quota, so this is the one example that costs
something to run.
"""

import pathlib
import sys

# Run from anywhere: python puts this file's own directory on the path, not the
# repository root, so ddclient would not be importable otherwise.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ddclient import DueDiligenceClient

crn = sys.argv[1] if len(sys.argv) > 1 else "03994971"

# URL and key come from FLEET_API_URL / FLEET_API_KEY when not passed here.
with DueDiligenceClient() as fleet:
    # on_event prints each stage and each message the agents send one another,
    # as they happen, so a long run does not look like a hang.
    report = fleet.run(crn, on_event=lambda e: print("  ", e.get("message", "")))

    print(f"\n{report.recommendation}: {report.company_name}")
    print(report.executive_summary)

    for finding in report.material_findings:
        print(f"\n[{finding.severity}] {finding.category}")
        print(f"  {finding.finding}")
        print(f"  evidence: {finding.evidentiary_quote}")

    # Figures the company's own filing contradicts. Anything derived from them
    # was suppressed rather than reported, and this is how you know.
    for check in report.reconciliation_failures:
        print(f"\n! {check.identity} does not reconcile: "
              f"expected {check.expected:,.0f}, filed {check.reported:,.0f}")

    usage = report.token_usage
    print(f"\n{usage.calls} model calls, {usage.total_tokens:,} tokens")
