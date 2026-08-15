"""Deterministic iXBRL accounts parser and financial mathematics.

Companies House publishes statutory accounts as inline XBRL (iXBRL). Every figure
on the balance sheet is machine-tagged, so the financials do not need to be read
by a language model at all - and must not be. This module extracts the tagged
facts and computes the ratios in plain Python, so the numbers in a Red Flag Report
are arithmetic over a government filing rather than model output.

The parser is intentionally dependency-free and namespace-tolerant: FRS 102 and
FRS 105 filings use different prefixes (`core:`, `ns5:`, `uk-bus:`), so facts are
matched on local name, case-insensitively.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

# Balance sheet concepts, by iXBRL local name. Order matters: the first tag found
# for a metric wins, so preferred spellings are listed first.
CONCEPTS: dict[str, tuple[str, ...]] = {
    "turnover": ("TurnoverRevenue", "Revenue", "TurnoverGrossOperatingRevenue"),
    "gross_profit": ("GrossProfitLoss",),
    "operating_profit": ("OperatingProfitLoss",),
    "profit_before_tax": ("ProfitLossOnOrdinaryActivitiesBeforeTax", "ProfitLossBeforeTax"),
    "profit_loss": ("ProfitLoss", "ProfitLossForPeriod"),
    "fixed_assets": ("FixedAssets", "TotalFixedAssets"),
    "current_assets": ("CurrentAssets", "TotalCurrentAssets"),
    "cash": ("CashBankOnHand", "CashBankInHand", "CashCashEquivalents"),
    "debtors": ("Debtors", "TotalDebtors"),
    "stocks": ("Stocks", "StocksInventory", "Inventories"),
    "creditors": ("Creditors",),
    "net_current_assets": ("NetCurrentAssetsLiabilities", "NetCurrentAssets"),
    "total_assets_less_current_liabilities": ("TotalAssetsLessCurrentLiabilities",),
    "net_assets": ("NetAssetsLiabilities", "NetAssets", "NetAssetsLiabilitiesIncludingPensionAssetLiability"),
    "equity": ("Equity", "ShareholderFunds", "TotalShareholdersFunds"),
    "share_capital": ("CalledUpShareCapital", "ShareCapital"),
    "employees": ("AverageNumberEmployeesDuringPeriod", "NumberEmployees"),
}

# Creditors are dimension-split by maturity; these markers appear in the context id
# or in the dimension member of the context.
WITHIN_ONE_YEAR = "withinoneyear"
AFTER_ONE_YEAR = "afteroneyear"

NUMERIC_TAG = "ix:nonfraction"
TEXT_TAG = "ix:nonnumeric"
CONTEXT_TAG = "xbrli:context"


@dataclass
class Fact:
    concept: str
    local_name: str
    value: float
    context_ref: str
    unit: str = ""
    dimension: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "concept": self.concept,
            "tag": self.local_name,
            "value": self.value,
            "context": self.context_ref,
            "unit": self.unit,
            "dimension": self.dimension,
        }


@dataclass
class Period:
    context_ref: str
    date: str = ""
    start_date: str = ""
    end_date: str = ""
    dimension: str = ""


@dataclass
class ParsedAccounts:
    facts: list[Fact] = field(default_factory=list)
    periods: dict[str, Period] = field(default_factory=dict)
    entity_name: str = ""
    company_number: str = ""
    balance_sheet_date: str = ""
    errors: list[str] = field(default_factory=list)


def _local_name(qualified: str) -> str:
    return qualified.split(":")[-1].strip()


def parse_ixbrl_number(raw: str, *, scale: str = "", sign: str = "", number_format: str = "") -> float | None:
    """Convert a formatted iXBRL numeric string into a signed float."""

    text = re.sub(r"<[^>]+>", "", raw or "")
    text = text.replace("\xa0", " ").strip()
    text = re.sub(r"[£$€\s]", "", text)
    if not text:
        return None

    # ixt:zerodash and friends render zero as a dash.
    if text in {"-", "–", "—", "‐"}:
        return 0.0

    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]

    fmt = number_format.lower()
    if "dotcomma" in fmt:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "")

    if text.startswith("-"):
        negative = True
        text = text[1:]

    try:
        value = float(text)
    except ValueError:
        return None

    try:
        if scale:
            value *= 10 ** int(scale)
    except ValueError:
        pass

    if sign == "-":
        negative = True
    return -value if negative else value


class _IxbrlParser(HTMLParser):
    """Streaming reader for the small subset of iXBRL that balance sheets use."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.result = ParsedAccounts()
        self._numeric_stack: list[dict[str, Any]] = []
        self._text_capture: dict[str, Any] | None = None
        self._context: dict[str, Any] | None = None
        self._context_field: str | None = None

    # -- iXBRL facts -----------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: (value or "") for key, value in attrs}

        if tag == NUMERIC_TAG:
            self._numeric_stack.append({"attrs": attributes, "text": []})
            return
        if tag == TEXT_TAG:
            self._text_capture = {"attrs": attributes, "text": []}
            return
        if tag == CONTEXT_TAG:
            self._context = {"id": attributes.get("id", ""), "dimension": ""}
            return

        if self._context is not None:
            if tag in {"xbrli:instant", "xbrli:startdate", "xbrli:enddate"}:
                self._context_field = tag
            elif tag == "xbrldi:explicitmember":
                self._context_field = tag

        if self._numeric_stack:
            self._numeric_stack[-1]["text"].append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag == NUMERIC_TAG and self._numeric_stack:
            self._finish_numeric(self._numeric_stack.pop())
            return
        if tag == TEXT_TAG and self._text_capture is not None:
            self._finish_text(self._text_capture)
            self._text_capture = None
            return
        if tag == CONTEXT_TAG and self._context is not None:
            self._finish_context(self._context)
            self._context = None
            return
        if self._context is not None and tag == self._context_field:
            self._context_field = None

    def handle_data(self, data: str) -> None:
        if self._numeric_stack:
            self._numeric_stack[-1]["text"].append(data)
        if self._text_capture is not None:
            self._text_capture["text"].append(data)
        if self._context is not None and self._context_field:
            value = data.strip()
            if not value:
                return
            if self._context_field == "xbrli:instant":
                self._context["date"] = value
            elif self._context_field == "xbrli:startdate":
                self._context["start_date"] = value
            elif self._context_field == "xbrli:enddate":
                self._context["end_date"] = value
            elif self._context_field == "xbrldi:explicitmember":
                self._context["dimension"] = _local_name(value)

    # -- element completion ---------------------------------------------
    def _finish_numeric(self, element: dict[str, Any]) -> None:
        attributes = element["attrs"]
        local = _local_name(attributes.get("name", ""))
        if not local:
            return

        concept = ""
        for candidate, tags in CONCEPTS.items():
            if any(local.lower() == tag.lower() for tag in tags):
                concept = candidate
                break
        if not concept:
            return

        value = parse_ixbrl_number(
            "".join(element["text"]),
            scale=attributes.get("scale", ""),
            sign=attributes.get("sign", ""),
            number_format=attributes.get("format", ""),
        )
        if value is None:
            self.result.errors.append(f"Unparseable value for {local}")
            return

        self.result.facts.append(
            Fact(
                concept=concept,
                local_name=local,
                value=value,
                context_ref=attributes.get("contextref", ""),
                unit=attributes.get("unitref", ""),
            )
        )

    def _finish_text(self, element: dict[str, Any]) -> None:
        local = _local_name(element["attrs"].get("name", "")).lower()
        text = re.sub(r"\s+", " ", "".join(element["text"])).strip()
        if not text:
            return
        if local == "entitycurrentlegalorregisteredname":
            self.result.entity_name = text
        elif local == "ukcompanieshouseregisterednumber":
            self.result.company_number = text
        elif local == "balancesheetdate":
            self.result.balance_sheet_date = text

    def _finish_context(self, context: dict[str, Any]) -> None:
        context_id = context.get("id", "")
        if not context_id:
            return
        self.result.periods[context_id] = Period(
            context_ref=context_id,
            date=context.get("date", ""),
            start_date=context.get("start_date", ""),
            end_date=context.get("end_date", ""),
            dimension=context.get("dimension", ""),
        )


