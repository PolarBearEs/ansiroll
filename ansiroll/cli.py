"""Command-line interface: ansiroll compile|run."""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout

from .compiler import compile_playbook


def main(argv=None):
    return _main(argv, callable_stdout=True)


def entrypoint():
    return _main(None, callable_stdout=False)


def _main(argv, *, callable_stdout):
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
            if callable_stdout:
                return _cmd_compile_stdout_in_child(args)
            return _cmd_compile_stdout(args)
        return _cmd_compile(args)
    if args.command == "run":
        return _cmd_run(args)
    parser.error("unknown command")


@contextmanager
def _compile_diagnostics_to_stderr():
    # Only used in processes dedicated to compilation. Keep fd 1 redirected
    # through shutdown so native buffers and exit handlers also go to stderr.
    with os.fdopen(os.dup(1), "wb") as saved_stdout:
        os.dup2(2, 1)
        sys.stdout.flush()
        with redirect_stdout(sys.stderr):
            yield saved_stdout


def _stdout_manifest_path(path, *stdout_fds):
    try:
        manifest_stat = os.stat(path)
    except FileNotFoundError:
        return path
    if not any(_fd_matches(manifest_stat, fd) for fd in stdout_fds if fd is not None):
        return path
    if os.path.samefile(path, os.devnull):
        # A path such as /dev/stdout would point to stderr after redirection.
        return os.devnull
    sys.stderr.write("ansiroll: manifest path points to stdout: %s\n" % path)
    return None


def _fd_matches(path_stat, fd):
    try:
        return os.path.samestat(path_stat, os.fstat(fd))
    except OSError as error:
        if error.errno != errno.EBADF:
            raise
        return False


def _cmd_compile_stdout(args):
    manifest_path = args.manifest
    if manifest_path:
        manifest_path = _stdout_manifest_path(manifest_path, 1)
        if manifest_path is None:
            return 2
    with _compile_diagnostics_to_stderr() as saved_stdout:
        compiled = _compile_source(args)
        if compiled is None:
            return 2
        source, manifest = compiled
        if manifest_path:
            # Duplicating stdout may have made a /dev/fd path resolve to it.
            manifest_path = _stdout_manifest_path(manifest_path, saved_stdout.fileno())
            if manifest_path is None:
                return 2
            _write_manifest(manifest_path, manifest)
        saved_stdout.write(source.encode("utf-8"))

    _report_compiled(args.playbook, "stdout", manifest)
    return 0


def _compile_worker(channel_fd):
    with socket.socket(fileno=channel_fd) as channel:
        channel.set_inheritable(False)
        with channel.makefile("rb") as request_stream:
            request = json.load(request_stream)
        sys.path[:] = request["import_path"]
        compiled = _compile_source(argparse.Namespace(**request["args"]))
        if compiled is None:
            return 2
        channel.sendall(json.dumps(compiled).encode("utf-8"))
    return 0


def _compile_input_fds(args):
    paths = [args.playbook, args.vault_password_file, os.environ.get("ANSIBLE_VAULT_PASSWORD_FILE")]
    paths.extend(value[1:] for value in args.extra_vars if value.startswith("@"))
    requested = set()
    for path in paths:
        if path:
            directory, name = os.path.split(os.path.abspath(path))
            if name.isascii() and name.isdecimal():
                directory = os.path.realpath(directory)
                if directory == "/dev/fd" or re.fullmatch(r"/proc/\d+(?:/task/\d+)?/fd", directory):
                    requested.add(int(name))

    available = set()
    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            available = {int(name) for name in os.listdir(directory) if name.isdigit()}
        except OSError:
            continue
        break
    descriptors = []
    for fd in sorted((available | requested) - {0, 1, 2}):
        try:
            if os.get_inheritable(fd) or fd in requested:
                descriptors.append(fd)
        except OSError:
            # A descriptor may have closed since the directory was read.
            continue
    return descriptors


