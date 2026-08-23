# Examples

Three runnable scripts, smallest first. Set two environment variables and any of
them works against a local fleet or a deployed one.

```powershell
$env:FLEET_API_URL = "http://localhost:8080"        # or your Cloud Run URL
$env:FLEET_API_KEY = "ddd_v1...."                   # omit if the fleet is unauthenticated
```

See [../docs/INTEGRATION.md](../docs/INTEGRATION.md) for how to obtain both.

| Script | What it does | Costs quota |
| --- | --- | --- |
| [read_only.py](read_only.py) | who am I, search, history, verify the audit chain | no |
| [quickstart.py](quickstart.py) | audit a company using the client library | **yes** |
| [plain_http.py](plain_http.py) | the same audit in raw HTTP, no library | **yes** |
| [curl.sh](curl.sh) | the four requests, as shell | **yes** |

Start with `read_only.py`. It proves the URL, the key and the scopes all work
before you spend anything.

```powershell
python examples/read_only.py
```

```
key      partner-crm (544338c13370)
scopes   audits:read, governance:read
budget   1/600 requests, 0/20 audits used this hour

search 'third party formations':
  03994971  THIRD PARTY FORMATIONS LIMITED  [active]
  SC406882  BARCH THIRD PARTY INSPECTION LIMITED  [dissolved]

audit history (12 total):
  06876015  SUCCEEDED  PROCEED WITH CAUTION   53s
  03994971  SUCCEEDED  PROCEED WITH CAUTION   76s

audit chain: intact across 1205 records
```

Then run a real audit. It takes roughly a minute:

```powershell
python examples/quickstart.py 03994971
```

## Which one to read

- **Using Python?** [quickstart.py](quickstart.py) is the whole thing in ten lines.
- **Using anything else?** [plain_http.py](plain_http.py) is the entire protocol, and
  it is four requests: search, submit, poll, fetch. Translate it directly.
- **Wiring up CI or a shell pipeline?** [curl.sh](curl.sh).

## Notes

- **Submitting an audit spends model quota.** `read_only.py` never does; the other
  three always do. The `audits:write` scope is the one that costs money.
- **A run takes 30–60 seconds.** `quickstart.py` waits; `plain_http.py` shows the
  polling loop explicitly. Neither blocks the fleet — the run continues server-side
  whether or not your script is still watching.
- **A fleet with no model credentials still completes**, falling back to deterministic
  analysis and saying so in `governance.analysis_mode`. That is how these examples were
  verified without spending anything, and it is a useful way to try the flow yourself.
- **Every figure comes from the company's filed accounts**, not from a model. When a
  filing fails its own balance-sheet identity, the affected ratios are suppressed and
  `report.reconciliation_failures` tells you which and why.
