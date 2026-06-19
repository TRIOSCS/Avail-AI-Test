# AvailAI Application Map — Database Schema

> **Auto-maintained reference.** Update this file whenever models, tables, or relationships change.

## Database Configuration

- **Engine:** PostgreSQL 16
- **ORM:** SQLAlchemy 2.0 with async support
- **Migrations:** Alembic (95+ migration files)
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

**`vendor_part_unavailability`** — Durable vendor+part unavailability knowledge ("this vendor's stock of this part is gone"): one row per (normalized vendor, normalized MPN) pair recording why + note + provenance. Outlives scraped Sighting rows — every sighting-persistence path re-stamps fresh rows from these records, and RFQ suggestions exclude matching vendors while a record is active. `Sighting.is_unavailable` is **demoted to a render cache**: the `is_active` predicate in `app/services/vendor_unavailability.py` is the single authority on every read surface (see APP_MAP_INTERACTIONS § 2d). Migrations 102 (base table) + 103 (policy/provenance columns).
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| vendor_name_normalized | String 255, not null, indexed | via `normalize_vendor_name()` (app/vendor_utils.py); @validates re-normalizes on write (empty result raises) |
| normalized_mpn | String 255, not null, indexed | via `normalize_mpn_key()` — same canonical dash-stripped key space offers use; @validates re-normalizes on write (empty result raises) |
| reason | String 32, not null | `UnavailabilityReason` StrEnum (bought_by_us\|sold_elsewhere\|broken\|not_really_there\|different_part\|other), validated on write; display text via the enum's `.label` property (single source of truth) |
| note | Text, nullable | free-text "what we learned" |
| created_by_id | FK -> users, SET NULL | knowledge outlives accounts |
| created_at | UTCDateTime, not null | dual default (Python + server); also the temporal-policy window anchor — re-mark refreshes it. NOT NULL so `is_active`'s None branch is provably pre-flush-only |
| qty_at_mark | Integer, nullable | 103. Per-key qty snapshot at mark/re-mark: max non-NULL `qty_available` over the vendor's sightings whose `normalize_mpn_key(mpn_matched)` equals THIS record's key (empty-MPN rows count toward the primary-key record); never cross-key. Re-mark keeps the old value when the new computation is NULL. Powers the O2 restock override; NULL ⇒ O2 never fires (fail-closed for records created before 103) |
| released_at | UTCDateTime, nullable | 103. Written ONLY by override O3 (buyer-routed vendor email) and the offer hook — both user-initiated paths, both via the model's `release()` transition; NULLed on re-mark (`re_arm()`). Non-NULL ⇒ record not active |
| release_trigger | String 32, nullable | 103. `ReleaseTrigger` StrEnum (vendor_email\|offer_received), validated on write (None allowed); advisory hint copy via the enum's `.label`. CHECK `ck_vendor_part_unavail_release_pair` enforces (released_at IS NULL) = (release_trigger IS NULL) |
| requirement_id | FK -> requirements, SET NULL, indexed | 103. Provenance: the requirement the mark was made from (refreshed on re-mark). SET NULL, not CASCADE — knowledge outlives requirements. Widens `clear_unavailability`'s delete predicate so a record whose key no longer matches the requirement's current keys is still clearable (zombie-record fix) |

> UNIQUE `uq_vendor_part_unavail_vendor_mpn` (vendor_name_normalized, normalized_mpn) — marking again for an existing pair is an upsert (the re-arm path), never a duplicate. Written and read only via `app/services/vendor_unavailability.py` (record/clear/apply/release/exclude) and `app/services/sighting_status.py` (reader-authority status branch).

**`contacts`** — outreach to vendors (RFQ emails, logged calls)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| requisition_id | FK -> requisitions (CASCADE) | |
| user_id | FK -> users (CASCADE) | |
| contact_type | String 20 | email (RFQ sends)\|phone (logged calls) |
| vendor_name | String 255 | |
| vendor_contact | String 255 | Email address |
| parts_included | JSON | Parts asked of the vendor — scoped to THIS row's requisition |
| graph_message_id | String 500 | Microsoft Graph tracking |
| graph_conversation_id | String 500 | Graph thread id — inbox-monitor Tier-1 reply matching |
| status | String 50 | sent\|replied\|etc |

> **Row semantics: one row per (requisition, vendor) pair.** A cross-requisition
> bulk RFQ (sightings composer) still sends ONE email per vendor, but
> `send_batch_rfq` writes one Contact per involved requisition — each
> `parts_included` holding only its own requisition's parts, all of a vendor's
> rows sharing that one email's `graph_message_id` / `graph_conversation_id`.
> The inbox monitor therefore treats `graph_conversation_id` as one-to-many
> (a reply on the thread progresses EVERY contact sharing it). No schema change
> was needed — multiplicity lives in rows, `requisition_id` stays singular.

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
| qualification_status | String 20, indexed, nullable | `QualificationStatus` snapshot: `unset`\|`incomplete`\|`essentials`\|`complete`. Written by `apply_qualification()`; recomputed live on `Offer.qualification_summary` (col is filter/report convenience). Migration 108. |
| qualification_note | Text, nullable | System-composed standardized note (NOT free buyer notes). Produced by `compose_note(condition, data)` in `offer_qualification.py`; overwritten on every save. Migration 108. |
| qualification | JSON, nullable | Condition-specific qualification detail + pending vendor requests. Shape: `{usage, refurbished_by, refurb_process, cert_doc, part_condition, provenance_story, terms, lead_time_reason, requests:[{kind, status, requested_at, contact_id}]}`. Migration 108. |

