# Vite — Test Fixtures Reference (Vitest)

## Contents
- Fixture philosophy for this stack
- Alpine component fixtures
- URL/DOM fixtures
- Store fixtures
- Shared factory helpers
- DO/DON'T pairs

## Fixture Philosophy for This Stack

AvailAI's Vitest tests don't use a fixtures API. Instead, fixtures are **factory
functions** called in `beforeEach`. Each test gets a fresh instance with no shared state.

```ts
// Pattern: factory + beforeEach reset
function createFilter() { return { /* ... */ }; }

let filter: ReturnType<typeof createFilter>;
beforeEach(() => { filter = createFilter(); });
```

This is idiomatic for Alpine.js component testing — each test starts clean.

## Alpine Component Fixtures

Mirror the shape of `htmx_app.js` component data objects exactly:

```ts
// tests/frontend/sourcing-progress.test.ts
function createSourcingProgress(requirementId: number, totalSources: number) {
  return {
    completed: 0,
    requirementId,
    totalSources,

    handleSourceComplete(data: { source: string; count: number; status: string }) {
      this.completed++;
    },

    get progressPct() {
      return Math.round((this.completed / this.totalSources) * 100);
    },
  };
}

describe('sourcingProgress', () => {
  it('starts at 0%', () => {
    const prog = createSourcingProgress(1, 6);
    expect(prog.progressPct).toBe(0);
  });

  it('reaches 100% when all sources complete', () => {
    const prog = createSourcingProgress(1, 3);
    ['BrokerBin', 'Nexar', 'DigiKey'].forEach(source =>
      prog.handleSourceComplete({ source, count: 5, status: 'done' })
    );
    expect(prog.progressPct).toBe(100);
  });
});
```

## URL/DOM Fixtures

When a component reads `window.location`, set up a controlled URL before each test:

```ts
function setURL(path: string, search = '') {
  Object.defineProperty(window, 'location', {
    value: Object.assign(new URL(`http://localhost${path}`), { search }),
    writable: true,
    configurable: true,
  });
}

beforeEach(() => setURL('/v2/materials'));

it('parses commodity and sf_ sub-filters from URL', () => {
  setURL('/v2/materials', '?commodity=resistors&sf_package=DIP-8');
  filter.syncFromURL();
  expect(filter.commodity).toBe('resistors');
  expect(filter.subFilters['package']).toEqual(['DIP-8']);
});

it('defaults page to 0 when absent', () => {
  setURL('/v2/materials', '');
  filter.syncFromURL();
  expect(filter.page).toBe(0);
});
```

## Store Fixtures

Minimal Alpine store stubs for components that call `Alpine.store(...)`:

```ts
function createStoreFixture(overrides: Record<string, any> = {}) {
  const defaults = {
    toast: { message: '', type: 'info', show: false },
    sidebar: { open: true, collapsed: false },
    preferences: { resultsPerPage: 25, compactTables: false },
    shortlist: { items: [], count: 0 },
  };
  const stores = { ...defaults, ...overrides };
  return { store: (name: string) => stores[name] };
}

beforeEach(() => {
  global.Alpine = createStoreFixture() as any;
});

// Override specific stores for targeted tests:
it('reads custom resultsPerPage', () => {
  global.Alpine = createStoreFixture({
    preferences: { resultsPerPage: 50, compactTables: true },
  }) as any;
  expect(component.pageSize()).toBe(50);
});
```

## Shared Factory Helpers

Put reusable factories in `tests/frontend/helpers.ts` once used in 3+ test files:

```ts
// tests/frontend/helpers.ts
export function createShortlistStore() {
  return {
    items: [] as Array<{ vendor_name: string; mpn: string }>,
    toggle(item: { vendor_name: string; mpn: string }) {
      const key = `${item.vendor_name}:${item.mpn}`;
      const idx = this.items.findIndex(i => `${i.vendor_name}:${i.mpn}` === key);
      if (idx >= 0) this.items.splice(idx, 1);
      else this.items.push(item);
    },
    has(vendorName: string, mpn: string) {
      return this.items.some(i => i.vendor_name === vendorName && i.mpn === mpn);
    },
    clear() { this.items = []; },
    get count() { return this.items.length; },
  };
}

export function createToastStore() {
  return { message: '', type: 'info' as const, show: false };
}
```

## DO/DON'T Pairs

**DO** use factory functions (not class instances) — they match how Alpine components
are defined as `Alpine.data('name', () => ({ ... }))`.

**NEVER** share fixture instances across tests by declaring them at module scope.
Shared state causes order-dependent test failures that are extremely difficult to debug.

**DO** keep factories close to their tests. Only move to `helpers.ts` when the same
factory is used in 3+ test files — don't pre-optimize.

**NEVER** use `Alpine.$persist` in test fixtures — it reads from `localStorage`, which
is shared across jsdom tests in the same run. Strip persistence from factory objects
or mock `localStorage` explicitly.
