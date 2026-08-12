import base64

import httpx
from runbooks import policy_name_to_slug

GITHUB_API = "https://api.github.com"
REPO = "greenearth-social/internal-tools"


def create_runbook_pr(token: str, policy_name: str, title: str, content: str) -> str:
    slug = policy_name_to_slug(policy_name)
    branch = f"runbook/{slug}"
    file_path = f"oncall/runbooks/{slug}.md"
    file_content = f"---\nalert_id: {slug}\n---\n\n# {title}\n\n{content}\n"
    encoded = base64.b64encode(file_content.encode()).decode()
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
    }

    with httpx.Client(headers=headers, base_url=GITHUB_API) as client:
        # Get main branch SHA
        ref_resp = client.get(f"/repos/{REPO}/git/refs/heads/main")
        ref_resp.raise_for_status()
        sha = ref_resp.json()["object"]["sha"]

        # Create branch
        branch_resp = client.post(f"/repos/{REPO}/git/refs", json={
            "ref": f"refs/heads/{branch}",
            "sha": sha,
        })
        branch_resp.raise_for_status()

        # Create file
        file_resp = client.put(f"/repos/{REPO}/contents/{file_path}", json={
            "message": f"docs(oncall): add runbook for {slug}",
            "content": encoded,
            "branch": branch,
        })
        file_resp.raise_for_status()

        # Create PR
        pr_resp = client.post(f"/repos/{REPO}/pulls", json={
            "title": f"runbook: add {slug}",
            "head": branch,
            "base": "main",
            "body": f"Adds runbook for `{slug}` captured after incident.",
        })
        pr_resp.raise_for_status()
        return pr_resp.json()["html_url"]
