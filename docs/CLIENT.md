# `ddclient` — the DueDiligence Direct client library

The fleet is a governed service, not a library you embed. Audits are submitted,
executed server-side under agent identities, and recorded in one hash-chained
audit trail. `ddclient` is the supported way to drive that service from Python.

It never runs an agent locally, and it never reinterprets what the fleet
returns: every figure in a report was parsed from a document the company filed
at Companies House.

```powershell
python -m pip install requests    # the only dependency
```

The package lives in the repository root, so a clone is importable as-is.

---

## Quick start

```python
from ddclient import DueDiligenceClient

with DueDiligenceClient("https://fleet.example.run.app", access_code="...") as fleet:
    report = fleet.run("03994971")

    print(report.recommendation)          # PROCEED WITH CAUTION
    print(report.company_name)            # THIRD PARTY FORMATIONS LIMITED

    for finding in report.material_findings:
        print(f"[{finding.severity}] {finding.category}: {finding.finding}")
```

`run()` submits the audit, waits for it, and hands back the report. For anything
that should outlive the calling process, use `submit()` and come back later:

```python
job = fleet.submit("03994971")
print(job.job_id)          # the run continues whether or not this process does
...
job = fleet.get_job(job_id)
if job.succeeded:
    report = job.report
```

## Connecting

```python
DueDiligenceClient(
    base_url,              # or FLEET_API_URL
    access_code=None,      # or FLEET_CONSOLE_ACCESS_CODE
    api_key=None,          # or FLEET_API_KEY
    token=None,            # or FLEET_API_TOKEN
    timeout=30.0,
)
```

Ask the operator for a key rather than the shared secret:

```powershell
python api_keys.py issue --name "your-integration" --scopes audits:read audits:write
```

`fleet.whoami()` reports what yours grants and how
much of its hourly budget is spent. A `PolicyDenied` naming a scope means the key
is missing it; a 429 means the budget is spent, and `Retry-After` says for how long.

Credentials resolve in this order: access code (exchanged once for a session
cookie, as the console does), explicit bearer token, Cloud Run metadata identity
token, then the shared API key. On Cloud Run nothing needs to be supplied — the
metadata server mints a token for the target audience automatically, provided
the caller's service account holds the Cloud Run Invoker role.

## Watching a run

`on_event` fires once per new event as it appears, so you never write a polling
loop. Stage events and inter-agent messages arrive on the same stream:

```python
def show(event):
    attributes = event.get("attributes") or {}
    if attributes.get("exchange"):
        print(f"{attributes['sender']} -> {attributes['recipient']}: {event['message']}")
    else:
        print(f"[{event['stage']}] {event['message']}")

report = fleet.run("03994971", on_event=show)
```

`wait_for()` raising `WaitTimeout` is a **client-side** give-up: the audit is
still executing on the fleet. Call `cancel(job_id)` if you actually want it
stopped.

## Deal documents

```python
report = fleet.run("03994971", documents=["nda.pdf", "services-agreement.md"])
```

Documents are uploaded to a fresh data room first. Uploads are untrusted by
definition — the fleet screens them at ingestion and quarantines anything that
tries to instruct the agents. `report.documents_quarantined` tells you if it did.

Accepted: `.csv`, `.md`, `.pdf`, `.txt`. The client rejects anything else before
sending, so a bad extension costs you a round trip rather than a 415.

## Listing the audit history

`GET /jobs` is the whole history, paged. Ask for summaries when you are listing
rather than reading — the difference is 6.6KB versus 312KB for twelve audits.

```python
page = fleet.job_page(limit=25, status="SUCCEEDED", include_result=False)
# `query` matches the audited company by name or number:
page = fleet.job_page(query="formations", include_result=False)
print(f"showing {len(page)} of {page.total}")
for job in page:
    print(job.crn, job.status, job.recommendation, job.duration_seconds)

# Walk everything without writing a paging loop.
for job in fleet.iter_jobs(crn="03994971"):
    print(job.job_id, job.recommendation)

print(fleet.count_jobs(status="FAILED"))
```

A summarised job still answers `.recommendation`, `.company_name` and
`.duration_seconds`; `.report` is `None` until you fetch it with
`fleet.get_job(job_id)`.

## Reading a report

