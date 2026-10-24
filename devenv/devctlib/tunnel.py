"""The public tunnel `devctl bsky` and `devctl eurosky` share.

One ngrok tunnel into a small Caddy gateway, both compose services under the
`bsky` profile. One origin rather than a tunnel per service (issue #25):
ngrok's free tier hands out a single development domain, so two tunnels come
back on the *same* hostname and ngrok treats them as a pool — it
load-balances requests across both upstreams, and each path answers from the
wrong service about half the time. Deterministic path routing in front of one
tunnel works the same on every ngrok plan.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import http
from .compose import compose
from .ctx import Ctx, warn

PROFILE = "bsky"

# trusted_proxies keeps the X-Forwarded-* that ngrok sets (it terminates TLS,
# so the hop into the gateway is plain http and would otherwise be re-stamped
# as X-Forwarded-Proto: http).
CADDY_GLOBALS = """\
{
\tadmin off
\tauto_https off
\tservers {
\t\ttrusted_proxies static private_ranges
\t}
}
"""


def write_caddyfile(path: Path, site: str) -> None:
    """The gateway's whole routing policy. site is the body of its :80 block,
    one directive per line. Upstreams are service names on the compose
    network, which Caddy resolves per dial, so it follows a recreated service
    to its new container instead of pinning a stale address. Like the ngrok
    config this holds no secrets, and is world-readable so the image's user
    can read it whatever the host ownership."""
    body = "".join(f"\t{line}\n" for line in site.splitlines())
    path.write_text(f"{CADDY_GLOBALS}\n:80 {{\n{body}}}\n")
    path.chmod(0o644)


def write_ngrok_config(ctx: Ctx, path: Path) -> None:
    """The agent config the container reads (mounted at /etc/ngrok.yml).

    Exactly one tunnel, dialing the gateway by service name on the compose
    network, so no host ports are involved — and ngrok can never pool two
    endpoints behind one free-tier hostname. web_addr binds the agent's
    inspection API to 0.0.0.0 so the published port is reachable from the
    host. No secrets (compose passes NGROK_AUTHTOKEN separately), and
    world-readable so the image's non-root user can read it.
    """
    # GE_DEV_NGROK_DOMAIN_API named this same reserved domain back when the
    # api had its own tunnel; honoured so an existing devenv.local.env works.
    domain = ctx.get("GE_DEV_NGROK_DOMAIN") or ctx.get("GE_DEV_NGROK_DOMAIN_API")
    lines = [
        'version: "2"',
        "web_addr: 0.0.0.0:4040",
        "tunnels:",
        "  public:",
        "    proto: http",
        "    addr: gateway:80",
    ]
    if domain:
        lines.append(f"    domain: {domain}")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o644)


def start(ctx: Ctx, *, sleep=time.sleep) -> bool:
    """Start the gateway and ngrok behind the Caddyfile already written to the
    runtime dir, fresh, so they pick up this session's config — then confirm
    both stayed up: the agent exits on any startup error, so a dead container
    means a real problem to show rather than swallow."""
    write_ngrok_config(ctx, ctx.runtime / "ngrok.yml")
    started = compose(
        ctx,
        "--profile",
        PROFILE,
        "up",
        "-d",
        "--force-recreate",
        "gateway",
        "ngrok",
        check=False,
        quiet=True,
        quiet_err=True,
    )
    if not started.ok:
        return False
    sleep(2)
    running = compose(
        ctx,
        "--profile",
        PROFILE,
        "ps",
        "--status",
        "running",
        "--format",
        "{{.Service}}",
        check=False,
        capture=True,
        quiet_err=True,
    ).stdout.split()
    ok = True
    for service in ("gateway", "ngrok"):
        if service in running:
            continue
        ok = False
        # The container came up but the process exited — surface why. Common
        # causes for ngrok: a bad NGROK_AUTHTOKEN, or a reserved domain still
        # held by another agent (ERR_NGROK_334), e.g. a leftover session that
        # hasn't released it; stop the holder or wait a few seconds and retry.
        warn(f"devctl: the {service} container exited on startup:")
        logs = compose(
            ctx,
            "--profile",
            PROFILE,
            "logs",
            "--tail=10",
            service,
            check=False,
            capture=True,
            quiet_err=True,
        )
        for line in logs.stdout.splitlines():
            warn(f"        {line}")
    return ok


def stop(ctx: Ctx) -> None:
    """Remove the tunnel containers; their configs are regenerated each session."""
    compose(
        ctx,
        "--profile",
        PROFILE,
        "rm",
        "-sf",
        "gateway",
        "ngrok",
        check=False,
        quiet=True,
        quiet_err=True,
    )


def public_endpoints(ctx: Ctx) -> list[tuple[str, str]] | None:
    """(hostname, upstream) for each https endpoint the agent reports, or None
    when its API doesn't answer."""
    response = http.request(f"http://127.0.0.1:{ctx.port('NGROK')}/api/tunnels", timeout=2)
    try:
        tunnels = json.loads(response.body).get("tunnels", [])
    except (ValueError, AttributeError):
        return None
    return [
        (t.get("public_url", "").split("://", 1)[1], t.get("config", {}).get("addr", ""))
        for t in tunnels
        if t.get("public_url", "").startswith("https://")
    ]


def host(ctx: Ctx, *, sleep=time.sleep) -> str | None:
    """Poll ngrok's local API for the session's public hostname.

    There is exactly one tunnel (see write_ngrok_config), so more than one
    https endpoint means the agent is also serving somebody else's config —
    and on the free tier those share a single development domain, which makes
    ngrok *pool* them and split requests between unrelated upstreams (issue
    #25). Refuse rather than pick one: a pooled hostname would publish feed
    records that answer intermittently.
    """
    for _ in range(30):
        endpoints = public_endpoints(ctx) or []
        if len(endpoints) == 1:
            return endpoints[0][0]
        if len(endpoints) > 1:
            warn(
                f"devctl: ngrok is publishing {len(endpoints)} endpoints for this session, not one:"
            )
            for hostname, addr in endpoints:
                warn(f"        https://{hostname} -> {addr}")
            warn(
                "        devctl runs a single tunnel into the gateway, so another agent or a\n"
                "        leftover session is sharing this account. Endpoints on one hostname are\n"
                "        pooled by ngrok and answer from whichever upstream it picks, so this\n"
                "        session would serve the wrong service at random. Stop the other agent\n"
                "        (check https://dashboard.ngrok.com/agents) and retry."
            )
            return None
        sleep(1)
    warn("devctl: timed out waiting for the tunnel to come up")
    return None


def wait_did(hostname: str, did: str, tries: int = 15, *, sleep=time.sleep) -> bool:
    """Wait for the feed generator's did.json to report did. Served through
    ngrok, so this also confirms the tunnel reaches the generator."""
    for _ in range(tries):
        response = http.request(f"https://{hostname}/.well-known/did.json", timeout=3)
        try:
            if json.loads(response.body).get("id") == did:
                return True
        except (ValueError, AttributeError):
            pass
        sleep(1)
    return False
