"""dependencies.py — Shared FastAPI Dependencies.

Reusable dependency functions for authentication, authorization,
and common query patterns. All routers import from here instead
of defining their own auth logic.

Business Rules:
- get_user returns None if not logged in (non-throwing)
- require_user raises 401 if not logged in, 403 if deactivated
- require_buyer raises 403 if user is not buyer/sales/trader/manager/admin
- require_admin raises 403 if user.role != "admin"
- require_settings_access allows admin only
- require_fresh_token handles M365 token refresh with 15-min buffer

Called by: all routers
Depends on: models, database, config
"""

import hmac
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from fastapi import Depends, HTTPException, Request
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .constants import (
    RESTRICTED_ROLES,
    ROLE_ACCESS_DEFAULTS,
    AccessKey,
    ApprovalRecipientStatus,
    BuyPlanStatus,
    UserRole,
)
from .database import get_db
from .models import AccountCollaborator, BuyPlan, Company, CustomerSite, Quote, Requisition, User

if TYPE_CHECKING:
    from .models import ProspectContact

# Non-interactive service account seeded by startup.py. It authenticates via the
# x-agent-key header and is barred from admin/settings/buyer (RFQ) endpoints.
_AGENT_EMAIL = "agent@availai.local"

# ── Authentication ────────────────────────────────────────────────────


