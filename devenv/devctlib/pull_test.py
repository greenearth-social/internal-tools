"""devctl pull over real git repositories in a scratch directory."""

import os
import subprocess

import pytest

from devctlib import pull
from devctlib.ctx import Die
from devctlib.runner import Runner
from devctlib.testing import make_ctx

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(path, *args):
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        env={**os.environ, **GIT_ENV},
    )


def commit(path, name):
    (path / name).write_text(name)
    git(path, "add", name)
    git(path, "commit", "-q", "-m", name)


@pytest.fixture
def root(tmp_path):
    """Repos in each state pull has to tell apart, cloned from local remotes."""
    remotes = tmp_path / "remotes"
    root = tmp_path / "root"
    root.mkdir()
    for repo in ("api", "ingex", "frontend", "inference-service", "bridg-eu"):
        remote = remotes / repo
        remote.mkdir(parents=True)
        git(remote, "init", "-q", "-b", "main")
        commit(remote, "one")
        git(root, "clone", "-q", str(remote), repo)
    commit(remotes / "api", "two")  # api: behind by one
    commit(remotes / "api", "three")
    (root / "ingex" / "scratch").write_text("dirty")  # ingex: dirty
    commit(root / "frontend", "local")  # frontend: diverged
    commit(remotes / "frontend", "remote")
    git(root / "inference-service", "checkout", "-q", "-b", "feature")  # no upstream
    return root


def test_pull_reports_each_state(tmp_path, root, capsys):
    ctx = make_ctx(tmp_path, env={**GIT_ENV, "GE_DEV_REPO_ROOT": str(root)}, runner=Runner())

    pull.cmd_pull(ctx, [])

    table = capsys.readouterr().out.split("\n\n")[0]
    rows = {line.split()[0]: line for line in table.splitlines()}
    assert rows["api"].endswith("pulled 2 commit(s)")
    assert rows["ingex"].endswith("skipped: uncommitted changes")
    assert rows["frontend"].endswith("skipped: diverged (needs manual merge)")
    assert rows["inference-service"].endswith("skipped: no upstream")
    assert rows["bridg-eu"].endswith("up to date")
    assert rows["inference-service"].split()[1] == "feature"
    assert (root / "api" / "three").exists()


def test_pull_names_branches_off_main_and_another_root(tmp_path, root, capsys):
    ctx = make_ctx(tmp_path, env=GIT_ENV, runner=Runner())

    pull.cmd_pull(ctx, ["--repo-root", str(root)])

    out = capsys.readouterr().out
    assert "on a branch other than main:\n  inference-service (feature)" in out
    assert f"(repo root: {root})" in out


def test_a_failed_fetch_exits_nonzero(tmp_path, root, capsys):
    git(root / "api", "remote", "set-url", "origin", str(tmp_path / "gone"))
    ctx = make_ctx(tmp_path, env={**GIT_ENV, "GE_DEV_REPO_ROOT": str(root)}, runner=Runner())

    with pytest.raises(SystemExit) as exc:
        pull.cmd_pull(ctx, [])

    assert exc.value.code == 1
    assert "error: fetch failed" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--bogus"], "usage: devctl pull"),
        (["--repo-root"], "--repo-root needs a directory"),
        (["--repo-root", "/no/such/dir"], "no such directory: /no/such/dir"),
    ],
)
def test_pull_arguments(tmp_path, args, message):
    with pytest.raises(Die, match=message):
        pull.cmd_pull(make_ctx(tmp_path), args)
