"""The trained models and the fixture set: what's here, and whether they agree."""

from __future__ import annotations

import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

from . import http
from .compose import RANKER_OVERRIDE
from .ctx import Ctx, die, warn


def models_manifest(ctx: Ctx) -> Path:
    return ctx.devenv / "models" / "data" / "manifest.json"


def fixtures_manifest(ctx: Ctx) -> Path:
    return ctx.devenv / "fixtures" / "data" / "manifest.json"


def have_models(ctx: Ctx) -> bool:
    return models_manifest(ctx).is_file()


def have_fixtures(ctx: Ctx) -> bool:
    return fixtures_manifest(ctx).is_file()


def models_tag(ctx: Ctx) -> str:
    return json.loads(models_manifest(ctx).read_text())["tag"]


def fixture_counts(ctx: Ctx) -> str:
    counts = json.loads(fixtures_manifest(ctx).read_text())["counts"]
    return f"{counts['posts']:,} posts / {counts['likes']:,} likes"


def python_tool(ctx: Ctx, script: str, *args: str) -> list[str]:
    """Run one of devenv's own Python scripts with devctl's interpreter."""
    return [sys.executable, str(ctx.devenv / script), *args]


def compat_argv(ctx: Ctx) -> list[str]:
    """models/check_compat.py over the two manifests; it explains why the
    towers and the fixture's embeddings have to agree."""
    return python_tool(
        ctx, "models/check_compat.py", str(models_manifest(ctx)), str(fixtures_manifest(ctx))
    )


def models_agree(ctx: Ctx) -> bool:
    return ctx.run(compat_argv(ctx), check=False, quiet=True, quiet_err=True).ok


def check_model_fixture_compat(ctx: Ctx) -> None:
    """Stop if the models, the fixture and any selected ranker disagree.

    A local ranker can be selected before either bundle arrives, so it's
    rechecked against whatever is now available. The tower/fixture check
    needs both bundles.
    """
    # The checkers print why; devctl just stops.
    if (ctx.runtime / RANKER_OVERRIDE).is_file():
        ranker_check = python_tool(
            ctx,
            "models/use_ranker.py",
            "--check",
            "--runtime",
            str(ctx.runtime),
            "--models-manifest-path",
            str(models_manifest(ctx)),
            "--fixture-manifest-path",
            str(fixtures_manifest(ctx)),
        )
        if not ctx.run(ranker_check, check=False).ok:
            raise SystemExit(1)
    if have_models(ctx) and have_fixtures(ctx) and not ctx.run(compat_argv(ctx), check=False).ok:
        raise SystemExit(1)


# --- downloads -----------------------------------------------------------------
# Deliberately the public releases of public repos, fetched without the gh CLI:
# a fresh clone gets what it needs with no GitHub account, no auth, and no
# extra tooling installed.


def fixture_repo(ctx: Ctx) -> str:
    return ctx.get("GE_DEV_FIXTURE_REPO", "greenearth-social/internal-tools")


FIXTURE_ASSET = "ge-dev-fixture.tar"
MODELS_ASSET = "ge-dev-models.tar"
# Pinned, unlike the fixture, which tracks the newest release. Model artifacts
# and seeded post embeddings have to agree — the towers only make sense over
# embeddings produced by the matching post tower — so everyone runs the same
# models until someone deliberately moves this. The tag names the two training
# runs in the bundle (two-tower, ranker); models/publish_models.sh derives it
# from the artifacts and prints what to set here.
MODELS_TAG = "models-1bf1ea3c-177899dd"


def models_tag_pinned(ctx: Ctx) -> str:
    return ctx.get("GE_DEV_MODELS_TAG", MODELS_TAG)


def fetch_release_asset(ctx: Ctx, tag: str, asset: str, dest: Path, size: str) -> None:
    """Download a release asset and replace dest with its contents."""
    if shutil.which("curl", path=ctx.env.get("PATH")) is None:
        die(f"curl is required to download {asset}")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        print(f"devctl: downloading {tag} ({size})...")
        url = f"https://github.com/{fixture_repo(ctx)}/releases/download/{tag}/{asset}"
        if not ctx.run(
            ["curl", "-fL", "--progress-bar", url, "-o", str(work / asset)], check=False
        ).ok:
            die(
                "download failed — check network access to github.com,"
                f" and that {tag} exists in {fixture_repo(ctx)}"
            )
        # Extract into a scratch directory first so a torn archive can't leave
        # the destination holding half of one release and half of another.
        extracted = work / "x"
        try:
            with tarfile.open(work / asset) as archive:
                archive.extractall(extracted, filter=without_mac_metadata)
        except (tarfile.TarError, OSError):
            die(f"{tag}: archive is unreadable")
        shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(extracted, dest)


