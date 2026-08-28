# Adverse deal documents (fixture)

Two contract extracts that are commercially one-sided against the target, plus the
register that summarises them. Nothing here attacks the agents — for that, see
[../deal_documents_tampered/](../deal_documents_tampered/). These are simply bad
terms, of the kind a seller hopes you skim past.

They exist as a pair because they exercise **different tiers of the pipeline**:

| File | Literal clause matches | What it demonstrates |
| --- | --- | --- |
| `customer_master_services_agreement.txt` | **10 of 10** | the keyword matcher, finding every clause in the taxonomy |
| `supply_agreement_paraphrased.txt` | **0 of 10** | the embedding tier, recognising the same clauses written differently |
| `contract_register.csv` | — | document triage on structured rather than prose input |

## The paraphrased one is the point

Every restriction in `supply_agreement_paraphrased.txt` is adverse, and none of it
uses the words a keyword search looks for. Measured with `gemini-embedding-001`:

| Clause recognised | Similarity | The wording it was recognised from |
| --- | --- | --- |
| Termination for Convenience | 0.783 | "may bring this agreement to an end at any time, for any reason or for no reason" |
| Auto-Renewal | 0.782 | "continues for further periods of one year at a time" |
| Assignment Restriction | 0.769 | "may not pass the benefit or burden of this agreement" |
| Exclusivity | 0.757 | "shall not supply goods of the same or similar description to any other buyer" |
| Change of Control | 0.756 | "if the persons who ultimately own or control the Seller cease to do so" |
| Uncapped Indemnity | 0.711 | "make good every loss ... without limit as to amount" |

That is the argument for a second tier in one table: the phrase never appears, and
the clause is found anyway.

## Severity

Every clause in the taxonomy is graded **MEDIUM or LOW**, and a bad contract will
not on its own produce a red-flag verdict. That is deliberate. A change-of-control
consent right is a negotiation item, not a reason to walk away — the statutory
record is where deal-breakers live. A contract review that shouted at every
indemnity would be ignored within a week.

## Running it

```powershell
python orchestrator.py 03994971 --data-room fixtures/deal_documents_adverse
```

Both tiers need credentials to show their full behaviour: without a model the
literal matcher still runs and the embedding pass reports nothing rather than
guessing.
