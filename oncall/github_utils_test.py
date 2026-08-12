from unittest.mock import MagicMock, patch

from github_utils import create_runbook_pr

GITHUB_API = "https://api.github.com"
REPO = "greenearth-social/internal-tools"
PR_URL_BASE = f"https://github.com/{REPO}/pull"


def _make_response(json_data: dict, status: int = 200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data
    r.raise_for_status = MagicMock()
    return r


def _mock_httpx_client(sha_resp, branch_resp, file_resp, pr_resp):
    """Sequence: GET (sha) → POST (branch) → PUT (file) → POST (PR)."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get.return_value = sha_resp
    client.post.side_effect = [branch_resp, pr_resp]
    client.put.return_value = file_resp
    return client


def test_create_runbook_pr_returns_url():
    sha_resp = _make_response({"object": {"sha": "abc123"}})
    branch_resp = _make_response({}, 201)
    file_resp = _make_response({}, 201)
    pr_resp = _make_response({"html_url": f"{PR_URL_BASE}/42"}, 201)

    mock_client = _mock_httpx_client(sha_resp, branch_resp, file_resp, pr_resp)
    with patch("github_utils.httpx.Client", return_value=mock_client):
        url = create_runbook_pr(
            "ghp_token", "ES Storage > 80%", "ES Storage High", "## Steps\n1. Fix it."
        )

    assert url == f"{PR_URL_BASE}/42"


def test_create_runbook_pr_slugifies_policy_name():
    sha_resp = _make_response({"object": {"sha": "def456"}})
    branch_resp = _make_response({}, 201)
    file_resp = _make_response({}, 201)
    pr_resp = _make_response({"html_url": f"{PR_URL_BASE}/99"}, 201)

    mock_client = _mock_httpx_client(sha_resp, branch_resp, file_resp, pr_resp)
    with patch("github_utils.httpx.Client", return_value=mock_client):
        create_runbook_pr(
            "ghp_token", "ES Storage > 80%", "ES Storage High", "## Steps\n1. Fix it."
        )

    # Verify branch was created with slugified name
    branch_call = mock_client.post.call_args_list[0]
    branch_json = branch_call[1]["json"]
    assert branch_json["ref"] == "refs/heads/runbook/es-storage-80"

    # Verify file was created at correct path
    file_call_args = mock_client.put.call_args
    assert "oncall/runbooks/es-storage-80.md" in file_call_args[0][0]


def test_create_runbook_pr_file_content_format():
    sha_resp = _make_response({"object": {"sha": "abc123"}})
    branch_resp = _make_response({}, 201)
    file_resp = _make_response({}, 201)
    pr_resp = _make_response({"html_url": f"{PR_URL_BASE}/1"}, 201)

    mock_client = _mock_httpx_client(sha_resp, branch_resp, file_resp, pr_resp)
    with patch("github_utils.httpx.Client", return_value=mock_client):
        import base64

        create_runbook_pr("ghp_token", "my-alert", "My Alert Title", "Fix steps here.")

    file_call_json = mock_client.put.call_args[1]["json"]
    decoded = base64.b64decode(file_call_json["content"]).decode()
    assert decoded == "---\nalert_id: my-alert\n---\n\n# My Alert Title\n\nFix steps here.\n"


def test_create_runbook_pr_call_sequence():
    sha_resp = _make_response({"object": {"sha": "abc123"}})
    branch_resp = _make_response({}, 201)
    file_resp = _make_response({}, 201)
    pr_resp = _make_response({"html_url": f"{PR_URL_BASE}/5"}, 201)

    mock_client = _mock_httpx_client(sha_resp, branch_resp, file_resp, pr_resp)
    with patch("github_utils.httpx.Client", return_value=mock_client):
        create_runbook_pr("ghp_token", "alert-name", "Alert Title", "Content.")

    # Verify call order: GET sha → POST branch → PUT file → POST PR
    assert mock_client.get.call_count == 1
    assert mock_client.post.call_count == 2
    assert mock_client.put.call_count == 1

    # GET: main branch ref
    get_call = mock_client.get.call_args[0][0]
    assert f"/repos/{REPO}/git/refs/heads/main" in get_call

    # POST 1: create branch
    post1_url = mock_client.post.call_args_list[0][0][0]
    assert f"/repos/{REPO}/git/refs" in post1_url

    # PUT: create file
    put_url = mock_client.put.call_args[0][0]
    assert f"/repos/{REPO}/contents/" in put_url

    # POST 2: create PR
    post2_url = mock_client.post.call_args_list[1][0][0]
    assert f"/repos/{REPO}/pulls" in post2_url
