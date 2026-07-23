import json
import pathlib
import textwrap

import init
import pytest

# --------------------------------------------------------------------------
# template naming
#
# These names have to match the ones prod's bootstrap job PUTs, or dev would
# silently create a second, differently-named template and the mappings would
# drift — the exact thing reading ingex's ConfigMaps is meant to prevent.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("posts-ilm-index-template.yaml", "posts_ilm_template"),
        ("likes-ilm-index-template.yaml", "likes_ilm_template"),
        ("post-tombstones-ilm-index-template.yaml", "post_tombstones_ilm_template"),
        ("like-tombstones-ilm-index-template.yaml", "like_tombstones_ilm_template"),
        ("replies-ilm-index-template.yaml", "replies_ilm_template"),
        ("reply-tombstones-ilm-index-template.yaml", "reply_tombstones_ilm_template"),
        ("hashtags-index-template.yaml", "hashtags_template"),
        ("inferences-index-template.yaml", "inferences_template"),
    ],
)
def test_template_names_match_the_prod_bootstrap_job(filename, expected):
    assert init.template_name(filename) == expected


# --------------------------------------------------------------------------
# ConfigMap parsing and variable substitution
# --------------------------------------------------------------------------


def _configmap(body: str, key: str = "posts-ilm-index-template.json") -> str:
    """Wrap *body* as the block scalar of a k8s ConfigMap, like ingex's files.

    Built by concatenation rather than a dedent()ed f-string: dedent runs after
    interpolation, so it would measure the embedded JSON's indentation and
    strip the block scalar's structure along with it.
    """
    header = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: posts-ilm-index-template\ndata:\n"
    return header + f"  {key}: |\n" + textwrap.indent(body, " " * 4) + "\n"


def test_shard_and_replica_vars_get_dev_sized_values(tmp_path):
    # Vars are unquoted in the real templates, so they substitute into JSON
    # numbers rather than strings.
    path = tmp_path / "posts-ilm-index-template.yaml"
    path.write_text(
        _configmap(
            """\
{
  "index_patterns": ["posts-*"],
  "template": {
    "settings": {
      "number_of_shards": $(POSTS_INDEX_SHARDS),
      "number_of_replicas": $(POSTS_INDEX_REPLICAS)
    }
  }
}"""
        )
    )

    _, body = init.load_template_json(path)

    settings = body["template"]["settings"]
    # Single-node dev cluster: one shard, and zero replicas so health is green.
    assert settings["number_of_shards"] == 1
    assert settings["number_of_replicas"] == 0


def test_substitution_produces_numbers_not_strings(tmp_path):
    # "1" in a settings field is accepted by ES but the templates use real
    # ints; keep the dev output identical in type as well as value.
    path = tmp_path / "posts-ilm-index-template.yaml"
    path.write_text(
        _configmap('{"template": {"settings": {"number_of_shards": $(POSTS_INDEX_SHARDS)}}}')
    )
    _, body = init.load_template_json(path)
    assert isinstance(body["template"]["settings"]["number_of_shards"], int)


def test_mappings_pass_through_untouched(tmp_path):
    # The whole point is that dev gets prod's mappings verbatim.
    mappings = {
        "properties": {
            "at_uri": {"type": "keyword", "index": True},
            "embeddings": {
                "properties": {
                    "all_MiniLM_L12_v2": {
                        "type": "dense_vector",
                        "dims": 384,
                        "index": True,
                        "similarity": "cosine",
                    }
                }
            },
        }
    }
    path = tmp_path / "posts-ilm-index-template.yaml"
    path.write_text(
        _configmap(
            json.dumps(
                {
                    "template": {
                        "settings": {"number_of_shards": "$(POSTS_INDEX_SHARDS)"},
                        "mappings": mappings,
                    }
                },
                indent=2,
            )
        )
    )

    _, body = init.load_template_json(path)

    assert body["template"]["mappings"] == mappings


def test_unknown_template_variable_is_fatal(tmp_path):
    # Silently leaving "$(SOMETHING_NEW)" in place would PUT a template that
    # differs from prod's in a way nobody notices until a query behaves oddly.
    path = tmp_path / "posts-ilm-index-template.yaml"
    path.write_text(
        _configmap('{"template": {"settings": {"refresh_interval": "$(SOMETHING_NEW)"}}}')
    )
    with pytest.raises(SystemExit):
        init.load_template_json(path)


def test_configmap_without_exactly_one_json_entry_is_fatal(tmp_path):
    path = tmp_path / "posts-ilm-index-template.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            apiVersion: v1
            kind: ConfigMap
            data:
              first.json: |
                {"a": 1}
              second.json: |
                {"b": 2}
            """
        )
    )
    with pytest.raises(SystemExit):
        init.load_template_json(path)


def test_returned_key_is_the_configmap_entry_name(tmp_path):
    path = tmp_path / "hashtags-index-template.yaml"
    path.write_text(_configmap('{"template": {}}', key="hashtags-index-template.json"))
    key, _ = init.load_template_json(path)
    assert key == "hashtags-index-template.json"


# --------------------------------------------------------------------------
# real prod templates
# --------------------------------------------------------------------------

INGEX_TEMPLATES = (
    pathlib.Path(__file__).resolve().parents[3] / "ingex/index/deploy/k8s/base/templates"
)

real_templates = (
    sorted(INGEX_TEMPLATES.glob("*-index-template.yaml")) if (INGEX_TEMPLATES.is_dir()) else []
)


@pytest.mark.skipif(not real_templates, reason="ingex checkout not present")
@pytest.mark.parametrize("path", real_templates, ids=lambda p: p.name)
def test_every_real_ingex_template_parses(path):
    """Canary for template drift.

    es-init reads these files straight from the ingex checkout so dev and prod
    mappings can't diverge. If prod introduces a new $(VAR) we don't know how
    to substitute, load_template_json exits — better to learn that here than
    when someone's environment won't start.
    """
    _, body = init.load_template_json(path)
    assert "template" in body
    settings = body["template"].get("settings", {})
    if "number_of_shards" in settings:
        assert settings["number_of_shards"] == 1
        assert settings["number_of_replicas"] == 0
