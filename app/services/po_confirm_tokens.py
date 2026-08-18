"""services/po_confirm_tokens.py — signed tokens for the buyer's no-login PO confirm.

Purpose: the approval email's per-line "Confirm PO" link must work from a phone with no
AVAIL session (Deal Sheet T4 — the same accounting pay-link idea, applied to the buyer
leg). BuyPlanLine has no token column (and the Deal Sheet ships without migrations), so
the token is STATELESS: an itsdangerous URLSafeTimedSerializer signature over
{line_id, buyer_id} with a dedicated salt. Single-use is enforced by STATE, not by the
token: confirm_po only acts on an AWAITING_PO line, so a spent link renders the
read-only "already confirmed" page.

Called by: routers/po_confirm.py (resolve), services/buyplan_notifications.py (mint).
Depends on: app.config (settings.secret_key), itsdangerous.
"""

from itsdangerous import BadSignature, URLSafeTimedSerializer

from ..config import settings

_SALT = "buyplan-po-confirm"

# Long enough that a busy week doesn't expire the buyer's link; the line's own state
# machine (awaiting_po only) is the real guard, so a long window leaks nothing.
MAX_AGE_SECONDS = 21 * 24 * 3600


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(str(settings.secret_key), salt=_SALT)


def mint_po_confirm_token(line_id: int, buyer_id: int) -> str:
    """The signed token for one buyer's confirm link on one line."""
    return str(_serializer().dumps({"l": int(line_id), "b": int(buyer_id)}))


def resolve_po_confirm_token(token: str) -> tuple[int, int] | None:
    """(line_id, buyer_id) for a valid, unexpired token; None for anything else."""
    try:
        data = _serializer().loads(token, max_age=MAX_AGE_SECONDS)
    except BadSignature:  # covers SignatureExpired (a subclass) too
        return None
    try:
        return int(data["l"]), int(data["b"])
    except (KeyError, TypeError, ValueError):
        return None
