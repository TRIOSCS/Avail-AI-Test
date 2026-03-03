# Prospecting Discovery Pool — Design Doc

**Date**: 2026-03-03
**Status**: Approved

## Problem

The current prospecting page is a 3-tab site-ownership manager (My Accounts / At Risk / Open Pool) built around `CustomerSite` ownership. Users want a **prospect discovery hunting ground** — a pool of unassigned, enriched company cards they can browse, filter, and claim into CRM.

## Solution

Replace the prospecting page with a single card-grid discovery pool powered by the existing `ProspectAccount` model, populated by Apollo + Lusha discovery and scored by AI.

## Page Layout

- Card grid (3 columns desktop, 1 mobile) with left filter sidebar
- Filter sidebar collapses to top bar on mobile
- "Load More" pagination (not infinite scroll)
- Default sort: fit score descending
- Active filter chips above grid with "Clear All"
- Footer: "Showing X of Y prospects"

## Card Content

Each card displays:
- Company name + website link icon
- AI-generated blurb (2-3 sentences from `ai_writeup`)
- Industry + HQ location
- Employee size + revenue range
- Fit score bar (green >70, amber 40-70, red <40)
- Buying intent signals (from `readiness_signals` JSONB)
- Similar existing customers (from `similar_customers` JSONB)
- Top 2-3 contacts preview (from `contacts_preview` JSONB)
- Discovery source + last enriched timestamp
- Actions: "View Details" (opens drawer) + "Claim" button

## Filters

| Filter | Type | Values |
|--------|------|--------|
| Search | Text | name + domain |
| Industry | Multi-select | Distinct from pool |
| Employee Size | Multi-select | 1-10, 11-50, 51-200, 201-500, 501-1K, 1K+ |
| Revenue Range | Multi-select | <$1M, $1M-$5M, $5M-$10M, $10M-$50M, $50M+ |
| Region/Location | Multi-select | Distinct from pool |
| Fit Score | Range slider | 0-100 |
| Buying Intent | Multi-select | High / Medium / Low |
| Source | Multi-select | Apollo / Lusha / Import |

## Sort Options

- Fit Score high→low (default)
- Buying Intent high→low
- Recently Added
- Company Size large→small
- Revenue high→low

## Claim-to-CRM Flow

1. User clicks "Claim" on a card
2. Confirmation: "Claim {name}? This will add the company and {N} contacts to your CRM."
3. Backend creates: Company + CustomerSite (owner = user) + VendorContacts (from contacts_preview)
4. ProspectAccount: status → "claimed", claimed_by → user, claimed_at → now, company_id → new Company
5. Card removed from pool
6. Success toast: "{name} added to your CRM — [Go to Account →]"

## Dismiss Flow

- "Not Interested" option (small X or menu) on each card
- Sets dismissed_by, dismissed_at, dismiss_reason
- Hides from user's pool view, remains in DB
- Admin can view/restore dismissed prospects

## Pool Population

### Apollo Discovery (scheduled job)
- Queries Apollo company search API for companies matching target criteria
- Creates ProspectAccount with discovery_source = "apollo"
- Enriches with company data, contacts, signals

### Lusha Suggestions (scheduled job)
- Queries Lusha company data API
- Creates ProspectAccount with discovery_source = "lusha"
- Enriches with company data and contacts

### Deduplication
- domain is unique on ProspectAccount
- Second source enriches existing record (doesn't create duplicate)

## AI Scoring

### Fit Score (Claude Haiku)
- Compares prospect vs. top 20 existing customers
- Inputs: industry, size, location, signals
- Outputs: fit_score (0-100) + fit_reasoning (one line)
- Runs as scheduled job on new/unenriched prospects

### AI Writeup (Claude Haiku)
- 2-3 sentence blurb about the company
- Based on enrichment data + why it's a good fit
- Stored in ai_writeup field

### Readiness Signals
- Populated from Apollo/Lusha data
- Job postings, tech stack changes, funding rounds, facility expansions
- Stored in readiness_signals JSONB

## Removed Features

- "My Accounts" tab → already exists in CRM/Accounts
- "At Risk" tab → already exists in CRM/Accounts
- Site-ownership-based prospecting flow → replaced by prospect discovery

## Data Model

Uses existing `ProspectAccount` model (no schema changes needed). All required fields already exist:
- name, domain, website, industry, naics_code
- employee_count_range, revenue_range, hq_location, region
- fit_score, fit_reasoning, readiness_score, readiness_signals
- discovery_source, discovery_batch_id
- status, claimed_by, claimed_at, dismissed_by, dismissed_at, dismiss_reason
- company_id, contacts_preview, similar_customers
- enrichment_data, email_pattern, ai_writeup, last_enriched_at
