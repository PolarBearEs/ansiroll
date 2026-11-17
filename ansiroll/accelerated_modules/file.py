"""Conservative accelerated subset of ansible.builtin.file."""

from __future__ import absolute_import

import errno
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


_FALLBACK = {"_ansiroll_fallback": True}


class _Fallback(Exception):
    """Raised before any mutation when only the official module is faithful."""


def _selinux_enabled():
    try:
        import selinux  # noqa: F401
    except ImportError:
        return False
    try:
        return selinux.is_selinux_enabled() == 1
    except Exception:
        return True


def _identity(value, group=False):
    """Resolve owner/group like set_owner/group_if_different; unknown names
    fall back so the official module reports them."""
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return (grp.getgrnam(str(value)).gr_gid if group
                else pwd.getpwnam(str(value)).pw_uid)
    except KeyError:
        raise _Fallback()


def _add_path_info(result):
    """AnsibleModule.add_path_info(), applied by exit_json/fail_json."""
    path = result.get("path", result.get("dest"))
    if path is None or not os.path.exists(path):
        return result
    st = os.lstat(path)
    result["uid"] = st.st_uid
    result["gid"] = st.st_gid
    try:
        result["owner"] = pwd.getpwuid(st.st_uid)[0]
    except KeyError:
        result["owner"] = str(st.st_uid)
    try:
        result["group"] = grp.getgrgid(st.st_gid)[0]
    except KeyError:
        result["group"] = str(st.st_gid)
    result["mode"] = "0%03o" % stat.S_IMODE(st.st_mode)
    if os.path.islink(path):
        result["state"] = "link"
    elif os.path.isdir(path):
        result["state"] = "directory"
    elif os.stat(path).st_nlink > 1:
        result["state"] = "hard"
    else:
        result["state"] = "file"
    result["size"] = st.st_size
    return result


def _get_state(path):
    try:
        st = os.lstat(path)
    except OSError as error:
        if error.errno == errno.ENOENT:
            return "absent"
        raise _Fallback()
    if stat.S_ISLNK(st.st_mode):
        return "link"
    if stat.S_ISDIR(st.st_mode):
        return "directory"
    if st.st_nlink > 1:
        return "hard"
    return "file"


class _Attributes(object):
    """set_fs_attributes_if_different() for owner, group and an octal or g+w
    mode (selinux and attributes are not accelerated)."""

    def __init__(self, uid, gid, mode, check_mode):
        self.uid = uid
        self.gid = gid
        self.mode = mode
        self.check_mode = check_mode

    def preflight(self, path):
        """Fall back before mutating when chown would need privileges."""
        if os.geteuid() == 0:
            return
        st = os.lstat(path) if os.path.lexists(path) else None
        uid = st.st_uid if st else os.geteuid()
        gid = st.st_gid if st else os.getegid()
        if self.uid is not None and self.uid != uid:
            raise _Fallback()
        if self.gid is not None and self.gid != gid and (
                self.gid not in os.getgroups() or uid != os.geteuid()):
            raise _Fallback()

    def apply(self, path, changed):
        if self.check_mode and not os.path.exists(path):
            return True
        st = os.lstat(path)
        if self.uid is not None and st.st_uid != self.uid:
            if self.check_mode:
                return True
            os.lchown(path, self.uid, -1)
            changed = True
        if self.gid is not None and os.lstat(path).st_gid != self.gid:
            if self.check_mode:
                return True
            os.lchown(path, -1, self.gid)
            changed = True
        if self.mode is not None:
            previous = stat.S_IMODE(os.lstat(path).st_mode)
            wanted = (previous | stat.S_IWGRP if self.mode == "g+w"
                      else self.mode)
            if previous != wanted:
                if self.check_mode:
                    return True
                os.chmod(path, wanted)
                if stat.S_IMODE(os.lstat(path).st_mode) != previous:
                    changed = True
        return changed


def _ensure_absent(path, check_mode):
    prev_state = _get_state(path)
    if prev_state == "absent":
        return {"path": path, "changed": False, "state": "absent"}
    if not check_mode:
        if prev_state == "directory":
            shutil.rmtree(path)
        else:
            try:
                os.unlink(path)
            except OSError as error:
                if error.errno != errno.ENOENT:
                    raise
    return {"path": path, "changed": True, "state": "absent"}


