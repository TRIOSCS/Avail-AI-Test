/**
 * kernel-walk.spec.ts — the spec §3 Launch-Kernel walk against the DEPLOYED
 * parallel instance (https://app.availai.net:8443), keys-off.
 *
 * One serial browser journey through the sell/buy walk and the resell walk,
 * creating entities prefixed "E2E-KERNEL" so they are recognizable in the DB
 * copy. Every step that genuinely cannot run keys-off (Graph email sends, the
 * AI paste-parse, the outreach log which requires a fresh M365 token) is an
 * explicit no-op test.step with the reason — no silent gaps.
 *
 * Login: POSTs the password form with DEFAULT_USER_EMAIL / DEFAULT_USER_PASSWORD
 * read from the worktree .env at runtime (never hardcoded here).
 *
 * Called by: npx playwright test --config=playwright.kernel.config.ts
 *            (scripts/simp-nightly.sh "E2E (kernel walk vs deployed)" stage)
 * Depends on: deployed parallel instance (disposable prod-DB copy), .env
 */

import { test, expect, type Page, type BrowserContext } from '@playwright/test';
import * as fs from 'node:fs';
import * as path from 'node:path';

// ── .env credentials (dotenv-style read, no secrets in the spec) ──────
function dotenvValue(name: string): string {
  if (process.env[name]) return process.env[name] as string;
  const envPath = path.join(__dirname, '..', '.env');
  const text = fs.readFileSync(envPath, 'utf8');
  for (const line of text.split('\n')) {
    const trimmed = line.trim();
    if (trimmed.startsWith(`${name}=`)) return trimmed.slice(name.length + 1).trim();
  }
  throw new Error(`${name} not found in ${envPath} or process.env`);
}

// ── Run-scoped names (unique per run so reruns never collide) ─────────
const RUN = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14); // yyyymmddHHMMSS
const COMPANY = `E2E-KERNEL Customer ${RUN}`;
const REQ_NAME = `E2E-KERNEL Req ${RUN}`;
const MPN = `E2E-KERNEL-MPN-${RUN}`;
const COMMON_MPN = 'LM317T'; // a real part so local sightings may attach keys-off
// The digits must DOMINATE the name: vendor resolution fuzzy-merges names scoring
// >=88 into the existing card (intended canonicalization), and a shared
// "E2E-KERNEL Broker " prefix with only the run stamp differing scores above that —
// every walk after the first on the same DB would silently reuse night one's card.
const BROKER = `E2E-KERNEL-BKR-${RUN}-${RUN}`;
const SO_NUMBER = `E2E-KERNEL-SO-${RUN}`;
const PO_NUMBER = `E2E-KERNEL-PO-${RUN}`;
const RESELL_TITLE = `E2E-KERNEL Excess ${RUN}`;
const RESELL_MPN = `E2E-KERNEL-XS-${RUN}`;

// ── Journey state shared across the serial tests ──────────────────────
let context: BrowserContext;
let page: Page;
let reqId = '';
let quoteId = '';
let planId = '';
let planApproved = false;
let poVerifyRightOn = false;
let poVerified = false;
let resellListId = '';

test.describe.configure({ mode: 'serial' });

async function csrfHeader(): Promise<Record<string, string>> {
  const cookies = await context.cookies();
  const token = cookies.find((c) => c.name === 'csrftoken')?.value ?? '';
  return { 'x-csrftoken': token, 'HX-Request': 'true' };
}

/** Let the current HTMX swap settle (debounce + swap + Alpine init). */
async function settle(ms = 500) {
  await page.waitForTimeout(ms);
}

/**
 * Click a button that loads a form into the global modal (#modal-content) and
 * wait for `readySelector` inside it. Freshly swapped-in buttons can be clicked
 * before their HTMX wiring settles — retry the click once before failing.
 */
