#!/usr/bin/env python3
"""Check that the downloaded models and the downloaded fixture agree (api#269).

The towers consume post embeddings; the fixture supplies them. If they came
from different encoders the arithmetic still works perfectly — vectors of the
same width multiply cleanly no matter what produced them — and every score is
meaningless. That failure is invisible: feeds come back, ranked, in an order
with nothing behind it.

So `devctl` calls this before `up` and `seed` and refuses to run on a mismatch.
Both manifests are written by the publishing scripts in this repo
(models/publish_models.sh, fixtures/generate_sample.py).

Usage: check_compat.py <models/manifest.json> <fixtures/manifest.json>
"""

import json
import pathlib
import sys


def incompatibilities(models: dict, fixture: dict) -> list[str]:
    """Return one line per disagreement; empty means the pair is usable."""
    problems = []
    if models["content_embedding_model"] != fixture["embedding_model"]:
        problems.append(
            f"  encoder:    models expect {models['content_embedding_model']}, "
            f"fixture has {fixture['embedding_model']}"
        )
    if models["content_embedding_dims"] != fixture["embedding_dims"]:
        problems.append(
            f"  dimensions: models expect {models['content_embedding_dims']}, "
            f"fixture has {fixture['embedding_dims']}"
        )
    return problems


def main(argv: list[str]) -> int:
    models = json.loads(pathlib.Path(argv[1]).read_text())
    fixture = json.loads(pathlib.Path(argv[2]).read_text())

    problems = incompatibilities(models, fixture)
    if not problems:
        return 0

    print("devctl: model artifacts and fixture disagree:", file=sys.stderr)
    print("\n".join(problems), file=sys.stderr)
    print(
        "\nRanking against them would produce confident nonsense. Fetch a "
        "matching pair:\n  devctl fetch-fixture && devctl fetch-models",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
