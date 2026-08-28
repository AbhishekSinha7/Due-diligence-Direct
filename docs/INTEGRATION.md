# Integrating with DueDiligence Direct

Everything another system needs to drive this fleet: what to collect, how to obtain
each piece, and how to make the first call.

There are two situations, and they need different things:

- **Calling someone else's fleet** — you need a URL and a key. Start at
  [Part 1](#part-1--calling-an-existing-fleet). Two items to collect.
- **Running your own** — you need the upstream credentials too. Start at
  [Part 2](#part-2--running-your-own-fleet). Six items to collect.

---

## Part 1 — Calling an existing fleet

### What to collect

| # | Detail | Example | Who gives it to you |
| --- | --- | --- | --- |
| 1 | Service URL | `https://due-diligence-direct-xxxx.run.app` | the fleet operator |
| 2 | API key | `ddd_v1.eyJhcGgi...` | the fleet operator |

That is all. Everything else is discoverable from the service.

### Step 1 — Get the service URL

Ask the operator, or if you have access to the Google Cloud project:

```powershell
gcloud run services describe due-diligence-direct --region europe-west1 --format "value(status.url)"
```

Confirm it is the right service before going further:

```powershell
curl https://your-service.run.app/healthz
```

Expect `{"status":"ok","service":"duediligence-direct"}`. If you get HTML, you sent an
`Accept: text/html` header and reached the console — which is also a good sign.

### Step 2 — Get an API key

Keys are per caller. Ask the operator for one, telling them **what you need to do**, so
they can scope it correctly:

| You want to | Ask for scope |
| --- | --- |
| Read audits, reports, company search | `audits:read` |
| Submit audits and upload documents | `audits:write` (spends model quota) |
| Add operator notes | `memory:write` |
| Read the audit trail, verify the chain | `governance:read` |

The operator runs (see [Part 2, step 7](#step-7--issue-api-keys-for-integrators)):

```powershell
python api_keys.py issue --name "your-system" --scopes audits:read audits:write --days 30
```

The key is shown **once**. Nothing on the server stores it — losing it means asking for
another.

### Step 3 — Check what your key grants

Before writing any code:

```powershell
curl -H "x-fleet-api-key: ddd_v1...." https://your-service.run.app/api/whoami
```

```json
{
  "authenticated": true,
  "name": "your-system",
  "key_id": "544338c13370",
  "scopes": ["audits:read", "audits:write"],
  "expires_at": 1788068004,
  "requests_per_hour": 600,
  "audits_per_hour": 20,
  "requests_used_this_hour": 1,
  "audits_used_this_hour": 0
}
```

This tells you your scopes, your budget, and when the key dies — without discovering any
of it through a sequence of failures.

### Step 4 — Read the contract

The service documents itself:

- `https://your-service.run.app/docs` — browsable reference with a live playground
- `https://your-service.run.app/openapi.json` — OpenAPI 3.1, for generating a client

### Step 5 — Make the first call

Runnable versions of everything below are in [../examples/](../examples/).

**Python** (the supported client — see [CLIENT.md](CLIENT.md)):

```powershell
python -m pip install requests
```

```python
from ddclient import DueDiligenceClient

with DueDiligenceClient("https://your-service.run.app", api_key="ddd_v1....") as fleet:
    company = fleet.find_company("third party formations")
    print(company)                       # THIRD PARTY FORMATIONS LIMITED (03994971)

    report = fleet.run(company.company_number, on_event=print)
    print(report.recommendation)
    for finding in report.material_findings:
        print(f"[{finding.severity}] {finding.category}: {finding.finding}")
```

**Any language, over HTTP:**

```bash
# 1. resolve the company
curl -H "x-fleet-api-key: $KEY" \
  "$URL/companies/search?q=third%20party%20formations&limit=5"

# 2. submit; returns immediately with a job id
curl -X POST -H "x-fleet-api-key: $KEY" -H "content-type: application/json" \
  -d '{"crn":"03994971","submitted_by":"your-system"}' "$URL/jobs"

# 3. poll until status is terminal
curl -H "x-fleet-api-key: $KEY" "$URL/jobs/job-2026...-a4a7a0"

# 4. or fetch the PDF once it has succeeded
curl -H "x-fleet-api-key: $KEY" -o report.pdf "$URL/jobs/job-2026...-a4a7a0/report.pdf"
```

**Another language, generated:**

```bash
curl -s "$URL/openapi.json" -o openapi.json
openapi-generator-cli generate -i openapi.json -g typescript-fetch -o ./client
```

### Step 6 — Decide how you learn a run has finished

An audit takes roughly 30–60 seconds. Three options:

| Approach | Use when |
| --- | --- |
| `fleet.run(...)` | a script that can wait |
| `submit()` then `get_job()` later | the caller must not block |
| Webhook | you own the fleet and want a push |

Polling is what the API supports directly; there is no per-caller callback registration.
If you run the fleet yourself, set `FLEET_NOTIFY_WEBHOOK` and every finished job POSTs:

```json
{
  "event": "diligence.job.finished",
  "status": "SUCCEEDED",
  "job_id": "job-20260822T190123-a4a7a0",
  "crn": "06876015",
  "company_name": "NE LTD",
  "recommendation": "PROCEED WITH CAUTION",
  "text": "🟠 PROCEED WITH CAUTION - NE LTD",
  "executive_summary": "...",
  "top_risks": ["..."],
  "severity_counts": {"HIGH": 1, "MEDIUM": 2},
  "trace_id": "2954666f...",
  "console_url": "https://your-console.run.app"
}
```

Slack and Google Chat incoming webhooks render it as-is; any JSON endpoint works. It is
fixed configuration, never taken from a request, so a caller cannot point it elsewhere.

### Step 7 — Handle the failures you will actually get

| Status | Meaning | What to do |
| --- | --- | --- |
| `401` | key missing, malformed, expired, or revoked | check `/api/whoami`; ask for a new key |
| `403` | key lacks the scope named in the message | ask the operator to widen it |
| `409` | report requested for a job that has not succeeded | poll until terminal first |
| `413` / `415` | upload too large, or not `.csv/.md/.pdf/.txt` | max 25 files, 10 MB total |
| `429` | request or audit budget spent | honour `Retry-After`; do not retry immediately |
| `400` | bad company number or unknown status filter | validate before sending |

The Python client raises a distinct exception per case (`AuthenticationError`,
`PolicyDenied`, `NotFound`, `WaitTimeout`), so you never string-match a message.

**`WaitTimeout` does not cancel the run.** It means your client gave up; the audit is
still executing. Call `cancel(job_id)` if you actually want it stopped.

### Step 8 — Know the limits before you design around them

- **Budgets are per key, per hour** — `requests_per_hour` and `audits_per_hour`, both on
  `/api/whoami`. They are enforced per process, so a multi-instance deployment gives a
  caller more than the configured number. Treat them as guard rails, not guarantees.
- **Queued work is capped** globally (`FLEET_MAX_PENDING_JOBS`, default 25). A burst of
  submissions gets `429` rather than an unbounded queue.
- **Page size caps at 100** on `GET /jobs`. Pass `include_result=false` when listing —
  it returns a summary instead of every full report, which on a twelve-row page is the
  difference between 6.6 KB and 312 KB.
- **An audit spends model quota.** `audits:write` is the scope that costs money.

---

## Part 2 — Running your own fleet

### What to collect

| # | Detail | Required | Where it comes from |
| --- | --- | --- | --- |
| 1 | Companies House API key | yes | Companies House developer hub |
| 2 | Gemini API key **or** Vertex AI access | yes | AI Studio, or your Google Cloud project |
| 3 | Google Cloud project | for deployment | Google Cloud console |
| 4 | `FLEET_SIGNING_KEY` | yes in production | you generate it |
| 5 | `FLEET_CONSOLE_ACCESS_CODE` | if the console is public | you generate it |
| 6 | Webhook URL | optional | Slack / Chat / your own endpoint |

### Step 1 — Companies House API key

This is the data source. Without it the fleet has nothing to audit.

1. Register at **https://developer.company-information.service.gov.uk/**
2. Sign in → **Manage applications** → **Add an application**
3. Give it a name and description; choose the **Live** environment
4. Open the application → **Create new key** → choose **REST**
5. Copy the key

Free, no payment details. Rate limit is 600 requests per five minutes, which is far more
than one audit needs. Access is **read-only**: the fleet only ever issues `GET`.

```
COMPANIES_HOUSE_API_KEY=your_key_here
```

Verify:

```powershell
python -c "import mcp_server, json; print(json.dumps(mcp_server.search_companies('tesco', 3), indent=2)[:400])"
```

### Step 2 — Model access

Two routes. **Vertex AI is the right answer for anything deployed**; the API key route is
for a laptop.

**Option A — Gemini API key (quick start)**

1. Go to **https://aistudio.google.com/apikey**
2. **Create API key**, choose a Google Cloud project
3. Copy it

```
GEMINI_API_KEY=your_key_here
GEMINI_MODEL=gemini-3.5-flash
```

**Option B — Vertex AI (production)**

No key at all: the service account's own identity authenticates.

```powershell
gcloud services enable aiplatform.googleapis.com --project YOUR_PROJECT
gcloud projects add-iam-policy-binding YOUR_PROJECT `
  --member "serviceAccount:YOUR_RUNTIME_SA@YOUR_PROJECT.iam.gserviceaccount.com" `
  --role roles/aiplatform.user
```

```
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=global
```

**Use `global`.** Regional endpoints such as `europe-west1` do not serve
`gemini-3.5-flash` and fail as `model_unavailable`. This is worth knowing before you
spend an afternoon on it.

Document triage (Gemma) and clause detection (embeddings) go through the Gemini API and
need `GEMINI_API_KEY` even when reasoning runs on Vertex. Without it those two tiers fall
back to deterministic analysis — the audit still completes and says so in
`governance.model_tiers`.

Verify:

```powershell
curl http://localhost:8080/readyz
```

`model_configured` and `companies_house_configured` must both be `true`.

### Step 3 — Google Cloud project

Needed only to deploy.

```powershell
gcloud projects create YOUR_PROJECT --name "DueDiligence Direct"
gcloud config set project YOUR_PROJECT
gcloud services enable run.googleapis.com cloudbuild.googleapis.com `
  artifactregistry.googleapis.com secretmanager.googleapis.com `
  aiplatform.googleapis.com cloudtrace.googleapis.com
```

Billing must be enabled even on free trial credit, or `run.googleapis.com` refuses.

### Step 4 — Signing key

This secures agent identity tokens **and** API keys. Anyone holding it can mint either.

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

Store it in Secret Manager rather than an environment variable in a YAML file:

```powershell
echo -n "PASTE_THE_KEY" | gcloud secrets create fleet-signing-key --data-file=-
gcloud secrets add-iam-policy-binding fleet-signing-key `
  --member "serviceAccount:YOUR_RUNTIME_SA@YOUR_PROJECT.iam.gserviceaccount.com" `
  --role roles/secretmanager.secretAccessor
```

Left unset, a development key is generated under `FLEET_STATE_DIR`. That is fine locally
and wrong in production: Cloud Run's disk is ephemeral, so the key — and every API key
signed with it — changes on redeploy.

### Step 5 — Console access code

Only if people will reach the console in a browser.

```powershell
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

```
FLEET_CONSOLE_ACCESS_CODE=the_generated_code
```

Setting it also locks the API to signed-in callers. Sign-in is throttled per caller and
globally, but the throttle is per process — so make the code long and random rather than
memorable.

### Step 6 — Deploy

```powershell
gcloud run deploy due-diligence-direct `
  --source . `
  --region europe-west1 `
  --service-account YOUR_RUNTIME_SA@YOUR_PROJECT.iam.gserviceaccount.com `
  --set-secrets "FLEET_SIGNING_KEY=fleet-signing-key:latest,COMPANIES_HOUSE_API_KEY=companies-house-key:latest,GEMINI_API_KEY=gemini-key:latest" `
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=YOUR_PROJECT,GOOGLE_CLOUD_LOCATION=global,FLEET_ENVIRONMENT=production,FLEET_DATA_REGION=europe-west1" `
  --no-allow-unauthenticated
```

**`--no-allow-unauthenticated` is the single highest-value control here.** With it, Cloud
Run IAM decides who reaches the service at all and the API keys are defence in depth.
Without it, everything else sits in front of an open door.

To let a person in, grant them the invoker role rather than making it public:

```powershell
gcloud run services add-iam-policy-binding due-diligence-direct `
  --region europe-west1 --member "user:them@example.com" --role roles/run.invoker
```

Callers then present an identity token:

```powershell
$env:FLEET_API_TOKEN = gcloud auth print-identity-token
```

On Cloud Run, `ddclient` mints one automatically from the metadata server — nothing to
configure, provided the calling service account holds `roles/run.invoker`.

### Step 7 — Issue API keys for integrators

```powershell
python api_keys.py scopes                     # what each scope allows
python api_keys.py issue --name "partner-crm" --scopes audits:read audits:write --days 30
python api_keys.py issue --name "monitoring" --scopes governance:read --days 90 --audits-per-hour 0
python api_keys.py inspect ddd_v1....         # decode one you were given
python api_keys.py verify  ddd_v1....         # validate as the service would
```

Keys are signed, not stored, so they survive redeploys and work across instances. The
cost of that is revocation: to kill one, list its id.

```powershell
gcloud run services update due-diligence-direct --region europe-west1 `
  --update-env-vars "FLEET_REVOKED_KEY_IDS=544338c13370"
```

The list is read on every request, so revocation takes effect without a rebuild. Keep
lifetimes short enough that you would tolerate being stuck with a key for its full term.

### Step 8 — Optional settings worth knowing

```
FLEET_NOTIFY_WEBHOOK=https://hooks.slack.com/services/...   # push on completion
FLEET_CONSOLE_URL=https://your-service.run.app              # link back from notifications
FLEET_PUBLIC_URL=https://your-service.run.app               # pin the OpenAPI server URL
FLEET_MAX_PENDING_JOBS=25                                   # bounds model spend
FLEET_ALLOWED_EGRESS_HOSTS=...                              # gateway refuses anything else
FLEET_CLOUD_TRACE=true                                      # spans to Cloud Trace
```

`FLEET_ALLOWED_EGRESS_HOSTS` is a real control, not a formality: a tool calling a host
outside the list is refused at the gateway and the refusal is audited.

---

## Alternative: MCP

The Companies House tools are also exposed as an MCP server, if you want them inside an
MCP-speaking assistant rather than through this API:

```powershell
python mcp_server.py
```

This gives you the **tools** — statutory records, filings, accounts analysis — not the
governed multi-agent audit. The gateway, agent identities, quotas and audit chain live in
the fleet, so an MCP client is not a substitute for it.

---

## Checklist

Calling an existing fleet:

- [ ] Service URL, confirmed with `/healthz`
- [ ] API key, with scopes matching what you will do
- [ ] `/api/whoami` returns the scopes you expected
- [ ] Read `/docs`
- [ ] Decided polling vs waiting
- [ ] Handle `401`, `403`, `429` distinctly; honour `Retry-After`

Running your own:

- [ ] Companies House key, `/readyz` shows `companies_house_configured: true`
- [ ] Vertex AI with `GOOGLE_CLOUD_LOCATION=global`, or a Gemini API key
- [ ] `FLEET_SIGNING_KEY` in Secret Manager, not in a config file
- [ ] `FLEET_CONSOLE_ACCESS_CODE` set if the console is reachable
- [ ] Deployed `--no-allow-unauthenticated`
- [ ] Per-caller keys issued; nobody sharing one secret
- [ ] `FLEET_MAX_PENDING_JOBS` set to a spend you accept
- [ ] `/audit/verify` returns `{"valid": true}`
