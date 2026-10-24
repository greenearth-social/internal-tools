import io
import json
import tarfile
from pathlib import Path

import pytest

from devctlib import assets, http
from devctlib.ctx import Die
from devctlib.runner import Result
from devctlib.testing import FakeRunner, fake, make_ctx


def tar_of(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as archive:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def downloads(payload: bytes | None):
    """A runner whose curl writes payload to its -o path (or fails)."""

    def respond(argv):
        if argv[0] != "curl":
            return None
        if payload is None:
            return Result(22)
        Path(argv[argv.index("-o") + 1]).write_bytes(payload)
        return None

    return FakeRunner(respond)


MODELS = {
    "manifest.json": json.dumps(
        {
            "tag": "models-x",
            "content_embedding_model": "m",
            "content_embedding_dims": 384,
            "output_embedding_dim": 128,
        }
    ),
    "towers/a.pt": "weights",
}


def test_fetch_models_replaces_the_bundle(tmp_path, capsys):
    ctx = make_ctx(
        tmp_path, runner=downloads(tar_of(MODELS)), env={"GE_DEV_MODELS_TAG": "models-x"}
    )
    stale = ctx.devenv / "models" / "data" / "old.pt"
    stale.parent.mkdir(parents=True)
    stale.write_text("old")

    assets.cmd_fetch_models(ctx, [])

    assert (ctx.devenv / "models/data/towers/a.pt").read_text() == "weights"
    assert not stale.exists()
    url = fake(ctx).calls[0].argv[3]
    assert url == (
        "https://github.com/greenearth-social/internal-tools/releases/download/"
        "models-x/ge-dev-models.tar"
    )
    out = capsys.readouterr().out
    assert "fetched models models-x — m (384d) in, 128d out" in out
    assert "Next: devctl up && devctl seed" in out


def test_a_failed_download_leaves_the_bundle_alone(tmp_path):
    ctx = make_ctx(tmp_path, runner=downloads(None))
    kept = ctx.devenv / "models" / "data" / "manifest.json"
    kept.parent.mkdir(parents=True)
    kept.write_text("{}")

    with pytest.raises(Die, match="download failed"):
        assets.cmd_fetch_models(ctx, [])
    assert kept.read_text() == "{}"


def test_a_torn_archive_leaves_the_bundle_alone(tmp_path):
    ctx = make_ctx(tmp_path, runner=downloads(b"not a tar"))
    with pytest.raises(Die, match="archive is unreadable"):
        assets.cmd_fetch_models(ctx, [])
    assert not (ctx.devenv / "models" / "data").exists()


def test_archives_cannot_write_outside_the_destination(tmp_path):
    ctx = make_ctx(tmp_path, runner=downloads(tar_of({"../escape": "x"})))
    with pytest.raises(Die, match="archive is unreadable"):
        assets.cmd_fetch_models(ctx, [])
    assert not (ctx.devenv / "models" / "escape").exists()


@pytest.mark.parametrize(
    ("response", "tag"),
    [
        (
            http.Response(200, json.dumps([{"tag_name": "models-1"}, {"tag_name": "fixture-9"}])),
            "fixture-9",
        ),
        (http.Response(200, "[]"), ""),
        (http.Response(403, "rate limited"), ""),
        (http.Response(0), ""),
    ],
)
def test_newest_fixture_tag(tmp_path, monkeypatch, response, tag):
    monkeypatch.setattr(http, "request", lambda url, **kw: response)
    assert assets.newest_fixture_tag(make_ctx(tmp_path)) == tag


def test_fetch_fixture_without_a_release_names_the_alternative(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "request", lambda url, **kw: http.Response(0))
    with pytest.raises(Die, match="no fixture release found in greenearth-social/internal-tools"):
        assets.cmd_fetch_fixture(make_ctx(tmp_path), [])


def test_use_ranker_passes_its_arguments_and_this_instances_paths(tmp_path, capsys):
    ctx = make_ctx(tmp_path, instance="alpha")
    ctx.runtime.mkdir(exist_ok=True)

    assets.cmd_use_ranker(ctx, ["gs://run", "--reset"])

    argv = fake(ctx).calls[-1].argv
    assert argv[2:4] == ["gs://run", "--reset"]
    assert argv[argv.index("--runtime") + 1] == str(ctx.devenv / ".runtime.alpha")
    assert "devctl restart --name alpha inference" in capsys.readouterr().out
