# AvailAI Application Map — Database Schema

> **Auto-maintained reference.** Update this file whenever models, tables, or relationships change.

## Database Configuration

- **Engine:** PostgreSQL 16
- **ORM:** SQLAlchemy 2.0 with async support
- **Migrations:** Alembic (81+ migration files)
- **Connection:** Pool size 20, max overflow 20, pool recycle 1800s
- **Timeouts:** Statement 30s, lock 5s
- **Extensions:** pg_stat_statements, Full-Text Search (TSVECTOR), pg_trgm

## Table Overview by Domain

### Auth & Users

**`users`** — Application users (Azure AD OAuth)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| email | String 255, unique | |
| name | String 255 | |
| role | String 20 | buyer\|sales\|trader\|manager\|admin |
| is_active | Boolean | default True |
| azure_id | String 255, unique | |
| refresh_token | EncryptedText | For Graph API offline access |
| access_token | EncryptedText | |
| token_expires_at | DateTime | |
| m365_connected | Boolean | Graph API health |
| commodity_tags | JSON | User specialties |
| timezone | String 100 | |
| eight_by_eight_extension | String 20 | Phone system |

---

### Core Sourcing Pipeline

**`requisitions`** — Customer requests for parts
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| name | String 255 | |
| customer_name | String 255 | |
| company_id | FK -> companies | |
| customer_site_id | FK -> customer_sites | |
| status | String 50 | active\|archived\|completed |
| urgency | String 20 | normal\|hot\|critical |
| opportunity_value | Numeric 12,2 | |
| claimed_by_id | FK -> users | |
| created_by | FK -> users | |
| **Relationships** | requirements, attachments, contacts, offers, quotes |

**`requirements`** — Individual part lines within a requisition
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| requisition_id | FK -> requisitions (CASCADE) | |
| material_card_id | FK -> material_cards | |
| primary_mpn | String 255 | |
| normalized_mpn | String 255, indexed | |
| manufacturer | String 255 | |
| target_qty | Integer | |
| target_price | Numeric 12,4 | |
| sourcing_status | String 20 | open -> sourcing -> offered -> quoted -> won -> lost |
| substitutes | JSON | Alternative MPNs |
| substitutes_text | Text, indexed (GIN) | Flattened substitute MPNs for ILIKE search (used by global search + parts list) |
| assigned_buyer_id | FK -> users | |
| **Relationships** | requisition, sightings, offers, attachments |

**`sightings`** — Search results from supplier APIs
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| requirement_id | FK -> requirements (CASCADE) | |
| material_card_id | FK -> material_cards | |
| vendor_name | String 255 | |
| vendor_name_normalized | String 255, indexed | |
| mpn_matched | String 255 | |
| qty_available | Integer | |
| unit_price | Numeric 12,4 | |
| source_type | String 50, indexed | nexar\|brokerbin\|digikey\|etc |
| confidence | Float | 0.0-1.0 |
| score | Float | 0-100 (composite) |
| evidence_tier | String 4 | T1-T7 |
| is_authorized | Boolean | Authorized distributor? |
| source_company_id | FK -> companies | |

> **Router note:** `sightings_list()` and `sightings_detail()` in `app/routers/sightings.py` build a `link_map` dict (MPN string → MaterialCard.id) by querying `material_cards` with `normalize_mpn_key()`. The map is passed to the template context so the `mpn_chips` macro can link MPN chips to material card detail pages.

**`contacts`** — RFQ emails sent to vendors
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| requisition_id | FK -> requisitions (CASCADE) | |
| user_id | FK -> users (CASCADE) | |
| contact_type | String 20 | rfq\|follow_up\|etc |
| vendor_name | String 255 | |
| vendor_contact | String 255 | Email address |
| graph_message_id | String 500 | Microsoft Graph tracking |
| status | String 50 | sent\|replied\|etc |

