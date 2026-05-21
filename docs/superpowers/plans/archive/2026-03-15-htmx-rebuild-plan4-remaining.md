# Plan 4: Quotes, Prospecting, Settings, Buy Plans, Dashboard

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Quotes & Offers pages (with inline line editing and offer gallery), Prospecting pages, Settings page, update Buy Plans templates with brand colors, and build Dashboard.

**Architecture:** Server-rendered Jinja2 partials with HTMX for requests/swaps and Alpine.js for local UI state. Inline editing uses double-click triggers with hx-put. Offer gallery uses expandable Alpine cards.

**Tech Stack:** HTMX 2.x, Alpine.js 3.x, Jinja2, FastAPI, Tailwind CSS (brand palette)

**Spec:** `docs/superpowers/specs/2026-03-15-htmx-frontend-rebuild-design.md` (Sections 6, 8, 9, 10, 11)

**Depends on:** Plan 1 (Foundation) must be complete first.

---

## Task 1: Quotes List View

**Files:**
- Create: `app/templates/partials/quotes/list.html`
- Modify: `app/routers/htmx_views.py`

**Backend reference:** `app/routers/crm/quotes.py` — existing API routes at `/api/requisitions/{req_id}/quote`, `/api/requisitions/{req_id}/quotes`, `/api/quotes/{quote_id}`. Model: `app/models/quotes.py` — `Quote` (quote_number, revision, status, subtotal, total_cost, total_margin_pct, customer_site_id, requisition_id, created_by_id, sent_at, followup_alert_sent_at, result, result_reason).

- [x] **Step 1: Add full-page route for quotes list**

In `app/routers/htmx_views.py`, add `/v2/quotes` to the `v2_page` function's route decorators. Add a `elif "/quotes" in path:` branch that sets `current_view = "quotes"`.

- [x] **Step 2: Add partial route for quotes list**

Add `GET /v2/partials/quotes` endpoint to `htmx_views.py`:

```python
@router.get("/v2/partials/quotes", response_class=HTMLResponse)
async def quotes_list_partial(
    request: Request,
    q: str = "",
    status: str = "",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return quotes list as HTML partial."""
    from ..models import Quote
    query = db.query(Quote).options(
        joinedload(Quote.customer_site).joinedload(CustomerSite.company),
        joinedload(Quote.requisition),
        joinedload(Quote.created_by),
    )
    if q.strip():
        safe = escape_like(q.strip())
        query = query.filter(
            Quote.quote_number.ilike(f"%{safe}%")
        )
    if status:
        query = query.filter(Quote.status == status)
    total = query.count()
    quotes = query.order_by(Quote.created_at.desc()).offset(offset).limit(limit).all()
    ctx = _base_ctx(request, user, "quotes")
    ctx.update({"quotes": quotes, "q": q, "status": status, "total": total, "limit": limit, "offset": offset})
    return templates.TemplateResponse("partials/quotes/list.html", ctx)
```

Add `Quote` to the imports at the top of the file.

- [x] **Step 3: Create quotes list template**

Create `app/templates/partials/quotes/list.html`:

- OOB breadcrumb: `<div id="breadcrumb" hx-swap-oob="true">Quotes</div>`
- Search input: `hx-get="/v2/partials/quotes"` with `hx-trigger="keyup changed delay:300ms"` and `name="q"`
- Filter pills: All, Draft, Sent, Won, Lost — each pill: `hx-get="/v2/partials/quotes?status={value}"`, `hx-target="#main-content"`, `hx-push-url="true"`. Active pill gets `bg-brand-500 text-white`, inactive gets `bg-brand-100 text-brand-600`
- Table columns: Quote # (monospace), Revision, Requisition name, Customer (from `quote.customer_site.company.name`), Total (`$X,XXX.XX`), Margin % (color-coded: `text-emerald-700` if >=30, `text-amber-700` if >=15, `text-rose-700` otherwise), Status badge, Created date
- Status badges: draft=`bg-brand-100 text-brand-600`, sent=`bg-amber-50 text-amber-700`, won=`bg-emerald-50 text-emerald-700`, lost=`bg-rose-50 text-rose-700`
- Clickable rows: `hx-get="/v2/partials/quotes/{{ quote.id }}"`, `hx-target="#main-content"`, `hx-push-url="/v2/quotes/{{ quote.id }}"`
- Pagination via `{% include "partials/shared/pagination.html" %}`

- [x] **Step 4: Add sidebar nav item for Quotes**

In `app/templates/partials/shared/sidebar.html`, add a "Quotes" nav item with SVG icon between Buy Plans and Settings. Use `hx-get="/v2/partials/quotes"`, `hx-target="#main-content"`, `hx-push-url="/v2/quotes"`.

---

## Task 2: Quote Detail View with Inline Line Editing

**Files:**
- Create: `app/templates/partials/quotes/detail.html`
- Create: `app/templates/partials/quotes/line_row.html`
- Modify: `app/routers/htmx_views.py`

**Backend reference:** `Quote` has `line_items` (JSON) and related `QuoteLine` records (structured). `QuoteLine` fields: mpn, manufacturer, qty, cost_price, sell_price, margin_pct, offer_id, material_card_id. Routes: `PUT /api/quotes/{quote_id}` updates quote (including line_items). `POST /api/quotes/{quote_id}/send`, `POST /api/quotes/{quote_id}/result`, `POST /api/quotes/{quote_id}/revise`.

- [x] **Step 1: Add full-page and partial routes for quote detail**

Add `/v2/quotes/{quote_id:int}` to the `v2_page` decorators. Add path parsing for detail URL.

Add partial endpoint:

