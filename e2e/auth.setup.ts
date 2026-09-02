// Setup project — logs the seeded e2e admin in ONCE and saves storageState.
// The webServer (scripts/e2e_server.py) seeds DEFAULT_USER_* with a PBKDF2
// password hash; POST /auth/login is form-encoded and CSRF-exempt, and the
// CSRF middleware is absent under TESTING — no token dance needed.
// Called by: every project that declares dependencies: ['setup']
//            (auto-run by filtered --project invocations, incl. CI's)
// Depends on: scripts/e2e_server.py (seeding), app/routers/auth.py (login)

import { test as setup, expect } from '@playwright/test';
import { STORAGE_STATE } from '../playwright.config';

const EMAIL = process.env.DEFAULT_USER_EMAIL || 'e2e-admin@availai.test';
const PASSWORD = process.env.DEFAULT_USER_PASSWORD || 'e2e-local-only-pw';

setup('authenticate as seeded admin', async ({ request }) => {
  const res = await request.post('/auth/login', {
    form: { email: EMAIL, password: PASSWORD },
  });
  expect(
    res.status(),
    'login must be 200 — check the webServer seeded DEFAULT_USER_* and set ' +
      'RATE_LIMIT_ENABLED=false (scripts/e2e_server.py logs the bootstrap steps)',
  ).toBe(200);
  const body = await res.json();
  expect(body.ok, `login body must be ok:true — got ${JSON.stringify(body)}`).toBe(true);

  const state = await request.storageState({ path: STORAGE_STATE });

  // Hard tripwire for a Secure-cookie regression: if APP_URL ever resolves to
  // https (e.g. the webServer's http pin is removed and a checkout .env wins),
  // the session cookie comes back Secure, is dropped over http, and every
  // "authed" project silently runs anonymous. Fail HERE, loudly, instead.
  const session = state.cookies.find((c) => c.name === 'session');
  expect(
    session,
    'storageState must contain the `session` cookie — a Secure-cookie regression ' +
      '(APP_URL not pinned to http:// in the webServer env) drops it silently',
  ).toBeTruthy();
});
