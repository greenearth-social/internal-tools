"""BridgEU's side of the stack, as devctl sees it.

BridgEU lives in another GitHub org, so not every checkout of this stack has
it. Its services run when it's there and are skipped, with a note, when not.
"""

from __future__ import annotations

from .compose import compose
from .ctx import Ctx, die

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


def issue_bridgeu_key(ctx: Ctx) -> None:
    """Make the local api accept the key bridgeu-api calls it with. Firestore
    emulator data doesn't survive a recreate, so this runs on every `up` and
    after `restart firebase`. Nothing to do under --live mysky (bridgeu-api
    isn't calling the local api) or without the bridg-eu checkout."""
    if not have_bridgeu(ctx) or ctx.live_has("mysky"):
        return
    print("devctl: issuing BridgEU's local mysky api key...")
    if not compose(ctx, "run", "--rm", "issue-bridgeu-key", check=False, quiet=True).ok:
        die("couldn't issue BridgEU's mysky api key — see: devctl logs firebase")
