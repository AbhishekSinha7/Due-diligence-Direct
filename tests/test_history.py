"""Tests for listing the full audit history.

The listing endpoint has one property that matters as much as correctness: it
must not send a complete report per row. A history page that downloads thirty
full audits to render a table is the reason it needed rewriting.
"""

from __future__ import annotations

import json
import unittest
import uuid

import runtime


def _seed(count: int, crn: str, status: str = runtime.STATUS_SUCCEEDED, result: dict | None = None):
    """Insert jobs directly; submitting real ones would run the fleet."""

    ids = []
    with runtime._db() as connection:
        for index in range(count):
            job_id = f"test-{uuid.uuid4().hex[:10]}"
            ids.append(job_id)
            connection.execute(
                "INSERT INTO jobs (job_id, crn, data_room_path, status, submitted_by,"
                " created_at, started_at, finished_at, trace_id, events, result, error,"
                " company_name)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id, crn, "", status, "tests",
                    f"2026-01-{index + 1:02d}T10:00:00+00:00",
                    f"2026-01-{index + 1:02d}T10:00:00+00:00",
                    f"2026-01-{index + 1:02d}T10:00:45+00:00",
                    "trace", json.dumps([{"stage": "x", "message": "y"}]),
                    json.dumps(result) if result else None, None,
                    runtime._company_name_of(result),
                ),
            )
    return ids


def _clear():
    with runtime._db() as connection:
        connection.execute("DELETE FROM jobs WHERE job_id LIKE 'test-%'")


FULL_RESULT = {
    "crn": "99999999",
    "red_flag_verdict": {"recommendation": "PROCEED WITH CAUTION", "executive_summary": "x" * 4000},
    "raw_statutory_data": {"profile": {"data": {"company_name": "TEST LIMITED", "company_status": "active"}}},
    "governance": {
        "severity_counts": {"HIGH": 1, "MEDIUM": 2, "LOW": 0, "CLEAR": 3},
        "token_usage": {"total_tokens": 18295},
        "models_used": ["gemini-3.5-flash"],
        "analysis_mode": "model",
    },
    "reasoning_chain": [{"seq": n, "message": "z" * 500} for n in range(14)],
}


class PaginationTests(unittest.TestCase):
    def setUp(self):
        _clear()
        self.addCleanup(_clear)

    def test_offset_walks_the_history_without_repeating(self):
        _seed(7, "88888888")
        first = runtime.list_jobs(limit=3, crn="88888888", include_result=False)
        second = runtime.list_jobs(limit=3, crn="88888888", offset=3, include_result=False)
        third = runtime.list_jobs(limit=3, crn="88888888", offset=6, include_result=False)

        self.assertEqual([len(first), len(second), len(third)], [3, 3, 1])
        ids = [job["job_id"] for job in first + second + third]
        self.assertEqual(len(set(ids)), 7, "pages must not overlap")

    def test_newest_first(self):
        _seed(5, "88888888")
        jobs = runtime.list_jobs(limit=5, crn="88888888", include_result=False)
        dates = [job["created_at"] for job in jobs]
        self.assertEqual(dates, sorted(dates, reverse=True))

    def test_count_matches_the_filters(self):
        _seed(4, "88888888", runtime.STATUS_SUCCEEDED)
        _seed(2, "88888888", runtime.STATUS_FAILED)
        _seed(3, "77777777", runtime.STATUS_SUCCEEDED)

        self.assertEqual(runtime.count_jobs(crn="88888888"), 6)
        self.assertEqual(runtime.count_jobs(crn="88888888", status=runtime.STATUS_FAILED), 2)
        self.assertEqual(runtime.count_jobs(crn="77777777"), 3)

    def test_status_filter_is_case_insensitive(self):
        _seed(3, "88888888", runtime.STATUS_FAILED)
        self.assertEqual(len(runtime.list_jobs(crn="88888888", status="failed")), 3)


class SummaryTests(unittest.TestCase):
    def setUp(self):
        _clear()
        self.addCleanup(_clear)

    def test_listing_omits_the_full_report_but_keeps_a_summary(self):
        _seed(1, "99999999", result=FULL_RESULT)
        job = runtime.list_jobs(crn="99999999", include_result=False)[0]

        self.assertNotIn("result", job)
        self.assertNotIn("events", job)
        self.assertEqual(job["event_count"], 1)

        summary = job["summary"]
        self.assertEqual(summary["recommendation"], "PROCEED WITH CAUTION")
        self.assertEqual(summary["company_name"], "TEST LIMITED")
        self.assertEqual(summary["severity_counts"]["HIGH"], 1)
        self.assertEqual(summary["total_tokens"], 18295)

    def test_the_summary_view_is_dramatically_smaller(self):
        """This is the whole reason the listing changed."""

        _seed(10, "99999999", result=FULL_RESULT)
        full = len(json.dumps(runtime.list_jobs(limit=10, crn="99999999")))
        light = len(json.dumps(runtime.list_jobs(limit=10, crn="99999999", include_result=False)))
        self.assertLess(light * 5, full, f"summary {light}b vs full {full}b")

    def test_a_job_with_no_result_still_lists(self):
        _seed(1, "99999999", status=runtime.STATUS_RUNNING)
        job = runtime.list_jobs(crn="99999999", include_result=False)[0]
        self.assertIsNone(job["summary"])
        self.assertEqual(job["status"], runtime.STATUS_RUNNING)

    def test_full_listing_keeps_its_published_shape(self):
        """Existing clients read job['result']; that must not break."""

        _seed(1, "99999999", result=FULL_RESULT)
        job = runtime.list_jobs(crn="99999999")[0]
        self.assertIn("result", job)
        self.assertIn("events", job)
        self.assertEqual(job["result"]["red_flag_verdict"]["recommendation"], "PROCEED WITH CAUTION")