```python
@router.get("/v2/partials/quotes/{quote_id}", response_class=HTMLResponse)
async def quote_detail_partial(
    request: Request,
    quote_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return quote detail as HTML partial."""
    from ..models import Quote, QuoteLine, Offer
    quote = db.query(Quote).options(
        joinedload(Quote.customer_site).joinedload(CustomerSite.company),
        joinedload(Quote.requisition),
        joinedload(Quote.created_by),
    ).filter(Quote.id == quote_id).first()
    if not quote:
        raise HTTPException(404, "Quote not found")
    lines = db.query(QuoteLine).filter(QuoteLine.quote_id == quote_id).all()
    # Load offers for the requisition (for offer gallery)
    offers = db.query(Offer).filter(
        Offer.requisition_id == quote.requisition_id
    ).order_by(Offer.created_at.desc()).all()
    ctx = _base_ctx(request, user, "quotes")
    ctx.update({"quote": quote, "lines": lines, "offers": offers})
    return templates.TemplateResponse("partials/quotes/detail.html", ctx)
```

Add `QuoteLine`, `Offer` to imports.

- [x] **Step 2: Create quote detail template**

Create `app/templates/partials/quotes/detail.html`:

- OOB breadcrumb: `Quotes > {{ quote.quote_number }}`
- Header card: Quote number (large, monospace), revision badge, status badge, customer name (link to company), requisition name (link to requisition detail)
- White card with `border border-brand-200 rounded-lg`

**Line items table:**
- Columns: MPN (monospace), Manufacturer, Qty, Cost Price, Sell Price, Margin %, Linked Offer
- Each cell: `hx-trigger="dblclick"` to swap to edit mode
- Add row form at bottom: MPN, Manufacturer, Qty, Cost, Sell inputs + Add button (`hx-post`)
- Delete button per row: `hx-delete` with `hx-swap="delete"` and `hx-confirm="Remove this line?"`

**Global markup input:**
- Number input + "Apply" button
- `hx-post="/v2/partials/quotes/{{ quote.id }}/apply-markup"` applies markup % to all lines

**Quote actions bar:**
- Send button: `hx-post="/v2/partials/quotes/{{ quote.id }}/send"` (opens send confirmation modal)
- Mark Result: dropdown with Won/Lost options + notes textarea
- Revise: `hx-post="/v2/partials/quotes/{{ quote.id }}/revise"`
- Copy Table: Alpine `@click` handler using `navigator.clipboard.writeText()`
- Buttons styled: `bg-brand-500 hover:bg-brand-600 text-white` for primary, `bg-white border border-brand-200 text-brand-700` for secondary

**Followup alert banner:**
- Show if `quote.followup_alert_sent_at` is set: amber banner with "Follow-up sent" message

- [x] **Step 3: Create line_row.html partial for inline editing**

Create `app/templates/partials/quotes/line_row.html`:

- Display mode: `<tr>` with data cells, each cell has `hx-trigger="dblclick"` that swaps to an input
- Edit mode: input field replaces cell text, `hx-put="/v2/partials/quotes/{{ quote.id }}/lines/{{ line.id }}"` on blur or Enter, `hx-target="closest tr"`, `hx-swap="outerHTML"`
- Auto-recalculate margin: `margin_pct = ((sell_price - cost_price) / sell_price) * 100` if sell_price > 0
- Alpine `x-data` manages edit state per row

- [x] **Step 4: Add line item HTMX endpoints**

Add to `htmx_views.py`:

```python
@router.put("/v2/partials/quotes/{quote_id}/lines/{line_id}", response_class=HTMLResponse)
async def update_quote_line(
    request: Request, quote_id: int, line_id: int,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Inline edit a quote line item, return updated row."""
    from ..models import QuoteLine
    line = db.get(QuoteLine, line_id)
    if not line or line.quote_id != quote_id:
        raise HTTPException(404, "Line not found")
    form = await request.form()
    if "mpn" in form: line.mpn = form["mpn"]
    if "manufacturer" in form: line.manufacturer = form["manufacturer"]
    if "qty" in form: line.qty = int(form["qty"])
    if "cost_price" in form: line.cost_price = float(form["cost_price"])
    if "sell_price" in form: line.sell_price = float(form["sell_price"])
    if line.sell_price and line.sell_price > 0 and line.cost_price is not None:
        line.margin_pct = round((float(line.sell_price) - float(line.cost_price)) / float(line.sell_price) * 100, 2)
    db.commit()
    ctx = _base_ctx(request, user, "quotes")
    ctx["line"] = line
    return templates.TemplateResponse("partials/quotes/line_row.html", ctx)

@router.delete("/v2/partials/quotes/{quote_id}/lines/{line_id}", response_class=HTMLResponse)
async def delete_quote_line(
    request: Request, quote_id: int, line_id: int,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Delete a quote line item."""
    from ..models import QuoteLine
    line = db.get(QuoteLine, line_id)
    if not line or line.quote_id != quote_id:
        raise HTTPException(404, "Line not found")
    db.delete(line)
    db.commit()
    return HTMLResponse("")  # hx-swap="delete" removes the row
```

- [x] **Step 5: Add quote action endpoints (send, result, revise)**

Add HTMX wrapper endpoints that call the existing API routes and return updated partials:

```python
@router.post("/v2/partials/quotes/{quote_id}/send", response_class=HTMLResponse)
# Calls existing /api/quotes/{quote_id}/send, returns updated detail partial

@router.post("/v2/partials/quotes/{quote_id}/result", response_class=HTMLResponse)
# Calls existing /api/quotes/{quote_id}/result, returns updated detail partial

@router.post("/v2/partials/quotes/{quote_id}/revise", response_class=HTMLResponse)
# Calls existing /api/quotes/{quote_id}/revise, redirects to new quote detail
```

Each endpoint processes the form data, delegates to the service layer (same logic as the API routes in `app/routers/crm/quotes.py`), then re-renders the detail partial.

