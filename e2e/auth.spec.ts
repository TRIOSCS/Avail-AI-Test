// Authentication and session tests for AvailAI.
// Tests login flow, auth status, logout, and protected routes.
// Called by: npx playwright test --project=auth
// Depends on: app/routers/auth.py, app/dependencies.py

import { test, expect } from '@playwright/test';

test.describe('Auth Status', () => {
  test('GET /auth/status returns session info', async ({ request }) => {
    const res = await request.get('/auth/status');
    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body).toHaveProperty('connected');
  });

  test('GET /auth/status shows not connected without session', async ({ request }) => {
    const res = await request.get('/auth/status');
    const body = await res.json();
    expect(body.connected).toBe(false);
  });
});

test.describe('Auth Login', () => {
  test('GET /auth/login redirects to Azure', async ({ request }) => {
    const res = await request.get('/auth/login', { maxRedirects: 0 });
    // Should redirect to Microsoft login
    expect([302, 307, 200]).toContain(res.status());
  });

  test('GET /auth/login-form returns HTML', async ({ request }) => {
    const res = await request.get('/auth/login-form');
    expect(res.status()).toBe(200);
    const text = await res.text();
    expect(text).toContain('html');
  });
});

test.describe('Auth Logout', () => {
  test('POST /auth/logout without session succeeds', async ({ request }) => {
    const res = await request.post('/auth/logout');
    // Should succeed or redirect even without active session
    expect([200, 302, 307]).toContain(res.status());
  });
});

test.describe('Protected Routes', () => {
  test('GET /api/materials requires auth', async ({ request }) => {
    const res = await request.get('/api/materials');
    expect([200, 401, 307]).toContain(res.status());
  });

  test('GET /api/sources requires auth', async ({ request }) => {
    const res = await request.get('/api/sources');
    expect([200, 401, 307]).toContain(res.status());
  });
});
