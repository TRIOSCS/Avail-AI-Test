"""tests/test_materials_nightly_coverage.py — Covers remaining branches in app/routers/materials.py.

Targets the three uncovered code paths (lines 447-449, 462-463, 476-481):
- IntegrityError race condition when creating a new VendorCard (lines 447-449)
- normalize_mpn_key returns empty string for symbol-only MPNs (lines 462-463)
- IntegrityError race condition when creating a new MaterialCard (lines 476-481)

Called by: pytest
Depends on: conftest.py fixtures (client, db_session), app models
"""

import os

os.environ["TESTING"] = "1"


from sqlalchemy.exc import IntegrityError

from app.models import MaterialCard, VendorCard


class TestImportStockNullMpnKey:
    """Lines 462-463: normalize_mpn_key returns empty string for symbol-only MPNs.

    '---' passes normalize_stock_row (length >= 3) but normalize_mpn_key('---')
    returns '' which is falsy, so those rows are skipped (lines 462-463).

    Direct handler calls are used so coverage registers in the main thread.
    """

    def _invoke(self, db_session, vendor_name: str, csv_content: bytes, monkeypatch):
        """Run the handler directly and return the response dict."""
        import asyncio

        from app.models import User
        from app.routers.materials import import_stock_list_standalone

        monkeypatch.setattr("app.routers.materials.get_credential_cached", lambda *a, **kw: None)
        monkeypatch.setattr("app.routers.materials.safe_background_task", lambda *a, **kw: None)
        monkeypatch.setattr("app.routers.materials.record_price_snapshot", lambda **kw: None)

        async def _run():
            class _File:
                filename = "stock.csv"

                async def read(inner_self):  # noqa: N805
                    return csv_content

            class _Form:
                def get(inner_self, key):  # noqa: N805
                    if key == "file":
                        return _File()
                    if key == "vendor_name":
                        return vendor_name
                    if key == "vendor_website":
                        return ""
                    return None

            class FakeRequest:
                async def form(inner_self):  # noqa: N805
                    return _Form()

                state = type("state", (), {"request_id": "test"})()

            user = User(
                email=f"sym_{vendor_name[:6]}@t.com",
                name="SBuyer",
                role="buyer",
                azure_id=f"sym_{vendor_name[:6]}",
            )
            return await import_stock_list_standalone(
                request=FakeRequest(),  # type: ignore[arg-type]
                user=user,
                db=db_session,
            )

        return asyncio.get_event_loop().run_until_complete(_run())

    def test_symbol_only_mpn_is_skipped(self, db_session, monkeypatch):
        """Rows whose MPN normalises to empty string are counted as skipped (lines 462-463)."""
        # '---' has length 3 (passes normalize_stock_row) but normalize_mpn_key returns ''
        content = b"mpn,qty,price\n---,100,0.50\n"
        result = self._invoke(db_session, "Symbol Vendor A", content, monkeypatch)
        assert result["skipped_rows"] >= 1
        assert result["imported_rows"] == 0

    def test_mixed_good_and_symbol_rows(self, db_session, monkeypatch):
        """Valid MPN rows import; symbol-only rows hit lines 462-463 and skip."""
        # LM317T → imported; '...' → normalize_mpn_key('...') = '' → skipped
        content = b"mpn,qty,price\nLM317T,100,0.50\n...,200,0.25\n"
        result = self._invoke(db_session, "Mixed MPN Vendor B", content, monkeypatch)
        assert result["imported_rows"] == 1
        assert result["skipped_rows"] == 1


