"""Conservative, result-compatible subset of ansible.builtin.stat."""

from __future__ import absolute_import

import errno
import grp
import hashlib
import os
import pwd
import shutil
import stat as stat_module
import subprocess

from . import _stdin


_PATH_KEYS = ("path", "dest", "name")
_MIME_KEYS = ("get_mime", "mime", "mime_type", "mime-type")
_ATTRIBUTE_KEYS = ("get_attributes", "attr", "attributes")
_CHECKSUM_KEYS = ("checksum_algorithm", "checksum", "checksum_algo")
_ALLOWED = frozenset(
    _PATH_KEYS + _MIME_KEYS + _ATTRIBUTE_KEYS + _CHECKSUM_KEYS + (
        "follow", "get_md5", "get_checksum",
    )
)
_ALGORITHMS = frozenset(("md5", "sha1", "sha224", "sha256", "sha384", "sha512"))
_FILE_ATTRIBUTES = {
    "A": "noatime", "a": "append", "c": "compressed", "C": "nocow",
    "d": "nodump", "D": "dirsync", "e": "extents", "E": "encrypted",
    "h": "blocksize", "i": "immutable", "I": "indexed", "j": "journalled",
    "N": "inline", "s": "zero", "S": "synchronous", "t": "notail",
    "T": "blockroot", "u": "undelete", "X": "compressedraw",
    "Z": "compresseddirty",
}


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
        follow = _single(args, ("follow",), False)
        get_md5 = _single(args, ("get_md5",), False)
        get_checksum = _single(args, ("get_checksum",), True)
        get_mime = _single(args, _MIME_KEYS, True)
        get_attributes = _single(args, _ATTRIBUTE_KEYS, True)
        algorithm = _single(args, _CHECKSUM_KEYS, "sha1")
    except ValueError:
        return False
    if not isinstance(path, str) or not path or ":" in path or "\n" in path:
        return False
    if not all(isinstance(value, bool) for value in (
            follow, get_md5, get_checksum, get_mime, get_attributes)):
        return False
    return isinstance(algorithm, str) and algorithm in _ALGORITHMS


def _digest(path, algorithm):
    digest = hashlib.new(algorithm)
    with open(path, "rb") as source:
        while True:
            chunk = source.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _command(command):
    try:
        process = _stdin.popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        stdout, stderr = process.communicate()
        return process.returncode, stdout.decode("utf-8", "surrogateescape"), stderr
    except Exception:
        return 1, "", b""


def _format(path, value, options):
    mode = value.st_mode
    output = {
        "exists": True,
        "path": path,
        "mode": "%04o" % stat_module.S_IMODE(mode),
        "isdir": stat_module.S_ISDIR(mode),
        "ischr": stat_module.S_ISCHR(mode),
        "isblk": stat_module.S_ISBLK(mode),
        "isreg": stat_module.S_ISREG(mode),
        "isfifo": stat_module.S_ISFIFO(mode),
        "islnk": stat_module.S_ISLNK(mode),
        "issock": stat_module.S_ISSOCK(mode),
        "uid": value.st_uid,
        "gid": value.st_gid,
        "size": value.st_size,
        "inode": value.st_ino,
        "dev": value.st_dev,
        "nlink": value.st_nlink,
        "atime": value.st_atime,
        "mtime": value.st_mtime,
        "ctime": value.st_ctime,
        "wusr": bool(mode & stat_module.S_IWUSR),
        "rusr": bool(mode & stat_module.S_IRUSR),
        "xusr": bool(mode & stat_module.S_IXUSR),
        "wgrp": bool(mode & stat_module.S_IWGRP),
        "rgrp": bool(mode & stat_module.S_IRGRP),
        "xgrp": bool(mode & stat_module.S_IXGRP),
        "woth": bool(mode & stat_module.S_IWOTH),
        "roth": bool(mode & stat_module.S_IROTH),
        "xoth": bool(mode & stat_module.S_IXOTH),
        "isuid": bool(mode & stat_module.S_ISUID),
        "isgid": bool(mode & stat_module.S_ISGID),
    }
    for source, target in (
            ("st_blocks", "blocks"), ("st_blksize", "block_size"),
            ("st_rdev", "device_type"), ("st_flags", "flags"),
            ("st_gen", "generation"), ("st_birthtime", "birthtime"),
            ("st_ftype", "file_type"), ("st_attrs", "attrs"),
            ("st_obtype", "object_type"), ("st_rsize", "real_size"),
            ("st_creator", "creator"), ("st_type", "file_type")):
        if hasattr(value, source):
            output[target] = getattr(value, source)
            if source == "st_blocks" and options.get("disk_usage_bytes"):
                output["disk_usage_bytes"] = value.st_blocks * 512
    return output


def run(args, check_mode=False, shell=False, environment=None, options=None):
    path = _single(args, _PATH_KEYS)
    follow = _single(args, ("follow",), False)
    get_md5 = _single(args, ("get_md5",), False)
    get_checksum = _single(args, ("get_checksum",), True)
    get_mime = _single(args, _MIME_KEYS, True)
    get_attributes = _single(args, _ATTRIBUTE_KEYS, True)
    algorithm = _single(args, _CHECKSUM_KEYS, "sha1")
    try:
        value = os.stat(path) if follow else os.lstat(path)
    except OSError as error:
        if error.errno == errno.ENOENT:
            return {"changed": False, "stat": {"exists": False}}
        return {"failed": True, "changed": False, "msg": error.strerror}

    output = _format(path, value, options or {})
    output["readable"] = os.access(path, os.R_OK)
    output["writeable"] = os.access(path, os.W_OK)
    output["executable"] = os.access(path, os.X_OK)
    if output["islnk"]:
        output["lnk_source"] = os.path.realpath(path)
        output["lnk_target"] = os.readlink(path)
    try:
        output["pw_name"] = pwd.getpwuid(value.st_uid).pw_name
    except (TypeError, KeyError):
        pass
    try:
        output["gr_name"] = grp.getgrgid(value.st_gid).gr_name
    except (KeyError, ValueError, OverflowError):
        pass

    if output["isreg"] and output["readable"]:
        if get_md5:
            try:
                output["md5"] = _digest(path, "md5")
            except ValueError:
                output["md5"] = None
        if get_checksum:
            output["checksum"] = _digest(path, algorithm)

    if get_mime:
        output["mimetype"] = output["charset"] = "unknown"
        executable = shutil.which("file")
        if executable:
            returncode, stdout, _ = _command([executable, "-i", path])
            if returncode == 0:
                try:
                    details = stdout.split(":")[1].split(";")
                    output["mimetype"] = details[0].strip()
                    output["charset"] = details[1].split("=")[1].strip()
                except Exception:
                    pass

    if get_attributes:
        output.update({"version": None, "attributes": [], "attr_flags": ""})
        executable = shutil.which("lsattr")
        if executable:
            returncode, stdout, _ = _command([executable, "-vd", path])
            if returncode == 0:
                try:
                    fields = stdout.split()
                    flags = fields[1].replace("-", "").strip()
                    output["attr_flags"] = flags
                    output["version"] = fields[0].strip()
                    output["attributes"] = [
                        _FILE_ATTRIBUTES[flag] for flag in flags
                        if flag in _FILE_ATTRIBUTES
                    ]
                except Exception:
                    pass
    return {"changed": False, "stat": output}
