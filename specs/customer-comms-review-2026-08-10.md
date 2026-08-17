# Customer Communications Review — 2026-08-10

Synthesis of the confirmed-HIGH findings from the outbound-email review.
Every finding below was independently verified against code (chains cited
inline). Ten raw findings deduped to 7 distinct defects. Reader: owner/dev
on a phone. All paths absolute under /root/availai.

---

## 1. Executive read

**How our mail looks from the other side.** The plumbing is solid — Graph
sends deliver, the quote carries a real T&C block, buy-plan notices are
rich and branded. But the two emails customers actually act on are the two
that arrive broken:

- **The flagship proactive offer arrives half-sent.** Any time a rep uses
  the AI draft (the path the UI steers them to), the customer gets 3–5
  sentences promising "details below" — and nothing below. No parts, no
  prices, no greeting, no signature. The complete email (table + signature)
  is built and then thrown away.
- **The quote — our money document — contradicts its own arithmetic.**
  Unit price is rounded to 2 (or 0) decimals while Ext. Price uses the full
  4-decimal stored value. A buyer who multiplies the printed numbers gets a
  different total than the one printed next to it. Sub-cent parts print as
  "$0".
- **Internal plumbing leaks outward** ([AVAIL-PROACTIVE-8127] in customer
  subjects — nothing ever parses it back) **while needed context never
  reaches recipients** (approval decision emails carry no deal id and drop
  the manager's mandatory rejection reason).
- **Vendor RFQs silently lose text** — anything a buyer types between < and
  > vanishes in the vendor's mail client because nothing HTML-escapes the
  body.

**Through-lines (why these keep happening):**

1. **What the sender approves is not what the recipient gets.** Proactive:
   AI's full HTML discarded, body-only sent. Quotes: preview omits the four
   offer-derived columns and shows different prices than the email. RFQs:
   preview is sanitized, the send is not. Three surfaces, same disease.
2. **Every sender hand-rolls its own HTML.** No shared base layout, no
   shared escaping, no shared money formatting — so each path drifts alone.
3. **No precision/escaping discipline at the composition boundary.**

**Money/wire clarity — singled out.** Two places money moves on the word of
an email, and both are murky:

- The **quote email's printed math does not check out** (unit × qty ≠ Ext ≠
  Subtotal on any sub-cent or >2-decimal line). Customers dispute it or PO
  at the wrong printed price — a 5.3% overstatement in the verified repro.
- **Prepayment (wire) rejections reach the requester as one context-free
  sentence.** decide() *requires* a rejection comment (service.py:159-160)
  and then the email throws it away; rich notices go only to accounting
  DLs. Nothing in the money mails is visually unmistakable — a wire-flow
  email should never require close reading to know whether money moves.
  See cross-cutting item C (OK-TO-PAY / DO-NOT-WIRE banner).

---

## 2. Findings by surface

### 2a. Proactive offers (customer-facing)

**F1 — AI-drafted/typed sends drop the parts table, greeting, signature.**
`app/routers/htmx/proactive.py:519` (send), `app/services/proactive_service.py:453-455` (verbatim use),
`app/services/proactive_email.py:37-39` (AI told table "will be inserted separately" — it never is),
`proactive.py:446` (ai_body hidden input hardcoded "" , full `result['html']` discarded).
Recipient: "please see the details below" → nothing below. Offer unusable;
sender looks broken. Table survives ONLY if the rep empties the body.
Fix: compose body inside the full template (greeting + escaped body +
shared parts table + signature) — reuse `_build_html` (proactive_email.py:143-186)
or extend `_template_email_html` (proactive_service.py:348-393) to accept a
custom body paragraph. Never send raw textarea contents as the message.