def parse_accounts(document: str) -> ParsedAccounts:
    """Parse an iXBRL accounts document into tagged facts and contexts."""

    parser = _IxbrlParser()
    try:
        parser.feed(document)
        parser.close()
    except Exception as exc:  # malformed filing should degrade, not crash the fleet
        parser.result.errors.append(f"{exc.__class__.__name__}: {exc}")
    return parser.result


def _period_end(parsed: ParsedAccounts, context_ref: str) -> str:
    period = parsed.periods.get(context_ref)
    if period is None:
        return ""
    return period.date or period.end_date or period.start_date


def _maturity(parsed: ParsedAccounts, context_ref: str) -> str:
    period = parsed.periods.get(context_ref)
    haystack = f"{context_ref} {period.dimension if period else ''}".lower()
    if WITHIN_ONE_YEAR in haystack:
        return "within_one_year"
    if AFTER_ONE_YEAR in haystack:
        return "after_one_year"
    return "unspecified"


def build_period_view(parsed: ParsedAccounts) -> list[dict[str, Any]]:
    """Group facts by balance sheet date, newest first."""

    grouped: dict[str, dict[str, Any]] = {}

    for fact in parsed.facts:
        date = _period_end(parsed, fact.context_ref)
        if not date:
            continue
        bucket = grouped.setdefault(date, {"period_end": date, "metrics": {}, "evidence": {}})

        key = fact.concept
        if fact.concept == "creditors":
            maturity = _maturity(parsed, fact.context_ref)
            if maturity == "unspecified":
                key = "creditors_total"
            else:
                key = f"creditors_{maturity}"

        if key in bucket["metrics"]:
            continue
        bucket["metrics"][key] = fact.value
        bucket["evidence"][key] = f"{fact.local_name}@{fact.context_ref}"

    return sorted(grouped.values(), key=lambda item: item["period_end"], reverse=True)


