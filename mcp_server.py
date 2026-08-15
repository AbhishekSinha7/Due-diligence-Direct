import os
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from fastmcp import FastMCP
from pydantic import BaseModel, Field, field_validator

import accounts_parser

load_dotenv()

mcp = FastMCP("CompaniesHouseFinancialServer")

BASE_URL = os.getenv(
    "COMPANIES_HOUSE_BASE_URL",
    "https://api.company-information.service.gov.uk",
).rstrip("/")
DOCUMENT_API_HOST = "document-api.company-information.service.gov.uk"
REQUEST_TIMEOUT_SECONDS = float(os.getenv("COMPANIES_HOUSE_TIMEOUT_SECONDS", "20"))
DOCUMENT_TIMEOUT_SECONDS = float(os.getenv("COMPANIES_HOUSE_DOCUMENT_TIMEOUT_SECONDS", "60"))
MAX_DOCUMENT_BYTES = int(os.getenv("COMPANIES_HOUSE_MAX_DOCUMENT_BYTES", str(8 * 1024 * 1024)))

# The document content endpoint 302s to object storage. Redirects are followed
# manually so the destination host is checked and the API credential is never
# replayed to a third-party host.
ALLOWED_DOCUMENT_HOSTS = (DOCUMENT_API_HOST,)
ALLOWED_REDIRECT_SUFFIXES = (
    ".amazonaws.com",
    ".company-information.service.gov.uk",
)


class CompanyQuery(BaseModel):
    crn: str = Field(
        ...,
        description="UK Company Registration Number, 8 characters e.g. '03994971'",
    )

    @field_validator("crn")
    @classmethod
    def normalize_crn(cls, value: str) -> str:
        crn = "".join(str(value).strip().split()).upper()
        if len(crn) != 8 or not crn.isalnum():
            raise ValueError("CRN must be exactly 8 alphanumeric characters.")
        return crn


def _api_key() -> str:
    return os.getenv("COMPANIES_HOUSE_API_KEY", "").strip()


