import profiles

_RAW_PROFILE_FULL = {
    "did": "did:plc:aaa",
    "handle": "realname.bsky.social",
    "displayName": "Real Name",
    "description": "hi",
    "avatar": "https://example.com/a.jpg",
    "createdAt": "2026-01-01T00:00:00.000Z",
    "followersCount": 12,
    "followsCount": 34,
    "postsCount": 56,
    "labels": [{"val": "bot", "src": "did:plc:aaa"}],
}

_RAW_PROFILE_BARE = {
    "did": "did:plc:bbb",
    "handle": "bbb.bsky.social",
    "followersCount": 0,
    "followsCount": 0,
    "postsCount": 0,
}


def test_parse_profile_extracts_full_fields():
    signal = profiles.parse_profile(_RAW_PROFILE_FULL)
    assert signal.did == "did:plc:aaa"
    assert signal.handle == "realname.bsky.social"
    assert signal.created_at == "2026-01-01T00:00:00.000Z"
    assert signal.followers_count == 12
    assert signal.follows_count == 34
    assert signal.posts_count == 56
    assert signal.has_avatar is True
    assert signal.has_description is True
    assert signal.self_labels == ["bot"]
    assert signal.fetch_error is None


def test_parse_profile_handles_missing_optional_fields():
    signal = profiles.parse_profile(_RAW_PROFILE_BARE)
    assert signal.did == "did:plc:bbb"
    assert signal.created_at is None
    assert signal.has_avatar is False
    assert signal.has_description is False
    assert signal.self_labels == []


def test_checkpoint_roundtrip(tmp_path):
    path = tmp_path / "checkpoint.jsonl"
    signals = [profiles.parse_profile(_RAW_PROFILE_FULL), profiles.parse_profile(_RAW_PROFILE_BARE)]
    profiles.append_checkpoint(path, signals)
    loaded = profiles.load_checkpoint(path)
    assert set(loaded) == {"did:plc:aaa", "did:plc:bbb"}
    assert loaded["did:plc:aaa"].handle == "realname.bsky.social"


def test_load_checkpoint_missing_file_returns_empty(tmp_path):
    assert profiles.load_checkpoint(tmp_path / "missing.jsonl") == {}


def test_append_checkpoint_is_additive(tmp_path):
    path = tmp_path / "checkpoint.jsonl"
    profiles.append_checkpoint(path, [profiles.parse_profile(_RAW_PROFILE_FULL)])
    profiles.append_checkpoint(path, [profiles.parse_profile(_RAW_PROFILE_BARE)])
    loaded = profiles.load_checkpoint(path)
    assert set(loaded) == {"did:plc:aaa", "did:plc:bbb"}
