# Quotes Relocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire the standalone top-level Quotes tab and surface quotes as a dedicated Quotes tab on the requirement (Reqs workspace) and on the CRM account.

**Architecture:** Pure information-architecture/navigation change. Quotes are already FK-bound to a requisition (`Quote.requisition_id`, NOT NULL), so there is **no DB migration**. We remove one nav entry, redirect the orphaned list page, add one requirement-scoped tab handler (reusing the existing quotes table template), and add one account-scoped tab handler + template. A small shared helper computes "quotes belonging to an account" via the union of its sites and its requisitions, fixing a real completeness gap.

**Tech Stack:** FastAPI + SQLAlchemy 2.0 + PostgreSQL + HTMX 2.x + Alpine.js 3.x + Jinja2 + Tailwind. Tests: pytest (in-memory SQLite, xdist).

**Spec:** `docs/superpowers/specs/2026-06-17-quotes-relocation-design.md`

## Global Constraints

- Stack is **HTMX + Alpine.js + Jinja2 — NOT React.** Server-render + HTMX swap; no `innerHTML`.
- **No DB migration** — quotes are already requisition-bound.
- Alpine attributes that embed values must not contain a literal `"` inside a double-quoted attribute. Use single-quoted JS string literals inside double-quoted Alpine attrs (e.g. `x-data="{ status: 'all' }"`); never `|tojson` in a double-quoted attr.
- Use `db.get(Model, id)`, not `db.query(Model).get(id)`.
- Quote status values come from `QuoteStatus` (`app/constants.py`): `draft, sent, won, lost, revised`.
- Every new file needs a header comment: what it does / what calls it / what it depends on.
- Loguru for logging, never `print()`.
- Reuse the existing `app/templates/htmx/partials/requisitions/tabs/quotes.html` for the requirement tab — do not create a second requirement-quotes template.
- Tests run as: `TESTING=1 PYTHONPATH=/root/availai pytest <path> -v`.
- After all tasks: `pre-commit run --all-files` and the full suite must pass. Update the APP_MAP docs.
- Commit message trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Working directory is the worktree `/root/availai/.claude/worktrees/feat+quotes-relocation` (branch `worktree-feat+quotes-relocation`).

---

### Task 1: Retire the standalone Quotes nav tab + redirect the orphaned list page

**Files:**
- Modify: `app/templates/htmx/partials/shared/mobile_nav.html` (remove the `quotes` nav tuple at lines 39-40 and the `'/v2/quotes':'quotes'` entry in the `urlToNav` map at line 14)
- Modify: `app/routers/htmx_views.py` (remove `@router.get("/v2/quotes", ...)` decorator at line 268 from the `v2_page` stack; add a dedicated redirect route; delete the `quotes_list_partial` handler at lines 7951-7977)
- Delete: `app/templates/htmx/partials/quotes/list.html`
- Test: `tests/test_quotes_relocation.py` (new)

**Interfaces:**
- Produces: route `GET /v2/quotes` → 307 redirect to `/v2/requisitions`; `GET /v2/quotes/{id}` unchanged (still renders the quote detail partial); `GET /v2/partials/quotes` → 404 (removed).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_quotes_relocation.py`:

```python
# tests/test_quotes_relocation.py
"""Tests for the quotes relocation: standalone Quotes tab retired; quotes
surfaced on the requirement (Reqs workspace) and the CRM account.

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user)
"""

from datetime import datetime, timezone


