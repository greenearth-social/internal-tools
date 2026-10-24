"""`devctl pull`: fast-forward every sibling checkout.

The thing to run at the start of a task: fetch and fast-forward all the repos
under the repo root at once, and call out any that sit on a branch other than
main. Never merges or rebases — a repo that can't fast-forward (local
commits, a diverged branch, a dirty tree, no upstream) is reported and skipped
so one repo's state never blocks the rest. Multi-root aware: acts on the
resolved repo root, and `--repo-root <dir>` points it at any working copy
(e.g. a worktree a parallel instance mounts).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .bridgeu import BRIDGEU_REPO
from .ctx import Ctx, die

REPOS = (
    "api",
    "ingex",
    "frontend",
    "inference-service",
    "internal-tools",
    "engagement-prediction",
    BRIDGEU_REPO,
)

ROW = "{:<22} {:<26} {}"


def git(ctx: Ctx, path: Path, *args: str) -> tuple[bool, str]:
    result = ctx.run(["git", "-C", str(path), *args], check=False, capture=True, quiet_err=True)
    return result.ok, result.stdout.strip()


def pull_one(ctx: Ctx, path: Path) -> str:
    """Fetch one checkout and fast-forward it; a one-line result."""
    _, dirty = git(ctx, path, "status", "--porcelain")
    if dirty:
        return "skipped: uncommitted changes"
    ok, upstream = git(ctx, path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if not ok or not upstream:
        return "skipped: no upstream"
    if not git(ctx, path, "fetch", "--quiet")[0]:
        return "error: fetch failed"
    _, head = git(ctx, path, "rev-parse", "HEAD")
    _, up = git(ctx, path, "rev-parse", "@{u}")
    if head == up:
        return "up to date"
    _, base = git(ctx, path, "merge-base", "HEAD", "@{u}")
    if base != head:
        return "skipped: diverged (needs manual merge)"
    _, behind = git(ctx, path, "rev-list", "--count", "HEAD..@{u}")
    if git(ctx, path, "merge", "--ff-only", "--quiet", "@{u}")[0]:
        return f"pulled {behind} commit(s)"
    return "error: fast-forward failed"


def cmd_pull(ctx: Ctx, args: list[str]) -> None:
    root = ctx.repo_root
    while args:
        if args[0] != "--repo-root":
            die("usage: devctl pull [--repo-root <dir>]")
        if len(args) < 2 or not args[1]:
            die("--repo-root needs a directory")
        if not Path(args[1]).is_dir():
            die(f"no such directory: {args[1]}")
        root = Path(os.path.abspath(args[1]))
        args = args[2:]
    if shutil.which("git", path=ctx.env.get("PATH")) is None:
        die("git is required for devctl pull")

    failed = False
    off_main = []
    print(ROW.format("REPO", "BRANCH", "RESULT"))
    for repo in REPOS:
        path = root / repo
        if not (path / ".git").exists():
            continue
        ok, branch = git(ctx, path, "rev-parse", "--abbrev-ref", "HEAD")
        branch = branch if ok else "?"
        result = pull_one(ctx, path)
        # Only a real failure (fetch/fast-forward error) is non-zero. A skip —
        # a dirty tree, a feature branch, no upstream — is a normal
        # start-of-task state the table already shows.
        failed = failed or result.startswith("error:")
        print(ROW.format(repo, branch, result))
        if branch not in ("main", "?"):
            off_main.append(f"{repo} ({branch})")

    if off_main:
        print()
        print("on a branch other than main:")
        for item in off_main:
            print(f"  {item}")
    if root != ctx.default_repo_root:
        print()
        print(f"(repo root: {root})")
    if failed:
        raise SystemExit(1)
