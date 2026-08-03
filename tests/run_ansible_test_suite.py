#!/usr/bin/env python3
"""Run upstream ansible-test targets in Docker under linear and Ansiroll.

The matching ansible-core source tag is the immutable linear baseline.  For the
Ansiroll run, the runner creates a disposable git worktree, injects Ansiroll
into the source payload, and adds an ``ANSIBLE_STRATEGY`` alias only to the
selected targets.  ansible-test creates a fresh Docker controller for each run.

Examples:

    python3 tests/run_ansible_test_suite.py 2.20
    python3 tests/run_ansible_test_suite.py 2.12 2.18
    python3 tests/run_ansible_test_suite.py 2.20 --target failed_when
    python3 tests/run_ansible_test_suite.py 2.20 --profile full
    python3 tests/run_ansible_test_suite.py 2.20 --profile native --native-only
    python3 tests/run_ansible_test_suite.py 2.21 --profile portable
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.run_official_suite import RECAP_RE, parse_task_trace
from tests.run_version_matrix import (
    DEFAULT_ROOT,
    PORTABLE_TARGETS,
    PYTHON_BY_SERIES,
    REPO_ROOT,
    prepare_environment,
    prepare_source,
)


DEFAULT_VERSIONS = ("2.12", "2.14", "2.16", "2.18", "2.19", "2.20", "2.21")
STRATEGY_ALIAS = "env/set/ANSIBLE_STRATEGY/ansiroll"
NATIVE_ONLY_ALIAS = "env/set/ANSIROLL_NATIVE_ONLY/1"
PROVENANCE_ALIAS = "env/set/ANSIROLL_PROVENANCE/1"

# Unlike run_official_suite.py, ansible-test runs an entire target, including
# its runme.sh and declared setup dependencies.  Keep the automatic profile to
# targets verified to usefully inherit ANSIBLE_STRATEGY on each source series.
SMOKE_TARGETS_BY_SERIES = {
    "2.10": ("tags", "failed_when", "ignore_errors"),
    "2.12": ("tags", "failed_when", "ignore_errors"),
    "2.14": ("tags", "register", "changed_when", "failed_when", "ignore_errors"),
    "2.16": ("tags", "register", "changed_when", "failed_when", "ignore_errors"),
    "2.17": ("tags", "register", "changed_when", "failed_when", "ignore_errors"),
    "2.18": ("tags", "register", "changed_when", "failed_when", "ignore_errors"),
    "2.19": (
        "tags", "register", "changed_when", "failed_when", "ignore_errors",
        "templating",
    ),
    "2.20": (
        "tags", "register", "changed_when", "failed_when", "ignore_errors",
        "templating",
    ),
    "2.21": (
        # The 2.21 ignore_errors target added cross-host hostvars assertions
        # which intentionally require task-lockstep execution. Ansiroll's
        # documented execution model is host-sequential, so keep that target
        # available through --target as a known-gap reproducer, not a smoke pass.
        "tags", "register", "changed_when", "failed_when", "templating",
    ),
}

FULL_TARGETS_BY_SERIES = {
    # Complete targets validated against the unmodified 2.20 linear strategy.
    # This is intentionally curated: the full ansible-core integration tree
    # also contains cloud/network credentials, destructive host mutation,
    # platform-specific dependencies, and tests unrelated to strategy behavior.
    "2.20": (
        "tags",
        "register",
        "changed_when",
        "failed_when",
        "ignore_errors",
        "templating",
        "conditionals",
        "set_fact",
        "blocks",
        "strategy_linear",
    ),
}

NATIVE_TARGETS_BY_SERIES = {
    # Targets proven to complete without linear/controller fallback. Keep this
    # separate from hybrid parity so new fallback cannot silently weaken the
    # native compatibility claim.
    "2.20": (
        "conditionals",
        "set_fact",
    ),
}


def selected_targets(series: str, profile: str, explicit: list[str]) -> tuple[str, ...]:
    if explicit:
        return tuple(dict.fromkeys(explicit))
    if profile == "portable":
        return PORTABLE_TARGETS
    if profile == "full":
        try:
            return FULL_TARGETS_BY_SERIES[series]
        except KeyError as ex:
            raise ValueError(
                "no validated full ansible-test profile for %s" % series
            ) from ex
    if profile == "native":
        try:
            return NATIVE_TARGETS_BY_SERIES[series]
        except KeyError as ex:
            raise ValueError(
                "no validated native ansible-test profile for %s" % series
            ) from ex
    try:
        return SMOKE_TARGETS_BY_SERIES[series]
    except KeyError as ex:
        raise ValueError("no ansible-test smoke profile for %s" % series) from ex


def parse_recaps(output: str) -> list[dict[str, dict[str, int]]]:
    """Return every PLAY RECAP in ansible-test's combined command output."""
    recaps: list[dict[str, dict[str, int]]] = []
    current: dict[str, dict[str, int]] | None = None
    for line in output.splitlines():
        if line.startswith("PLAY RECAP "):
            if current:
                recaps.append(current)
            current = {}
            continue
        if current is None:
            continue
        match = RECAP_RE.match(line)
        if match:
            current[match.group("host")] = {
                key: int(match.group(key))
                for key in (
                    "ok", "changed", "unreachable", "failed", "skipped",
                    "rescued", "ignored",
                )
            }
        elif current and not line.strip():
            recaps.append(current)
            current = None
    if current:
        recaps.append(current)
    return recaps


