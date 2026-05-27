# IBM Spec Code Resolver — Design

**Date:** 2026-05-27
**Status:** Approved (pending implementation plan)
**Author:** Claude (Opus 4.7), under direction of the project owner
**Scope:** sourcing engine — resolution layer between requirement and connector fanout

---

## 1. Problem statement

The sourcing engine fans out queries to eight distributor and broker connectors (Mouser, DigiKey, Element14, OEMSecrets, Sourcengine, eBay, BrokerBin, Nexar/Octopart), all of which are keyed on **manufacturer part numbers (MPNs)**. When a buyer's input is an **OEM internal spec code** — for example IBM's `SPREJ`, which references a part by its specification rather than by a specific manufacturer — every connector returns zero results, because spec codes are not indexed in public distributor or broker catalogs.

This was discovered during an attempt to source 700 pcs of `SPREJ`. The connector fanout produced zero authorized-distributor hits and zero broker hits; Mouser returned fuzzy keyword matches unrelated to the actual spec. No code path exists today to translate a spec code into the underlying approved manufacturer part numbers, so spec-coded requirements silently fail.

## 2. Goals and non-goals

### Goals

- Resolve OEM spec codes to the underlying approved MPN(s) (the "Approved Vendor List" — AVL) before connector fanout.
- Run the full sourcing fanout against every resolved AVL MPN.
- Aggregate results under the original spec code so the buyer sees one consolidated view.
- Build a self-improving mapping table: known mappings are looked up; unknown mappings are discovered via LLM with web grounding, queued for human approval, and promoted to the authoritative table once approved.
- Never silently use an unverified mapping permanently — pending mappings may drive a single speculative fanout but cannot become canonical until a human approves them.

### Non-goals

- Not a generalized "unknown-input resolver" framework (Cisco part codes, customer internal PNs, military FSCM lookups). Schema is multi-OEM-ready; only IBM is loaded and prompted at launch.
- No changes to how known MPNs are sourced. Resolver only fires when the normal fanout returns universal zero.
- No changes to the AI intake parser. Detection happens at sourcing time via the zero-hit gate, not at intake.
- No bulk import UI. Manual AVL entry / CSV import is a future enhancement.
- No changes to the requirement-creation UX.

## 3. Key decisions (settled during brainstorming)

| Decision | Choice | Rationale |
|---|---|---|
| Mapping source | Hybrid: lookup table first, LLM fallback with web grounding, cache after human approval | Pure table is a brick wall on new specs; pure LLM is slow, expensive, and risks hallucination; hybrid grows the table organically with human-verified data. |
| Fanout on 1→N AVL | Fan all resolved MPNs to all connectors in parallel; aggregate results tagged with `source_mpn` | Highest fill rate; a single approved alternate at a broker can be the difference between a closed sale and a lost lead. API cost is bounded by AVL length (typically 2-5). |
| Trust gate on LLM mappings | Speculative sourcing immediately; queue for human approval before caching | Zero buyer wait, no permanent pollution from hallucinations. |
| Detection trigger | Zero-hit fallback — try connectors first, route through resolver on universal zero | Zero false positives on real MPNs. Cost of a wasted fanout on first encounter is negligible (connector calls are cheap). Naturally handles typos: bogus inputs return zero and the resolver decides they're unresolvable. |

## 4. Data model

Three new tables in `app/models/sourcing.py`; one extension to `Sighting`.

### 4.1 `OemSpecCode` — authoritative table

```python
class OemSpecCode(Base):
    __tablename__ = "oem_spec_codes"
    id = Column(Integer, primary_key=True)
    oem = Column(String(64), nullable=False, index=True)          # "IBM" at launch
    spec_code = Column(String(64), nullable=False, index=True)
    avl = Column(JSONB, nullable=False)                            # [{"mpn", "manufacturer", "rank", "notes"}]
    source = Column(String(32), nullable=False)                    # "manual" | "llm_approved" | "csv_import"
    approved_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    approved_at = Column(UTCDateTime, nullable=False)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (UniqueConstraint("oem", "spec_code", name="uq_oem_spec_code"),)
```

`avl` is a list of objects with shape `{"mpn": str, "manufacturer": str, "rank": int, "notes": str | null}`. `rank` orders preference; lower is better. `notes` may carry revision, datecode, or substitution caveats from the AVL source.

### 4.2 `OemSpecCodePending` — discovery queue