| Accessor | What it gives you |
| --- | --- |
| `.recommendation` | `GREEN LIGHT`, `PROCEED WITH CAUTION`, `RED FLAG DEAL BREAKER` |
| `.executive_summary`, `.top_risks`, `.required_human_review` | the synthesised report |
| `.findings` | every graded finding, most severe first |
| `.material_findings` / `.findings_at("HIGH")` | filtered by severity |
| `.legal_findings`, `.financial_findings` | per agent |
| `.debate_points` | where the agents disagreed, and how it resolved |
| `.reasoning_chain` | every typed message the agents sent each other |
| `.company`, `.officers`, `.pscs`, `.charges`, `.insolvency_cases`, `.filings` | the statutory record, unwrapped |
| `.periods`, `.reconciliation_failures` | filed accounts and their identity checks |
| `.token_usage` | totals and per-call cost |
| `.severity_counts`, `.models_used`, `.used_deterministic_fallback` | governance |
| `.raw` | the complete payload, nothing dropped |

Every accessor tolerates missing keys. A partial or failed run still reads
cleanly rather than raising — which matters, because a run that degraded is
exactly the one you want to inspect.

### The reconciliation check that matters

The fleet verifies four balance-sheet identities against the company's own filed
figures. When one fails, ratios derived from it are **suppressed rather than
reported**, and the failure is surfaced:

```python
for check in report.reconciliation_failures:
    print(f"{check.identity}: expected {check.expected:,.0f}, filed {check.reported:,.0f}")
    print(f"  {check.formula}")
```

This is the difference between a tool that reports a current ratio and one that
tells you the accounts do not add up.

## Errors

```
FleetError
├── TransportError      the control plane could not be reached
├── APIError            it returned an error
│   ├── AuthenticationError   401 — no or bad credential
│   ├── PolicyDenied          403 — the gateway refused: missing scope or tool
│   └── NotFound              404 — no such job, company, or memory record
├── JobFailed           the audit ended in a non-success terminal state
└── WaitTimeout         the client gave up; the run continues server-side
```

## Command line

Everything above is available without writing a script. Connection details come
from flags or the environment.

```powershell
$env:FLEET_API_URL="https://fleet.example.run.app"

python -m ddclient search third party formations
python -m ddclient audit 03994971 --watch --pdf report.pdf
python -m ddclient audit 03994971 -d nda.pdf -d services-agreement.md --watch
python -m ddclient job job-20260822T190123-a4a7a0 -v
python -m ddclient jobs -n 10
python -m ddclient jobs --all --status FAILED
python -m ddclient jobs --search formations
python -m ddclient jobs --crn 03994971 --offset 25
python -m ddclient memory 03994971 --note "Seller disclosed a pending claim."
python -m ddclient fleet
python -m ddclient verify
python -m ddclient ready
```

`-v` adds every finding (not just material ones) and a per-call token breakdown.
`verify` exits non-zero if the audit chain does not recompute, so it drops
straight into a monitoring check.

## Full API

The running service publishes this contract itself: browse `/docs`, or fetch
`/openapi.json` to generate a client in another language.

| Method | Endpoint |
| --- | --- |
| `index()`, `health()`, `ready()`, `fleet()` | `/api`, `/healthz`, `/readyz`, `/fleet` |
| `whoami()` | `GET /api/whoami` |
| `search_companies(query, limit)`, `find_company(query)` | `GET /companies/search` |
| `upload_documents(documents)` | `POST /data-rooms` |
| `submit(crn, ...)`, `run(crn, ...)` | `POST /jobs` |
| `get_job(id)`, `list_jobs(limit, crn)`, `wait_for(id, ...)` | `GET /jobs` |
| `cancel(id)` | `POST /jobs/{id}/cancel` |
| `report_pdf(id)`, `save_report_pdf(id, path)` | `GET /jobs/{id}/report.pdf` |
| `memory(crn)`, `add_note(crn, note, author)` | `/memory/{crn}` |
| `audit_records(limit, trace_id)`, `verify_audit_chain()` | `/audit`, `/audit/verify` |

## What this library will not do

- Run agents in your process. The governance controls — identity, scope, quota,
  egress allowlist, audit chain — live on the fleet. A client that bypassed them
  would be a different, ungoverned system wearing the same name.
- Generate, estimate, or fill in financial figures. If the register has no filed
  accounts, `report.accounts_available` is `False` and there are no numbers.
- Re-grade findings. Severity is set by the agents under a calibration the fleet
  owns, so two clients cannot disagree about what `HIGH` means.
