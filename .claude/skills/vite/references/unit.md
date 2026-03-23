# Vite — Unit Test Reference (Vitest)

## Contents
- Running tests
- Component factory pattern
- Store unit tests
- URL/navigation logic
- DO/DON'T pairs
- Common errors

## Running Tests

```bash
npm run test:vitest          # Single run
npm run test:vitest:watch    # Watch mode (development)
```

Tests match `tests/frontend/**/*.test.ts`. Config: `vitest.config.ts`.

## Component Factory Pattern

Alpine.js components cannot be imported and instantiated directly — they depend on the
Alpine runtime. The correct approach is to extract the **data object factory** and test
it in isolation.

```ts
// GOOD — extract the factory, test pure logic
function createMaterialsFilter() {
  return {
    commodity: '',
    subFilters: {} as Record<string, any>,
    q: '',
    page: 0,

    get activeFilterCount() {
      let count = 0;
      for (const [, val] of Object.entries(this.subFilters)) {
        if (Array.isArray(val)) count += (val as any[]).length;
        else if (val !== '' && val !== null) count += 1;
      }
      return count;
    },

    toggleFilter(specKey: string, value: string) {
      if (!this.subFilters[specKey]) {
        this.subFilters[specKey] = [value];
      } else {
        const idx = this.subFilters[specKey].indexOf(value);
        if (idx >= 0) {
          this.subFilters[specKey].splice(idx, 1);
          if (this.subFilters[specKey].length === 0) delete this.subFilters[specKey];
        } else {
          this.subFilters[specKey].push(value);
        }
      }
    },
  };
}

describe('materialsFilter', () => {
  let filter: ReturnType<typeof createMaterialsFilter>;
  beforeEach(() => { filter = createMaterialsFilter(); });

  it('toggleFilter adds a new value', () => {
    filter.toggleFilter('package', 'DIP-8');
    expect(filter.subFilters['package']).toEqual(['DIP-8']);
  });

  it('activeFilterCount counts array items', () => {
    filter.subFilters = { package: ['DIP-8', 'SOP-8'], voltage_min: 3.3 };
    expect(filter.activeFilterCount).toBe(3);
  });
});
```

## Store Unit Tests

```ts
// Test Alpine store shape — no Alpine runtime needed
describe('shortlist store', () => {
  function createShortlist() {
    return {
      items: [] as any[],
      toggle(item: { vendor_name: string; mpn: string }) {
        const key = `${item.vendor_name}:${item.mpn}`;
        const idx = this.items.findIndex(i => `${i.vendor_name}:${i.mpn}` === key);
        if (idx >= 0) this.items.splice(idx, 1);
        else this.items.push(item);
      },
      get count() { return this.items.length; },
    };
  }

  it('deduplicates by vendor+mpn key', () => {
    const store = createShortlist();
    store.toggle({ vendor_name: 'Arrow', mpn: 'LM317T' });
    store.toggle({ vendor_name: 'Arrow', mpn: 'LM317T' });
    expect(store.count).toBe(0);
  });

  it('distinguishes same MPN from different vendors', () => {
    const store = createShortlist();
    store.toggle({ vendor_name: 'Arrow', mpn: 'LM317T' });
    store.toggle({ vendor_name: 'Mouser', mpn: 'LM317T' });
    expect(store.count).toBe(2);
  });
});
```

## URL/Navigation Logic

Test path-routing helpers by extracting them as standalone functions:

```ts
function _viewFromPath(path: string): string {
  if (/\/buy-plans(\/|$)/.test(path)) return 'buy-plans';
  if (/\/requisitions(\/|$)/.test(path)) return 'requisitions';
  return 'requisitions';
}

describe('_viewFromPath', () => {
  it.each([
    ['/v2/buy-plans/42', 'buy-plans'],
    ['/v2/requisitions/123', 'requisitions'],
    ['/', 'requisitions'],
  ])('maps %s → %s', (path, expected) => {
    expect(_viewFromPath(path)).toBe(expected);
  });
});
```

## DO/DON'T Pairs

**DO** reset `window.location` in `beforeEach` when testing URL parsing:
```ts
beforeEach(() => {
  Object.defineProperty(window, 'location', {
    value: new URL('http://localhost/v2/materials'),
    writable: true,
    configurable: true,
  });
});
```

**NEVER** import `htmx_app.js` directly into Vitest — it calls `Alpine.start()` on
import, which requires a full browser DOM and will throw in jsdom.

**DO** use `it.each` for path-routing and URL-mapping tests — they have many cases and
`it.each` makes failures obvious without repetitive boilerplate.

**NEVER** test Alpine reactivity (computed property updates triggered by watchers) in
Vitest. Use Playwright E2E tests for that. Vitest tests pure data logic only.

## Common Errors

**`Alpine is not defined`** — You're importing from `htmx_app.js`. Extract the factory
function instead and test it standalone.

**`window.location is not configurable`** — Use `Object.defineProperty` with both
`writable: true` and `configurable: true` in `beforeEach`, not direct assignment.

**Test file not found** — Files must match `tests/frontend/**/*.test.ts`. The glob is
defined in `vitest.config.ts`, not `vite.config.js`.