def _make_request(endpoint: str) -> dict[str, Any]:
    api_key = _api_key()
    if not api_key or api_key in {"YOUR_API_KEY", "your_actual_companies_house_key"}:
        return {
            "status": "config_missing",
            "message": "Set COMPANIES_HOUSE_API_KEY in .env to query Companies House.",
            "endpoint": endpoint,
        }

    url = f"{BASE_URL}{endpoint}"
    try:
        response = requests.get(
            url,
            auth=(api_key, ""),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.Timeout:
        return {
            "status": "timeout",
            "message": "Companies House request timed out.",
            "endpoint": endpoint,
        }
    except requests.RequestException as exc:
        return {
            "status": "network_error",
            "message": str(exc),
            "endpoint": endpoint,
        }

    if response.status_code == 200:
        return {
            "status": "success",
            "endpoint": endpoint,
            "data": response.json(),
        }
    if response.status_code == 404:
        return {
            "status": "not_found",
            "endpoint": endpoint,
            "message": "No statutory records found for this endpoint.",
        }

    return {
        "status": "error",
        "endpoint": endpoint,
        "code": response.status_code,
        "error": response.text[:1000],
    }


@mcp.tool()
def get_company_overview(input_data: CompanyQuery) -> dict[str, Any]:
    """Fetch general profile, incorporation date, status, and SIC codes."""
    return _make_request(f"/company/{input_data.crn}")


@mcp.tool()
def get_insolvency_records(input_data: CompanyQuery) -> dict[str, Any]:
    """Check for active or past winding-up orders, liquidations, and administrations."""
    return _make_request(f"/company/{input_data.crn}/insolvency")


@mcp.tool()
def get_company_charges(input_data: CompanyQuery) -> dict[str, Any]:
    """Fetch registered mortgages, debentures, and floating charges."""
    return _make_request(f"/company/{input_data.crn}/charges")


@mcp.tool()
def get_significant_controllers(input_data: CompanyQuery) -> dict[str, Any]:
    """Fetch Persons with Significant Control and ultimate beneficial owners."""
    return _make_request(f"/company/{input_data.crn}/persons-with-significant-control")


@mcp.tool()
def get_filing_history(input_data: CompanyQuery) -> dict[str, Any]:
    """Fetch recent statutory filings, prioritizing accounts filings."""
    accounts = _make_request(
        f"/company/{input_data.crn}/filing-history?category=accounts&items_per_page=10"
    )
    if accounts["status"] == "success":
        return accounts

    fallback = _make_request(f"/company/{input_data.crn}/filing-history?items_per_page=10")
    if fallback["status"] == "success":
        return {
            **fallback,
            "status": "fallback_success",
            "primary_accounts_result": accounts,
        }
    return accounts


def _fetch_document(url: str, accept: str) -> dict[str, Any]:
    """Fetch a Companies House document, following the storage redirect safely."""

    api_key = _api_key()
    if not api_key:
        return {"status": "config_missing", "message": "COMPANIES_HOUSE_API_KEY is not set."}

    host = urlparse(url).hostname or ""
    if host not in ALLOWED_DOCUMENT_HOSTS:
        return {"status": "blocked", "message": f"Document host {host} is not allowed."}

    try:
        response = requests.get(
            url,
            auth=(api_key, ""),
            headers={"Accept": accept},
            timeout=DOCUMENT_TIMEOUT_SECONDS,
            allow_redirects=False,
            stream=True,
        )

        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location", "")
            redirect_host = urlparse(location).hostname or ""
            if not any(
                redirect_host == suffix.lstrip(".") or redirect_host.endswith(suffix)
                for suffix in ALLOWED_REDIRECT_SUFFIXES
            ):
                return {
                    "status": "blocked",
                    "message": f"Document redirect to {redirect_host} is not on the allowlist.",
                }
            response.close()
            # No credential is sent to storage; the signed URL carries its own auth.
            response = requests.get(location, timeout=DOCUMENT_TIMEOUT_SECONDS, stream=True)

        if response.status_code != 200:
            body = response.text[:500]
            response.close()
            return {"status": "error", "code": response.status_code, "error": body}

        content = response.raw.read(MAX_DOCUMENT_BYTES + 1, decode_content=True)
        truncated = len(content) > MAX_DOCUMENT_BYTES
        response.close()
        return {
            "status": "success",
            "content_type": response.headers.get("content-type", ""),
            "bytes": len(content),
            "truncated": truncated,
            "text": content[:MAX_DOCUMENT_BYTES].decode("utf-8", errors="replace"),
        }
    except requests.Timeout:
        return {"status": "timeout", "message": "Document download timed out."}
    except requests.RequestException as exc:
        return {"status": "network_error", "message": str(exc)}


@mcp.tool()
def get_accounts_filings(input_data: CompanyQuery, limit: int = 3) -> dict[str, Any]:
    """List recent statutory accounts filings and their document metadata links."""

    history = _make_request(
        f"/company/{input_data.crn}/filing-history?category=accounts&items_per_page=10"
    )
    if history["status"] != "success":
        return history

    filings: list[dict[str, Any]] = []
    for item in history.get("data", {}).get("items", []):
        if not isinstance(item, dict):
            continue
        filings.append(
            {
                "date": item.get("date"),
                "type": item.get("type"),
                "description": item.get("description"),
                "made_up_date": (item.get("description_values") or {}).get("made_up_date"),
                "transaction_id": item.get("transaction_id"),
                "document_metadata": (item.get("links") or {}).get("document_metadata"),
            }
        )
        if len(filings) >= limit:
            break

    return {"status": "success", "endpoint": history["endpoint"], "filings": filings}


@mcp.tool()
def analyze_statutory_accounts(input_data: CompanyQuery, max_filings: int = 2) -> dict[str, Any]:
    """Download filed accounts and compute balance sheet metrics deterministically.

    Figures come from the iXBRL tags in the company's own filing and are turned
    into ratios by `accounts_parser`, not by a language model.
    """

    listing = get_accounts_filings(input_data, limit=max_filings)
    if listing["status"] != "success":
        return {"status": listing["status"], "message": listing.get("message", ""), "filings": []}

    analyses: list[dict[str, Any]] = []
    for filing in listing["filings"]:
        metadata_url = filing.get("document_metadata")
        record: dict[str, Any] = {
            "filing_date": filing.get("date"),
            "description": filing.get("description"),
            "made_up_date": filing.get("made_up_date"),
            "transaction_id": filing.get("transaction_id"),
        }
        if not metadata_url:
            record.update({"status": "no_document", "message": "Filing has no downloadable document."})
            analyses.append(record)
            continue

        metadata = _fetch_document(metadata_url, "application/json")
        if metadata["status"] != "success":
            record.update({"status": metadata["status"], "message": metadata.get("message", "")})
            analyses.append(record)
            continue

        try:
            import json as _json

            meta = _json.loads(metadata["text"])
        except ValueError:
            record.update({"status": "metadata_unreadable"})
            analyses.append(record)
            continue

        resources = meta.get("resources", {}) if isinstance(meta.get("resources"), dict) else {}
        content_url = (meta.get("links") or {}).get("document", f"{metadata_url}/content")
        record["document_id"] = str(metadata_url).rsplit("/", 1)[-1]
        record["available_formats"] = sorted(resources)
        record["pages"] = meta.get("pages")

        if "application/xhtml+xml" not in resources:
            record.update(
                {
                    "status": "no_ixbrl_available",
                    "message": (
                        "This filing is only published as a scanned or PDF document, so tagged "
                        "figures cannot be extracted. Manual review of the filing is required."
                    ),
                    "document_url": content_url,
                }
            )
            analyses.append(record)
            continue

        document = _fetch_document(content_url, "application/xhtml+xml")
        if document["status"] != "success":
            record.update({"status": document["status"], "message": document.get("message", "")})
            analyses.append(record)
            continue

        analysis = accounts_parser.analyze_document(document["text"])
        record.update(
            {
                "status": analysis["status"],
                "source": "companies_house_ixbrl",
                "document_url": content_url,
                "document_bytes": document["bytes"],
                "analysis": analysis,
            }
        )
        analyses.append(record)

    successful = [item for item in analyses if item.get("status") == "success"]
    return {
        "status": "success" if successful else "no_parseable_accounts",
        "crn": input_data.crn,
        "filings_examined": len(analyses),
        "latest": successful[0] if successful else None,
        "filings": analyses,
    }


@mcp.tool()
def collect_company_records(input_data: CompanyQuery) -> dict[str, Any]:
    """Collect the statutory record bundle used by the due diligence graph."""
    return {
        "profile": get_company_overview(input_data),
        "insolvency": get_insolvency_records(input_data),
        "charges": get_company_charges(input_data),
        "filings": get_filing_history(input_data),
        "pscs": get_significant_controllers(input_data),
    }


if __name__ == "__main__":
    mcp.run()