async function openModal(button: ReturnType<Page['locator']>, readySelector: string) {
  await expect(button).toBeVisible({ timeout: 20_000 });
  await settle(300);
  await button.click();
  const ready = page.locator(`#modal-content ${readySelector}`);
  try {
    await ready.waitFor({ state: 'visible', timeout: 6_000 });
  } catch {
    await button.click(); // one retry — the first click raced the HTMX init
    await ready.waitFor({ state: 'visible', timeout: 15_000 });
  }
}

/**
 * Open an Approvals-workspace tab, filter the list by the run's SO number
 * (keyup-triggered search — type key-by-key, then wait out the debounce), and
 * return the first row matching `rowSelector` inside #aw-list. The caller
 * asserts/clicks — some rows are legitimately absent for this viewer.
 */
async function openApprovalRow(tab: string, rowSelector: string) {
  await page.goto(`/v2/approvals?tab=${tab}`);
  const search = page.locator("#aw-filters input[name='q']");
  await expect(search).toBeVisible({ timeout: 20_000 });
  await search.pressSequentially(SO_NUMBER, { delay: 20 });
  await settle(900);
  return page.locator(`#aw-list ${rowSelector}`).first();
}

test.beforeAll(async ({ browser, baseURL }) => {
  context = await browser.newContext({ baseURL });
  const res = await context.request.post('/auth/login', {
    form: { email: dotenvValue('DEFAULT_USER_EMAIL'), password: dotenvValue('DEFAULT_USER_PASSWORD') },
  });
  expect(res.status(), 'password login against the deployed instance').toBe(200);
  const body = await res.json();
  expect(body.ok).toBe(true);
  page = await context.newPage();
  // hx-confirm uses window.confirm — accept every dialog or the walk stalls.
  page.on('dialog', (d) => d.accept().catch(() => {}));
});

test.afterAll(async () => {
  await context?.close();
});

// ═════════════════════════════════ SELL / BUY WALK (spec §3) ═════════

test('app shell loads for the authenticated admin', async () => {
  // The Deals list view (the workspace default is the parts triage board).
  const resp = await page.goto('/v2/requisitions?view=list');
  expect(resp?.status()).toBe(200);
  await expect(page.getByRole('button', { name: 'New Requisition' })).toBeVisible({ timeout: 20_000 });
});

test('admin approver rights checked in the app (read-only)', async () => {
  // "Approver toggles permitting" (§2): READ the seeded admin's rights from the
  // Settings → Users panel — never grant/escalate them from a test. Later legs
  // skip honestly when a toggle is off.
  await page.goto('/v2/settings?tab=users');
  const row = page.locator('#users-content tr', { hasText: dotenvValue('DEFAULT_USER_EMAIL') }).first();
  await expect(row).toBeVisible({ timeout: 20_000 });
  const poToggle = row.locator('form[hx-post$="/purchase-order-approver"] input[type="checkbox"]');
  await expect(poToggle).toBeVisible({ timeout: 10_000 });
  poVerifyRightOn = await poToggle.isChecked();
});

test('CRM: create the E2E-KERNEL customer (company + default site)', async () => {
  await page.goto('/v2/customers');
  await openModal(page.getByRole('button', { name: '+ New account' }), 'input[name="name"]');
  const modal = page.locator('#modal-content');
  await modal.locator('input[name="name"]').fill(COMPANY);
  await modal.getByRole('button', { name: 'Create account' }).click();
  // Success loads the new account's detail into the CDM right panel.
  await expect(page.locator('#cdm-detail')).toContainText(COMPANY, { timeout: 20_000 });
});

