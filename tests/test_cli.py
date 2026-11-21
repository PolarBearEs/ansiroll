import errno
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
        if not hasattr(os, "O_PATH"):
            pytest.skip("requires metadata-only file descriptors")
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


@pytest.mark.parametrize("manifest_name", ["manifest.json", "COMPILED.py"])
def test_file_compile_rejects_aliases_detected_only_when_opened(monkeypatch, tmp_path, manifest_name):
    output = tmp_path / "compiled.py"
    alias = tmp_path / manifest_name
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", {}))
    stat_path = os.stat
    open_fd = os.open

    def aliasing_stat(path, *args, **kwargs):
        if path == str(alias):
            path = str(output)
        return stat_path(path, *args, **kwargs)

    def aliasing_open(path, *args, **kwargs):
        return open_fd(str(output) if path == str(alias) else path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", aliasing_stat)
    monkeypatch.setattr(os, "open", aliasing_open)
    monkeypatch.setattr(os, "mkdir", lambda *args, **kwargs: pytest.fail("preflight created a probe directory"))
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(alias)]) == 2
    assert output.read_bytes() == b""
    assert list(tmp_path.iterdir()) == [output]


def test_file_compile_checks_identity_of_missing_destinations_parents(monkeypatch, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    real_stat = os.stat

    def aliasing_stat(path, *args, **kwargs):
        return real_stat(str(first) if path == str(second) else path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", aliasing_stat)
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: pytest.fail("aliases should fail before compilation"))
    output = first / "compiled.py"
    manifest = second / "compiled.py"
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)]) == 2
    assert not output.exists()
    assert not manifest.exists()


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


@pytest.mark.skipif(not hasattr(os, "O_PATH") or not os.path.isdir("/proc/self/fd"),
                    reason="requires reopening a metadata-only descriptor")
@pytest.mark.parametrize("mode", ["stdout", "file"])
@pytest.mark.parametrize("redirect", [None, "before validation", "after validation"])
def test_manifest_overwrite_can_truncate_only_the_validated_inode(monkeypatch, tmp_path, mode, redirect):
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    saved = tmp_path / "saved.json"
    manifest.write_text("previous manifest")
    source = "pass\n"
    data = {"modules": [], "fallback_tasks": []}
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: (source, data))
    open_fd = os.open
    truncated = []

    def replace_path():
        manifest.rename(saved)
        manifest.symlink_to(output)

    def require_truncate(path, flags, *args, **kwargs):
        if path == str(manifest) and flags & os.O_ACCMODE == os.O_WRONLY:
            assert not flags & os.O_TRUNC
            if redirect == "before validation":
                replace_path()
            raise PermissionError(errno.EPERM, "filesystem requires O_TRUNC at open time", path)
        if str(path).startswith("/proc/self/fd/") and flags & os.O_TRUNC:
            truncated.append(path)
            if redirect == "after validation":
                replace_path()
        return open_fd(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", require_truncate)
    if mode == "stdout":
        with output.open("w") as stream:
            stream.write(source)
            stream.flush()
            if redirect == "before validation":
                with pytest.raises(ValueError, match="manifest path points to stdout"):
                    cli._write_stdout_manifest(str(manifest), data, stream.fileno())
            else:
                cli._write_stdout_manifest(str(manifest), data, stream.fileno())
    else:
        result = cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)])
        assert result == (2 if redirect == "before validation" else 0)

    assert output.read_text() == source
    if redirect == "before validation":
        assert not truncated
        assert saved.read_text() == "previous manifest"
    else:
        assert len(truncated) == 1
        written = saved if redirect == "after validation" else manifest
        assert json.loads(written.read_text()) == data


@pytest.mark.skipif(not hasattr(os, "O_PATH") or not os.path.isdir("/proc/self/fd"),
                    reason="requires reopening a metadata-only descriptor")
