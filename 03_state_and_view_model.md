# Screen Flows

## Flow 1 — Buyer reviews sourcing results
1. Buyer opens sourcing page for a part.
2. System shows ranked leads.
3. Buyer scans vendor, confidence, safety, reason summary, and contactability.
4. Buyer opens a lead detail drawer for more evidence.
5. Buyer chooses to contact or skip.

## Flow 2 — Buyer marks outreach started
1. Buyer clicks `Contacted`.
2. Optional note appears.
3. Buyer saves note.
4. Lead status updates and activity timeline records event.

## Flow 3 — Buyer records outcome
1. Buyer opens lead card or detail.
2. Buyer selects `Has Stock`, `No Stock`, `Bad Lead`, or `Do Not Contact`.
3. Optional structured reason + free-text note.
4. Lead updates.
5. Feedback is stored for future ranking/tuning.

## Flow 4 — Buyer checks safety before outreach
1. Buyer sees safety badge on lead list.
2. If safety is not low-risk, buyer opens detail.
3. Safety review section explains concerns.
4. Buyer chooses:
   - contact normally
   - verify identity first
   - deprioritize
   - do not contact

## Flow 5 — Queue-based follow-up
1. Buyer opens Buyer Queue.
2. Selects status tab.
3. Works through queue items.
4. Updates statuses quickly.
5. Uses detail view only when more context is needed.

## Empty/Failure Flows

### No leads
Show:
- no leads found
- suggestion to widen search or check later

### Partial source failure
Show:
- some sources unavailable
- results may be incomplete

### Only weak leads
Show:
- only exploratory leads found
- caution that confidence is low
