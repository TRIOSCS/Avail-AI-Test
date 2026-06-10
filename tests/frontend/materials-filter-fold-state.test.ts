// tests/frontend/materials-filter-fold-state.test.ts — fold-state defaults on the
// materialsFilter Alpine component: Data confidence (trust) opens by default, the heavy
// folds (sourcing / more attributes) start closed, and the legacy 'mat_confidence_open'
// localStorage key (which holds a persisted `false` for every pre-rotation visitor) is
// removed at module load. Mirrors the mock harness in materials-filter-sourcing.test.ts.
// Static twin: tests/test_static_analysis.py::test_materials_fold_state_defaults_pinned.
import { describe, it, expect, beforeEach, vi } from 'vitest'

let registry: Record<string, any> = {}

vi.mock('htmx.org', () => ({
  default: {
    on: vi.fn(), off: vi.fn(), ajax: vi.fn(), process: vi.fn(),
    defineExtension: vi.fn(), createExtension: vi.fn(),
    config: {},
  },
}))

const alpineMock = {
  data: (n: string, f: any) => { registry[n] = f },
  store: vi.fn(),
  plugin: vi.fn(),
  start: vi.fn(),
  directive: vi.fn(),
  magic: vi.fn(),
}

vi.mock('alpinejs', () => ({ default: alpineMock }))
vi.mock('@alpinejs/focus', () => ({ default: vi.fn() }))
vi.mock('@alpinejs/persist', () => ({ default: vi.fn() }))
vi.mock('@alpinejs/intersect', () => ({ default: vi.fn() }))
vi.mock('@alpinejs/collapse', () => ({ default: vi.fn() }))
vi.mock('@alpinejs/morph', () => ({ default: vi.fn() }))
vi.mock('@alpinejs/mask', () => ({ default: vi.fn() }))
vi.mock('@alpinejs/sort', () => ({ default: vi.fn() }))
vi.mock('@alpinejs/anchor', () => ({ default: vi.fn() }))
vi.mock('@alpinejs/resize', () => ({ default: vi.fn() }))
vi.mock('htmx-ext-alpine-morph', () => ({}))
vi.mock('htmx-ext-response-targets', () => ({}))
vi.mock('htmx-ext-sse', () => ({}))
vi.mock('htmx-ext-json-enc', () => ({}))
vi.mock('htmx-ext-preload', () => ({}))
vi.mock('htmx-ext-loading-states', () => ({}))
vi.mock('htmx-ext-path-deps', () => ({}))
vi.mock('htmx-ext-remove-me', () => ({}))

beforeEach(() => {
  registry = {}
  alpineMock.data = (n: string, f: any) => { registry[n] = f }
  vi.resetModules()
  localStorage.clear()
  history.replaceState({}, '', '/v2/materials')
})

describe('materialsFilter fold-state defaults (fresh browser, no persisted state)', () => {
  it('opens the Data-confidence (trust) fold and keeps the heavy folds closed', async () => {
    await import('../../app/static/htmx_app.js')
    const c = registry['materialsFilter']()
    // The headline change of the layout-polish spec: trust filter expanded by default.
    expect(c.confidenceOpen).toBe(true)
    // Collapse policy: the heavy folds stay closed until the user opens them.
    expect(c.sourcingOpen).toBe(false)
    expect(c.moreAttrsOpen).toBe(false)
  })

  it('removes the legacy mat_confidence_open key on load (key rotated to v2)', async () => {
    // Every browser that loaded the page under the old `persistOr(false, ...)` default
    // has `false` persisted — @alpinejs/persist writes the current value on init. The
    // module-load migration must drop it so the key can never override the new default.
    localStorage.setItem('mat_confidence_open', 'false')
    await import('../../app/static/htmx_app.js')
    expect(localStorage.getItem('mat_confidence_open')).toBeNull()
  })
})