> **Qualification enums (app/constants.py):**
> - `OfferCondition` (StrEnum) — `new` \| `new_no_pkg` \| `pulls` \| `refurb`. Governs the condition-spine validation and note composition. Distinct from `MaterialCondition` (the capitalized card/facet vocab).
> - `QualificationStatus` (StrEnum) — `unset` (no condition chosen) \| `incomplete` (an essential is missing; legacy/API only) \| `essentials` (essentials met, some recommended fields missing) \| `complete` (all essentials + recommended present).

> **Migration 108 (`108_offer_qualification`)** — adds the 3 columns + `ix_offers_qualification_status` index; also migrates legacy `condition = 'used'` → `'pulls'` (one-way data change, not reversed on downgrade).

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
| status | String 30 | draft -> pending -> active -> completed (also halted / cancelled) |
| so_status | String 30 | pending -> approved / rejected (ops SO-verify track) |
| total_cost / total_revenue / total_margin_pct | Numeric | |
| purchase_history_recorded_at | UTCDateTime, nullable | Idempotency stamp set by `record_buyplan_purchase_history` when CPH rows have been written for this plan (migration `bp_cph_recorded_at`). NULL = not yet recorded; non-NULL = safe to skip on retry/backfill. |

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
| status | String 30 | awaiting_po -> pending_verify -> verified (also issue / cancelled) |
| po_number | String 100 | |
| estimated_ship_date / po_confirmed_at | UTCDateTime | Vendor dock date + buyer confirm time |
| last_nudge_at | UTCDateTime | Idempotency clock for the unconfirmed-instruction nudge job |

**`verification_group_members`** — Ops users who can verify SO/PO (gates buy-plan completion)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| user_id | FK -> users (CASCADE), unique | One row per user; toggle `is_active`, never delete |
| is_active | Boolean | |
| added_at | UTCDateTime | |

Managed via Settings > Ops Group (admin only); seeded from `ADMIN_EMAILS` on startup.

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
| last_activity_at | UTCDateTime, nullable | Bumped by `log_outreach_initiated()` on every click-to-contact event; used by the CDM account workspace `staleness` sort (oldest = longest since activity first). |
| disposition | String 20, indexed | Migration 118. `active`\|`bucket` (`CompanyDisposition` StrEnum); NULL ⇒ active (mirrors `tier`'s NULL ⇒ standard). `bucket` accounts are suppressed from the "needs a call" call-list (chip COUNT + click-through) via the shared `crm_service._needs_call_filter` and from `cdm_company_query`'s base, NULL-safe (`or_(disposition != 'bucket', disposition.is_(None))`) — re-surfaced ONLY by the explicit `staleness='bucket'` facet. Set via `POST /v2/partials/customers/{id}/disposition` (owner-or-admin). NEVER overloaded onto `is_active`. |
| disposition_reason | String, nullable | Optional free-text rationale for the disposition (parity with prospect dismiss audit). |
| disposition_set_by | FK -> users (SET NULL) | Who set the disposition. |
| disposition_set_at | UTCDateTime, nullable | When the disposition was last set. |
| normalized_name | String 255, indexed (btree + Postgres GIN pg_trgm), **nullable, NOT unique** | Migration 120 (Increment 3, AI-org). Suffix-stripped/lowercased dedup match key, kept in lockstep with `name` by `Company._sync_normalized_name` (`@validates("name")`) using `vendor_utils.normalize_vendor_name` — the SAME normalizer the dedup scanner scores with. Mirrors VendorCard but is **nullable + non-unique** on purpose (companies legitimately share a normalized form across the dedup window; the policy keeps different-owner accounts separate). The `ix_companies_normalized_name_trgm` GIN index is Postgres-guarded (`dialect.name == 'postgresql'`); SQLite gets only the btree and the scanner falls back to rapidfuzz. |
| alternate_names | JSON (default []) | Migration 120. Names this company has been known by. `merge_companies` appends the loser's `name` (+ its own `alternate_names`, deduped, never keep's display name) so a re-import of the old name fuzzy-matches the survivor instead of recreating the duplicate (mirrors `VendorCard._record_alternate_name`). |

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
| last_activity_at | UTCDateTime, nullable | Bumped by `log_outreach_initiated()` alongside `companies.last_activity_at`. |

