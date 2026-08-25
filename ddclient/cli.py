"""Command line front end for the client library.

    python -m ddclient --url https://fleet.example.run.app audit 03994971 --watch

Every command takes its connection details from flags or from the environment
(FLEET_API_URL, FLEET_CONSOLE_ACCESS_CODE, FLEET_API_KEY, FLEET_API_TOKEN), so
a configured shell needs only the verb.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .client import DueDiligenceClient
from .errors import FleetError, JobFailed, WaitTimeout
from .models import Job, Report

SEVERITY_MARK = {"HIGH": "!!", "MEDIUM": " !", "LOW": " ·", "CLEAR": " ok"}


def _out(line: str = "") -> None:
    print(line, flush=True)


def _client(args: argparse.Namespace) -> DueDiligenceClient:
    return DueDiligenceClient(
        args.url,
        access_code=args.access_code,
        api_key=args.api_key,
        token=args.token,
        timeout=args.timeout,
    )


def _print_event(event: dict[str, Any]) -> None:
    attributes = event.get("attributes") or {}
    stamp = str(event.get("timestamp", ""))[11:19]
    if attributes.get("exchange"):
        route = f"{attributes.get('sender', '?')} -> {attributes.get('recipient', '?')}"
        _out(f"  {stamp}  {route}: {event.get('message', '')}")
    else:
        _out(f"  {stamp}  [{event.get('stage', '?')}] {event.get('message', '')}")


def _print_report(report: Report, verbose: bool) -> None:
    _out()
    _out("=" * 72)
    _out(f"  {report.recommendation}")
    _out(f"  {report.company_name or report.crn}  ({report.crn})")
    _out("=" * 72)

    if report.executive_summary:
        _out()
        _out(report.executive_summary)

    if report.top_risks:
        _out()
        _out("Top risks")
        for risk in report.top_risks:
            _out(f"  - {risk}")

    findings = report.findings if verbose else report.material_findings
    if findings:
        _out()
        _out("Findings" if verbose else "Material findings")
        for finding in findings:
            mark = SEVERITY_MARK.get(finding.severity, "  ")
            _out(f"  {mark} [{finding.severity}] {finding.category}")
            _out(f"       {finding.finding}")
            if verbose and finding.evidentiary_quote:
                _out(f"       evidence: {finding.evidentiary_quote}")

    failures = report.reconciliation_failures
    if failures:
        _out()
        _out("Filed accounts do not reconcile")
        for check in failures:
            _out(f"  ! {check.identity}: expected {check.expected:,.0f}, filed {check.reported:,.0f}")
            _out(f"       {check.formula}")
        _out("  Ratios derived from these figures were suppressed, not reported.")

    if report.required_human_review:
        _out()
        _out("Requires human review")
        for item in report.required_human_review:
            _out(f"  - {item}")

    usage = report.token_usage
    _out()
    _out(
        f"Models: {', '.join(report.models_used) or 'none'}  |  "
        f"{usage.calls} call(s), {usage.total_tokens:,} tokens, {usage.total_model_latency_ms:,}ms"
    )
    if verbose and usage.by_call:
        for index, call in enumerate(usage.by_call, start=1):
            _out(
                f"  {index}. {call.schema:<22} {call.model:<20} "
                f"{call.prompt_tokens:>7,} + {call.output_tokens:>6,} = {call.total_tokens:>7,}  "
                f"{call.latency_ms:>6,}ms"
            )
    if report.used_deterministic_fallback:
        _out("  ! At least one agent fell back to deterministic analysis; see limitations.")
    if report.trace_id:
        _out(f"Trace: {report.trace_id}")
    if report.reliance_disclaimer:
        _out()
        _out(report.reliance_disclaimer)


def _print_jobs(jobs: list[Job]) -> None:
    if not jobs:
        _out("No audits match.")
        return
    _out(f"{'JOB':<28} {'COMPANY':<10} {'STATUS':<12} {'VERDICT':<24} CREATED")
    for job in jobs:
        _out(
            f"{job.job_id:<28} {job.crn:<10} {job.status:<12} "
            f"{job.recommendation:<24} {job.created_at[:19]}"
        )


# -- commands ------------------------------------------------------------


def cmd_audit(args: argparse.Namespace) -> int:
    with _client(args) as fleet:
        job = fleet.submit(args.crn, documents=args.document or None)
        _out(f"Submitted {job.job_id} for {job.crn}.")
        if not args.watch:
            _out("Run `python -m ddclient job " + job.job_id + "` to check on it.")
            return 0

        _out("Waiting for the fleet. Ctrl-C stops watching; the run continues.")
        try:
            finished = fleet.wait_for(
                job.job_id, timeout=args.timeout_wait, on_event=_print_event
            )
        except WaitTimeout as exc:
            _out(str(exc))
            return 1
        except KeyboardInterrupt:
            _out(f"\nStopped watching. {job.job_id} is still running on the fleet.")
            return 130

        report = finished.report
        if report is None:
            raise JobFailed(finished)
        _print_report(report, args.verbose)

        if args.pdf:
            path = fleet.save_report_pdf(finished.job_id, args.pdf)
            _out(f"\nPDF written to {path}")
        if args.json:
            with open(args.json, "w", encoding="utf-8") as handle:
                json.dump(report.raw, handle, indent=2, default=str)
            _out(f"JSON written to {args.json}")
        return 0


def cmd_job(args: argparse.Namespace) -> int:
    with _client(args) as fleet:
        job = fleet.get_job(args.job_id)
        _out(f"{job.job_id}  {job.crn}  {job.status}")
        if job.error:
            _out(f"Error: {job.error}")
        for event in job.stage_events:
            _print_event(event)
        report = job.report
        if report:
            _print_report(report, args.verbose)
        return 0 if job.succeeded or not job.is_terminal else 1


def cmd_jobs(args: argparse.Namespace) -> int:
    with _client(args) as fleet:
        if args.all:
            jobs = list(
                fleet.iter_jobs(
                    crn=args.crn, status=args.status, query=args.search, include_result=False
                )
            )
            _print_jobs(jobs)
            _out(f"\n{len(jobs)} audit(s).")
            return 0

        page = fleet.job_page(
            limit=args.limit,
            crn=args.crn,
            status=args.status,
            offset=args.offset,
            query=args.search,
            include_result=False,
        )
        _print_jobs(list(page.jobs))
        first = page.offset + 1 if page.total else 0
        _out(f"\nShowing {first} to {page.offset + len(page)} of {page.total}.")
        if page.has_more:
            _out(f"Next page: --offset {page.next_offset}   (or --all)")
        return 0


def cmd_search(args: argparse.Namespace) -> int:
    with _client(args) as fleet:
        results = fleet.search_companies(args.query, limit=args.limit)
        if not results:
            _out(f"No companies matched {args.query!r}.")
            return 1
        for item in results:
            status = f"[{item.company_status}]" if item.company_status else ""
            _out(f"{item.company_number:<10} {item.title} {status}")
            if item.address_snippet:
                _out(f"           {item.address_snippet}")
        return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    with _client(args) as fleet:
        _out(json.dumps(fleet.cancel(args.job_id), indent=2))
        return 0


def cmd_pdf(args: argparse.Namespace) -> int:
    with _client(args) as fleet:
        _out(f"Written to {fleet.save_report_pdf(args.job_id, args.output)}")
        return 0


def cmd_memory(args: argparse.Namespace) -> int:
    with _client(args) as fleet:
        if args.note:
            fleet.add_note(args.crn, args.note, author=args.author)
            _out("Note recorded.")
            return 0
        _out(json.dumps(fleet.memory(args.crn), indent=2, default=str))
        return 0


def cmd_fleet(args: argparse.Namespace) -> int:
    with _client(args) as fleet:
        data = fleet.fleet()
        for card in data.get("agents", []):
            tools = ", ".join(card.get("tools", []) or card.get("permitted_tools", []) or [])
            _out(f"{card.get('agent_id'):<20} v{card.get('version'):<8} {tools}")
        hosts = data.get("allowed_egress_hosts", [])
        if hosts:
            _out()
            _out("Egress allowlist: " + ", ".join(hosts))
        return 0


def cmd_verify(args: argparse.Namespace) -> int:
    with _client(args) as fleet:
        result = fleet.verify_audit_chain()
        _out(json.dumps(result, indent=2))
        return 0 if result.get("valid") else 2


def cmd_ready(args: argparse.Namespace) -> int:
    with _client(args) as fleet:
        result = fleet.ready()
        _out(json.dumps(result, indent=2))
        return 0 if result.get("status") == "ready" else 2


# -- parser --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ddclient",
        description="Drive the DueDiligence Direct fleet from the command line.",
    )
    parser.add_argument("--url", default=None, help="control plane URL (or FLEET_API_URL)")
    parser.add_argument("--access-code", default=None, help="console access code")
    parser.add_argument("--api-key", default=None, help="x-fleet-api-key shared secret")
    parser.add_argument("--token", default=None, help="bearer token")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request timeout")
    parser.add_argument("-v", "--verbose", action="store_true", help="show every finding and call")

    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="submit an audit")
    audit.add_argument("crn", help="company number, e.g. 03994971")
    audit.add_argument("-d", "--document", action="append", help="deal document to upload")
    audit.add_argument("-w", "--watch", action="store_true", help="stream progress until it finishes")
    audit.add_argument("--pdf", help="save the Red Flag Report PDF here")
    audit.add_argument("--json", help="save the full result JSON here")
    audit.add_argument("--timeout-wait", type=float, default=900.0, help="how long to watch")
    audit.set_defaults(func=cmd_audit)

    job = sub.add_parser("job", help="show one job")
    job.add_argument("job_id")
    job.set_defaults(func=cmd_job)

    jobs = sub.add_parser("jobs", help="list audits")
    jobs.add_argument("-n", "--limit", type=int, default=25, help="page size")
    jobs.add_argument("--offset", type=int, default=0, help="skip this many")
    jobs.add_argument("--all", action="store_true", help="page through every match")
    jobs.add_argument("--crn", default=None, help="only this company number, exactly")
    jobs.add_argument(
        "--search", default=None, metavar="TERM",
        help="match the audited company's name or number",
    )
    jobs.add_argument(
        "--status",
        default=None,
        choices=["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"],
    )
    jobs.set_defaults(func=cmd_jobs)

    search = sub.add_parser("search", help="find a company by name")
    search.add_argument("query", nargs="+")
    search.add_argument("-n", "--limit", type=int, default=10)
    search.set_defaults(func=cmd_search)

    cancel = sub.add_parser("cancel", help="cancel a running job")
    cancel.add_argument("job_id")
    cancel.set_defaults(func=cmd_cancel)

    pdf = sub.add_parser("pdf", help="download a report as PDF")
    pdf.add_argument("job_id")
    pdf.add_argument("-o", "--output", default=".")
    pdf.set_defaults(func=cmd_pdf)

    memory = sub.add_parser("memory", help="recall or annotate a company")
    memory.add_argument("crn")
    memory.add_argument("--note", default=None, help="add an operator note instead of reading")
    memory.add_argument("--author", default="ddclient")
    memory.set_defaults(func=cmd_memory)

    fleet_cmd = sub.add_parser("fleet", help="show the agent registry")
    fleet_cmd.set_defaults(func=cmd_fleet)

    verify = sub.add_parser("verify", help="verify the audit hash chain")
    verify.set_defaults(func=cmd_verify)

    ready = sub.add_parser("ready", help="check readiness and configuration")
    ready.set_defaults(func=cmd_ready)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Search takes the query as several words so quoting is optional.
    if getattr(args, "query", None) and isinstance(args.query, list):
        args.query = " ".join(args.query)
    try:
        return int(args.func(args))
    except FleetError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