test('requisition created with manual lines (no AI parse)', async () => {
  await test.step('paste-parse — SKIPPED keys-off: "Parse with AI" needs the AI key; manual rows used instead (§3.1)', async () => {});
  await page.goto('/v2/requisitions?view=list');
  await openModal(
    page.getByRole('button', { name: 'New Requisition' }),
    'input[placeholder="e.g. Acme Q2 BOM, Harris RFQ #4521"]',
  );
  const modal = page.locator('#modal-content');
  await modal.getByPlaceholder('e.g. Acme Q2 BOM, Harris RFQ #4521').fill(REQ_NAME);

  // Customer picker: typeahead → pick the company created above.
  const search = modal.getByPlaceholder('Search customers...');
  await search.click();
  await search.pressSequentially(COMPANY, { delay: 20 });
  const result = page.locator('#customer-typeahead-results button', { hasText: COMPANY }).first();
  await expect(result).toBeVisible({ timeout: 15_000 });
  await result.click();
  await settle(300);

  // Row 1: the kernel MPN. The modal opens with one blank row.
  await modal.getByLabel('MPN *').first().fill(MPN);
  await modal.getByLabel('Mfr *').first().fill('E2E-KERNEL Mfg');
  await modal.getByLabel('Qty').first().fill('100');

  // Row 2: a real part so sightings/vendors can attach keys-off.
  await modal.getByRole('button', { name: '+ Add row' }).click();
  await modal.getByLabel('MPN *').nth(1).fill(COMMON_MPN);
  await modal.getByLabel('Mfr *').nth(1).fill('Texas Instruments');
  await modal.getByLabel('Qty').nth(1).fill('25');

  await modal.locator('button[type="submit"]', { hasText: 'Create Requisition' }).click();
  // import-save closes the modal, toasts, and refreshes the list.
  await expect(page.locator('#main-content')).toContainText(REQ_NAME, { timeout: 20_000 });

  // Open the deal and capture its id from the Send-RFQ button. (W3: the button
  // now opens the ONE surviving composer — the sightings vendor-modal — via an
  // open-modal dispatch instead of an hx-get to the deleted rfq-compose route,
  // so the id comes from its data-req-id attribute.)
  await page.locator('#main-content a', { hasText: REQ_NAME }).first().click();
  const rfqBtn = page.getByRole('button', { name: 'Send RFQ' });
  await expect(rfqBtn).toBeVisible({ timeout: 20_000 });
  reqId = (await rfqBtn.getAttribute('data-req-id')) ?? '';
  expect(reqId, 'requisition id parsed from the detail header').not.toBe('');
});

test('sourcing board: parts board works keys-off', async () => {
  // Parts tab (default) shows both requirement rows.
  await expect(page.locator('#tab-content')).toContainText(MPN);
  await expect(page.locator('#tab-content')).toContainText(COMMON_MPN);

  // "Search Sources" fans out keys-off (external connectors keyless → local data only).
  const respPromise = page.waitForResponse(
    (r) => r.url().includes(`/requisitions/${reqId}/search-all`),
    { timeout: 90_000 },
  );
  await page.getByRole('button', { name: 'Search Sources' }).click();
  const resp = await respPromise;
  expect(resp.status(), 'search-all must not 5xx keys-off').toBeLessThan(500);
  await settle(800);

  // The split-panel Sourcing-Leads workspace (/v2/sourcing/{id}/workspace) was
  // deleted in the Wave 2 sweep (spec §8) — the deal's parts board above IS the
  // kernel sourcing surface.

  // Back to the deal for the rest of the walk.
  await page.goto(`/v2/requisitions/${reqId}`);
  await expect(page.getByRole('button', { name: 'Send RFQ' })).toBeVisible({ timeout: 20_000 });
});

