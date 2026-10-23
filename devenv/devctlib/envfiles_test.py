from devctlib.envfiles import env_file_value, load_local_env, read_env_file, write_env_file


def test_a_missing_file_reads_as_empty(tmp_path):
    assert read_env_file(tmp_path / "nope.env") == {}
    assert env_file_value(tmp_path / "nope.env", "X") == ""


def test_values_round_trip(tmp_path):
    path = tmp_path / "state.env"
    write_env_file(path, {"GE_DEV_LIVE": "es,inference", "GE_DEV_LIVE_ENV": "prod", "EMPTY": ""})

    assert read_env_file(path) == {
        "GE_DEV_LIVE": "es,inference",
        "GE_DEV_LIVE_ENV": "prod",
        "EMPTY": "",
    }
    assert env_file_value(path, "GE_DEV_LIVE_ENV") == "prod"


def test_state_values_are_taken_literally(tmp_path):
    path = tmp_path / "state.env"
    path.write_text("A=${HOME}\n")

    assert env_file_value(path, "A") == "${HOME}"


def test_local_env_overrides_the_process_environment(tmp_path):
    (tmp_path / "devenv.local.env").write_text(
        "# a comment\n"
        "GE_DEV_ES_HEAP=512m\n"
        'GE_DEV_BSKY_HANDLE="me.bsky.social"\n'
        "export GE_DEV_LIVE=es\n"
    )
    env = {"GE_DEV_ES_HEAP": "1g", "OTHER": "kept"}

    load_local_env(tmp_path, env)

    assert env == {
        "GE_DEV_ES_HEAP": "512m",
        "GE_DEV_BSKY_HANDLE": "me.bsky.social",
        "GE_DEV_LIVE": "es",
        "OTHER": "kept",
    }


def test_no_local_env_is_fine(tmp_path):
    env = {"A": "1"}
    load_local_env(tmp_path, env)
    assert env == {"A": "1"}
