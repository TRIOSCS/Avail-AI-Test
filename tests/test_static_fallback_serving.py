"""The /static mount must serve assets from BOTH the built dist and the source tree.

Public images + bundled js live only in app/static/dist (Vite copies publicDir into the
dist root); unbundled source CSS lives only in app/static. A single-directory mount 404s
one set or the other — the fallback mount resolves both. Regression for the
avail_logo_tight.png 404.
"""

import os

os.environ["TESTING"] = "1"


def test_static_public_image_served_from_dist(client):
    # avail_logo_tight.png exists only under app/static/dist (publicDir copy).
    r = client.get("/static/avail_logo_tight.png")
    assert r.status_code == 200, "public image (dist-only) must serve via /static"
    assert r.headers.get("content-type", "").startswith("image/")


def test_static_source_css_served_from_source(client):
    # styles.css exists only under app/static (source, unbundled) — fallback serves it.
    r = client.get("/static/styles.css")
    assert r.status_code == 200, "unbundled source CSS must still serve via /static"


def test_static_missing_file_404(client):
    assert client.get("/static/definitely-not-a-real-asset.xyz").status_code == 404
