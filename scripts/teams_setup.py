"""One-time Teams channel discovery and configuration script.

Run inside Docker: docker compose exec -T app python scripts/teams_setup.py
"""

import asyncio
import os
import sys

sys.path.insert(0, "/app")
os.environ.setdefault("TESTING", "")

from app.database import SessionLocal
from app.models import User
from app.scheduler import get_valid_token
from app.utils.graph_client import GraphClient


async def main():
    db = SessionLocal()
    try:
        # Get admin user with valid token
        admin = db.query(User).filter(User.id == 1).first()
        if not admin:
            print("ERROR: Admin user not found")
            return

        token = await get_valid_token(admin, db)
        if not token:
            print("ERROR: Could not get valid Graph API token")
            return

        gc = GraphClient(token)

        # Discover teams
        print("\n=== Teams you have access to ===\n")
        teams_result = await gc.get_json("/me/joinedTeams", params={"$select": "id,displayName"})
        if "error" in teams_result:
            print(f"ERROR: {teams_result}")
            return

        teams_list = teams_result.get("value", [])
        if not teams_list:
            print("No Teams found. Make sure your account has Teams access.")
            return

        all_channels = []
        idx = 1
        for team in teams_list:
            channels_result = await gc.get_json(
                f"/teams/{team['id']}/channels",
                params={"$select": "id,displayName,membershipType"},
            )
            channels = channels_result.get("value", [])
            for ch in channels:
                all_channels.append(
                    {
                        "idx": idx,
                        "team_id": team["id"],
                        "team_name": team.get("displayName", ""),
                        "channel_id": ch["id"],
                        "channel_name": ch.get("displayName", ""),
                    }
                )
                print(f"  [{idx}] {team.get('displayName', '')} → #{ch.get('displayName', '')}")
                idx += 1

        if not all_channels:
            print("No channels found.")
            return

        print(f"\nFound {len(all_channels)} channel(s).")
        print("\nTo configure, pass the channel number as argument:")
        print("  docker compose exec -T app python scripts/teams_setup.py <number>")

        # If a channel number was provided, configure it
        if len(sys.argv) > 1:
            try:
                choice = int(sys.argv[1])
            except ValueError:
                print(f"Invalid number: {sys.argv[1]}")
                return

            selected = next((c for c in all_channels if c["idx"] == choice), None)
            if not selected:
                print(f"Invalid choice: {choice}")
                return

            print(f"\n=== Configuring: {selected['team_name']} → #{selected['channel_name']} ===\n")

            from app.models.config import SystemConfig

            def upsert(key, value):
                row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
                if row:
                    row.value = value
                else:
                    db.add(SystemConfig(key=key, value=value, updated_by=admin.email))
                print(f"  {key} = {value}")

            upsert("teams_team_id", selected["team_id"])
            upsert("teams_channel_id", selected["channel_id"])
            upsert("teams_channel_name", selected["channel_name"])
            upsert("teams_enabled", "true")
            upsert("teams_hot_threshold", "10000")
            db.commit()
            print("\nTeams configuration saved!")

            # Send test card
            print("\nSending test card...")
            from app.services.teams import _make_card, post_to_channel

            card = _make_card(
                title="AVAIL TEST",
                subtitle="Teams integration is working correctly.",
                facts=[
                    {"title": "Sent By", "value": admin.name or admin.email},
                    {"title": "Status", "value": "Connection verified"},
                ],
                action_url="",
                action_title="Open AVAIL",
                accent_color="accent",
            )
            ok = await post_to_channel(selected["team_id"], selected["channel_id"], card, token)
            if ok:
                print("Test card posted successfully!")
            else:
                print("WARNING: Failed to post test card. Check Graph API permissions.")

    finally:
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
