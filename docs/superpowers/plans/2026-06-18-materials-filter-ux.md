# Materials Filter UX Improvements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to
> implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three research-backed distributor-filter patterns to the materials filter tree:
numeric-aware overflow sort (P5), search-within for long enum facets (P3), and common-value
chips on numeric facets (P2).

**Architecture:** All changes live in the existing faceted-search stack —
`app/services/faceted_search_service.py` (options/filters/counts),
`app/templates/htmx/partials/materials/filters/_macros.html` (widgets), and
`app/static/htmx_app.js` (`materialsFilter()` Alpine component). No new tables, no migration.
The numeric-chip filter extends the `MaterialSpecFacet` EXISTS-via-IN predicate with a
`value_numeric.in_()` branch keyed `"{spec_key}__vals"`.

**Tech Stack:** FastAPI · SQLAlchemy 2.0 · PostgreSQL · Jinja2 · HTMX · Alpine.js · pytest.

**Reference:** `docs/superpowers/specs/2026-06-18-materials-filter-ux-design.md`.

---

### Task 1: P5 — numeric-aware enum overflow sort

**Files:**
- Modify: `app/services/faceted_search_service.py` (add `_natural_sort_key`; use it at the
  fixed-vocab overflow append, currently lines 618-621)
- Test: `tests/test_faceted_search_service.py` (or the existing faceted-search test module)

- [ ] **Step 1: Write the failing test** — natural sort of overflow values.

```python
def test_subfilter_overflow_values_sort_numerically(db_session):
    # Seed a fixed-vocab enum schema whose data carries unexpected overflow values.
    _seed_schema(db_session, commodity="capacitors", spec_key="package",
                 data_type="enum", enum_values=["0402", "0603"], is_filterable=True)
    for pkg in ["1210", "0805", "205"]:  # all "overflow" (not in enum_values)
        _seed_facet(db_session, commodity="capacitors", spec_key="package", value_text=pkg)
    opts = {o["spec_key"]: o for o in get_subfilter_options(db_session, "capacitors")}
    vals = opts["package"]["values"]
    # Seed order preserved, then overflow in NUMERIC order (205 < 805 < 1210), not lexical.
    assert vals == ["0402", "0603", "205", "0805", "1210"]
```

- [ ] **Step 2: Run it, confirm it fails** (lexical order gives `["0402","0603","0805","1210","205"]`).

Run: `TESTING=1 python3 -m pytest tests/test_faceted_search_service.py -k overflow_values_sort -q`

- [ ] **Step 3: Implement** — add the helper and use it.

```python
import re

def _natural_sort_key(s: str):
    """Split on digit runs so '205' sorts before '1210' (numeric runs as ints)."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s) if t != ""]
```

Replace the overflow append (lines ~619-621):

```python
option["values"] = list(schema.enum_values) + sorted(
    (v for v in observed if v not in schema.enum_values),
    key=_natural_sort_key,
)
```

- [ ] **Step 4: Run the test, confirm PASS.**
- [ ] **Step 5: Commit** — `feat(materials-filter): natural-sort enum overflow values (P5)`.

---

### Task 2: P3 — search-within for long fixed-vocabulary enum facets

**Files:**
- Modify: `app/templates/htmx/partials/materials/filters/_macros.html` (`checkbox_group` macro)
- Test: frontend lint/build + an e2e console-error check (`tests/e2e/`), and/or a template
  render assertion if the suite renders partials.

- [ ] **Step 1: Write the failing/guard test** — render `checkbox_group` with >12 values and
  assert a search input bound to `ui.facetSearch` is present; render with ≤12 and assert it is
  absent. (Use the project's Jinja-render test helper if one exists; otherwise an e2e assertion
  on the connectors commodity, which has 19 `connector_type` values.)

- [ ] **Step 2: Run it, confirm it fails** (no search input today).

- [ ] **Step 3: Implement** — in `checkbox_group`, gated on `{% if values|length > 12 %}`, add
  before the values `<div>`:

