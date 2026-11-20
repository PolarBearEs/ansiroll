"""Command-line interface: ansiroll compile|run."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
from threading import RLock

from .compiler import compile_playbook

_main_lock = RLock()


def main(argv=None):
    # Callable invocations share the process's stdout stream and descriptor.
    with _main_lock:
        return _main(argv, restore_stdout=True)


def entrypoint():
    return _main(None, restore_stdout=False)


def _main(argv, *, restore_stdout):
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
            return _cmd_compile_stdout(args, restore_stdout=restore_stdout)
        return _cmd_compile(args)
    if args.command == "run":
        return _cmd_run(args)
    parser.error("unknown command")


@contextmanager
def _compile_diagnostics_to_stderr(restore_stdout):
    # Redirect inherited stdout for helpers as well as Python's stdout stream.
    stdout = sys.stdout
    stdout.flush()
    original_stdout = sys.__stdout__
    if original_stdout is not None and original_stdout is not stdout:
        original_stdout.flush()
    if restore_stdout:
        from ctypes import CDLL

        stdout_inheritable = os.get_inheritable(1)
        # Drain the caller's existing C stdio output before redirecting fd 1.
        flush_native = CDLL(None).fflush
        flush_native(None)
    with os.fdopen(os.dup(1), "wb") as saved_stdout:
        os.dup2(2, 1)
        try:
            with redirect_stdout(sys.stderr):
                yield saved_stdout
        finally:
            # The standalone CLI keeps fd 1 redirected through process shutdown
            # so native buffers and exit handlers also write their diagnostics to stderr.
            if restore_stdout:
                try:
                    stdout.flush()
                    if original_stdout is not None and original_stdout is not stdout:
                        original_stdout.flush()
                    flush_native(None)
                finally:
                    os.dup2(saved_stdout.fileno(), 1, inheritable=stdout_inheritable)


def _stdout_manifest_path(path, stdout_fd):
    try:
        manifest_stat = os.stat(path)
    except FileNotFoundError:
        return path
    if not os.path.samestat(manifest_stat, os.fstat(stdout_fd)):
        return path
    if os.path.samefile(path, os.devnull):
        # A path such as /dev/stdout would point to stderr after redirection.
        return os.devnull
    sys.stderr.write("ansiroll: manifest path points to stdout: %s\n" % path)
    return None


def _cmd_compile_stdout(args, restore_stdout):
    stdout = sys.stdout
    stdout_fd = 1
    if restore_stdout:
        try:
            stdout_fd = stdout.fileno()
        except (AttributeError, OSError, ValueError):
            stdout_fd = None
    if args.manifest and stdout_fd is not None:
        manifest_path = _stdout_manifest_path(args.manifest, stdout_fd)
        if manifest_path is None:
            return 2
        args.manifest = manifest_path
    with _compile_diagnostics_to_stderr(restore_stdout) as saved_stdout:
        if stdout_fd != 1:
            return _cmd_compile(args, write_source=stdout.write, stdout_fd=stdout_fd)

        def write_source(source):
            saved_stdout.write(source.encode("utf-8"))

        return _cmd_compile(args, write_source=write_source, stdout_fd=saved_stdout.fileno())


def _cmd_compile(args, write_source=None, stdout_fd=None):
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
    if write_source is None:
        with open(args.output, "w") as fh:
            fh.write(source)
        os.chmod(args.output, os.stat(args.output).st_mode | stat.S_IEXEC | stat.S_IXGRP)
    if args.manifest:
        manifest_path = args.manifest
        if stdout_fd is not None:
            # Duplicating stdout may have made a /dev/fd path resolve to it.
            manifest_path = _stdout_manifest_path(manifest_path, stdout_fd)
            if manifest_path is None:
                return 2
        with open(manifest_path, "w") as fh:
            json.dump(manifest, fh, indent=2)
    if write_source is not None:
        write_source(source)
    sys.stderr.write(
        "ansiroll: compiled %s -> %s (%d modules embedded, %d fallback tasks)\n"
        % (args.playbook, "stdout" if write_source is not None else args.output, len(manifest["modules"]),
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
