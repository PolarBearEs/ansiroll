#!/usr/bin/env python3
"""Run ansible-core's own integration test targets under the ansiroll strategy.

The official suite lives in the ansible/ansible source tree (not the pip
package).  Get a checkout matching the installed ansible-core version:

    git clone --depth 1 --branch v2.20.1 https://github.com/ansible/ansible.git /tmp/ansible-src

Then:

    python3 tests/run_official_suite.py [--src /tmp/ansible-src] [target ...]

Each curated target is executed twice — once with the stock ``linear``
strategy (baseline) and once with ``ansiroll.core.ansiroll`` — and the
exit codes and PLAY RECAP counters are compared.  A target whose baseline run
fails (usually because it needs the full ansible-test harness) is reported as
BASELINE-FAIL and does not count against parity.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ANSIROLL_STRATEGY = "ansiroll.core.ansiroll"
RECAP_RE = re.compile(
    r"^(?P<host>\S+)\s*:\s*ok=(?P<ok>\d+)\s+changed=(?P<changed>\d+)\s+"
    r"unreachable=(?P<unreachable>\d+)\s+failed=(?P<failed>\d+)\s+"
    r"skipped=(?P<skipped>\d+)\s+rescued=(?P<rescued>\d+)\s+ignored=(?P<ignored>\d+)"
)
TASK_RE = re.compile(r"^(?:TASK|RUNNING HANDLER) \[(?P<name>.+)] \*+$")
RESULT_RE = re.compile(
    r"^(?P<status>ok|changed|skipping|fatal|included): \[(?P<host>[^]]+)]"
)
VOLATILE_DATETIME_RE = re.compile(
    rb"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)

# These targets require ansible-test harness setup which this lightweight
# runner intentionally does not reproduce.  Any other baseline failure is a
# real harness regression and must fail the run.
EXPECTED_BASELINE_FAILURES = {
    "gathering/implicit.yml",
    "until/shim_until.yml",
}


@dataclasses.dataclass
class Spec:
    """One curated integration target."""

    name: str
    # Playbooks relative to the target dir; empty means wrap tasks as a role.
    playbooks: tuple[str, ...] = ()
    role: bool = False
    inventory: str | None = None  # relative to target dir; default: generated
    extra_args: tuple[str, ...] = ()
    env: dict[str, str] = dataclasses.field(default_factory=dict)
    # Some plays hardcode `strategy: "{{ test_strategy | default('linear') }}"`,
    # which beats the ANSIBLE_STRATEGY env var; pass it as an extra var too.
    strategy_var: str | None = None
    gather_facts: bool = True  # for the role shim


TARGETS = [
    Spec("conditionals", playbooks=("play.yml",)),
    Spec("register", playbooks=("can_register.yml",)),
    Spec("set_fact", playbooks=("set_fact.yml",)),
    Spec("environment", playbooks=("test_environment.yml",)),
    Spec("gathering", playbooks=("implicit.yml",), env={"ANSIBLE_GATHERING": "implicit"}),
    Spec("gathering", playbooks=("explicit.yml",), env={"ANSIBLE_GATHERING": "explicit"}),
    Spec("loop_control", playbooks=("label.yml", "extended.yml", "break_when.yml")),
    Spec("tags", playbooks=("test_tags.yml",)),
    Spec("blocks", playbooks=("main.yml",), strategy_var="test_strategy"),
    Spec(
        "handlers",
        playbooks=("test_handlers.yml",),
        inventory="inventory.handlers",
        extra_args=("--tags", "scenario1"),
    ),
    Spec("loops", role=True),
    Spec("until", role=True),
    Spec("loop-until", role=True),
    Spec("changed_when", role=True),
    Spec("failed_when", role=True),
    Spec("ignore_errors", role=True),
    Spec("special_vars", role=True),
    Spec("templating", role=True),
    Spec(
        "var_precedence",
        playbooks=("test_var_precedence.yml",),
        inventory="inventory",
        extra_args=(
            "-e", "extra_var=extra_var",
            "-e", "extra_var_override=extra_var_override",
        ),
        env={"ANSIBLE_GATHERING": "explicit"},
    ),
    Spec(
        "include_vars",
        playbooks=("test_as_playbook.yml",),
        inventory="../../inventory",
    ),
    Spec(
        "group_by",
        playbooks=("test_group_by.yml", "test_group_by_skipped.yml"),
        inventory="inventory.group_by",
    ),
    Spec(
        "delegate_to",
        playbooks=("delegate_and_nolog.yml",),
        inventory="inventory",
    ),
    Spec(
        "strategy_linear",
        playbooks=("task_templated_run_once.yml",),
        inventory="inventory",
    ),
    Spec(
        "check_mode",
        playbooks=("check_mode.yml", "check_mode-on-cli.yml"),
        extra_args=("--check",),
    ),
    Spec("check_mode", playbooks=("check_mode-not-on-cli.yml",)),
]


def parse_recap(output: str) -> dict[str, dict[str, int]]:
    recap: dict[str, dict[str, int]] = {}
    for line in output.splitlines():
        m = RECAP_RE.match(line.strip())
        if m:
            host = m.group("host")
            recap[host] = {k: int(v) for k, v in m.groupdict().items() if k != "host"}
    return recap


def parse_task_trace(output: str) -> collections.Counter[tuple[str, str, str]]:
    """Return task/result events, ignoring host scheduling order and payload formatting."""
    trace: collections.Counter[tuple[str, str, str]] = collections.Counter()
    task = None
    for raw in output.splitlines():
        line = raw.strip()
        task_match = TASK_RE.match(line)
        if task_match:
            task = task_match.group("name")
            continue
        result_match = RESULT_RE.match(line)
        if task is not None and result_match:
            status = result_match.group("status")
            status = "failed" if status == "fatal" else (
                "skipped" if status == "skipping" else status
            )
            host = result_match.group("host").split(" -> ", 1)[0]
            trace[(task, host, status)] += 1
    return trace


def snapshot_tree(path: Path) -> dict[str, bytes]:
    """Capture deterministic output artifacts produced under ``output_dir``."""
    snapshot = {}
    root = str(path).encode()
    for item in sorted(path.rglob("*")):
        if not item.is_file():
            continue
        data = item.read_bytes().replace(root, b"<OUTPUT_DIR>")
        data = VOLATILE_DATETIME_RE.sub(b"<DATETIME>", data)
        snapshot[str(item.relative_to(path))] = data
    return snapshot


def run_play(
    spec: Spec,
    target_dir: Path,
    playbook: Path,
    inventory: Path,
    strategy: str,
    output_dir: Path,
) -> tuple[int, dict[str, dict[str, int]], str]:
    env = os.environ.copy()
    env.update(spec.env)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["ANSIBLE_COLLECTIONS_PATH"] = str(REPO_ROOT / "collections")
    env["ANSIBLE_ROLES_PATH"] = str(target_dir.parent)
    env["ANSIBLE_STRATEGY"] = strategy
    env["ANSIBLE_NOCOLOR"] = "1"
    env.setdefault("ANSIBLE_DEPRECATION_WARNINGS", "0")
    cmd = [
        "ansible-playbook",
        "-i",
        str(inventory),
        str(playbook),
        "-e",
        f"output_dir={output_dir}",
    ]
    if spec.strategy_var:
        cmd += ["-e", f"{spec.strategy_var}={strategy}"]
    cmd += list(spec.extra_args)
    proc = subprocess.run(
        cmd, cwd=target_dir, env=env, capture_output=True, text=True, timeout=600
    )
    output = proc.stdout + proc.stderr
    return proc.returncode, parse_recap(proc.stdout), output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("targets", nargs="*", help="run only these target names")
    parser.add_argument(
        "--src",
        default=os.environ.get("ANSIBLE_SRC", "/tmp/ansible-src"),
        help="path to an ansible/ansible checkout (default: %(default)s)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="dump ansiroll output on mismatch")
    args = parser.parse_args()

    targets_dir = Path(args.src) / "test" / "integration" / "targets"
    if not targets_dir.is_dir():
        print(f"error: {targets_dir} not found — clone ansible-core first (see module docstring)")
        return 2

    specs = TARGETS
    if args.targets:
        specs = [s for s in TARGETS if s.name in args.targets]
        if not specs:
            print(f"error: no curated spec matches {args.targets}")
            return 2

    results: list[tuple[str, str, str]] = []  # (label, status, detail)
    with tempfile.TemporaryDirectory(prefix="ansiroll-official-") as tmp:
        tmpdir = Path(tmp)
        default_inventory = tmpdir / "inventory"
        default_inventory.write_text(
            "testhost ansible_connection=local"
            f" ansible_python_interpreter={sys.executable}\n"
            "[local]\ntesthost\n"
        )
        for spec in specs:
            target_dir = targets_dir / spec.name
            if not target_dir.is_dir():
                results.append((spec.name, "MISSING", "target not in checkout"))
                continue
            if spec.role:
                shim = tmpdir / f"shim_{spec.name}.yml"
                shim.write_text(
                    f"- hosts: testhost\n"
                    f"  gather_facts: {'true' if spec.gather_facts else 'false'}\n"
                    f"  roles:\n    - {spec.name}\n"
                )
                playbooks = [shim]
            else:
                playbooks = [target_dir / p for p in spec.playbooks]
            inventory = (
                target_dir / spec.inventory if spec.inventory else default_inventory
            )
            for playbook in playbooks:
                label = f"{spec.name}/{playbook.name}"
                output_root = tmpdir / "out" / label.replace("/", "_")
                baseline_output_dir = output_root / "linear"
                ansiroll_output_dir = output_root / "ansiroll"
                baseline_output_dir.mkdir(parents=True, exist_ok=True)
                ansiroll_output_dir.mkdir(parents=True, exist_ok=True)
                base_rc, base_recap, base_out = run_play(
                    spec, target_dir, playbook, inventory, "linear", baseline_output_dir
                )
                if base_rc != 0:
                    status = ("EXPECTED-BASELINE-FAIL" if label in EXPECTED_BASELINE_FAILURES
                              else "BASELINE-FAIL")
                    results.append((label, status, f"linear rc={base_rc}"))
                else:
                    ansi_rc, ansi_recap, ansi_out = run_play(
                        spec, target_dir, playbook, inventory, ANSIROLL_STRATEGY,
                        ansiroll_output_dir,
                    )
                    base_trace = parse_task_trace(base_out)
                    ansi_trace = parse_task_trace(ansi_out)
                    base_files = snapshot_tree(baseline_output_dir)
                    ansi_files = snapshot_tree(ansiroll_output_dir)
                    if (ansi_rc == base_rc and ansi_recap == base_recap
                            and ansi_trace == base_trace and ansi_files == base_files):
                        results.append((label, "PASS", ""))
                    else:
                        mismatches = []
                        if ansi_rc != base_rc:
                            mismatches.append(f"rc {base_rc}->{ansi_rc}")
                        if ansi_recap != base_recap:
                            mismatches.append(
                                f"recap linear={base_recap} ansiroll={ansi_recap}"
                            )
                        if ansi_trace != base_trace:
                            mismatches.append(
                                f"task trace linear={base_trace} ansiroll={ansi_trace}"
                            )
                        if ansi_files != base_files:
                            mismatches.append(
                                "output artifacts differ: linear=%s ansiroll=%s"
                                % (sorted(base_files), sorted(ansi_files))
                            )
                        detail = "; ".join(mismatches)
                        results.append((label, "PARITY-FAIL", detail))
                        if args.verbose:
                            print(f"\n===== {label} (ansiroll output) =====\n{ansi_out}")
                print(f"{results[-1][1]:>13}  {label}  {results[-1][2]}")

    counts: dict[str, int] = {}
    for _, status, _ in results:
        counts[status] = counts.get(status, 0) + 1
    print("\nSummary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    hard_failures = sum(counts.get(k, 0) for k in ("PARITY-FAIL", "BASELINE-FAIL", "MISSING"))
    return 1 if hard_failures else 0


if __name__ == "__main__":
    sys.exit(main())