def _tolerance(*values: float) -> float:
    """Rounding slack proportional to the magnitudes being compared."""

    return max(1.0, 0.005 * max((abs(value) for value in values), default=0.0))


def reconcile_period(metrics: dict[str, Any]) -> list[dict[str, Any]]:
    """Check the balance sheet identities that a well-formed filing must satisfy.

    Small-company filings are self-tagged and frequently contain inconsistent
    tags. Every ratio downstream is only as good as these identities, so they are
    checked explicitly rather than assumed.
    """

    checks: list[dict[str, Any]] = []

    def check(name: str, expected: float, actual: float, formula: str) -> None:
        difference = expected - actual
        checks.append(
            {
                "identity": name,
                "formula": formula,
                "expected": round(expected, 2),
                "reported": round(actual, 2),
                "difference": round(difference, 2),
                "consistent": abs(difference) <= _tolerance(expected, actual),
            }
        )

    current_assets = metrics.get("current_assets")
    creditors_short = metrics.get("creditors_within_one_year", metrics.get("creditors_total"))
    net_current = metrics.get("net_current_assets")
    fixed_assets = metrics.get("fixed_assets")
    total_less_current = metrics.get("total_assets_less_current_liabilities")
    creditors_long = metrics.get("creditors_after_one_year")
    net_assets = metrics.get("net_assets")
    equity = metrics.get("equity")

    if current_assets is not None and creditors_short is not None and net_current is not None:
        # Creditors may be filed signed or unsigned; compare against the deduction.
        check(
            "working_capital",
            current_assets - abs(creditors_short),
            net_current,
            "current_assets - creditors_within_one_year = net_current_assets",
        )
    if net_current is not None and total_less_current is not None:
        check(
            "total_assets_less_current_liabilities",
            net_current + (fixed_assets or 0.0),
            total_less_current,
            "net_current_assets + fixed_assets = total_assets_less_current_liabilities",
        )
    if total_less_current is not None and net_assets is not None:
        check(
            "net_assets",
            total_less_current - abs(creditors_long or 0.0),
            net_assets,
            "total_assets_less_current_liabilities - creditors_after_one_year = net_assets",
        )
    if net_assets is not None and equity is not None:
        check("balance_sheet_balances", net_assets, equity, "net_assets = equity")

    return checks


def _months_between(later: str, earlier: str) -> float | None:
    pattern = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
    first, second = pattern.match(later or ""), pattern.match(earlier or "")
    if not first or not second:
        return None
    months = (int(first.group(1)) - int(second.group(1))) * 12 + (int(first.group(2)) - int(second.group(2)))
    months += (int(first.group(3)) - int(second.group(3))) / 30.0
    return round(months, 2) if months > 0 else None


