# Node dev-runner image for the frontend and the Firebase emulators
# (issue greenearth-social/api#301).
#
# Like images/pydev, this carries no application code: the frontend repo is
# bind-mounted and dependencies install at container start into named volumes,
# so the image never needs rebuilding when code or deps change.
#
# Node 24 because frontend/functions/package.json pins engines.node = 24.
FROM node:24-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        # The Firestore and Auth emulators are Java programs.
        default-jre-headless \
        # Both services bridge loopback-bound ports; see docker-compose.yml.
        socat \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g firebase-tools@14

# Keep the CLI from phoning home or prompting in a container.
ENV FIREBASE_CLI_DISABLE_UPDATE_CHECK=1 \
    CI=1
