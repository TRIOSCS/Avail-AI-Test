"""Tests for app/management/enrichment_coverage_report.py — coverage telemetry.

Seeds a small material-card/facet/fru_links fixture set and asserts the collected
metrics, the run-over-run delta math, the --json output shape, and the log-file
behavior. Runs against the shared in-memory SQLite engine, so it exercises the
sqlite json_each branch of the spec-source counter; the PG jsonb_each branch has
an opt-in parity test (set PG_TEST_DSN to a Postgres DSN), otherwise verify it
against live PG when changing the SQL.

Called by: pytest
Depends on: app/management/enrichment_coverage_report.py, conftest db_session
"""

import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session as PlainSession

from app.management.enrichment_coverage_report import (
    _pin_snapshot,
    _spec_source_counts,
    append_metrics,
    collect_metrics,
    compute_deltas,
    format_report,
    main,
    read_last_metrics,
)
from app.models import MaterialCard, MaterialSpecFacet
from app.models.fru_link import FruLink
from app.services.commodity_registry import CANONICAL_COMMODITY_KEYS
from tests.conftest import force_card_category, requires_postgres


def _card(db, mpn, **kwargs):
    # A deliberately non-canonical category (e.g. " Other ") is legacy residue the
    # coverage report must still bucket; the @validates guard rejects it on assignment,
    # so seed it post-flush via force_card_category exactly as a pre-guard writer left it.
    category = kwargs.pop("category", None)
    residue = category is not None and category not in CANONICAL_COMMODITY_KEYS
    card = MaterialCard(normalized_mpn=mpn, display_mpn=mpn, **kwargs)
    if not residue and category is not None:
        card.category = category
    db.add(card)
    db.flush()
    if residue:
        force_card_category(db, card, category)
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

    def test_provenance_defaults_to_none_buckets(self, seeded):
        # Fixture has no provenance anywhere: every categorized card / facet row lands
        # in "(none)", and nothing is unregistered.
        m = collect_metrics(seeded)
        assert m["category_sources"] == {"(none)": 4}
        assert m["facet_sources"] == {"(none)": 3}  # deleted card's facet row excluded
        assert m["unregistered_sources"] == []

    def test_provenance_sources_and_unregistered_callout(self, seeded):
        a = seeded.query(MaterialCard).filter_by(normalized_mpn="MPN-A").one()
        b = seeded.query(MaterialCard).filter_by(normalized_mpn="MPN-B").one()
        a.category_source, a.category_tier = "mpn_decode", 85
        b.category_source, b.category_tier = "typo_writer", 0  # NOT in SOURCE_TIER
        facet = (
            seeded.query(MaterialSpecFacet)
            .filter(MaterialSpecFacet.material_card_id == a.id, MaterialSpecFacet.spec_key == "ddr_type")
            .one()
        )
        facet.source = "mpn_decode"
        seeded.commit()

        m = collect_metrics(seeded)
        assert m["category_sources"] == {"(none)": 2, "mpn_decode": 1, "typo_writer": 1}
        assert m["facet_sources"] == {"(none)": 2, "mpn_decode": 1}
        # Observed in category provenance but absent from SOURCE_TIER → tier-0 callout.
        assert m["unregistered_sources"] == ["typo_writer"]

        report = format_report(m)
        assert "Category sources: " in report
        assert "Facet sources: " in report
        assert "WARNING unregistered sources (tier 0 — every write loses conflicts): typo_writer" in report

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
        assert m["category_sources"] == {}
        assert m["facet_sources"] == {}
        assert m["unregistered_sources"] == []
        assert m["enrichment_status"] == {}
        assert m["fru_links"] == {"rows": 0, "distinct_frus": 0}

    def test_fru_table_absent(self, seeded):
        fake_inspector = MagicMock()
        fake_inspector.has_table.return_value = False
        with patch("app.management.enrichment_coverage_report.sa_inspect", return_value=fake_inspector):
            m = collect_metrics(seeded)
        assert m["fru_links"] is None
        assert "FRU links: (table absent)" in format_report(m)


