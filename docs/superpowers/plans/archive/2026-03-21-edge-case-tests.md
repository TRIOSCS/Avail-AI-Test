# Edge Case Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ~101 edge case tests across 6 critical modules to catch boundary, null, error-path, and business-logic regressions.

**Architecture:** Each task adds tests to an existing test file. No new source code — tests only. Each task is independent and can run in parallel.

**Tech Stack:** pytest, pytest-asyncio, unittest.mock (MagicMock, AsyncMock, patch), SQLAlchemy in-memory SQLite via `db_session` fixture.

**Spec:** `docs/superpowers/specs/2026-03-21-edge-case-tests-design.md`

---

### Task 1: Enrichment Edge Cases

**Files:**
- Modify: `tests/test_enrichment_orchestrator.py`
- Modify: `tests/test_enrich_batch.py`
- Reference: `app/services/enrichment_orchestrator.py:302-370` (`apply_confident_data`)
- Reference: `app/services/enrichment.py:117-156` (`enrich_batch`)

**Context:** `apply_confident_data(entity, merged, db, threshold=0.90)` takes a list of `{field, value, confidence, source, reasoning}` dicts and applies fields where `confidence >= threshold`. Returns `{applied, rejected, sources_used}`. The entity is any object with settable attributes. `fire_all_sources` is async, returns `{source_name: result | None}`. `claude_merge` is async, merges multi-source results.

**Existing tests (DO NOT duplicate):** `test_apply_confident_above_threshold`, `test_apply_confident_below_threshold`, `test_fire_all_sources_company`, `test_fire_all_sources_contact`, `test_partial_failure_handling`, `test_claude_merge_picks_best`, `test_enrich_on_demand_end_to_end`.

**Existing fixture pattern:**
```python
def _make_entity(**kwargs):
    entity = MagicMock()
    attrs = {"legal_name": None, "domain": None, "industry": None, ...}
    attrs.update(kwargs)
    # hasattr returns True for keys in attrs
```

- [ ] **Step 1: Add confidence gate boundary tests to test_enrichment_orchestrator.py**

Append these tests to the file:

```python
class TestApplyConfidentBoundaries:
    """Edge cases for the 0.90 confidence threshold gate."""

    def _make_entity(self):
        entity = MagicMock()
        for attr in ("legal_name", "domain", "industry", "last_enriched_at", "enrichment_source"):
            setattr(entity, attr, None)
        entity.__class__.__name__ = "Company"
        return entity

    def test_confidence_exactly_at_threshold(self, db_session):
        entity = self._make_entity()
        merged = [{"field": "legal_name", "value": "Acme", "confidence": 0.90, "source": "s1", "reasoning": "x"}]
        result = apply_confident_data(entity, merged, db_session, threshold=0.90)
        assert len(result["applied"]) == 1
        assert result["applied"][0]["value"] == "Acme"

    def test_confidence_just_below_threshold(self, db_session):
        entity = self._make_entity()
        merged = [{"field": "legal_name", "value": "Acme", "confidence": 0.8999, "source": "s1", "reasoning": "x"}]
        result = apply_confident_data(entity, merged, db_session, threshold=0.90)
        assert len(result["applied"]) == 0
        assert len(result["rejected"]) == 1

    def test_confidence_just_above_threshold(self, db_session):
        entity = self._make_entity()
        merged = [{"field": "legal_name", "value": "Acme", "confidence": 0.9001, "source": "s1", "reasoning": "x"}]
        result = apply_confident_data(entity, merged, db_session, threshold=0.90)
        assert len(result["applied"]) == 1

    def test_confidence_none_raises_type_error(self, db_session):
        """confidence=None triggers TypeError on >= comparison. This documents current behavior."""
        entity = self._make_entity()
        merged = [{"field": "legal_name", "value": "Acme", "confidence": None, "source": "s1", "reasoning": "x"}]
        with pytest.raises(TypeError):
            apply_confident_data(entity, merged, db_session, threshold=0.90)

    def test_confidence_zero(self, db_session):
        entity = self._make_entity()
        merged = [{"field": "legal_name", "value": "Acme", "confidence": 0.0, "source": "s1", "reasoning": "x"}]
        result = apply_confident_data(entity, merged, db_session, threshold=0.90)
        assert len(result["applied"]) == 0
        assert len(result["rejected"]) == 1

    def test_confidence_one(self, db_session):
        entity = self._make_entity()
        merged = [{"field": "legal_name", "value": "Acme", "confidence": 1.0, "source": "s1", "reasoning": "x"}]
        result = apply_confident_data(entity, merged, db_session, threshold=0.90)
        assert len(result["applied"]) == 1

    def test_confidence_above_one_still_applies(self, db_session):
        entity = self._make_entity()
        merged = [{"field": "legal_name", "value": "Acme", "confidence": 1.5, "source": "s1", "reasoning": "x"}]
        result = apply_confident_data(entity, merged, db_session, threshold=0.90)
        assert len(result["applied"]) == 1

    def test_custom_threshold(self, db_session):
        entity = self._make_entity()
        merged = [{"field": "legal_name", "value": "Acme", "confidence": 0.94, "source": "s1", "reasoning": "x"}]
        result = apply_confident_data(entity, merged, db_session, threshold=0.95)
        assert len(result["applied"]) == 0
        assert len(result["rejected"]) == 1

    def test_empty_merged_list(self, db_session):
        entity = self._make_entity()
        result = apply_confident_data(entity, [], db_session)
        assert result["applied"] == []
        assert result["rejected"] == []
        assert result["sources_used"] == []

    def test_field_not_on_entity_skipped(self, db_session):
        entity = MagicMock(spec=[])  # no attributes
        merged = [{"field": "nonexistent_field", "value": "x", "confidence": 0.95, "source": "s1", "reasoning": "x"}]
        result = apply_confident_data(entity, merged, db_session)
        assert len(result["applied"]) == 0
        assert len(result["rejected"]) == 0

    def test_mixed_apply_and_reject(self, db_session):
        entity = self._make_entity()
        merged = [
            {"field": "legal_name", "value": "Acme", "confidence": 0.95, "source": "s1", "reasoning": "x"},
            {"field": "domain", "value": "acme.com", "confidence": 0.50, "source": "s2", "reasoning": "y"},
            {"field": "industry", "value": "Electronics", "confidence": 0.91, "source": "s3", "reasoning": "z"},
        ]
        result = apply_confident_data(entity, merged, db_session)
        assert len(result["applied"]) == 2
        assert len(result["rejected"]) == 1
        assert result["rejected"][0]["field"] == "domain"
```

