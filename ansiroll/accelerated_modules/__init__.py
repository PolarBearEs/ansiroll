"""Small, stdlib-only module implementations with explicit capability gates."""

from __future__ import absolute_import

import locale

from . import (
    apt, blockinfile, command, file, find, group, replace, service, slurp,
    stat, systemd, user,
)

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


_LOCALE_CHECKED = []


def check_locale():
    """AnsibleModule._check_locale(): official modules (and the ansible CLI)
    call setlocale(LC_ALL, ''), so strerror() texts and strftime() names
    follow the user's locale. Accelerated modules run in-process, so apply it
    once here."""
    if _LOCALE_CHECKED:
        return
    _LOCALE_CHECKED.append(True)
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        # The official fallback is the best parsable locale (C.utf8, C, ...),
        # which renders the same English messages as the untouched C locale.
        pass


def run(name, args, check_mode=False, environment=None, options=None):
    check_locale()
    short_name = str(name).split(".")[-1]
    implementation = _IMPLEMENTATIONS[short_name]
    return implementation.run(
        args or {}, check_mode=bool(check_mode), shell=short_name == "shell",
        environment=environment, options=options or {},
    )