@pytest.mark.parametrize("destination", ["output", "manifest"])
@pytest.mark.parametrize("failure, error", [
    ("open", errno.EPERM),
    ("truncate", errno.EINVAL),
    ("truncate", errno.ENOSYS),
])
def test_compile_reopens_validated_destination_with_truncation(monkeypatch, tmp_path, destination, failure, error):
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    files = {"output": output, "manifest": manifest}
    for path in files.values():
        path.write_text("old content")
    source = "pass\n"
    data = {"modules": [], "fallback_tasks": []}
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: (source, data))
    open_fd, fdopen = os.open, os.fdopen
    append_fd = None
    reopened = []

    def open_append_only(path, flags, *args, **kwargs):
        nonlocal append_fd
        target = path == str(files[destination]) and flags & os.O_ACCMODE == os.O_WRONLY
        if target and failure == "open":
            raise OSError(error, "filesystem requires O_TRUNC", path)
        fd = open_fd(path, flags, *args, **kwargs)
        if target:
            append_fd = fd
        if str(path).startswith("/proc/self/fd/") and flags & os.O_TRUNC:
            reopened.append(path)
        return fd

    class AppendOnly:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.file.close()

        def truncate(self, size):
            raise OSError(error, "handle does not support standalone truncation")

        def close(self):
            self.file.close()

    def wrap_append_handle(fd, mode):
        file = fdopen(fd, mode)
        return AppendOnly(file) if fd == append_fd and not reopened else file

    monkeypatch.setattr(os, "open", open_append_only)
    monkeypatch.setattr(os, "fdopen", wrap_append_handle)
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)]) == 0
    assert len(reopened) == 1
    assert output.read_text() == source
    assert json.loads(manifest.read_text()) == data