def _ensure_directory(path, recurse, attributes, check_mode):
    prev_state = _get_state(path)
    if prev_state == "link":
        raise _Fallback()  # follow=True operates on the link target
    if prev_state == "absent":
        if check_mode:
            return {"path": path, "changed": True}
        # mkdir -p, applying the attributes to every directory it creates.
        changed = False
        curpath = ""
        for dirname in path.strip("/").split("/"):
            curpath = "/".join([curpath, dirname])
            if not os.path.isabs(path):
                curpath = curpath.lstrip("/")
            # Creating an earlier component can make an existing path like
            # new/.. reachable. Apply attributes only after this live check.
            if os.path.exists(curpath):
                continue
            if not changed:
                attributes.preflight(curpath)
            try:
                os.mkdir(curpath)
                changed = True
            except OSError as error:
                if not (error.errno == errno.EEXIST and os.path.isdir(curpath)):
                    if not changed:
                        raise _Fallback()
                    return {
                        "failed": True, "path": path,
                        "msg": "There was an issue creating %s as requested: %s"
                               % (curpath, error),
                    }
            changed = attributes.apply(curpath, changed)
        return {"path": path, "changed": changed}
    if prev_state != "directory":
        return {
            "failed": True, "path": path,
            "msg": "%s already exists as a %s" % (path, prev_state),
        }
    # The existing-directory branch uses load_file_common_arguments for
    # the root, then recurses from the once-expanded path with expand=False.
    attribute_path = os.path.expanduser(os.path.expandvars(path))
    if attribute_path != path and (
            os.path.islink(attribute_path) or not os.path.exists(attribute_path)):
        raise _Fallback()
    targets = [attribute_path]
    if recurse:
        for root, directories, files in os.walk(path):
            for name in directories + files:
                item = os.path.join(root, name)
                if os.path.islink(item):
                    raise _Fallback()
                targets.append(item)
    for item in targets:
        attributes.preflight(item)
    changed = False
    for item in targets:
        changed = attributes.apply(item, changed) or changed
    return {"path": path, "changed": changed}


def _execute_touch(path, preserve_times, attributes, check_mode, options):
    prev_state = _get_state(path)
    if prev_state == "link":
        raise _Fallback()
    result = {"dest": path}
    if check_mode and options.get("omit_touch_check_changed"):
        return result
    if prev_state == "absent":
        if check_mode:
            result["changed"] = True
            return result
        parent = os.path.dirname(path) or "."
        if not os.path.isdir(parent):
            raise _Fallback()  # the official module reports the failure
        attributes.preflight(path)
        open(path, "ab").close()
        changed = True
    else:
        attributes.preflight(path)
        changed = False
    changed = attributes.apply(path, changed)
    if not preserve_times:
        # modification_time/access_time default to "now" for touch.
        if not check_mode:
            os.utime(path, None)
        changed = True
    result["changed"] = changed
    return result


def run(args, check_mode=False, shell=False, environment=None, options=None):
    options = options or {}
    if options.get("diff_mode") or _selinux_enabled():
        return _FALLBACK
    path = args.get("path") or args.get("dest") or args.get("name")
    path = os.path.expanduser(os.path.expandvars(path))
    state = args.get("state", "file")
    try:
        wanted_mode = _mode(args.get("mode"))
        attributes = _Attributes(
            _identity(args.get("owner")),
            _identity(args.get("group"), group=True),
            wanted_mode, check_mode)
        if state == "absent":
            return _add_path_info(_ensure_absent(path, check_mode))
        if state == "directory":
            return _add_path_info(_ensure_directory(
                path, args.get("recurse", False), attributes, check_mode))
        # Touch and link perform their initial operation on the validated
        # path, then load attributes and apply timestamps on a second path.
        # Let Ansible handle split targets, including touch rollback and
        # symlink following, before either path has been modified.
        if os.path.expanduser(os.path.expandvars(path)) != path:
            return _FALLBACK
        if state == "touch":
            preserve_times = (
                args.get("modification_time") == "preserve"
                and args.get("access_time") == "preserve"
            )
            return _add_path_info(_execute_touch(
                path, preserve_times, attributes, check_mode, options))
        return _ensure_link(args, path, check_mode)
    except _Fallback:
        return _FALLBACK
    except Exception as ex:
        return _add_path_info({
            "failed": True, "changed": False, "path": path, "msg": str(ex)
        })


def _ensure_link(args, path, check_mode):
    source = os.path.expanduser(os.path.expandvars(args["src"]))
    exists = os.path.lexists(path)
    force = args.get("force", False)
    source_base = (
        path if os.path.isdir(path) and not os.path.islink(path)
        else os.path.dirname(path)
    )
    resolved_source = os.path.join(source_base, source)
    if not force and not os.path.exists(resolved_source):
        return _add_path_info({
            "failed": True, "changed": False, "path": path,
            "src": source,
            "msg": (
                "src file does not exist, use 'force=yes' if you "
                "really want to create the link: %s" % resolved_source
            ),
        })
    is_link = os.path.islink(path)
    if is_link and os.readlink(path) == source:
        return _add_path_info({"changed": False, "dest": path, "src": source})
    is_directory = exists and os.path.isdir(path) and not is_link
    if exists and not is_link and not force:
        return _add_path_info({
            "failed": True, "changed": False, "path": path,
            "msg": "refusing to convert existing destination to symlink",
        })
    if is_directory and os.listdir(path):
        return _add_path_info({
            "failed": True, "changed": False, "path": path,
            "msg": "the directory is not empty, refusing to convert it",
        })
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
    return _add_path_info({"changed": True, "dest": path, "src": source})
