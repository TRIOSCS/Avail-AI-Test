"""Tests for the scripts/import_part_numbers.py CLI loader."""


def test_bare_loader_creates_cards_without_enriching(monkeypatch, db_session, tmp_path):
    """The CLI loader upserts bare cards only — it never imports or calls enrich_card,
    so a large operator load cannot fire uncapped connector/web/AI calls (the worker is
    the single paced enrichment authority).

    Counts reflect what would be created.
    """
    import scripts.import_part_numbers as imp

    # Structural guard: the inline-enrichment plumbing must be gone.
    assert not hasattr(imp, "enrich_card")
    assert not hasattr(imp, "_connectors_in_order")

    monkeypatch.setattr(imp, "SessionLocal", lambda: db_session)
    f = tmp_path / "s.csv"
    f.write_text("mpn\nOK1\nOK2\n")

    result = imp._run(str(f), commit=False)  # dry-run: rolled back, counts still computed
    assert result["total"] == 2
    assert result["created"] == 2
    assert result["skipped"] == 0
