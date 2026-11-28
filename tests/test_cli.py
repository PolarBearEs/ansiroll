import errno
import json
import os
import stat
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from ansiroll import cli


@pytest.fixture
def cli_os(monkeypatch):
    # Terminal tests must leave the OS functions used by pytest capture intact.
    isolated = Namespace(**vars(os))
    monkeypatch.setattr(cli, "os", isolated)
    return isolated


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
import os
import subprocess
import sys
from ansiroll import cli

def compile_playbook(*args, **kwargs):
    print("compiler diagnostic")
    ctypes.CDLL(None).puts(b"native diagnostic")
    subprocess.run([sys.executable, "-c", "print('helper diagnostic')"], check=True)
    os.write(2, b"first descriptor diagnostic\\n")
    os.write(1, b"second descriptor diagnostic\\n")
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
    assert b"first descriptor diagnostic\nsecond descriptor diagnostic\n" in result.stderr


def test_stdout_compile_rejects_manifest_alias():
    result = subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", "play.yml",
         "-o", "-", "-m", "/dev/stdout"],
        capture_output=True,
    )

    assert result.returncode == 2
    assert result.stdout == b""
    assert b"manifest path points to stdout" in result.stderr


def test_stdout_compile_rejects_manifest_aliasing_stderr(tmp_path):
    manifest = tmp_path / "manifest.json"
    with manifest.open("wb") as diagnostics:
        result = subprocess.run(
            [sys.executable, "-m", "ansiroll.cli", "compile", "missing.yml", "-o", "-", "-m", str(manifest)],
            stdout=subprocess.PIPE, stderr=diagnostics,
        )

    assert result.returncode == 2
    assert result.stdout == b""
    assert manifest.read_text() == "ansiroll: manifest path points to stderr: %s\n" % manifest


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux controlling terminal device identities")
@pytest.mark.parametrize("same_terminal", [False, True])
def test_stdout_manifest_compares_underlying_terminals(same_terminal):
    pty = pytest.importorskip("pty")
    driver = """
import fcntl
import os
import subprocess
import sys
import termios

same_terminal, first, second = map(int, sys.argv[1:])
fcntl.ioctl(first, termios.TIOCSCTTY, 0)
cli = '''
import errno
import os
from ansiroll import cli

open_fd = os.open
def only_writable_tty(path, flags, *args, **kwargs):
    if path == '/dev/tty' and flags & os.O_ACCMODE == os.O_RDONLY and not flags & getattr(os, 'O_PATH', 0):
        raise PermissionError(errno.EACCES, 'read access declined', path)
    return open_fd(path, flags, *args, **kwargs)
os.open = only_writable_tty
cli.compile_playbook = lambda *a, **k: ('pass', {'modules': [], 'fallback_tasks': []})
raise SystemExit(cli.entrypoint())
'''
with open('/dev/tty', 'wb') as inherited_terminal:
    result = subprocess.run(
        [sys.executable, '-c', cli, 'compile', 'pb.yml', '-o', '-', '-m', '/dev/tty'],
        stderr=first if same_terminal else inherited_terminal,
        start_new_session=not same_terminal, pass_fds=(second,),
        preexec_fn=None if same_terminal else lambda: fcntl.ioctl(second, termios.TIOCSCTTY, 0),
        timeout=10,
    )
raise SystemExit(result.returncode)
"""
    first_read, first_write = pty.openpty()
    second_read, second_write = pty.openpty()
    try:
        args = [str(int(same_terminal)), str(first_write), str(second_write)]
        result = subprocess.run([sys.executable, "-c", driver, *args], stdout=subprocess.PIPE,
                                start_new_session=True, pass_fds=(first_write, second_write), timeout=15)
        os.set_blocking(first_read, False)
        diagnostics = os.read(first_read, 4096)
        if same_terminal:
            assert result.returncode == 2 and result.stdout == b""
            assert diagnostics == b"ansiroll: manifest path points to stderr: /dev/tty\r\n"
        else:
            assert result.returncode == 0 and result.stdout == b"pass"
            assert b"compiled pb.yml" in diagnostics
            os.set_blocking(second_read, False)
            assert json.loads(os.read(second_read, 4096)) == {"modules": [], "fallback_tasks": []}
    finally:
        for fd in (first_read, first_write, second_read, second_write):
            os.close(fd)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux terminal ioctl encodings")
@pytest.mark.parametrize(("machine", "expected"), [("x86_64", 0x80045432), ("ppc64le", 0x40045432)])
def test_linux_terminal_uses_native_ioctl_encoding(monkeypatch, cli_os, machine, expected):
    import fcntl
    import struct

    def ioctl(fd, request, data):
        assert fd == 3 and request == expected and data == bytes(4)
        return struct.pack("I", 1056)

    cli_os.uname = lambda: Namespace(machine=machine)
    monkeypatch.setattr(fcntl, "ioctl", ioctl)
    assert cli._linux_terminal_device(3) == 1056


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux devpts device identities")
@pytest.mark.parametrize("alias_fd", [2, 6])
def test_manifest_terminal_checks_session_when_device_numbers_match(monkeypatch, cli_os, alias_fd):
    device = os.makedev(136, 5)
    alias_device = os.makedev(5, 0)
    destination = Namespace(st_rdev=alias_device if alias_fd == 6 else device, st_mode=stat.S_IFCHR)
    stream = Namespace(st_rdev=alias_device if alias_fd == 2 else device, st_mode=stat.S_IFCHR)
    cli_os.fstat = lambda fd: stream
    monkeypatch.setattr(cli, "_linux_terminal_device", lambda fd: device)
    cli_os.isatty = lambda fd: False
    cli_os.tcgetpgrp = lambda fd: fd + 123  # A foreground job switch does not change identity.
    assert cli._same_manifest_stream("manifest", destination, 2, destination_fd=6)

    def outside_session(fd):
        if fd == 2:
            raise OSError(errno.ENOTTY, "terminal is outside this session")
        return 123

    cli_os.tcgetpgrp = outside_session
    assert not cli._same_manifest_stream("manifest", destination, 2, destination_fd=6)

    def denied_foreground_group(fd):
        if fd == 2:
            raise PermissionError(errno.EPERM, "terminal foreground group denied")
        return 123

    cli_os.tcgetpgrp = denied_foreground_group
    with pytest.raises(ValueError, match="cannot verify terminal for manifest path"):
        cli._same_manifest_stream("manifest", destination, 2, destination_fd=6)

    def no_controlling_session(fd):
        raise OSError(errno.ENOTTY, "compiler has no controlling session")

    cli_os.tcgetpgrp = no_controlling_session
    with pytest.raises(ValueError, match="cannot distinguish terminal for manifest path"):
        cli._same_manifest_stream("manifest", destination, 2, destination_fd=6)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux devpts device identities")
def test_manifest_compares_direct_pty_slaves_by_filesystem_identity(cli_os):
    device = os.makedev(136, 5)
    destination = Namespace(st_rdev=device, st_mode=stat.S_IFCHR, st_ino=5, st_dev=17)
    stream = Namespace(st_rdev=device, st_mode=stat.S_IFCHR, st_ino=5, st_dev=18)
    cli_os.fstat = lambda fd: stream
    assert not cli._same_manifest_stream("manifest", destination, 2)
    stream.st_dev = destination.st_dev
    assert cli._same_manifest_stream("manifest", destination, 2)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux terminal aliases")