```python
class OemSpecCodePending(Base):
    __tablename__ = "oem_spec_codes_pending"
    id = Column(Integer, primary_key=True)
    oem = Column(String(64), nullable=False, index=True)
    spec_code = Column(String(64), nullable=False, index=True)
    proposed_avl = Column(JSONB, nullable=False)                   # same shape as OemSpecCode.avl
    llm_confidence = Column(Float, nullable=False)                 # 0..1 self-rated
    citations = Column(JSONB, default=list)                        # [{"url", "snippet"}]
    discovered_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    first_requirement_id = Column(Integer, ForeignKey("requirements.id", ondelete="SET NULL"))
    used_in_requirement_ids = Column(JSONB, default=list)          # ids that consumed it speculatively
    __table_args__ = (UniqueConstraint("oem", "spec_code", name="uq_pending_oem_spec_code"),)
```

A row exists only while the mapping is unverified. Approval promotes it to `OemSpecCode` and deletes the pending row. Rejection moves it to the blacklist and deletes the pending row.

### 4.3 `OemSpecCodeBlacklist` — rejected mappings

```python
class OemSpecCodeBlacklist(Base):
    __tablename__ = "oem_spec_codes_blacklist"
    id = Column(Integer, primary_key=True)
    oem = Column(String(64), nullable=False, index=True)
    spec_code = Column(String(64), nullable=False)
    rejected_mpns = Column(JSONB, nullable=False)                  # list of mpns previously proposed and rejected
    rejected_by_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))
    rejected_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))
    reason = Column(Text)
```

Used as an LLM-prompt input ("do not propose these MPNs") so rejected mappings don't keep getting suggested.

### 4.4 `Sighting` and `Offer` extension

Add two nullable columns to **both** `Sighting` and `Offer`:

- `resolved_via_spec_code: String(64) | null` — the spec code that triggered resolution, if any.
- `source_mpn: String(255) | null` — the AVL MPN this sighting/offer was actually fetched against.

Both nullable; no schema break for existing rows. An offer derived from a spec-resolved sighting carries the same lineage tags.

### 4.5 `Requirement` extension

Add one nullable column to `Requirement`:

- `oem_hint: String(64) | null` — an optional buyer-supplied hint identifying which OEM's spec-code vocabulary applies. When `null`, the resolver defaults to `"IBM"` at launch. Future-proofs the schema for multi-OEM expansion without breaking changes.

## 5. Service layer

New module: `app/services/spec_code_resolver.py`.

```python
@dataclass
class ResolverResult:
    status: Literal["approved", "pending", "unresolved"]
    avl: list[dict]              # [{"mpn", "manufacturer", "rank", "notes"}]
    confidence: float            # 1.0 if approved; LLM-self-rated if pending; 0.0 if unresolved
    citations: list[dict]        # [{"url", "snippet"}]; empty for approved
    source: Literal["table", "llm", "none"]


class SpecCodeResolver:
    def __init__(self, db: Session, ai_client: AIClient, web_search: WebSearchClient): ...

    def resolve(self, spec_code: str, oem: str = "IBM") -> ResolverResult:
        """
        Resolution order:
          1. OemSpecCode hit       -> ResolverResult(status="approved", source="table")
          2. OemSpecCodePending hit -> ResolverResult(status="pending",  source="llm")  # reuse prior LLM result
          3. Blacklist check: load OemSpecCodeBlacklist.rejected_mpns for this (oem, spec_code)
          4. LLM call (Claude Opus + grounded WebSearch), passing the blacklist as exclusion set
          5. Validate response (schema, confidence floor)
             - confidence < SPEC_RESOLVER_MIN_CONFIDENCE (default 0.3) -> ResolverResult("unresolved")
             - else -> write OemSpecCodePending row; ResolverResult("pending", source="llm")
        """
```

### 5.1 LLM contract

Single Claude Opus call with grounded web search. System message describes the task; user message provides the spec code and the blacklist.

**Output schema (strict JSON):**

```json
{
  "avl": [
    {"mpn": "GRM188R71H103KA01D", "manufacturer": "Murata", "rank": 1, "notes": "primary AVL, all revs"},
    {"mpn": "C0603C103K5RACTU",   "manufacturer": "KEMET",  "rank": 2, "notes": "approved alternate"}
  ],
  "confidence": 0.82,
  "citations": [
    {"url": "https://www.ibm.com/.../redbook-...pdf", "snippet": "SPREJ — 10nF 50V X7R 0603, Murata GRM188..."}
  ],
  "reasoning": "Brief explanation of how the mapping was derived."
}
```

Empty `avl` + `confidence: 0` is the explicit "I don't know" response. Anything that fails to parse against this schema is treated as `unresolved` and logged.

### 5.2 Confidence floor

`settings.SPEC_RESOLVER_MIN_CONFIDENCE` (default `0.3`). Results below this floor are not written to the pending table — they're treated as unresolved. Web-search-unavailable results receive a `× 0.7` penalty before comparison.

## 6. Integration with the sourcing fanout

Modify `app/services/enrichment.py`. The fanout pseudocode becomes:

```
function enrich_requirement(req):
    # Phase 1: existing behavior, unchanged
    normalized = ai_normalizer.normalize(req.primary_mpn)
    results = fanout_all_connectors(normalized)
    if aggregate_authorized_and_broker_count(results) > 0:
        persist_sightings(req, results, resolved_via_spec_code=None, source_mpn=normalized)
        return

    # Phase 2: zero-hit fallback — try spec resolver
    if not settings.SPEC_RESOLVER_ENABLED:
        return  # legacy behavior

    resolution = SpecCodeResolver(db, ai, web).resolve(req.primary_mpn, oem=req.oem_hint or "IBM")
    if resolution.status == "unresolved":
        return

    # Phase 3: parallel fanout per AVL MPN
    tagged_results = []
    for entry in resolution.avl:
        per_mpn = fanout_all_connectors(entry["mpn"])
        tagged_results.append((entry["mpn"], per_mpn))

    persist_sightings(req, tagged_results,
                      resolved_via_spec_code=req.primary_mpn,
                      source_mpn_per_result=True)

    if resolution.status == "pending":
        append_requirement_id_to_pending_row(req.id, req.primary_mpn)
```

`req.oem_hint` is a new nullable column on `Requirement` (default `null`); when `null`, the resolver defaults to `"IBM"`. This is a one-line model change, not a migration risk — adding a nullable column is safe.

Phase 1 is unchanged code paths and unchanged cost. Phase 2/3 only execute when the existing path returns universal zero. The resolver itself short-circuits at any cached layer (table → pending → blacklist) before issuing an LLM call.

## 7. Admin UI for the approval queue

One new page: `/admin/spec-codes/pending` (gated by `require_settings_access`).

- **Listing.** Server-rendered HTMX table. Columns: spec code, OEM, proposed AVL (collapsible JSON), confidence, citations (link icon), discovered at, requirement IDs that used it speculatively.
- **Row actions:**
  - **Approve** → POST `/admin/spec-codes/{id}/approve`. Promotes to `OemSpecCode`, deletes pending row. Records `approved_by_user_id`, `approved_at`.
  - **Edit & Approve** → POST `/admin/spec-codes/{id}/approve-edited` with an edited AVL payload. Same effect, with user-corrected AVL.
  - **Reject** → POST `/admin/spec-codes/{id}/reject` with a reason. Moves rejected MPNs to `OemSpecCodeBlacklist`, deletes pending row.
  - **Re-resolve** → POST `/admin/spec-codes/{id}/re-resolve`. Re-runs the LLM with the current blacklist; overwrites `proposed_avl`, `llm_confidence`, `citations`. Useful if the LLM's first guess was rejected and we want a second attempt.
- **No bulk operations.** YAGNI; volume will be low at launch.

Routes live in a new file `app/routers/admin/spec_codes.py`. Template lives in `app/templates/htmx/admin/spec_codes_pending.html`. One link from the existing admin index page; no global nav changes.

## 8. Error handling

| Failure | Behavior |
|---|---|
| LLM API down / timeout | `resolve()` returns `unresolved`. Logged to Sentry with the spec code. Sourcing degrades to existing zero-results path. |
| LLM returns malformed JSON or schema-invalid output | Caught, logged to Sentry, treated as unresolved. No partial caching. |
| LLM confidence < floor | Treated as unresolved. Nothing written to pending table. |
| Concurrent resolution of the same spec code | `INSERT ... ON CONFLICT DO NOTHING` on `(oem, spec_code)` for pending rows. After insert, re-read and return the row. |
| WebSearch unavailable | LLM called without grounded citations; result confidence × 0.7 penalty; still allowed if above floor. |
| Approved mapping later discovered wrong | Manually delete from `OemSpecCode` and add to blacklist via SQL or a future admin action. Existing persisted sightings are unaffected; they're historical record. |
| AVL contains an MPN that itself returns zero from all connectors | Recorded as a sighting with zero quantity; surfaces in the buyer's UI as "approved alternate, no current stock." Does not retry through the resolver — recursion is explicitly disallowed. |

## 9. Testing strategy

### Unit tests — `tests/services/test_spec_code_resolver.py`

Stub the AI client and the DB session. Cover every branch:

- Table hit returns `approved` with `source="table"`, no LLM call.
- Pending hit returns `pending`, no LLM call.
- Blacklist-only-proposals path returns `unresolved`.
- LLM success above floor → pending row written, `pending` returned.
- LLM empty AVL → `unresolved`, no pending row.
- LLM confidence below floor → `unresolved`, no pending row.
- LLM malformed JSON → `unresolved`, Sentry capture asserted.
- LLM timeout → `unresolved`, Sentry capture asserted.
- WebSearch unavailable + LLM result with raw confidence 0.5 → penalty applied → 0.35, above floor 0.3 → pending row written.
- Concurrent insert collision (simulated `IntegrityError`) → recover by re-reading existing pending row.