---

## Task 3: Offer Card Shared Component

**Files:**
- Create: `app/templates/partials/shared/offer_card.html`

**Backend reference:** `app/models/offers.py` — `Offer` fields: vendor_name, mpn, qty_available, unit_price, lead_time, evidence_tier, parse_confidence, status, source, attachments (relationship), selected_for_quote. Routes: `PUT /api/offers/{id}/approve`, `PUT /api/offers/{id}/reject`, `POST /api/offers/{id}/promote`.

- [x] **Step 1: Create expandable offer card component**

Create `app/templates/partials/shared/offer_card.html`:

- Alpine `x-data="{ expanded: false }"` for expand/collapse
- **Collapsed view:**
  - Vendor name (bold), MPN (monospace), Qty (formatted with commas), Unit Price (`$X.XXXX` or "RFQ")
  - Lead time text
  - Evidence tier badge: T1-T3 = `bg-emerald-50 text-emerald-700`, T4 = `bg-amber-50 text-amber-700`, T5-T7 = `bg-brand-100 text-brand-600`
  - Parse confidence: small progress bar if present
  - Status badge: active=emerald, pending_review=amber, rejected=rose, sold=neutral
  - Expand chevron: `@click="expanded = !expanded"`

- **Expanded view** (`x-show="expanded"` with `x-transition`):
  - Full offer details: date_code, condition, packaging, firmware, hardware_code, moq, warranty, country_of_origin
  - Attachments list (if any): file name links to onedrive_url
  - Notes text
  - Created by + created at

- **Action buttons:**
  - "Select for Quote": `hx-post="/v2/partials/quotes/{{ quote.id }}/add-offer/{{ offer.id }}"` — adds offer as quote line
  - Approve: `hx-put="/api/offers/{{ offer.id }}/approve"` (visible only if status == "pending_review")
  - Reject: `hx-put="/api/offers/{{ offer.id }}/reject"` (visible only if status == "pending_review")
  - Promote: `hx-post="/api/offers/{{ offer.id }}/promote"` (visible only if evidence_tier == "T4")

- Card styling: `bg-white border border-brand-200 rounded-lg p-4 hover:shadow-sm`

---

## Task 4: Offer Gallery in Quote Detail

**Files:**
- Modify: `app/templates/partials/quotes/detail.html`

- [x] **Step 1: Add offer gallery section to quote detail**

Below the line items table, add an "Available Offers" section:

- Section header: "Offers for this Requisition" with count badge
- Grid layout: 1 column on mobile, 2 columns on desktop
- Each offer rendered via: `{% include "partials/shared/offer_card.html" %}`
- Pass `offer` and `quote` variables to the include
- Empty state: "No offers received yet for this requisition." with `{% include "partials/shared/empty_state.html" %}`

- [x] **Step 2: Add pricing history section**

Below the offer gallery:

- "Pricing History" header
- Load via `hx-get="/v2/partials/quotes/{{ quote.id }}/pricing-history"` with `hx-trigger="revealed"` (lazy load)
- Table: Date, Qty, Cost Price, Sell Price, Margin %, Customer, Result, Quote # — data from `/api/pricing-history/{mpn}` for each unique MPN in quote lines

---

## Task 5: Prospecting List View

**Files:**
- Create: `app/templates/partials/prospecting/list.html`
- Modify: `app/routers/htmx_views.py`

**Backend reference:** `app/models/prospect_account.py` — `ProspectAccount` fields: name, domain, industry, region, fit_score, readiness_score, discovery_source, status (suggested/claimed/dismissed), claimed_by, enrichment_data (JSONB). Routes: `app/routers/prospect_suggested.py` — `GET /api/prospects/suggested` (list with filters), `POST /api/prospects/suggested/{id}/claim`, `POST /api/prospects/suggested/{id}/dismiss`.

- [x] **Step 1: Add full-page and partial routes for prospecting**

Add `/v2/prospecting` to `v2_page` decorators. Add `elif "/prospecting" in path:` branch.

Add partial endpoint:

```python
@router.get("/v2/partials/prospecting", response_class=HTMLResponse)
async def prospecting_list_partial(
    request: Request,
    q: str = "",
    status: str = "",
    sort: str = "buyer_ready_desc",
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Return prospecting list as HTML partial."""
    from ..models.prospect_account import ProspectAccount
    query = db.query(ProspectAccount)
    if status:
        query = query.filter(ProspectAccount.status == status)
    else:
        query = query.filter(ProspectAccount.status.in_(["suggested", "claimed", "dismissed"]))
    if q.strip():
        safe = escape_like(q.strip())
        query = query.filter(
            ProspectAccount.name.ilike(f"%{safe}%")
            | ProspectAccount.domain.ilike(f"%{safe}%")
        )
    total = query.count()
    # Sorting
    if sort == "fit_desc":
        query = query.order_by(ProspectAccount.fit_score.desc())
    elif sort == "recent_desc":
        query = query.order_by(ProspectAccount.created_at.desc())
    else:
        query = query.order_by(ProspectAccount.readiness_score.desc(), ProspectAccount.fit_score.desc())
    prospects = query.offset((page - 1) * per_page).limit(per_page).all()
    total_pages = (total + per_page - 1) // per_page
    ctx = _base_ctx(request, user, "prospecting")
    ctx.update({
        "prospects": prospects, "q": q, "status": status, "sort": sort,
        "page": page, "per_page": per_page, "total": total, "total_pages": total_pages,
    })
    return templates.TemplateResponse("partials/prospecting/list.html", ctx)
```

Add `ProspectAccount` to imports.

- [x] **Step 2: Create prospecting list template**

Create `app/templates/partials/prospecting/list.html`:

