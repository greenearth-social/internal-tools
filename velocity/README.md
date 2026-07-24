# Velocity & backlog projection

Pivotal-Tracker-style velocity tracking and backlog projection for GitHub
Project [#2](https://github.com/orgs/greenearth-social/projects/2/views/3). See
issue [#1](https://github.com/greenearth-social/internal-tools/issues/1).

- `velocity/` — tested library. All computation lives here:
  - `points.py` — point/type normalization (blank type → feature, bugs → 0
    points, unpointed → 2 points).
  - `velocity.py` — weekly completed points and the 3-week velocity average.
  - `burndown.py` — completion history, forward burndown projection, and which
    week each backlog task is projected to finish.
  - `github_project.py` — the only I/O: fetches project items via `gh api
    graphql`.
- `notebooks/velocity.ipynb` — presentation only (charts + projection table).

Run everything below from this directory (`internal-tools/velocity/`). The
Python environment is shared across `internal-tools`, so `pipenv` commands work
from here or from the repo root.

## Setup

```bash
pipenv install --dev

# One-time: the gh CLI needs the project scope to read Projects v2 data.
gh auth refresh -s read:project
```

The library shells out to the `gh` CLI, so `gh` must be installed and
authenticated.

## Run the tests

```bash
pipenv run pytest velocity
```

Tests are co-located with the source (`*_test.py`) and run entirely on fixture
data — no network access required.

## Run the notebook

```bash
pipenv run jupyter notebook notebooks/velocity.ipynb
```

The notebook has `%autoreload 2` at the top, so edits to the `velocity` library
are picked up on the next cell run without restarting the kernel.

## Editing the notebook

Keep logic in the library (it's unit-tested); the notebook should stay
presentation-only. To verify a change headlessly without a running kernel:

```bash
pipenv run jupyter nbconvert --to notebook --execute \
  --output /tmp/velocity-check.ipynb notebooks/velocity.ipynb
```

Note that `gh` colorizes JSON when it detects a color-forcing environment (some
Jupyter kernels set `CLICOLOR_FORCE`), which would break parsing — the fetcher
in `github_project.py` forces color off to avoid this.

## Assumptions

- A task is completed when its Status is `Done`; the week it counts toward is
  taken from the underlying issue's `closedAt` timestamp. Tasks closed as "not
  planned" (wontfix) or "duplicate" are excluded — they delivered no work, so
  their points don't count toward velocity.
- Points come from the project's single-select `Points` field (Fibonacci
  options). A task's type is GitHub's native issue type (e.g. `Bug`); items with
  no issue type are treated as features.
- Velocity is the average completed points over the last 3 completed weeks (the
  current partial week is excluded).
- The backlog matches the **Backlog - Unified** view (project view #13): every
  item whose Status is `Backlog`, `In Progress`, `In Review`, or `On Hold`,
  across all repos.
- Backlog items are read in the GraphQL API's board order, which is treated as
  priority order. Per-view (view #13) custom sorting is not exposed by the API.
- Titles containing a release-marker emoji (🚀 rocket, ✅/✔/☑ check, or 🚢/🛳
  ship) are **release markers**, not tasks: they carry 0 points and are excluded
  from the task list. Each marker's projected release date comes from its
  position in the backlog (when all higher-priority work is projected to finish)
  and is shown as a vertical line on the burndown plus a row in the release
  table.