### Integration tests — `tests/services/test_enrichment_with_spec_resolver.py`

Real DB session (Postgres test container). Mock the connectors and the LLM, leave the resolver wiring live.

- Known MPN path: assert zero resolver calls and zero LLM calls.
- Unknown input with mocked LLM returning a 2-MPN AVL: assert two additional fanouts ran, all sightings tagged with `resolved_via_spec_code` and `source_mpn`, pending row created with `used_in_requirement_ids` containing the requirement id.
- Unknown input, blacklist-only candidates: assert no LLM call and no sightings.
- `SPEC_RESOLVER_ENABLED=False`: assert resolver never runs even on zero hits.

### End-to-end — `tests/e2e/test_spec_code_resolver_e2e.py`

One test, real DB + mocked external HTTP:

- Create a requisition with `primary_mpn="SPREJ"`.
- Stub the LLM client to return a fixture AVL containing two MPNs.
- Stub connector responses: one MPN has 1500 pcs at a broker; the other has zero.
- Assert: at least one sighting persisted, tagged `resolved_via_spec_code="SPREJ"` and `source_mpn` matching the fixture; pending row exists.

### Admin UI smoke tests — `tests/routers/admin/test_spec_codes_pending.py`

- GET `/admin/spec-codes/pending` returns 200 with seeded data visible.
- Approve action: pending row deleted, `OemSpecCode` row exists with `approved_by_user_id` set.
- Reject action: pending row deleted, `OemSpecCodeBlacklist` row exists with reason.
- Re-resolve action: pending row updated with new LLM output.

## 10. Build sequence

Stacked PRs, mergeable bottom-up. Each PR is small and independently testable.

| # | PR title | Contents | Depends on |
|---|---|---|---|
| 1 | `feat(db): migrations for oem_spec_codes tables and sighting/offer lineage columns` | Alembic migration only. Verify clean apply on empty DB. | — |
| 2 | `feat(models): OemSpecCode, OemSpecCodePending, OemSpecCodeBlacklist, Requirement.oem_hint` | SQLAlchemy models + Pydantic schemas + unit tests for invariants. | 1 |
| 3 | `feat(services): SpecCodeResolver service` | The resolver class, LLM prompt module, unit tests. Not wired into enrichment yet. | 2 |
| 4 | `feat(sourcing): wire SpecCodeResolver into enrichment fanout` | Modify `enrichment.py`; add `SPEC_RESOLVER_ENABLED` flag (default `False`); integration tests; e2e test. | 3 |
| 5 | `feat(admin): pending spec-code approval queue UI` | New router + template + smoke tests. | 2 |
| 6 | `chore(config): enable SPEC_RESOLVER_ENABLED in production; update APP_MAP docs` | Flag flip + docs. | 4, 5 |

PRs 3 and 5 can be developed in parallel by separate subagents once PR 2 is merged. PR 6 is a one-line config change after 4 and 5 land.

## 11. Operational notes

- **DB state.** Per project memory: the production DB is intentionally empty pending SFDC import. The three new tables are independent of `materials` and any other SFDC-imported entity, so PR-1 can land any time without coordination.
- **Nexar quota.** Already exhausted as of 2026-05-27 (separate ops issue). Does not block this work; the resolver does not depend on Nexar.
- **Sentry.** Resolver failures use the existing Sentry instrumentation. New tags: `spec_code`, `oem`, `resolver_phase`.
- **Cost.** Bounded by zero-hit volume. One Opus call per first encounter with each spec code; cached afterward. Single-digit dollars/month at launch volume.
- **Feature flag.** `SPEC_RESOLVER_ENABLED` defaults to `False` so PR 4 can land without behavior change. PR 6 flips it.
- **App health.** Separate from this work, the production container is currently `Up (unhealthy)` with `db` DNS resolution errors in the logs. That's a deployment-environment issue, not a blocker for this design.

## 12. Out of scope (deferred)

These were considered and explicitly deferred:

- Generalizing to OEMs beyond IBM (schema is ready; prompts and seed data are not).
- Bulk CSV import of AVL tables (manual entry sufficient for launch).
- A buyer-facing "this was resolved from a spec code" indicator in the requisition UI beyond the existing `cross_references` display.
- Auto-promotion of high-confidence LLM mappings without human review (decided against in brainstorming).
- Recursive resolution (AVL MPN itself being a spec code) — explicitly disallowed.
- Integration with IBM's internal PLM systems for authoritative AVL fetch (would require enterprise access not in scope).