**`vendor_responses`** — Replies from vendors to RFQs
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| contact_id | FK -> contacts | |
| requisition_id | FK -> requisitions | |
| vendor_email | String 255 | |
| body | Text | Raw email body |
| parsed_data | JSON | AI-extracted pricing |
| confidence | Float | Parse confidence |
| classification | String 50 | offer\|stock_list\|ooo\|spam |
| message_id | String 255, unique | |

---

### Offers & Quotes

**`offers`** — Vendor proposals (manual entry or AI-parsed from email)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| requisition_id | FK -> requisitions (CASCADE) | |
| requirement_id | FK -> requirements (CASCADE) | |
| vendor_card_id | FK -> vendor_cards | |
| material_card_id | FK -> material_cards | |
| mpn | String 255 | |
| unit_price | Numeric 12,4 | |
| qty_available | Integer | |
| lead_time | String 100 | |
| source | String 50 | manual\|email_parsed\|proactive |
| evidence_tier | String 4 | T1-T7 |
| parse_confidence | Float | 0.0-1.0 |
| status | String 20 | active\|sold |
| selected_for_quote | Boolean | Included in quote? |
| vendor_response_id | FK -> vendor_responses | |
| entered_by_id | FK -> users | |

**`quotes`** — Formal quotes sent to customers
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| requisition_id | FK -> requisitions (CASCADE) | |
| customer_site_id | FK -> customer_sites | |
| quote_number | String 50, unique | Auto-generated |
| line_items | JSON | |
| subtotal | Numeric 12,2 | |
| total_margin_pct | Numeric 5,2 | |
| status | String 20 | draft\|sent\|accepted\|rejected |
| result | String 20 | won\|lost |
| won_revenue | Numeric 12,2 | |

**`quote_lines`** — Individual parts in a quote
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| quote_id | FK -> quotes (CASCADE) | |
| offer_id | FK -> offers | |
| material_card_id | FK -> material_cards | |
| mpn | String 255 | |
| description | String 500 | AI-verified part description |
| qty | Integer | |
| cost_price | Numeric 12,4 | |
| sell_price | Numeric 12,4 | |
| margin_pct | Numeric 5,2 | |

---

### Buy Plans (Fulfillment)

**`buy_plans_v3`** — Purchase fulfillment after quote acceptance
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| quote_id | FK -> quotes (CASCADE) | |
| requisition_id | FK -> requisitions (CASCADE) | |
| sales_order_number | String 100 | |
| customer_po_number | String 100 | |
| status | String 30 | DRAFT -> SUBMITTED -> APPROVED -> COMPLETE |
| so_status | String 30 | PENDING -> VERIFIED -> REJECTED |
| total_cost / total_revenue / total_margin_pct | Numeric | |
| approval_token | String 100, unique | External approval link |

**`buy_plan_lines`** — Individual line items for purchasing
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| buy_plan_id | FK -> buy_plans_v3 (CASCADE) | |
| requirement_id | FK -> requirements | |
| offer_id | FK -> offers | |
| quantity | Integer | |
| unit_cost / unit_sell | Numeric 12,4 | |
| buyer_id | FK -> users | Assigned buyer |
| status | String 30 | AWAITING_PO -> CONFIRMED -> SHIPPED |
| po_number | String 100 | |

---

### CRM

**`companies`** — Customers, vendors, prospects
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| name | String 255 | |
| domain | String 255, indexed | |
| website | String 500 | |
| account_type | String 50 | Customer\|Prospect\|Partner\|Competitor |
| account_owner_id | FK -> users | |
| employee_size | String 50 | |
| hq_city / hq_state / hq_country | String | |
| brand_tags / commodity_tags | JSON | |
| enrichment_source | String 50 | explorium\|apollo\|manual |
| is_strategic | Boolean | |
| sf_account_id | String 255, unique | Salesforce link |

