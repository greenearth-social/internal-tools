# internal-tools

Internal tooling for GreenEarth. Two self-contained subprojects live here, each
with its own README:

## [`devenv/`](devenv/README.md) — local dev environment

The whole Green Earth stack on your laptop (or a remote host): Elasticsearch
seeded with a real Bluesky sample, the api, inference-service serving the
trained towers, the Firebase emulators, and the frontend — no credentials
needed. Everything runs through one wrapper, `devctl`.

```bash
cd devenv
./devctl bootstrap     # first time: images, models, sample data, start, seed
./devctl feed          # render a ranked feed in the terminal
```

- [`devenv/README.md`](devenv/README.md) — how it works: fixtures, models,
  seeding, auth, live services, tunnels.
- [`devenv/docs/`](devenv/docs/) — task guides: `onboarding.md` (fresh-machine
  walkthrough), `troubleshooting.md`, `agents.md` (using it as a coding-agent
  sandbox), `remote.md` (running it on another host), `runbooks.md`
  (regenerating fixtures/models).

## [`velocity/`](velocity/README.md) — velocity & backlog projection

Pivotal-Tracker-style velocity tracking and backlog projection for our GitHub
Project: a tested library plus a presentation notebook.

```bash
cd velocity
pipenv run pytest velocity              # the library's tests
pipenv run jupyter notebook notebooks/velocity.ipynb
```

See [`velocity/README.md`](velocity/README.md) for setup, assumptions, and how
the projection is computed.

## Shared Python setup

Both subprojects share one `pipenv` environment defined at the repo root
(`Pipfile`, `pyproject.toml`). Install it once, from anywhere in the repo:

```bash
pipenv install --dev
```

`pyproject.toml` also holds the shared ruff and pyright config; `pipenv run
pytest` from the root runs both subprojects' test suites.
