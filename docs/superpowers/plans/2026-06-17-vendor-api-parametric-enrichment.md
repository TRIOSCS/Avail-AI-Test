# Vendor-API Parametric Enrichment — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Populate the deep materials filter facets (capacitance/voltage/tolerance/package/resistance/…) for the high-demand commodity subset by harvesting structured parametrics from the Mouser & Element14 APIs, demand-ordered and quota-paced.

**Architecture:** Extend the Mouser/Element14 connectors to map raw `ProductAttributes`/params → our seeded spec keys; a `vendor_spec_enrich` writer writes category + specs via the F1 ladder at the existing `mouser_api`/`element14_api` tier 90; a `backfill_vendor_specs` CLI selects cards `sourced_qty_90d DESC`, calls connectors within a daily quota, and writes via the writer (dry-run default, resumable, metered).

**Tech Stack:** FastAPI/SQLAlchemy app; connectors in `app/connectors/`; F1 ladder in `app/services/spec_tiers.py` + `app/services/spec_write_service.py`; seeded commodities in `app/data/commodity_seeds.json`; `intel_cache` date counters; host run via `scripts/mgmt.sh`.

**Spec:** `docs/superpowers/specs/2026-06-17-vendor-api-parametric-enrichment-design.md`

> ⚠️ **REVISED 2026-06-17 (post Task-2 harvest).** The measure-gate found Mouser's
> `ProductAttributes` carry NO parametrics (only Packaging). See spec **Revision 1**. Net plan
> change: **Task 3 (map Mouser ProductAttributes) is DROPPED.** Replace with:
> **Task 3' — extend `app/services/desc_extractor/` to passive commodities** (capacitors,
> resistors, mosfets, …) so the grammar parses capacitance/voltage/dielectric/package/tolerance/
> resistance from the distributor *description* (Mouser's description is rich + good quota; it
> already flows via the shipped `connector_desc` harvest at tier 84 — no Mouser connector change).
> **Task 4 stands** (extend `element14.py:_parse` to map structured `attributes`→facets, tier 90,
> bounded slice — Element14 rate-limits hard). Tasks 5-7 stand. Build Task 3' as one TDD module
> per commodity (mirror `desc_extractor/storage.py`/`memory.py` + register in the dispatch and
> `SPEC_COMMODITIES`), highest-demand commodity first (capacitors → resistors → mosfets).

**Reference signatures (do not re-derive):**
- `set_category(card, value, source, confidence, *, write=True) -> bool` (`spec_tiers.py:671`) — normalizes commodity, ladder-arbitrated.
- `record_spec(db, card_id, spec_key, value, *, source, confidence, unit=None, schema_cache=None) -> bool` (`spec_write_service.py:171`) — validates/normalizes, syncs facets, ladder-arbitrated, no commit.
- `generic_attribute(attrs, name_key, value_key, names) -> str | None` (`_core_attrs.py:67`) — pulls a named attribute from a `[{name_key:…, value_key:…}]` list.
- Sources `mouser_api`/`element14_api` already = tier **90** in `SOURCE_TIER`.
- CLI template: `app/management/backfill_oem_crosswalk.py` (dry-run/apply, daily caps via `intel_cache.incr_count`, chunked commits, demand-ordered select).

---

### Task 1: Isolated worktree + feature branch

**Files:** none (git setup).

