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
- Be thorough but finish within 3 minutes

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

## What Correct Looks Like
- Contacts list loads with readable names and company associations
- Clicking a contact shows full detail with phone, email, title
- Phone and email fields show decrypted, human-readable values
- No ciphertext, raw JSON, or encoding artifacts visible
- No console errors during navigation

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Ciphertext or base64 strings displayed where readable data should be
- Phone or email fields showing "Error" or empty when data should exist
- Buttons that don't respond to clicks
