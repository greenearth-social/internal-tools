"""Live services: pointing individual dependencies at deployed ones.

Everything runs locally by default. `--live <svc>[,<svc>]` (or GE_DEV_LIVE in
devenv.local.env) points individual dependencies at deployed ones instead, for
the cases where the local stand-in can't reproduce what you're chasing.

  devctl up --live inference,perspective
  devctl up --live es --live-env prod

`up` is the only command that decides: it reads the flag, falling back to
devenv.local.env, and records the result in .runtime/. Every other command
reads that record, so `seed` and `status` act on the environment that is
actually running. Deciding this way means a bare `devctl up` always returns to
all-local, instead of quietly inheriting a live service from hours ago.
"""

from __future__ import annotations

import json
import re
import shutil
import time

from . import bsky, eurosky, http
from .bridgeu import BRIDGEU_LOCAL_MYSKY_API_KEY, BRIDGEU_LOCAL_MYSKY_API_URL
from .ctx import Ctx, die, warn
from .envfiles import write_env_file

# `mysky` is the odd one out: not something the api calls, but the mysky api
# itself as BridgEU sees it. Live, bridgeu-api calls a deployed api with a real
# partner key instead of the local api with the key devctl issues it.
LIVE_SERVICES = ("es", "inference", "perspective", "mysky")

# Which variable holds each live service's key.
KEY_VARS = {
    "inference": "GE_DEV_INFERENCE_API_KEY",
    "perspective": "GE_PERSPECTIVE_API_KEY",
    "es": "GE_DEV_ES_API_KEY",
    "mysky": "GE_DEV_MYSKY_API_KEY",
}


def live_services(ctx: Ctx) -> list[str]:
    return [s for s in re.split(r"[,\s]+", ctx.live) if s]


def live_url(ctx: Ctx, service: str) -> str:
    """Where each live service actually is — kept here so nobody has to
    remember a Cloud Run hostname to use one."""
    env = ctx.live_env
    if service == "inference":
        return {
            "stage": "https://inference-stage.greenearth.social",
            "prod": "https://inference.greenearth.social",
        }.get(env, "")
    if service == "perspective":
        return "https://commentanalyzer.googleapis.com"
    if service == "es":
        # Reached through the tunnel `devctl tunnel` opens: the cluster's ES
        # sits behind an internal load balancer, so there's no address a
        # container could dial directly. host.docker.internal is the host
        # running kubectl.
        return f"https://host.docker.internal:{tunnel_port(ctx)}"
    if service == "mysky" and env == "prod":
        # The stage api has no public hostname; GE_DEV_BRIDGEU_MYSKY_API_URL
        # can point at one you can reach.
        return "https://api.greenearth.social"
    return ""


def tunnel_port(ctx: Ctx) -> str:
    return ctx.get("GE_DEV_ES_TUNNEL_PORT", "9200")


def resolve_live(ctx: Ctx) -> None:
    """Turn GE_DEV_LIVE into the GE_DEV_*_URL variables docker-compose.yml
    reads, after checking that each service named there is real and has a
    key. Failing here beats failing as a 401 inside a container ten seconds
    later."""
    # Environment first: a typo'd --live-env would otherwise be reported as
    # whichever key happened to be missing, which reads as an unrelated problem.
    if ctx.live_env not in ("stage", "prod"):
        die(f"--live-env must be stage or prod (got: {ctx.live_env})")

    for service in live_services(ctx):
        if service not in LIVE_SERVICES:
            die(f"unknown live service: {service} (choose from: {' '.join(LIVE_SERVICES)})")
        key_var = KEY_VARS[service]
        if not ctx.get(key_var):
            die(f"--live {service} needs {key_var} — set it in devenv.local.env")

    # GE_DEV_INFERENCE_URL doubles as the escape hatch for an inference service
    # that isn't one of ours — one you're running by hand, say. Setting it
    # directly skips the lookup but still routes the api away from local.
    if ctx.live_has("inference"):
        ctx.export(
            "GE_DEV_INFERENCE_URL", ctx.get("GE_DEV_INFERENCE_URL") or live_url(ctx, "inference")
        )
    if ctx.live_has("perspective"):
        ctx.export("GE_DEV_PERSPECTIVE_HOST", live_url(ctx, "perspective"))
    if ctx.live_has("es"):
        ctx.export("GE_DEV_ES_URL", live_url(ctx, "es"))

    # Exported either way, because bridgeu-api's compose entry has no default
    # key. GE_DEV_BRIDGEU_MYSKY_API_URL overrides only the live target (a stage
    # api, say): the local key opens nothing but the local api.
    if ctx.live_has("mysky"):
        url = ctx.get("GE_DEV_BRIDGEU_MYSKY_API_URL") or live_url(ctx, "mysky")
        if not url:
            die(
                f"--live mysky has no known {ctx.live_env} endpoint — use --live-env prod,\n"
                "        or set GE_DEV_BRIDGEU_MYSKY_API_URL in devenv.local.env"
            )
        ctx.export("GE_DEV_BRIDGEU_MYSKY_API_URL", url)
        ctx.export("GE_DEV_BRIDGEU_MYSKY_API_KEY", ctx.get("GE_DEV_MYSKY_API_KEY"))
    else:
        ctx.export("GE_DEV_BRIDGEU_MYSKY_API_URL", BRIDGEU_LOCAL_MYSKY_API_URL)
        ctx.export("GE_DEV_BRIDGEU_MYSKY_API_KEY", BRIDGEU_LOCAL_MYSKY_API_KEY)
    # Everything that recreates a service comes through here (up, restart), so
    # an active bsky or eurosky session survives the restart after an edit.
    bsky.export_session_env(ctx)
    eurosky.export_session_env(ctx)

    if ctx.live_env == "prod" and ctx.live:
        warn(f"devctl: WARNING — using PROD services: {ctx.live}")


