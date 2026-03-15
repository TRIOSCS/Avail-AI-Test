# Avail Sourcing Engine Planning Blueprint

## 1. Overview

### Purpose
Build a sourcing engine that helps purchasing agents find, prioritize, and safely evaluate vendor leads to contact and verify stock availability.

### Core outcome
For each requested part, the system should produce a ranked set of buyer-usable leads with:
- vendor identity
- part match quality
- source attribution
- freshness
- explainable evidence
- lead-confidence (stock likelihood)
- vendor-safety assessment
- suggested next action
- buyer workflow status

### Primary users
- Purchasing agents / buyers
- Sourcing managers
- Operations reviewers / admins

## 2. Product Goal
The sourcing engine should help buyers answer:
- Who might have stock for this part?
- Why do we think that?
- How fresh is the signal?
- Is the vendor reasonably safe to contact?
- What should the buyer do next?

### Success definition
The engine is successful if it gives buyers a smaller number of strong, explainable leads that reduce manual searching time and improve verified-stock follow-up rates.

## 3. Non-Goals
The sourcing engine is not intended to:
- automatically place orders
- autonomously decide a vendor is legitimate or illegitimate
- replace buyer judgment
- optimize for lead volume at the expense of lead quality
- silently suppress leads based only on weak web/AI signals

## 4. Product Principles
1. Explainability over mystery
2. Buyer usefulness over technical elegance
3. Conservative dedupe over risky merging
4. Separate stock-confidence from vendor-safety
5. Human review remains central
6. Source attribution must always be preserved
7. Buyer feedback should improve future ranking

## 5. Core Domain Model

### Lead definition
One lead = one vendor for one part.
A lead may aggregate many evidence items from many sources.

### Evidence definition
One evidence item = one signal that supports or weakens a lead.
Evidence should be structured, attributable, time-stamped, and human-readable.

## 6. Lead Schema (MVP)
### Identity
- lead_id
- part_number_requested
- part_number_matched
- match_type (exact, normalized, fuzzy, cross_ref)
- vendor_name
- vendor_name_normalized
- canonical_vendor_id (optional)

### Source summary
- primary_source_type
- primary_source_name
- source_reference
- source_first_seen_at
- source_last_seen_at

### Buyer usefulness
- contact_name
- contact_email
- contact_phone
- contact_url
- location
- notes_for_buyer
- suggested_next_action

### Lead-confidence
- confidence_score (0-100)
- confidence_band (high, medium, low)
- freshness_score
- source_reliability_score
- contactability_score
- historical_success_score

### Explainability
- reason_summary
- risk_flags
- evidence_count
- corroborated (bool)

### Vendor-safety
- vendor_safety_score (0-100 or inverse risk)
- vendor_safety_band (low_risk, medium_risk, high_risk, unknown)
- vendor_safety_summary
- vendor_safety_flags
- vendor_safety_last_checked_at

### Workflow
- buyer_status (new, contacted, replied, no_stock, has_stock, bad_lead, do_not_contact)
- buyer_owner_user_id
- last_buyer_action_at
- buyer_feedback_summary

### Audit
- created_at
- updated_at

## 7. Evidence Schema (MVP)
### Identity
- evidence_id
- lead_id

### Source
- signal_type
- source_type
- source_name
- source_reference

### Observed details
- part_number_observed
- vendor_name_observed
- observed_text
- observed_at
- freshness_age_days

### Scoring contribution
- weight
- confidence_impact
- explanation

### Trust / verification
- source_reliability_band
- verification_state (raw, inferred, buyer_confirmed, rejected)

### Audit
- created_at

## 8. Source Strategy

### API / posting sources
Use for:
- direct stock-likelihood signal
- freshness
- structured part matches
- vendor discovery

Trust role:
- high contributor to stock-confidence
- medium contributor to vendor-safety

### ICSource / NetComponents / similar marketplaces
Use for:
- part-specific vendor discovery
- corroboration
- contact finding

Trust role:
- medium-high contributor to stock-confidence
- medium contributor to vendor-safety

### Salesforce history
Use for:
- relationship memory
- prior successful engagement
- contact enrichment
- prior quote / deal evidence

Trust role:
- medium contributor to stock-confidence
- high contributor to vendor-safety

### Avail internal history
Use for:
- internal sourcing memory
- prior activity / quote history
- response patterns
- buyer feedback history

Trust role:
- medium contributor to stock-confidence
- high contributor to vendor-safety

### AI/current web search
Use for:
- discovery of new vendors
- enrichment of contact info
- corroboration
- safety review and business-footprint analysis

Trust role:
- low-medium contributor to stock-confidence
- medium contributor to vendor-safety, with caution

## 9. Scoring Model

### Two separate dimensions
1. Lead Confidence — how likely the vendor may currently have stock
2. Vendor Safety — how comfortable the buyer should feel contacting or relying on the vendor

### Lead-confidence factors
Positive:
- trusted source
- exact / normalized match
- recent signal
- multiple-source corroboration
- historical vendor success
- usable contact information
- prior buyer-confirmed good outcomes

Negative:
- stale signal
- fuzzy-only match
- no contact path
- low-trust source
- prior bad-lead outcomes
- repeated no-stock outcomes

### Vendor-safety factors
Positive:
- known vendor in Salesforce / Avail
- consistent identity across sources
- stable domain/contact information
- valid business footprint
- buyer-confirmed positive history

