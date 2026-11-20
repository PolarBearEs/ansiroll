import json
import os
import stat
import subprocess
import sys
from argparse import Namespace

import pytest

from ansiroll import cli


def test_compile_streams_executable_python_and_writes_manifest(tmp_path):
    pytest.importorskip("ansible")
    playbook = tmp_path / "play.yml"
    playbook.write_text("- hosts: localhost\n  gather_facts: false\n  tasks: []\n")
    manifest = tmp_path / "manifest.json"

    result = subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", str(playbook),
         "-o", "-", "-m", str(manifest)],
        env=dict(os.environ, PYTHONIOENCODING="utf-16le"),
        capture_output=True, check=True,
    )

    assert result.stdout.startswith(b"#!")
    subprocess.run([sys.executable, "-"], input=result.stdout, capture_output=True, check=True)
    assert " -> stdout " in result.stderr.decode("utf-16le")
    assert json.loads(manifest.read_text())["playbook"] == str(playbook)


def test_stdout_compile_keeps_compiler_subprocess_diagnostics_on_stderr():
    script = """
import ctypes
import subprocess
import sys
from ansiroll import cli

def compile_playbook(*args, **kwargs):
    print("compiler diagnostic")
    ctypes.CDLL(None).puts(b"native diagnostic")
    subprocess.run([sys.executable, "-c", "print('helper diagnostic')"], check=True)
    return "pass\\n", {"modules": [], "fallback_tasks": []}

cli.compile_playbook = compile_playbook
sys.argv[1:] = ["compile", "play.yml", "-o", "-"]
raise SystemExit(cli.entrypoint())
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, check=True)
    assert result.stdout == b"pass\n"
    assert b"compiler diagnostic\n" in result.stderr
    assert b"native diagnostic\n" in result.stderr
    assert b"helper diagnostic\n" in result.stderr


def test_stdout_compile_rejects_manifest_alias():
    result = subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", "play.yml",
         "-o", "-", "-m", "/dev/stdout"],
        capture_output=True,
    )

    assert result.returncode == 2
    assert result.stdout == b""
    assert b"manifest path points to stdout" in result.stderr


@pytest.mark.parametrize("destination", ["pipe", "file"])
def test_stdout_compile_rejects_merged_diagnostic_stream(tmp_path, destination):
    path = tmp_path / "compiled.py"
    with path.open("wb") as stream:
        result = subprocess.run(
            [sys.executable, "-m", "ansiroll.cli", "compile", "play.yml", "-o", "-"],
            stdout=subprocess.PIPE if destination == "pipe" else stream,
            stderr=subprocess.STDOUT,
        )

    assert result.returncode == 2
    output = result.stdout if destination == "pipe" else path.read_bytes()
    assert output == b"ansiroll: compile -o - requires separate stdout and stderr streams\n"


def test_stdout_compile_rejects_shared_terminal():
    pty = pytest.importorskip("pty")
    master, slave = pty.openpty()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ansiroll.cli", "compile", "play.yml", "-o", "-"],
            stdout=slave, stderr=slave,
        )
        os.set_blocking(master, False)
        output = os.read(master, 4096)
    finally:
        os.close(slave)
        os.close(master)

    assert result.returncode == 2
    assert output.rstrip() == b"ansiroll: compile -o - requires separate stdout and stderr streams"


@pytest.mark.parametrize("redirection", ['exec "$@" 2>&-', 'exec "$@" 2</dev/null'])
def test_compile_streams_python_without_writable_stderr(redirection):
    script = r"""
import sys
from ansiroll import cli

cli.compile_playbook = lambda *a, **k: ("pass\n", {"modules": [], "fallback_tasks": []})
sys.argv[1:] = ["compile", "\u00e1.yml", "-o", "-"]
raise SystemExit(cli.entrypoint())
"""
    result = subprocess.run(
        ["sh", "-c", redirection, "ansiroll", sys.executable, "-c", script],
        env=dict(os.environ, LC_ALL="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0",
                 PYTHONIOENCODING="ascii"),
        capture_output=True, check=True,
    )

    assert result.stdout == b"pass\n"
    assert result.stderr == b""


def test_compile_writes_executable_file(monkeypatch, tmp_path, capsys):
    manifest = {"modules": [], "fallback_tasks": []}
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", manifest))
    output = tmp_path / "compiled.py"
    output.touch()
    output.chmod(0o640)

    assert cli.main(["compile", "play.yml", "-o", str(output)]) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ansiroll: compiled play.yml -> %s" % output in captured.err
    assert output.read_text() == "pass\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o750


def test_run_streams_compiled_source_without_creating_a_file(monkeypatch):
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("print('ok')\n", {}))
    monkeypatch.setattr(
        cli.tempfile, "mkstemp",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected temp file")),
    )
    called = {}

    def fake_run(command, **kwargs):
        called["command"] = command
        called.update(kwargs)
        return Namespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    args = Namespace(playbook="play.yml", interpreter="python3", keep=False,
                     vault_password_file=None)

    assert cli._cmd_run(args) == 0
    assert called == {
        "command": ["python3", "-"],
        "input": "print('ok')\n",
        "universal_newlines": True,
    }
