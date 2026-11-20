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


@pytest.mark.parametrize("echo", [False, True])
def test_stdout_compile_checks_echo_between_pseudoterminal_ends(echo):
    pty = pytest.importorskip("pty")
    import tty

    script = """
import sys
from ansiroll import cli

cli.compile_playbook = lambda *a, **k: ("pass\\n", {"modules": [], "fallback_tasks": []})
sys.argv[1:] = ["compile", "play.yml", "-o", "-"]
raise SystemExit(cli.entrypoint())
"""
    master, slave = pty.openpty()
    try:
        if not echo:
            tty.setraw(slave)
        result = subprocess.run([sys.executable, "-c", script], stdout=slave, stderr=master)
        os.set_blocking(master, False)
        os.set_blocking(slave, False)
        if echo:
            assert result.returncode == 2
            assert b"requires separate stdout and stderr" in os.read(slave, 4096)
        else:
            assert result.returncode == 0
            assert os.read(master, 4096) == b"pass\n"
            assert b" -> stdout " in os.read(slave, 4096)
    finally:
        os.close(slave)
        os.close(master)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux controlling terminals")
@pytest.mark.parametrize("alias", ["stderr", "manifest"])
@pytest.mark.parametrize("same_terminal", [False, True])
def test_stdout_compile_checks_controlling_terminal_alias(alias, same_terminal):
    import pty
    from select import select

    script = r"""
import fcntl
import os
import sys
import termios
from ansiroll import cli

fcntl.ioctl(0, termios.TIOCSCTTY, 0)
cli.compile_playbook = lambda *a, **k: ("pass\n", {"modules": [], "fallback_tasks": []})
args = ["compile", "play.yml", "-o", "-"]
if sys.argv[1] == "stderr":
    with open("/dev/tty", "w") as terminal:
        os.dup2(terminal.fileno(), 2)
else:
    args.extend(["-m", "/dev/tty"])
sys.argv[1:] = args
raise SystemExit(cli.entrypoint())
"""
    master, slave = pty.openpty()
    controlling_master, controlling_slave = pty.openpty()
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, alias],
            stdin=slave if same_terminal else controlling_slave,
            stdout=slave, stderr=subprocess.PIPE, start_new_session=True,
        )
        output = os.read(master, 4096) if select([master], [], [], 0)[0] else b""
        if not same_terminal:
            assert result.returncode == 0, result.stderr
            assert output == b"pass\r\n"
            os.set_blocking(controlling_master, False)
            diagnostics = os.read(controlling_master, 4096)
            expected = b" -> stdout " if alias == "stderr" else b'"fallback_tasks": []'
            assert expected in diagnostics
    finally:
        os.close(controlling_slave)
        os.close(controlling_master)
        os.close(slave)
        os.close(master)

    if same_terminal:
        assert result.returncode == 2
        if alias == "stderr":
            assert output.rstrip() == b"ansiroll: compile -o - requires separate stdout and stderr streams"
        else:
            assert output == b""
            assert b"manifest path points to stdout" in result.stderr


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires Linux PTY masters")
def test_stdout_compile_accepts_independent_pseudoterminal_masters():
    import pty
    import tty

    script = r"""
import sys
from ansiroll import cli

cli.compile_playbook = lambda *a, **k: ("pass\n", {"modules": [], "fallback_tasks": []})
sys.argv[1:] = ["compile", "play.yml", "-o", "-"]
raise SystemExit(cli.entrypoint())
"""
    stdout_master, stdout_slave = pty.openpty()
    stderr_master, stderr_slave = pty.openpty()
    try:
        tty.setraw(stdout_slave)
        tty.setraw(stderr_slave)
        subprocess.run(
            [sys.executable, "-c", script], stdout=stdout_master, stderr=stderr_master, check=True,
        )
        os.set_blocking(stdout_slave, False)
        os.set_blocking(stderr_slave, False)
        assert os.read(stdout_slave, 4096) == b"pass\n"
        assert b" -> stdout " in os.read(stderr_slave, 4096)
    finally:
        for fd in (stdout_master, stdout_slave, stderr_master, stderr_slave):
            os.close(fd)


def test_stdout_compile_rejects_manifest_replaced_with_alias(tmp_path):
    script = """
import os
import sys
from ansiroll import cli

output, manifest = sys.argv[1:]
cli.compile_playbook = lambda *a, **k: ("pass", {"modules": [], "fallback_tasks": []})
open_fd = os.open

def replace_manifest(path, flags, *args, **kwargs):
    if path == manifest:
        os.unlink(manifest)
        os.symlink(output, manifest)
    return open_fd(path, flags, *args, **kwargs)

os.open = replace_manifest
sys.argv[1:] = ["compile", "play.yml", "-o", "-", "-m", manifest]
raise SystemExit(cli.entrypoint())
"""
    output = tmp_path / "compiled.py"
    output.write_text("# existing output\n")
    manifest = tmp_path / "manifest.json"
    manifest.touch()
    with output.open("r+b") as stream:
        result = subprocess.run(
            [sys.executable, "-c", script, str(output), str(manifest)],
            stdout=stream, stderr=subprocess.PIPE,
        )

    assert result.returncode == 2
    assert output.read_text() == "# existing output\n"
    assert b"manifest path points to stdout" in result.stderr


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
