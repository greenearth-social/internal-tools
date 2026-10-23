"""The port against the bash original, command by command.

Each case runs `devctl <args>` through devctl-legacy and through devctlib, in
identical sandboxes with fake docker/kubectl/gcloud and a fake Elasticsearch,
and checks they did the same thing (see testing.assert_parity). Removed with
devctl-legacy once everything is ported.
"""

import pytest

from devctlib.testing import parity


@pytest.mark.parametrize("args", [(), ("help",), ("-h",), ("--help",), ("--name", "alpha", "help")])
def test_help(tmp_path, args):
    parity(tmp_path, *args, phrases=["Usage: devctl", "use-ranker"])


def test_invalid_instance_name(tmp_path):
    parity(tmp_path, "--name", "Bad", "help", phrases=["invalid instance name: 'Bad'"])


def test_name_needs_a_value(tmp_path):
    parity(tmp_path, "help", "--name", phrases=["--name needs an instance name"])
