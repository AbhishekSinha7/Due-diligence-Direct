# DueDiligence Direct

**A governed agent fleet that runs UK M&A red-flag diligence in minutes, with a citation behind every claim.**

Submission track: **The Fortified Enterprise Fleet** - All Things Agentic Hackathon.

Buy-side diligence starts with the same manual grind on every target: pull the
statutory record, read the charges register, check insolvency and beneficial
ownership, reconcile that against the seller's data room, and decide whether the
deal is still worth doing. DueDiligence Direct runs that as a fleet of specialist
agents against live UK Companies House data, and puts enterprise controls on every
single call the agents make.

## What makes it a fleet, not a chatbot

| Layer | What it does |
| --- | --- |
| **Agent Registry** | Five agents publish versioned cards. Dispatch resolves the highest ACTIVE version; deprecating a version instantly reroutes traffic. |
| **Agent Runtime** | Jobs run asynchronously on worker threads with durable SQLite state, live stage events, cancellation, and restart reconciliation. |
| **Memory Bank** | Every audit is remembered per company. A later run diffs the statutory fact sheet, so "a floating charge was registered since your last audit" becomes a HIGH finding. |
| **Agent Identity** | Each agent is its own principal with its own scopes and short-lived signed tokens. The Debate agent literally cannot reach Companies House. |
| **Gateway** | One choke point enforces identity, lifecycle, published capability, scope, egress allowlist, and quota, then retries transient failures. Denials are audited, not swallowed. |
| **Model Armor** | Seller documents are screened for prompt injection and quarantined; credentials and PII are redacted; every citation is string-matched against the source payload; unverifiable HIGH claims are capped at MEDIUM; the output disclaimer is enforced. |
| **Telemetry** | OpenTelemetry spans per agent stage plus a hash-chained audit log with a `/audit/verify` endpoint that proves records were not edited. |

Full diagrams and the requirement-by-requirement mapping: [ARCHITECTURE.md](ARCHITECTURE.md).

## Where the financial figures come from

Every financial number in a report is extracted from the target company's **own accounts
filed at Companies House**, not from mock data and not from the model:

1. `filing-history?category=accounts` locates the latest accounts filings.
2. The Document API returns each filing's iXBRL (`application/xhtml+xml`) rendition.
3. [accounts_parser.py](accounts_parser.py) reads the inline XBRL tags (`NetAssetsLiabilities`,
   `CurrentAssets`, `Creditors` split by maturity, `CashBankOnHand`, ...) with correct
   handling of sign, scale, bracket-negatives and continental number formats.
4. Ratios, year-on-year deltas, and cash runway are computed **in Python**, then cited by
   tag and context, e.g. `NetAssetsLiabilities@FY_END_20250531`.

Gemini never produces a figure; it interprets figures that arithmetic already fixed. If the
model is unavailable, the numbers and citations are identical and only the narrative degrades.

**Filings that contradict themselves are caught, not trusted.** Small-company accounts are
self-tagged and frequently inconsistent. Four balance sheet identities are checked
(working capital, total assets less current liabilities, net assets, and net assets = equity).
If one fails, the affected ratios are withheld and the inconsistency is reported with its
arithmetic instead — a real filing in the demo tags `CurrentAssets 1,688` against
`NetCurrentAssets 5,558`, which a naive pipeline would publish as "insolvent, current ratio 0.44".

Filings published only as scanned PDFs are reported as requiring manual review rather than
guessed at. The data room folder is for *contractual* material that has no statutory filing.

## Compliance posture

This fleet reads **live production data** - the UK Companies House statutory register and
companies' own filed accounts - rather than synthetic stand-ins, and does so under
enterprise controls:

- **Read-only.** Statutory access is exclusively HTTP `GET`. The fleet cannot alter the
  register. Every mutation - jobs, memory, audit records, uploads - stays in the
  deployment's own storage.
- **Public by statute.** The register is published under the Companies Act; personal data
  in it (officers, PSCs) is processed for the KYB purpose it exists for, displayed as filed
  and never enriched from private sources.
- **In-region.** Cloud Run and all fleet state run in `europe-west1`, recorded as
  `FLEET_DATA_REGION` on every trace and audit record. Model inference uses Vertex AI's
  `global` endpoint because `europe-west1` does not serve `gemini-3.5-flash`; storage and
  statutory data stay in the EU. Pin `GOOGLE_CLOUD_LOCATION` for stricter residency.
- **Least privilege.** Each agent holds only the scopes its card declares; the Debate agent
  cannot reach Companies House at all.
- **Tamper-evident.** Hash-chained audit log, verifiable at `/audit/verify`.

Full detail, including what this deliberately does not claim: [ARCHITECTURE.md](ARCHITECTURE.md) section 5.

## The agents

