"""Command-line interface: ansiroll compile|run."""

from __future__ import annotations

import argparse
import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout

from .compiler import compile_playbook


def entrypoint():
    return main(_standalone=True)


class _ArgumentParser(argparse.ArgumentParser):
    compile_parser = None
    compile_operands = None
    parsed = None

    def parse_known_args(self, args=None, namespace=None):
        if namespace is None:
            namespace = argparse.Namespace()
        self.parsed = namespace
        return super().parse_known_args(args, namespace)

    def error(self, message):
        compile_parser = self.compile_parser
        if compile_parser is not None:
            playbook = getattr(compile_parser.parsed, "playbook", None)
            # An option error can occur before argparse reaches the positional.
            # Treat the remaining tokens as possible inputs in that case.
            candidates = [playbook] if playbook is not None else compile_parser.compile_operands
            if any(_stderr_may_be_playbook(path) for path in candidates):
                self.exit(2)
        super().error(message)


def main(argv=None, *, _standalone=False):
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = _ArgumentParser(prog="ansiroll", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    pc = sub.add_parser("compile", help="Compile a playbook to a single .py file")
    pc.add_argument("playbook")
    pc.add_argument("-o", "--output", required=True, help="output .py path (use - for stdout)")
    pc.add_argument("-m", "--manifest", help="optional manifest .json path")
    pc.add_argument("--interpreter", default="/usr/bin/python3",
                    help="target python interpreter path (default: /usr/bin/python3)")
    pc.add_argument("--vault-password-file",
                    help="vault password file for decrypting baked group_vars "
                         "(default: $ANSIBLE_VAULT_PASSWORD_FILE or ansible.cfg)")
    pc.add_argument("--tree-shake", action="store_true",
                    help="remove branches made unreachable by compile-time extra vars")
    pc.add_argument("--freeze-playbook-vars", action="store_true",
                    help="freeze safely resolvable top-level play vars")
    pc.add_argument("--freeze-var", action="append", default=[], metavar="NAME",
                    help="freeze one named play/group variable (repeatable)")
    pc.add_argument("-e", "--extra-vars", action="append", default=[], metavar="VARS",
                    help="fixed extra vars, parsed like ansible-playbook -e: key=value, "
                         "'{key: value}' (JSON/YAML starting with { or [), or @file")

    pr = sub.add_parser("run", help="Compile and run a playbook locally")
    pr.add_argument("playbook")
    pr.add_argument("--interpreter", default=sys.executable)
    pr.add_argument("--keep", action="store_true", help="keep the compiled file")
    pr.add_argument("--vault-password-file",
                    help="vault password file for decrypting baked group_vars "
                         "(default: $ANSIBLE_VAULT_PASSWORD_FILE or ansible.cfg)")

    if argv[:1] == ["compile"]:
        parser.compile_parser = pc.compile_parser = pc
        pc.compile_operands = argv[1:]
    args = parser.parse_args(argv)

    if args.command == "compile":
        if args.output == "-" and _standalone:
            return _cmd_compile_stdout(args)
        if _stderr_may_be_playbook(args.playbook):
            return 2
        if args.output == "-":
            parser.error("use the ansiroll command for -o -, or compile_playbook() from Python")
        return _cmd_compile(args)
    if args.command == "run":
        return _cmd_run(args)
    parser.error("unknown command")


def _same_stream(first, second):
    return os.path.samestat(first, second) or (
        stat.S_ISCHR(first.st_mode) and stat.S_ISCHR(second.st_mode)
        and first.st_rdev == second.st_rdev
    )


def _discard_stream(stream):
    return stat.S_ISCHR(stream.st_mode) and _same_stream(stream, os.stat(os.devnull))


def _manifest_for_stdout(path, stdout_fd):
    if not path:
        return path
    try:
        manifest_stat = os.stat(path)
    except FileNotFoundError:
        return path
    return _validate_stdout_manifest(path, manifest_stat, stdout_fd)


def _linux_terminal_device(fd):
    import fcntl
    import struct

    # TIOCGDEV is _IOR('T', 0x32, unsigned int). Linux uses two read encodings.
    machine = os.uname().machine.lower()
    alternate = machine.startswith(("alpha", "hppa", "mips", "parisc", "powerpc", "ppc", "sparc"))
    request = 0x40045432 if alternate else 0x80045432
    try:
        return struct.unpack("I", fcntl.ioctl(fd, request, bytes(4)))[0]
    except OSError:
        return None


def _same_manifest_stream(path, info, fd, destination_fd=None):
    stream = os.fstat(fd)
    if not (sys.platform.startswith("linux") and stat.S_ISCHR(info.st_mode) and stat.S_ISCHR(stream.st_mode)):
        return _same_stream(info, stream)

    console_aliases = {os.makedev(4, 0), os.makedev(5, 1)}  # tty0, console
    if info.st_rdev in console_aliases or stream.st_rdev in console_aliases:
        # The Linux memory devices include null, zero and full.
        if 1 in (os.major(info.st_rdev), os.major(stream.st_rdev)):
            return False
        # TIOCGDEV reports the tty opened, which can differ from the device
        # receiving writes after a TIOCCONS redirection.
        raise ValueError("cannot verify redirected console for manifest path %s" % path)

    controlling_alias = os.makedev(5, 0)
    destination_alias = info.st_rdev == controlling_alias
    stream_alias = stream.st_rdev == controlling_alias
    if not (destination_alias or stream_alias):
        # Unix98 PTY slave numbers are allocated independently by devpts mounts.
        if info.st_rdev == stream.st_rdev and 136 <= os.major(info.st_rdev) <= 143:
            return os.path.samestat(info, stream)
        return _same_stream(info, stream)

    if 1 in (os.major(info.st_rdev), os.major(stream.st_rdev)):
        return False
    probe_error = "cannot verify terminal for manifest path %s" % path
    stream_device = _linux_terminal_device(fd) if stream_alias else stream.st_rdev
    if stream_device is None:
        raise ValueError(probe_error)
    if not destination_alias and info.st_rdev != stream_device:
        return False  # In particular, do not open an unrelated character device.

    def controls_terminal(terminal):
        try:
            os.tcgetpgrp(terminal)
        except OSError as ex:
            if ex.errno == errno.ENOTTY:
                return False
            raise ValueError(probe_error) from ex
        return True

    with ExitStack() as stack:
        if destination_fd is None:
            try:
                pinned_fd = os.open(path, os.O_PATH)
            except OSError as ex:
                raise ValueError(probe_error) from ex
            stack.callback(os.close, pinned_fd)
            pinned = os.fstat(pinned_fd)
            if not stat.S_ISCHR(pinned.st_mode) or not os.path.samestat(info, pinned):
                return False  # Final validation uses the actual writable descriptor.
            fd_path = "/proc/self/fd/%d" % pinned_fd
            try:
                destination_fd = os.open(fd_path, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
            except OSError as ex:
                raise ValueError(probe_error) from ex
            stack.callback(os.close, destination_fd)
            opened = os.fstat(destination_fd)
            if not os.path.samestat(info, opened) or not stat.S_ISCHR(opened.st_mode):
                return False  # The final validation checks the destination actually opened for writing.
        destination_device = _linux_terminal_device(destination_fd) if destination_alias else info.st_rdev
        if destination_device is None:
            raise ValueError(probe_error)
        if destination_device != stream_device:
            return False
        if not 136 <= os.major(destination_device) <= 143:
            return True
        # Only the controlling session can query a PTY slave's foreground
        # group. Use success, since the foreground group itself can change.
        destination_controlled = controls_terminal(destination_fd)
        stream_controlled = controls_terminal(fd)
        if not (destination_controlled or stream_controlled):
            raise ValueError("cannot distinguish terminal for manifest path %s from a redirected stream" % path)
        return destination_controlled and stream_controlled


def _validate_stdout_manifest(path, info, stdout_fd, destination_fd=None, *, closed_stdout=None):
    if closed_stdout is None:
        same_stdout = _same_manifest_stream(path, info, stdout_fd, destination_fd)
    else:
        same_stdout = _same_stream(info, closed_stdout)
    if same_stdout:
        stream = "stdout"
    elif _same_manifest_stream(path, info, 2, destination_fd):
        stream = "stderr"
    else:
        return path
    if _discard_stream(info):
        return os.devnull
    raise ValueError("manifest path points to %s: %s" % (stream, path))


class _TruncatingReopenError(OSError):
    pass


def _truncation_unavailable(path, original_error):
    return _TruncatingReopenError(
        original_error.errno,
        "safe truncating reopen requires /proc/self/fd. Compile to a local staging file and copy the output",
        path,
    )


def _pin_for_truncate(path, fd, original_error):
    if not hasattr(os, "O_PATH"):
        raise _truncation_unavailable(path, original_error) from original_error
    try:
        return os.open("/proc/self/fd/%d" % fd, os.O_PATH)
    except OSError as ex:
        raise _truncation_unavailable(path, original_error) from ex


def _open_truncated_destination(path, reference, original_error, validate, prepare=None, pin=None):
    fd_path = "/proc/self/fd/%d" % reference
    try:
        os.stat(fd_path)
    except OSError as ex:
        raise _truncation_unavailable(path, original_error) from ex
    # Following the procfs link can block on the destination filesystem.
    info = os.fstat(reference)
    if not stat.S_ISREG(info.st_mode):
        validate(info, None)
        raise original_error
    if prepare is not None:
        prepare(info, reference, pin)
    validate(info, None)
    return os.open(fd_path, os.O_WRONLY | os.O_TRUNC)


_TRUNCATING_RETRY_ERRORS = (errno.EPERM, errno.EINVAL, errno.EOPNOTSUPP)


def _open_manifest_fd(path, flags, validate, before_fifo):
    # A blocking open could wait on a replacement FIFO before validation
    # gets the chance to discover that it is the Python output.
    while True:
        try:
            return os.open(path, flags | os.O_NONBLOCK, 0o666)
        except OSError as ex:
            if ex.errno != errno.ENXIO:
                raise
            info = _try_stat(path)
            if info is None or not stat.S_ISFIFO(info.st_mode):
                raise
            validate(info, None)
            if before_fifo is not None:
                before_fifo()
            time.sleep(0.01)


@contextmanager
def _open_compile_destination(path, validate, *, release_output=None, prepare=None,
                              manifest=False, before_fifo=None):
    nonblocking_open = manifest and hasattr(os, "O_NONBLOCK")

    def open_destination(flags):
        if nonblocking_open:
            return _open_manifest_fd(path, flags, validate, before_fifo)
        return os.open(path, flags, 0o666)

    with ExitStack() as stack, ExitStack() as initial_pin:
        # A failed create-open gives us no fd. Capture the existing inode
        # before trying it so a retry cannot truncate a replacement file.
        reference = None
        if hasattr(os, "O_PATH"):
            try:
                reference = os.open(path, os.O_PATH)
            except OSError:
                pass
            else:
                initial_pin.callback(os.close, reference)

        flags = os.O_WRONLY | os.O_CREAT
        created = False
        released_for_emfile = False
        try:
            try:
                if reference is None:
                    try:
                        fd = open_destination(flags | os.O_EXCL)
                    except FileExistsError:
                        fd = open_destination(flags)
                    else:
                        created = True
                else:
                    fd = open_destination(flags)
            except OSError as ex:
                if ex.errno != errno.EMFILE or (reference is None and release_output is None):
                    raise
                if release_output is None:
                    initial_pin.close()
                    reference = None
                else:
                    # Retain the manifest pin, when available, for a safe
                    # truncating reopen after releasing the output pin.
                    if reference is not None:
                        validate(os.fstat(reference), None)
                    release_output()
                released_for_emfile = reference is None
                fd = open_destination(flags)
        except OSError as ex:
            # Preserve EACCES, including the kernel's protected_regular
            # check on the initial O_CREAT open in a sticky directory.
            if ex.errno not in _TRUNCATING_RETRY_ERRORS:
                raise
            if reference is None:
                # EPERM may be an ordinary permission denial.
                current = _try_stat(path) if ex.errno != errno.EPERM else None
                if current is not None and stat.S_ISREG(current.st_mode):
                    validate(current, None)
                    raise _truncation_unavailable(path, ex) from ex
                raise
            original_error = ex
            # A failed create-open hasn't established permission to replace
            # someone else's file.
            info = os.fstat(reference)
            if info.st_uid != os.geteuid():
                raise
            try:
                current = os.stat(path)
            except OSError:
                raise original_error
            if not os.path.samestat(info, current):
                validate(current, None)
                raise original_error
        else:
            initial_pin.close()
            reference = None
            writer = stack.enter_context(os.fdopen(fd, "w"))
            info = os.fstat(fd)
            validate(info, fd)
            if nonblocking_open:
                os.set_blocking(fd, True)
            if before_fifo is not None and stat.S_ISFIFO(info.st_mode):
                before_fifo()
                validate(info, fd)
            if prepare is not None:
                reference = prepare(info, fd)
                prepare = None
                info = os.fstat(fd)
                validate(info, fd)
            if created or not stat.S_ISREG(info.st_mode):
                yield writer
                return
            try:
                writer.truncate(0)
            except OSError as ex:
                if ex.errno != errno.ENOSYS and ex.errno not in _TRUNCATING_RETRY_ERRORS:
                    raise
                # An empty file can be written through the current handle
                # without truncating it or opening another descriptor.
                info = os.fstat(fd)
                if info.st_size == 0:
                    validate(info, fd)
                    yield writer
                    return
                if released_for_emfile:
                    raise
                original_error = ex
                if reference is None:
                    reference = _pin_for_truncate(path, fd, ex)
                    stack.callback(os.close, reference)
                writer.close()
            else:
                validate(os.fstat(fd), fd)
                yield writer
                return

        # Reopen the validated inode even if its pathname has since changed.
        fd = _open_truncated_destination(path, reference, original_error, validate, prepare, initial_pin)
        with os.fdopen(fd, "w") as writer:
            validate(os.fstat(fd), fd)
            yield writer


def _write_stdout_manifest(path, manifest, stdout_fd, *, validate_input=None, write_stdout=None):
    path = _manifest_for_stdout(path, stdout_fd)
    closed_stdout = None
    wrote_stdout = False

    def validate(info, fd):
        _validate_stdout_manifest(path, info, stdout_fd, fd, closed_stdout=closed_stdout)
        if validate_input is not None:
            validate_input(info, "manifest path", path)

    def before_fifo():
        nonlocal closed_stdout, wrote_stdout
        if not wrote_stdout:
            info = os.fstat(stdout_fd)
            # Keep a character device open for terminal alias validation.
            close_stdout = not stat.S_ISCHR(info.st_mode)
            write_stdout(close_stdout)
            if close_stdout:
                closed_stdout = info
            wrote_stdout = True

    with _open_compile_destination(path, validate, manifest=True,
                                   before_fifo=before_fifo if write_stdout is not None else None) as fh:
        json.dump(manifest, fh, indent=2)


def _stat_if_present(path):
    try:
        return os.stat(path)
    except FileNotFoundError:
        return


def _follow_destination_links(path):
    # Resolve leaf links without collapsing ".." before a parent symlink.
    for _ in range(40):
        if not os.path.islink(path):
            return path
        path = os.path.join(os.path.dirname(path), os.readlink(path))
    return None


def _missing_destination(path):
    path = _follow_destination_links(path)
    if path is None:
        return
    parent, name = os.path.split(path)
    parent = parent or os.curdir
    if name:
        directory = os.stat(parent)
        if stat.S_ISDIR(directory.st_mode):
            return directory.st_dev, directory.st_ino, name


def _check_file_destinations(output_path, manifest_path):
    try:
        output_stat = _stat_if_present(output_path)
        manifest_stat = _stat_if_present(manifest_path)
        if output_stat is None and manifest_stat is None:
            output = _missing_destination(output_path)
            if output is not None and output == _missing_destination(manifest_path):
                raise ValueError("manifest path points to compiled output: %s" % manifest_path)
    except OSError:
        # The normal write path reports destination errors in output order.
        return
    if output_stat is not None and manifest_stat is not None and _same_stream(output_stat, manifest_stat):
        raise ValueError("manifest path points to compiled output: %s" % manifest_path)


def _check_manifest_path(path, output_stat):
    try:
        manifest_stat = os.stat(path)
    except OSError:
        return
    if _same_stream(output_stat, manifest_stat):
        raise ValueError("manifest path points to compiled output: %s" % path)


def _pin_output_inode(path, output_fd, output_stat):
    candidates = []
    if sys.platform == "linux":
        linked_fd = "/proc/self/fd/%d" % output_fd
        if hasattr(os, "O_PATH"):
            # A read-open can wait on remote storage even with O_NONBLOCK.
            candidates.append((linked_fd, os.O_PATH))
        candidates.append((linked_fd, os.O_RDONLY | os.O_NONBLOCK))
    if hasattr(os, "O_PATH"):
        # A replacement may be a FIFO. Inspect it without opening a reader.
        candidates.append((path, os.O_PATH))

    for candidate, flags in candidates:
        try:
            reference = os.open(candidate, flags)
        except OSError:
            continue
        if os.path.samestat(output_stat, os.fstat(reference)):
            return reference
        os.close(reference)
    return None


def _make_output_executable(path, reference, output_stat, mode):
    if reference is not None and hasattr(os, "fchmod"):
        try:
            os.fchmod(reference, mode)
            return
        except OSError as ex:
            if ex.errno != errno.EBADF or not hasattr(os, "O_PATH"):
                raise
            # An O_PATH descriptor pins even a write-only file. Linux chmod
            # can follow its procfs link without reopening it for writing.
            try:
                os.chmod("/proc/self/fd/%d" % reference, mode)
            except OSError as ex:
                if ex.errno not in (errno.ENOENT, errno.ENOTDIR, errno.EACCES, errno.EPERM, errno.ELOOP):
                    raise
            else:
                return
    if not os.path.samestat(output_stat, os.stat(path)):
        raise OSError(errno.ESTALE, "output path changed before setting permissions", path)
    os.chmod(path, mode)


def _private_output_path(path):
    target = _follow_destination_links(path)
    if target is None:
        raise ValueError("too many symlinks in Vault-bearing output path: %s" % path)
    parent, name = os.path.split(target)
    parent = parent or os.curdir
    # Require a real parent at the requested path before canonicalizing it.
    if name in ("", ".", "..") or not os.path.isdir(parent):
        raise ValueError("Vault-bearing output needs a regular file in an existing directory: %s" % path)
    directory = os.path.realpath(parent)
    if not os.path.samefile(parent, directory):
        raise ValueError("Vault-bearing output directory changed: %s" % path)
    return os.path.join(directory, name)


@contextmanager
def _open_private_compile_destination(path, validate, *, prepare):
    # A reader that already opened the previous output retains access to
    # that inode even after chmod. Publish the new source on a private inode.
    destination = _private_output_path(path)
    directory = os.path.dirname(destination)

    def destination_stat():
        try:
            return os.lstat(destination)
        except FileNotFoundError:
            return None

    fd, temp_path = tempfile.mkstemp(prefix=".ansiroll-vault-", dir=directory)
    try:
        with os.fdopen(fd, "w") as writer, ExitStack() as existing_ref:
            yield writer
            writer.flush()
            os.fchmod(fd, 0o700)
            existing = destination_stat()
            if existing is not None:
                validate(existing, None)
                if not stat.S_ISREG(existing.st_mode):
                    raise ValueError("Vault-bearing output needs a regular destination file: %s" % path)
                existing_fd = os.open(destination, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                existing_ref.callback(os.close, existing_fd)
                if not os.path.samestat(existing, os.fstat(existing_fd)):
                    raise OSError(errno.ESTALE, "Vault-bearing output changed before access check", path)
            info = os.fstat(fd)
            validate(info, fd)
            if _private_output_path(path) != destination:
                raise ValueError("Vault-bearing output path changed: %s" % path)
            current = destination_stat()
            if ((existing is None) != (current is None)
                    or (existing is not None and not os.path.samestat(existing, current))):
                raise OSError(errno.ESTALE, "Vault-bearing output changed before replacement", path)
            os.replace(temp_path, destination)
            prepare(info, fd)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def _write_compile_files(output_path, manifest_path, source, manifest, validate_input):
    # Paths can change during compilation or between the check and open.
    if manifest_path:
        _check_file_destinations(output_path, manifest_path)

    def validate_output(info, _fd):
        # The manifest lookup may block while the input pathname changes.
        if manifest_path:
            _check_manifest_path(manifest_path, info)
        validate_input(info, "output path", output_path)

    with ExitStack() as output_reference:
        reference = None
        output_stat = None

        def prepare_output(info, fd, pin=None):
            nonlocal reference, output_stat
            output_stat = info
            if pin is not None:
                reference = fd
                output_reference.callback(pin.pop_all().close)
            elif stat.S_ISREG(output_stat.st_mode):
                reference = _pin_output_inode(output_path, fd, output_stat)
                if reference is not None:
                    output_reference.callback(os.close, reference)
            return reference

        sensitive = manifest.get("contains_vault_plaintext", False)
        open_output = _open_private_compile_destination if sensitive else _open_compile_destination
        with open_output(output_path, validate_output, prepare=prepare_output) as output:
            output.write(source)
            output.flush()
            mode = os.fstat(output.fileno()).st_mode | stat.S_IEXEC | stat.S_IXGRP
        if not sensitive:
            _make_output_executable(output_path, reference, output_stat, mode)
        if not manifest_path:
            return

        def validate_manifest(info, _fd):
            try:
                current_output = os.stat(output_path)
            except OSError:
                current_output = None
            if (_same_stream(output_stat, info)
                    or current_output is not None and _same_stream(current_output, info)):
                raise ValueError("manifest path points to compiled output: %s" % manifest_path)
            validate_input(info, "manifest path", manifest_path)

        # Reopen the current pathname after Python output succeeds.
        _check_file_destinations(output_path, manifest_path)
        release_output = output_reference.close if reference is not None else None
        with _open_compile_destination(
            manifest_path, validate_manifest, release_output=release_output, manifest=True,
        ) as manifest_file:
            json.dump(manifest, manifest_file, indent=2)


def _diagnostic_stream_may_be_playbook(stream, playbook):
    if not (stat.S_ISREG(stream.st_mode) or stat.S_ISFIFO(stream.st_mode)):
        return False
    path = os.path.abspath(playbook)
    directory, name = os.path.split(playbook)
    if not path.isascii() or "$" in playbook or "$" in path or name.startswith(("~", "'", "\"")):
        # Ansible can emit diagnostics during import. Keep them off this
        # stream until the input's identity is known.
        try:
            with open(os.devnull, "w") as sink, redirect_stderr(sink), redirect_stdout(sink):
                from ansible.parsing.quoting import unquote
                from ansible.utils.path import unfrackpath

                from .loader import _core_version, _playbook_read_path

                # Mirror the loader's path resolution without loading its
                # configuration, whose errors still need to reach safe stderr.
                if not os.path.isabs(playbook) or _core_version() >= (2, 20):
                    directory = os.path.abspath(directory)
                name = unquote(name)
                given = name if name.startswith((os.sep, "~")) else os.path.join(directory, name)
                resolved = unfrackpath(given, follow=False)
                path = _playbook_read_path(unfrackpath(resolved, follow=False))
        except (Exception, SystemExit):
            return True
    try:
        source = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except (OSError, UnicodeError):
        # The open stream may still reach the input even if its path cannot
        # be inspected. Keep diagnostics off it unless we can rule out an alias.
        return True
    return os.path.samestat(stream, source)


def _fd_is_read_only(fd):
    if os.name != "posix":
        return False
    from fcntl import F_GETFL, fcntl

    return (fcntl(fd, F_GETFL) & os.O_ACCMODE) == os.O_RDONLY


def _stream_fd(stream):
    try:
        return stream.fileno()
    except (AttributeError, OSError, ValueError):
        pass  # In-memory streams and closed streams have no descriptor.


def _writable_stream_stat(fd):
    if fd is not None:
        try:
            if not _fd_is_read_only(fd):
                return os.fstat(fd)
        except OSError as ex:
            if ex.errno != errno.EBADF:
                raise


def _diagnostics_may_be_playbook(playbook, fd, stream):
    for descriptor in {fd, _stream_fd(stream)}:
        info = _writable_stream_stat(descriptor)
        if info is not None and _diagnostic_stream_may_be_playbook(info, playbook):
            return True
    return False


def _stderr_may_be_playbook(playbook):
    return _diagnostics_may_be_playbook(playbook, 2, sys.stderr)


def _reserve_closed_standard_fds(stack):
    # Keep saved diagnostic handles and spools out of stdin/stdout/stderr.
    for fd in (0, 1, 2):
        try:
            os.fstat(fd)
        except OSError as ex:
            if ex.errno != errno.EBADF:
                raise
            sink = os.open(os.devnull, os.O_RDWR)
            if sink != fd:
                try:
                    os.dup2(sink, fd, inheritable=False)
                finally:
                    os.close(sink)
            stack.callback(os.close, fd)


@contextmanager
def _hold_compile_diagnostics(input_alias):
    # Ansible may emit warnings before it opens even the first playbook.
    # Delay file and pipe diagnostics until their destination can be compared
    # with every input opened during compilation.
    streams = (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)
    descriptors = {1, 2, *map(_stream_fd, streams)}
    redirected = set()
    pending = []
    original_streams = {}
    destinations = {}
    for fd in sorted(descriptors - {None}):
        info = _writable_stream_stat(fd)
        if info is not None and (stat.S_ISREG(info.st_mode) or stat.S_ISFIFO(info.st_mode)):
            destinations[fd] = info
    with ExitStack() as buffers, ExitStack() as redirects:
        if destinations:
            _reserve_closed_standard_fds(buffers)
        for fd, info in destinations.items():
            saved = os.dup(fd)
            buffers.callback(os.close, saved)
            spool = buffers.enter_context(tempfile.TemporaryFile())
            pending.append((info, spool, saved))
            spool_info = os.fstat(spool.fileno())
            original_streams[(spool_info.st_dev, spool_info.st_ino)] = info
            inheritable = os.get_inheritable(fd)
            os.dup2(spool.fileno(), fd, inheritable=inheritable)
            redirects.callback(os.dup2, saved, fd, inheritable=inheritable)
            redirected.add(fd)
        try:
            yield original_streams
        finally:
            try:
                if redirected:
                    for stream in streams:
                        if _stream_fd(stream) in redirected:
                            stream.flush()
                    if os.name == "posix":
                        # Native plugins may also buffer writes to stdout.
                        import ctypes

                        ctypes.CDLL(None).fflush(None)
            finally:
                redirects.close()
            for info, spool, saved in pending:
                if spool.seek(0, os.SEEK_END) and not input_alias(info):
                    spool.seek(0)
                    with open(saved, "wb", closefd=False) as output:
                        shutil.copyfileobj(spool, output)


def _cmd_compile_stdout(args):
    try:
        stdout_stat = os.fstat(1)
        _prepare_compile_stderr()
        stderr_stat = os.fstat(2)
        if _stderr_may_be_playbook(args.playbook):
            return 2
        if os.isatty(1):
            raise ValueError("compile -o - requires redirected output: redirect stdout to a pipe or file")
        if _same_stream(stdout_stat, stderr_stat) and not _discard_stream(stdout_stat):
            raise ValueError("compile -o - requires separate stdout and stderr streams")
        args.manifest = _manifest_for_stdout(args.manifest, 1)
        with os.fdopen(os.dup(1), "wb") as stdout:
            # Keep inherited stdout and native buffers on stderr through CLI exit.
            os.dup2(2, 1)
            sys.stdout.flush()
            with redirect_stdout(sys.stderr):
                return _cmd_compile(args, stdout)
    except ValueError as ex:
        sys.stderr.write("ansiroll: %s\n" % ex)
        return 2


def _prepare_compile_stderr():
    try:
        os.fstat(2)
        if not _fd_is_read_only(2):
            return
    except OSError as ex:
        if ex.errno != errno.EBADF:
            raise
    # A closed stderr can be reused by interpreter startup for a read-only file.
    sink = os.open(os.devnull, os.O_WRONLY)
    if sink == 2:
        os.set_inheritable(2, True)
    else:
        try:
            os.dup2(sink, 2)
        finally:
            os.close(sink)
    if sys.stderr is None:
        sys.stderr = open(2, "w", closefd=False, errors="backslashreplace")


def _try_stat(path):
    try:
        return os.stat(path)
    except (OSError, UnicodeError):
        # Let the compiler or destination open report inaccessible paths.
        return None


def _cmd_compile(args, stdout=None):
    input_paths = {}
    input_inodes = set()

    def input_alias(info, *, check_paths=True):
        if (info.st_dev, info.st_ino) in input_inodes:
            return True
        if not check_paths:
            return False
        # Recheck the entry playbook last, immediately before writing.
        for input_path in reversed(input_paths):
            try:
                current_input = os.stat(input_path)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except (OSError, UnicodeError) as ex:
                raise ValueError("cannot check current input playbook: %s" % ex) from ex
            if os.path.samestat(info, current_input):
                return True
        return False

    def suppress_diagnostics(info):
        if info is None or not (stat.S_ISREG(info.st_mode) or stat.S_ISFIFO(info.st_mode)):
            return False
        try:
            if input_paths:
                return input_alias(info)
            return _diagnostic_stream_may_be_playbook(info, args.playbook)
        except ValueError:
            return True

    def stderr_is_input():
        return suppress_diagnostics(_writable_stream_stat(_stream_fd(sys.stderr)))

    def report(message):
        if not stderr_is_input():
            sys.stderr.write(message)

    def validate_input(info, name, path, *, check_paths=True):
        if info is not None and input_alias(info, check_paths=check_paths):
            raise ValueError("%s points to input playbook: %s" % (name, path))

    def check_input_destinations(*, check_paths=True):
        if stdout is None:
            validate_input(_try_stat(args.output), "output path", args.output, check_paths=check_paths)
        else:
            validate_input(os.fstat(stdout.fileno()), "stdout", args.playbook, check_paths=check_paths)
        if args.manifest:
            validate_input(_try_stat(args.manifest), "manifest path", args.manifest, check_paths=check_paths)

    def on_playbook_open(path, info):
        input_paths[path] = None
        # A descriptor path can reach a redirected diagnostic buffer.
        # Protect the original destination and refuse to parse that buffer.
        diagnostic = original_streams.get((info.st_dev, info.st_ino))
        if diagnostic is not None:
            info = diagnostic
        input_inodes.add((info.st_dev, info.st_ino))
        # A saved diagnostic writer also keeps a matching FIFO from reaching EOF.
        if diagnostic is not None or (
            stat.S_ISFIFO(info.st_mode) and any(os.path.samestat(info, stream) for stream in original_streams.values())
        ):
            raise ValueError("input playbook points to a diagnostic stream: %s" % os.fsdecode(path))
        check_input_destinations(check_paths=False)

    try:
        if stdout is None:
            if _diagnostics_may_be_playbook(args.playbook, 1, sys.stdout):
                raise ValueError("stdout points to input playbook: %s" % args.playbook)
            if args.manifest:
                _check_file_destinations(args.output, args.manifest)
    except ValueError as ex:
        report("ansiroll: %s\n" % ex)
        return 2
    try:
        with _hold_compile_diagnostics(suppress_diagnostics) as original_streams:
            try:
                extra_vars = _load_extra_vars(args.extra_vars, args.vault_password_file)
            except Exception as ex:
                sys.stderr.write("ansiroll: invalid extra vars: %s\n" % ex)
                return 2
            try:
                source, manifest = compile_playbook(args.playbook, python_interpreter=args.interpreter,
                                                    vault_password_file=args.vault_password_file,
                                                    extra_vars=extra_vars,
                                                    tree_shake=args.tree_shake,
                                                    freeze_playbook_vars=args.freeze_playbook_vars,
                                                    freeze_vars=args.freeze_var,
                                                    on_playbook_open=on_playbook_open)
            except Exception as ex:
                if not isinstance(ex, (ValueError,) + _ansible_errors()):
                    raise
                message = str(ex)
                if not message.startswith("ansiroll: "):
                    message = "ansiroll: " + message
                sys.stderr.write(message + "\n")
                return 2
    except SystemExit as ex:
        if ex.code is not None and not isinstance(ex.code, int) and stderr_is_input():
            raise SystemExit(1) from None
        raise
    except (Exception, KeyboardInterrupt) as ex:
        if stderr_is_input():
            return 130 if isinstance(ex, KeyboardInterrupt) else 2
        raise
    try:
        check_input_destinations()
        if stdout is None:
            _write_compile_files(args.output, args.manifest, source, manifest, validate_input)
        else:
            encoded = source.encode("utf-8")
            wrote_stdout = False

            def write_stdout(close=False):
                nonlocal wrote_stdout
                validate_input(os.fstat(stdout.fileno()), "stdout", args.playbook)
                stdout.write(encoded)
                stdout.flush()
                if close:
                    stdout.close()
                wrote_stdout = True

            if args.manifest:
                _write_stdout_manifest(args.manifest, manifest, stdout.fileno(),
                                       validate_input=validate_input, write_stdout=write_stdout)
            if not wrote_stdout:
                write_stdout()
    except (Exception, KeyboardInterrupt) as ex:
        if isinstance(ex, (ValueError, _TruncatingReopenError)):
            report("ansiroll: %s\n" % ex)
        elif not stderr_is_input():
            raise
        return 130 if isinstance(ex, KeyboardInterrupt) else 2
    report(
        "ansiroll: compiled %s -> %s (%d modules embedded, %d fallback tasks)\n"
        % (args.playbook, "stdout" if stdout is not None else args.output, len(manifest["modules"]),
           len(manifest["fallback_tasks"]))
    )
    return 0


def _ansible_errors():
    try:
        from ansible.errors import AnsibleError
    except ImportError:
        return ()
    return (AnsibleError,)


def _load_extra_vars(specs, vault_password_file=None):
    """Parse -e values exactly like ansible.utils.vars.load_extra_vars."""
    if not specs:
        return {}
    from ansible.parsing.dataloader import DataLoader
    from ansible.parsing.splitter import parse_kv
    from ansible.utils.vars import combine_vars
    from .loader import _vault_secrets

    loader = DataLoader()
    secrets = _vault_secrets(vault_password_file)
    if secrets:
        loader.set_vault_secrets(secrets)
    merged = {}
    for spec in specs:
        if not spec:
            continue
        if spec.startswith("@"):
            # Argument is a YAML file (JSON is a subset of YAML)
            try:
                value = loader.load_from_file(spec[1:], trusted_as_template=True)
            except TypeError:  # older ansible-core without the trust kwarg
                value = loader.load_from_file(spec[1:])
        elif spec[0] in ("/", "."):
            raise ValueError("Please prepend extra_vars filename '%s' with '@'" % spec)
        elif spec[0] in ("[", "{"):
            value = loader.load(spec)
        else:
            value = parse_kv(spec)
        if not isinstance(value, dict):
            raise ValueError("Invalid extra vars data supplied. '%s' could not be "
                             "made into a dictionary" % spec)
        merged = combine_vars(merged, value)
    return merged


def _cmd_run(args):
    source, _ = compile_playbook(args.playbook, python_interpreter=args.interpreter,
                                 vault_password_file=args.vault_password_file)
    if not args.keep:
        proc = subprocess.run([args.interpreter, "-"], input=source,
                              universal_newlines=True)
        return proc.returncode

    fd, path = tempfile.mkstemp(suffix=".py", prefix="ansiroll_play_")
    with os.fdopen(fd, "w") as fh:
        fh.write(source)
    proc = subprocess.run([args.interpreter, path])
    sys.stderr.write("ansiroll: kept compiled file at %s\n" % path)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(entrypoint())
