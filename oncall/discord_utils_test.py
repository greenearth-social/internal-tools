from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from discord_utils import verify_discord_request, format_ts, send_channel_message


def test_format_ts():
    dt = datetime(2026, 8, 15, 23, 59, 59, tzinfo=timezone.utc)
    result = format_ts(dt)
    assert result == f"<t:{int(dt.timestamp())}:f>"


def test_verify_discord_request_invalid_rejects():
    # Bad signature should return False
    result = verify_discord_request("a" * 64, "b" * 128, "12345", b"body")
    assert result is False


def test_send_channel_message_posts_to_discord():
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    with patch("discord_utils.httpx.post", return_value=mock_response) as mock_post:
        send_channel_message("chan123", "Bot token123", "hello")
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    assert "chan123" in call_kwargs.args[0]
    assert call_kwargs.kwargs["json"]["content"] == "hello"
