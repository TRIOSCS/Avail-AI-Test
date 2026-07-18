# Non-Production Code Audit — 2026-07-18

Audit of code present in the repository that is **not wired to production** — never
imported, never registered, never rendered, never scheduled, or gated off by default.
Method: five parallel sweeps (routers, jobs, services/modules, templates/static,
flags/models/scripts), each cross-checked for dynamic imports, string-built paths,
aggregator indirection, and management-CLI entrypoints before anything was declared dead.

Verdicts: **DEAD** = zero production reachability; **TEST-ONLY** = imported only by
`tests/`; **MGMT-ONLY** = reachable only via operator-run `app/management/` CLIs;
**FLAG-OFF** = wired but inert under default configuration.

---

## 1. Dead code (highest confidence — no production path reaches it)

### Services / utils (TEST-ONLY: imported by tests, nothing else)

| Module | Evidence |
|---|---|
| `app/services/buyplan_service.py` | Self-described re-export façade; production imports `app/services/buyplan_workflow/*` directly. Only `tests/test_buy_plan_service.py` imports it. |
| `app/services/contact_quality.py` | Only `tests/test_contact_quality.py`; the mention in `customer_enrichment_service.py:12` is a stale docstring, not an import. |
| `app/services/engagement_scorer.py` | Superseded — `app/services/vendor_score.py:3`: "Replaces engagement_scorer logic." Test importers only. |
| `app/services/presence_service.py` | Zero production references of any kind; two test files import it. |
| `app/services/vendor_email_lookup.py` | Six test files import it; no production importer. |
| `app/utils/sanitize.py` | Only `tests/test_sanitize.py`. Production sanitization uses unrelated local helpers (`template_env._sanitize_html_filter`, `routers/htmx/_shared._sanitize_hx_params`, `datasheet_library._sanitize`). |
| `app/services/ics_worker/monitoring.py` | Worker's own `worker.py`/`search_engine.py` don't import it; prod uses `search_worker_base.monitoring` (via `app/jobs/worker_liveness_jobs.py`). Test-only. |
| `app/services/nc_worker/monitoring.py` | Same pattern as ics variant. Test-only. |
| `app/services/tbf_worker/monitoring.py` | Same pattern. Test-only. |
| `app/services/tbf_worker/human_behavior.py` | Test-only (the ics/nc `human_behavior` variants ARE prod-used; tbf's is orphaned). |

### ORM models (tables exist; nothing reads or writes them)

| Model | Evidence |
|---|---|
| `FacetAudit` (`app/models/telemetry.py:45`, table `facet_audit`) | Referenced only by `app/models/__init__.py:174`. Its docstring names its writer as `app/management/audit_facets.py` ("future") — **that file does not exist**. |
| `KnowledgeConfig` (`app/models/knowledge.py:86`, table `knowledge_config`) | Referenced only by `app/models/__init__.py:100` and migrations 001/064/174. Docstring claims "Called by: services/teams_qa_service.py" — **that service does not exist**. |

### Static assets

| Asset | Evidence |
|---|---|
| `app/static/public/sw.js` | Nothing registers it; the live `GET /sw.js` route (`app/main.py:681`) serves an inline hardcoded duplicate. The `Caddyfile:42` no-cache handler for `/static/sw.js` protects a file no browser ever requests. Already flagged as DC-09 in `docs/audit/2026-07-02-production-polish-review.md`. |
| `app/static/public/icons/icon-512.png` | No template, route, or web-app manifest references it (repo has no `*.webmanifest`/`manifest.json`). `icon-192.png` and `apple-touch-icon.png` are linked from `base.html:10-11`; the 512 variant is orphaned. |

### Scripts (nothing in committed automation references them)

| Script | Evidence |
|---|---|
| `scripts/post-deploy.sh` | Zero references outside itself; overlaps `deploy.sh` steps 2–4. |
| `scripts/update.sh` | Referenced only in historical `docs/audit/2026-07-02-*`; superseded by `deploy.sh`. |
| `scripts/nightly_tests.sh` | Header says "Called by: root crontab (30 2 * * *)" but no repo automation installs that crontab line. |
| `scripts/weekly_cleanup.sh` | Same pattern — self-documented crontab line, never installed by repo automation. |
| `scripts/enrichment_watchdog.sh` | Same pattern — manual `crontab -` install one-liner in header, nothing wires it. |

---

## 2. Wiring inconsistency (production-affecting)

**`deploy.yml` vs `deploy.sh` systemd divergence.** `deploy.sh:298-313` restarts all
three host workers (`avail-nc-worker`, `avail-ics-worker`, `avail-tbf-worker`), but the
GitHub Actions release path `.github/workflows/deploy.yml:114-116` copies/restarts only
`avail-ics-worker` and `avail-nc-worker` — it omits `deploy/avail-tbf-worker.service`
**and** `deploy/avail-xvfb.service` (which all three browser workers `Requires=`).
A CI-driven release therefore never refreshes the TBF worker or the Xvfb unit; only the
manual `deploy.sh` path does. If TBF is a live source, CI deploys leave it running stale
code.

---

## 3. Management-CLI-only code (live, but never touched by the running app)

Reachable solely through operator-run `python -m app.management.<cmd>` /
`scripts/mgmt.sh` — dead to the app/worker containers, alive as backfill tooling.
Retire only if the corresponding CLI is decommissioned.

- `app/services/material_enrichment_service.py` — via `app/management/reenrich.py`
- `app/services/vendor_spec_enrich.py` — via `app/management/backfill_vendor_specs.py`
- `app/services/cpu_pollution/classifier.py` — via `app/management/fix_cpu_pollution.py`
- `app/services/source_ingest/` (entire package: `clean`, `consolidate`, `ingest`,
  `parsers`, plus intra-package `models`, `ai_correct`) — via
  `app/management/ingest_source_data.py` and `app/management/import_demand_telemetry.py`
- Model `ReconcileRun` (`app/models/telemetry.py:31`) — written only by
  `app/management/reconcile_decoded_facets.py`; no runtime reader.
- One-shot operator scripts (unreferenced by design): `scripts/backfill_material_card_ids.py`,
  `scripts/backfill_oem_enrichment.py`, `scripts/import_part_numbers.py`,
  `scripts/merge_suffix_material_cards.py`, `scripts/migrate_sf_pool.py`,
  `scripts/seed_proactive_demo.py`, `scripts/setup_readonly_role.sql`.

---

## 4. Wired but inert by default (flag/credential-gated)

Present and registered, but never executes under default configuration. Listed for
completeness — these look intentional (go-live switches), not accidents.

**Scheduler jobs whose gate defaults OFF** (`app/config.py` defaults):

- `app/jobs/email_jobs.py:_job_ownership_sweep` and `_job_site_ownership_sweep` — `ownership_sweep_enabled=False` (config.py:247)
- `app/jobs/prospecting_jobs.py:_job_account_sweep` — `account_sweep_enabled=False` (config.py:376)
- `app/jobs/eight_by_eight_jobs.py:_job_poll_8x8_cdrs` — `eight_by_eight_enabled=False` (config.py:312), also requires 8x8 credentials

**Feature-flag-dormant code paths** (default `False`): `expose_api_docs` (Swagger/ReDoc
routes never registered), `spec_resolver_enabled` (`app/services/spec_code_resolver.py`
dormant), `ai_screen_enabled` + `ai_screen_web_search_enabled`
(`app/services/prospect_screening.py`), `explorium_enrichment_enabled`,
`lusha_enrichment_enabled`, `clay_enrichment_enabled` (`app/services/enrichment_router.py`
branches), `email_mining_enabled`. Several are DB-overridable via `system_config`
(`get_effective_flag`), so "off by default" ≠ "off in prod" — verify against the
production DB before acting on any of these.

**Credential-gated connectors** seeding not-LIVE out of the box (per
`app/data/api_sources.json` + `app/startup.py:1577-1624`): nexar, brokerbin, ebay,
digikey, mouser, anthropic_ai, azure_oauth, explorium, lusha, hunter, 8x8, icsource,
thebrokersite, teams_notifications. Three have **no `config.py` Settings field at all**
(purely env-driven): `oemsecrets`, `sourcengine`, `element14`. The seed JSON also
contains a literal `future` placeholder source.

---

## 5. Confirmed clean (no dead code found)

- **Routers** — all 43 `include_router` calls in `app/main.py:849-891` are unconditional;
  every `APIRouter` module reaches production, some via non-obvious aggregation:
  `app/routers/htmx/{my_day,email_views,insights_views,search_views,requisitions_edit}.py`
  wire through the legacy `app/routers/htmx_views.py` aggregator (lines 34–58), and
  admin/crm/requisitions/v13_features/htmx-offers wire via package `__init__.py`
  aggregation. `app/routers/htmx/companies/*` share the single router created in
  `htmx/companies/__init__.py:36`. **Do not false-positive these.**
- **Jobs** — all 17 `app/jobs/` modules import via `app/jobs/__init__.py:register_all_jobs()`
  (invoked from `app/main.py:182`); every `_job_*` function is registered.
  `email_jobs._scan_user_inbox` also has a router caller (`app/routers/htmx/settings.py:209`,
  the "scan now" button).
- **Templates** — all 338 Jinja2 templates reachable (literal-string render roots +
  transitive include/extends/import graph; no dynamic template-name construction exists).
  Note: `docs/frprp/runs/2026-03-24/template-graph.json` claims 141 orphans — it is
  **stale** (references deleted dirs) and should not be trusted.
- **Connectors** — all 18 `app/connectors/` modules have production importers; the one
  dynamic-import site (`app/services/enrichment.py:119`) only loads connector modules.
- **Compose services** — every docker-compose entrypoint resolves to real code
  (`enrichment-worker` runs `app/services/enrichment_worker`, `db-backup` runs
  `scripts/backup-cron.sh`). Worker `__main__.py` files and `app/main.py` are
  entrypoints, not orphans, despite having zero importers.

---

## Suggested next steps (not performed in this audit)

1. Delete the ten TEST-ONLY modules in §1 with their now-pointless test files, or
   re-wire any that were meant to be used (e.g. decide whether `utils/sanitize.py`
   should replace the ad-hoc sanitizers).
2. Drop `FacetAudit`/`KnowledgeConfig` (models + Alembic migration) or build their
   missing consumers; fix the stale docstrings either way.
3. Resolve the sw.js duplication per the 2026-07-02 audit (one canonical copy);
   delete `icon-512.png` or add a web manifest that uses it.
4. Fix `.github/workflows/deploy.yml` to include `avail-tbf-worker.service` and
   `avail-xvfb.service`, matching `deploy.sh`.
5. Either install the three cron scripts via deploy automation or delete them;
   remove superseded `post-deploy.sh` / `update.sh`.
