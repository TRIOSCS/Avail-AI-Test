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

export default defineConfig({
  testDir: './e2e',
  timeout: 30000,
  retries: 0,
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
    command: `TESTING=1 DATABASE_URL=sqlite:// REDIS_URL="" CACHE_BACKEND=none PYTHONPATH=${path.resolve(repoRoot)} python3 -m uvicorn app.main:app --host 127.0.0.1 --port ${port}`,
    port,
    timeout: 15000,
    reuseExistingServer: false,
  },
  projects: [
    { name: 'api', testMatch: /api\.spec\.ts$/ },
    { name: 'auth', testMatch: /auth\.spec\.ts$/ },
    { name: 'smoke', testMatch: /smoke\.spec\.ts$/ },
    { name: 'data-validation', testMatch: /data-validation\.spec\.ts$/ },
    { name: 'accessibility', testMatch: /accessibility\.spec\.ts$/ },
    { name: 'visual', testMatch: /visual\.spec\.ts$/ },
    { name: 'dead-ends', testMatch: /dead-ends\.spec\.ts$/ },
    { name: 'workflows', testMatch: /workflows\.spec\.ts$/ },
    { name: 'materials-ui', testMatch: /materials-ui\.spec\.ts$/ },
    { name: 'sales-hub-ui', testMatch: /sales-hub-ui\.spec\.ts$/ },
  ],
});