- [ ] **Step 2: Add fire_all_sources and claude_merge edge cases**

```python
class TestFireAllSourcesEdges:
    """Edge cases for async source orchestration."""

    @pytest.mark.asyncio
    @patch("app.services.enrichment_orchestrator._SOURCE_FUNCS", {})
    async def test_unknown_entity_type_returns_empty(self):
        result = await fire_all_sources("unknown_type", "test-id")
        assert result == {}

    @pytest.mark.asyncio
    async def test_all_sources_return_none(self):
        """When every source function returns None, all values in result are None."""
        null_fn = AsyncMock(return_value=None)
        with patch("app.services.enrichment_orchestrator.COMPANY_SOURCES", ["null_src"]), \
             patch.dict("app.services.enrichment_orchestrator._SOURCE_FUNCS", {"null_src": null_fn}):
            result = await fire_all_sources("company", "test-id")
            assert all(v is None for v in result.values())


class TestClaudeMergeEdges:
    """Edge cases for multi-source merge logic."""

    @pytest.mark.asyncio
    async def test_no_valid_sources_returns_empty(self):
        result = await claude_merge({"src1": None, "src2": None}, "company")
        assert result == []

    @pytest.mark.asyncio
    @patch("app.services.enrichment_orchestrator.claude_json", new_callable=AsyncMock)
    async def test_single_source_skips_claude(self, mock_claude):
        raw = {"src1": {"legal_name": "Acme"}}
        result = await claude_merge(raw, "company")
        mock_claude.assert_not_called()
        assert len(result) > 0
        assert all(item["confidence"] == 0.85 for item in result)
```

- [ ] **Step 3: Add batch enrichment edge cases to test_enrichment_orchestrator.py**

Note: `test_enrich_batch.py` tests `scripts/enrich_batch.py` (different module). These tests target `app.services.enrichment.enrich_batch` and belong in `test_enrichment_orchestrator.py`.

```python
class TestEnrichBatchEdges:
    """Edge cases for batch material card enrichment."""

    @pytest.mark.asyncio
    @patch("app.services.enrichment.enrich_material_card", new_callable=AsyncMock)
    async def test_batch_empty_list(self, mock_enrich, db_session):
        result = await enrich_batch([], db_session)
        assert result["total"] == 0
        assert result["matched"] == 0
        mock_enrich.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.services.enrichment.enrich_material_card", new_callable=AsyncMock)
    async def test_batch_single_item(self, mock_enrich, db_session):
        mock_enrich.return_value = {"manufacturer": "TI", "category": "IC", "source": "test", "confidence": 0.95}
        result = await enrich_batch(["LM317T"], db_session)
        assert result["total"] == 1

    @pytest.mark.asyncio
    @patch("app.services.enrichment.enrich_material_card", new_callable=AsyncMock)
    async def test_batch_all_fail_no_crash(self, mock_enrich, db_session):
        mock_enrich.return_value = None
        result = await enrich_batch(["BAD1", "BAD2"], db_session)
        assert result["matched"] == 0
        assert result["skipped"] == 2
```

- [ ] **Step 4: Run enrichment tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_enrichment_orchestrator.py -v`
Expected: All new + existing tests PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_enrichment_orchestrator.py
git commit -m "test: add enrichment edge cases (confidence gate, fire_all_sources, batch)"
```

