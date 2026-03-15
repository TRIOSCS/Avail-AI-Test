# Deduplication and Source Rules

## Dedupe philosophy
Conservative dedupe only.

Merge when:
- same vendor identity strongly matches
- same domain/email/phone/internal identity strongly supports it
- multiple medium signals point to the same vendor for the same part

Do not aggressively merge ambiguous vendors.

## One lead per vendor per part
Different evidence items and sources should roll up under one lead where confidence is high enough.

## Source strategy
- API/posting sources = strong stock signals
- ICSource / NetComponents / marketplace-like data = medium-high stock signals
- Salesforce history = strong trust/history signals
- Avail historical activity = strong trust/history signals
- AI/current web search = discovery + enrichment + safety review only

## Preserve source attribution
After dedupe, do not lose source provenance.
