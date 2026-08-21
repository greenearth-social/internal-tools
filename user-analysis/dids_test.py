from pathlib import Path

import dids


def _write_csv(tmp_path: Path, name: str, rows: list[str]) -> Path:
    path = tmp_path / name
    path.write_text("distinct_id\n" + "\n".join(rows) + "\n")
    return path


def test_load_dids_dedupes_across_files(tmp_path):
    a = _write_csv(tmp_path, "a.csv", ["did:plc:aaa", "did:plc:bbb"])
    b = _write_csv(tmp_path, "b.csv", ["did:plc:bbb", "did:plc:ccc"])
    assert dids.load_dids([a, b]) == ["did:plc:aaa", "did:plc:bbb", "did:plc:ccc"]


def test_load_dids_skips_blank_rows(tmp_path):
    a = _write_csv(tmp_path, "a.csv", ["did:plc:aaa", "", "did:plc:bbb"])
    assert dids.load_dids([a]) == ["did:plc:aaa", "did:plc:bbb"]


def test_chunk_splits_into_fixed_size_groups():
    assert dids.chunk(["a", "b", "c", "d", "e"], 2) == [["a", "b"], ["c", "d"], ["e"]]


def test_chunk_empty_input_returns_empty_list():
    assert dids.chunk([], 25) == []
