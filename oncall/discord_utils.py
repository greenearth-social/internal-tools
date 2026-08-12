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
