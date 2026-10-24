"""Commands that look at an environment rather than change it."""

from __future__ import annotations

from . import ports as port_table
from .compose import compose, compose_argv, oom_killed_services, running_projects
from .ctx import Ctx, die, warn
from .envfiles import env_file_value, nonempty
from .es import ES_OWNER, data_age_hours, es_count, es_curl, share_es, warn_stale_data

# --- ports -------------------------------------------------------------------

# What `ports --ssh` forwards: everything a browser or client on another
# machine would reach. Not ngrok's inspection API, which is local tooling.
SSH_PORTS = (
    "FRONTEND",
    "API",
    "ES",
    "FIRESTORE",
    "FIREBASE_AUTH",
    "FUNCTIONS",
    "FIREBASE_UI",
    "FIREBASE_UI_WS",
    "BRIDGEU_API",
)

# The service is called elasticsearch everywhere else in this tool; the port
# variable has always been the shorter GE_DEV_PORT_ES.
PORT_ALIASES = {"elasticsearch": "es", "auth": "firebase_auth", "ui": "firebase_ui"}


def cmd_ports(ctx: Ctx, args: list[str]) -> None:
    # --ssh prints ready-made port-forwarding flags for reaching this instance
    # from another machine, one -L per published port, same number both ends:
    #   ssh -N $(devctl ports --ssh) devbox        # run where devctl runs
    # See docs/remote.md.
    if args and args[0] == "--ssh":
        print(" ".join(f"-L {p}:127.0.0.1:{p}" for p in (ctx.port(n) for n in SSH_PORTS)))
        return
    # With a service name, print just the number, so it can be substituted
    # straight into a command:
    #   curl http://127.0.0.1:$(devctl ports api --name alpha)/health
    if args:
        name = PORT_ALIASES.get(args[0], args[0])
        value = ctx.get("GE_DEV_PORT_" + name.upper().replace("-", "_"))
        if not value:
            die(f"no published port for '{args[0]}' (try: devctl ports)")
        print(value)
        return
    project = ctx.get("GE_DEV_NAME")
    print(f"instance      {ctx.instance} (compose project {project}, offset {ctx.offset})")
    for label, name in (
        ("api", "API"),
        ("elasticsearch", "ES"),
        ("frontend", "FRONTEND"),
        ("firestore", "FIRESTORE"),
        ("firebase_auth", "FIREBASE_AUTH"),
        ("functions", "FUNCTIONS"),
        ("firebase_ui", "FIREBASE_UI"),
        ("bridgeu-api", "BRIDGEU_API"),
    ):
        print(f"{label:<13} {ctx.port(name)}")


# --- ls ----------------------------------------------------------------------

LS_ROW = "{:<14} {:<9} {:<7} {:<7} {:<9} {}"


def cmd_ls(ctx: Ctx, args: list[str]) -> None:
    # Every instance that exists on disk, whether or not it's running — a
    # stopped instance still owns its ports and its volumes, which is exactly
    # what someone asking "what's here?" needs to know.
    running: set[str] = set()
    if ctx.run(["docker", "info"], check=False, quiet=True, quiet_err=True).ok:
        running = running_projects(ctx)

    print(LS_ROW.format("NAME", "STATE", "OFFSET", "API", "FRONTEND", "ROOT"))
    bases = port_table.resolve_bases(ctx.env)
    instances = port_table.list_instances(ctx.devenv)
    for name, offset in instances:
        ports = port_table.ports_for(offset, bases)
        state = "running" if f"ge-{name}" in running else "stopped"
        # The recorded repo root, shown only when it isn't the shared default
        # — that's the row where "which checkout is this instance on?" has a
        # surprising answer.
        runtime = port_table.runtime_dir(ctx.devenv, name)
        root = env_file_value(runtime / "repo_root.env", "GE_DEV_REPO_ROOT")
        if not root or root == str(ctx.default_repo_root):
            root = "-"
        print(LS_ROW.format(name, state, offset, ports["API"], ports["FRONTEND"], root))
    if not instances:
        print("(no instances yet — devctl up)")


# --- es ----------------------------------------------------------------------


def cmd_es(ctx: Ctx, args: list[str]) -> None:
    # Query this instance's Elasticsearch without having to find its port or
    # its key first — both move per instance, and the cluster has security on,
    # so the obvious `curl localhost:9200/...` just returns a 401.
    #
    #   devctl es /posts/_count
    #   devctl es '/posts/_search?size=1&pretty'
    #   devctl es /posts/_search -X POST -H 'Content-Type: application/json' -d '{...}'
    #
    # Read-only: this is the same key the api reads with.
    if not args:
        die("usage: devctl es <path> [curl args...]\n        e.g. devctl es /posts/_count")
    es_curl(ctx, args[0], *args[1:])
    print()


# --- logs --------------------------------------------------------------------


def cmd_logs(ctx: Ctx, args: list[str]) -> None:
    # Two modes on purpose. The default prints and exits, so `devctl logs api`
    # is a question you can ask and get an answer to. Following is opt-in with
    # -t, because a follow on an idle environment prints the backlog and then
    # sits there silently, which reads exactly like a hang.
    follow = any(a in ("-t", "-f", "--follow") for a in args)
    passthrough = [a for a in args if a not in ("-t", "-f", "--follow")]
    # Only supply a default line count when the caller didn't pick one, so
    # `devctl logs --tail=500 api` works.
    if not any(a == "--tail" or a.startswith("--tail=") for a in passthrough):
        passthrough.insert(0, "--tail=100")
    ctx.exec(compose_argv(ctx, "logs", *(["-f"] if follow else []), *passthrough))


# --- status ------------------------------------------------------------------


def cmd_status(ctx: Ctx, args: list[str]) -> None:
    compose(ctx, "ps")
    # `compose ps` shows a 137 exit code but not what it implies, and a stopped
    # Elasticsearch is the version of this environment failing that looks
    # least like a memory problem.
    killed = oom_killed_services(ctx)
    if killed:
        print()
        warn(f"** killed by the kernel, almost certainly out of memory: {' '.join(killed)}")
        warn(f"   restart with: devctl up{ctx.name_flag()}   (running instances: devctl ls)")
    print()
    if share_es(ctx):
        print(f"elasticsearch shared from '{ES_OWNER}':")
    if share_es(ctx) or nonempty(ctx.runtime / "es_key_ro.env"):
        for alias in ("posts", "likes", "inferences"):
            print(f"{alias:<12} {es_count(ctx, alias)} docs")
    if ctx.repo_root != ctx.default_repo_root:
        print(f"code         mounted from {ctx.repo_root}")
    age = data_age_hours(ctx)
    if age is not None:
        print(f"data age     newest post {age}h old")
    warn_stale_data(ctx)
