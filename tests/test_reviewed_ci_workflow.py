"""Exercise the trusted report and reconciliation steps without GitHub writes."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/reviewed-ci.yml"
HEAD = "a" * 40
BASE = "b" * 40
OLD_BASE = "c" * 40
MERGE = "d" * 40
MERGE_CONTEXT = "Manual Full CI (merge candidate)"
LEGACY_CONTEXT = "Manual Full CI"
CURRENT_PR = {
    "state": "open",
    "head": {"sha": HEAD},
    "base": {"sha": BASE, "ref": "master"},
    "mergeable": True,
    "merge_commit_sha": MERGE,
}


@pytest.fixture
def workflow():
    yaml = pytest.importorskip("yaml")
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


@pytest.fixture
def run_step(tmp_path, monkeypatch, workflow):
    if not shutil.which("jq"):
        pytest.skip("workflow fixtures require jq")
    gh = tmp_path / "gh"
    gh.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

directory = Path(__file__).parent
args = sys.argv[2:]
with (directory / "calls").open("a") as log:
    log.write(json.dumps(args) + "\\n")
if "--method" in args and args[args.index("--method") + 1] == "PATCH":
    sys.exit(0)
if "POST" in args and "context=" + os.environ.get("MOCK_FAIL_STATUS_CONTEXT", "") in args:
    sys.exit(42)
routes = json.loads((directory / "routes").read_text())
for endpoint in routes:
    if endpoint in args:
        print(routes[endpoint])
        break
else:
    sys.exit("Unexpected gh call: " + repr(args))
"""
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path), prepend=":")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.example")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/ansiroll")
    monkeypatch.setenv("GITHUB_RUN_ID", "100")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")

    def run(job, routes, *, expected_exit=0, **env):
        (tmp_path / "routes").write_text(json.dumps({f"repos/example/ansiroll/{k}": v for k, v in routes.items()}))
        for key, value in workflow.get("env", {}).items():
            monkeypatch.setenv(key, value)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        script = workflow["jobs"][job]["steps"][0]["run"]
        result = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True)
        assert result.returncode == expected_exit, result.stdout + result.stderr
        return [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]

    return run


def report(check_id, owner, base, conclusion="success"):
    return {
        "id": check_id,
        "app": {"slug": "github-actions"},
        "external_id": owner,
        "conclusion": conclusion,
        "output": {"summary": f"Merge commit: {MERGE}\nBase commit: {base}"},
    }


@pytest.mark.parametrize(("branch", "cancelled"), [("master", {1, 7}), ("other", {1, 2, 7})])
def test_reconcile_checks_for_the_current_pr_only(run_step, branch, cancelled):
    owner = "manual-full-ci:pr:12"
    checks = [
        report(1, owner, OLD_BASE),
        report(2, owner, BASE),
        report(3, "manual-full-ci:pr:123", OLD_BASE),
        report(4, "", OLD_BASE),
        report(5, owner, OLD_BASE, "cancelled"),
        dict(report(6, owner, OLD_BASE), app={"slug": "another-app"}),
        report(8, "manual-full-ci:pr:123", BASE),
    ]
    calls = run_step(
        "invalidate-head-checks",
        {
            "pulls/12": f"12\t{HEAD}\t{BASE}\t{branch}",
            f"commits/{HEAD}/check-runs": "\n".join(
                json.dumps({"check_runs": page}) for page in (checks, [report(7, owner, OLD_BASE)])
            ),
        },
        EVENT_PR="12",
    )
    listing = next(args for args in calls if f"repos/example/ansiroll/commits/{HEAD}/check-runs" in args)
    assert "--paginate" in listing
    assert "filter=all" in listing
    assert {int(args[args.index("PATCH") + 1].rsplit("/", 1)[1]) for args in calls if "PATCH" in args} == cancelled


def test_base_push_skips_a_pr_that_moved_since_listing(run_step):
    calls = run_step(
        "invalidate-head-checks",
        {
            "pulls?state=open&base=master&per_page=100": f"12\t{HEAD}\t{BASE}\tmaster",
            "pulls/12": f"12\t{HEAD}\t{'e' * 40}\tmaster",
            f"commits/{HEAD}/check-runs": json.dumps({"check_runs": [report(1, "manual-full-ci:pr:12", OLD_BASE)]}),
        },
        EVENT_PR="",
    )
    assert not any("PATCH" in args for args in calls)


def test_synchronize_only_reconciles_head_checks(workflow):
    assert "synchronize" in workflow["on"]["pull_request_target"]["types"]
    assert "github.event.action == 'synchronize'" in workflow["jobs"]["invalidate-head-checks"]["if"]
    assert "github.event_name == 'workflow_dispatch'" in workflow["jobs"]["reviewed-head"]["if"]
    assert workflow["jobs"]["tests"]["needs"] == "reviewed-head"
    assert "uses" not in workflow["jobs"]["invalidate-head-checks"]["steps"][0]