---

### Task 2: Requisition Service Edge Cases

**Files:**
- Modify: `tests/test_requisition_state.py`
- Modify: `tests/test_requisition_service.py`
- Reference: `app/services/requisition_state.py` (ALLOWED_TRANSITIONS, `transition()`)
- Reference: `app/services/requisition_service.py` (`clone_requisition`, `safe_commit`, utilities)

**Context:** `transition(req, new_status, actor, db)` validates against ALLOWED_TRANSITIONS dict and raises ValueError for illegal moves. `clone_requisition(db, source_req, user_id)` clones with requirements and offers. `safe_commit(db, entity="record")` catches IntegrityError → HTTP 409.

**ALLOWED_TRANSITIONS:**
```
draft→{active,archived}, open→{active,sourcing,offers,quoting,won,archived},
active→{sourcing,offers,quoting,won,archived}, sourcing→{active,offers,archived},
offers→{quoting,won,archived}, quoting→{quoted,reopened,won,archived},
quoted→{won,lost,reopened,archived}, reopened→{quoting,won,archived},
won→{active,archived}, lost→{active,archived,reopened}, archived→{active}
```

**Existing tests (DO NOT duplicate):** `test_allowed_transition`, `test_illegal_transition_raises` (draft→won), `test_noop_when_same_status`, `test_archived_to_active`, `test_won_can_go_to_active`, `test_none_actor`, `test_activity_log_exception_suppressed`, `test_clone_requisition_duplicate_mpn_preserves_offer_mapping`.

- [ ] **Step 1: Add state transition edge cases to test_requisition_state.py**

```python
class TestTransitionEdgeCases:
    """Boundary and illegal transition edge cases."""

    def _make_user_and_req(self, db_session, status, suffix=""):
        import uuid
        uid = uuid.uuid4().hex[:8]
        user = User(email=f"edge-{uid}@test.com", name="T", role="admin", azure_id=f"edge-{uid}")
        db_session.add(user)
        db_session.flush()
        req = Requisition(name=f"test-{suffix or uid}", status=status, created_by=user.id)
        db_session.add(req)
        db_session.flush()
        return user, req

    def test_archived_to_won_fails(self, db_session):
        user, req = self._make_user_and_req(db_session, "archived")
        with pytest.raises(ValueError, match="Invalid transition"):
            transition(req, "won", user, db_session)

    def test_lost_to_sourcing_fails(self, db_session):
        user, req = self._make_user_and_req(db_session, "lost")
        with pytest.raises(ValueError, match="Invalid transition"):
            transition(req, "sourcing", user, db_session)

    def test_won_to_archived_to_active_roundtrip(self, db_session):
        user, req = self._make_user_and_req(db_session, "won")
        transition(req, "archived", user, db_session)
        assert req.status == "archived"
        transition(req, "active", user, db_session)
        assert req.status == "active"

    def test_rapid_double_transition(self, db_session):
        user, req = self._make_user_and_req(db_session, "active")
        transition(req, "sourcing", user, db_session)
        transition(req, "active", user, db_session)
        assert req.status == "active"

    def test_every_illegal_transition_from_archived(self, db_session):
        """archived can ONLY go to active. All others must fail."""
        user, _ = self._make_user_and_req(db_session, "active")
        illegal = {"won", "lost", "sourcing", "offers", "quoting", "quoted", "reopened", "draft", "open"}
        for target in illegal:
            req = Requisition(name=f"test-{target}", status="archived", created_by=user.id)
            db_session.add(req)
            db_session.flush()
            with pytest.raises(ValueError):
                transition(req, target, user, db_session)
```

- [ ] **Step 2: Add requisition service edge cases to test_requisition_service.py**

```python
class TestCloneEdgeCases:
    """Edge cases for clone_requisition."""

    def _make_user(self, db_session):
        import uuid
        uid = uuid.uuid4().hex[:8]
        user = User(email=f"clone-{uid}@test.com", name="T", role="admin", azure_id=f"clone-{uid}")
        db_session.add(user)
        db_session.flush()
        return user

    def test_clone_with_zero_requirements(self, db_session):
        user = self._make_user(db_session)
        source = Requisition(name="empty", status="active", created_by=user.id)
        db_session.add(source)
        db_session.flush()
        clone = clone_requisition(db_session, source, user.id)
        assert clone.id != source.id
        assert clone.name.startswith("empty")

    def test_clone_preserves_name_prefix(self, db_session):
        user = self._make_user(db_session)
        source = Requisition(name="Original RFQ", status="active", created_by=user.id)
        db_session.add(source)
        db_session.flush()
        clone = clone_requisition(db_session, source, user.id)
        assert "Original RFQ" in clone.name


class TestParseEdgeCases:
    """Boundary cases for parsing helpers."""

    def test_parse_date_field_whitespace_only_raises(self):
        with pytest.raises(HTTPException) as exc_info:
            parse_date_field("   ", "deadline")
        assert exc_info.value.status_code == 400

    def test_parse_positive_int_float_string_raises(self):
        with pytest.raises(HTTPException) as exc_info:
            parse_positive_int("3.14", "quantity")
        assert exc_info.value.status_code == 400

    def test_parse_positive_int_max_value(self):
        result = parse_positive_int(999999999, "quantity")
        assert result == 999999999

    def test_to_utc_with_far_future_date(self):
        from datetime import datetime as dt_cls, timezone as tz
        dt = dt_cls(2099, 12, 31, 23, 59, 59, tzinfo=tz.utc)
        assert to_utc(dt) == dt

    def test_safe_commit_on_generic_exception(self, db_session):
        db_session.commit = MagicMock(side_effect=Exception("unexpected"))
        with pytest.raises(Exception, match="unexpected"):
            safe_commit(db_session, entity="test")
```