def test_manifest_terminal_preflight_pins_requested_path(monkeypatch, cli_os):
    alias = Namespace(st_rdev=os.makedev(5, 0), st_mode=stat.S_IFCHR, st_ino=5, st_dev=17)
    slave = Namespace(st_rdev=os.makedev(136, 5), st_mode=stat.S_IFCHR, st_ino=5, st_dev=18)
    handles = {2: slave, 6: alias, 8: alias}
    calls = []

    def open_terminal(path, flags):
        calls.append(path)
        if flags == os.O_PATH:
            assert path == "/proc/self/fd/4"
            return 8
        assert path == "/proc/self/fd/8" and flags & os.O_ACCMODE == os.O_WRONLY
        return 6

    def device(fd):
        calls.append(fd)
        return slave.st_rdev

    cli_os.open = open_terminal
    cli_os.close = lambda fd: None
    cli_os.fstat = handles.__getitem__
    cli_os.tcgetpgrp = lambda fd: 123
    monkeypatch.setattr(cli, "_linux_terminal_device", device)
    assert cli._same_manifest_stream("/proc/self/fd/4", alias, 2)
    assert calls == ["/proc/self/fd/4", "/proc/self/fd/8", 6]

    # A swap after stat cannot supply the metadata of an unrelated terminal.
    handles[8] = slave
    calls.clear()
    assert not cli._same_manifest_stream("/proc/self/fd/4", alias, 2)
    assert calls == ["/proc/self/fd/4"]

    def denied_reopen(path, flags):
        if flags == os.O_PATH:
            return 8
        raise PermissionError(errno.EACCES, "write probe denied", path)

    handles[8] = alias
    cli_os.open = denied_reopen
    with pytest.raises(ValueError, match="cannot verify terminal for manifest path"):
        cli._same_manifest_stream("/proc/self/fd/4", alias, 2)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux terminal aliases")
def test_manifest_terminal_does_not_probe_unrelated_character_device(monkeypatch, cli_os):
    stream = Namespace(st_rdev=os.makedev(5, 0), st_mode=stat.S_IFCHR)
    destination = Namespace(st_rdev=os.makedev(10, 130), st_mode=stat.S_IFCHR)
    cli_os.fstat = lambda fd: stream
    monkeypatch.setattr(cli, "_linux_terminal_device", lambda fd: os.makedev(136, 1))

    def unexpected_open(*args):
        pytest.fail("unrelated character device was opened during preflight")

    cli_os.open = unexpected_open
    assert not cli._same_manifest_stream("device", destination, 2)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux virtual consoles")
@pytest.mark.parametrize(("first", "second"), [
    ((4, 0), (5, 0)), ((5, 0), (4, 0)), ((5, 1), (136, 5)), ((136, 5), (5, 1)),
])
def test_manifest_rejects_ambiguous_console_aliases(cli_os, first, second):
    destination = Namespace(st_rdev=os.makedev(*first), st_mode=stat.S_IFCHR)
    stream = Namespace(st_rdev=os.makedev(*second), st_mode=stat.S_IFCHR)
    cli_os.fstat = lambda fd: stream
    with pytest.raises(ValueError, match="cannot verify redirected console"):
        cli._same_manifest_stream("virtual console", destination, 2, destination_fd=6)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux terminal aliases")
@pytest.mark.parametrize("alias_fd", [2, 6])
def test_manifest_rejects_unidentified_potential_terminal_alias(monkeypatch, cli_os, alias_fd):
    alias = Namespace(st_rdev=os.makedev(5, 0), st_mode=stat.S_IFCHR)
    slave = Namespace(st_rdev=os.makedev(136, 5), st_mode=stat.S_IFCHR)
    cli_os.fstat = lambda fd: alias if alias_fd == 2 else slave
    monkeypatch.setattr(cli, "_linux_terminal_device", lambda fd: None)
    with pytest.raises(ValueError, match="cannot verify terminal for manifest path"):
        cli._same_manifest_stream("manifest", alias if alias_fd == 6 else slave, 2, destination_fd=6)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux virtual consoles")
def test_manifest_distinguishes_console_and_zero(cli_os):
    console = Namespace(st_rdev=os.makedev(5, 1), st_mode=stat.S_IFCHR)
    cli_os.fstat = lambda fd: console
    assert not cli._same_manifest_stream("/dev/zero", os.stat("/dev/zero"), 2)


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


@pytest.mark.parametrize("alias", ["same", "hardlink", "expanded"])
def test_merged_compile_diagnostics_leave_the_input_playbook_intact(tmp_path, alias):
    if alias == "expanded":
        pytest.importorskip("ansible")
    playbook = tmp_path / "play.yml"
    original = b"- hosts: localhost\n  gather_facts: false\n  tasks: []\n"
    playbook.write_bytes(original)
    destination = playbook
    if alias == "hardlink":
        destination = tmp_path / "diagnostics"
        os.link(playbook, destination)
    source = tmp_path / "$ANSIROLL_INPUT" if alias == "expanded" else playbook
    with destination.open("ab") as stream:
        result = subprocess.run([sys.executable, "-m", "ansiroll.cli", "compile", str(source), "-o", "-"],
                                stdout=stream, stderr=subprocess.STDOUT, timeout=10,
                                env=dict(os.environ, ANSIROLL_INPUT=playbook.name))
    assert result.returncode == 2
    assert playbook.read_bytes() == original


@pytest.mark.parametrize("output_mode", ["file", "stdout"])
def test_compile_rejects_stderr_alias_with_separate_stdout(tmp_path, output_mode):
    playbook = tmp_path / "play.yml"
    original = b"- hosts: localhost\n  gather_facts: false\n  tasks: []\n"
    playbook.write_bytes(original)
    output = tmp_path / "compiled.py"
    with playbook.open("ab") as diagnostics:
        result = subprocess.run(
            [sys.executable, "-m", "ansiroll.cli", "compile", str(playbook),
             "-o", "-" if output_mode == "stdout" else str(output)],
            stdout=subprocess.PIPE, stderr=diagnostics, timeout=10,
        )
    assert result.returncode == 2
    assert result.stdout == b""
    assert not output.exists()
    assert playbook.read_bytes() == original


@pytest.mark.skipif(os.name != "posix", reason="requires descriptor access modes")
@pytest.mark.parametrize("read_only", [True, False])
def test_file_compile_checks_inherited_and_python_stderr_separately(tmp_path, read_only):
    playbook = tmp_path / "play.yml"
    original = "- hosts: localhost\n  gather_facts: false\n  tasks: []\n"
    playbook.write_text(original)
    output = tmp_path / "compiled.py"
    script = """
import os
import sys
from ansiroll import cli

playbook, output, read_only = sys.argv[1:]
if read_only == 'True':
    sys.stderr = open(os.devnull, 'w')
    with open(playbook) as inherited:
        os.dup2(inherited.fileno(), 2)
else:
    sys.stderr = open(playbook, 'a')
cli.compile_playbook = lambda *args, **kwargs: ('pass\\n', {'modules': [], 'fallback_tasks': []})
raise SystemExit(cli.main(['compile', playbook, '-o', output]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(playbook), str(output), str(read_only)],
        capture_output=True, timeout=10,
    )
    assert result.returncode == (0 if read_only else 2), result.stderr
    assert output.exists() == read_only
    assert playbook.read_text() == original


@pytest.mark.parametrize("before_operand", [False, True])
@pytest.mark.parametrize("protected", [False, True])
def test_compile_argument_error_checks_diagnostic_destination(monkeypatch, capsys, before_operand, protected):
    monkeypatch.setattr(cli, "_stderr_may_be_playbook", lambda path: protected and path == "play.yml")
    args = ["-o", "--tree-shake", "play.yml"] if before_operand else ["play.yml"]

    with pytest.raises(SystemExit) as error:
        cli.main(["compile", *args])

    assert error.value.code == 2
    diagnostics = capsys.readouterr().err
    if protected:
        assert not diagnostics
    else:
        assert "usage: ansiroll compile" in diagnostics


def test_file_compile_checks_stdout_before_compilation(monkeypatch, capsys):
    def alias(path, fd, stream):
        assert path == "play.yml"
        return fd == 1

    monkeypatch.setattr(cli, "_diagnostics_may_be_playbook", alias)
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **kw: pytest.fail("started compilation"))
    assert cli.main(["compile", "play.yml", "-o", "compiled.py"]) == 2
    assert "stdout points to input playbook" in capsys.readouterr().err


@pytest.mark.parametrize("match", ["none", "destination", "buffer", "interrupt", "exit"])
@pytest.mark.parametrize("stream", [1, 2])
def test_file_compile_defers_diagnostics_until_input_identities_are_known(tmp_path, match, stream):
    log = tmp_path / "diagnostics"
    output = tmp_path / "compiled.py"
    driver = """
