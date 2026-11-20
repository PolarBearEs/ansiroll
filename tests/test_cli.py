import json
import os
import stat
import subprocess
import sys
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
from threading import Event

import pytest

from ansiroll import cli


@pytest.fixture
def playbook(tmp_path):
    pytest.importorskip("ansible")
    path = tmp_path / "play.yml"
    path.write_text("- hosts: localhost\n  gather_facts: false\n  tasks: []\n")
    return path


def test_compile_writes_source_to_stdout(monkeypatch, playbook, capfdbinary):
    monkeypatch.chdir(playbook.parent)

    assert cli.main(["compile", "play.yml", "-o", "-", "-m", "manifest.json"]) == 0

    captured = capfdbinary.readouterr()
    assert captured.out.startswith(b"#!")
    compile(captured.out, "<stdout>", "exec")
    assert b"ansiroll: compiled play.yml -> stdout" in captured.err
    assert sorted(path.name for path in playbook.parent.iterdir()) == ["manifest.json", "play.yml"]
    assert json.loads((playbook.parent / "manifest.json").read_text())["playbook"] == str(playbook)


def test_compile_stdout_is_utf8_with_a_different_stdout_encoding(playbook):
    result = subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", str(playbook), "-o", "-"],
        env=dict(os.environ, PYTHONIOENCODING="utf-16le"),
        capture_output=True,
        check=True,
    )

    assert result.stdout.startswith(b"#!")
    compile(result.stdout, "<stdout>", "exec")


@pytest.mark.parametrize("forward_to_fd", [False, True])
def test_callable_compile_writes_to_the_callers_stdout(playbook, capfdbinary, forward_to_fd):
    class Tee(StringIO):
        def fileno(self):
            return 1

        def write(self, text):
            os.write(self.fileno(), text.encode("utf-8"))
            return super().write(text)

    output = Tee() if forward_to_fd else StringIO()

    with redirect_stdout(output):
        assert cli.main(["compile", str(playbook), "-o", "-"]) == 0

    source = output.getvalue()
    assert source.startswith("#!")
    compile(source, "<stdout>", "exec")
    captured = capfdbinary.readouterr()
    assert captured.out == (source.encode("utf-8") if forward_to_fd else b"")
    assert b" -> stdout " in captured.err


def test_callable_compile_leaves_host_stdout_untouched(monkeypatch, playbook, capfdbinary):
    worker_started = Event()
    allow_worker_to_finish = Event()
    run = subprocess.run

    def run_worker(*args, **kwargs):
        worker_started.set()
        assert allow_worker_to_finish.wait(5)
        return run(*args, **kwargs)

    monkeypatch.setattr(cli.subprocess, "run", run_worker)
    output = StringIO()
    with redirect_stdout(output), os.fdopen(os.dup(1), "wb") as retained_stdout:
        with ThreadPoolExecutor(max_workers=1) as pool:
            task = pool.submit(cli.main, ["compile", str(playbook), "-o", "-"])
            try:
                assert worker_started.wait(5)
                print("application output")
                os.write(1, b"fd 1 output\n")
                retained_stdout.write(b"retained descriptor output\n")
                retained_stdout.flush()
            finally:
                allow_worker_to_finish.set()
            assert task.result(timeout=10) == 0

    application_output, source = output.getvalue().split("\n", 1)
    assert application_output == "application output"
    compile(source, "<stdout>", "exec")
    captured = capfdbinary.readouterr()
    assert captured.out == b"fd 1 output\nretained descriptor output\n"
    assert b"application output" not in captured.err


def test_callable_compile_keeps_the_callers_fd_inheritance(playbook, monkeypatch, capfdbinary):
    inheritable = os.get_inheritable(1)
    args = ["ansiroll", "compile", str(playbook), "-o", "-"]
    monkeypatch.setattr(sys, "argv", args)
    output = StringIO()
    with redirect_stdout(output):
        os.set_inheritable(1, False)
        try:
            assert cli.main() == 0
            assert not os.get_inheritable(1)
        finally:
            os.set_inheritable(1, inheritable)
    assert output.getvalue().startswith("#!")
    assert capfdbinary.readouterr().out == b""