**`customer_sites`** — Delivery/contact locations for a company
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| company_id | FK -> companies (CASCADE) | |
| site_name | String 255 | |
| owner_id | FK -> users | |
| contact fields | name, email, phone, title, linkedin | |
| address fields | line1, line2, city, state, zip, country | |
| site_type | String 50 | HQ\|Branch\|Warehouse\|Manufacturing |
| payment_terms / shipping_terms | String 100 | |

**`site_contacts`** — Individual people at customer sites
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| customer_site_id | FK -> customer_sites (CASCADE) | |
| full_name | String 255 | |
| email | String 255 | Unique per site |
| contact_role | String 50 | buyer\|technical\|decision_maker\|operations |
| email_verified | Boolean | |
| enrichment_source | String 50 | lusha\|apollo\|hunter\|manual |

---

### Vendors

**`vendor_cards`** — Normalized vendor profiles
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| normalized_name | String 255, unique | Dedup key |
| display_name | String 255 | |
| domain | String 255, indexed | |
| emails / phones / contacts | JSON | |
| sighting_count | Integer | |
| vendor_score | Float 0-100 | Composite reliability |
| engagement_score | Float | |
| response_rate | Float 0-1.0 | |
| brand_tags / commodity_tags | JSONB | |
| search_vector | TSVECTOR | Full-text search |
| is_blacklisted | Boolean | |

**`vendor_contacts`** — People at vendor companies
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| vendor_card_id | FK -> vendor_cards (CASCADE) | |
| full_name | String 255 | |
| email | String 255 | Unique per vendor |
| confidence | Integer 0-100 | |
| relationship_score | Float 0-100 | |
| activity_trend | String 20 | warming\|stable\|cooling\|dormant |

**`vendor_reviews`** — Team feedback on vendors (1-5 rating)

**`strategic_vendors`** — Claimed vendor-buyer relationships with expiry

---

### Materials & Parts

**`material_cards`** — Deduplicated part number profiles
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| normalized_mpn | String 255, unique | Dedup key |
| display_mpn | String 255 | |
| manufacturer | String 255, indexed | |
| description | Text | AI-enriched part description |
| category | String 255 | AI-enriched commodity category |
| lifecycle_status | String 50 | active\|nrfnd\|eol\|obsolete\|ltb |
| package_type | String 100 | QFP-64\|BGA-256\|0603 |
| rohs_status | String 50 | compliant\|non-compliant\|exempt |
| condition | String 20, nullable, indexed | Broker stock condition: `New`\|`Recertified`\|`Refurbished`\|`Used`\|`Pulled`\|`Unknown`. Application-validated (no DB CHECK). Powers the Condition global facet; NULL until a source (offer/sighting provenance) populates it — the facet renders only values with data. Migration 091. |
| enrichment_status | String 20 | `unenriched` \| `verified` \| `web_sourced` \| `oem_sourced` \| `ai_inferred` \| `not_found` \| `not_catalogued`. Validated on write against `MaterialEnrichmentStatus` (constants.py). `oem_sourced` = single official OEM page; `not_catalogued` = recognised OEM/FRU part with no public specs (retries on 30-day backoff). No migration — varchar column. |
| cross_references | JSONB | Alternative MPNs; also records OEM FRU→commodity-MPN linkages written by the cross-ref enrichment tier (`[{"mpn": <resolved>, "manufacturer": <mfr>}]`). |
| specs_structured | JSONB | Parametric data |
| enriched_at | UTCDateTime, nullable | When the first-pass card enrichment (description/category/lifecycle) ran; NULL = not yet run |
| specs_enriched_at | UTCDateTime, nullable, indexed | When the second-pass structured-spec extraction ran; NULL = spec pass not yet run |
| search_vector | TSVECTOR | Trigger-maintained FTS (weighted: MPN=A, manufacturer=B, description/category=C) |

> **Startup backfill:** `_backfill_material_cards()` in `startup.py` runs at boot to ensure every MPN in requirements has a corresponding material card.

