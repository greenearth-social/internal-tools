"""Test setup for the devenv scripts.

The seed and es-init scripts are standalone entrypoints that read their
required configuration at import time and fail loudly when it's missing —
that's deliberate (a dev environment pointed at nothing should not start
half-working). Tests still need to import them, so supply dummy values here.

conftest is imported before the test modules that import those scripts, so
setting the environment at module scope is early enough.

pytest puts each test file's directory on sys.path (rootdir "prepend" import
mode), so `import rebase` works from seed/ and `import init` from es-init/
even though neither directory is an importable package — the hyphen in
"es-init" never has to be a module name.
"""

import os

os.environ.setdefault("GE_ELASTICSEARCH_URL", "http://elasticsearch.invalid:9200")
os.environ.setdefault("GE_ELASTICSEARCH_API_KEY", "test-key-not-used")
os.environ.setdefault("ELASTIC_PASSWORD", "test-password-not-used")
