"""Conservative, stdlib-only subset of ansible.builtin.find."""

from __future__ import absolute_import

import fnmatch
import errno
import grp
import os
import pwd
import stat


_PATH_KEYS = ("paths", "path", "name")
_ALLOWED = frozenset(_PATH_KEYS + (
    "patterns", "pattern", "excludes", "exclude", "file_type", "recurse",
    "hidden", "follow",
))
_FILE_TYPES = frozenset(("any", "directory", "file", "link"))


def _single(args, keys, default=None):
    present = [key for key in keys if key in args]
    if len(present) > 1:
        raise ValueError("conflicting aliases")
    return args[present[0]] if present else default


def _strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)) and all(
            isinstance(item, str) for item in value):
        return list(value)
    raise ValueError("expected a string or list of strings")


def can_accelerate(args, check_mode=False, shell=False):
    if not isinstance(args, dict) or not set(args).issubset(_ALLOWED):
        return False
    try:
        paths = _strings(_single(args, _PATH_KEYS))
        _strings(_single(args, ("patterns", "pattern"), ["*"]))
        excludes = _single(args, ("excludes", "exclude"))
        if excludes is not None:
            _strings(excludes)
    except (TypeError, ValueError):
        return False
    if not paths or any(not path or "\0" in path for path in paths):
        return False
    if args.get("file_type", "file") not in _FILE_TYPES:
        return False
    return all(
        isinstance(args.get(key, default), bool)
        for key, default in (
            ("recurse", False), ("hidden", False), ("follow", False)
        )
    )


