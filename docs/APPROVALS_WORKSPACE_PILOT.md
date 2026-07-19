# Approvals Workspace — Group-Meeting Pilot Checklist

> **Purpose:** run ONE real TSO end-to-end in AVAIL (`/v2/approvals`) while the Teams
> approval forms and the QP workbooks stay live as fallback. Nothing in Teams is
> retired until this checklist completes cleanly. Print it, walk it, check it off.

Roles in play: **sales**, **buyer**, **manager** (Aniket, with Mike/Marcus backing up),
**accounting** (Myrna/Katy — pay link only, no AVAIL login).

---

## The walkthrough

1. **[sales] Create the sales order.** Sales Orders tab → "New sales order" → pick the
   **order type**, then the requisition.
   *Verify:* the five types are offered — **New** (fresh deal, full sourcing path),
   **Revision** (edit + resubmit of an approved deal; the resubmit carries a reason),
   **Testing Service**, **Comps**, **Stock Sale** (the last three are non-sourcing:
   the **lite path** — SO record + QP data only, no buy-plan lines, no kanban; also
   the right pick for ship-from-stock deals). Changing the type reloads the picker.
   *Replaces:* opening a Teams "TSO Approval Request" form.

2. **[sales] Fill the QP-sales fields.** In the draft's pane, expand **Quality —
   sales section** → Edit → fill condition, quantity, FW/HW/REV/date codes,
   commodity, testing, packaging, ship-early/partial, routing, subs/BOM notes → Save.
   *Verify:* the grid saves in place; fields are optional at submit (the manager
   completes any gaps at approval).
   *Replaces:* the QP workbook's SALES section on SharePoint.

3. **[sales] Submit for approval.**
   *Verify:* status flips to pending; the pane shows "Awaiting manager approval";
   sales can still add notes but not edit fields.
   *Replaces:* posting the TSO form and pinging the manager.

4. **[manager] Land on the decision.** Open the Sales Orders (or Buy Plans — same
   object, same single approval) tab.
   *Verify:* a **"Needs your approval"** group sits at the top of the list, oldest
   first, and the tab auto-selects the oldest pending one — a decision, not a hunt.
   The **change summary** (audit log since submission, "was X → now Y") sits in the
   approval block; empty if nothing changed.

5. **[manager] Edit if needed, then the two-part approve.** While pending the
   manager may amend anything (vendor changes are offer swaps, never free text).
   Then choose the handoff:
   - **Approve & notify** — activates now; buyers get cut-PO tasks; sales gets the
     change summary.
   - **Send back for sign-off** — optional note, "Send back with summary"; the plan
     returns to draft with the manager's edits intact for sales to review and resubmit.
   - **Reject** stays the hard no — a note to the fixer is required.
   *Verify:* both handoffs work; the note lands in the item's notes thread.
   *Replaces:* the Teams TSO approval click + the reply-chain back to sales.

6. **[buyer] Confirm the PO.** Purchase Orders tab → "My assigned lines" → the line
   → "Confirm the PO you cut in Acctivate": **PO number, est. ship date, payment
   method** (wire / PayPal / credit card / ACH / COD) → fill **Quality — purchasing
   section** (incl. the AS9120B answers: traceability verified, counterfeit risk,
   risk level, CoC available, vendor rating, SN history) → **Confirm PO**.
   *Verify:* the line moves to Pending approval; PO# renders as a copy chip.
   *Replaces:* the QP workbook's PURCHASING section + posting a Teams "TPO Approval Request".

7. **[manager] Approve the PO.** Purchase Orders tab → pending queue (oldest first).
   *Verify:* the block shows the **line amount against your approval limit**; the
   sent-mail check is display-only (never auto-approves); manager edit-anything
   (qty, unit cost, PO#, dates) is available with the "Edits here do not change
   Acctivate" warning and an **Edited by manager** marker afterward. Click **Approve**
   (or Send back with a note to the buyer).
   *Replaces:* the Teams TPO approval.

8. **[buyer] Request the prepayment.** On the cut line → **Request prepayment**:
   amount, **payment method dropdown** (wire / PayPal / credit card / ACH — COD
   lines can't request one), and the **test-report-sent toggle**.
   *Verify:* the report state shows loud on the card when NOT sent; one live
   request per line.
   *Replaces:* the Teams "Prepayment Approval" form.

9. **[manager] OK to pay.** Prepayments tab → the card shows amount + payee always.
   *Verify:* the **payment-method dropdown on the card** is adjustable before
   deciding (logged); the approve button reads **"OK to pay — {method}"** and follows
   the field; reject requires a note.
   *Replaces:* the Teams prepayment approval + the "OK TO WIRE" chase.

10. **[accounting] Confirm payment.** Myrna/Katy open the tokenized pay link from
    the OK-to-pay notice — **no AVAIL login**.
    *Verify:* after confirming, the prepayment shows Paid with the wire reference
    (copy chip) in the workspace.

11. **[all] Read the kanban.** Open the SO — the PO board is the centerpiece on
    sourcing orders. Lanes: **Awaiting PO → Pending approval → Paid · awaiting
    delivery → Approved → Received**, plus **Re-sourcing** when a vendor fell down
    (claimable pool). **Paid · awaiting delivery is the risk lane**: prepayment paid,
    goods not here — regardless of approval state, any advance method (never COD).
    Its cards show **amount + payee on the face** and age green → amber (**3 days**)
    → red (**7 days**). **Cards never drag** — they move only by the real action
    (confirm, approve, mark received); the action is the gate.
    *Replaces:* the Microsoft Planner buy-plan boards.

12. **[buyer/manager] Mark received.** Goods arrive → **Mark received** on the card.
    *Verify:* the card lands in Received and leaves the risk lane.
    *Replaces:* the workbook's "OPS Received (Y/N)" column.

13. **[anyone] Notes and attachments — any item, any stage.** Add a note and upload
    a file (customer PO, CoC, wire confirmation) on the SO, a PO line, and the
    prepayment.
    *Verify:* field locks never lock notes/files; counts show on cards and rows.

14. **[two people] Trip the stale-edit guard.** Open the same item in two windows;
    save an edit in one, then try saving in the other.
    *Verify:* the second save is rejected with **"This changed — refresh."** —
    nothing is silently overwritten. Refresh and re-apply.

## What to watch for

- **Tab badges** = items waiting on *you* — confirm each role sees the right counts.
- **Copy chips everywhere:** every SO#/PO# (pane headers, kanban cards, rows,
  prepayment cards) taps to clipboard with a "Copied" flash — nobody retypes an
  Acctivate number.
- **Edited by manager** markers appear wherever a manager touched a buyer's line.
- PO vocabulary is **Approve / Approved / Pending approval** — if backend words
  (verify/pending_verify) leak into the UI, flag it.

## Fallback / rollback

The Teams forms and QP workbooks remain **authoritative** until Mike declares
cutover. Any blocker during the pilot: keep working the deal in Teams as today,
note exactly where AVAIL fell short, and file the issue. Nothing is lost by
falling back — the workspace reads the same pipeline either way.