class ListingEndpointTests(unittest.TestCase):
    def setUp(self):
        from starlette.testclient import TestClient

        import service

        _clear()
        self.addCleanup(_clear)
        self.client = TestClient(service.app)

    def test_response_carries_the_total_so_a_client_can_page(self):
        _seed(6, "88888888")
        with self.client as client:
            body = client.get("/jobs?crn=88888888&limit=2&include_result=false").json()
        self.assertEqual(len(body["jobs"]), 2)
        self.assertEqual(body["total"], 6)
        self.assertEqual(body["limit"], 2)
        self.assertEqual(body["offset"], 0)

    def test_page_size_is_capped(self):
        with self.client as client:
            body = client.get("/jobs?limit=100000").json()
        self.assertLessEqual(body["limit"], 100)

    def test_unknown_status_is_rejected_rather_than_silently_ignored(self):
        with self.client as client:
            response = client.get("/jobs?status=BANANA")
        self.assertEqual(response.status_code, 400)
        self.assertIn("unknown status", response.json()["error"])

    def test_include_result_defaults_to_the_published_behaviour(self):
        _seed(1, "99999999", result=FULL_RESULT)
        with self.client as client:
            body = client.get("/jobs?crn=99999999").json()
        self.assertIn("result", body["jobs"][0])


class ClientPagingTests(unittest.TestCase):
    def setUp(self):
        _clear()
        self.addCleanup(_clear)

    def test_iter_jobs_walks_every_page(self):
        from ddclient import DueDiligenceClient
        from ddclient.models import Job, JobPage

        _seed(12, "88888888")
        client = DueDiligenceClient("http://127.0.0.1:9", timeout=0.4)

        pages: list[JobPage] = []

        def fake_page(limit=25, crn=None, *, status=None, offset=0, include_result=True, query=None):
            rows = runtime.list_jobs(
                limit=limit, crn=crn, status=status, offset=offset,
                include_result=include_result, query=query,
            )
            page = JobPage(
                jobs=tuple(Job(row) for row in rows),
                total=runtime.count_jobs(crn=crn, status=status),
                limit=limit,
                offset=offset,
            )
            pages.append(page)
            return page

        client.job_page = fake_page  # type: ignore[assignment]
        collected = list(client.iter_jobs(crn="88888888", page_size=5))
        client.close()

        self.assertEqual(len(collected), 12)
        self.assertEqual(len({job.job_id for job in collected}), 12)
        self.assertEqual(len(pages), 3, "should stop once the last page is short")

    def test_job_page_reports_whether_more_remain(self):
        from ddclient.models import Job, JobPage

        page = JobPage(jobs=tuple(Job({"job_id": str(n)}) for n in range(5)), total=12, limit=5, offset=0)
        self.assertTrue(page.has_more)
        self.assertEqual(page.next_offset, 5)
        self.assertEqual(len(page), 5)

        last = JobPage(jobs=tuple(Job({"job_id": "x"}) for _ in range(2)), total=12, limit=5, offset=10)
        self.assertFalse(last.has_more)

    def test_job_reads_its_verdict_from_a_summary_when_there_is_no_report(self):
        from ddclient.models import Job

        job = Job({
            "job_id": "j", "status": "SUCCEEDED",
            "summary": {"recommendation": "GREEN LIGHT", "company_name": "SUMMARY LTD"},
        })
        self.assertIsNone(job.report)
        self.assertEqual(job.recommendation, "GREEN LIGHT")
        self.assertEqual(job.company_name, "SUMMARY LTD")

    def test_duration_is_computed_only_for_finished_runs(self):
        from ddclient.models import Job

        finished = Job({
            "started_at": "2026-01-01T10:00:00+00:00",
            "finished_at": "2026-01-01T10:00:45+00:00",
        })
        self.assertEqual(finished.duration_seconds, 45)
        self.assertIsNone(Job({"started_at": "2026-01-01T10:00:00+00:00"}).duration_seconds)


if __name__ == "__main__":
    unittest.main()


