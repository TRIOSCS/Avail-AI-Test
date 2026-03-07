# AI Intelligence Layer -- Integrated Across AvailAI

**Date**: 2026-03-07
**Goal**: Replace daily Excel handoff, break information silos between sales and purchasing, and sprinkle contextual AI insights throughout every surface of the app.

## Core Concept

A Knowledge Graph + AI Assistant woven into every existing view. No new modules -- every view gets smarter. The system captures all interactions (notes, Q&A, quotes, searches, emails) into a shared knowledge base, and AI surfaces the right info at the right time to the right person. Teams integration automates communication so nothing falls through the cracks.

## Architecture: Two New Foundations

### 1. Knowledge Ledger

A single `knowledge_entry` table that captures every meaningful fact:
- Part intelligence (last quoted price, preferred vendors, lead times, alt parts)
- Customer intelligence (buying patterns, past requests, price sensitivity)
- Vendor intelligence (reliability notes, negotiation outcomes, MOQ flexibility)
- Q&A threads (sales question -> buyer answer, linked to req/part/customer)

Entries are auto-tagged to entities (MPN, vendor, customer, req) so they resurface anywhere those entities appear.

### 2. AI Context Engine

A service that, given any entity (req, part, vendor, customer), pulls all relevant knowledge entries and generates contextual insights. Called inline from every UI surface. Uses Claude Haiku for speed, upgrades to Sonnet for complex analysis.

## Data Model

```
knowledge_entry:
  id, created_at, created_by (user_id)
  entry_type: "fact" | "question" | "answer" | "note" | "ai_insight"
  content: text
  -- linkage (nullable, multi-attach)
  mpn: str | null
  vendor_card_id: int | null
  company_id: int | null
  requisition_id: int | null
  requirement_id: int | null
  -- Q&A support
  parent_id: int | null (FK to self -- answer links to question)
  assigned_to: int | null (user_id -- who should answer)
  answered_by: int | null (user_id)
  answered_at: datetime | null
  -- metadata
  confidence: float | null (for AI-generated entries)
  source: "manual" | "ai_extracted" | "system" | "email_parsed" | "teams_bot"
  expires_at: datetime | null (price facts expire)
  is_resolved: bool default false
```

## Where AI Shows Up (Integrated Into Existing Views)

### Requisition Detail (biggest win -- replaces the Excel)
- **Q&A thread** right on the req: sales posts a question, AI routes notification to assigned buyer, buyer answers inline. Tagged to MPN + customer so it resurfaces later.
- **"AI knows" sidebar** -- when viewing a req, AI auto-shows: "This MPN was quoted 3 months ago to Customer Y at $2.15 from Vendor Z" / "Buyer Mike noted this part has 16-week lead time" / "3 other open reqs need this same part"
- **Status nudge** -- AI detects unanswered questions older than 4h, pings the buyer via Teams DM

### Activity Feed (company drawer)
- AI summary card at top: "2 emails exchanged this week, 1 unanswered question from sales, deal #452 stalling"
- Grouped by significance, not just chronology

### Pipeline Tab (company drawer)
- Deal health indicator per req (green/yellow/red) based on: days stale, unanswered questions, quote coverage
- "Needs attention" items surfaced first

### Contacts Tab
- "Who to call next" -- AI ranks by: days since last contact, open reqs pending, engagement trend
- Relationship strength indicator

### Sourcing View
- After search results load: "Last time you sourced this MPN, best price was $X from Vendor Y (3 months ago)"
- Vendor reliability note inline if knowledge exists

### Vendor Popup
- AI one-liner: "Reliable -- 12 quotes, 90% on-time, avg 2-day response" or "Caution -- last 3 quotes were 30%+ above market"

### Dashboard (home/login)
- Personalized morning briefing: unanswered questions for you, stalling deals, customers going quiet, parts that resurfaced

### Offer Review
- Outlier flag: "This price is 3x the last quote for this MPN"
- "Vendor quoted this same part to another customer at $1.80 last month"

## Communication Layer (Teams + Email Integration)

### Teams -> AvailAI (inbound)
- Buyer can answer a Q&A question directly from Teams DM -- reply gets captured as a knowledge entry on the req
- "What's the lead time on LM317?" in Teams -> bot checks knowledge ledger + open reqs, responds with context and logs the Q&A

### AvailAI -> Teams (outbound)
- Unanswered question >4h -> DM the assigned buyer with context: "Sarah asked about MOQ for MPN X on Req #452 -- no answer yet"
- AI daily digest replaces the Excel: morning DM to each user with their action items, unanswered questions, stalling deals, parts that resurfaced
- Deal risk alerts: "Req #380 has 3 unanswered questions and no new quotes in 9 days"

### Email (existing Graph API)
- When AI parses incoming vendor email, extract facts (lead time, MOQ, pricing notes) -> auto-create knowledge entries
- When a sales person forwards a customer email to shared inbox, AI links it to the right req and extracts the question

### Cross-channel continuity
- Question asked in Teams -> shows on the req in AvailAI -> answer given in AvailAI -> confirmation sent back via Teams
- No matter where the conversation happens, the knowledge ledger captures it

## Knowledge Capture (How Info Gets In)

- **Automatic**: Every quote, RFQ, email parse, search, offer, and activity log auto-creates knowledge entries
- **Manual**: Q&A thread on reqs, notes on any entity
- **AI extraction**: When emails are parsed, AI extracts facts ("lead time 12 weeks", "MOQ 1000") and stores them as knowledge entries on the MPN/vendor
- **Teams bot**: Questions and answers via Teams DM get logged as knowledge entries

## Phased Rollout

| Phase | What | Details |
|-------|------|---------|
| 1 | Knowledge ledger + Q&A on reqs + "AI knows" sidebar | Migration, model, service, Q&A UI on req detail, context engine, knowledge API |
| 2 | Teams Q&A routing + daily digest | Answer from DM, unanswered nudges (4h), morning digest replacing Excel |
| 3 | AI sprinkles across all views | Activity summary, pipeline health, contact priority, sourcing history, vendor inline, offer outliers |
| 4 | Email fact extraction + dashboard briefing + cross-customer resurfacing | AI extracts facts from parsed emails, login briefing, "this part was quoted before" everywhere |
| 5 | Refinement | Knowledge expiry, confidence decay, feedback loop, stale fact cleanup |
