from collections import Counter

from tests import run_ansible_test_suite as suite


def test_parse_recaps_returns_every_play_recap():
    output = """
PLAY RECAP *********************************************************************
first : ok=2 changed=1 unreachable=0 failed=0 skipped=3 rescued=0 ignored=0

PLAY RECAP *********************************************************************
second : ok=4 changed=0 unreachable=0 failed=1 skipped=0 rescued=1 ignored=2
"""
    assert suite.parse_recaps(output) == [
        {
            "first": {
                "ok": 2, "changed": 1, "unreachable": 0, "failed": 0,
                "skipped": 3, "rescued": 0, "ignored": 0,
            }
        },
        {
            "second": {
                "ok": 4, "changed": 0, "unreachable": 0, "failed": 1,
                "skipped": 0, "rescued": 1, "ignored": 2,
            }
        },
    ]


def test_parse_provenance_returns_machine_readable_records():
    output = """
ANSIROLL_PROVENANCE {"mode": "native-agent", "scope": "play"}
ok: [localhost]
ANSIROLL_PROVENANCE {"mode": "real-ansiballz", "scope": "task"}
"""
    assert suite.parse_provenance(output) == [
        {"mode": "native-agent", "scope": "play"},
        {"mode": "real-ansiballz", "scope": "task"},
    ]


def test_add_strategy_alias_is_idempotent(tmp_path):
    aliases = tmp_path / "aliases"
    aliases.write_text("shippable/posix/group1\n")
    suite.add_strategy_alias(aliases)
    suite.add_strategy_alias(aliases)
    assert aliases.read_text().splitlines() == [
        "shippable/posix/group1",
        suite.STRATEGY_ALIAS,
    ]


def test_add_injected_aliases_includes_native_only_and_provenance(tmp_path):
    aliases = tmp_path / "aliases"
    aliases.write_text("shippable/posix/group1\n")

    suite.add_injected_aliases(
        aliases, native_only=True, provenance=True
    )
    suite.add_injected_aliases(
        aliases, native_only=True, provenance=True
    )

    assert aliases.read_text().splitlines() == [
        "shippable/posix/group1",
        suite.STRATEGY_ALIAS,
        suite.NATIVE_ONLY_ALIAS,
        suite.PROVENANCE_ALIAS,
    ]


def test_selected_targets_are_version_specific_and_deduplicate_explicit():
    assert "templating" not in suite.selected_targets("2.12", "smoke", [])
    assert "templating" in suite.selected_targets("2.20", "smoke", [])
    assert "ignore_errors" not in suite.selected_targets("2.21", "smoke", [])
    assert suite.selected_targets(
        "2.20", "smoke", ["failed_when", "failed_when", "tags"]
    ) == ("failed_when", "tags")
    assert suite.selected_targets(
        "2.20", "full", []
    ) == suite.FULL_TARGETS_BY_SERIES["2.20"]
    assert {
        "conditionals", "set_fact", "blocks", "strategy_linear"
    }.issubset(suite.selected_targets("2.20", "full", []))
    assert suite.selected_targets(
        "2.20", "native", []
    ) == suite.NATIVE_TARGETS_BY_SERIES["2.20"]


def test_full_profile_rejects_an_unvalidated_series():
    try:
        suite.selected_targets("2.21", "full", [])
    except ValueError as ex:
        assert "no validated full ansible-test profile" in str(ex)
    else:
        raise AssertionError("unvalidated full profile should fail")


def test_compare_runs_checks_rc_recaps_and_task_trace(monkeypatch):
    monkeypatch.setattr(suite, "parse_recaps", lambda value: [value])
    monkeypatch.setattr(suite, "parse_task_trace", lambda value: Counter({value: 1}))
    assert suite.compare_runs(0, "same", 0, "same") == []
    mismatches = suite.compare_runs(0, "linear", 2, "ansiroll")
    assert any(item.startswith("rc ") for item in mismatches)
    assert any(item.startswith("recaps ") for item in mismatches)
    assert any(item.startswith("task trace ") for item in mismatches)


def test_linear_failure_short_circuits_comparison():
    assert suite.compare_runs(3, "", 0, "") == ["linear ansible-test rc=3"]
