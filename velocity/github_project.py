"""Fetch project items from a GitHub Projects v2 board via the ``gh`` CLI.

This is the only impure module: it shells out to ``gh api graphql`` (reusing the
caller's existing ``gh`` authentication) and parses the response into
``ProjectItem`` objects. Everything downstream is pure.

Requires the ``read:project`` token scope::

    gh auth refresh -s read:project
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime

from .models import ProjectItem

DEFAULT_ORG = "greenearth-social"
DEFAULT_PROJECT_NUMBER = 2
PAGE_SIZE = 100

# Custom field names matched case-insensitively. Adjust here if the project
# renames a field. ``POINTS_FIELD_NAMES`` lists accepted aliases. In project #2
# Points is a single-select field whose option labels are the point values
# ("0".."8"). Type is *not* a project field -- it comes from the issue's native
# issue type (``content.issueType``).
POINTS_FIELD_NAMES = ("points", "estimate")
STATUS_FIELD_NAME = "status"

_QUERY = """
query($org: String!, $number: Int!, $pageSize: Int!, $after: String) {
  organization(login: $org) {
    projectV2(number: $number) {
      items(first: $pageSize, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          fieldValues(first: 50) {
            nodes {
              __typename
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                field { ... on ProjectV2FieldCommon { name } }
              }
              ... on ProjectV2ItemFieldNumberValue {
                number
                field { ... on ProjectV2FieldCommon { name } }
              }
              ... on ProjectV2ItemFieldTextValue {
                text
                field { ... on ProjectV2FieldCommon { name } }
              }
            }
          }
          content {
            __typename
            ... on Issue { title url closedAt issueType { name } }
            ... on PullRequest { title url closedAt }
            ... on DraftIssue { title }
          }
        }
      }
    }
  }
}
"""


# gh colorizes JSON when it thinks it's attached to a terminal. Some
# environments (e.g. Jupyter kernels) export CLICOLOR_FORCE/FORCE_COLOR, which
# makes gh emit ANSI escapes into stdout and breaks JSON parsing. Force color
# off and disable the pager/update notifier for clean, parseable output.
_GH_ENV = {
    **os.environ,
    "NO_COLOR": "1",
    "CLICOLOR": "0",
    "CLICOLOR_FORCE": "0",
    "GH_PAGER": "cat",
    "GH_NO_UPDATE_NOTIFIER": "1",
}


def _run_graphql(variables: dict) -> dict:
    """Execute the GraphQL query via ``gh`` and return the parsed ``data``."""
    args = ["gh", "api", "graphql", "-f", f"query={_QUERY}"]
    for key, value in variables.items():
        flag = "-F" if isinstance(value, int) else "-f"
        args += [flag, f"{key}={value}"]
    proc = subprocess.run(args, capture_output=True, text=True, env=_GH_ENV)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(
            "`gh api graphql` failed "
            f"(exit {proc.returncode}). Is gh installed and authenticated with "
            "the read:project scope (`gh auth refresh -s read:project`)?\n"
            f"stderr: {proc.stderr.strip() or '(empty)'}"
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"gh returned non-JSON output (exit {proc.returncode}): "
            f"{proc.stdout[:200]!r} / stderr: {proc.stderr[:200]!r}"
        ) from exc
    if "errors" in payload:
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")
    return payload["data"]


def _field_name(node: dict) -> str:
    return (node.get("field") or {}).get("name", "").strip().lower()


def _parse_item(node: dict) -> ProjectItem | None:
    content = node.get("content") or {}
    if not content:
        return None  # skip redacted / inaccessible items

    status = None
    raw_points: float | None = None
    for fv in node.get("fieldValues", {}).get("nodes", []):
        name = _field_name(fv)
        if not name:
            continue
        if name == STATUS_FIELD_NAME and "name" in fv:
            status = fv["name"]
        elif name in POINTS_FIELD_NAMES:
            # Points is a single-select ("name") in project #2, but tolerate a
            # plain number field too.
            value = fv.get("name", fv.get("number"))
            if value is not None:
                raw_points = float(value)

    # Type is the issue's native type (e.g. "Bug"); None for PRs/draft items.
    issue_type = content.get("issueType")
    raw_type = issue_type.get("name") if issue_type else None

    closed_at = None
    if content.get("closedAt"):
        closed_at = datetime.fromisoformat(content["closedAt"].replace("Z", "+00:00"))

    return ProjectItem(
        id=node["id"],
        title=content.get("title", "(untitled)"),
        url=content.get("url"),
        status=status,
        raw_type=raw_type,
        raw_points=raw_points,
        closed_at=closed_at,
    )


def fetch_items(
    org: str = DEFAULT_ORG,
    project_number: int = DEFAULT_PROJECT_NUMBER,
) -> list[ProjectItem]:
    """Fetch all items from the org project, in board (priority) order.

    The GraphQL ``items`` connection returns items in the project's manual
    order, which we treat as priority order for the backlog.
    """
    items: list[ProjectItem] = []
    after: str | None = None
    while True:
        variables = {"org": org, "number": project_number, "pageSize": PAGE_SIZE}
        if after:
            variables["after"] = after
        data = _run_graphql(variables)
        connection = data["organization"]["projectV2"]["items"]
        for node in connection["nodes"]:
            item = _parse_item(node)
            if item is not None:
                items.append(item)
        page = connection["pageInfo"]
        if not page["hasNextPage"]:
            break
        after = page["endCursor"]
    return items
