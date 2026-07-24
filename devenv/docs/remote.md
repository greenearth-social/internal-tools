# Running the environment on a remote host

The environment is plain Docker Compose, so a remote Linux box runs it the
same way a laptop does. The reasons to want that: more memory (several
instances fit where a laptop holds one), and parking agent sandboxes
somewhere that isn't your machine.

## Bring it up

On the remote host, follow [onboarding.md](onboarding.md) exactly as written
— clone the sibling repos, `./devctl bootstrap`. Nothing about the setup is
macOS-specific; the one Linux-specific piece (how containers reach the host,
see below) is already handled in the compose file.

## Reach it: SSH port-forwarding

Every published port binds `127.0.0.1` on the remote host, so nothing is
exposed to the network — by design. Reach it by forwarding the ports over
SSH, which makes the remote instance indistinguishable from a local one
(same numbers, same URLs, and the browser's hardcoded emulator endpoints
keep working).

`devctl ports --ssh` prints the flags, one `-L` per published port:

```bash
# On the remote host:
./devctl ports --ssh
# -L 3000:127.0.0.1:3000 -L 8300:127.0.0.1:8300 -L 9201:127.0.0.1:9201 ...

# On your laptop:
ssh -N $(ssh devbox 'greenearth/internal-tools/devenv/devctl ports --ssh') devbox
```

Then http://127.0.0.1:3000, `devctl feed`-style curls against
`127.0.0.1:8300`, and the Firebase emulator UI on 4000 all work from your
laptop as if the stack ran there. For a named instance, add `--name <n>` to
the `ports --ssh` call — its offset block forwards the same way.

Don't have the local ports free (a local instance is using them)? Forward to
shifted local ports by editing the `-L` flags (`-L 13000:127.0.0.1:3000 ...`)
— everything works except browser sign-in, which needs the Firestore/Auth
ports at their real numbers (the SDK connects to hardcoded
`127.0.0.1:8080/9099` from your browser).

## Exposing ports beyond loopback (usually: don't)

If SSH forwarding genuinely won't do (a shared team box on a trusted
network), `GE_DEV_BIND=0.0.0.0` in `devenv.local.env` publishes every port
on all interfaces. Understand what that means before setting it: the
environment is developer-grade — a known Elasticsearch password, minted keys
on disk, an api that accepts dev sessions — so treat a widely-bound instance
as public to whoever can reach the machine, and firewall accordingly
(`ufw allow from <your subnet>`, or a cloud security group).

## Linux specifics

Docker Desktop provides `host.docker.internal` (how a container reaches the
host); native Linux gets it from the `extra_hosts: host-gateway` mapping in
the compose file, which resolves to the **bridge gateway IP**, not loopback.
Two features route through that name and need one extra setting on Linux:

- **Multi-instance shared Elasticsearch.** A named instance dials the owner's
  cluster at `host.docker.internal:<owner ES port>` — which on Linux arrives
  via the bridge, where a `127.0.0.1`-bound port isn't listening. To run
  named instances on a Linux host, set `GE_DEV_BIND=0.0.0.0` (and firewall,
  per above) so the owner's port is reachable from the bridge. A single
  instance needs none of this.
- **`--live es` tunnels.** `devctl tunnel` listens on `127.0.0.1` by
  default; containers can't reach that over the bridge. Set
  `GE_DEV_ES_TUNNEL_BIND=0.0.0.0` in `devenv.local.env` on a Linux host
  (the tunnel carries a read-only-enforced key, but the same firewall
  caveat applies).

## Editing code on the remote host

The bind-mounted checkouts live on the remote filesystem, so point a remote
editor at them: VS Code / Cursor **Remote-SSH** (open the `greenearth/`
parent as the workspace) or a terminal editor over SSH both work. Everything
in the normal loop — `devctl restart api` after a change, `devctl test`,
`devctl logs` — runs on the remote host's shell.

For a coding agent on the remote host, see [agents.md](agents.md) — the
sandbox story is identical, and a remote box is the natural place for the
more autonomous configurations.
