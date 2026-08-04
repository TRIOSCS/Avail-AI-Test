# Wave 2 checklist — DRAFT (pre-adoption)

Status: **DRAFT, work-ahead.** Brief rule 3.3 adopts a wave checklist into
STATE.md at wave start; Wave 1 is still open (W1.9, W1.10, W1.13, W1.17,
W1.18, W1.A unchecked; Packet 1 undelivered). Adopt this into STATE.md only
after Packet 1 sign-off + the Wave-2 wave-start ritual below. Packet-1
decisions (4 job flip-ables, seeded-admin PO-approve toggle, spec §12
final-read) may amend items before adoption.

Sources: spec v1.1 §4/§5/§8/§9/§10-W2, brief §4.1/§4.2, evidence packs
docs/evidence/w2-api-orphans.md ("the manifest"), w2-glossary.md,
w2-screen-diet.md; W1_JOB_DISPOSITION.md for already-done job-side work.

Wave-2 acceptance (spec §10): tab bar shows exactly 5 tabs + gear; deleted
surfaces 404; app boots clean on fresh DB (drift gate green); kernel E2E
green. Per-item checks below map to these four.

---

## 0. Wave-start ritual (brief rule 3.7 + CUTOVER.md appendix)

- [ ] W2.R1 DB refresh: `./scripts/simp-refresh-db.sh` from the worktree —
      newest checksum-verified prod dump, drop/create, pg_restore, app
      restart (= alembic-upgrade rehearsal #2), confirm printed alembic head
      + row sanity.
- [ ] W2.R2 Cert refresh check: prod LE cert rotates ~60d (current expires
      2026-10-16); if rotated, `docker cp` crt+key from availai-caddy-1 to
      /root/availai-simp-secrets/certs/ and restart availai-simp-caddy-1
      (commands in CUTOVER.md appendix).
- [ ] W2.R3 Main-merge: merge origin/main into the simplification branch
      (pick up anything shipped to prod since base bcfb9a54); suite green
      after merge.
- [ ] W2.R4 Post-refresh sanity (W1.15/W1.16 lesson — refresh re-imports
      prod flags/worker rows): scheduler job list == §3 kernel exactly;
      kernel-walk E2E green on the redeployed instance BEFORE the first W2
      commit.
- [ ] W2.R5 Checklist adoption: fold this draft (as amended by Packet 1)
      into STATE.md as the Wave-2 checklist (rule 3.3); log any deltas in
      the deviation log.

## 1. Nav 10→5 + Settings gear (§4)

- [ ] W2.1 Nav template restructure (§4): mobile_nav.html `nav_items`
      10→5 — Deals (today's Sales Hub entry), Approvals, Resell, CRM,
      Tasks; Settings becomes a gear icon OUTSIDE the tab bar (replacing
      the current More/Settings slot); update the `urlToNav` map for
      removed entries; update tests/test_buyplan_nav.py (asserts the
      Approvals tuple) same commit. *Check: headless render shows exactly
      5 tabs + gear (the §10 literal-count acceptance); no orphaned badges.*
- [ ] W2.2 Materials → contextual lookup (§4): remove the Materials nav
      entry; the page opens only from existing Deals/CRM context links
      (verify at least one existing door per context — rule 2.1: no new UI;
      if no door exists anywhere, STOP → backlog line). *Check: not in tab
      bar; lookup reachable from a deal line; kernel E2E green.*
- [ ] W2.3 Search fold-in, nav step only (§4 → W4 dependency): remove the
      Search nav tab. The actual fold into Deals (part-dossier from a deal
      line, §5.1) is **Wave 4** — until then /v2/search stays routable via
      the topbar global search, which stays per §4. *Check: not in tab bar;
      topbar search works; AI-intent honest-off state intact (W1.6).*
- [ ] W2.4 Sightings fold-in, nav step only (§4 → W4 dependency): remove
      the Sightings nav tab. The slim/split + Deals merge is **Wave 4
      pre-merge** (§5.1/§10); page stays routable un-navved until then.
      *Check: not in tab bar; kernel E2E (sourcing board steps) green.*
- [ ] W2.5 Proactive parks whole, one unit (§4/§5.4/§8): workspace +
      matching engine + badge behind the existing
      `proactive_matching_enabled` flag (config default True → off; clear
      the system_config=true override on the simp DB — code-level default
      change so wave refreshes don't resurrect it); remove nav entry; sweep
      the 2 labels living outside the module (quotes badge
      quotes/detail.html:34, unified_modal.html:443 tooltip — per glossary
      doc row 8). Scheduler side already parked (W1.4). Comeback trigger:
      Proactive revival / Wave-4 Deals-badge decision (disposition table
      notes the §4-vs-§11 tension for the parent to resolve). *Check:
      /v2/proactive hidden/404 behind flag; no proactive badge; no
      proactive log lines in 48h window.*
- [ ] W2.6 Prospecting → CRM lens (§4/§5.4): remove nav entry; manual
      prospect intake + free enrichment (SAM.gov + Google News, W1.7 path)
      + warm intros reachable inside CRM as a lens; keeps its name (owner
      vocabulary, glossary doc). *Check: not in tab bar; lens reachable
      from CRM; enrich honest-off labels intact.*

## 2. Park lanes behind existing flags (§5.3, §8 — no new flag frameworks)

- [ ] W2.7 Trader lane park (§5.3): "Open to Me" lens + Submit Offer modal
      flagged off behind an EXISTING flag (identify exact flag/module-access
      toggle at execution; if none exists, that is a blocker ping, not a new
      framework). Comeback: second trader user. *Check: lane invisible;
      resell kernel walk (intake→post→bids→award) green.*
- [ ] W2.8 Buyer-intelligence park (§5.3): BuyerScore surfaces, ranked
      suggestions, nudge, auto My-Day tasks flagged off (recompute job
      already parked W1.4). Comeback: second trader user. *Check: no
      buyer-intel UI; no recompute logs.*
- [ ] W2.9 Stop resell→Sighting mirror dual-write (§5.3) — DRAFT-FLAG:
      §10-W2 doesn't wave this line explicitly; riding this park batch
      because its comeback is "whichever unparks first: trader lane or
      Proactive". If moved to W3, deviation-log it. *Check: posting a line
      creates no Sighting row; existing mirror rows untouched (tables never
      dropped).*

## 3. The delete list (§8, §10-W2; manifest = docs/evidence/w2-api-orphans.md)

One commit per manifest batch; every deleted route takes its tests with it
(§9-K): 15 whole-file test deletes (4,604 LOC) land with the batch that
kills their last route; 65 mixed files get trimmed per batch.

- [ ] W2.10 Orphan batch: requisitions/requirements.py — 17 routes,
      1,082 LOC (includes the /api/leads* Sourcing-Leads API rows —
      cross-ref W2.14).
- [ ] W2.11 Orphan batch: v13_features/activity.py — 14 routes, 347 LOC.
- [ ] W2.12 Orphan batch: ai.py — 11 routes, 380 LOC.
- [ ] W2.13 Orphan batch: crm/offers.py — 9 routes, 723 LOC.
- [ ] W2.14 Orphan batch: sources.py — 9 routes, 335 LOC (email-mining API
      surface; job side already flag-gated W1).
- [ ] W2.15 Orphan batch: crm/quotes.py — 8 routes, 405 LOC.
- [ ] W2.16 Orphan batch: admin/system.py — 7 routes, 249 LOC.
- [ ] W2.17 Orphan batch: crm/enrichment.py (6) + requisitions/core.py (6)
      — 12 routes, 126 LOC.
- [ ] W2.18 Orphan batch: materials.py (5) + vendor_contacts.py (5) — 10
      routes, 475 LOC (vendor_contacts timeline/summary = contact-intel
      surface — cross-ref W2.23).
- [ ] W2.19 Orphan tail batch: crm/companies.py (3) + vendors_crud.py (3)
      + error_reports.py (2) + crm/clone.py (1) +
      requisitions/attachments.py (1) — 10 routes, 293 LOC.
      *Check (W2.10–W2.19): each deleted path returns 404; suite green;
      route-count drop shows in the baseline table (809 → target ≈700).*
- [ ] W2.20 KEEP-AMBIGUOUS re-verify (35 routes, manifest §KEEP-AMBIGUOUS):
      manual check of the "UI moved to /v2" candidates; promote confirmed-
      dead ones to one supplementary delete commit; leave the rest KEEP with
      a one-line verdict each. *Check: verdict table in Packet 2.*
- [ ] W2.21 Legacy JSON create endpoint: POST /api/requisitions
      (requisitions/core.py:518) — measured keep-ambiguous but named
      explicitly by spec §5.1/§0.6; delete (deviation already logged
      2026-08-04). *Check: 404; UI create path (unified modal) green in
      kernel E2E.*
- [ ] W2.22 Coverage farm delete + diff-coverage gate — SAME PR (§9-K):
      delete the coverage-padding test files (manifest marks the *_coverage*
      farm; final list at execution), replace ci.yml `--cov-fail-under=85`
      global gate with a diff-coverage gate; test_static_analysis.py keeps
      its ~8 bug-class guards, loses line-keyed allowlists/style ratchets.
      Host-side daily_coverage_report.sh cron removal stays deferred to
      cutover (CUTOVER.md §5). *Check: CI green on a probe PR; no global
      fail-under remains.*
- [ ] W2.23 Sourcing Leads workspace delete (§8): routers/htmx/sourcing.py
      surface + templates; API rows die in W2.10; the 2 vendor empty-states
      referencing it go too (glossary doc note). *Check: 404; no nav/link
      residue.*
- [ ] W2.24 Dashboard + Knowledge pages delete (§8): dashboard.html,
      _dashboard_cards.html, knowledge/ templates + their routes
      (knowledge_expire_stale job already deleted W1.4). *Check: 404s;
      fresh-DB boot green.*
- [ ] W2.25 Email-Intelligence dashboard delete (§5.4): write-only,
      linked nowhere — routers/htmx/email_views.py surface (Data Capture
      Initiative rebuilds proper surfaces post-launch). *Check: 404.*
- [ ] W2.26 Contact-intelligence layer delete (§5.4): computed-displayed-
      nowhere writers + remaining readers (jobs already deleted W1.4;
      routes die in W2.18); ORM columns stay per drift-gate grandfather.
      *Check: no writer remains; suite green.*
- [ ] W2.27 Backfill graveyard delete (§8): app/management one-shots —
      build the disposition sub-list at execution; delete completed
      one-shots (backfill_*.py, fix_*, reattribute_activity.py, …); KEEP
      the W1.3 on-demand commands (ai_tagging, prefix_backfill,
      spec/enrich), notify_nightly_status.py (nightly seam), seed scripts.
      *Check: kept-list documented in Packet 2; nothing imports a deleted
      module.*
- [ ] W2.28 startup.py backfills → alembic (§8): the ~11 deferred
      SLOW-path backfills (startup.py docstring table) become one-time
      alembic migrations (or are dropped where already-completed no-ops);
      startup keeps only FAST guards. *Check: fresh-DB boot green with
      drift gate, §11 "no startup backfills"; restart time drop noted.*
- [ ] W2.29 Write-only Notification table: delete in-app Notification
      writers (incl. the approval_outbox_drain in-app write path — email
      path KEPT) + the dead read-nowhere rows' UI stubs; table itself
      NEVER dropped (grandfather rule; ORM model removal only per that
      rule). *Check: exactly one delivery system per event (§5.5);
      approval emails still send in kernel E2E.*
- [ ] W2.30 Settings slim (§5.4/§5.5) — DRAFT-FLAG (gear move is §4/W2;
      slim rides it): connectors page lists only connectors that exist
      with honest keys-off state (W1 groundwork); delete dead connector
      rows + the per-user 8x8 toggle (module parked to Data Capture
      Initiative). *Check: settings page shows no dead rows.*

## 4. QP serial/FRU relink (§5.2 Decision E)

- [ ] W2.31 One link from the workspace QP pane to the existing serial/FRU
      page (routes live in app/routers/quality_plans.py — /v2/qp/{id}/serial
      CRUD + FRU pin/unpin); restores reachability stranded by the retired
      Deal view. NO absorption (that waits on comeback trigger: first live
      TSO with serial tracking). *Check: link renders in the QP pane;
      serial/FRU page reachable + functional from the workspace.*

## 5. SIGN-OFF-GATED — ship evidence in Packet 2, apply ONLY after approval

- [ ] W2.G1 UI glossary sweep (brief §4.1 — GATED): old→new table =
      docs/evidence/w2-glossary.md (125 label sites, ~55 templates;
      Requisition→Deal 55, Req→Deal 21, Sighting→Availability 26,
      Requirement→Line 18, Sales Hub→Deals 3, Materials→Parts 2). Five
      flags need owner answers first (Deal-vs-Reqs, REQ- ID prefix,
      vendor-facing rfq_summary.html, nav-test update, Availability has no
      seed in owner vocab). Labels only; DB/code/routes untouched.
      **Apply only after Packet-2 sign-off.** *Check: post-approval sweep
      commit(s) + nav test updated; zero old-term label sites remain in the
      approved rows.*
- [ ] W2.G2 Screen diet (brief §4.2 — GATED): cut list =
      docs/evidence/w2-screen-diet.md — 21 UI fields at ~100% null with no
      workflow (win_probability, 12 vendor_cards displays, companies
      tax_id/revenue_range, 3 site_contacts fields, excess date_code/notes,
      so_rejection_note display, quotes.source) + DEMOTE set (6 spec fields
      → one "Specs" disclosure; customer_name → company link). LOW-N rows
      marked flip-able. UI only; tables never change. **Apply only after
      Packet-2 sign-off.** *Check: approved fields absent from forms/lists;
      DEMOTE disclosure renders; suite green.*

## 6. Wave close (brief §7 + §6)

- [ ] W2.A Acceptance: 5 tabs + gear literal count; deleted surfaces 404
      (spot-list in packet); fresh-DB boot green (migrations + drift gate,
      no startup backfills); kernel E2E green; suite green.
- [ ] W2.B Packet 2 assembled: baseline→current numbers table opener,
      glossary table (W2.G1), field-cut evidence (W2.G2), delete-list
      confirmation (what now 404s), nav check, deviation log, flip-ables
      (W2.9, W2.30, LOW-N rows, KEEP-AMBIGUOUS verdicts).

## Draft open questions (resolve at adoption, not mid-wave)

1. Packet-1 outcomes may amend scope (4 job flip-ables; PO-approve toggle
   unskips 2 kernel-walk steps; §12 final-read).
2. Exact existing flags for trader lane / buyer intelligence — identify at
   execution; none exists → blocker ping (no new flag frameworks, §8).
3. §5.4 org-scale CRM parks (saved views, segment tags, custom fields,
   collaborators, Activity Scorecard, pool-governance UI, cross-company
   contact pages): §10-W2's "park lanes … (§5)" arguably includes them —
   confirm W2 vs later; not itemized above pending that call.
4. /v2/search + /v2/sightings interim state: routable-but-unlinked until
   their W4 fold (drafted here), or 404 now — confirm ("deleted surfaces
   404" acceptance covers deletes, not fold-ins).
5. W2.9 mirror-stop wave placement (W2 park batch vs W3 resell session).