- [ ] **Step 1:** Create an isolated worktree (a concurrent session shares this checkout) via the `superpowers:using-git-worktrees` skill / `EnterWorktree`, branch `feat/vendor-api-enrichment` based on `origin/main` (clean base — excludes the concurrent session's unpushed `cc368172`). Copy the spec + this plan into the worktree if not present.
- [ ] **Step 2:** Verify: `git -C <worktree> status` clean, `git branch --show-current` = `feat/vendor-api-enrichment`. All subsequent tasks run inside the worktree; commits go to this branch, never `main`.

---

### Task 2: Harvest the per-commodity attribute alias map (measure step)

**Files:**
- Create: `app/connectors/_vendor_spec_map.py`
- Create (scratch, not committed): a harvest script under `scripts/` run via `scripts/mgmt.sh`-style host invocation.

**Why:** vendor attribute names vary; the map must come from real responses, not guesses.

- [ ] **Step 1:** For the top ~8 demand commodities (capacitors, resistors, dram, mosfets, ssd, hdd, connectors, ics_other), pick ~20 top-demand clean MPNs each from the DB (`sourced_qty_90d DESC`, `< 2147483647`). Call `MouserConnector.search(mpn)` + `Element14Connector.search(mpn)` **in-container** (working creds via `_build_connectors`), and dump the distinct raw `AttributeName` values per commodity. Read-only; respects daily quota (≤ ~320 calls total). Run: `docker compose exec -T app python - <<'PY' … PY`.
- [ ] **Step 2:** Build `_vendor_spec_map.py` as a literal dict `VENDOR_SPEC_MAP: dict[str, dict[str, tuple[str, ...]]]` keyed `commodity -> seeded_spec_key -> (vendor attribute name aliases)`, e.g. `"capacitors": {"capacitance": ("Capacitance",), "voltage": ("Voltage Rating", "Voltage - Rated"), "tolerance": ("Tolerance", "Capacitance Tolerance"), "dielectric": ("Dielectric", "Temperature Coefficient"), "package": ("Package / Case", "Case Code - in")}`. Populate every commodity's entry from the Step 1 dump; seeded keys are the authoritative target (validate each against the card's category schema — drop keys not in the seed schema). File header comment per CLAUDE.md.
- [ ] **Step 3:** Commit: `git add app/connectors/_vendor_spec_map.py && git commit -m "feat(vendor-enrich): per-commodity vendor-attribute alias map (harvested)"`.

---

### Task 3: Mouser connector — extract commodity-specific specs

**Files:**
- Modify: `app/connectors/mouser.py:_parse` (result dict, ~line 150-171)
- Test: `tests/test_mouser_connector.py` (extend if exists, else create)

- [ ] **Step 1: Write the failing test** — with a recorded Mouser response fixture for a ceramic cap (ProductAttributes incl. `{"AttributeName":"Capacitance","AttributeValue":"0.1 µF"}`, voltage, tolerance, package), assert the parsed result dict has `specs == {"capacitance": "0.1 µF", "voltage": …, "tolerance": …, "package": …}` keyed by seeded spec keys, and that an unmapped attribute is absent from `specs`.

```python
def test_mouser_parse_extracts_commodity_specs():
    conn = MouserConnector("k")
    data = {"SearchResults": {"Parts": [{
        "ManufacturerPartNumber": "CL05B104KO5NNWC", "Manufacturer": "Samsung",
        "Category": "Multilayer Ceramic Capacitors", "Description": "CAP CER 0.1UF 16V X7R 0402",
        "ProductAttributes": [
            {"AttributeName": "Capacitance", "AttributeValue": "0.1 µF"},
            {"AttributeName": "Voltage Rating", "AttributeValue": "16 V"},
            {"AttributeName": "Tolerance", "AttributeValue": "±10%"},
            {"AttributeName": "Package / Case", "AttributeValue": "0402"},
            {"AttributeName": "Operating Temperature", "AttributeValue": "-55°C ~ 125°C"},
        ]}]}}
    r = conn._parse(data, "CL05B104KO5NNWC")[0]
    assert r["specs"] == {"capacitance": "0.1 µF", "voltage": "16 V", "tolerance": "±10%", "package": "0402"}
```