> **Indexes & Triggers:**
> - `trig_material_cards_search_vector` — PostgreSQL trigger maintains `search_vector` TSVECTOR on INSERT/UPDATE (weighted: display_mpn=A, manufacturer=B, description/category=C)
> - `ix_material_cards_search_vector` — GIN index for fast full-text search via `plainto_tsquery()` + `ts_rank()`
> - `ix_material_cards_trgm_mpn` — pg_trgm GIN index on `display_mpn` for typo-tolerant search

**`material_vendor_history`** — Which vendors sell which parts (deduplicated)

**`material_card_audit`** — Audit trail for card lifecycle events

**`material_price_snapshots`** — Historical pricing data points

**`customer_part_history`** — What parts each customer has bought (for proactive matching)

**`fru_links`** — IBM/Lenovo FRU crosswalk: one row per FRU ↔ related-PN edge (migration 094)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| fru_raw / fru_norm | String 64, NOT NULL | FRU as in source / `normalize_mpn_key` form (`ix_fru_links_fru_norm`) |
| related_raw / related_norm | String 64, NOT NULL | Related PN raw / normalized (`ix_fru_links_related_norm`) |
| rel_kind | String 24, NOT NULL | `FruLinkKind` (constants.py): `ibm_11s`\|`mfg_model`\|`option`\|`option_pn`\|`sourcing_pn`\|`lenovo_pn`\|`lenovo_ppn`\|`tray`\|`tray_alt`\|`bracket`\|`board`\|`screws`\|`shuttle`\|`dongle`\|`drive_pn`\|`assembly`. Validated on write. |
| manufacturer | String 128, nullable | Maker of the related part (mfg_model/drive_pn rows) |
| description / note | Text, nullable | Part description / free-text context (feature codes, FW, carrier notes) |
| series / machine | String 64 / 128, nullable | Platform context (xSeries, Storwize, POWER 8, ...) |
| qual_status / qual_date | String 64 / Date, nullable | Free text from the workbook qual column (e.g. `qlot approved`, `qlot approved - Only EMEA`) or the app-synthesized `CDC_PENDING` sentinel (constants.py); date when known |
| source_sheet | String 64, NOT NULL | Workbook sheet the edge came from |
| created_at / updated_at | UTCDateTime | |

> UNIQUE `uq_fru_links_edge` (fru_norm, related_norm, rel_kind, source_sheet). Populated by
> `python -m app.management.ingest_fru_matrix <xlsx> [--apply]`; read by
> `app/services/fru_matrix_service.py` for the materials detail "FRU matrix" / "Used in FRUs" panels.

---

### Excess Inventory

**`excess_lists`** — Customer surplus inventory batches
- company_id -> companies, owner_id -> users
- Status: draft -> active -> bidding -> closed -> expired

**`excess_line_items`** — Individual parts in an excess list
- part_number, description, manufacturer, quantity, asking_price, demand_match_count

**`bids`** — Vendor bids on excess items
- bidder_company_id, bidder_vendor_card_id, unit_price, quantity_wanted, status

**`bid_solicitations`** — Outbound emails soliciting bids
- graph_message_id for email tracking

---

### Sourcing Leads & Evidence

**`sourcing_leads`** — AI-ranked vendor leads per requirement
- requirement_id + vendor_name_normalized (unique)
- confidence_score, confidence_band (low/medium/high)
- vendor_safety_score, vendor_safety_band
- buyer_status: new -> contacted -> quoted -> won

**`lead_evidence`** — Supporting signals for each lead
- signal_type, source_type, weight, confidence_impact

**`lead_feedback_events`** — Buyer actions on leads

---

### Proactive Matching

**`proactive_matches`** — Vendor offers matched to customer purchase history
- offer_id -> offers, requirement_id, customer_site_id
- match_score, margin_pct, customer_last_price

