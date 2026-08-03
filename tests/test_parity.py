"""
Parity tests: a compiled/strategy run must match `ansible-playbook` (linear).

Runnable two ways:
    pytest tests/test_parity.py
    python3 tests/test_parity.py        # standalone, prints PASS/FAIL
"""

import os
import re
import subprocess
import sys

import pytest

pytest.importorskip("ansible")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
RECAP_RE = re.compile(
    r"ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)\s+"
    r"skipped=(\d+)\s+rescued=(\d+)\s+ignored=(\d+)"
)

FIXTURE_FILES = ["simple.yml", "advanced.yml", "delegate.yml", "sequence.yml"]


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["ANSIBLE_COLLECTIONS_PATH"] = os.path.join(ROOT, "collections")
    env["ANSIBLE_DEPRECATION_WARNINGS"] = "False"
    return env


def _recap(text):
    m = RECAP_RE.search(text)
    assert m, "no recap found in:\n%s" % text
    keys = ["ok", "changed", "unreachable", "failed", "skipped", "rescued", "ignored"]
    return dict(zip(keys, (int(x) for x in m.groups())))


def _ansible_recap(playbook, *args):
    out = subprocess.run(
        ["ansible-playbook", playbook, "-c", "local"] + list(args),
        cwd=FIXTURES, env=_env(), capture_output=True, text=True,
    )
    return _recap(out.stdout)


def _strategy_recap(playbook, *args):
    env = _env()
    env["ANSIBLE_STRATEGY"] = "ansiroll.core.ansiroll"
    out = subprocess.run(
        ["ansible-playbook", playbook, "-c", "local"] + list(args),
        cwd=FIXTURES, env=env, capture_output=True, text=True,
    )
    return _recap(out.stdout)


def _standalone_recap(playbook):
    out = subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "run", os.path.join(FIXTURES, playbook)],
        cwd=ROOT, env=_env(), capture_output=True, text=True,
    )
    return _recap(out.stdout + "\n" + out.stderr)


def check(fixture):
    base = _ansible_recap(fixture)
    strat = _strategy_recap(fixture)
    standalone = _standalone_recap(fixture)
    assert strat == base, "strategy %s != ansible %s (%s)" % (strat, base, fixture)
    assert standalone == base, "standalone %s != ansible %s (%s)" % (standalone, base, fixture)
    return base


def test_parity_simple():
    check("simple.yml")


def test_parity_advanced():
    check("advanced.yml")


def test_parity_delegate():
    # Exercises segment-splitting: target -> delegate_to controller -> target,
    # with register state carried across the boundaries.
    check("delegate.yml")


def test_parity_sequence():
    check("sequence.yml")


def test_strategy_respects_tags():
    assert _strategy_recap("tags.yml", "--tags", "run") == _ansible_recap(
        "tags.yml", "--tags", "run"
    )


if __name__ == "__main__":
    failures = 0
    for fx in FIXTURE_FILES:
        try:
            recap = check(fx)
            print("PASS %-14s %s" % (fx, recap))
        except AssertionError as e:
            failures += 1
            print("FAIL %-14s %s" % (fx, e))
    sys.exit(1 if failures else 0)
