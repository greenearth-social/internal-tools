"""Initialize the dev Elasticsearch to mirror prod (issue api#268).

Adapted from ingex/index/deploy/k8s/base/bootstrap-job.yaml, but instead of
duplicating the index mappings it reads the very same template ConfigMaps from
the mounted ingex checkout (/templates), so dev and prod mappings cannot drift.

Steps:
1. Wait for ES.
2. Create ILM policies (dev-sized ages).
3. Apply every index template from /templates, substituting the kustomize
   $(FOO_INDEX_SHARDS)/$(FOO_INDEX_REPLICAS) vars with dev values (1 shard,
   0 replicas).
4. Create initial indexes + write aliases that prod creates outside the
   bootstrap job (hashtags via alias file; inferences/likes/like_tombstones
   bootstrap indexes — in prod the jetstream ingest service creates the likes
   indexes, but the dev seed pipeline bulk-loads likes directly).
5. Mint two ES API keys mirroring prod's key split — read-only (api) and
   read-write (ingest/seed) — into /runtime/es_key_ro.env / es_key_rw.env.

Idempotent: safe to re-run; existing keys with the same names are invalidated
and re-minted (the .env files are rewritten each time).
"""

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ES_URL = os.environ["GE_ELASTICSEARCH_URL"].rstrip("/")
AUTH = base64.b64encode(f"elastic:{os.environ['ELASTIC_PASSWORD']}".encode()).decode()

TEMPLATES_DIR = Path("/templates")
RUNTIME_DIR = Path("/runtime")

# Dev-sized ILM ages (prod values come from the ilm-settings ConfigMap). Data
# in this environment is disposable and re-seeded, so deletion ages just need
# to be comfortably longer than a seed's useful lifetime.
INFERENCE_ROLLOVER_AGE = "7d"
ILM_DELETE_AGE = "90d"
ILM_FORCEMERGE_AGE = "1d"


def request(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        ES_URL + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Basic {AUTH}", "Content-Type": "application/json"},
    )
    # A freshly-started single-node ES can be slow to answer its first
    # requests (initializing system indices); retry transient timeouts.
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
        except (TimeoutError, urllib.error.URLError, OSError) as e:
            if attempt == 3:
                raise
            print(f"  transient error on {method} {path} ({e}); retrying...")
            time.sleep(5)
    raise AssertionError("unreachable")


def must(method: str, path: str, body: dict | None = None) -> dict:
    status, payload = request(method, path, body)
    if status >= 300:
        sys.exit(f"FATAL: {method} {path} -> {status}: {json.dumps(payload)[:2000]}")
    return payload


def wait_for_es(timeout_sec: int = 180) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            status, _ = request("GET", "/_cluster/health?wait_for_status=yellow&timeout=5s")
            if status == 200:
                print("Elasticsearch is ready")
                return
        except (urllib.error.URLError, OSError):
            pass
        print("Waiting for Elasticsearch...")
        time.sleep(3)
    sys.exit("FATAL: Elasticsearch did not become ready")


def create_ilm_policies() -> None:
    delete_policy = {
        "policy": {
            "phases": {
                "delete": {"min_age": ILM_DELETE_AGE, "actions": {"delete": {}}},
            }
        }
    }
    forcemerge_policy = {
        "policy": {
            "phases": {
                "warm": {
                    "min_age": ILM_FORCEMERGE_AGE,
                    "actions": {"forcemerge": {"max_num_segments": 1}},
                },
                "delete": {"min_age": ILM_DELETE_AGE, "actions": {"delete": {}}},
            }
        }
    }
    inference_policy = {
        "policy": {
            "phases": {
                "hot": {
                    "min_age": "0ms",
                    "actions": {"rollover": {"max_age": INFERENCE_ROLLOVER_AGE}},
                },
                "delete": {"min_age": "0ms", "actions": {"delete": {}}},
            }
        }
    }
    for name, policy in [
        ("inferences_ilm_policy", inference_policy),
        ("posts_ilm_policy", forcemerge_policy),
        ("replies_ilm_policy", forcemerge_policy),
        ("likes_ilm_policy", delete_policy),
        ("tombstones_ilm_policy", delete_policy),
    ]:
        must("PUT", f"/_ilm/policy/{name}", policy)
        print(f"ILM policy {name} applied")


