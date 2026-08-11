#!/bin/bash
set -e

GE_GCP_PROJECT_ID="${GE_GCP_PROJECT_ID:-greenearth-471522}"
GE_GCP_REGION="${GE_GCP_REGION:-us-east1}"
GE_ENVIRONMENT="${GE_ENVIRONMENT:-stage}"

SERVICE_NAME="oncall-bot-${GE_ENVIRONMENT}"
IMAGE="gcr.io/${GE_GCP_PROJECT_ID}/${SERVICE_NAME}"
GIT_SHA=$(git rev-parse --short HEAD)

echo "[INFO] Building image ${IMAGE}:${GIT_SHA}"
docker build --build-arg GIT_SHA="${GIT_SHA}" -t "${IMAGE}:${GIT_SHA}" -t "${IMAGE}:latest" .

echo "[INFO] Pushing image"
docker push "${IMAGE}:${GIT_SHA}"
docker push "${IMAGE}:latest"

echo "[INFO] Deploying to Cloud Run"
gcloud run deploy "${SERVICE_NAME}" \
  --image "${IMAGE}:${GIT_SHA}" \
  --region "${GE_GCP_REGION}" \
  --project "${GE_GCP_PROJECT_ID}" \
  --platform managed \
  --no-allow-unauthenticated \
  --set-env-vars "GE_FIRESTORE_PROJECT_ID=greenearth-prod,GE_ONCALL_RUNBOOKS_BRANCH=main" \
  --update-secrets \
    "GE_DISCORD_APPLICATION_ID=discord-oncall-app-id:latest,\
GE_DISCORD_PUBLIC_KEY=discord-oncall-public-key:latest,\
GE_DISCORD_BOT_TOKEN=discord-oncall-bot-token:latest,\
GE_DISCORD_ONCALL_CHANNEL_ID=discord-oncall-channel-id:latest,\
GE_GITHUB_TOKEN=oncall-github-token:latest"

echo "[INFO] Done. Service: ${SERVICE_NAME}"
