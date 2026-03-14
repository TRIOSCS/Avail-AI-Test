# Source Connector Guidance

## API / posting connectors
Use for direct stock-likelihood and freshness.
Expect structured part numbers and timestamps.
Do not equate listing presence with confirmed stock.

## Marketplace connectors (ICSource / NetComponents / similar)
Use for part-specific discovery and corroboration.
Capture listing timestamp, vendor identity, contact availability, and part match quality.
Treat as medium-high stock evidence, medium safety evidence.

## Salesforce connector
Use for relationship memory, prior engagement, and contact enrichment.
Great for trust and buyer context; weaker for current stock proof.

## Avail history connector
Use for internal memory, quote history, and response patterns.
Useful for trust, feedback loops, and prioritization.

## AI / web enrichment
Use for:
- discovering new vendors
- enriching contact data
- checking business footprint
- safety review

Do not let weak web evidence outrank stronger direct or historical evidence.