class TestImportStockVendorCardIntegrityError:
    """Lines 447-449: IntegrityError when flushing a new VendorCard.

    Simulates a race condition: initial query finds nothing (so the router
    creates a new VendorCard), flush raises IntegrityError, rollback, then
    re-query finds the concurrently-inserted vendor.
    """

    def test_integrity_error_on_vendor_flush_falls_back_to_existing(self, db_session, monkeypatch):
        """IntegrityError on VendorCard flush triggers rollback + re-query (lines 447-449).

        Strategy: patch rollback to insert the vendor after rollback so the
        re-query at line 449 finds it.
        """
        import asyncio

        from app.models import User
        from app.routers.materials import import_stock_list_standalone
        from app.vendor_utils import normalize_vendor_name

        vendor_name = "Race Condition Vendor IE"
        norm = normalize_vendor_name(vendor_name)

        monkeypatch.setattr("app.routers.materials.get_credential_cached", lambda *a, **kw: None)
        monkeypatch.setattr("app.routers.materials.safe_background_task", lambda *a, **kw: None)
        monkeypatch.setattr("app.routers.materials.record_price_snapshot", lambda **kw: None)

        original_flush = db_session.flush
        original_rollback = db_session.rollback

        # On the first flush (VendorCard at line 445), raise IntegrityError.
        # On the subsequent rollback, insert the vendor so line 449 re-query finds it.
        raise_on_next = [True]
        rollback_calls = [0]

        def patched_flush(*args, **kwargs):
            if raise_on_next[0]:
                raise_on_next[0] = False
                raise IntegrityError("duplicate normalized_name", {}, None)
            return original_flush(*args, **kwargs)

        def patched_rollback():
            rollback_calls[0] += 1
            original_rollback()
            if rollback_calls[0] == 1:
                # Simulate concurrent insert after rollback so re-query finds the vendor
                concurrent_vc = VendorCard(
                    normalized_name=norm,
                    display_name=vendor_name,
                    sighting_count=0,
                    emails=[],
                    phones=[],
                )
                db_session.add(concurrent_vc)
                db_session.commit()

        monkeypatch.setattr(db_session, "flush", patched_flush)
        monkeypatch.setattr(db_session, "rollback", patched_rollback)

        csv_content = b"mpn,qty,price\nLM317T,100,0.50\n"

        async def _run():
            class _File:
                filename = "stock.csv"

                async def read(self):
                    return csv_content

            class _Form:
                def get(self, key):
                    if key == "file":
                        return _File()
                    if key == "vendor_name":
                        return vendor_name
                    if key == "vendor_website":
                        return ""
                    return None

            class FakeRequest:
                async def form(self):
                    return _Form()

                state = type("state", (), {"request_id": "test"})()

            user = User(email="buyer@trioscs.com", name="Buyer", role="buyer", azure_id="xyz")
            return await import_stock_list_standalone(
                request=FakeRequest(),  # type: ignore[arg-type]
                user=user,
                db=db_session,
            )

        result = asyncio.get_event_loop().run_until_complete(_run())
        # After IntegrityError + rollback + re-query finds the vendor → import proceeds
        assert "imported_rows" in result


