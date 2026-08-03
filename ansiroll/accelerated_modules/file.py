"""Conservative accelerated subset of ansible.builtin.file."""

from __future__ import absolute_import

import grp
import os
import pwd
import shutil
import stat
import tempfile

_PATH_KEYS = frozenset(("path", "dest", "name"))
_ALLOWED = frozenset((
    "path", "dest", "name", "src", "state", "mode", "recurse", "owner",
    "group", "force", "modification_time", "access_time",
))
_STATES = frozenset(("absent", "directory", "touch", "link"))


def _mode(value):
    if value is None:
        return None
    if isinstance(value, int):
        raise ValueError("integer modes are ambiguous; use the official file module")
    text = str(value)
    if text == "g+w":
        return "g+w"
    if not text or any(char not in "01234567" for char in text):
        raise ValueError("symbolic and special modes require the official file module")
    return int(text, 8)


def can_accelerate(args, check_mode=False, shell=False):
    if not isinstance(args, dict) or not set(args).issubset(_ALLOWED):
        return False
    if args.get("state", "file") not in _STATES:
        return False
    recurse = args.get("recurse", False)
    if not isinstance(recurse, bool):
        return False
    # With no file attributes to propagate, recurse=True is equivalent to
    # ensuring the directory itself exists. Attribute-bearing recursion stays
    # on the official module.
    if recurse and (
            args.get("state", "file") != "directory"
            or args.get("mode") not in (None, "g+w")
            or args.get("owner") is not None
            or args.get("group") is not None):
        return False
    paths = [key for key in _PATH_KEYS if args.get(key)]
    if len(paths) != 1:
        return False
    try:
        wanted_mode = _mode(args.get("mode"))
    except (TypeError, ValueError):
        return False
    for key in ("owner", "group"):
        value = args.get(key)
        if value is not None and not isinstance(value, (str, int)):
            return False
    force = args.get("force", False)
    if not isinstance(force, bool):
        return False
    state = args.get("state", "file")
    if state == "link":
        return (
            isinstance(args.get("src"), str)
            and bool(args.get("src"))
            and wanted_mode is None
            and not recurse
            and args.get("owner") is None
            and args.get("group") is None
            and args.get("modification_time") is None
            and args.get("access_time") is None
        )
    if args.get("src") is not None:
        return False
    time_values = (
        args.get("modification_time"), args.get("access_time"))
    if any(value is not None for value in time_values) and not (
            state == "touch" and time_values == ("preserve", "preserve")):
        return False
    return True


def _current_mode(path):
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        return None


def _identity(value, group=False):
    if value is None:
        return None
    if isinstance(value, int) or str(value).isdigit():
        return int(value)
    return (
        grp.getgrnam(str(value)).gr_gid if group
        else pwd.getpwnam(str(value)).pw_uid
    )