def compute_metrics(parsed: ParsedAccounts) -> dict[str, Any]:
    """Compute solvency, liquidity, and cash trend figures in plain Python."""

    periods = build_period_view(parsed)
    if not periods:
        return {
            "status": "no_tagged_figures",
            "message": "The filing contained no recognised iXBRL balance sheet tags.",
            "periods": [],
            "derived": {},
            "signals": [],
            "parse_errors": parsed.errors,
        }

    for period in periods:
        metrics = period["metrics"]
        current_assets = metrics.get("current_assets")
        creditors_short = metrics.get("creditors_within_one_year") or metrics.get("creditors_total")

        period["reconciliation"] = reconcile_period(metrics)
        failed = [check for check in period["reconciliation"] if not check["consistent"]]
        period["internally_consistent"] = not failed

        if current_assets is not None and creditors_short:
            magnitude = abs(creditors_short)
            metrics["current_ratio"] = round(current_assets / magnitude, 3) if magnitude else None
        if current_assets is not None and creditors_short is not None:
            metrics.setdefault(
                "working_capital",
                metrics.get("net_current_assets", current_assets - abs(creditors_short)),
            )

    latest = periods[0]
    previous = periods[1] if len(periods) > 1 else None
    derived: dict[str, Any] = {
        "latest_period_end": latest["period_end"],
        "previous_period_end": previous["period_end"] if previous else None,
    }
    signals: list[dict[str, Any]] = []

    net_assets = latest["metrics"].get("net_assets")
    if net_assets is not None:
        derived["net_assets"] = net_assets
        if net_assets < 0:
            signals.append(
                {
                    "code": "negative_net_assets",
                    "severity": "HIGH",
                    "detail": f"Balance sheet is insolvent on a net assets basis: {net_assets:,.0f} at {latest['period_end']}.",
                    "evidence": latest["evidence"].get("net_assets", "net_assets"),
                }
            )

    failed_checks = [check for check in latest.get("reconciliation", []) if not check["consistent"]]
    derived["internally_consistent"] = not failed_checks
    if failed_checks:
        # The filing contradicts itself. Report that as the finding and refuse to
        # derive liquidity conclusions from figures that do not add up.
        names = ", ".join(check["identity"] for check in failed_checks)
        signals.append(
            {
                "code": "filing_internally_inconsistent",
                "severity": "MEDIUM",
                "detail": (
                    f"The filed accounts fail {len(failed_checks)} balance sheet identity check(s) ({names}). "
                    "Liquidity ratios are unreliable for this filing and require manual verification "
                    "against the source document."
                ),
                "evidence": "; ".join(
                    f"{check['formula']} -> expected {check['expected']:,.0f}, filed {check['reported']:,.0f}"
                    for check in failed_checks
                ),
            }
        )

    current_ratio = latest["metrics"].get("current_ratio")
    if current_ratio is not None:
        derived["current_ratio"] = current_ratio
        derived["current_ratio_reliable"] = not failed_checks
    if current_ratio is not None and not failed_checks:
        if current_ratio < 1:
            signals.append(
                {
                    "code": "current_ratio_below_one",
                    "severity": "HIGH",
                    "detail": f"Current liabilities exceed current assets (current ratio {current_ratio}).",
                    "evidence": latest["evidence"].get("current_assets", "current_assets"),
                }
            )
        elif current_ratio < 1.2:
            signals.append(
                {
                    "code": "thin_liquidity",
                    "severity": "MEDIUM",
                    "detail": f"Liquidity is thin (current ratio {current_ratio}).",
                    "evidence": latest["evidence"].get("current_assets", "current_assets"),
                }
            )

    if previous:
        months = _months_between(latest["period_end"], previous["period_end"])
        derived["months_between_periods"] = months

        for metric in ("net_assets", "cash", "current_assets", "turnover", "profit_loss"):
            new_value = latest["metrics"].get(metric)
            old_value = previous["metrics"].get(metric)
            if new_value is None or old_value is None:
                continue
            change = new_value - old_value
            derived[f"{metric}_change"] = change
            if old_value:
                derived[f"{metric}_change_pct"] = round(change / abs(old_value) * 100, 2)

        cash_change = derived.get("cash_change")
        if cash_change is not None and months:
            monthly = cash_change / months
            derived["monthly_cash_movement"] = round(monthly, 2)
            latest_cash = latest["metrics"].get("cash")
            if monthly < 0 and latest_cash:
                runway = latest_cash / abs(monthly)
                derived["cash_runway_months"] = round(runway, 1)
                if runway < 12:
                    signals.append(
                        {
                            "code": "short_cash_runway",
                            "severity": "HIGH" if runway < 6 else "MEDIUM",
                            "detail": (
                                f"Cash fell {abs(cash_change):,.0f} over {months} month(s); at that rate the "
                                f"{latest_cash:,.0f} balance covers about {runway:.1f} month(s)."
                            ),
                            "evidence": f"{latest['evidence'].get('cash', 'cash')} vs {previous['evidence'].get('cash', 'cash')}",
                        }
                    )

        net_assets_change = derived.get("net_assets_change")
        if net_assets_change is not None and net_assets_change < 0:
            signals.append(
                {
                    "code": "net_assets_declining",
                    "severity": "MEDIUM",
                    "detail": f"Net assets fell by {abs(net_assets_change):,.0f} between {previous['period_end']} and {latest['period_end']}.",
                    "evidence": latest["evidence"].get("net_assets", "net_assets"),
                }
            )

    return {
        "status": "success",
        "entity_name": parsed.entity_name,
        "company_number": parsed.company_number,
        "balance_sheet_date": parsed.balance_sheet_date or latest["period_end"],
        "periods": periods,
        "derived": derived,
        "signals": signals,
        "fact_count": len(parsed.facts),
        "parse_errors": parsed.errors,
    }


def analyze_document(document: str) -> dict[str, Any]:
    """Parse one iXBRL accounts document and compute its metrics."""

    return compute_metrics(parse_accounts(document))