class TestSpecSourceBranches:
    """The three dialect branches of _spec_source_counts must bucket identically."""

    def test_python_fallback_matches_sqlite_branch(self, db_session):
        _card(
            db_session,
            "MPN-EDGE",
            specs_structured={
                "a": {"value": 1, "source": "mpn_decode"},
                "b": {"value": 2, "source": ""},  # present-but-empty → its own bucket
                "c": {"value": 3},  # missing source → "(none)"
                "d": "legacy",  # non-dict entry → "(none)"
            },
        )
        db_session.commit()
        expected = {"(none)": 2, "": 1, "mpn_decode": 1}
        assert _spec_source_counts(db_session) == expected  # sqlite json_each branch
        with patch.object(db_session.get_bind().dialect, "name", "duckdb"):
            assert _spec_source_counts(db_session) == expected  # streamed Python fallback
        # An empty-string source stays visible (quoted) in the human report.
        m = collect_metrics(db_session)
        assert "'' 1" in format_report(m)

    @requires_postgres
    def test_pg_jsonb_each_branch_matches_sqlite(self):
        """Opt-in parity check for _PG_SOURCES_SQL (CI runs SQLite only)."""
        engine = create_engine(os.environ["PG_TEST_DSN"])
        try:
            with engine.connect() as conn:
                conn.execute(text("CREATE TEMP TABLE material_cards (specs_structured jsonb, deleted_at timestamptz)"))
                conn.execute(
                    text(
                        "INSERT INTO material_cards (specs_structured, deleted_at) VALUES "
                        "(CAST(:s AS jsonb), CAST(:d AS timestamptz))"
                    ),
                    [
                        {  # object entries with sources, incl. missing and empty-string
                            "s": json.dumps(
                                {
                                    "ddr_type": {"value": "DDR4", "source": "mpn_decode"},
                                    "capacity": {"value": 16, "source": "mpn_decode"},
                                    "ecc": {"value": True, "source": "desc_parse"},
                                    "rank": {"value": 2, "source": ""},
                                    "voltage": {"value": 1.2},
                                }
                            ),
                            "d": None,
                        },
                        {"s": json.dumps({"legacy": "DDR4"}), "d": None},  # scalar entry → "(none)"
                        {"s": json.dumps("not an object"), "d": None},  # non-object payload → skipped
                        {"s": None, "d": None},  # NULL payload → skipped
                        {  # soft-deleted → excluded entirely
                            "s": json.dumps({"x": {"value": 1, "source": "mpn_decode"}}),
                            "d": "2026-01-01T00:00:00Z",
                        },
                    ],
                )
                counts = _spec_source_counts(PlainSession(bind=conn))
        finally:
            engine.dispose()
        # Same buckets the SQLite branch produces for this data: missing/null source
        # and scalar entries → "(none)"; empty-string source is its own bucket.
        assert counts == {"mpn_decode": 2, "(none)": 2, "desc_parse": 1, "": 1}


class TestSnapshotPinning:
    def test_pin_snapshot_sets_repeatable_read_on_fresh_pg_session(self):
        db = MagicMock()
        db.get_bind.return_value.dialect.name = "postgresql"
        db.in_transaction.return_value = False
        _pin_snapshot(db)
        db.connection.assert_called_once_with(execution_options={"isolation_level": "REPEATABLE READ"})

    def test_pin_snapshot_noop_mid_transaction_and_on_other_dialects(self):
        mid_tx = MagicMock()
        mid_tx.get_bind.return_value.dialect.name = "postgresql"
        mid_tx.in_transaction.return_value = True
        _pin_snapshot(mid_tx)
        mid_tx.connection.assert_not_called()  # isolation can't change mid-transaction

        sqlite = MagicMock()
        sqlite.get_bind.return_value.dialect.name = "sqlite"
        sqlite.in_transaction.return_value = False
        _pin_snapshot(sqlite)
        sqlite.connection.assert_not_called()  # already snapshot-consistent per transaction


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
        # Corrupt trailing line (torn write) → scan back to the last well-formed line.
        assert read_last_metrics(bad) == {"cards": {"total": 1}}
        only_junk = tmp_path / "junk.jsonl"
        only_junk.write_text("not json\nstill not json\n")
        assert read_last_metrics(only_junk) is None  # no well-formed line at all

    def test_read_last_metrics_wrong_shape_warns_and_falls_back(self, tmp_path):
        log = tmp_path / "log.jsonl"
        log.write_text(
            '{"ts": "t1", "metrics": {"cards": {"total": 1}}}\n["not", "a", "dict"]\n{"ts": "t2", "metrics": "oops"}\n'
        )
        warnings: list[str] = []
        sink = logger.add(lambda msg: warnings.append(str(msg)), level="WARNING")
        try:
            assert read_last_metrics(log) == {"cards": {"total": 1}}
        finally:
            logger.remove(sink)
        # Both wrong-shape variants are surfaced, not silently skipped.
        assert len(warnings) == 2
        assert all("metrics" in w for w in warnings)

    def test_append_after_truncated_line_heals_history(self, seeded, tmp_path):
        log = tmp_path / "coverage.jsonl"
        metrics = collect_metrics(seeded)
        append_metrics(log, metrics)
        # Simulate a torn write: a partial line with no trailing newline.
        with log.open("a", encoding="utf-8") as fh:
            fh.write('{"ts": "t2", "metr')
        append_metrics(log, metrics)
        lines = log.read_text().splitlines()
        assert len(lines) == 3  # healed: new entry on its own line, never merged
        assert json.loads(lines[2])["metrics"] == json.loads(json.dumps(metrics))
        assert read_last_metrics(log) == json.loads(json.dumps(metrics))


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
            "category_sources",
            "facet_sources",
            "unregistered_sources",
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
