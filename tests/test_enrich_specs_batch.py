"""Tests for scripts/enrich_specs_batch.py — Spec extraction logic.

Tests prompt building, schema generation, and specs_summary formatting
without hitting real APIs.

Called by: pytest
Depends on: scripts/enrich_specs_batch.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.enrich_specs_batch import (
    COMMODITY_SPECS,
    _build_spec_prompt,
    _build_spec_schema,
    _specs_to_summary,
)


# ── COMMODITY_SPECS coverage ──────────────────────────────────────────


class TestCommoditySpecs:
    def test_has_at_least_15_commodities(self):
        assert len(COMMODITY_SPECS) >= 15

    def test_all_have_specs_list(self):
        for cat, schema in COMMODITY_SPECS.items():
            assert "specs" in schema, f"{cat} missing specs"
            assert len(schema["specs"]) >= 2, f"{cat} has too few specs"

    def test_dram_specs(self):
        specs = COMMODITY_SPECS["dram"]["specs"]
        keys = [s["key"] for s in specs]
        assert "ddr_type" in keys
        assert "capacity_gb" in keys
        assert "ecc" in keys

    def test_capacitors_specs(self):
        specs = COMMODITY_SPECS["capacitors"]["specs"]
        keys = [s["key"] for s in specs]
        assert "capacitance" in keys
        assert "voltage_rating" in keys
        assert "dielectric" in keys


# ── _build_spec_prompt tests ──────────────────────────────────────────


class TestBuildSpecPrompt:
    def test_includes_specs(self):
        cards = [{"display_mpn": "M393A2K43DB3-CWE", "manufacturer": "Samsung", "description": "16GB DDR4 RDIMM"}]
        prompt = _build_spec_prompt("dram", cards)
        assert "ddr_type" in prompt
        assert "capacity_gb" in prompt
        assert "DDR3, DDR4, DDR5" in prompt

    def test_includes_card_info(self):
        cards = [{"display_mpn": "GRM155R71C104KA88D", "manufacturer": "Murata", "description": "100nF 16V X7R"}]
        prompt = _build_spec_prompt("capacitors", cards)
        assert "GRM155R71C104KA88D" in prompt
        assert "Murata" in prompt


# ── _build_spec_schema tests ──────────────────────────────────────────


class TestBuildSpecSchema:
    def test_has_parts_array(self):
        schema = _build_spec_schema("dram")
        assert schema["properties"]["parts"]["type"] == "array"

    def test_includes_spec_keys(self):
        schema = _build_spec_schema("dram")
        item_props = schema["properties"]["parts"]["items"]["properties"]
        assert "ddr_type" in item_props
        assert "ddr_type_confidence" in item_props
        assert "capacity_gb" in item_props

    def test_includes_mpn(self):
        schema = _build_spec_schema("capacitors")
        item_required = schema["properties"]["parts"]["items"]["required"]
        assert "mpn" in item_required


# ── _specs_to_summary tests ──────────────────────────────────────────


class TestSpecsToSummary:
    def test_basic_summary(self):
        ai_part = {
            "ddr_type": "DDR4",
            "ddr_type_confidence": 0.95,
            "capacity_gb": 16,
            "capacity_gb_confidence": 0.90,
            "ecc": True,
            "ecc_confidence": 0.88,
        }
        summary = _specs_to_summary("dram", ai_part)
        assert "DDR Type: DDR4" in summary
        assert "Capacity (GB): 16" in summary
        assert "ECC: True" in summary

    def test_low_confidence_excluded(self):
        ai_part = {
            "ddr_type": "DDR4",
            "ddr_type_confidence": 0.95,
            "capacity_gb": 16,
            "capacity_gb_confidence": 0.50,  # Too low
        }
        summary = _specs_to_summary("dram", ai_part)
        assert "DDR Type: DDR4" in summary
        assert "Capacity" not in summary

    def test_null_values_excluded(self):
        ai_part = {
            "ddr_type": None,
            "ddr_type_confidence": 0.95,
        }
        summary = _specs_to_summary("dram", ai_part)
        assert summary is None or "DDR Type" not in (summary or "")

    def test_empty_when_all_low_confidence(self):
        ai_part = {
            "ddr_type": "DDR4",
            "ddr_type_confidence": 0.50,
            "capacity_gb": 16,
            "capacity_gb_confidence": 0.30,
        }
        summary = _specs_to_summary("dram", ai_part)
        assert summary is None

    def test_pipe_separated(self):
        ai_part = {
            "capacitance": "100nF",
            "capacitance_confidence": 0.95,
            "voltage_rating": 16,
            "voltage_rating_confidence": 0.90,
        }
        summary = _specs_to_summary("capacitors", ai_part)
        assert " | " in summary