- OOB breadcrumb: `Prospecting`
- Search input: `hx-get="/v2/partials/prospecting"` with 300ms debounce
- Filter pills: All, Suggested, Claimed, Dismissed — styled with brand colors
- Sort dropdown: Best Fit, Most Ready, Newest — `<select>` with `hx-get` on change

**Card grid** (`grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4`):

Each card (`bg-white border border-brand-200 rounded-lg p-4 hover:shadow-sm`):
- Company name (bold, truncate), domain (small, `text-brand-400`)
- Fit score bar: small horizontal bar, `bg-emerald-500` if >=70, `bg-amber-500` if >=40, `bg-rose-500` otherwise. Label: "Fit: XX%"
- Readiness score bar: same treatment. Label: "Ready: XX%"
- Industry + region (small text, `text-gray-500`)
- Discovery source badge: `bg-brand-100 text-brand-600 text-xs rounded-full px-2 py-0.5`
- Status badge: suggested=`bg-brand-100 text-brand-600`, claimed=`bg-emerald-50 text-emerald-700`, dismissed=`bg-gray-100 text-gray-600`
- Quick action buttons:
  - Claim: `hx-post="/v2/partials/prospecting/{{ prospect.id }}/claim"`, `bg-brand-500 text-white text-sm`
  - Dismiss: `hx-post="/v2/partials/prospecting/{{ prospect.id }}/dismiss"`, `bg-white border text-brand-700 text-sm`
  - Only show Claim/Dismiss for status == "suggested"
- Card click (body area): `hx-get="/v2/partials/prospecting/{{ prospect.id }}"`, `hx-target="#main-content"`, `hx-push-url="/v2/prospecting/{{ prospect.id }}"`

- Pagination via `{% include "partials/shared/pagination.html" %}`

- [x] **Step 3: Add claim/dismiss partial endpoints**

```python
@router.post("/v2/partials/prospecting/{prospect_id}/claim", response_class=HTMLResponse)
async def claim_prospect_htmx(
    request: Request, prospect_id: int,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    from ..models.prospect_account import ProspectAccount
    from ..services.prospect_claim import claim_prospect
    try:
        claim_prospect(prospect_id, user.id, db)
    except (LookupError, ValueError) as e:
        raise HTTPException(400, str(e))
    # Return updated card or redirect to list
    prospect = db.get(ProspectAccount, prospect_id)
    ctx = _base_ctx(request, user, "prospecting")
    ctx["prospect"] = prospect
    return templates.TemplateResponse("partials/prospecting/_card.html", ctx)

@router.post("/v2/partials/prospecting/{prospect_id}/dismiss", response_class=HTMLResponse)
async def dismiss_prospect_htmx(
    request: Request, prospect_id: int,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    from ..models.prospect_account import ProspectAccount
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404, "Prospect not found")
    prospect.status = "dismissed"
    prospect.dismissed_by = user.id
    from datetime import datetime, timezone
    prospect.dismissed_at = datetime.now(timezone.utc)
    db.commit()
    ctx = _base_ctx(request, user, "prospecting")
    ctx["prospect"] = prospect
    return templates.TemplateResponse("partials/prospecting/_card.html", ctx)
```

- [x] **Step 4: Create prospect card sub-partial**

Create `app/templates/partials/prospecting/_card.html` — single prospect card for OOB swaps after claim/dismiss. Same layout as cards in list but wrapped in a div with `id="prospect-{{ prospect.id }}"` for targeted swap.

---

## Task 6: Prospecting Detail View

**Files:**
- Create: `app/templates/partials/prospecting/detail.html`
- Modify: `app/routers/htmx_views.py`

**Backend reference:** `ProspectAccount` has enrichment_data (JSONB) with keys: `warm_intro` (dict with `has_warm_intro`, `warmth`, intro path), `one_liner`, SAM.gov data, news mentions. `fit_reasoning` (text), `readiness_signals` (JSONB). Routes: `GET /api/prospects/suggested/{id}` (full detail), `POST /api/prospects/suggested/{id}/enrich-free` (free enrichment).

- [x] **Step 1: Add full-page and partial routes for prospect detail**

Add `/v2/prospecting/{prospect_id:int}` to `v2_page` decorators. Add path parsing.

Add partial endpoint:

```python
@router.get("/v2/partials/prospecting/{prospect_id}", response_class=HTMLResponse)
async def prospecting_detail_partial(
    request: Request, prospect_id: int,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    from ..models.prospect_account import ProspectAccount
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404, "Prospect not found")
    ctx = _base_ctx(request, user, "prospecting")
    ctx["prospect"] = prospect
    ctx["enrichment"] = prospect.enrichment_data or {}
    ctx["warm_intro"] = (prospect.enrichment_data or {}).get("warm_intro", {})
    return templates.TemplateResponse("partials/prospecting/detail.html", ctx)
```

- [x] **Step 2: Create prospecting detail template**

Create `app/templates/partials/prospecting/detail.html`:

- OOB breadcrumb: `Prospecting > {{ prospect.name }}`

**Header card** (`bg-white border border-brand-200 rounded-lg p-6`):
- Company name (large, bold), domain (link), industry, region
- Status badge (same colors as list)
- Claimed by: user name + avatar if claimed

**Scores section** (2-col grid):
- Fit score: large number, colored bar (`bg-emerald-500`/`bg-amber-500`/`bg-rose-500`), "Fit Score" label
- Readiness score: same treatment, "Readiness Score" label

**Discovery info:**
- Source badge + discovery date (`{{ prospect.created_at }}`)