test('RFQ composer opens (send is not exercised)', async () => {
  // W3 §5.1: the deal's Send-RFQ button now opens the ONE composer — the
  // sightings vendor-modal (coverage-ranked vendors, [ref:id] tracking) — in the
  // global modal, with every line of the deal in the basket. The old
  // requisition-scoped rfq-compose tab surface was deleted.
  const rfqBtn = page.getByRole('button', { name: 'Send RFQ' });
  await settle(300);
  await rfqBtn.click();
  const modal = page.locator('#modal-content');
  try {
    await modal.getByText('Send Inquiry').waitFor({ state: 'visible', timeout: 6_000 });
  } catch {
    await rfqBtn.click(); // one retry — the first click raced the Alpine init
  }
  await expect(modal.getByText('Send Inquiry')).toBeVisible({ timeout: 15_000 });
  // Compose surface: the vendor-selection block renders — "(N suggested)" may be
  // 0 keys-off (no sightings carried vendor contacts), which is the honest state.
  await expect(modal.getByText('Select Vendors', { exact: false })).toBeVisible({ timeout: 10_000 });
  await page.keyboard.press('Escape'); // close the modal so the walk continues on the deal page
  await test.step('RFQ send — SKIPPED keys-off: Graph creds are blanked on this instance; compose surface asserted, nothing sent', async () => {});
});

test('manual Add-offer door creates an offer', async () => {
  await page.goto(`/v2/requisitions/${reqId}`);
  // W4.2 lens toggle: Offers lives in the Sourcing lens (default is Sales).
  await page.getByRole('group', { name: 'Deal lens' }).getByRole('button', { name: 'Sourcing' }).click();
  await page.getByRole('button', { name: 'Offers', exact: true }).click();
  const tab = page.locator('#tab-content');
  await expect(tab.getByRole('button', { name: 'Add Offer' })).toBeVisible({ timeout: 20_000 });
  await tab.getByRole('button', { name: 'Add Offer' }).click();

  // W3.6: the ONE Add-offer form loads inline into #parse-area — manual fields
  // plus an optional paste box (its own form; an honest AI-off note keys-off).
  // Filter to the form that carries the manual fields.
  const form = page
    .locator('#parse-area form')
    .filter({ has: page.locator('input[name="vendor_name"]') });
  await expect(form.locator('input[name="vendor_name"]')).toBeVisible({ timeout: 15_000 });
  await form.locator('input[name="vendor_name"]').fill(BROKER);
  await form.locator('input[name="mpn"]').fill(MPN);
  await form.locator('input[name="qty_available"]').fill('500');
  await form.locator('input[name="unit_price"]').fill('1.0000');
  // Link the offer to the kernel requirement so Build Quote sees its cost.
  await form.locator('select[name="requirement_id"]').selectOption({ label: `${MPN} (qty 100)` });
  await form.getByRole('button', { name: 'Save Offer' }).click();

  await expect(tab).toContainText(BROKER, { timeout: 20_000 });
});

test('Build Quote assembles — margin guardrail visible, PDF downloads', async () => {
  // W4.2 lens toggle: Build Quote lives in the Sales lens (we're on Sourcing after Add Offer).
  await page.getByRole('group', { name: 'Deal lens' }).getByRole('button', { name: 'Sales' }).click();
  await page.getByRole('button', { name: 'Build Quote' }).click();
  const tab = page.locator('#tab-content');
  await expect(tab.getByRole('button', { name: /Assemble (quote|revision)/ })).toBeVisible({ timeout: 20_000 });

  const include = tab.getByLabel(`Include ${MPN} in the quote`);
  await include.check();
  const price = tab.getByLabel(`Sell price for ${MPN}`);

  // Guardrail: a sell below the $1.00 cost must flag "below cost".
  await price.fill('0.4000');
  await expect(tab.getByText('below cost').first()).toBeVisible({ timeout: 10_000 });
  // Healthy price clears the guardrail.
  await price.fill('2.5000');
  await expect(tab.getByText('below cost')).toHaveCount(0);

  await tab.getByRole('button', { name: /Assemble (quote|revision)/ }).click();
  const pdfLink = page.locator('#tab-content a[href*="/export/pdf"]');
  await expect(pdfLink).toBeVisible({ timeout: 30_000 });

  const href = (await pdfLink.getAttribute('href')) ?? '';
  quoteId = href.match(/quote_id=(\d+)/)?.[1] ?? '';
  expect(quoteId, 'quote id parsed from the Download-PDF link').not.toBe('');

  // PDF generation works without email.
  const pdf = await page.request.get(href);
  expect(pdf.status(), 'quote PDF').toBe(200);
  expect(pdf.headers()['content-type'] ?? '').toContain('pdf');
});