def test_compile_writes_new_files_without_truncation_or_procfs(monkeypatch, tmp_path):
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    data = {"modules": [], "fallback_tasks": []}
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", data))
    open_fd, fdopen = os.open, os.fdopen

    def without_procfs(path, flags, *args):
        if str(path).startswith("/proc/self/fd/"):
            raise FileNotFoundError(errno.ENOENT, "procfs unavailable", path)
        return open_fd(path, flags, *args)

    def sequential_writer(fd, mode):
        file = fdopen(fd, mode)

        def truncate(size):
            raise OSError(errno.EOPNOTSUPP, "standalone truncation unavailable")

        file.truncate = truncate
        return file

    monkeypatch.setattr(os, "open", without_procfs)
    monkeypatch.setattr(os, "fdopen", sequential_writer)
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)]) == 0
    assert output.read_text() == "pass\n"
    assert json.loads(manifest.read_text()) == data


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="requires metadata-only file descriptors")
@pytest.mark.parametrize("destination", ["output", "manifest"])
def test_compile_releases_optional_pin_when_write_open_hits_emfile(monkeypatch, tmp_path, destination):
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    target = output if destination == "output" else manifest
    target.write_text("longer prior contents")
    data = {"modules": [], "fallback_tasks": []}
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", data))
    open_fd = os.open
    reference = None
    attempts = 0

    def with_one_free_slot(path, flags, *args):
        nonlocal reference, attempts
        if path == str(target) and flags & os.O_PATH:
            reference = open_fd(path, flags, *args)
            return reference
        if path == str(target) and flags & os.O_ACCMODE == os.O_WRONLY:
            attempts += 1
            assert not flags & os.O_TRUNC
            assert reference is not None
            if attempts == 1:
                os.fstat(reference)
                raise OSError(errno.EMFILE, "too many open files", path)
            with pytest.raises(OSError) as error:
                os.fstat(reference)
            assert error.value.errno == errno.EBADF
        return open_fd(path, flags, *args)

    monkeypatch.setattr(os, "open", with_one_free_slot)
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)]) == 0
    assert attempts == 2
    assert output.read_text() == "pass\n"
    assert json.loads(manifest.read_text()) == data


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="requires metadata-only file descriptors")
def test_manifest_truncating_reopen_reports_unavailable_procfs(monkeypatch, tmp_path):
    output = tmp_path / "stdout"
    manifest = tmp_path / "manifest.json"
    manifest.write_text("previous manifest")
    open_fd, stat_path = os.open, os.stat

    def require_truncate(path, flags, *args, **kwargs):
        if path == str(manifest) and flags & os.O_ACCMODE == os.O_WRONLY:
            raise PermissionError(errno.EPERM, "filesystem requires O_TRUNC", path)
        return open_fd(path, flags, *args, **kwargs)

    def stat_without_procfs(path, *args, **kwargs):
        if str(path).startswith("/proc/self/fd/"):
            raise FileNotFoundError(errno.ENOENT, "procfs unavailable", path)
        return stat_path(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", require_truncate)
    monkeypatch.setattr(os, "stat", stat_without_procfs)
    with output.open("w") as stream:
        with pytest.raises(PermissionError, match="safe truncating reopen requires /proc/self/fd"):
            cli._write_stdout_manifest(str(manifest), {}, stream.fileno())
    assert manifest.read_text() == "previous manifest"


@pytest.mark.parametrize("destination", ["output", "manifest"])
def test_compile_leaves_permission_denials_unchanged(monkeypatch, tmp_path, destination):
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    output.write_text("previous Python")
    manifest.write_text("previous manifest")
    target = output if destination == "output" else manifest
    before = target.read_text()
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", {}))
    open_fd = os.open

    def deny_write(path, flags, *args, **kwargs):
        if path == str(target) and flags & os.O_ACCMODE == os.O_WRONLY:
            raise PermissionError(errno.EACCES, "write denied", path)
        return open_fd(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", deny_write)
    monkeypatch.setattr(cli, "_open_truncated_destination", lambda *args: pytest.fail("permission denial retried"))
    with pytest.raises(PermissionError, match="write denied"):
        cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)])
    assert target.read_text() == before


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="requires metadata-only file descriptors")
@pytest.mark.parametrize("destination", ["output", "manifest"])
def test_compile_rejects_truncating_retry_for_a_foreign_file(monkeypatch, tmp_path, destination):
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    output.write_text("previous Python")
    manifest.write_text("previous manifest")
    target = output if destination == "output" else manifest
    prior = target.read_text()
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", {}))
    open_fd, fstat = os.open, os.fstat
    pinned = None

    def open_replacement(path, flags, *args, **kwargs):
        nonlocal pinned
        if path == str(target) and flags & os.O_ACCMODE == os.O_WRONLY:
            raise PermissionError(errno.EPERM, "create-open permission not established", path)
        assert not flags & os.O_TRUNC
        fd = open_fd(path, flags, *args, **kwargs)
        if path == str(target) and flags & os.O_PATH:
            pinned = fd
        return fd

    def foreign_owner(fd):
        if fd == pinned:
            fields = list(fstat(fd))
            fields[stat.ST_UID] = os.geteuid() + 1
            return os.stat_result(fields)
        return fstat(fd)

    monkeypatch.setattr(os, "open", open_replacement)
    monkeypatch.setattr(os, "fstat", foreign_owner)
    with pytest.raises(PermissionError, match="create-open permission not established"):
        cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)])
    assert pinned is not None
    assert target.read_text() == prior


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="requires metadata-only file descriptors")
@pytest.mark.parametrize("destination", ["output", "manifest"])
@pytest.mark.parametrize("existed", [False, True])
def test_compile_does_not_retry_truncation_on_a_replacement(monkeypatch, tmp_path, destination, existed):
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    target = output if destination == "output" else manifest
    saved = tmp_path / "saved"
    if existed:
        target.write_text("original file")
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", {}))
    open_fd = os.open

    def replace_on_create_open(path, flags, *args, **kwargs):
        assert not flags & os.O_TRUNC
        if path == str(target) and flags & os.O_ACCMODE == os.O_WRONLY:
            if existed:
                target.rename(saved)
            target.write_text("replacement file")
            raise PermissionError(errno.EPERM, "create-open failed", path)
        return open_fd(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_on_create_open)
    with pytest.raises(PermissionError, match="create-open failed"):
        cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)])
    assert target.read_text() == "replacement file"
    if existed:
        assert saved.read_text() == "original file"