def _statinfo(info, options=None):
    try:
        user = pwd.getpwuid(info.st_uid).pw_name
    except Exception:
        user = ""
    try:
        group = grp.getgrgid(info.st_gid).gr_name
    except Exception:
        group = ""
    mode = info.st_mode
    result = {
        "mode": "%04o" % stat.S_IMODE(mode),
        "isdir": stat.S_ISDIR(mode),
        "ischr": stat.S_ISCHR(mode),
        "isblk": stat.S_ISBLK(mode),
        "isreg": stat.S_ISREG(mode),
        "isfifo": stat.S_ISFIFO(mode),
        "islnk": stat.S_ISLNK(mode),
        "issock": stat.S_ISSOCK(mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "size": info.st_size,
        "inode": info.st_ino,
        "dev": info.st_dev,
        "nlink": info.st_nlink,
        "atime": info.st_atime,
        "mtime": info.st_mtime,
        "ctime": info.st_ctime,
        "gr_name": group,
        "pw_name": user,
        "wusr": bool(mode & stat.S_IWUSR),
        "rusr": bool(mode & stat.S_IRUSR),
        "xusr": bool(mode & stat.S_IXUSR),
        "wgrp": bool(mode & stat.S_IWGRP),
        "rgrp": bool(mode & stat.S_IRGRP),
        "xgrp": bool(mode & stat.S_IXGRP),
        "woth": bool(mode & stat.S_IWOTH),
        "roth": bool(mode & stat.S_IROTH),
        "xoth": bool(mode & stat.S_IXOTH),
        "isuid": bool(mode & stat.S_ISUID),
        "isgid": bool(mode & stat.S_ISGID),
    }
    if (options or {}).get("disk_usage_bytes"):
        blocks = getattr(info, "st_blocks", 0)
        result["blocks"] = blocks
        result["disk_usage_bytes"] = blocks * 512
    return result


def _matches(name, patterns, excludes):
    return (
        any(fnmatch.fnmatch(name, pattern) for pattern in patterns)
        and not any(fnmatch.fnmatch(name, pattern) for pattern in excludes)
    )


def _warning(message, error=None, typed=False):
    if not typed:
        return message
    event = {
        "msg": message,
        "__ansible_type": "Event",
    }
    if error is not None:
        event["chain"] = {
            "msg_reason": "<<< caused by >>>",
            "traceback_reason": (
                "The above exception was the direct cause of the following warning:"
            ),
            "event": {
                "msg": str(error),
                "__ansible_type": "Event",
            },
            "follow": True,
            "__ansible_type": "EventChain",
        }
    return {
        "event": event,
        "__ansible_type": "WarningSummary",
    }


def run(args, check_mode=False, shell=False, environment=None, options=None):
    paths = _strings(_single(args, _PATH_KEYS))
    patterns = _strings(_single(args, ("patterns", "pattern"), ["*"])) or ["*"]
    raw_excludes = _single(args, ("excludes", "exclude"))
    excludes = _strings(raw_excludes) if raw_excludes is not None else []
    wanted_type = args.get("file_type", "file")
    recurse = args.get("recurse", False)
    hidden = args.get("hidden", False)
    follow = args.get("follow", False)
    files = []
    examined = 0
    messages = []
    skipped_paths = {}
    warnings = []
    structured_skips = bool((options or {}).get("structured_skipped_paths"))
    typed_warnings = bool((options or {}).get("typed_warnings"))

    for raw_path in paths:
        path = os.path.expanduser(os.path.expandvars(raw_path))
        try:
            if not os.path.isdir(path):
                raise Exception("'%s' is not a directory" % path)

            def walk_error(error):
                if error.errno in (errno.EPERM, errno.EACCES, errno.ENOENT):
                    skipped_paths[error.filename] = str(error)
                    return
                raise error

            for root, directories, names in os.walk(
                    path,
                    onerror=walk_error if structured_skips else None,
                    followlinks=follow):
                examined += len(directories) + len(names)
                for name in names + directories:
                    if not hidden and name.startswith("."):
                        continue
                    item = os.path.normpath(os.path.join(root, name))
                    try:
                        info = os.lstat(item)
                    except OSError as error:
                        if structured_skips:
                            skipped_paths[item] = str(error)
                            if typed_warnings:
                                warnings.append(_warning(
                                    "Skipped entry %r due to access issue." % item,
                                    error=error,
                                    typed=True,
                                ))
                            else:
                                warnings.append(
                                    "Skipped entry '%s' due to this access issue: %s\n"
                                    % (item, error)
                                )
                        else:
                            messages.append(
                                "%s was skipped as it does not seem to be a valid "
                                "file or it cannot be accessed\n" % item
                            )
                        continue
                    if not _matches(name, patterns, excludes):
                        continue
                    mode = info.st_mode
                    type_matches = (
                        wanted_type == "any"
                        or (wanted_type == "file" and stat.S_ISREG(mode))
                        or (wanted_type == "directory" and stat.S_ISDIR(mode))
                        or (wanted_type == "link" and stat.S_ISLNK(mode))
                    )
                    if type_matches:
                        result = {"path": item}
                        result.update(_statinfo(info, options))
                        files.append(result)
                if not recurse:
                    break
        except Exception as error:
            if structured_skips:
                skipped_paths[path] = str(error)
                warning = (
                    "Skipped '%s' path due to this access issue: %s\n"
                    % (path, error)
                )
                warnings.append(_warning(warning, typed=typed_warnings))
            else:
                if isinstance(error, OSError):
                    messages.append(
                        "%s was skipped as it does not seem to be a valid "
                        "directory or it cannot be accessed\n" % path
                    )
                else:
                    messages.append(
                        "%s was skipped as it does not seem to be a valid "
                        "directory or it cannot be accessed\n" % path
                    )

    result = {
        "files": files,
        "changed": False,
        "msg": (
            "Not all paths examined, check warnings for details"
            if warnings else
            "".join(messages)
            or ("All paths examined"
                if (options or {}).get("all_paths_examined")
                else "")
        ),
        "matched": len(files),
        "examined": examined,
    }
    if skipped_paths:
        result["skipped_paths"] = skipped_paths
    if warnings:
        result["warnings"] = warnings
    return result
