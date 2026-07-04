"""Pure path → module-access-key resolution for ModuleAccessMiddleware.

Single source of truth for which request paths are *module-exclusive* HTMX
sub-partials that must be gated by per-user MODULE access. Extracted as a pure,
unit-testable function so the middleware stays a thin chokepoint.

THE CRITICAL SAFETY RULE — do NOT over-block shared partials. Many
``/v2/partials/*`` fragments are SHARED: they are embedded (``hx-get``/``hx-post``)
by SEVERAL modules' templates, so gating them by path prefix would break unrelated
features. Only prefixes EMPIRICALLY confirmed to be module-exclusive (referenced
solely from their own module's templates) are guarded here. The confirmed-shared
prefixes — ``parts``, ``sightings``, ``materials``, ``search``, ``buy-plans`` plus
all the CRM *data* partials (``customers``/``contacts``/``vendors``/
``vendor-contacts``) and the capability/global partials (``offers``, ``quotes``,
``settings``, ``alerts``, ``follow-ups``, ``trouble-tickets``, global search) — are
DELIBERATELY absent: module revocation hides the section/nav and blocks the
module-prefixed shell, but shared data fragments remain reachable by design.

Cross-module embedding evidence for each EXCLUDED prefix (why it is shared):
- parts:     materials/tabs/sourcing.html embeds /v2/partials/parts/workspace
- sightings: requisitions/tabs/parts.html, offers/_qual_checklist.html and
             shared/_macros.html embed /v2/partials/sightings/...
- materials: search/{dossier_hero,lead_detail,history_panel}.html embed
             /v2/partials/materials/{id}
- search:    dashboard.html, shared/topbar.html and shared/search_results.html
             embed /v2/partials/search (and /search/global, /search/results)
- buy-plans: customers/tabs/buy_plans_tab.html and requisitions/tabs/buy_plans.html
             embed /v2/partials/buy-plans/{id}

Called by: app.main.ModuleAccessMiddleware
Depends on: app.constants.AccessKey (leaf-ish — only an enum import)
"""

from .constants import AccessKey

# Guarded, MODULE-EXCLUSIVE path bases → the AccessKey they require.
#
# A base is matched when the path EQUALS it or starts with ``base + "/"`` — never a
# bare string prefix, so "/v2/partials/proactive" can't swallow a sibling such as
# "/v2/partials/proactivex". Each of these is a prefix confirmed module-exclusive (see
# module docstring); the other module entry-partials
# (parts/sightings/materials/search/buy-plans) are SHARED and intentionally omitted.
#
# NOTE the two Prospecting bases: the tab + grid live under ``/v2/partials/prospecting``
# (with -ing), but the reclaim/reassign admin actions live under ``/v2/partials/prospects``
# (plural, no -ing). Both are Prospecting-exclusive, so BOTH require the PROSPECTING key
# (audit M12 — without the plural entry a user with Prospecting revoked could still hit
# reclaim/reassign). The two are disjoint under the EQUALS-or-``base + "/"`` rule
# ("/v2/partials/prospecting" does not start with "/v2/partials/prospects/"), so ordering
# is irrelevant.
_GUARDED_BASES: tuple[tuple[str, AccessKey], ...] = (
    ("/v2/partials/crm", AccessKey.CRM),
    ("/v2/partials/resell", AccessKey.RESELL),
    ("/v2/partials/proactive", AccessKey.PROACTIVE),
    ("/v2/partials/prospecting", AccessKey.PROSPECTING),
    ("/v2/partials/prospects", AccessKey.PROSPECTING),
    ("/v2/partials/my-day", AccessKey.MY_DAY),
)


def module_key_for_path(path: str) -> AccessKey | None:
    """Return the AccessKey a *path* requires, or None if it is not module-gated.

    None means "pass through" — the path is a shared partial, a capability/global
    partial, global search, or any non-module path. A returned AccessKey means the path
    is a module-exclusive sub-partial and the caller must verify the user holds that key
    before serving the fragment.
    """
    if not path:
        return None
    for base, key in _GUARDED_BASES:
        if path == base or path.startswith(base + "/"):
            return key
    return None
