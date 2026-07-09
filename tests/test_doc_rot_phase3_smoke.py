# Phase-3 doc-rot cleanup smoke test.
# What it does: imports each module touched by the comment/docstring truth-fix and
#   asserts observable behavior is unchanged (no behavior edits were made).
# Called by: pytest.
# Depends on: app.main, app.routers.htmx_views, app.jobs.knowledge_jobs,
#   app.utils.file_validation, app.services.vendor_unavailability.
import inspect


def test_modules_import():
    import app.jobs.knowledge_jobs
    import app.main
    import app.routers.htmx_views
    import app.services.vendor_unavailability
    import app.utils.file_validation  # noqa: F401


def test_nav_id_alias_not_empty():
    from app.routers.htmx_views import _NAV_ID_ALIAS

    # The "Empty now" comment was stale; the dict carries real aliases.
    assert _NAV_ID_ALIAS == {
        "contacts": "crm",
        "vendor-contacts": "crm",
        "approvals": "buy-plans",
    }


def test_file_fingerprint_signature_and_behavior():
    from app.utils.file_validation import file_fingerprint

    # `rows` param removed (was unused); single positional arg remains.
    assert list(inspect.signature(file_fingerprint).parameters) == ["content"]
    # First 4KB drives the fingerprint: identical prefixes collide past 4096 bytes.
    a = b"X" * 4096 + b"tail-a"
    b = b"X" * 4096 + b"tail-b"
    assert file_fingerprint(a) == file_fingerprint(b)
    assert file_fingerprint(b"MPN,Qty\nLM317T,100") != file_fingerprint(b"MPN,Qty\nLM7805,200")
