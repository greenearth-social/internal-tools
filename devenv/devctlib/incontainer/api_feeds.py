# pyright: reportMissingImports=false
"""Each feed the api publishes, as "rkey<TAB>label" lines (run in the api).

The label is the feeds.py key (e.g. unranked-your-feed) — the recognizable
name; the URL uses the published rkey, which in dev is the feed's
internal_rkey. Read straight from the api's FEEDS config (the authoritative
source, and exactly what publish_feed.py --sync publishes), so no bsky
round-trip is needed.
"""

import sys

sys.path.insert(0, "src")
from app.feeds import FEEDS  # noqa: E402

for key, cfg in FEEDS.items():
    print((cfg.internal_rkey or key) + "\t" + key)
