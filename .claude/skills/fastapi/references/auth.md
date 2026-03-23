# Auth Reference

## Contents
- Dependency Functions
- Permission Levels
- Agent API Key Auth
- Token Management
- OAuth Flow
- Anti-Patterns

## Dependency Functions

All auth dependencies live in `app/dependencies.py`. Import and use them — never re-implement auth logic in routers:

```python
from ..dependencies import require_user, require_buyer, require_admin, require_sales, require_fresh_token

# Tiers (each inherits the previous):
# require_user    → any logged-in, active user
# require_buyer   → buyer | sales | trader | manager | admin
# require_sales   → sales | trader | manager | admin
# require_admin   → admin only (blocks agent service account)
```

## Permission Levels

| Dependency | Roles Allowed | Use For |
|------------|--------------|---------|
| `require_user` | All roles | Read-only views, general access |
| `require_buyer` | buyer, sales, trader, manager, admin | RFQ actions, sourcing |
| `require_sales` | sales, trader, manager, admin | CRM, prospect management |
| `require_admin` | admin | User management, system config |
| `require_settings_access` | admin | Settings pages (blocks agent key) |
| `require_fresh_token` | Any with valid M365 token | Email operations via Graph API |

## Agent API Key Auth

Service-to-service calls use `x-agent-key` header instead of session cookies:

```python
# Client: set header
headers = {"x-agent-key": settings.agent_api_key}

# Server: handled automatically by require_user
# Agent key authenticates as the "agent@availai.local" service account
# require_admin and require_settings_access explicitly BLOCK the agent account
```

Never give the agent account admin privileges. It should only access non-privileged endpoints.

## Token Management

M365 tokens are stored in the DB (not just sessions) so background jobs can use them:

```python
# Use require_fresh_token for any endpoint that calls Microsoft Graph
@router.post("/api/rfq/{id}/send")
async def send_rfq(
    id: int,
    access_token: str = Depends(require_fresh_token),
    user: User = Depends(require_user),
    db: Session = Depends(get_db)
):
    # access_token is valid and refreshed if needed
    await send_via_graph_api(access_token, ...)
```

`require_fresh_token` refreshes proactively when within 15 minutes of expiry. If refresh fails, it raises HTTP 401.

## OAuth Flow

```
GET /auth/login
  → redirect to Azure AD authorize URL (with PKCE state)

GET /auth/callback?code=...&state=...
  → validate state (CSRF check via hmac.compare_digest)
  → exchange code for tokens via Azure AD
  → upsert User record (auto-create on first login)
  → store tokens in DB + session
  → redirect to /v2/requisitions

GET /auth/logout
  → clear session
  → redirect to /auth/login
```

State validation uses `hmac.compare_digest` — NEVER use `==` for secret comparison (timing attack).

## WARNING: Anti-Patterns

### Rolling Your Own Auth Check

```python
# BAD — re-implementing auth in a router
@router.get("/api/admin/users")
async def list_users(request: Request, db: Session = Depends(get_db)):
    uid = request.session.get("user_id")
    user = db.get(User, uid)
    if not user or user.role != "admin":
        raise HTTPException(403, "Not allowed")
```

**Why This Breaks:** Every router that does this has slightly different logic. Deactivated accounts still pass. Agent key is ignored. When auth rules change, you update 20 files instead of 1.

```python
# GOOD — always use the shared dependency
@router.get("/api/admin/users")
async def list_users(user: User = Depends(require_admin), db: Session = Depends(get_db)):
    ...
```

### String Comparison for Secrets

```python
# BAD — timing attack vulnerability
if api_key == settings.agent_api_key:
    ...

# GOOD — constant-time comparison
import hmac
if hmac.compare_digest(api_key, settings.agent_api_key):
    ...
```
```

---
