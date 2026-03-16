# AVAIL Frontend Redesign — World-Class Plan

**Date:** 2026-03-16
**Perspective:** Senior UX architect + 20-year veteran electronic component broker
**Premise:** Ignore what's currently built. Design the frontend a buyer deserves.

---

## The Broker's Truth

I've spent decades sourcing hard-to-find semiconductors. Here's what matters:

**Speed kills deals.** When a buyer at Trio gets a customer request for 5,000 units of an obsolete Samsung NAND flash, the first broker who finds stock and makes the call wins. Every extra click, every page load, every moment spent hunting for a phone number is money left on the table.

**Trust is everything.** In this industry, you're one bad vendor away from shipping counterfeits. The UI must make risk visible without being alarmist. A buyer needs to know "this vendor has delivered before" vs "we've never heard of this company" at a glance.

**Context is king.** A good broker doesn't just search — they remember. "Oh, GlobalTech had Samsung memory last month." "This vendor burned us on lead times." The UI should surface this institutional memory automatically.

**The phone is still the weapon.** Email RFQs are volume plays. But when you find a real lead on a hard-to-find part, you pick up the phone. Contact info must be one click away, always.

---

## Design Philosophy

### 1. Bloomberg Terminal Meets Modern SaaS
The best sourcing UI should feel like a Bloomberg terminal for parts — dense, fast, keyboard-navigable — but wrapped in modern, clean design. Brokers don't want whitespace-heavy dashboards. They want **information density with hierarchy**.

### 2. Three-Second Rule
From any screen, a buyer should be able to answer these questions in under 3 seconds:
- **Where am I?** (breadcrumb + page title)
- **What needs my attention?** (badges, counts, urgency indicators)
- **What should I do next?** (suggested actions, prominent CTAs)

### 3. The Sourcing Engine is the Product
Everything else — requisitions, companies, quotes — is scaffolding. The sourcing results view is where buyers live. It must be the best screen in the app.

---

## Architecture Decision: HTMX + Alpine.js + Tailwind

**Not React. Not Vue. Here's why:**

| Factor | HTMX + Alpine | React SPA |
|--------|--------------|-----------|
| Team size needed | 1-2 devs | 3-5 devs |
| Time to ship | Weeks | Months |
| Server-side rendering | Free (Jinja2) | Complex (Next.js/SSR) |
| Real-time updates | SSE + HTMX swap | WebSocket + state management |
| Learning curve | HTML-first | JS ecosystem knowledge |
| Bundle size | ~30KB | 200KB+ |
| Backend coupling | Tight (good for small team) | Loose (good for large team) |

For a 1-2 person team building a tool for 5-15 internal buyers, HTMX is the correct choice. The existing Vite + Tailwind + Alpine infrastructure stays. We rebuild every template from scratch.

---

## Phase 0: Design System Foundation (Day 1-2)

### Brand Palette
Already defined in design docs. Steel blue from the AVAIL logo:

```
brand-500: #3d6895  (primary)
brand-700: #2b4c6e  (sidebar)
brand-900: #142a40  (sidebar active)
```

### Component Library (Tailwind @apply classes in styles.css)

Build these 12 reusable primitives before touching any page:

| Component | Purpose | Key Behavior |
|-----------|---------|-------------|
| `btn-primary` | Primary actions | brand-500 bg, white text, hover brand-600 |
| `btn-secondary` | Secondary actions | white bg, brand-500 border |
| `btn-danger` | Destructive actions | rose-600 bg |
| `badge-{status}` | Status indicators | 5 semantic colors (success/warning/danger/info/neutral) |
| `source-badge-{name}` | Connector source labels | 7 distinct hues per connector |
| `card` | Content container | white bg, brand-200 border, rounded-lg |
| `stat-card` | Metric display | Large number + label + optional trend |
| `data-table` | Sortable tables | Sticky header, hover rows, compact density |
| `filter-pills` | Toggle filters | Active = brand-500 fill, inactive = outline |
| `confidence-bar` | 0-100% visual | Green ≥70, amber ≥40, rose <40 |
| `safety-badge` | Risk indicator | Low/Medium/High with emerald/amber/rose |
| `input-search` | Search inputs | Magnifying glass icon, 300ms debounce |