test('quote marked Won (customer send skipped keys-off)', async () => {
  await test.step('quote email send — SKIPPED keys-off: Send requires a fresh M365 Graph token; Won is recorded via the same /result endpoint the UI button posts (draft→won is a valid transition)', async () => {});
  const res = await page.request.post(`/v2/partials/quotes/${quoteId}/result`, {
    headers: await csrfHeader(),
    form: { result: 'won' },
  });
  expect(res.status(), 'quote result → won').toBe(200);

  // UI truth: the quote detail now shows Won + the Build Buy Plan action.
  await page.goto(`/v2/requisitions/${reqId}`);
  await page.getByRole('button', { name: 'Quotes', exact: true }).click();
  const row = page.locator(`#tab-content tr[hx-get="/v2/partials/quotes/${quoteId}"]`);
  await expect(row).toBeVisible({ timeout: 20_000 });
  await row.click();
  await expect(page.locator('#main-content').getByRole('button', { name: 'Build Buy Plan' })).toBeVisible({
    timeout: 20_000,
  });
});

test('Build Buy Plan from the won quote', async () => {
  await page.locator('#main-content').getByRole('button', { name: 'Build Buy Plan' }).click();
  const main = page.locator('#main-content');
  await expect(main).toContainText('Line Items', { timeout: 30_000 });

  const qpBtn = main.locator('button[hx-get^="/v2/qp/for-buy-plan/"]').first();
  await expect(qpBtn).toBeVisible({ timeout: 10_000 });
  const hx = (await qpBtn.getAttribute('hx-get')) ?? '';
  planId = hx.match(/for-buy-plan\/(\d+)/)?.[1] ?? '';
  expect(planId, 'buy plan id parsed from the plan detail').not.toBe('');
});

test('edit a plan line (whole-table edit mode) before submitting', async () => {
  // The draft plan's working editor: workspace row → pane → "Open full plan →"
  // (the Build-Buy-Plan response is a slim summary without the edit affordances).
  await page.goto('/v2/approvals?tab=deals');
  await expect(page.locator("#aw-filters input[name='q']")).toBeVisible({ timeout: 20_000 });
  const row = page.locator(`#aw-list [data-row-key="plan-${planId}"]`);
  await expect(row).toBeVisible({ timeout: 20_000 });
  await row.click();
  const pane = page.locator('#aw-pane #aw-pane-body');
  await expect(pane).toContainText('Draft — not yet submitted', { timeout: 15_000 });
  await pane.getByText('Open full plan').click();

  const main = page.locator('#main-content');
  await expect(main).toContainText('Line Items', { timeout: 20_000 });
  await main.getByRole('button', { name: 'Edit plan' }).click();
  const qty = main.getByLabel('Quantity').first();
  await expect(qty).toBeVisible({ timeout: 10_000 });
  await qty.fill('90');
  await main.getByRole('button', { name: 'Save all' }).click();
  await expect(main).toContainText('90', { timeout: 20_000 });
  await expect(main.getByRole('button', { name: 'Edit plan' })).toBeVisible({ timeout: 10_000 });
});

test('submit the plan for approval with the SO number', async () => {
  const main = page.locator('#main-content');
  await main.getByRole('button', { name: 'Submit', exact: true }).first().click();
  const soInput = page.getByPlaceholder('Acctivate Sales Order #');
  await expect(soInput).toBeVisible({ timeout: 10_000 });
  await soInput.fill(SO_NUMBER);
  await page.getByRole('button', { name: 'Submit', exact: true }).last().click();
  await expect(main).toContainText(/pending/i, { timeout: 20_000 });
});

