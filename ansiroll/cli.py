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
from contextlib import redirect_stdout

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


def _manifest_for_stdout(path, stdout_fd):
    if not path:
        return path
    try:
        aliases_stdout = os.path.samestat(os.stat(path), os.fstat(stdout_fd))
    except FileNotFoundError:
        return path
    if not aliases_stdout:
        return path
    if os.path.samefile(path, os.devnull):
        return os.devnull
    raise ValueError("manifest path points to stdout: %s" % path)


def _cmd_compile_stdout(args):
    try:
        stdout_stat = os.fstat(1)
        stderr_stat = _compile_stderr_stat()
        if ((stat.S_ISFIFO(stdout_stat.st_mode) or stat.S_ISREG(stdout_stat.st_mode)
             or stat.S_ISSOCK(stdout_stat.st_mode) or os.isatty(1))
                and os.path.samestat(stdout_stat, stderr_stat)):
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


def _compile_stderr_stat():
    try:
        stderr_stat = os.fstat(2)
        if os.name != "posix":
            return stderr_stat
        from fcntl import F_GETFL, fcntl

        if (fcntl(2, F_GETFL) & os.O_ACCMODE) != os.O_RDONLY:
            return stderr_stat
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
    return os.fstat(2)


def _cmd_compile(args, stdout=None):
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
        with open(args.output, "w") as fh:
            fh.write(source)
        os.chmod(args.output, os.stat(args.output).st_mode | stat.S_IEXEC | stat.S_IXGRP)
    if args.manifest:
        manifest_path = _manifest_for_stdout(args.manifest, stdout.fileno()) if stdout else args.manifest
        with open(manifest_path, "w") as fh:
            json.dump(manifest, fh, indent=2)
    if stdout is not None:
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
