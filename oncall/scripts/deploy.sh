#!/bin/bash
set -e

GE_GCP_PROJECT_ID="${GE_GCP_PROJECT_ID:-greenearth-471522}"
GE_GCP_REGION="${GE_GCP_REGION:-us-east1}"

# The oncall bot is a single-instance internal tool. There is no stage
# environment — secrets (`discord-oncall-*`, `oncall-github-token`) and the
# Firestore project (`greenearth-prod`) are single-instance, not env-suffixed,
# so a `-stage` service would just be a second live prod bot pointing at the
# same state. If real stage isolation is ever needed, that's a bigger change
# (separate Discord app, env-suffixed secrets, separate Firestore).
SERVICE_NAME="oncall-bot-prod"
GIT_SHA=$(git rev-parse --short HEAD)

# Cloud Build's Python buildpack reads requirements.txt, not Pipfile, so
# regenerate it from the current Pipfile.lock right before deploy. The file
# is gitignored — it's a build artifact, not a source of truth.
echo "[INFO] Generating requirements.txt from Pipfile.lock..."
pipenv requirements > requirements.txt

echo "[INFO] Deploying ${SERVICE_NAME} from source (git sha: ${GIT_SHA})"
gcloud run deploy "${SERVICE_NAME}" \
  --source=. \
  --region "${GE_GCP_REGION}" \
  --project "${GE_GCP_PROJECT_ID}" \
  --platform managed \
  --no-allow-unauthenticated \
  --labels="git-sha=${GIT_SHA}" \
  --set-env-vars "GE_GIT_SHA=${GIT_SHA},\
GE_FIRESTORE_PROJECT_ID=greenearth-prod,\
GE_ONCALL_RUNBOOKS_BRANCH=main,\
GE_ONCALL_RUNBOOK_PROJECT_ID=PVT_kwDODjFtiM4BFpwX,\
GE_ONCALL_RUNBOOK_STATUS_FIELD_ID=PVTSSF_lADODjFtiM4BFpwXzg27V8w,\
GE_ONCALL_RUNBOOK_STATUS_INREVIEW_OPTION_ID=260c4616" \
  --update-secrets \
    "GE_DISCORD_APPLICATION_ID=discord-oncall-app-id:latest,\
GE_DISCORD_PUBLIC_KEY=discord-oncall-public-key:latest,\
GE_DISCORD_BOT_TOKEN=discord-oncall-bot-token:latest,\
GE_DISCORD_ONCALL_CHANNEL_ID=discord-oncall-channel-id:latest,\
GE_GITHUB_TOKEN=oncall-github-token:latest"

echo "[INFO] Done. Service: ${SERVICE_NAME}"
