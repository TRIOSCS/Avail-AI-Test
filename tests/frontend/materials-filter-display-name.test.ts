import { describe, it, expect, beforeEach, vi } from 'vitest'

// Minimal registry to capture Alpine.data calls from htmx_app.js.
let registry: Record<string, any> = {}

// Mock htmx and all Alpine plugins so htmx_app.js can be imported cleanly in jsdom.
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

beforeEach(async () => {
  registry = {}
  alpineMock.data = (n: string, f: any) => { registry[n] = f }
  vi.resetModules()
  await import('../../app/static/htmx_app.js')
})

describe('materialsFilter.commodityDisplayName', () => {
  it('uses the injected display-names map, falling back to title-case', () => {
    const c = registry['materialsFilter']()
    c.$el = { dataset: { displayNames: JSON.stringify({ analog_ic: 'Analog ICs' }) } }
    c.init()

    c.commodity = 'analog_ic'
    expect(c.commodityDisplayName).toBe('Analog ICs')   // mapped

    c.commodity = 'voltage_regulators'
    expect(c.commodityDisplayName).toBe('Voltage Regulators')  // title-case fallback
  })
})
