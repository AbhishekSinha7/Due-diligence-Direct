"""The same audit over plain HTTP, with no client library.

Read this one if you are integrating from a language other than Python: it is
the whole protocol, and it is four requests.

    set FLEET_API_URL=https://your-service.run.app
    set FLEET_API_KEY=ddd_v1....
    python examples/plain_http.py "third party formations"
"""

import os
import sys
import time

import requests

BASE = os.environ["FLEET_API_URL"].rstrip("/")
HEADERS = {"x-fleet-api-key": os.environ.get("FLEET_API_KEY", "")}
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}


def resolve(term: str) -> str:
    """1. Turn a name or number into a company number."""

    response = requests.get(
        f"{BASE}/companies/search", params={"q": term, "limit": 5}, headers=HEADERS, timeout=30
    )
    response.raise_for_status()
    results = response.json().get("results", [])
    if not results:
        sys.exit(f"No company matched {term!r}.")
    for item in results:
        print(f"  {item['company_number']}  {item['title']}")
    return results[0]["company_number"]


def submit(crn: str) -> str:
    """2. Queue the audit. Returns at once; the run continues server-side."""

    response = requests.post(
        f"{BASE}/jobs",
        json={"crn": crn, "submitted_by": "plain-http-example"},
        headers=HEADERS,
        timeout=30,
    )
    if response.status_code == 403:
        sys.exit("This key lacks the audits:write scope.")
    if response.status_code == 429:
        sys.exit(f"Budget spent. Retry after {response.headers.get('retry-after')}s.")
    response.raise_for_status()
    return response.json()["job_id"]


def wait(job_id: str) -> dict:
    """3. Poll until the job finishes, printing progress as it appears."""

    seen = 0
    while True:
        response = requests.get(f"{BASE}/jobs/{job_id}", headers=HEADERS, timeout=30)
        response.raise_for_status()
        job = response.json()

        events = job.get("events", [])
        for event in events[seen:]:
            print(f"  {event.get('message', '')}")
        seen = len(events)

        if job["status"] in TERMINAL:
            return job
        time.sleep(2)


term = sys.argv[1] if len(sys.argv) > 1 else "03994971"

print(f"Searching for {term!r}...")
crn = resolve(term)

print(f"\nAuditing {crn}...")
job = wait(submit(crn))

if job["status"] != "SUCCEEDED":
    sys.exit(f"\nAudit {job['status']}: {job.get('error') or 'no detail recorded'}")

# 4. The report is on the finished job. Nothing else to fetch.
report = job["result"]
verdict = report["red_flag_verdict"]
profile = report["raw_statutory_data"]["profile"]["data"]

print(f"\n{verdict['recommendation']}: {profile.get('company_name')}")
print(verdict["executive_summary"])

for risk in verdict.get("top_risks", []):
    print(f"  - {risk}")

# A PDF of the same report, if you want one to file.
pdf = requests.get(f"{BASE}/jobs/{job['job_id']}/report.pdf", headers=HEADERS, timeout=60)
if pdf.ok:
    with open(f"{crn}.pdf", "wb") as handle:
        handle.write(pdf.content)
    print(f"\nWrote {crn}.pdf")
