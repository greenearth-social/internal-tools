import gzip
import json
import sqlite3
import zipfile
from collections import Counter

import generate_graph as gg
import pytest

AUTHORS = [f"did:plc:author{i:017d}" for i in range(12)]
WINDOW = ("2026-11-20T00:00:00Z", "2026-11-22T00:00:00Z")


def _uri(author: str, i: int) -> str:
    return f"at://{author}/app.bsky.feed.post/post{i:09d}"


def _fixture(tmp_path):
    """A fixture with 60 posts by 12 authors and a 2-person cohort's likes."""
    directory = tmp_path / "data"
    directory.mkdir(parents=True)
    posts = [(_uri(AUTHORS[i % 12], i), "2026-11-20T10:00:00Z") for i in range(60)]
    db_path = tmp_path / "chunk.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE enriched_posts (at_uri TEXT, raw_post TEXT)")
        conn.executemany(
            "INSERT INTO enriched_posts VALUES (?, ?)",
            [
                (uri, json.dumps({"message": {"commit": {"record": {"createdAt": at}}}}))
                for uri, at in posts
            ],
        )
    with zipfile.ZipFile(directory / "mega_jetstream_20261120_000000.db.zip", "w") as zf:
        zf.write(db_path, "mega_jetstream_20261120_000000.db")
    cohort = ["did:plc:cohort0000000000000000a", "did:plc:cohort0000000000000000b"]
    with gzip.open(directory / "likes.jsonl.gz", "wt") as f:
        for n, (liker, (uri, _)) in enumerate(zip(cohort * 5, posts, strict=False)):
            f.write(
                json.dumps(
                    {
                        "at_uri": f"at://{liker}/app.bsky.feed.like/like{n:09d}",
                        "subject_uri": uri,
                        "author_did": liker,
                        "created_at": "2026-11-20T11:00:00Z",
                        "indexed_at": "2026-11-20T11:00:01Z",
                    }
                )
                + "\n"
            )
    manifest = {
        "window_start": WINDOW[0],
        "window_end": WINDOW[1],
        "personas": [{"did": cohort[0], "likes": 5}],
        "dev_users": [{"handle": "dev.example", "did": "did:plc:devuser000000000000000"}],
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory, dict(posts), cohort


def _read(path):
    with gzip.open(path, "rt") as f:
        return [json.loads(line) for line in f]


def test_synthetic_graph_is_consistent_with_the_fixture_and_recorded(tmp_path):
    directory, posts, cohort = _fixture(tmp_path)

    meta = gg.generate(directory, source="synthetic", seed=7, synthetic_likers=40, communities=3)

    actors = _read(directory / gg.ACTORS_FILE)
    follows = _read(directory / gg.FOLLOWS_FILE)
    likes = _read(directory / gg.LIKES_FILE)
    dids = {a["did"] for a in actors}
    assert set(AUTHORS) | set(cohort) | {"did:plc:devuser000000000000000"} <= dids
    assert all(f["follower_did"] in dids and f["subject_did"] in dids for f in follows)
    assert all(lk["subject_uri"] in posts and lk["liker_did"] in dids for lk in likes)
    # The cohort's real likes come through unchanged.
    assert Counter(lk["liker_did"] for lk in likes)[cohort[0]] == 5
    manifest = json.loads((directory / "manifest.json").read_text())
    assert manifest["graph"] == meta
    assert meta["source"] == "synthetic"
    assert meta["counts"] == {"actors": len(actors), "follows": len(follows), "likes": len(likes)}
    assert manifest["window_start"] == WINDOW[0]  # the rest of the manifest is kept


def test_synthetic_graph_is_deterministic_under_its_seed(tmp_path):
    first, _, _ = _fixture(tmp_path / "a")
    second, _, _ = _fixture(tmp_path / "b")
    for directory in (first, second):
        gg.generate(directory, source="synthetic", seed=3, synthetic_likers=30)
    for name in (gg.ACTORS_FILE, gg.FOLLOWS_FILE, gg.LIKES_FILE):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_prod_graph_without_a_url_says_how_to_go_without(tmp_path):
    directory, _, _ = _fixture(tmp_path)
    with pytest.raises(ValueError, match="--source synthetic"):
        gg.generate(directory, source="prod-graph", graph_url=None)


def test_prod_graph_sql_bounds_the_window_and_liker_count(tmp_path):
    sql = gg.prod_graph_sql(*WINDOW, 1234, tmp_path)
    assert "BEGIN;" in sql
    assert "INSERT" not in sql and "UPDATE" not in sql and "DELETE" not in sql
    assert "l.indexed_at >= '2026-11-20T00:00:00Z' AND l.indexed_at < '2026-11-22T00:00:00Z'" in sql
    assert "LIMIT 1234" in sql
    # Follows by index lookup per node, never a scan of graph.follow.
    assert "SELECT * FROM graph.follow f WHERE f.follower_id = n.id" in sql
    assert f"'{tmp_path}/follows.csv'" in sql


def test_prod_graph_reads_psqls_csv_export(tmp_path, monkeypatch):
    directory, posts, cohort = _fixture(tmp_path)
    uri = next(iter(posts))
    author = uri.removeprefix("at://").split("/")[0]

    def fake_psql(argv, **kwargs):
        script = open(argv[argv.index("--file") + 1]).read()
        out = script.split("TO '")[1].split("/actors.csv")[0]
        with open(f"{out}/actors.csv", "w") as f:
            f.write(f"{cohort[0]},active\n{author},deactivated\n")
        with open(f"{out}/likes.csv", "w") as f:
            f.write(f"{cohort[0]},rk1,{uri},2026-11-20 10:00:00.5+00,2026-11-20 10:00:01+00\n")
        with open(f"{out}/follows.csv", "w") as f:
            f.write(f"{cohort[0]},{author},fk1,,2026-11-20 09:00:00+00\n")
        return type("Result", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(gg.shutil, "which", lambda name: "/usr/bin/psql")
    monkeypatch.setattr(gg.subprocess, "run", fake_psql)

    meta = gg.generate(directory, source="prod-graph", graph_url="postgres://ro@prod/graph")

    assert meta["counts"] == {"actors": 2, "follows": 1, "likes": 1}
    assert _read(directory / gg.FOLLOWS_FILE) == [
        {
            "follower_did": cohort[0],
            "subject_did": author,
            "rkey": "fk1",
            "created_at": None,
            "indexed_at": "2026-11-20T09:00:00Z",
        }
    ]
    assert _read(directory / gg.LIKES_FILE)[0]["created_at"] == "2026-11-20T10:00:00Z"
    assert _read(directory / gg.ACTORS_FILE)[1] == {"did": author, "status": "deactivated"}


@pytest.mark.parametrize(
    ("follows", "likes", "error"),
    [
        ([], [], "no follows"),
        ([{"follower_did": "a", "subject_did": "x", "rkey": "1"}], [], "endpoint not in actors"),
        (
            [{"follower_did": "a", "subject_did": "b", "rkey": "1"}] * 2,
            [],
            "duplicate follow",
        ),
        (
            [{"follower_did": "a", "subject_did": "b", "rkey": "1"}],
            [{"liker_did": "a", "rkey": "1", "subject_uri": "at://b/app.bsky.feed.post/nope"}],
            "not a fixture post",
        ),
    ],
)
def test_check_graph_rejects_what_the_loader_cant_use(follows, likes, error):
    actors = [{"did": "a", "status": "active"}, {"did": "b", "status": "active"}]
    with pytest.raises(ValueError, match=error):
        gg.check_graph(actors, follows, likes, {"at://b/app.bsky.feed.post/p": "t"})
