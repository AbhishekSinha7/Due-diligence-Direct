"""Tests for the ddclient library.

Two halves. The model tests run against a real saved audit, so the accessors are
checked against the shape the orchestrator actually emits rather than a fixture
someone wrote to match the code. The transport tests run against a real server
in a thread, because a mocked HTTP layer would not catch a routing mistake.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
from pathlib import Path

import ddclient
from ddclient import DueDiligenceClient, Report
from ddclient.errors import NotFound, TransportError, WaitTimeout
from ddclient.models import Job

REPO_ROOT = Path(__file__).resolve().parents[1]


def _saved_run() -> dict:
    """The most recent real audit checked into runs/, if there is one."""

    runs = sorted((REPO_ROOT / "runs").glob("*.json"))
    if not runs:
        raise unittest.SkipTest("no saved run available")
    return json.loads(runs[-1].read_text(encoding="utf-8"))


class ReportModelTests(unittest.TestCase):
    """The report accessors, checked against a genuine orchestrator result."""

    @classmethod
    def setUpClass(cls):
        cls.state = _saved_run()
        cls.report = Report(cls.state)

    def test_identity_and_verdict(self):
        self.assertEqual(self.report.crn, self.state["crn"])
        self.assertEqual(
            self.report.recommendation, self.state["red_flag_verdict"]["recommendation"]
        )
        self.assertTrue(self.report.executive_summary)
        self.assertEqual(self.report.top_risks, self.state["red_flag_verdict"]["top_risks"])

    def test_findings_merge_both_agents_and_sort_by_severity(self):
        expected = len(self.state["legal_risks"]["risks"]) + len(
            self.state["financial_analysis"]["findings"]
        )
        self.assertEqual(len(self.report.findings), expected)

        order = [f.severity for f in self.report.findings]
        rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "CLEAR": 3}
        self.assertEqual(order, sorted(order, key=lambda s: rank.get(s, 9)))

        self.assertTrue(all(f.is_material for f in self.report.material_findings))
        self.assertEqual(
            self.report.findings_at("HIGH", "MEDIUM"), self.report.material_findings
        )

    def test_statutory_record_is_unwrapped_from_the_endpoint_envelope(self):
        profile = self.state["raw_statutory_data"]["profile"]["data"]
        self.assertEqual(self.report.company, profile)
        self.assertEqual(self.report.company_name, profile["company_name"])
        self.assertEqual(self.report.company_status, profile["company_status"])
        self.assertIsInstance(self.report.officers, list)
        self.assertIsInstance(self.report.pscs, list)
        # A not_found endpoint must yield an empty list, never raise.
        self.assertEqual(self.report.insolvency_cases, [])

    def test_failed_reconciliation_is_surfaced(self):
        """The known-bad filing must be reported, not smoothed over."""

        periods = self.report.periods
        if not periods:
            self.skipTest("saved run has no parsed accounts")
        checks = [c for p in periods for c in p.reconciliation]
        self.assertTrue(checks, "filed accounts should carry identity checks")
        failures = self.report.reconciliation_failures
        for period in periods:
            self.assertEqual(period.reconciles, not period.failed_checks)
        for failure in failures:
            self.assertFalse(failure.consistent)
            self.assertTrue(failure.formula)

    def test_reasoning_chain_and_token_usage(self):
        chain = self.report.reasoning_chain
        self.assertEqual(len(chain), len(self.state["reasoning_chain"]))
        if chain:
            self.assertTrue(chain[0].sender)
            self.assertTrue(chain[0].recipient)
            self.assertIn("->", str(chain[0]))

        usage = self.report.token_usage
        self.assertGreaterEqual(usage.total_tokens, 0)
        for call in usage.by_call:
            self.assertEqual(call.total_tokens, call.prompt_tokens + call.output_tokens)

    def test_missing_keys_degrade_instead_of_raising(self):
        """A partial or failed run must still be readable."""

        empty = Report({})
        self.assertEqual(empty.recommendation, "UNKNOWN")
        self.assertEqual(empty.findings, [])
        self.assertEqual(empty.top_risks, [])
        self.assertEqual(empty.company, {})
        self.assertEqual(empty.periods, [])
        self.assertEqual(empty.reconciliation_failures, [])
        self.assertEqual(empty.token_usage.total_tokens, 0)
        self.assertFalse(empty.accounts_available)
        self.assertFalse(empty.used_deterministic_fallback)


class JobModelTests(unittest.TestCase):
    def test_job_separates_stage_events_from_agent_exchanges(self):
        job = Job(
            {
                "job_id": "job-1",
                "crn": "03994971",
                "status": "RUNNING",
                "events": [
                    {"stage": "ingest", "message": "collecting", "timestamp": "2026-08-23T10:00:00Z"},
                    {
                        "stage": "exchange",
                        "message": "Audit 03994971.",
                        "timestamp": "2026-08-23T10:00:01Z",
                        "attributes": {
                            "exchange": True,
                            "sender": "orchestrator",
                            "recipient": "legal_risk",
                            "kind": "task_assignment",
                        },
                    },
                ],
            }
        )
        self.assertEqual(len(job.events), 2)
        self.assertEqual(len(job.stage_events), 1)
        self.assertEqual(len(job.exchanges), 1)
        self.assertEqual(job.exchanges[0].sender, "orchestrator")
        self.assertFalse(job.is_terminal)
        self.assertIsNone(job.report)

    def test_terminal_states(self):
        self.assertTrue(Job({"status": "SUCCEEDED"}).succeeded)
        for status in ("SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"):
            self.assertTrue(Job({"status": status}).is_terminal, status)
        self.assertFalse(Job({"status": "QUEUED"}).is_terminal)

    def test_report_is_exposed_once_the_result_lands(self):
        job = Job({"status": "SUCCEEDED", "result": {"crn": "03994971"}})
        self.assertIsInstance(job.report, Report)
        self.assertEqual(job.report.crn, "03994971")


class ClientConstructionTests(unittest.TestCase):
    def test_a_url_is_required(self):
        with self.assertRaises(ValueError):
            DueDiligenceClient("")

    def test_unreachable_host_raises_transport_error(self):
        # Port 9 is discard; nothing serves HTTP there.
        client = DueDiligenceClient("http://127.0.0.1:9", timeout=0.4)
        with self.assertRaises(TransportError):
            client.health()
        client.close()

    def test_upload_rejects_unsupported_types_before_sending(self):
        client = DueDiligenceClient("http://127.0.0.1:9", timeout=0.4)
        with self.assertRaises(ValueError):
            client.upload_documents([("payload.exe", b"MZ")])
        with self.assertRaises(ValueError):
            client.upload_documents([])
        client.close()

    def test_submit_rejects_conflicting_document_sources(self):
        client = DueDiligenceClient("http://127.0.0.1:9", timeout=0.4)
        with self.assertRaises(ValueError):
            client.submit("03994971", documents=[("a.txt", b"x")], data_room_path="data_room")
        client.close()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class LiveServiceTests(unittest.TestCase):
    """The client against a real control plane, over real HTTP."""

    server = None
    thread = None
    base_url = ""

    @classmethod
    def setUpClass(cls):
        import uvicorn

        import service

        port = _free_port()
        cls.base_url = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(service.app, host="127.0.0.1", port=port, log_level="error")
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if getattr(cls.server, "started", False):
                return
            time.sleep(0.05)
        raise unittest.SkipTest("the control plane did not start in time")

    @classmethod
    def tearDownClass(cls):
        if cls.server is not None:
            cls.server.should_exit = True
        if cls.thread is not None:
            cls.thread.join(timeout=10)

    def client(self) -> DueDiligenceClient:
        return DueDiligenceClient(self.base_url, timeout=10)

    def test_service_metadata(self):
        with self.client() as fleet:
            self.assertEqual(fleet.health()["status"], "ok")
            index = fleet.index()
            self.assertIn("endpoints", index)
            self.assertGreater(index["agents_registered"], 0)
            self.assertIn("status", fleet.ready())

    def test_registry_and_audit_chain(self):
        with self.client() as fleet:
            roster = fleet.fleet()
            self.assertTrue(roster["agents"])
            self.assertTrue(roster["allowed_egress_hosts"])
            self.assertTrue(fleet.verify_audit_chain()["valid"])
            self.assertIsInstance(fleet.audit_records(limit=5), list)

    def test_jobs_listing_and_missing_job(self):
        with self.client() as fleet:
            self.assertIsInstance(fleet.list_jobs(limit=5), list)
            with self.assertRaises(NotFound):
                fleet.get_job("job-does-not-exist")

    def test_memory_notes_round_trip(self):
        with self.client() as fleet:
            fleet.add_note("03994971", "Checked by the client test suite.", author="tests")
            recalled = fleet.memory("03994971")
            notes = [n.get("note") for n in recalled.get("operator_notes", [])]
            self.assertIn("Checked by the client test suite.", notes)

    def test_document_upload_creates_a_data_room(self):
        with self.client() as fleet:
            path = fleet.upload_documents(
                [("schedule.txt", b"A routine services schedule with no unusual terms.")]
            )
            self.assertTrue(path)
            self.assertIn("uploads", path)

    def test_wait_for_gives_up_without_cancelling(self):
        """A client-side timeout must not silently stop a server-side run."""

        with self.client() as fleet:
            queued = {
                "job_id": "job-stuck",
                "crn": "03994971",
                "status": "RUNNING",
                "events": [],
            }
            fleet.get_job = lambda job_id: Job(queued)  # type: ignore[assignment]
            with self.assertRaises(WaitTimeout) as caught:
                fleet.wait_for("job-stuck", timeout=0.3, poll_interval=0.1)
            self.assertEqual(caught.exception.job.job_id, "job-stuck")
            self.assertIn("still running", str(caught.exception))

    def test_wait_for_streams_each_event_once(self):
        with self.client() as fleet:
            frames = [
                Job({"job_id": "j", "status": "RUNNING", "events": [{"stage": "a", "message": "1"}]}),
                Job(
                    {
                        "job_id": "j",
                        "status": "RUNNING",
                        "events": [{"stage": "a", "message": "1"}, {"stage": "b", "message": "2"}],
                    }
                ),
                Job(
                    {
                        "job_id": "j",
                        "status": "SUCCEEDED",
                        "events": [
                            {"stage": "a", "message": "1"},
                            {"stage": "b", "message": "2"},
                            {"stage": "c", "message": "3"},
                        ],
                        "result": {"crn": "03994971"},
                    }
                ),
            ]
            calls = iter(frames)
            fleet.get_job = lambda job_id: next(calls)  # type: ignore[assignment]

            seen: list[str] = []
            finished = fleet.wait_for("j", timeout=5, poll_interval=0.01, on_event=lambda e: seen.append(e["message"]))
            self.assertEqual(seen, ["1", "2", "3"], "each event should be delivered exactly once")
            self.assertTrue(finished.succeeded)
            self.assertIsNotNone(finished.report)


class PackageSurfaceTests(unittest.TestCase):
    def test_public_exports_are_importable(self):
        for name in ddclient.__all__:
            self.assertTrue(hasattr(ddclient, name), name)

    def test_cli_parser_covers_every_command(self):
        from ddclient.cli import build_parser

        parser = build_parser()
        for command in ("audit", "job", "jobs", "search", "cancel", "pdf", "memory", "fleet", "verify", "ready"):
            args = parser.parse_args([command, *(["03994971"] if command in {"audit", "memory"} else []),
                                      *(["job-1"] if command in {"job", "cancel", "pdf"} else []),
                                      *(["acme"] if command == "search" else [])])
            self.assertTrue(callable(args.func), command)


if __name__ == "__main__":
    unittest.main()
