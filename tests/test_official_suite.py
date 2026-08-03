from pathlib import Path

from tests.run_official_suite import parse_task_trace, snapshot_tree


def test_parse_task_trace_normalizes_statuses_delegation_and_order():
    output = """
TASK [one] *********************************************************************
ok: [first] => {"changed": false}
changed: [second -> localhost] => {"changed": true}
TASK [two] *********************************************************************
skipping: [second]
fatal: [first]: FAILED! => {"changed": false}
"""
    assert parse_task_trace(output) == {
        ("one", "first", "ok"): 1,
        ("one", "second", "changed"): 1,
        ("two", "second", "skipped"): 1,
        ("two", "first", "failed"): 1,
    }


def test_snapshot_tree_uses_relative_paths_and_bytes(tmp_path: Path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "value").write_bytes(
        ("root=%s at 2026-07-15 19:34:34.104229" % tmp_path).encode()
    )
    assert snapshot_tree(tmp_path) == {
        "nested/value": b"root=<OUTPUT_DIR> at <DATETIME>"
    }