- [ ] **Step 3: Run requisition tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_requisition_state.py tests/test_requisition_service.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_requisition_state.py tests/test_requisition_service.py
git commit -m "test: add requisition state transition and service edge cases"
```

---

### Task 3: Proactive Matching Edge Cases

**Files:**
- Modify: `tests/test_proactive_matching.py`
- Modify: `tests/test_proactive_helpers.py`
- Reference: `app/services/proactive_matching.py` (`_score_margin`, `_score_recency`, `_score_frequency`, `compute_match_score`)
- Reference: `app/services/proactive_helpers.py` (`is_do_not_offer`, `is_throttled`, batch helpers)

**Context:** Scoring functions: `_score_recency(last_purchased_at) -> int` (<=180→100, <=365→80, <=730→60, >730→40, None→20). `_score_frequency(purchase_count) -> int` (>=5→100, >=3→80, ==2→60, ==1→40). `_score_margin(customer_avg_price, our_cost) -> tuple[int, float|None]` (>=30→100, >=20→80, >=10→60, >0→40, <=0→10, unknown→50). `compute_match_score` uses weights: recency 40%, frequency 30%, margin 30%. Min margin filter: `if margin_pct is not None and margin_pct < min_margin: continue`.

**Existing scoring tests (DO NOT duplicate):** `test_score_recency_365_days`, `test_score_recency_730_days`, `test_score_margin_10_to_20_pct`, `test_score_margin_0_to_10_pct`, `test_score_negative_margin`, `test_score_no_purchase_date`.

- [ ] **Step 1: Add scoring boundary tests to test_proactive_matching.py**

```python
class TestScoringBoundaries:
    """Exact boundary tests for scoring tier transitions."""

    def test_margin_exactly_zero_scores_10(self):
        score, margin = _score_margin(100.0, 100.0)  # (100-100)/100 = 0%
        assert score == 10
        assert margin == pytest.approx(0.0)

    def test_margin_exactly_10_pct_scores_60(self):
        # Formula: (avg_price - cost) / avg_price * 100 = 10%
        # avg_price=100, cost=90 → (100-90)/100 = 10%
        score, margin = _score_margin(100.0, 90.0)
        assert score == 60
        assert margin == pytest.approx(10.0, rel=0.01)

    def test_margin_just_below_10_pct_scores_40(self):
        # avg_price=100, cost=90.01 → (100-90.01)/100 = 9.99%
        score, margin = _score_margin(100.0, 90.01)
        assert score == 40

    def test_margin_exactly_20_pct_scores_80(self):
        # avg_price=100, cost=80 → (100-80)/100 = 20%
        score, margin = _score_margin(100.0, 80.0)
        assert score == 80

    def test_margin_exactly_30_pct_scores_100(self):
        # avg_price=100, cost=70 → (100-70)/100 = 30%
        score, margin = _score_margin(100.0, 70.0)
        assert score == 100
        assert margin == pytest.approx(30.0, rel=0.01)

    def test_margin_just_below_30_pct_scores_80(self):
        # avg_price=100, cost=70.01 → (100-70.01)/100 = 29.99%
        score, margin = _score_margin(100.0, 70.01)
        assert score == 80

    def test_margin_unknown_both_none_scores_50(self):
        score, margin = _score_margin(None, None)
        assert score == 50
        assert margin is None

    def test_margin_unknown_cost_none_scores_50(self):
        score, margin = _score_margin(100.0, None)
        assert score == 50
        assert margin is None

    def test_recency_exactly_180_days_scores_100(self):
        dt = datetime.now(timezone.utc) - timedelta(days=180)
        score = _score_recency(dt)
        assert score == 100

    def test_recency_181_days_scores_80(self):
        dt = datetime.now(timezone.utc) - timedelta(days=181)
        score = _score_recency(dt)
        assert score == 80

    def test_recency_730_days_scores_60(self):
        dt = datetime.now(timezone.utc) - timedelta(days=730)
        score = _score_recency(dt)
        assert score == 60

    def test_recency_731_days_scores_40(self):
        dt = datetime.now(timezone.utc) - timedelta(days=731)
        score = _score_recency(dt)
        assert score == 40

    def test_recency_none_scores_20(self):
        score = _score_recency(None)
        assert score == 20

    def test_recency_future_date_scores_100(self):
        dt = datetime.now(timezone.utc) + timedelta(days=30)
        score = _score_recency(dt)
        assert score == 100  # negative days_ago → <=180

    def test_frequency_zero_scores_40(self):
        score = _score_frequency(0)
        assert score == 40  # 0 falls into count < 2 branch → 40

    def test_frequency_exactly_5_scores_100(self):
        score = _score_frequency(5)
        assert score == 100

    def test_frequency_exactly_3_scores_80(self):
        score = _score_frequency(3)
        assert score == 80

    # Note: test_frequency_exactly_2 omitted — already covered by test_score_frequency_two_purchases

    def test_composite_score_weights(self):
        """Verify composite = recency*0.4 + frequency*0.3 + margin*0.3"""
        dt = datetime.now(timezone.utc) - timedelta(days=90)  # recency=100
        # avg_price=100, cost=70 → 30% margin → margin_score=100, freq=5 → 100
        score, margin = compute_match_score(dt, 5, 100.0, 70.0)
        assert score == 100  # 100*0.4 + 100*0.3 + 100*0.3 = 100
