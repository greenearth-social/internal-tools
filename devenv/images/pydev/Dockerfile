# Generic Python dev-runner image for Green Earth services (issue api#268).
#
# Deliberately contains no service code: the service repo is bind-mounted at
# /app and dependencies are pipenv-installed at container start into a named
# volume (/app/.venv), so the image never needs rebuilding when code or deps
# change. git is required because Pipfiles pin the `shared` package from
# engagement-prediction as a git dependency.
FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pipenv
