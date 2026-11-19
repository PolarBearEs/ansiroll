"""Conservative accelerated implementation of command and shell."""

from __future__ import absolute_import

import ast
import datetime
import glob
import io
import os
import selectors
import shlex
import subprocess

from . import _stdin
import sys
import traceback

_ALLOWED = frozenset((
    "_raw_params", "_uses_shell", "cmd", "chdir", "creates", "removes",
    "stdin", "stdin_add_newline", "strip_empty_ends", "executable",
    # Removed from ansible-core after 2.10.  The 2.10 command module accepts
    # this boolean controller-warning toggle but it does not affect execution.
    "warn",
))


def can_accelerate(args, check_mode=False, shell=False):
    if not isinstance(args, dict) or not set(args).issubset(_ALLOWED):
        return False
    raw = args.get("_raw_params", args.get("cmd"))
    if raw in (None, ""):
        return False
    if args.get("_raw_params") not in (None, "") and args.get("cmd") not in (None, ""):
        return False
    if not isinstance(raw, (str, list, tuple)):
        return False
    if isinstance(raw, (list, tuple)) and (
            not raw or not all(isinstance(value, str) for value in raw)):
        return False
    for key in ("stdin_add_newline", "strip_empty_ends", "_uses_shell", "warn"):
        if key in args and not isinstance(args[key], bool):
            return False
    if not shell and args.get("executable"):
        return False
    return True


def _exists(pattern):
    return bool(glob.glob(os.path.expanduser(str(pattern))))


def _stamp(value):
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


def _python_import_source(cmd):
    if (
            not hasattr(os, "fork")
            or len(cmd) != 3
            or cmd[1] != "-c"
            or os.path.realpath(cmd[0]) != os.path.realpath(sys.executable)):
        return None
    try:
        tree = ast.parse(cmd[2], mode="exec")
    except (SyntaxError, TypeError, ValueError):
        return None
    if not tree.body or not all(
            isinstance(node, (ast.Import, ast.ImportFrom))
            for node in tree.body):
        return None
    return cmd[2]


def _drain_fork_pipes(stdout_fd, stderr_fd):
    chunks = {stdout_fd: [], stderr_fd: []}
    selector = selectors.DefaultSelector()
    selector.register(stdout_fd, selectors.EVENT_READ)
    selector.register(stderr_fd, selectors.EVENT_READ)
    while selector.get_map():
        for key, _ in selector.select():
            chunk = os.read(key.fd, 65536)
            if chunk:
                chunks[key.fd].append(chunk)
            else:
                selector.unregister(key.fd)
    os.close(stdout_fd)
    os.close(stderr_fd)
    return (
        b"".join(chunks[stdout_fd]).decode("utf-8", "surrogateescape"),
        b"".join(chunks[stderr_fd]).decode("utf-8", "surrogateescape"),
    )


def _fork_python_import(source, environment=None, chdir=None):
    stdout_read, stdout_write = os.pipe()
    stderr_read, stderr_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(stdout_read)
            os.close(stderr_read)
            os.dup2(stdout_write, 1)
            os.dup2(stderr_write, 2)
            os.close(stdout_write)
            os.close(stderr_write)
            stdin_read = _stdin.eof_pipe()
            os.dup2(stdin_read, 0)
            os.close(stdin_read)
            sys.stdin = io.open(0, "r", encoding="utf-8", closefd=False)
            sys.stdout = io.open(1, "w", encoding="utf-8", closefd=False)
            sys.stderr = io.open(2, "w", encoding="utf-8", closefd=False)
            if environment:
                os.environ.update({
                    str(key): str(value) for key, value in environment.items()
                })
            if chdir:
                os.chdir(chdir)
            exec(compile(source, "<string>", "exec"), {"__name__": "__main__"})
        except BaseException:
            traceback.print_exc()
            sys.stderr.flush()
            os._exit(1)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    os.close(stdout_write)
    os.close(stderr_write)
    stdout, stderr = _drain_fork_pipes(stdout_read, stderr_read)
    _, status = os.waitpid(pid, 0)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status), stdout, stderr
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status), stdout, stderr
    return 1, stdout, stderr