import os
import sys
from ansiroll import cli

stream, match, log, output = sys.argv[1:]
def compile_playbook(path, *, on_playbook_open, **kwargs):
    print("early Python diagnostic", file=sys.stdout if stream == "1" else sys.stderr, flush=True)
    os.write(int(stream), b"early descriptor diagnostic\\n")
    if match in ("destination", "interrupt", "exit"):
        on_playbook_open("observed-input.yml", os.stat(log))
    elif match == "buffer":
        on_playbook_open("observed-input.yml", os.fstat(int(stream)))
    if match == "interrupt":
        raise KeyboardInterrupt
    if match == "exit":
        raise SystemExit("extension stopped compilation")
    return "pass\\n", {"modules": [], "fallback_tasks": []}

cli.compile_playbook = compile_playbook
raise SystemExit(cli.main(["compile", "entry.yml", "-o", output]))
"""
    with log.open("wb") as destination:
        result = subprocess.run(
            [sys.executable, "-c", driver, str(stream), match, str(log), str(output)],
            stdout=destination if stream == 1 else subprocess.PIPE,
            stderr=destination if stream == 2 else subprocess.PIPE,
            timeout=30,
        )

    if match in ("buffer", "interrupt", "exit"):
        if match == "buffer":
            assert result.returncode == 2
        elif match == "exit":
            assert result.returncode == 1
        elif stream == 2:
            assert result.returncode == 130
        else:
            assert result.returncode != 0
        assert not output.exists()
    else:
        assert result.returncode == 0
        assert output.read_text() == "pass\n"
    if match != "none":
        assert log.read_bytes() == b""
    else:
        diagnostics = log.read_text()
        assert diagnostics.startswith("early Python diagnostic\nearly descriptor diagnostic\n")
        if stream == 2:
            assert "compiled entry.yml" in diagnostics
    if stream == 1 and match in ("none", "destination"):
        assert b"compiled entry.yml" in result.stderr


def test_compile_diagnostic_capture_preserves_independent_file_offsets(tmp_path):
    log = tmp_path / "diagnostics"
    log.write_bytes(b"........")
    driver = """
import os
from ansiroll import cli

with cli._hold_compile_diagnostics(lambda info: False):
    os.write(1, b"stdout")
    os.write(2, b"stderr")
"""
    with log.open("r+b") as stdout, log.open("ab") as stderr:
        stdout.seek(2)
        subprocess.run([sys.executable, "-c", driver], stdout=stdout, stderr=stderr,
                       check=True, timeout=10)
    assert log.read_bytes() == b"..stdoutstderr"


def test_compile_diagnostic_capture_keeps_saved_stream_off_closed_stdout():
    driver = """
import errno
import os
from ansiroll import cli

os.close(1)
with cli._hold_compile_diagnostics(lambda info: False):
    os.write(1, b"discarded stdout")
    os.write(2, b"kept stderr")
try:
    os.fstat(1)
except OSError as error:
    assert error.errno == errno.EBADF
else:
    raise AssertionError("stdout was left open")
"""
    result = subprocess.run([sys.executable, "-c", driver], capture_output=True, check=True, timeout=10)
    assert result.stdout == b""
    assert result.stderr == b"kept stderr"


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX pipes")
def test_compile_does_not_read_a_fifo_while_holding_its_diagnostic_writer(tmp_path):
    output = tmp_path / "compiled.py"
    driver = """
import os
import sys
from ansiroll import cli

original_stderr = os.fstat(2)
def compile_playbook(path, *, on_playbook_open, **kwargs):
    sys.stderr.write("early diagnostic\\n")
    on_playbook_open("observed-input.yml", original_stderr)
    raise AssertionError("would have started reading from the pipe")

cli.compile_playbook = compile_playbook
raise SystemExit(cli.main(["compile", "entry.yml", "-o", sys.argv[1]]))
"""
    result = subprocess.run(
        [sys.executable, "-c", driver, str(output)], capture_output=True, timeout=10,
    )
    assert result.returncode == 2
    assert result.stderr == b""
    assert not output.exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires named pipes")
def test_merged_compile_diagnostics_leave_the_input_fifo_empty(tmp_path):
    playbook = tmp_path / "play.pipe"
    os.mkfifo(playbook)
    with os.fdopen(os.open(playbook, os.O_RDONLY | os.O_NONBLOCK), "rb") as reader:
        with playbook.open("wb") as stream:
            result = subprocess.run(
                [sys.executable, "-m", "ansiroll.cli", "compile", str(playbook), "-o", "-"],
                stdout=stream, stderr=subprocess.STDOUT, timeout=10,
            )
        assert reader.read() == b""
    assert result.returncode == 2


@pytest.mark.parametrize("output_mode", ["file", "stdout"])
def test_compile_reports_input_with_a_nondirectory_component(tmp_path, output_mode):
    pytest.importorskip("ansible")
    parent = tmp_path / "regular-file"
    parent.touch()
    playbook = parent / "play.yml"
    output = tmp_path / "compiled.py"
    result = subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", str(playbook),
         "-o", "-" if output_mode == "stdout" else str(output)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
    assert "play.yml" in result.stderr
    assert result.stdout == ""
    assert not output.exists()


@pytest.mark.parametrize("component", ["filename", "directory"])
def test_merged_compile_diagnostics_use_legacy_filename_encoding(monkeypatch, cli_os, tmp_path, component):
    pytest.importorskip("ansible")
    from ansiroll import loader

    folder = tmp_path / "café" if component == "directory" else tmp_path
    folder.mkdir(exist_ok=True)
    path = folder / ("play-é.yml" if component == "filename" else "play.yml")
    path.write_text("input playbook")
    monkeypatch.chdir(folder)
    monkeypatch.setattr(loader, "_core_version", lambda: (2, 10))
    stream = path.stat()

    def stat_legacy_path(candidate):
        if candidate == str(path).encode("utf-8"):
            return stream
        raise FileNotFoundError(candidate)

    monkeypatch.setattr(cli_os, "stat", stat_legacy_path)
    assert cli._diagnostic_stream_may_be_playbook(stream, path.name)


def test_merged_compile_diagnostics_fail_closed_if_sink_cannot_open(monkeypatch, tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("input playbook")

    def exhausted_sink(*args, **kwargs):
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(cli, "open", exhausted_sink, raising=False)
    assert cli._diagnostic_stream_may_be_playbook(playbook.stat(), "$ANSIROLL_INPUT")


@pytest.mark.parametrize(("error", "emits_diagnostic"), [(errno.ENOENT, True), (errno.EACCES, False), (errno.EIO, False)])
def test_merged_compile_diagnostics_handle_failed_input_stat(monkeypatch, cli_os, tmp_path, capsys,
                                                       error, emits_diagnostic):
    playbook = tmp_path / "play.yml"
    playbook.write_text("input playbook")
    stream = playbook.stat()
    cli_os.fstat = lambda fd: stream
    cli_os.isatty = lambda fd: False
    monkeypatch.setattr(cli, "_prepare_compile_stderr", lambda: None, raising=False)

    def unreadable_input(path):
        assert path == str(playbook)
        raise OSError(error, os.strerror(error), path)

    cli_os.stat = unreadable_input
    assert cli.main(["compile", str(playbook), "-o", "-"], _standalone=True) == 2
    diagnostic = capsys.readouterr().err
    if emits_diagnostic:
        assert "requires separate stdout and stderr" in diagnostic
    else:
        assert diagnostic == ""


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


@pytest.mark.parametrize("alias", ["stdout", "stderr"])
def test_stdout_compile_rejects_manifest_replaced_with_alias(tmp_path, alias):
    script = """
