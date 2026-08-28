import inspect
import os
import re
import tempfile
from pathlib import Path
import unittest

from pydantic import ValidationError

import data_room_loader
import mcp_server
from orchestrator import run_due_diligence


class EnvironmentPatch:
    def __init__(self, *names: str):
        self.names = names
        self.previous: dict[str, str | None] = {}

    def __enter__(self):
        for name in self.names:
            self.previous[name] = os.environ.pop(name, None)
        return self

    def __exit__(self, exc_type, exc, tb):
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class CoreBehaviorTests(unittest.TestCase):
    def test_company_query_normalizes_crn(self):
        query = mcp_server.CompanyQuery(crn=" 0399 4971 ")
        self.assertEqual(query.crn, "03994971")

    def test_company_query_rejects_invalid_crn(self):
        with self.assertRaises(ValidationError):
            mcp_server.CompanyQuery(crn="123")

    def test_missing_companies_house_key_returns_config_status(self):
        with EnvironmentPatch("COMPANIES_HOUSE_API_KEY"):
            result = mcp_server._make_request("/company/03994971")
        self.assertEqual(result["status"], "config_missing")

    def test_offline_graph_returns_structured_report(self):
        with EnvironmentPatch("COMPANIES_HOUSE_API_KEY", "GEMINI_API_KEY"):
            state = run_due_diligence("03994971", save_artifact=False)

        report = state["red_flag_verdict"]
        self.assertIn(
            report["recommendation"],
            {"GREEN LIGHT", "PROCEED WITH CAUTION", "RED FLAG DEAL BREAKER"},
        )
        self.assertIn("raw_statutory_data", state)

    def test_fallback_evidence_uses_charge_and_filing_details(self):
        bundle = {
            "profile": {
                "status": "success",
                "data": {
                    "company_name": "Example Ltd",
                    "company_status": "active",
                    "accounts": {"next_due": "2026-12-31", "overdue": False},
                },
            },
            "charges": {
                "status": "success",
                "data": {
                    "total_count": 1,
                    "items": [
                        {
                            "id": "abc123",
                            "status": "outstanding",
                            "created_on": "2025-01-01",
                            "classification": {"description": "debenture"},
                        }
                    ],
                },
            },
            "filings": {
                "status": "success",
                "data": {
                    "items": [
                        {
                            "date": "2026-01-15",
                            "category": "accounts",
                            "description": "accounts-with-accounts-type-total-exemption-full",
                        }
                    ]
                },
            },
            "insolvency": {"status": "not_found"},
            "pscs": {"status": "success", "data": {"items": []}},
        }

        from orchestrator import _fallback_financial, _fallback_legal

        legal = _fallback_legal(bundle)
        financial = _fallback_financial(bundle)

        self.assertIn("abc123", legal["risks"][1]["evidentiary_quote"])
        self.assertIn("2026-01-15", financial["findings"][1]["evidentiary_quote"])

    def test_data_room_loader_reads_text_and_csv_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "contract.txt").write_text(
                "This agreement includes a change of control termination clause.",
                encoding="utf-8",
            )
            (root / "financials.csv").write_text(
                "month,revenue,expenses\nJan,100,80\n",
                encoding="utf-8",
            )

            result = data_room_loader.load_data_room(root)

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["documents"]), 2)
        classifications = {doc["file_name"]: doc["classification"] for doc in result["documents"]}
        self.assertEqual(classifications["contract.txt"], "legal")
        self.assertEqual(classifications["financials.csv"], "financial")

    def test_data_room_loader_handles_missing_folder(self):
        result = data_room_loader.load_data_room("__missing_data_room__")
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["documents"], [])

    def test_empty_path_does_not_scan_the_working_directory(self):
        # A blank path must mean "no documents", never "everything under cwd".
        for blank in ("", "   ", ".", "./", None):
            result = data_room_loader.load_data_room(blank)
            self.assertEqual(result["status"], "not_provided")
            self.assertEqual(result["documents"], [])

    def test_statutory_only_run_completes_without_documents(self):
        with EnvironmentPatch("COMPANIES_HOUSE_API_KEY", "GEMINI_API_KEY"):
            state = run_due_diligence("03994971", save_artifact=False, data_room_path="")

        self.assertEqual(state["data_room"]["status"], "not_provided")
        self.assertIn(
            state["red_flag_verdict"]["recommendation"],
            {"GREEN LIGHT", "PROCEED WITH CAUTION", "RED FLAG DEAL BREAKER"},
        )


