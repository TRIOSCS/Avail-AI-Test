"""Claude agent test prompt generator -- produces detailed test prompts for each UI area.

Generates structured prompts that tell a Claude-in-Chrome agent exactly what to
test, what correct behavior looks like, and how to submit findings as trouble
tickets.

Called by: routers/trouble_tickets.py
Depends on: nothing
"""

AREA_PROMPTS: dict[str, dict] = {
    "search": {
        "url_hash": "#view-sourcing",
        "prompt": (
            "WHAT TO TEST:\n"
            "1. Enter a known MPN (e.g. LM358) in the search bar and click Search.\n"
            "2. Verify results appear from multiple sources (BrokerBin, Nexar, DigiKey, Mouser, OEMSecrets).\n"
            "3. Click a result row to expand details -- check price, qty, vendor name render correctly.\n"
            "4. Try searching an invalid/gibberish MPN and confirm a 'no results' message appears.\n"
            "5. Check that the source badges and scoring indicators display properly.\n\n"
            "WHAT CORRECT LOOKS LIKE:\n"
            "- Results load within 10 seconds with a progress indicator.\n"
            "- Each result row shows vendor, MPN, quantity, price, and source.\n"
            "- No JavaScript errors in the console.\n"
            "- Empty search shows a validation message, not a crash.\n\n"
            "SUBMITTING FINDINGS:\n"
            "Before creating a ticket, POST to /api/trouble-tickets/similar with your description "
            "to check for duplicates. Then POST to /api/trouble-tickets with "
            '{"source": "agent", "tested_area": "search", "title": "...", "description": "..."}.'
        ),
    },
    "requisitions": {
        "url_hash": "#view-requisitions",
        "prompt": (
            "WHAT TO TEST:\n"
            "1. Navigate to the requisitions view and verify the list loads.\n"
            "2. Click a requisition to open its detail drawer.\n"
            "3. Check that parts list, status badge, and assignee render correctly.\n"
            "4. Try filtering by status (open, closed) and verify results update.\n"
            "5. Clone a requisition and confirm the clone appears in the list.\n\n"
            "WHAT CORRECT LOOKS LIKE:\n"
            "- Requisition list loads with pagination controls.\n"
            "- Detail drawer shows all parts with quantities and target prices.\n"
            "- Status filters actually reduce the visible list.\n"
            "- Cloned requisition has a new ID but same parts.\n\n"
            "SUBMITTING FINDINGS:\n"
            "Before creating a ticket, POST to /api/trouble-tickets/similar with your description "
            "to check for duplicates. Then POST to /api/trouble-tickets with "
            '{"source": "agent", "tested_area": "requisitions", "title": "...", "description": "..."}.'
        ),
    },
    "rfq": {
        "url_hash": "#view-rfq",
        "prompt": (
            "WHAT TO TEST:\n"
            "1. Open the RFQ view and verify pending/sent/responded tabs load.\n"
            "2. Check that RFQ rows show vendor, part, quantity, and status.\n"
            "3. Open an RFQ detail and verify the email thread renders.\n"
            "4. Check that response parsing results (if any) display with confidence scores.\n"
            "5. Verify the batch send UI shows recipient count and confirmation dialog.\n\n"
            "WHAT CORRECT LOOKS LIKE:\n"
            "- Tab counts match the actual number of RFQs in each state.\n"
            "- Email thread shows sent and received messages in chronological order.\n"
            "- Parsed responses show extracted price, quantity, and lead time.\n"
            "- No console errors when switching between tabs.\n\n"
            "SUBMITTING FINDINGS:\n"
            "Before creating a ticket, POST to /api/trouble-tickets/similar with your description "
            "to check for duplicates. Then POST to /api/trouble-tickets with "
            '{"source": "agent", "tested_area": "rfq", "title": "...", "description": "..."}.'
        ),
    },
    "crm_companies": {
        "url_hash": "#view-companies",
        "prompt": (
            "WHAT TO TEST:\n"
            "1. Load the companies list and verify pagination works (Load More button).\n"
            "2. Click a company to open the detail drawer.\n"
            "3. Check that Sites, Contacts, Requisitions tabs load inside the drawer.\n"
            "4. Verify owner filter dropdown filters the list server-side.\n"
            "5. Check that site_count and open_req_count display correctly.\n\n"
            "WHAT CORRECT LOOKS LIKE:\n"
            "- Companies load quickly (under 1 second) with counts.\n"
            "- Drawer tabs lazy-load their content without full page reload.\n"
            "- Owner filter reduces the company list to only that owner's companies.\n"
            "- No N+1 query indicators (no stalling on large lists).\n\n"
            "SUBMITTING FINDINGS:\n"
            "Before creating a ticket, POST to /api/trouble-tickets/similar with your description "
            "to check for duplicates. Then POST to /api/trouble-tickets with "
            '{"source": "agent", "tested_area": "crm_companies", "title": "...", "description": "..."}.'
        ),
    },
    "crm_contacts": {
        "url_hash": "#view-contacts",
        "prompt": (
            "WHAT TO TEST:\n"
            "1. Load the contacts list and verify bulk loading works.\n"
            "2. Search for a contact by name or email.\n"
            "3. Click a contact to view details -- check phone, email, company link.\n"
            "4. Verify encrypted fields (phone, email) decrypt and display correctly.\n"
            "5. Check that the enrichment status indicators render.\n\n"
            "WHAT CORRECT LOOKS LIKE:\n"
            "- Contacts load via bulk endpoint (not individual requests).\n"
            "- Search filters the displayed contacts.\n"
            "- Encrypted fields show actual values, not ciphertext or errors.\n"
            "- Company links navigate to the correct company in the CRM.\n\n"
            "SUBMITTING FINDINGS:\n"
            "Before creating a ticket, POST to /api/trouble-tickets/similar with your description "
            "to check for duplicates. Then POST to /api/trouble-tickets with "
            '{"source": "agent", "tested_area": "crm_contacts", "title": "...", "description": "..."}.'
        ),
    },
    "crm_quotes": {
        "url_hash": "#view-quotes",
        "prompt": (
            "WHAT TO TEST:\n"
            "1. Load the quotes list and verify it displays.\n"
            "2. Check that quote rows show customer, total, status, and date.\n"
            "3. Open a quote detail and verify line items render with prices.\n"
            "4. Check that currency formatting handles large values correctly.\n"
            "5. Verify status transitions (draft -> sent -> accepted) are reflected in the UI.\n\n"
            "WHAT CORRECT LOOKS LIKE:\n"
            "- Quotes list loads without errors.\n"
            "- Currency values use proper formatting (commas, decimals).\n"
            "- Line items sum to the displayed total.\n"
            "- Status badges use distinct colors for each state.\n\n"
            "SUBMITTING FINDINGS:\n"
            "Before creating a ticket, POST to /api/trouble-tickets/similar with your description "
            "to check for duplicates. Then POST to /api/trouble-tickets with "
            '{"source": "agent", "tested_area": "crm_quotes", "title": "...", "description": "..."}.'
        ),
    },
    "prospecting": {
        "url_hash": "#view-suggested",
        "prompt": (
            "WHAT TO TEST:\n"
            "1. Navigate to the prospecting discovery pool.\n"
            "2. Verify company cards render with name, industry, revenue range.\n"
            "3. Check that filter controls (industry, region, revenue) work.\n"
            "4. Click a card to view company details.\n"
            "5. Verify the stats endpoint shows discovery pool metrics.\n\n"
            "WHAT CORRECT LOOKS LIKE:\n"
            "- Cards display in a grid layout with consistent formatting.\n"
            "- Filters reduce the visible set of cards.\n"
            "- Card click opens a detail view or drawer.\n"
            "- Stats show total pool size and breakdown by source.\n\n"
            "SUBMITTING FINDINGS:\n"
            "Before creating a ticket, POST to /api/trouble-tickets/similar with your description "
            "to check for duplicates. Then POST to /api/trouble-tickets with "
            '{"source": "agent", "tested_area": "prospecting", "title": "...", "description": "..."}.'
        ),
    },
    "vendors": {
        "url_hash": "#view-vendors",
        "prompt": (
            "WHAT TO TEST:\n"
            "1. Load the vendors list and verify it populates.\n"
            "2. Search for a vendor by name.\n"
            "3. Click a vendor to see their detail card with reliability score.\n"
            "4. Check that sighting counts and last-seen dates display.\n"
            "5. Verify vendor normalization (no duplicate entries for same vendor).\n\n"
            "WHAT CORRECT LOOKS LIKE:\n"
            "- Vendor list loads with name, source count, and reliability indicators.\n"
            "- Search narrows the list in real time.\n"
            "- Detail card shows historical sighting data.\n"
            "- No duplicate vendor entries for the same normalized name.\n\n"
            "SUBMITTING FINDINGS:\n"
            "Before creating a ticket, POST to /api/trouble-tickets/similar with your description "
            "to check for duplicates. Then POST to /api/trouble-tickets with "
            '{"source": "agent", "tested_area": "vendors", "title": "...", "description": "..."}.'
        ),
    },
    "tagging": {
        "url_hash": "#view-tagging",
        "prompt": (
            "WHAT TO TEST:\n"
            "1. Open the tagging view and verify tag statistics load.\n"
            "2. Check that coverage percentage and confidence distribution display.\n"
            "3. Verify the tag list shows manufacturer tags with counts.\n"
            "4. Check that threshold configuration controls are accessible (admin).\n"
            "5. Verify the internal part count is shown in status.\n\n"
            "WHAT CORRECT LOOKS LIKE:\n"
            "- Tag statistics show total tagged, coverage percentage, and confidence bands.\n"
            "- Tag list is sorted by count descending.\n"
            "- Threshold controls show min_count and min_percentage values.\n"
            "- Internal part count is a non-negative integer.\n\n"
            "SUBMITTING FINDINGS:\n"
            "Before creating a ticket, POST to /api/trouble-tickets/similar with your description "
            "to check for duplicates. Then POST to /api/trouble-tickets with "
            '{"source": "agent", "tested_area": "tagging", "title": "...", "description": "..."}.'
        ),
    },
    "tickets": {
        "url_hash": "#view-tickets",
        "prompt": (
            "WHAT TO TEST:\n"
            "1. Open the trouble tickets view and verify the list loads.\n"
            "2. Check that tickets show title, status, source, and created date.\n"
            "3. Filter by status (submitted, diagnosed, resolved) and verify filtering.\n"
            "4. Open a ticket detail and check the diagnosis and action history.\n"
            "5. Verify keyboard shortcuts (e for execute, s for skip, r for reject) work.\n\n"
            "WHAT CORRECT LOOKS LIKE:\n"
            "- Ticket list loads with color-coded status badges.\n"
            "- Filters reduce the list to matching tickets only.\n"
            "- Detail view shows full description, AI diagnosis, and execution log.\n"
            "- Keyboard shortcuts trigger the correct action with confirmation.\n\n"
            "SUBMITTING FINDINGS:\n"
            "Before creating a ticket, POST to /api/trouble-tickets/similar with your description "
            "to check for duplicates. Then POST to /api/trouble-tickets with "
            '{"source": "agent", "tested_area": "tickets", "title": "...", "description": "..."}.'
        ),
    },
    "admin_api_health": {
        "url_hash": "#view-api-health",
        "prompt": (
            "WHAT TO TEST:\n"
            "1. Navigate to the API Health admin view.\n"
            "2. Verify the status grid shows all configured API sources.\n"
            "3. Check that each source displays status (healthy/degraded/down), last check time.\n"
            "4. Verify the usage overview section shows monthly call counts.\n"
            "5. Check that the persistent warning banner appears for any degraded/down sources.\n\n"
            "WHAT CORRECT LOOKS LIKE:\n"
            "- Status grid has a card for each API source with color-coded status.\n"
            "- Last check timestamps are recent (within the polling interval).\n"
            "- Usage numbers are non-negative integers.\n"
            "- Banner only appears when at least one source is not healthy.\n\n"
            "SUBMITTING FINDINGS:\n"
            "Before creating a ticket, POST to /api/trouble-tickets/similar with your description "
            "to check for duplicates. Then POST to /api/trouble-tickets with "
            '{"source": "agent", "tested_area": "admin_api_health", "title": "...", "description": "..."}.'
        ),
    },
    "admin_settings": {
        "url_hash": "#view-settings",
        "prompt": (
            "WHAT TO TEST:\n"
            "1. Open the admin settings view.\n"
            "2. Verify system configuration values display.\n"
            "3. Check that feature flags (email mining, activity tracking, etc.) are shown.\n"
            "4. Verify API source credentials show masked values (not plaintext).\n"
            "5. Check that saving a setting change persists after page reload.\n\n"
            "WHAT CORRECT LOOKS LIKE:\n"
            "- Settings page loads without errors.\n"
            "- Credentials are masked (show asterisks or 'configured' indicator).\n"
            "- Feature flags show on/off toggle state.\n"
            "- Saved changes survive a page refresh.\n\n"
            "SUBMITTING FINDINGS:\n"
            "Before creating a ticket, POST to /api/trouble-tickets/similar with your description "
            "to check for duplicates. Then POST to /api/trouble-tickets with "
            '{"source": "agent", "tested_area": "admin_settings", "title": "...", "description": "..."}.'
        ),
    },
    "notifications": {
        "url_hash": "#",
        "prompt": (
            "WHAT TO TEST:\n"
            "1. Look for the bell icon in the top navigation bar on any page.\n"
            "2. Click the bell to open the notifications dropdown.\n"
            "3. Verify unread count badge displays correctly.\n"
            "4. Click a notification to verify it navigates to the relevant item.\n"
            "5. Check that marking a notification as read decrements the badge count.\n\n"
            "WHAT CORRECT LOOKS LIKE:\n"
            "- Bell icon is visible on all pages.\n"
            "- Dropdown shows recent notifications in reverse chronological order.\n"
            "- Unread badge shows a number > 0 when there are unread notifications.\n"
            "- Clicking a notification marks it read and navigates correctly.\n\n"
            "SUBMITTING FINDINGS:\n"
            "Before creating a ticket, POST to /api/trouble-tickets/similar with your description "
            "to check for duplicates. Then POST to /api/trouble-tickets with "
            '{"source": "agent", "tested_area": "notifications", "title": "...", "description": "..."}.'
        ),
    },
    "auth": {
        "url_hash": "#",
        "prompt": (
            "WHAT TO TEST:\n"
            "1. Visit the login page and verify it renders with the app version.\n"
            "2. Check that the Microsoft OAuth login button is present and styled.\n"
            "3. Verify that accessing a protected page while logged out redirects to /auth/login.\n"
            "4. Check that 401 responses from API calls trigger a redirect to /auth/login.\n"
            "5. Verify session persistence -- refresh the page and confirm you stay logged in.\n\n"
            "WHAT CORRECT LOOKS LIKE:\n"
            "- Login page shows app name, version, and Microsoft sign-in button.\n"
            "- Unauthenticated API calls return 401, not 500.\n"
            "- 401 triggers client-side redirect to /auth/login (not /login).\n"
            "- Refreshing a page preserves the session.\n\n"
            "SUBMITTING FINDINGS:\n"
            "Before creating a ticket, POST to /api/trouble-tickets/similar with your description "
            "to check for duplicates. Then POST to /api/trouble-tickets with "
            '{"source": "agent", "tested_area": "auth", "title": "...", "description": "..."}.'
        ),
    },
    "upload": {
        "url_hash": "#view-upload",
        "prompt": (
            "WHAT TO TEST:\n"
            "1. Navigate to the upload view.\n"
            "2. Verify the file upload area renders with drag-and-drop support.\n"
            "3. Try uploading a valid Excel file and verify it processes.\n"
            "4. Try uploading an invalid file type and verify the error message.\n"
            "5. Check that upload progress indication works during processing.\n\n"
            "WHAT CORRECT LOOKS LIKE:\n"
            "- Upload area shows accepted file types and size limits.\n"
            "- Valid files process and show a success summary with row count.\n"
            "- Invalid files show a clear error message without crashing.\n"
            "- Progress indicator appears during file processing.\n\n"
            "SUBMITTING FINDINGS:\n"
            "Before creating a ticket, POST to /api/trouble-tickets/similar with your description "
            "to check for duplicates. Then POST to /api/trouble-tickets with "
            '{"source": "agent", "tested_area": "upload", "title": "...", "description": "..."}.'
        ),
    },
    "pipeline": {
        "url_hash": "#view-pipeline",
        "prompt": (
            "WHAT TO TEST:\n"
            "1. Open the pipeline view and verify columns/stages render.\n"
            "2. Check that deal cards appear in the correct stage columns.\n"
            "3. Verify deal values display with proper currency formatting.\n"
            "4. Check that the pipeline total and stage subtotals are calculated.\n"
            "5. Verify that over-capacity indicators display when applicable.\n\n"
            "WHAT CORRECT LOOKS LIKE:\n"
            "- Pipeline shows distinct columns for each stage.\n"
            "- Deal cards show company name, value, and status.\n"
            "- Currency values use fmtCurrency() formatting (commas, no overflow).\n"
            "- Stage subtotals sum correctly to the pipeline total.\n\n"
            "SUBMITTING FINDINGS:\n"
            "Before creating a ticket, POST to /api/trouble-tickets/similar with your description "
            "to check for duplicates. Then POST to /api/trouble-tickets with "
            '{"source": "agent", "tested_area": "pipeline", "title": "...", "description": "..."}.'
        ),
    },
    "activity": {
        "url_hash": "#view-activity",
        "prompt": (
            "WHAT TO TEST:\n"
            "1. Navigate to the activity feed view.\n"
            "2. Verify recent activity entries load in reverse chronological order.\n"
            "3. Check that each entry shows user, action, target, and timestamp.\n"
            "4. Verify that different activity types use distinct icons or badges.\n"
            "5. Check that the activity feed updates after performing an action elsewhere.\n\n"
            "WHAT CORRECT LOOKS LIKE:\n"
            "- Activity feed loads with recent entries.\n"
            "- Entries are ordered newest first.\n"
            "- Each entry has a clear description of what happened.\n"
            "- Performing an action (e.g. creating a requisition) adds a new entry.\n\n"
            "SUBMITTING FINDINGS:\n"
            "Before creating a ticket, POST to /api/trouble-tickets/similar with your description "
            "to check for duplicates. Then POST to /api/trouble-tickets with "
            '{"source": "agent", "tested_area": "activity", "title": "...", "description": "..."}.'
        ),
    },
}


def generate_all_prompts() -> list[dict]:
    """Return all area test prompts as a list of {area, url_hash, prompt} dicts."""
    return [
        {"area": area, "url_hash": data["url_hash"], "prompt": data["prompt"]}
        for area, data in AREA_PROMPTS.items()
    ]


def generate_area_prompt(area: str) -> dict | None:
    """Return the test prompt for a single area, or None if not found."""
    data = AREA_PROMPTS.get(area)
    if data is None:
        return None
    return {"area": area, "url_hash": data["url_hash"], "prompt": data["prompt"]}
