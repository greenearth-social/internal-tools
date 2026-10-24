import json

import pytest

from devctlib import stack
from devctlib.ctx import Die
from devctlib.testing import FakeRunner, fake, make_ctx


def with_models(ctx):
    data = ctx.devenv / "models" / "data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "manifest.json").write_text(json.dumps({"tag": "t"}))


def docker_on_path(ctx, tmp_path):
    """check_docker looks for the binary itself before asking the runner."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text("")
    (bin_dir / "docker").chmod(0o755)
    ctx.env["PATH"] = str(bin_dir)


def with_repos(tmp_path, *repos):
    for repo in ("api", "ingex", "frontend", "inference-service", *repos):
        (tmp_path / repo / ".git").mkdir(parents=True)


@pytest.mark.parametrize(
    ("bridgeu", "built"), [(False, "seed-firestore-users"), (True, "bridgeu-api")]
)
def test_setup_builds_pulls_and_installs(tmp_path, capsys, bridgeu, built):
    ctx = make_ctx(tmp_path)
    docker_on_path(ctx, tmp_path)
    with_repos(tmp_path, *(["bridg-eu"] if bridgeu else []))
    with_models(ctx)

    stack.cmd_setup(ctx, [])

    calls = fake(ctx).compose_lines()
    assert calls[0].startswith("--profile tools build es-init api inference")
    assert calls[0].endswith(built)
    assert calls[1] == (
        "--profile tools pull elasticsearch perspective-stub dev-auth seed-rebase"
        " seed-megastream --ignore-buildable"
    )
    assert calls[2] == "run --rm --no-deps inference bash /inference/start.sh --install-only"
    captured = capsys.readouterr()
    assert "setup complete" in captured.out
    assert "No fixture set found" in captured.err


def test_setup_stops_if_the_inference_install_fails(tmp_path):
    ctx = make_ctx(
        tmp_path, runner=FakeRunner(lambda argv: 1 if "--install-only" in argv else None)
    )
    docker_on_path(ctx, tmp_path)
    with_repos(tmp_path)
    with_models(ctx)

    with pytest.raises(Die, match="failed to install inference-service dependencies"):
        stack.cmd_setup(ctx, [])