```jinja2
{% if values|length > 12 %}
<input type="text" x-model="ui.facetSearch['{{ spec_key }}']"
       placeholder="Search {{ display_name|lower }}…" aria-label="Search {{ display_name }}"
       class="w-full mb-1 px-2 py-1 text-[13px] border border-gray-200 rounded focus:ring-1 focus:ring-brand-500 focus:border-brand-500">
{% endif %}
```

and add to each value `<label>` (only meaningful when the box renders; harmless otherwise):

```jinja2
x-show='!ui.facetSearch["{{ spec_key }}"] || {{ val|tojson }}.toLowerCase().includes((ui.facetSearch["{{ spec_key }}"]||"").toLowerCase())'
```

Keep single-quoted attributes (|tojson emits double quotes) per the existing macro comments.

- [ ] **Step 4: Verify** — `npm run lint && npm run build`; e2e console-error check on the
  connectors filter; confirm typing filters the list and the "Show all" toggle still works.
- [ ] **Step 5: Commit** — `feat(materials-filter): search-within long enum facets (P3)`.

---

### Task 3: P2 backend — numeric chip predicate, options, and counts

**Files:**
- Modify: `app/services/faceted_search_service.py` (`_apply_facet_filters`,
  `get_subfilter_options`, `get_facet_counts`; add `NUMERIC_CHIP_N = 8`)
- Test: `tests/test_faceted_search_service.py`

- [ ] **Step 1: Write failing tests.**

```python
def test_apply_facet_filters_numeric_vals_in(db_session):
    for cap, n in [(8.0, "A"), (16.0, "B"), (32.0, "C")]:
        _seed_card_with_facet(db_session, mpn=n, commodity="dram",
                              spec_key="capacity_gb", value_numeric=cap)
    cards, total = search_materials_faceted(
        db_session, commodity="dram", sub_filters={"capacity_gb__vals": [8, 32]})
    assert total == 2  # 8 and 32, not 16

def test_subfilter_numeric_chips_top_n(db_session):
    # 16GB appears 3×, 8GB 2×, 32GB 1× → chips ordered by numeric value ascending.
    _seed_n(db_session, "dram", "capacity_gb", {8.0: 2, 16.0: 3, 32.0: 1})
    opt = {o["spec_key"]: o for o in get_subfilter_options(db_session, "dram")}["capacity_gb"]
    assert [c["value"] for c in opt["chips"]] == [8.0, 16.0, 32.0]
    assert {c["value"]: c["count"] for c in opt["chips"]} == {8.0: 2, 16.0: 3, 32.0: 1}
    assert opt["range"]["min"] == 8.0 and opt["range"]["max"] == 32.0

def test_facet_counts_numeric_chip_self_exclusion(db_session):
    _seed_n(db_session, "dram", "capacity_gb", {8.0: 2, 16.0: 3})
    # With 8GB actively selected, the capacity facet's own counts still show 16GB (OR-within).
    counts = get_facet_counts(db_session, "dram",
                              active_filters={"capacity_gb__vals": [8]})
    assert counts["capacity_gb"].get("16") == 3 and counts["capacity_gb"].get("8") == 2
```

- [ ] **Step 2: Run, confirm failures** (no `__vals` branch, no `chips`, no numeric counts).

- [ ] **Step 3: Implement.**

(a) `_apply_facet_filters` — add BEFORE the `isinstance(values, list)` branch:

```python
if key.endswith("__vals"):
    if not (isinstance(values, list) and values):
        continue
    spec_key, value_predicate = key[: -len("__vals")], MaterialSpecFacet.value_numeric.in_(values)
elif key.endswith("_min"):
    ...
```

(b) `get_subfilter_options` numeric branch — add a grouped top-N numeric query (mirroring the
text `count_map`) and attach chips ordered by numeric value:

```python
NUMERIC_CHIP_N = 8
# ... in the numeric grouped query, also collect counts per (spec_key, value_numeric)
# numeric_count_map: {spec_key: {value: card_count}}
top = sorted(numeric_count_map.get(schema.spec_key, {}).items(),
             key=lambda kv: kv[1], reverse=True)[:NUMERIC_CHIP_N]
option["chips"] = [{"value": v, "count": c} for v, c in sorted(top)]  # display asc by value
```

