"""Tests for app/management/enrichment_coverage_report.py — coverage telemetry.

Seeds a small material-card/facet/fru_links fixture set and asserts the collected
metrics, the run-over-run delta math, the --json output shape, and the log-file
behavior. Runs against the shared in-memory SQLite engine, so it exercises the
sqlite json_each branch of the spec-source counter (the PG jsonb_each branch is
the same shape; verify it against live PG when changing the SQL).

Called by: pytest
Depends on: app/management/enrichment_coverage_report.py, conftest db_session
"""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.management.enrichment_coverage_report import (
    append_metrics,
    collect_metrics,
    compute_deltas,
    format_report,
    main,
    read_last_metrics,
)
from app.models import MaterialCard, MaterialSpecFacet
from app.models.fru_link import FruLink


def _card(db, mpn, **kwargs):
    card = MaterialCard(normalized_mpn=mpn, display_mpn=mpn, **kwargs)
    db.add(card)
    db.flush()
    return card


@pytest.fixture()
def seeded(db_session):
    """Fixture set covering every metric branch.

    Active cards: A (dram, desc, 3 spec entries), B (ssd, 1 entry), C (' Other ',
    blank desc), D (no category, desc), F (dram, legacy non-dict spec entry).
    E is soft-deleted and must be excluded everywhere (cards, facets, sources).
    """
    a = _card(
        db_session,
        "MPN-A",
        category="dram",
        description="16GB DDR4 RDIMM",
        enrichment_status="verified",
        specs_structured={
            "ddr_type": {"value": "DDR4", "source": "mpn_decode"},
            "capacity": {"value": 16, "source": "mpn_decode"},
            "ecc": {"value": True, "source": "desc_parse"},
        },
    )
    b = _card(
        db_session,
        "MPN-B",
        category="ssd",
        enrichment_status="web_sourced",
        specs_structured={"capacity": {"value": 960, "source": "spec_extraction"}},
    )
    _card(db_session, "MPN-C", category=" Other ", description="", enrichment_status="unenriched")
    _card(db_session, "MPN-D", description="mystery part", enrichment_status="unenriched")
    e = _card(
        db_session,
        "MPN-E",
        category="dram",
        enrichment_status="verified",
        deleted_at=datetime.now(timezone.utc),
        specs_structured={"ddr_type": {"value": "DDR4", "source": "mpn_decode"}},
    )
    _card(
        db_session,
        "MPN-F",
        category="dram",
        enrichment_status="ai_inferred",
        specs_structured={"ddr_type": "DDR4"},  # legacy non-dict entry → source "(none)"
    )

    db_session.add_all(
        [
            MaterialSpecFacet(material_card_id=a.id, category="dram", spec_key="ddr_type", value_text="DDR4"),
            MaterialSpecFacet(
                material_card_id=a.id, category="dram", spec_key="capacity", value_numeric=16, value_unit="GB"
            ),
            MaterialSpecFacet(
                material_card_id=b.id, category="ssd", spec_key="capacity", value_numeric=960, value_unit="GB"
            ),
            # Facet row on the soft-deleted card — must be excluded from facet metrics.
            MaterialSpecFacet(material_card_id=e.id, category="dram", spec_key="ddr_type", value_text="DDR4"),
        ]
    )
    db_session.add_all(
        [
            FruLink(
                fru_raw="00NV340",
                fru_norm="00nv340",
                related_raw="ST1000NX0313",
                related_norm="st1000nx0313",
                rel_kind="mfg_model",
                source_sheet="Main",
            ),
            FruLink(
                fru_raw="00NV340",
                fru_norm="00nv340",
                related_raw="00NV341",
                related_norm="00nv341",
                rel_kind="ibm_11s",
                source_sheet="Main",
            ),
            FruLink(
                fru_raw="01AB123",
                fru_norm="01ab123",
                related_raw="X1",
                related_norm="x1",
                rel_kind="tray",
                source_sheet="Main",
            ),
        ]
    )
    db_session.commit()
    return db_session


