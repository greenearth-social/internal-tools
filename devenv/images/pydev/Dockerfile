# Generic Python dev-runner image for Green Earth services (issue api#268).
#
# Deliberately contains no service code: the service repo is bind-mounted at
# /app and dependencies are pipenv-installed at container start into a named
# volume (/app/.venv), so the image never needs rebuilding when code or deps
# change. git is required because Pipfiles pin the `shared` package from
# engagement-prediction as a git dependency.
# inference-service pins python 3.11 (its Pipfile requires it, and the torch
# build it serves has no 3.13 wheel), while the api tracks 3.13. Same image,
# different tag.
ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION}-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pipenv
