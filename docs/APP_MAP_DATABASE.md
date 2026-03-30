# AvailAI Application Map — Database Schema

> **Auto-maintained reference.** Update this file whenever models, tables, or relationships change.

## Database Configuration

- **Engine:** PostgreSQL 16
- **ORM:** SQLAlchemy 2.0 with async support
- **Migrations:** Alembic (81+ migration files)
- **Connection:** Pool size 20, max overflow 20, pool recycle 1800s
- **Timeouts:** Statement 30s, lock 5s
- **Extensions:** pg_stat_statements, Full-Text Search (TSVECTOR)

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
| lifecycle_status | String 50 | active\|nrfnd\|eol\|obsolete\|ltb |
| package_type | String 100 | QFP-64\|BGA-256\|0603 |
| rohs_status | String 50 | compliant\|non-compliant\|exempt |
| cross_references | JSONB | Alternative MPNs |
| specs_structured | JSONB | Parametric data |
| search_vector | TSVECTOR | Full-text search |

**`material_vendor_history`** — Which vendors sell which parts (deduplicated)

**`material_card_audit`** — Audit trail for card lifecycle events

**`material_price_snapshots`** — Historical pricing data points

**`customer_part_history`** — What parts each customer has bought (for proactive matching)

---

### Excess Inventory

**`excess_lists`** — Customer surplus inventory batches
- company_id -> companies, owner_id -> users
- Status: draft -> active -> bidding -> closed -> expired

**`excess_line_items`** — Individual parts in an excess list
- part_number, manufacturer, quantity, asking_price, demand_match_count

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

**`ics_search_queue`** — ICS browser automation queue (priority, status, gate_decision)
**`nc_search_queue`** — NetComponents browser automation queue (same structure)

### Faceted Search

**`commodity_spec_schemas`** — Parametric filter definitions per commodity
**`material_spec_facets`** — Parametric values per material card