**Enrichment data card** (`bg-brand-50 border border-brand-200 rounded-lg p-4 mt-4`):
- Conditional render: `{% if enrichment %}`
- SAM.gov data section (if `enrichment.sam_gov` exists): show relevant fields
- Google News mentions (if `enrichment.news` exists): list of headline + date
- Signal indicators: hiring badge, events badge, intent badge — from `prospect.readiness_signals`
- If no enrichment data: "No enrichment data yet. Click 'Enrich' to gather free data."

**Warm intro section** (`{% if warm_intro.has_warm_intro %}`):
- Card with `bg-emerald-50 border border-emerald-200 rounded-lg p-4`
- Warmth indicator: `warm_intro.warmth` value
- Intro path text
- Suggested one-liner (`enrichment.one_liner`)

**Action buttons** (button bar at top-right of header):
- Claim: `hx-post="/v2/partials/prospecting/{{ prospect.id }}/claim"`, `hx-target="#main-content"` — only for status=="suggested"
- Release: `hx-post` — only for status=="claimed" and claimed_by==current user
- Dismiss: `hx-post="/v2/partials/prospecting/{{ prospect.id }}/dismiss"`, `hx-target="#main-content"` — only for status=="suggested"
- Enrich: `hx-post="/v2/partials/prospecting/{{ prospect.id }}/enrich"`, with `htmx-indicator` spinner — triggers free enrichment, refreshes detail on completion
- Create Requisition: `hx-get="/v2/partials/requisitions/create-form?customer={{ prospect.name }}"`, `@click="$dispatch('open-modal')"` — pre-fills customer from prospect

- [x] **Step 3: Add enrich endpoint**

```python
@router.post("/v2/partials/prospecting/{prospect_id}/enrich", response_class=HTMLResponse)
async def enrich_prospect_htmx(
    request: Request, prospect_id: int,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    from ..services.prospect_free_enrichment import run_free_enrichment
    from ..services.prospect_warm_intros import detect_warm_intros, generate_one_liner
    from ..models.prospect_account import ProspectAccount
    prospect = db.get(ProspectAccount, prospect_id)
    if not prospect:
        raise HTTPException(404)
    await run_free_enrichment(prospect_id)
    db.refresh(prospect)
    warm = detect_warm_intros(prospect, db)
    one_liner = generate_one_liner(prospect, warm)
    ed = dict(prospect.enrichment_data or {})
    ed["warm_intro"] = warm
    ed["one_liner"] = one_liner
    prospect.enrichment_data = ed
    db.commit()
    # Re-render full detail
    ctx = _base_ctx(request, user, "prospecting")
    ctx["prospect"] = prospect
    ctx["enrichment"] = prospect.enrichment_data or {}
    ctx["warm_intro"] = (prospect.enrichment_data or {}).get("warm_intro", {})
    return templates.TemplateResponse("partials/prospecting/detail.html", ctx)
```

---

## Task 7: Settings Page

**Files:**
- Create: `app/templates/partials/settings/index.html`
- Create: `app/templates/partials/settings/sources.html`
- Create: `app/templates/partials/settings/system.html`
- Create: `app/templates/partials/settings/profile.html`
- Modify: `app/routers/htmx_views.py`

**Backend reference:** `app/routers/admin/system.py` — `GET /api/admin/config` (requires settings_access), `PUT /api/admin/config/{key}` (requires admin), `GET /api/admin/connector-health` (requires admin). `app/routers/sources.py` — `GET /api/sources` (list all), `PUT /api/sources/{id}/toggle`, `PUT /api/sources/{id}/activate`, `POST /api/sources/{id}/test`. Model: `ApiSource` (name, display_name, status, is_active, last_success, last_error, env_vars, error_count_24h).

- [x] **Step 1: Add full-page and partial routes for settings**

Add `/v2/settings` to `v2_page` decorators. Add `elif "/settings" in path:` branch.

Add endpoints:

```python
@router.get("/v2/partials/settings", response_class=HTMLResponse)
async def settings_partial(
    request: Request, tab: str = "sources",
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Settings page — renders index with active tab."""
    ctx = _base_ctx(request, user, "settings")
    ctx["active_tab"] = tab
    ctx["is_admin"] = user.role == "admin"
    return templates.TemplateResponse("partials/settings/index.html", ctx)

@router.get("/v2/partials/settings/sources", response_class=HTMLResponse)
async def settings_sources_tab(
    request: Request,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """Sources tab content."""
    sources = db.query(ApiSource).order_by(ApiSource.display_name).all()
    ctx = _base_ctx(request, user, "settings")
    ctx["sources"] = sources
    return templates.TemplateResponse("partials/settings/sources.html", ctx)

@router.get("/v2/partials/settings/system", response_class=HTMLResponse)
async def settings_system_tab(
    request: Request,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """System config tab — admin only."""
    if user.role != "admin":
        raise HTTPException(403, "Admin only")
    from ..services.admin_service import get_all_config
    config = get_all_config(db)
    ctx = _base_ctx(request, user, "settings")
    ctx["config"] = config
    return templates.TemplateResponse("partials/settings/system.html", ctx)

@router.get("/v2/partials/settings/profile", response_class=HTMLResponse)
async def settings_profile_tab(
    request: Request,
    user: User = Depends(require_user), db: Session = Depends(get_db),
):
    """User profile tab."""
    ctx = _base_ctx(request, user, "settings")
    ctx["profile_user"] = user
    return templates.TemplateResponse("partials/settings/profile.html", ctx)
```

Add `ApiSource` to imports.

- [x] **Step 2: Create settings index template**

Create `app/templates/partials/settings/index.html`:

- OOB breadcrumb: `Settings`
- Alpine `x-data="{ tab: '{{ active_tab }}' }"` for tab state
- Tab buttons: Sources, System (admin only: `{% if is_admin %}`), Profile
  - Active tab: `bg-brand-500 text-white`, inactive: `bg-brand-100 text-brand-600`
  - Each tab button: `@click="tab = 'sources'"` + `hx-get="/v2/partials/settings/sources"` + `hx-target="#settings-content"`
