"""Everything you can do without spending model quota.

A good first script to run against a fleet you have just been given access to:
it proves the URL, the key, and the scopes all work, and costs nothing.

    set FLEET_API_URL=https://your-service.run.app
    set FLEET_API_KEY=ddd_v1....
    python examples/read_only.py
"""

import pathlib
import sys

# Run from anywhere: python puts this file's own directory on the path, not the
# repository root, so ddclient would not be importable otherwise.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ddclient import DueDiligenceClient
from ddclient.errors import PolicyDenied

with DueDiligenceClient() as fleet:
    # Who am I, and what am I allowed to do? Ask before discovering it via 403s.
    me = fleet.whoami()
    print(f"key      {me['name']} ({me['key_id']})")
    print(f"scopes   {', '.join(me['scopes'])}")
    print(f"budget   {me['requests_used_this_hour']}/{me['requests_per_hour']} requests, "
          f"{me['audits_used_this_hour']}/{me['audits_per_hour']} audits used this hour")

    print("\nsearch 'third party formations':")
    for company in fleet.search_companies("third party formations", limit=3):
        print(f"  {company.company_number}  {company.title}  [{company.company_status}]")

    # Summaries, not full reports: a listing needs a verdict, not the whole audit.
    page = fleet.job_page(limit=5, include_result=False)
    print(f"\naudit history ({page.total} total):")
    for job in page:
        took = f"{job.duration_seconds:.0f}s" if job.duration_seconds else "-"
        print(f"  {job.crn}  {job.status:<10} {job.recommendation or '-':<22} {took}")

    try:
        chain = fleet.verify_audit_chain()
        print(f"\naudit chain: {'intact' if chain['valid'] else 'BROKEN'} "
              f"across {chain.get('records', 0)} records")
    except PolicyDenied:
        print("\naudit chain: this key lacks the governance:read scope")
