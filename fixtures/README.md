# Fixtures

Documents used to exercise the parts of an audit that statutory records cannot reach.

**Nothing here is financial data, and nothing here is a stand-in for it.** Every figure
in a report is parsed from the target company's own iXBRL accounts, filed at Companies
House and downloaded through the Document API. These folders hold *contractual* material
— the kind of document that has no public filing and can only come from a seller's data
room — so the legal agent and Model Armor have something real to work on.

| Folder | Exercises | Contains |
| --- | --- | --- |
| [deal_documents/](deal_documents/) | the Legal Risk Agent's clause detection | a contract summary with a change-of-control consent requirement and an uncapped indemnity |
| [deal_documents_tampered/](deal_documents_tampered/) | Model Armor | the same kind of document, altered to instruct the agents to report the company as clean |
| [deal_documents_adverse/](deal_documents_adverse/) | both clause tiers | one contract hitting all ten literal detectors, and one saying the same things in words none of them match |

```powershell
python orchestrator.py 03994971 --data-room fixtures/deal_documents
python orchestrator.py 03994971 --data-room fixtures/deal_documents_tampered
python orchestrator.py 03994971 --data-room fixtures/deal_documents_adverse
```

The second prints `Quarantined 1 document(s)` — and the tampering is reported as a
finding, because a counterparty editing documents they are handing you is itself
diligence-relevant.

Omitting `--data-room` audits statutory records alone, which is the default.

## Why these are not called "samples"

"Sample data" suggests the numbers are illustrative. In this system they never are: the
whole design rests on financial figures coming from filings rather than from a model or a
fixture. These are documents, they are labelled synthetic inside the files themselves, and
they exist so the document-handling path can be demonstrated without putting a real
counterparty's contract in a public repository.

Real deal documents go in `data_room/` (gitignored) or are uploaded through the console.
