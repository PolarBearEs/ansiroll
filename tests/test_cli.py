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
    popen = subprocess.Popen

    def start_worker(*args, **kwargs):
        worker = popen(*args, **kwargs)
        worker_started.set()
        if not allow_worker_to_finish.wait(5):
            worker.kill()
            worker.communicate()
            pytest.fail("host output was not checked while the worker was active")
        return worker

    monkeypatch.setattr(cli.subprocess, "Popen", start_worker)
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
def test_compile_flushes_buffered_diagnostics_to_stderr(compile_fails):
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
compile_fails = sys.argv[1]
sys.argv[1:] = ["compile", "play.yml", "-o", "-"]
raise SystemExit(cli.entrypoint())
"""
    env = dict(os.environ)
    env.pop("PYTHONUNBUFFERED", None)

    result = subprocess.run(
        [sys.executable, "-c", script, str(compile_fails)], capture_output=True, env=env,
    )

    assert result.returncode == (2 if compile_fails else 0), result.stderr
    assert result.stdout == (b"" if compile_fails else b"print('ok')\n")
    assert b"native shutdown diagnostic\n" in result.stderr
    assert b"buffered diagnostic\n" in result.stderr
    assert b"native diagnostic\n" in result.stderr
    assert b"helper diagnostic\n" in result.stderr


@pytest.mark.parametrize("input_path", [
    "/dev/stdin",
    "/dev/fd",
    pytest.param("/proc/thread-self/fd", marks=pytest.mark.skipif(
        not os.path.isdir("/proc/thread-self/fd"), reason="requires thread-self procfs descriptors")),
])
def test_callable_compile_preserves_descriptor_backed_inputs(playbook, input_path):
    script = """
import os
import sys
from ansiroll import cli

path, manifest, vars_file, input_path = sys.argv[1:]
args = ["compile", path, "-o", "-", "-m", manifest]
if input_path == "/dev/stdin":
    raise SystemExit(cli.main(args + ["-e", "@" + input_path]))
with open(vars_file, "rb") as stream:
    assert not os.get_inheritable(stream.fileno())
    raise SystemExit(cli.main(args + ["-e", "@%s/%d" % (input_path, stream.fileno())]))
"""
    extra_vars = b'{"pipe_input": 42}'
    vars_file = playbook.with_name("vars.json")
    vars_file.write_bytes(extra_vars)
    manifest = playbook.with_name("manifest.json")
    result = subprocess.run(
        [sys.executable, "-c", script, str(playbook), str(manifest),
         str(vars_file), input_path],
        input=extra_vars, capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(manifest.read_text())["tree_shake"]["specialized_var_names"] == ["pipe_input"]
    compile(result.stdout, "<stdout>", "exec")


def test_callable_worker_uses_callers_import_path(monkeypatch, playbook, capfdbinary):
    import ansible

    shim = playbook.parent / "ansible"
    shim.mkdir()
    original_path = ansible.__file__
    (shim / "__init__.py").write_text(
        "__path__.append(%r)\n"
        "__file__ = %r\n"
        "with open(__file__, 'rb') as source:\n"
        "    exec(compile(source.read(), __file__, 'exec'))\n"
        "print('caller Ansible path')\n"
        % (os.path.dirname(original_path), original_path)
    )
    monkeypatch.syspath_prepend(str(playbook.parent))

    assert cli.main(["compile", str(playbook), "-o", "-"]) == 0

    captured = capfdbinary.readouterr()
    compile(captured.out, "<stdout>", "exec")
    assert b"caller Ansible path\n" in captured.err


def test_worker_startup_output_is_diagnostic(monkeypatch, playbook, capfdbinary):
    (playbook.parent / "sitecustomize.py").write_text(
        "import ctypes\n"
        "print('startup diagnostic', flush=True)\n"
        "ctypes.CDLL(None).puts(b'native startup diagnostic')\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(playbook.parent), prepend=os.pathsep)

    assert cli.main(["compile", str(playbook), "-o", "-"]) == 0

    captured = capfdbinary.readouterr()
    assert captured.out.startswith(b"#!")
    compile(captured.out, "<stdout>", "exec")
    assert b"startup diagnostic\n" in captured.err
    assert b"native startup diagnostic\n" in captured.err


def test_module_compile_routes_buffered_startup_output_to_stderr(playbook):
    (playbook.parent / "sitecustomize.py").write_text("print('buffered startup diagnostic')\n")
    env = dict(os.environ, PYTHONPATH=str(playbook.parent))
    env.pop("PYTHONUNBUFFERED", None)
    result = subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", str(playbook), "-o", "-"],
        capture_output=True, env=env,
    )

    assert result.returncode == 0, result.stderr
    compile(result.stdout, "<stdout>", "exec")
    assert b"buffered startup diagnostic\n" in result.stderr


@pytest.mark.parametrize("isolation_flag", ["-E", "-I"])
def test_callable_worker_keeps_interpreter_isolation(playbook, isolation_flag):
    (playbook.parent / "sitecustomize.py").write_text("print('unexpected startup output')\n")
    script = """
import json
import sys
sys.path[:] = json.loads(sys.argv[1])
from ansiroll import cli
raise SystemExit(cli.main(["compile", sys.argv[2], "-o", "-"]))
"""
    result = subprocess.run(
        [sys.executable, isolation_flag, "-c", script, json.dumps(sys.path), str(playbook)],
        capture_output=True, env=dict(os.environ, PYTHONPATH=str(playbook.parent)),
    )

    assert result.returncode == 0, result.stderr
    compile(result.stdout, "<stdout>", "exec")
    assert b"unexpected startup output" not in result.stderr


def test_callable_compile_writes_manifest_with_closed_fd_one(playbook):
    script = """
import io
import os
import sys
from ansiroll import cli

os.close(1)
sys.stdout = io.StringIO()
result = cli.main(["compile", sys.argv[1], "-o", "-", "-m", sys.argv[2]])
assert result == 0
compile(sys.stdout.getvalue(), "<stdout>", "exec")
"""
    manifest = playbook.with_name("manifest.json")
    manifest.touch()
    result = subprocess.run(
        [sys.executable, "-c", script, str(playbook), str(manifest)], capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(manifest.read_text())["playbook"] == str(playbook)


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
