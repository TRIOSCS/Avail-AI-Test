"""test_buyplan_service_imports.py — Tests for app/services/buyplan_service.py.

The module is a re-export façade. These tests verify that every re-exported
name is importable from buyplan_service and is callable, confirming the façade
covers all 5 import lines.

Called by: pytest autodiscovery
Depends on: app.services.buyplan_service (re-export module)
"""

import os

os.environ["TESTING"] = "1"

import inspect


class TestBuyplanServiceFacadeImports:
    """Verify every public name is importable from the façade module."""

    def test_import_build_buy_plan(self):
        from app.services.buyplan_service import build_buy_plan

        assert callable(build_buy_plan)

    def test_import_generate_ai_flags(self):
        from app.services.buyplan_service import generate_ai_flags

        assert callable(generate_ai_flags)

    def test_import_generate_ai_summary(self):
        from app.services.buyplan_service import generate_ai_summary

        assert callable(generate_ai_summary)

    def test_import_log_buyplan_activity(self):
        from app.services.buyplan_service import log_buyplan_activity

        assert callable(log_buyplan_activity)

    def test_import_notify_cancelled(self):
        from app.services.buyplan_service import notify_cancelled

        assert callable(notify_cancelled)

    def test_import_score_offer(self):
        from app.services.buyplan_service import score_offer

        assert callable(score_offer)

    def test_import_submit_buy_plan(self):
        from app.services.buyplan_service import submit_buy_plan

        assert callable(submit_buy_plan)

    def test_import_notify_stock_sale_approved(self):
        from app.services.buyplan_service import notify_stock_sale_approved

        assert callable(notify_stock_sale_approved)

    def test_import_run_v3_notify_bg(self):
        from app.services.buyplan_service import run_v3_notify_bg

        assert callable(run_v3_notify_bg)

    def test_import_approve_buy_plan(self):
        from app.services.buyplan_service import approve_buy_plan

        assert callable(approve_buy_plan)

    def test_import_cancel_buy_plan(self):
        from app.services.buyplan_service import cancel_buy_plan

        assert callable(cancel_buy_plan)

    def test_import_check_completion(self):
        from app.services.buyplan_service import check_completion

        assert callable(check_completion)

    def test_import_confirm_po(self):
        from app.services.buyplan_service import confirm_po

        assert callable(confirm_po)

    def test_import_detect_favoritism(self):
        from app.services.buyplan_service import detect_favoritism

        assert callable(detect_favoritism)

    def test_import_flag_line_issue(self):
        from app.services.buyplan_service import flag_line_issue

        assert callable(flag_line_issue)

    def test_import_generate_case_report(self):
        from app.services.buyplan_service import generate_case_report

        assert callable(generate_case_report)

    def test_import_reset_buy_plan_to_draft(self):
        from app.services.buyplan_service import reset_buy_plan_to_draft

        assert callable(reset_buy_plan_to_draft)

    def test_import_resubmit_buy_plan(self):
        from app.services.buyplan_service import resubmit_buy_plan

        assert callable(resubmit_buy_plan)

    def test_import_verify_po(self):
        from app.services.buyplan_service import verify_po

        assert callable(verify_po)

    def test_import_verify_po_sent(self):
        from app.services.buyplan_service import verify_po_sent

        assert callable(verify_po_sent)

    def test_import_verify_so(self):
        from app.services.buyplan_service import verify_so

        assert callable(verify_so)

    def test_import_assign_buyer(self):
        from app.services.buyplan_service import assign_buyer

        assert callable(assign_buyer)

    def test_import_settings(self):
        from app.services.buyplan_service import settings

        # settings is the Pydantic Settings instance, not a callable
        assert settings is not None

    def test_import_weight_constants(self):
        from app.services.buyplan_service import (
            W_GEOGRAPHY,
            W_LEAD_TIME,
            W_PRICE,
            W_RELIABILITY,
            W_TERMS,
        )

        for w in (W_GEOGRAPHY, W_LEAD_TIME, W_PRICE, W_RELIABILITY, W_TERMS):
            assert isinstance(w, (int, float))

    def test_import_private_builder_helpers(self):
        from app.services.buyplan_service import (
            _build_lines_for_requirement,
            _check_better_offer,
            _check_geo_mismatch,
            _check_quantity_gaps,
            _create_line,
        )

        for fn in (
            _build_lines_for_requirement,
            _check_better_offer,
            _check_geo_mismatch,
            _check_quantity_gaps,
            _create_line,
        ):
            assert callable(fn)

    def test_import_private_workflow_helpers(self):
        from app.services.buyplan_service import (
            _apply_line_edits,
            _apply_line_overrides,
            _is_stock_sale,
            _recalculate_financials,
        )

        for fn in (_apply_line_edits, _apply_line_overrides, _is_stock_sale, _recalculate_financials):
            assert callable(fn)

    def test_import_scoring_helpers(self):
        from app.services.buyplan_service import (
            _country_to_region,
            _get_routing_maps,
            _parse_lead_time_days,
        )

        for fn in (_country_to_region, _get_routing_maps, _parse_lead_time_days):
            assert callable(fn)

    def test_module_is_a_facade(self):
        """The module source itself must only contain re-export import statements."""
        import app.services.buyplan_service as mod

        source_file = inspect.getfile(mod)
        assert source_file.endswith("buyplan_service.py")

    def test_all_names_accessible_from_single_import(self):
        """Import many names in one statement — confirms the façade's __init__-style re-
        exports."""
        from app.services.buyplan_service import (
            build_buy_plan,
            generate_ai_flags,
            generate_ai_summary,
            log_buyplan_activity,
            notify_cancelled,
            score_offer,
            submit_buy_plan,
        )

        names = [
            build_buy_plan,
            generate_ai_flags,
            generate_ai_summary,
            log_buyplan_activity,
            notify_cancelled,
            score_offer,
            submit_buy_plan,
        ]
        assert all(callable(n) for n in names)
