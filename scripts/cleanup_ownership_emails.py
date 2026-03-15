"""Cleanup script — delete ownership warning emails from all users' inboxes.

Searches for emails with subject containing "[AVAIL]" and "days left on"
or "Account Health Digest", then deletes them via Graph API.

Usage (inside Docker):
    docker compose exec app python -m scripts.cleanup_ownership_emails

Or standalone:
    python scripts/cleanup_ownership_emails.py
"""

import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    from loguru import logger
    from sqlalchemy.orm import Session

    from app.database import SessionLocal
    from app.models import User
    from app.utils.graph_client import GraphClient
    from app.utils.token_manager import get_valid_token

    db: Session = SessionLocal()
    try:
        # Get all users with tokens
        users = db.query(User).filter(User.access_token.isnot(None)).all()
        logger.info(f"Found {len(users)} users with tokens")

        total_deleted = 0

        for user in users:
            token = await get_valid_token(user, db)
            if not token:
                logger.warning(f"No valid token for {user.email}, skipping")
                continue

            gc = GraphClient(token)
            user_deleted = 0

            # Search patterns for ownership alert emails
            search_filters = [
                "subject:'[AVAIL]' AND subject:'days left on'",
                "subject:'[AVAIL]' AND subject:'Account Health Digest'",
                "subject:'[AVAIL]' AND subject:'Ownership Warning'",
            ]

            for search_query in search_filters:
                try:
                    # Search inbox + sent items
                    messages = await gc.get_all_pages(
                        "/me/messages",
                        params={
                            "$search": f'"{search_query}"',
                            "$select": "id,subject,receivedDateTime",
                            "$top": "100",
                        },
                        max_items=5000,
                    )

                    if not messages:
                        continue

                    logger.info(
                        f"  {user.email}: found {len(messages)} emails matching '{search_query}'"
                    )

                    # Delete each message
                    for msg in messages:
                        msg_id = msg.get("id")
                        if not msg_id:
                            continue
                        try:
                            url = f"https://graph.microsoft.com/v1.0/me/messages/{msg_id}"
                            from app.http_client import http

                            resp = await http.delete(
                                url,
                                headers={
                                    "Authorization": f"Bearer {token}",
                                },
                                timeout=30,
                            )
                            if resp.status_code == 204:
                                user_deleted += 1
                            else:
                                logger.warning(
                                    f"  Failed to delete msg {msg_id}: {resp.status_code}"
                                )
                        except Exception as e:
                            logger.error(f"  Error deleting msg {msg_id}: {e}")

                except Exception as e:
                    logger.error(f"  Search failed for {user.email}: {e}")

            if user_deleted:
                logger.info(f"  {user.email}: deleted {user_deleted} emails")
                total_deleted += user_deleted

        db.commit()
        logger.info(f"\nDone! Deleted {total_deleted} ownership alert emails total.")

    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