### Shared Partials (Build Once, Use Everywhere)

```
partials/shared/
├── sidebar.html          # Collapsible nav, 9 items, brand colors, AVAIL logo
├── topbar.html           # Breadcrumb (OOB swap) + global search + notifications + user menu
├── mobile_nav.html       # Bottom 5-item nav bar
├── modal.html            # Alpine x-trap, escape-close, backdrop-close
├── toast.html            # Auto-dismiss, color-coded, top-right
├── pagination.html       # HTMX prev/next with page count
├── empty_state.html      # Icon + message + optional CTA
├── safety_review.html    # Positive/caution signals, recommendation
├── enrich_button.html    # One-click enrich with spinner
├── offer_card.html       # Expandable offer with evidence tier
└── search_results.html   # Global search dropdown (grouped by type)
```

---

## Phase 1: App Shell (Day 2-3)

### Sidebar — The Command Center

**Collapsed state** (64px): Icons only. One-click expand.
**Expanded state** (256px): Icon + label. AVAIL logo at top.

```
Navigation:
─────────────────────
[AVAIL logo]
─────────────────────
📋 Requisitions        ← Where work starts
🔍 Part Search         ← Ad-hoc lookup
🛒 Buy Plans           ← Procurement workflow
─── Relationships ─────
🏢 Vendors             ← Supplier master
🏛️ Companies           ← Customer/prospect CRM
─── Pipeline ──────────
📄 Quotes              ← Quote lifecycle
🧭 Prospecting         ← New business
─────────────────────
⚙️ Settings
─────────────────────
[User avatar + name]
[Logout]
```

**Why this order:** Requisitions → Search → Buy Plans is the daily workflow. Vendors and Companies are reference data. Quotes and Prospecting are pipeline management.

### Topbar — Context + Quick Access

```
┌──────────────────────────────────────────────────────────────────┐
│ Requisitions > REQ-2024-0142    [🔍 Search parts, vendors...]  🔔 [👤]│
└──────────────────────────────────────────────────────────────────┘
```

- **Breadcrumb** updates via OOB swap — every partial pushes its breadcrumb
- **Global search** with 300ms debounce — searches requisitions, companies, vendors. Results grouped. Click navigates.
- **Notification bell** — count badge, dropdown
- **User menu** — profile, settings, logout

### Mobile — Phone-First for Field Buyers

Some buyers are on the road visiting vendors. Mobile must work.

- Hamburger → sidebar overlay
- Bottom nav: Requisitions, Search, Buy Plans, Vendors, Companies
- Tables → card layout below 768px
- 44px touch targets
- Full-screen modals

---

## Phase 2: The Sourcing Engine (Day 3-7) — THE MONEY SCREEN

This is where Avail wins or loses. Everything else is infrastructure for this moment: a buyer looks at a part and decides who to call.

### Sourcing Results View

**Entry:** Click "Search" on a requirement row in a requisition.

