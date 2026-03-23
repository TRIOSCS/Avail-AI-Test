---
name: vite
description: |
  Configures Vite 6.x build system for the AvailAI FastAPI + HTMX stack: asset bundling,
  content-hashed fingerprinting, dev proxy, and Vitest unit testing.
  Use when: adding new JS/CSS entry points, modifying vite.config.js, debugging build
  output, configuring the dev proxy, writing Vitest tests for Alpine.js components or
  stores, running smoke tests after a production build.
allowed-tools: Read, Edit, Write, Glob, Grep, Bash
---

# Vite — AvailAI Build System

Vite 6.x bundles three entry points from `app/static/` into `app/static/dist/` with
content-hashed filenames. The manifest (`dist/.vite/manifest.json`) maps source names to
hashed output names. In production, FastAPI serves the hashed files via Caddy. In
development, the raw files are served directly from `app/static/` via `npm run dev`.

See the **htmx** and **frontend-design** skills for template/component patterns.

## Quick Start

```bash
# Development — Vite dev server on :5173, proxies /api /auth /health to FastAPI :8000
npm run dev

# Production build → app/static/dist/
npm run build

# Unit tests (Vitest + jsdom)
npm run test:vitest

# Watch mode
npm run test:vitest:watch

# Full frontend validation (lint + build + tests + Playwright)
npm run test:all-frontend
```

## Entry Points (`vite.config.js`)

```js
rollupOptions: {
  input: {
    htmx_app: resolve(__dirname, "app/static/htmx_app.js"),    // main bundle
    htmx_mobile: resolve(__dirname, "app/static/htmx_mobile.css"),
    styles: resolve(__dirname, "app/static/styles.css"),
  },
},
```

Add new entry points here only when a chunk is needed independently (e.g., a separate
admin bundle). Never create an entry point just to split code — Rollup handles that via
dynamic imports.

## Manifest-Based Asset Loading

```json
// app/static/dist/.vite/manifest.json
{
  "htmx_app.js": {
    "file": "assets/htmx_app-BkJmAFC8.js",
    "css": ["assets/styles-DKo8fjgb.css", "assets/htmx_mobile-CgLDqmJx.css"]
  }
}
```

The manifest is written to `dist/.vite/manifest.json`. If you need Jinja2 to inject
hashed URLs, read and cache this file at startup.

## Dev Proxy

```js
server: {
  proxy: {
    "/api": "http://localhost:8000",
    "/auth": "http://localhost:8000",
    "/health": "http://localhost:8000",
  },
},
```

All FastAPI routes (not just `/api`) must be explicitly listed. HTMX requests to `/v2/*`
are NOT proxied — run `docker compose up app` alongside `npm run dev` for full routing.

## Vitest Setup

`vitest.config.ts` uses `jsdom` environment and the `@static` alias:

```ts
resolve: {
  alias: { '@static': resolve(__dirname, 'app/static') },
},
```

Tests live in `tests/frontend/**/*.test.ts`. See [unit](references/unit.md) for patterns.

## See Also

- [unit](references/unit.md) — Vitest unit test patterns for Alpine.js components
- [integration](references/integration.md) — Build pipeline and proxy integration
- [mocking](references/mocking.md) — Mocking HTMX, Alpine, and browser APIs
- [fixtures](references/fixtures.md) — Test fixtures for Alpine stores and components

## Related Skills

- See the **htmx** skill for HTMX extension registration and usage
- See the **frontend-design** skill for Tailwind CSS + Alpine.js component patterns
- See the **playwright** skill for E2E browser test patterns