def _run_compile_worker(args):
    request = json.dumps({
        "args": vars(args),
        "import_path": [path for path in sys.path if isinstance(path, str)],
    }).encode("utf-8")
    input_fds = _compile_input_fds(args)
    try:
        os.fstat(0)
        stdin = 0
    except OSError:
        stdin = None
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    worker = (
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from ansiroll.cli import _compile_worker; sys.exit(_compile_worker(int(sys.argv[2])))"
    )
    flags = []
    if sys.flags.isolated:
        flags.append("-I")
    else:
        if sys.flags.ignore_environment:
            flags.append("-E")
        if sys.flags.no_user_site:
            flags.append("-s")
        if getattr(sys.flags, "safe_path", False):
            flags.append("-P")
    if sys.flags.no_site:
        flags.append("-S")
    parent, child = socket.socketpair()
    with parent, child:
        with subprocess.Popen(
            [sys.executable, *flags, "-c", worker, package_root, str(child.fileno())],
            stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            pass_fds=(*input_fds, child.fileno()),
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        ) as proc:
            child.close()
            # Drain diagnostics while exchanging the request and artifact.
            with ThreadPoolExecutor(max_workers=1) as pool:
                drain = pool.submit(proc.communicate)
                try:
                    parent.sendall(request)
                    parent.shutdown(socket.SHUT_WR)
                    with parent.makefile("rb") as response:
                        payload = response.read()
                except (BrokenPipeError, ConnectionResetError):
                    payload = b""
                diagnostics, _ = drain.result()
    return proc.returncode, payload, diagnostics.decode("utf-8", errors="replace")


def _cmd_compile_stdout_in_child(args):
    stdout, stderr = sys.stdout, sys.stderr
    try:
        stdout_fd = stdout.fileno()
    except (AttributeError, OSError, ValueError):
        stdout_fd = None
    manifest_path = args.manifest
    if manifest_path:
        manifest_path = _stdout_manifest_path(manifest_path, 1, stdout_fd)
        if manifest_path is None:
            return 2

    # Compile in a fresh interpreter so application threads and retained
    # streams are unaffected by the compiler's process-wide redirection.
    returncode, payload, diagnostics = _run_compile_worker(args)
    if diagnostics:
        stderr.write(diagnostics)
    if returncode:
        return returncode
    source, manifest = json.loads(payload)
    if manifest_path:
        manifest_path = _stdout_manifest_path(manifest_path, 1, stdout_fd)
        if manifest_path is None:
            return 2
        _write_manifest(manifest_path, manifest)
    if stdout is sys.__stdout__ and stdout_fd == 1:
        stdout.flush()
        stdout.buffer.write(source.encode("utf-8"))
    else:
        stdout.write(source)
    _report_compiled(args.playbook, "stdout", manifest)
    return 0


def _compile_source(args):
    try:
        extra_vars = _load_extra_vars(args.extra_vars, args.vault_password_file)
    except Exception as ex:
        sys.stderr.write("ansiroll: invalid extra vars: %s\n" % ex)
        return None
    try:
        return compile_playbook(args.playbook, python_interpreter=args.interpreter,
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
        return None


def _cmd_compile(args):
    compiled = _compile_source(args)
    if compiled is None:
        return 2
    source, manifest = compiled
    with open(args.output, "w") as fh:
        fh.write(source)
    os.chmod(args.output, os.stat(args.output).st_mode | stat.S_IEXEC | stat.S_IXGRP)
    if args.manifest:
        _write_manifest(args.manifest, manifest)
    _report_compiled(args.playbook, args.output, manifest)
    return 0


def _write_manifest(path, manifest):
    with open(path, "w") as fh:
        json.dump(manifest, fh, indent=2)


def _report_compiled(playbook, destination, manifest):
    sys.stderr.write(
        "ansiroll: compiled %s -> %s (%d modules embedded, %d fallback tasks)\n"
        % (playbook, destination, len(manifest["modules"]), len(manifest["fallback_tasks"]))
    )


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
