"""SP-Ingest — TRIO authoritative source-data ingestion pipeline.

What: clean → normalize → consolidate → (AI-correct) → AUGMENT-ingest TRIO source files
      (SFDC part master + operational inventory sheets) into ``material_cards`` as the TOP
      tier of the enrichment provenance ladder (``trio_source``=95, ``trio_source_ai``=88).
Called by: app/management/ingest_source_data.py (the CLI entry point).
Depends on: parsers, clean, consolidate, ai_correct, ingest (this package) +
      app.services.spec_tiers / spec_write_service for the SP2 tier ladder.
"""