**F2 — Internal tracking tag in every customer subject.**
`app/services/proactive_service.py:475` and `:736` — `[AVAIL-PROACTIVE-{po.id}] {subject}`.
Grep: nothing parses it back (the only inbound tag regex, shared_constants.py:130,
can't match it). Reads as automated bulk mail, hurts spam scoring, leaks
tool name, buries the AI's crafted subject on mobile.
Fix: delete the prefix at both lines; if traceability is wanted, carry
po.id in a Graph `internetMessageHeaders` X-header instead.

**F3 — Unverified AI part-equivalence guesses sent as firm availability.**
`app/services/proactive_service.py:648` (build_draft_offers), `:718` (send_draft_offer) —
neither checks `has_ai_variants`. Chain: part_equivalence.py:10-14 pools AI
"same" verdicts with no verification prerequisite; proactive_matching.py:557-565
displays the customer's own spelling over a variant offer's qty/condition/
lead time. The amber "AI match — verify" chip exists only on the Matches
row (`_match_row.html:47-51`) — "All → send" → Process → prepared card
(`_prepared_offers.html:45-52`) carries zero trace of it.
Recipient: asked for LTC1234**E**, gets "LTC1234E — Qty 5,000 — 2 weeks"
backed by I-grade stock (live verdicts have already shown E-vs-I to be
DIFFERENT). Wrong-part commitment from a broker whose product IS part
identity.
Fix: propagate `has_ai_variants` onto the draft in build_draft_offers;
amber "AI variant — verify before sending" on the prepared card; block or
explicit-confirm one-click Send until a human marks it verified. Same gate
in send_proactive_offer for the prepare-page path.

### 2b. Quotes (customer-facing money document)

**F4 — Printed unit price contradicts printed extended price.**
`app/services/quote_send.py:225-229` (`_fmt_price` = 2dp, or 0dp when frac
< $0.005) vs `:250-251` (Ext from raw 4-decimal value) vs `:270` (subtotal
raw). Executed repros: 0.0475×5,000 → "$0.05 / $237.50" (printed math says
$250.00); 22.5049×250 → "$22.50 / $5,626.22" (vs $5,625.00); 0.004 → "$0";
3.004 → "$3". Sub-cent is the designed case: sell_price Numeric(12,4)
(app/models/quotes.py:110), inputs step="0.0001", markup rounds to 4dp.
Fix: format to stored precision — up to 4 decimals, trim trailing zeros to
a 2-decimal floor; delete the 0dp branch; keep unit × qty ≡ shown Ext.

**F5 — Preview is a different document than the sent email.**
`app/templates/htmx/partials/quotes/preview.html:49` (the only UI-wired
preview) vs `quote_send.py::_build_quote_email_html` (the send). Preview
omits the Condition / Date Code / Packaging / Lead Time columns the email
includes (copied from the linked Offer — routers/htmx/quotes.py:91-94), the
greeting, recipient block, signature, and the 9-item T&C (incl. 1.5%
finance charge, 90-day warranty); shows 4dp prices where the email shows
2/0dp. A wrong date code or "Refurb" condition goes out unreviewed. A
faithful endpoint exists (crm/quotes.py:381-398) but nothing links to it.
Fix: render preview from the same `_build_quote_email_html()` output in a
sandboxed iframe/srcdoc so approved = received, byte-identical.

### 2c. Vendor outreach — batch RFQ, sightings inquiry, replies

**F6 — User-typed text sent as raw HTML; angle-bracket text vanishes.**
`app/email_service.py:44-49` (`_build_html_body` does only \n→<br>, no
html.escape) feeding three vendor-facing sends: batch RFQ (email_service.py:222
← sightings.py:2632/2691/2731), sightings preview/send (sightings.py:2567 —
preview sanitizes, send does not), replies (routers/htmx/email_views.py:65,97,101 —
no preview at all). "<no substitutes>", "<NEW date codes only>", "<qty>"
placeholders are swallowed by every client; "<500 pcs" breaks in
Word-engine Outlook; bare "&reg"-style text mis-renders.
Recipient: a corrupted RFQ with the constraint silently missing → wrong
vendor quotes.
Fix: `html.escape(plain_text)` before the <br> replacement — one line,
covers all three paths.

### 2d. System notices (internal recipients)

**F7 — Approval decision email carries zero context and drops the
mandatory rejection reason.**
`app/services/approvals/notifications.py:65-73` — subject "Approval request
approved/rejected" (only system email with no [AVAIL] prefix), body one
sentence: no id, gate, SO/plan number, customer, amount, decider, or link.
`service.py:159-160` requires the reject comment; `:210` payloads it; the
email ignores it (in-app uses it, line 82). Live gates hitting this:
BUY_PLAN (where the rich buyplan email also fires — so this one is noise)
and **PREPAYMENT — where this bare email is the requester's ONLY notice**;
rich prepayment mail goes to accounting DLs only.
Recipient: a rep with three requests in flight can't tell which deal died
or why; must hunt across four workspace tabs.
Fix: subject "[AVAIL] {Gate} {id} {decision} — {customer}"; body = decider
name, escaped comment in the styled reason block buyplan notify_rejected
already has, deep link to /v2/approvals. Add request context to the
payload at service.py:210.

---

## 3. Fix-once cross-cutting items

**A. One shared email base builder.** A single module owning greeting /
body paragraph / line-items table / signature / footer with inlined CSS,
used by proactive, quotes, and RFQ paths. F1, F4, F5 all exist because
each path assembles HTML by hand. The builder is also where B–D live once.

**B. Escape at the boundary.** Every user-typed or AI-drafted string is
html.escape'd inside the shared builder — F6 becomes impossible to
reintroduce. (Interim: the one-line fix in `_build_html_body`.)

**C. Unmistakable money banner.** Any email that authorizes or halts money
movement (quote totals, prepayment decisions, pay-link notices) carries a
full-width color block at the top: green "APPROVED — OK TO PAY" / red
"REJECTED — DO NOT WIRE", with the deal id and amount inside the banner.
Never let a wire decision live in body prose. Internal tracking ids move
to `internetMessageHeaders` X-headers, never subjects (kills F2 class).

**D. Readable-without-HTML rule.** Graph sendMail bodies are single-part,
so a true text/plain alternative isn't available on these paths — the
practical rule: every table-bearing email also states the key facts in one
sentence ("3 lines, total $5,625.00, valid 30 days"), so a text-only or
clipped render still carries the offer. Enforced in the shared builder.

**E. Preview = send path, everywhere.** Any preview must render the exact
send-path builder output (iframe srcdoc). No second template. F5's fix is
the pattern; apply it to proactive prepare and RFQ preview too.

---

## 4. Top-6 fix order

1. **F1 Proactive body-only send** — every AI-drafted offer arrives broken.
   Compose body inside the full template. Fold **F2** in (same service:
   delete tag at proactive_service.py:475/736 — two lines).
2. **F4 Quote `_fmt_price`** — money doc must agree with itself. 4dp with
   2dp floor, drop the 0dp branch (quote_send.py:225-229).
3. **F6 `html.escape` in `_build_html_body`** — one line, fixes three
   vendor paths (email_service.py:46).
4. **F3 AI-variant gate** — propagate has_ai_variants to drafts; amber chip
   + explicit confirm before send (proactive_service.py:599/648/718).
5. **F5 Preview from send path** — render `_build_quote_email_html` in the
   preview pane (preview.html + routers/htmx/quotes.py:132).
6. **F7 Approval decision email** — id + gate + decider + rejection reason
   + link + [AVAIL] prefix (notifications.py:65-73, service.py:210).
   Prepayment requesters are the acute case.

All 7 distinct defects covered in 6 slots (F2 folded into #1).
