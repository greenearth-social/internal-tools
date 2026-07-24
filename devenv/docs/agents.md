# Using the environment as a coding-agent sandbox

The devenv was built with agents in mind: everything runs through one
wrapper (`devctl`), no language toolchain exists on the host, the data is
disposable, and the guardrails that matter are enforced in code rather than
written in a doc. This page is how to set an agent loose on it — and how
loose you can afford to make it.

## What the agent should know

There's a Claude Code skill at `internal-tools/.claude/skills/devenv/`
covering the working knowledge: check what's running before starting
anything, run tests with `devctl test`, restart instead of expecting reload,
`--name` for parallel instances, and the traps (stale seeds, model changes,
feeds before seeding).

Claude Code discovers it automatically when a session starts in
`internal-tools` or in the parent directory holding all the checkouts. For a
session rooted in another repo:

```bash
claude --add-dir ../internal-tools
```

which loads the skill and gives the agent read access to `devctl` and these
docs.

## The permission model: allowlist one script

The reason everything routes through `devctl` is that a sandbox can then
allow *one command* instead of granting `docker`, `pipenv`, `npm`, and `go`
separately — and every capability below comes with it:

```jsonc
// .claude/settings.json (project) or settings.local.json
{
  "permissions": {
    "allow": [
      "Bash(./devctl *)",
      "Bash(devctl *)"
    ]
  }
}
```

With that one allowance an agent can run every repo's tests
(`devctl test`), run anything inside a service (`devctl exec api pipenv run
pytest -k ...`), read logs, query Elasticsearch (read-only key), render
feeds, restart services after edits, diagnose the environment
(`devctl doctor`), and rebuild the world (`nuke`/`up`/`seed`). Code edits
happen on the host checkouts with the normal file tools; `devctl` is only
how things *execute*.

## What the sandbox can and can't damage

Worth being precise about, because it's what determines how much autonomy is
reasonable:

- **Everything local is disposable.** The worst destructive command,
  `devctl nuke`, costs a ~15 minute rebuild. Fixtures and models re-download
  from public releases. There is nothing in the environment an agent can
  lose that matters.
- **Deployed systems are out of reach through devctl, enforced.** Seeding
  and ingestion address the local cluster by container name — no flag
  combination points them elsewhere. `--live es` requires a key the cluster
  itself verifies is read-only before anything starts. Firestore is always
  the emulator. A named instance sharing `dev`'s cluster is read-only
  against it, also enforced.
- **The remaining risk is keys, not commands.** `devenv.local.env` may hold
  live-service keys (read-only ES, inference, Perspective). An agent that
  can read files can read them; devctl only ever uses them read-only, but
  don't park write-capable credentials there, and don't grant `--live-env
  prod` workflows to an unattended agent. The standing rule in the skill:
  never point an environment at prod without being asked.

## Working in parallel

Give each concurrent agent its own instance **and its own working copy** —
that's the whole point of instances: several agents each editing, testing
and rendering feeds against their own code, none of them able to trip over
another's half-finished change.

Git worktrees make the working copies cheap (shared object store, no
re-clone). Put one worktree of each repo under a directory per agent, then
point the instance at it:

```bash
ROOT=~/src/ge-agent1
mkdir -p "$ROOT"
for r in api ingex frontend inference-service; do
  git -C ~/src/greenearth-social/$r worktree add "$ROOT/$r" -b agent1
done

devctl up --name agent1 --repo-root "$ROOT"   # ~90s; shares dev's ES, read-only
devctl --name agent1 test api
devctl --name agent1 feed
devctl nuke --name agent1                     # when the task is done
```

`--repo-root` is remembered per instance: later commands and later `up`s act
on the same working copy without repeating the flag, `devctl ls` shows which
instance mounts what, and `devctl nuke` forgets it. A plain clone of the
repos works exactly the same way — worktrees are just the cheap way to get
one.

Named instances cost ~1GB each (they read `dev`'s cluster rather than
running their own), so a well-provisioned machine holds several. Two
caveats: instances sharing one working copy also share its frontend
`functions/lib` build (two frontend branches would overwrite each other —
separate `--repo-root`s dissolve this), and a sharer can't seed — data
changes happen on `dev`.

For agent farms, run the instances on a remote Linux host
([remote.md](remote.md)) — same setup, more memory, and your laptop stays
out of the blast radius of an enthusiastic test suite.

## The autonomous loop

The shape that works, and that the skill teaches:

1. `devctl status` / `devctl ls` — see what exists before creating anything.
2. Edit code on the host → `devctl restart <service>` (deliberate restarts
   beat `--watch`, which can reload the service mid-request under the agent).
3. `devctl test <repo>` for the suite, `devctl exec` for one file or a
   linter.
4. `devctl feed` to see the actual product outcome of a ranking change —
   per-post generator and score detail, not just green tests.
5. When the environment misbehaves: `devctl doctor`, which names the fix —
   including the stale-seed trap that otherwise reads as "my change broke
   the feed".

Point an agent at this doc and the skill, allowlist `devctl`, and the rest
of the environment's design — enforced read-only boundaries, disposable
data, one entry point — does the containment work.
