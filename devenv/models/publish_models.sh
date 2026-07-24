#!/usr/bin/env bash
# publish_models.sh — publish trained model artifacts for the dev environment
# (issue api#269).
#
# The dev environment runs the *real* inference-service against the *real*
# trained towers. Those artifacts live in a private GCS bucket that only
# maintainers can read, so this script mirrors a set of them into a public
# GitHub release — the same distribution channel the fixtures already use
# (see fixtures/publish_fixture.sh), for the same reason: a fresh clone should
# be able to get everything it needs with no Google account and no credentials.
#
# Publishing model weights publicly is deliberate. The training code, the
# training data source, and the serving code are all public already; the
# weights are the one piece that wasn't, and keeping them private only made
# the environment harder to stand up.
#
# Maintainer-only, and rare — run it when the models prod serves change:
#
#   models/publish_models.sh                      # newest in the prod bucket
#   models/publish_models.sh --dry-run            # stage and inspect, no upload
#
# Then pin the new tag in devctl (MODELS_TAG) so everyone gets it.
#
# Requires: gcloud (authenticated, with read access to the prod model bucket),
# gh, python3.
set -euo pipefail

MODELS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUCKET="${GE_MODEL_BUCKET:-gs://greenearth-471522-engagement-prediction-model-prod}"
REPO="${GE_DEV_FIXTURE_REPO:-greenearth-social/internal-tools}"
ASSET_NAME="ge-dev-models.tar"
# Where the bundle gets mounted in the compose environment. The serving
# manifests are rewritten to point here, so inference-service loads plain local
# files and never touches GCS or ClearML.
MOUNT_POINT="${GE_DEV_MODELS_MOUNT:-/models}"

TWO_TOWER_MANIFEST=""
RANKER_MANIFEST=""
DRY_RUN=0

die() {
  echo "publish_models: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
  --two-tower-manifest)
    TWO_TOWER_MANIFEST="$2"
    shift 2
    ;;
  --ranker-manifest)
    RANKER_MANIFEST="$2"
    shift 2
    ;;
  --dry-run)
    DRY_RUN=1
    shift
    ;;
  *) die "unknown argument: $1" ;;
  esac
done

command -v gcloud >/dev/null || die "gcloud is required (https://cloud.google.com/sdk)"
command -v python3 >/dev/null || die "python3 is required"
[[ $DRY_RUN -eq 1 ]] || command -v gh >/dev/null ||
  die "the gh CLI is required to publish (https://cli.github.com/)"

# --- pick which artifacts to mirror ----------------------------------------
# Newest-in-the-bucket is a proxy for what prod serves, not a reading of it:
# the deployed URIs live in the Cloud Run service config. It has matched every
# time so far, and the script prints its choice so you can check. Override with
# --two-tower-manifest / --ranker-manifest when it doesn't.

newest_manifest() {
  local name="$1"
  gcloud storage ls -l -r "$BUCKET/**/$name" 2>/dev/null |
    grep -v '^TOTAL:' |
    sort -k2 |
    tail -1 |
    sed 's/^ *[0-9]* *[^ ]* *//'
}

[[ -n "$TWO_TOWER_MANIFEST" ]] ||
  TWO_TOWER_MANIFEST="$(newest_manifest two_tower_serving_manifest.json)"
[[ -n "$RANKER_MANIFEST" ]] ||
  RANKER_MANIFEST="$(newest_manifest ranker_serving_manifest.json)"

[[ -n "$TWO_TOWER_MANIFEST" ]] || die "no two_tower_serving_manifest.json found in $BUCKET"
[[ -n "$RANKER_MANIFEST" ]] || die "no ranker_serving_manifest.json found in $BUCKET"

echo "publish_models: two-tower manifest: $TWO_TOWER_MANIFEST"
echo "publish_models: ranker manifest:    $RANKER_MANIFEST"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT
stage="$workdir/models"
mkdir -p "$stage"

fetch() {
  local uri="$1" dest="$2"
  gcloud storage cp "$uri" "$dest" >/dev/null 2>&1 ||
    die "failed to download $uri (is your gcloud account authorized for $BUCKET?)"
}

fetch "$TWO_TOWER_MANIFEST" "$workdir/two_tower_src.json"
fetch "$RANKER_MANIFEST" "$workdir/ranker_src.json"

read_json() {
  python3 -c "
import json, sys
print(json.load(open(sys.argv[1]))[sys.argv[2]])
" "$1" "$2"
}

