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
- Be thorough but finish within 3 minutes

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
