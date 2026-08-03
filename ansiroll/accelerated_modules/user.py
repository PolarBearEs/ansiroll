"""Linux useradd/usermod implementation matching ansible.builtin.user."""

from __future__ import absolute_import

import grp
import os
import pwd
import shutil
import subprocess


_ALLOWED = frozenset((
    "name", "state", "group", "groups", "append", "uid", "shell",
    "system", "create_home", "createhome", "home", "comment",
))


def can_accelerate(args, check_mode=False, shell=False):
    if not isinstance(args, dict) or not set(args).issubset(_ALLOWED):
        return False
    if (
            not isinstance(args.get("name"), str)
            or not args.get("name")
            or args.get("state", "present") != "present"):
        return False
    for key in ("append", "system", "create_home", "createhome"):
        if key in args and not isinstance(args[key], bool):
            return False
    if args.get("uid") is not None and not isinstance(args["uid"], int):
        return False
    for key in ("group", "shell", "home", "comment"):
        if args.get(key) is not None and not isinstance(args[key], (str, int)):
            return False
    groups = args.get("groups")
    return (
        groups is None
        or isinstance(groups, str)
        or (
            isinstance(groups, (list, tuple))
            and all(isinstance(group, (str, int)) for group in groups)
        )
    )


def _gid(value):
    if isinstance(value, int) or str(value).isdigit():
        return int(value)
    return grp.getgrnam(str(value)).gr_gid


def _group_names(values):
    if isinstance(values, str):
        values = [value.strip() for value in values.split(",") if value.strip()]
    return set(_group(value).gr_name for value in (values or []))


def _group(value):
    try:
        return grp.getgrgid(_gid(value))
    except KeyError:
        raise KeyError("Group %s does not exist" % value)


def _memberships(name, include_primary=False):
    entry = pwd.getpwnam(name)
    return [
        group.gr_name for group in grp.getgrall()
        if name in group.gr_mem
        and (include_primary or group.gr_gid != entry.pw_gid)
    ]


def _execute(command, environment):
    search_path = (environment or {}).get("PATH")
    executable = shutil.which(command[0], path=search_path)
    if executable is None:
        return (
            None,
            "",
            "Failed to find required executable %s in paths: %s"
            % (command[0], search_path or os.environ.get("PATH", "")),
        )
    process_environment = os.environ.copy()
    if environment:
        process_environment.update(
            {str(key): str(value) for key, value in environment.items()})
    process = subprocess.Popen(
        [executable] + [str(value) for value in command[1:]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=process_environment,
    )
    stdout, stderr = process.communicate()
    return (
        process.returncode,
        stdout.decode("utf-8", "surrogateescape"),
        stderr.decode("utf-8", "surrogateescape"),
    )


def run(args, check_mode=False, shell=False, environment=None, options=None):
    name = args["name"]
    if args.get("append") and args.get("groups") is None:
        return {
            "failed": True,
            "msg": "append is True but all of the following are missing: groups",
        }
    try:
        entry = pwd.getpwnam(name)
        existed = True
    except KeyError:
        entry = None
        existed = False

    try:
        primary = _group(args["group"]) if args.get("group") is not None else None
        wanted_groups = (
            _group_names(args["groups"])
            if args.get("groups") is not None else None
        )
    except KeyError as error:
        return {"failed": True, "msg": str(error).strip("'")}

    command = ["usermod" if existed else "useradd"]
    if args.get("uid") is not None and (
            not existed or entry.pw_uid != args["uid"]):
        command.extend(["-u", str(args["uid"])])
    if primary is not None and (
            not existed or entry.pw_gid != primary.gr_gid):
        command.extend(["-g", str(primary.gr_gid) if existed else str(args["group"])])
    elif not existed:
        try:
            grp.getgrnam(name)
        except KeyError:
            pass
        else:
            command.append("-N")

    if wanted_groups is not None:
        if not existed:
            if wanted_groups:
                command.extend(["-G", ",".join(sorted(wanted_groups))])
        else:
            current_groups = set(_memberships(name, include_primary=True))
            difference = current_groups.symmetric_difference(wanted_groups)
            if difference:
                if args.get("append", False):
                    additions = wanted_groups.difference(current_groups)
                    if additions:
                        command.extend(["-a", "-G", ",".join(sorted(wanted_groups))])
                else:
                    command.extend(["-G", ",".join(sorted(wanted_groups))])

    for key, option, current in (
            ("comment", "-c", entry.pw_gecos if entry else None),
            ("home", "-d", entry.pw_dir if entry else None),
            ("shell", "-s", entry.pw_shell if entry else None)):
        value = args.get(key)
        if value is not None and (not existed or current != value):
            command.extend([option, str(value)])
    if not existed:
        command.append("-m" if args.get(
            "create_home", args.get("createhome", True)) else "-M")
        if args.get("system", False):
            command.append("-r")

    changed = len(command) > 1
    if changed:
        command.append(name)
    if check_mode and not existed:
        return {"changed": True}

    stdout = stderr = ""
    if changed and not check_mode:
        rc, stdout, stderr = _execute(command, environment)
        if rc is None:
            return {"failed": True, "msg": stderr}
        if rc != 0:
            return {"failed": True, "name": name, "msg": stderr, "rc": rc}

    result = {"name": name, "state": "present"}
    if existed:
        result["append"] = args.get("append", False)
        result["move_home"] = False
    else:
        result["system"] = args.get("system", False)
        result["create_home"] = args.get(
            "create_home", args.get("createhome", True))
    result["changed"] = changed
    if stdout:
        result["stdout"] = stdout
    if stderr:
        result["stderr"] = stderr

    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        return result
    result.update({
        "uid": entry.pw_uid,
        "group": entry.pw_gid,
        "comment": entry.pw_gecos,
        "home": entry.pw_dir,
        "shell": entry.pw_shell,
    })
    if (options or {}).get("result_memberships"):
        result["groups"] = ",".join(_memberships(name))
    elif args.get("groups") is not None:
        groups = args["groups"]
        result["groups"] = (
            groups if isinstance(groups, str)
            else ",".join(str(group) for group in groups)
        )
    create_home = args.get("create_home", args.get("createhome", True))
    if (
            create_home
            and entry.pw_dir not in ("/", "/var/empty")
            and not os.path.exists(entry.pw_dir)):
        result["changed"] = True
        if not check_mode:
            os.makedirs(entry.pw_dir)
            os.chown(entry.pw_dir, entry.pw_uid, entry.pw_gid)
    return result
