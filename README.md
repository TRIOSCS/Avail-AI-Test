# AvailAI — Supplier Sourcing Engine

Search for electronic components across Octopart + BrokerBin, score and rank vendors,
send RFQ emails, and automatically track replies with AI-powered quote extraction.

**Total cost:** ~$18/month (one DigitalOcean server)

---

## What You Need Before Starting

1. **A DigitalOcean account** — sign up at digitalocean.com
2. **A domain name** — e.g. `app.yourdomain.com` (needed for HTTPS / Microsoft login)
3. **A Microsoft 365 account** — the one you send RFQs from
4. **An Anthropic API key** — for AI-parsing vendor reply emails (get one at console.anthropic.com)
5. **Octopart API key** (optional) — from octopart.com/api
6. **BrokerBin API key** (optional) — from brokerbin.com

---

## Setup: Step by Step

### Step 1 — Create Your Server

1. Log into DigitalOcean → **Create** → **Droplets**
2. Pick these settings:
   - **Region:** New York (or closest to you)
   - **Image:** Ubuntu 24.04
   - **Size:** Basic → Regular → **$12/mo** (2 GB RAM, 1 CPU) — this is plenty
   - **Authentication:** Password (pick a strong one) or SSH key
3. Click **Create Droplet**
4. Copy the IP address it gives you (e.g. `143.198.xxx.xxx`)

### Step 2 — Point Your Domain

Go to your domain registrar (GoDaddy, Namecheap, Cloudflare, etc.) and add an **A record**:

| Type | Name              | Value              |
|------|-------------------|--------------------|
| A    | app (or whatever) | 143.198.xxx.xxx    |

Wait a few minutes for DNS to propagate. You can check with: `ping app.yourdomain.com`

### Step 3 — Register Your Microsoft App

This lets AvailAI send emails and read replies through your Outlook account.

1. Go to **https://portal.azure.com** → search for **App registrations** → **New registration**
2. Fill in:
   - **Name:** AvailAI
   - **Supported account types:** Single tenant
   - **Redirect URI:** Web → `https://app.yourdomain.com/auth/callback`
3. Click **Register**
4. On the app page, copy these (you'll need them later):
   - **Application (client) ID**
   - **Directory (tenant) ID**
5. Go to **Certificates & secrets** → **New client secret** → copy the **Value** (save it now, you can't see it again)
6. Go to **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated permissions** → add:
   - `User.Read`
   - `Mail.Send`
   - `Mail.Read`
7. Click **Grant admin consent** (the green checkmark button)

### Step 4 — Connect to Your Server and Install

Open a terminal (Mac: Terminal app, Windows: PowerShell) and connect:

```bash
ssh root@143.198.xxx.xxx
```

Type `yes` when asked, then enter your password.

Now run these commands one at a time:

```bash
# Download and run the setup script
apt update && apt install -y curl git

# Clone the project (or upload the zip)
cd /root
git clone https://github.com/YOUR_REPO/availai.git
# OR: upload the zip and unzip it
cd availai

# Run the setup script — this installs Docker and everything else
bash scripts/setup.sh
```

### Step 5 — Configure Your Settings

```bash
nano .env
```

Fill in your actual values (the file has instructions for each one).
When done: press **Ctrl+X**, then **Y**, then **Enter** to save.

**Important:** Also update the domain in the Caddy config:

```bash
nano Caddyfile
```

Change `app.yourdomain.com` to your actual domain.

### Step 6 — Launch It

```bash
docker compose up -d
```

That's it. Wait about 30 seconds, then open `https://app.yourdomain.com` in your browser.

You should see the AvailAI login screen. Click **Sign in with Microsoft**.

### Step 7 — Verify Everything Works

1. **Login works** — you can sign in with Microsoft
2. **Search works** — type a part number and hit Search
3. **Upload works** — drag a vendor stock list CSV/Excel onto the Upload tab
4. **Send RFQ** — search, select vendors, click Send RFQ
5. **Check Inbox** — go to Responses tab, click "Check Inbox" after getting replies

---

## Day-to-Day Usage

### Searching
Type part numbers (comma-separated or one per line), optionally set a target quantity, hit Search.
Results are ranked by a 6-factor score: recency, quantity, vendor reliability, data completeness,
source credibility, and price.

### Uploading Stock Lists
Drag and drop CSV/Excel files from vendors. The system auto-detects columns
(part number, qty, price, etc.) and stores everything for future searches.

### Sending RFQs
After searching, check the vendors you want to contact, click "Send RFQ".
Preview the email, edit if needed, click Send. Emails go out through your Outlook.

### Tracking Replies
Go to the **Responses** tab and click **Check Inbox**. The system:
1. Scans your Outlook inbox for replies to RFQs you sent
2. AI reads each reply and extracts: price, quantity, lead time, condition, date code
3. High-confidence quotes auto-create sightings (show up in future searches)
4. Lower-confidence quotes show up for you to approve or reject
5. Vendor reliability scores update automatically based on who replies and how fast

The system also checks automatically every 5 minutes in the background.

---

## Common Tasks

### Restarting the app
```bash
cd /root/availai
docker compose restart
```

### Viewing logs
```bash
docker compose logs -f app
```

### Updating the code
```bash
cd /root/availai
git pull
docker compose up -d --build
```

### Stopping everything
```bash
docker compose down
```

### Checking if it's running
```bash
docker compose ps
```

---

## Troubleshooting

**"Cannot connect" in browser**
→ Make sure your domain DNS is pointing to the server IP. Run `docker compose ps` to check containers are running.

**Microsoft login fails**
→ Double-check your redirect URI in Azure matches exactly: `https://app.yourdomain.com/auth/callback`
→ Make sure you granted admin consent on the API permissions.

**Search returns no results**
→ Check your Octopart/BrokerBin API keys in `.env`. You can also upload vendor stock lists to build your database.

**"Check Inbox" finds nothing**
→ Replies must be in the same email thread as the RFQ you sent. Check that `Mail.Read` permission is granted in Azure.

**AI parsing gives low confidence**
→ This is normal for messy emails. You can manually approve quotes in the Responses tab.
