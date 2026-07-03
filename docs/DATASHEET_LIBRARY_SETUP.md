# AvailAI — Datasheet Library Setup (one-time M365 admin task)

One-time Microsoft 365 admin procedure to give AvailAI a dedicated, company-owned
SharePoint document library for datasheets and file attachments. The deliverable is a
single value — the library's **drive id** — which goes into `DATASHEET_LIBRARY_DRIVE_ID`
in `/root/availai/.env`.

_Written against `app/services/datasheet_library.py`, `app/services/graph_app_auth.py`,
and `app/services/attachment_service.py` as of 2026-07-03._

---

## 1. What this sets up and why

AvailAI automatically captures a verified byte-copy of each part's manufacturer datasheet
(a permanent archive that survives dead vendor links) and stores user file attachments.
Until `DATASHEET_LIBRARY_DRIVE_ID` is set, datasheet storage is **skipped entirely** and
user attachments fall back to the uploading user's personal OneDrive — both of which die
with that user's account. This procedure creates a dedicated SharePoint site that the
company owns forever, and grants the app **least-privilege** access to it: the app's Azure
registration gets `Sites.Selected`, scoped to this one site only — it can never touch any
other SharePoint content. Users never need access to the site itself: the app serves every
file through an in-app proxy using its own app token, and the app exposes **no delete
endpoint for stored datasheets** (the archive is append-only from the app's side).

**Token model (what the code actually does):** the app uses an **application permission /
client-credentials token** — `app/services/graph_app_auth.py::get_app_graph_token()`
posts `grant_type=client_credentials` with scope `https://graph.microsoft.com/.default`
using `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID` (already set in
`.env` — never print these values). No user signs in for this path, so the grant in
Step B must be the **Application** flavor of `Sites.Selected`, not Delegated.

With the drive id configured, the app makes exactly these Graph calls against the site:

| Call | Purpose |
|---|---|
| `PUT /v1.0/drives/{drive-id}/root:/Datasheets/{Manufacturer}/{MPN}-datasheet.pdf:/content` | store captured datasheets |
| `PUT /v1.0/drives/{drive-id}/root:/Attachments/{Entity}/{id}/{file}:/content` | store user attachments |
| `GET /v1.0/drives/{drive-id}/items/{item-id}/content` | serve files via the in-app proxy |
| `DELETE /v1.0/drives/{drive-id}/items/{item-id}` | only when a user deletes their own req/offer/company attachment |

---

## 2. Step A — Create the SharePoint site

1. Go to the **SharePoint admin center**: <https://admin.microsoft.com> → **Show all** →
   **SharePoint** (or directly `https://<tenant>-admin.sharepoint.com`).
2. **Sites → Active sites → Create → Team site**.
3. Name: **AvailAI Datasheet Library**. Adjust the site address if offered — e.g.
   `.../sites/AvailAIDatasheetLibrary`. Note this full URL; you need it in Steps B and C.
4. Privacy: **Private**. Owner: yourself (the admin). **Do not add any members** — the
   app is the only reader/writer; users get files through the app, never from SharePoint.
5. Finish. The site's default **Documents** library is the one the app will use — no
   library configuration needed (the app creates its `Datasheets/` and `Attachments/`
   folders on first upload).

---

## 3. Step B — Grant the app Sites.Selected on just that site

### B.0 — Ensure the app registration has the Sites.Selected application permission

1. **Entra admin center** (<https://entra.microsoft.com>) → **Identity → Applications →
   App registrations** → select the AvailAI app (the one whose Application (client) ID
   matches `AZURE_CLIENT_ID` in `/root/availai/.env`).
2. **API permissions → Add a permission → Microsoft Graph → Application permissions** →
   search **Sites.Selected** → check it → **Add permissions**.
3. Click **Grant admin consent for `<tenant>`** and confirm. The Status column must show
   a green check for Sites.Selected (Application).

`Sites.Selected` grants access to **zero** sites until a per-site grant is added. Do that
with either path below (they are equivalent — pick one).

### B.1 — Path 1: PowerShell (PnP.PowerShell)

```powershell
Install-Module PnP.PowerShell -Scope CurrentUser

# Connect to the ADMIN site (not the new site), signing in as a SharePoint admin.
Connect-PnPOnline "https://<tenant>-admin.sharepoint.com" -Interactive

Grant-PnPAzureADAppSitePermission `
  -AppId "<AZURE_CLIENT_ID from /root/availai/.env>" `
  -DisplayName "AvailAI" `
  -Site "https://<tenant>.sharepoint.com/sites/AvailAIDatasheetLibrary" `
  -Permissions Write
```

Note: PnP.PowerShell 2.12+ requires `-ClientId <your own PnP app id>` on
`Connect-PnPOnline -Interactive` (run `Register-PnPEntraIDAppForInteractiveLogin` once if
you don't have one). If you'd rather not set that up, use Path 2.

### B.2 — Path 2: Graph Explorer / plain HTTP (no PowerShell)

Open **Graph Explorer** (<https://developer.microsoft.com/graph/graph-explorer>) signed
in as an admin. Consent to the delegated `Sites.FullControl.All` permission when prompted
(required to manage site permissions; this consents Graph Explorer, not the AvailAI app).

1. Resolve the site id:

   ```http
   GET https://graph.microsoft.com/v1.0/sites/<tenant>.sharepoint.com:/sites/AvailAIDatasheetLibrary
   ```

   The `id` in the response looks like
   `contoso.sharepoint.com,1a2b3c4d-...-guid,5e6f7a8b-...-guid` — use the whole
   three-part string.

2. Grant the app write on the site:

   ```http
   POST https://graph.microsoft.com/v1.0/sites/{site-id}/permissions
   Content-Type: application/json

   {
     "roles": ["write"],
     "grantedToIdentities": [
       { "application": { "id": "<AZURE_CLIENT_ID>", "displayName": "AvailAI" } }
     ]
   }
   ```

   A `201 Created` response means the grant is in place.

---

## 4. Step C — Get the drive id

Still in Graph Explorer (or any HTTP client with an admin token):

1. Resolve the site id (same call as B.2 step 1, skip if you already have it):

   ```http
   GET https://graph.microsoft.com/v1.0/sites/<tenant>.sharepoint.com:/sites/AvailAIDatasheetLibrary
   ```

2. List the site's document libraries:

   ```http
   GET https://graph.microsoft.com/v1.0/sites/{site-id}/drives
   ```

3. In the response, find the entry with `"name": "Documents"` (the site's default
   library — or a purpose-created library if you made one) and copy its `id`. It looks
   like:

   ```text
   b!x0f9kL2mN3pQr4sT5uV6wXyZ7aB8cD9eF0gH1iJ2kL3mN4oP5qR6sT7uV8wXyZ
   ```

   (always starts with `b!`, ~60-70 URL-safe base64 characters). That string is the
   value for `DATASHEET_LIBRARY_DRIVE_ID`.

---

## 5. Step D — Configure, restart, verify

1. Edit `/root/availai/.env` and set:

   ```bash
   DATASHEET_LIBRARY_DRIVE_ID=b!x0f9...   # the id from Step C
   # DATASHEET_LIBRARY_SUBPATH=Datasheets  # optional; default is fine
   ```

2. Recreate the containers so they pick up the new env (`docker compose restart` does
   NOT reload `.env` — use `up -d`):

   ```bash
   cd /root/availai && docker compose up -d app enrichment-worker
   ```

   (or run a normal `./deploy.sh` from `main` if you're deploying anyway).

3. **Verify in the app.** Trigger a capture — open a Part Dossier for an MPN or add a
   requirement — then watch the logs:

   ```bash
   docker compose logs -f app | grep -i datasheet
   ```

   - Success: `datasheet captured mpn=<MPN> source=connector` (or `source=web`)
     — from `app/services/datasheet_capture.py`.
   - Must **no longer** appear: `datasheet library not configured — skipping storage`
     — that's the unconfigured path in `app/services/datasheet_library.py`.
   - Failure signatures (see §6): `no app Graph token — skipping datasheet storage`,
     `datasheet library upload failed <status> <body>`,
     `app-only Graph token failed: <status> <body>`.

4. **Verify in Graph** — list the library with the app's own token (end-to-end proof the
   Sites.Selected grant works; prints no secrets):

   ```bash
   cd /root/availai && set -a && . ./.env && set +a
   TOKEN=$(curl -s -X POST "https://login.microsoftonline.com/${AZURE_TENANT_ID}/oauth2/v2.0/token" \
     -d "client_id=${AZURE_CLIENT_ID}" -d "client_secret=${AZURE_CLIENT_SECRET}" \
     -d "grant_type=client_credentials" -d "scope=https://graph.microsoft.com/.default" \
     | jq -r .access_token)
   curl -s "https://graph.microsoft.com/v1.0/drives/${DATASHEET_LIBRARY_DRIVE_ID}/root:/Datasheets:/children?\$select=name,size" \
     -H "Authorization: Bearer $TOKEN" | jq .
   ```

   Expect one folder per manufacturer containing `<MPN>-datasheet.pdf` files. (A `404
   itemNotFound` for `Datasheets` before the very first capture is normal — the folder is
   created on first upload; list `/root/children` instead to confirm access.)

---

## 6. Troubleshooting

| Symptom (log line / response) | Cause | Fix |
|---|---|---|
| `datasheet library upload failed 403 ...` or Graph `403 accessDenied` | Sites.Selected app permission not admin-consented, or the per-site grant is missing / on the wrong site | Redo Step B; confirm the grant with `GET /v1.0/sites/{site-id}/permissions` |
| `datasheet library upload failed 404 ...` or Graph `404 itemNotFound` on the drive | Wrong `DATASHEET_LIBRARY_DRIVE_ID` | Redo Step C; copy the full `b!...` id of the intended library |
| `datasheet library not configured — skipping storage` still logged after Step D | Container still running the old env | `docker compose up -d app enrichment-worker` (recreate — `restart` doesn't reload `.env`) |
| Attachments still landing in the uploader's OneDrive | Same as above — drive id empty at runtime | Same fix; confirm with `docker compose exec app env \| grep DATASHEET` |
| `no app Graph token — skipping datasheet storage` | `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID` missing or wrong in `.env` | Check the three vars are set (values must match the app registration) |
| `app-only Graph token failed: 401 ...` (`invalid_client`) | Client secret expired or rotated | Create a new client secret on the app registration; update `AZURE_CLIENT_SECRET`; recreate containers |

## One-time after go-live: clear the capture negative-cache

Every MPN whose datasheet capture ran while `DATASHEET_LIBRARY_DRIVE_ID` was unset was
stamped `datasheet_searched_at` and will not re-attempt capture for 30 days
(`CAPTURE_COOLDOWN_DAYS`, `app/services/datasheet_capture.py`). To make previously-viewed
parts pick up their datasheet immediately after the library goes live, clear the stamps
for parts that have no captured datasheet yet (safe — it only re-enables the attempt):

```bash
docker compose exec db psql -U availai -d availai -c \
  "UPDATE material_cards SET datasheet_searched_at = NULL \
   WHERE datasheet_searched_at IS NOT NULL AND datasheet_url IS NULL;"
```

Fresh MPNs (never viewed before) need no reset and will capture on first sight.