@pytest.mark.parametrize(
    ("optional", "failed_context", "expected_contexts", "exit_code"),
    [
        pytest.param("", "", [MERGE_CONTEXT, LEGACY_CONTEXT], 0, id="publish-both"),
        pytest.param("", LEGACY_CONTEXT, [MERGE_CONTEXT, LEGACY_CONTEXT], 42, id="legacy-required"),
        pytest.param("true", LEGACY_CONTEXT, [MERGE_CONTEXT, LEGACY_CONTEXT], 0, id="legacy-optional"),
        pytest.param("true", MERGE_CONTEXT, [MERGE_CONTEXT], 42, id="candidate-required"),
    ],
)
def test_dispatch_marks_merge_statuses_pending(run_step, tmp_path, optional, failed_context, expected_contexts, exit_code):
    output = tmp_path / "outputs"
    calls = run_step(
        "reviewed-head",
        {
            "pulls/12": json.dumps(CURRENT_PR),
            f"git/commits/{MERGE}": json.dumps({"parents": [{"sha": BASE}, {"sha": HEAD}]}),
            f"statuses/{MERGE}": "{}",
        },
        expected_exit=exit_code,
        PR="12", EXPECTED_HEAD=HEAD, GITHUB_SHA=BASE, GITHUB_OUTPUT=str(output),
        LEGACY_MERGE_STATUS_OPTIONAL=optional, MOCK_FAIL_STATUS_CONTEXT=failed_context,
    )
    writes = [args for args in calls if "POST" in args]
    assert len(writes) == len(expected_contexts)
    for args, context in zip(writes, expected_contexts):
        assert f"repos/example/ansiroll/statuses/{MERGE}" in args
        assert f"context={context}" in args
        assert "state=pending" in args
    assert (f"merge={MERGE}" in output.read_text().splitlines()) == (exit_code == 0)


@pytest.mark.parametrize(
    ("tests_result", "optional", "failed_context", "writes_count", "head_result", "exit_code"),
    [
        pytest.param("success", "", "", 2, "success", 0, id="passing-ci"),
        pytest.param("failure", "", "", 2, "failure", 1, id="failing-ci"),
        pytest.param("success", "", LEGACY_CONTEXT, 1, "action_required", 42, id="legacy-required"),
        pytest.param("success", "true", LEGACY_CONTEXT, 2, "success", 0, id="legacy-optional"),
        pytest.param("success", "true", MERGE_CONTEXT, 2, "action_required", 42, id="candidate-required"),
    ],
)
def test_report_publishes_merge_statuses_and_pr_head_check(
    run_step, tests_result, optional, failed_context, writes_count, head_result, exit_code,
):
    calls = run_step(
        "report-pr-ci",
        {
            f"commits/{MERGE}/status": json.dumps(
                {"statuses": [{
                    "context": MERGE_CONTEXT,
                    "target_url": "https://github.example/example/ansiroll/actions/runs/100",
                    "description": f"Testing merge {MERGE}",
                }]}
            ),
            "actions/runs/100/jobs?filter=all&per_page=100": json.dumps(
                {"jobs": [
                    {"id": i, "run_attempt": 1, "name": f"tests / job {i}", "conclusion": "success"}
                    for i in range(20)
                ]}
            ),
            f"statuses/{MERGE}": "{}",
            "pulls/12": json.dumps(CURRENT_PR),
            "check-runs": "123",
        },
        expected_exit=exit_code,
        PR="12", HEAD=HEAD, BASE=BASE, MERGE=MERGE, MERGE_ATTEMPT="1", TESTS_RESULT=tests_result,
        LEGACY_MERGE_STATUS_OPTIONAL=optional, MOCK_FAIL_STATUS_CONTEXT=failed_context,
    )
    writes = [args for args in calls if "POST" in args and f"repos/example/ansiroll/statuses/{MERGE}" in args]
    assert len(writes) == writes_count
    for args, context in zip(writes, [LEGACY_CONTEXT, MERGE_CONTEXT]):
        assert f"context={context}" in args
        assert f"state={tests_result}" in args
    creation = next(args for args in calls if "repos/example/ansiroll/check-runs" in args)
    assert "external_id=manual-full-ci:pr:12" in creation
    assert f"head_sha={HEAD}" in creation
    assert f"name={LEGACY_CONTEXT}" in creation
    assert f"conclusion={head_result}" in creation
    assert f"output[summary]=Merge commit: {MERGE}\nBase commit: {BASE}" in creation


def test_legacy_status_cannot_claim_merge_gate(run_step):
    calls = run_step(
        "report-pr-ci",
        {
            f"commits/{MERGE}/status": json.dumps(
                {"statuses": [{
                    "context": LEGACY_CONTEXT,
                    "target_url": "https://github.example/example/ansiroll/actions/runs/100",
                    "description": f"Testing merge {MERGE}",
                }]}
            ),
        },
        PR="12", HEAD=HEAD, BASE=BASE, MERGE=MERGE, MERGE_ATTEMPT="1", TESTS_RESULT="success",
    )
    assert calls == [[f"repos/example/ansiroll/commits/{MERGE}/status"]]