```

- [ ] **Step 2: Add min-margin filter edge case**

```python
class TestMinMarginFilter:
    """Test margin filtering with different min_margin_pct settings."""

    def test_negative_margin_with_zero_min_filtered(self, db_session):
        """margin=-5% with min_margin_pct=0 → filtered out."""
        # Uses the full _setup_scenario pattern from existing tests
        # Set CPH avg_unit_price=95, offer cost=100 → margin ~ -5%
        # With settings.proactive_min_margin_pct=0 → match skipped
        scenario = _setup_scenario(db_session)
        scenario["cph"].avg_unit_price = 95.0  # Below cost
        db_session.flush()
        offer = Offer(
            requisition_id=scenario["requisition"].id,
            requirement_id=scenario["requirement"].id,
            material_card_id=scenario["card"].id,
            mpn=scenario["card"].display_mpn,
            unit_price=100.0,
            qty_available=100,
            vendor_name="TestVendor",
            source="manual",
        )
        db_session.add(offer)
        db_session.flush()
        with patch("app.services.proactive_matching.settings") as mock_settings:
            mock_settings.proactive_min_margin_pct = 0
            mock_settings.proactive_throttle_days = 90
            matches = find_matches_for_offer(offer.id, db_session)
        assert len(matches) == 0

    def test_negative_margin_with_negative_min_not_filtered(self, db_session):
        """margin=-5% with min_margin_pct=-10 → NOT filtered."""
        scenario = _setup_scenario(db_session)
        scenario["cph"].avg_unit_price = 95.0
        db_session.flush()
        offer = Offer(
            requisition_id=scenario["requisition"].id,
            requirement_id=scenario["requirement"].id,
            material_card_id=scenario["card"].id,
            mpn=scenario["card"].display_mpn,
            unit_price=100.0,
            qty_available=100,
            vendor_name="TestVendor",
            source="manual",
        )
        db_session.add(offer)
        db_session.flush()
        with patch("app.services.proactive_matching.settings") as mock_settings:
            mock_settings.proactive_min_margin_pct = -10
            mock_settings.proactive_throttle_days = 90
            matches = find_matches_for_offer(offer.id, db_session)
        assert len(matches) >= 1
```

- [ ] **Step 3: Add helper edge cases to test_proactive_helpers.py**

```python
class TestHelperEdgeCases:
    """Edge cases for DNO/throttle helpers."""

    def test_is_throttled_exactly_at_boundary(self, db_session):
        """Throttle entry created exactly throttle_days ago → should NOT be throttled (expired)."""
        company, site = _make_company_and_site(db_session)
        cutoff = datetime.now(timezone.utc) - timedelta(days=90)
        throttle = ProactiveThrottle(
            customer_site_id=site.id,
            mpn="TEST-MPN",
            last_offered_at=cutoff,
        )
        db_session.add(throttle)
        db_session.flush()
        with patch("app.services.proactive_helpers.settings") as mock_s:
            mock_s.proactive_throttle_days = 90
            result = is_throttled(db_session, "TEST-MPN", site.id)
        assert result is False

    def test_batch_dno_with_duplicate_company_ids(self, db_session):
        """Passing duplicate company_ids should still work (set dedup)."""
        company, site = _make_company_and_site(db_session)
        owner = db_session.query(User).first()
        dno = ProactiveDoNotOffer(
            company_id=company.id,
            mpn="DUP-MPN",
            created_by_id=owner.id,
        )
        db_session.add(dno)
        db_session.flush()
        result = build_batch_dno_set(db_session, "DUP-MPN", {company.id, company.id})
        assert company.id in result

    def test_batch_throttle_empty_returns_empty(self, db_session):
        result = build_batch_throttle_set(db_session, "ANY-MPN", set())
        assert result == set()