def _req_with_part(db_session, test_user, *, company_id=None, customer_name="Acme Corp"):
    """Create a requisition (optionally linked to a company) with one part."""
    from app.models import Requirement, Requisition

    reqn = Requisition(
        name="Test Req",
        status="active",
        urgency="normal",
        customer_name=customer_name,
        company_id=company_id,
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(reqn)
    db_session.commit()
    db_session.refresh(reqn)

    part = Requirement(
        requisition_id=reqn.id,
        primary_mpn="MPN-001",
        target_qty=100,
        sourcing_status="open",
    )
    db_session.add(part)
    db_session.commit()
    db_session.refresh(part)
    return reqn, part


def _quote(db_session, *, requisition_id, number, site_id=None, status="draft"):
    """Create a Quote (minimal valid row)."""
    from app.models.quotes import Quote

    q = Quote(
        requisition_id=requisition_id,
        quote_number=number,
        customer_site_id=site_id,
        line_items=[],
        status=status,
    )
    db_session.add(q)
    db_session.commit()
    db_session.refresh(q)
    return q


def test_v2_quotes_bare_redirects_to_requisitions(client):
    resp = client.get("/v2/quotes", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/v2/requisitions"


def test_v2_quote_detail_still_renders(client, db_session, test_user):
    reqn, _ = _req_with_part(db_session, test_user)
    q = _quote(db_session, requisition_id=reqn.id, number="Q-DET-1")
    resp = client.get(f"/v2/quotes/{q.id}")
    assert resp.status_code == 200


def test_quotes_list_partial_removed(client):
    assert client.get("/v2/partials/quotes").status_code == 404


def test_reqs_page_has_no_quotes_nav_link(client):
    resp = client.get("/v2/requisitions")
    assert resp.status_code == 200
    assert 'href="/v2/quotes"' not in resp.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quotes_relocation.py -v`
Expected: `test_v2_quotes_bare_redirects_to_requisitions` FAILS (currently returns 200 page, not 307); `test_quotes_list_partial_removed` FAILS (currently 200); `test_reqs_page_has_no_quotes_nav_link` FAILS (link present). `test_v2_quote_detail_still_renders` PASSES already.

- [ ] **Step 3: Remove the quotes entry from the nav**

In `app/templates/htmx/partials/shared/mobile_nav.html`:

Delete these two lines from `urlToNav` (line 14) — change:
```
const map = {'/v2/requisitions':'requisitions','/v2/sightings':'sightings','/v2/materials':'materials','/v2/search':'search','/v2/buy-plans':'buy-plans','/v2/crm':'crm','/v2/customers':'crm','/v2/vendors':'crm','/v2/proactive':'proactive','/v2/quotes':'quotes','/v2/prospecting':'prospecting','/v2/settings':'settings'};
```
to (remove `'/v2/quotes':'quotes',`):
```
const map = {'/v2/requisitions':'requisitions','/v2/sightings':'sightings','/v2/materials':'materials','/v2/search':'search','/v2/buy-plans':'buy-plans','/v2/crm':'crm','/v2/customers':'crm','/v2/vendors':'crm','/v2/proactive':'proactive','/v2/prospecting':'prospecting','/v2/settings':'settings'};
```

Delete the `quotes` tuple from `nav_items` (lines 39-40):
```
      ('quotes', 'Quotes', '/v2/quotes', '/v2/partials/quotes',
       'M9 14l6-6m-5.5.5h.01m4.99 5h.01M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z'),
```
The `proactive` tuple above it already ends with `),`, and `prospecting` becomes the final entry — the list stays valid. (The header comment "8 primary nav items" is now accurate.)

- [ ] **Step 4: Replace the bare `/v2/quotes` page route with a redirect; remove the list partial**

In `app/routers/htmx_views.py`, remove the decorator line 268 from the `v2_page` stack:
```python
@router.get("/v2/quotes", response_class=HTMLResponse)
```
(Keep line 269 `@router.get("/v2/quotes/{quote_id:int}", ...)` and keep `"quotes"` in both `_VIEW_SEGMENTS` and `_DETAIL_VIEWS`.)

Add a dedicated redirect route immediately above the `v2_page` definition (just before `@router.get("/v2", ...)` at line 256):
```python
@router.get("/v2/quotes")
async def quotes_list_redirect():
    """Standalone Quotes list retired — quotes now live on the requirement
    (Reqs workspace Quotes tab) and the CRM account (Quotes tab). Kept as a
    redirect so stale bookmarks/links land somewhere sensible.
    Called by: browser navigation to the old /v2/quotes URL.
    """
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/v2/requisitions", status_code=307)
```

Delete the `quotes_list_partial` handler (lines 7951-7977):
```python
@router.get("/v2/partials/quotes", response_class=HTMLResponse)
async def quotes_list_partial(
    ...
):
    ...
    return template_response("htmx/partials/quotes/list.html", ctx)
```
(Keep `quote_detail_partial` at line 7979 — `GET /v2/partials/quotes/{quote_id}`.)

- [ ] **Step 5: Delete the orphaned list template**

```bash
git rm app/templates/htmx/partials/quotes/list.html
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quotes_relocation.py -v`
Expected: all four tests PASS.

- [ ] **Step 7: Guard against dangling references**

Run: `grep -rn '/v2/partials/quotes"' app/templates app/routers`
Expected: no matches (the list partial route + its only callers are gone). The `/v2/partials/quotes/{id}` detail route remains and is unaffected.

- [ ] **Step 8: Commit**

```bash
git add app/templates/htmx/partials/shared/mobile_nav.html app/routers/htmx_views.py tests/test_quotes_relocation.py
git commit -m "$(cat <<'EOF'
feat(quotes): retire standalone Quotes nav tab; redirect /v2/quotes

Remove the top-level Quotes nav item and the orphaned cross-req list page
(bare /v2/quotes now 307-redirects to /v2/requisitions; list partial +
template deleted). Quote detail deep links (/v2/quotes/{id}) are unchanged.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Quotes tab on the requirement (Reqs workspace)

**Files:**
- Modify: `app/templates/htmx/partials/parts/workspace.html:88` (append `('quotes', 'Quotes')` to the tab-strip loop)
- Modify: `app/routers/htmx_views.py` (add `part_tab_quotes` handler next to `part_tab_req_details`, ~line 9789)
- Reuse: `app/templates/htmx/partials/requisitions/tabs/quotes.html` (no change)
- Test: `tests/test_quotes_relocation.py` (extend)

**Interfaces:**
- Consumes: `_req_with_part` / `_quote` helpers from Task 1.
- Produces: route `GET /v2/partials/parts/{requirement_id}/tab/quotes` → 200 HTML rendering the parent requisition's quotes; 404 if the requirement does not exist.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_quotes_relocation.py`:

```python
def test_part_quotes_tab_lists_requisition_quotes(client, db_session, test_user):
    reqn, part = _req_with_part(db_session, test_user)
    _quote(db_session, requisition_id=reqn.id, number="Q-WS-1")
    _quote(db_session, requisition_id=reqn.id, number="Q-WS-2")
    resp = client.get(f"/v2/partials/parts/{part.id}/tab/quotes")
    assert resp.status_code == 200
    assert "Q-WS-1" in resp.text
    assert "Q-WS-2" in resp.text


def test_part_quotes_tab_404_for_missing_requirement(client):
    assert client.get("/v2/partials/parts/999999/tab/quotes").status_code == 404


def test_part_quotes_tab_empty_state(client, db_session, test_user):
    _, part = _req_with_part(db_session, test_user)
    resp = client.get(f"/v2/partials/parts/{part.id}/tab/quotes")
    assert resp.status_code == 200
    assert "No quotes" in resp.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quotes_relocation.py -k part_quotes -v`
Expected: the two 200 tests FAIL with 404 (route not defined); `test_part_quotes_tab_404_for_missing_requirement` may already pass (unknown path → 404).

- [ ] **Step 3: Add the workspace tab button**

In `app/templates/htmx/partials/parts/workspace.html`, change line 88 from:
```jinja
{% for tab_key, tab_label in [('offers', 'Offers'), ('sourcing', 'Sightings'), ('notes', 'Sales Notes'), ('activity', 'Activity'), ('comms', 'Tasks'), ('req-details', 'REQ Detail')] %}
```
to:
```jinja
{% for tab_key, tab_label in [('offers', 'Offers'), ('sourcing', 'Sightings'), ('notes', 'Sales Notes'), ('activity', 'Activity'), ('comms', 'Tasks'), ('req-details', 'REQ Detail'), ('quotes', 'Quotes')] %}
```

- [ ] **Step 4: Add the route handler**

In `app/routers/htmx_views.py`, add immediately after the `part_tab_req_details` handler (after its `return template_response("htmx/partials/parts/tabs/req_details.html", ctx)` near line 9821):

```python
@router.get("/v2/partials/parts/{requirement_id}/tab/quotes", response_class=HTMLResponse)
async def part_tab_quotes(
    requirement_id: int,
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Quotes for the parent requisition of the selected part. Quotes are
    requisition-level, so every part of the same requisition shows the same set.
    Called by: parts workspace tab strip (Quotes tab).
    Depends on: Quote, Requirement models; requisitions/tabs/quotes.html.
    """
    requirement = db.get(Requirement, requirement_id)
    if not requirement:
        raise HTTPException(404, "Part not found")

    quotes = (
        db.query(Quote)
        .filter(Quote.requisition_id == requirement.requisition_id)
        .order_by(Quote.created_at.desc().nullslast())
        .all()
    )
    ctx = _base_ctx(request, user, "requisitions")
    ctx.update({"quotes": quotes, "req": requirement.requisition})
    return template_response("htmx/partials/requisitions/tabs/quotes.html", ctx)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quotes_relocation.py -k part_quotes -v`
Expected: all three PASS.

- [ ] **Step 6: Commit**

```bash
git add app/templates/htmx/partials/parts/workspace.html app/routers/htmx_views.py tests/test_quotes_relocation.py
git commit -m "$(cat <<'EOF'
feat(quotes): add Quotes tab to the Reqs workspace

New /v2/partials/parts/{requirement_id}/tab/quotes handler lists the parent
requisition's quotes, reusing requisitions/tabs/quotes.html. Adds the Quotes
tab to the workspace tab strip.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Quotes tab on the CRM account + site/requisition union completeness fix

**Files:**
- Create: `app/templates/htmx/partials/customers/tabs/quotes_tab.html`
- Modify: `app/routers/htmx_views.py` (add `_company_quotes_query` helper; add `"quotes"` to `valid_tabs` at line 4695; add the `quotes` branch in `company_tab`; align the Activity branch quotes query at lines 4799-4809; add `quote_count` to the detail ctx at lines 4666-4678)
- Modify: `app/templates/htmx/partials/customers/detail.html:132-137` (append the Quotes tab tuple)
- Test: `tests/test_quotes_relocation.py` (extend)

**Interfaces:**
- Consumes: `_req_with_part` / `_quote` helpers; `Company`, `CustomerSite` models.
- Produces: helper `_company_quotes_query(db, company)` → SQLAlchemy `Query` over `Quote` for that account (union of site-linked and requisition-linked), or `None` when the account can own no quotes; route `GET /v2/partials/customers/{company_id}/tab/quotes` → 200.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_quotes_relocation.py`:

```python
def _company_with_site(db_session, *, name="Acme Corp"):
    from app.models.crm import Company, CustomerSite

    company = Company(name=name, is_active=True)
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)
    site = CustomerSite(company_id=company.id, site_name="HQ", is_active=True)
    db_session.add(site)
    db_session.commit()
    db_session.refresh(site)
    return company, site


def test_company_quotes_tab_unions_site_and_requisition(client, db_session, test_user):
    company, site = _company_with_site(db_session)
    reqn, _ = _req_with_part(db_session, test_user, company_id=company.id)
    # Quote linked only via the customer site:
    _quote(db_session, requisition_id=reqn.id, number="Q-SITE-1", site_id=site.id)
    # Quote linked only via the requisition (site is NULL) — must still appear:
    _quote(db_session, requisition_id=reqn.id, number="Q-REQONLY-1", site_id=None)
    resp = client.get(f"/v2/partials/customers/{company.id}/tab/quotes")
    assert resp.status_code == 200
    assert "Q-SITE-1" in resp.text
    assert "Q-REQONLY-1" in resp.text  # regression guard for the union fix


def test_company_quotes_tab_empty_state(client, db_session, test_user):
    company, _ = _company_with_site(db_session)
    resp = client.get(f"/v2/partials/customers/{company.id}/tab/quotes")
    assert resp.status_code == 200
    assert "No quotes" in resp.text


def test_company_unknown_tab_still_404(client, db_session, test_user):
    company, _ = _company_with_site(db_session)
    assert client.get(f"/v2/partials/customers/{company.id}/tab/bogus").status_code == 404


def test_company_detail_shows_quotes_tab_button(client, db_session, test_user):
    company, _ = _company_with_site(db_session)
    resp = client.get(f"/v2/partials/customers/{company.id}")
    assert resp.status_code == 200
    assert "tab/quotes" in resp.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quotes_relocation.py -k company -v`
Expected: `test_company_quotes_tab_*` and `test_company_detail_shows_quotes_tab_button` FAIL (quotes tab not in `valid_tabs` → 404; button absent). `test_company_unknown_tab_still_404` PASSES.

- [ ] **Step 3: Add the shared account-quotes helper**

In `app/routers/htmx_views.py`, add this helper just above `company_detail_partial` (line 4625):

```python
def _company_quotes_query(db: Session, company):
    """Quotes belonging to an account: union of quotes linked via the
    company's customer sites OR via the company's requisitions (the latter
    catches quotes whose customer_site_id is NULL). Returns a Query, or None
    when the account can own no quotes (no sites and no requisitions).
    Called by: company_detail_partial (count), company_tab (quotes + activity).
    """
    site_ids = [
        s.id
        for s in db.query(CustomerSite.id).filter(CustomerSite.company_id == company.id).all()
    ]
    req_ids = [
        r.id
        for r in db.query(Requisition.id)
        .filter(
            or_(
                Requisition.company_id == company.id,
                sqlfunc.lower(sqlfunc.trim(Requisition.customer_name)) == company.name.lower().strip(),
            )
        )
        .all()
    ]
    conds = []
    if site_ids:
        conds.append(Quote.customer_site_id.in_(site_ids))
    if req_ids:
        conds.append(Quote.requisition_id.in_(req_ids))
    if not conds:
        return None
    return db.query(Quote).filter(or_(*conds))
```

- [ ] **Step 4: Pass `quote_count` into the customer detail context**

In `company_detail_partial`, extend the `ctx.update({...})` block (lines 4666-4678) to include the count. Change:
```python
    ctx = _base_ctx(request, user, "customers")
    ctx.update(
        {
            "company": company,
            "sites": sites,
            "open_req_count": open_req_count,
```
to add `quote_count` (compute it just before the `ctx.update`):
```python
    _cq = _company_quotes_query(db, company)
    quote_count = _cq.count() if _cq is not None else 0

    ctx = _base_ctx(request, user, "customers")
    ctx.update(
        {
            "company": company,
            "sites": sites,
            "open_req_count": open_req_count,
            "quote_count": quote_count,
```
(Leave the rest of the dict — `contact_rows`, `user`, the comment — unchanged.)

- [ ] **Step 5: Add the Quotes tab button to the account detail template**

In `app/templates/htmx/partials/customers/detail.html`, change the tab loop list (lines 132-137) from:
```jinja
        {% for tab_id, tab_label, tab_count in [
          ('contacts', 'Contacts', contact_rows|length if contact_rows is defined else none),
          ('sites', 'Sites', sites|length),
          ('requisitions', 'Requisitions', open_req_count),
          ('activity', 'Activity', none)
        ] %}
```
to:
```jinja
        {% for tab_id, tab_label, tab_count in [
          ('contacts', 'Contacts', contact_rows|length if contact_rows is defined else none),
          ('sites', 'Sites', sites|length),
          ('requisitions', 'Requisitions', open_req_count),
          ('activity', 'Activity', none),
          ('quotes', 'Quotes', quote_count if quote_count is defined else none)
        ] %}
```

- [ ] **Step 6: Add `quotes` to `valid_tabs` and the `quotes` branch in `company_tab`**

In `company_tab`, change line 4695:
```python
    valid_tabs = {"sites", "contacts", "requisitions", "activity"}
```
to:
```python
    valid_tabs = {"sites", "contacts", "requisitions", "activity", "quotes"}
```

Add a `quotes` branch before the final `else:  # activity` block (i.e., right after the `requisitions` branch returns, ~line 4761):
```python
    elif tab == "quotes":
        cq = _company_quotes_query(db, company)
        quotes = (
            cq.order_by(Quote.created_at.desc().nullslast()).all() if cq is not None else []
        )
        ctx = _base_ctx(request, user, "customers")
        ctx.update({"company": company, "quotes": quotes})
        return template_response("htmx/partials/customers/tabs/quotes_tab.html", ctx)
```

- [ ] **Step 7: Align the Activity-tab quotes query with the union**

In the `else:  # activity` branch, replace the site-only quotes query (lines 4799-4809):
```python
        # Quotes linked to company's sites
        site_ids = [s.id for s in db.query(CustomerSite.id).filter(CustomerSite.company_id == company_id).all()]
        quotes = []
        if site_ids:
            quotes = (
                db.query(Quote)
                .filter(Quote.customer_site_id.in_(site_ids))
                .order_by(Quote.created_at.desc().nullslast())
                .limit(20)
                .all()
            )
```
with the union (so the Activity count matches the Quotes tab):
```python
        # Quotes for this account — union of site-linked and requisition-linked
        # (matches the Quotes tab; site link alone misses NULL-site quotes).
        cq = _company_quotes_query(db, company)
        quotes = (
            cq.order_by(Quote.created_at.desc().nullslast()).limit(20).all()
            if cq is not None
            else []
        )
```

- [ ] **Step 8: Create the account Quotes tab template**

Create `app/templates/htmx/partials/customers/tabs/quotes_tab.html`:

```jinja
{# quotes_tab.html — Quotes tab for the CRM account (company) detail.
   Receives: company (Company), quotes (list[Quote]).
   Called by: company_tab route (tab == "quotes").
   Depends on: Alpine.js (status filter), HTMX (row deep-link), brand palette.
#}
<div x-data="{ status: 'all' }">
  {% if quotes %}
  <div class="flex items-center gap-2 mb-4">
    <p class="text-sm text-gray-500 mr-2">
      <span class="font-semibold text-gray-900">{{ quotes|length }}</span> quote{{ "s" if quotes|length != 1 }}
    </p>
    {% for f_key, f_label in [('all','All'),('draft','Draft'),('sent','Sent'),('won','Won'),('lost','Lost'),('revised','Revised')] %}
    <button type="button"
            @click="status = '{{ f_key }}'"
            :class="status === '{{ f_key }}' ? 'bg-brand-100 text-brand-700' : 'bg-gray-50 text-gray-500 hover:bg-gray-100'"
            class="px-2.5 py-1 text-xs font-medium rounded-full transition-colors">
      {{ f_label }}
    </button>
    {% endfor %}
  </div>
  <div class="overflow-x-auto bg-white rounded-lg border border-brand-200">
    <table class="min-w-full divide-y divide-gray-200">
      <thead class="bg-gray-50 sticky top-0">
        <tr>
          <th class="px-4 py-2.5 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Quote #</th>
          <th class="px-4 py-2.5 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Requisition</th>
          <th class="px-4 py-2.5 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">Total</th>
          <th class="px-4 py-2.5 text-right text-xs font-medium text-gray-500 uppercase tracking-wider">Margin %</th>
          <th class="px-4 py-2.5 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Status</th>
          <th class="px-4 py-2.5 text-left text-xs font-medium text-gray-500 uppercase tracking-wider">Created</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-gray-200">
        {% for q in quotes %}
        <tr class="hover:bg-brand-50 cursor-pointer transition-colors"
            x-show="status === 'all' || status === '{{ q.status }}'"
            hx-get="/v2/partials/quotes/{{ q.id }}"
            hx-target="#main-content"
            hx-push-url="/v2/quotes/{{ q.id }}">
          <td class="px-4 py-2.5 text-sm font-medium text-brand-500 hover:text-brand-600">{{ q.quote_number or "Q-" ~ q.id }}</td>
          <td class="px-4 py-2.5 text-sm text-gray-500">{{ q.requisition.name if q.requisition else "—" }}</td>
          <td class="px-4 py-2.5 text-sm text-gray-900 font-medium text-right">{{ "${:,.2f}".format(q.subtotal) if q.subtotal else "—" }}</td>
          <td class="px-4 py-2.5 text-sm text-right">
            {% if q.total_margin_pct is not none %}
              {% if q.total_margin_pct >= 30 %}
                <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-emerald-50 text-emerald-700">{{ "%.1f%%"|format(q.total_margin_pct) }}</span>
              {% elif q.total_margin_pct >= 15 %}
                <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-amber-50 text-amber-700">{{ "%.1f%%"|format(q.total_margin_pct) }}</span>
              {% else %}
                <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full bg-rose-50 text-rose-700">{{ "%.1f%%"|format(q.total_margin_pct) }}</span>
              {% endif %}
            {% else %}
              <span class="text-gray-500">—</span>
            {% endif %}
          </td>
          <td class="px-4 py-2.5">
            {% set quote_colors = {
              "draft": "bg-brand-100 text-brand-600",
              "sent": "bg-amber-50 text-amber-700",
              "won": "bg-emerald-50 text-emerald-700",
              "lost": "bg-rose-50 text-rose-700",
              "revised": "bg-gray-100 text-gray-600"
            } %}
            <span class="inline-flex px-2 py-0.5 text-xs font-medium rounded-full {{ quote_colors.get(q.status, 'bg-gray-100 text-gray-600') }}">
              {{ q.status|capitalize }}
            </span>
          </td>
          <td class="px-4 py-2.5 text-sm text-gray-500">{{ q.created_at.strftime('%b %d, %Y') if q.created_at else "—" }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <div class="py-12 text-center">
    <svg class="mx-auto h-12 w-12 text-gray-300" fill="none" viewBox="0 0 24 24" stroke="currentColor">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
            d="M9 14l6-6m-5.5.5h.01m4.99 5h.01M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16l3.5-2 3.5 2 3.5-2 3.5 2z"/>
    </svg>
    <p class="text-sm font-medium text-gray-500 mt-3">No quotes for this account.</p>
    <p class="text-xs text-gray-400 mt-1">Quotes are created from a requirement's Offers tab.</p>
  </div>
  {% endif %}
</div>
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_quotes_relocation.py -k company -v`
Expected: all four `company` tests PASS.

- [ ] **Step 10: Commit**

```bash
git add app/routers/htmx_views.py app/templates/htmx/partials/customers/detail.html app/templates/htmx/partials/customers/tabs/quotes_tab.html tests/test_quotes_relocation.py
git commit -m "$(cat <<'EOF'
feat(quotes): add Quotes tab to the CRM account; fix account-quote union

New /v2/partials/customers/{id}/tab/quotes lists all of an account's quotes
(status filter) via a shared _company_quotes_query that unions site-linked and
requisition-linked quotes — catching NULL-site quotes the Activity tab missed.
Activity tab realigned to the same union; tab button + count badge added.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Docs, static-analysis guards, and full-suite verification

**Files:**
- Modify: `docs/APP_MAP_INTERACTIONS.md` (quote surfaces / nav flow)
- Modify: `docs/APP_MAP_ARCHITECTURE.md` (template inventory)
- Verify: `tests/test_static_analysis.py` and the full suite

**Interfaces:** none (documentation + verification).

- [ ] **Step 1: Update APP_MAP_INTERACTIONS.md**

In `docs/APP_MAP_INTERACTIONS.md`, update the quotes section to state: the standalone Quotes nav tab is removed; bare `/v2/quotes` redirects to `/v2/requisitions`; quotes are surfaced via the Reqs workspace **Quotes** tab (`/v2/partials/parts/{requirement_id}/tab/quotes`, reusing `requisitions/tabs/quotes.html`) and the CRM account **Quotes** tab (`/v2/partials/customers/{id}/tab/quotes`); the account quote set is the union of site-linked and requisition-linked quotes via `_company_quotes_query`. The quote detail page `/v2/quotes/{id}` is unchanged.

- [ ] **Step 2: Update APP_MAP_ARCHITECTURE.md**

In `docs/APP_MAP_ARCHITECTURE.md`, in the template inventory: remove the `quotes/list.html` entry; add `customers/tabs/quotes_tab.html` (CRM account quotes tab, Alpine status filter).

- [ ] **Step 3: Run the static-analysis guards (line-keyed; may need a bump)**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_static_analysis.py -v`
Expected: PASS. If a line-keyed guard (e.g. `_TOJSON_IN_DOUBLE_QUOTED_ALPINE_ALLOWLIST`, keyed by `(file, line)`) fails because line numbers shifted in a file it references, update the keyed line number to the new location — do **not** add new allowlist entries for our new template (it uses single-quoted JS literals and no `|tojson`, so it must not need one; if the guard flags `quotes_tab.html`, that is a real violation to fix, not allowlist).

- [ ] **Step 4: Run the full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -q`
Expected: all pass (no regressions in `test_htmx_views*`, `test_req_details_tab`, `test_activity_quality`, `tests/test_quotes_relocation.py`).

- [ ] **Step 5: Run pre-commit across all files**

Run: `pre-commit run --all-files`
Expected: PASS (ruff, ruff-format, mypy, docformatter). Fix any findings; if docformatter rewraps a docstring, run pre-commit twice.

- [ ] **Step 6: Commit**

```bash
git add docs/APP_MAP_INTERACTIONS.md docs/APP_MAP_ARCHITECTURE.md tests/test_static_analysis.py
git commit -m "$(cat <<'EOF'
docs(quotes): update APP_MAP for quotes relocation; sync static guards

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review (completed by author)

**Spec coverage:**
- Remove standalone nav tab → Task 1 (nav tuple + urlToNav). ✓
- Redirect bare `/v2/quotes`, keep detail, delete list template/route → Task 1. ✓
- Requirement Quotes tab (workspace tab strip, reuse template) → Task 2. ✓
- Account Quotes tab (valid_tabs, branch, new template, button, count) → Task 3. ✓
- Union completeness fix + Activity alignment → Task 3 (`_company_quotes_query`, Steps 6-7). ✓
- No DB migration → respected (no alembic step). ✓
- Tests for all routes incl. NULL-site union guard → Tasks 1-3. ✓
- APP_MAP updates → Task 4. ✓
- `/requisitions2` out of scope → not touched. ✓

**Placeholder scan:** No TBDs; every code step shows complete code and exact commands.

**Type consistency:** `_company_quotes_query(db, company)` returns `Query | None` and is consumed identically in Steps 4, 6, 7 (guarded `if ... is not None`). `_req_with_part` / `_quote` / `_company_with_site` helper signatures match all call sites. Route paths match between handlers and tests.
