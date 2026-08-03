#!/usr/bin/env python3
"""Run Ansiroll against matching ansible-core releases and source trees.

The runner creates isolated uv environments under /tmp, installs the newest
patch release in each requested ansible-core series, checks out that exact tag
from ansible/ansible, then runs the repository tests and upstream parity suite.

Examples:

    python3 tests/run_version_matrix.py
    python3 tests/run_version_matrix.py 2.19 2.21
    python3 tests/run_version_matrix.py 2.20 --profile full
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = Path("/tmp/ansiroll-version-matrix")
DEFAULT_VERSIONS = ("2.19", "2.20", "2.21")

# Targets whose locations and harness requirements are stable across maintained
# ansible-core releases.  Each run still compares against that release's own
# linear strategy, so intentional semantic changes do not become false failures.
PORTABLE_TARGETS = (
    "conditionals",
    "register",
    "set_fact",
    "environment",
    "loop_control",
    "tags",
    "blocks",
    "handlers",
    "changed_when",
    "failed_when",
    "ignore_errors",
    "templating",
    "strategy_linear",
    "check_mode",
)

# A deliberately conservative subset for end-of-life releases.  These targets
# existed with the same entry points before the maintained portable set gained
# newer files such as loop_control/break_when.yml.
LEGACY_SMOKE_TARGETS = (
    "tags",
    "blocks",
    "handlers",
    "failed_when",
    "ignore_errors",
)

PYTHON_BY_SERIES = {
    "2.10": "3.9",
    "2.12": "3.9",
    "2.14": "3.10",
    "2.16": "3.11",
    "2.17": "3.11",
    "2.18": "3.12",
    "2.19": "3.13",
    "2.20": "3.13",
    "2.21": "3.13",
}


def run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=REPO_ROOT, env=env, check=True)


def output(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, cwd=REPO_ROOT, text=True).strip()


def distribution(series: str) -> str:
    if series == "2.10":
        return "ansible-base==2.10.17"
    return f"ansible-core=={series}.*"


def effective_profile(series: str, requested: str) -> str:
    if requested != "auto":
        return requested
    if series == "2.20":
        return "full"
    if tuple(map(int, series.split("."))) >= (2, 19):
        return "portable"
    return "smoke"


def prepare_environment(series: str, root: Path, recreate: bool) -> tuple[Path, str]:
    python_version = PYTHON_BY_SERIES.get(series)
    if python_version is None:
        raise ValueError("unsupported series %s; choose one of %s" % (
            series, ", ".join(PYTHON_BY_SERIES)
        ))
    venv = root / ("venv-" + series)
    if recreate and venv.exists():
        shutil.rmtree(venv)
    if not venv.exists():
        run(["uv", "venv", "--python", python_version, str(venv)])
    python = venv / "bin" / "python"
    run([
        "uv", "pip", "install", "--python", str(python), "--upgrade",
        distribution(series), "jinja2", "packaging", "pytest",
    ])
    run([
        "uv", "pip", "install", "--python", str(python), "--no-deps",
        "--editable", str(REPO_ROOT),
    ])
    installed = output([
        str(python), "-c", "import ansible; print(ansible.__version__)"
    ])
    if not installed.startswith(series + "."):
        raise RuntimeError(
            "requested ansible-core %s but installed %s" % (series, installed)
        )
    return python, installed


def prepare_source(version: str, root: Path, recreate: bool) -> Path:
    source = root / ("ansible-" + version)
    expected_tag = "v" + version
    if source.exists():
        try:
            actual = output([
                "git", "-C", str(source), "describe", "--tags", "--exact-match",
            ])
        except subprocess.CalledProcessError:
            actual = ""
        if recreate or actual != expected_tag:
            shutil.rmtree(source)
    if not source.exists():
        run([
            "git", "clone", "--depth", "1", "--branch", expected_tag,
            "https://github.com/ansible/ansible.git", str(source),
        ])
    return source


def run_one(
    series: str,
    root: Path,
    profile: str,
    recreate: bool,
    skip_repo_tests: bool,
    skip_upstream: bool,
) -> None:
    print("\n===== ansible-core %s =====" % series, flush=True)
    python, installed = prepare_environment(series, root, recreate)
    env = os.environ.copy()
    env["PATH"] = str(python.parent) + os.pathsep + env.get("PATH", "")
    env["ANSIBLE_COLLECTIONS_PATH"] = str(REPO_ROOT / "collections")
    env["ANSIBLE_DEPRECATION_WARNINGS"] = "False"
    if not skip_repo_tests:
        run([str(python), "-m", "pytest", "tests", "-q"], env=env)

    if skip_upstream:
        return

    source = prepare_source(installed, root, recreate)

    selected_profile = effective_profile(series, profile)
    cmd = [
        str(python), "tests/run_official_suite.py", "--src", str(source),
    ]
    if selected_profile == "portable":
        cmd.extend(PORTABLE_TARGETS)
    elif selected_profile == "smoke":
        cmd.extend(LEGACY_SMOKE_TARGETS)
    run(cmd, env=env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "versions", nargs="*", default=list(DEFAULT_VERSIONS),
        help="ansible-core series to test (default: %(default)s)",
    )
    parser.add_argument(
        "--profile", choices=("auto", "smoke", "portable", "full"), default="auto",
        help=("upstream target profile; auto runs full on 2.20, portable on "
              "maintained adjacent releases, and smoke on legacy releases"),
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--recreate", action="store_true")
    parser.add_argument("--skip-repo-tests", action="store_true")
    parser.add_argument("--skip-upstream", action="store_true")
    args = parser.parse_args()

    if shutil.which("uv") is None:
        parser.error("uv is required to provision compatible Python environments")
    args.root.mkdir(parents=True, exist_ok=True)
    failures = []
    for series in args.versions:
        try:
            run_one(
                series, args.root, args.profile, args.recreate,
                args.skip_repo_tests, args.skip_upstream,
            )
        except (subprocess.CalledProcessError, RuntimeError, ValueError) as ex:
            failures.append((series, str(ex)))
            print("FAILED ansible-core %s: %s" % (series, ex), file=sys.stderr)
    if failures:
        print("\nVersion matrix failures:", file=sys.stderr)
        for series, detail in failures:
            print("  %s: %s" % (series, detail), file=sys.stderr)
        return 1
    print("\nVersion matrix passed: %s" % ", ".join(args.versions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