```

- [ ] **Step 4: Run proactive matching tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_proactive_matching.py tests/test_proactive_helpers.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_proactive_matching.py tests/test_proactive_helpers.py
git commit -m "test: add proactive matching scoring boundaries and helper edge cases"
```

---

### Task 4: Sourcing Connector Edge Cases

**Files:**
- Modify: `tests/test_connectors.py`
- Reference: `app/connectors/sources.py` (CircuitBreaker, BaseConnector)

**Context:** `CircuitBreaker(name, fail_max=5, reset_timeout=60)`. States: closed (normal) → open (after fail_max failures) → half_open (after reset_timeout). `record_success()` resets to closed. `record_failure()` increments count, opens at fail_max. `BaseConnector.search()` skips when breaker is open.

**Existing tests (DO NOT duplicate):** `test_initial_state_closed`, `test_stays_closed_below_fail_max`, `test_opens_at_fail_max`, `test_half_open_after_timeout`, `test_success_resets`, `test_get_breaker_caches`.

**Helper pattern:** `_mock_response(status_code, json_data, text)` builds fake httpx.Response. Clean breaker cache: `_breakers.pop("name", None)`.

- [ ] **Step 1: Add circuit breaker and connector edge cases to test_connectors.py**

```python
class TestCircuitBreakerEdgeCases:
    """New edge cases: half-open failure, independent breakers."""

    def test_half_open_failure_reopens(self):
        cb = CircuitBreaker("test-reopen", fail_max=2, reset_timeout=0.01)
        cb.record_failure()
        cb.record_failure()
        assert cb.current_state == "open"
        time.sleep(0.02)
        assert cb.current_state == "half_open"
        cb.record_failure()
        assert cb.current_state == "open"

    def test_independent_breakers(self):
        cb1 = CircuitBreaker("breaker-a", fail_max=2, reset_timeout=60)
        cb2 = CircuitBreaker("breaker-b", fail_max=2, reset_timeout=60)
        cb1.record_failure()
        cb1.record_failure()
        assert cb1.current_state == "open"
        assert cb2.current_state == "closed"

    def test_success_from_half_open_closes(self):
        cb = CircuitBreaker("test-close", fail_max=1, reset_timeout=0.01)
        cb.record_failure()
        assert cb.current_state == "open"
        time.sleep(0.02)
        assert cb.current_state == "half_open"
        cb.record_success()
        assert cb.current_state == "closed"


class TestConnectorMalformedResponses:
    """Test connectors handle malformed API responses gracefully."""

    @pytest.mark.asyncio
    async def test_empty_json_response(self):
        """Connector returns {} → should return empty results, no crash."""
        connector = self._make_connector()
        connector._do_search = AsyncMock(return_value=[])
        results = await connector.search("LM317T")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_empty_mpn_returns_empty(self):
        connector = self._make_connector()
        connector._do_search = AsyncMock(return_value=[])
        results = await connector.search("")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_whitespace_mpn_returns_empty(self):
        connector = self._make_connector()
        connector._do_search = AsyncMock(return_value=[])
        results = await connector.search("   ")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_unicode_mpn_no_crash(self):
        connector = self._make_connector()
        connector._do_search = AsyncMock(return_value=[])
        results = await connector.search("LM317T-\u00e9\u00e8")
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_search_special_chars_mpn(self):
        connector = self._make_connector()
        connector._do_search = AsyncMock(return_value=[])
        results = await connector.search("IC/123#A&B")
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_search_very_long_mpn(self):
        connector = self._make_connector()
        connector._do_search = AsyncMock(return_value=[])
        results = await connector.search("A" * 500)
        assert isinstance(results, list)

    def _make_connector(self):
        """Create a concrete BaseConnector subclass for testing."""
        from app.connectors.sources import BaseConnector, _breakers
        _breakers.pop("TestEdge", None)

        class TestEdgeConnector(BaseConnector):
            source_name = "TestEdge"
            async def _do_search(self, mpn):
                return []

        return TestEdgeConnector()
```

