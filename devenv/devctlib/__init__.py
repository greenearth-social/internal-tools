"""devctl — Green Earth local development environment (issue api#268).

Orchestrates the docker-compose stack in devenv/. The key sequencing it owns:
es-init must complete (minting ES API keys into .runtime/) before the api
starts, and seeding runs rebase -> megastream ingest -> likes -> quality.

Layout assumption: internal-tools is checked out next to its sibling repos
(api, ingex, ...) under one parent directory, like the greenearth-social
monorepo checkout. Override with GE_DEV_REPO_ROOT.

`devenv/devctl` is a small launcher that keeps this package's virtualenv and
runs `cli.main`. Every command takes a `Ctx` (ctx.py), which holds what used
to be the bash script's globals: the instance, its ports, and the environment
handed to docker compose.
"""