- [ ] **Step 2: Run it, verify FAIL** — `TESTING=1 PYTHONPATH=/root/availai python3 -m pytest tests/test_mouser_connector.py::test_mouser_parse_extracts_commodity_specs -v --override-ini="addopts="` → FAIL (`KeyError: 'specs'`).
- [ ] **Step 3: Implement** — in `_parse`, after `category` is computed, normalize it to a commodity (reuse the category-normalization used by `set_category`/the connector-desc path), look up `VENDOR_SPEC_MAP.get(commodity, {})`, and for each `(seeded_key, aliases)` call `generic_attribute(product_attrs, "AttributeName", "AttributeValue", aliases)`; collect non-None into `specs`. Add `"specs": specs` to the result dict.

```python
from app.connectors._vendor_spec_map import VENDOR_SPEC_MAP
from app.services.spec_tiers import normalize_commodity  # the canonical category normalizer
# … inside the part loop, after `category = clean_str(part.get("Category"), maxlen=255)`:
commodity = normalize_commodity(category)
specs = {}
for seeded_key, aliases in VENDOR_SPEC_MAP.get(commodity or "", {}).items():
    v = generic_attribute(product_attrs, "AttributeName", "AttributeValue", aliases)
    if v is not None:
        specs[seeded_key] = v
# … add to results.append({... "specs": specs})
```
(If `normalize_commodity` is not the exact public name, use the same normalizer `set_category` calls — confirm in `spec_tiers.py` during implementation; do not invent.)

- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit** — `git add app/connectors/mouser.py tests/test_mouser_connector.py && git commit -m "feat(vendor-enrich): Mouser _parse maps ProductAttributes -> seeded specs"`.

---

### Task 4: Element14 connector — extract commodity-specific specs

**Files:** Modify `app/connectors/element14.py:_parse`; Test `tests/test_element14_connector.py`.

- [ ] **Step 1-5:** Same pattern as Task 3, using Element14's attribute structure (inspect its raw response param shape from the Task 2 harvest — Farnell returns `attributes` with `attributeLabel`/`attributeValue`; use `generic_attribute(attrs, "attributeLabel", "attributeValue", aliases)`). Test with an Element14 resistor fixture asserting `specs == {"resistance":…, "power":…, "tolerance":…, "package":…}`. Commit.

---

### Task 5: vendor_spec_enrich writer

**Files:**
- Create: `app/services/vendor_spec_enrich.py`
- Test: `tests/test_vendor_spec_enrich.py`

- [ ] **Step 1: Write the failing test** — given a card (uncategorized) and a connector result `{"category":"Multilayer Ceramic Capacitors","manufacturer":"Samsung","specs":{"capacitance":"0.1 µF","voltage":"16 V"}}`, assert `enrich_card_from_vendor(db, card, [result], source="mouser_api")` sets `card.category == "capacitors"` and persists the specs (a follow-up `record_spec`-readback / facet query shows capacitance+voltage), and returns a `{"categorized":1,"specs_written":2}` summary.

```python
def test_enrich_writes_category_and_specs(db_session, make_card):
    card = make_card(display_mpn="CL05B104KO5NNWC", category=None)
    res = [{"category": "Multilayer Ceramic Capacitors", "manufacturer": "Samsung",
            "specs": {"capacitance": "0.1 µF", "voltage": "16 V"}}]
    stats = enrich_card_from_vendor(db_session, card, res, source="mouser_api")
    db_session.flush()
    assert card.category == "capacitors"
    assert stats["categorized"] == 1 and stats["specs_written"] == 2
```

- [ ] **Step 2: Run, verify FAIL** (module/function missing).
- [ ] **Step 3: Implement** — `enrich_card_from_vendor(db, card, results, *, source)`: take the best result (first with a `specs` dict / highest confidence); `set_category(card, result["category"], source, confidence=0.97)`; for each `(spec_key, value)` in `result["specs"]` call `record_spec(db, card.id, spec_key, value, source=source, confidence=0.97)`. Tally categorized + specs_written. No commit (caller owns the txn). Header comment.

