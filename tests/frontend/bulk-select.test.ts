// tests/frontend/bulk-select.test.ts — shared bulkSelect Alpine component (htmx_app.js):
// the list bulk-select scope extracted from customers/_account_list.html and
// customers/contacts_list.html. selectedIds is an Object-as-Set keyed by row-id STRINGS;
// toggle add/remove, toggleAll select-all/clear, allSelected page-size comparison,
// idsStr comma-join (the bulk forms' hidden ids field), and clear (@clear-selection).
// Mirrors the mock harness in materials-filter-numeric-chips.test.ts.
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

beforeEach(async () => {
  registry = {}
  alpineMock.data = (n: string, f: any) => { registry[n] = f }
  vi.resetModules()
  await import('../../app/static/htmx_app.js')
})

describe('bulkSelect — toggle + count', () => {
  it('starts empty, toggles a row on and off (Object-as-Set, string keys)', () => {
    const c = registry['bulkSelect']([1, 2, 3])
    expect(c.count).toBe(0)
    c.toggle('2')
    expect(c.selectedIds['2']).toBe(true)
    expect(c.count).toBe(1)
    c.toggle('2')
    expect('2' in c.selectedIds).toBe(false)
    expect(c.count).toBe(0)
  })

  it('idsStr comma-joins the selected ids for the bulk forms', () => {
    const c = registry['bulkSelect']([1, 2, 3])
    c.toggle('1')
    c.toggle('3')
    expect(c.idsStr()).toBe('1,3')
  })
})

describe('bulkSelect — allSelected + toggleAll', () => {
  it('allSelected is true only when every page row is selected (never on an empty page)', () => {
    const empty = registry['bulkSelect']([])
    expect(empty.allSelected).toBe(false)
    const c = registry['bulkSelect']([1, 2])
    expect(c.allSelected).toBe(false)
    c.toggle('1')
    expect(c.allSelected).toBe(false)
    c.toggle('2')
    expect(c.allSelected).toBe(true)
  })

  it('toggleAll(true) selects the whole page; toggleAll(false) clears', () => {
    const c = registry['bulkSelect']([10, 20, 30])
    c.toggleAll(true)
    expect(c.count).toBe(3)
    expect(c.allSelected).toBe(true)
    expect(c.idsStr()).toBe('10,20,30')
    c.toggleAll(false)
    expect(c.count).toBe(0)
    expect(c.selectedIds).toEqual({})
  })

  it('clear() empties the selection (@clear-selection.window after a bulk apply)', () => {
    const c = registry['bulkSelect']([1, 2])
    c.toggleAll(true)
    c.clear()
    expect(c.selectedIds).toEqual({})
    expect(c.count).toBe(0)
  })

  it('accepts numeric ids from |tojson and treats them as the same string keys rows use', () => {
    const c = registry['bulkSelect']([7])
    c.toggle('7')  // rows call toggle('{{ c.id }}') — a string
    expect(c.allSelected).toBe(true)
    expect(c.idsStr()).toBe('7')
  })
})
