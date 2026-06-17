# Quotes Relocation — Remove standalone Quotes tab; surface quotes on the requirement and the account

**Project**: AvailAI — Trio Supply Chain Solutions
**Date**: 2026-06-17
**Status**: Approved design — ready for implementation plan
**Author**: Brainstorm session (user + Claude)

---

## 1. Motivation

Quotes currently live behind a dedicated top-level **Quotes** nav tab (`/v2/quotes`). That
is the wrong information architecture: a quote always belongs to the requisition it was
built from (and, through that requisition, to a customer account). The standalone tab is
a silo, redundant with where quotes logically belong.

**Goal:** retire the standalone Quotes tab and surface quotes in the two places users
actually work — on the **requirement** (the Reqs workspace) and on the **account** (CRM
customer detail) — each as a dedicated **Quotes** tab.

---

## 2. Current state (verified in code)

### Data model — already requisition-bound (no migration needed)
- `app/models/quotes.py:29` — `Quote.requisition_id = Column(Integer, ForeignKey("requisitions.id", ondelete="CASCADE"), nullable=False)`. **A quote cannot exist without a parent requisition.**
- `app/models/quotes.py:30` — `Quote.customer_site_id` → `customer_sites.id`, `ondelete="SET NULL"`, **nullable**.
- `app/models/sourcing.py:83` — reverse relationship `Requisition.quotes` (`cascade="all, delete-orphan"`).
- A quote ties to an account two ways: (a) `customer_site_id → CustomerSite.company_id`, and (b) `requisition_id → Requisition.company_id`. **(a) is nullable**, so neither alone is complete.

### Where quotes appear today
| Surface | Quotes? | Mechanism |
|---|---|---|
| Standalone **Quotes** nav tab `/v2/quotes` | Yes (list + detail) | `mobile_nav.html`, `quotes/list.html`, `quotes/detail.html` |
| **Reqs** workspace (primary daily view) | **No** | `htmx/partials/parts/workspace.html` — part-centric tabs: Offers/Sightings/Sales Notes/Activity/Tasks/REQ Detail |
| Full requisition detail (deep-link `/v2/requisitions/{id}`) | Yes | `htmx/partials/requisitions/detail.html` has a Quotes sub-tab (`requisitions/tabs/quotes.html`) |
| **CRM** customer/account detail | Partial | quotes listed *inside* the Activity tab only (`customers/tabs/activity_tab.html`) |
| `/requisitions2` opportunity table (NOT in bottom nav) | No | `requisitions2/_detail_panel.html` — parts/offers/activity |

### Key routing facts
- Bottom-nav **Reqs** → `/v2/requisitions` → `v2_page` (`htmx_views.py:257`) loads the split-panel **parts workspace** partial `/v2/partials/parts/workspace` (`htmx_views.py:312-314`).
- Workspace tab content loads from `GET /v2/partials/parts/{requirement_id}/tab/{key}` (`htmx_views.py:9723+`); `req-details` → `parts/tabs/req_details.html` (`htmx_views.py:9789-9821`).
- Customer detail tabs load from `GET /v2/partials/customers/{company_id}/tab/{tab}` (`htmx_views.py:4682`); `valid_tabs = {"sites","contacts","requisitions","activity"}` (`htmx_views.py:4695`). The Activity branch already fetches the company's quotes by `customer_site_id ∈ site_ids` (`htmx_views.py:4799-4809`).
- Quote detail page: `GET /v2/quotes/{quote_id:int}` (`htmx_views.py:269`) → partial `/v2/partials/quotes/{id}` → `quotes/detail.html`.
- Bare list page: `GET /v2/quotes` (`htmx_views.py:268`, part of the `v2_page` catch-all) → partial `/v2/partials/quotes` → `quotes/list.html`.

---

## 3. Decisions (locked with user)

1. **Remove** the standalone Quotes nav tab.
2. **Requirement:** add a dedicated **Quotes tab** to the **Reqs workspace tab strip** (sibling of *REQ Detail*).
3. **Account:** add a dedicated **Quotes tab** to the CRM customer detail (alongside Sites/Contacts/Requisitions/Activity).
4. **Old list page:** bare `/v2/quotes` **redirects to `/v2/requisitions`**; the list template is deleted. The quote **detail** page (`/v2/quotes/{id}`) **stays** (deep links from emailed quotes, PDFs, and the new tabs).
5. **No DB migration** — quotes are already requisition-bound.
6. **Out of scope:** `/requisitions2` opportunity table (not in the bottom nav). No quotes tab added there in this change.

---

## 4. Changes

