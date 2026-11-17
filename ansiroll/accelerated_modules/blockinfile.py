"""Conservative, stdlib-only subset of ansible.builtin.blockinfile."""

from __future__ import absolute_import

import errno
import io
import os
import re
import shutil
import stat
import tempfile
import time


_PATH_KEYS = ("path", "dest", "name")
_ALLOWED = frozenset(_PATH_KEYS + (
    "block", "content", "marker", "marker_begin", "marker_end",
    "insertafter", "insertbefore", "create", "state", "backup", "validate",
))


def _text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", "surrogateescape")
    return value


def _single(args, keys, default=None):
    present = [key for key in keys if key in args]
    if len(present) > 1:
        raise ValueError("conflicting aliases")
    return args[present[0]] if present else default


def can_accelerate(args, check_mode=False, shell=False):
    if not isinstance(args, dict) or not set(args).issubset(_ALLOWED):
        return False
    try:
        path = _single(args, _PATH_KEYS)
        block = _single(args, ("block", "content"), "")
    except ValueError:
        return False
    marker = args.get("marker", "# {mark} ANSIBLE MANAGED BLOCK")
    if not all(isinstance(value, str) for value in (path, block, marker)):
        return False
    if not path or "\0" in path or "{mark}" not in marker or "\n" in marker:
        return False
    for key in ("marker_begin", "marker_end", "insertafter", "insertbefore"):
        value = args.get(key)
        if value is not None and not isinstance(value, str):
            return False
    if args.get("insertafter") is not None and args.get("insertbefore") is not None:
        return False
    if args.get("state", "present") not in ("present", "absent"):
        return False
    if args.get("backup", False) is not False or args.get("validate") is not None:
        return False
    return isinstance(args.get("create", False), bool)


def _current_umask():
    # Read it without the os.umask(0) round trip where the kernel exposes it.
    try:
        with io.open("/proc/self/status") as status:
            for line in status:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (IOError, OSError, ValueError):
        pass
    umask = os.umask(0)
    os.umask(umask)
    return umask


