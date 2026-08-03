"""Small fact collector for explicitly opted-in, latency-sensitive agents.

This intentionally covers a small, stable set of commonly used facts.  It is not
a drop-in replacement for every subset supported by ``ansible.builtin.setup``;
callers must keep the official module as the fallback.
"""

from __future__ import absolute_import

import datetime
import os
import platform
import re
import time


_OCTAL_ESCAPE_RE = re.compile(r"\\[0-7]{3}")


def _release_values():
    values = {}
    with open("/etc/os-release", "r") as release_file:
        for raw_line in release_file:
            key, separator, value = raw_line.rstrip("\n").partition("=")
            if separator:
                values[key] = value.strip().strip('"')
    distribution_id = values.get("ID", "").lower()
    names = {"debian": "Debian", "raspbian": "Debian", "ubuntu": "Ubuntu"}
    distribution = names.get(distribution_id)
    # Ansible's Debian collector keeps VERSION_ID as the distribution version.
    # /etc/debian_version often contains a point release (10.13) while setup
    # reports the release ID (10) on a normal Debian 10 installation.
    version = values.get("VERSION_ID", "")
    release = values.get("VERSION_CODENAME", "")
    if not version and distribution_id in ("debian", "raspbian"):
        try:
            with open("/etc/debian_version", "r") as version_file:
                detected = version_file.read().strip()
            if detected[:1].isdigit():
                version = detected
        except (IOError, OSError):
            pass
    if not release:
        pretty = values.get("PRETTY_NAME", "")
        match = re.search(r"\(([^)]+)\)", pretty)
        if match:
            release = match.group(1)
    if not distribution or not version:
        raise ValueError("distribution could not be identified")
    return distribution, version, release


def _unescape_mount(value):
    return _OCTAL_ESCAPE_RE.sub(
        lambda match: chr(int(match.group(0)[1:], 8)), value)


def _mount_uuids():
    uuids = {}
    directory = "/dev/disk/by-uuid"
    try:
        names = os.listdir(directory)
    except (IOError, OSError):
        return uuids
    for name in names:
        path = os.path.join(directory, name)
        try:
            target = os.path.realpath(path)
        except (IOError, OSError):
            continue
        uuids[target] = name
    return uuids


def _mount_size(mount):
    stat = os.statvfs(mount)
    fragment_size = stat.f_frsize or stat.f_bsize
    return {
        "size_total": fragment_size * stat.f_blocks,
        "size_available": fragment_size * stat.f_bavail,
        "block_size": stat.f_bsize,
        "block_total": stat.f_blocks,
        "block_available": stat.f_bavail,
        "block_used": stat.f_blocks - stat.f_bavail,
        "inode_total": stat.f_files,
        "inode_available": stat.f_favail,
        "inode_used": stat.f_files - stat.f_favail,
    }


def _mounts():
    mounts = []
    seen = set()
    uuids = _mount_uuids()
    with open("/proc/mounts", "r") as mounts_file:
        for line in mounts_file:
            fields = line.split()
            if len(fields) < 4:
                continue
            device, mount, fstype, options = fields[:4]
            device = _unescape_mount(device)
            mount = _unescape_mount(mount)
            if ((not device.startswith("/") and ":/" not in device)
                    or fstype == "none"):
                continue
            if mount in seen:
                continue
            try:
                mount_size = _mount_size(mount)
            except (IOError, OSError):
                continue
            seen.add(mount)
            mount_info = {
                "mount": mount,
                "device": device,
                "fstype": fstype,
                "options": options,
                "uuid": uuids.get(device, "N/A"),
            }
            mount_info.update(mount_size)
            mounts.append(mount_info)
    mounts.sort(key=lambda item: (item["mount"] != "/", item["mount"]))
    if not mounts or mounts[0]["mount"] != "/":
        raise ValueError("root filesystem could not be inspected")
    return mounts


def _modern_date_schema(fact_schema):
    try:
        major, minor = str(fact_schema or "2.10").split(".", 1)
        return (int(major), int(minor)) >= (2, 12)
    except (TypeError, ValueError):
        return False


def _date_time(fact_schema=None, epoch_ts=None):
    # Match Ansible by deriving local and UTC values from one timestamp.
    epoch_ts = time.time() if epoch_ts is None else epoch_ts
    now = datetime.datetime.fromtimestamp(epoch_ts)
    utc = datetime.datetime.fromtimestamp(
        epoch_ts, datetime.timezone.utc).replace(tzinfo=None)
    result = {
        "date": now.strftime("%Y-%m-%d"),
        "day": now.strftime("%d"),
        "epoch": str(int(epoch_ts)),
        "hour": now.strftime("%H"),
        "iso8601": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "iso8601_micro": utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "iso8601_basic": now.strftime("%Y%m%dT%H%M%S%f"),
        "iso8601_basic_short": now.strftime("%Y%m%dT%H%M%S"),
        "minute": now.strftime("%M"),
        "month": now.strftime("%m"),
        "second": now.strftime("%S"),
        "time": now.strftime("%H:%M:%S"),
        "tz": time.strftime("%Z", time.localtime(epoch_ts)),
        "tz_offset": time.strftime("%z", time.localtime(epoch_ts)),
        "weekday": now.strftime("%A"),
        "weekday_number": now.strftime("%w"),
        "weeknumber": now.strftime("%W"),
        "year": now.strftime("%Y"),
    }
    if _modern_date_schema(fact_schema):
        result["epoch_int"] = str(int(epoch_ts))
        result["tz_dst"] = time.tzname[1]
    return result


def run(fact_schema=None):
    try:
        distribution, version, release = _release_values()
        facts = {
            "ansible_date_time": _date_time(fact_schema=fact_schema),
            "ansible_distribution": distribution,
            "ansible_distribution_major_version": version.split(".", 1)[0],
            "ansible_distribution_release": release,
            "ansible_distribution_version": version,
            "ansible_kernel": platform.release(),
            "ansible_mounts": _mounts(),
        }
        return {"changed": False, "ansible_facts": facts}
    except Exception as ex:
        return {"_ansiroll_fallback": True,
                "msg": "fast fact collection unavailable: %s" % ex}