import os
import sys
from ansiroll import cli

target, manifest = sys.argv[1:]
cli.compile_playbook = lambda *a, **k: ("pass", {"modules": [], "fallback_tasks": []})
open_fd = os.open

def replace_manifest(path, flags, *args, **kwargs):
    if path == manifest and flags & os.O_ACCMODE == os.O_WRONLY:
        os.unlink(manifest)
        os.symlink(target, manifest)
    return open_fd(path, flags, *args, **kwargs)

os.open = replace_manifest
sys.argv[1:] = ["compile", "play.yml", "-o", "-", "-m", manifest]
raise SystemExit(cli.entrypoint())
"""
    output = tmp_path / "compiled.py"
    output.write_text("# existing output\n")
    manifest = tmp_path / "manifest.json"
    manifest.touch()
    diagnostics = tmp_path / "diagnostics.txt"
    diagnostics.write_text("existing diagnostics\n")
    target = output if alias == "stdout" else diagnostics
    with output.open("r+b") as stream, diagnostics.open("ab") as errors:
        result = subprocess.run(
            [sys.executable, "-c", script, str(target), str(manifest)],
            stdout=stream, stderr=errors,
        )

    assert result.returncode == 2
    assert output.read_text() == "# existing output\n"
    assert diagnostics.read_text() == "existing diagnostics\nansiroll: manifest path points to %s: %s\n" % (
        alias, manifest)


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


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
@pytest.mark.parametrize("destination", ["output", "manifest", "stdout-manifest"])
def test_compile_rejects_destinations_aliasing_the_playbook(tmp_path, alias, destination):
    pytest.importorskip("ansible")
    playbook = tmp_path / "play.yml"
    original = b"- hosts: localhost\n  gather_facts: false\n  tasks: []\n"
    playbook.write_bytes(original)
    playbook.chmod(0o640)
    target = playbook if alias == "same" else tmp_path / "alias"
    if alias == "symlink":
        target.symlink_to(playbook)
    elif alias == "hardlink":
        os.link(playbook, target)

    output = tmp_path / "compiled.py"
    args = [sys.executable, "-m", "ansiroll.cli", "compile", str(playbook)]
    if destination == "output":
        args.extend(["-o", str(target)])
    else:
        args.extend(["-o", "-" if destination == "stdout-manifest" else str(output), "-m", str(target)])
    result = subprocess.run(args, capture_output=True, timeout=10)

    assert result.returncode == 2
    assert b"points to input playbook" in result.stderr
    assert not result.stdout
    assert playbook.read_bytes() == original
    assert stat.S_IMODE(playbook.stat().st_mode) == 0o640
    assert not output.exists()


@pytest.mark.parametrize("name", ["play.yml", "$ANSIROLL_PLAYBOOK_BASENAME"])
def test_compile_checks_the_expanded_playbook_path(tmp_path, monkeypatch, name):
    pytest.importorskip("ansible")
    playbook = tmp_path / "play.yml"
    original = b"- hosts: localhost\n  gather_facts: false\n  tasks: []\n"
    playbook.write_bytes(original)
    monkeypatch.setenv("ANSIROLL_INPUT_NAME", name)
    monkeypatch.setenv("ANSIROLL_PLAYBOOK_BASENAME", playbook.name)
    result = subprocess.run([sys.executable, "-m", "ansiroll.cli", "compile",
                             str(tmp_path / "$ANSIROLL_INPUT_NAME"), "-o", str(playbook)],
                            capture_output=True, timeout=10)
    assert result.returncode == 2
    assert b"points to input playbook" in result.stderr
    assert playbook.read_bytes() == original


def test_compile_checks_absolute_input_with_expansion_and_parent_traversal(tmp_path, monkeypatch):
    pytest.importorskip("ansible")
    from ansible.parsing.dataloader import DataLoader

    monkeypatch.setenv("ANSIROLL_INPUT_DIR", "parent/child")
    directory = tmp_path / "$ANSIROLL_INPUT_DIR" / ".."
    loader = DataLoader()
    loader.set_basedir(str(directory))
    playbook = Path(loader.path_dwim("play.yml"))
    playbook.parent.mkdir(exist_ok=True)
    original = b"- hosts: localhost\n  gather_facts: false\n  tasks: []\n"
    playbook.write_bytes(original)
    result = subprocess.run([sys.executable, "-m", "ansiroll.cli", "compile", str(directory / "play.yml"),
                             "-o", str(playbook)], capture_output=True, timeout=10)
    assert result.returncode == 2
    assert b"points to input playbook" in result.stderr
    assert playbook.read_bytes() == original


@pytest.mark.skipif(os.name != "posix", reason="filename contains quotes and a backslash")
def test_compile_preserves_a_literal_playbook_name_with_an_escaped_final_quote(tmp_path):
    pytest.importorskip("ansible")
    playbook = tmp_path / '"play\\"'
    original = b"- hosts: localhost\n  gather_facts: false\n  tasks: []\n"
    playbook.write_bytes(original)
    result = subprocess.run([sys.executable, "-m", "ansiroll.cli", "compile", str(playbook), "-o", str(playbook)],
                            capture_output=True, timeout=10)
    assert result.returncode == 2
    assert b"points to input playbook" in result.stderr
    assert playbook.read_bytes() == original


@pytest.mark.parametrize("destination", ["output-only", "output-with-manifest", "manifest"])
def test_compile_checks_destination_inode_after_open(monkeypatch, tmp_path, capsys, destination):
    pytest.importorskip("ansible")
    playbook = tmp_path / "play.yml"
    original = "- hosts: localhost\n  gather_facts: false\n  tasks: []\n"
    playbook.write_text(original)
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    open_fd = os.open

    target = manifest if destination == "manifest" else output

    def replace_destination_before_open(path, flags, *args, **kwargs):
        if path == str(target) and flags & os.O_ACCMODE == os.O_WRONLY and not target.exists():
            target.symlink_to(playbook)
        return open_fd(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_destination_before_open)
    args = ["compile", str(playbook), "-o", str(output)]
    if destination != "output-only":
        args.extend(["-m", str(manifest)])

    assert cli.main(args) == 2
    assert "points to input playbook" in capsys.readouterr().err
    assert playbook.read_text() == original
    if destination == "manifest":
        assert output.read_text().startswith("#!")
    else:
        assert not manifest.exists()


def test_compile_preserves_the_inode_it_read_when_the_playbook_symlink_changes(tmp_path, monkeypatch, capsys):
    pytest.importorskip("ansible")
    from ansible.parsing.dataloader import DataLoader

    original = "- hosts: localhost\n  gather_facts: false\n  tasks: []\n"
    initial, selected, final = (tmp_path / name for name in ("first.yml", "selected.yml", "final.yml"))
    for path in (initial, selected, final):
        path.write_text(original)
    playbook = tmp_path / "play.yml"
    playbook.symlink_to(initial)
    load_from_file = DataLoader.load_from_file

    def load_after_replacing_link(loader, path, *args, **kwargs):
        if path != playbook.name:
            return load_from_file(loader, path, *args, **kwargs)
        playbook.unlink()
        playbook.symlink_to(selected)
        try:
            return load_from_file(loader, path, *args, **kwargs)
        finally:
            playbook.unlink()
            playbook.symlink_to(final)

    monkeypatch.setattr(DataLoader, "load_from_file", load_after_replacing_link)
    assert cli.main(["compile", str(playbook), "-o", str(selected)]) == 2
    assert "output path points to input playbook" in capsys.readouterr().err
    assert selected.read_text() == original
    assert playbook.resolve() == final


def test_compile_reports_ansible_configuration_errors_for_an_expanded_input(tmp_path):
    release = pytest.importorskip("ansible.release")
    output = tmp_path / "compiled.py"
    result = subprocess.run([sys.executable, "-m", "ansiroll.cli", "compile", "$ANSIROLL_INPUT", "-o", str(output)],
                            env=dict(os.environ, ANSIBLE_VERBOSITY="bogus", ANSIROLL_INPUT="input.yml"),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    if tuple(map(int, release.__version__.split(".")[:2])) >= (2, 16):
        assert "Traceback" not in result.stderr
    assert "DEFAULT_VERBOSITY" in result.stderr
    assert not output.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="requires the Linux C locale")
def test_legacy_compile_checks_ansible_filename_bytes_in_an_ascii_locale(tmp_path):
    release = pytest.importorskip("ansible.release")
    if tuple(map(int, release.__version__.split(".")[:2])) >= (2, 17):
        pytest.skip("newer Ansible requires a UTF-8 locale")

    playbook = tmp_path / "play-é.yml"
    original = b"- hosts: localhost\n  gather_facts: false\n  tasks: []\n"
    playbook.write_bytes(original)
    result = subprocess.run([sys.executable, "-m", "ansiroll.cli", "compile",
                             str(tmp_path / "$ANSIROLL_INPUT"), "-o", str(playbook)],
                            env=dict(os.environ, LC_ALL="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0",
                                     ANSIROLL_INPUT=playbook.name),
                            capture_output=True, timeout=10)
    assert result.returncode == 2
    assert b"output path points to input playbook" in result.stderr
    assert playbook.read_bytes() == original


def test_stdout_compile_rejects_output_descriptor_aliasing_the_playbook(tmp_path):
    pytest.importorskip("ansible")
    playbook = tmp_path / "play.yml"
    original = b"- hosts: localhost\n  gather_facts: false\n  tasks: []\n"
    playbook.write_bytes(original)
    with playbook.open("ab") as destination:
        result = subprocess.run([sys.executable, "-m", "ansiroll.cli", "compile", str(playbook), "-o", "-"],
                                stdout=destination, stderr=subprocess.PIPE, timeout=10)
    assert result.returncode == 2
    assert b"stdout points to input playbook" in result.stderr
    assert playbook.read_bytes() == original


@pytest.mark.parametrize(("destination", "alias"), [
    ("output", "direct"), ("output", "hardlink"), ("output", "symlink"),
    ("manifest", "hardlink"), ("stdout", "direct"),
])
def test_compile_rejects_destinations_aliasing_a_transitive_import(tmp_path, destination, alias):
    pytest.importorskip("ansible")
    playbook = tmp_path / "play.yml"
    (tmp_path / "nested").mkdir()
    imported = tmp_path / "nested" / "child.yml"
    original = b"- hosts: localhost\n  gather_facts: false\n  tasks: []\n"
    imported.write_bytes(original)
    (tmp_path / "parent.yml").write_text("- import_playbook: nested/child.yml\n")
    playbook.write_text("- import_playbook: parent.yml\n")
    target = imported if alias == "direct" else tmp_path / "alias"
    if alias == "hardlink":
        os.link(imported, target)
    elif alias == "symlink":
        target.symlink_to(imported)
    command = [sys.executable, "-m", "ansiroll.cli", "compile", str(playbook)]
    if destination == "stdout":
        with target.open("ab") as stdout:
            result = subprocess.run(command + ["-o", "-"], stdout=stdout, stderr=subprocess.PIPE, timeout=10)
    else:
        args = (["-o", str(target)] if destination == "output"
                else ["-o", str(tmp_path / "compiled.py"), "-m", str(target)])
        result = subprocess.run(command + args, capture_output=True, timeout=10)
    assert result.returncode == 2, result.stderr
    assert b"points to input playbook" in result.stderr
    assert imported.read_bytes() == original


@pytest.mark.parametrize("encrypted", [False, True])
def test_compile_protects_import_previously_read_through_vars_files(tmp_path, encrypted):
    pytest.importorskip("ansible")
    playbook = tmp_path / "play.yml"
    imported = tmp_path / "child.yml"
    original = b"- hosts: localhost\n  gather_facts: false\n  tasks: []\n"
    vault_args = []
    if encrypted:
        from ansible.parsing.vault import VaultLib, VaultSecret

        password = tmp_path / "vault-password"
        password.write_text("fixture-password\n")
        secret = VaultSecret(b"fixture-password")
        original = VaultLib([("default", secret)]).encrypt(original, secret=secret)
        vault_args = ["--vault-password-file", str(password)]
    imported.write_bytes(original)
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars_files: [child.yml]
  tasks: []
- import_playbook: child.yml
""")
    command = [sys.executable, "-m", "ansiroll.cli", "compile", str(playbook), "-o", str(imported)]
    result = subprocess.run(command + vault_args, capture_output=True, timeout=10)
    assert result.returncode == 2, result.stderr
    assert b"points to input playbook" in result.stderr
    assert imported.read_bytes() == original


