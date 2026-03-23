# Vite — Build Pipeline & Integration Reference

## Contents
- Build pipeline overview
- Dev mode asset serving
- Manifest consumption in FastAPI
- Dev proxy configuration
- Adding entry points
- Post-build smoke tests
- DO/DON'T pairs

## Build Pipeline Overview

```
npm run build
  → Vite reads app/static/{htmx_app.js,styles.css,htmx_mobile.css}
  → Rollup bundles + tree-shakes
  → Terser minifies
  → app/static/dist/assets/{name}-{hash}.{js,css}
  → app/static/dist/.vite/manifest.json  ← source-of-truth for hashed URLs
  → postbuild: scripts/smoke-test-bundles.mjs (if file exists)
```

## Dev Mode Asset Serving

In development (`npm run dev`), `base.html` loads raw source files directly:

```html
<!-- app/templates/base.html -->
<link rel="stylesheet" href="/static/styles.css">
<script src="/static/htmx_app.js" type="module"></script>
```

Vite dev server on `:5173` serves `app/static/` at `/static/`. FastAPI runs on `:8000`.
The browser hits Vite; Vite proxies `/api`, `/auth`, `/health` to FastAPI.

HTMX partial requests to `/v2/*` are **not proxied** — they go directly to FastAPI.
You must run `docker compose up app` alongside `npm run dev` for HTMX navigation to work.

## Manifest Consumption

In production, Caddy serves the Docker container and FastAPI must inject hashed asset
URLs. If you add a Jinja2 helper to read the manifest:

```python
# app/utils/vite.py
import json
from pathlib import Path
from functools import lru_cache

MANIFEST_PATH = Path("app/static/dist/.vite/manifest.json")

@lru_cache(maxsize=1)
def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {}
    return json.loads(MANIFEST_PATH.read_text())

def asset_url(entry: str) -> str:
    manifest = load_manifest()
    if entry in manifest:
        return f"/static/dist/{manifest[entry]['file']}"
    return f"/static/{entry}"  # dev fallback
```

Cache with `lru_cache` — the manifest never changes at runtime. Do NOT read the file
on every request.

## Adding a New Entry Point

1. Create the source file in `app/static/`
2. Add it to `vite.config.js`:

```js
rollupOptions: {
  input: {
    htmx_app: resolve(__dirname, "app/static/htmx_app.js"),
    htmx_mobile: resolve(__dirname, "app/static/htmx_mobile.css"),
    styles: resolve(__dirname, "app/static/styles.css"),
    admin: resolve(__dirname, "app/static/admin.js"),  // new entry
  },
},
```

3. Run `npm run build` and verify the manifest includes the new entry.
4. Reference via `asset_url("admin.js")` in templates.

**Only add entry points for independently-loaded chunks** (e.g., a separate admin panel).
Never split a single page's assets across multiple entry points — it defeats tree-shaking.

## Dev Proxy Configuration

```js
// vite.config.js
server: {
  proxy: {
    "/api": "http://localhost:8000",
    "/auth": "http://localhost:8000",
    "/health": "http://localhost:8000",
  },
},
```

To proxy HTMX partial requests during development, add `/v2`:

```js
"/v2": "http://localhost:8000",
```

Without this, HTMX `hx-get="/v2/..."` requests hit `:5173` (Vite) instead of FastAPI
and return 404 in dev mode.

## Post-Build Smoke Tests

```bash
# package.json postbuild hook — runs automatically after npm run build
"postbuild": "if [ -f scripts/smoke-test-bundles.mjs ]; then node scripts/smoke-test-bundles.mjs; fi"
```

The smoke test validates bundle output (file existence, size limits, expected exports).
It runs automatically after every `npm run build`. To skip it, run Rollup directly.

## DO/DON'T Pairs

**DO** keep `manifest: true` in the build config — FastAPI needs it to resolve hashed
filenames without hardcoding them in templates.

**NEVER** import from `app/static/dist/` in source files. The dist folder is build
output; importing from it creates a circular dependency that breaks incremental builds.

**DO** set `emptyOutDir: true` so stale hashed chunks don't accumulate in `dist/assets/`
across builds. Without it, old files pile up and inflate the Docker image.

**NEVER** commit `app/static/dist/` to version control if it's in `.gitignore`. Verify
with `git check-ignore -v app/static/dist/` before committing.

**DO** use `sourcemap: false` in production (current config) — source maps expose source
code and roughly double the bundle size. Enable only in staging if actively debugging.
