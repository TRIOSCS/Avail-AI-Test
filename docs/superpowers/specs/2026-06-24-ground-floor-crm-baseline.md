# Ground-Floor CRM Baseline — the universal fields & functions every CRM has

**Purpose:** the rubric for the basic-functionality audit. Every item below is something
Salesforce, HubSpot, Zoho, Pipedrive, and Dynamics all ship as table-stakes. The audit asks two
questions per item: **(a) does AvailAI have it?** and **(b) does it actually WORK end-to-end?**
(A field/route existing ≠ working — the contact `is_active=NULL` bug proved a whole surface can be
present in code yet dead for the user.)

Legend: **P0** = truly universal (no CRM ships without it). **P1** = standard, nearly universal.

---

## 1. ACCOUNT / COMPANY

### Fields
- **P0** Name; Website/Domain; Phone; Industry; Address (street, city, state/region, postal, country)
- **P0** Owner (account owner — a user); Type (customer / prospect / vendor / partner); Status / lifecycle stage
- **P0** Description / notes; Created date; Last-modified date
- **P1** Employee count / size; Annual revenue; Parent account (hierarchy); Source / lead source; Tags / labels
- **P1** Primary contact (pointer); Created-by / modified-by (audit trail); Tax id / registration #

### Functions
- **P0** Create; **Edit** (inline field + full form); Deactivate / delete; View detail page
- **P0** List view with **search**, **filter**, **sort**; See related contacts; See activity timeline
- **P0** Add note; Add task; Assign / change owner
- **P1** Merge duplicates; Attach files; Bulk select + bulk action; Parent→child rollup view

---

## 2. CONTACT / PERSON

### Fields
- **P0** First name; Last name (full name); Job title; Email; Phone (work/mobile)
- **P0** Account/Company link; Owner; Created / modified date
- **P0** Role / relationship; Primary-contact flag; Do-not-contact / email-opt-out
- **P1** Secondary email/phone; LinkedIn; Mailing address; Reports-to (org chart); Source; Tags; Description

### Functions
- **P0** Create; **Edit** (inline + form); Delete; Set primary; View detail
- **P0** Search; Log activity against the contact (call / email / meeting); See the contact's timeline
- **P0** Add note; Add task
- **P1** Dedup / merge; Click-to-call / click-to-email; Archive; Move to another account

---

## 3. ACTIVITY / INTERACTION (the timeline)

### Fields
- **P0** Type (call / email / meeting / note); Subject; Body / description; Date-time; Related-to (contact + account)
- **P0** Owner / logged-by; Direction (inbound / outbound)
- **P1** Outcome (connected / voicemail / no-answer); Duration; Follow-up flag; Attachments

### Functions
- **P0** **Log manually** (quick-add a call/email/meeting/note); View unified timeline; Filter timeline by type
- **P0** Auto-capture emails (and calls/meetings where integrated)
- **P1** Edit / delete an activity; Pin; Link to a deal

---

## 4. TASK / TO-DO / ACTIVITY-PLANNING

### Fields
- **P0** Title / subject; Due date; Assignee; Status (open / completed); Related-to (contact / account / deal)
- **P1** Priority; Reminder; Type (call/email/to-do); Notes

### Functions
- **P0** Create; **Complete**; Edit; Delete; "My open tasks" / today view; Overdue indication
- **P1** Snooze / reschedule; Recurring; Reassign

---

## 5. NOTE

- **P0** Fields: body; author; timestamp; related-to. Functions: add; view as a feed.
- **P1** Edit; delete; pin; @-mention.

---

## 6. DEAL / OPPORTUNITY (pipeline)
*AvailAI's equivalent is the **Requisition / buy-plan**, not a classic sales pipeline — so judge by
intent, not by a literal "Deals" tab.*
- **P0** A record of the in-progress commercial item with: name, account, value, a **stage**, owner, expected close.
- **P0** Move through stages; mark won/lost (or the AvailAI equivalent: requisition status lifecycle).
- **P1** Probability / forecast category; products/line-items; quote.

---

## 7. CROSS-CUTTING (applies to every object)
- **P0** Global search; Ownership & assignment; Created/modified timestamps
- **P0** A detail page that consolidates: summary + related contacts + timeline + tasks + notes
- **P1** Created-by / modified-by audit trail; File attachments; Tags / labels; Import; Export (CSV); Validation with clear error messages

---

## How the audit uses this
For each row: mark **HAVE+WORKS / HAVE+BROKEN / MISSING**, with file/route evidence and (if broken)
the root cause. The output is a coverage matrix → the fix backlog, P0 gaps first. The
`is_active=NULL` contact bug is the first confirmed **HAVE+BROKEN** (entire contact-edit surface).
