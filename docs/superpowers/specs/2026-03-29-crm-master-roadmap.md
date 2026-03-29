# CRM Redesign — Master Roadmap

**Created:** 2026-03-29
**Status:** Active
**Goal:** Unify vendor and customer management into a single CRM tab with automated interaction intelligence, smart vendor discovery, and sales performance visibility.

## Context

The app currently has Vendors, Customers, My Vendors, and Prospect as separate bottom nav tabs. The cleanup of dead code simplified layouts and removed split-panel CRM functions. Rather than regressing, we're building forward with a redesigned CRM that combines both under one roof while keeping vendor and customer experiences completely segregated.

## Design Decisions (Confirmed)

- **Single CRM tab** in bottom nav replaces Vendors + Customers + My Vendors
- **Top tab bar** inside CRM: Customers | Vendors — completely segregated experiences
- **Role-based default**: salespeople land on Customers, sourcing lands on Vendors
- **Customer side**: account list with visual staleness indicators (not a work queue)
- **Vendor side**: discovery-focused — "find the right vendor for the right parts"
- **Interaction tracking**: fully automated via Graph (email), Teams, 8x8 (phone) — zero manual logging
- **AI analysis**: coverage, quality, responsiveness, outcome — 4 dimensions of team performance
- **Cadence goal**: every account scanned weekly, absolute max once per month between contacts

## Phases

### Phase 1: CRM Shell + Customer Sales Workspace
**Status:** Designing now
**Scope:**
- New CRM bottom nav tab with Customers | Vendors tab bar
- Customer list redesigned with staleness indicators (overdue/due/recent)
- Wire up existing interaction data (Graph emails, activity logs) as initial cadence signal
- Role-based default tab selection
- Vendor tab initially renders existing vendor list (no rework yet)

### Phase 2: Interaction Intelligence Engine
**Status:** Planned
**Scope:**
- Passive watchers: Microsoft Graph (email + Teams), 8x8 (phone/call logs)
- Interaction log model — normalized records from all channels
- AI analysis layer: reads interaction content, scores quality (real conversation vs voicemail/auto-reply)
- Coverage scoring: are all accounts being touched on cadence?
- Responsiveness scoring: how fast does the team respond to inbound?
- Automatic staleness timer updates from passive signals

### Phase 3: Vendor Discovery Rethink
**Status:** Planned
**Scope:**
- "I have this MPN, show me the best vendors" workflow
- Simplified vendor profiles — less tabs, more signal
- Better search/filtering by part capability, commodity, brand expertise
- Vendor ranking by likelihood they have the part (sighting history, win rate, response time)

### Phase 4: Performance Dashboard
**Status:** Planned
**Scope:**
- Team-level and individual-level metrics
- 4 dimensions: coverage, quality, responsiveness, outcome
- Trend lines over time
- Management visibility into honest interaction data
- Outcome correlation: interactions → RFQs → quotes → orders

## Architecture Notes

- All phases build on HTMX + Alpine.js (no React)
- Interaction intelligence is a backend service; UI consumes via HTMX partials
- Existing models (Company, VendorCard, ActivityLog) are the foundation
- New models needed: InteractionLog (Phase 2), PerformanceMetric (Phase 4)
- htmx_views.py is already 8800+ lines — new CRM routes should go in a dedicated router