### 4.1 Remove the standalone Quotes nav tab
**File:** `app/templates/htmx/partials/shared/mobile_nav.html`
- Delete the `('quotes', 'Quotes', '/v2/quotes', '/v2/partials/quotes', <icon>)` tuple from the `nav_items` list (line ~39).
- Delete the `'/v2/quotes':'quotes'` entry from the `urlToNav` map (line ~14).
- Verify no other nav/topbar template references `/v2/quotes` as a primary link (grep `'/v2/quotes'` across `app/templates/`); the macro/detail deep-links to `/v2/quotes/{id}` must remain untouched.

### 4.2 Redirect the bare list page; keep the detail page
**File:** `app/routers/htmx_views.py`
- Remove `@router.get("/v2/quotes", response_class=HTMLResponse)` (line 268) from the `v2_page` catch-all decorator stack.
- Add a dedicated route **before** the catch-all:
  ```python
  @router.get("/v2/quotes")
  async def quotes_list_redirect():
      """Standalone Quotes list retired — quotes now live on the requirement and the account."""
      return RedirectResponse(url="/v2/requisitions", status_code=307)
  ```
  (Use 307 to preserve method; the nav is gone so this is only hit by stale bookmarks/links.)
- **Keep** `@router.get("/v2/quotes/{quote_id:int}")` (line 269) and `quotes` in both `_VIEW_SEGMENTS` (line 292) and `_DETAIL_VIEWS` (line 330) so `/v2/quotes/{id}` still resolves and paints the detail partial.
- Remove the now-dead list partial route (`GET /v2/partials/quotes` handler) and verify nothing else targets it.

**File:** `app/templates/htmx/partials/quotes/list.html` — **delete** (no longer referenced).
Keep `quotes/detail.html`, `quotes/_macros.html`, `quotes/line_row.html`, `quotes/preview.html` (used by the detail page and quote builder).

### 4.3 Requirement-side Quotes tab (Reqs workspace)
**File:** `app/templates/htmx/partials/parts/workspace.html` (line 88)
- Append `('quotes', 'Quotes')` to the tab-strip loop list, after `('req-details', 'REQ Detail')`.

