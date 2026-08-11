#!/usr/bin/env python3
"""
Register Discord slash commands for the oncall bot.

Run once after initial deploy, or whenever commands change:
  GE_DISCORD_APPLICATION_ID=... GE_DISCORD_BOT_TOKEN=... GE_DISCORD_GUILD_ID=... python scripts/register_commands.py

Uses guild commands (instant) rather than global commands (up to 1h propagation).
Set GE_DISCORD_GUILD_ID to your server ID.
"""
import os
import httpx

APPLICATION_ID = os.environ["GE_DISCORD_APPLICATION_ID"]
BOT_TOKEN = os.environ["GE_DISCORD_BOT_TOKEN"]
GUILD_ID = os.environ["GE_DISCORD_GUILD_ID"]

COMMANDS = [
    {
        "name": "register",
        "description": "Link your Discord account to the oncall system",
        "type": 1,
    },
    {
        "name": "oncall",
        "description": "Manage oncall schedule",
        "type": 1,
        "options": [
            {
                "name": "set",
                "description": "Set who is on call",
                "type": 1,
                "options": [
                    {"name": "user", "description": "User to set as oncall", "type": 6, "required": True},
                    {"name": "until", "description": "Until date (YYYY-MM-DD), defaults to end of week", "type": 3, "required": False},
                ],
            },
            {"name": "who", "description": "Show who is currently on call", "type": 1},
        ],
    },
    {
        "name": "ack",
        "description": "Acknowledge a critical alert",
        "type": 1,
        "options": [{"name": "alert_id", "description": "Alert ID to acknowledge", "type": 3, "required": True}],
    },
    {
        "name": "resolve",
        "description": "Mark an alert as resolved",
        "type": 1,
        "options": [{"name": "alert_id", "description": "Alert ID to resolve", "type": 3, "required": True}],
    },
    {
        "name": "runbook",
        "description": "Manage runbooks",
        "type": 1,
        "options": [{"name": "add", "description": "Add a new runbook via GitHub PR", "type": 1}],
    },
]


def main():
    url = f"https://discord.com/api/v10/applications/{APPLICATION_ID}/guilds/{GUILD_ID}/commands"
    headers = {"Authorization": f"Bot {BOT_TOKEN}"}
    resp = httpx.put(url, headers=headers, json=COMMANDS)
    resp.raise_for_status()
    registered = resp.json()
    print(f"Registered {len(registered)} commands:")
    for cmd in registered:
        print(f"  /{cmd['name']}")


if __name__ == "__main__":
    main()
