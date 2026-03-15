# Data Model Intent

## Lead model
One lead = one vendor for one part.

Lead should support:
- vendor identity
- requested part and matched part
- source attribution
- confidence score and band
- vendor safety score and band
- reason summary
- caution/risk flags
- contact information
- suggested next action
- buyer status
- timestamps
- feedback/outcome history

## Evidence model
A lead can have many evidence items.

Each evidence item should support:
- signal type
- source type/name/reference
- observed values
- freshness/timestamp
- scoring contribution
- explanation
- verification state

## Buyer statuses
- New
- Contacted
- Replied
- No Stock
- Has Stock
- Bad Lead
- Do Not Contact
