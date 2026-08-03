"""Linux groupadd/groupmod implementation matching ansible.builtin.group."""

from __future__ import absolute_import

import grp
import os
import shutil
import subprocess


_ALLOWED = frozenset(("name", "state", "gid", "system"))


def can_accelerate(args, check_mode=False, shell=False):
    if not isinstance(args, dict) or not set(args).issubset(_ALLOWED):
        return False
    return (
        isinstance(args.get("name"), str)
        and bool(args.get("name"))
        and args.get("state", "present") == "present"
        and (args.get("gid") is None or isinstance(args.get("gid"), int))
        and isinstance(args.get("system", False), bool)
    )


def run(args, check_mode=False, shell=False, environment=None, options=None):
    name = args["name"]
    try:
        entry = grp.getgrnam(name)
    except KeyError:
        entry = None

    wanted_gid = args.get("gid")
    command = None
    if entry is None:
        command = ["groupadd"]
        if wanted_gid is not None:
            command.extend(["-g", str(wanted_gid)])
        if args.get("system", False):
            command.append("-r")
        command.append(name)
    elif wanted_gid is not None and entry.gr_gid != wanted_gid:
        command = ["groupmod", "-g", str(wanted_gid), name]

    # The official module's create check-mode return happens before it builds
    # the normal result mapping.
    if check_mode and entry is None:
        return {"changed": True}

    stdout = stderr = ""
    if command is not None and not check_mode:
        search_path = (environment or {}).get("PATH")
        executable = shutil.which(command[0], path=search_path)
        if executable is None:
            return {
                "failed": True,
                "msg": "Failed to find required executable %s in paths: %s"
                       % (command[0], search_path or os.environ.get("PATH", "")),
            }
        process_environment = os.environ.copy()
        if environment:
            process_environment.update(
                {str(key): str(value) for key, value in environment.items()})
        process = subprocess.Popen(
            [executable] + command[1:],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=process_environment,
        )
        raw_stdout, raw_stderr = process.communicate()
        stdout = raw_stdout.decode("utf-8", "surrogateescape")
        stderr = raw_stderr.decode("utf-8", "surrogateescape")
        if process.returncode != 0:
            return {"failed": True, "name": name, "msg": stderr}

    result = {
        "name": name,
        "state": "present",
        "changed": command is not None,
    }
    if stdout:
        result["stdout"] = stdout
    if stderr:
        result["stderr"] = stderr
    try:
        entry = grp.getgrnam(name)
    except KeyError:
        entry = None
    if entry is not None:
        result["system"] = args.get("system", False)
        result["gid"] = entry.gr_gid
    return result
