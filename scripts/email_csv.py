#!/usr/bin/env python3
"""Email a CSV file as an attachment via Microsoft Graph API.

Run inside Docker:
    docker compose exec app env PYTHONPATH=/app python scripts/email_csv.py \
        --to mkhoury@trioscs.com \
        --subject "Martina Tewes Account Export" \
        --file /tmp/export.csv

Depends on: a valid access_token in the users table (uses first admin user).
"""

import argparse
import asyncio
import base64
import os
import sys

from app.database import SessionLocal
from app.models.auth import User
from app.utils.graph_client import GraphClient


async def send_email(token: str, to: str, subject: str, body: str,
                     filename: str, file_bytes: bytes):
    gc = GraphClient(token)
    csv_b64 = base64.b64encode(file_bytes).decode("utf-8")

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": body},
            "toRecipients": [{"emailAddress": {"address": to}}],
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": filename,
                    "contentBytes": csv_b64,
                    "isInline": False,
                }
            ],
        },
        "saveToSentItems": "true",
    }

    result = await gc.post_json("/me/sendMail", payload)
    return result


def main():
    parser = argparse.ArgumentParser(description="Email a CSV attachment")
    parser.add_argument("--to", required=True, help="Recipient email")
    parser.add_argument("--subject", default="AvailAI Export", help="Email subject")
    parser.add_argument("--body", default="<p>Please see the attached export.</p>",
                        help="HTML body")
    parser.add_argument("--file", required=True, help="Path to CSV file")
    parser.add_argument("--sender-id", type=int, help="User ID to send as (default: first admin)")
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    db = SessionLocal()
    try:
        if args.sender_id:
            user = db.get(User, args.sender_id)
        else:
            admin_emails = os.getenv("ADMIN_EMAILS", "").split(",")
            user = db.query(User).filter(User.email.in_(admin_emails)).first()
            if not user:
                user = db.query(User).filter(User.access_token.isnot(None)).first()

        if not user or not user.access_token:
            print("No user with valid access token found", file=sys.stderr)
            sys.exit(1)

        print(f"Sending as: {user.name} ({user.email})", file=sys.stderr)

        with open(args.file, "rb") as f:
            file_bytes = f.read()

        filename = os.path.basename(args.file)
        result = asyncio.run(send_email(
            user.access_token, args.to, args.subject, args.body,
            filename, file_bytes,
        ))

        if result and "error" in result:
            print(f"Send failed: {result}", file=sys.stderr)
            sys.exit(1)

        print(f"Email sent to {args.to} with attachment {filename}", file=sys.stderr)

    finally:
        db.close()


if __name__ == "__main__":
    main()
