# Try the prompt feed on your own machine

You type a prompt ("science but funny") on the MySky settings page, wait about
10 seconds, and the preview shows posts picked for that prompt, tagged with a
peach **Prompt** badge.

Everything runs on your own machine, in Docker. The posts come from the real
prod search index, or from a local 131k-post sample if you prefer.

**Cost:** each Send is about $0.12 on the Anthropic key you use. Don't loop it.

## 1. What you need

| For | You need |
|---|---|
| everything | Docker Desktop running, git, python3, curl. Nothing else on your machine: every language toolchain runs inside containers. |
| the prod index | `gcloud` (signed in, with `gke-gcloud-auth-plugin`), `kubectl`, and access to the prod cluster |

## 2. Get the branches

All five repos sit side by side in one folder (`internal-tools`, `api`,
`ingex`, `inference-service`, `frontend`). If `git switch` complains about
local changes, `git stash` first.

```bash
git -C api            fetch && git -C api            switch 482-llm-qv-end-to-end    && git -C api pull
git -C frontend       fetch && git -C frontend       switch llm-qv                   && git -C frontend pull
git -C internal-tools fetch && git -C internal-tools switch devenv-anthropic-api-key && git -C internal-tools pull
git -C ingex pull && git -C inference-service pull          # both on main
```

(`devenv-anthropic-api-key` is internal-tools PR #46. Once it merges, use main.)

This guide assumes the devenv has already run on this machine. If it never
has, do [onboarding](onboarding.md) first (20–30 min, mostly unattended).

## 3. Put the settings in

They go in `internal-tools/devenv/devenv.local.env`, which is gitignored.
Never commit a key.

```bash
cd internal-tools/devenv
[ -f devenv.local.env ] || cp devenv.local.env.example devenv.local.env

# Anthropic key. Any Anthropic API key works; this fetches the team's:
echo "GE_ANTHROPIC_API_KEY=$(gcloud secrets versions access latest --secret=anthropic-api-key-stage --project=greenearth-471522)" >> devenv.local.env

# Prod index only: the READ-ONLY prod search key, and a longer time limit,
# because the prompt search on prod takes ~9 s and the default limit is 4 (ingex#513).
echo "GE_DEV_ES_API_KEY=$(gcloud secrets versions access latest --secret=elasticsearch-api-key-readonly-prod --project=greenearth-471522)" >> devenv.local.env
echo "GE_CANDIDATE_GENERATOR_TIMEOUT_SEC=15" >> devenv.local.env
```

No access to Secret Manager, or a command failed? Ask Juan for the keys, open
`devenv.local.env` in an editor, and make sure it ends with these lines (no
quotes, no spaces around `=`):

```bash
GE_ANTHROPIC_API_KEY=<the Anthropic key>
GE_DEV_ES_API_KEY=<the read-only prod search key>
GE_CANDIDATE_GENERATOR_TIMEOUT_SEC=15
```

A failed `gcloud` command still leaves a line with nothing after the `=`.
Delete that line: the last line for a name wins, so an empty one can blank out a good key.

## 4. Start it

This starts the whole stack on your machine, in the background: the website
on port 3000, the api on 8300, and local stand-ins for sign-in and storage.
It keeps running until `./devctl down`.

**Prod index (the real thing).** Two terminals, both in `internal-tools/devenv`:

```bash
./devctl tunnel prod                      # terminal 1: leave it running
```
```bash
./devctl up --live es --live-env prod     # terminal 2
./devctl restart api
```

`up` checks that the key works and that it cannot write, and refuses to start
otherwise. Nothing in this setup writes to prod: your prompt and settings are
stored in the local stand-in.

OPTIONAL alternative: **Local sample instead** (no cluster access needed, faster previews, but
fewer posts and from early August):

```bash
./devctl up                # also how you go back from prod to local
./devctl seed              # only if `up` warns the data is old (5–10 min)
./devctl restart api
```

## 5. Check before opening the browser

Give the api a minute to install its packages, then:

```bash
./devctl exec api printenv GE_ANTHROPIC_API_KEY | cut -c1-10     # sk-ant-api
curl -s http://127.0.0.1:8300/openapi.json | grep -o 'llm-query-vectors/[a-z]*' | sort -u
#   llm-query-vectors/current
#   llm-query-vectors/fit
```

If the second check prints nothing, the api is still starting: `./devctl logs api | tail`
until you see `Application startup complete`.

## 6. Sign in as yourself

```bash
./devctl login <your did>        # prints a link; open it
```

There is no password because this never touches Bluesky: the local stand-in
for sign-in trusts devctl, and it exists only on your machine. The link lasts
an hour; once opened, you stay signed in. Plain `./devctl login` signs in as a
sample user instead.

## 7. Use it

1. Open http://127.0.0.1:3000/#/settings/your-feed
2. In the **Prompt** card, type a prompt and press **Send**. "Fitting…" for ~10 s.
3. The Prompt slider wakes up at 20%. Drag it higher to see more prompt posts.
4. Press **Preview** (on prod, ~10 s). Posts with the peach **Prompt** badge came from your prompt.

Stuck? `./devctl doctor` checks everything and names the fix.
