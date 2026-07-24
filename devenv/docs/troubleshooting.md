# Troubleshooting

Start here:

```bash
./devctl doctor
```

It checks the prerequisites, artifacts, containers, Elasticsearch, seed
state and memory budget, and names a fix for everything it flags. The
sections below cover the symptoms doctor can't fully diagnose, roughly in
order of how often they happen.

## Feeds are empty (and nothing errors)

An empty feed is almost never a failure — it's a feed with nothing eligible
to serve, which is why nothing errors. Three causes, in likelihood order:

1. **The seed went stale.** Feed recency windows are anchored to *now*
   (popularity looks back 24h), while the seeded data stays where the seed
   put it. From ~23 hours after a seed, recency-ranked feeds go empty with
   nothing visibly wrong. `devctl status`, `devctl feed`, and `devctl doctor`
   all warn when this is the case. Fix: `./devctl seed` — re-seeding rebases
   every timestamp so the window ends an hour before now.
2. **Nothing was ever seeded.** `devctl status` shows 0 posts. Fix:
   `./devctl seed`.
3. **The models changed without a re-seed.** See the next section — this
   usually shows as a *thin* feed rather than an empty one.

Two feeds can be legitimately empty on healthy data: `network-likes` needs
the persona's followed accounts to have liked something inside the sampled
window, which is rare in any sample; and any feed can go empty for a persona
whose DID isn't real (pre-2026 pseudonymized fixtures). The api's logs say
which ("No liked posts found for followed users of ...").

## Feeds are thin (a few posts where there should be dozens)

The `two_tower` generator runs kNN over seeded post embeddings *filtered by
the model UUID that produced them*. If the models changed since the last
seed — a new model release, switching `--live inference` on or off — the
filter matches nothing and the feed falls back to whatever the other
generators found. This is the safe failure, but an easy one to misread.

**Any model change needs a re-seed.** `devctl doctor` checks that the model
bundle and fixture agree on the content encoder, but it cannot tell whether
the *seeded embeddings* came from the current towers — if in doubt, re-seed.

## A container was "Exited (137)" — or the whole environment "broke" <a name="memory"></a>

137 is SIGKILL: the kernel reclaimed memory, and Elasticsearch (the biggest
process) is usually the casualty. This is a machine-capacity problem, not a
service bug, and it typically strikes minutes after starting a second
instance or during a seed. `devctl status` and `devctl doctor` name any
killed container.

A seeded instance holds ~3GB resident (ES is ~1.9GB of it); Docker Desktop's
default ~8GB fits one comfortably and two only if they share a cluster
(named instances do by default). To run two dedicated clusters: give Docker
~12GB (Docker Desktop → Settings → Resources), or set `GE_DEV_ES_HEAP=512m`
in `devenv.local.env`, or drop `--dedicated-es`.

## `devctl up` fails with a port bind error

Something else on the machine holds one of this instance's ports — compose
names the port in its error. `devctl ports` shows which ports this instance
expects. Either stop the squatter or override the base in `devenv.local.env`
(`GE_DEV_PORT_API=...` etc.). Named instances allocate a free block at first
`up` and keep it, so this mostly affects the default instance's well-known
ports (8300, 9201, 3000, 8080...).

## `curl localhost:9201/...` returns 401

The cluster has security enabled, exactly as prod does. Use
`devctl es <path>`, which supplies this instance's port and read-only key:

```bash
./devctl es /posts/_count
./devctl es '/posts/_search?size=1&pretty'
```

## The frontend shows plausible-looking posts that aren't in the data

That's the frontend repo's mock mode (`VITE_USE_MOCK_SERVICES`), which
renders convincing hardcoded cards. The devenv forces it off by default;
if you set `GE_DEV_FRONTEND_MOCK=true` for UI work, remember it's on.
The tell: mock posts don't change after a re-seed.

## `devctl feed` shows "(not in Elasticsearch)" for an item

A real dangling reference, reported honestly: `your-feed` pins a specific
post that isn't in the fixture, and a cached feed can outlive a re-seed. Not
a failure.

## A named instance refuses to seed (or errors about the owner)

Named instances share the default (`dev`) instance's Elasticsearch and are
**read-only** against it: `devctl seed` and `--with-ingest` refuse there by
design. Seed the owner (`devctl seed`, no `--name`), or give the instance
its own cluster (`devctl up --name <n> --dedicated-es`) if your work is
about seeding itself. If the sharer's api can't reach the owner, the owner
isn't up: `devctl up`.

## Signed out of the frontend after changing the app origin

Any origin change (say, pointing `GE_DEV_APP_ORIGIN` at a tunnel for OAuth
work) is a fresh Firebase auth origin, so existing sessions drop. Expected;
sign in again.

## The first `devctl up` after a reboot sits there for minutes

First start of the api or frontend container runs `pipenv install` /
`npm ci` into a cached volume; later starts are quick. Watch it move:
`devctl logs -t api`. `devctl setup` (or `bootstrap`) pre-warms the slow
ones precisely so this doesn't look like a hang.

## Something else

- `devctl logs <service>` prints recent logs and exits; bare `devctl logs`
  aggregates every service, prefixed. Follow with `-t`, widen with
  `--tail=500`.
- `devctl restart <service>` recreates the container, picking up changed
  config as well as code.
- When in doubt: the data is disposable. `devctl nuke && devctl up &&
  devctl seed` rebuilds the world in ~15 minutes.