def parse_provenance(output: str) -> list[dict[str, object]]:
    prefix = "ANSIROLL_PROVENANCE "
    records = []
    for line in output.splitlines():
        if not line.startswith(prefix):
            continue
        records.append(json.loads(line[len(prefix):]))
    return records


def add_injected_aliases(
    path: Path,
    *,
    native_only: bool = False,
    provenance: bool = False,
) -> None:
    """Make one integration target inherit injected settings idempotently."""
    lines = path.read_text().splitlines()
    required = [STRATEGY_ALIAS]
    if native_only:
        required.append(NATIVE_ONLY_ALIAS)
    if provenance:
        required.append(PROVENANCE_ALIAS)
    for alias in required:
        if alias not in lines:
            lines.append(alias)
    path.write_text("\n".join(lines) + "\n")


def add_strategy_alias(path: Path) -> None:
    """Backward-compatible helper for tests and external callers."""
    add_injected_aliases(path)


def remove_worktree(source: Path, worktree: Path) -> None:
    subprocess.run(
        ["git", "-C", str(source), "worktree", "remove", "--force", str(worktree)],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if worktree.exists():
        shutil.rmtree(worktree)
    subprocess.run(
        ["git", "-C", str(source), "worktree", "prune"],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def prepare_injected_worktree(
    source: Path,
    worktree: Path,
    targets: tuple[str, ...],
    *,
    native_only: bool = False,
    provenance: bool = False,
) -> None:
    """Create a staged disposable source tree that ansible-test will package."""
    remove_worktree(source, worktree)
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run(
        ["git", "-C", str(source), "worktree", "add", "--detach", str(worktree), revision],
        cwd=REPO_ROOT,
        check=True,
    )

    package_target = worktree / "lib" / "ansiroll"
    shutil.copytree(
        REPO_ROOT / "ansiroll",
        package_target,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    strategy_target = worktree / "lib" / "ansible" / "plugins" / "strategy" / "ansiroll.py"
    shutil.copy2(
        REPO_ROOT
        / "collections" / "ansible_collections" / "ansiroll" / "core"
        / "plugins" / "strategy" / "ansiroll.py",
        strategy_target,
    )

    aliases = []
    for target in targets:
        target_dir = worktree / "test" / "integration" / "targets" / target
        alias_path = target_dir / "aliases"
        if not target_dir.is_dir() or not alias_path.is_file():
            raise ValueError("ansible-test target %s is missing from %s" % (target, worktree))
        add_injected_aliases(
            alias_path,
            native_only=native_only,
            provenance=provenance,
        )
        aliases.append(alias_path)

    paths = [package_target, strategy_target, *aliases]
    subprocess.run(
        ["git", "-C", str(worktree), "add", "-f", "--", *map(str, paths)],
        cwd=REPO_ROOT,
        check=True,
    )


def stream_command(cmd: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    print("+", " ".join(cmd), flush=True)
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    lines = []
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line)
        sys.stdout.write(line)
        sys.stdout.flush()
    return proc.wait(), "".join(lines)


def ansible_test_command(
    python: Path,
    source: Path,
    targets: tuple[str, ...],
    image: str,
    python_version: str,
    verbosity: int,
) -> list[str]:
    cmd = [
        str(python), str(source / "bin" / "ansible-test"), "integration",
        *targets,
        "--docker", image,
        "--python", python_version,
        "--docker-terminate", "always",
        "--color", "no",
        "--truncate", "0",
    ]
    if verbosity:
        cmd.append("-" + "v" * verbosity)
    return cmd


def compare_runs(
    baseline_rc: int,
    baseline_output: str,
    ansiroll_rc: int,
    ansiroll_output: str,
) -> list[str]:
    mismatches = []
    if baseline_rc:
        mismatches.append("linear ansible-test rc=%d" % baseline_rc)
        return mismatches
    if ansiroll_rc != baseline_rc:
        mismatches.append("rc %d->%d" % (baseline_rc, ansiroll_rc))
    baseline_recaps = parse_recaps(baseline_output)
    ansiroll_recaps = parse_recaps(ansiroll_output)
    if ansiroll_recaps != baseline_recaps:
        mismatches.append(
            "recaps linear=%r ansiroll=%r" % (baseline_recaps, ansiroll_recaps)
        )
    baseline_trace = parse_task_trace(baseline_output)
    ansiroll_trace = parse_task_trace(ansiroll_output)
    if ansiroll_trace != baseline_trace:
        mismatches.append(
            "task trace linear=%r ansiroll=%r"
            % (baseline_trace, ansiroll_trace)
        )
    return mismatches


def run_series(
    series: str,
    targets: tuple[str, ...],
    root: Path,
    image: str,
    recreate: bool,
    verbosity: int,
    keep_worktree: bool,
    native_only: bool = False,
    provenance: bool = False,
) -> None:
    print("\n===== ansible-test Docker: ansible-core %s =====" % series, flush=True)
    python, installed = prepare_environment(series, root, recreate)
    source = prepare_source(installed, root, recreate)
    worktree = root / ("ansible-test-ansiroll-" + installed)
    isolated_home = root / ("ansible-test-home-" + installed)
    if recreate and isolated_home.exists():
        shutil.rmtree(isolated_home)
    isolated_home.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
    env["HOME"] = str(isolated_home)
    env["ANSIBLE_NOCOLOR"] = "1"
    env["ANSIBLE_DEPRECATION_WARNINGS"] = "False"
    if native_only:
        env["ANSIROLL_NATIVE_ONLY"] = "1"
    if provenance:
        env["ANSIROLL_PROVENANCE"] = "1"

    python_version = PYTHON_BY_SERIES[series]
    baseline_cmd = ansible_test_command(
        python, source, targets, image, python_version, verbosity
    )
    baseline_rc, baseline_output = stream_command(baseline_cmd, source, env)
    if baseline_rc:
        raise RuntimeError(
            "linear ansible-test baseline failed for %s (rc=%d)"
            % (series, baseline_rc)
        )

    try:
        prepare_injected_worktree(
            source,
            worktree,
            targets,
            native_only=native_only,
            provenance=provenance,
        )
        ansiroll_cmd = ansible_test_command(
            python, worktree, targets, image, python_version, verbosity
        )
        ansiroll_rc, ansiroll_output = stream_command(
            ansiroll_cmd, worktree, env
        )
        mismatches = compare_runs(
            baseline_rc, baseline_output, ansiroll_rc, ansiroll_output
        )
        if provenance:
            records = parse_provenance(ansiroll_output)
            if not records:
                mismatches.append("no Ansiroll provenance records emitted")
            fallback_records = [
                record for record in records
                if str(record.get("mode", "")).endswith("fallback")
            ]
            if native_only and fallback_records:
                mismatches.append(
                    "native-only run emitted fallback provenance %r"
                    % fallback_records
                )
        if mismatches:
            raise RuntimeError("; ".join(mismatches))
    finally:
        if not keep_worktree:
            remove_worktree(source, worktree)

    print(
        "PASS ansible-test %s: %s" % (series, ", ".join(targets)),
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "versions", nargs="*", default=list(DEFAULT_VERSIONS),
        help="ansible-core series (default: %(default)s)",
    )
    parser.add_argument(
        "--profile", choices=("smoke", "portable", "full", "native"),
        default="smoke",
        help="target profile used when --target is omitted",
    )
    parser.add_argument(
        "--target", action="append", default=[],
        help="explicit ansible-test target; repeat to select multiple",
    )
    parser.add_argument("--image", default="default", help="ansible-test Docker image")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--recreate", action="store_true")
    parser.add_argument("--keep-worktree", action="store_true")
    parser.add_argument(
        "--native-only", action="store_true",
        help="reject every Ansiroll linear/controller fallback",
    )
    parser.add_argument(
        "--provenance", action="store_true",
        help="print machine-readable Ansiroll execution provenance",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args()

    if shutil.which("uv") is None:
        parser.error("uv is required to provision compatible controller Python versions")
    if shutil.which("docker") is None:
        parser.error("Docker is required; ansible-test will not run on the host")
    docker_probe = subprocess.run(
        ["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if docker_probe.returncode:
        parser.error("the Docker daemon is unavailable")

    args.root.mkdir(parents=True, exist_ok=True)
    failures = []
    for series in args.versions:
        try:
            targets = selected_targets(series, args.profile, args.target)
            run_series(
                series, targets, args.root, args.image, args.recreate,
                args.verbose, args.keep_worktree,
                native_only=args.native_only,
                provenance=args.provenance,
            )
        except (subprocess.CalledProcessError, RuntimeError, ValueError) as ex:
            failures.append((series, str(ex)))
            print("FAILED ansible-test %s: %s" % (series, ex), file=sys.stderr)

    if failures:
        print("\nansible-test Docker failures:", file=sys.stderr)
        for series, detail in failures:
            print("  %s: %s" % (series, detail), file=sys.stderr)
        return 1
    print("\nansible-test Docker matrix passed: %s" % ", ".join(args.versions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
