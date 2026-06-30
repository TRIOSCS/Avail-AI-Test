"""materials_list_partial must enforce MATERIALS access.

GET /v2/partials/materials was gated only by require_user, then called
materials_workspace_partial() directly as a function — so the inner route's
Depends(require_access(MATERIALS)) never ran, leaking the full workspace to a user
without materials access. The list partial must carry the same gate.

Called by: pytest
Depends on: app.routers.htmx.materials, conftest (client, db_session, test_user)
"""

from app.constants import AccessKey


def test_materials_list_denied_without_materials_access(client, db_session, test_user):
    test_user.access_overrides = {AccessKey.MATERIALS.value: False}
    db_session.commit()
    assert client.get("/v2/partials/materials").status_code == 403


def test_materials_list_allowed_with_access(client, db_session, test_user):
    # Buyer default grants MATERIALS access.
    assert client.get("/v2/partials/materials").status_code == 200
