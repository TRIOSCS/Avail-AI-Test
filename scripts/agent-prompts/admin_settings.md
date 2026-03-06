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

### Test 6: Navigate Through All Admin Sidebar Sections
1. Use `browser_navigate` to go to `{{BASE_URL}}/#admin`
2. Use `browser_snapshot` to identify all sidebar navigation items
3. Look for these sections: Profile, Users, System Health, Config, Data Sources, Enrichment, Teams, Tickets, API Health, Account Transfer, Report Issue
4. For each section found, use `browser_click` on its sidebar item
5. Use `browser_snapshot` after each click
6. VERIFY: Each section loads content (not a blank page or perpetual spinner)
7. VERIFY: Each section has a heading or title that matches the sidebar label
8. VERIFY: No section shows "undefined", "null", or a JavaScript error in the content area
9. Use `browser_console_messages` after navigating to each section to check for JS errors

### Test 7: Verify Users Section Content
1. Navigate to the Users section in the admin sidebar
2. Use `browser_snapshot` to inspect the user list
3. VERIFY: A list or table of users is displayed
4. VERIFY: Each user row shows a name (not empty or "undefined")
5. VERIFY: Each user row shows an email address
6. VERIFY: Each user row shows a role (e.g. admin, buyer, viewer)
7. VERIFY: The list is not empty (at least one admin user should exist)
8. Use `browser_console_messages` to check for errors

### Test 8: Verify Data Sources Section
1. Navigate to the Data Sources section in the admin sidebar
2. Use `browser_snapshot` to inspect the data sources display
3. VERIFY: Each configured connector is listed (e.g. BrokerBin, Nexar, DigiKey, Mouser, OEMSecrets, Element14)
4. VERIFY: Each connector shows a status indicator (enabled/disabled, connected/disconnected, or similar)
5. VERIFY: Status values are not empty, "undefined", or "null"
6. VERIFY: Disabled connectors (e.g. TME) are clearly marked as disabled
7. Use `browser_console_messages` to check for errors

### Test 9: Test Save Button with No Changes
1. Navigate through admin sections looking for any form with a Save, Update, or Submit button
2. Use `browser_snapshot` to identify the form and its current values
3. Without making any changes, use `browser_click` on the Save/Update button
4. Use `browser_snapshot` to capture the result
5. VERIFY: No error message is displayed (saving with no changes should be a no-op or show a success message)
6. VERIFY: The page does not crash or show a 500 error
7. Use `browser_console_messages` to check for JavaScript errors
8. Use `browser_network_requests` to check for failed API calls (4xx, 5xx)

### Test 10: Verify Section Content Is Not Blank
1. Use `browser_navigate` to go to `{{BASE_URL}}/#admin`
2. For each admin sidebar section, click to navigate there
3. Use `browser_snapshot` to capture the content area
4. VERIFY: The main content area has visible text, tables, or form elements (not just whitespace)
5. VERIFY: No section shows only a loading spinner for more than 5 seconds
6. VERIFY: No section displays a generic "Error" message without details
7. Count how many sections load successfully vs fail — report the ratio

## What Correct Looks Like
- Settings page loads within 3 seconds at the #settings hash route
- Page is organized into clearly labeled configuration sections
- Feature flags show descriptive names with visible on/off states
- API credentials are masked with dots or asterisks (never shown in plaintext)
- Form elements are properly rendered and aligned
- Save/update buttons are present and accessible where applicable
- All admin sidebar sections load content without errors
- Users section shows a list with names, emails, and roles
- Data Sources section shows each connector with its status
- Saving a form with no changes does not produce errors
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
- Admin sidebar sections that load blank or fail to render content
- Users list missing names, emails, or roles
- Data Sources missing status indicators for connectors
- Save button producing errors when clicked with no changes
