# Onboarding: from clone to a ranked feed

This walkthrough takes you from nothing to a running Green Earth stack —
Elasticsearch seeded with real Bluesky data, the api, the real trained models,
the Firebase emulators, and the frontend — with a ranked feed on screen at the
end. It requires **no credentials of any kind**: no GCP, no Firebase, no AWS,
no GitHub account. Everything it downloads comes from public GitHub releases.

Budget 20–30 minutes, most of it unattended (image builds and the seed).

## 1. Prerequisites

- **Docker with Compose v2.** Docker Desktop on macOS/Windows, or docker +
  the compose plugin on Linux.
- **Docker memory**: a single environment wants ~5GB available to Docker.
  Docker Desktop's default (~8GB) is fine for one; see
  [troubleshooting](troubleshooting.md#memory) before running two.
- **git**, **python3**, and **curl** — present on any development machine.

Nothing else. No Node, no Go, no Python packages on your host — every
language toolchain runs inside containers.

## 2. Clone the repos

The environment runs the real services from sibling checkouts, so all of them
sit next to each other under one parent directory:

```bash
mkdir greenearth && cd greenearth

# Internal engineers (SSH):
git clone git@github.com:greenearth-social/internal-tools.git
git clone git@github.com:greenearth-social/api.git
git clone git@github.com:greenearth-social/ingex.git
git clone git@github.com:greenearth-social/inference-service.git
git clone git@github.com:greenearth-social/frontend.git

# External contributors (HTTPS, no account needed to read):
git clone https://github.com/greenearth-social/internal-tools.git
# ...and the same four siblings via https.
```

`engagement-prediction` (the ML training pipeline) is optional — the
environment serves already-trained models and only needs that repo if you're
retraining.

If your checkouts live somewhere else, set `GE_DEV_REPO_ROOT=<parent dir>` in
`internal-tools/devenv/devenv.local.env`.

## 3. Bootstrap

```bash
cd internal-tools/devenv
./devctl bootstrap
```

This runs the whole first-time sequence: builds images, downloads the pinned
trained models (~33MB) and the published data fixture (~170MB, a real ~130k
post sample of Bluesky), starts the stack, and seeds Elasticsearch. The seed
is the slow part (~10 minutes) — it ingests every post through the real ingest
binary and embeds each one with the real post tower.

Bootstrap is resumable: if it fails partway (network hiccup, Docker not
started), fix the cause and run it again — completed steps are skipped.

If anything looks off at any point, from here on and forever:

```bash
./devctl doctor
```

It checks everything the environment needs and names the fix for whatever it
finds.

## 4. See a feed

```bash
./devctl feed
```

That requests `your-feed` from the local api as the seeded persona (a real
Bluesky account sampled into the fixture), ranks it with the real models, and
prints each post with its like count and pipeline detail — which candidate
generator retrieved it and what the ranker scored it. The order you see is the
order the model would actually serve.

For the browser version:

1. Open http://127.0.0.1:3000
2. Click **Sign in with Bluesky** — it signs you in as the seeded persona, no
   credentials involved (an emulator-only shim; see the README's Firebase
   section for how).
3. The transparency UI reports on feeds the api has served you. Run
   `./devctl feed` and reload the page to give it something to show.

## 5. Daily use

```bash
./devctl up              # bring the stack back (after a reboot, a `down`)
./devctl status          # what's running, doc counts, seed age
./devctl test            # every repo's test suite, in the right containers
./devctl test api        # just one
./devctl logs api        # recent api logs (add -t to follow)
./devctl restart api     # after editing api code
./devctl down            # stop; data survives
```

Source code is bind-mounted from your checkouts: edit with your normal editor
and commit with your normal git, then `devctl restart <service>` to pick the
change up (or run `devctl up --watch` for auto-reload). The frontend
hot-reloads on its own.

Two things worth knowing on day one:

- **The seeded data ages out.** Feed recency windows are anchored to now, so
  ~23 hours after a seed, recency-ranked feeds go empty. `devctl status` and
  `devctl feed` warn when this has happened. The fix is always
  `./devctl seed` (a re-seed is ~10 minutes, and wipes nothing you care
  about — seeded data is disposable by design).
- **Run tests inside the environment, not on your host.** There is no
  node_modules or venv in your checkouts. `devctl test` knows each repo's
  runner; `devctl exec <service> <cmd>` runs anything finer (a single test
  file, a linter). See the README's "Running commands inside the environment".

## Where to next

- [README](../README.md) — how the environment actually works: fixtures,
  models, seeding, live services, the Firebase/auth setup, multi-instance.
- [troubleshooting.md](troubleshooting.md) — symptoms and fixes.
- [remote.md](remote.md) — running this on a machine that isn't your laptop.
- [agents.md](agents.md) — using the environment as a coding-agent sandbox.

## Notes for external contributors

Everything above works without being a member of the GitHub org — the fixture
and model releases are public, and nothing in the environment reaches a
deployed Green Earth system. A few things are intentionally out of reach:

- **Live services** (`--live es|inference|perspective`) point at deployed
  infrastructure and need keys that aren't published.
- **Publishing fixtures or models** (`fixtures/publish_fixture.sh`,
  `models/publish_models.sh`) needs org and GCP access — see
  [runbooks.md](runbooks.md). You can still *generate* a local fixture from
  public data with `fixtures/generate_sample.py --source megastream-files`.
- **Live ingestion of posts** (`--with-megastream`) reads a private S3
  bucket. The public half (`--with-jetstream`, likes from the public
  firehose) works for anyone.