Negative:
- conflicting phone/email/domain
- no business footprint
- suspicious domain patterns
- public complaint reports
- prior internal bad experiences
- repeated do-not-contact outcomes

### Buyer-visible outputs
- confidence score
- confidence band
- vendor-safety band
- reason summary
- risk / caution flags
- suggested next action

## 10. Deduplication Policy
Objective: avoid showing the same vendor multiple times under slightly different names.

### Deduplication levels
1. Exact duplicate — merge automatically
2. Strong likely duplicate — merge automatically if two medium signals agree
3. Possible duplicate — flag only, do not auto-merge

### Strong merge signals
- same canonical_vendor_id
- same normalized domain
- same normalized phone
- same normalized vendor name plus same part
- same CRM/internal record reference

### Medium signals
- very similar normalized vendor name
- same city/state
- same contact email domain
- same contact person
- same website root
- same matched part

### Guardrails
- dedupe primarily at vendor + part level
- preserve all source attribution on merge
- prefer false negatives over false positives
- mark ambiguous cases as `duplicate_candidate`

## 11. Buyer Workflow
Statuses:
- New
- Contacted
- Replied
- No Stock
- Has Stock
- Bad Lead
- Do Not Contact

### Core buyer actions
- Mark Contacted
- Mark Replied
- Mark Has Stock
- Mark No Stock
- Mark Bad Lead
- Mark Do Not Contact
- Add Note

### Stored feedback
- status
- note
- timestamp
- optional reason code
- contact method
- contact attempt count

## 12. Feedback Loop
Purpose: improve future ranking and source weighting from buyer outcomes.

### Positive outcomes
- had stock
- replied quickly
- contact info valid
- source produced a useful lead

### Negative outcomes
- no stock
- stale
- bad lead
- invalid contact info
- duplicate
- do not contact

### MVP learning philosophy
Use rule-based weighting before any machine-learning layer.

## 13. Vendor Trust & Safety Review
Purpose: help buyers avoid wasting time or taking unnecessary risk with new or weakly known vendors.

### Safety review output
- vendor_safety_band
- vendor_safety_summary
- vendor_safety_flags
- evidence list / references
- caution-oriented suggested action

### Suggested caution language
Use:
- risk indicators found
- identity consistency is weak
- manual verification recommended

Avoid:
- definitive accusations or labels

## 14. Main Screens / Wireframe Spec

### Screen A — Sourcing Results View
Purpose: buyer work queue for a part.
Show:
- requested part
- run timestamp
- filters
- sort
- lead cards with confidence, safety, source badges, freshness, contact preview, suggested action, status, and quick actions

### Screen B — Lead Detail View
Purpose: full evidence and decision support.
Show:
- lead summary
- why this lead exists
- all source attribution
- contact info
- safety review
- buyer actions
- lead activity timeline

### Screen C — Buyer Follow-Up Queue
Purpose: work leads by status.
Views:
- New
- Contacted
- Replied
- Has Stock
- No Stock
- Bad Lead
- Do Not Contact

### Screen D — Safety Review Block
Purpose: show caution signals without hiding leads.
Show:
- safety band
- summary
- flags
- recommended caution action

## 15. Acceptance Criteria

### Discovery quality
- multiple source types can contribute leads
- source attribution is preserved
- duplicate vendor noise is reduced

### Buyer usability
- buyers can quickly understand why a lead exists
- buyers can act on leads directly
- buyers can update status and outcomes easily

### Safety/trust
- new vendors can show caution flags
- safety review is visible and explainable
- risky vendors are warned, not silently hidden

### Learning
- buyer outcomes are captured
- future scoring can use past results

## 16. Risks and Constraints
Product risks:
- too many weak leads
- duplicate noise
- opaque score output
- noisy safety review

Technical risks:
- source connector fragility
- scraping/legal/rate-limit issues
- stale data
- entity resolution mistakes
- hard-to-explain scoring logic

Operational risks:
- buyers not entering feedback
- inconsistent outcome tagging
- excessive false alarms in safety review

## 17. Phased Build Plan

### Phase 1 — discovery and current-state mapping
Inventory existing sourcing code, flows, data models, and UI.

### Phase 2 — lead + evidence schema foundation
Create or extend storage models to support one lead per vendor per part plus many evidence items.

### Phase 3 — scoring separation
Implement confidence and vendor-safety as separate streams with explainable outputs.

### Phase 4 — deduplication foundation
Normalize vendors and merge only on strong signals.

### Phase 5 — buyer workflow
Support statuses, quick actions, and note capture.

### Phase 6 — feedback loop
Persist buyer outcomes and use simple rule-based adjustments.

### Phase 7 — buyer-facing UI
Implement sourcing results, detail panel, queue, and safety block.

### Phase 8 — connector hardening & enrichment
Improve connectors, source attribution, freshness, and contact enrichment.

### Phase 9 — QA and cleanup
Run targeted tests, remove dead code, harden edge cases.

## 18. Open Questions
- Which lead sources are truly in MVP vs later?
- Should evidence be stored in a table, JSON, or both?
- Which safety-review signals are allowed / practical?
- How aggressive should dedupe be in v1?
- Which screens already exist and should be extended vs replaced?
- What exact buyer notes/status UX best fits the current Avail UI?
