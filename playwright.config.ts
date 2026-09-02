// Playwright configuration for AvailAI API and E2E tests.
// Uses the FastAPI test server started via webServer config.
// Called by: npx playwright test
// Depends on: app/main.py (FastAPI app)

import { defineConfig } from '@playwright/test';
import path from 'path';

const port = parseInt(process.env.PW_PORT || '8787', 10);
const isCI = !!process.env.CI;

// Repo root — was hardcoded to /root/availai, which only exists on the
// author's machine and breaks the webServer command on GitHub-hosted
// runners (and any other worktree/checkout path). __dirname resolves to
// wherever this config file actually lives.
const repoRoot = __dirname;

// Session state written by e2e/auth.setup.ts (the seeded admin's session
// cookie). A project runs authed by declaring dependencies: ['setup'] and
// use: { storageState: STORAGE_STATE } — applies to BOTH page and request
// fixtures. Gitignored (e2e/.auth/).
export const STORAGE_STATE = path.join(repoRoot, 'e2e/.auth/admin.json');

export default defineConfig({
  testDir: './e2e',
  timeout: 30000,
  retries: 0,
  // Serial by design (R3): one shared in-memory DB across all tests,
  // empty-state assertions, and the launcher's threadpool serialization all
  // assume workers: 1. Raising this is a deliberate future change, not a tweak.
  workers: 1,
  reporter: isCI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    extraHTTPHeaders: {
      'Accept': 'application/json',
    },
    trace: 'retain-on-failure',
  },
  webServer: {
    // scripts/e2e_server.py boots the TESTING app with schema + a seeded
    // DEFAULT_USER_* admin in the SAME process (StaticPool sqlite is
    // per-process), then serves with the sync threadpool serialized.
    // APP_URL=http://… is mandatory: env beats a checkout's .env
    // (pydantic-settings), so the session cookie can never come out Secure
    // and get dropped over http. RATE_LIMIT_ENABLED=false: /auth/login is
    // 5/minute and the limiter is live under TESTING. Credentials are
    // committed, NON-secret (TESTING-only process, throwaway in-memory DB).
    command: `TESTING=1 DATABASE_URL=sqlite:// REDIS_URL="" CACHE_BACKEND=none RATE_LIMIT_ENABLED=false APP_URL=http://127.0.0.1:${port} DEFAULT_USER_EMAIL=e2e-admin@availai.test DEFAULT_USER_PASSWORD=e2e-local-only-pw DEFAULT_USER_ROLE=admin PYTHONPATH=${path.resolve(repoRoot)} python3 scripts/e2e_server.py --host 127.0.0.1 --port ${port}`,
    port,
    timeout: 30000, // create_all (~127 tables) adds boot time over the old bare-uvicorn 15s
    reuseExistingServer: false,
  },
  projects: [
    // Logs the seeded admin in once and writes STORAGE_STATE. A project flips
    // authed (dependencies + storageState) ONLY in the same commit that
    // converts its spec files, so the nine-CI-project run stays green at
    // every commit. Filtered --project runs auto-include dependencies, so
    // CI's explicit project list needs no change.
    { name: 'setup', testMatch: /auth\.setup\.ts$/ },
    { name: 'api', testMatch: /api\.spec\.ts$/ },
    // auth stays anonymous forever — its purpose IS the anonymous baseline
    // (connected===false, protected-route 401 probes).
    { name: 'auth', testMatch: /auth\.spec\.ts$/ },
    {
      name: 'smoke',
      testMatch: /smoke\.spec\.ts$/,
      dependencies: ['setup'],
      use: { storageState: STORAGE_STATE },
    },
    { name: 'data-validation', testMatch: /data-validation\.spec\.ts$/ },
    { name: 'accessibility', testMatch: /accessibility\.spec\.ts$/ },
    // visual stays anonymous — its committed baseline is login-page.png
    // (author's nightly cron + npm run test:visual stay valid).
    { name: 'visual', testMatch: /visual\.spec\.ts$/ },
    {
      name: 'dead-ends',
      testMatch: /dead-ends\.spec\.ts$/,
      dependencies: ['setup'],
      use: { storageState: STORAGE_STATE },
    },
    { name: 'workflows', testMatch: /workflows\.spec\.ts$/ },
    { name: 'materials-ui', testMatch: /materials-ui\.spec\.ts$/ },
    { name: 'sales-hub-ui', testMatch: /sales-hub-ui\.spec\.ts$/ },
  ],
});