**`proactive_offers`** — Emails sent for proactive matches
**`proactive_throttle`** — Rate limit: MPN + site (unique), last_offered_at
**`proactive_do_not_offer`** — Blacklist: MPN + company (unique)

---

### Tagging & Classification

**`tags`** — Hierarchical tag definitions (brand\|commodity, with parent_id)
**`material_tags`** — Tags on material_cards (with confidence and source)
**`entity_tags`** — Tags on vendor_cards/companies (with interaction counts)
**`tag_threshold_config`** — Visibility thresholds per entity type

---

### Intelligence & Activity

**`activity_log`** — Every user interaction (polymorphic FKs)
- Links to: company, vendor_card, vendor_contact, requisition, requirement, quote, customer_site, site_contact, buy_plan
- direction: inbound\|outbound
- quality_score, quality_classification

**`activity_digest`** — AI-generated digest cache (one row per entity, migration `086_add_activity_digest.py`)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| entity_type | String(50) | DigestEntityType: requisition\|company |
| entity_id | Integer | FK target (no DB-level FK — polymorphic) |
| headline | String(300) | One-line summary |
| narrative | Text | 2-4 sentence plain-language summary |
| highlights | JSON | list[{"label": str, "value": str}], max 5 |
| next_step | String(500) | Suggested next action |
| status_signal | String(20) | DigestStatusSignal: on_track\|stalled\|needs_attention |
| generated_at | UTCDateTime | When Claude produced this version |
| basis_last_activity_at | UTCDateTime | Max created_at of source activities (freshness key) |
| basis_activity_count | Integer | Count of source activities (freshness key) |
| cooldown_until | UTCDateTime | No regen before this time (default: 120 s after last build) |
| model | String(50) | Model tier used ("smart") |

Unique constraint on `(entity_type, entity_id)` — always exactly one cached row per entity.
Self-invalidates: service regens when `basis_last_activity_at` or `basis_activity_count` changes.

**`change_log`** — Field-level edit history on offers/requirements/requisitions

**`email_intelligence`** — Classified inbox emails (offer, stock_list, ooo, spam)

**`knowledge_entries`** — Q&A, facts, AI insights linked to entities

---

### Enrichment

**`enrichment_jobs`** — Batch enrichment tracking
**`enrichment_queue`** — Proposed field changes awaiting review
**`email_signature_extracts`** — Parsed email signatures (unique by sender_email)
**`prospect_contacts`** — Web-found contacts awaiting import
**`prospect_accounts`** — Discovered prospect companies (unique by domain)
**`discovery_batches`** — Import batch tracking

---

### Performance & Scoring

**`vendor_metrics_snapshot`** — Monthly vendor reliability (response_rate, on_time_delivery, composite_score)
**`buyer_leaderboard_snapshot`** — Monthly buyer points and rank
**`avail_score_snapshot`** — Monthly behavior + outcome scoring (0-100)
**`multiplier_score_snapshot`** — Monthly point multiplier system
**`unified_score_snapshot`** — Combined monthly score across all dimensions
**`buyer_vendor_stats`** — Per-buyer per-vendor relationship metrics
**`stock_list_hashes`** — Deduplicate uploaded stock lists

---

### Tasks & Tickets

**`requisition_tasks`** — Tasks tied to requisitions (manual, system, or AI-generated)
**`trouble_tickets`** — Bug reports with screenshots, AI diagnosis
**`root_cause_groups`** — Grouped similar tickets
**`notifications`** — User notification queue

---

### System & Config

**`api_sources`** — Supplier connector config (credentials, quotas, health)
**`system_config`** — Key-value app settings
**`graph_subscriptions`** — Microsoft Graph webhook registrations
**`intel_cache`** — PostgreSQL fallback cache (when Redis unavailable)
**`processed_messages`** — Idempotency tracking for email processing
**`sync_state`** — Email folder sync tokens
**`pending_batches`** — Async batch job tracking

---

