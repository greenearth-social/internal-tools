import base64

import httpx
from runbooks import policy_name_to_slug

GITHUB_API = "https://api.github.com"
GITHUB_GRAPHQL = "https://api.github.com/graphql"
REPO = "greenearth-social/internal-tools"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }


def create_runbook_pr(token: str, policy_name: str, title: str, content: str) -> dict:
    """Open a runbook PR against the internal-tools repo.

    Returns a dict with the PR's html_url, node_id, and number so callers
    can chain reviewer requests and Projects v2 mutations off the same PR.
    """
    slug = policy_name_to_slug(policy_name)
    branch = f"runbook/{slug}"
    file_path = f"oncall/runbooks/{slug}.md"
    file_content = f"---\nalert_id: {slug}\n---\n\n# {title}\n\n{content}\n"
    encoded = base64.b64encode(file_content.encode()).decode()

    with httpx.Client(headers=_headers(token), base_url=GITHUB_API) as client:
        ref_resp = client.get(f"/repos/{REPO}/git/refs/heads/main")
        ref_resp.raise_for_status()
        sha = ref_resp.json()["object"]["sha"]

        branch_resp = client.post(
            f"/repos/{REPO}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": sha},
        )
        branch_resp.raise_for_status()

        file_resp = client.put(
            f"/repos/{REPO}/contents/{file_path}",
            json={
                "message": f"docs(oncall): add runbook for {slug}",
                "content": encoded,
                "branch": branch,
            },
        )
        file_resp.raise_for_status()

        pr_resp = client.post(
            f"/repos/{REPO}/pulls",
            json={
                "title": f"runbook: add {slug}",
                "head": branch,
                "base": "main",
                "body": f"Adds runbook for `{slug}` captured after incident.",
            },
        )
        pr_resp.raise_for_status()
        pr_json = pr_resp.json()
        return {
            "html_url": pr_json["html_url"],
            "node_id": pr_json["node_id"],
            "number": pr_json["number"],
        }


def request_pr_reviewers(token: str, pr_number: int, reviewers: list[str]) -> None:
    """Request reviews from the given GitHub handles on a PR in the internal-tools repo."""
    if not reviewers:
        return
    resp = httpx.post(
        f"{GITHUB_API}/repos/{REPO}/pulls/{pr_number}/requested_reviewers",
        headers=_headers(token),
        json={"reviewers": reviewers},
    )
    resp.raise_for_status()


_ADD_ITEM_MUTATION = """
mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
    item { id }
  }
}
"""

_SET_STATUS_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId,
    itemId: $itemId,
    fieldId: $fieldId,
    value: {singleSelectOptionId: $optionId}
  }) {
    projectV2Item { id }
  }
}
"""


def _graphql(token: str, query: str, variables: dict) -> dict:
    resp = httpx.post(
        GITHUB_GRAPHQL,
        headers=_headers(token),
        json={"query": query, "variables": variables},
    )
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        raise RuntimeError(f"GraphQL errors: {body['errors']}")
    return body["data"]


def add_pr_to_project(token: str, project_id: str, pr_node_id: str) -> str:
    """Add a PR to a Projects v2 board. Returns the new item's ID."""
    data = _graphql(
        token,
        _ADD_ITEM_MUTATION,
        {"projectId": project_id, "contentId": pr_node_id},
    )
    return data["addProjectV2ItemById"]["item"]["id"]


def set_project_item_status(
    token: str, project_id: str, item_id: str, field_id: str, option_id: str
) -> None:
    """Set the Status single-select field on a Projects v2 item."""
    _graphql(
        token,
        _SET_STATUS_MUTATION,
        {
            "projectId": project_id,
            "itemId": item_id,
            "fieldId": field_id,
            "optionId": option_id,
        },
    )
