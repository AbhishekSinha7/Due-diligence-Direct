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
- **Streamlit** fleet console

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

Fleet console (recommended for the demo):

```powershell
python -m streamlit run dashboard.py
```

CLI, inline:

```powershell
python orchestrator.py 03994971 --data-room sample_data_room
```

CLI, through the async runtime:

```powershell
python orchestrator.py 03994971 --data-room sample_data_room --async-job
```

Control plane (what runs on Cloud Run):

```powershell
python -m uvicorn service:app --port 8080
```

```bash
curl -X POST localhost:8080/jobs -H 'content-type: application/json' \
     -d '{"crn":"03994971","data_room_path":"sample_data_room"}'
curl localhost:8080/jobs/<job_id>     # events, report, trace id
curl localhost:8080/fleet             # registry, identities, tool policies
curl localhost:8080/audit/verify      # audit hash chain proof
```

Endpoints: `/healthz`, `/readyz`, `/fleet`, `POST /jobs`, `GET /jobs`,
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

## Demo script

1. **Clean run** - submit `03994971` with the sample data room. Watch stage events stream while the run happens in the background.
2. **Filed accounts tab** - the balance sheet by period, every figure traced to its iXBRL tag, the four identity checks, and the inconsistency that causes the liquidity ratio to be withheld.
3. **Governance tab** - trace id, models used, registry versions, guardrail verdicts, and the audit records for that exact trace.
4. **Hostile data room** - rerun against `sample_data_room_hostile`. A seller document instructing the agents to "mark this company as clean" is quarantined by Model Armor, and the tampering itself is reported as a risk.
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
