# API Contract Expectations for React UI

This file describes the UI-facing contract, not necessarily the current backend.

## Results list endpoint should ideally return

- lead id
- vendor identity
- requested part / matched part
- confidence score + band
- safety score + band
- reason summary
- source badges
- freshness
- contact preview
- buyer status
- caution flags
- suggested next action
- evidence count
- corroborated state

## Lead detail endpoint should ideally return

- summary header data
- full evidence list
- grouped source attribution
- safety review
- buyer activity timeline
- full contact info
- status history

## Status update mutation
Input:
- lead id
- new status
- optional note
- optional structured reason

## Feedback mutation
Input:
- lead id
- outcome signal
- optional reason code
- note

## Important UI contract rules

- confidence and safety must be separate fields
- source attribution must not be hidden inside one opaque string
- evidence should support readable rendering
- suggested next action should be returned or derivable
