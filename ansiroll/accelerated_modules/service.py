"""Conservative systemd-backed subset of ansible.builtin.service."""

from __future__ import absolute_import

from . import systemd


_ALLOWED = frozenset(("name", "state", "enabled"))


def can_accelerate(args, check_mode=False, shell=False):
    if not isinstance(args, dict) or not set(args).issubset(_ALLOWED):
        return False
    return systemd.can_accelerate(args, check_mode=check_mode)


def run(args, check_mode=False, shell=False, environment=None, options=None):
    # systemd.run probes the unit before making a change and requests an
    # official-module fallback when systemctl is unavailable or the name is
    # not a systemd unit. This keeps the generic service action safe on hosts
    # that still use another service manager.
    return systemd.run(
        args,
        check_mode=check_mode,
        environment=environment,
        options=options,
    )
