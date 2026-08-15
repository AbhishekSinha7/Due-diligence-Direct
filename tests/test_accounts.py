"""Tests for deterministic iXBRL accounts extraction and financial mathematics.

The fixtures below are minimal iXBRL documents in the shape Companies House
publishes. They exercise the parser, not any company's real position.
"""

import unittest

import accounts_parser

CONSISTENT_FILING = """
<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:xbrli="http://www.xbrl.org/2003/instance">
<body>
<div style="display:none">
  <xbrli:context id="FY_END_20250531">
    <xbrli:entity><xbrli:identifier scheme="ch">01234567</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:instant>2025-05-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="FY_END_20240531">
    <xbrli:entity><xbrli:identifier scheme="ch">01234567</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:instant>2024-05-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="FY_END_20250531_CORE_MATURITIESOREXPIRATIONPERIODSDIMENSION_CORE_WITHINONEYEAR">
    <xbrli:entity><xbrli:identifier scheme="ch">01234567</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:instant>2025-05-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="FY_END_20240531_CORE_MATURITIESOREXPIRATIONPERIODSDIMENSION_CORE_WITHINONEYEAR">
    <xbrli:entity><xbrli:identifier scheme="ch">01234567</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:instant>2024-05-31</xbrli:instant></xbrli:period>
  </xbrli:context>
</div>
<p><ix:nonNumeric contextRef="FY_END_20250531" name="bus:EntityCurrentLegalOrRegisteredName">EXAMPLE TRADING LIMITED</ix:nonNumeric></p>
<p><ix:nonFraction contextRef="FY_END_20250531" name="core:CurrentAssets" unitRef="GBP" format="ixt:numcommadot" decimals="0">120,000</ix:nonFraction></p>
<p><ix:nonFraction contextRef="FY_END_20240531" name="core:CurrentAssets" unitRef="GBP" format="ixt:numcommadot" decimals="0">200,000</ix:nonFraction></p>
<p><ix:nonFraction contextRef="FY_END_20250531_CORE_MATURITIESOREXPIRATIONPERIODSDIMENSION_CORE_WITHINONEYEAR" name="core:Creditors" sign="-" unitRef="GBP" format="ixt:numcommadot" decimals="0">50,000</ix:nonFraction></p>
<p><ix:nonFraction contextRef="FY_END_20240531_CORE_MATURITIESOREXPIRATIONPERIODSDIMENSION_CORE_WITHINONEYEAR" name="core:Creditors" sign="-" unitRef="GBP" format="ixt:numcommadot" decimals="0">40,000</ix:nonFraction></p>
<p><ix:nonFraction contextRef="FY_END_20250531" name="core:NetCurrentAssetsLiabilities" unitRef="GBP" format="ixt:numcommadot" decimals="0">70,000</ix:nonFraction></p>
<p><ix:nonFraction contextRef="FY_END_20240531" name="core:NetCurrentAssetsLiabilities" unitRef="GBP" format="ixt:numcommadot" decimals="0">160,000</ix:nonFraction></p>
<p><ix:nonFraction contextRef="FY_END_20250531" name="core:TotalAssetsLessCurrentLiabilities" unitRef="GBP" format="ixt:numcommadot" decimals="0">70,000</ix:nonFraction></p>
<p><ix:nonFraction contextRef="FY_END_20240531" name="core:TotalAssetsLessCurrentLiabilities" unitRef="GBP" format="ixt:numcommadot" decimals="0">160,000</ix:nonFraction></p>
<p><ix:nonFraction contextRef="FY_END_20250531" name="core:NetAssetsLiabilities" unitRef="GBP" format="ixt:numcommadot" decimals="0">70,000</ix:nonFraction></p>
<p><ix:nonFraction contextRef="FY_END_20240531" name="core:NetAssetsLiabilities" unitRef="GBP" format="ixt:numcommadot" decimals="0">160,000</ix:nonFraction></p>
<p><ix:nonFraction contextRef="FY_END_20250531" name="core:CashBankOnHand" unitRef="GBP" format="ixt:numcommadot" decimals="0">30,000</ix:nonFraction></p>
<p><ix:nonFraction contextRef="FY_END_20240531" name="core:CashBankOnHand" unitRef="GBP" format="ixt:numcommadot" decimals="0">90,000</ix:nonFraction></p>
</body></html>
"""

# Same filing, but the tagged CurrentAssets contradicts NetCurrentAssets, which is a
# common defect in self-tagged small-company accounts.
INCONSISTENT_FILING = CONSISTENT_FILING.replace(
    '<ix:nonFraction contextRef="FY_END_20250531" name="core:CurrentAssets" unitRef="GBP" format="ixt:numcommadot" decimals="0">120,000</ix:nonFraction>',
    '<ix:nonFraction contextRef="FY_END_20250531" name="core:CurrentAssets" unitRef="GBP" format="ixt:numcommadot" decimals="0">1,688</ix:nonFraction>',
)

INSOLVENT_FILING = CONSISTENT_FILING.replace(
    '<ix:nonFraction contextRef="FY_END_20250531" name="core:NetAssetsLiabilities" unitRef="GBP" format="ixt:numcommadot" decimals="0">70,000</ix:nonFraction>',
    '<ix:nonFraction contextRef="FY_END_20250531" name="core:NetAssetsLiabilities" unitRef="GBP" format="ixt:numcommadot" decimals="0" sign="-">70,000</ix:nonFraction>',
)