def get_user(request: Request, db: Session) -> User | None:
    """Return current user from session, or None if not logged in."""
    uid = request.session.get("user_id")
    if not uid:
        return None
    try:
        return db.get(User, uid)
    except Exception:
        logger.warning("Failed to load user from session (uid={})", uid, exc_info=True)
        request.session.clear()
        return None


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: raises 401 if no authenticated user, 403 if deactivated."""
    user = get_user(request, db)
    if not user:
        # Check for agent API key (service-to-service auth)
        from .config import settings

        agent_key = request.headers.get("x-agent-key")
        if agent_key and settings.agent_api_key and hmac.compare_digest(agent_key, settings.agent_api_key):
            logger.info("Agent API access: method={} path={}", request.method, request.url.path)
            user = db.query(User).filter_by(email=_AGENT_EMAIL).first()
            if not user:
                logger.error("Agent API key valid but {} user not found in DB — seed it first", _AGENT_EMAIL)
                raise HTTPException(503, "Agent user not provisioned")
    if not user:
        raise HTTPException(401, "Not authenticated")
    if not getattr(user, "is_active", True):
        request.session.clear()
        raise HTTPException(403, "Account deactivated — contact admin")
    # NOTE: the viewer's display timezone is published by AuditUserMiddleware (async
    # context), NOT here. require_user is a SYNC dependency that FastAPI runs in a
    # threadpool, so a contextvar .set() here would land in a discarded thread-context
    # copy and never reach the async endpoint / template render (the bug this replaces).
    return user


def is_admin(user: User) -> bool:
    """Check if user has admin privileges (by role)."""
    return bool(user.role == UserRole.ADMIN)


def is_manager_or_admin(user: User) -> bool:
    """True for MANAGER or ADMIN roles (the supervisor/oversight tier).

    Use this to gate visibility and management actions that a supervisor should always
    be able to perform regardless of account ownership.
    """
    return user.role in (UserRole.MANAGER, UserRole.ADMIN)


def can_manage_account(user: User, company: Company, db: "Session") -> bool:
    """True if *user* may act on *company* as an account manager.

    Allowed when ANY of the following holds:
    - is_manager_or_admin(user)  — supervisors see/manage everything
    - company.account_owner_id == user.id  — primary account owner
    - user owns at least one CustomerSite under this company
    - user is an AccountCollaborator (helper role) on this company  [Phase 3]

    NOTE: this does NOT gate team-management actions (add/remove collaborators,
    reassign ownership). Use can_manage_account_team() for those.
    """
    if is_manager_or_admin(user):
        return True
    if company.account_owner_id is not None and company.account_owner_id == user.id:
        return True
    # Site-owner check: efficient exists() subquery — index-backed via ix_cs_owner/ix_cs_company
    site_exists = (
        select(CustomerSite.id)
        .where(
            CustomerSite.company_id == company.id,
            CustomerSite.owner_id == user.id,
        )
        .exists()
    )
    if bool(db.scalar(select(site_exists))):
        return True
    # Collaborator check (Phase 3): helper collaborators can view + work the account.
    collab_exists = (
        select(AccountCollaborator.id)
        .where(
            AccountCollaborator.company_id == company.id,
            AccountCollaborator.user_id == user.id,
        )
        .exists()
    )
    return bool(db.scalar(select(collab_exists)))


def manageable_company_ids(user: User, companies: Iterable[Company], db: "Session") -> set[int]:
    """Return the subset of *companies*' ids that this rep may manage — batched.

    Batched equivalent of calling ``can_manage_account`` once per company for a
    non-manager: a company is manageable when the user is its ``account_owner``, owns
    one of its sites, or is a named collaborator. Runs in at most two ownership queries
    total regardless of how many companies are passed — the per-row alternative issues
    up to 2*N DB round-trips (one site + one collaborator EXISTS per company). Managers
    and admins manage everything, so callers must gate on ``is_manager_or_admin`` first
    and skip this entirely.
    """
    company_list = list(companies)
    ids: set[int] = {int(c.id) for c in company_list}
    if not ids:
        return set()
    # account-owner: resolved from the already-loaded Company rows (no query).
    manageable: set[int] = {int(c.id) for c in company_list if c.account_owner_id == user.id}
    remaining = ids - manageable
    if remaining:
        manageable.update(
            int(cid)
            for (cid,) in db.query(CustomerSite.company_id)
            .filter(CustomerSite.company_id.in_(remaining), CustomerSite.owner_id == user.id)
            .distinct()
        )
        remaining = ids - manageable
    if remaining:
        manageable.update(
            int(cid)
            for (cid,) in db.query(AccountCollaborator.company_id)
            .filter(AccountCollaborator.company_id.in_(remaining), AccountCollaborator.user_id == user.id)
            .distinct()
        )
    return manageable


def require_prospect_site_access(db: Session, user: User, pc: "ProspectContact") -> None:
    """Gate a mutation on a site-linked prospect contact on account-management rights.

    A prospect tied to a CustomerSite belongs to a customer account, so the actor must be
    able to manage that account (mirrors the promote-prospect path). Vendor-linked prospects
    (``pc.vendor_card_id``) are global and stay un-gated. Raises 403 if not authorized.

    Shared by the JSON prospect endpoints (``app/routers/ai.py``) and the HTMX
    vendor-prospect partials (``app/routers/htmx_views.py``) so the guard lives once.
    """
    if pc.customer_site_id and not pc.vendor_card_id:
        site = db.get(CustomerSite, pc.customer_site_id)
        company = db.get(Company, site.company_id) if site else None
        if not company or not can_manage_account(user, company, db):
            raise HTTPException(403, "Not authorized to manage this account")


def can_manage_account_team(user: User, company: Company) -> bool:
    """True if *user* may add/remove collaborators or change the primary owner.

    This is a STRICTER gate than can_manage_account. Helper collaborators and
    site-owners are excluded — only the primary account owner and manager/admin
    may alter the team roster.

    Allowed when:
    - is_manager_or_admin(user)  — supervisors manage all teams
    - company.account_owner_id == user.id  — primary owner manages their own team

    Intentionally does NOT accept collaborators, site-owners, or buyers.
    """
    return bool(
        is_manager_or_admin(user) or (company.account_owner_id is not None and company.account_owner_id == user.id)
    )


def _require_admin_user(request: Request, db: Session, *, agent_msg: str, role_msg: str) -> User:
    """Resolve an admin-only user, blocking the agent service account.

    Raises 403 with *agent_msg* if the caller is the agent account, or *role_msg* if the
    caller is not an admin.
    """
    user = require_user(request, db)
    if user.email == _AGENT_EMAIL:
        raise HTTPException(403, agent_msg)
    if user.role != UserRole.ADMIN:
        raise HTTPException(403, role_msg)
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: raises 403 if user is not an admin. Blocks agent service account."""
    return _require_admin_user(
        request,
        db,
        agent_msg="Agent keys cannot access admin endpoints",
        role_msg="Admin access required",
    )


