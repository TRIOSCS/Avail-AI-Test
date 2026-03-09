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
    "tested_area": "crm_contacts",
    "title": "SHORT TITLE",
    "description": "DETAILED DESCRIPTION WITH STEPS",
    "current_page": "URL WHERE ISSUE OCCURRED",
    "console_errors": "ANY JS ERRORS FROM CONSOLE",
    "current_view": "crm_contacts"
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
If all tests pass with no issues, just output: PASS: crm_contacts

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

# Test Area: CRM Contacts

Navigate to: https://app.availai.net/#contacts

## Workflow Tests

### Test 1: Contacts list loads
1. Navigate to https://app.availai.net/#contacts
2. Take a screenshot to capture the initial state
3. Use browser_snapshot to read the accessibility tree
4. VERIFY: A list of contacts is visible with names
5. VERIFY: The list has multiple entries (not empty)
6. Check browser_console_messages for any JS errors
7. Check browser_network_requests for any failed API calls

### Test 2: Contact detail view
1. Click on the first contact in the list
2. Take a screenshot after clicking
3. Use browser_snapshot to read the detail content
4. VERIFY: Contact detail opens showing the contact's name
5. VERIFY: Contact information fields are visible (phone, email, title, company)
6. Check browser_console_messages for any JS errors

### Test 3: Encrypted fields display correctly
1. In the contact detail view, examine the phone and email fields
2. VERIFY: Phone numbers display as readable phone numbers (e.g., "+1-555-123-4567"), NOT as base64 ciphertext (long random strings starting with "gAAAAA" or similar)
3. VERIFY: Email addresses display as readable emails (e.g., "user@company.com"), NOT as encrypted blobs
4. VERIFY: No fields show "Error", "undefined", or "null" where real data should be
5. Take a screenshot showing the contact fields

### Test 4: Contact list data integrity
1. Navigate back to the contacts list
2. Scan several rows in the list
3. VERIFY: Names are readable text (first name, last name)
4. VERIFY: Company associations show company names, not IDs
5. VERIFY: No rows display "[object Object]" or raw JSON
6. Check browser_console_messages for decryption or parsing errors

### Test 5: Contact search/filter
1. Look for a search input or filter field on the contacts list page
2. Type a partial name (e.g. the first 3-4 characters of a contact visible in the list)
3. Take a screenshot after typing
4. VERIFY: The list filters down to show only matching contacts
5. VERIFY: Results update dynamically without a full page reload
6. VERIFY: Clearing the search field restores the full list
7. If available, try filtering by company name and verify it narrows results
8. Check browser_console_messages for any JS errors during search
9. Check browser_network_requests for any failed API calls during filtering

### Test 6: Contact detail field completeness
1. Click on a contact in the list to open the detail view
2. Take a screenshot of the full detail view
3. Use browser_snapshot to read all fields in the detail view
4. VERIFY: The contact's full name (first and last) is displayed prominently
5. VERIFY: Job title or role is shown if available (not "undefined" or "null")
6. VERIFY: Company name is shown and is readable text (not an ID or UUID)
7. VERIFY: At least one communication channel is present (email or phone)
8. VERIFY: No fields show placeholder text like "N/A" where real data exists in other contacts
9. Check browser_console_messages for any JS errors

### Test 7: Phone number formatting
1. In a contact detail view, examine the phone number field
2. VERIFY: Phone numbers are formatted in a readable way (e.g. "+1 (555) 123-4567" or "+1-555-123-4567")
3. VERIFY: Phone numbers are NOT displayed as unformatted digit strings (e.g. "15551234567")
4. VERIFY: Phone numbers are NOT displayed as ciphertext (long strings starting with "gAAAAA" or similar base64)
5. VERIFY: If multiple phone numbers exist, each is on its own line or clearly separated
6. Check several contacts to confirm formatting is consistent across the list
7. Take a screenshot showing phone number display

### Test 8: Email links
1. In a contact detail view, examine the email address field
2. VERIFY: Email addresses are displayed as readable addresses (e.g. "user@company.com")
3. VERIFY: Email addresses are clickable links
4. Click on the email address (or inspect its HTML)
5. VERIFY: The link uses a "mailto:" href (should open an email client, not navigate to a broken page)
6. VERIFY: Email addresses are NOT displayed as ciphertext or encrypted blobs
7. Take a screenshot showing the email link

### Test 9: Vendor card association
1. In a contact detail view, look for a linked vendor, vendor card, or company association
2. VERIFY: The contact is associated with a company or vendor name (not orphaned)
3. If a vendor card or company link is clickable, click it
4. VERIFY: It navigates to the correct company or vendor detail view
5. Navigate back to the contacts list
6. VERIFY: The contacts list is still intact after navigating back
7. Check browser_console_messages for any JS errors during navigation

### Test 10: Duplicate detection or merge UI
1. On the contacts list page, look for any duplicate detection indicators (e.g. warning badges, "possible duplicate" labels, merge buttons)
2. If a merge or dedup feature exists, take a screenshot and note the UI elements
3. Look for contacts with the same name or very similar names in the list
4. VERIFY: If duplicates exist, there is some visual indication or grouping
5. If a merge button exists, click it but do NOT confirm the merge (just verify the UI opens)
6. If no duplicate detection UI exists, note this as an observation but do not file a failure
7. Check browser_console_messages for any JS errors

## What Correct Looks Like
- Contacts list loads with readable names and company associations
- Clicking a contact shows full detail with phone, email, title
- Phone and email fields show decrypted, human-readable values
- No ciphertext, raw JSON, or encoding artifacts visible
- Search/filter narrows the list dynamically and clears properly
- Contact detail shows all relevant fields (name, title, company, phone, email)
- Phone numbers are formatted readably, not raw digit strings
- Email addresses are clickable mailto: links
- Contacts are associated with vendors or companies
- No console errors during navigation

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Ciphertext or base64 strings displayed where readable data should be
- Phone or email fields showing "Error" or empty when data should exist
- Phone numbers displayed as unformatted digit strings
- Email addresses that are not clickable or use wrong href
- Contacts not linked to any company or vendor
- Search/filter that does not work or throws errors
- Buttons that don't respond to clicks