class TestCollectMetrics:
    def test_card_metrics(self, seeded):
        m = collect_metrics(seeded)
        assert m["cards"]["total"] == 5  # soft-deleted E excluded
        assert m["cards"]["with_category"] == 4
        assert m["cards"]["with_category_pct"] == 80.0
        assert m["cards"]["category_other"] == 1  # ' Other ' normalized
        assert m["cards"]["with_description"] == 2  # blank-string desc excluded
        assert m["cards"]["top_categories"] == [
            {"category": "dram", "count": 2},
            {"category": "other", "count": 1},
            {"category": "ssd", "count": 1},
        ]

    def test_facet_metrics(self, seeded):
        m = collect_metrics(seeded)
        assert m["facets"]["cards_with_facets"] == 2
        assert m["facets"]["cards_with_facets_pct"] == 40.0
        assert m["facets"]["rows_total"] == 3  # deleted card's facet row excluded
        assert m["facets"]["by_commodity"] == [
            {"commodity": "dram", "rows": 2, "spec_keys": 2},
            {"commodity": "ssd", "rows": 1, "spec_keys": 1},
        ]

    def test_spec_sources(self, seeded):
        m = collect_metrics(seeded)
        assert m["spec_sources"] == {
            "mpn_decode": 2,
            "(none)": 1,  # legacy non-dict entry on card F
            "desc_parse": 1,
            "spec_extraction": 1,
        }
        # Ordered by count desc, then name — dict preserves insertion order.
        assert list(m["spec_sources"]) == ["mpn_decode", "(none)", "desc_parse", "spec_extraction"]
        assert m["spec_entries_total"] == 5

    def test_status_and_fru(self, seeded):
        m = collect_metrics(seeded)
        assert m["enrichment_status"] == {
            "unenriched": 2,
            "ai_inferred": 1,
            "verified": 1,
            "web_sourced": 1,
        }
        assert m["fru_links"] == {"rows": 3, "distinct_frus": 2}

    def test_empty_database(self, db_session):
        m = collect_metrics(db_session)
        assert m["cards"]["total"] == 0
        assert m["cards"]["with_category_pct"] == 0.0  # no ZeroDivisionError
        assert m["cards"]["top_categories"] == []
        assert m["facets"] == {
            "cards_with_facets": 0,
            "cards_with_facets_pct": 0.0,
            "rows_total": 0,
            "by_commodity": [],
        }
        assert m["spec_sources"] == {}
        assert m["spec_entries_total"] == 0
        assert m["enrichment_status"] == {}
        assert m["fru_links"] == {"rows": 0, "distinct_frus": 0}

    def test_fru_table_absent(self, seeded):
        fake_inspector = MagicMock()
        fake_inspector.has_table.return_value = False
        with patch("app.management.enrichment_coverage_report.sa_inspect", return_value=fake_inspector):
            m = collect_metrics(seeded)
        assert m["fru_links"] is None
        assert "FRU links: (table absent)" in format_report(m)


class TestDeltas:
    def test_delta_math(self, seeded, tmp_path):
        log = tmp_path / "coverage.jsonl"
        first = collect_metrics(seeded)
        append_metrics(log, first)

        card = _card(seeded, "MPN-NEW", category="hdd", enrichment_status="verified", description="2TB SATA")
        seeded.add(MaterialSpecFacet(material_card_id=card.id, category="hdd", spec_key="capacity", value_numeric=2))
        seeded.commit()

        second = collect_metrics(seeded)
        prev = read_last_metrics(log)
        assert prev is not None
        deltas = compute_deltas(prev, second)
        assert deltas == {
            "cards.total": 1,
            "cards.with_category": 1,
            "cards.with_description": 1,
            "facets.cards_with_facets": 1,
            "facets.rows_total": 1,
            "spec_entries_total": 0,
            "fru_links.rows": 0,
        }

    def test_delta_skips_keys_missing_on_either_side(self, seeded):
        m = collect_metrics(seeded)
        prev = json.loads(json.dumps(m))
        prev["fru_links"] = None  # e.g. previous run on a DB without the table
        deltas = compute_deltas(prev, m)
        assert "fru_links.rows" not in deltas
        assert deltas["cards.total"] == 0

    def test_read_last_metrics_missing_and_malformed(self, tmp_path):
        assert read_last_metrics(tmp_path / "absent.jsonl") is None
        bad = tmp_path / "bad.jsonl"
        bad.write_text('{"ts": "x", "metrics": {"cards": {"total": 1}}}\nnot json\n')
        assert read_last_metrics(bad) is None  # malformed last line → no deltas


