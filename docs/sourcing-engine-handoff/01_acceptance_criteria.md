# Acceptance Criteria

## Product readiness
- Buyers can open a part and see vendor leads ranked by lead-confidence.
- Buyers can also see a separate vendor-safety assessment.
- Every lead preserves source attribution and reason summary.
- Duplicates are materially reduced.
- Buyers can update status in one click plus optional note.
- Buyer outcomes are stored and visible in history.
- Weak or risky vendors are warned, not silently hidden.

## Data readiness
- One lead exists per vendor per part.
- Multiple evidence items can attach to a single lead.
- Confidence and safety fields are stored separately.
- Source timestamps and references are preserved.
- Buyer status and outcome metadata are persisted.

## UX readiness
- Results view supports filters and sort by confidence, safety, freshness, and status.
- Lead detail view shows source evidence and safety review.
- Follow-up queue supports status-based work.
- Quick actions exist for Contacted, Replied, Has Stock, No Stock, Bad Lead, and Do Not Contact.

## Operational readiness
- Connectors fail gracefully.
- Source weighting is configurable.
- Buyer feedback can influence future ranking.
- Safety review uses caution language and show-your-work evidence.