def run(args, check_mode=False, shell=False, environment=None, options=None):
    path = args.get("path") or args.get("dest") or args.get("name")
    state = args.get("state", "file")
    wanted_mode = _mode(args.get("mode"))
    symbolic_group_write = wanted_mode == "g+w"
    if symbolic_group_write:
        wanted_mode = None
    try:
        wanted_uid = _identity(args.get("owner"))
        wanted_gid = _identity(args.get("group"), group=True)
    except KeyError as error:
        return {
            "failed": True, "changed": False, "path": path,
            "msg": "failed to look up user or group: %s" % error,
        }
    try:
        if state == "absent":
            exists = os.path.lexists(path)
            if exists and not check_mode:
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                else:
                    os.unlink(path)
            return {"changed": exists, "path": path, "state": "absent"}

        if state == "link":
            source = args["src"]
            exists = os.path.lexists(path)
            force = args.get("force", False)
            source_base = (
                path if os.path.isdir(path) and not os.path.islink(path)
                else os.path.dirname(path)
            )
            resolved_source = os.path.join(source_base, source)
            if not force and not os.path.exists(resolved_source):
                return {
                    "failed": True, "changed": False, "path": path,
                    "src": source,
                    "msg": (
                        "src file does not exist, use 'force=yes' if you "
                        "really want to create the link: %s" % resolved_source
                    ),
                }
            is_link = os.path.islink(path)
            if is_link and os.readlink(path) == source:
                return {
                    "changed": False, "path": path, "dest": path,
                    "src": source, "state": "link",
                }
            is_directory = exists and os.path.isdir(path) and not is_link
            if exists and not is_link and not force:
                return {
                    "failed": True, "changed": False, "path": path,
                    "msg": "refusing to convert existing destination to symlink",
                }
            if is_directory and os.listdir(path):
                return {
                    "failed": True, "changed": False, "path": path,
                    "msg": "the directory is not empty, refusing to convert it",
                }
            if not check_mode:
                if not exists:
                    os.symlink(source, path)
                else:
                    # Retarget links atomically, matching ansible.builtin.file.
                    # Empty directories must be removed first because rename
                    # cannot replace them; non-empty directories were refused.
                    parent = os.path.dirname(path) or "."
                    descriptor, temporary = tempfile.mkstemp(
                        prefix=".ansiroll-link-", dir=parent
                    )
                    os.close(descriptor)
                    os.unlink(temporary)
                    try:
                        os.symlink(source, temporary)
                        if is_directory:
                            os.rmdir(path)
                        os.replace(temporary, path)
                    finally:
                        if os.path.lexists(temporary):
                            os.unlink(temporary)
            return {
                "changed": True, "path": path, "dest": path,
                "src": source, "state": "link",
            }

        if state == "directory":
            exists = os.path.isdir(path)
            before_mode = _current_mode(path)
            before = os.stat(path) if exists else None
            changed = not exists or (
                wanted_mode is not None and before_mode != wanted_mode
            ) or (
                wanted_uid is not None and before.st_uid != wanted_uid
            ) or (
                wanted_gid is not None and before.st_gid != wanted_gid
            )
            recursive_paths = []
            if exists and symbolic_group_write:
                recursive_paths = [path]
                for root, directories, files in os.walk(path):
                    recursive_paths.extend(
                        os.path.join(root, name)
                        for name in directories + files
                        if not os.path.islink(os.path.join(root, name))
                    )
                changed = changed or any(
                    not (os.stat(item).st_mode & stat.S_IWGRP)
                    for item in recursive_paths
                )
            if not check_mode:
                if not exists:
                    os.makedirs(path)
                if wanted_mode is not None:
                    os.chmod(path, wanted_mode)
                if wanted_uid is not None or wanted_gid is not None:
                    os.chown(
                        path,
                        wanted_uid if wanted_uid is not None else -1,
                        wanted_gid if wanted_gid is not None else -1,
                    )
                if symbolic_group_write:
                    if not recursive_paths:
                        recursive_paths = [path]
                    for item in recursive_paths:
                        current = os.stat(item).st_mode
                        if not (current & stat.S_IWGRP):
                            os.chmod(item, current | stat.S_IWGRP)
            return {
                "changed": changed, "path": path, "state": "directory",
                "mode": "%04o" % wanted_mode if wanted_mode is not None else None,
            }

        existed = os.path.exists(path)
        before_mode = _current_mode(path)
        before = os.stat(path) if existed else None
        preserve_times = (
            args.get("modification_time") == "preserve"
            and args.get("access_time") == "preserve"
        )
        changed = not existed if preserve_times else True
        if not check_mode:
            parent = os.path.dirname(path)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with open(path, "a"):
                if not preserve_times:
                    os.utime(path, None)
            if wanted_mode is not None:
                os.chmod(path, wanted_mode)
            if wanted_uid is not None or wanted_gid is not None:
                os.chown(
                    path,
                    wanted_uid if wanted_uid is not None else -1,
                    wanted_gid if wanted_gid is not None else -1,
                )
        return {
            "changed": (
                changed or not existed
                or (wanted_mode is not None and before_mode != wanted_mode)
                or (wanted_uid is not None and before.st_uid != wanted_uid)
                or (wanted_gid is not None and before.st_gid != wanted_gid)
            ),
            "path": path, "state": "file", "dest": path,
            "mode": "%04o" % wanted_mode if wanted_mode is not None else None,
        }
    except Exception as ex:
        return {
            "failed": True, "changed": False, "path": path, "msg": str(ex)
        }
