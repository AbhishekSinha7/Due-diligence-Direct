# Sample data room

**Financial figures never come from this folder.** All financial analysis is computed
from the target company's own iXBRL accounts filed at Companies House, downloaded through
the Document API and parsed deterministically by `accounts_parser.py`.

This folder holds only *contractual* material of the kind a seller supplies alongside the
statutory record - the sort of document that has no public filing and can only come from a
data room.

`contract_summary.txt` is a synthetic illustration of contract terms. It is not attributable
to any real company, and it is labelled as synthetic inside the file itself so it cannot be
mistaken for a real agreement in a demo recording.

Put real deal documents in `data_room/` (gitignored) or upload them through the dashboard.
