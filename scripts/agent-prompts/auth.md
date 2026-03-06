# Test Area: Authentication

Navigate to: {{BASE_URL}}/auth/login

## Workflow Tests

### Test 1: Verify Login Page Renders
1. Use `browser_navigate` to go to `{{BASE_URL}}/auth/login`
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
1. Use `browser_navigate` to go to `{{BASE_URL}}/`
2. Use `browser_snapshot` to capture the page state
3. If the session is active: VERIFY that the main application content loads (sidebar, dashboard, etc.)
4. If the session is not active: VERIFY that the page redirects to the login page at `/auth/login`
5. VERIFY: There is no intermediate error page or broken state between login and protected content
6. Use `browser_console_messages` to check for authentication-related errors

### Test 5: Verify No Credential Leaks
1. Use `browser_navigate` to go to `{{BASE_URL}}/auth/login`
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
