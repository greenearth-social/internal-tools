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


# The key bridgeu-api calls the local api with. Deliberately fixed and public:
# it only ever opens the local api, which carries no data of its own, and a
# well-known value means nothing has to be minted and handed between the two
# sides. issue_bridgeu_key makes the local api accept it.
BRIDGEU_LOCAL_MYSKY_API_KEY = "gea_bridgeu0devenvlocalkeynotasecretdevenvlocalkeynotasecret"
BRIDGEU_LOCAL_MYSKY_API_URL = "http://mysky-api:8000"
