---
description: Run and inspect the Green Earth local dev environment (Elasticsearch, api, inference, Firebase emulators, frontend) with devctl. Use when working on api, ingex, inference-service or frontend and you need to start the stack, run tests inside it, seed data, look at a feed, or read service logs.
---

# The Green Earth dev environment

`devenv/devctl` runs the whole stack locally: Elasticsearch seeded with a real
sample of Bluesky, the api, inference-service serving the real trained towers,
the Firebase emulators, and the frontend. It needs no credentials — fixtures
and models come from public GitHub releases.

Everything goes through `devctl`. Run it from `internal-tools/devenv/`, or by
absolute path from anywhere.

## Before anything else

Check whether the environment is already up before starting one:

```bash
devctl ls        # every instance on this machine, running or not
devctl status    # containers + how many posts/likes/inferences are indexed
```

`devctl status` showing zero posts means it's running but unseeded. A first-time
machine needs `devctl setup` (images, models, inference dependencies including
torch) and `devctl fetch-fixture` (~170MB of sample data) — several minutes
between them, so don't start that speculatively.

## Running things inside the environment

**Dependencies are installed in containers, not on the host.** `pytest`, `npm`,
and `go` on the host will fail or use the wrong versions. Use:

```bash
devctl exec api pipenv run pytest
devctl exec api pipenv run pytest src/app/lib/firestore_test.py -v
devctl exec frontend npm run typecheck
devctl exec frontend npm run test:unit
devctl exec inference pipenv run pytest
```

Service names: `api`, `frontend`, `inference`, `elasticsearch`, `firebase`.

## The normal loop

```bash
devctl up                       # start (or bring back) the stack
devctl seed                     # load the fixture — needed once per fresh volume
devctl feed                     # render your-feed in the terminal as the persona
devctl feed popularity --limit 5
devctl logs api                 # recent logs, then exits
devctl logs -t api              # follow instead
devctl restart api              # after editing api code
```

Source code is bind-mounted from the sibling checkouts, so edits are visible
inside the containers immediately. The api and inference services do **not**
reload on save by default — that's deliberate, so a save mid-request doesn't
restart the service underneath you. Pick one:

- `devctl restart api` when you've finished a change (preferred while an agent
  is editing), or
- `devctl up --watch` to run api and inference under `--reload` for a session.

The frontend always hot-reloads. The Go ingest services are `go run`, so they
recompile on `devctl restart`.

## Working in parallel

Two tasks at once get two instances, with their own containers and ports:

```bash
devctl up --name featurework     # a second environment, ~90s
devctl --name featurework exec api pipenv run pytest
devctl ports --name featurework  # which ports this one got
devctl nuke --name featurework   # destroys only this instance
```

`--name` applies to every command and defaults to `dev`. Forgetting it acts on
the default instance, so prefer `devctl --name X <cmd>` consistently once you
have one.

A named instance **shares `dev`'s Elasticsearch** — that's the expensive part
(~1.9GB and an hour to seed), so it isn't duplicated. This means:

- It comes up fast and already sees `dev`'s data — no per-instance seed.
- It is **read-only** against that data. `devctl seed` and `--with-ingest`
  refuse from a named instance; seed `dev` instead.
- `dev` must be up and seeded first. Don't `nuke` `dev` while named instances
  exist (devctl refuses, but still).

Only pass `devctl up --name X --dedicated-es` when the task is *about* seeding
or the index setup and needs its own cluster to write to. That costs a second
~3GB Elasticsearch; two clusters need ~12GB of Docker memory or a smaller
`GE_DEV_ES_HEAP`, and `devctl up` warns when they won't fit. Otherwise share.

## Reading the data directly

```bash
devctl es /posts/_count
devctl es '/posts/_search?size=1&pretty'
devctl feed --json               # raw getFeedSkeleton response
```

`devctl es` supplies the port and the read-only key, both of which differ per
instance. The cluster has security enabled, so a bare
`curl localhost:9200/...` returns 401.

## Things that will waste your time

- **Don't run `docker compose` directly.** devctl sequences steps that compose
  doesn't know about: minting the ES API keys before the api starts, waiting
  for inference to load its models before seeding.
- **Don't expect a feed before seeding.** An empty index returns an empty feed,
  not an error.
- **A changed model or fixture needs a re-seed.** Post embeddings are stamped
  with the post tower's ID and the ranker filters on it; a mismatch shows up as
  a thin feed rather than a failure.
- **`devctl seed` wipes and reloads.** It is not additive.
- **Live services are opt-in.** `devctl up --live inference,es,perspective`
  points individual backends at deployed ones; without the flag everything is
  local. Never point an environment at prod without being asked to.

## Where to look next

`internal-tools/devenv/README.md` covers fixtures and how to regenerate them,
model provenance, seeding and time rebasing, the Firebase/auth setup, live
services and tunnels, and known limitations. `devctl help` lists every command.
