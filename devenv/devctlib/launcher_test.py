"""The bash launcher: finding a Python, keeping devctl's venv, starting devctl.

These build real virtualenvs (a few seconds each), over a stub devctlib and a
requirements file with nothing to install, so no network is needed.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from devctlib.testing import DEVENV_DIR

STUB_CLI = """\
import sys


def main(argv, devenv):
    print("argv", argv)
    print("devenv", devenv)
    print("python", sys.version_info[:2])
    return 7
"""


@pytest.fixture
def devenv(tmp_path) -> Path:
    devenv = tmp_path / "devenv"
    (devenv / "devctlib").mkdir(parents=True)
    shutil.copy2(DEVENV_DIR / "devctl", devenv / "devctl")
    (devenv / "devctlib" / "__init__.py").touch()
    (devenv / "devctlib" / "cli.py").write_text(STUB_CLI)
    (devenv / "devctl-requirements.txt").write_text("# nothing yet\n")
    return devenv


def launch(devenv: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    # The interpreter running the tests is a 3.13+ — put it first on PATH.
    bin_dir = devenv.parent / "bin"
    bin_dir.mkdir(exist_ok=True)
    python = bin_dir / "python3"
    if not python.exists():
        python.symlink_to(sys.executable)
    return subprocess.run(
        [str(devenv / "devctl"), *args],
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(devenv.parent), **env},
        cwd=devenv.parent,
    )


def test_launcher_builds_its_venv_once_and_passes_everything_through(devenv):
    first = launch(devenv, "up", "--name", "a b")
    second = launch(devenv, "status")

    assert first.returncode == 7, first.stderr
    assert "preparing devctl's virtualenv" in first.stderr
    assert "argv ['up', '--name', 'a b']" in first.stdout
    assert f"devenv {devenv}" in first.stdout
    assert second.returncode == 7, second.stderr
    assert "preparing" not in second.stderr
    assert "argv ['status']" in second.stdout
    assert (devenv / ".venv-devctl" / "bin" / "python").exists()


def test_launcher_resyncs_when_requirements_change(devenv):
    launch(devenv)
    (devenv / "devctl-requirements.txt").write_text("# nothing yet, still\n")

    result = launch(devenv)

    assert "preparing devctl's virtualenv" in result.stderr
    assert not list(devenv.glob(".venv-devctl.tmp.*"))


def test_launcher_ignores_the_working_directory(devenv):
    # python -c would otherwise import this instead of the real package.
    (devenv.parent / "devctlib").mkdir()
    (devenv.parent / "devctlib" / "__init__.py").write_text("raise SystemExit('wrong one')\n")

    result = launch(devenv)

    assert result.returncode == 7, result.stderr


def test_launcher_names_a_python_that_is_too_old(devenv):
    old = devenv.parent / "old-python"
    old.write_text("#!/bin/sh\nexit 1\n")
    old.chmod(0o755)

    result = launch(devenv, GE_DEV_PYTHON=str(old))

    assert result.returncode == 1
    assert f"GE_DEV_PYTHON={old} isn't a Python 3.13 or newer" in result.stderr


def test_launcher_explains_when_no_python_is_new_enough(devenv):
    result = subprocess.run(
        ["/bin/bash", str(devenv / "devctl")],
        capture_output=True,
        text=True,
        env={"PATH": str(devenv.parent / "nothing-here"), "HOME": str(devenv.parent)},
    )

    assert result.returncode == 1
    assert "devctl needs Python 3.13 or newer, and found none" in result.stderr
