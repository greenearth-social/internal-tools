# pyright: reportMissingImports=false
"""A handle's DID, resolved the way BridgEU's publish script does (run in
bridgeu-api): DNS or the handle's own /.well-known, so no AppView is involved.
"""

import sys

sys.path.insert(0, "src")
from app.lib.feed_publishing import AtprotoResolver, resolve_account  # noqa: E402

print(resolve_account(sys.argv[1], AtprotoResolver())[0])