class TestImportStockMaterialCardIntegrityError:
    """Lines 476-481: IntegrityError when flushing a new MaterialCard.

    Uses a mock session to precisely control what happens on each flush call
    so the router hits the try/except IntegrityError around db.flush() for
    new MaterialCard creation.
    """

    def _make_fake_request(self, vendor_name: str, csv_content: bytes):
        """Build a minimal fake Request that returns a form with file + vendor_name."""

        class _File:
            filename = "stock.csv"

            async def read(inner_self):  # noqa: N805
                return csv_content

        class _Form:
            def get(inner_self, key):  # noqa: N805
                if key == "file":
                    return _File()
                if key == "vendor_name":
                    return vendor_name
                if key == "vendor_website":
                    return ""
                return None

        class FakeRequest:
            async def form(inner_self):  # noqa: N805
                return _Form()

            state = type("state", (), {"request_id": "test"})()

        return FakeRequest()

    def test_integrity_error_on_material_card_flush_skips_row_when_not_found(self, db_session, monkeypatch):
        """IntegrityError on MaterialCard flush causes that row to be skipped.

        Covers lines 476-481: after IntegrityError + rollback, re-query returns
        None so the row is skipped (imported_rows stays 0).
        """
        import asyncio

        from app.models import User
        from app.routers.materials import import_stock_list_standalone

        vendor_name = "IE MaterialCard Skip Vendor"
        csv_content = b"mpn,qty,price\nUNIQUE_IE_SKIP_XYZ,100,1.00\n"

        monkeypatch.setattr("app.routers.materials.get_credential_cached", lambda *a, **kw: None)
        monkeypatch.setattr("app.routers.materials.safe_background_task", lambda *a, **kw: None)
        monkeypatch.setattr("app.routers.materials.record_price_snapshot", lambda **kw: None)

        original_flush = db_session.flush
        # MaterialCard flush is the 2nd call (after VendorCard flush at line 445).
        # After the IntegrityError at line 476, rollback is called, and the
        # re-query at line 478 must return None (simulated by the card not
        # existing in DB + rollback clearing the new card object).
        # We need flush to raise on the MaterialCard add but NOT on VendorCard.
        # Strategy: first flush (VendorCard) succeeds; second flush (line 475
        # for MaterialCard) raises; all subsequent succeed.
        flush_count = [0]

        def controlled_flush(*args, **kwargs):
            flush_count[0] += 1
            if flush_count[0] == 2:
                raise IntegrityError("duplicate key normalized_mpn", {}, None)
            return original_flush(*args, **kwargs)

        monkeypatch.setattr(db_session, "flush", controlled_flush)

        async def _run():
            user = User(email="buyer_skip@trioscs.com", name="BuyerSkip", role="buyer", azure_id="skip1")
            return await import_stock_list_standalone(
                request=self._make_fake_request(vendor_name, csv_content),  # type: ignore[arg-type]
                user=user,
                db=db_session,
            )

        result = asyncio.get_event_loop().run_until_complete(_run())
        # Re-query returns None after rollback → row skipped
        assert result["skipped_rows"] >= 1
        assert result["imported_rows"] == 0

    def test_integrity_error_on_material_card_flush_uses_existing_card(self, db_session, monkeypatch):
        """IntegrityError on MaterialCard flush recovers when re-query finds existing.

        Covers lines 476-478: IntegrityError → rollback → re-query finds the card
        (inserted by a concurrent request) → row is imported.
        """
        import asyncio

        from app.models import User
        from app.routers.materials import import_stock_list_standalone
        from app.utils.normalization import normalize_mpn_key

        vendor_name = "IE MaterialCard Found Vendor"
        mpn = "LM741CN_FOUND"
        norm = normalize_mpn_key(mpn)
        csv_content = f"mpn,qty,price\n{mpn},50,0.75\n".encode()

        monkeypatch.setattr("app.routers.materials.get_credential_cached", lambda *a, **kw: None)
        monkeypatch.setattr("app.routers.materials.safe_background_task", lambda *a, **kw: None)
        monkeypatch.setattr("app.routers.materials.record_price_snapshot", lambda **kw: None)

        # The card does NOT exist initially. After IntegrityError + rollback,
        # we simulate a concurrent insert by inserting the card in the rollback
        # side-effect so the re-query at line 478 finds it.
        original_rollback = db_session.rollback
        rollback_calls = [0]

        def side_effect_rollback():
            rollback_calls[0] += 1
            original_rollback()
            if rollback_calls[0] == 1:
                # Simulate concurrent insert: insert the card directly after rollback
                concurrent_mc = MaterialCard(normalized_mpn=norm, display_mpn=mpn, manufacturer="")
                db_session.add(concurrent_mc)
                db_session.commit()

        monkeypatch.setattr(db_session, "rollback", side_effect_rollback)

        original_flush = db_session.flush
        flush_count = [0]

        def controlled_flush(*args, **kwargs):
            flush_count[0] += 1
            if flush_count[0] == 2:
                raise IntegrityError("duplicate key normalized_mpn", {}, None)
            return original_flush(*args, **kwargs)

        monkeypatch.setattr(db_session, "flush", controlled_flush)

        async def _run():
            user = User(email="buyer_found@trioscs.com", name="BuyerFound", role="buyer", azure_id="found1")
            return await import_stock_list_standalone(
                request=self._make_fake_request(vendor_name, csv_content),  # type: ignore[arg-type]
                user=user,
                db=db_session,
            )

        result = asyncio.get_event_loop().run_until_complete(_run())
        # Re-query after rollback finds the concurrently inserted card → row imported
        assert "imported_rows" in result
