"""Runs the Emulator UI proxy's node tests, when node is installed."""

import shutil
import subprocess
from pathlib import Path

import pytest

FIREBASE_UI_PROXY_TEST = Path(__file__).parent / "ui-proxy_test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_firebase_ui_proxy_rewrites_named_instance_ports():
    result = subprocess.run(
        ["node", "--test", str(FIREBASE_UI_PROXY_TEST)],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
