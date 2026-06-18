// tests/frontend/materials-filter-numeric-chips.test.ts — P2 common-value chips on the
// materialsFilter Alpine component: toggleNumericChip maintains subFilters['{spec}__vals']
// as a NUMBER array (add/remove, delete-when-empty), the URL round-trips the comma-joined
// number list (pushURL emits, syncFromURL parses back to numbers and drops NaN), __vals
// selections feed activeFilterCount, and clearSubFilters / clearAllFilters drop them.
// Mirrors the mock harness in materials-filter-sourcing.test.ts.
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

function makeComponent() {
  const c = registry['materialsFilter']()
  c.$el = { dataset: { displayNames: '{}' } }
  return c
}

beforeEach(async () => {
  registry = {}
  alpineMock.data = (n: string, f: any) => { registry[n] = f }
  vi.resetModules()
  history.replaceState({}, '', '/v2/materials')
  await import('../../app/static/htmx_app.js')
})

describe('materialsFilter numeric chips — toggleNumericChip', () => {
  it('adds a NUMBER to subFilters[spec__vals] and removes it on second click', () => {
    const c = makeComponent()
    c.init()
    c.toggleNumericChip('capacity_gb', 8)
    expect(c.subFilters['capacity_gb__vals']).toEqual([8])
    // Stored as a JS number (the :class includes() membership check compares numbers).
    expect(typeof c.subFilters['capacity_gb__vals'][0]).toBe('number')
    c.toggleNumericChip('capacity_gb', 32)
    expect(c.subFilters['capacity_gb__vals']).toEqual([8, 32])
    // Toggling 8 off leaves only 32.
    c.toggleNumericChip('capacity_gb', 8)
    expect(c.subFilters['capacity_gb__vals']).toEqual([32])
  })

  it('deletes the key entirely when the last value is toggled off', () => {
    const c = makeComponent()
    c.init()
    c.toggleNumericChip('capacity_gb', 16)
    c.toggleNumericChip('capacity_gb', 16)
    expect('capacity_gb__vals' in c.subFilters).toBe(false)
  })
})

describe('materialsFilter numeric chips — URL round-trip', () => {
  it('pushURL emits a comma-joined number list under sf_{spec}__vals', () => {
    const c = makeComponent()
    c.init()
    c.toggleNumericChip('capacity_gb', 8)
    c.toggleNumericChip('capacity_gb', 32)
    c.pushURL()
    const params = new URLSearchParams(window.location.search)
    expect(params.get('sf_capacity_gb__vals')).toBe('8,32')
  })

  it('syncFromURL parses the list back to NUMBERS and drops NaN', () => {
    history.replaceState({}, '', '/v2/materials?commodity=dram&sf_capacity_gb__vals=8,abc,32')
    const c = makeComponent()
    c.init()
    expect(c.subFilters['capacity_gb__vals']).toEqual([8, 32])
    expect(c.subFilters['capacity_gb__vals'].every((v: any) => typeof v === 'number')).toBe(true)
  })

  it('drops empty CSV segments — no phantom 0 from a truncated link (Number("")===0)', () => {
    history.replaceState({}, '', '/v2/materials?commodity=dram&sf_capacity_gb__vals=8,')
    const c = makeComponent()
    c.init()
    expect(c.subFilters['capacity_gb__vals']).toEqual([8])  // NOT [8, 0]
  })

  it('ignores an empty __vals param entirely (no key set)', () => {
    history.replaceState({}, '', '/v2/materials?commodity=dram&sf_capacity_gb__vals=')
    const c = makeComponent()
    c.init()
    expect('capacity_gb__vals' in c.subFilters).toBe(false)
  })

  it('round-trips push → sync without drift (copied link restores selection)', () => {
    const c1 = makeComponent()
    c1.init()
    c1.toggleNumericChip('voltage', 1.5)
    c1.toggleNumericChip('voltage', 3.3)
    c1.pushURL()
    const url = window.location.pathname + window.location.search
    history.replaceState({}, '', url)
    const c2 = makeComponent()
    c2.init()
    expect(c2.subFilters['voltage__vals']).toEqual([1.5, 3.3])
  })
})

describe('materialsFilter numeric chips — count + clear', () => {
  it('__vals selections feed activeFilterCount (one per value)', () => {
    const c = makeComponent()
    c.init()
    const base = c.activeFilterCount
    c.toggleNumericChip('capacity_gb', 8)
    c.toggleNumericChip('capacity_gb', 16)
    expect(c.activeFilterCount).toBe(base + 2)
  })

  it('clearSubFilters and clearAllFilters drop __vals keys', () => {
    const c = makeComponent()
    c.init()
    c.toggleNumericChip('capacity_gb', 8)
    c.clearSubFilters()
    expect(c.subFilters).toEqual({})
    c.toggleNumericChip('capacity_gb', 8)
    c.clearAllFilters()
    expect(c.subFilters).toEqual({})
  })

  it('removeFilter (applied-strip × button) drops one __vals NUMBER from the array', () => {
    // The applied-filter strip calls removeFilter(fullKey, value) with the full "__vals"
    // key and a NUMBER — exercise that number-equality path, distinct from toggleNumericChip.
    const c = makeComponent()
    c.init()
    c.toggleNumericChip('capacity_gb', 8)
    c.toggleNumericChip('capacity_gb', 32)
    c.removeFilter('capacity_gb__vals', 8)
    expect(c.subFilters['capacity_gb__vals']).toEqual([32])
    // Removing the last value deletes the key entirely.
    c.removeFilter('capacity_gb__vals', 32)
    expect('capacity_gb__vals' in c.subFilters).toBe(false)
  })
})
