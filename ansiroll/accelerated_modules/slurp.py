"""Conservative, stdlib-only implementation of ansible.builtin.slurp."""

from __future__ import absolute_import

import base64
import errno


_SOURCE_KEYS = ("src", "path")
_ALLOWED = frozenset(_SOURCE_KEYS + ("armor",))


def _source(args):
    present = [key for key in _SOURCE_KEYS if key in args]
    if len(present) != 1:
        raise ValueError("missing or conflicting source")
    return args[present[0]]


def can_accelerate(args, check_mode=False, shell=False):
    if not isinstance(args, dict) or not set(args).issubset(_ALLOWED):
        return False
    try:
        source = _source(args)
    except ValueError:
        return False
    # ``armor=False`` returns arbitrary bytes through Ansible's JSON encoder.
    # Keep that uncommon form on the official module rather than changing its
    # version-dependent serialization behavior.
    armor = args.get("armor", True)
    return (
        isinstance(source, str)
        and bool(source)
        and "\0" not in source
        and isinstance(armor, bool)
        and armor
    )


def run(args, check_mode=False, shell=False, environment=None, options=None):
    source = _source(args)
    try:
        with open(source, "rb") as source_file:
            content = base64.b64encode(source_file.read()).decode("ascii")
    except OSError as error:
        if error.errno == errno.ENOENT:
            message = "%sile not found: %s" % (
                "f" if (options or {}).get("lowercase_not_found") else "F",
                source,
            )
        elif error.errno == errno.EACCES:
            message = "File is not readable: %s" % source
        elif error.errno == errno.EISDIR:
            message = "Source is a directory and must be a file: %s" % source
        else:
            message = (
                "Unable to slurp file: {source}"
                if (options or {}).get("typed_exceptions")
                else "Unable to slurp file: %s" % source
            )
        result = {"failed": True, "msg": message}
        if (options or {}).get("typed_exceptions"):
            result["exception"] = {
                "event": {
                    "msg": message,
                    "chain": {
                        "msg_reason": "<<< caused by >>>",
                        "traceback_reason": (
                            "The above exception was the direct cause of the "
                            "following error:"
                        ),
                        "event": {
                            "msg": str(error),
                            "__ansible_type": "Event",
                        },
                        "follow": True,
                        "__ansible_type": "EventChain",
                    },
                    "__ansible_type": "Event",
                },
                "__ansible_type": "ErrorSummary",
            }
        return result
    return {
        "content": content,
        "source": source,
        "encoding": "base64",
    }