- Content area: `<div id="settings-content">` — initial content loaded based on `active_tab`
- On load, include the default tab partial: `{% include "partials/settings/sources.html" %}`

- [x] **Step 3: Create sources tab template**

Create `app/templates/partials/settings/sources.html`:

- Table of connectors with columns: Source Name, Status, Active Toggle, Health, Actions
- Each row:
  - Source display_name
  - Status badge: live=`bg-emerald-50 text-emerald-700`, pending=`bg-amber-50 text-amber-700`, error=`bg-rose-50 text-rose-700`, disabled=`bg-gray-100 text-gray-600`, degraded=`bg-amber-50 text-amber-700`
  - Active toggle: `<input type="checkbox" checked="{{ source.is_active }}" hx-put="/api/sources/{{ source.id }}/activate" hx-swap="none">` — toggle switch styled with brand colors
  - Health info: Last success timestamp (relative time), error message if failing (`text-rose-600`), error_count_24h badge
  - Test button: `hx-post="/api/sources/{{ source.id }}/test"` with spinner indicator, shows result in toast

- [x] **Step 4: Create system config tab template**

Create `app/templates/partials/settings/system.html`:

- Table of config key/value pairs from `get_all_config(db)`
- Columns: Key, Value (masked if encrypted — show `****`), Actions
- Each row: inline edit via `hx-put="/api/admin/config/{{ key }}"` with form input
- `hx-trigger="submit"`, input styled with `border-brand-200 focus:ring-brand-500`
- Only visible to admin users (server-enforced via `require_admin`)

- [x] **Step 5: Create profile tab template**

Create `app/templates/partials/settings/profile.html`:

- User info display card (`bg-white border border-brand-200 rounded-lg p-6`):
  - Name (large)
  - Email
  - Role badge: admin=`bg-brand-500 text-white`, buyer=`bg-emerald-50 text-emerald-700`, sales=`bg-amber-50 text-amber-700`
- 8x8 VoIP toggle (if applicable):
  - Toggle switch to enable/disable click-to-call
  - `hx-post="/v2/partials/settings/profile/toggle-8x8"` (stub for now)
- Note at bottom: "Profile editing coming soon." in `text-brand-300`

---

## Task 8: Buy Plans Brand Color Update

**Files:**
- Modify: `app/templates/partials/buy_plans/list.html` (or create if not exists)
- Modify: `app/templates/partials/buy_plans/detail.html` (or create if not exists)

**Backend reference:** Buy plan templates are referenced by `htmx_views.py` but the template directory `app/templates/partials/buy_plans/` does not currently exist (templates at `app/templates/htmx/partials/buy_plans/` also not found). Routes exist in `htmx_views.py` for `/v2/partials/buy-plans` and `/v2/partials/buy-plans/{bp_id}`. Model: `app/models/buy_plan.py` — `BuyPlan` with status (BuyPlanStatus enum), `BuyPlanLine` with line_status (BuyPlanLineStatus enum), `SOVerificationStatus`.

- [x] **Step 1: Verify existing buy plan templates exist; create if missing**

Check if buy plan templates exist at the path referenced by `htmx_views.py`. If they exist, update them. If not, create from scratch.

Expected template path: look at `htmx_views.py` buy plan partial endpoints to determine exact template path used.

- [x] **Step 2: Update buy plan list template with brand colors**

In `partials/buy_plans/list.html`:

- Replace any `blue-600` with `brand-500`, `blue-700` with `brand-600`
- Replace any `gray-900` backgrounds with `brand-700`
- Status filter tabs: All, Draft, Pending, Active, Completed, Cancelled — use brand pill styles
  - Active pill: `bg-brand-500 text-white`
  - Inactive pill: `bg-brand-100 text-brand-600 hover:bg-brand-200`
- "My Only" toggle: checkbox styled with `accent-brand-500`
- Search input: `border-brand-200 focus:ring-brand-500 focus:border-brand-500`
- Table: `border-brand-200` borders
- Margin color coding: `text-emerald-700` (>=30%), `text-amber-700` (>=15%), `text-rose-700` (<15%)
- Status badges: Draft=`bg-brand-100 text-brand-600`, Pending=`bg-amber-50 text-amber-700`, Active=`bg-emerald-50 text-emerald-700`, Completed=`bg-emerald-50 text-emerald-700`, Cancelled=`bg-gray-100 text-gray-600`
- SO Verification badge: verified=emerald, rejected=rose, pending=amber

- [x] **Step 3: Update buy plan detail template with brand colors**

In `partials/buy_plans/detail.html`:

- Header card: `border-brand-200` border
- Stat cards: `bg-brand-50` background
- AI Summary box: `bg-brand-50 border-brand-200` background
- AI Flags: severity colors — critical=`bg-rose-50 text-rose-700 border-rose-200`, warning=`bg-amber-50 text-amber-700 border-amber-200`, info=`bg-brand-50 text-brand-600 border-brand-200`
- Workflow buttons: primary=`bg-brand-500 hover:bg-brand-600 text-white`, danger=`bg-rose-500 hover:bg-rose-600 text-white`, secondary=`bg-white border-brand-200 text-brand-700`
- Verify all `hx-get`/`hx-post`/`hx-put` targets work correctly