def load_template_json(path: Path) -> tuple[str, dict]:
    """Extract the template JSON from a k8s ConfigMap YAML and substitute vars."""
    doc = yaml.safe_load(path.read_text())
    data = doc.get("data") or {}
    json_items = [(k, v) for k, v in data.items() if k.endswith(".json")]
    if len(json_items) != 1:
        sys.exit(f"FATAL: expected exactly one .json entry in {path.name}, got {len(json_items)}")
    key, raw = json_items[0]

    def substitute(match: re.Match) -> str:
        var = match.group(1)
        if var.endswith("_SHARDS"):
            return "1"
        if var.endswith("_REPLICAS"):
            return "0"
        sys.exit(f"FATAL: unknown template variable $({var}) in {path.name}")

    return key, json.loads(re.sub(r"\$\(([A-Z_]+)\)", substitute, raw))


def template_name(filename: str) -> str:
    # posts-ilm-index-template.yaml -> posts_ilm_template (matches bootstrap-job.yaml)
    stem = filename.removesuffix(".yaml").removesuffix("-index-template")
    return stem.replace("-", "_") + "_template"


def apply_templates() -> None:
    for path in sorted(TEMPLATES_DIR.glob("*-index-template.yaml")):
        _, body = load_template_json(path)
        name = template_name(path.name)
        must("PUT", f"/_index_template/{name}", body)
        print(f"Index template {name} applied ({path.name})")


def index_exists(name: str) -> bool:
    status, _ = request("HEAD", f"/{name}")
    return status == 200


def alias_has_members(alias: str) -> bool:
    status, _ = request("GET", f"/*/_alias/{alias}")
    return status == 200


def create_initial_indexes() -> None:
    # Hashtags: index + alias from the checked-in alias file (mirrors bootstrap).
    if not index_exists("hashtags_v1"):
        must("PUT", "/hashtags_v1")
        alias_path = TEMPLATES_DIR / "hashtags-alias.yaml"
        doc = yaml.safe_load(alias_path.read_text())
        (alias_body,) = [json.loads(v) for k, v in doc["data"].items() if k.endswith(".json")]
        must("POST", "/_aliases", alias_body)
        print("hashtags_v1 + alias created")

    # Write-alias bootstrap indexes. In prod, inferences comes from the
    # bootstrap job while likes/like_tombstones are created on demand by
    # jetstream_ingest (EnsureIndex); here the seed pipeline writes likes
    # directly, so the index must exist up front.
    for alias, index in [
        ("inferences", "inferences-000001"),
        ("likes", "likes-000001"),
        ("like_tombstones", "like-tombstones-000001"),
    ]:
        if alias_has_members(alias):
            print(f"alias {alias} already has members - skipping")
        else:
            must("PUT", f"/{index}", {"aliases": {alias: {"is_write_index": True}}})
            print(f"{index} created with write alias {alias}")


def mint_api_keys() -> None:
    keys = {
        # Mirrors prod's elasticsearch-api-key-readonly used by the api.
        "ge-dev-readonly": (
            "es_key_ro.env",
            {
                "cluster": ["monitor"],
                "indices": [
                    {
                        "names": ["*"],
                        "privileges": ["read", "view_index_metadata", "monitor"],
                        "allow_restricted_indices": False,
                    }
                ],
            },
        ),
        # Mirrors the ingest read/write key; index-level manage is needed for
        # EnsureIndex (index creation + alias management).
        "ge-dev-readwrite": (
            "es_key_rw.env",
            {
                "cluster": ["monitor"],
                "indices": [
                    {
                        "names": ["*"],
                        "privileges": ["all"],
                        "allow_restricted_indices": False,
                    }
                ],
            },
        ),
    }
    for name, (env_file, role) in keys.items():
        must("DELETE", "/_security/api_key", {"name": name})
        created = must(
            "POST",
            "/_security/api_key",
            {"name": name, "role_descriptors": {name.replace("-", "_"): role}},
        )
        out = RUNTIME_DIR / env_file
        out.write_text(f"GE_ELASTICSEARCH_API_KEY={created['encoded']}\n")
        print(f"API key {name} minted -> .runtime/{env_file}")


def main() -> None:
    wait_for_es()
    create_ilm_policies()
    apply_templates()
    create_initial_indexes()
    mint_api_keys()
    print("es-init completed successfully")


if __name__ == "__main__":
    main()
