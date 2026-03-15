# Avail Sourcing Engine — React UI Master Spec

## Purpose

Build a buyer-facing sourcing UI that helps purchasing agents:
- see the best vendor leads for a part
- understand why each lead was suggested
- assess vendor safety/risk
- take action quickly
- record follow-up outcomes

The UI should optimize for buyer usefulness, not feature sprawl.

## Core UI Principles

- Show fewer, stronger, more explainable leads.
- Separate Lead Confidence from Vendor Safety.
- Preserve source attribution.
- Make follow-up actions fast.
- Use caution language for risky vendors, not accusation language.
- Keep evidence visible enough to build trust, but not so detailed that it clutters the default list.

## Core Screens

### 1. Sourcing Results View
Primary work screen showing ranked leads for a requested part.

Must show per lead:
- vendor name
- part requested / matched part
- confidence band
- vendor safety band
- reason summary
- source badges
- freshness
- contact preview
- buyer status
- suggested next action
- risk/caution flags

Core actions:
- View details
- Mark Contacted
- Mark Replied
- Mark Has Stock
- Mark No Stock
- Mark Bad Lead
- Mark Do Not Contact
- Add Note

### 2. Lead Detail Panel / Drawer
Expanded view of a single lead.

Must show:
- summary header
- why this lead was generated
- evidence list
- source attribution
- contact info
- safety review block
- buyer actions
- activity / status history

### 3. Buyer Follow-Up Queue
Operational view grouped by buyer status:
- New
- Contacted
- Replied
- Has Stock
- No Stock
- Bad Lead
- Do Not Contact

Must support:
- filtering
- quick status updates
- opening lead detail
- basic sorting

### 4. Safety Review Block
Can live inside lead detail and optionally surface summary state on lead cards.

Must show:
- safety band
- safety summary
- safety flags
- suggested caution

Use wording like:
- "Verify identity before engaging"
- "Limited business footprint"
- "Conflicting contact information"

Do NOT use wording like:
- "Scammer"
- "Fraudster"
- "Unsafe company"

## Confidence vs Safety

These are two separate visible concepts.

### Lead Confidence
“How likely is it that this vendor may currently have stock?”

### Vendor Safety
“How risky or trustworthy does this vendor appear for outreach?”

Examples:
- High confidence + Unknown safety
- Medium confidence + Low risk
- High confidence + Medium risk

## Lead Card Fields

Each lead card or row should support:
- vendor_name
- part_number_requested
- part_number_matched
- confidence_band
- vendor_safety_band
- reason_summary
- source_badges[]
- freshness_label
- contact_email
- contact_phone
- suggested_next_action
- buyer_status
- caution_flags[]

## Filters

Results view should support:
- confidence band
- safety band
- source type
- freshness
- buyer status
- has contact info
- corroborated only

## Sorting

Support:
- best overall
- freshest
- safest
- easiest to contact
- most historically successful

## Empty States

Need explicit states for:
- no leads found
- only low-confidence leads found
- only risky/unknown vendors found
- loading / enriching
- error / partial-source-failure

## Buyer Workflow

Status flow:
- New
- Contacted
- Replied
- Has Stock
- No Stock
- Bad Lead
- Do Not Contact

A buyer should be able to update status in one click plus optional note.

## Safety Review Inputs

Safety UI should be able to surface signals like:
- internal bad experience
- internal good experience
- identity inconsistency
- contact inconsistency
- suspicious domain pattern
- public warning signals
- weak business footprint
- unknown vendor / insufficient data

## Evidence Display

Evidence should be grouped and readable. Suggested grouping:
- Inventory / marketplace evidence
- Internal history
- Web / AI enrichment
- Safety / trust signals

Each evidence item should show:
- signal type
- source name
- short explanation
- timestamp / freshness
- verification state

## Suggested Next Action

Every lead should expose a system recommendation such as:
- Contact now
- Contact, but verify identity first
- Review history before outreach
- Low priority
- Do not prioritize yet

## Preferred React Composition

Top-level sourcing UI should be composed from reusable cards, sections, badges, timelines, filters, and action controls rather than one giant component.
