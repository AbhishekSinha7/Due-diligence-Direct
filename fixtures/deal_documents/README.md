# Deal documents (fixture)

**No financial figure ever comes from this folder.** All financial analysis is computed
from the target company's own iXBRL accounts, filed at Companies House, downloaded through
the Document API and parsed deterministically by `accounts_parser.py`.

This holds *contractual* material of the kind a seller supplies alongside the statutory
record — the sort of document that has no public filing and can only come from a data room.
It exists so the Legal Risk Agent's clause detection has something to find.

`contract_summary.txt` is a synthetic illustration of contract terms: a change-of-control
consent requirement and an uncapped indemnity. It is not attributable to any real company,
and it says so on its first line, so it cannot be mistaken for a real agreement in a demo
recording.

Real deal documents go in `data_room/` (gitignored), or are uploaded through the console
when starting an audit.
