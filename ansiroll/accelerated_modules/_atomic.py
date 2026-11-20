"""Attribute copying for accelerated modules that use Ansible's atomic_move."""

import errno
import os
import shutil
import stat
import time


def copy_replacement_attributes(path, temporary, original, chmod_before_chown):
    """Match the attribute order used by the compiling Ansible version."""
    if chmod_before_chown:
        os.chmod(temporary, stat.S_IMODE(original.st_mode))
    os.chown(temporary, original.st_uid, original.st_gid)
    if chmod_before_chown:
        if hasattr(os, "chflags") and hasattr(original, "st_flags"):
            try:
                os.chflags(temporary, original.st_flags)
            except OSError as error:
                if error.errno not in (getattr(errno, "ENOTSUP", None),
                                       getattr(errno, "EOPNOTSUPP", None)):
                    raise
    else:
        shutil.copystat(path, temporary)
        now = time.time()
        os.utime(temporary, (now, now))