@pytest.mark.skipif(not hasattr(os, "mkfifo") or not hasattr(os, "O_PATH"), reason="requires Linux FIFOs")
@pytest.mark.parametrize("metadata_available", [True, False])
def test_output_inode_fallback_does_not_open_replacement_fifo(monkeypatch, tmp_path, metadata_available):
    output = tmp_path / "compiled.py"
    open_fd, metadata = os.open, os.O_PATH
    with output.open("w") as file:
        original = os.fstat(file.fileno())
        output.unlink()
        os.mkfifo(output)
        inspected = []

        def open_without_procfs(path, flags, *args, **kwargs):
            if str(path).startswith("/proc/self/fd/"):
                raise FileNotFoundError(errno.ENOENT, "procfs unavailable", path)
            assert path == str(output) and flags == metadata
            inspected.append(path)
            return open_fd(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", open_without_procfs)
        if not metadata_available:
            monkeypatch.delattr(os, "O_PATH")
        assert cli._pin_output_inode(str(output), file.fileno(), original) is None
        assert inspected == ([str(output)] if metadata_available else [])


def test_file_compile_defers_manifest_creation_until_python_output_succeeds(monkeypatch, tmp_path):
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", {}))

    def fail_chmod(*args, **kwargs):
        raise PermissionError("could not make output executable")

    monkeypatch.setattr(cli, "_make_output_executable", fail_chmod)
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
@pytest.mark.parametrize("swap", [False, True])
def test_file_compile_does_not_connect_manifest_fifo_when_python_fails(monkeypatch, tmp_path, swap):
    import select

    if not hasattr(select, "poll"):
        pytest.skip("requires poll")
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    fifo = tmp_path / "waiting-fifo" if swap else manifest
    os.mkfifo(fifo)
    if swap:
        if not hasattr(os, "O_PATH"):
            pytest.skip("requires metadata-only file descriptors")
        manifest.write_text("{}")
        open_fd = os.open

        def replace_with_fifo(path, flags, *args, **kwargs):
            if path == str(manifest) and flags & os.O_PATH and fifo.exists():
                os.replace(fifo, manifest)
            return open_fd(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", replace_with_fifo)
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", {}))

    def fail_chmod(*args, **kwargs):
        raise PermissionError("could not make output executable")

    monkeypatch.setattr(cli, "_make_output_executable", fail_chmod)
    with os.fdopen(os.open(fifo, os.O_RDONLY | os.O_NONBLOCK), "rb") as reader:
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
        if path == str(manifest) and (flags & os.O_ACCMODE) == os.O_WRONLY:
            manifest.unlink()
            manifest.symlink_to(output)
        return open_fd(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_fifo_on_blocking_open)
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)]) == 2
    assert output.read_text() == "pass\n"


def test_file_compile_writes_to_current_manifest_after_replacement(monkeypatch, tmp_path):
    output = tmp_path / "compiled.py"
    manifest_path = tmp_path / "manifest.json"
    prior_path = tmp_path / "previous.json"
    manifest_path.write_text('{"previous": true}')
    manifest = {"modules": [], "fallback_tasks": []}
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", manifest))
    make_executable = cli._make_output_executable

    def replace_manifest(*args):
        make_executable(*args)
        os.replace(manifest_path, prior_path)
        manifest_path.write_text('{}')

    monkeypatch.setattr(cli, "_make_output_executable", replace_manifest)
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest_path)]) == 0
    assert json.loads(prior_path.read_text()) == {"previous": True}
    assert json.loads(manifest_path.read_text()) == manifest


@pytest.mark.parametrize("during_open", [False, True])
def test_file_compile_detects_manifest_redirected_to_missing_output(monkeypatch, tmp_path, during_open):
    output = tmp_path / "compiled.py"
    saved = tmp_path / "saved.py"
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", {}))
    make_executable = cli._make_output_executable
    open_fd = os.open

    def remove_output(*args):
        make_executable(*args)
        output.rename(saved)
        if not during_open:
            manifest.symlink_to(output)

    def redirect_manifest(path, flags, *args, **kwargs):
        if during_open and path == str(manifest) and flags & os.O_WRONLY and not manifest.is_symlink():
            manifest.symlink_to(output)
        return open_fd(path, flags, *args, **kwargs)

    monkeypatch.setattr(cli, "_make_output_executable", remove_output)
    monkeypatch.setattr(os, "open", redirect_manifest)
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)]) == 2
    assert saved.read_text() == "pass\n"
    if during_open:
        assert output.read_bytes() == b""
    else:
        assert not output.exists()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="requires procfs descriptor links")
