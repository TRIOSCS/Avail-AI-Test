# Site Test Agent Instructions

You are testing the AvailAI application at https://app.availai.net.
You have access to the Playwright MCP tools to control a real browser.

## How to navigate
Use the Playwright browser tools:
1. browser_navigate to go to URLs
2. browser_snapshot to see the current page state (accessibility tree)
3. browser_click to click elements (use ref numbers from snapshot)
4. browser_fill_form to type into inputs
5. browser_take_screenshot to capture visual evidence
6. browser_console_messages to check for JS errors
7. browser_network_requests to check for failed API calls

## Authentication
The dispatcher has already set your session cookie — you should be logged in when you navigate to the site.

## When you find an issue
1. Take a screenshot with browser_take_screenshot
2. Check browser_console_messages for JS errors
3. Check browser_network_requests for failed requests
4. File a trouble ticket using Bash:

```bash
curl -s -X POST https://app.availai.net/api/trouble-tickets \
  -H "Content-Type: application/json" \
  -H "x-agent-key: Cmwq2kFDWnEbDO2fy4UF-UVf5QGgDDq-HDE6ZwYnkaU" \
  -d '{
    "source": "agent",
    "tested_area": "auth",
    "title": "SHORT TITLE",
    "description": "DETAILED DESCRIPTION WITH STEPS",
    "current_page": "URL WHERE ISSUE OCCURRED",
    "console_errors": "ANY JS ERRORS FROM CONSOLE",
    "current_view": "auth"
  }'
```

## Before filing a ticket
Check for duplicates first:

```bash
curl -s "https://app.availai.net/api/trouble-tickets/similar?title=URL_ENCODED_TITLE&description=URL_ENCODED_DESC" \
  -H "x-agent-key: Cmwq2kFDWnEbDO2fy4UF-UVf5QGgDDq-HDE6ZwYnkaU"
```

If the response contains a match with similarity > 0.7, skip filing.

## When everything works
If all tests pass with no issues, just output: PASS: auth

## Rules
- Do NOT click delete/remove/logout/destroy/purge buttons
- Take a screenshot BEFORE and AFTER each major action
- If a page doesn't load within 15 seconds, file a ticket and move on
- Check console for errors after every navigation and click
- Be thorough — work through COMPLETE workflows, not just surface checks

## Deep Testing Strategy
Go beyond just "does the page load" — exercise the full workflow:
1. **Navigate** to the section and verify it renders
2. **Interact** with every control: filters, dropdowns, tabs, search, sort, pagination
3. **Submit data** where safe: fill forms, run searches, apply filters, expand/collapse sections
4. **Follow chains**: click a result row → check detail drawer → click each tab → verify data loads
5. **Test edge cases**: empty searches, special characters, very long inputs, rapid clicks
6. **Check responsiveness**: verify tables have data, counts match, no stale/cached data
7. **Verify API health**: check network tab for any 4xx/5xx responses on EVERY action
8. **Console errors**: check after EVERY click and navigation, not just page load
9. **Cross-reference**: if a count says "42 vendors", click through and verify the list has ~42 items
10. **Try breaking things**: enter SQL-like input, paste HTML into text fields, use unicode characters

File a ticket for ANYTHING wrong — broken layouts, missing data, slow loads (>5s),
misleading labels, dead links, empty states that should have data, inconsistent counts,
buttons that do nothing, modals that don't close, filters that don't filter.

---

# Test Area: Authentication

Navigate to: https://app.availai.net/auth/login

## Workflow Tests

### Test 1: Verify Login Page Renders
1. Use `browser_navigate` to go to `https://app.availai.net/auth/login`
2. Use `browser_snapshot` to capture the login page
3. VERIFY: The login page renders fully without errors
4. VERIFY: The app name "AvailAI" or equivalent branding is displayed
5. VERIFY: A version number is displayed on the login page (e.g. "v1.x.x")
6. Use `browser_console_messages` to check for JavaScript errors
7. Use `browser_network_requests` to check for failed resource loads (CSS, JS, images)