- [ ] **Step 2: Run connector tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_connectors.py::TestCircuitBreakerEdgeCases tests/test_connectors.py::TestConnectorMalformedResponses -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_connectors.py
git commit -m "test: add circuit breaker and connector malformed response edge cases"
```

---

### Task 5: Vendor/Customer Analysis Edge Cases

**Files:**
- Modify: `tests/test_vendor_analysis_service.py`
- Modify: `tests/test_customer_analysis_service.py`
- Reference: `app/services/vendor_analysis_service.py` (`_analyze_vendor_materials(card_id, db_session=None)`)
- Reference: `app/services/customer_analysis_service.py` (`analyze_customer_materials(company_id, db_session=None)`)

**Context:** Both services query DB for parts, send to Claude for tagging, apply max 5 tags. Both handle own session if `db_session=None`. Mock pattern: `@patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)`.

**Existing vendor tests (DO NOT duplicate):** `test_nonexistent_vendor_card_no_call`, `test_vendor_with_no_parts_no_call`, `test_claude_returns_none_card_unchanged`, `test_claude_returns_invalid_no_crash`, `test_max_5_tags_enforced`, `test_own_session_path`, `test_exception_with_own_session_rolls_back`.

**Existing customer tests (DO NOT duplicate):** `test_analyze_no_requisitions`, `test_analyze_with_site_but_no_parts`, `test_analyze_invalid_company`, `test_analyze_claude_returns_none`, `test_analyze_claude_returns_empty`, `test_analyze_truncates_to_five`, `test_analyze_own_session_exception`, `test_analyze_own_session_close`.

- [ ] **Step 1: Add vendor analysis edge cases**

```python
class TestVendorAnalysisEdgeCases:
    """Edge cases for vendor material analysis."""

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_vendor_single_transaction(self, mock_claude, db_session):
        """Vendor with exactly 1 MVH row → still calls Claude."""
        card = _make_vendor_card(db_session)
        mc = _make_material_card(db_session, mpn="SINGLE-001")
        _make_mvh(db_session, mc.id, card.normalized_name, manufacturer="TI")
        mock_claude.return_value = {"brands": ["TI"], "commodities": ["IC"]}
        await _analyze_vendor_materials(card.id, db_session)
        mock_claude.assert_called_once()
        db_session.refresh(card)
        assert card.brand_tags == ["TI"]

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_vendor_all_null_manufacturer(self, mock_claude, db_session):
        """MVH rows with no manufacturer → still sends MPNs to Claude."""
        card = _make_vendor_card(db_session)
        mc = _make_material_card(db_session, mpn="NULL-MFR-001", manufacturer=None)
        _make_mvh(db_session, mc.id, card.normalized_name, manufacturer=None)
        mock_claude.return_value = {"brands": [], "commodities": ["Passive"]}
        await _analyze_vendor_materials(card.id, db_session)
        mock_claude.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_vendor_exactly_5_tags_no_truncation(self, mock_claude, db_session):
        """Claude returns exactly 5 → no truncation needed."""
        card = _make_vendor_card(db_session)
        mc = _make_material_card(db_session)
        _make_mvh(db_session, mc.id, card.normalized_name)
        mock_claude.return_value = {"brands": ["A", "B", "C", "D", "E"], "commodities": ["X"]}
        await _analyze_vendor_materials(card.id, db_session)
        db_session.refresh(card)
        assert len(card.brand_tags) == 5

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_vendor_claude_returns_empty_lists(self, mock_claude, db_session):
        """Claude returns empty lists → tags set to empty, updated_at set."""
        card = _make_vendor_card(db_session)
        mc = _make_material_card(db_session)
        _make_mvh(db_session, mc.id, card.normalized_name)
        mock_claude.return_value = {"brands": [], "commodities": []}
        await _analyze_vendor_materials(card.id, db_session)
        db_session.refresh(card)
        assert card.brand_tags == []
        assert card.material_tags_updated_at is not None
```

- [ ] **Step 2: Add customer analysis edge cases**

```python
class TestCustomerAnalysisEdgeCases:
    """Edge cases for customer material analysis."""

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_customer_single_requirement(self, mock_claude, db_session, company_with_reqs):
        """Company with minimal data still gets analyzed."""
        mock_claude.return_value = {"brands": ["Intel"], "commodities": ["Memory"]}
        await analyze_customer_materials(company_with_reqs.id, db_session)
        mock_claude.assert_called_once()
        db_session.refresh(company_with_reqs)
        assert company_with_reqs.brand_tags == ["Intel"]

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_customer_claude_returns_string_no_crash(self, mock_claude, db_session, company_with_reqs):
        """Claude returns unexpected type → no crash."""
        mock_claude.return_value = "unexpected string"
        await analyze_customer_materials(company_with_reqs.id, db_session)
        # Should not raise

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_customer_exactly_5_tags_boundary(self, mock_claude, db_session, company_with_reqs):
        mock_claude.return_value = {"brands": ["A", "B", "C", "D", "E"], "commodities": ["X", "Y", "Z"]}
        await analyze_customer_materials(company_with_reqs.id, db_session)
        db_session.refresh(company_with_reqs)
        assert len(company_with_reqs.brand_tags) == 5

    @pytest.mark.asyncio
    @patch("app.utils.claude_client.claude_json", new_callable=AsyncMock)
    async def test_customer_company_id_zero(self, mock_claude, db_session):
        """company_id=0 → no company found, early return."""
        await analyze_customer_materials(0, db_session)
        mock_claude.assert_not_called()
```

- [ ] **Step 3: Run analysis tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_vendor_analysis_service.py tests/test_customer_analysis_service.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add tests/test_vendor_analysis_service.py tests/test_customer_analysis_service.py
git commit -m "test: add vendor and customer analysis service edge cases"
```

---

