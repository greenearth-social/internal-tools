"""What the compose files promise, read from the files themselves."""

from pathlib import Path

import yaml

DEVENV_DIR = Path(__file__).parent
COMPOSE_FILE = DEVENV_DIR / "docker-compose.yml"
BRIDGEU_COMPOSE_FILE = DEVENV_DIR / "docker-compose.bridgeu.yml"


def test_quality_backfill_uses_local_es_and_the_ingest_checkout():
    services = yaml.safe_load(COMPOSE_FILE.read_text())["services"]
    quality = services["seed-quality"]
    ingest = services["seed-megastream"]

    assert quality["command"] == ["go", "run", "./cmd/backfill_quality_index"]
    assert "GE_ELASTICSEARCH_URL=http://elasticsearch:9200" in quality["environment"]
    assert "GE_INDEX_PERIOD=week" in quality["environment"]
    assert quality["env_file"] == ingest["env_file"]
    assert "${GE_DEV_REPO_ROOT:-../..}/ingex/ingest:/src" in quality["volumes"]
    assert "inference" not in quality.get("depends_on", {})


def test_compose_forwards_authtoken_without_running_ngrok_as_root():
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    ngrok = compose["services"]["ngrok"]

    assert ngrok["environment"] == ["NGROK_AUTHTOKEN"]
    assert "user" not in ngrok
    assert ngrok["volumes"] == ["${GE_DEV_RUNTIME:-./.runtime}/ngrok.yml:/etc/ngrok.yml:ro"]


def test_compose_tunnels_into_the_gateway_and_starts_neither_by_default():
    compose = yaml.safe_load(COMPOSE_FILE.read_text())
    gateway = compose["services"]["gateway"]

    assert gateway["profiles"] == ["bsky"]
    assert compose["services"]["ngrok"]["profiles"] == ["bsky"]
    assert gateway["volumes"] == ["${GE_DEV_RUNTIME:-./.runtime}/Caddyfile:/etc/caddy/Caddyfile:ro"]
    # The gateway is only reachable from the compose network: the tunnel is the
    # one way in, so nothing is exposed on the host when no session is up.
    assert "ports" not in gateway


def test_bridgeu_services_reach_mysky_only_through_the_api():
    """BridgEU talks to mysky over the api's HTTP surface and nothing else.

    In production the two run on different clouds; here the network layout is
    what keeps a BridgEU service from quietly depending on mysky's
    Elasticsearch or Firestore. Mysky services stay on the default network
    (they declare none), BridgEU services sit only on `bridgeu`, and the api is
    the single service on both.
    """
    mysky = yaml.safe_load(COMPOSE_FILE.read_text())["services"]
    bridgeu = yaml.safe_load(BRIDGEU_COMPOSE_FILE.read_text())["services"]

    assert all("networks" not in svc for svc in mysky.values())

    api = bridgeu.pop("api")
    assert set(api) == {"networks"}, "the BridgEU file only adds the api to its network"
    assert set(api["networks"]) == {"default", "bridgeu"}
    assert api["networks"]["bridgeu"]["aliases"] == ["mysky-api"]

    # The tunnel gateway joins too, to route a `devctl eurosky` session to
    # bridgeu-api. It's a profile-gated reverse proxy that never runs by default.
    gateway = bridgeu.pop("gateway")
    assert gateway == {"networks": {"default": {}, "bridgeu": {}}}
    assert mysky["gateway"]["profiles"] == ["bsky"]

    assert bridgeu, "expected at least one BridgEU service"
    for name, svc in bridgeu.items():
        assert name.startswith("bridgeu-"), name
        assert name not in mysky, f"{name} would merge into a mysky service"
        assert svc["networks"] == ["bridgeu"], name
        assert "depends_on" not in svc or set(svc["depends_on"]) <= set(bridgeu), name
        env = " ".join(svc.get("environment", []))
        for mysky_store in ("elasticsearch", "firebase:"):
            assert mysky_store not in env, f"{name} addresses {mysky_store} directly"


def test_bridgeu_key_issuer_is_a_mysky_side_tool():
    """The key lives in the api's Firestore, so the job writing it is mysky's."""
    mysky = yaml.safe_load(COMPOSE_FILE.read_text())["services"]
    issuer = mysky["issue-bridgeu-key"]
    assert issuer["profiles"] == ["tools"]
    assert "GE_DEV_BRIDGEU_MYSKY_API_KEY=${GE_DEV_BRIDGEU_MYSKY_API_KEY:-}" in issuer["environment"]

    bridgeu_api = yaml.safe_load(BRIDGEU_COMPOSE_FILE.read_text())["services"]["bridgeu-api"]
    assert "BRIDGEU_MYSKY_API_KEY=${GE_DEV_BRIDGEU_MYSKY_API_KEY:-}" in bridgeu_api["environment"]
