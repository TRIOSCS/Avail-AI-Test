// tests/frontend/ratio-split-mixin.test.ts — window.ratioSplitMixin (htmx_app.js):
// the shared drag-to-resize state spread into the sightings / Sales-Hub parts /
// Approvals-workspace split x-data objects. Covers the stored-ratio vs default seed,
// startDrag/onDrag clamp math against $refs.container, and stopDrag's toFixed(3)
// localStorage persistence — each surface keeps its own key + clamp bounds.
// Mirrors the mock harness in materials-filter-numeric-chips.test.ts.
import { describe, it, expect, beforeEach, vi } from 'vitest'

vi.mock('htmx.org', () => ({
  default: {
    on: vi.fn(), off: vi.fn(), ajax: vi.fn(), process: vi.fn(),
    defineExtension: vi.fn(), createExtension: vi.fn(),
    config: {},
  },
}))

const alpineMock = {
  data: vi.fn(),
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

// Build a mixin instance with a stubbed x-ref container (left edge 0, width 1000).
function makeMixin(key = 'test-split', def = 0.5, min = 0.25, max = 0.75) {
  const m: any = (window as any).ratioSplitMixin(key, def, min, max)
  m.$refs = { container: { getBoundingClientRect: () => ({ left: 0, width: 1000 }) } }
  return m
}

beforeEach(async () => {
  localStorage.clear()
  vi.resetModules()
  await import('../../app/static/htmx_app.js')
})

describe('ratioSplitMixin — seed ratio', () => {
  it('seeds splitRatio from the default when nothing is stored', () => {
    expect(makeMixin('aw-split', 0.42, 0.25, 0.75).splitRatio).toBe(0.42)
  })

  it('seeds splitRatio from the surface-keyed stored value when present', () => {
    localStorage.setItem('sightings-split', '0.612')
    expect(makeMixin('sightings-split', 0.5, 0.25, 0.75).splitRatio).toBe(0.612)
    // A different surface's key is untouched by that storage.
    expect(makeMixin('parts-split', 0.5, 0.2, 0.8).splitRatio).toBe(0.5)
  })
})

describe('ratioSplitMixin — drag math', () => {
  it('startDrag sets dragging and prevents the default (text selection)', () => {
    const m = makeMixin()
    const e = { preventDefault: vi.fn() }
    m.startDrag(e)
    expect(m.dragging).toBe(true)
    expect(e.preventDefault).toHaveBeenCalledOnce()
  })

  it('onDrag is a no-op unless dragging', () => {
    const m = makeMixin()
    m.onDrag({ clientX: 300 })
    expect(m.splitRatio).toBe(0.5)
  })

  it('onDrag tracks the pointer as a ratio of container width', () => {
    const m = makeMixin()
    m.startDrag({ preventDefault: vi.fn() })
    m.onDrag({ clientX: 300 })
    expect(m.splitRatio).toBe(0.3)
  })

  it('onDrag clamps to the surface-specific min/max bounds', () => {
    const m = makeMixin('test-split', 0.5, 0.25, 0.75)
    m.startDrag({ preventDefault: vi.fn() })
    m.onDrag({ clientX: 10 })
    expect(m.splitRatio).toBe(0.25)
    m.onDrag({ clientX: 990 })
    expect(m.splitRatio).toBe(0.75)
    // The wider Sales-Hub bounds clamp differently.
    const wide = makeMixin('parts-split', 0.5, 0.2, 0.8)
    wide.startDrag({ preventDefault: vi.fn() })
    wide.onDrag({ clientX: 10 })
    expect(wide.splitRatio).toBe(0.2)
  })
})

describe('ratioSplitMixin — persistence', () => {
  it('stopDrag persists the ratio toFixed(3) under the surface key and ends the drag', () => {
    const m = makeMixin('test-split', 0.5, 0.25, 0.75)
    m.startDrag({ preventDefault: vi.fn() })
    m.onDrag({ clientX: 333 })
    m.stopDrag()
    expect(m.dragging).toBe(false)
    expect(localStorage.getItem('test-split')).toBe('0.333')
  })

  it('stopDrag without an active drag writes nothing (mouseup passthrough)', () => {
    const m = makeMixin('test-split')
    m.stopDrag()
    expect(localStorage.getItem('test-split')).toBeNull()
  })
})