### Task 6: Faceted Search Edge Cases

**Files:**
- Modify: `tests/test_faceted_search_service.py`
- Reference: `app/services/faceted_search_service.py` (`search_materials_faceted`, `get_commodity_counts`, `get_manufacturer_options`, `get_subfilter_options`)

**Context:** No mocking needed — direct DB calls. Existing helpers: `_seed_dram_schema(db)` and `_make_dram_card(db, mpn, ddr, capacity, ecc)`. `search_materials_faceted(db, commodity, q, sub_filters, manufacturers, limit, offset)` returns `(list[MaterialCard], int)`.

**Existing tests (DO NOT duplicate):** `test_get_commodity_counts`, `test_search_materials_faceted_by_commodity`, `test_search_materials_faceted_numeric_range`, `test_search_materials_faceted_pagination_offset`, `test_search_materials_faceted_text_search_by_mpn`.

- [ ] **Step 1: Add faceted search edge cases**

```python
class TestFacetedSearchEdgeCases:
    """Boundary and validation edge cases for faceted search."""

    def test_empty_commodity_returns_all(self, db_session):
        _seed_dram_schema(db_session)
        _make_dram_card(db_session, "MEM-001", "DDR4", 16)
        _make_dram_card(db_session, "MEM-002", "DDR5", 32)
        results, total = search_materials_faceted(db_session, commodity=None)
        assert total >= 2

    def test_nonexistent_commodity_returns_empty(self, db_session):
        _seed_dram_schema(db_session)
        _make_dram_card(db_session, "MEM-001", "DDR4", 16)
        results, total = search_materials_faceted(db_session, commodity="nonexistent_xyz")
        assert total == 0
        assert results == []

    def test_offset_beyond_total_returns_empty(self, db_session):
        _seed_dram_schema(db_session)
        _make_dram_card(db_session, "MEM-001", "DDR4", 16)
        results, total = search_materials_faceted(db_session, commodity="dram", offset=9999)
        assert results == []
        assert total == 1  # total still reflects full count

    def test_limit_zero_returns_empty_results(self, db_session):
        _seed_dram_schema(db_session)
        _make_dram_card(db_session, "MEM-001", "DDR4", 16)
        results, total = search_materials_faceted(db_session, commodity="dram", limit=0)
        assert results == []

    def test_special_chars_in_text_search(self, db_session):
        _seed_dram_schema(db_session)
        _make_dram_card(db_session, "MEM-001", "DDR4", 16)
        results, total = search_materials_faceted(db_session, q="'; DROP TABLE--")
        assert isinstance(results, list)  # no SQL injection crash

    def test_numeric_range_min_equals_max(self, db_session):
        _seed_dram_schema(db_session)
        _make_dram_card(db_session, "MEM-001", "DDR4", 16)
        _make_dram_card(db_session, "MEM-002", "DDR5", 32)
        results, total = search_materials_faceted(
            db_session, commodity="dram",
            sub_filters={"capacity_gb_min": 16, "capacity_gb_max": 16},
        )
        assert total == 1

    def test_unicode_manufacturer_filter(self, db_session):
        _seed_dram_schema(db_session)
        results, total = search_materials_faceted(
            db_session, manufacturers=["\u00e9\u00e8\u00fc"],
        )
        assert results == []

    def test_get_commodity_counts_empty_db(self, db_session):
        counts = get_commodity_counts(db_session)
        assert counts == {} or len(counts) == 0

    def test_get_manufacturer_options_empty_db(self, db_session):
        options = get_manufacturer_options(db_session)
        assert options == []

    def test_get_subfilter_options_nonexistent_commodity(self, db_session):
        options = get_subfilter_options(db_session, "nonexistent_xyz")
        assert options == []

    def test_search_with_empty_manufacturers_list(self, db_session):
        _seed_dram_schema(db_session)
        _make_dram_card(db_session, "MEM-001", "DDR4", 16)
        results, total = search_materials_faceted(db_session, manufacturers=[])
        assert total >= 1  # empty list should not filter

    def test_search_with_empty_sub_filters(self, db_session):
        _seed_dram_schema(db_session)
        _make_dram_card(db_session, "MEM-001", "DDR4", 16)
        results, total = search_materials_faceted(db_session, sub_filters={})
        assert total >= 1
```

- [ ] **Step 2: Run faceted search tests**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/test_faceted_search_service.py -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_faceted_search_service.py
git commit -m "test: add faceted search boundary and validation edge cases"
```

---

### Task 7: Final Validation

- [ ] **Step 1: Run full test suite**

Run: `TESTING=1 PYTHONPATH=/root/availai pytest tests/ -v`
Expected: All tests pass, no regressions. New test count should be ~101 higher than baseline (7,406).

- [ ] **Step 2: Verify no regressions**

If any existing tests fail, investigate and fix before committing.

- [ ] **Step 3: Final commit if any fixes needed**

```bash
git add -A && git commit -m "fix: resolve any test regressions from edge case additions"
```