@pytest.mark.parametrize("compile_fails", [False, True])
@pytest.mark.parametrize("invocation", ["entrypoint", "worker"])
def test_compile_flushes_buffered_diagnostics_to_stderr(compile_fails, invocation):
    script = r"""
import atexit
import ctypes
import subprocess
import sys
from ansiroll import cli

puts = ctypes.CDLL(None).puts
puts.argtypes = [ctypes.c_char_p]

def compile_playbook(*args, **kwargs):
    sys.__stdout__.write("buffered diagnostic\n")
    puts(b"native diagnostic")
    atexit.register(puts, b"native shutdown diagnostic")
    subprocess.run([sys.executable, "-c", "print('helper diagnostic')"], check=True)
    if compile_fails == "True":
        raise ValueError("compile failed")
    return "print('ok')\n", {"modules": [], "fallback_tasks": []}

cli.compile_playbook = compile_playbook
compile_fails, invocation = sys.argv[1:]
args = ["compile", "play.yml", "-o", "-"]
sys.argv[1:] = args
if invocation == "entrypoint":
    raise SystemExit(cli.entrypoint())
raise SystemExit(cli._compile_worker())
"""
    env = dict(os.environ)
    env.pop("PYTHONUNBUFFERED", None)

    result = subprocess.run(
        [sys.executable, "-c", script, str(compile_fails), invocation],
        input=b'{"playbook": "play.yml", "extra_vars": [], "vault_password_file": null, '
              b'"interpreter": "python3", "tree_shake": false, "freeze_playbook_vars": false, '
              b'"freeze_var": []}',
        capture_output=True, env=env,
    )

    assert result.returncode == (2 if compile_fails else 0), result.stderr
    source = b"" if compile_fails else b"print('ok')\n"
    if compile_fails or invocation == "entrypoint":
        assert result.stdout == source
    else:
        assert json.loads(result.stdout)[0] == source.decode("utf-8")
    assert b"native shutdown diagnostic\n" in result.stderr
    assert b"buffered diagnostic\n" in result.stderr
    assert b"native diagnostic\n" in result.stderr
    assert b"helper diagnostic\n" in result.stderr


def test_compile_rejects_manifest_that_points_to_stdout(capfdbinary):
    output = StringIO()
    with redirect_stdout(output):
        assert cli.main(["compile", "play.yml", "-o", "-", "-m", "/dev/stdout"]) == 2

    assert not output.getvalue()
    captured = capfdbinary.readouterr()
    assert captured.out == b""
    assert b"manifest path points to stdout" in captured.err


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="requires procfs file descriptors")
def test_compile_rejects_manifest_that_points_to_saved_stdout():
    script = """
import os
import sys
from ansiroll import cli

cli.compile_playbook = lambda *args, **kwargs: ("pass", {"modules": [], "fallback_tasks": []})
next_fd = os.dup(1)
os.close(next_fd)
sys.argv[1:] = ["compile", "play.yml", "-o", "-", "-m", "/proc/self/fd/%d" % next_fd]
raise SystemExit(cli.entrypoint())
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True)

    assert result.returncode == 2
    assert result.stdout == b""
    assert b"manifest path points to stdout" in result.stderr


@pytest.mark.parametrize("manifest_path", [os.devnull, "/dev/stdout"])
def test_compile_allows_manifest_and_source_to_be_discarded(playbook, manifest_path):
    result = subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", str(playbook),
         "-o", "-", "-m", manifest_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True,
    )

    assert b" -> stdout " in result.stderr
    assert b'"fallback_tasks":' not in result.stderr


def test_compile_stdout_stays_empty_if_manifest_write_fails(playbook, capfdbinary):
    with pytest.raises(IsADirectoryError):
        cli.main(["compile", str(playbook), "-o", "-", "-m", str(playbook.parent)])

    assert capfdbinary.readouterr().out == b""


def test_compile_writes_executable_file(monkeypatch, tmp_path, capsys):
    source = "print('ok')\n"
    manifest = {"modules": [], "fallback_tasks": []}
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: (source, manifest))
    output = tmp_path / "compiled.py"
    output.write_text("old source\n")
    output.chmod(0o640)

    assert cli.main(["compile", "play.yml", "-o", str(output)]) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ansiroll: compiled play.yml -> %s" % output in captured.err
    assert output.read_text() == source
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
