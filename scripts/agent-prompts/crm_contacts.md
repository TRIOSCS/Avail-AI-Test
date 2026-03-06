# Test Area: CRM Contacts

Navigate to: {{BASE_URL}}/#contacts

## Workflow Tests

### Test 1: Contacts list loads
1. Navigate to {{BASE_URL}}/#contacts
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