test('QP sales section is editable in the workspace pane (pending plan)', async () => {
  // QP-sales locks once the plan is ACTIVE (spec §7 matrix) — edit while PENDING.
  const row = await openApprovalRow('deals', `[data-row-key="plan-${planId}"]`);
  await expect(row).toBeVisible({ timeout: 20_000 });
  await row.click();

  const pane = page.locator('#aw-pane #aw-pane-body');
  await expect(pane).toContainText('Quality — sales section', { timeout: 15_000 });
  // Same race class openModal() guards: an Edit click that lands before Alpine
  // mounts the swapped pane is a dead @click (qpEdit never flips). Retry once.
  const editBtn = pane.getByRole('button', { name: 'Edit', exact: true }).first();
  await settle(300);
  await editBtn.click();
  const cond = pane.locator("input[name='qp_sales_condition']").first();
  try {
    await cond.waitFor({ state: 'visible', timeout: 6_000 });
  } catch {
    await editBtn.click();
  }
  await expect(cond).toBeVisible({ timeout: 10_000 });
  await cond.fill('E2E-KERNEL NEW');
  await pane.locator("form[hx-post*='qp-sales']").getByRole('button', { name: 'Save' }).click();
  await expect(page.locator('#aw-pane')).toContainText('E2E-KERNEL NEW', { timeout: 20_000 });
});

test('manager approves the deal in the Approvals workspace', async () => {
  const pane = page.locator('#aw-pane #aw-pane-body');
  await expect(pane).toBeVisible({ timeout: 15_000 });
  await settle(300);

  if ((await pane.getByText('Awaiting your approval').count()) === 0) {
    test.skip(true, 'seeded admin cannot decide this plan (approver toggle/limit) — approval rights checked in the app');
  }
  await pane.getByRole('button', { name: 'Approve & notify' }).click();
  await expect(page.locator('#aw-pane')).toContainText('Approved by', { timeout: 20_000 });
  planApproved = true;
});

test('buy instruction → enter PO number (+ QP purchasing) → per-line verify', async () => {
  test.skip(!planApproved, 'plan was not approved (previous step skipped) — no active line to confirm');

  // The approved plan's pane shows the PO kanban; tap the line's card.
  const card = page.locator('#aw-pane [data-lane] [role="link"]').first();
  await expect(card).toBeVisible({ timeout: 15_000 });
  await card.click();
  const pane = page.locator('#aw-pane #aw-pane-body');
  await expect(pane).toContainText('Confirm the PO you cut in Acctivate', { timeout: 20_000 });

  // QP purchasing section is editable inline on the confirm form.
  await expect(pane).toContainText('Quality — purchasing section');
  await pane.locator("input[name='qp_purchasing_condition']").fill('E2E-KERNEL NEW');

  await pane.locator("input[name='po_number']").fill(PO_NUMBER);
  await pane.locator("select[name='payment_method']").selectOption('wire');
  await pane.getByRole('button', { name: 'Confirm PO' }).click();
  await expect(pane.getByText('Pending approval').first()).toBeVisible({ timeout: 20_000 });

  // Per-line verify (manager, dollar-limit permitting).
  const approve = pane.locator("form[hx-post*='verify-po']").getByRole('button', { name: 'Approve' });
  if ((await approve.count()) === 0) {
    test.skip(
      true,
      poVerifyRightOn
        ? 'seeded admin cannot verify this PO line (dollar limit) — confirm path exercised'
        : 'seeded admin has can_approve_purchase_orders OFF (checked read-only in Settings → Users) — buy instruction + PO number exercised; per-line verify needs the toggle granted',
    );
  }
  await approve.first().click();
  await expect(page.locator('#aw-pane')).toContainText(/Approved|Verified/i, { timeout: 20_000 });
  poVerified = true;
});