post_tower_uri="$(read_json "$workdir/two_tower_src.json" post_tower_uri)"
user_tower_uri="$(read_json "$workdir/two_tower_src.json" user_tower_uri)"
ranker_uri="$(read_json "$workdir/ranker_src.json" ranker_uri)"

# The author index map isn't named in the serving manifests — it's a sibling
# artifact of the training run that produced them, so derive it from the
# manifest's own prefix rather than hardcoding a filename that changes per run.
author_map_for() {
  local manifest_uri="$1" prefix
  prefix="${manifest_uri%/artifacts/*}"
  gcloud storage ls "$prefix/artifacts/author_idx_mapping/*.parquet" 2>/dev/null | tail -1
}

two_tower_author_map="$(author_map_for "$TWO_TOWER_MANIFEST")"
ranker_author_map="$(author_map_for "$RANKER_MANIFEST")"
[[ -n "$two_tower_author_map" ]] || die "no author_idx_mapping parquet beside $TWO_TOWER_MANIFEST"
[[ -n "$ranker_author_map" ]] || die "no author_idx_mapping parquet beside $RANKER_MANIFEST"

echo "publish_models: downloading artifacts (~33MB)..."
fetch "$post_tower_uri" "$stage/engagement_post_tower.pt"
fetch "$user_tower_uri" "$stage/engagement_user_tower.pt"
fetch "$ranker_uri" "$stage/ranker.pt"
# Copied under both names even when they're the same file today: the two-tower
# and the ranker are trained separately and can drift onto different maps.
fetch "$two_tower_author_map" "$stage/two_tower_author_idx.parquet"
fetch "$ranker_author_map" "$stage/ranker_author_idx.parquet"

# --- rewrite the serving manifests to local paths --------------------------
# Same schema inference-service already reads, with gs:// URIs swapped for the
# mount point. Nothing else about how it loads models changes.

python3 - "$workdir" "$stage" "$MOUNT_POINT" <<'PY'
import json, pathlib, sys

workdir, stage, mount = (pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3])

two_tower = json.loads((workdir / "two_tower_src.json").read_text())
two_tower["post_tower_uri"] = f"{mount}/engagement_post_tower.pt"
two_tower["user_tower_uri"] = f"{mount}/engagement_user_tower.pt"
(stage / "two_tower_serving_manifest.json").write_text(json.dumps(two_tower, indent=2) + "\n")

ranker = json.loads((workdir / "ranker_src.json").read_text())
ranker["ranker_uri"] = f"{mount}/ranker.pt"
(stage / "ranker_serving_manifest.json").write_text(json.dumps(ranker, indent=2) + "\n")
PY

# --- the environment file compose reads ------------------------------------
# Generated from the artifacts rather than hand-maintained in docker-compose.yml,
# so a model whose shape changed can't be served under stale dimensions.

output_dim="$(read_json "$workdir/two_tower_src.json" output_embedding_dim)"
content_dim="${GE_DEV_CONTENT_EMBED_DIM:-384}"
# Serving config, not a property of the artifact — it isn't recorded in the
# manifest, so it can't be derived and has to be mirrored from how prod runs
# these same models:
#   gcloud run services describe engagement-prediction-inference-prod \
#     --region=us-east1 --format='value(spec.template.spec.containers[0].env)'
# Getting it wrong doesn't fail: the tower just receives a history padded to a
# length it wasn't trained on and returns worse embeddings, quietly. Re-check
# it when prod's deployment changes.
max_history="${GE_DEV_MAX_HISTORY_LEN:-50}"

cat >"$stage/models.env" <<EOF
# Generated by models/publish_models.sh — do not edit.
# Read by the \`inference\` service in docker-compose.yml.
GE_INFERENCE_MODELS=post-tower,user-tower,ranker
GE_INFERENCE_CONTENT_EMBED_DIM=$content_dim
GE_INFERENCE_TWO_TOWER_MANIFEST_URI=$MOUNT_POINT/two_tower_serving_manifest.json
GE_INFERENCE_TWO_TOWER_AUTHOR_MAP_URI=$MOUNT_POINT/two_tower_author_idx.parquet
GE_INFERENCE_TWO_TOWER_MAX_HISTORY_LEN=$max_history
GE_INFERENCE_RANKER_MANIFEST_URI=$MOUNT_POINT/ranker_serving_manifest.json
GE_INFERENCE_RANKER_AUTHOR_MAP_URI=$MOUNT_POINT/ranker_author_idx.parquet
GE_INFERENCE_RANKER_MAX_HISTORY_LEN=$max_history
EOF