### Search Queues

**`ics_search_queue`** — ICS browser automation queue (priority, status, gate_decision). Dedup keyed on `(requirement_id, normalized_mpn)` — backed by a composite UNIQUE (`uq_ics_queue_requirement_mpn`) that replaced the legacy per-requirement UNIQUE — so the spec-code resolver can enqueue multiple AVL MPNs per requirement while concurrent enqueues still can't double-insert (the app-layer check in `QueueManager.enqueue_search` catches the resulting `IntegrityError` and returns the winning row); carries `resolved_via_spec_code` lineage.
**`nc_search_queue`** — NetComponents browser automation queue (same structure + same composite-UNIQUE dedup `uq_nc_queue_requirement_mpn` / lineage change)

### OEM Spec-Code Resolver

Translates an OEM spec code (e.g. IBM `SPREJ`) to approved MPNs when the normal connector fanout returns universal zero. See `app/services/spec_code_resolver.py` and `app/routers/admin/spec_codes.py`.

**`oem_spec_codes`** — Authoritative, human-approved spec-code → AVL mappings. `source` (validated against `SpecCodeSource`: manual/llm_approved/csv_import), `avl` (JSONB), `approved_at` (TIMESTAMPTZ), UNIQUE `(oem, spec_code)`.
**`oem_spec_codes_pending`** — LLM-discovered mappings awaiting approval. `llm_confidence` (0–1, model-validated), `citations` (JSONB, structural http(s) URL check at model layer), `used_in_requirement_ids` (JSONB), UNIQUE `(oem, spec_code)`. The resolver splits read+LLM (`propose()`) from the write (`persist()`): the read transaction is released before the grounded LLM call so no connection is pinned for its ~60s duration, and the pending-row insert happens in a short SAVEPOINT afterward (concurrent-insert races recover to the winning row rather than erroring).
**`oem_spec_codes_blacklist`** — Rejected MPNs fed back into the LLM exclusion prompt; multiple rows per `(oem, spec_code)` allowed.

Lineage columns added to existing tables: `requirements.oem_hint`; `sightings.resolved_via_spec_code` / `sightings.source_mpn`; `offers.resolved_via_spec_code` / `offers.source_mpn`. (Today only the synchronous fanout tags sightings; the async ICS/NC workers record the tag on the queue row but do not yet copy it onto worker-created sightings.)

### Faceted Search

**`commodity_spec_schemas`** — Parametric filter definitions per commodity
**`material_spec_facets`** — Parametric values per material card