def record_live_choice(ctx: Ctx) -> None:
    write_env_file(ctx.live_state, {"GE_DEV_LIVE": ctx.live, "GE_DEV_LIVE_ENV": ctx.live_env})


def probe_url(url: str) -> str:
    """A URL as the host sees it. host.docker.internal is how a *container*
    names the host, and it doesn't resolve on the host itself — so probing it
    verbatim would report every tunnel as closed. Since that name only ever
    appears in the tunnel URL, and a tunnel is a host-local listener,
    127.0.0.1 is the same endpoint seen from here."""
    return url.replace("host.docker.internal", "127.0.0.1")


def preflight_live(ctx: Ctx) -> None:
    """Reach each live service once, from the host, before anything depends on
    it. A bad key or a closed tunnel otherwise shows up as feeds that 500 with
    the reason buried in a container log."""
    if ctx.live_has("inference"):
        url = ctx.get("GE_DEV_INFERENCE_URL")
        code = http.status(
            f"{probe_url(url)}/ready",
            headers={"X-API-Key": ctx.get("GE_DEV_INFERENCE_API_KEY")},
            timeout=15,
            insecure=True,
        ).code
        if code == "200":
            print(f"devctl: live inference OK ({url})")
        elif code in ("401", "403"):
            die(f"live inference rejected GE_DEV_INFERENCE_API_KEY (HTTP {code})")
        elif code == "000":
            die(f"live inference unreachable at {url}")
        else:
            die(f"live inference returned HTTP {code} from {url}/ready")
    if ctx.live_has("es"):
        url = ctx.get("GE_DEV_ES_URL")
        code = http.status(
            f"{probe_url(url)}/_cluster/health",
            headers={"Authorization": f"ApiKey {ctx.get('GE_DEV_ES_API_KEY')}"},
            timeout=15,
            insecure=True,
        ).code
        if code == "200":
            print(f"devctl: live Elasticsearch OK ({url})")
        elif code in ("401", "403"):
            die(f"live Elasticsearch rejected GE_DEV_ES_API_KEY (HTTP {code})")
        elif code == "000":
            die(
                f"live Elasticsearch unreachable at {url} — is the tunnel open?\n"
                f"        devctl tunnel {ctx.live_env}"
            )
        else:
            die(f"live Elasticsearch returned HTTP {code}")
        check_es_key_readonly(ctx)
    # GET /candidates/generators is the cheapest call that checks the key: it
    # generates nothing.
    if ctx.live_has("mysky"):
        url = ctx.get("GE_DEV_BRIDGEU_MYSKY_API_URL")
        code = http.status(
            f"{url}/candidates/generators",
            headers={"X-API-Key": ctx.get("GE_DEV_BRIDGEU_MYSKY_API_KEY")},
            timeout=15,
        ).code
        if code == "200":
            print(f"devctl: live mysky api OK ({url})")
        elif code in ("401", "403"):
            die(f"live mysky api rejected GE_DEV_MYSKY_API_KEY (HTTP {code})")
        elif code == "000":
            die(f"live mysky api unreachable at {url}")
        else:
            die(f"live mysky api returned HTTP {code} from {url}/candidates/generators")
    # Perspective isn't probed: every call costs quota, and a bad key surfaces
    # immediately as a failed feed rather than something subtle.


WRITE_PRIVILEGES = ["write", "index", "delete", "create_index", "delete_index", "manage"]


