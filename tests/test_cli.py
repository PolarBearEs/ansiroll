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


def test_stdout_compile_requires_redirected_output():
    pty = pytest.importorskip("pty")
    master, slave = pty.openpty()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ansiroll.cli", "compile", "play.yml", "-o", "-"],
            stdout=slave, stderr=subprocess.PIPE,
        )
        os.set_blocking(master, False)
        with pytest.raises(BlockingIOError):
            os.read(master, 4096)
    finally:
        os.close(slave)
        os.close(master)

    assert result.returncode == 2
    assert b"redirect stdout to a pipe or file" in result.stderr


def test_stdout_compile_can_discard_all_output():
    script = """
import os
import sys
from ansiroll import cli

cli.compile_playbook = lambda *a, **k: ("pass", {"modules": [], "fallback_tasks": []})
sys.argv[1:] = ["compile", "play.yml", "-o", "-", "-m", os.devnull]
raise SystemExit(cli.entrypoint())
"""
    subprocess.run(
        [sys.executable, "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, check=True,
    )


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


@pytest.mark.parametrize("with_manifest", [False, True])
def test_compile_writes_executable_file(monkeypatch, tmp_path, capsys, with_manifest):
    manifest = {"modules": [], "fallback_tasks": []}
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", manifest))
    output = tmp_path / "compiled.py"
    output.write_text("# previous output with a longer line\n")
    output.chmod(0o640)
    args = ["compile", "play.yml", "-o", str(output)]
    if with_manifest:
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(" " * 100)
        args.extend(["-m", str(manifest_path)])

    assert cli.main(args) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ansiroll: compiled play.yml -> %s" % output in captured.err
    assert output.read_text() == "pass\n"
    assert stat.S_IMODE(output.stat().st_mode) == 0o750
    if with_manifest:
        assert manifest_path.read_text() == json.dumps(manifest, indent=2)


@pytest.mark.parametrize("alias", ["missing", "dangling", "same", "symlink", "hardlink", "replaced"])
def test_file_compile_rejects_manifest_alias(monkeypatch, tmp_path, capsys, alias):
    output = tmp_path / "compiled.py"
    original = "# existing output\n"
    if alias not in ("missing", "dangling"):
        output.write_text(original)
    manifest = tmp_path / "manifest.json"
    if alias in ("missing", "same"):
        manifest = output
    elif alias in ("dangling", "symlink"):
        manifest.symlink_to(output)
    elif alias == "hardlink":
        os.link(output, manifest)
    else:
        manifest.write_text("{}")
        open_fd = os.open

        def replace_manifest(path, flags, *args, **kwargs):
            if path == str(manifest):
                manifest.unlink()
                manifest.symlink_to(output)
            return open_fd(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", replace_manifest)

    def compile_playbook(*args, **kwargs):
        assert alias == "replaced", "expected alias to be rejected before compiling"
        return "pass\n", {"modules": [], "fallback_tasks": []}

    monkeypatch.setattr(cli, "compile_playbook", compile_playbook)
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)]) == 2
    assert "manifest path points to compiled output" in capsys.readouterr().err
    if alias in ("missing", "dangling"):
        assert not output.exists()
    else:
        assert output.read_text() == original


def test_file_compile_rejects_aliases_detected_only_when_opened(monkeypatch, tmp_path):
    output = tmp_path / "compiled.py"
    alias = tmp_path / "manifest.json"
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", {}))
    stat_path = os.stat

    def aliasing_stat(path, *args, **kwargs):
        if path == str(alias):
            path = str(output)
        return stat_path(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", aliasing_stat)
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(alias)]) == 2
    assert output.read_bytes() == b""
    assert not alias.exists()


def test_file_compile_preserves_symlink_path_lookup(monkeypatch, tmp_path):
    target = tmp_path / "target.py"
    target.write_text("# existing contents\n")
    output = tmp_path / "compiled.py"
    output.symlink_to("missing/../target.py")
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", {}))

    with pytest.raises(FileNotFoundError):
        cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)])
    assert target.read_text() == "# existing contents\n"
    assert not manifest.exists()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="requires Linux procfs descriptors")
