"""Small, stdlib-only module implementations with explicit capability gates."""

from __future__ import absolute_import

from . import (
    apt, blockinfile, command, file, find, group, replace, service, slurp,
    stat, systemd, user,
)
from .environment import module_environment

_IMPLEMENTATIONS = {
    "apt": apt,
    "blockinfile": blockinfile,
    "command": command,
    "shell": command,
    "file": file,
    "find": find,
    "group": group,
    "replace": replace,
    "service": service,
    "slurp": slurp,
    "stat": stat,
    "systemd": systemd,
    "systemd_service": systemd,
    "user": user,
}

NAMES = frozenset(_IMPLEMENTATIONS)

# These implementations fully own every outcome after their capability gate:
# they support check mode and never ask Runtime to fall back based on host state.
# The compiler may omit the official module only when every call of an action is
# statically classified into this set and is not asynchronous.
TOTAL_NAMES = frozenset((
    "blockinfile",
    "command",
    "shell",
    "find",
    "group",
    "replace",
    "slurp",
    "stat",
    "user",
))


def can_accelerate(name, args, check_mode=False):
    implementation = _IMPLEMENTATIONS.get(str(name).split(".")[-1])
    return bool(implementation and implementation.can_accelerate(
        args or {}, check_mode=bool(check_mode),
        shell=str(name).split(".")[-1] == "shell",
    ))


def run(name, args, check_mode=False, environment=None, options=None):
    short_name = str(name).split(".")[-1]
    implementation = _IMPLEMENTATIONS[short_name]
    with module_environment(environment) as module_env:
        return implementation.run(
            args or {}, check_mode=bool(check_mode), shell=short_name == "shell",
            environment=module_env, options=options or {},
        )
