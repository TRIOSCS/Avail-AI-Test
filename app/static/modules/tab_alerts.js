/**
 * tab_alerts.js — cross-app tab alerts: the in-tab spotlight for new/actionable
 * rows (data-alert-new rails, seen-marking via IntersectionObserver, and the
 * floating "N new" pill). Self-contained IIFE; talks to htmx via window.htmx.
 *
 * What calls it: imported for side effects by app/static/htmx_app.js (Vite entry).
 * Depends on: window.htmx (./globals.js).
 */

/* ────────────────────────────────────────────────────────────────────────
   Cross-app tab alerts — in-tab spotlight for new / actionable rows.

   List rows carrying data-alert-new (stamped by _alert_macros.html) get an
   emerald accent rail; the page glides to the first, and each row is marked
   seen as it scrolls into view. FYI rows fade their rail and drain the badge;
   ACTION rows keep the rail (the work-state count owns it). A floating pill
   jumps between the still-unviewed rows. Reuses the proactive emerald palette.
   ──────────────────────────────────────────────────────────────────────── */
(() => {
  const PILL_ID = 'tab-alert-pill';

  const scopeEl = () => document.getElementById('main-content') || document;

  // Refs the client has already consumed this session — authoritative over the server's
  // eventually-consistent seen-state, so a list refresh that races an in-flight seen-ping
  // cannot resurrect a row and hijack the scroll.
  const consumedRefs = new Set();

  const refsOf = (row) =>
    (row.getAttribute('data-alert-refs') || '').split(',').map((s) => s.trim()).filter(Boolean);

  const pendingRows = () =>
    Array.from(scopeEl().querySelectorAll('[data-alert-new]:not([data-alert-consumed])'));

  const prefersReducedMotion = () =>
    window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  const glideTo = (el) => {
    if (el) el.scrollIntoView({ behavior: prefersReducedMotion() ? 'auto' : 'smooth', block: 'center' });
  };

  const ensurePill = () => {
    let pill = document.getElementById(PILL_ID);
    if (pill) return pill;
    pill = document.createElement('button');
    pill.id = PILL_ID;
    pill.type = 'button';
    pill.className = 'tab-alert-pill';
    pill.style.display = 'none';
    pill.addEventListener('click', () => glideTo(pendingRows()[0]));
    document.body.appendChild(pill);
    return pill;
  };

  const refreshPill = () => {
    const pill = ensurePill();
    const n = pendingRows().length;
    if (n === 0) { pill.style.display = 'none'; return; }
    pill.textContent = `${n} new ↓`;
    pill.style.display = '';
  };

  const markSeen = (kind, refs) => {
    if (!window.htmx || !refs.length) return;
    const url = `/v2/partials/alerts/${encodeURIComponent(kind)}/seen`;
    const body = { ref_ids: refs.join(',') };
    // One background ping per row (all its refs batched): no spinner (indicator: null);
    // htmx.ajax still applies the OOB nav-badge swap from the response.
    window.htmx.ajax('POST', url, { target: 'body', swap: 'none', indicator: null, values: body });
  };

  const consume = (row) => {
    if (row.dataset.alertConsumed) return;
    row.dataset.alertConsumed = '1';
    const kind = row.getAttribute('data-alert-kind');
    const refs = refsOf(row);
    refs.forEach((r) => consumedRefs.add(r));
    markSeen(kind, refs);
    if (row.getAttribute('data-alert-temperament') === 'fyi') {
      row.classList.remove('alert-rail-pulse');
      row.classList.add('alert-rail-fade');
      setTimeout(() => row.classList.remove('alert-rail', 'alert-rail-fade'), 700);
    }
    // ACTION rows keep .alert-rail — the work-state badge owns the count.
    refreshPill();
  };

  let observer = null;
  const getObserver = () => {
    if (observer) return observer;
    observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          consume(entry.target);
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.6 });
    return observer;
  };

  const spotlight = (root) => {
    const scope = root && root.querySelectorAll ? root : scopeEl();
    const rows = Array.from(scope.querySelectorAll('[data-alert-new]:not([data-alert-spotlit])'));
    if (!rows.length) { refreshPill(); return; }
    const obs = getObserver();
    let firstFresh = null;
    rows.forEach((row) => {
      row.dataset.alertSpotlit = '1';
      const refs = refsOf(row);
      // A row whose every ref we've already consumed is a refresh-resurrected row (the
      // server's seen-state hadn't caught up yet) — settle it silently: no rail, no glide.
      if (refs.length && refs.every((r) => consumedRefs.has(r))) {
        row.dataset.alertConsumed = '1';
        return;
      }
      row.classList.add('alert-rail', 'alert-rail-pulse');
      obs.observe(row);
      if (!firstFresh) firstFresh = row;
    });
    refreshPill();
    // Glide only to a genuinely-fresh row — never on a refresh that surfaced no new work.
    if (firstFresh) setTimeout(() => glideTo(firstFresh), 140);
  };

  document.body.addEventListener('htmx:afterSettle', (evt) => {
    spotlight(evt.detail ? evt.detail.elt : document);
  });
  document.addEventListener('DOMContentLoaded', () => spotlight(document));
})();