```
┌─────────────────────────────────────────────────────────────────────────┐
│ ◀ REQ-2024-0142                                                        │
│ XC7A200T-2FBG676I  •  Xilinx  •  Searched 6 sources  •  12 leads      │
│ Run: 2026-03-16 14:32  •  Elapsed: 4.2s                               │
├─────────────────────────────────────────────────────────────────────────┤
│ Confidence: [All] [High] [Med] [Low]    Safety: [All] [Low Risk] [Med] │
│ Freshness: [24h] [7d] [30d] [All]       Source: [BB ✓] [Nexar ✓] ...  │
│ Status: [New] [Contacted] [Has Stock]   Sort: [Best Overall ▼]        │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │ ABC Components                          Freshness: 2 days ago   │   │
│  │ ██████████████░░ 87%  [LOW RISK]  [BrokerBin] [Nexar]         │   │
│  │ CORROBORATED (3 signals)                                        │   │
│  │ Qty: 1,200  •  $14.50/ea  •  Contact: sarah@abc.com           │   │
│  │ "Recent exact match on NetComponents, prior Salesforce history" │   │
│  │ Suggested: Send RFQ  →  [View Detail] [Contacted] [Has Stock]  │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │ Global Semi                             Freshness: Today        │   │
│  │ ████████████░░░░ 72%  [UNKNOWN]  [BrokerBin]                   │   │
│  │ Qty: 500  •  RFQ  •  Contact: info@globalsemi.com             │   │
│  │ "API listing, no prior relationship, contact info limited"      │   │
│  │ Suggested: Call vendor  →  [View Detail] [Contacted] [Dismiss]  │   │
│  └─────────────────────────────────────────────────────────────────┘   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Why Cards, Not Table Rows

A broker scanning leads needs to absorb 6+ data points per vendor: confidence, safety, source, freshness, contact, reasoning. Table rows force horizontal scanning. Cards allow vertical grouping of related info. The wireframes confirm this.

### Lead Card Anatomy (Each card contains):

1. **Vendor name + domain** — who is this?
2. **Confidence bar** — visual 0-100%, color-coded (emerald/amber/rose)
3. **Safety badge** — LOW RISK / MEDIUM RISK / HIGH RISK / UNKNOWN
4. **Source badges** — colored pills per connector (sky=BB, violet=Nexar, etc.)
5. **Corroboration indicator** — "Corroborated (3 signals)" in emerald if multi-source
6. **Freshness** — relative time ("2 hours ago", "3 days ago")
7. **Qty + Price** — from best sighting, or "RFQ" if unknown
8. **Contact preview** — first email or phone, truncated
9. **Reasoning summary** — one-line explanation of why this lead exists
10. **Suggested next action** — "Send RFQ" / "Call vendor" / "Research contact"
11. **Quick action buttons** — Contacted, Has Stock, No Stock, Dismiss

### SSE Streaming Progress (Real-Time Search)

When search kicks off, SSE streams connector progress:

```
┌──────────────────────────────────┐
│ Searching 6 sources...           │
│ ✓ BrokerBin    42 results  1.2s │
│ ✓ Nexar        18 results  2.1s │
│ ⏳ DigiKey     searching...      │
│ ✓ Mouser       3 results   1.8s │
│ ⏳ OEMSecrets  searching...      │
│ ✓ Element14    0 results   0.9s │
│ ████████████░░░░ 4/6 complete    │
└──────────────────────────────────┘
```

Results stream in as each source completes — buyers see leads appearing in real-time. This is the "Bloomberg ticker" moment.

### Lead Detail View (Click into a card)

```
┌─────────────────────────────────────────────────────────────────────┐
│ ◀ Back to results                                                   │
│                                                                     │
│ ┌─────────────────────┐  ┌──────────────────────────────────────┐  │
│ │ Lead Summary        │  │ Source Attribution                    │  │
│ │ ABC Components      │  │ Source      │ Type    │ Date  │ What │  │
│ │ XC7A200T-2FBG676I   │  │ NetComp    │ Market  │ 03/12 │ ...  │  │
│ │ [HIGH 87%] [LOW RISK]│  │ Salesforce │ CRM     │ 12/10 │ ...  │  │
│ │ Status: New          │  │ Avail      │ History │ 11/22 │ ...  │  │
│ │ Next: Send RFQ       │  │ Website    │ Web     │ 03/14 │ ...  │  │
│ └─────────────────────┘  └──────────────────────────────────────┘  │
│                                                                     │
│ ┌────────────────┐ ┌──────────────────┐ ┌────────────────────────┐ │
│ │ Evidence       │ │ Contact Info     │ │ Safety Review          │ │
│ │ ● Exact match  │ │ Sarah Lee        │ │ [LOW RISK]             │ │
│ │   NetComp, 2d  │ │ sales@abc.com    │ │ Consistent identity    │ │
│ │ ● SF history   │ │ (714) 555-1212   │ │ across CRM, web, and  │ │
│ │   prior win    │ │ abccomponents.com│ │ marketplace sources.   │ │
│ │ ● Quote resp   │ │ Anaheim, CA      │ │ ✓ Business website     │ │
│ │   within 24h   │ │                  │ │ ✓ Email matches domain │ │
│ │ ● Validated    │ │ [📞 Call]        │ │ ⚠ No complaint signals │ │
│ │   contact info │ │ [✉️ Send RFQ]    │ │ Action: Standard       │ │
│ └────────────────┘ └──────────────────┘ └────────────────────────┘ │
│                                                                     │
│ ┌──────────────────────────────────────────────────────────────────┐│
│ │ Buyer Actions                                                    ││
│ │ [Contacted] [Replied] [Has Stock] [No Stock] [Bad Lead] [DNC]   ││
│ │ [+ Add Note]                                                     ││
│ │                                                                  ││
│ │ Timeline:                                                        ││
│ │ • Lead created from sourcing run          Mar 16, 2:32 PM       ││
│ │ • Opened by buyer                         Mar 16, 2:35 PM       ││
│ └──────────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────────┘
```

### The Broker's Details That Matter

- **Evidence sorted by confidence impact** — strongest signals first
- **Contact info has click-to-call** — tel: links + 8x8 logging
- **Safety is caution-oriented** — never says "scammer," says "verify identity"
- **Suggested next action is context-aware**: has email → "Send RFQ", has phone only → "Call vendor", no contact → "Research contact"
- **Status is lightweight** — buyer's personal notes, not a workflow engine

---

## Phase 3: Requisitions — The Work Hub (Day 7-10)

### List View — The Morning Dashboard

This is where buyers start their day. "What needs work?"

```
┌────────────────────────────────────────────────────────────────────────┐
│ Requisitions                                        [+ New Requisition]│
├────────────────────────────────────────────────────────────────────────┤
│ [All] [Open ●23] [Awarded ●5] [Archived]                             │
│ Owner: [All ▼]  Urgency: [All] [Normal] [Hot] [Critical]             │
│ Date: [From ___] [To ___]                                             │
│ 🔍 Search requisitions...                                             │
├──┬──────────────┬──────────────┬───────┬───┬────┬─────────┬──────────┤
│☐ │ Name         │ Customer     │ Owner │ # │ Off│ Status  │ Created  │
├──┼──────────────┼──────────────┼───────┼───┼────┼─────────┼──────────┤
│☐ │ Samsung NAND │ TechCorp     │ Mike  │ 3 │ 7  │ 🟢 Open │ Mar 15   │
│☐ │ Xilinx FPGAs │ DataSys 🔴  │ Sarah │ 8 │ 2  │ 🟢 Open │ Mar 14   │
│☐ │ Intel CPUs   │ ServerPro    │ Mike  │ 1 │ 12 │ 🟡 Awd  │ Mar 10   │
├──┴──────────────┴──────────────┴───────┴───┴────┴─────────┴──────────┤
│ ☑ 2 selected: [Archive] [Assign ▼] [Activate]                        │
│ Page 1 of 5  [< Prev] [Next >]                                       │
└────────────────────────────────────────────────────────────────────────┘
```

Key features:
- **Filter state preserved in URL** — bookmarkable, shareable
- **Bulk operations** — select multiple, archive/assign/activate
- **Parts count + Offers count** visible — shows work remaining
- **Urgency badges** — 🔴 Critical inline with customer name
- **Role-based visibility** — sales users see only their own reqs (server-enforced)

### Detail View — Tabbed Workspace

```
Tabs: [Parts] [Offers] [Quotes] [Buy Plans] [Tasks] [Activity]
```

- **Parts tab** (default): Requirements table with inline editing (double-click), "Search" button per row, add requirement inline
- **Offers tab**: Received vendor offers with confidence badges
- **Quotes tab**: Generated quotes with margin indicators
- **Buy Plans tab**: Linked procurement plans
- **Tasks tab**: Kanban-lite task board (To Do / In Progress / Done)
- **Activity tab**: Timeline of all actions

---

## Phase 4: CRM Pages (Day 10-14)

### Companies — Know Your Customers

List: searchable table with account type badges (Customer/Prospect/Partner/Competitor). Click → detail.

Detail: Header card → quick info grid (owner, terms, phone) → tabs (Sites, Contacts, Requisitions, Activity). **Enrich button** fires all sources in parallel, Claude merges at 90% confidence gate.

### Vendors — Know Your Suppliers

List: searchable table with **"Hide blacklisted" toggle**. Blacklisted vendors shown in rose-tinted rows.

Detail: Header with score/blacklisted badge → 4-stat row (Sightings, Win Rate, Total POs, Avg Response Time) → tabs:
- **Overview**: Safety review block + recent sightings
- **Contacts**: Full contact table with click-to-call
- **Analytics**: Scorecard + offer history + parts summary
- **Offers**: Historical offers from this vendor

### Quotes — The Revenue Engine

List: search + status filter pills (Draft/Sent/Won/Lost). Margin color-coded.

Detail: Inline line item editing (double-click cells). **Offer gallery** — browse received offers, click "Select for Quote" to add as line item. Global markup input. Send/Won/Lost/Revise actions.

### Prospecting — New Business Pipeline

Card grid (not table — prospects need more visual space). Fit score + readiness score bars. Claim/dismiss/enrich actions. SAM.gov data, news mentions, warm intro suggestions.

---

## Phase 5: Part Search + Buy Plans + Settings (Day 14-17)

### Part Search — Quick Lookup

Large search input → "Search All Sources" → results table with source badges. Simple. Fast. Shows elapsed time. This is the ad-hoc complement to requisition-based sourcing.

### Buy Plans — Procurement Workflow

List: status tabs (Draft/Pending/Active/Completed/Cancelled). Detail: 6-stat grid + AI summary box + AI flags (severity-colored) + workflow action bar (Submit/Approve/Reject/Finalize).

### Settings

Tabs: Sources (connector toggles + health), System (admin config), Profile (user info + 8x8 toggle).

---

## Phase 6: Polish + Performance (Day 17-20)

### Keyboard Navigation
- `⌘K` / `Ctrl+K` — global search focus
- `Esc` — close modal/drawer
- Arrow keys — navigate lead cards in sourcing results
- `Enter` — open selected lead detail

### Loading States
- Skeleton screens on page transitions (not spinners)
- SSE streaming for sourcing (spinners only where progress is meaningful)
- `htmx-indicator` on all buttons that trigger server requests

### Accessibility
- All modals trap focus (`x-trap.noscroll`)
- ARIA labels on interactive elements
- Color is never the only differentiator (icons + text alongside badges)
- Screen reader announcements on HTMX swaps

### Performance
- `historyCacheSize: 0` — no stale HTML in memory
- `selfRequestsOnly: true` — no CSRF via HTMX
- Debounced search (300ms)
- Pagination (50/page default, 100 max)
- No lazy-loading images (this app has almost no images)

---

## Implementation Order — Why This Sequence

```
Phase 0: Design system    ← Everything depends on this
Phase 1: App shell        ← Navigation + layout = skeleton
Phase 2: Sourcing engine  ← THE product. Build the best screen first.
Phase 3: Requisitions     ← The work hub that feeds sourcing
Phase 4: CRM pages        ← Companies, vendors, quotes, prospecting
Phase 5: Search + plans   ← Supporting workflows
Phase 6: Polish           ← Keyboard, a11y, performance
```

**Why sourcing before requisitions?** Because the sourcing engine is the highest-value, highest-complexity screen. If we nail it early, every other page is straightforward by comparison. And if we run out of time, buyers can still use a basic requisition list to get to the sourcing engine.

---

## Technical Implementation Approach

### Router Split
The current 6,442-line `htmx_views.py` must be split:

```
app/routers/htmx/
├── __init__.py           # Register all sub-routers
├── shell.py              # Dashboard, global search, base page renders
├── requisitions.py       # Requisition CRUD + tabs + bulk
├── sourcing.py           # Sourcing results + lead detail + SSE
├── companies.py          # Company CRUD + tabs
├── vendors.py            # Vendor CRUD + tabs + analytics
├── quotes.py             # Quote CRUD + line editing + offer gallery
├── buy_plans.py          # Buy plan CRUD + workflow
├── prospecting.py        # Prospect CRUD + claim/enrich
├── search.py             # Part search
└── settings.py           # Settings tabs
```

### Template Organization

```
app/templates/
├── base.html                     # App shell (sidebar + topbar + #main-content)
├── base_page.html                # Standalone pages (login, error)
├── login.html
└── partials/
    ├── shared/                   # 11 reusable components
    ├── dashboard.html            # Welcome + stats + quick actions
    ├── requisitions/             # list, detail, create_modal, req_row, tabs/*
    ├── sourcing/                 # results, lead_card, lead_detail, search_progress
    ├── companies/                # list, detail
    ├── vendors/                  # list, detail
    ├── quotes/                   # list, detail, line_row
    ├── buy_plans/                # list, detail
    ├── prospecting/              # list, detail
    ├── search/                   # form, results
    └── settings/                 # index, sources, system, profile