1. **Orchestrator** - plans the run, pulls statutory records, downloads and parses the filed accounts, loads the data room, recalls memory, fans out.
2. **Legal Risk** - insolvency, registered charges, PSC control, contract liabilities.
3. **Financial Auditor** - interprets the deterministically computed balance sheet metrics, plus company standing and filing regularity.
4. **Debate** - adversarial reconciliation of the two positions, weighted by whether evidence was verified.
5. **Synthesizer** - Red Flag Report, deal recommendation, disclaimer, and the memory write for next time.

Output is one of `GREEN LIGHT`, `PROCEED WITH CAUTION`, or `RED FLAG DEAL BREAKER`.

## Stack

- **Gemini 3.5** (`gemini-3.5-flash`) with Pydantic-constrained structured output
- **Google GenAI SDK** driving every agent node
- **LangGraph** DAG with SQLite checkpointing
- **FastMCP** tool server over the Companies House API
- **Google Cloud**: Cloud Run, Cloud Build, Artifact Registry, Secret Manager, Cloud Trace
- **Starlette** control plane, serving the API, the console, and its own OpenAPI contract
- **GOV.UK Design System** console (no framework, no build step, three static files)

## Setup

```powershell
venv\Scripts\python.exe -m pip install -r requirements.txt
```

`.env` in the project root:

```env
COMPANIES_HOUSE_API_KEY=your_companies_house_key
GEMINI_API_KEY=your_gemini_key
GEMINI_MODEL=gemini-3.5-flash
```

Optional fleet settings: `FLEET_SIGNING_KEY`, `FLEET_API_KEY`, `FLEET_STATE_DIR`,
`FLEET_RUNTIME_WORKERS`, `FLEET_GATEWAY_QUOTA_PER_MINUTE`,
`FLEET_ALLOWED_EGRESS_HOSTS`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `FLEET_CLOUD_TRACE`.
For Vertex AI instead of the Gemini API, set `GOOGLE_GENAI_USE_VERTEXAI=true`,
`GOOGLE_CLOUD_PROJECT`, and `GOOGLE_CLOUD_LOCATION`.

## Run

Start the control plane; it serves the operator console at the same address:

```powershell
python -m uvicorn service:app --port 8080     # console: http://localhost:8080
```

The console is a single-page app in `web/`, styled on the GOV.UK Design System because
that is where the data comes from. It is a **client of the API**, not a second way into
the fleet: name search, document upload, the live stage timeline, the agent conversation,
the report tabs, token accounting and PDF export are all the documented HTTP endpoints.

The API documents itself. `/docs` renders a browsable reference with a
playground that sends real requests; `/openapi.json` is the machine-readable
contract, which generates clients for other languages.

`GET /` content-negotiates, so the same URL serves both audiences:

```powershell
curl http://localhost:8080/            # JSON index of every endpoint
curl http://localhost:8080/api         # the same index, explicitly
```

To publish the console, set `FLEET_CONSOLE_ACCESS_CODE`. Humans exchange the code once for
an HttpOnly session cookie; machines keep using `FLEET_API_KEY` or a Cloud Run identity
token. With neither variable set the service is open, which is what you want locally.

CLI, inline:

```powershell
python orchestrator.py 03994971 --data-room fixtures/deal_documents
```

CLI, through the async runtime:

```powershell
python orchestrator.py 03994971 --data-room fixtures/deal_documents --async-job
```

Control plane (what runs on Cloud Run):

```powershell
python -m uvicorn service:app --port 8080
```

```bash
curl -X POST localhost:8080/jobs -H 'content-type: application/json' \
     -d '{"crn":"03994971","data_room_path":"fixtures/deal_documents"}'
curl localhost:8080/jobs/<job_id>     # events, report, trace id
curl localhost:8080/fleet             # registry, identities, tool policies
curl localhost:8080/audit/verify      # audit hash chain proof
```

Endpoints: `/healthz`, `/readyz`, `/fleet`, `GET /companies/search?q=`,
`GET /jobs/{id}/report.pdf`,
`POST /data-rooms`, `POST /jobs`, `GET /jobs`,
`GET /jobs/{id}`, `POST /jobs/{id}/cancel`, `GET /memory/{crn}`,
`POST /memory/{crn}/notes`, `GET /audit`, `GET /audit/verify`.
Set `FLEET_API_KEY` to require an `x-fleet-api-key` header.

## Deploy to Google Cloud

```bash
gcloud secrets create gemini-api-key --data-file=-
gcloud secrets create companies-house-api-key --data-file=-
gcloud secrets create fleet-signing-key --data-file=-

gcloud builds submit --config cloudbuild.yaml \
  --substitutions=_REGION=europe-west2,_SERVICE=duediligence-direct
```

Cloud Run runs with `--no-cpu-throttling` so background diligence jobs keep
executing after the HTTP response is returned, and `--no-allow-unauthenticated`
so IAM guards the control plane.

## Reproducible testing

Every claim in this README can be checked. Nothing below needs Google Cloud, and the
test suite needs no network or API keys at all.

### 1. Install