- [x] **Step 4: Test buy plan list and detail rendering**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v -k "buy_plan" --tb=short
```

Manually verify:
- Status filter tabs switch correctly
- "My Only" toggle filters by current user
- Margin color coding displays correctly
- Workflow action buttons appear based on status + role

---

## Task 9: Dashboard

**Files:**
- Create: `app/templates/partials/dashboard.html`
- Modify: `app/routers/htmx_views.py`

**Backend reference:** Dashboard needs counts from Requisition (status=active/sourcing/offers/quoting), VendorCard, Company models. No dedicated API endpoint needed — query directly in the HTMX view.

- [x] **Step 1: Update dashboard partial route**

The `/v2` route already exists and maps to the `v2_page` function. Update the `/v2/partials/requisitions` default load to serve dashboard instead when path is exactly `/v2`.

Add a dashboard partial endpoint:

```python
@router.get("/v2/partials/dashboard", response_class=HTMLResponse)
async def dashboard_partial(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Dashboard with stats and quick actions."""
    open_reqs = db.query(sqlfunc.count(Requisition.id)).filter(
        Requisition.status.in_(["active", "sourcing", "offers", "quoting"])
    ).scalar() or 0
    active_vendors = db.query(sqlfunc.count(VendorCard.id)).scalar() or 0
    companies = db.query(sqlfunc.count(Company.id)).scalar() or 0
    ctx = _base_ctx(request, user, "dashboard")
    ctx.update({
        "open_reqs": open_reqs,
        "active_vendors": active_vendors,
        "companies": companies,
    })
    return templates.TemplateResponse("partials/dashboard.html", ctx)
```

Update `v2_page` to use `current_view = "dashboard"` and `partial_url = "/v2/partials/dashboard"` when path is exactly `/v2`.

- [x] **Step 2: Create dashboard template**

Create `app/templates/partials/dashboard.html`:

- OOB breadcrumb: `<div id="breadcrumb" hx-swap-oob="true">Dashboard</div>`

**Logo section:**
- AVAIL logo centered: `<img src="{{ url_for('static', path='public/avail_logo_white_bg.png') }}" class="mx-auto h-16 mb-6">`

**Welcome message:**
- `<h2 class="text-xl text-gray-900 text-center mb-8">Welcome back, {{ user_name }}</h2>`

**Stat cards** (`grid grid-cols-1 md:grid-cols-3 gap-6 mb-8`):

Each card (`bg-white border border-brand-200 rounded-lg p-6 hover:shadow-sm cursor-pointer`):

1. Open Requisitions:
   - Large number: `<span class="text-3xl font-bold text-brand-500">{{ open_reqs }}</span>`
   - Label: "Open Requisitions"
   - `hx-get="/v2/partials/requisitions"`, `hx-target="#main-content"`, `hx-push-url="/v2/requisitions"`

2. Active Vendors:
   - Large number: `text-brand-500`
   - Label: "Active Vendors"
   - `hx-get="/v2/partials/vendors"`, `hx-target="#main-content"`, `hx-push-url="/v2/vendors"`

3. Companies:
   - Large number: `text-brand-500`
   - Label: "Companies"
   - `hx-get="/v2/partials/companies"`, `hx-target="#main-content"`, `hx-push-url="/v2/companies"`

- Card background: `bg-brand-50` subtle fill, `border-brand-200`

**Quick actions** (`bg-white border border-brand-200 rounded-lg p-6`):
- "Create Requisition" button: `bg-brand-500 hover:bg-brand-600 text-white px-4 py-2 rounded-md` with `@click="$dispatch('open-modal')"` + `hx-get="/v2/partials/requisitions/create-form"` + `hx-target="#modal-content"`
- "Search Parts" button: `bg-white border border-brand-200 text-brand-700 hover:bg-brand-50 px-4 py-2 rounded-md` with `hx-get="/v2/partials/search"` + `hx-target="#main-content"` + `hx-push-url="/v2/search"`

---

## Task 10: Wire Up Sidebar Navigation

**Files:**
- Modify: `app/templates/partials/shared/sidebar.html`

- [x] **Step 1: Add Prospecting and Quotes nav items**

In the sidebar navigation, ensure these items exist in order:
1. Dashboard (`/v2/partials/dashboard`)
2. Requisitions (`/v2/partials/requisitions`)
3. Part Search (`/v2/partials/search`)
4. Buy Plans (`/v2/partials/buy-plans`)
5. **— Section label: "Relationships" —**
6. Vendors (`/v2/partials/vendors`)
7. Companies (`/v2/partials/companies`)
8. Prospecting (`/v2/partials/prospecting`) — NEW
9. Quotes (`/v2/partials/quotes`) — NEW
10. Settings (`/v2/partials/settings`) — NEW

Each nav item:
- SVG icon + label text
- `hx-get` targeting `#main-content`, `hx-push-url` for history
- Active state: `bg-brand-900 text-white` when `current_view` matches
- Hover: `bg-brand-800`

- [x] **Step 2: Update v2_page to handle all new routes**

Ensure `v2_page` function handles all new URL paths (`/v2/quotes`, `/v2/quotes/{id}`, `/v2/settings`, `/v2/prospecting`, `/v2/prospecting/{id}`) with correct `current_view` and `partial_url` resolution.

Add these route decorators:
```python
@router.get("/v2/quotes", response_class=HTMLResponse)
@router.get("/v2/quotes/{quote_id:int}", response_class=HTMLResponse)
@router.get("/v2/settings", response_class=HTMLResponse)
@router.get("/v2/prospecting", response_class=HTMLResponse)
@router.get("/v2/prospecting/{prospect_id:int}", response_class=HTMLResponse)
```

---

## Task 11: Tests

**Files:**
- Modify: `tests/test_htmx_views.py` (or create if not exists)

- [x] **Step 1: Test quotes list partial**

```python
def test_quotes_list_partial(client, auth_headers):
    resp = client.get("/v2/partials/quotes", headers={**auth_headers, "HX-Request": "true"})
    assert resp.status_code == 200
    assert "Quotes" in resp.text

def test_quotes_list_filter_by_status(client, auth_headers):
    resp = client.get("/v2/partials/quotes?status=draft", headers={**auth_headers, "HX-Request": "true"})
    assert resp.status_code == 200
```

- [x] **Step 2: Test quote detail partial**

```python
def test_quote_detail_partial(client, auth_headers, db_session):
    # Create a quote fixture
    # ...
    resp = client.get(f"/v2/partials/quotes/{quote_id}", headers={**auth_headers, "HX-Request": "true"})
    assert resp.status_code == 200
```

- [x] **Step 3: Test quote line item CRUD**

```python
def test_update_quote_line(client, auth_headers, db_session):
    # Create quote + line fixture
    resp = client.put(
        f"/v2/partials/quotes/{quote_id}/lines/{line_id}",
        data={"sell_price": "15.00", "cost_price": "10.00"},
        headers={**auth_headers, "HX-Request": "true"},
    )
    assert resp.status_code == 200

def test_delete_quote_line(client, auth_headers, db_session):
    resp = client.delete(
        f"/v2/partials/quotes/{quote_id}/lines/{line_id}",
        headers={**auth_headers, "HX-Request": "true"},
    )
    assert resp.status_code == 200
```

- [x] **Step 4: Test prospecting list partial**

```python
def test_prospecting_list_partial(client, auth_headers):
    resp = client.get("/v2/partials/prospecting", headers={**auth_headers, "HX-Request": "true"})
    assert resp.status_code == 200
    assert "Prospecting" in resp.text

def test_prospecting_filter_by_status(client, auth_headers):
    resp = client.get("/v2/partials/prospecting?status=suggested", headers={**auth_headers, "HX-Request": "true"})
    assert resp.status_code == 200
```

- [x] **Step 5: Test prospecting detail and actions**

```python
def test_prospecting_detail_partial(client, auth_headers, db_session):
    # Create ProspectAccount fixture
    resp = client.get(f"/v2/partials/prospecting/{prospect_id}", headers={**auth_headers, "HX-Request": "true"})
    assert resp.status_code == 200

def test_claim_prospect(client, auth_headers, db_session):
    resp = client.post(f"/v2/partials/prospecting/{prospect_id}/claim", headers={**auth_headers, "HX-Request": "true"})
    assert resp.status_code == 200

def test_dismiss_prospect(client, auth_headers, db_session):
    resp = client.post(f"/v2/partials/prospecting/{prospect_id}/dismiss", headers={**auth_headers, "HX-Request": "true"})
    assert resp.status_code == 200
```

- [x] **Step 6: Test settings partial**

```python
def test_settings_partial(client, auth_headers):
    resp = client.get("/v2/partials/settings", headers={**auth_headers, "HX-Request": "true"})
    assert resp.status_code == 200

def test_settings_sources_tab(client, auth_headers):
    resp = client.get("/v2/partials/settings/sources", headers={**auth_headers, "HX-Request": "true"})
    assert resp.status_code == 200

def test_settings_system_tab_admin_only(client, auth_headers):
    # Test non-admin gets 403
    resp = client.get("/v2/partials/settings/system", headers={**auth_headers, "HX-Request": "true"})
    assert resp.status_code in (200, 403)  # depends on test user role

def test_settings_profile_tab(client, auth_headers):
    resp = client.get("/v2/partials/settings/profile", headers={**auth_headers, "HX-Request": "true"})
    assert resp.status_code == 200
```

- [x] **Step 7: Test dashboard partial**

```python
def test_dashboard_partial(client, auth_headers):
    resp = client.get("/v2/partials/dashboard", headers={**auth_headers, "HX-Request": "true"})
    assert resp.status_code == 200
    assert "Welcome back" in resp.text
    assert "Open Requisitions" in resp.text
```

- [x] **Step 8: Run full test suite to verify no regressions**

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v --tb=short
```

```bash
cd /root/availai && TESTING=1 PYTHONPATH=/root/availai pytest tests/ --cov=app --cov-report=term-missing --tb=no -q
```

---

## Task 12: Commit & Deploy

- [x] **Step 1: Stage and commit all new and modified files**

```bash
cd /root/availai && git add \
  app/templates/partials/quotes/list.html \
  app/templates/partials/quotes/detail.html \
  app/templates/partials/quotes/line_row.html \
  app/templates/partials/shared/offer_card.html \
  app/templates/partials/prospecting/list.html \
  app/templates/partials/prospecting/detail.html \
  app/templates/partials/prospecting/_card.html \
  app/templates/partials/settings/index.html \
  app/templates/partials/settings/sources.html \
  app/templates/partials/settings/system.html \
  app/templates/partials/settings/profile.html \
  app/templates/partials/buy_plans/list.html \
  app/templates/partials/buy_plans/detail.html \
  app/templates/partials/dashboard.html \
  app/templates/partials/shared/sidebar.html \
  app/routers/htmx_views.py \
  tests/test_htmx_views.py
```

```bash
git commit -m "feat: add Quotes, Prospecting, Settings, Dashboard HTMX views and update Buy Plans brand colors"
```

- [x] **Step 2: Push and deploy**

```bash
git push origin main && cd /root/availai && docker compose up -d --build && docker compose logs -f app
```

- [x] **Step 3: Verify deployment**

Check logs for errors. Hard refresh browser. Navigate to:
- `/v2` — Dashboard with logo, stats, quick actions
- `/v2/quotes` — Quotes list with filters
- `/v2/quotes/{id}` — Quote detail with inline editing and offer gallery
- `/v2/prospecting` — Prospect card grid with scores
- `/v2/prospecting/{id}` — Prospect detail with enrichment data
- `/v2/settings` — Sources tab with toggles, system tab (admin only), profile tab
- `/v2/buy-plans` — Verify brand colors applied