```

### Testing Strategy

Every phase includes tests. Pattern:

```python
# test_htmx_{domain}.py
def test_list_returns_200(client):
    resp = client.get("/v2/partials/requisitions")
    assert resp.status_code == 200

def test_list_contains_brand_colors(client):
    resp = client.get("/v2/partials/requisitions")
    assert "brand-" in resp.text

def test_detail_has_tabs(client):
    resp = client.get("/v2/partials/requisitions/1")
    assert "tab" in resp.text.lower()

def test_no_cdn_references(client):
    resp = client.get("/v2/requisitions")
    assert "cdn.tailwindcss.com" not in resp.text
```

---

## What "World Class" Means for Avail

It doesn't mean flashy animations or gradient backgrounds. For a component sourcing tool, world class means:

1. **A buyer opens a requisition and sees 12 leads ranked by confidence in under 2 seconds**
2. **Each lead card tells a story** — not just "vendor X has stock" but "vendor X had stock 2 days ago on NetComponents, they've delivered to us before via Salesforce, and their contact info checks out"
3. **One click to call, one click to RFQ** — no hunting through detail views to find a phone number
4. **Safety signals prevent costly mistakes** — the UI warns about untrusted vendors without blocking action
5. **Corroboration builds confidence** — when 3 sources agree a vendor has stock, that's highlighted prominently
6. **The system remembers** — vendor affinity, past outcomes, response times all surface automatically
7. **It works on a phone** — because sometimes you're at a trade show and need to check stock

This is a tool that makes a $500K/year buyer 20% more productive. That's $100K in value per buyer per year. The frontend is the entire user experience of that value.

---

## Estimated Scope

| Phase | Templates | Routes | Tests | Days |
|-------|-----------|--------|-------|------|
| 0: Design system | 11 shared | 0 | 5 | 2 |
| 1: App shell | 3 (base, sidebar, topbar) | 2 | 8 | 1 |
| 2: Sourcing engine | 4 (results, card, detail, progress) | 6 | 12 | 4 |
| 3: Requisitions | 8 (list, detail, modal, row, 4 tabs) | 8 | 10 | 3 |
| 4: CRM pages | 10 (companies, vendors, quotes, prospecting) | 14 | 12 | 4 |
| 5: Search + plans + settings | 7 | 6 | 6 | 3 |
| 6: Polish | 0 (updates to existing) | 0 | 4 | 3 |
| **Total** | **~43 templates** | **~36 routes** | **~57 tests** | **~20 days** |

---

## Success Criteria

The redesign is done when:

- [ ] A buyer can go from "new requisition" to "calling a vendor with stock" in under 60 seconds
- [ ] Sourcing results show confidence, safety, sources, freshness, and next action on every lead
- [ ] Confidence and safety are never conflated — always shown separately
- [ ] Every lead explains WHY it was shown (evidence + source attribution)
- [ ] The Safety Review block uses caution language, never accusation
- [ ] Contact info is one click away on every lead card and vendor detail
- [ ] All 12 pages render correctly with brand colors
- [ ] Mobile works for all core flows (requisitions, search, sourcing, vendors)
- [ ] No CDN dependencies — everything through Vite build
- [ ] All existing backend tests still pass
- [ ] 57+ new frontend tests pass
- [ ] Keyboard shortcuts work (⌘K search, Esc close, arrow nav)
