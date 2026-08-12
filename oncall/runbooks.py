import re

import frontmatter
import httpx

REPO = "greenearth-social/internal-tools"
RAW_BASE = "https://raw.githubusercontent.com"


def policy_name_to_slug(name: str) -> str:
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def fetch_runbook(policy_name: str, branch: str) -> tuple[bool, str]:
    slug = policy_name_to_slug(policy_name)
    url = f"{RAW_BASE}/{REPO}/{branch}/oncall/runbooks/{slug}.md"
    try:
        response = httpx.get(url, timeout=5.0)
        if response.status_code != 200:
            return False, ""
        post = frontmatter.loads(response.text)
        return True, post.content
    except Exception:
        return False, ""