def run(args, check_mode=False, shell=False, environment=None, options=None):
    raw = args.get("_raw_params", args.get("cmd"))
    cmd = (
        raw if shell
        else list(raw) if isinstance(raw, (list, tuple))
        else shlex.split(str(raw))
    )
    if not shell and (options or {}).get("expand_argument_vars"):
        cmd = [os.path.expandvars(value) for value in cmd]
    result = {
        "changed": False,
        "stdout": "",
        "stderr": "",
        "rc": None,
        "cmd": cmd,
        "start": None,
        "end": None,
        "delta": None,
        "msg": "",
    }
    creates = args.get("creates")
    removes = args.get("removes")
    if creates and _exists(creates):
        result.update({
            "rc": 0,
            "stdout": "skipped, since %s exists" % creates,
            "msg": "%s not run command since '%s' exists" % (
                "Would" if check_mode else "Did", creates),
        })
        result["stdout_lines"] = result["stdout"].splitlines()
        result["stderr_lines"] = []
        return result
    if removes and not _exists(removes):
        result.update({
            "rc": 0,
            "stdout": "skipped, since %s does not exist" % removes,
            "msg": "%s not run command since '%s' does not exist" % (
                "Would" if check_mode else "Did", removes),
        })
        result["stdout_lines"] = result["stdout"].splitlines()
        result["stderr_lines"] = []
        return result

    if check_mode:
        result.update({
            "changed": (
                creates is not None or removes is not None
                or not (options or {}).get("check_skip_unchanged")
            ),
            "rc": 0,
            "msg": "Command would have run if not in check mode",
            "stdout_lines": [],
            "stderr_lines": [],
        })
        if (
                (options or {}).get("check_always_skipped")
                or (creates is None and removes is None)):
            result["skipped"] = True
        return result

    stdin = args.get("stdin")
    if stdin is not None:
        stdin = str(stdin)
        if args.get("stdin_add_newline", True):
            stdin += "\n"
    started = datetime.datetime.now()
    process_env = None
    if environment:
        process_env = os.environ.copy()
        process_env.update({str(key): str(value)
                            for key, value in environment.items()})
    try:
        if shell:
            try:
                split_raw = shlex.split(raw) if isinstance(raw, str) else None
                import_source = (
                    _python_import_source(split_raw)
                    if split_raw is not None and stdin is None else None
                )
            except ValueError:
                split_raw = None
                import_source = None
            if import_source is not None:
                rc, stdout, stderr = _fork_python_import(
                    import_source, environment=environment,
                    chdir=args.get("chdir"))
                proc = None
            else:
                proc = _stdin.popen(
                    raw, shell=True, executable=args.get("executable"),
                    cwd=args.get("chdir"),
                    stdin=subprocess.PIPE if stdin is not None else None,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    universal_newlines=True, env=process_env,
                )
        else:
            import_source = (
                _python_import_source(cmd) if stdin is None else None)
            if import_source is not None:
                rc, stdout, stderr = _fork_python_import(
                    import_source, environment=environment,
                    chdir=args.get("chdir"))
                proc = None
            else:
                proc = _stdin.popen(
                    cmd, cwd=args.get("chdir"),
                    stdin=subprocess.PIPE if stdin is not None else None,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    universal_newlines=True, env=process_env,
                )
        if proc is not None:
            stdout, stderr = proc.communicate(stdin)
            rc = proc.returncode
    except Exception as ex:
        ended = datetime.datetime.now()
        return {
            "changed": False, "failed": True, "rc": 1, "cmd": cmd,
            "stdout": "", "stderr": str(ex), "msg": str(ex),
            "start": _stamp(started), "end": _stamp(ended),
            "delta": str(ended - started),
        }
    ended = datetime.datetime.now()
    stdout = stdout or ""
    stderr = stderr or ""
    if args.get("strip_empty_ends", True):
        stdout = stdout.rstrip("\r\n")
        stderr = stderr.rstrip("\r\n")
    result = {
        "changed": True, "rc": rc, "cmd": cmd,
        "stdout": stdout, "stderr": stderr,
        "stdout_lines": stdout.splitlines(), "stderr_lines": stderr.splitlines(),
        "start": _stamp(started), "end": _stamp(ended),
        "delta": str(ended - started),
        "msg": (
            "The command exited with a non-zero return code."
            if rc and (options or {}).get("verbose_nonzero")
            else "non-zero return code" if rc else ""
        ),
    }
    if rc != 0:
        result["failed"] = True
    return result