class DataRoomDefaultTests(unittest.TestCase):
    """An audit must never ingest documents nobody asked it to read.

    The CLI once defaulted to "data_room", which is the folder uploads land in.
    A run for one company therefore ingested every document every caller had
    ever uploaded, for any company.
    """

    def test_cli_defaults_to_no_documents(self):
        import orchestrator

        declaration = re.search(
            r'"--data-room",\s*default="([^"]*)"',
            inspect.getsource(orchestrator),
        )
        self.assertIsNotNone(declaration, "could not find the --data-room argument")
        self.assertEqual(
            declaration.group(1),
            "",
            "the CLI must not default to a folder holding other callers' uploads",
        )

    def test_an_empty_path_reads_nothing(self):
        import data_room_loader

        result = data_room_loader.load_data_room("")
        self.assertEqual(result.get("documents", []), [])
        self.assertNotEqual(result.get("status"), "success")

    def test_a_named_folder_still_loads(self):
        import data_room_loader

        result = data_room_loader.load_data_room("fixtures/deal_documents")
        names = [doc.get("file_name") for doc in result.get("documents", [])]
        self.assertIn("contract_summary.txt", names)
        # The folder's own README explains the fixture; it is not deal material.
        self.assertNotIn("README.md", names)


class ContractFindingPriorityTests(unittest.TestCase):
    """The cap on contract findings must drop the least serious, not the last.

    Semantic matches arrive ordered by similarity, so a LOW clause matched at 0.84
    used to displace a MEDIUM one matched at 0.75 — and change of control is not
    something a diligence report should lose to an auto-renewal.
    """

    def _room(self, clauses):
        return {
            "status": "success",
            "documents": [],
            "semantic_clauses": {
                "matches": [
                    {
                        "file_name": "contract.txt",
                        "clause": clause,
                        "similarity": similarity,
                        "excerpt": f"...{clause}...",
                    }
                    for clause, similarity in clauses
                ]
            },
        }

    def test_a_more_serious_clause_survives_the_cap(self):
        from orchestrator import _data_room_findings

        # Five LOW clauses matched more strongly than the one MEDIUM.
        room = self._room([
            ("Auto-Renewal", 0.90),
            ("Exclusivity", 0.89),
            ("Non-Compete", 0.88),
            ("Liquidated Damages", 0.87),
            ("Governing Law", 0.86),
            ("Change of Control", 0.75),
        ])
        kept = _data_room_findings(room, limit=3)
        severities = [finding["severity"] for finding in kept]

        self.assertEqual(len(kept), 3)
        self.assertIn("MEDIUM", severities, "the MEDIUM clause must not be cut for being less similar")
        self.assertEqual(severities[0], "MEDIUM", "findings should be ordered most serious first")

    def test_similarity_order_is_kept_within_one_severity(self):
        from orchestrator import _data_room_findings

        room = self._room([
            ("Exclusivity", 0.90),
            ("Non-Compete", 0.80),
            ("Auto-Renewal", 0.70),
        ])
        kept = _data_room_findings(room)
        clauses = [finding["category"] for finding in kept]
        self.assertEqual(
            clauses,
            [
                "Contract Term: Exclusivity",
                "Contract Term: Non-Compete",
                "Contract Term: Auto-Renewal",
            ],
        )

if __name__ == "__main__":
    unittest.main()