def test_file_compile_holds_output_inode_until_manifest_open(monkeypatch, tmp_path):
    output = tmp_path / "compiled.py"
    manifest_path = tmp_path / "manifest.json"
    manifest = {"modules": [], "fallback_tasks": []}
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", manifest))
    make_executable = cli._make_output_executable
    open_fd, pin = os.open, cli._pin_output_inode
    output_references = {}

    def remember_reference(path, fd, info):
        reference = pin(path, fd, info)
        assert reference is not None
        output_references[reference] = os.fstat(fd)
        return reference

    def remove_output(*args):
        make_executable(*args)
        output.unlink()

    def check_output_reference(path, flags, *args, **kwargs):
        if path == str(manifest_path) and flags & os.O_WRONLY:
            assert not output.exists()
            assert len(output_references) == 1
            duplicate, expected = next(iter(output_references.items()))
            assert os.path.samestat(os.fstat(duplicate), expected)
        return open_fd(path, flags, *args, **kwargs)

    monkeypatch.setattr(cli, "_pin_output_inode", remember_reference)
    monkeypatch.setattr(cli, "_make_output_executable", remove_output)
    monkeypatch.setattr(os, "open", check_output_reference)
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest_path)]) == 0
    assert json.loads(manifest_path.read_text()) == manifest
    with pytest.raises(OSError):
        os.fstat(next(iter(output_references)))


def test_file_compile_writes_manifest_after_output_parent_becomes_a_file(monkeypatch, tmp_path):
    parent = tmp_path / "output"
    parent.mkdir()
    moved = tmp_path / "moved"
    output = parent / "compiled.py"
    manifest = tmp_path / "manifest.json"
    data = {"modules": [], "fallback_tasks": []}
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", data))
    make_executable = cli._make_output_executable

    def move_parent(*args):
        make_executable(*args)
        parent.rename(moved)
        parent.touch()

    monkeypatch.setattr(cli, "_make_output_executable", move_parent)
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)]) == 0
    assert json.loads(manifest.read_text()) == data
    assert (moved / output.name).read_text() == "pass\n"


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="requires metadata-only file descriptors")
@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError])
def test_file_compile_falls_back_when_procfs_reference_is_unavailable(monkeypatch, tmp_path, failure):
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    data = {"modules": [], "fallback_tasks": []}
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", data))
    chmod, open_fd = os.chmod, os.open

    def pin_output(path, fd, info):
        return open_fd(path, os.O_PATH)

    def unavailable_link(path, mode):
        if os.fspath(path).startswith("/proc/self/fd/"):
            raise failure("descriptor link unavailable")
        chmod(path, mode)

    monkeypatch.setattr(cli, "_pin_output_inode", pin_output)
    monkeypatch.setattr(os, "chmod", unavailable_link)
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)]) == 0
    assert output.stat().st_mode & stat.S_IEXEC
    assert json.loads(manifest.read_text()) == data


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX executable files")
def test_file_compile_releases_output_writer_before_setting_execute_bits(monkeypatch, tmp_path):
    if os.statvfs(tmp_path).f_flag & getattr(os, "ST_NOEXEC", 0):
        pytest.skip("temporary directory is on a noexec mount")
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    seen = []
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: (
        "#!/bin/sh\nexit 0\n", {"modules": [], "fallback_tasks": []},
    ))
    make_executable = cli._make_output_executable

    def run_output_as_soon_as_executable(*args):
        make_executable(*args)
        subprocess.run([str(output)], capture_output=True, check=True, timeout=5)
        seen.append(output)

    monkeypatch.setattr(cli, "_make_output_executable", run_output_as_soon_as_executable)
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)]) == 0
    assert seen == [output]


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