**`site_contacts`** — Individual people at customer sites
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| customer_site_id | FK -> customer_sites (CASCADE) | |
| full_name | String 255 | |
| email | String 255 | Unique per site |
| phone | String 100 | |
| wechat_id | String 100, nullable | WeChat handle for click-to-message outreach (migration 095_wechat_id). Written by the site-contact create form; rendered in `tabs/contacts_tab.html` as a `weixin://` deep link with `data-outreach-log`. |
| contact_role | String 50 | buyer\|technical\|decision_maker\|operations |
| do_not_contact | Boolean NOT NULL (server_default false) | Migration 116. Suppresses outreach affordances; toggled via `POST .../contacts/{id}/do-not-contact` (`_dnc_toggle.html`). |
| is_priority | Boolean NOT NULL (server_default false) | Migration 118. Surfaces the contact to the TOP of the roster (`company_contact_rows` order_by). Toggled via `POST .../contacts/{id}/priority` (`_priority_toggle.html`). Mirrors `do_not_contact`. |
| is_archived | Boolean NOT NULL (server_default false) | Migration 118. Sorts the contact to the BOTTOM of the roster but keeps it visible (NOT `is_active`, which would hide it). Toggled via `POST .../contacts/{id}/archive` (`_archive_toggle.html`). |
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
| manufacturer | String 255, indexed | Dual-brand semantics (migration 097): the ACTUAL MAKER (`Seagate Technology`, `Kingston Technology`, composite `Hitachi/IBM` verbatim). Written ONLY via `spec_tiers.set_manufacturer` (F1 ladder + `normalize_brand_name`); legacy direct writes rank at the `legacy_backfill` floor (50) on the next arbitration. Brand canonicalization (migration 106): `set_manufacturer`/`set_brand` reject `is_garbage_brand_value` fragments (the "(TP,F)" ingest-leak residue `F)`/`F`/`LF(T` — unbalanced parens / len<2) before the ladder; the one-shot `app/management/normalize_manufacturers.py --apply` NULLs those fragments (provenance cleared) and folds alias variants to canonical (provenance preserved) catalog-wide. |
| brand | String 255, nullable, indexed (`ix_material_cards_brand`) | Migration 097. The OEM LABEL on the part (`IBM`, `Dell Technologies`, `HPE`, `Lenovo`) — most cards never get one. Written ONLY via `spec_tiers.set_brand`, gated to source-backed evidence (`OEM_TRAILING_RE` description token, explicit ingest column, B1 legacy reclassify) — never guessed. The materials "Brand" facet ORs across `brand` + `manufacturer` (one combined facet; wire param stays `manufacturers`). Brand canonicalization (migration 106) folds the HPE family 4 ways (Hewlett Packard Enterprise / HP / Hewlett Packard / Hewlett-Packard → `HPE`) and case-folds Dell (DELL/Dell → `Dell Technologies`) so the facet no longer wastes 7 of its top-20 slots on duplicates. |
| brand_source / brand_confidence / brand_tier / brand_updated_at | String 50 / Float / Integer / UTCDateTime — all nullable | Migration 097. Provenance for `brand`, same F1 contract as `category_*` (valued-but-NULL-provenance ranks at the legacy floor 50; `brand_updated_at` is the ladder tie-break stamp). |
| manufacturer_source / manufacturer_confidence / manufacturer_tier / manufacturer_updated_at | String 50 / Float / Integer / UTCDateTime — all nullable | Migration 097. Provenance for `manufacturer` — required so trio_source (95) maker evidence (fru_links `mfg_model`) can displace an OEM name sitting in `manufacturer` from legacy data via the ladder. Maker writers: `mpn_decode` (85, decoder's own vendor), `fru_matrix_decode` (84, §2.6(d) — the UNANIMOUS deterministic vendor across a FRU's decoded canonical models, conf 0.9), vendor APIs (90), trio_source (95), manual (100). All pre-097 rows are NULL → legacy floor 50 at runtime (no in-migration backfill; the data backfill is `python -m app.management.backfill_dual_brand`, dry-run by default, run post-deploy). |
| description | Text | AI-enriched part description |
| category | String 255 | AI-enriched commodity category |
| lifecycle_status | String 50 | active\|nrfnd\|eol\|obsolete\|ltb |
| package_type | String 100 | QFP-64\|BGA-256\|0603 |
| rohs_status | String 50 | compliant\|non-compliant\|exempt |
| condition | String 20, nullable, indexed | Broker stock condition: the `constants.MaterialCondition` StrEnum vocabulary (`New`\|`Recertified`\|`Refurbished`\|`Used`\|`Pulled`\|`Unknown`). Application-validated (no DB CHECK). Powers the Condition global facet; NULL until a source (offer/sighting provenance, or SP-Ingest's fill-only-when-empty write) populates it — "no data" stays NULL, a synthetic `Unknown` is never written (and an existing `Unknown` counts as empty for the fill guard). Migration 091. |
| enrichment_status | String 20 | `unenriched` \| `verified` \| `web_sourced` \| `oem_sourced` \| `ai_inferred` \| `not_found` \| `not_catalogued`. Validated on write against `MaterialEnrichmentStatus` (constants.py). `oem_sourced` = single official OEM page; `not_catalogued` = recognised OEM/FRU part with no public specs (retries on 30-day backoff). No migration — varchar column. |
| cross_references | JSONB | Alternative MPNs; also records OEM FRU→commodity-MPN linkages written by the cross-ref enrichment tier (`[{"mpn": <resolved>, "manufacturer": <mfr>}]`). |
| specs_structured | JSONB | Parametric data — `{spec_key: {value, source, confidence, tier, updated_at}}`. `tier` (SP2/F2, migration 096) is the F1 ladder rank of the writing source so `record_spec` can rank conflicting writes without re-deriving; legacy entries lacking `tier` are backfilled in-memory from `source` before comparison. Source vocabulary (ladder tier): `manual` (100) · `trio_source` (95) · vendor APIs `digikey_api`\|`nexar_api`\|`mouser_api`\|… (90) · `trio_source_ai` (88) · `mpn_decode` (85) · `fru_matrix_decode` (84, FRU crosswalk intersection) · `desc_parse` (83) · `fru_desc_parse` (82, FRU-linked qual-sheet description intersection — below the card's OWN description, above the OEM scrapers) · `spec_extraction` (60, AI quality-floored at ≥ 0.85) · `legacy_backfill` (50) · `{ai_guess,claude_opus_inferred,claude_haiku}` (40); unknown sources rank 0 (once-per-source WARNING). |
| category_source | String 50, nullable | SP2/F2 (migration 096). Which source set `category` (e.g. `mpn_decode`, `digikey_api`, `claude_opus_inferred`, `legacy_backfill`; `desc_parse`/83 + `fru_desc_parse`/82 when set by the categorize-from-description channel — see APP_MAP_INTERACTIONS §desc-parse). Written only via `spec_tiers.set_category`. |
| category_confidence | Float, nullable | SP2/F2. Confidence of the source that set `category`. |
| category_tier | Integer, nullable | SP2/F2. F1 ladder rank of `category_source`. A lower-tier source can never overwrite a higher-tier category (the ladder, not write order, decides). Legacy valued-but-unprovenanced rows are backfilled to mid-tier 50 (`legacy_backfill`/0.5); `set_category` applies the SAME floor at runtime to a valued category with NULL provenance, so pre- and post-migration data rank identically. |
| category_updated_at | UTCDateTime, nullable | SP2/F2 (migration 096). When the category was last (re)written through the ladder — the tie-break timestamp for `set_category` (never borrowed from the card-wide `updated_at`). NULL for legacy rows (ranks as ""). |
| enriched_at | UTCDateTime, nullable | When the first-pass card enrichment (description/category/lifecycle) ran; NULL = not yet run |
| specs_enriched_at | UTCDateTime, nullable, indexed | When the second-pass structured-spec extraction ran; NULL = spec pass not yet run |
| enrich_requested_at | UTCDateTime, nullable, indexed | Worker priority-lane stamp (migration 099, on-add enrichment). Set ONLY by `POST /api/materials/add` (a user is actively waiting on the card); bulk/stock/email/search creation never stamps. The worker's `select_batch` orders `ASC NULLS LAST` first (stamped-first FIFO — the old leading `IS NOT NULL DESC` term was dropped as redundant in migration 105 so the ORDER BY matches `ix_mc_demand_queue`); `run_one_batch` clears the stamp on every batch card pre-await so a terminal `not_found` card can't pin the lane. It also drives the lane split: stamped = priority lane (`enrich_card(full_pipeline=True)`), NULL = bulk lane (`full_pipeline=False`, paid tiers skipped). |
| sourced_qty_90d | Integer, nullable | Migration 105. Units TRIO sourced for this MPN in the trailing 90 days of its SFDC Weekly Export (`LSC1__Material__c.Sourced_Qty_Last_90_Days__c`). PRIORITIZATION SIGNAL ONLY — never displayed as a fact, so it bypasses the F1 ladder. Backfilled one-shot by `app/management/import_demand_telemetry.py` (dry-run by default; `--apply` to write; NO recurring refresh — re-run only when a new export lands). NULL = no telemetry row matched this card's `normalized_mpn`. Primary demand key in `select_batch` + `enrich_pending_specs` ordering (`DESC NULLS LAST`). |
| last_sourced_at | UTCDateTime (TIMESTAMPTZ), nullable | Migration 105. Most-recent sourcing-event timestamp from the same export (`Most_Recent_Source_TS__c`). Secondary demand tiebreak after `sourced_qty_90d` (`DESC NULLS LAST`). Prioritization signal only; bypasses the F1 ladder; backfilled with `sourced_qty_90d` by `import_demand_telemetry.py`. |
| validation_conflicts | JSONB, nullable | List of conflicts where a tier≥80 authoritative source contradicted a `manual` (tier 100) value — the ladder KEPT the manual value, `spec_tiers.record_validation_conflict` persisted the contradiction. Entries: `{"key": <spec_key\|"category"\|"brand"\|"manufacturer">, "manual": {value, updated_at}, "evidence": {source, tier, confidence, value, observed_at}}`; de-duped per `(key, evidence.source)`, newest evidence replaces. Cleared per-key by a PUT re-assertion of the field or the conflict-accept route. Migration 099. |
| has_validation_conflict | Boolean NOT NULL default false | `true` iff `validation_conflicts` is non-empty — the "Needs review" review-queue filter predicate (`has_validation_conflict=true` on the faceted route). Migration 099. |
| search_vector | TSVECTOR | Trigger-maintained FTS (weighted: MPN=A, manufacturer=B, description/category=C) |

> **Startup backfill:** `_backfill_material_cards()` in `startup.py` runs at boot to ensure every MPN in requirements has a corresponding material card.

> **Indexes & Triggers:**
> - `trig_material_cards_search_vector` — PostgreSQL trigger maintains `search_vector` TSVECTOR on INSERT/UPDATE (weighted: display_mpn=A, manufacturer=B, description/category=C)
> - `ix_material_cards_search_vector` — GIN index for fast full-text search via `plainto_tsquery()` + `ts_rank()`. Owned by migration `eabe89205d07`, which is still on the active mainline and creates it on every fresh replay; migration 098 also creates it `IF NOT EXISTS` because the live DB was provisioned by stamping the revision history past `eabe89205d07` without executing it (its trigger/function are likewise absent on live — FTS there is maintained by `startup.py`'s `trg_mc_fts`), so the index was missing on live out-of-band.
> - `ix_material_cards_trgm_mpn` — pg_trgm GIN index on `display_mpn` for typo-tolerant search. Owned by `eabe89205d07`; 098 creates it `IF NOT EXISTS` for the same live-only gap.
> - `ix_material_cards_enrich_requested_at` — btree on the priority-lane stamp (worker `select_batch` ordering; migration 099)
> - `ix_material_cards_needs_review` — PARTIAL index `(has_validation_conflict) WHERE has_validation_conflict` backing the review-queue filter (conflicted cards are a tiny minority; migration 099)
> - `ix_mc_demand_queue` (**PostgreSQL only**, migration 105) — PARTIAL expression btree whose key order is the EXACT `select_batch` ORDER BY: `(enrich_requested_at ASC, (enrichment_status = 'unenriched') DESC, sourced_qty_90d DESC NULLS LAST, last_sourced_at DESC NULLS LAST, id) WHERE deleted_at IS NULL AND is_internal_part IS false`. Turns the per-loop-tick (~30s) worker-queue scan from a ~740k-row top-N heapsort into an ordered Index Scan + LIMIT (verified on a scratch PG 16 at live volume). The DESC-NULLS-LAST keys are not valid SQLite index DDL, so the index is **NOT** created on SQLite (the test engine queries the same shape unindexed) and is deliberately **NOT** declared on the model (migration-owned, like the 098 perf indexes).
> - **Migration 098 (`098_materials_perf_idx`)** — post-ingest (743k rows) faceted-page indexes, each justified by a measured `EXPLAIN (ANALYZE, BUFFERS)` seq-scan plan:
>   - `ix_mc_order_live` — partial btree `(search_count DESC, created_at DESC) WHERE deleted_at IS NULL` (default page order/pagination)
>   - `ix_mc_cat_order_live` — partial expression btree `(lower(btrim(category)), search_count DESC, created_at DESC) WHERE deleted_at IS NULL AND lower(btrim(category)) IS NOT NULL` (commodity-scoped pages, counts, commodity tree)
>   - `ix_mc_trgm_norm_mpn` / `ix_mc_trgm_manufacturer` / `ix_mc_trgm_description` — pg_trgm GIN on `normalized_mpn`/`manufacturer`/`description`; together with the two `eabe89205d07` indexes above they let the OR'd ILIKE/FTS `q=` paths BitmapOr (every OR branch must be indexed)
>   - `ix_mc_has_datasheet` — partial btree `(id) WHERE datasheet_url IS NOT NULL`
>   - `ix_mc_has_crosses` — partial btree `(id) WHERE cross_references IS NOT NULL AND cross_references::text NOT IN ('[]','null','')`; paired with `stx_mc_crosses_text` extended statistics on `(cross_references::text)` (without expression stats the planner guesses ~98.5% selectivity for the NOT IN — every ingested row holds a non-NULL `'[]'` — and skips the index)
>   - `ix_mc_last_searched` — partial btree `(last_searched_at) WHERE last_searched_at IS NOT NULL` (`searched_within` buckets)
>   - Plain (non-CONCURRENT) builds per repo alembic pattern: each takes a write-blocking ShareLock on `material_cards` (~25s total for the migration on the live-size heap); reads unaffected.
>   - Downgrade drops only the eight 098-owned indexes + the statistics object; the two `eabe89205d07`-owned names survive (only that revision's own downgrade may remove them).

**`material_vendor_history`** — Which vendors sell which parts (deduplicated)

**`material_card_audit`** — Audit trail for card lifecycle events (actions: created, linked, unlinked, deleted, merged, healed, restored, soft_deleted, plus `categorized` — written by `app/management/categorize_from_desc.py` when the categorize-from-description channel sets a previously-NULL category, `details` carrying the resulting category/source/tier/channel)

**`material_price_snapshots`** — Historical pricing data points

**`customer_part_history`** — What parts each customer has bought (for proactive matching). The `source` column now takes the value `"buy_plan"` for rows written by `record_buyplan_purchase_history` (buy-plan completion hook); prior values (`salesforce_import`, `avail_offer`, `avail_quote_won`, `acctivate_po`) remain valid for legacy/import rows.

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
> The `mfg_model` (always) and `drive_pn` (gated by `settings.fru_crosswalk_drive_pn_decode_enabled`)
> edges feed the FRU crosswalk DECODE channel (`fru_crosswalk_enrich.py` → tier-84 category +
> deterministic-maker + specs); `mfg_model`/`drive_pn` descriptions feed the DESC channel
> (tier-82 specs). The targeted drain + dangling-card creation over these edges is
> `python -m app.management.run_fru_crosswalk [drain|create|all] [--apply]` (dry-run default;
> lenovo_ppn danglers are explicitly skipped). See APP_MAP_INTERACTIONS "FRU Crosswalk".

**`oem_crosswalk`** — permanent OEM spare→canonical-MPN web-resolution cache, incl. negative rows (migration 101)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| spare_raw / spare_norm | String 64, NOT NULL | Spare PN as displayed / `normalize_mpn_key` form (`ix_oem_crosswalk_spare_norm` — the Pass-B join key against cards' display_mpn norm) |
| vendor | String 16, NOT NULL | `hpe` \| `lenovo` (`@validates` against the classifier vocabulary with a stable lookup surface — Phase A: hpe/PartSurfer; Phase B: lenovo/PSREF) |
| status | String 16, NOT NULL | `OemCrosswalkStatus` (constants.py): `resolved` \| `no_match` — only two states; a resolver gate-fail IS no_match. Validated on write. (`ix_oem_crosswalk_status`) |
| canonical_mpn_raw / canonical_mpn_norm | String 64, nullable | The commodity MPN the spare relabels; NULL iff no_match — `ck_oem_crosswalk_status_canonical` enforces the norm leg (`ix_oem_crosswalk_canonical_norm`) |
| canonical_manufacturer | String 128, nullable | |
| title | Text, nullable | OEM page part title/description verbatim (the Pass-B title channel's input — CPU titles parse to all six cpu facets) |
| confidence | Float, nullable | Resolver confidence (>= 0.90 when resolved) |
| source_url / source_domain | Text nullable / String 128 NOT NULL default `''` | The allowlisted page the verbatim quote was taken from; no_match rows store `source_domain=''` (sentinel, never NULL — NULLs are pairwise-distinct in a UNIQUE constraint), so `uq_oem_crosswalk_edge` enforces ONE negative row per (spare_norm, vendor) |
| payload | JSON, nullable | Full raw extraction (forensics, kept for negative rows too) |
| looked_up_at | UTCDateTime, NOT NULL | Drives the negative-cache window: `resolved` rows are PERMANENT (never re-fetched); `no_match` rows block re-resolution for 90 days and are updated in place on retry |
| created_at / updated_at | UTCDateTime | |

> UNIQUE `uq_oem_crosswalk_edge` (spare_norm, vendor, source_domain) + CHECK
> `ck_oem_crosswalk_status_canonical` ((status='resolved') = (canonical_mpn_norm IS NOT NULL)).
> Written by the enrichment worker's paced Pass-A resolution
> (`enrichment_worker/oem_crosswalk_resolver.py` — Claude-grounded, NO direct HTTP to
> PartSurfer/PSREF) and `python -m app.management.backfill_oem_crosswalk` — BOTH through the single
> `oem_crosswalk_enrich.apply_resolution` row writer (the keeper of the nullability invariant and
> the `''` sentinel; clamps LLM strings to column widths); read by
> `app/services/oem_crosswalk_enrich.py` (the deterministic tier-80 partsurfer/psref writer pass).

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
- New `ActivityType` values (app/constants.py): `WECHAT_MESSAGE` (written by `log_outreach_initiated()` when channel=wechat). Companion `Channel` enum adds `WECHAT` alongside existing phone\|email\|teams values.
- Click-to-contact events (channel phone\|email\|teams\|wechat) are written by `log_outreach_initiated()` in `app/services/activity_service.py` — maps channel → activity_type (phone→call_logged, email→email_sent, teams→teams_message, wechat→wechat_message), direction=outbound, is_meaningful=True; bumps `companies.last_activity_at` and `customer_sites.last_activity_at`.

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

**`alert_seen`** — Per-user read-state for the cross-app nav alerts (migration 117). One row records that a user has SEEN one alert item. FYI alert counts EXCLUDE seen items (seeing drains the badge); ACTION alert counts ignore this table for counting and use it only to suppress re-pulsing an already-viewed row. Written/read only via `app/services/alerts/` (`record_seen` / `seen_ref_ids`). See APP_MAP_INTERACTIONS § Cross-app alerts.
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| user_id | FK -> users (CASCADE) | |
| alert_kind | String 40 | `AlertKind` value (offer_confirmed\|inbound_customer\|inbound_vendor\|buyplan_action) |
| ref_id | Integer | Source item's id (offer.id, activity_log.id, buy_plan(_line).id — no DB-level FK, kind-scoped) |
| seen_at | UTCDateTime | When marked seen (Python default) |

> UNIQUE `uq_alert_seen_user_kind_ref` (user_id, alert_kind, ref_id) — `record_seen` is an idempotent check-then-insert with an `IntegrityError` fallback for the concurrent case. Index `ix_alert_seen_user_kind` (user_id, alert_kind) backs `seen_ref_ids`.

**`email_intelligence`** — Classified inbox emails (offer, stock_list, ooo, spam)

**`knowledge_entries`** — Q&A, facts, AI insights linked to entities

---

### Enrichment

**`enrichment_jobs`** — Batch enrichment tracking
**`enrichment_queue`** — Proposed field changes awaiting review
**`email_signature_extracts`** — Parsed email signatures (unique by sender_email)
**`prospect_contacts`** — Web-found contacts awaiting import
**`prospect_accounts`** — Discovered prospect companies (unique by domain)

Key columns:
| Column | Type | Notes |
|---|---|---|
| trio_match_score | Integer | default 0, indexed; AI procurement-fit score (0-100); 0 until screened (SP3) |
| opportunity_score | Integer | default 0, indexed; AI opportunity size score (0-100); 0 until screened (SP3) |
| swept_from_owner_id | INT FK users (SET NULL) | owner whose account was auto-swept by the daily 90-day sweep (SP4) |
| swept_at | UTCDateTime | when the account was swept into the pool (SP4) |
| parked_by_id | INT FK users (SET NULL) | user who manually parked the account via the sales-park flow (SP4) |

`enrichment_data['ai_screen']` (JSONB) holds the full AI screen verdict:
`{trio_match_score, opportunity_score, excess_likelihood, verdict, rationale, evidence, confidence, model, screened_at, grounding_fingerprint, needs_more_enrichment?}`.
`grounding_fingerprint` (SHA-256 of the assembled context) drives cache invalidation — a
re-screen with materially new grounding produces a different hash and bypasses the cached verdict.
Verdict values: `pass`, `screened_out`, `insufficient_data`, `disabled`, `cap_reached`, `error`.

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

SP4 Account Reclamation config keys (sourced from `.env` / `app/config.py`):
| Key | Type | Default | Description |
|---|---|---|---|
| prospecting_resurface_days | int | 180 | days before a dismissed prospect can resurface |
| account_sweep_enabled | bool | False | enable/disable the daily 90-day hardline sweep |
| account_sweep_inactivity_days | int | 90 | days of inactivity before an account is swept into the pool |
| account_sweep_manager_email | str | "" | CC email for sweep digest notifications (blank = no digest) |
| account_reactivation_sweep_enabled | bool | True | enable/disable auto-surface of past-customer unassigned accounts |

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
**`reconcile_runs`** — Durable per-run tallies for `reconcile_decoded_facets` (migration 104)
**`facet_audits`** — Per-row facet-accuracy verdicts (migration 104)

> **Trust telemetry tables (trust architecture §1.2, migration 104):** `reconcile_runs` persists one row per `app/management/reconcile_decoded_facets.py` execution — `mode` ('dry-run'|'apply'), `sources`/`keys` (JSONB lists — the run scope), `by_class` (JSONB `{failure_class: {action: count}}`) and `totals` (JSONB `{cards, facets, corrected, deleted, unchanged, skipped, failed}`), indexed on `ran_at`. Both prior reconcile rounds' apply tallies were runtime-log-only and are unrecoverable; every run (dry-run AND apply) now leaves a queryable row, written via `record_reconcile_run` (flush-only; the CLI owns the commit — a dry-run commits the report row AFTER its facet-write rollback). `facet_audits` stores one verdict per audited facet row for the volume-weighted accuracy audits — `card_id` (no FK, survives card deletion), `category`/`spec_key`/`value`/`source`, and `verdict` (`correct`|`wrong`|`unverifiable`), indexed on `audited_at`, `card_id`, and `(category, spec_key)`. The closed verdict vocabulary is pinned at the DB level by `CHECK ck_facet_audits_verdict` AND the model's `@validates("verdict")` (the model guard only catches ORM writers; the CHECK catches everything). `facet_audits` lands in this migration so the Phase-2.2 audit harness needs no second one. Models: `app/models/telemetry.py` (`ReconcileRun`, `FacetAudit`). Downgrade drops both tables (telemetry, not source data — acceptable loss on rollback).

> **Brand canonicalization (OPTIMIZATION_PLAN §1.5B, migration 106 — data-only on the `manufacturers` lookup table):** the live brand facet wasted 7 of its top-20 slots on duplicates: the HPE family split four ways (Hewlett Packard Enterprise / HP / HPE / HEWLETT PACKARD — selecting "HP" silently missed the ~4,400 HPE-labeled cards) and `Texas Instruments (TI)` duplicated `Texas Instruments`. Migration 106 (1) renames the canonical `Hewlett Packard Enterprise` row to `HPE` and merges its alias list to `["Hewlett Packard Enterprise", "HP", "Hewlett Packard", "Hewlett-Packard"]` (matching the updated `startup._seed_manufacturers` seed — defensive against the seed/migration race: if a fresh `HPE` row already exists, the legacy long-name row is DELETEd and the survivor's aliases reasserted) and (2) adds the `Texas Instruments (TI)` alias to `Texas Instruments`. The Dell family (Dell Technologies / DELL / Dell) needs no table change — the existing `Dell Technologies` row's `Dell` alias already folds both case variants (lookup is case-insensitive). Downgrade restores the prior canonical name + alias lists exactly. This migration corrects the lookup VOCABULARY only; the per-card `material_cards.manufacturer`/`brand` value rewrites (folding live duplicates + NULLing the "(TP,F)" ingest-leak fragments) are an operator action via `app/management/normalize_manufacturers.py --apply` (dry-run gated), run post-deploy — NOT part of the migration.

> **Facet provenance (SP2/F2, migration 096):** each facet row carries `source` (String 50, nullable), `confidence` (Float, nullable), and `tier` (Integer, nullable), set by `record_spec` to mirror the winning `specs_structured` entry on every write that wins the F1 ladder (a losing write never mutates the facet). Legacy rows are backfilled from the matching `material_cards.specs_structured -> spec_key` JSONB entry (PG-only backfill in migration 096; tier computed via a `CASE` snapshot of the `SOURCE_TIER` map — a sync test pins the snapshot against the live ladder).

> **Seed source of truth:** `app/data/commodity_seeds.json` (loaded by `commodity_registry.py`). `seed_commodity_schemas()` only INSERTs missing `(commodity, spec_key)` pairs at boot — it never updates an existing row — and `reseed_changed_schemas()` (also run at boot, right after the inserter) reconciles rows whose seed definition drifted via delete-then-reinsert. Net-new spec keys on already-seeded commodities therefore reach an existing DB automatically; *removing* a seed never deletes its DB row and needs a data migration (e.g. `093_normalize_legacy_categories` retiring `connectors/series` after the 2026-06-09 taxonomy expansion replaced it with `rows`). Two tree keys are declared coarse buckets with NO parametric seeds (`COARSE_BUCKETS_WITHOUT_SEEDS` = `ics_other`, `oem_assemblies`) — they bucket generic ICs and whole OEM assemblies, which have no honest parametric vocabulary. `tape_drives` (Storage & Drives) is fully seeded (drive_type/interface primary, form_factor, native_capacity_gb, encryption).
>
> **Canonical filter values:** for a fixed-vocabulary enum (non-empty `enum_values`), `get_subfilter_options()` renders the full declared list — unstocked values still show with a `(0)` count. Open-vocabulary enums (no `enum_values`, e.g. motherboard `chipset`) render top-N observed values behind a typeahead. Booleans always offer Yes/No.
>
> **Category canonicalization:** `app/services/category_normalizer.py` maps free-text `material_cards.category` variants (e.g. `connectors, interconnects` → `connectors`) to the canonical commodity keys the faceted sidebar buckets on — including the globally-unambiguous TRIO SFDC part-master `Commodity_Code__c` codes (`Main Board`→`motherboards`, `Hard Drive`→`hdd`, `LCD`/`LCD ASSY`→`displays`, `PSU`→`power_supplies`, `Graphics Card`→`gpu`, `Tape Drive`→`tape_drives`, `IC`/`Integrated Circuits (ICs)`→`ics_other`, `OEM ASSY`→`oem_assemblies`). Source-scoped codes that are only unambiguous inside TRIO's export live in `TRIO_SFDC_COMMODITY_CODES` (bare `Memory`→`dram` — supplier taxonomies use "Memory" for flash/EEPROM/SRAM too) and resolve only through `normalize_trio_category()` (the SFDC ingest entry point; falls back to the global map); the global `normalize_category()` never consults them. Forward hook at the three card category write sites; one-off backfill via `scripts/normalize_categories.py --dry-run|--apply`. Ambiguous strings are left untouched. Legacy rows already in the DB were normalized once by data migration `093_normalize_legacy_categories` (case-insensitive rewrite through a frozen snapshot of the full alias vocabulary, incl. `memory`→`dram` — safe because every existing row carries TRIO provenance; downgrade is a documented no-op for categories; migration `096_spec_provenance` (SP2) was re-parented onto `095_wechat_id`, keeping a single linear head). Because 093's snapshot is frozen, an alias added later only covers NEW writes: `tests/test_category_normalizer.py::test_runtime_aliases_are_backfilled_by_093_or_documented` fails CI unless every post-093 alias is registered with its own backfill (first instance: the four 2026-06-10 distributor-taxonomy aliases `hard drives`/`internal hard drives`→`hdd`, `memory module`/`memory modules`→`dram`, backfilled by data migration `100_taxonomy_alias_backfill`), and the boot-time residue check (`startup._warn_non_canonical_categories`) WARNs every boot with count + worst offenders whenever any `material_cards.category` falls outside the canonical keys (such rows are invisible to all commodity browsing).
>
> **Deterministic MPN decode (Phase 1 of MPN→spec enrichment):** `app/services/mpn_decoder/` reads facet specs straight from standard manufacturer drive/SSD/DIMM part numbers (HDD: Seagate/WD/Toshiba/HGST in `storage.py`; SSD: Samsung/Micron/Intel-Solidigm/Kioxia/WD in `ssd.py`; DRAM: Samsung/Hynix/Micron/Kingston/Crucial in `memory.py`) — zero network/LLM, strict per-vendor regex gates that require the full family structure (e.g. Toshiba `^(MG|MN|MD|MQ|DT)\d{2}[A-Z]{3}`, so short OEM spares like Dell DPNs don't false-match; HGST `HUS` requires a digit next so the HUSMM/HUSSL SAS-SSD families don't misdecode as 3.5" HDDs), unrecognized schemes skipped. DRAM modules additionally decode `rank` (enum 1Rx4/1Rx8/2Rx4/2Rx8/4Rx4/8Rx4 — 8Rx4 is emittable via the Hynix device-count math but no shipping part exercises it), `registered` (Registered/Unbuffered/Load-Reduced) and `voltage` (numeric V: 1.2/1.35/1.5; DDR5 1.1 V deliberately omitted) where the org block pins them — all three are seeded `dram` spec schemas in `commodity_seeds.json`, and `tests/test_mpn_decoder_seed_sync.py` pins decoder↔seed sync so `record_spec` never silently drops decoder output; SSD NVMe `interface` is emitted only when the family pins the PCIe generation (the seeded enum has no bare "NVMe"). The full vendor/scheme inventory table lives in APP_MAP_INTERACTIONS.md. The worker second pass (`mpn_decoder/writer.py::decode_and_record_specs`, gated by `settings.mpn_decode_enabled`, default on) writes via `record_spec(source="mpn_decode", confidence=0.95)`, then the deterministic description→spec pass (`app/services/desc_extractor/`, `source="desc_parse"`, confidence 0.90, gated by `settings.desc_parse_enabled`), then the AI spec pass. **As of SP2 the F1 tier ladder — not run order — is authoritative:** `mpn_decode` is tier 85 > `desc_parse` 83 > AI `spec_extraction` 60, so a later lower-tier pass can never clobber a decode value regardless of which ran first (the old "decode runs BEFORE the AI pass" run-order band-aid and the desc writer's confidence pre-gate are gone — `record_spec` arbitrates). Category handling: the decode's commodity is written via `spec_tiers.set_category` (tier 85), which corrects a lower-tier category (e.g. an `ai_guess`/40 misfile) but never overwrites a TRIO-source (95), vendor-API (90), or manual (100) category — a ladder loss against a *different* existing category is counted in the returned stats (`skipped_category_conflict`, INFO-logged by the worker every batch) and WARNed with the `(card_category -> decoded_commodity)` pairs, since a recurring pair signals a missing `CATEGORY_ALIASES` entry; a card with NO category is **categorized from the decode** (the regex-gated commodity). Each card writes inside a `db.begin_nested()` SAVEPOINT so a single DB failure can't poison the shared batch transaction. Coverage dry-run + backfill: `scripts/decode_mpn_dryrun.py` (read-only by default; `--apply` backfills existing inventory in chunked commits). OEM/FRU spare numbers don't match the gates → resolved in later phases (PartSurfer cross-ref / datasheet).
