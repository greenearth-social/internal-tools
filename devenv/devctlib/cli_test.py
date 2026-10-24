import json
import subprocess

import pytest

from devctlib import cli
from devctlib.ctx import Die
from devctlib.testing import Sandbox, devctl_argv


@pytest.mark.parametrize(
    ("argv", "instance", "command", "args"),
    [
        ([], "dev", "", []),
        (["up"], "dev", "up", []),
        (["--name", "alpha", "up", "--watch"], "alpha", "up", ["--watch"]),
        (["up", "--watch", "--name", "alpha"], "alpha", "up", ["--watch"]),
        # Everything after exec belongs to the command in the container.
        (["exec", "api", "x", "--name", "y"], "dev", "exec", ["api", "x", "--name", "y"]),
        (["--name", "a", "exec", "api", "x"], "a", "exec", ["api", "x"]),
    ],
)
def test_parse_global(argv, instance, command, args):
    inv = cli.parse_global(argv, "dev")
    assert (inv.instance, inv.command, inv.args) == (instance, command, args)


def test_parse_global_needs_a_name_after_name():
    with pytest.raises(Die, match="--name needs an instance name"):
        cli.parse_global(["up", "--name"], "dev")


@pytest.mark.parametrize("argv", [[], ["help"], ["-h"], ["--help"], ["--name", "alpha", "help"]])
def test_help_prints_usage(tmp_path, capsys, argv):
    assert cli.run(argv, devenv=tmp_path, environ={}) == 0
    assert "Usage: devctl" in capsys.readouterr().out


def test_an_invalid_instance_name_is_refused(tmp_path, capsys):
    assert cli.main(["--name", "Bad", "help"], str(tmp_path)) == 1
    assert "invalid instance name: 'Bad'" in capsys.readouterr().err


def test_local_env_picks_the_instance(tmp_path, capsys):
    (tmp_path / "devenv.local.env").write_text("GE_DEV_INSTANCE=Nope\n")
    assert cli.main(["help"], str(tmp_path)) == 1
    assert "'Nope'" in capsys.readouterr().err


def test_unported_commands_reach_the_bash_original_untouched(tmp_path):
    sandbox = Sandbox(tmp_path)
    legacy = sandbox.devenv / cli.LEGACY
    legacy.write_text("#!/usr/bin/env python3\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    legacy.chmod(0o755)
    argv = ["--name", "alpha", "nosuchcommand", "--flag", "a b"]

    result = subprocess.run(devctl_argv(sandbox.devenv, *argv), capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == argv


def test_instances_is_ls_even_for_an_unknown_name(tmp_path, capsys):
    """The bash version resolved ports for `instances` (not for `ls`), so an
    unknown --name made the alias fail."""
    assert cli.main(["--name", "ghost", "instances"], str(tmp_path)) == 0
    assert "(no instances yet" in capsys.readouterr().out
