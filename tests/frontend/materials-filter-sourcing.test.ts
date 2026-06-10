// tests/frontend/materials-filter-sourcing.test.ts — Layer-3 "Sourcing signals"
// state on the materialsFilter Alpine component: URL sync (read+write), the
// toggle/segment/chip/numeric setters, the active-count badge and clearAllFilters.
// Mirrors the mock harness in materials-filter-display-name.test.ts.
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

describe('materialsFilter sourcing signals — syncFromURL', () => {
  it('parses the operational params from the URL', () => {
    history.replaceState({}, '',
      '/v2/materials?has_stock=true&has_price=true&has_crosses=true&internal=internal&searched_within=30d&min_searches=4')
    const c = makeComponent()
    c.init()
    expect(c.hasStock).toBe(true)
    expect(c.hasPrice).toBe(true)
    expect(c.hasCrosses).toBe(true)
    expect(c.internal).toBe('internal')
    expect(c.searchedWithin).toBe('30d')
    expect(c.minSearches).toBe(4)
  })

  it('degrades unknown/negative values to the no-op defaults', () => {
    history.replaceState({}, '', '/v2/materials?internal=bogus&searched_within=last-week&min_searches=-3')
    const c = makeComponent()
    c.init()
    expect(c.internal).toBe('all')
    expect(c.searchedWithin).toBe('any')
    expect(c.minSearches).toBe(0)
  })
})

describe('materialsFilter sourcing signals — pushURL', () => {
  it('serializes only non-default operational state', () => {
    const c = makeComponent()
    c.init()
    c.hasStock = true
    c.internal = 'standard'
    c.searchedWithin = '7d'
    c.minSearches = 2
    c.pushURL()
    const params = new URLSearchParams(window.location.search)
    expect(params.get('has_stock')).toBe('true')
    expect(params.get('has_price')).toBeNull()    // default omitted
    expect(params.get('has_crosses')).toBeNull()  // default omitted
    expect(params.get('internal')).toBe('standard')
    expect(params.get('searched_within')).toBe('7d')
    expect(params.get('min_searches')).toBe('2')
  })

  it('writes a clean URL when everything is at its default', () => {
    const c = makeComponent()
    c.init()
    c.pushURL()
    const search = window.location.search
    for (const p of ['has_stock', 'has_price', 'has_crosses', 'internal', 'searched_within', 'min_searches']) {
      expect(search).not.toContain(p)
    }
  })
})

describe('materialsFilter sourcing signals — setters', () => {
  it('toggleSourcingFlag flips only the whitelisted flags', () => {
    const c = makeComponent()
    c.init()
    c.toggleSourcingFlag('hasStock')
    expect(c.hasStock).toBe(true)
    c.toggleSourcingFlag('hasStock')
    expect(c.hasStock).toBe(false)
    c.toggleSourcingFlag('commodity')  // not a sourcing flag — ignored
    expect(c.commodity).toBe('')
  })

  it('setSearchedWithin toggles off when re-clicking the active bucket', () => {
    const c = makeComponent()
    c.init()
    c.setSearchedWithin('30d')
    expect(c.searchedWithin).toBe('30d')
    c.setSearchedWithin('30d')
    expect(c.searchedWithin).toBe('any')
    c.setSearchedWithin('whenever')
    expect(c.searchedWithin).toBe('any')
  })

  it('setInternal accepts only the known modes', () => {
    const c = makeComponent()
    c.init()
    c.setInternal('internal')
    expect(c.internal).toBe('internal')
    c.setInternal('nope')
    expect(c.internal).toBe('all')
  })

  it('setMinSearches floors junk and negatives to 0', () => {
    const c = makeComponent()
    c.init()
    c.setMinSearches('7')
    expect(c.minSearches).toBe(7)
    c.setMinSearches('-2')
    expect(c.minSearches).toBe(0)
    c.setMinSearches('abc')
    expect(c.minSearches).toBe(0)
  })
})

describe('materialsFilter sourcing signals — counts and reset', () => {
  it('sourcingActiveCount feeds activeFilterCount', () => {
    const c = makeComponent()
    c.init()
    expect(c.sourcingActiveCount).toBe(0)
    const base = c.activeFilterCount
    c.hasStock = true
    c.hasCrosses = true
    c.internal = 'standard'
    c.searchedWithin = '90d'
    c.minSearches = 1
    expect(c.sourcingActiveCount).toBe(5)
    expect(c.activeFilterCount).toBe(base + 5)
  })

  it('clearAllFilters resets every sourcing signal', () => {
    const c = makeComponent()
    c.init()
    c.hasStock = true
    c.hasPrice = true
    c.hasCrosses = true
    c.internal = 'internal'
    c.searchedWithin = '7d'
    c.minSearches = 9
    c.clearAllFilters()
    expect(c.hasStock).toBe(false)
    expect(c.hasPrice).toBe(false)
    expect(c.hasCrosses).toBe(false)
    expect(c.internal).toBe('all')
    expect(c.searchedWithin).toBe('any')
    expect(c.minSearches).toBe(0)
  })
})
