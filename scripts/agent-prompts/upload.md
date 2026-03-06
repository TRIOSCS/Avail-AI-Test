# Test Area: Upload

Navigate to: {{BASE_URL}}/

## Workflow Tests

### Test 1: Navigate to Upload Section
1. Use `browser_navigate` to go to `{{BASE_URL}}/`
2. Use `browser_snapshot` to capture the page state
3. Look for an "Upload" or "Import" link in the sidebar navigation
4. Use `browser_click` on the Upload sidebar item
5. Use `browser_snapshot` to capture the upload page
6. VERIFY: The upload section loads and displays a file upload area
7. Use `browser_console_messages` to check for JavaScript errors on load

### Test 2: Verify File Upload Area Renders
1. Use `browser_snapshot` to inspect the upload interface
2. VERIFY: A file upload zone is visible (drag-and-drop area, file input button, or browse button)
3. VERIFY: The upload area has clear instructions or a prompt (e.g. "Drag files here" or "Choose file")
4. VERIFY: The upload area is not broken or hidden behind other elements
5. VERIFY: No JavaScript errors in the console related to the upload component

### Test 3: Check Accepted File Types
1. Use `browser_snapshot` to look for file type restrictions or hints
2. VERIFY: Accepted file types are indicated somewhere on the page (e.g. ".csv", ".xlsx", "Excel files")
3. VERIFY: The file type information is clearly visible and readable
4. VERIFY: If a file input element exists, check that it has an `accept` attribute or equivalent restriction

### Test 4: Verify Upload Page Layout
1. Use `browser_snapshot` to check the overall layout
2. VERIFY: The upload area is properly sized and positioned
3. VERIFY: Any instructional text is readable and not truncated
4. VERIFY: If there is a history of previous uploads, it displays properly
5. VERIFY: No overlapping elements or broken CSS
6. Use `browser_take_screenshot` to capture a visual record

### Test 5: Verify No Auto-Upload Triggers
1. Use `browser_snapshot` to confirm the page is in a ready state
2. VERIFY: No upload is triggered automatically on page load
3. VERIFY: No progress bars or upload indicators are active without user action
4. DO NOT actually upload any files (this could create test data in the system)
5. Use `browser_network_requests` to verify no unexpected POST requests are made on load

## What Correct Looks Like
- Upload section is accessible from the sidebar navigation
- A clear file upload zone is displayed (drag-and-drop area or file browse button)
- Accepted file types are indicated on the page
- The upload area is properly styled and responsive
- No uploads are triggered without explicit user action
- No console errors or failed network requests on page load
- Previous upload history (if shown) displays properly

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Buttons that don't respond to clicks
- Upload area not rendering or not visible
- Missing file type indicators
- Auto-triggered uploads on page load
- Broken drag-and-drop zone styling