class TestMain:
    def test_json_output_shape(self, seeded, capsys):
        with patch("app.database.SessionLocal", MagicMock(return_value=seeded)):
            returned = main(json_output=True)
        printed = json.loads(capsys.readouterr().out)
        assert printed == json.loads(json.dumps(returned))  # stdout is the returned dict
        assert set(printed) == {
            "generated_at",
            "cards",
            "facets",
            "spec_sources",
            "spec_entries_total",
            "enrichment_status",
            "fru_links",
        }
        assert set(printed["cards"]) == {
            "total",
            "with_category",
            "with_category_pct",
            "category_other",
            "with_description",
            "top_categories",
        }
        assert set(printed["facets"]) == {
            "cards_with_facets",
            "cards_with_facets_pct",
            "rows_total",
            "by_commodity",
        }
        assert "deltas" not in printed  # no --log-file → no deltas key

    def test_human_report_block(self, seeded, capsys):
        with patch("app.database.SessionLocal", MagicMock(return_value=seeded)):
            main()
        out = capsys.readouterr().out
        assert "Enrichment coverage —" in out
        assert "Cards: 5 total · category 4 (80.0%) · 'other' 1 · description 2" in out
        assert "Top categories: dram 2 · other 1 · ssd 1" in out
        assert "Facets: 2 cards covered (40.0%) · 3 rows" in out
        assert "By commodity: dram 2 rows/2 keys · ssd 1 rows/1 keys" in out
        assert "Spec sources (5 entries): mpn_decode 2 · (none) 1 · desc_parse 1 · spec_extraction 1" in out
        assert "Status: unenriched 2 · ai_inferred 1 · verified 1 · web_sourced 1" in out
        assert "FRU links: 3 rows · 2 distinct FRUs" in out
        assert "Δ" not in out  # no log file → no delta line

    def test_log_file_first_then_delta_run(self, seeded, tmp_path, capsys):
        log = tmp_path / "coverage.jsonl"
        with patch("app.database.SessionLocal", MagicMock(return_value=seeded)):
            main(log_file=str(log))
            first_out = capsys.readouterr().out
            assert "Δ" not in first_out  # first run has no previous line

            second = main(json_output=True, log_file=str(log))
            capsys.readouterr()

        assert second["deltas"] == {
            "cards.total": 0,
            "cards.with_category": 0,
            "cards.with_description": 0,
            "facets.cards_with_facets": 0,
            "facets.rows_total": 0,
            "spec_entries_total": 0,
            "fru_links.rows": 0,
        }
        lines = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
        assert len(lines) == 2  # one JSONL line appended per run
        assert all(set(entry) == {"ts", "metrics"} for entry in lines)
        assert lines[1]["ts"] == lines[1]["metrics"]["generated_at"]

    def test_delta_line_in_human_output(self, seeded, tmp_path, capsys):
        log = tmp_path / "coverage.jsonl"
        with patch("app.database.SessionLocal", MagicMock(return_value=seeded)):
            main(log_file=str(log))
            capsys.readouterr()
            _card(seeded, "MPN-NEW2", category="hdd", enrichment_status="verified")
            seeded.commit()
            main(log_file=str(log))
        out = capsys.readouterr().out
        assert "Δ since last run (vs " in out
        assert "cards +1" in out
        assert "with-category +1" in out
        assert "facet-rows +0" in out