test('prepayment request + OK-to-pay (pay-link confirm stays keys-off)', async () => {
  test.skip(!poVerified, 'no verified PO line (previous step skipped) — nothing to prepay');

  const requestBtn = page.locator('#aw-pane').getByRole('button', { name: 'Request prepayment' });
  if ((await requestBtn.count()) === 0) {
    test.skip(true, 'Request-prepayment not offered to this viewer on the verified line — §3 marks prepayment "when needed"');
  }
  await requestBtn.first().click();
  const modal = page.locator('#modal-content');
  const total = modal.locator("input[name='total_incl_fees']");
  await expect(total).toBeVisible({ timeout: 15_000 });
  await total.fill('180.00');
  await modal.locator('button[type="submit"]').first().click();
  await settle(800);

  // Decide it in the Prepayments tab.
  const row = await openApprovalRow('prepayments', '[data-row-key^="prepay-"]');
  if ((await row.count()) === 0) {
    test.skip(true, 'prepayment row not surfaced for this viewer — request path exercised');
  }
  await row.click();
  const okBtn = page.locator('#aw-pane').getByRole('button', { name: /OK to pay —/ });
  if ((await okBtn.count()) === 0) {
    test.skip(true, 'viewer cannot decide this prepayment (toggle/limit) — request path exercised');
  }
  await okBtn.first().click();
  await expect(page.locator('#aw-pane')).toContainText('Accounting confirms via the pay link', { timeout: 20_000 });
  await test.step('accounting pay-link confirm — SKIPPED keys-off: the tokenized link is delivered by email to accounting; this run has no mailbox', async () => {});
});

// ═════════════════════════════════ RESELL WALK (spec §3) ═════════════

test('resell intake: list created with one line', async () => {
  await page.goto('/v2/resell');
  await openModal(page.getByRole('button', { name: 'New List' }), 'input[name="title"]');
  const modal = page.locator('#modal-content');
  await modal.locator('input[name="title"]').fill(RESELL_TITLE);
  await modal.locator('select[name="company_id"]').selectOption({ label: COMPANY });
  await modal.getByRole('button', { name: 'Create list' }).click();
  await expect(page.locator('#resell-list-body')).toContainText(RESELL_TITLE, { timeout: 20_000 });

  // Open the new list's detail.
  await page.locator('#resell-list-rows a', { hasText: RESELL_TITLE }).first().click();
  const detail = page.locator('#split-right-resell');
  await expect(detail).toContainText(RESELL_TITLE, { timeout: 20_000 });
  const idAttr = await detail.locator('button[hx-get*="/add-line-form"]').first().getAttribute('hx-get');
  resellListId = idAttr?.match(/resell\/(\d+)\/add-line-form/)?.[1] ?? '';
  expect(resellListId, 'excess list id parsed from the detail').not.toBe('');

  // Add one line.
  await openModal(detail.locator('button[hx-get*="/add-line-form"]').first(), 'input[name="part_number"]');
  const lineModal = page.locator('#modal-content');
  await lineModal.locator('input[name="part_number"]').fill(RESELL_MPN);
  await lineModal.locator('input[name="quantity"]').fill('50');
  await lineModal.locator('input[name="asking_price"]').fill('2.00');
  await lineModal.getByRole('button', { name: 'Add line' }).click();
  await expect(page.locator('#split-right-resell')).toContainText(RESELL_MPN, { timeout: 20_000 });
});

test('resell: post the list (lines lock, brokers see it)', async () => {
  const detail = page.locator('#split-right-resell');
  await detail.getByRole('button', { name: 'Post', exact: true }).click(); // hx-confirm auto-accepted
  // Posted state: the owner actions flip to Close / Close (no bid).
  await expect(detail.getByRole('button', { name: 'Close', exact: true })).toBeVisible({ timeout: 20_000 });
  await test.step('broker outreach log — SKIPPED keys-off: POST /api/resell/{id}/outreach requires a fresh M365 token even for a manual-channel log; bids enter via the upload door below', async () => {});
});

