#!/usr/bin/env bash
# Publish the local fixture set as a GitHub Release asset (issue api#268).
#
# Fixtures are large compressed binaries that get regenerated periodically.
# Committing them would grow the repo permanently by ~170MB per generation
# (git can't delta compressed blobs), so they ride as release assets instead:
# engineers fetch one with `devctl fetch-fixture` and never generate their own.
#
# Maintainer-only, and rare — run it after generating a fresh sample:
#
#   python3 fixtures/generate_sample.py --source prod-es
#   fixtures/publish_fixture.sh
#
# Takes an optional fixture directory, defaulting to fixtures/data/. Passing
# one lets a freshly generated set be published straight out of its staging
# directory (see regen_fixture.sh) without first overwriting the fixture the
# local environment is currently seeded from:
#
#   fixtures/publish_fixture.sh fixtures/data.staging
#
# Each publish creates a new dated release; `devctl fetch-fixture` picks the
# most recent one. Delete old releases when they stop being useful — unlike
# git history, they can be reclaimed.
set -euo pipefail

FIXTURES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${1:-$FIXTURES_DIR/data}"
REPO="${GE_DEV_FIXTURE_REPO:-greenearth-social/internal-tools}"
ASSET_NAME="ge-dev-fixture.tar"

die() {
  echo "publish_fixture: $*" >&2
  exit 1
}

command -v gh >/dev/null || die "the gh CLI is required (https://cli.github.com/)"
[[ -f "$DATA_DIR/manifest.json" ]] ||
  die "no fixture set in $DATA_DIR — generate one first (see fixtures/generate_sample.py)"

# Tag from the fixture's own window end, so the release name states which slice
# of Bluesky it holds rather than when someone happened to upload it.
window_end="$(python3 -c "
import json, pathlib
m = json.loads(pathlib.Path('$DATA_DIR/manifest.json').read_text())
print(m['window_end'])
")"
tag="fixture-$(python3 -c "
import datetime
print(datetime.datetime.fromisoformat('$window_end'.replace('Z', '+00:00')).strftime('%Y%m%d-%H%M'))
")"

summary="$(python3 -c "
import json, pathlib
m = json.loads(pathlib.Path('$DATA_DIR/manifest.json').read_text())
c = m['counts']
print(f\"{c['posts']:,} posts, {c['likes']:,} likes, cohort of {c['cohort']}\")
print()
print(f\"- Window: {m['window_start']} .. {m['window_end']}\")
print(f\"- Source: {m['source']}\")
print(f\"- Embeddings: {m['embedding_model']} ({m['embedding_dims']}d)\")
print(f\"- Generated: {m['generated_at']} by generator {m['generator_commit']}\")
print()
print('Fetch with \`devctl fetch-fixture\`. Seeding rebases every timestamp so')
print('the window ends an hour before now, no matter how old this release is.')
")"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

# Contents are already compressed (zip/gzip), so tar without compression.
echo "publish_fixture: bundling $DATA_DIR ..."
tar -cf "$workdir/$ASSET_NAME" -C "$DATA_DIR" .
echo "publish_fixture: bundle is $(du -h "$workdir/$ASSET_NAME" | cut -f1)"

if gh release view "$tag" --repo "$REPO" >/dev/null 2>&1; then
  echo "publish_fixture: release $tag exists; replacing its asset"
  gh release upload "$tag" "$workdir/$ASSET_NAME" --repo "$REPO" --clobber
else
  echo "publish_fixture: creating release $tag"
  gh release create "$tag" "$workdir/$ASSET_NAME" \
    --repo "$REPO" \
    --title "Dev fixture $tag" \
    --notes "$summary"
fi

echo "publish_fixture: done — fetch it with: devctl fetch-fixture"