(c) `get_facet_counts` — add a numeric-count path parallel to the `value_text` passes, returning
`{spec_key: {str(value): count}}` for chip-eligible numeric specs, with the same pass-1 / pass-2
self-exclusion logic (treat a `"{spec_key}__vals"` active filter as the actively-filtered key
whose siblings must be recomputed without its own selection).

- [ ] **Step 4: Run tests, confirm PASS;** run the broader faceted-search module green.
- [ ] **Step 5: Commit** — `feat(materials-filter): numeric common-value chip backend (P2)`.

---

### Task 4: P2 frontend — Alpine toggle + chip rendering

**Files:**
- Modify: `app/static/htmx_app.js` (`materialsFilter()` — add `toggleNumericChip`; extend
  `clearSubFilters`, `syncToURL`, `syncFromURL`, applied-chip strip / active-count)
- Modify: `app/templates/htmx/partials/materials/filters/_macros.html` (`render_subfilter`
  numeric branch renders chips above `range_input`)
- Test: frontend test/e2e — toggle a chip, assert URL param + applied-chip + result refresh.

- [ ] **Step 1: Write failing e2e/frontend test** — on the DRAM commodity, click a capacity
  chip; assert `subFilters['capacity_gb__vals']` contains the value, the URL gains
  `capacity_gb__vals=…`, an applied-filter chip appears, and results refresh; clicking again
  removes it.

- [ ] **Step 2: Run, confirm failure.**

- [ ] **Step 3: Implement.**

Alpine:
```js
toggleNumericChip(specKey, value) {
  const k = specKey + '__vals';
  const arr = this.subFilters[k] ? [...this.subFilters[k]] : [];
  const i = arr.indexOf(value);
  if (i === -1) arr.push(value); else arr.splice(i, 1);
  if (arr.length) this.subFilters[k] = arr; else delete this.subFilters[k];
  this.applyFilters();
},
```
Extend `syncToURL` (emit `k=v1,v2`), `syncFromURL` (split, `Number(...)`, drop `NaN`),
`clearSubFilters` (drop `*__vals`), and the applied-filter chip strip + active-count to include
`*__vals` selections.

Template `render_subfilter` numeric branch — before `range_input`:
```jinja2
{% if opt.chips %}
<div class="mb-1.5 flex flex-wrap gap-1">
  {% for chip in opt.chips %}
  <button type="button" @click="toggleNumericChip('{{ opt.spec_key }}', {{ chip.value|tojson }})"
          :class="(subFilters['{{ opt.spec_key }}__vals'] || []).includes({{ chip.value|tojson }}) ? 'bg-brand-50 text-brand-700 border-brand-300' : 'bg-white text-gray-500 border-gray-200 hover:border-gray-300'"
          class="px-2 py-0.5 text-[12px] border rounded-md transition-colors tabular-nums">
    {{ '%g'|format(chip.value) }} <span class="opacity-60">({{ chip.count }})</span>
  </button>
  {% endfor %}
</div>
{% endif %}
```

- [ ] **Step 4: Verify** — `npm run lint && npm run build`; e2e passes; no console errors;
  range inputs still work alongside chips.
- [ ] **Step 5: Commit** — `feat(materials-filter): numeric common-value chips UI (P2)`.

---

### Task 5: Final review + finish

- [ ] Dispatch a final code reviewer over the whole branch diff (spec compliance + quality).
- [ ] Run `pre-commit run --files <changed>` and the faceted-search test module + frontend build.
- [ ] Update `docs/APP_MAP_INTERACTIONS.md` (filter widgets) per the update-APP_MAP rule.
- [ ] Live-verify on real Postgres after deploy (numeric grouping is DB-specific — guard the
  SQLite-masks-Postgres class): chips render with correct counts and filter correctly.
- [ ] Use superpowers:finishing-a-development-branch to open the PR.