### Test 2: Verify Microsoft OAuth Button
1. Use `browser_snapshot` to inspect the login page controls
2. VERIFY: A "Sign in with Microsoft" button (or similar OAuth button) is present
3. VERIFY: The button has appropriate styling (Microsoft branding, icon, or recognizable text)
4. VERIFY: The button appears clickable and is not disabled
5. DO NOT click the OAuth button (it would redirect to Microsoft's auth flow)

### Test 3: Verify Login Page Layout
1. Use `browser_snapshot` to check overall page design
2. VERIFY: The login form is centered or prominently placed on the page
3. VERIFY: No layout issues (overlapping elements, text overflow, broken images)
4. VERIFY: The page uses proper styling (not unstyled HTML or broken CSS)
5. Use `browser_take_screenshot` to capture a visual record of the login page

### Test 4: Verify Session-Protected Content
1. Use `browser_navigate` to go to `https://app.availai.net/`
2. Use `browser_snapshot` to capture the page state
3. If the session is active: VERIFY that the main application content loads (sidebar, dashboard, etc.)
4. If the session is not active: VERIFY that the page redirects to the login page at `/auth/login`
5. VERIFY: There is no intermediate error page or broken state between login and protected content
6. Use `browser_console_messages` to check for authentication-related errors

### Test 5: Verify No Credential Leaks
1. Use `browser_navigate` to go to `https://app.availai.net/auth/login`
2. Use `browser_snapshot` to inspect the page source
3. VERIFY: No API keys, tokens, or secrets are visible in the page content
4. VERIFY: No sensitive configuration values are exposed in the HTML
5. Use `browser_network_requests` to check that no authentication tokens are sent to unexpected endpoints

### Test 6: Verify authenticated app loads fully
1. Navigate to https://app.availai.net/ (with session cookie from dispatcher)
2. Use browser_snapshot to read the full page
3. VERIFY: Sidebar navigation is visible with sections (RFQs, Vendors, Customers, Admin, etc.)
4. VERIFY: User name or email appears in header/profile area
5. VERIFY: No "login" redirect — session cookie is working
6. Check browser_console_messages for any auth-related errors

### Test 7: Navigate all sidebar sections
1. From the authenticated home page, click each sidebar item one by one
2. For each: take a snapshot, verify content loads, check console for errors
3. Test these sections: RFQs, Vendors, Customers, Pipeline/Dashboard, Admin
4. VERIFY: Every section loads content (not blank pages)
5. VERIFY: No 401 or 403 errors in network requests
6. VERIFY: No redirect to login page during navigation

### Test 8: Health endpoint
1. Use Bash to run: `curl -s https://app.availai.net/health`
2. VERIFY: Returns JSON with `{"status": "ok"}` or similar
3. VERIFY: Response time < 2 seconds
4. VERIFY: HTTP status code is 200

### Test 9: Auth status endpoint
1. Use Bash to run: `curl -s https://app.availai.net/auth/status -b "session=SESSION_COOKIE"`
2. Or navigate to auth/status via browser
3. VERIFY: Returns JSON with user info (connected status, email, role)
4. VERIFY: No sensitive tokens in the response

### Test 10: Error page handling
1. Navigate to https://app.availai.net/#nonexistent-section
2. VERIFY: Shows a graceful fallback or redirects to a valid section
3. VERIFY: No crash, no white screen, no unhandled JS error
4. Navigate to https://app.availai.net/api/nonexistent-endpoint
5. VERIFY: Returns proper JSON error (not HTML error page or stack trace)

## What Correct Looks Like
- Login page loads within 3 seconds at `/auth/login`
- App name and version number are clearly displayed
- A Microsoft OAuth sign-in button is prominently placed
- The page has clean, styled layout with proper branding
- Navigating to `/` either loads the app (if session exists) or redirects to login
- No credentials or secrets are exposed in the page content
- No console errors or failed network requests

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Buttons that don't respond to clicks
- Missing OAuth button or broken button styling
- Version number not displayed on login page
- Credentials or tokens visible in page source
- Redirect loop between login and protected pages
