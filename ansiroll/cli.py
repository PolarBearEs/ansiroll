"""Command-line interface: ansiroll compile|run."""

from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import ExitStack, contextmanager, redirect_stdout

from .compiler import compile_playbook


def entrypoint():
    return main(_standalone=True)


def main(argv=None, *, _standalone=False):
    parser = argparse.ArgumentParser(prog="ansiroll", description=__doc__)
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

    args = parser.parse_args(argv)

    if args.command == "compile":
        if args.output == "-":
            if not _standalone:
                parser.error("use the ansiroll command for -o -, or compile_playbook() from Python")
            return _cmd_compile_stdout(args)
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
    if not _same_stream(manifest_stat, os.fstat(stdout_fd)):
        return path
    if _discard_stream(manifest_stat):
        return os.devnull
    raise ValueError("manifest path points to stdout: %s" % path)


def _truncation_unavailable(path, original_error):
    return OSError(original_error.errno, "safe truncating reopen requires /proc/self/fd", path)


def _pin_for_truncate(path, target, original_error):
    if not hasattr(os, "O_PATH"):
        raise _truncation_unavailable(path, original_error) from original_error
    try:
        return os.open(target, os.O_PATH)
    except OSError as ex:
        if target != path:
            raise _truncation_unavailable(path, original_error) from ex
        raise original_error from ex


def _open_truncated_destination(path, reference, original_error):
    fd_path = "/proc/self/fd/%d" % reference
    try:
        os.stat(fd_path)
    except OSError as ex:
        raise _truncation_unavailable(path, original_error) from ex
    return os.open(fd_path, os.O_WRONLY | os.O_TRUNC)


_TRUNCATING_RETRY_ERRORS = (errno.EPERM, errno.EINVAL, errno.EOPNOTSUPP)


@contextmanager
def _open_compile_destination(path, validate):
    with ExitStack() as stack:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o666)
        except OSError as ex:
            # Preserve EACCES, including the kernel's protected_regular
            # check on the initial O_CREAT open in a sticky directory.
            if ex.errno not in _TRUNCATING_RETRY_ERRORS:
                raise
            original_error = ex
            reference = _pin_for_truncate(path, path, ex)
            stack.callback(os.close, reference)
        else:
            writer = stack.enter_context(os.fdopen(fd, "w"))
            info = os.fstat(fd)
            validate(info)
            if not stat.S_ISREG(info.st_mode):
                yield writer
                return
            try:
                writer.truncate(0)
            except OSError as ex:
                if ex.errno != errno.ENOSYS and ex.errno not in _TRUNCATING_RETRY_ERRORS:
                    raise
                original_error = ex
                reference = _pin_for_truncate(path, "/proc/self/fd/%d" % fd, ex)
                stack.callback(os.close, reference)
                writer.close()
            else:
                yield writer
                return

        # Reopen the validated inode even if its pathname has since changed.
        info = os.fstat(reference)
        validate(info)
        if not stat.S_ISREG(info.st_mode):
            raise original_error
        fd = _open_truncated_destination(path, reference, original_error)
        with os.fdopen(fd, "w") as writer:
            yield writer


def _write_stdout_manifest(path, manifest, stdout_fd):
    path = _manifest_for_stdout(path, stdout_fd)

    def validate(info):
        if _same_stream(info, os.fstat(stdout_fd)) and not _discard_stream(info):
            raise ValueError("manifest path points to stdout: %s" % path)

    with _open_compile_destination(path, validate) as fh:
        json.dump(manifest, fh, indent=2)


def _stat_if_present(path):
    try:
        return os.stat(path)
    except FileNotFoundError:
        return


def _missing_destination(path):
    # Follow the final symlink without collapsing an unresolved component.
    for _ in range(40):
        if not os.path.islink(path):
            break
        path = os.path.join(os.path.dirname(path), os.readlink(path))
    else:
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
        if hasattr(os, "O_PATH"):
            # Metadata-only handles don't connect to FIFO readers, even if
            # the destination changes while we're checking it.
            fd = os.open(path, os.O_PATH)
            try:
                manifest_stat = os.fstat(fd)
            finally:
                os.close(fd)
        else:
            manifest_stat = os.stat(path)
    except OSError:
        return
    if _same_stream(output_stat, manifest_stat):
        raise ValueError("manifest path points to compiled output: %s" % path)