class NumberParsingTests(unittest.TestCase):
    def test_comma_grouped_number(self):
        self.assertEqual(accounts_parser.parse_ixbrl_number("1,234,567"), 1234567.0)

    def test_sign_attribute_negates(self):
        self.assertEqual(accounts_parser.parse_ixbrl_number("3,870", sign="-"), -3870.0)

    def test_scale_multiplies(self):
        self.assertEqual(accounts_parser.parse_ixbrl_number("12", scale="3"), 12000.0)

    def test_brackets_are_negative(self):
        self.assertEqual(accounts_parser.parse_ixbrl_number("(500)"), -500.0)

    def test_dash_is_zero(self):
        self.assertEqual(accounts_parser.parse_ixbrl_number("-"), 0.0)

    def test_continental_format(self):
        self.assertEqual(
            accounts_parser.parse_ixbrl_number("1.234,50", number_format="ixt:numdotcomma"), 1234.5
        )

    def test_unparseable_returns_none(self):
        self.assertIsNone(accounts_parser.parse_ixbrl_number("not a number"))


class ExtractionTests(unittest.TestCase):
    def test_facts_and_entity_are_extracted(self):
        parsed = accounts_parser.parse_accounts(CONSISTENT_FILING)
        self.assertEqual(parsed.entity_name, "EXAMPLE TRADING LIMITED")
        self.assertTrue(parsed.facts)
        self.assertEqual(parsed.errors, [])

    def test_periods_are_grouped_newest_first(self):
        parsed = accounts_parser.parse_accounts(CONSISTENT_FILING)
        periods = accounts_parser.build_period_view(parsed)
        self.assertEqual([period["period_end"] for period in periods], ["2025-05-31", "2024-05-31"])
        self.assertEqual(periods[0]["metrics"]["current_assets"], 120000.0)
        self.assertEqual(periods[0]["metrics"]["creditors_within_one_year"], -50000.0)

    def test_evidence_records_tag_and_context(self):
        parsed = accounts_parser.parse_accounts(CONSISTENT_FILING)
        periods = accounts_parser.build_period_view(parsed)
        self.assertEqual(
            periods[0]["evidence"]["net_assets"], "NetAssetsLiabilities@FY_END_20250531"
        )

    def test_document_without_tags_degrades(self):
        result = accounts_parser.analyze_document("<html><body>Scanned image only</body></html>")
        self.assertEqual(result["status"], "no_tagged_figures")
        self.assertEqual(result["signals"], [])


class MetricsTests(unittest.TestCase):
    def test_ratios_and_year_on_year_changes(self):
        result = accounts_parser.analyze_document(CONSISTENT_FILING)
        derived = result["derived"]
        self.assertEqual(derived["net_assets"], 70000.0)
        self.assertEqual(derived["current_ratio"], 2.4)
        self.assertEqual(derived["net_assets_change"], -90000.0)
        self.assertEqual(derived["net_assets_change_pct"], -56.25)
        self.assertTrue(derived["internally_consistent"])

    def test_cash_runway_is_computed_from_two_periods(self):
        result = accounts_parser.analyze_document(CONSISTENT_FILING)
        derived = result["derived"]
        # Cash fell 60,000 over 12 months, leaving 30,000: six months of runway.
        self.assertEqual(derived["monthly_cash_movement"], -5000.0)
        self.assertEqual(derived["cash_runway_months"], 6.0)
        codes = {signal["code"] for signal in result["signals"]}
        self.assertIn("short_cash_runway", codes)

    def test_negative_net_assets_is_high_severity(self):
        result = accounts_parser.analyze_document(INSOLVENT_FILING)
        signal = next(s for s in result["signals"] if s["code"] == "negative_net_assets")
        self.assertEqual(signal["severity"], "HIGH")

    def test_inconsistent_filing_suppresses_liquidity_conclusions(self):
        result = accounts_parser.analyze_document(INCONSISTENT_FILING)
        codes = {signal["code"] for signal in result["signals"]}
        self.assertIn("filing_internally_inconsistent", codes)
        # The mis-tagged figure would imply a current ratio of 0.03; it must not be
        # published as a liquidity finding.
        self.assertNotIn("current_ratio_below_one", codes)
        self.assertNotIn("thin_liquidity", codes)
        self.assertFalse(result["derived"]["current_ratio_reliable"])

    def test_reconciliation_reports_the_failing_identity(self):
        result = accounts_parser.analyze_document(INCONSISTENT_FILING)
        checks = result["periods"][0]["reconciliation"]
        failing = [check for check in checks if not check["consistent"]]
        self.assertEqual(len(failing), 1)
        self.assertEqual(failing[0]["identity"], "working_capital")
        self.assertEqual(failing[0]["reported"], 70000.0)

    def test_single_period_filing_has_no_trend_signals(self):
        # Drop every comparative-period fact, leaving one balance sheet date.
        single = "\n".join(
            line
            for line in CONSISTENT_FILING.splitlines()
            if not (line.lstrip().startswith("<p><ix:nonFraction") and "20240531" in line)
        )
        result = accounts_parser.analyze_document(single)
        self.assertIsNone(result["derived"]["previous_period_end"])
        self.assertNotIn("cash_runway_months", result["derived"])


if __name__ == "__main__":
    unittest.main()