def _atomic_write(path, contents, text_mode):
    """blockinfile.write_changes() + AnsibleModule.atomic_move()."""
    directory = os.path.dirname(path) or "."
    descriptor, temporary = tempfile.mkstemp(
        prefix=".ansiroll-blockinfile-", dir=directory)
    try:
        if text_mode:
            # ansible-core >= 2.20: text file, newline=None.
            output = io.open(descriptor, "w", encoding="utf-8", closefd=True)
        else:
            output = io.open(descriptor, "wb", closefd=True)
        with output:
            output.write(contents)
        creating = not os.path.exists(path)
        if not creating:
            try:
                dest_stat = os.stat(path)
                os.chown(temporary, dest_stat.st_uid, dest_stat.st_gid)
                shutil.copystat(path, temporary)
                now = time.time()
                os.utime(temporary, (now, now))
            except OSError as error:
                if error.errno != errno.EPERM:
                    raise
        os.rename(temporary, path)
        if creating:
            os.chmod(path, 0o666 & ~_current_umask())
            dir_stat = os.stat(directory)
            try:
                os.chown(path, os.geteuid(),
                         dir_stat.st_gid if dir_stat.st_mode & stat.S_ISGID
                         else os.getegid())
            except OSError:
                pass
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def run(args, check_mode=False, shell=False, environment=None, options=None):
    options = options or {}
    path = _single(args, _PATH_KEYS)
    diff_mode = bool(options.get("diff_mode"))
    # ansible-core < 2.20 worked on bytes; 2.20 added encoding (utf-8).
    text_mode = bool(options.get("text_mode"))
    if text_mode:
        conv = lambda value: value  # noqa: E731
    else:
        conv = lambda value: value.encode("utf-8", "surrogateescape")  # noqa: E731
    if os.path.isdir(path):
        return {
            "failed": True,
            "msg": "Path %s is a directory !" % path,
            "rc": 256,
        }
    exists = os.path.exists(path)
    if not exists and not args.get("create", False):
        return {
            "failed": True,
            "msg": "Path %s does not exist !" % path,
            "rc": 257,
        }
    if not exists:
        parent = os.path.dirname(path)
        if parent and not os.path.exists(parent) and not check_mode:
            try:
                os.makedirs(parent)
            except OSError as error:
                return {"failed": True, "msg": (
                    "Error creating %s Error code: %s Error description: %s"
                    % (parent, error.errno, error.strerror))}
        content = None
        lines = []
    else:
        try:
            if text_mode:
                # newline="" preserves the file's newline sequences.
                with io.open(path, "r", encoding="utf-8", newline="") as source:
                    content = source.read()
            else:
                with io.open(path, "rb") as source:
                    content = source.read()
        except UnicodeDecodeError as error:
            # The official module dies with this exception.
            return {"failed": True, "changed": False,
                    "msg": "Task failed: Module failed: %s" % error}
        except (IOError, OSError) as error:
            return {"failed": True, "msg": str(error)}
        lines = content.splitlines(True)

    diff = {
        "before": _text(content) if diff_mode and content else "",
        "after": "",
        "before_header": "%s (content)" % path,
        "after_header": "%s (content)" % path,
    }
    state = args.get("state", "present")
    if state == "absent" and content is None:
        return {"changed": False, "msg": "File %s not present" % path}

    insertbefore = args.get("insertbefore")
    insertafter = args.get("insertafter")
    if insertbefore is None and insertafter is None:
        insertafter = "EOF"
    if insertafter not in (None, "EOF"):
        insertre = re.compile(conv(insertafter))
    elif insertbefore not in (None, "BOF"):
        insertre = re.compile(conv(insertbefore))
    else:
        insertre = None

    marker = conv(args.get("marker", "# {mark} ANSIBLE MANAGED BLOCK"))
    line_separator = conv(os.linesep)
    begin = re.sub(conv(r"{mark}"), conv(args.get("marker_begin", "BEGIN")),
                   marker) + line_separator
    end = re.sub(conv(r"{mark}"), conv(args.get("marker_end", "END")),
                 marker) + line_separator
    block = conv(_single(args, ("block", "content"), ""))
    if state == "present" and block:
        if not block.endswith(line_separator):
            block += line_separator
        blocklines = [begin] + block.splitlines(True) + [end]
    else:
        blocklines = []

    n0 = n1 = None
    for index, line in enumerate(lines):
        if line == begin:
            n0 = index
        if line == end:
            n1 = index

    if None in (n0, n1):
        n0 = None
        if insertre is not None:
            if insertre.flags & re.MULTILINE:
                match = insertre.search(content or conv(""))
                if match:
                    if insertafter:
                        n0 = (content or conv("")).count(conv("\n"), 0, match.end())
                    elif insertbefore:
                        n0 = (content or conv("")).count(conv("\n"), 0, match.start())
            else:
                for index, line in enumerate(lines):
                    if insertre.search(line):
                        n0 = index
            if n0 is None:
                n0 = len(lines)
            elif insertafter is not None:
                n0 += 1
        elif insertbefore is not None:
            n0 = 0
        else:
            n0 = len(lines)
    elif n0 < n1:
        lines[n0:n1 + 1] = []
    else:
        lines[n1:n0 + 1] = []
        n0 = n1

    if n0 > 0 and not lines[n0 - 1].endswith(line_separator):
        lines[n0 - 1] += line_separator
    lines[n0:n0] = blocklines
    result = conv("").join(lines)
    if diff_mode:
        diff["after"] = _text(result)

    if content == result:
        message = ""
        changed = False
    elif content is None:
        message = "File created"
        changed = True
    elif not blocklines:
        message = "Block removed"
        changed = True
    else:
        message = "Block inserted"
        changed = True

    if changed and not check_mode:
        try:
            _atomic_write(os.path.realpath(path), result, text_mode)
        except (IOError, OSError) as error:
            return {"failed": True, "msg": str(error)}
    if check_mode and not exists:
        return {"changed": changed, "msg": message, "diff": diff}
    attributes = {
        "before_header": "%s (file attributes)" % path,
        "after_header": "%s (file attributes)" % path,
    }
    return {"changed": changed, "msg": message, "diff": [diff, attributes]}