def test_playbook_open_observer_bypasses_a_populated_vault_cache(tmp_path):
    pytest.importorskip("ansible")
    from ansible.parsing.dataloader import DataLoader
    from ansible.parsing.vault import VaultLib, VaultSecret
    from ansible.playbook import Playbook
    from ansiroll.loader import _core_version, _make_rewriting_loader

    playbook = tmp_path / "child.yml"
    secret = VaultSecret(b"fixture-password")
    secrets = [("default", secret)]
    playbook.write_bytes(VaultLib(secrets).encrypt(
        b"- hosts: localhost\n  gather_facts: false\n  tasks: []\n", secret=secret))
    loader = _make_rewriting_loader(DataLoader)
    loader.set_vault_secrets(secrets)
    loader.load_from_file(str(playbook), cache="vaulted" if _core_version() >= (2, 17) else True)
    assert str(playbook) in loader._FILE_CACHE

    opened = []
    loader.on_playbook_open = lambda path, st: opened.append((Path(os.fsdecode(path)), st.st_ino))
    Playbook.load(str(playbook), loader=loader)
    identity = (playbook, playbook.stat().st_ino)
    assert opened == [identity]
    playbook.unlink()
    assert len(Playbook.load(str(playbook), loader=loader).get_plays()) == 1
    assert opened == [identity]


def test_playbook_open_observer_bypasses_a_cached_empty_document(tmp_path):
    pytest.importorskip("ansible")
    from ansible.errors import AnsibleParserError
    from ansible.parsing.dataloader import DataLoader
    from ansible.playbook import Playbook
    from ansiroll.loader import _make_rewriting_loader

    playbook = tmp_path / "empty.yml"
    playbook.write_text("# empty\n")
    loader = _make_rewriting_loader(DataLoader)
    assert loader.load_from_file(str(playbook)) is None
    assert str(playbook) in loader._FILE_CACHE

    opened = []
    loader.on_playbook_open = lambda path, info: opened.append((Path(os.fsdecode(path)), info.st_ino))
    with pytest.raises(AnsibleParserError, match="Empty playbook"):
        Playbook.load(str(playbook), loader=loader)
    assert opened == [(playbook, playbook.stat().st_ino)]


def test_stdout_compile_rechecks_playbook_after_writing_manifest(monkeypatch, tmp_path, capsys):
    playbook = tmp_path / "play.yml"
    destination = tmp_path / "stdout"
    playbook.write_bytes(b"input playbook")
    destination.write_bytes(b"")

    def compile_playbook(path, *, on_playbook_open, **kwargs):
        on_playbook_open(path, playbook.stat())
        return "pass\n", {"modules": [], "fallback_tasks": []}

    def write_manifest(*args, **kwargs):
        playbook.unlink()
        playbook.symlink_to(destination)

    monkeypatch.setattr(cli, "compile_playbook", compile_playbook)
    monkeypatch.setattr(cli, "_write_stdout_manifest", write_manifest)
    args = Namespace(playbook=str(playbook), output="-", manifest=str(tmp_path / "manifest.json"),
                     interpreter="python3", extra_vars=[], vault_password_file=None, tree_shake=False,
                     freeze_playbook_vars=False, freeze_var=[])
    with destination.open("ab") as stdout:
        assert cli._cmd_compile(args, stdout) == 2
    assert "stdout points to input playbook" in capsys.readouterr().err
    assert destination.read_bytes() == b""


