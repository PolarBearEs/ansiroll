import io
import time
from types import SimpleNamespace

from ansible.module_utils.facts.system import date_time as official_date_time
from ansible.release import __version__ as ansible_version

from ansiroll.accelerated_modules import facts


def test_fast_facts_collect_required_values():
    result = facts.run()
    assert not result.get("_ansiroll_fallback")
    gathered = result["ansible_facts"]
    assert gathered["ansible_distribution"]
    assert gathered["ansible_distribution_major_version"]
    assert gathered["ansible_distribution_version"]
    assert gathered["ansible_kernel"]
    assert gathered["ansible_date_time"]["iso8601"].endswith("Z")
    assert gathered["ansible_mounts"][0]["mount"] == "/"
    assert gathered["ansible_mounts"][0]["size_available"] >= 0


def test_fast_facts_report_fallback_when_release_is_unavailable(monkeypatch):
    def unavailable():
        raise IOError("missing")

    monkeypatch.setattr(facts, "_release_values", unavailable)
    result = facts.run()
    assert result["_ansiroll_fallback"] is True
    assert "missing" in result["msg"]


def test_debian_version_uses_os_release_id_not_point_release(monkeypatch):
    files = {
        "/etc/os-release": (
            'PRETTY_NAME="Debian GNU/Linux 10 (buster)"\n'
            'VERSION_ID="10"\nVERSION_CODENAME=buster\nID=debian\n'
        ),
        "/etc/debian_version": "10.13\n",
    }

    monkeypatch.setattr(
        "builtins.open",
        lambda path, mode="r": io.StringIO(files[path]),
    )
    assert facts._release_values() == ("Debian", "10", "buster")


def test_date_time_matches_compiling_ansible_contract(monkeypatch):
    epoch = 1594449296.123456
    real_strftime = official_date_time.time.strftime
    monkeypatch.setattr(official_date_time.time, "time", lambda: epoch)
    # The collector reads tz/tz_offset via time.strftime() at the *real*
    # current time; freeze it at the same epoch so DST cannot differ.
    monkeypatch.setattr(
        official_date_time.time, "strftime",
        lambda fmt, t=None: real_strftime(
            fmt, time.localtime(epoch) if t is None else t))
    official = official_date_time.DateTimeFactCollector().collect()["date_time"]
    accelerated = facts._date_time(
        fact_schema=".".join(ansible_version.split(".")[:2]),
        epoch_ts=epoch,
    )
    assert accelerated == official


def test_legacy_date_schema_omits_post_210_fields():
    gathered = facts._date_time(fact_schema="2.10", epoch_ts=1594449296.123456)
    assert "epoch_int" not in gathered
    assert "tz_dst" not in gathered
    assert gathered["iso8601_micro"].endswith("Z")


def test_modern_date_schema_adds_epoch_int_and_tz_dst():
    gathered = facts._date_time(fact_schema="2.12", epoch_ts=1594449296.123456)
    assert gathered["epoch_int"] == gathered["epoch"]
    assert gathered["tz_dst"] == facts.time.tzname[1]


def test_mount_size_matches_ansible_fields(monkeypatch):
    stat = SimpleNamespace(
        f_frsize=4096,
        f_bsize=4096,
        f_blocks=100,
        f_bavail=25,
        f_files=40,
        f_favail=10,
    )
    monkeypatch.setattr(facts.os, "statvfs", lambda _mount: stat)
    assert facts._mount_size("/") == {
        "size_total": 409600,
        "size_available": 102400,
        "block_size": 4096,
        "block_total": 100,
        "block_available": 25,
        "block_used": 75,
        "inode_total": 40,
        "inode_available": 10,
        "inode_used": 30,
    }


def test_mounts_keep_only_device_backed_filesystems(monkeypatch):
    mounts_file = io.StringIO(
        "/dev/root / ext4 rw,noatime 0 0\n"
        "tmpfs /run tmpfs rw,nosuid 0 0\n"
        "server:/data /mnt/data nfs rw 0 0\n"
        "none /ignored none rw 0 0\n"
    )
    monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: mounts_file)
    monkeypatch.setattr(facts, "_mount_uuids", lambda: {"/dev/root": "root-id"})
    monkeypatch.setattr(
        facts, "_mount_size",
        lambda _mount: {
            "size_total": 1, "size_available": 1, "block_size": 1,
            "block_total": 1, "block_available": 1, "block_used": 0,
            "inode_total": 1, "inode_available": 1, "inode_used": 0,
        },
    )
    gathered = facts._mounts()
    assert [item["mount"] for item in gathered] == ["/", "/mnt/data"]
    assert gathered[0]["uuid"] == "root-id"
    assert gathered[1]["uuid"] == "N/A"