> **Seed source of truth:** `app/data/commodity_seeds.json` (loaded by `commodity_registry.py`). `seed_commodity_schemas()` only INSERTs missing `(commodity, spec_key)` pairs at boot — it never updates an existing row — and `reseed_changed_schemas()` (also run at boot, right after the inserter) reconciles rows whose seed definition drifted via delete-then-reinsert. Net-new spec keys on already-seeded commodities therefore reach an existing DB automatically; *removing* a seed never deletes its DB row and needs a data migration (e.g. `093_normalize_legacy_categories` retiring `connectors/series` after the 2026-06-09 taxonomy expansion replaced it with `rows`). Two tree keys are declared coarse buckets with NO parametric seeds (`COARSE_BUCKETS_WITHOUT_SEEDS` = `ics_other`, `oem_assemblies`) — they bucket generic ICs and whole OEM assemblies, which have no honest parametric vocabulary. `tape_drives` (Storage & Drives) is fully seeded (drive_type/interface primary, form_factor, native_capacity_gb, encryption).
>
> **Canonical filter values:** for a fixed-vocabulary enum (non-empty `enum_values`), `get_subfilter_options()` renders the full declared list — unstocked values still show with a `(0)` count. Open-vocabulary enums (no `enum_values`, e.g. motherboard `chipset`) render top-N observed values behind a typeahead. Booleans always offer Yes/No.
>
> **Category canonicalization:** `app/services/category_normalizer.py` maps free-text `material_cards.category` variants (e.g. `connectors, interconnects` → `connectors`) to the canonical commodity keys the faceted sidebar buckets on — including the globally-unambiguous TRIO SFDC part-master `Commodity_Code__c` codes (`Main Board`→`motherboards`, `Hard Drive`→`hdd`, `LCD`/`LCD ASSY`→`displays`, `PSU`→`power_supplies`, `Graphics Card`→`gpu`, `Tape Drive`→`tape_drives`, `IC`/`Integrated Circuits (ICs)`→`ics_other`, `OEM ASSY`→`oem_assemblies`). Source-scoped codes that are only unambiguous inside TRIO's export live in `TRIO_SFDC_COMMODITY_CODES` (bare `Memory`→`dram` — supplier taxonomies use "Memory" for flash/EEPROM/SRAM too) and resolve only through `normalize_trio_category()` (the SFDC ingest entry point; falls back to the global map); the global `normalize_category()` never consults them. Forward hook at the three card category write sites; one-off backfill via `scripts/normalize_categories.py --dry-run|--apply`. Ambiguous strings are left untouched. Legacy rows already in the DB were normalized once by data migration `093_normalize_legacy_categories` (case-insensitive rewrite through a frozen snapshot of the full alias vocabulary, incl. `memory`→`dram` — safe because every existing row carries TRIO provenance; downgrade is a documented no-op for categories).
>
> **Deterministic MPN decode (Phase 1 of MPN→spec enrichment):** `app/services/mpn_decoder/` reads facet specs straight from standard manufacturer drive/SSD/DIMM part numbers (HDD: Seagate/WD/Toshiba/HGST in `storage.py`; SSD: Samsung/Micron/Intel-Solidigm/Kioxia/WD in `ssd.py`; DRAM: Samsung/Hynix/Micron/Kingston/Crucial in `memory.py`) — zero network/LLM, strict per-vendor regex gates that require the full family structure (e.g. Toshiba `^(MG|MN|MD|MQ|DT)\d{2}[A-Z]{3}`, so short OEM spares like Dell DPNs don't false-match; HGST `HUS` requires a digit next so the HUSMM/HUSSL SAS-SSD families don't misdecode as 3.5" HDDs), unrecognized schemes skipped. DRAM modules additionally decode `rank` (enum 1Rx4/1Rx8/2Rx4/2Rx8/4Rx4/8Rx4 — 8Rx4 is emittable via the Hynix device-count math but no shipping part exercises it), `registered` (Registered/Unbuffered/Load-Reduced) and `voltage` (numeric V: 1.2/1.35/1.5; DDR5 1.1 V deliberately omitted) where the org block pins them — all three are seeded `dram` spec schemas in `commodity_seeds.json`, and `tests/test_mpn_decoder_seed_sync.py` pins decoder↔seed sync so `record_spec` never silently drops decoder output; SSD NVMe `interface` is emitted only when the family pins the PCIe generation (the seeded enum has no bare "NVMe"). The full vendor/scheme inventory table lives in APP_MAP_INTERACTIONS.md. The worker second pass (`mpn_decoder/writer.py::decode_and_record_specs`, gated by `settings.mpn_decode_enabled`, default on) writes via `record_spec(source="mpn_decode", confidence=0.95)` BEFORE the AI spec pass. Category handling: an existing category is authoritative and a decoded-commodity mismatch is skipped; a card with NO category is **categorized from the decode** (the regex-gated commodity). Each card writes inside a `db.begin_nested()` SAVEPOINT so a single DB failure can't poison the shared batch transaction. Coverage dry-run + backfill: `scripts/decode_mpn_dryrun.py` (read-only by default; `--apply` backfills existing inventory in chunked commits). OEM/FRU spare numbers don't match the gates → resolved in later phases (PartSurfer cross-ref / datasheet).
