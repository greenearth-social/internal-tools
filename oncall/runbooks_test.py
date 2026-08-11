from unittest.mock import patch, MagicMock
from runbooks import policy_name_to_slug, fetch_runbook

SAMPLE_MD = """\
---
alert_id: es-storage-high
severity: warning
---

## ES Storage > 80%

**Likely cause:** Post volume spike.

**Steps:**
1. Check storage.
"""


def test_policy_name_to_slug_basic():
    assert policy_name_to_slug("ES Storage > 80%") == "es-storage-80"


def test_policy_name_to_slug_spaces_to_hyphens():
    assert policy_name_to_slug("Ingest Freshness") == "ingest-freshness"


def test_policy_name_to_slug_strips_leading_trailing_hyphens():
    assert policy_name_to_slug("  feeds degraded  ") == "feeds-degraded"


def test_fetch_runbook_returns_body_when_found():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = SAMPLE_MD
    with patch("runbooks.httpx.get", return_value=mock_response):
        found, content = fetch_runbook("ES Storage > 80%", "main")
    assert found is True
    assert "## ES Storage > 80%" in content
    assert "alert_id" not in content  # frontmatter stripped


def test_fetch_runbook_returns_false_on_404():
    mock_response = MagicMock()
    mock_response.status_code = 404
    with patch("runbooks.httpx.get", return_value=mock_response):
        found, content = fetch_runbook("Unknown Alert", "main")
    assert found is False
    assert content == ""


def test_fetch_runbook_returns_false_on_network_error():
    with patch("runbooks.httpx.get", side_effect=Exception("network error")):
        found, content = fetch_runbook("ES Storage > 80%", "main")
    assert found is False
    assert content == ""