# --- provenance ------------------------------------------------------------
# What devctl checks the fixture against, and what tells a human which training
# run they're actually serving.

# Derived from the training runs, not the clock, so the tag is a function of
# what's in the bundle. That means MODELS_TAG can be pinned in devctl before
# anyone uploads, re-running this for the same artifacts is idempotent, and the
# tag says which runs it holds instead of when someone happened to press go.
tag="models-$(read_json "$workdir/two_tower_src.json" clearml_task_id | cut -c1-8)-$(
  read_json "$workdir/ranker_src.json" clearml_task_id | cut -c1-8)"

python3 - "$stage/manifest.json" <<PY
import json, pathlib
pathlib.Path("$stage/manifest.json").write_text(json.dumps({
    "schema_version": 1,
    "tag": "$tag",
    "published_at": __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc).isoformat().replace("+00:00", "Z"),
    # What the towers consume. The fixture's post embeddings have to match
    # this exactly or every score is garbage, so devctl compares them.
    "content_embedding_model": "all-MiniLM-L12-v2",
    "content_embedding_dims": int("$content_dim"),
    # What the post tower emits, and therefore the dims of the ES
    # ge_post_embedding field the two_tower generator runs kNN over.
    "output_embedding_dim": int("$output_dim"),
    "max_history_len": int("$max_history"),
    "source": {
        "bucket": "$BUCKET",
        "two_tower_manifest_uri": "$TWO_TOWER_MANIFEST",
        "ranker_manifest_uri": "$RANKER_MANIFEST",
        "post_tower_uri": "$post_tower_uri",
        "user_tower_uri": "$user_tower_uri",
        "ranker_uri": "$ranker_uri",
        "two_tower_author_map_uri": "$two_tower_author_map",
        "ranker_author_map_uri": "$ranker_author_map",
    },
    # Kept separate: the two towers and the ranker come from different
    # training runs, so they have different task ids. Merging the two
    # manifests would silently drop one of them.
    "clearml": {
        "two_tower": json.loads(pathlib.Path("$workdir/two_tower_src.json").read_text()),
        "ranker": json.loads(pathlib.Path("$workdir/ranker_src.json").read_text()),
    },
}, indent=2) + "\n")
PY

echo "publish_models: staged bundle:"
ls -la "$stage"

if [[ $DRY_RUN -eq 1 ]]; then
  keep="$MODELS_DIR/data.staging"
  rm -rf "$keep"
  cp -R "$stage" "$keep"
  echo "publish_models: --dry-run, not uploading. Bundle kept at $keep"
  exit 0
fi

# Already-compressed weights, so tar without compression (same as fixtures).
tar -cf "$workdir/$ASSET_NAME" -C "$stage" .
echo "publish_models: bundle is $(du -h "$workdir/$ASSET_NAME" | cut -f1)"

notes="$(python3 -c "
import json, pathlib
m = json.loads(pathlib.Path('$stage/manifest.json').read_text())
s = m['source']
print('Trained two-tower + ranker artifacts for the local dev environment.')
print()
print(f\"- Content embeddings: {m['content_embedding_model']} ({m['content_embedding_dims']}d)\")
print(f\"- Tower output dim: {m['output_embedding_dim']}\")
print(f\"- Max history: {m['max_history_len']}\")
print(f\"- Two-tower run: {s['two_tower_manifest_uri']}\")
print(f\"- Ranker run: {s['ranker_manifest_uri']}\")
print()
print('Fetch with \`devctl fetch-models\`. Pin it by setting MODELS_TAG in devctl.')
")"

if gh release view "$tag" --repo "$REPO" >/dev/null 2>&1; then
  # Same artifacts, same tag: replacing the asset is the intended path for
  # re-running this after a bundling fix.
  echo "publish_models: release $tag exists; replacing its asset"
  gh release upload "$tag" "$workdir/$ASSET_NAME" --repo "$REPO" --clobber
else
  echo "publish_models: creating release $tag"
  gh release create "$tag" "$workdir/$ASSET_NAME" \
    --repo "$REPO" \
    --title "Dev models $tag" \
    --notes "$notes"
fi

echo "publish_models: done.

Pin it: set MODELS_TAG=\"$tag\" in devctl, then: devctl fetch-models"
