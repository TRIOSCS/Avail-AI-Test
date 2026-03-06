# Test Area: Admin Settings

Navigate to: {{BASE_URL}}/#settings

## Workflow Tests

### Test 1: Navigate to Settings Page
1. Use `browser_navigate` to go to `{{BASE_URL}}/#settings`
2. Use `browser_snapshot` to capture the page state
3. VERIFY: The settings page loads and displays configuration sections
4. VERIFY: The page has a clear heading or title indicating "Settings" or "Configuration"
5. Use `browser_console_messages` to check for JavaScript errors on load
6. Use `browser_network_requests` to check for failed API calls

### Test 2: Verify Configuration Sections
1. Use `browser_snapshot` to inspect the settings layout
2. VERIFY: The page is organized into logical sections (e.g. API Keys, Feature Flags, System Config)
3. VERIFY: Each section has a heading or label
4. VERIFY: Section content is not empty or showing loading spinners indefinitely
5. VERIFY: No sections display "undefined", "null", or "[object Object]"

### Test 3: Verify Feature Flags Display
1. Use `browser_snapshot` to look for feature flag toggles or indicators
2. VERIFY: Feature flags are listed with descriptive names
3. VERIFY: Each flag shows an on/off state (toggle switch, checkbox, or text indicator)
4. VERIFY: The current state of each flag is clearly visible (not ambiguous)
5. VERIFY: Flag names are human-readable (not raw config key names like "ENABLE_FOO_BAR")

### Test 4: Verify API Credentials Are Masked
1. Use `browser_snapshot` to look for API key or credential fields
2. VERIFY: API keys and secrets are masked (shown as dots, asterisks, or "****" patterns)
3. VERIFY: No plaintext API keys, tokens, or secrets are visible on the page
4. VERIFY: If a "show" or "reveal" button exists, it requires confirmation before unmasking
5. VERIFY: Password fields use type="password" or equivalent masking

### Test 5: Verify Settings Page Responsiveness
1. Use `browser_snapshot` to check overall page rendering
2. VERIFY: All form elements (inputs, toggles, buttons) are properly aligned
3. VERIFY: Save or update buttons (if present) are visible and accessible
4. VERIFY: No overlapping elements or broken layouts
5. Use `browser_console_messages` to confirm no JavaScript errors

## What Correct Looks Like
- Settings page loads within 3 seconds at the #settings hash route
- Page is organized into clearly labeled configuration sections
- Feature flags show descriptive names with visible on/off states
- API credentials are masked with dots or asterisks (never shown in plaintext)
- Form elements are properly rendered and aligned
- Save/update buttons are present and accessible where applicable
- No console errors or failed network requests

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Buttons that don't respond to clicks
- API keys or secrets displayed in plaintext (security issue)
- Feature flags with ambiguous or missing state indicators
- Broken layout or overlapping elements