```python
def enrich_card_from_vendor(db, card, results, *, source):
    stats = {"categorized": 0, "specs_written": 0}
    best = next((r for r in results if r.get("specs")), results[0] if results else None)
    if not best:
        return stats
    if best.get("category") and set_category(card, best["category"], source, 0.97):
        stats["categorized"] = 1
    for key, val in (best.get("specs") or {}).items():
        if record_spec(db, card.id, key, val, source=source, confidence=0.97):
            stats["specs_written"] += 1
    return stats
```

- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit** — `feat(vendor-enrich): vendor_spec_enrich writer (ladder tier 90)`.

---

### Task 6: backfill_vendor_specs CLI

**Files:**
- Create: `app/management/backfill_vendor_specs.py` (mirror `backfill_oem_crosswalk.py`)
- Test: `tests/test_backfill_vendor_specs.py`

- [ ] **Step 1: Write the failing test** — seed 3 cards with `sourced_qty_90d` 100/50/0; mock `_build_connectors` to return a fake Mouser returning specs for the first two; run `run_backfill(db, apply=False, daily_cap=10)`; assert it processes in demand order (100 then 50 then 0), reports `would_enrich`, writes nothing (dry-run), and respects `daily_cap` (stops at cap). Then `apply=True` writes via the writer and increments the per-source date counter.

- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3: Implement** — copy the structure of `backfill_oem_crosswalk.py`: argparse (`--apply`, `--limit`, `--daily-cap`, `--source mouser|element14`), demand-ordered select (`MaterialCard … ORDER BY sourced_qty_90d DESC NULLS LAST, id`), per-card connector `search` within the daily cap (date counter `intel:vendor_api:{source}:calls:{date}` via `intel_cache.incr_count`, cap check before each call), `enrich_card_from_vendor` write, per-chunk commit, dry-run report. Reuse the connector circuit breakers/semaphores already in `BaseConnector`.
- [ ] **Step 4: Run, verify PASS.**
- [ ] **Step 5: Commit** — `feat(vendor-enrich): demand-ordered quota-paced backfill_vendor_specs CLI`.

---

### Task 7: Dry-run → live batch → coverage check → campaign

**Files:** none (operational).

- [ ] **Step 1:** Dry-run: `scripts/mgmt.sh backfill_vendor_specs --limit 500` → review would-enrich counts + demand ordering.
- [ ] **Step 2:** Capture baseline: `docker compose exec -T app python -m app.management.enrichment_coverage_report` (note facet coverage).
- [ ] **Step 3:** Live batch: `scripts/mgmt.sh backfill_vendor_specs --apply --limit 200` (DB write — needs the `scripts/mgmt.sh:*`/`bash:*` allow path). Verify: re-run coverage report → demand-weighted facet coverage rose; spot-check 5 cards' new capacitance/voltage/etc. facets in the DB.
- [ ] **Step 4:** If clean, run the full paced campaign day-over-day within quota (re-invoke each day or via a cron); monitor the per-source request counters and coverage report. Stop criterion: high-demand subset facet coverage plateau, or quota economics revisited.

---

## Self-Review

- **Spec coverage:** connector extension (Task 3-4) ✓; writer at tier 90 (Task 5) ✓; demand-ordered quota-paced metered backfill (Task 6) ✓; harvest step (Task 2) ✓; measure-gated rollout (Task 7) ✓; isolation/branch (Task 1) ✓. Mouser+Element14 only ✓. No Nexar/DigiKey/Clay/long-tail ✓.
- **Placeholder scan:** the only "fill from harvest" item (Task 2's alias map) is a defined output of a concrete harvest step, not a TBD. One flagged uncertainty: the exact public name of the category normalizer (`normalize_commodity`) — Task 3 Step 3 instructs to confirm the real name in `spec_tiers.py`, not invent.
- **Type consistency:** `enrich_card_from_vendor(db, card, results, *, source)` and its `{"categorized","specs_written"}` summary used consistently (Task 5 ↔ Task 6); `specs` dict key consistent across Tasks 3/4/5/6; `record_spec`/`set_category` signatures match their definitions.
