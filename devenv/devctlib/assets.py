"""The trained models and the fixture set: what's here, and whether they agree."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .compose import RANKER_OVERRIDE
from .ctx import Ctx


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