@pytest.mark.parametrize("target", ["output", "manifest"])
def test_file_compile_checks_input_after_the_other_destination_lookup(monkeypatch, cli_os, tmp_path, capsys, target):
    pytest.importorskip("ansible")
    playbook = tmp_path / "play.yml"
    playbook.write_text("- hosts: localhost\n  gather_facts: false\n  tasks: []\n")
    output, manifest = (tmp_path / name for name in ("compiled.py", "manifest.json"))
    destination, other = (output, manifest) if target == "output" else (manifest, output)
    original = b"existing destination\n"
    destination.write_bytes(original)
    opened = replaced = False

    def track_write_open(path, flags, *args, **kwargs):
        nonlocal opened
        fd = os.open(path, flags, *args, **kwargs)
        if path == str(destination) and flags & os.O_ACCMODE == os.O_WRONLY:
            opened = True
        return fd

    def replace_input_on_lookup(path, *args, **kwargs):
        nonlocal replaced
        if opened and not replaced and path == str(other):
            playbook.rename(tmp_path / "original.yml")
            playbook.symlink_to(destination)
            replaced = True
        return os.stat(path, *args, **kwargs)

    cli_os.open = track_write_open
    cli_os.stat = replace_input_on_lookup
    status = cli.main(["compile", str(playbook), "-o", str(output), "-m", str(manifest)])
    assert replaced
    assert status == 2
    assert "points to input playbook" in capsys.readouterr().err
    assert destination.read_bytes() == original


@pytest.mark.parametrize("existing", [False, True])
def test_file_compile_rechecks_input_after_pinning_output(monkeypatch, tmp_path, capsys, existing):
    pytest.importorskip("ansible")
    playbook = tmp_path / "play.yml"
    playbook.write_text("- hosts: localhost\n  gather_facts: false\n  tasks: []\n")
    original = tmp_path / "original.yml"
    output = tmp_path / "compiled.py"
    previous = b"previous artifact\n" if existing else b""
    if existing:
        output.write_bytes(previous)

    def replace_input_during_pin(*args):
        playbook.rename(original)
        playbook.symlink_to(output)

    monkeypatch.setattr(cli, "_pin_output_inode", replace_input_during_pin)
    assert cli.main(["compile", str(playbook), "-o", str(output)]) == 2
    assert "output path points to input playbook" in capsys.readouterr().err
    assert playbook.read_bytes() == previous
    assert original.read_text().startswith("- hosts: localhost\n")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires named pipes")
def test_file_compile_rechecks_input_before_writing_to_fifo(monkeypatch, cli_os, tmp_path, capsys):
    pytest.importorskip("ansible")
    playbook = tmp_path / "play.yml"
    source = "- hosts: localhost\n  gather_facts: false\n  tasks: []\n"
    playbook.write_text(source)
    original = tmp_path / "original.yml"
    output = tmp_path / "compiled.pipe"
    os.mkfifo(output)
    compile_playbook = cli.compile_playbook
    writer_fd = None
    writer_checks = 0

    def compile_small_output(*args, **kwargs):
        _, manifest = compile_playbook(*args, **kwargs)
        return "pass\n", manifest

    def track_writer(path, flags, *args):
        nonlocal writer_fd
        fd = os.open(path, flags, *args)
        if path == str(output) and flags & os.O_ACCMODE == os.O_WRONLY:
            writer_fd = fd
        return fd

    def replace_input_before_write(fd):
        nonlocal writer_checks
        if fd == writer_fd:
            writer_checks += 1
            if writer_checks == 2:
                playbook.rename(original)
                playbook.symlink_to(output)
        return os.fstat(fd)

    monkeypatch.setattr(cli, "compile_playbook", compile_small_output)
    cli_os.open = track_writer
    cli_os.fstat = replace_input_before_write
    reader = os.open(output, os.O_RDONLY | os.O_NONBLOCK)
    try:
        status = cli.main(["compile", str(playbook), "-o", str(output)])
        assert (status, os.read(reader, 4096)) == (2, b"")
    finally:
        os.close(reader)
    assert "output path points to input playbook" in capsys.readouterr().err
    assert playbook.resolve() == output
    assert original.read_text() == source


@pytest.mark.parametrize("error", [errno.EACCES, errno.EIO])
def test_file_compile_preserves_destination_when_current_input_identity_is_unavailable(
        cli_os, tmp_path, capsys, error):
    pytest.importorskip("ansible")
    playbook = tmp_path / "play.yml"
    playbook.write_text("- hosts: localhost\n  gather_facts: false\n  tasks: []\n")
    output, manifest = (tmp_path / name for name in ("compiled.py", "manifest.json"))
    original = b"existing destination\n"
    output.write_bytes(original)
    replaced = False

    def replace_input_on_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == str(output) and flags & os.O_ACCMODE == os.O_WRONLY and not replaced:
            playbook.rename(tmp_path / "original.yml")
            playbook.symlink_to(output)
            replaced = True
        return os.open(path, flags, *args, **kwargs)

    def unavailable_input(path, *args, **kwargs):
        if replaced and os.fsdecode(path) == str(playbook):
            raise OSError(error, os.strerror(error), path)
        return os.stat(path, *args, **kwargs)

    cli_os.open = replace_input_on_open
    cli_os.stat = unavailable_input
    status = cli.main(["compile", str(playbook), "-o", str(output), "-m", str(manifest)])
    assert replaced
    assert status == 2
    assert "cannot check current input playbook" in capsys.readouterr().err
    assert output.read_bytes() == original
    assert not manifest.exists()


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
            if path == str(output) and flags & os.O_ACCMODE == os.O_WRONLY:
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

    def aliasing_open(path, flags, *args, **kwargs):
        if path == str(alias) and flags & getattr(os, "O_PATH", 0):
            raise OSError(errno.EMFILE, "metadata open cannot allocate a descriptor", path)
        return open_fd(str(output) if path == str(alias) else path, flags, *args, **kwargs)

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
def test_compile_reopens_validated_destination_with_truncation(
        monkeypatch, cli_os, tmp_path, destination, failure, error):
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
    active_fds = set()

    def open_append_only(path, flags, *args, **kwargs):
        nonlocal append_fd
        target = path == str(files[destination]) and flags & os.O_ACCMODE == os.O_WRONLY
        if target and failure == "open":
            raise OSError(error, "filesystem requires O_TRUNC", path)
        if destination == "output" and not reopened:
            for fd in active_fds.copy():
                try:
                    os.fstat(fd)
                except OSError as ex:
                    assert ex.errno == errno.EBADF
                    active_fds.remove(fd)
            if len(active_fds) == 2:
                raise OSError(errno.EMFILE, "no slot for another output descriptor", path)
        fd = open_fd(path, flags, *args, **kwargs)
        active_fds.add(fd)
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

    cli_os.open = open_append_only
    cli_os.fdopen = wrap_append_handle
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)]) == 0
    assert len(reopened) == 1
    assert output.read_text() == source
    assert json.loads(manifest.read_text()) == data


