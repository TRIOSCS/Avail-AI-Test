"""routers/trouble_report.py — Floating bug report button that files GitHub Issues.

Provides a GET endpoint for the trouble report form partial and a POST endpoint
that creates a GitHub Issue via the `gh` CLI. The feature is gated on whether
the `gh` CLI is available on the server.

Called by: main.py (router mount), base.html (HTMX button)
Depends on: dependencies (require_user), config (settings), gh CLI
"""

import asyncio
import shutil
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger

from ..config import APP_VERSION, settings
from ..dependencies import require_user
from ..models import User

router = APIRouter(prefix="/api/trouble-report", tags=["trouble-report"])
templates = Jinja2Templates(directory="app/templates")


def _gh_available() -> bool:
    """Check if the gh CLI is installed and on PATH."""
    return shutil.which("gh") is not None


@router.get("/form", response_class=HTMLResponse)
async def trouble_report_form(request: Request, user: User = Depends(require_user)):
    """Return the trouble report form partial for the modal."""
    return templates.TemplateResponse(
        "htmx/partials/shared/trouble_report_form.html",
        {"request": request, "user_name": user.name, "user_email": user.email},
    )


@router.post("", response_class=HTMLResponse)
async def submit_trouble_report(
    request: Request,
    description: str = Form(...),
    page_url: str = Form(""),
    user_agent: str = Form(""),
    viewport: str = Form(""),
    error_log: str = Form(""),
    user: User = Depends(require_user),
):
    """Validate and file a GitHub Issue via `gh issue create`."""
    # Validate description
    desc = description.strip()
    if not desc or len(desc) < 10:
        return HTMLResponse(
            '<div class="p-4 text-rose-600 text-sm">Please provide a description of at least 10 characters.</div>',
            status_code=422,
        )

    if not _gh_available():
        logger.error("gh CLI not found — cannot file trouble report")
        return HTMLResponse(
            '<div class="p-4 text-rose-600 text-sm">'
            "Bug reporting is temporarily unavailable. Please contact support directly."
            "</div>",
            status_code=503,
        )

    # Build markdown body
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    body_parts = [
        "## Trouble Report",
        "",
        f"**Reporter:** {user.name} ({user.email})",
        f"**Date:** {now}",
        f"**App Version:** {APP_VERSION}",
        "",
        "### Description",
        "",
        desc,
        "",
        "### Context",
        "",
        f"- **Page URL:** {page_url or 'N/A'}",
        f"- **User Agent:** {user_agent or 'N/A'}",
        f"- **Viewport:** {viewport or 'N/A'}",
    ]

    if error_log and error_log.strip() and error_log.strip() != "[]":
        body_parts.extend(
            [
                "",
                "### Recent JS Errors",
                "",
                "```json",
                error_log.strip(),
                "```",
            ]
        )

    body = "\n".join(body_parts)

    # Build title
    title = f"[Trouble] {desc[:80]}"
    if len(desc) > 80:
        title += "..."

    repo = settings.github_trouble_report_repo

    try:
        proc = await asyncio.create_subprocess_exec(
            "gh",
            "issue",
            "create",
            "--repo",
            repo,
            "--title",
            title,
            "--body",
            body,
            "--label",
            "bug",
            "--label",
            "trouble-report",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if proc.returncode != 0:
            err_msg = stderr.decode().strip() if stderr else "Unknown error"
            logger.error("gh issue create failed (rc={}): {}", proc.returncode, err_msg)
            return HTMLResponse(
                '<div class="p-4 text-rose-600 text-sm">Failed to file the report. Please try again later.</div>',
                status_code=500,
            )

        issue_url = stdout.decode().strip()
        logger.info("Trouble report filed: {} by {}", issue_url, user.email)

        return HTMLResponse(
            '<div class="p-4 text-center">'
            '<div class="text-emerald-600 font-medium mb-2">Report submitted successfully!</div>'
            f'<a href="{issue_url}" target="_blank" rel="noopener" '
            'class="text-brand-600 underline text-sm">View issue on GitHub</a>'
            '<div class="mt-3">'
            '<button type="button" @click="$dispatch(\'close-modal\')" '
            'class="px-4 py-2 text-sm text-gray-600 hover:text-gray-800">Close</button>'
            "</div></div>"
        )

    except asyncio.TimeoutError:
        logger.error("gh issue create timed out")
        return HTMLResponse(
            '<div class="p-4 text-rose-600 text-sm">Request timed out. Please try again.</div>',
            status_code=504,
        )
    except Exception as e:
        logger.error("Trouble report error: {}", str(e))
        return HTMLResponse(
            '<div class="p-4 text-rose-600 text-sm">An unexpected error occurred. Please try again.</div>',
            status_code=500,
        )
