#!/usr/bin/env bash
# Start (or just prepare) inference-service in the dev environment (api#269).
#
# Lives here rather than inline in docker-compose.yml because `devctl setup`
# runs the same steps with --install-only to pre-warm the venv volume. Doing
# the install during setup, where waiting is expected, keeps the first
# `devctl up` from sitting silently on what looks like a hang.
set -euo pipefail

# The venv lives in a volume that outlasts the image, so after a Python
# upgrade it still holds a venv built for the old interpreter. Asking its
# bin/python isn't enough: that's a symlink to /usr/local/bin/python, which
# now *is* the new interpreter, running over the old version's site-packages.
# pyvenv.cfg records what the venv was built for. On a mismatch, empty it (the
# volume is the mount point, so its contents, not the directory) and let
# pipenv rebuild against the interpreter that's actually here.
want="$(python -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if [[ -f .venv/pyvenv.cfg ]]; then
  have="$(sed -n 's/^version\(_info\)\{0,1\} *= *\([0-9]*\.[0-9]*\).*/\2/p' .venv/pyvenv.cfg | head -1)"
  if [[ "$have" != "$want" ]]; then
    echo "inference: venv is for Python $have, this image runs $want — rebuilding it"
    find .venv -mindepth 1 -delete
  fi
fi

pipenv install --dev

# Prod pins 2.5.1 (inference-service's Dockerfile), but its Python 3.13 wheels
# only exist for x86_64; the first 3.13 build for aarch64 — which is what
# Docker runs on Apple silicon — is 2.6.0. Match prod where we can.
if [[ -z "${GE_DEV_TORCH_VERSION:-}" ]]; then
  case "$(uname -m)" in
  x86_64) GE_DEV_TORCH_VERSION=2.5.1 ;;
  *) GE_DEV_TORCH_VERSION=2.6.0 ;;
  esac
fi

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
  echo "inference: installing torch ${GE_DEV_TORCH_VERSION} (CPU build, one time)..."
  pipenv run pip install --index-url https://download.pytorch.org/whl/cpu \
    "torch==${GE_DEV_TORCH_VERSION}"
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
