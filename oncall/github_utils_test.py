from unittest.mock import MagicMock, patch

from github_utils import (
    add_pr_to_project,
    create_runbook_pr,
    request_pr_reviewers,
    set_project_item_status,
)

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
    """Sequence: GET (sha) -> POST (branch) -> PUT (file) -> POST (PR)."""
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get.return_value = sha_resp
    client.post.side_effect = [branch_resp, pr_resp]
    client.put.return_value = file_resp
    return client


def _pr_response(number: int = 42, node_id: str = "PR_kwDO_abc") -> dict:
    return {
        "html_url": f"{PR_URL_BASE}/{number}",
        "node_id": node_id,
        "number": number,
    }


def test_create_runbook_pr_returns_url_node_id_and_number():
    sha_resp = _make_response({"object": {"sha": "abc123"}})
    branch_resp = _make_response({}, 201)
    file_resp = _make_response({}, 201)
    pr_resp = _make_response(_pr_response(number=42, node_id="PR_kwDO_abc"), 201)

    mock_client = _mock_httpx_client(sha_resp, branch_resp, file_resp, pr_resp)
    with patch("github_utils.httpx.Client", return_value=mock_client):
        result = create_runbook_pr(
            "ghp_token", "ES Storage > 80%", "ES Storage High", "## Steps\n1. Fix it."
        )

    assert result == {
        "html_url": f"{PR_URL_BASE}/42",
        "node_id": "PR_kwDO_abc",
        "number": 42,
    }


def test_create_runbook_pr_slugifies_policy_name():
    sha_resp = _make_response({"object": {"sha": "def456"}})
    branch_resp = _make_response({}, 201)
    file_resp = _make_response({}, 201)
    pr_resp = _make_response(_pr_response(number=99), 201)

    mock_client = _mock_httpx_client(sha_resp, branch_resp, file_resp, pr_resp)
    with patch("github_utils.httpx.Client", return_value=mock_client):
        create_runbook_pr(
            "ghp_token", "ES Storage > 80%", "ES Storage High", "## Steps\n1. Fix it."
        )

    branch_call = mock_client.post.call_args_list[0]
    branch_json = branch_call[1]["json"]
    assert branch_json["ref"] == "refs/heads/runbook/es-storage-80"

    file_call_args = mock_client.put.call_args
    assert "oncall/runbooks/es-storage-80.md" in file_call_args[0][0]


def test_create_runbook_pr_file_content_format():
    sha_resp = _make_response({"object": {"sha": "abc123"}})
    branch_resp = _make_response({}, 201)
    file_resp = _make_response({}, 201)
    pr_resp = _make_response(_pr_response(number=1), 201)

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
    pr_resp = _make_response(_pr_response(number=5), 201)

    mock_client = _mock_httpx_client(sha_resp, branch_resp, file_resp, pr_resp)
    with patch("github_utils.httpx.Client", return_value=mock_client):
        create_runbook_pr("ghp_token", "alert-name", "Alert Title", "Content.")

    assert mock_client.get.call_count == 1
    assert mock_client.post.call_count == 2
    assert mock_client.put.call_count == 1

    get_call = mock_client.get.call_args[0][0]
    assert f"/repos/{REPO}/git/refs/heads/main" in get_call

    post1_url = mock_client.post.call_args_list[0][0][0]
    assert f"/repos/{REPO}/git/refs" in post1_url

    put_url = mock_client.put.call_args[0][0]
    assert f"/repos/{REPO}/contents/" in put_url

    post2_url = mock_client.post.call_args_list[1][0][0]
    assert f"/repos/{REPO}/pulls" in post2_url


def test_request_pr_reviewers_posts_to_github():
    with patch("github_utils.httpx.post", return_value=_make_response({}, 201)) as mock_post:
        request_pr_reviewers("ghp_token", 42, ["ian-gh", "max-gh"])

    mock_post.assert_called_once()
    url = mock_post.call_args[0][0]
    assert url == f"{GITHUB_API}/repos/{REPO}/pulls/42/requested_reviewers"
    assert mock_post.call_args[1]["json"] == {"reviewers": ["ian-gh", "max-gh"]}


def test_request_pr_reviewers_no_op_on_empty_list():
    with patch("github_utils.httpx.post") as mock_post:
        request_pr_reviewers("ghp_token", 42, [])
    mock_post.assert_not_called()


def test_add_pr_to_project_returns_item_id():
    graphql_resp = _make_response(
        {"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_lADO_new"}}}}
    )
    with patch("github_utils.httpx.post", return_value=graphql_resp) as mock_post:
        item_id = add_pr_to_project("ghp_token", "PVT_kwDO_proj", "PR_kwDO_abc")

    assert item_id == "PVTI_lADO_new"
    mock_post.assert_called_once()
    body = mock_post.call_args[1]["json"]
    assert "addProjectV2ItemById" in body["query"]
    assert body["variables"] == {"projectId": "PVT_kwDO_proj", "contentId": "PR_kwDO_abc"}


def test_add_pr_to_project_raises_on_graphql_errors():
    graphql_resp = _make_response(
        {"errors": [{"message": "Project not found"}], "data": None}
    )
    with patch("github_utils.httpx.post", return_value=graphql_resp):
        try:
            add_pr_to_project("ghp_token", "PVT_bad", "PR_bad")
        except RuntimeError as e:
            assert "GraphQL errors" in str(e)
        else:
            raise AssertionError("Expected RuntimeError")


def test_set_project_item_status_sends_mutation():
    graphql_resp = _make_response(
        {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_lADO_new"}}}}
    )
    with patch("github_utils.httpx.post", return_value=graphql_resp) as mock_post:
        set_project_item_status(
            "ghp_token", "PVT_kwDO_proj", "PVTI_lADO_new", "PVTSSF_lADO_status", "260c4616"
        )

    body = mock_post.call_args[1]["json"]
    assert "updateProjectV2ItemFieldValue" in body["query"]
    assert body["variables"] == {
        "projectId": "PVT_kwDO_proj",
        "itemId": "PVTI_lADO_new",
        "fieldId": "PVTSSF_lADO_status",
        "optionId": "260c4616",
    }