class NameSearchTests(unittest.TestCase):
    """The history is searchable by the company's name, not only its number."""

    def setUp(self):
        _clear()
        self.addCleanup(_clear)

    def _named(self, crn: str, name: str, status: str = runtime.STATUS_SUCCEEDED):
        result = dict(FULL_RESULT)
        result["raw_statutory_data"] = {
            "profile": {"data": {"company_name": name, "company_status": "active"}}
        }
        return _seed(1, crn, status=status, result=result)

    def test_the_registered_name_is_stored_and_returned(self):
        self._named("11111111", "ACME TRADING LIMITED")
        job = runtime.list_jobs(crn="11111111", include_result=False)[0]
        self.assertEqual(job["company_name"], "ACME TRADING LIMITED")

    def test_search_matches_part_of_a_name(self):
        self._named("11111111", "ACME TRADING LIMITED")
        self._named("22222222", "BETA HOLDINGS PLC")
        self.assertEqual(runtime.count_jobs(query="trading"), 1)
        self.assertEqual(runtime.count_jobs(query="ACME"), 1)
        self.assertEqual(runtime.count_jobs(query="LIMITED"), 1)

    def test_search_is_case_insensitive(self):
        self._named("11111111", "ACME TRADING LIMITED")
        for term in ("acme", "ACME", "AcMe"):
            self.assertEqual(runtime.count_jobs(query=term), 1, term)

    def test_the_same_box_also_matches_a_number(self):
        """An operator should not have to say which kind of term they typed."""

        self._named("11111111", "ACME TRADING LIMITED")
        self._named("22222222", "BETA HOLDINGS PLC")
        self.assertEqual(runtime.count_jobs(query="1111"), 1)
        self.assertEqual(runtime.count_jobs(query="22222222"), 1)

    def test_search_combines_with_the_status_filter(self):
        self._named("11111111", "ACME TRADING LIMITED", runtime.STATUS_SUCCEEDED)
        self._named("11111111", "ACME TRADING LIMITED", runtime.STATUS_FAILED)
        self.assertEqual(runtime.count_jobs(query="acme"), 2)
        self.assertEqual(
            runtime.count_jobs(query="acme", status=runtime.STATUS_FAILED), 1
        )

    def test_a_job_with_no_report_has_an_empty_name_rather_than_breaking(self):
        _seed(1, "33333333", status=runtime.STATUS_CANCELLED)
        job = runtime.list_jobs(crn="33333333", include_result=False)[0]
        self.assertEqual(job["company_name"], "")
        self.assertEqual(runtime.count_jobs(query="33333333"), 1)

    def test_no_match_returns_nothing_rather_than_everything(self):
        """A filter that silently ignores its term is worse than one that fails."""

        self._named("11111111", "ACME TRADING LIMITED")
        self.assertEqual(runtime.count_jobs(query="zzzznotacompany"), 0)
        self.assertEqual(runtime.list_jobs(query="zzzznotacompany"), [])

    def test_endpoint_exposes_the_search(self):
        from starlette.testclient import TestClient

        import service

        self._named("11111111", "ACME TRADING LIMITED")
        self._named("22222222", "BETA HOLDINGS PLC")
        with TestClient(service.app) as client:
            body = client.get("/jobs?q=beta&include_result=false").json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["jobs"][0]["company_name"], "BETA HOLDINGS PLC")


class MigrationTests(unittest.TestCase):
    """Existing job stores must gain the column and be backfilled from their reports."""

    def test_an_older_store_is_migrated_and_backfilled(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        original = runtime.JOBS_DB
        directory = Path(tempfile.mkdtemp())
        legacy = directory / "jobs.sqlite3"

        # A store written before company_name existed.
        connection = sqlite3.connect(legacy)
        connection.execute(
            "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, crn TEXT NOT NULL,"
            " data_room_path TEXT NOT NULL, status TEXT NOT NULL, submitted_by TEXT NOT NULL,"
            " created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,"
            " trace_id TEXT NOT NULL DEFAULT '', events TEXT NOT NULL DEFAULT '[]',"
            " result TEXT, error TEXT)"
        )
        result = dict(FULL_RESULT)
        result["raw_statutory_data"] = {"profile": {"data": {"company_name": "LEGACY LIMITED"}}}
        connection.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("old-1", "44444444", "", "SUCCEEDED", "tests", "2026-01-01T00:00:00+00:00",
             None, None, "", "[]", json.dumps(result), None),
        )
        connection.commit()
        connection.close()

        runtime.JOBS_DB = legacy
        try:
            self.assertEqual(runtime.count_jobs(query="legacy"), 1)
            job = runtime.list_jobs(include_result=False)[0]
            self.assertEqual(job["company_name"], "LEGACY LIMITED")
        finally:
            runtime.JOBS_DB = original

    def test_migrating_twice_is_harmless(self):
        before = runtime.count_jobs()
        with runtime._db() as connection:
            runtime._migrate(connection)
            runtime._migrate(connection)
        self.assertEqual(runtime.count_jobs(), before)
