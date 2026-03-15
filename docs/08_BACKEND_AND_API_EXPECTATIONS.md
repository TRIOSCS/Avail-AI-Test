# Backend and API Expectations

## Results list contract should ideally expose
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

## Lead detail contract should ideally expose
- summary header data
- grouped evidence list
- source attribution
- safety review
- buyer activity timeline
- full contact information
- status history

## Mutations
### Status update
Input:
- lead id
- new status
- optional note
- optional structured reason

### Feedback
Input:
- lead id
- outcome signal
- optional reason code
- note

## Important rules
- do not collapse confidence and safety into one opaque field
- do not hide source attribution inside one free-text string
- structure evidence so it can be rendered clearly