def without_mac_metadata(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
    """tarfile's safe "data" filter, minus macOS metadata.

    A release tarred on a Mac carries an AppleDouble `._<name>` beside each
    file. macOS's own tar folds those back into extended attributes, but
    anything else extracts them as files — and `._x.db.zip` then matches the
    seed's `*.db.zip` and fails as "file is not a database".
    """
    name = Path(member.name)
    if name.name.startswith("._") or "__MACOSX" in name.parts:
        return None
    return tarfile.data_filter(member, dest)


def newest_fixture_tag(ctx: Ctx) -> str:
    """The newest fixture release — a fixture is a sample of Bluesky rather than
    something the rest of the stack has to agree with, so unlike the models
    it isn't pinned."""
    response = http.request(
        f"https://api.github.com/repos/{fixture_repo(ctx)}/releases?per_page=50", timeout=30
    )
    if response.status != 200:
        return ""
    try:
        releases = json.loads(response.body)
    except ValueError:
        return ""
    tags = [
        r["tag_name"]
        for r in releases
        if isinstance(r, dict) and str(r.get("tag_name", "")).startswith("fixture-")
    ]
    return tags[0] if tags else ""


def fixtures_hint(ctx: Ctx) -> None:
    warn(
        "\n"
        "No fixture set found in devenv/fixtures/data/. Get one:\n"
        "\n"
        "  # Download the published sample (what you usually want):\n"
        "  devctl fetch-fixture\n"
        "\n"
        "  # Or generate your own from real data (needs read-only prod ES,\n"
        "  # port-forwarded — see README):\n"
        f"  python3 {ctx.devenv}/fixtures/generate_sample.py --source prod-es\n"
        "\n"
        "  # Or without any credentials (uses ingex's checked-in sample files\n"
        "  # and synthesizes the like graph):\n"
        f"  python3 {ctx.devenv}/fixtures/generate_sample.py --source megastream-files\n"
        "\n"
        "Then run: devctl seed"
    )


def models_hint(ctx: Ctx) -> None:
    warn(
        "\n"
        "No model artifacts found in devenv/models/data/. Get them:\n"
        "\n"
        "  devctl fetch-models\n"
        "\n"
        f"That downloads the pinned release ({models_tag_pinned(ctx)}, ~33MB) — no account or\n"
        "credentials needed. `devctl setup` does it for you."
    )


def cmd_fetch_fixture(ctx: Ctx, args: list[str]) -> None:
    tag = newest_fixture_tag(ctx)
    if not tag:
        die(
            f"no fixture release found in {fixture_repo(ctx)}"
            " (or GitHub is unreachable / rate-limiting).\n"
            "Generate a set locally instead — see: devctl help"
        )
    # Replace rather than merge: mixing chunks from different samples would
    # give the seed an inconsistent post/like graph.
    fetch_release_asset(ctx, tag, FIXTURE_ASSET, fixtures_manifest(ctx).parent, "~170MB")
    manifest = json.loads(fixtures_manifest(ctx).read_text())
    counts = manifest["counts"]
    print(
        f"devctl: fetched {counts['posts']:,} posts / {counts['likes']:,} likes "
        f"(window ends {manifest['window_end']}, source {manifest['source']})"
    )
    ctx.hint("Next: devctl up && devctl seed")


def cmd_fetch_models(ctx: Ctx, args: list[str]) -> None:
    # Same public-release channel as the fixture, and for the same reason: a
    # fresh clone gets working trained models with no Google account, no GCS
    # credentials and no ClearML. Replace rather than merge: the serving
    # manifests name specific weight files, and a half-updated directory would
    # load one release's towers against another's author index map.
    fetch_release_asset(
        ctx, models_tag_pinned(ctx), MODELS_ASSET, models_manifest(ctx).parent, "~33MB"
    )
    m = json.loads(models_manifest(ctx).read_text())
    print(
        f"devctl: fetched models {m['tag']} — {m['content_embedding_model']} "
        f"({m['content_embedding_dims']}d) in, {m['output_embedding_dim']}d out"
    )
    check_model_fixture_compat(ctx)
    ctx.hint("Next: devctl up && devctl seed")


def cmd_use_ranker(ctx: Ctx, args: list[str]) -> None:
    # Preparation is independent of Docker and port allocation: developers can
    # select a model before starting an instance for the first time.
    ctx.run(
        python_tool(
            ctx,
            "models/use_ranker.py",
            *args,
            "--runtime",
            str(ctx.runtime),
            "--models-manifest-path",
            str(models_manifest(ctx)),
            "--fixture-manifest-path",
            str(fixtures_manifest(ctx)),
        )
    )
    print("devctl: selection applies on the next local inference start.")
    print(f"        Start the stack: devctl up{ctx.name_flag()}")
    print(f"        Apply to a running service: devctl restart{ctx.name_flag()} inference")
