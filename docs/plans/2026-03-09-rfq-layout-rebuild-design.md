# RFQ Layout Rebuild + Task Sidebar Restyle

## Context
The UX rebuild deleted/restructured too much. Rolling back RFQ layout redesign and rebuilding with clear requirements.

## Main View Modes
- **Sales** (renamed from "rfq") — customer-focused: customer, quote status, bid due, offers, age
- **Purchasing** (renamed from "sourcing") — buyer-focused: parts, sightings, RFQs sent, response rate, vendor coverage
- **Archive** — unchanged

## Sub-tabs (drill-down)
Restore original separate tabs (no consolidation):
- **Sales**: Parts, Offers, Quotes, Tasks, Files
- **Purchasing**: Details, Sightings, Activity, Offers, Tasks, Files
- **Archive**: Parts, Offers, Quotes, Activity, Tasks, Files

## Card Styling
- Default: white background, light border
- Nearly late (within 24-48h of bid due): soft red (#FEF2F2, border #FECACA)
- No grey/beige

## Removed
- Priority lanes
- Onboarding banner / notification bar
- Lane collapse localStorage state

## Kept
- Split-pane layout
- Current spacing
- Inline RFQ sticky bar
- Strategic Vendors
- Task board (Kanban)

## Task Sidebar
- 240px width, white background, subtle left border
- Structured sections: Overdue (red), Due Today (amber), Upcoming
- Compact task cards: title, requisition name, due date, type pill
- Filter out auto-generated noise tasks
