"""test_user_avatar_macro.py — the shared user_avatar(user, size) Jinja macro.

Covers app/templates/htmx/partials/shared/_macros.html user_avatar:
  - renders an <img> pointing at /api/user/avatar/{filename} when avatar_path is set;
  - renders the initials fallback (accent-tinted circle) when avatar_path is None;
  - renders a neutral '?' circle when user is None (so callers need no None guard);
  - size sm/md/lg select the documented dimension classes.

Called by: pytest
Depends on: app.template_env (templates.env), _macros.html
"""

from types import SimpleNamespace

import pytest

from app.template_env import templates

ENV = templates.env


def render(call_expr: str, **ctx) -> str:
    tpl = ENV.from_string(
        '{% from "htmx/partials/shared/_macros.html" import user_avatar %}' + call_expr,
    )
    return tpl.render(**ctx).strip()


def _user(name=None, email=None, avatar_path=None):
    return SimpleNamespace(name=name, email=email, avatar_path=avatar_path)


# ── Photo branch ──────────────────────────────────────────────────────


def test_renders_img_when_avatar_path_set():
    html = render("{{ user_avatar(u) }}", u=_user(name="Ada", avatar_path="user_1_abcd1234.png"))
    assert "<img" in html
    assert 'src="/api/user/avatar/user_1_abcd1234.png"' in html
    assert "object-cover" in html
    # Name surfaces as alt/title for the photo.
    assert "Ada" in html


# ── Fallback branch ───────────────────────────────────────────────────


def test_renders_initials_when_no_avatar_path():
    html = render("{{ user_avatar(u) }}", u=_user(name="Ada Lovelace"))
    assert "<img" not in html
    assert ">A<" in html  # first initial, uppercased
    assert "bg-brand-100" in html
    assert "text-brand-600" in html


def test_initials_use_email_when_name_missing():
    html = render("{{ user_avatar(u) }}", u=_user(email="zoe@trioscs.com"))
    assert ">Z<" in html


def test_none_user_renders_question_circle():
    html = render("{{ user_avatar(None) }}")
    assert "<img" not in html
    assert ">?<" in html
    assert "Unassigned" in html  # title fallback


# ── Sizes ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "size,dim",
    [("sm", "h-6 w-6"), ("md", "h-9 w-9"), ("lg", "h-12 w-12")],
)
def test_size_dimensions_fallback(size, dim):
    html = render(f"{{{{ user_avatar(u, size='{size}') }}}}", u=_user(name="Ada"))
    assert dim in html


@pytest.mark.parametrize(
    "size,w,h",
    [("sm", "h-6", "w-6"), ("md", "h-9", "w-9"), ("lg", "h-12", "w-12")],
)
def test_size_dimensions_photo(size, w, h):
    html = render(
        f"{{{{ user_avatar(u, size='{size}') }}}}",
        u=_user(name="Ada", avatar_path="user_1_abcd1234.png"),
    )
    assert w in html and h in html


def test_unknown_size_falls_back_to_sm():
    html = render("{{ user_avatar(u, size='xl') }}", u=_user(name="Ada"))
    assert "h-6 w-6" in html
