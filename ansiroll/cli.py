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
from contextlib import ExitStack, redirect_stdout

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


def _write_stdout_manifest(path, manifest, stdout_fd):
    path = _manifest_for_stdout(path, stdout_fd)
    # Validate the actual destination before truncating or writing anything.
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT, 0o666), "w") as fh:
        manifest_stat = os.fstat(fh.fileno())
        if _same_stream(manifest_stat, os.fstat(stdout_fd)) and not _discard_stream(manifest_stat):
            raise ValueError("manifest path points to stdout: %s" % path)
        if stat.S_ISREG(manifest_stat.st_mode):
            fh.truncate(0)
        json.dump(manifest, fh, indent=2)


def _check_file_destinations(output_path, manifest_path):
    message = "manifest path points to compiled output: %s" % manifest_path
    if os.path.normcase(os.path.realpath(output_path)) == os.path.normcase(os.path.realpath(manifest_path)):
        raise ValueError(message)
    try:
        manifest_stat = os.stat(manifest_path)
    except FileNotFoundError:
        return
    try:
        output_stat = os.stat(output_path)
    except FileNotFoundError:
        return manifest_stat
    if _same_stream(output_stat, manifest_stat):
        raise ValueError(message)
    return manifest_stat


def _open_file_manifest(stack, path, output_stat):
    stream = stack.enter_context(os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT, 0o666), "w"))
    if _same_stream(output_stat, os.fstat(stream.fileno())):
        raise ValueError("manifest path points to compiled output: %s" % path)
    return stream


def _write_compile_files(output_path, manifest_path, source, manifest):
    if not manifest_path:
        with open(output_path, "w") as output:
            output.write(source)
        os.chmod(output_path, os.stat(output_path).st_mode | stat.S_IEXEC | stat.S_IXGRP)
        return

    # Paths can change during compilation or between the check and open.
    manifest_stat = _check_file_destinations(output_path, manifest_path)
    with ExitStack() as stack:
        with os.fdopen(os.open(output_path, os.O_WRONLY | os.O_CREAT, 0o666), "w") as output:
            output_stat = os.fstat(output.fileno())
            # A FIFO consumer may wait for the Python output before opening the manifest.
            sequential = stat.S_ISFIFO(output_stat.st_mode) or (
                manifest_stat is not None and stat.S_ISFIFO(manifest_stat.st_mode)
            )
            if not sequential:
                manifest_file = _open_file_manifest(stack, manifest_path, output_stat)
            if stat.S_ISREG(output_stat.st_mode):
                output.truncate(0)
            output.write(source)
            output.flush()
            mode = os.fstat(output.fileno()).st_mode | stat.S_IEXEC | stat.S_IXGRP
            if hasattr(os, "fchmod"):
                os.fchmod(output.fileno(), mode)
            else:
                os.chmod(output_path, mode)
        if sequential:
            manifest_file = _open_file_manifest(stack, manifest_path, output_stat)
        if stat.S_ISREG(os.fstat(manifest_file.fileno()).st_mode):
            manifest_file.truncate(0)
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
