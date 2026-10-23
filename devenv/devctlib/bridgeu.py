"""BridgEU's side of the stack, as devctl sees it.

BridgEU lives in another GitHub org, so not every checkout of this stack has
it. Its services run when it's there and are skipped, with a note, when not.
"""

from __future__ import annotations

from .ctx import Ctx

BRIDGEU_REPO = "bridg-eu"
BRIDGEU_CLONE_URL = "git@github.com:eurosky-social/bridg-eu.git"


def have_bridgeu(ctx: Ctx) -> bool:
    return (ctx.repo_root / BRIDGEU_REPO / ".git").exists()


def clone_hint(ctx: Ctx) -> str:
    """The command that fetches the checkout BridgEU's services need."""
    return f'git clone {BRIDGEU_CLONE_URL} "{ctx.repo_root}/{BRIDGEU_REPO}"'
