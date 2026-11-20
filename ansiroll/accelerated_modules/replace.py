"""Conservative, stdlib-only subset of ansible.builtin.replace."""

from __future__ import absolute_import

import codecs
import errno
import io
import os
import re
import tempfile

from ._atomic import copy_replacement_attributes


_PATH_KEYS = ("path", "dest", "destfile", "name")
_ALLOWED = frozenset(
    _PATH_KEYS + (
        "regexp", "replace", "after", "before", "backup", "validate",
        "encoding",
    )
)


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
        codecs.lookup(args.get("encoding", "utf-8"))
        re.compile(args.get("regexp"), re.MULTILINE)
        if args.get("after") is not None:
            re.compile(args["after"], re.DOTALL)
        if args.get("before") is not None:
            re.compile(args["before"], re.DOTALL)
    except (LookupError, TypeError, ValueError, re.error):
        return False
    return (
        isinstance(path, str)
        and bool(path)
        and "\0" not in path
        and isinstance(args.get("regexp"), str)
        and isinstance(args.get("replace", ""), str)
        and (args.get("after") is None or isinstance(args["after"], str))
        and (args.get("before") is None or isinstance(args["before"], str))
        and args.get("backup", False) is False
        and args.get("validate") is None
        and isinstance(args.get("encoding", "utf-8"), str)
    )


def _failed(message, rc=None):
    result = {"failed": True, "msg": message}
    if rc is not None:
        result["rc"] = rc
    return result


def _section(contents, after, before):
    separator = b"(?P<subsection>.*?)" if isinstance(contents, bytes) else "(?P<subsection>.*?)"
    trailing = b"(?P<subsection>.*)" if isinstance(contents, bytes) else "(?P<subsection>.*)"
    leading = b"(?P<subsection>.*)" if isinstance(contents, bytes) else "(?P<subsection>.*)"
    if after and before:
        pattern = after + separator + before
    elif after:
        pattern = after + trailing
    elif before:
        pattern = leading + before
    else:
        return contents, None, None
    match = re.search(re.compile(pattern, re.DOTALL), contents)
    if not match:
        return None, None, pattern
    return (
        match.group("subsection"),
        (match.start("subsection"), match.end("subsection")),
        pattern,
    )


def _atomic_write(path, contents, encoding, original, chmod_before_chown):
    """Match atomic_move's attribute ordering and ignore EPERM before renaming."""
    directory = os.path.dirname(path) or "."
    descriptor, temporary = tempfile.mkstemp(prefix=".ansiroll-replace-", dir=directory)
    try:
        mode = "wb" if isinstance(contents, bytes) else "w"
        with io.open(
                descriptor, mode, encoding=None if mode == "wb" else encoding,
                closefd=True) as output:
            output.write(contents)
        try:
            copy_replacement_attributes(path, temporary, original, chmod_before_chown)
        except OSError as error:
            if error.errno != errno.EPERM:
                raise
        os.rename(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def run(args, check_mode=False, shell=False, environment=None, options=None):
    options = options or {}
    path = _single(args, _PATH_KEYS)
    encoding = args.get("encoding", "utf-8")
    if os.path.isdir(path):
        return _failed("Path %s is a directory !" % path, 256)
    if not os.path.exists(path):
        return _failed("Path %s does not exist !" % path, 257)
    real_path = os.path.realpath(path)
    binary_io = bool(options.get("binary_io"))
    include_rc = not bool(options.get("omit_result_rc"))
    try:
        original = os.stat(real_path)
        mode = "rb" if binary_io else "r"
        with io.open(
                real_path, mode, encoding=None if binary_io else encoding) as source:
            contents = source.read()
    except (IOError, OSError):
        return _failed("Unable to read the contents of %r." % path)

    try:
        if binary_io:
            regexp = args["regexp"].encode("utf-8", "surrogateescape")
            replacement_text = args.get("replace", "").encode("utf-8", "surrogateescape")
            after = args.get("after")
            before = args.get("before")
            after = after.encode("utf-8", "surrogateescape") if after is not None else None
            before = before.encode("utf-8", "surrogateescape") if before is not None else None
        else:
            regexp = args["regexp"]
            replacement_text = args.get("replace", "")
            after = args.get("after")
            before = args.get("before")
        section, indices, pattern = _section(
            contents, after, before)
        if section is None:
            if isinstance(pattern, bytes):
                pattern = pattern.decode("utf-8", "surrogateescape")
            result = {
                "msg": "Pattern for before/after params did not match the given file: %s"
                       % pattern,
                "changed": False,
            }
            if include_rc:
                result["rc"] = 0
            return result
        replacement, count = re.subn(
            re.compile(regexp, re.MULTILINE),
            replacement_text,
            section,
            count=0,
        )
    except re.error as error:
        return _failed("Unable to process replace due to error: %s" % error)

    changed = count > 0 and section != replacement
    if not changed:
        result = {"msg": "", "changed": False}
        if include_rc:
            result["rc"] = 0
        return result
    if indices is not None:
        replacement = (
            contents[:indices[0]] + replacement + contents[indices[1]:]
        )
    if not check_mode:
        try:
            _atomic_write(real_path, replacement, encoding, original,
                          options.get("chmod_before_chown"))
        except (IOError, OSError) as error:
            return _failed("Unable to write changes to %r: %s" % (path, error))
    result = {
        "msg": "%s replacements made" % count,
        "changed": True,
    }
    if include_rc:
        result["rc"] = 0
    if options.get("diff_mode"):
        def text(value):
            return (
                value.decode("utf-8", "surrogateescape")
                if isinstance(value, bytes) else value
            )
        result["diff"] = {
            "before_header": path,
            "before": text(contents),
            "after_header": path,
            "after": text(replacement),
        }
    return result
