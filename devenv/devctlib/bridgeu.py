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


# The feed_view.py arguments for a BridgEU feed. The feed goes as a full AT
# URI under BridgEU's publisher, because a bare name means a mysky feed to the
# viewer (it even aliases `mysky` to your-feed). BridgEU serves mysky's slates
# without writing the api's feed snapshots, so there is no pipeline detail to
# wait for: --no-pipeline.
BRIDGEU_DEFAULT_FEED = "passthrough"
BRIDGEU_API_URL = "http://bridgeu-api:8000"
# feed_view.py's options that take a value, so the value isn't mistaken for
# the feed.
VIEWER_VALUE_OPTIONS = ("--user", "--limit", "--cursor", "--pages", "--api-url", "--publisher")


def bridgeu_feed_args(publisher: str, args: list[str]) -> list[str]:
    feed = ""
    viewer = ["--api-url", BRIDGEU_API_URL, "--no-pipeline"]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in VIEWER_VALUE_OPTIONS:
            viewer += [arg, args[i + 1] if i + 1 < len(args) else ""]
            i += 2
        elif arg.startswith("-"):
            viewer.append(arg)
            i += 1
        else:
            if feed:
                die(f"feed given twice: {feed} and {arg}")
            feed = arg
            i += 1
    feed = feed or BRIDGEU_DEFAULT_FEED
    if not feed.startswith("at://"):
        feed = f"at://{publisher}/app.bsky.feed.generator/{feed}"
    return [feed, *viewer]
