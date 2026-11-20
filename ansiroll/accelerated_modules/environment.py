"""Task environment and locale for modules running in a persistent process."""

from __future__ import absolute_import

from contextlib import contextmanager
import locale
import os
import subprocess


def best_parsable_locale(environment=None):
    """Select a locale with English messages as Ansible's locale helper does."""
    env = os.environ.copy()
    env.update({str(key): str(value) for key, value in (environment or {}).items()})
    paths = env.get("PATH", "").split(os.pathsep)
    paths.extend(("/sbin", "/usr/sbin", "/usr/local/sbin"))
    for directory in filter(None, paths):
        executable = os.path.join(directory, "locale")
        if os.path.isfile(executable) and os.access(executable, os.X_OK):
            break
    else:
        return "C"
    try:
        available = subprocess.check_output(
            [executable, "-a"], stderr=subprocess.PIPE, env=env).splitlines()
    except (OSError, subprocess.CalledProcessError):
        return "C"
    for preferred in ("C.utf8", "C.UTF-8", "en_US.utf8", "en_US.UTF-8", "C", "POSIX"):
        if preferred.encode("ascii") in available:
            return preferred
    return "C"


@contextmanager
def _temporary_environment(overrides):
    previous = {key: os.environ.get(key) for key in overrides}
    try:
        os.environ.update(overrides)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def module_environment(environment, legacy_locale=False):
    """Apply AnsibleModule._check_locale and restore the agent after the task."""
    overrides = {str(key): str(value) for key, value in (environment or {}).items()}
    previous_locale = locale.setlocale(locale.LC_ALL)
    try:
        with _temporary_environment(overrides):
            try:
                locale.setlocale(locale.LC_ALL, "")
            except locale.Error:
                best = "C" if legacy_locale else best_parsable_locale()
                locale.setlocale(locale.LC_ALL, best)
                fallback = dict.fromkeys(("LANG", "LC_ALL", "LC_MESSAGES"), best)
                overrides.update(fallback)
                with _temporary_environment(fallback):
                    yield overrides
            else:
                yield overrides or None
    finally:
        locale.setlocale(locale.LC_ALL, previous_locale)
