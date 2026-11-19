"""Conservative accelerated subset of ansible.builtin.apt package removal."""

from __future__ import absolute_import

import os
import re
import shutil
import subprocess

from . import _stdin


_PACKAGE_KEYS = ("name", "package", "pkg")
_ALLOWED = frozenset(_PACKAGE_KEYS + (
    "state", "purge", "autoremove", "autoclean",
))
_PACKAGE_RE = re.compile(
    r"^[a-z0-9][a-z0-9+.-]*(?::[a-z0-9][a-z0-9-]*)?$"
)
_MISSING_PREFIX = "dpkg-query: no packages found matching "


def _packages(args):
    keys = [key for key in _PACKAGE_KEYS if key in args]
    if len(keys) != 1:
        raise ValueError("exactly one package-name alias is required")
    value = args[keys[0]]
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise ValueError("packages must be a string or list")
    if not values or not all(
            isinstance(name, str) and _PACKAGE_RE.match(name)
            for name in values):
        raise ValueError("only explicit Debian package names are supported")
    # Preserve the user's order on Python 3.5 while avoiding repeated
    # apt/dpkg arguments.
    unique = []
    seen = set()
    for name in values:
        if name not in seen:
            unique.append(name)
            seen.add(name)
    return unique


def can_accelerate(args, check_mode=False, shell=False):
    if not isinstance(args, dict) or not set(args).issubset(_ALLOWED):
        return False
    if args.get("state", "present") != "absent":
        return False
    if any(
            key in args and not isinstance(args[key], bool)
            for key in ("purge", "autoremove", "autoclean")):
        return False
    try:
        _packages(args)
    except (TypeError, ValueError):
        return False
    return True


def _environment(environment, force_c_locale=False):
    result = os.environ.copy()
    if environment:
        result.update({str(key): str(value) for key, value in environment.items()})
    if force_c_locale:
        result["LC_ALL"] = "C"
        result["LANG"] = "C"
    return result


def _command(argv, environment=None, force_c_locale=False):
    try:
        process = _stdin.popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_environment(environment, force_c_locale),
        )
        stdout, stderr = process.communicate()
        return (
            process.returncode,
            stdout.decode("utf-8", "surrogateescape"),
            stderr.decode("utf-8", "surrogateescape"),
        )
    except Exception as error:
        return None, "", str(error)


def _requested_name(binary_name, requested):
    if binary_name in requested:
        return binary_name
    base = binary_name.split(":", 1)[0]
    return base if base in requested else None


def _query_states(executable, packages, environment):
    command = [
        executable,
        "-W",
        "-f=${binary:Package}\\t${db:Status-Abbrev}\\n",
    ] + packages
    returncode, stdout, stderr = _command(
        command, environment, force_c_locale=True
    )
    if returncode not in (0, 1):
        return None
    requested = set(packages)
    states = {name: "absent" for name in packages}
    for line in stdout.splitlines():
        try:
            binary_name, abbreviation = line.split("\t", 1)
        except ValueError:
            return None
        name = _requested_name(binary_name, requested)
        abbreviation = abbreviation.strip()
        if name is None or len(abbreviation) < 2:
            return None
        state = abbreviation[-1]
        if state == "i":
            states[name] = "installed"
        elif state == "c":
            states[name] = "config-files"
        elif state != "n":
            # Half-configured/unpacked and other unusual dpkg states use the
            # official module's apt cache interpretation.
            return None
    for line in stderr.splitlines():
        if line and not line.startswith(_MISSING_PREFIX):
            return None
    return states


def _apt_remove_command(executable, packages, args, check_mode):
    command = [
        executable,
        "-q",
        "-y",
        "-o",
        "Dpkg::Options::=--force-confdef",
        "-o",
        "Dpkg::Options::=--force-confold",
    ]
    if args.get("purge"):
        command.append("--purge")
    if args.get("autoremove"):
        command.append("--auto-remove")
    if check_mode:
        command.append("--simulate")
    command.append("remove")
    command.extend(packages)
    return command


def run(args, check_mode=False, shell=False, environment=None, options=None):
    try:
        packages = _packages(args)
    except (TypeError, ValueError):
        return {"_ansiroll_fallback": True}
    search_path = (environment or {}).get("PATH")
    dpkg_query = shutil.which("dpkg-query", path=search_path)
    apt_get = shutil.which("apt-get", path=search_path)
    if not dpkg_query or not apt_get:
        return {"_ansiroll_fallback": True}

    states = _query_states(dpkg_query, packages, environment)
    if states is None:
        return {"_ansiroll_fallback": True}
    purge = bool(args.get("purge"))
    remove = [
        name for name in packages
        if states[name] == "installed"
        or (purge and states[name] == "config-files")
    ]
    if not remove:
        return {"changed": False}

    command = _apt_remove_command(apt_get, remove, args, check_mode)
    returncode, stdout, stderr = _command(command, environment)
    if returncode is None:
        # The process may have started before communication failed. Never
        # fall back after a potentially mutating command: that could apply a
        # non-idempotent package transition twice.
        return {
            "failed": True,
            "changed": False,
            "stdout": stdout,
            "stderr": stderr,
            "msg": "accelerated apt removal outcome is unknown: %s" % stderr,
        }
    if returncode != 0:
        return {
            "failed": True,
            "changed": False,
            "rc": returncode,
            "stdout": stdout,
            "stderr": stderr,
            "msg": "'apt-get remove %s' failed: %s"
                   % (" ".join(remove), stderr),
        }
    # ansible.builtin.apt's autoclean option is not executed independently
    # when an explicit package list is supplied.
    return {
        "changed": True,
        "stdout": stdout,
        "stderr": stderr,
        "diff": {},
    }