def _pin_output_inode(path, output_fd, output_stat):
    flags = getattr(os, "O_NONBLOCK", 0)
    if sys.platform == "linux":
        paths = ("/proc/self/fd/%d" % output_fd, path)
    else:
        paths = (path,)
    modes = (os.O_RDONLY, os.O_PATH) if hasattr(os, "O_PATH") else (os.O_RDONLY,)
    for candidate in paths:
        for mode in modes:
            try:
                reference = os.open(candidate, flags | mode)
            except OSError:
                continue
            if os.path.samestat(output_stat, os.fstat(reference)):
                return reference
            os.close(reference)
            break
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
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                pass
            else:
                return
    if not os.path.samestat(output_stat, os.stat(path)):
        raise OSError(errno.ESTALE, "output path changed before setting permissions", path)
    os.chmod(path, mode)


def _write_compile_files(output_path, manifest_path, source, manifest):
    if not manifest_path:
        with open(output_path, "w") as output:
            output.write(source)
        os.chmod(output_path, os.stat(output_path).st_mode | stat.S_IEXEC | stat.S_IXGRP)
        return

    # Paths can change during compilation or between the check and open.
    _check_file_destinations(output_path, manifest_path)

    def validate_output(info):
        _check_manifest_path(manifest_path, info)

    with ExitStack() as output_reference:
        reference = None
        with _open_compile_destination(output_path, validate_output) as output:
            output_stat = os.fstat(output.fileno())
            if stat.S_ISREG(output_stat.st_mode):
                reference = _pin_output_inode(output_path, output.fileno(), output_stat)
                if reference is not None:
                    output_reference.callback(os.close, reference)
            output.write(source)
            output.flush()
            mode = os.fstat(output.fileno()).st_mode | stat.S_IEXEC | stat.S_IXGRP
        _make_output_executable(output_path, reference, output_stat, mode)

        def validate_manifest(info):
            try:
                current_output = os.stat(output_path)
            except OSError:
                current_output = None
            if (_same_stream(output_stat, info)
                    or current_output is not None and _same_stream(current_output, info)):
                raise ValueError("manifest path points to compiled output: %s" % manifest_path)

        # Reopen the current pathname after Python output succeeds.
        _check_file_destinations(output_path, manifest_path)
        with _open_compile_destination(manifest_path, validate_manifest) as manifest_file:
            json.dump(manifest, manifest_file, indent=2)


def _cmd_compile_stdout(args):
    try:
        stdout_stat = os.fstat(1)
        _prepare_compile_stderr()
        if os.isatty(1):
            raise ValueError("compile -o - requires redirected output: redirect stdout to a pipe or file")
        if _same_stream(stdout_stat, os.fstat(2)) and not _discard_stream(stdout_stat):
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
        if os.name != "posix":
            return
        from fcntl import F_GETFL, fcntl

        if (fcntl(2, F_GETFL) & os.O_ACCMODE) != os.O_RDONLY:
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


def _cmd_compile(args, stdout=None):
    if stdout is None and args.manifest:
        try:
            _check_file_destinations(args.output, args.manifest)
        except ValueError as ex:
            sys.stderr.write("ansiroll: %s\n" % ex)
            return 2
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
                                            freeze_vars=args.freeze_var)
    except Exception as ex:
        if not isinstance(ex, (ValueError,) + _ansible_errors()):
            raise
        message = str(ex)
        if not message.startswith("ansiroll: "):
            message = "ansiroll: " + message
        sys.stderr.write(message + "\n")
        return 2
    if stdout is None:
        try:
            _write_compile_files(args.output, args.manifest, source, manifest)
        except ValueError as ex:
            sys.stderr.write("ansiroll: %s\n" % ex)
            return 2
    else:
        if args.manifest:
            _write_stdout_manifest(args.manifest, manifest, stdout.fileno())
        stdout.write(source.encode("utf-8"))
        stdout.flush()
    sys.stderr.write(
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