def check_es_key_readonly(ctx: Ctx) -> None:
    """Refuse a live Elasticsearch key that can write.

    `--live es` points a *development* environment at a deployed cluster, and
    everything here that writes to Elasticsearch — the seed, both ingest
    services — addresses the local one by name, so nothing is supposed to
    write there. Enforce that at the key: prod publishes a read-only API key
    precisely for this (elasticsearch-api-key-readonly), and pasting the
    read-write one instead is an easy mistake with no natural symptom, because
    reading works identically either way. _has_privileges answers for
    whichever key authenticated the request, so this asks the cluster rather
    than trusting the key's name.
    """
    if ctx.get("GE_DEV_ALLOW_ES_WRITE_KEY"):
        warn("devctl: WARNING — live ES write check skipped (GE_DEV_ALLOW_ES_WRITE_KEY)")
        return
    response = http.request(
        f"{probe_url(ctx.get('GE_DEV_ES_URL'))}/_security/user/_has_privileges",
        method="POST",
        headers={
            "Authorization": f"ApiKey {ctx.get('GE_DEV_ES_API_KEY')}",
            "Content-Type": "application/json",
        },
        body=json.dumps({"index": [{"names": ["*"], "privileges": WRITE_PRIVILEGES}]}),
        timeout=15,
        insecure=True,
    )
    try:
        per_index = json.loads(response.body)["index"]
        granted = sorted({name for privs in per_index.values() for name, ok in privs.items() if ok})
    except (ValueError, KeyError, TypeError, AttributeError):
        die(
            "couldn't verify that GE_DEV_ES_API_KEY is read-only — the cluster's\n"
            "        answer to _has_privileges wasn't readable. Re-run with\n"
            "        GE_DEV_ALLOW_ES_WRITE_KEY=1 if you're sure."
        )
    if granted:
        die(
            f"GE_DEV_ES_API_KEY can write to live Elasticsearch (grants: {','.join(granted)}).\n"
            "        Use the read-only key — in prod that's the secret\n"
            "        elasticsearch-api-key-readonly, the same one the deployed api reads\n"
            "        with. Nothing in this environment writes to a live cluster, so a\n"
            "        write-capable key here only adds risk."
        )
    print("devctl: live Elasticsearch key is read-only")


# --- tunnel ------------------------------------------------------------------


def configure_tunnel_context(ctx: Ctx, env: str) -> str:
    """Select the GKE cluster for env and return its kubectl context. stage and
    prod live in separate clusters, so select the matching one instead of
    inheriting whichever global context happened to be active."""
    cluster = ctx.get("GE_DEV_K8S_CLUSTER", f"greenearth-{env}-cluster")
    region = ctx.get("GE_GCP_REGION", "us-east1")
    project = ctx.get("GE_GCP_PROJECT_ID", "greenearth-471522")

    if shutil.which("gcloud", path=ctx.env.get("PATH")) is None:
        die(f"gcloud is required for devctl tunnel to select the {env} cluster")
    warn(f"devctl: selecting Kubernetes cluster {cluster} ({project}/{region})")
    credentials = [
        "gcloud",
        "container",
        "clusters",
        "get-credentials",
        cluster,
        f"--location={region}",
        f"--project={project}",
    ]
    # gcloud's own chatter goes to stderr, keeping stdout for devctl.
    selected = ctx.run(credentials, check=False, capture=True)
    if selected.stdout:
        warn(selected.stdout.rstrip("\n"))
    if not selected.ok:
        die(
            f"couldn't configure kubectl for {cluster} — check your gcloud login and cluster access"
        )

    current = ctx.run(
        ["kubectl", "config", "current-context"], check=False, capture=True, quiet_err=True
    )
    context = current.stdout.strip()
    if not current.ok or not context:
        die(f"gcloud configured {cluster}, but kubectl has no current context")
    return context


def cmd_tunnel(ctx: Ctx, args: list[str], *, sleep=time.sleep) -> None:
    # `--live es` needs one of these open: the cluster sits behind an internal
    # load balancer with no address a container can dial. Runs in the
    # foreground and restarts itself, because kubectl port-forward drops
    # connections routinely and a silently dead tunnel looks like a broken
    # environment.
    env = args[0] if args else ctx.live_env
    if env not in ("stage", "prod"):
        die("usage: devctl tunnel [stage|prod]")
    if shutil.which("kubectl", path=ctx.env.get("PATH")) is None:
        die("kubectl is required for devctl tunnel")
    namespace = ctx.get("GE_DEV_ES_NAMESPACE", f"greenearth-{env}")
    service = ctx.get("GE_DEV_ES_SERVICE", "svc/greenearth-es-internal-lb")
    port = tunnel_port(ctx)

    # Pinned to the selected context, so a later context change can't
    # redirect the tunnel.
    context = configure_tunnel_context(ctx, env)

    target = f"{namespace}/{service}"
    print(f"devctl: tunneling {target} to localhost:{port} via {context} (ctrl-c to stop)")
    print(f"        then, in another shell: devctl up --live es --live-env {env}")
    # The listener must be reachable from containers, which name the host as
    # host.docker.internal. Docker Desktop routes that to loopback, so the
    # default is fine there; on native Linux it's the bridge gateway, so the
    # tunnel has to listen wider: GE_DEV_ES_TUNNEL_BIND=0.0.0.0 (docs/remote.md).
    bind = ctx.get("GE_DEV_ES_TUNNEL_BIND", "127.0.0.1")
    forward = [
        "kubectl",
        "--context",
        context,
        "port-forward",
        service,
        f"{port}:9200",
        "-n",
        namespace,
        "--address",
        bind,
    ]
    while True:
        ctx.run(forward, check=False)
        warn("devctl: tunnel dropped, reconnecting...")
        sleep(2)
