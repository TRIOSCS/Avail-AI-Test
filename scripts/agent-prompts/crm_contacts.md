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