**File:** `app/routers/htmx_views.py` — new handler mirroring `part_tab_req_details` (9789-9821):
```python
@router.get("/v2/partials/parts/{requirement_id}/tab/quotes", response_class=HTMLResponse)
async def part_tab_quotes(
    request: Request,
    requirement_id: int,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Cross-requisition quote history for the selected part: every quote LINE
    whose MPN matches this part (primary + substitutes) OR whose material_card
    matches this part's canonical card — across ALL requisitions/customers."""
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Part not found")

    from ..utils.normalization import parse_substitute_mpns

    mpns: set[str] = set()
    if requirement.primary_mpn:
        mpns.add(requirement.primary_mpn.upper())
    for sub in parse_substitute_mpns(requirement.substitutes or [], requirement.primary_mpn or ""):
        if sub.get("mpn"):
            mpns.add(sub["mpn"].upper())

    conds = []
    if mpns:
        conds.append(sqlfunc.upper(QuoteLine.mpn).in_(mpns))
    if requirement.material_card_id:
        conds.append(QuoteLine.material_card_id == requirement.material_card_id)

    quote_lines = []
    if conds:
        quote_lines = (
            db.query(QuoteLine)
            .join(Quote, QuoteLine.quote_id == Quote.id)
            .options(joinedload(QuoteLine.quote).joinedload(Quote.requisition))
            .filter(or_(*conds))
            .order_by(Quote.created_at.desc().nullslast())
            .all()
        )
    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"requirement": requirement, "quote_lines": quote_lines})
    return template_response("htmx/partials/parts/tabs/quotes.html", ctx)
```
- **DESIGN CHANGE (per user, 2026-06-17):** the requirement tab is **MPN-scoped and cross-requisition**, not requisition-scoped. It answers "what quotes has this part number been on, and at what price" across every requisition and customer.
- **Match breadth (user choice):** this part's `primary_mpn` + parsed `substitutes` (upper-cased, matched against `QuoteLine.mpn`) **OR** the same canonical part (`QuoteLine.material_card_id == requirement.material_card_id`). `QuoteLine.mpn` is indexed (`ix_quote_lines_mpn`); `material_card_id` is indexed.
- **Line-centric rows.** One row per matching `QuoteLine` (a quote that carried this part on two lines legitimately shows both quoted prices), ordered by the quote's `created_at` desc. `joinedload(QuoteLine.quote → Quote.requisition)` avoids N+1; the customer is `line.quote.requisition.customer_name`.
- **New template `app/templates/htmx/partials/parts/tabs/quotes.html`** (the part view needs cross-requisition columns — Quote #, Customer, Requisition, Qty, Sell, Margin %, Status, Created — that the requisition-scoped `requisitions/tabs/quotes.html` lacks). Rows deep-link to `/v2/quotes/{line.quote.id}`. The requisition-detail sub-tab keeps using `requisitions/tabs/quotes.html` unchanged.
- `Requirement` has `primary_mpn`, `substitutes` (JSON), and `material_card_id` (`app/models/sourcing.py`) — confirmed. Requires `QuoteLine` imported in `htmx_views.py`.

### 4.4 Account-side Quotes tab (CRM customer detail)
**File:** `app/templates/htmx/partials/customers/detail.html` (line 132)
- Append `('quotes', 'Quotes', quote_count)` to the tab loop tuple list as the **last** tab (after `activity`).

**File:** `app/routers/htmx_views.py`
- Add `"quotes"` to `valid_tabs` (line 4695).
- New branch in `company_tab`:
  ```python
  elif tab == "quotes":
      site_ids = [s.id for s in db.query(CustomerSite.id)
                  .filter(CustomerSite.company_id == company_id).all()]
      req_ids = [r.id for r in db.query(Requisition.id).filter(
          or_(Requisition.company_id == company.id,
              sqlfunc.lower(sqlfunc.trim(Requisition.customer_name))
                  == company.name.lower().strip())).all()]
      conds = []
      if site_ids:
          conds.append(Quote.customer_site_id.in_(site_ids))
      if req_ids:
          conds.append(Quote.requisition_id.in_(req_ids))
      quotes = (db.query(Quote).filter(or_(*conds)).order_by(
          Quote.created_at.desc().nullslast()).all()) if conds else []
      ctx = _base_ctx(request, user, "customers")
      ctx.update({"company": company, "quotes": quotes})
      return template_response("htmx/partials/customers/tabs/quotes_tab.html", ctx)
  ```
- **Data-correctness fix:** the union of `customer_site_id ∈ sites` **and** `requisition_id ∈ company reqs` (deduped by the `OR`) catches quotes whose `customer_site_id` is null — which the current Activity tab (`htmx_views.py:4799-4809`) silently misses. Update the Activity branch to use the same union so the Activity quote count and the new tab agree.

**File (new):** `app/templates/htmx/partials/customers/tabs/quotes_tab.html`
- Dedicated account quotes list: same columns as `requisitions/tabs/quotes.html` (Quote #, Customer, Total, Margin %, Status, Created) **plus a client-side status filter** (draft / sent / won / lost) via an Alpine `x-data` filter over the rendered rows (single-quoted attribute per the tojson/Alpine quoting rule in CLAUDE.md). Rows deep-link to `/v2/quotes/{id}` (`hx-target="#main-content"`, `hx-push-url`). Empty state mirrors the requisition tab.
- File header comment per CLAUDE.md (what it does / what calls it / what it depends on).

**Count badge:** add `quote_count` to the `ctx.update({...})` block at `htmx_views.py:4666-4678` (where `open_req_count` is set), computed with the **same union** the tab uses (site-linked OR requisition-linked, deduped). `detail.html` already renders the badge when `tab_count` is truthy.

---

## 5. Edge cases & behavior

- **Quote detail nav highlight:** with Quotes gone from nav, viewing `/v2/quotes/{id}` highlights no nav item. Acceptable; optionally have the detail breadcrumb link back to its requisition. Decision: leave nav highlight as-is (no special handling).
- **Empty states:** both tabs use the existing "No quotes generated… Create a quote from the Offers tab" empty state.
- **Permissions:** new handlers use the same `require_user` dependency as their siblings; no new permission surface.
- **Quote builder unaffected:** launched from the requisition Offers flow; no change.

---

## 6. Tests (required — CLAUDE.md)

Add to the appropriate existing test modules (or new ones):
1. `GET /v2/quotes` → 307 redirect to `/v2/requisitions`.
2. `GET /v2/quotes/{id}` → still 200 and renders the detail partial.
3. `GET /v2/partials/parts/{requirement_id}/tab/quotes` → 200; returns the requisition's quotes; 404 for a missing requirement; empty list renders the empty state.
4. `GET /v2/partials/customers/{company_id}/tab/quotes` → 200; returns the union of site-linked and requisition-linked quotes; a quote with **null** `customer_site_id` but a requisition under the company **is included** (regression guard for the completeness fix).
5. `quotes` accepted in customer `valid_tabs`; unknown tab still 404s.
6. Nav: `mobile_nav.html` no longer emits a `/v2/quotes` primary nav entry (template-render assertion).

Run the full suite (`TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`) before PR.

---

## 7. Docs to update (CLAUDE.md rule)

- `docs/APP_MAP_INTERACTIONS.md` — quote surfaces / nav flow (remove standalone tab; add the two new tabs; note the union query).
- `docs/APP_MAP_ARCHITECTURE.md` — template inventory (delete `quotes/list.html`; add `customers/tabs/quotes_tab.html`).

---

## 8. Out of scope

- `/requisitions2` opportunity-table detail panel (not in bottom nav) — no quotes tab added.
- Consolidated "hub" tab merging offers+quotes+RFQs (explicitly not chosen).
- Any change to the quote data model, quote builder, or quote PDF.