@pytest.mark.parametrize("manifest", [False, True])
@pytest.mark.parametrize("reopen", [False, True])
def test_destination_revalidates_after_truncation(monkeypatch, tmp_path, manifest, reopen):
    path = tmp_path / "existing"
    path.write_text("previous artifact\n")
    truncated = False
    fdopen = os.fdopen

    def track_truncation(fd, mode, *args, **kwargs):
        writer = fdopen(fd, mode, *args, **kwargs)
        truncate = writer.truncate

        def truncate_artifact(size):
            nonlocal truncated
            if reopen:
                raise OSError(errno.EOPNOTSUPP, "requires truncating reopen")
            truncate(size)
            truncated = True

        writer.truncate = truncate_artifact
        return writer

    def reopen_artifact(*args):
        nonlocal truncated
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
        truncated = True
        return fd

    def validate(info, fd):
        if truncated:
            raise ValueError("destination no longer safe")

    if reopen and not os.path.isdir("/proc/self/fd"):
        pytest.skip("requires procfs descriptor links")
    monkeypatch.setattr(cli.os, "fdopen", track_truncation)
    monkeypatch.setattr(cli, "_open_truncated_destination", reopen_artifact)
    with pytest.raises(ValueError, match="destination no longer safe"):
        with cli._open_compile_destination(str(path), validate, manifest=manifest):
            pytest.fail("received writer after destination validation failed")
    assert truncated
    assert path.read_bytes() == b""



@pytest.mark.skipif(not hasattr(os, "O_PATH") or not os.path.isdir("/proc/self/fd"),
                    reason="requires reopening a metadata-only descriptor")
def test_compile_rechecks_input_after_procfs_lookup(monkeypatch, cli_os, tmp_path, capsys):
    pytest.importorskip("ansible")
    playbook = tmp_path / "play.yml"
    playbook.write_text("- hosts: localhost\n  gather_facts: false\n  tasks: []\n")
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    original = "previous output\n"
    output.write_text(original)
    replaced = False

    def require_truncate(path, flags, *args, **kwargs):
        if path == str(output) and flags & os.O_ACCMODE == os.O_WRONLY:
            raise PermissionError(errno.EPERM, "filesystem requires O_TRUNC", path)
        return os.open(path, flags, *args, **kwargs)

    def replace_on_procfs_lookup(path, *args, **kwargs):
        nonlocal replaced
        if str(path).startswith("/proc/self/fd/") and not replaced:
            playbook.rename(tmp_path / "original.yml")
            playbook.symlink_to(output)
            replaced = True
        return os.stat(path, *args, **kwargs)

    cli_os.open = require_truncate
    cli_os.stat = replace_on_procfs_lookup
    status = cli.main(["compile", str(playbook), "-o", str(output), "-m", str(manifest)])
    assert replaced
    assert status == 2
    assert "output path points to input playbook" in capsys.readouterr().err
    assert output.read_text() == original
    assert not manifest.exists()


@pytest.mark.parametrize("through_symlinks", [False, True])
def test_compile_writes_new_files_without_truncation_or_procfs(monkeypatch, tmp_path, through_symlinks):
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    if through_symlinks:
        output.symlink_to(tmp_path / "actual.py")
        manifest.symlink_to(tmp_path / "actual.json")
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


@pytest.mark.parametrize("mode", ["stdout", "file"])
def test_compile_rechecks_input_after_preparing_manifest_writer(cli_os, tmp_path, capsys, mode):
    pytest.importorskip("ansible")
    playbook = tmp_path / "play.yml"
    source = "- hosts: localhost\n  gather_facts: false\n  tasks: []\n"
    playbook.write_text(source)
    original = tmp_path / "original.yml"
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    manifest.touch()
    writer_fd = None
    truncate_failed = replaced = False

    def track_manifest_writer(path, flags, *args):
        nonlocal writer_fd
        fd = os.open(path, flags, *args)
        if path == str(manifest) and flags & os.O_ACCMODE == os.O_WRONLY:
            writer_fd = fd
        return fd

    def cannot_truncate(_size):
        nonlocal truncate_failed
        truncate_failed = True
        raise OSError(errno.EOPNOTSUPP, "standalone truncation unavailable")

    def wrap_writer(fd, mode):
        file = os.fdopen(fd, mode)
        if fd == writer_fd:
            file.truncate = cannot_truncate
        return file

    def replace_input_on_empty_file_check(fd):
        nonlocal replaced
        if fd == writer_fd and truncate_failed and not replaced:
            playbook.rename(original)
            playbook.symlink_to(manifest)
            replaced = True
        return os.fstat(fd)

    cli_os.open = track_manifest_writer
    cli_os.fdopen = wrap_writer
    cli_os.fstat = replace_input_on_empty_file_check
    if mode == "stdout":
        args = Namespace(playbook=str(playbook), output="-", manifest=str(manifest),
                         interpreter="python3", extra_vars=[], vault_password_file=None, tree_shake=False,
                         freeze_playbook_vars=False, freeze_var=[])
        with output.open("wb") as stdout:
            status = cli._cmd_compile(args, stdout)
    else:
        status = cli.main(["compile", str(playbook), "-o", str(output), "-m", str(manifest)])
    assert status == 2 and replaced
    assert "manifest path points to input playbook" in capsys.readouterr().err
    assert manifest.read_bytes() == b""
    assert original.read_text() == source


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="requires metadata-only file descriptors")
@pytest.mark.parametrize("destination", ["output", "manifest"])
@pytest.mark.parametrize("unsupported_truncate", [False, True])
def test_compile_releases_optional_pin_when_write_open_hits_emfile(
    monkeypatch, tmp_path, destination, unsupported_truncate,
):
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    target = output if destination == "output" else manifest
    target.write_text("" if unsupported_truncate else "longer prior contents")
    data = {"modules": [], "fallback_tasks": []}
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", data))
    open_fd, fdopen = os.open, os.fdopen
    reference = None
    writer_fd = None
    attempts = 0

    def with_one_free_slot(path, flags, *args):
        nonlocal reference, writer_fd, attempts
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
            writer_fd = open_fd(path, flags, *args)
            return writer_fd
        return open_fd(path, flags, *args)

    def with_optional_truncate(fd, mode):
        file = fdopen(fd, mode)
        if unsupported_truncate and fd == writer_fd:
            def truncate(size):
                raise OSError(errno.EOPNOTSUPP, "standalone truncation unavailable")

            file.truncate = truncate
        return file

    # There is no spare descriptor to pin the output while its writer is open.
    monkeypatch.setattr(cli, "_pin_output_inode", lambda *args: None)
    monkeypatch.setattr(os, "open", with_one_free_slot)
    monkeypatch.setattr(os, "fdopen", with_optional_truncate)
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)]) == 0
    assert attempts == 2
    assert output.read_text() == "pass\n"
    assert json.loads(manifest.read_text()) == data


@pytest.mark.skipif(not hasattr(os, "O_PATH") or not os.path.isdir("/proc/self/fd"),
                    reason="requires reopening a metadata-only descriptor")
@pytest.mark.parametrize("manifest_pin_available", [True, False])
def test_manifest_open_can_release_output_pin(monkeypatch, tmp_path, manifest_pin_available):
    output = tmp_path / "compiled.py"
    manifest = tmp_path / "manifest.json"
    manifest.write_text("previous manifest with trailing content")
    data = {"modules": [], "fallback_tasks": []}
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("pass\n", data))
    open_fd, pin_output = os.open, cli._pin_output_inode
    output_pin = manifest_pin = None
    attempts = 0
    reopened = []

    def track_output_pin(*args):
        nonlocal output_pin
        output_pin = pin_output(*args)
        assert output_pin is not None
        return output_pin

    def open_with_limited_descriptors(path, flags, *args, **kwargs):
        nonlocal manifest_pin, attempts
        if path == str(manifest) and flags & os.O_PATH:
            if not manifest_pin_available:
                raise OSError(errno.EMFILE, "no slot for the manifest pin", path)
            manifest_pin = open_fd(path, flags, *args, **kwargs)
            return manifest_pin
        if path == str(manifest) and flags & os.O_ACCMODE == os.O_WRONLY:
            attempts += 1
            assert output_pin is not None
            if manifest_pin_available:
                assert manifest_pin is not None
                assert os.path.samestat(os.fstat(manifest_pin), manifest.stat())
            if attempts == 1:
                assert os.path.samestat(os.fstat(output_pin), output.stat())
                raise OSError(errno.EMFILE, "no free descriptor slots", path)
            with pytest.raises(OSError) as error:
                os.fstat(output_pin)
            assert error.value.errno == errno.EBADF
            if manifest_pin_available:
                raise OSError(errno.EPERM, "filesystem requires O_TRUNC", path)
        if str(path).startswith("/proc/self/fd/") and flags & os.O_TRUNC:
            reopened.append(path)
        return open_fd(path, flags, *args, **kwargs)

    monkeypatch.setattr(cli, "_pin_output_inode", track_output_pin)
    monkeypatch.setattr(os, "open", open_with_limited_descriptors)
    assert cli.main(["compile", "play.yml", "-o", str(output), "-m", str(manifest)]) == 0
    assert attempts == 2
    assert reopened == ([f"/proc/self/fd/{manifest_pin}"] if manifest_pin_available else [])
    assert output.read_text() == "pass\n"
    assert json.loads(manifest.read_text()) == data


@pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="requires metadata-only file descriptors")
@pytest.mark.parametrize("destination", ["stdout-manifest", "output"])
def test_compile_truncating_reopen_reports_unavailable_procfs(monkeypatch, tmp_path, capsys, destination):
    output = tmp_path / "stdout"
    manifest = tmp_path / "manifest.json"
    target = output if destination == "output" else manifest
    target.write_text("previous content")
    open_fd, stat_path = os.open, os.stat

    def require_truncate(path, flags, *args, **kwargs):
        if path == str(target) and flags & os.O_ACCMODE == os.O_WRONLY:
            raise PermissionError(errno.EPERM, "filesystem requires O_TRUNC", path)
        return open_fd(path, flags, *args, **kwargs)

    def stat_without_procfs(path, *args, **kwargs):
        if str(path).startswith("/proc/self/fd/"):
            raise FileNotFoundError(errno.ENOENT, "procfs unavailable", path)
        return stat_path(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", require_truncate)
    monkeypatch.setattr(os, "stat", stat_without_procfs)
    if destination == "output":
        monkeypatch.setattr(cli, "compile_playbook", lambda *args, **kwargs: ("pass\n", {}))
        assert cli.main(["compile", "play.yml", "-o", str(output)]) == 2
        assert "Compile to a local staging file" in capsys.readouterr().err
    else:
        with output.open("w") as stream:
            with pytest.raises(OSError, match="safe truncating reopen requires /proc/self/fd"):
                cli._write_stdout_manifest(str(manifest), {}, stream.fileno())
    assert target.read_text() == "previous content"


@pytest.mark.parametrize("error", [errno.EINVAL, errno.EOPNOTSUPP, errno.EACCES, errno.EPERM])
def test_compile_handles_a_failed_output_open_without_a_metadata_pin(monkeypatch, cli_os, tmp_path, capsys, error):
    output = tmp_path / "compiled.py"
    original = "previous Python"
    output.write_text(original)
    monkeypatch.setattr(cli, "compile_playbook", lambda *args, **kwargs: ("pass\n", {}))
    monkeypatch.delattr(cli_os, "O_PATH", raising=False)
    open_fd = cli_os.open

    def require_truncate(path, flags, *args, **kwargs):
        if path == str(output) and flags & os.O_ACCMODE == os.O_WRONLY and not flags & os.O_EXCL:
            raise OSError(error, os.strerror(error), path)
        return open_fd(path, flags, *args, **kwargs)

    monkeypatch.setattr(cli_os, "open", require_truncate)
    args = ["compile", "play.yml", "-o", str(output)]
    if error in (errno.EACCES, errno.EPERM):
        with pytest.raises(PermissionError) as failure:
            cli.main(args)
        assert failure.value.errno == error
        assert not capsys.readouterr().err
    else:
        assert cli.main(args) == 2
        assert "Compile to a local staging file" in capsys.readouterr().err
    assert output.read_text() == original


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


@pytest.mark.skipif(not hasattr(os, "O_PATH") or not os.path.isdir("/proc/self/fd"),
                    reason="requires reopening a metadata-only descriptor")
def test_pin_output_inode_uses_metadata_before_opening_a_reader(monkeypatch, tmp_path):
    output = tmp_path / "compiled.py"
    open_fd = os.open
    with output.open("w") as writer:
        writer_fd = writer.fileno()
        original = os.fstat(writer_fd)

        def metadata_only(path, flags):
            assert path == f"/proc/self/fd/{writer_fd}"
            assert flags == os.O_PATH
            return open_fd(path, flags)

        monkeypatch.setattr(os, "open", metadata_only)
        reference = cli._pin_output_inode(str(output), writer_fd, original)
        assert reference is not None
        try:
            assert os.path.samestat(original, os.fstat(reference))
        finally:
            os.close(reference)


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
        manifest.write_text("{}")
        stat_path = os.stat

        def replace_with_fifo(path, *args, **kwargs):
            if path == str(manifest) and output.exists() and fifo.exists():
                os.replace(fifo, manifest)
            return stat_path(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", replace_with_fifo)
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


@pytest.mark.skipif(not hasattr(os, "O_NONBLOCK"), reason="requires nonblocking descriptor opens")
def test_manifest_wait_checks_destination_before_retrying(monkeypatch, cli_os):
    fifo = Namespace(st_mode=stat.S_IFIFO)

    def open_without_reader(path, flags, mode):
        assert flags & os.O_NONBLOCK
        raise OSError(errno.ENXIO, "no FIFO reader")

    def reject_destination(info, fd):
        assert info is fifo and fd is None
        raise ValueError("manifest path points to compiled output")

    cli_os.open = open_without_reader
    cli_os.stat = lambda path: fifo
    monkeypatch.setattr(cli.time, "sleep", lambda delay: pytest.fail("waited for unvalidated destination"))
    with pytest.raises(ValueError, match="manifest path points to compiled output"):
        cli._open_manifest_fd("manifest", os.O_WRONLY, reject_destination, None)


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
@pytest.mark.parametrize("failure", [errno.ENOENT, errno.EACCES, errno.ELOOP])
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
            raise OSError(failure, "descriptor link unavailable", path)
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


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires named pipes")
@pytest.mark.parametrize("manifest_first", [False, True])
def test_stdout_compile_streams_with_either_sequential_fifo_order(tmp_path, manifest_first):
    manifest = tmp_path / "manifest.fifo"
    os.mkfifo(manifest)
    reader_script = """
import sys
from pathlib import Path

def read_stdout():
    sys.stdout.write(sys.stdin.read())

def read_manifest():
    sys.stdout.write(Path(sys.argv[1]).read_text())

if sys.argv[2] == "True":
    read_manifest()
    read_stdout()
else:
    read_stdout()
    read_manifest()
"""
    compile_script = """
import sys
from ansiroll import cli

cli.compile_playbook = lambda *a, **k: (
    'pass\\n' * (512 * 1024),
    {'modules': [], 'fallback_tasks': [], 'padding': 'x' * (2 * 1024 * 1024)},
)
raise SystemExit(cli.main(['compile', 'play.yml', '-o', '-', '-m', sys.argv[1]], _standalone=True))
"""
    producer = subprocess.Popen(
        [sys.executable, "-c", compile_script, str(manifest)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    reader = subprocess.Popen(
        [sys.executable, "-c", reader_script, str(manifest), str(manifest_first)],
        stdin=producer.stdout, stdout=subprocess.PIPE, text=True,
    )
    producer.stdout.close()
    try:
        received, _ = reader.communicate(timeout=30)
        _, diagnostics = producer.communicate(timeout=30)
    finally:
        for process in (producer, reader):
            if process.poll() is None:
                process.kill()
                process.communicate()

    assert producer.returncode == reader.returncode == 0, diagnostics
    source = "pass\n" * (512 * 1024)
    metadata = json.dumps({"modules": [], "fallback_tasks": [], "padding": "x" * (2 * 1024 * 1024)}, indent=2)
    assert received == (metadata + source if manifest_first else source + metadata)


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
