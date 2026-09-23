from datetime import datetime

import httpx
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

DISCORD_API_BASE = "https://discord.com/api/v10"


def verify_discord_request(public_key: str, signature: str, timestamp: str, body: bytes) -> bool:
    try:
        vk = VerifyKey(bytes.fromhex(public_key))
        vk.verify(timestamp.encode() + body, bytes.fromhex(signature))
        return True
    except (BadSignatureError, Exception):
        return False


def format_ts(dt: datetime) -> str:
    return f"<t:{int(dt.timestamp())}:f>"


def send_channel_message(channel_id: str, bot_token: str, content: str) -> None:
    response = httpx.post(
        f"{DISCORD_API_BASE}/channels/{channel_id}/messages",
        headers={"Authorization": bot_token},
        json={"content": content},
    )
    response.raise_for_status()


def edit_original_interaction_response(
    application_id: str, interaction_token: str, content: str
) -> None:
    """Edit the deferred response for a Discord interaction.

    Used when a handler returns type 5 (deferred) and later needs to post
    the actual result. The interaction token in the URL is the auth — no
    Authorization header is required.
    """
    response = httpx.patch(
        f"{DISCORD_API_BASE}/webhooks/{application_id}/{interaction_token}/messages/@original",
        json={"content": content},
    )
    response.raise_for_status()
