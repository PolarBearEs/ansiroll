"""Standard input that official Ansible modules (and their children) see.

Connection plugins start each module with a pipe on stdin and close its write
end after sending any pipelined payload (``ansible/plugins/connection/local.py``
``Popen(stdin=PIPE)`` + ``communicate(in_data)``). ``AnsibleModule.run_command``
passes ``stdin=None`` unless ``data`` is given, so commands inherit that
already-closed pipe and read EOF immediately.
"""

from __future__ import absolute_import

import os
import subprocess


def eof_pipe():
    """Return the read end of a pipe whose write end is already closed."""
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    return read_fd


def popen(*args, **kwargs):
    """``subprocess.Popen`` whose child reads EOF unless ``stdin`` is given."""
    if kwargs.get("stdin") is not None:
        return subprocess.Popen(*args, **kwargs)
    read_fd = eof_pipe()
    try:
        kwargs["stdin"] = read_fd
        return subprocess.Popen(*args, **kwargs)
    finally:
        os.close(read_fd)
