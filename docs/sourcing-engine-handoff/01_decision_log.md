# Decision Log

## Confirmed decisions
1. One lead per vendor per part.
2. Buyer statuses attach to the lead, not to individual evidence items.
3. Stock-confidence and vendor-safety are separate scoring dimensions.
4. The engine is a lead-generation and ranking system for buyers, not an autonomous purchasing bot.
5. Deduplication should be conservative.
6. Buyer feedback should improve future ranking.
7. Safety review should warn, not silently block, and should avoid definitive accusations.

## Deferred decisions
1. Exact data storage strategy for evidence (table only vs table + JSON cache).
2. Exact source connector implementations and legality/rate-limit strategies.
3. Whether canonical_vendor_id exists already or must be introduced.
4. Whether AI/web enrichment runs synchronously or asynchronously.
5. How much safety review should be automated before manual escalation.

## Open questions for implementation discovery
1. Which source connectors already exist in Avail?
2. Which sourcing screens already exist and should be extended?
3. What user/ownership model currently exists for buyer assignment?
4. Where should follow-up queue live in the UI?