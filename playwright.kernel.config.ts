// playwright.kernel.config.ts — kernel-walk E2E against the DEPLOYED parallel instance.
//
// Runs ONLY e2e/kernel-walk.spec.ts against the live simplification instance
// (default https://app.availai.net:8443, override via KERNEL_BASE_URL). Unlike
// playwright.config.ts this config starts NO webServer — the target is the deployed
// app, not a self-hosted branch server. Serial, single worker: the walk is one
// stateful journey (spec §3).
//
// Called by: scripts/simp-nightly.sh ("E2E (kernel walk vs deployed)" stage),
//            npx playwright test --config=playwright.kernel.config.ts
// Depends on: e2e/kernel-walk.spec.ts, .env (DEFAULT_USER_* read at runtime)

import { defineConfig } from '@playwright/test';

const BASE = process.env.KERNEL_BASE_URL || 'https://app.availai.net:8443';

export default defineConfig({
  testDir: './e2e',
  testMatch: /kernel-walk\.spec\.ts$/,
  timeout: 120_000,
  retries: 0,
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: BASE,
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  // Deliberately no webServer: this drives the deployed parallel instance.
});
