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

> **Raw-DDL index reconciliation (#464 / migration 172).** ~35 indexes that lived in the DB as raw DDL but were never declared on the ORM are now declared directly on their models, so `compare_metadata` (the `scripts/check_schema_matches_models.py` drift gate) sees them instead of flagging them as phantom `remove_index` drift. They span `companies`, `site_contacts`, `material_cards`, `activity_log`, `offers`, `requisitions`, `requirements`, `sightings`, `vendor_cards`, `vendor_contacts` and cover pg_trgm GIN (fuzzy ILIKE search), GIN on JSONB tag arrays, the FTS tsvector GIN, plain btree FK, and simple partial indexes. The DDL still lives in its original owning migration — the model declaration only makes autogenerate / the drift gate aware of the index, it does **not** create a new one. The same migration also **drops** the redundant duplicate `ix_requisitions_company_id` (the model-declared `ix_requisitions_company` from baseline `001` stays), and replaces `site_contacts.reports_to_id`'s column-level `index=True` with the explicit `ix_sc_reports_to` (migration 144's real index name). Indexes that deliberately **stay** grandfathered raw-DDL: (1) `ix_ecu_provider_month` on the orphan `enrichment_credit_usage` table (can't be reconciled without reconciling its table first), and (2) PostgreSQL-only expression / complex-partial indexes (`ix_mc_order_live`, `ix_mc_cat_order_live`, `ix_mc_has_datasheet`, `ix_mc_has_crosses`, `ix_mc_demand_queue`, and the `lower()` / `TRIM()` / `DESC NULLS LAST` ones) whose definitions can't be expressed on the model in a way that stays valid for the SQLite test engine.
>
> **#464 finish (migration 174, 2026-07-02).** The 21 unique constraints the models declared but the migration-built baseline never created now exist for real (all targets duplicate-checked clean on the live PG first), the three model-less legacy tables the old chain created — `buy_plans` (V1, superseded by `buy_plans_v3` via migration 076), `notification_engagement`, `self_heal_log` — are dropped (`IF EXISTS`; live staging already lacked them), and `material_cards.enrichment_status` carries its vocabulary comment on both model and DB. The drift gate's `_GRANDFATHERED_ADD_CONSTRAINTS` / `_GRANDFATHERED_MODIFY_COMMENT` sets are now empty and `_GRANDFATHERED_REMOVE_TABLES` holds only `_sp1_desc_backup` (migration 091 downgrade path) and `enrichment_credit_usage` (empty + unreferenced; kept pending an explicit product decision).

### Auth & Users

**`users`** — Application users (Azure AD OAuth)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| email | String 255, unique | |
| name | String 255 | |
| role | String 20 | buyer\|sales\|trader\|manager\|admin |
| is_active | Boolean NOT NULL (server_default true) | Migration 149 hardened it (was nullable): a NULL `is_active` made `require_user` 403-lock the user. `companies.is_active` + `customer_sites.is_active` were hardened identically in the same migration (backfill NULL→true + NOT NULL + server_default), closing the same silent-vanish/lockout class as migration 139 did for `site_contacts`. |
| azure_id | String 255, unique | |
| refresh_token | EncryptedText | For Graph API offline access |
| access_token | EncryptedText | |
| token_expires_at | DateTime | |
| m365_connected | Boolean | Graph API health |
| commodity_tags | JSON | User specialties |
| timezone | String 100 | **Graph mailbox zone** (Windows format, e.g. "Pacific Standard Time") from `/me/mailboxSettings`, used for RFQ send-window scheduling. NOT a valid IANA name — do not use it for timestamp display; use `display_timezone` instead. |
| display_timezone | String 64, nullable | Migration 181. **Per-user DISPLAY timezone** — an IANA zone name (e.g. `America/New_York`, `Asia/Tokyo`) used to render stored-UTC timestamps in this viewer's own zone. Auto-detected from the browser (`Intl.DateTimeFormat().resolvedOptions().timeZone`) and posted to `POST /v2/profile/timezone` on page load when it differs from the stored value; also settable via the Profile-tab `<select>`. NULL ⇒ fall back to `app.utils.timezones.DEFAULT_DISPLAY_TZ` (`America/New_York`). Read per-request into the `current_user_display_tz_var` contextvar by `require_user`; consumed by the `\|localtime` / `\|localdate` Jinja filters and `template_env._task_due_state`. |
| eight_by_eight_extension | String 20 | Phone system |
| eight_by_eight_enabled | Boolean | default False |
| notify_buyplan_email_enabled | Boolean NOT NULL | default True; Profile-tab toggle — suppress buy-plan email notifications (migration 151) |
| notify_new_offer_alert_enabled | Boolean NOT NULL | default True; Profile-tab toggle — suppress new-offer alert notifications (migration 151) |
| can_approve_buy_plans | Boolean NOT NULL | default False (server_default `false`); migration 155. **Per-user buy-plan approval right** — admin-toggled in the Users settings tab, gates the buy-plan approve/reject action via `dependencies.require_buyplan_approver` / `can_approve_buy_plans(user)`. Role-independent: admins do NOT auto-qualify, the column is the single source of truth. Migration 155 also sweeps any legacy `role='ops'` rows to `'manager'`. |
| can_approve_qp_sales | Boolean NOT NULL | default False (server_default `false`); created as `can_approve_sales_orders` by migration 160 (QP Phase C2a), **renamed to `can_approve_qp_sales` by SP-2 migration 164**. **Per-user QP Sales-section approval right** — admin-toggled in the Users settings tab ("Approve SOs"); `routing.route_request` routes a `QP_SALES` gate to every active holder (no amount check). Role-independent (the column is the single source of truth). The admin route/handler keep their legacy `sales-order-approver` / `set_sales_order_approver` names (intentional asymmetry, SP-2 spec §13). |
| can_approve_qp_purchasing | Boolean NOT NULL | default False (server_default `false`); created as `can_approve_pos` by migration 160 (QP Phase C2a), **renamed to `can_approve_qp_purchasing` by SP-3 migration 166** (de-collided from the deal-level PO gate). **Per-user QP Purchasing-section approval right** — admin-toggled in the Users settings tab ("QP Purchasing"); `routing.route_request` routes a `QP_PURCHASING` gate to every active holder (no amount check). The admin route/handler keep their legacy `po-approver` / `set_po_approver` names (intentional asymmetry, mirrors SP-2's SO handling). |
| can_approve_purchase_orders | Boolean NOT NULL | default False (server_default `false`); migration 166 (SP-3). **Per-user purchase-order approval right** — admin-toggled in the Users settings tab ("Approve POs"); gates the per-line PO sign-off (`verify_po` Verify/Reject on a PENDING_VERIFY line — Phase 3 retired the deal-level `PURCHASE_ORDER` engine gate). `routing._eligible_approvers` still consults the column (with the limit below) for the per-line no-approver stall detectors. Role-independent (the column is the single source of truth). |
| purchase_order_approval_limit | Numeric(12,2), nullable | Migration 166 (SP-3). Optional dollar cap on the per-line PO sign-off: `verify_po` (and the `can_verify_po_line` Jinja predicate) reject a line whose `unit_cost × quantity` exceeds it. NULL = unlimited; e.g. `10000` lets this user verify only POs ≤ $10,000. |
| last_login_at | UTCDateTime, nullable | Migration 148. Stamped on every successful OAuth callback. NULL + no azure_id ⇒ an "Invited" (pre-provisioned, never-logged-in) row. |
| access_overrides | JSON, default `{}` | Migration 148. **Explicit per-user access overrides only**: `{access_key: bool}` keyed by `constants.AccessKey`. An *absent* key means "use the role default" (`constants.ROLE_ACCESS_DEFAULTS`) — the dict never stores the role default, so it stays empty until an admin grants/revokes a specific key. Read by `dependencies.user_has_access` (override wins over role default; admin → all). `ops_verification` is NOT stored here (it lives in `verification_group_members`). |
| invited_by_id | FK -> users (SET NULL), nullable | Migration 148. The admin who invited this user (set by the Users-tab invite flow); SET NULL so the row survives the inviter's deletion. |
| password_hash | EncryptedText, nullable | PBKDF2 password hash (`<salt_b64>$<hash_b64>`), encrypted at rest. Only used when password login is enabled. |
| avatar_path | String 255, nullable | Migration 156. Stored basename of the uploaded profile photo under `avatars.AVATARS_DIR` (`/app/uploads/avatars`, e.g. `user_12_a1b2c3d4.png`); set by `POST /api/user/avatar` (own-profile only, `require_user`), served by `GET /api/user/avatar/{filename}`. NULL ⇒ the shared `user_avatar` macro renders the initials fallback. |

> **`EncryptedText` columns (`refresh_token`, `access_token`, `password_hash`)** are Fernet-encrypted at rest via `app/utils/encrypted_type.py`, keyed by `SECRET_KEY` + `ENCRYPTION_SALT` (legacy static salt when unset). `SECRET_KEY`/`ENCRYPTION_SALT` are jointly load-bearing — see `STABLE.md` → *Encryption*. Rotate the salt without orphaning ciphertext via `python -m app.management.rotate_encryption_salt` (decrypt-old → re-encrypt-new, idempotent, `--dry-run`); full procedure in `docs/PRE_ROLLOUT_CHECKLIST.md` Gate 4.

**`user_admin_audit`** — Append-only trail of admin actions against users (Migration 148)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| actor_id | FK -> users (SET NULL), nullable | The admin who performed the action; SET NULL so the trail survives the actor's deletion (renders as "system"). |
| target_user_id | FK -> users (CASCADE), indexed, NOT NULL | The user acted upon; CASCADE so a user's audit rows are removed with the user. |
| action | String 32 | `constants.UserAuditAction`: invite \| role_change \| activate \| deactivate \| access_grant \| access_revoke. |
| detail | JSON, default `{}` | Action context, e.g. `{"from": "buyer", "to": "manager"}` (role change) or `{"key": "send_rfq", "value": "off"}` (access). |
| created_at | UTCDateTime, indexed | |

Written by `services.user_admin.record_user_audit` (caller commits); surfaced by the Settings > Users audit-log viewer (admin only).

---

### Core Sourcing Pipeline

**`requisitions`** — Customer requests for parts
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| name | String 255 | |
| customer_name | String 255 | |
| company_id | FK -> companies | |
| customer_site_id | FK -> companies / customer_sites | |
| status | String 50 | Sales Hub pipeline: `draft` -> `open` -> `rfqs_sent` -> `offers` -> `quoted` -> `won`/`lost`; `hotlist` (off-pipeline monitor — see Proactive); `cancelled` (retained). Enforced by `ck_requisitions_status` CHECK (migration 158): `IN ('draft','open','rfqs_sent','offers','quoted','won','lost','hotlist','cancelled')`. "open" automatically means sourcing. Legacy `active`/`sourcing`/`reopened` were remapped to `open`, `quoting` to `quoted`, and the old `archived` rows to `lost`. There is **no requisition archive/hide capability** — a requisition ends in `won` or `lost` (each carrying a required `outcome_reason`). `RequisitionStatus` (app/constants.py) is the source of truth (`TERMINAL`={won,lost,cancelled}, `OPEN_PIPELINE`={open,rfqs_sent,offers,quoted}, `MONITOR`={hotlist}). |
| outcome_reason | Text, nullable | Migration 158. The required Won/Lost close reason. Nullable at the DB level (so existing rows and non-closed reqs stay valid); enforcement is **app-side** — every transition to `won`/`lost` via `requisition_state.transition()` requires a non-empty reason or raises `OutcomeReasonRequired` (router → 400). Cleared automatically when a req is reopened off a terminal state. |
| urgency | String 20 | normal\|hot\|critical |
| opportunity_value | Numeric 12,2 | |
| win_probability | Integer, nullable | 0-100; deal win % (migration 146) |
| claimed_by_id | FK -> users | |
| created_by | FK -> users | |
| **Relationships** | requirements, attachments, contacts, offers, quotes |

> **Index note (#464 / migration 172).** `company_id` historically carried two identical btree indexes — `ix_requisitions_company` (baseline `001`, model-declared) and the redundant `ix_requisitions_company_id` (added by migration `078`, never model-declared). Migration 172 **drops the duplicate** (downgrade re-creates it); `ix_requisitions_company` stays and serves every `company_id` lookup, so it is a behavioral no-op that just sheds redundant write/maintenance cost. With it gone the grandfathered drift-allowlist entry is retired and the drift gate enforces the real schema. The `customer_name` / `name` pg_trgm GIN indexes are now ORM-declared in the same change (see the Raw-DDL index reconciliation note under "Table Overview by Domain").

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
| outcome_reason | Text, nullable | Migration 185. The per-part "why won / why lost" close reason. Set by the Sales-Hub bulk Won/Lost action (`POST /v2/partials/parts/bulk-outcome`) when a part-line is marked WON/LOST — the per-part replacement for the removed bulk Archive. Nullable at the DB level (existing/non-closed lines stay valid); enforcement is **app-side** — `bulk_outcome` (routers/htmx/parts.py) 400s on a blank reason, then stamps the reason on every selected `Requirement` it transitions to WON/LOST via the sourcing state machine (`transition_requirement`). Mirrors the requisition-level `Requisition.outcome_reason` (migration 158). |
| substitutes | JSON | Alternative MPNs |
| substitutes_text | Text, indexed (GIN) | Flattened substitute MPNs for ILIKE search (used by global search + parts list) |
| assigned_buyer_id | FK -> users, indexed (`ix_requirements_assigned_buyer`, migration `71d3fef96529`) | P3.1: had no index anywhere despite being filtered on every buyer's default sightings board (`routers/sightings.py:413,585`) and the offers alert source (`services/alerts/sources/offers.py:58-60`) |
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

**`vendor_part_unavailability`** — Durable vendor+part unavailability knowledge ("this vendor's stock of this part is gone"): one row per (normalized vendor, normalized MPN, condition) triple recording why + note + provenance. `condition` NULL means "all conditions" (catch-all); a non-NULL value (new/refurb/used) scopes the record to that condition only. Outlives scraped Sighting rows — every sighting-persistence path re-stamps fresh rows from these records, and RFQ suggestions exclude matching vendors while a NULL-condition active record exists. `Sighting.is_unavailable` is **demoted to a render cache**: the `is_active` predicate in `app/services/vendor_unavailability.py` is the single authority on every read surface (see APP_MAP_INTERACTIONS § 2d). Migrations 102 (base table) + 103 (policy/provenance columns) + 171 (condition column + partial unique indexes).

**`po_cancellations`** — Immutable, append-only one-row-per-cancelled-PO vendor-fall-down fact (migration 162). Written by `app/services/po_cancellation_service.record_po_cancellation` when a buyer re-sources a line whose cut PO the vendor cancelled. Lives OUTSIDE `buy_plan_lines` because a line is re-bound to a new vendor/offer on re-source (and can be re-sourced repeatedly) — FKs (`buy_plan_id`/`buy_plan_line_id`/`requirement_id`/`offer_id`/`vendor_card_id`) are SET NULL and the vendor key is denormalized so the fact survives all of them. Carries `po_cut_at`, `cancelled_at`, `days_to_cancel` (slow cancel = >7d, weighs the vendor score down harder) + `reason_code`/`reason_text`. Powers `vendor_cards.{cancellation_rate,avg_days_to_cancel,slow_cancel_count}` (refreshed inline + nightly) and the windowed `vendor_metrics_snapshot.{cancellation_rate,avg_days_to_cancel}`. See APP_MAP_INTERACTIONS § 6e.

Migration 162 also adds the new status value **`resourcing`** to `buy_plan_lines.status` (open claim pool — a cancelled-PO line, unassigned, awaiting a new buyer), the `vendor_cards.{avg_days_to_cancel,slow_cancel_count}` + `vendor_metrics_snapshot.avg_days_to_cancel` metric columns, and `users.notify_resource_alert_enabled` (gates the urgent re-source email + Teams DM).
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| vendor_name_normalized | String 255, not null, indexed | via `normalize_vendor_name()` (app/vendor_utils.py); @validates re-normalizes on write (empty result raises) |
| normalized_mpn | String 255, not null, indexed | via `normalize_mpn_key()` — same canonical dash-stripped key space offers use; @validates re-normalizes on write (empty result raises) |
| reason | String 32, not null | `UnavailabilityReason` StrEnum (bought_by_us\|sold_elsewhere\|broken\|not_really_there\|different_part\|other), validated on write; display text via the enum's `.label` property (single source of truth) |
| condition | String 16, nullable | 171. Condition scoping: NULL = all-conditions catch-all; else one of new/refurb/used (normalized via `normalize_condition()`). Reason→condition policy inside `record_unavailability`: condition-specific reasons (bought_by_us/sold_elsewhere/broken) store the passed condition; agnostic reasons (not_really_there/different_part/other) always store NULL |
| note | Text, nullable | free-text "what we learned" |
| created_by_id | FK -> users, SET NULL | knowledge outlives accounts |
| created_at | UTCDateTime, not null | dual default (Python + server); also the temporal-policy window anchor — re-mark refreshes it. NOT NULL so `is_active`'s None branch is provably pre-flush-only |
| qty_at_mark | Integer, nullable | 103. Per-key qty snapshot at mark/re-mark: max non-NULL `qty_available` over the vendor's sightings whose `normalize_mpn_key(mpn_matched)` equals THIS record's key (empty-MPN rows count toward the primary-key record); never cross-key. Re-mark keeps the old value when the new computation is NULL. Powers the O2 restock override; NULL ⇒ O2 never fires (fail-closed for records created before 103) |
| released_at | UTCDateTime, nullable | 103. Written ONLY by override O3 (buyer-routed vendor email) and the offer hook — both user-initiated paths, both via the model's `release()` transition; NULLed on re-mark (`re_arm()`). Non-NULL ⇒ record not active |
| release_trigger | String 32, nullable | 103. `ReleaseTrigger` StrEnum (vendor_email\|offer_received), validated on write (None allowed); advisory hint copy via the enum's `.label`. CHECK `ck_vendor_part_unavail_release_pair` enforces (released_at IS NULL) = (release_trigger IS NULL) |
| requirement_id | FK -> requirements, SET NULL, indexed | 103. Provenance: the requirement the mark was made from (refreshed on re-mark). SET NULL, not CASCADE — knowledge outlives requirements. Widens `clear_unavailability`'s delete predicate so a record whose key no longer matches the requirement's current keys is still clearable (zombie-record fix) |

> Two partial unique indexes replace the old single unique constraint: `uq_vpu_vendor_mpn_condition` UNIQUE (vendor_name_normalized, normalized_mpn, condition) WHERE condition IS NOT NULL; `uq_vpu_vendor_mpn_allcond` UNIQUE (vendor_name_normalized, normalized_mpn) WHERE condition IS NULL. Together they ensure at most one record per (vendor, mpn, condition) triple and at most one all-conditions catch-all per (vendor, mpn) — marking again is an upsert (re-arm), never a duplicate. Written and read only via `app/services/vendor_unavailability.py` (record/clear/apply/release/exclude) and `app/services/sighting_status.py` (reader-authority status branch).

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
| status | String 50, default `new` | new\|parsed\|reviewed\|rejected\|flagged (VendorResponseStatus — the review queue) |
| message_id | String 255, unique | |

**`requisition_attachments`** — Files attached to a requisition (Migration 126: renamed `onedrive_item_id`→`library_item_id`, `onedrive_url`→`library_web_url`; added `library_drive_id`)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| requisition_id | FK -> requisitions (CASCADE), indexed | |
| file_name | String 500, not null | |
| library_item_id | String 500, nullable | Graph item id (`NULL` = not yet uploaded) |
| library_drive_id | String 200, nullable | `NULL` → user OneDrive fallback; non-NULL → company SharePoint library |
| library_web_url | Text, nullable | Shareable URL |
| thumbnail_url | Text, nullable | |
| content_type | String 100, nullable | |
| size_bytes | Integer, nullable | |
| uploaded_by_id | FK -> users (SET NULL) | |
| created_at | UTCDateTime | |

**`requirement_attachments`** — Files attached to a requirement line (Migration 126: same column renames + `library_drive_id` as above)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| requirement_id | FK -> requirements (CASCADE), indexed | |
| file_name | String 500, not null | |
| library_item_id | String 500, nullable | |
| library_drive_id | String 200, nullable | `NULL` → user OneDrive; non-NULL → company SharePoint library |
| library_web_url | Text, nullable | |
| thumbnail_url | Text, nullable | |
| content_type | String 100, nullable | |
| size_bytes | Integer, nullable | |
| uploaded_by_id | FK -> users (SET NULL) | |
| created_at | UTCDateTime | |

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
| status | String 20 | pending_review\|active\|approved\|rejected\|sold\|won\|expired (OfferStatus) |
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

> **Migration 157 (`157_qp_approvals`)** — adds 6 new nullable columns to `offers`: `is_primary` (Boolean default false), `sourcing_type` (String 50; `SourcingType` enum), `vendor_rating` (Numeric 3,1), `terms` (JSON), `location` (String 255), `specifics` (Text). See also `###Approvals Engine & QP` section below.
>
> **Migration 188 (`188_canonical_offers_excess_fk`)** — converges the FK on `offers.excess_line_item_id -> excess_line_items` onto its single canonical name `offers_excess_line_item_id_fkey` (the PostgreSQL default the unnamed model `ForeignKey` produces). Migration `d1a2b3c4e5f6`'s replay guard checked `pg_constraint` for its OWN name `fk_offers_excess_line_item_id` — which baseline 001 never creates — so a fresh-chain replay left TWO identical FKs on the column (invisible to the drift gate: alembic compares FKs by signature, not name). 188 drops the stray when both exist, RENAMEs it when it's the only one, and no-ops otherwise; the `d1a2b3c4e5f6`/`5c6736d6381f` guards are now COLUMN-scoped so the duplicate can't be recreated. PostgreSQL-only (dialect-guarded no-op on SQLite); irreversible cleanup → documented no-op downgrade.

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
| status | String 20 | draft\|sent\|won\|lost\|revised (QuoteStatus, validated on write) |
| result | String 20 | won\|lost |
| won_revenue | Numeric 12,2 | |
| sent_at | UTCDateTime | Set when the quote is emailed |
| graph_message_id | String 255, nullable | Migration 153 — Graph id of the outbound quote email (reply threading) |
| graph_conversation_id | String 255, nullable | Migration 153 — Graph conversation id; NULL-safe when the Sent-Items lookup misses |

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

**`quote_requisitions`** — Join table linking a quote to EVERY requisition it draws lines from (Migration 175, OQ-02). A combined quote spans 2+ requisitions selected together in the list "Build Quote" flow; `Quote.requisition_id` stays the PRIMARY/anchor while one row here per contributing requisition (primary included) makes the full membership queryable. Invariant: every quote has ≥1 join row (its primary self-row) — existing quotes were backfilled by 175, and every NEW quote gets its self-row via the `Quote` `after_insert` listener (`app/models/quotes.py`), so ANY creation path (builder, revise, proactive, offers, CRM) is visible on its requisition. The single arbitration point for reads/writes is `app/services/quote_requisitions.py` (`quotes_for_requisition` replaces the old `Quote.requisition_id == req_id` filter so secondary reqs also surface the combined quote).
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| quote_id | FK -> quotes (CASCADE), indexed (`ix_quote_requisitions_quote`) | |
| requisition_id | FK -> requisitions (CASCADE), indexed (`ix_quote_requisitions_req`) | |
| created_at | UTCDateTime | |
| | | `uq_quote_requisition` unique on (quote_id, requisition_id) |

**`offer_attachments`** — Files attached to a vendor offer (Migration 126: renamed `onedrive_item_id`→`library_item_id`, `onedrive_url`→`library_web_url`; added `library_drive_id`)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| offer_id | FK -> offers (CASCADE), indexed (`ix_offer_attachments_offer`) | |
| file_name | String 500, not null | |
| library_item_id | String 500, nullable | |
| library_drive_id | String 200, nullable | `NULL` → user OneDrive; non-NULL → company SharePoint library |
| library_web_url | Text, nullable | |
| thumbnail_url | Text, nullable | |
| content_type | String 100, nullable | |
| size_bytes | Integer, nullable | |
| uploaded_by_id | FK -> users (SET NULL) | |
| created_at | UTCDateTime | |

---

### Buy Plans (Fulfillment)

**`buy_plans_v3`** — Purchase fulfillment after quote acceptance
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| quote_id | FK -> quotes (CASCADE), **nullable** | NULL for Sales-Order-origin buy plans built directly from RFQ offers with no customer quote (SP-2 migration 163); set when the plan derives from an accepted customer quote. |
| requisition_id | FK -> requisitions (CASCADE) | |
| sales_order_number | String 100 | |
| customer_po_number | String 100 | |
| status | String 30 | `BuyPlanStatus`: draft -> pending -> active -> completed (also halted / cancelled). The `inbound` enum member remains for historical rows only — Phase 3 retired the deal-level PO gate and its receiving step (no code path writes or reads `inbound` any more). |
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

### Approvals Engine & Quality Plans (Migration 157)

> **Migration 159 (`159_approval_subject_poly`)** — foundation cleanup before QP Phase C:
> (1) `approval_requests` swaps the two nullable subject FK columns
> (`subject_quality_plan_id`/`subject_prepayment_id` + their indexes) for a **polymorphic
> `(subject_type, subject_id)` pair** (no cross-table FK; `ApprovalSubjectType` enum,
> composite index `ix_approval_req_subject`); (2) `approval_outbox.channel` server_default
> flips `email` → `in_app`; (3) `approval_events.note` (dead) is dropped — `payload` is the
> comment sink. Reversible (the downgrade reconstructs the FK columns + indexes, EXISTS-
> guarding the backfill so a since-deleted subject downgrades to a NULL FK instead of
> violating it).

**`approval_gate_configs`** — Per-gate configuration: which user is the approver and up to what amount.
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| gate_type | String 50 | `ApprovalGateType` enum: `buy_plan`\|`prepayment`\|`qp_sales`\|`qp_purchasing`\|`purchase_order` |
| approver_user_id | FK -> users (CASCADE) | |
| max_amount | Numeric 12,2, nullable | NULL = applies to any amount |
| active | Boolean NOT NULL | server_default true; only one active row per gate type expected |

**`quality_plans`** — QC documentation per buy-plan order.
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| buy_plan_id | FK -> buy_plans_v3 (CASCADE) | |
| vendor_card_id | FK -> vendor_cards (SET NULL), nullable | |
| status | String 50 NOT NULL | `QualityPlanStatus`: `draft`\|`in_review`\|`approved`\|`rejected` |
| order_type | String 20 NOT NULL | `QPOrderType`: `new`\|`revision` |
| inspection_level | String 50, nullable | e.g. "AQL 1.5" |
| sampling_rate | String 50, nullable | |
| notes | Text, nullable | |
| sales_* (17 cols) | String 255 / Text / Integer / Boolean, all nullable | § Sales "Quality Questions" (QP Phase C2b): `sales_condition`, `sales_quantity` (Int), `sales_fw_hw_rev`, `sales_product_commodity`, `sales_testing_required`/`_option`/`_specifics`, `sales_test_location`, `sales_serial_preapproval_required`, `sales_authorized_ship_early`/`_partial`, `sales_routing_prescreening_whs`, `sales_vendor_rating`, `sales_third_party_pkg_ok`, `sales_pkg_requirements`, `sales_bom_matrix_links`, `sales_notes` (Boolean for Y/N). Completeness gate enforces the required subset at submit, not the DB. The canonical SO# now lives on `buy_plans_v3.sales_order_number` (SP-2 migration 164 retired `sales_so_number` from the QP). |
| purchasing_* (10 cols) | String 255 / Text / Boolean, all nullable | § Purchasing "Quality Questions" (C2b): `purchasing_po_number`, `purchasing_condition`, `purchasing_fw_hw_rev`, `purchasing_product_commodity`, `purchasing_testing_required`/`_option`, `purchasing_routing_prescreening_whs`, `purchasing_packaging`, `purchasing_tpo_ship_complete` (Bool), `purchasing_tpo_notes`. |
| sales_section_reviewed_at | UTCDateTime, nullable | Phase 3 (migration 177, renamed from `sales_section_approved_at`): stamped by `toggle_section_reviewed(mark)` when the sales section is marked reviewed; cleared on `unmark`. The section is now a lightweight per-section review toggle, not an approval gate. |
| purchasing_section_reviewed_at | UTCDateTime, nullable | Ditto for the purchasing section (renamed from `purchasing_section_approved_at`). |
| sales_section_reviewed_by_id | FK -> users (SET NULL), nullable | Phase 3 (177): who marked the sales section reviewed. |
| purchasing_section_reviewed_by_id | FK -> users (SET NULL), nullable | Phase 3 (177): who marked the purchasing section reviewed. |
| created_by_id | FK -> users (SET NULL) | |

> Migration 177 also DROPPED the always-NULL top-level `approved_by_id`/`approved_at` columns (never written or rendered by any code path). `QualityPlanStatus.approved`/`rejected` enum values are kept but no longer reachable — `submit()`/`submit_section()` and the QP_SALES/QP_PURCHASING engine gates were retired in the same fold.

> Relationships: `serial_entries` (QpSerialEntry, delete-orphan) and `fru_lookups` (QpFruLookup, delete-orphan). Added by migration 161 (QP Phase C2b).

**`qp_serial_entries`** — Serial-preapproval tracking rows on a QP's Serial section (QP Phase C2b).
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| qp_id | FK -> quality_plans (CASCADE) | Indexed (`ix_qp_serial_qp`) |
| buyer_id | FK -> users (SET NULL), nullable | |
| submitted_by_id | FK -> users (SET NULL), nullable | Defaults to the acting user on create |
| buyer_date | Date, nullable | |
| has_sn_prev_received | Boolean, nullable | Has SN previously been received? (Y/N) |
| purchase_order / part_number / serial_number / seagate_sn / tso / customer_po | String 255, nullable | |
| submitted_to_customer_date | Date, nullable | |
| customer_approved | Boolean, nullable | Did customer approve? (Y/N) |
| customer_approved_date | Date, nullable | |
| ops_received | Boolean, nullable | OPS received (Y/N) |
| created_at | UTCDateTime | |

> Customer-side fields stay internal — the vendor share view (QP Phase C2c) excludes this whole section.

**`qp_fru_lookups`** — FRU part numbers pinned to a QP's FRU crosswalk section (QP Phase C2b).
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| qp_id | FK -> quality_plans (CASCADE) | Indexed (`ix_qp_fru_qp`) |
| fru_norm | String 64 NOT NULL | `normalize_mpn_key` of the FRU; the view live-joins `fru_links` by this key |
| created_at | UTCDateTime | |

> Unique `(qp_id, fru_norm)` (`uq_qp_fru_lookup`) — a FRU can't be pinned twice; the router checks-then-inserts so a re-pin is a no-op.

**`prepayments`** — Upfront vendor payment records linked to a buy plan.
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| vendor_card_id | FK -> vendor_cards (SET NULL), nullable | |
| buy_plan_id | FK -> buy_plans_v3 (CASCADE) | |
| buy_plan_line_id | FK -> buy_plan_lines (SET NULL), nullable, indexed | Migration 178 — the specific cut PO line prepaid; SET NULL so the record outlives a line delete |
| total_incl_fees | Numeric 12,2 NOT NULL | Inclusive of all fees |
| currency | String 10 NOT NULL | server_default USD |
| payment_method | String 20, nullable | `PaymentMethod`: `cc`\|`paypal`\|`wire` |
| vendor_name | String 255, nullable | Migration 178 — payee snapshot; `create_prepayment` derives it server-side (line's offer, else vendor card) so the approver/AP always see who is paid even if the line/offer later changes |
| test_report_sent | Boolean NOT NULL | server_default false |
| buyer_remarks | Text, nullable | |
| created_by_id | FK -> users (SET NULL) | |
| status | String 20 NOT NULL, indexed | Migration 179 — `PrepaymentStatus` lifecycle: `requested → approved → paid`, or `void`. Source of truth for the closure loop; synced at each transition (create/approve/reject/mark-paid/teardown). server_default `requested`; 179 backfills from the linked PREPAYMENT ApprovalRequest |
| approved_by_id / approved_at | FK -> users (SET NULL) / UTCDateTime, nullable | Migration 179 — stamped when the manager approves (in `prepay_request_decide`) |
| pay_token | String 64, nullable, UNIQUE | Migration 179 — single-use `secrets.token_urlsafe(32)` minted at approval; the public `/p/confirm/{token}` link in the "OK TO WIRE" email lets non-Avail accounting mark it paid. Cleared on paid/void; re-minted on manager undo |
| paid_at / paid_by_id / paid_by_label / paid_via / wire_reference / paid_amount | UTCDateTime / FK users SET NULL / String 120 / String 20 / String 120 / Numeric 12,2, all nullable | Migration 179 — set by `mark_prepayment_paid`. `paid_via` ∈ {`accounting_email`,`in_app`}; `paid_by_label` carries the accounting confirmer's initials (no User row) or the in-app user's name |
| voided_at / voided_by_id / void_reason | UTCDateTime / FK users SET NULL / String 255, nullable | Migration 179 — set on reject or teardown-void (an approved-but-unwired prepayment whose plan is cancelled/halted/completed/re-sourced → `void` + a "DO NOT WIRE" stand-down notice to accounting/AP) |

**`approval_requests`** — Root record for one approval workflow instance.
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| gate_type | String 50 NOT NULL | `ApprovalGateType` |
| status | String 50 NOT NULL | `ApprovalRequestStatus`: `requested`\|`approved`\|`rejected`\|`cancelled`\|`expired` |
| amount | Numeric 12,2, nullable | Spend amount for gate decisions |
| currency | String 10, nullable | |
| requested_by_id | FK -> users (SET NULL) | |
| owner_id | FK -> users (SET NULL) | Indexed (`ix_approval_req_owner`) |
| subject_type | String 50, nullable | `ApprovalSubjectType` (`quality_plan`\|`prepayment`\|`buy_plan`; QUOTE/RESELL_OFFER added by later QP-Phase-C phases). `buy_plan` (QP Phase C1) routes the live buy-plan gate through the engine. **Polymorphic** — no cross-table FK (mirrors `MaterialCardAudit.material_card_id`); survives subject deletion. Migration 159 replaced the two nullable subject FK columns with this pair. |
| subject_id | Integer, nullable | The subject's PK. `(subject_type, subject_id)` composite-indexed (`ix_approval_req_subject`). |
| resolved_at | UTCDateTime, nullable | |
| expires_at | UTCDateTime, nullable | |

> Set by `approvals.service.create_request` from the passed subject (Prepayment → `prepayment`, QualityPlan → `quality_plan`, BuyPlan → `buy_plan`). The router `_serialize_request` JSON shape now exposes `subject_type`/`subject_id` (QP Phase C1, so a `buy_plan` request links back to its plan detail partial); the read-only buy-plan bridge `_buy_plan_as_queue_item` was retired.

**`approval_steps`** — Ordered stages within an ApprovalRequest.
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| request_id | FK -> approval_requests (CASCADE) | |
| seq | Integer NOT NULL | server_default 1 |
| rule | String 20 NOT NULL | `ApprovalStepRule`: `any`\|`all` |
| status | String 50 NOT NULL | |

**`approval_step_recipients`** — Per-user assignment within a step.
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| step_id | FK -> approval_steps (CASCADE) | |
| user_id | FK -> users (CASCADE) | |
| status | String 50 NOT NULL | `ApprovalRecipientStatus`: `pending`\|`approved`\|`rejected`\|`reassigned` |
| decided_at | UTCDateTime, nullable | |
| decision_note | Text, nullable | |
| reassigned_to_id | FK -> users (SET NULL), nullable | |
| UNIQUE | (step_id, user_id) | `uq_approval_step_recipient` |

**`approval_events`** — Immutable audit trail for state changes.
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| request_id | FK -> approval_requests (CASCADE) | |
| actor_id | FK -> users (SET NULL), nullable | |
| event_type | String 50 NOT NULL | e.g. `submitted`\|`approved`\|`step_advanced`. The genesis `submitted` row is recorded by `create_request`. |
| payload | JSON, nullable | Extra structured context — the comment sink (the decision `comment` rides here, NOT a `note` column). Migration 159 dropped the dead `note` column. |

**`approval_outbox`** — Transactional outbox for notifications.
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| request_id | FK -> approval_requests (CASCADE) | |
| recipient_user_id | FK -> users (CASCADE) | |
| channel | String 50 NOT NULL | `email`\|`in_app`; server_default `in_app` (migration 159 flipped it from `email` so the channel is never implicit-and-wrong). `decide()` enqueues BOTH an `in_app` and an `email` row per decision (Mike's locked dual-channel notice). |
| payload | JSON, nullable | |
| sent_at | UTCDateTime, nullable | NULL = not yet dispatched |
| fail_count | Integer NOT NULL | server_default 0; the drain skips rows at `MAX_OUTBOX_FAIL_COUNT` (dead-letter) — a deleted recipient or unknown channel is failed (fail_count++), never marked sent |
| last_error | Text, nullable | last failure reason; set on send error, deleted recipient, or unknown channel |

---

### CRM

**`companies`** — Customers, vendors, prospects
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| name | String 255 | |
| domain | String 255, indexed | |
| website | String 500 | |
| account_type | String 50, indexed (`ix_companies_account_type`) | Customer\|Prospect\|Partner\|Competitor. Migration 191 adds the btree index — the column is filtered in the CRM list path (`crm_service.list_companies`, `== account_type` for CDM types) and the inbound-customer alert source (`== "Customer"`), which had no index. |
| account_owner_id | FK -> users | |
| employee_size | String 50 | |
| hq_city / hq_state / hq_country | String | |
| brand_tags / commodity_tags | JSON | |
| enrichment_source | String 50 | explorium|lusha|clay|manual |
| is_strategic | Boolean | |
| sf_account_id | String 255, unique | Salesforce link |
| last_activity_at | UTCDateTime, nullable | Bumped by `log_outreach_initiated()` on every click-to-contact event; used by the CDM account workspace `staleness` sort (oldest = longest since activity first). |
| disposition | String 20, indexed | Migration 118. `active`\|`bucket` (`CompanyDisposition` StrEnum); NULL ⇒ active (mirrors `tier`'s NULL ⇒ standard). `bucket` accounts are suppressed from the "needs a call" call-list (chip COUNT + click-through) via the shared `crm_service._needs_call_filter` and from `cdm_company_query`'s base, NULL-safe (`or_(disposition != 'bucket', disposition.is_(None))`) — re-surfaced ONLY by the explicit `staleness='bucket'` facet. Set via `POST /v2/partials/customers/{id}/disposition` (owner-or-admin). NEVER overloaded onto `is_active`. |
| disposition_reason | String, nullable | Optional free-text rationale for the disposition (parity with prospect dismiss audit). |
| disposition_set_by | FK -> users (SET NULL) | Who set the disposition. |
| disposition_set_at | UTCDateTime, nullable | When the disposition was last set. |
| normalized_name | String 255, indexed (btree + Postgres GIN pg_trgm), **nullable, NOT unique** | Migration 120 (Increment 3, AI-org). Suffix-stripped/lowercased dedup match key, kept in lockstep with `name` by `Company._sync_normalized_name` (`@validates("name")`) using `vendor_utils.normalize_vendor_name` — the SAME normalizer the dedup scanner scores with. Mirrors VendorCard but is **nullable + non-unique** on purpose (companies legitimately share a normalized form across the dedup window; the policy keeps different-owner accounts separate). The `ix_companies_normalized_name_trgm` GIN index is Postgres-guarded (`dialect.name == 'postgresql'`); SQLite gets only the btree and the scanner falls back to rapidfuzz. |
| alternate_names | JSON (default []) | Migration 120. Names this company has been known by. `merge_companies` appends the loser's `name` (+ its own `alternate_names`, deduped, never keep's display name) so a re-import of the old name fuzzy-matches the survivor instead of recreating the duplicate (mirrors `VendorCard._record_alternate_name`). |
| ticker | String 20, nullable | Migration 125. Stock ticker symbol (e.g. `INTC`). Written by `apply_enrichment_to_company` via the `firmo_tiers` blending ladder; Explorium is the highest-authority source (tier 90). |
| naics | String 20, nullable | Migration 125. NAICS industry code. SAM.gov is authoritative (tier 95); Explorium second (tier 85). |
| revenue_range | String 50, nullable | Migration 125. Annual revenue band (e.g. `1000000-5000000`), formatted from a `{min, max}` range by the Explorium connector. Explorium is the highest-authority source (tier 90). |
| enrichment_provenance | JSONB, nullable, server_default `{}` | Migration 125. Per-field provenance store written by `_apply_enrichment` (enrichment_service.py). Shape: `{field: {source, tier, confidence}}`. Guards the provenance-aware overwrite rule: a field with no stored provenance is treated as manual/legacy and is never clobbered by an automated source; a field with provenance is overwritten only when the incoming (tier, confidence) pair strictly beats the stored one. |
| created_by_id | FK -> users (SET NULL), nullable | Migration 147. Set automatically by `app/audit_listeners.py` (`before_insert` event) from `current_user_id_var` on every authenticated request; NULL for background/import writes. |
| modified_by_id | FK -> users (SET NULL), nullable | Migration 147. Set automatically by `app/audit_listeners.py` (`before_update` event) from `current_user_id_var`; NULL for background/import writes. |
| **Relationships** | customer_sites, requisitions, attachments (`CompanyAttachment`), entity_tags, created_by (`User`), modified_by (`User`) | Migration 126 adds `attachments`. Migration 147 adds audit trail. |

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
| created_by_id | FK -> users (SET NULL), nullable | Migration 147. Set automatically by `app/audit_listeners.py` on authenticated request; NULL for background/import writes. |
| modified_by_id | FK -> users (SET NULL), nullable | Migration 147. Set automatically by `app/audit_listeners.py` on authenticated request; NULL for background/import writes. |
| do_not_contact | Boolean NOT NULL (server_default false) | Migration 148. When True, site is excluded from `staleness=needs_call` call-list and the DNC badge renders in `site_card.html`. Toggled via `POST /v2/partials/customers/{cid}/sites/{sid}/mark-dnc` (`can_manage_account` gate). Replaces the "Delete Site" action — DNC preserves the site record while hiding it from call surfaces. |

**`site_contacts`** — Individual people at customer sites
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| customer_site_id | FK -> customer_sites (CASCADE) | |
| full_name | String 255 | Derived field — recomposed from first_name + last_name on every form/inline-edit write (migration 134 seeded via backfill). |
| first_name | String 120, nullable | Migration 134. Editable source of truth; recomposed into full_name on write. |
| last_name | String 120, nullable | Migration 134. Editable source of truth; recomposed into full_name on write. |
| contact_owner_id | FK -> users (SET NULL), indexed | Migration 134. Override contact owner; falls back to company.account_owner when NULL. |
| email | String 255 | Unique per site |
| phone | String 100 | |
| secondary_email | String 255, nullable | Migration 144. Second email address (e.g. personal or backup). Inline-editable via `EDITABLE_CONTACT_FIELDS`. |
| secondary_phone | String 100, nullable | Migration 144. Second phone number. Inline-editable via `EDITABLE_CONTACT_FIELDS`. |
| reports_to_id | FK -> site_contacts (SET NULL), indexed | Migration 144. Self-referential org-chart link. Rendered in contact card as "Reports to: X". |
| wechat_id | String 100, nullable | WeChat handle for click-to-message outreach (migration 095_wechat_id). Written by the site-contact create form; rendered in `tabs/contacts_tab.html` as a `weixin://` deep link with `data-outreach-log`. |
| contact_role | String 50 | buyer\|technical\|decision_maker\|operations |
| do_not_contact | Boolean NOT NULL (server_default false) | Migration 116. Suppresses outreach affordances; toggled via `POST .../contacts/{id}/do-not-contact` (`_dnc_toggle.html`). |
| is_priority | Boolean NOT NULL (server_default false) | Migration 118. Surfaces the contact to the TOP of the roster (`company_contact_rows` order_by). Toggled via `POST .../contacts/{id}/priority` (`_priority_toggle.html`). Mirrors `do_not_contact`. |
| is_archived | Boolean NOT NULL (server_default false) | Migration 118. Sorts the contact to the BOTTOM of the roster but keeps it visible (NOT `is_active`, which would hide it). Toggled via `POST .../contacts/{id}/archive` (`_archive_toggle.html`). |
| email_verified | Boolean | |
| enrichment_source | String 50 | lusha|clay|hunter|explorium|manual |
| created_by_id | FK -> users (SET NULL), nullable | Migration 147. Set automatically by `app/audit_listeners.py` on authenticated request; NULL for background/import writes. |
| modified_by_id | FK -> users (SET NULL), nullable | Migration 147. Set automatically by `app/audit_listeners.py` on authenticated request; NULL for background/import writes. |
| **Relationships** | customer_site, attachments (`SiteContactAttachment`), contact_owner (`User`), reports_to (`SiteContact`, self-ref), created_by (`User`), modified_by (`User`) | Migration 126 adds `attachments`. Migration 134 adds `contact_owner`. Migration 144 adds `reports_to` self-reference. Migration 147 adds audit trail. |

**`company_attachments`** — Files attached to a CRM company (Migration 126, new table)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| company_id | FK -> companies (CASCADE), indexed (`ix_company_attachments_company`) | |
| file_name | String 500, not null | |
| library_item_id | String 500, nullable | |
| library_drive_id | String 200, nullable | `NULL` → user OneDrive; non-NULL → company SharePoint library |
| library_web_url | Text, nullable | |
| thumbnail_url | Text, nullable | |
| content_type | String 100, nullable | |
| size_bytes | Integer, nullable | |
| uploaded_by_id | FK -> users (SET NULL) | |
| created_at | UTCDateTime | |

**`site_contact_attachments`** — Files attached to a site contact (Migration 126, new table)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| site_contact_id | FK -> site_contacts (CASCADE), indexed (`ix_site_contact_attachments_contact`) | |
| file_name | String 500, not null | |
| library_item_id | String 500, nullable | |
| library_drive_id | String 200, nullable | `NULL` → user OneDrive; non-NULL → company SharePoint library |
| library_web_url | Text, nullable | |
| thumbnail_url | Text, nullable | |
| content_type | String 100, nullable | |
| size_bytes | Integer, nullable | |
| uploaded_by_id | FK -> users (SET NULL) | |
| created_at | UTCDateTime | |

**`crm_field_history`** — Per-record field-change audit trail (Migration 169, CRM P5 trust, new table)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| entity_type | String 20, not null | `'company'` \| `'contact'` — polymorphic discriminator (mirrors `approval_requests.subject_type`); pairs with `entity_id` to scope a record's history without two near-identical tables |
| entity_id | Integer, not null | `companies.id` or `site_contacts.id` (no FK — polymorphic) |
| field_name | String 64, not null | raw inline-edit field key (e.g. `industry`, `title`); display label resolved via `FIELD_LABELS` in `app/routers/htmx/companies/_registries.py` |
| old_value | Text, nullable | canonical (stripped) prior value; NULL when previously empty |
| new_value | Text, nullable | canonical new value; NULL when cleared |
| changed_by_id | FK -> users (SET NULL), nullable | the editing user (passed explicitly from the route, not the audit contextvar) |
| created_at | UTCDateTime | indexed via `ix_crm_field_history_entity` (entity_type, entity_id, created_at) |

Written by `app/services/crm_field_history.py:record_field_change` from the inline single-field POST handlers (`company_field_post` / `contact_field_post`) — one row per edit that actually changed a value (no-op edits and None↔"" write nothing). This is the field-DIFF log; complements `companies/site_contacts.modified_by_id` (latest-editor only) and is distinct from `ActivityLog` (outreach timeline). Surfaced on the account **History** tab and the contact **History** modal.

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
| is_active | Boolean NOT NULL, server_default `true`, indexed (`ix_vendor_cards_is_active`) | Migration 165 (CRM P5). Soft-archive flag — archived vendors (`is_active=False`) are hidden from the default vendor list/search and the global vendor-contacts list, but never deleted. Mirrors `Company.is_active` (migrations 139/149). Flipped via `POST /v2/partials/vendors/{id}/archive` and `/unarchive`; the list lifts the filter with `?include_archived=1` ("Show archived" toggle). |
| ticker | String 20, nullable | Migration 125. Stock ticker symbol. Written by `apply_enrichment_to_vendor` via the `firmo_tiers` blending ladder; mirrors the `companies` column. |
| naics | String 20, nullable | Migration 125. NAICS industry code; mirrors `companies.naics`. |
| revenue_range | String 50, nullable | Migration 125. Annual revenue band; mirrors `companies.revenue_range`. |
| enrichment_provenance | JSONB, nullable, server_default `{}` | Migration 125. Per-field provenance store; same shape and semantics as `companies.enrichment_provenance` — written by `apply_enrichment_to_vendor` via `_apply_enrichment`. |
| custom_fields | JSONB, nullable, server_default `{}` | Migration 145 (P1). Arbitrary key:value "Additional details" store. Validator: max 30 keys, key ≤60 chars, value ≤500 chars. Mirrors `Company.custom_fields`. Managed via `POST/DELETE /v2/partials/vendors/{id}/custom-fields`. |

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
| is_primary | Boolean NOT NULL, server_default false | Migration 145 (P1). Designates the primary contact for this vendor. `POST /v2/partials/vendors/{id}/contacts/{cid}/set-primary` clears all others atomically. |

**`vendor_reviews`** — Team feedback on vendors (1-5 rating)

**`strategic_vendors`** — Claimed vendor-buyer relationships with expiry

**`vendor_card_attachments`** — Files attached to a vendor card (vendor parity with `company_attachments`)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| vendor_card_id | FK -> vendor_cards (CASCADE), indexed | |
| file_name | String 500, not null | |
| library_item_id | String 500, nullable | Graph item id (`NULL` = not yet uploaded) |
| library_drive_id | String 200, nullable | `NULL` → user OneDrive fallback; non-NULL → company SharePoint library |
| library_web_url | Text, nullable | Shareable URL |
| content_type | String 100, nullable | |
| size_bytes | Integer, nullable | |
| uploaded_by_id | FK -> users (SET NULL) | |
| created_at | UTCDateTime | |

Model: `VendorCardAttachment` (`app/models/vendors.py`).

**`vendor_contact_attachments`** — Files attached to a vendor contact (same column shape as `vendor_card_attachments`)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| vendor_contact_id | FK -> vendor_contacts (CASCADE), indexed | |
| file_name | String 500, not null | |
| library_item_id | String 500, nullable | |
| library_drive_id | String 200, nullable | `NULL` → user OneDrive; non-NULL → company SharePoint library |
| library_web_url | Text, nullable | |
| content_type | String 100, nullable | |
| size_bytes | Integer, nullable | |
| uploaded_by_id | FK -> users (SET NULL) | |
| created_at | UTCDateTime | |

Model: `VendorContactAttachment` (`app/models/vendors.py`).

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
| **Relationships** | requirements, sightings, offers, attachments (`MaterialCardAttachment`) | Migration 126 adds `attachments`. |

> **Startup backfill:** `_backfill_material_cards()` in `startup.py` runs at boot to ensure every MPN in requirements has a corresponding material card.

> **Indexes & Triggers:**
> - `trig_material_cards_search_vector` — PostgreSQL trigger maintains `search_vector` TSVECTOR on INSERT/UPDATE (weighted: display_mpn=A, manufacturer=B, description/category=C)
> - `ix_material_cards_search_vector` — GIN index for fast full-text search via `plainto_tsquery()` + `ts_rank()`. Owned by migration `eabe89205d07`, which is still on the active mainline and creates it on every fresh replay; migration 098 also creates it `IF NOT EXISTS` because the live DB was provisioned by stamping the revision history past `eabe89205d07` without executing it (its trigger/function are likewise absent on live — FTS there is maintained by `startup.py`'s `trg_mc_fts`), so the index was missing on live out-of-band.
> - `ix_material_cards_trgm_mpn` — pg_trgm GIN index on `display_mpn` for typo-tolerant search. Owned by `eabe89205d07`; 098 creates it `IF NOT EXISTS` for the same live-only gap.
> - `ix_material_cards_enrich_requested_at` — btree on the priority-lane stamp (worker `select_batch` ordering; migration 099)
> - `ix_material_cards_needs_review` — PARTIAL index `(has_validation_conflict) WHERE has_validation_conflict` backing the review-queue filter (conflicted cards are a tiny minority; migration 099)
> - `ix_mc_demand_queue` (**PostgreSQL only**, migration 105) — PARTIAL expression btree whose key order is the EXACT `select_batch` ORDER BY: `(enrich_requested_at ASC, (enrichment_status = 'unenriched') DESC, sourced_qty_90d DESC NULLS LAST, last_sourced_at DESC NULLS LAST, id) WHERE deleted_at IS NULL AND is_internal_part IS false`. Turns the per-loop-tick (~30s) worker-queue scan from a ~740k-row top-N heapsort into an ordered Index Scan + LIMIT (verified on a scratch PG 16 at live volume). The DESC-NULLS-LAST keys are not valid SQLite index DDL, so the index is **NOT** created on SQLite (the test engine queries the same shape unindexed) and is deliberately **NOT** declared on the model (migration-owned). The same holds for the four expression / partial 098 indexes below (`ix_mc_order_live`, `ix_mc_cat_order_live`, `ix_mc_has_datasheet`, `ix_mc_has_crosses`). The remaining 098 / `eabe89205d07` perf indexes — `ix_material_cards_search_vector`, `ix_material_cards_trgm_mpn`, `ix_mc_trgm_norm_mpn`, `ix_mc_trgm_manufacturer`, `ix_mc_trgm_description`, and `ix_mc_last_searched` — **are** now declared on the model so the drift gate sees them (#464 / migration 172); the declaration does not move their DDL (each owning migration still creates the index).
> - **Migration 098 (`098_materials_perf_idx`)** — post-ingest (743k rows) faceted-page indexes, each justified by a measured `EXPLAIN (ANALYZE, BUFFERS)` seq-scan plan:
>   - `ix_mc_order_live` — partial btree `(search_count DESC, created_at DESC) WHERE deleted_at IS NULL` (default page order/pagination)
>   - `ix_mc_cat_order_live` — partial expression btree `(lower(btrim(category)), search_count DESC, created_at DESC) WHERE deleted_at IS NULL AND lower(btrim(category)) IS NOT NULL` (commodity-scoped pages, counts, commodity tree)
>   - `ix_mc_trgm_norm_mpn` / `ix_mc_trgm_manufacturer` / `ix_mc_trgm_description` — pg_trgm GIN on `normalized_mpn`/`manufacturer`/`description`; together with the two `eabe89205d07` indexes above they let the OR'd ILIKE/FTS `q=` paths BitmapOr (every OR branch must be indexed)
>   - `ix_mc_has_datasheet` — partial btree `(id) WHERE datasheet_url IS NOT NULL`
>   - `ix_mc_has_crosses` — partial btree `(id) WHERE cross_references IS NOT NULL AND cross_references::text NOT IN ('[]','null','')`; paired with `stx_mc_crosses_text` extended statistics on `(cross_references::text)` (without expression stats the planner guesses ~98.5% selectivity for the NOT IN — every ingested row holds a non-NULL `'[]'` — and skips the index)
>   - `ix_mc_last_searched` — partial btree `(last_searched_at) WHERE last_searched_at IS NOT NULL` (`searched_within` buckets)
>   - Plain (non-CONCURRENT) builds per repo alembic pattern: each takes a write-blocking ShareLock on `material_cards` (~25s total for the migration on the live-size heap); reads unaffected.
>   - Downgrade drops only the eight 098-owned indexes + the statistics object; the two `eabe89205d07`-owned names survive (only that revision's own downgrade may remove them).
> - **Migration 187 (`187_startup_backfill_partial_idx`, P2.7)** — eight PostgreSQL-only partial indexes (`postgresql_where`, no-op on the SQLite test DB), one per `app/startup.py` deferred-backfill helper's `IS NULL` predicate, so a repeat-boot scan for the shrinking set of legacy rows still needing normalization is an index scan over the remaining rows instead of a full seq scan on every restart: `ix_requirements_backfill_norm_mpn` (requirements, `normalized_mpn IS NULL AND primary_mpn IS NOT NULL`), `ix_material_cards_backfill_norm_mpn` (material_cards, `normalized_mpn IS NULL AND display_mpn IS NOT NULL`), `ix_sightings_backfill_norm_mpn` / `ix_sightings_backfill_vendor_norm` (sightings, normalized-mpn and vendor-name-normalized `IS NULL` predicates), `ix_offers_backfill_norm_mpn` / `ix_offers_backfill_vendor_norm` (offers, same two predicates), `ix_trouble_tickets_backfill_defaults` (trouble_tickets, `risk_tier IS NULL AND category IS NULL`), and `ix_prospect_accounts_backfill_cooldown` (prospect_accounts, `swept_at IS NOT NULL AND reclaim_blocked_until IS NULL AND status != 'dismissed'`). Each indexes only `id` (existence-check shape, mirroring `ix_mc_has_datasheet`/`ix_mc_has_crosses` above). Serialized directly after migration `71d3fef96529` on the same branch (linear history, no merge point). Downgrade drops all eight (dialect-guarded the same way).

**`material_card_attachments`** — User-uploaded files attached to a material card part dossier (Migration 126, new table; distinct from system-captured `material_card_datasheets`)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| material_card_id | FK -> material_cards (CASCADE), indexed (`ix_material_card_attachments_card`) | |
| file_name | String 500, not null | |
| library_item_id | String 500, nullable | |
| library_drive_id | String 200, nullable | `NULL` → user OneDrive; non-NULL → company SharePoint library |
| library_web_url | Text, nullable | |
| thumbnail_url | Text, nullable | |
| content_type | String 100, nullable | |
| size_bytes | Integer, nullable | |
| uploaded_by_id | FK -> users (SET NULL) | |
| created_at | UTCDateTime | |

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

**`partsurfer_desc_negative`** — durable negative cache for PartSurfer DESCRIPTION misses (migration 125)
| Column | Type | Notes |
|--------|------|-------|
| id | Integer PK | |
| spare_norm | String 64, NOT NULL, UNIQUE | `normalize_mpn_key(display_mpn)` — one row per spare (`uq_partsurfer_neg_spare_norm`; the upsert + lookup key) |
| spare_raw | String 64, NOT NULL | Last-seen display form (forensics) |
| reason | String 16, NOT NULL | `no_result` (fetch returned no description) \| `ungrammatical` (a description came back but the desc-grammar declined it) — `ck_partsurfer_neg_reason` CHECK |
| looked_up_at / retry_after | UTCDateTime, NOT NULL | When the miss was recorded / when re-fetching is allowed again (= looked_up_at + window: 90d for `no_result`, 14d for `ungrammatical` — a parse miss is NOT a permanent verdict). `ix_partsurfer_neg_retry_after` indexes the freshness comparison |
| created_at / updated_at | UTCDateTime | |

> Distinct sub-resource from `oem_crosswalk`: that caches the spare→CANONICAL-MPN web
> resolution (Pass A / Claude web_search); this caches the spare→verbatim-DESCRIPTION
> direct fetch (`_partsurfer_desc_pass`). A spare can miss one and hit the other, so the
> two negatives are kept on SEPARATE keys (reusing oem_crosswalk's `(spare_norm,'hpe','')`
> no_match key would conflate "no description" with "no crosswalk"). Written + read by
> `enrichment_worker/partsurfer_negative_cache.py` (`record_negative` writer / `blocked_spare_norms`
> selector); a throttle (`PartSurferTransient`) is NEVER cached.

---

### Excess Inventory / Resell (resell-brokerage)

> Migration 126 added the inbound-offer tables + rollup columns; migration 127 added the
> bid-back tables + posting window; **migration 128 (the CUTOVER) dropped the old
> `bids`/`bid_solicitations` tables** — the legacy outbound email-RFQ "Bid"/"BidSolicitation"
> concept is fully replaced by `excess_offers`/`customer_bids` and is GONE (models, constants
> `Bid*`, schemas, the `app/routers/excess.py` router + `partials/excess/*` templates, the
> `email_service`/`email_jobs` inbox-RFQ callers, and the `create_bid`/`accept_bid`/
> `send_bid_solicitation`/`match_excess_demand` service methods). `ExcessListStatus` keeps the
> Resell lifecycle members (open/collecting/bid_out/awarded); the pre-Resell active/bidding
> members remain DEFINED for back-compat, but **migration 193 remapped every legacy ROW**
> (`active`->`open` + stamp `open_at`, `bidding`->`collecting` + stamp `open_at`; `closed`
> stays CLOSED, distinct from `bid_out` — decision D5), so no live row carries a legacy
> status and the publish guard can rely on `draft` being the only pre-post state.
>
> Service logic lives in `app/services/excess_service.py`:
> `can_post`/`can_offer` (role-derived capabilities), `submit_offer` (per_line/take_all;
> part-number-only matching via `normalize_mpn_key`; unmatched/ambiguous rows queued),
> `recompute_line_rollup`/`withdraw_offer` (min priced active offer -> best_offer_*;
> `withdraw_offer` is GUARDED at the service layer to open/late offers — 409 for a won
> offer [unaward it first] or a lost/withdrawn one, mirroring the router guard),
> `award_offer` (the single chokepoint that flips an offer -> `won`: owner-gated; a
> `take_all` offer awards EVERY non-withdrawn line (it carries no offer lines), a
> `per_line` offer awards its matched lines; idempotent for an already-won offer; 409
> unless the offer is open/late (a lost/withdrawn offer is not awardable — this guard
> runs BEFORE the line-scope check); 409 if a
> line is already awarded to a different offer; marks lines `awarded`, recomputes rollups,
> fires the buyer-score win-hook `buyer_affinity_service.recompute_buyer_score_on_win`
> BEFORE the commit — no-ops for an offer with no canonical buyer; RETIRES the sold lines
> from the Sighting mirror via `excess_mirror.sync_list_mirror`; and DERIVES the list's own
> `awarded` status once every line is decided (awarded/withdrawn) with ≥1 awarded — nothing
> else flips `excess_lists.status`->`awarded`. Routed as `POST /api/resell/{id}/offers/{offer_id}/award`),
> `unaward_offer` (the explicit inverse — never a silent auto-swap to a new winner: 409 if
> not won, reverts offer->`open` + lines->`available`, recomputes rollups + buyer score
> (full-history recompute self-heals `wins` back down), re-mirrors the lines, and steps the
> list off `awarded` -> `bid_out` (close_at set) else `collecting`; `POST /api/resell/{id}/offers/{offer_id}/unaward`).
> Award never auto-marks the losing offers `lost` (`ExcessOfferStatus.LOST` stays
> defined-but-unassigned) — "not selected" is a pure render decision (line awarded + this
> row's offer != won). `close_list`, `get_excess_stats` (offer counts), list/line CRUD + import, and
> `material_card_id` resolution on the import path. The thin router is `app/routers/resell.py`
> (templates under `app/templates/htmx/partials/resell/*`).
>
> Sighting live-mirror lives in `app/services/excess_mirror.py` (Chunk C, additive):
> `sync_list_mirror`/`publish_list` are the dual-write owners (`publish_list` is GUARDED
> to `draft` — 409 otherwise, so a resolved posting can't be re-opened and re-mirrored —
> and clears any stale `close_at` on publish) — every active posted
> `excess_line_items` row mirrors into a `sightings` row (`source_type='customer_excess'`,
> `source_company_id=excess_lists.company_id`, synthesized `vendor_name`="Customer Excess",
> NOT the seller) so the existing matcher sees it for free. `Sighting.requirement_id`
> (NOT NULL) hangs on a per-list system-owned **virtual requirement** — a single
> `is_scratch=True` "Customer Excess (list N)" requisition+requirement (found by the
> deterministic name; hidden from sales views by the existing
> `Requisition.is_scratch.is_(False)` filter). The mirror upserts by
> `(source_company_id, material_card_id)` — NOT the connector-aware
> delete-by-`(requirement_id, source_type)` path — so a re-publish updates the row and
> never wipes a sibling list's `customer_excess` sightings. `retire_line` deletes the
> mirror on award / withdraw / qty->0. Lines whose MPN won't resolve to a MaterialCard
> are skipped (the upsert key needs the card), never raised.
>
> Bid-back assembly lives in `app/services/bid_back_service.py` (Chunk E, additive):
> `build_bid_back` (owner-only) assembles selected lines into a draft `customer_bids`
> header + `customer_bid_lines`. Re-assemble semantics (D3): a non-terminal latest bid
> (draft/sent) bumps `revision` in place on the SAME row; a TERMINAL latest (accepted/
> rejected) is frozen history, so a re-assemble INSERTs a NEW `customer_bids` row
> (`revision`+1, draft) and leaves the answered row — status, send/response stamps and its
> lines — untouched. It seeds each `customer_unit_price` from the line's
> `best_offer_unit_price` rollup (trader override per line); the chosen offer ids are
> recorded INTERNALLY (`selected_offer_id`/`selected_offer_line_id`) for audit and are
> NEVER exported. `bid_back_export_context` is a PURE WHITELIST — line dicts carry only
> part/mfr/qty/condition/unit+extended price and the header carries no seller identity,
> so customer-doc cleanliness is enforced at ASSEMBLY, not by template omission. The
> clean PDF (`generate_bid_report_pdf` -> `app/templates/documents/bid_report.html`,
> cloned from `quote_report.html`, WeasyPrint) renders only that context. Migration 127
> (ADDITIVE) adds the two `customer_bids*` tables and the `open_at`/`close_at` posting
> window on `excess_lists`; `excess_mirror.publish_list` now stamps `open_at` and
> `excess_service.close_list` (owner-only) flips to `bid_out` + stamps `close_at` (which
> drives the "closes in Xd" header chip).

**`excess_lists`** — Customer surplus inventory batches (the posting)
- company_id -> companies, owner_id -> users
- Status: draft -> open -> collecting -> bid_out -> awarded -> closed/expired (legacy active/bidding enum members remain defined but migration 193 remapped all legacy rows -> open/collecting; closed kept distinct from bid_out)
- version (int, default 1) — lock-on-post; a revision bumps version
- open_at (stamped on publish), close_at (stamped on close_list) — posting window (Chunk E)

**`excess_line_items`** — Individual parts in an excess list
- part_number, description, manufacturer, quantity, asking_price, demand_match_count
- material_card_id -> material_cards (SET NULL) — resolved on create for the Sighting mirror
- best_offer_unit_price, best_offer_id (plain int, not a hard FK), offer_count — best-price rollup

**`excess_offers`** — Inbound broker offer to BUY a posted list (the Resell offer model; replaced the dropped `bids`)
- excess_list_id -> excess_lists (CASCADE), submitted_by -> users
- offerer_company_id -> companies / offerer_vendor_card_id -> vendor_cards (both SET NULL)
- scope: per_line | take_all; take_all_total_price (lump, take_all only); valid_until
- status: open -> won -> lost -> expired -> withdrawn (late = post-close, queued)

**`excess_offer_lines`** — Per-line rows of a per_line offer (incl. the unmatched queue)
- offer_id -> excess_offers (CASCADE), excess_line_item_id -> excess_line_items (nullable, SET NULL)
- mpn_raw, quantity, unit_price (nullable), lead_time_days, terms_text
- match_status: matched | unmatched | ambiguous (unmatched/ambiguous = held for manual resolution)

**`customer_bids`** — Outbound bid back to the seller (Trio's offer to BUY their excess; Chunk E)
- excess_list_id -> excess_lists (CASCADE), owner_id -> users
- status: draft -> sent -> accepted/rejected; revision (int, default 1); notes
- One per assembly; the clean PDF + summary render from `bid_back_export_context`

**`customer_bid_lines`** — Per-line priced rows of a customer bid (Chunk E)
- customer_bid_id -> customer_bids (CASCADE), excess_line_item_id -> excess_line_items (SET NULL)
- customer_unit_price (seeded from best_offer_unit_price, overridable), quantity
- selected_offer_id -> excess_offers / selected_offer_line_id -> excess_offer_lines (SET NULL)
  — INTERNAL provenance only; NEVER exported to the customer doc

**`excess_outreach`** — Outbound record: who the trader OFFERED a list to + the response (Resell Outreach; migration 133)
- excess_list_id -> excess_lists (CASCADE), excess_line_item_id -> excess_line_items (nullable, SET NULL — per buyer×line)
- target_vendor_card_id -> vendor_cards (SET NULL, the canonical buyer), submitted_by -> users
- channel: email | phone | teams | marketplace | other (ExcessOutreachChannel)
- status: sending -> sent -> opened -> responded -> bid | declined; no_response = GENUINE buyer silence past a real sent (ExcessOutreachStatus). Send-outcome states (send never reached the buyer, NOT silence): failed (skipped/DNC/send error/outage — reason in send_error) + interrupted (a 'sending' row the sweeper found orphaned). Both retryable. (migration 194 added failed/interrupted + send_error)
- send_error (Text, nullable; migration 194) — persisted send-failure reason on failed/interrupted rows (or a "reply-matching degraded" note on a delivered row whose Graph-id lookup came back empty); NULL on a clean send. Surfaced in the tracker cell + the CSV export "Note" column so a failed/interrupted (or delivered-but-degraded) row is never silent
- send_subject / send_body (Text, nullable; migration 195) — the EXACT subject/body an email campaign was sent with, so a one-click Retry matches an already-delivered CUSTOMIZED-subject message in the double-send guard (email_service._find_sent_message is an exact-subject match) and a legitimate resend reuses the original wording; NULL on manual-log + legacy email rows
- graph_message_id / graph_conversation_id (email only), parts_included (JSON), sent_at
- No DB (buyer×line) uniqueness — re-offers are legitimate; overlap is advisory (buyer_affinity_service.overlap_warning). Same-campaign double-submits are deduped in enqueue (skip a buyer with a live sending/sent row on the same list+line within a 1h window). Downstream "was this buyer genuinely offered?" readers (offered tally, response_rate denominator, last_offered_at, don't-forget nudge, tracker, AND the team-overlap advisory overlap_warning / overlap_warnings_for) exclude the not-sent set {sending, failed, interrupted} via buyer_affinity_service._NOT_SENT_STATUSES

**`buyer_scores`** — Per-buyer "good bidder" rollup (migration 133; inverts the vendor scorecard)
- vendor_card_id -> vendor_cards (UNIQUE index)
- offers_received, wins, avg_bid_pct_of_ask, response_rate, median_response_hours, last_offered_at
- commodity_affinity (JSON, per-commodity counts) — fed from excess_offers + excess_outreach; recompute on offer-win + nightly
- `activity_log` gains a nullable `excess_list_id` scope (migration 133) so outreach events join the unified timeline + cadence clocks

> **Dropped in migration 128 (cutover):** `bids` (vendor bids on excess items) and
> `bid_solicitations` (outbound bid-request emails). The Resell module's
> `excess_offers`/`excess_offer_lines` + `customer_bids`/`customer_bid_lines` replace them;
> the migration's downgrade recreates both tables structure-only (schema-reversible).

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
| alert_kind | String 40 | `AlertKind` value (offer_confirmed\|inbound_customer\|inbound_vendor\|buyplan_action\|tasks_action\|approval_action) |
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
| buyer_ready_score | Integer | nullable, indexed (`ix_prospect_accounts_buyer_ready_score`); write-through CACHE of `prospect_priority.build_priority_snapshot()`'s composite score, kept in lockstep by the `ProspectAccount` before_insert/before_update listener so the `buyer_ready_desc` list sort ranks in SQL. Recompute stays the source of truth; backfilled in migration 170 |
| swept_from_owner_id | INT FK users (SET NULL) | owner whose account was auto-swept by the daily 90-day sweep (SP4) |
| swept_at | UTCDateTime | when the account was swept into the pool (SP4) |
| parked_by_id | INT FK users (SET NULL) | user who manually parked the account via the sales-park flow (SP4) |
| reclaim_blocked_until | UTCDateTime | SP4 Phase 4 compliance cooldown: former owner cannot reclaim until this passes (set at sweep = swept_at + 30d; managers bypass via reassign, which clears it) |

`enrichment_data['ai_screen']` (JSONB) holds the full AI screen verdict:
`{trio_match_score, opportunity_score, excess_likelihood, verdict, rationale, evidence, confidence, model, screened_at, grounding_fingerprint, needs_more_enrichment?}`.
`grounding_fingerprint` (SHA-256 of the assembled context) drives cache invalidation — a
re-screen with materially new grounding produces a different hash and bypasses the cached verdict.
Verdict values: `pass`, `screened_out`, `insufficient_data`, `disabled`, `cap_reached`, `error`.

**`discovery_batches`** — Import batch tracking. Audit trail for every prospect
discovery/enrichment run (`app/models/discovery_batch.py`); sole writer is
`prospect_scheduler.job_discover_prospects`. `status` (String 20, indexed —
`ix_discovery_batches_status`, `ix_discovery_batches_source_status`) uses the
`DiscoveryBatchStatus` StrEnum (app/constants.py): `running` (default) \| `completed` \|
`failed`. `failed` is reserved (not currently written — an unhandled exception leaves the
row at `running`); the vocabulary follows the `PendingBatchStatus` run-lifecycle
convention. Also carries `batch_id` (unique), `source`/`segment`/`regions`/
`search_filters` provenance, `prospects_found/new/updated` + `credits_used` counters,
`error_message`, and `started_at`/`completed_at` timestamps.

**`enrichment_worker_status`** — Singleton (id=1, `ck_enrichment_worker_status_singleton`)
heartbeat + daily-stats row for the paced material-enrichment worker
(`app/services/enrichment_worker/worker.py`). Seeded by migration 088; per-tier daily
counters added in 089. The worker write-throughs `last_heartbeat`, the per-tier
`*_today` counts, and circuit-breaker state every tick.
**Durable daily cap (migration 168):** `enriched_today` is tagged with
`enriched_today_date` (nullable Date, the UTC day it belongs to). On startup the worker
reads both back (`_load_today_counters`): if the stored date == today it RESUMES the
count so the `daily_cap` stays enforced across a container restart (a same-day restart no
longer hands the worker a fresh budget); if the date differs/NULL the counters reset for
the new day. The UTC-midnight roll persists the zeroed counters + new date, archiving the
prior day's tallies into `daily_stats_json`.

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

**`requisition_tasks`** — Tasks tied to requisitions, CRM accounts, or vendor cards (manual, system, or AI-generated). The `CHECK ck_requisition_task_scope` constraint requires exactly one of five scope columns to be non-NULL: `requisition_id` (req task), `company_id` (CRM account task), `site_contact_id` (CRM contact task), `vendor_card_id` (vendor card task), or `vendor_contact_id` (vendor contact task).

New columns (vendor parity):
| Column | Type | Notes |
|--------|------|-------|
| vendor_card_id | FK -> vendor_cards (CASCADE), nullable, indexed (`ix_requisition_tasks_vendor_card`) | Scope: task belongs to a vendor card |
| vendor_contact_id | FK -> vendor_contacts (CASCADE), nullable, indexed (`ix_requisition_tasks_vendor_contact`) | Scope: task belongs to a vendor contact |

> Previously the CHECK constraint covered 3 scope columns (requisition_id, company_id, site_contact_id); it now extends to all 5. Tasks scoped to `vendor_contact_id` only are **not** surfaced by `get_open_tasks_for_vendor_card` (which queries by `vendor_card_id`); see `# NOTE` in `app/services/task_service.py`.

**`trouble_tickets`** — Unified bug + feature tickets with screenshots, captured
runtime context (`console_errors`, `network_errors`, `browser_info`,
`auto_captured_context`, `current_view`), and AI diagnosis (`diagnosis` JSON,
`generated_prompt`, `diagnosed_at`, `cost_tokens`, `cost_usd`) populated by
`ticket_diagnosis_service`. `ticket_type` (String(20), NOT NULL, server_default
`'bug'`, indexed `ix_trouble_tickets_ticket_type`) is the kind discriminator
(`TicketType.BUG` | `TicketType.FEATURE`) — one inbox, both kinds. `admin_notes`
carries the reviewer's notes that feed the notes-aware "Create Prompt" flow
(`ticket_prompt_service`). See APP_MAP_INTERACTIONS "Trouble Tickets — Report capture
+ AI diagnosis".
**`root_cause_groups`** — Grouped similar tickets
**`notifications`** — User notification queue

---

### System & Config

**`api_sources`** — Supplier connector config (credentials, quotas, health)
**`system_config`** — Key-value app settings. **DB row is authoritative over env** for
the 4 System-tab feature flags (`email_mining_enabled`, `proactive_matching_enabled`,
`activity_tracking_enabled`, `inbox_scan_interval_min`): consumers resolve via
`admin_service.get_effective_flag/get_effective_int(db, key, env_default)` — the row's
value wins when present/parseable, else the env-backed `settings.<flag>` is the fallback.
A startup reconcile (`startup._reconcile_system_config`) mirrors the env value into each
never-admin-edited row (`updated_by IS NULL`) so behaviour doesn't flip at cutover;
`set_config_value` invalidates the 5-min in-memory config cache so a toggle takes effect
promptly. `updated_by IS NULL` == never edited via the UI.

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

**`ics_search_queue`** — ICS browser automation queue (priority, status, gate_decision). Dedup keyed on `(requirement_id, normalized_mpn)` — backed by a composite UNIQUE (`uq_ics_queue_requirement_mpn`) that replaced the legacy per-requirement UNIQUE — so the spec-code resolver can enqueue multiple AVL MPNs per requirement while concurrent enqueues still can't double-insert (the app-layer check in `QueueManager.enqueue_search` catches the resulting `IntegrityError` and returns the winning row); carries `resolved_via_spec_code` lineage. `status` (String 20, default `pending`) uses the `SearchQueueStatus` StrEnum (app/constants.py): `pending` (enqueued, awaiting AI-gate classification) -> `queued` (gate approved, or reclaimed after a stale/failed attempt) -> `searching` (claimed by a worker) -> `completed` (results recorded) or `gated_out` (AI gate decided not worth searching) or `failed` (worker gave up after retries/circuit-breaker trip). `app/services/search_worker_base/{queue_manager,ai_gate}.py` and the ics_worker wrapper are the sole readers/writers; values are DB-persisted `String` columns, not native DB enums, so they must equal the pre-enum string literals exactly.
**`nc_search_queue`** — NetComponents browser automation queue (same structure + same composite-UNIQUE dedup `uq_nc_queue_requirement_mpn` / lineage change; same `SearchQueueStatus` vocabulary, nc_worker as reader/writer)
**`tbf_search_queue`** — The Broker Forum (thebrokersite.com) browser automation queue (same structure + same composite-UNIQUE dedup `uq_tbf_queue_requirement_mpn` / lineage change; same `SearchQueueStatus` vocabulary, tbf_worker as reader/writer). Backed by the `avail-tbf-worker` host worker (ACTIVE: authenticates with member creds and records the real seller `vendor_name` + `vendor_phone` per listing). Sister tables `tbf_search_log` (per-search audit) + `tbf_worker_status` (singleton id=1 heartbeat row, seeded by migration 130 / `seed_tbf_worker_status_singleton`).

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

> **Trust telemetry table (trust architecture §1.2, migration 104):** `reconcile_runs` persists one row per `app/management/reconcile_decoded_facets.py` execution — `mode` ('dry-run'|'apply'), `sources`/`keys` (JSONB lists — the run scope), `by_class` (JSONB `{failure_class: {action: count}}`) and `totals` (JSONB `{cards, facets, corrected, deleted, unchanged, skipped, failed}`), indexed on `ran_at`. Both prior reconcile rounds' apply tallies were runtime-log-only and are unrecoverable; every run (dry-run AND apply) now leaves a queryable row, written via `record_reconcile_run` (flush-only; the CLI owns the commit — a dry-run commits the report row AFTER its facet-write rollback). Model: `app/models/telemetry.py` (`ReconcileRun`). Migration 104 also created a `facet_audits` table + `FacetAudit` model for a planned Phase-2.2 volume-weighted accuracy audit harness (`app/management/audit_facets.py`) that was never built; both were removed as dead code (zero readers/writers — docs/audit/2026-07-18-non-production-code-audit.md §1) and the table dropped by migration 196.

> **Brand canonicalization (OPTIMIZATION_PLAN §1.5B, migration 106 — data-only on the `manufacturers` lookup table):** the live brand facet wasted 7 of its top-20 slots on duplicates: the HPE family split four ways (Hewlett Packard Enterprise / HP / HPE / HEWLETT PACKARD — selecting "HP" silently missed the ~4,400 HPE-labeled cards) and `Texas Instruments (TI)` duplicated `Texas Instruments`. Migration 106 (1) renames the canonical `Hewlett Packard Enterprise` row to `HPE` and merges its alias list to `["Hewlett Packard Enterprise", "HP", "Hewlett Packard", "Hewlett-Packard"]` (matching the updated `startup._seed_manufacturers` seed — defensive against the seed/migration race: if a fresh `HPE` row already exists, the legacy long-name row is DELETEd and the survivor's aliases reasserted) and (2) adds the `Texas Instruments (TI)` alias to `Texas Instruments`. The Dell family (Dell Technologies / DELL / Dell) needs no table change — the existing `Dell Technologies` row's `Dell` alias already folds both case variants (lookup is case-insensitive). Downgrade restores the prior canonical name + alias lists exactly. This migration corrects the lookup VOCABULARY only; the per-card `material_cards.manufacturer`/`brand` value rewrites (folding live duplicates + NULLing the "(TP,F)" ingest-leak fragments) are an operator action via `app/management/normalize_manufacturers.py --apply` (dry-run gated), run post-deploy — NOT part of the migration.

> **Facet provenance (SP2/F2, migration 096):** each facet row carries `source` (String 50, nullable), `confidence` (Float, nullable), and `tier` (Integer, nullable), set by `record_spec` to mirror the winning `specs_structured` entry on every write that wins the F1 ladder (a losing write never mutates the facet). Legacy rows are backfilled from the matching `material_cards.specs_structured -> spec_key` JSONB entry (PG-only backfill in migration 096; tier computed via a `CASE` snapshot of the `SOURCE_TIER` map — a sync test pins the snapshot against the live ladder).

> **Seed source of truth:** `app/data/commodity_seeds.json` (loaded by `commodity_registry.py`). `seed_commodity_schemas()` only INSERTs missing `(commodity, spec_key)` pairs at boot — it never updates an existing row — and `reseed_changed_schemas()` (also run at boot, right after the inserter) reconciles rows whose seed definition drifted via delete-then-reinsert. Net-new spec keys on already-seeded commodities therefore reach an existing DB automatically; *removing* a seed never deletes its DB row and needs a data migration (e.g. `093_normalize_legacy_categories` retiring `connectors/series` after the 2026-06-09 taxonomy expansion replaced it with `rows`). Two tree keys are declared coarse buckets with NO parametric seeds (`COARSE_BUCKETS_WITHOUT_SEEDS` = `ics_other`, `oem_assemblies`) — they bucket generic ICs and whole OEM assemblies, which have no honest parametric vocabulary. `tape_drives` (Storage & Drives) is fully seeded (drive_type/interface primary, form_factor, native_capacity_gb, encryption).
>
> **Canonical filter values:** for a fixed-vocabulary enum (non-empty `enum_values`), `get_subfilter_options()` renders the full declared list — unstocked values still show with a `(0)` count. Open-vocabulary enums (no `enum_values`, e.g. motherboard `chipset`) render top-N observed values behind a typeahead. Booleans always offer Yes/No.
>
> **Category canonicalization:** `app/services/category_normalizer.py` maps free-text `material_cards.category` variants (e.g. `connectors, interconnects` → `connectors`) to the canonical commodity keys the faceted sidebar buckets on — including the globally-unambiguous TRIO SFDC part-master `Commodity_Code__c` codes (`Main Board`→`motherboards`, `Hard Drive`→`hdd`, `LCD`/`LCD ASSY`→`displays`, `PSU`→`power_supplies`, `Graphics Card`→`gpu`, `Tape Drive`→`tape_drives`, `IC`/`Integrated Circuits (ICs)`→`ics_other`, `OEM ASSY`→`oem_assemblies`). Source-scoped codes that are only unambiguous inside TRIO's export live in `TRIO_SFDC_COMMODITY_CODES` (bare `Memory`→`dram` — supplier taxonomies use "Memory" for flash/EEPROM/SRAM too) and resolve only through `normalize_trio_category()` (the SFDC ingest entry point; falls back to the global map); the global `normalize_category()` never consults them. Forward hook at the three card category write sites; one-off backfill via `scripts/normalize_categories.py --dry-run|--apply`. Ambiguous strings are left untouched. Legacy rows already in the DB were normalized once by data migration `093_normalize_legacy_categories` (case-insensitive rewrite through a frozen snapshot of the full alias vocabulary, incl. `memory`→`dram` — safe because every existing row carries TRIO provenance; downgrade is a documented no-op for categories; migration `096_spec_provenance` (SP2) was re-parented onto `095_wechat_id`, keeping a single linear head). Because 093's snapshot is frozen, an alias added later only covers NEW writes: `tests/test_category_normalizer.py::test_runtime_aliases_are_backfilled_by_093_or_documented` fails CI unless every post-093 alias is registered with its own backfill (first instance: the four 2026-06-10 distributor-taxonomy aliases `hard drives`/`internal hard drives`→`hdd`, `memory module`/`memory modules`→`dram`, backfilled by data migration `100_taxonomy_alias_backfill`), and the boot-time residue check (`startup._warn_non_canonical_categories`) WARNs every boot with count + worst offenders whenever any `material_cards.category` falls outside the canonical keys (such rows are invisible to all commodity browsing).
>
> **Migration 189 (`189_category_residue_backfill`)** — 2026-07 residue remap, data-only. The boot residue check reported 139 stranded cards on live staging (2026-07-15); this PR added 64 new `CATEGORY_ALIASES` entries (the "2026-07 residue remap" block — distributor/OEM taxonomy + FRU strings, e.g. `schottky diodes & rectifiers`→`diodes`, `8-bit microcontrollers - mcu`→`microcontrollers`, `raid controller accessory / battery module`→`batteries`) and migration 189 backfills existing rows in three passes on `LOWER(TRIM(category))`: (a) the 64 new aliases; (b) a re-run of 8 aliases already in 093/100's frozen snapshots whose variant strings leaked back onto rows before the F1 ladder closed the bypass on 2026-06-10 (`connectors, interconnects`, `switching voltage regulators`, `arm microcontrollers - mcu`, `cpu - central processing units`, `emmc`, `integrated circuits (ics)`, `battery products`, `solid state drives - ssd` — current targets equal 093's, nothing strands); (c) a 093-step-1b-style lowercase pass for case-variants of canonical keys. Soft-deleted rows included; `category_source`/`category_confidence`/`category_tier`/`category_updated_at` untouched (spelling canonicalized, source unchanged); irreversible many-to-one → documented no-op downgrade. The 17 residue strings with no unambiguous bucket (28 cards: bare manufacturer names, `circuit protection`, `eeprom`, `isolators`, `varistors`, …) are deliberately NOT mapped — owner mapping calls tracked in `docs/superpowers/2026-07-03-master-requested-work-backlog.md` (row R).
>
> **Deterministic MPN decode (Phase 1 of MPN→spec enrichment):** `app/services/mpn_decoder/` reads facet specs straight from standard manufacturer drive/SSD/DIMM part numbers (HDD: Seagate/WD/Toshiba/HGST in `storage.py`; SSD: Samsung/Micron/Intel-Solidigm/Kioxia/WD in `ssd.py`; DRAM: Samsung/Hynix/Micron/Kingston/Crucial in `memory.py`) — zero network/LLM, strict per-vendor regex gates that require the full family structure (e.g. Toshiba `^(MG|MN|MD|MQ|DT)\d{2}[A-Z]{3}`, so short OEM spares like Dell DPNs don't false-match; HGST `HUS` requires a digit next so the HUSMM/HUSSL SAS-SSD families don't misdecode as 3.5" HDDs), unrecognized schemes skipped. DRAM modules additionally decode `rank` (enum 1Rx4/1Rx8/2Rx4/2Rx8/4Rx4/8Rx4 — 8Rx4 is emittable via the Hynix device-count math but no shipping part exercises it), `registered` (Registered/Unbuffered/Load-Reduced) and `voltage` (numeric V: 1.2/1.35/1.5; DDR5 1.1 V deliberately omitted) where the org block pins them — all three are seeded `dram` spec schemas in `commodity_seeds.json`, and `tests/test_mpn_decoder_seed_sync.py` pins decoder↔seed sync so `record_spec` never silently drops decoder output; SSD NVMe `interface` is emitted only when the family pins the PCIe generation (the seeded enum has no bare "NVMe"). The full vendor/scheme inventory table lives in APP_MAP_INTERACTIONS.md. The worker second pass (`mpn_decoder/writer.py::decode_and_record_specs`, gated by `settings.mpn_decode_enabled`, default on) writes via `record_spec(source="mpn_decode", confidence=0.95)`, then the deterministic description→spec pass (`app/services/desc_extractor/`, `source="desc_parse"`, confidence 0.90, gated by `settings.desc_parse_enabled`), then the AI spec pass. **As of SP2 the F1 tier ladder — not run order — is authoritative:** `mpn_decode` is tier 85 > `desc_parse` 83 > AI `spec_extraction` 60, so a later lower-tier pass can never clobber a decode value regardless of which ran first (the old "decode runs BEFORE the AI pass" run-order band-aid and the desc writer's confidence pre-gate are gone — `record_spec` arbitrates). Category handling: the decode's commodity is written via `spec_tiers.set_category` (tier 85), which corrects a lower-tier category (e.g. an `ai_guess`/40 misfile) but never overwrites a TRIO-source (95), vendor-API (90), or manual (100) category — a ladder loss against a *different* existing category is counted in the returned stats (`skipped_category_conflict`, INFO-logged by the worker every batch) and WARNed with the `(card_category -> decoded_commodity)` pairs, since a recurring pair signals a missing `CATEGORY_ALIASES` entry; a card with NO category is **categorized from the decode** (the regex-gated commodity). Each card writes inside a `db.begin_nested()` SAVEPOINT so a single DB failure can't poison the shared batch transaction. Coverage dry-run + backfill: `scripts/decode_mpn_dryrun.py` (read-only by default; `--apply` backfills existing inventory in chunked commits). OEM/FRU spare numbers don't match the gates → resolved in later phases (PartSurfer cross-ref / datasheet).

---

## PostgreSQL-Only Code Path Coverage (P6.2 tracker)

Some code paths (`ILIKE`, `JSONB`, `tsvector`/FTS, `pg_trgm` `similarity()`,
`plainto_tsquery`) only run correctly on PostgreSQL — the in-memory SQLite test engine
either raises or silently degrades to a different branch, so a real regression there is
invisible to the main suite. These paths carry the `@requires_postgres` marker (see
`tests/conftest.py` — `pg_engine`/`pg_session`/`pg_client` fixtures, `PG_TEST_DSN`-gated,
skip cleanly on SQLite) and are exercised for real only by CI's dedicated
`postgres-paths` job.

Checklist of modules matching
`grep -rl 'ILIKE\|JSONB\|tsvector\|pg_trgm\|plainto_tsquery\|similarity(' app/`
(deduped; generated 2026-07-09) — check off as each gains a real `@requires_postgres`
test:

- [x] `app/services/vendor_duplicates.py` — pg_trgm `similarity()` ranking
      (`tests/test_vendor_duplicates.py::TestFuzzyMatchPgTrgmDirect`)
- [x] `app/services/faceted_search_service.py` — FTS `plainto_tsquery`/`ts_rank`
      (`tests/test_faceted_search_service.py::TestFacetedSearchFtsRealPostgres`)
- [ ] `app/cache/intel_cache.py`
- [ ] `app/company_utils.py`
- [ ] `app/management/cleanup_known_bad.py`
- [ ] `app/management/enrichment_coverage_report.py`
- [ ] `app/management/reconcile_decoded_facets.py`
- [ ] `app/management/reenrich.py`
- [ ] `app/models/config.py`
- [ ] `app/models/crm.py`
- [ ] `app/models/discovery_batch.py`
- [ ] `app/models/enrichment.py`
- [ ] `app/models/enrichment_run.py`
- [ ] `app/models/faceted_search.py`
- [ ] `app/models/intelligence.py`
- [ ] `app/models/offers.py`
- [ ] `app/models/pipeline.py`
- [ ] `app/models/prospect_account.py`
- [ ] `app/models/sourcing.py`
- [ ] `app/models/telemetry.py`
- [ ] `app/models/vendors.py`
- [ ] `app/routers/crm/offers.py`
- [ ] `app/routers/htmx/prospecting.py`
- [ ] `app/routers/vendors_crud.py`
- [ ] `app/search_service.py`
- [ ] `app/services/activity_service.py`
- [ ] `app/services/enrichment_types.py`
- [ ] `app/services/global_search_service.py`
- [ ] `app/services/prospect_discovery_explorium.py`
- [ ] `app/services/prospect_free_enrichment.py`
- [ ] `app/services/prospect_priority.py`
- [ ] `app/services/prospect_scoring.py`
- [ ] `app/services/prospect_screening.py`
- [ ] `app/services/prospect_signals.py`
- [ ] `app/services/source_ingest/ingest.py`
- [ ] `app/services/spec_enrichment_service.py`
- [ ] `app/services/spec_tiers.py`
- [ ] `app/services/spec_write_service.py`
- [ ] `app/startup.py` (FTS trigger creation/backfill — see faceted_search_service entry
      above for the consumer-side test; the trigger DDL itself is exercised indirectly)
- [ ] `app/utils/search_builder.py`
- [ ] `app/utils/sql_helpers.py`
- [ ] `app/utils/vendor_helpers.py`
- [ ] `app/vendor_utils.py`

Note: many of these hits are `JSONB` column declarations in `app/models/*.py` with no
PG-only *query logic* to exercise beyond what the ORM read/write round-trip already
covers on both dialects (SQLAlchemy's `JSONB` type falls back to JSON semantics on
SQLite) — those are listed for completeness/auditability, not because each needs its
own bespoke `@requires_postgres` test. Prioritize the ones with actual `ILIKE`/
`similarity()`/`tsvector` *query* logic (`services/*`, `routers/*`, `search_service.py`,
`vendor_utils.py`, `company_utils.py`) over pure-model JSONB declarations.
