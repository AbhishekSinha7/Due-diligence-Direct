#!/usr/bin/env bash
# The whole API in four requests.
#
#   export FLEET_API_URL=https://your-service.run.app
#   export FLEET_API_KEY=ddd_v1....
#   ./examples/curl.sh 03994971
#
# JSON is read with python rather than jq: this is a python project, so python is
# already there, and one fewer thing to install is one fewer reason it does not run.
# Submitting an audit spends model quota.

set -euo pipefail

BASE="${FLEET_API_URL:?set FLEET_API_URL}"
AUTH="x-fleet-api-key: ${FLEET_API_KEY:-}"
QUERY="${1:-03994971}"

# Read one value out of JSON on stdin, e.g. field .results.0.company_number
field() { python -c "
import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1].strip('.').split('.'):
    d = d[int(k)] if k.isdigit() else d.get(k) if isinstance(d, dict) else None
    if d is None: sys.exit('not found: ' + sys.argv[1])
print(d)" "$1"; }

# 0. What is this key allowed to do? Free, and saves guessing later.
echo "credential:"
curl -sS -H "$AUTH" "$BASE/api/whoami" | python -m json.tool | head -12

# 1. Resolve a name or number to a company number.
CRN=$(curl -sS -H "$AUTH" --get --data-urlencode "q=$QUERY" --data "limit=1" \n  "$BASE/companies/search" | field .results.0.company_number)
echo "company: $CRN"

# 2. Submit. Returns immediately; the run continues on the fleet.
JOB=$(curl -sS -X POST -H "$AUTH" -H 'content-type: application/json' \n  -d "{\"crn\":\"$CRN\",\"submitted_by\":\"curl-example\"}" "$BASE/jobs" | field .job_id)
echo "job: $JOB"

# 3. Poll until the status is terminal.
while :; do
  STATUS=$(curl -sS -H "$AUTH" "$BASE/jobs/$JOB" | field .status)
  echo "  $STATUS"
  case "$STATUS" in
    SUCCEEDED|FAILED|CANCELLED|INTERRUPTED) break ;;
  esac
  sleep 2
done

[ "$STATUS" = "SUCCEEDED" ] || { echo "audit $STATUS"; exit 1; }

# 4. The report is on the job. Nothing further to fetch.
JSON=$(curl -sS -H "$AUTH" "$BASE/jobs/$JOB")
echo
echo "verdict: $(echo "$JSON" | field .result.red_flag_verdict.recommendation)"
echo "company: $(echo "$JSON" | field .result.raw_statutory_data.profile.data.company_name)"
echo "$JSON" | python -c "
import json,sys
for risk in json.load(sys.stdin)['result']['red_flag_verdict'].get('top_risks', []):
    print('  -', risk)"

# Optional: the same report as a PDF.
curl -sS -H "$AUTH" -o "$CRN.pdf" "$BASE/jobs/$JOB/report.pdf"
echo "wrote $CRN.pdf"