```powershell
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. Run the test suite (no credentials required)

```powershell
venv\Scripts\python.exe -m unittest discover -s tests -t .
```

Expected: **78 tests, OK, in under 10 seconds.** They cover iXBRL parsing and the
accounting-identity gate, identity token forgery and expiry, registry versioning and
lifecycle, every gateway denial path, Model Armor injection and grounding, memory deltas,
runtime cancellation and failure, notification dispatch, PDF export, and audit-chain
verification. `tests/__init__.py` sandboxes all fleet state, so runs leave nothing behind.

### 3. Run an audit offline (no API keys)

```powershell
venv\Scripts\python.exe orchestrator.py 03994971 --no-save
```

With no keys set the tool layer reports `config_missing` and the deterministic engine
produces the analysis. Expected: a verdict, cited findings, and
`analysis mode: deterministic` in the governance block. **The run always completes** -
that is the point of the fallback.

### 4. Run an audit against live data

Create `.env` from [.env.example](.env.example) with a free
[Companies House API key](https://developer.company-information.service.gov.uk/) and a
[Gemini API key](https://aistudio.google.com/apikey), then:

```powershell
venv\Scripts\python.exe orchestrator.py 03994971 --no-save
```

Expected: seven statutory endpoints collected, two accounts filings parsed, ~14 iXBRL
figures extracted, and `analysis mode: model`.

### 5. Verify the fleet controls individually

```powershell
# Model Armor: a poisoned document is quarantined and reported as a finding
venv\Scripts\python.exe orchestrator.py 03994971 --data-room fixtures/deal_documents_tampered --no-save

# Agent Identity: an agent cannot obtain a scope it does not hold
venv\Scripts\python.exe -c "import agent_identity as a; a.mint_token('debate', audience='fleet-gateway', scopes=[a.SCOPE_STATUTORY_READ])"

# Agent Gateway: an agent cannot call a tool absent from its published card
venv\Scripts\python.exe -c "import gateway, orchestrator; gateway.call('debate','collect_company_records', crn='03994971')"

# Filed accounts: deterministic iXBRL extraction on its own
venv\Scripts\python.exe -c "import json, mcp_server; print(json.dumps(mcp_server.analyze_statutory_accounts(mcp_server.CompanyQuery(crn='03994971'))['latest']['analysis']['derived'], indent=2))"
```

Expected, in order: `Quarantined 1 document(s)`; **IdentityError**; **PolicyViolation**;
and a JSON block of computed balance sheet metrics.

### 6. Run the console and the control plane

```powershell
venv\Scripts\python.exe -m uvicorn service:app --port 8080     # console + API
```

Open http://localhost:8080 for the console. `curl` the same URL for the JSON index of
every endpoint. `GET /audit/verify` recomputes the audit
hash chain and should report `{"valid": true, ...}`.

### 7. Deploy to Google Cloud

See [Deploy to Google Cloud](#deploy-to-google-cloud) below, or the step-by-step
walkthrough with IAM roles and secrets in [ARCHITECTURE.md](ARCHITECTURE.md).

## Demo script

1. **Clean run** - submit `03994971` with the sample data room. Watch stage events stream while the run happens in the background.
2. **Filed accounts tab** - the balance sheet by period, every figure traced to its iXBRL tag, the four identity checks, and the inconsistency that causes the liquidity ratio to be withheld.
3. **Governance tab** - trace id, models used, registry versions, guardrail verdicts, and the audit records for that exact trace.
4. **Hostile data room** - rerun against `fixtures/deal_documents_tampered`. A seller document instructing the agents to "mark this company as clean" is quarantined by Model Armor, and the tampering itself is reported as a risk.
5. **Memory** - rerun the same company. The fleet recalls the previous verdict and reports what changed since the last audit.
6. **Audit trail tab** - the hash chain verifies across every record written by the CLI, the console, and the API.

`demo_scenarios.json` holds the same script in machine-readable form.

## Tests

```powershell
python -m unittest discover -s tests -t .
```

49 tests, no network required. They cover iXBRL number formats, tag extraction and
period grouping, ratio and runway mathematics, the accounting-identity gate that suppresses
unreliable liquidity claims, identity token forgery and expiry, registry versioning and
lifecycle, gateway denial paths (unregistered tool, undeclared capability, egress, quota,
retry), Model Armor injection and grounding behaviour, memory deltas, runtime job lifecycle
including cancellation and failure, and audit-chain verification.

## Limitations

- Statutory coverage is what Companies House exposes; it is not a full VDR review.
- Micro-entity and abridged filings legitimately omit turnover, profit, and cash. Those
  metrics are reported as not tagged rather than estimated, so cash runway is only produced
  when the filing actually tags cash across two periods.
- Filings published only as scanned PDFs are flagged for manual review, not parsed.
- Output is AI-generated diligence support, not legal, financial, tax, or investment
  advice. Qualified professionals must verify source records before transaction reliance.