test('resell: log one broker bid via the compiled-sheet upload door', async () => {
  const detail = page.locator('#split-right-resell');
  await detail.getByRole('tab', { name: 'Offers', exact: true }).click();
  // The file input itself is class="hidden" — wait on its visible form instead.
  await openModal(detail.getByRole('button', { name: 'Upload Bids' }), 'form[hx-post*="bids/upload-preview"]');
  const modal = page.locator('#modal-content');
  const fileInput = modal.locator('input[type="file"][name="file"]');
  await expect(fileInput).toBeAttached({ timeout: 15_000 });
  // Freshly swapped-in forms can receive input before their HTMX wiring settles —
  // the @change → requestSubmit() then falls through to a NATIVE GET navigation
  // (third instance of this race class; see openModal and the W2.A Edit hardening).
  // Wait for htmx's internal binding on the form before firing the change event.
  await page.waitForFunction(
    () =>
      !!(
        document.querySelector('#modal-content form[hx-post*="bids/upload-preview"]') as
          | (Element & Record<string, unknown>)
          | null
      )?.['htmx-internal-data'],
    { timeout: 15_000 },
  );
  const csv = `bidder,mpn,qty,price\n${BROKER},${RESELL_MPN},50,1.25\n`;
  await fileInput.setInputFiles({ name: 'e2e-kernel-bids.csv', mimeType: 'text/csv', buffer: Buffer.from(csv) });

  // Preview grid renders in-place; confirm the single accepted row.
  const confirm = modal.getByRole('button', { name: /Confirm upload \(1\)/ });
  await expect(confirm).toBeVisible({ timeout: 20_000 });
  await confirm.click();
  await expect(page.locator('#split-right-resell')).toContainText(BROKER, { timeout: 20_000 });
});

test('resell: award the bid, assemble the bid-back, record the outcome path', async () => {
  const detail = page.locator('#split-right-resell');
  const award = detail.getByRole('button', { name: 'Award', exact: true });
  await expect(award.first()).toBeVisible({ timeout: 20_000 });
  await award.first().click(); // hx-confirm auto-accepted
  await expect(detail).toContainText('Awarded ✓', { timeout: 20_000 });

  // Bid-back to the customer: Build Bid tab → select the line → assemble → PDF.
  await detail.getByRole('tab', { name: 'Build Bid', exact: true }).click();
  const include = detail.getByLabel(`Include ${RESELL_MPN} in the bid`);
  await expect(include).toBeVisible({ timeout: 20_000 });
  await include.check();
  await detail.getByRole('button', { name: 'Assemble bid' }).click();
  const pdfLink = detail.locator('a[href*="/bid/"][href$="/pdf"]');
  await expect(pdfLink).toBeVisible({ timeout: 30_000 });
  const pdf = await page.request.get((await pdfLink.getAttribute('href')) ?? '');
  expect(pdf.status(), 'bid-back PDF').toBe(200);
  expect(pdf.headers()['content-type'] ?? '').toContain('pdf');

  await test.step('bid send + Mark accepted — SKIPPED keys-off: Send emails the PDF via Graph; accept/reject only unlock on a SENT bid', async () => {});

  // The ladder's last step (award → outcome, spec §5.3): close the awarded list
  // with a recorded outcome. The award action swaps only the offers tab body, so
  // the header (where the close-with-outcome form renders on awarded state) is
  // re-rendered by reloading the list detail — the same thing a returning owner's
  // navigation does. Keys-off legal — no send involved.
  await page.goto(`/v2/resell/${resellListId}`);
  await settle(800);
  // Direct-URL render does not nest the detail under #split-right-resell — scope
  // the outcome controls to the page.
  const outcomeSelect = page.locator('[data-testid="close-outcome"]');
  await expect(outcomeSelect).toBeVisible({ timeout: 20_000 });
  await outcomeSelect.selectOption('sold');
  await page.getByRole('button', { name: 'Close with outcome' }).click(); // hx-confirm auto-accepted
  await expect(page.locator('[data-testid="list-outcome"]')).toContainText('sold', { timeout: 20_000 });
});