def require_settings_access(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: allows admin only. Blocks agent service account."""
    return _require_admin_user(
        request,
        db,
        agent_msg="Agent keys cannot access settings",
        role_msg="Settings access required",
    )


# Roles allowed through require_buyer. Single source of truth — templates that hide
# buyer-only UI (e.g. the materials workspace "Add part" button) check the SAME set
# via has_buyer_role, so the surface can never show an action the POST would 403.
BUYER_ROLES = frozenset({UserRole.BUYER, UserRole.SALES, UserRole.TRADER, UserRole.MANAGER, UserRole.ADMIN})


def require_requisition_access(
    db: Session,
    req_id: int | None,
    user: User,
    *,
    owner_id: int | None = None,
    label: str = "Requisition",
) -> None:
    """Enforce role-scoped ownership for an action on a requisition-scoped resource.

    No-op for unrestricted roles (buyer/manager/admin). For SALES/TRADER, allows the
    action only when the user owns the requisition (created_by) or, for unscoped/scratch
    resources where ``req_id`` is None, when ``owner_id`` matches the user. Raises
    HTTPException(404) otherwise (404 not 403 so existence isn't leaked).
    """
    if getattr(user, "role", None) not in RESTRICTED_ROLES:
        return
    if req_id is not None:
        req = db.get(Requisition, req_id)
        if req is not None and req.created_by == user.id:
            return
    if owner_id is not None and owner_id == user.id:
        return
    raise HTTPException(status_code=404, detail=f"{label} not found")


def require_requisition_access_bulk(
    db: Session,
    req_ids: Iterable[int | None],
    user: User,
    *,
    label: str = "Requisition",
) -> None:
    """Bulk equivalent of ``require_requisition_access`` — one query for many ids.

    Batched equivalent of calling ``require_requisition_access`` once per id in a loop
    (the 6 batch endpoints in ``routers/sightings.py`` used to do up to
    ``MAX_BATCH_SIZE`` sequential ``db.get()`` calls for SALES/TRADER users): a single
    ``id IN (...)`` select resolves every ``created_by`` in one round trip instead of one
    per item — mirrors the batching rationale of ``_manageable_company_ids``
    (routers/htmx/companies.py). No-op for unrestricted roles (buyer/manager/admin),
    same fast path as the single-item version. For SALES/TRADER, every id in *req_ids*
    must resolve to a requisition owned (``created_by``) by *user*; any missing or
    non-owned id raises ``HTTPException(404)`` exactly like the single-item version (404
    not 403 so existence isn't leaked). ``None`` entries and duplicates in *req_ids* are
    ignored/deduplicated so callers can pass a set, or a list with repeats, directly.
    Does not support the single-item version's ``owner_id`` fallback (unscoped/scratch
    resources) — none of the current bulk call sites need it.
    """
    if getattr(user, "role", None) not in RESTRICTED_ROLES:
        return
    ids = {rid for rid in req_ids if rid is not None}
    if not ids:
        return
    owners: dict[int, int | None] = {
        row.id: row.created_by
        for row in db.execute(select(Requisition.id, Requisition.created_by).where(Requisition.id.in_(ids)))
    }
    for rid in ids:
        if owners.get(rid) != user.id:
            raise HTTPException(status_code=404, detail=f"{label} not found")


def has_buyer_role(user: User | None) -> bool:
    """True when *user* holds a buyer-tier role (require_buyer's allowed set)."""
    return user is not None and user.role in BUYER_ROLES


def require_buyer(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: requires a buyer-tier role for RFQ actions.

    UserRole.AGENT is intentionally absent from the allowed set: the agent
    service account is non-interactive and must reach only non-privileged
    (require_user-level) endpoints — never RFQ/buyer actions.
    """
    user = require_user(request, db)
    if not has_buyer_role(user):
        raise HTTPException(403, "Buyer role required for this action")
    return user


# ── Buy-plan approval right (per-user grant) ──────────────────────────


def can_approve_buy_plans(user: User | None) -> bool:
    """True if *user* holds the per-user buy-plan approval right.

    Reads the User.can_approve_buy_plans column directly (single source of truth — the
    grant is admin-toggled in Users settings, not role-derived). This predicate is the
    shared gate so any surface that hides the approve/reject UI checks the SAME flag the
    POST enforces via require_buyplan_approver.
    """
    return bool(user is not None and getattr(user, "can_approve_buy_plans", False))


def require_buyplan_approver(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: 403 unless the current user holds the buy-plan approval right.

    Gates the buy-plan approve/reject action (wired by a separate task). The right is a
    per-user grant (User.can_approve_buy_plans), not a role — see can_approve_buy_plans.
    """
    user = require_user(request, db)
    if not can_approve_buy_plans(user):
        raise HTTPException(403, "Buy-plan approval right required for this action")
    return user


def can_approve_purchase_orders(user: User | None) -> bool:
    """True if *user* holds the per-user purchase-order approval right.

    Reads the User.can_approve_purchase_orders column directly (single source of truth —
    the grant is admin-toggled in Users settings, not role-derived). Parallel to
    :func:`can_approve_buy_plans`: it is the shared gate so any surface that hides the
    verify-PO UI checks the SAME flag the POST enforces via require_buyplan_po_approver.
    """
    return bool(user is not None and getattr(user, "can_approve_purchase_orders", False))


def can_verify_po_line(user: User | None, line) -> bool:
    """True if *user* may verify/reject THIS line's PO (right + per-line dollar limit).

    Wraps :func:`can_approve_purchase_orders` with the same admin-configured
    ``purchase_order_approval_limit`` check ``verify_po`` enforces against the line's
    dollar amount (NULL = unlimited) — the per-line UI predicate, so a line above the
    viewer's limit hides the Verify/Reject buttons instead of 403ing on submit. Lazy
    import: buyplan_workflow lazily imports this module, so a top-level import back
    would be circular.
    """
    if not can_approve_purchase_orders(user):
        return False
    limit: float | None = getattr(user, "purchase_order_approval_limit", None)
    if limit is None:
        return True
    from .services.buyplan_workflow import _line_amount

    return _line_amount(line) <= limit


# Buy-plan header statuses at/after which a NEW prepayment request makes no sense: the
# three terminal states (a dead plan keeps VERIFIED lines) plus INBOUND (goods already
# inbound, awaiting receipt). Single source of truth shared by create_prepayment (the
# service guard) and can_request_prepayment (the button-visibility predicate) so the two
# never drift.
PREPAYMENT_BLOCKED_PLAN_STATUSES = frozenset(
    {
        BuyPlanStatus.COMPLETED.value,
        BuyPlanStatus.CANCELLED.value,
        BuyPlanStatus.HALTED.value,
        BuyPlanStatus.INBOUND.value,
    }
)


def can_request_prepayment(user: User | None, line) -> bool:
    """True if *user* may request a prepayment on THIS PO line.

    Mirrors the gate ``create_prepayment`` enforces so the Request-prepayment button hides
    exactly where the service would reject the request:
      - the parent buy plan must not be terminal/inbound (see
        ``PREPAYMENT_BLOCKED_PLAN_STATUSES``); and
      - the line must have a cut PO (``po_number`` set) in PENDING_VERIFY / VERIFIED; and
      - the actor must be able to access the parent buy plan — the SAME rule
        ``get_buyplan_for_user`` applies (restricted roles must own the parent Requisition;
        buyer/manager/admin are unrestricted).

    The ownership rule is replicated inline (reading the already-loaded ``line.buy_plan``
    → ``requisition``) rather than re-querying via ``get_buyplan_for_user`` so this
    per-row template predicate does no extra DB round-trip — the same shape as
    :func:`can_verify_po_line`, which is also a pure ``(user, line)`` predicate.
    """
    if user is None or line is None:
        return False
    from .constants import BuyPlanLineStatus

    plan = getattr(line, "buy_plan", None)
    if plan is not None and getattr(plan, "status", None) in PREPAYMENT_BLOCKED_PLAN_STATUSES:
        return False
    if not line.po_number or line.status not in (
        BuyPlanLineStatus.PENDING_VERIFY.value,
        BuyPlanLineStatus.VERIFIED.value,
    ):
        return False
    if getattr(user, "role", None) not in RESTRICTED_ROLES:
        return True
    req = getattr(plan, "requisition", None) if plan is not None else None
    return bool(req is not None and req.created_by == user.id)


def require_buyplan_po_approver(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: 403 unless the current user holds the purchase-order approval right.

    Gates the buy-plan line verify-PO action. Parallel to ``require_buyplan_approver``:
    the right is a per-user grant (User.can_approve_purchase_orders), not a role — see
    can_approve_purchase_orders. (Phase D: PO verification moved off the ops
    verification-group membership onto this manager-held right.)
    """
    user = require_user(request, db)
    if not can_approve_purchase_orders(user):
        raise HTTPException(403, "Purchase-order approval right required for this action")
    return user


# ── QP section review rights (per-user grants; decision C — reviewer, not approver) ──


def can_review_qp_sales_section(user: User | None) -> bool:
    """True if *user* may mark the QP Sales section reviewed.

    Reuses the existing per-user User.can_approve_qp_sales column (migrations 160/166),
    reframed from "approver" to "reviewer" semantics under decision C's lightweight
    fold: marking a section reviewed is an instant per-section toggle, not a routed
    approval. Shared gate so the template hides the Mark/Unmark-Reviewed controls using
    the SAME flag toggle_section_reviewed enforces on the POST.
    """
    return bool(user is not None and getattr(user, "can_approve_qp_sales", False))


def can_review_qp_purchasing_section(user: User | None) -> bool:
    """True if *user* may mark the QP Purchasing section reviewed.

    Reuses the existing per-user User.can_approve_qp_purchasing column (migrations
    160/166) with reviewer semantics — parallel to :func:`can_review_qp_sales_section`.
    """
    return bool(user is not None and getattr(user, "can_approve_qp_purchasing", False))


def require_approval_gatekeeper(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
) -> User:
    """Dependency: 403 unless the current user is a PENDING recipient on this request.

    Resolves request_id from path param "id". Checks:
      - Direct PENDING row: ApprovalStepRecipient.user_id == user.id
      - Delegate row: ApprovalStepRecipient.reassigned_to_id == user.id (status PENDING)

    Using Depends(require_user) so test overrides propagate correctly.
    """
    from .models.approvals import ApprovalStep, ApprovalStepRecipient

    request_id_str = request.path_params.get("id")
    if not request_id_str:
        raise HTTPException(403, "No request id in path")

    try:
        request_id = int(request_id_str)
    except (ValueError, TypeError) as e:
        raise HTTPException(403, "Invalid request id") from e

    # Direct PENDING recipient
    recipient = db.execute(
        select(ApprovalStepRecipient)
        .join(ApprovalStep, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .where(
            ApprovalStep.request_id == request_id,
            ApprovalStepRecipient.user_id == user.id,
            ApprovalStepRecipient.status == ApprovalRecipientStatus.PENDING,
        )
    ).scalar_one_or_none()

    if recipient is not None:
        return user

    # Delegate: another slot was reassigned_to_id == user.id and is still PENDING
    delegate = db.execute(
        select(ApprovalStepRecipient)
        .join(ApprovalStep, ApprovalStepRecipient.step_id == ApprovalStep.id)
        .where(
            ApprovalStep.request_id == request_id,
            ApprovalStepRecipient.reassigned_to_id == user.id,
            ApprovalStepRecipient.status == ApprovalRecipientStatus.PENDING,
        )
    ).scalar_one_or_none()

    if delegate is not None:
        return user

    raise HTTPException(403, "You are not a pending recipient of this approval request")


# ── Per-feature access (user-management foundation) ───────────────────


def user_has_access(user: User, key, db: Session | None = None) -> bool:
    """True if *user* may use the access *key* (an AccessKey or its str value).

    admin → always True. ops_verification delegates to VerificationGroupMember (single
    source of truth). Otherwise: explicit per-user override wins, else role default. An
    unknown key (not in AccessKey) is denied.
    """
    if user.role == UserRole.ADMIN:
        return True
    key_str = str(key)
    if key_str == AccessKey.OPS_VERIFICATION:
        if db is None:
            return False
        from .models.buy_plan import VerificationGroupMember

        m = db.query(VerificationGroupMember).filter_by(user_id=user.id).first()
        return bool(m and m.is_active)
    overrides = cast(dict, user.access_overrides or {})
    if key_str in overrides:
        return bool(overrides[key_str])
    try:
        ak = AccessKey(key_str)
    except ValueError:
        return False
    return ak in ROLE_ACCESS_DEFAULTS.get(user.role, frozenset())  # type: ignore[call-overload]  # user.role is a plain str at instance level; StrEnum-keyed lookup by str works


def can_export_bulk_data(user: User | None) -> bool:
    """True if *user* may export bulk CRM/vendor/requisition/sighting datasets.

    Single source of truth for the Settings "Data export" page (tab button AND tab
    route) — the SAME predicate ``require_access(AccessKey.EXPORT_BULK_DATA)`` enforces
    on the export routes (ISS-028: companies/contacts/vendors/requisitions/sightings
    exports are admin-only by default; supersedes ISS-022's manager+admin default — a
    manager may still be granted the capability via an explicit per-user
    access_overrides grant, which also surfaces the Settings tab).
    No list toolbar renders export controls anymore (ISS-028). Quote-builder
    single-deal Excel/PDF exports stay gated on ``EXPORT_DATA`` and are unaffected by
    this predicate.

    ``isinstance`` (not a bare None check) so this degrades to False — never raises —
    for a Jinja ``Undefined`` sentinel or a test-harness stand-in object lacking
    ``.role``/``.access_overrides`` (template-compilation smoke tests render every
    partial with a minimal dummy context); a real request always injects a genuine
    ``User`` row here.
    """
    return isinstance(user, User) and user_has_access(user, AccessKey.EXPORT_BULK_DATA)


def require_access(key):
    """Dependency factory: 403 unless the current user has *key*.

    Depends on ``require_user`` via ``Depends`` (not a direct call) so that test
    ``dependency_overrides[require_user]`` — and any future require_user wrapper —
    flow through to the access check unchanged.
    """

    def _dep(user: User = Depends(require_user), db: Session = Depends(get_db)) -> User:
        if not user_has_access(user, key, db):
            raise HTTPException(403, "You don't have access to this feature")
        return user

    return _dep


# ── Query Helpers ─────────────────────────────────────────────────────


def get_req_for_user(db: Session, user: User, req_id: int, options=None) -> Requisition:
    """Get a single requisition with role-based access check.

    Args:
        options: Additional SQLAlchemy loader options (e.g., joinedload).
                 Defaults to selectinload(Requisition.requirements).
    """
    load_opts = options or [selectinload(Requisition.requirements)]
    q = db.query(Requisition).options(*load_opts).filter_by(id=req_id)
    if user.role in RESTRICTED_ROLES:
        q = q.filter_by(created_by=user.id)
    req = q.first()
    if not req:
        raise HTTPException(status_code=404, detail="Requisition not found")
    return req


def get_quote_for_user(db: Session, user: User, quote_id: int, options=None) -> Quote:
    """Get a single quote with role-based requisition ownership checks."""
    load_opts = options or []
    q = (
        db.query(Quote)
        .options(*load_opts)
        .join(Requisition, Quote.requisition_id == Requisition.id)
        .filter(Quote.id == quote_id)
    )
    if user.role in RESTRICTED_ROLES:
        q = q.filter(Requisition.created_by == user.id)
    quote = q.first()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")
    return quote


def get_buyplan_for_user(db: Session, user: User, plan_id: int, options=None) -> BuyPlan:
    """Get a single buy plan with role-based requisition ownership checks.

    Ownership derives through the parent Requisition (BuyPlan.requisition_id is NOT
    NULL).
    """
    load_opts = options or []
    q = (
        db.query(BuyPlan)
        .options(*load_opts)
        .join(Requisition, BuyPlan.requisition_id == Requisition.id)
        .filter(BuyPlan.id == plan_id)
    )
    if user.role in RESTRICTED_ROLES:
        q = q.filter(Requisition.created_by == user.id)
    plan = q.first()
    if not plan:
        raise HTTPException(status_code=404, detail="Buy plan not found")
    return plan


# ── Token Management ──────────────────────────────────────────────────


async def require_fresh_token(request: Request, db: Session = Depends(get_db)) -> str:
    """Return a valid M365 access token, raising 401 only if truly expired.

    Tokens stored in DB (not just session) so background jobs can use them. The
    background scheduler job (_job_token_refresh) refreshes tokens proactively when
    within 15 min of expiry — no inline refresh is done here to avoid latency spikes on
    request handlers.
    """
    user = get_user(request, db)
    if not user:
        raise HTTPException(401, "Not authenticated — please log in")

    token = user.access_token
    if not token:
        raise HTTPException(401, "No access token — please log in")

    # The background scheduler job (_job_token_refresh) refreshes proactively within the
    # 15-min buffer, so the only failure case to handle inline is a truly-expired token
    # (background job missed it or no refresh token) — everything else uses the DB token.
    if user.token_expires_at:
        expiry = user.token_expires_at if user.token_expires_at.tzinfo else user.token_expires_at.replace(tzinfo=UTC)
        if datetime.now(UTC) > expiry:
            user.m365_connected = False  # type: ignore[assignment]  # ORM Column[bool] instance write
            db.commit()
            raise HTTPException(401, "Session expired — please log in again")

    return str(token)