def test_file_compile_distinguishes_same_named_unlinked_files(monkeypatch, tmp_path):
    manifest = {"modules": [], "fallback_tasks": []}
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", manifest))
    backing_path = tmp_path / "unlinked"
    with backing_path.open("w+b") as output:
        backing_path.unlink()
        with backing_path.open("w+b") as manifest_file:
            backing_path.unlink()
            output_path = "/proc/self/fd/%s" % output.fileno()
            manifest_path = "/proc/self/fd/%s" % manifest_file.fileno()
            assert cli.main(["compile", "play.yml", "-o", output_path, "-m", manifest_path]) == 0
            assert output.read() == b"pass\n"
            assert json.load(manifest_file) == manifest


def test_file_compile_defers_manifest_creation_until_python_output_succeeds(monkeypatch, tmp_path):
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", {}))

    def fail_chmod(*args, **kwargs):
        raise PermissionError("could not make output executable")

    monkeypatch.setattr(os, "fchmod" if hasattr(os, "fchmod") else "chmod", fail_chmod)
    with pytest.raises(PermissionError, match="could not make output executable"):
        cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)])
    assert not manifest.exists()


@pytest.mark.parametrize("manifest_path, error", [
    (".", IsADirectoryError),
    ("missing/../compiled.py", FileNotFoundError),
])
def test_file_compile_writes_python_before_reporting_manifest_open_error(monkeypatch, tmp_path, manifest_path, error):
    output = tmp_path / "compiled.py"
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", {}))

    with pytest.raises(error):
        cli.main(["compile", "play.yml", "-o", str(output), "-m", str(tmp_path / manifest_path)])
    assert output.read_text() == "pass\n"
    assert output.stat().st_mode & stat.S_IEXEC


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires named pipes")
def test_file_compile_does_not_connect_manifest_fifo_when_python_fails(monkeypatch, tmp_path):
    import select

    if not hasattr(select, "poll"):
        pytest.skip("requires poll")
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    os.mkfifo(manifest)
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", {}))

    def fail_chmod(*args, **kwargs):
        raise PermissionError("could not make output executable")

    monkeypatch.setattr(os, "fchmod", fail_chmod)
    with os.fdopen(os.open(manifest, os.O_RDONLY | os.O_NONBLOCK), "rb") as reader:
        poller = select.poll()
        poller.register(reader, select.POLLHUP)
        with pytest.raises(PermissionError, match="could not make output executable"):
            cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)])
        assert poller.poll(0) == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires named pipes")
def test_file_compile_never_writes_manifest_through_replaced_deferred_fifo(monkeypatch, tmp_path):
    output = tmp_path / "compiled.py"
    output.write_text("# existing output\n")
    manifest = tmp_path / "manifest.json"
    os.mkfifo(manifest)
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", {}))
    open_fd = os.open

    def replace_fifo_on_blocking_open(path, flags, *args, **kwargs):
        if path == str(manifest) and not (flags & os.O_NONBLOCK):
            manifest.unlink()
            manifest.symlink_to(output)
        return open_fd(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_fifo_on_blocking_open)
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)]) == 2
    assert output.read_text() == "pass\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires named pipes")
def test_file_compile_streams_to_sequential_fifos(tmp_path):
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    os.mkfifo(output)
    os.mkfifo(manifest)
    args = [str(output), str(manifest)]
    read_script = """
import sys
from pathlib import Path

for path in sys.argv[1:]:
    sys.stdout.write(Path(path).read_text())
"""
    compile_script = """
import sys
from ansiroll import cli

cli.compile_playbook = lambda *a, **k: ('pass\\n', {'modules': [], 'fallback_tasks': []})
raise SystemExit(cli.main(['compile', 'play.yml', '-o', sys.argv[1], '-m', sys.argv[2]]))
"""
    reader = subprocess.Popen(
        [sys.executable, "-c", read_script, *args],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        subprocess.run(
            [sys.executable, "-c", compile_script, *args],
            capture_output=True, check=True, timeout=10,
        )
        received, _ = reader.communicate(timeout=10)
    finally:
        if reader.poll() is None:
            reader.kill()
            reader.communicate()
    assert reader.returncode == 0
    assert received == "pass\n" + json.dumps({"modules": [], "fallback_tasks": []}, indent=2)


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
