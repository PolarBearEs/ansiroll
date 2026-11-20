"""Conservative accelerated implementation of command and shell."""

from __future__ import absolute_import

import datetime
import glob
import os
import re
import shlex
import subprocess

from . import _stdin

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


def _stamp(value):
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


def _expand_path(value):
    """AnsibleModule type='path': expand $VARS and ~."""
    return os.path.expanduser(os.path.expandvars(str(value)))


def _matching_guard(creates, removes):
    if creates and glob.glob(creates):
        return "creates"
    if removes and not glob.glob(removes):
        return "removes"
    return None


def _check_guards(chdir, creates, removes):
    if not chdir:
        return _matching_guard(creates, removes), None
    # Glob from the actual directory so its name is literal, and restore
    # the agent's working directory before returning.
    # Linux permits fchdir on an O_PATH directory descriptor with only
    # search permission. POSIX O_SEARCH serves the same purpose where present.
    search_flag = getattr(os, "O_PATH", getattr(os, "O_SEARCH", os.O_RDONLY))
    if not os.access(".", os.X_OK):
        return "fallback", None
    try:
        saved = os.open(".", search_flag)
    except PermissionError:
        return "fallback", None
    changed_directory = False
    try:
        try:
            os.chdir(chdir.encode("utf-8", "surrogateescape"))
            changed_directory = True
        except OSError as error:
            return None, error
        return _matching_guard(creates, removes), None
    finally:
        try:
            if changed_directory:
                os.fchdir(saved)
        finally:
            os.close(saved)


_PASSWD_ARG_RE = re.compile(r'^[-]{0,2}pass[-]?(word|wd)?')


def _clean_args(args):
    """AnsibleModule._clean_args(): printable command, password args masked."""
    if isinstance(args, str):
        args = shlex.split(args)
    clean = []
    is_passwd = False
    for arg in args:
        if is_passwd:
            is_passwd = False
            clean.append("********")
            continue
        if _PASSWD_ARG_RE.match(arg):
            sep = arg.find("=")
            if sep > -1:
                clean.append("%s=********" % arg[:sep])
                continue
            is_passwd = True
        clean.append(arg)
    return " ".join(shlex.quote(arg) for arg in clean)


def run(args, check_mode=False, shell=False, environment=None, options=None):
    options = options or {}
    raw = args.get("_raw_params", args.get("cmd"))
    cmd = (
        raw if shell
        else list(raw) if isinstance(raw, (list, tuple))
        else shlex.split(str(raw))
    )
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
    chdir, creates, removes = (
        _expand_path(args[key]) if args.get(key) is not None else None
        for key in ("chdir", "creates", "removes")
    )
    guard, error = _check_guards(chdir, creates, removes)
    if guard == "fallback":
        return {"_ansiroll_fallback": True}
    if error is not None:
        # ansible-core < 2.19 appended the error text.
        result["msg"] = (
            "Unable to change directory before execution: %s" % error
            if options.get("chdir_error_detail")
            else "Unable to change directory before execution."
        )
        result["failed"] = True
        result["stdout_lines"] = []
        result["stderr_lines"] = []
        return result
    if guard == "creates":
        result.update({
            "rc": 0,
            "stdout": "skipped, since %s exists" % creates,
            "msg": "%s not run command since '%s' exists" % (
                "Would" if check_mode else "Did", creates),
        })
        result["stdout_lines"] = result["stdout"].splitlines()
        result["stderr_lines"] = []
        return result
    if guard == "removes":
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
                or not options.get("check_skip_unchanged")
            ),
            "rc": 0,
            "msg": "Command would have run if not in check mode",
            "stdout_lines": [],
            "stderr_lines": [],
        })
        if (
                options.get("check_always_skipped")
                or (creates is None and removes is None)):
            result["skipped"] = True
        return result

    stdin = args.get("stdin")
    if stdin is not None:
        stdin = str(stdin)
        if args.get("stdin_add_newline", True):
            stdin += "\n"
    started = datetime.datetime.now()
    argv = None
    process_env = None
    if environment:
        process_env = os.environ.copy()
        process_env.update({str(key): str(value)
                            for key, value in environment.items()})
    try:
        if shell:
            # run_command(use_unsafe_shell=True): [executable, -c, cmd] when
            # an executable is given, else Popen(cmd, shell=True).
            executable = args.get("executable")
            script = (" ".join(shlex.quote(value) for value in raw)
                      if isinstance(raw, (list, tuple)) else raw)
            argv = [executable, "-c", script] if executable else script
            proc = _stdin.popen(
                [value.encode("utf-8", "surrogateescape") for value in argv]
                if executable else script.encode("utf-8", "surrogateescape"),
                shell=not executable,
                cwd=chdir or None,
                stdin=subprocess.PIPE if stdin is not None else None,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=process_env,
            )
        else:
            # run_command(expand_user_and_vars=True) expands ~ and $VARS in
            # every argument (even quoted ones); the reported cmd is not.
            argv = [os.path.expanduser(os.path.expandvars(value)) for value in cmd]
            proc = _stdin.popen(
                [value.encode("utf-8", "surrogateescape") for value in argv],
                cwd=chdir or None,
                stdin=subprocess.PIPE if stdin is not None else None,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=process_env,
            )
        stdout, stderr = proc.communicate(
            stdin.encode("utf-8", "surrogateescape") if stdin is not None else None)
        rc = proc.returncode
    except Exception as ex:
        # run_command uses errno (257 for non-OSError) and a sanitized command.
        # It started including empty output fields in ansible-core 2.11.
        result = {
            "failed": True,
            "rc": ex.errno if isinstance(ex, OSError) else 257,
            "cmd": _clean_args(argv if argv is not None else raw),
            # ansible-core < 2.19 reported to_native(e) instead.
            "msg": (str(ex) if options.get("exec_error_detail")
                    else "Error executing command."),
        }
        if not options.get("omit_exec_error_output"):
            result.update(stdout="", stderr="", stdout_lines=[], stderr_lines=[])
        return result
    ended = datetime.datetime.now()
    # The module runs with encoding=None and converts bytes with to_text.
    stdout = (stdout or b"").decode("utf-8", "surrogateescape")
    stderr = (stderr or b"").decode("utf-8", "surrogateescape")
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
            if rc and options.get("verbose_nonzero")
            else "non-zero return code" if rc else ""
        ),
    }
    if rc != 0:
        result["failed"] = True
    return result
