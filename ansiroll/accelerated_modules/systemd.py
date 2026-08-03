"""Conservative systemctl adapter matching the common systemd module contract."""

from __future__ import absolute_import

import os
import shutil
import subprocess


_NAME_KEYS = ("name", "service", "unit")
_RELOAD_KEYS = ("daemon_reload", "daemon-reload")
_ALLOWED = frozenset(_NAME_KEYS + _RELOAD_KEYS + ("state", "enabled"))
_STATES = frozenset(("started", "stopped", "restarted"))
_UNIT_SUFFIXES = (
    ".service", ".socket", ".timer", ".mount", ".path", ".target",
    ".slice", ".scope", ".automount", ".swap", ".device",
)


def _single(args, keys, default=None):
    present = [key for key in keys if key in args]
    if len(present) > 1:
        raise ValueError("conflicting aliases")
    return args[present[0]] if present else default


def can_accelerate(args, check_mode=False, shell=False):
    if check_mode or not isinstance(args, dict) or not set(args).issubset(_ALLOWED):
        return False
    try:
        name = _single(args, _NAME_KEYS)
        daemon_reload = _single(args, _RELOAD_KEYS, False)
    except ValueError:
        return False
    state = args.get("state")
    enabled = args.get("enabled")
    if name is not None and (not isinstance(name, str) or any(
            marker in name for marker in ("*", "?", "[", "\n"))):
        return False
    if state is not None and state not in _STATES:
        return False
    if enabled is not None and not isinstance(enabled, bool):
        return False
    if not isinstance(daemon_reload, bool):
        return False
    if state is None and enabled is None and not daemon_reload:
        return False
    if (state is not None or enabled is not None) and name is None:
        return False
    # Official systemd reloads before probing the named unit.  Only do that for
    # explicit systemd unit names; ambiguous SysV-style names use the official
    # module so a fallback can occur before any mutation.
    if daemon_reload and name and not name.endswith(_UNIT_SUFFIXES):
        return False
    return True


def _command(argv, environment):
    process_env = os.environ.copy()
    if environment:
        process_env.update({str(key): str(value) for key, value in environment.items()})
    process_env.setdefault("XDG_RUNTIME_DIR", "/run/user/%s" % os.geteuid())
    try:
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=process_env
        )
        stdout, stderr = process.communicate()
        return (
            process.returncode,
            stdout.decode("utf-8", "surrogateescape"),
            stderr.decode("utf-8", "surrogateescape"),
        )
    except Exception as error:
        return 1, "", str(error)


def _parse_show(text):
    parsed = {}
    multiple = []
    key = None
    for line in text.splitlines():
        if key is None:
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.startswith("Exec") and value.lstrip().startswith("{"):
                if not value.rstrip().endswith("}"):
                    multiple.append(value)
                    continue
            parsed[key] = value.strip()
            key = None
        else:
            multiple.append(line)
            if line.rstrip().endswith("}"):
                parsed[key] = "\n".join(multiple).strip()
                multiple = []
                key = None
    return parsed


def _show_is_ignored(stdout, stderr):
    if "=" in stdout:
        return False
    combined = stdout + stderr
    return "ignoring request" in combined or "ignoring command" in combined


def _failed(message, status=None):
    result = {"failed": True, "msg": message}
    if status is not None:
        result["status"] = status
    return result


def run(args, check_mode=False, shell=False, environment=None, options=None):
    name = _single(args, _NAME_KEYS)
    daemon_reload = _single(args, _RELOAD_KEYS, False)
    state = args.get("state")
    wanted_enabled = args.get("enabled")
    search_path = (environment or {}).get("PATH")
    executable = shutil.which("systemctl", path=search_path)
    if not executable:
        return {"_ansiroll_fallback": True}

    result = {"name": name, "changed": False, "status": {}}
    # A fallback must happen before daemon-reload mutates the manager.  Probe
    # named units first so a missing unit can safely use the official module
    # without performing daemon-reload twice.
    if daemon_reload and name is not None:
        probe_rc, probe_out, probe_err = _command(
            [executable, "show", name], environment
        )
        probe_status = _parse_show(probe_out)
        if (probe_rc != 0 or _show_is_ignored(probe_out, probe_err)
                or probe_status.get("LoadState") in (None, "not-found")):
            return {"_ansiroll_fallback": True}
    if daemon_reload:
        returncode, _, stderr = _command([executable, "daemon-reload"], environment)
        if returncode != 0:
            return _failed(
                "failure %d during daemon-reload: %s" % (returncode, stderr)
            )

    if name is None:
        return result

    returncode, stdout, stderr = _command([executable, "show", name], environment)
    if returncode != 0 or _show_is_ignored(stdout, stderr):
        if daemon_reload:
            return _failed("Could not find the requested service %s: host" % name)
        return {"_ansiroll_fallback": True}
    status = _parse_show(stdout)
    result["status"] = status
    if status.get("LoadState") in (None, "not-found"):
        if daemon_reload:
            return _failed("Could not find the requested service %s: host" % name)
        return {"_ansiroll_fallback": True}
    if status.get("LoadState") != "masked" and "LoadError" in status:
        return _failed(
            "Error loading unit file '%s': %s" % (name, status["LoadError"]),
        )

    if wanted_enabled is not None:
        command = [executable, "is-enabled", name]
        modern = bool((options or {}).get("modern_is_enabled"))
        if modern:
            command.append("-l")
        enabled_rc, enabled_out, _ = _command(command, environment)
        current_enabled = enabled_rc == 0
        if modern and enabled_out.rstrip() in (
                "enabled-runtime", "indirect", "alias"):
            current_enabled = False
        result["enabled"] = current_enabled
        if current_enabled != wanted_enabled:
            result["changed"] = True
            action = "enable" if wanted_enabled else "disable"
            action_rc, action_out, action_err = _command(
                [executable, action, name], environment
            )
            if action_rc != 0:
                return _failed(
                    "Unable to %s service %s: %s"
                    % (action, name, action_out + action_err),
                )
            result["enabled"] = wanted_enabled

    if state is not None:
        result["state"] = state
        active_state = status.get("ActiveState")
        running = active_state in ("active", "activating")
        deactivating = active_state == "deactivating"
        action = None
        if state == "started":
            if not running:
                action = "start"
        elif state == "stopped":
            if running or deactivating:
                action = "stop"
        elif not running:
            action = "start"
            result["state"] = "started"
        else:
            action = "restart"
            result["state"] = "started"
        if action:
            result["changed"] = True
            action_rc, _, action_err = _command(
                [executable, action, name], environment
            )
            if action_rc != 0:
                return _failed(
                    "Unable to %s service %s: %s" % (action, name, action_err),
                )
    return result
