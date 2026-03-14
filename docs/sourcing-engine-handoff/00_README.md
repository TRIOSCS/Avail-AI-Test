# Avail Sourcing Engine Handoff Bundle

This bundle is designed for upload into Cursor (or use by a contractor) so the sourcing engine can be implemented with minimal ambiguity and strong continuity of product intent.

## What is in this bundle
- `01_master_planning_blueprint.docx` — polished planning document
- `01_master_planning_blueprint.md` — same plan in markdown for easy LLM ingestion
- `schemas/` — structured lead/evidence schemas in JSON and YAML
- `wireframes/` — annotated wireframe PNGs plus a screen spec
- `prompts/` — copy/paste Cursor prompts for discovery, design, implementation, QA, and cleanup
- `qa/` — acceptance criteria, smoke tests, human test checklist
- `decisions/` — decision log and open questions
- `notes/` — implementation slices and source connector guidance

## Product mission
Generate high-quality, explainable supplier leads for purchasing agents to follow up by phone or email, verify live stock, and record outcomes that improve future ranking.

## Key product principles
1. One lead per vendor per part.
2. Separate stock-confidence from vendor-safety.
3. Preserve source attribution.
4. Conservative deduplication beats risky auto-merging.
5. Optimize for buyer usefulness, not lead volume.
6. Buyer feedback should improve future ranking.

## Suggested implementation order
1. Lead and evidence schema foundation
2. Confidence and safety scoring separation
3. Deduplication
4. Buyer workflow and statuses
5. Feedback loop
6. Buyer-facing UI
7. Connector hardening and enrichment
8. QA, cleanup, and human testing

## How to use in Cursor
1. Upload this entire folder or zip.
2. Start with `prompts/01_cursor_discovery.txt`
3. Keep the agent constrained to one phase/slice at a time.
4. Require tests/checks after each slice.
5. Preserve the decision log during the project.
