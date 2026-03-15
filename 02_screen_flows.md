# React Component Tree Proposal

## Top-Level

- `SourcingPage`
  - `SourcingHeader`
  - `SourcingFiltersBar`
  - `SourcingResultsPanel`
    - `LeadList`
      - `LeadCard`
        - `ConfidenceBadge`
        - `SafetyBadge`
        - `SourceBadgeList`
        - `FreshnessPill`
        - `ContactPreview`
        - `SuggestedActionChip`
        - `BuyerStatusBadge`
        - `RiskFlagList`
        - `LeadQuickActions`
  - `LeadDetailDrawer`
    - `LeadSummaryHeader`
    - `WhyFoundSection`
    - `EvidenceSection`
      - `EvidenceGroup`
      - `EvidenceItem`
    - `SourceAttributionSection`
    - `ContactSection`
    - `SafetyReviewSection`
    - `BuyerActionsSection`
    - `LeadActivityTimeline`
  - `BuyerQueueView` (route or tab)
    - `BuyerQueueHeader`
    - `BuyerQueueTabs`
    - `BuyerQueueList`
      - `BuyerQueueItem`

## Shared Components

- `Badge`
- `StatusBadge`
- `IconLabel`
- `SectionCard`
- `EmptyState`
- `LoadingState`
- `InlineNoteEditor`
- `ActionButtonRow`
- `Timeline`
- `TimelineItem`

## Suggested Component Responsibilities

### `LeadCard`
- compact buyer-facing summary
- no deep logic
- accepts normalized lead view model
- handles only surface actions

### `LeadDetailDrawer`
- full context
- evidence and safety review
- buyer action entry
- status history

### `SourcingFiltersBar`
- controlled state
- emits query/filter changes upward
- no business logic

### `BuyerQueueView`
- operational follow-up workflow
- grouped by status
- fast status change affordances

## Component Design Rule
Do not let one component own:
- lead fetching
- scoring logic
- dedupe logic
- status mutation
- detail rendering
all at once.

Keep domain logic in hooks/services, not in giant UI components.
