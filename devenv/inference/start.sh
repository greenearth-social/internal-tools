#!/usr/bin/env bash
# Start (or just prepare) inference-service in the dev environment (api#269).
#
# Lives here rather than inline in docker-compose.yml because `devctl setup`
# runs the same steps with --install-only to pre-warm the venv volume. Doing
# the install during setup, where waiting is expected, keeps the first
# `devctl up` from sitting silently on what looks like a hang.
set -euo pipefail

pipenv install --dev

# torch is deliberately absent from inference-service's Pipfile so the CPU and
# GPU images can each pick their own build; its Dockerfile installs it
# separately, and so do we. Skipped once the venv volume has it, which is what
# keeps later starts quick.
#
# No "+cpu" suffix on the version: that local-version tag only exists for
# 2.6.0 and later on this index, and every wheel served here is a CPU build
# regardless, so pinning the bare version works across more of them.
# (inference-service's own Dockerfile still says +cpu, which is why it pins a
# version that has one.)
if ! pipenv run python -c "import torch" 2>/dev/null; then
  echo "inference: installing torch ${GE_DEV_TORCH_VERSION:-2.5.1} (CPU build, one time)..."
  pipenv run pip install --index-url https://download.pytorch.org/whl/cpu \
    "torch==${GE_DEV_TORCH_VERSION:-2.5.1}"
fi

if [[ "${1:-}" == "--install-only" ]]; then
  echo "inference: dependencies ready"
  exit 0
fi

# --reload only under `devctl up --watch`. Off by default because a reload
# here re-loads the TorchScript towers from disk, which takes seconds and
# leaves the service failing readiness for the duration — an editor autosave
# mid-request is a worse failure mode than restarting deliberately.
exec pipenv run uvicorn app:app --host 0.0.0.0 --port 8000 \
  ${GE_DEV_INFERENCE_RELOAD:+--reload}
