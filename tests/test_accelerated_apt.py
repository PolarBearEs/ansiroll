import pytest

from ansiroll.accelerated_modules import apt


def test_capability_gate_accepts_explicit_absent_packages():
    assert apt.can_accelerate({
        "pkg": ["mosquitto", "openhab2"],
        "state": "absent",
        "purge": True,
        "autoclean": True,
        "autoremove": True,
    })
    assert apt.can_accelerate(
        {"name": "mosquitto", "state": "absent"},
        check_mode=True,
    )


@pytest.mark.parametrize("args", [
    {"pkg": ["mosquitto"], "state": "present"},
    {"pkg": ["mosquitto*"], "state": "absent"},
    {"pkg": ["mosquitto=2.0"], "state": "absent"},
    {"pkg": [], "state": "absent"},
    {"pkg": ["mosquitto"], "name": "openhab2", "state": "absent"},
    {"pkg": ["mosquitto"], "state": "absent", "purge": "yes"},
    {"pkg": ["mosquitto"], "state": "absent", "update_cache": True},
])
def test_capability_gate_rejects_ambiguous_or_unsupported_arguments(args):
    assert not apt.can_accelerate(args)


def _executables(monkeypatch):
    monkeypatch.setattr(
        apt.shutil,
        "which",
        lambda name, path=None: "/usr/bin/" + name,
    )


def test_absent_packages_return_without_running_apt_get(monkeypatch):
    _executables(monkeypatch)
    calls = []

    def command(argv, environment=None, force_c_locale=False):
        calls.append((argv, force_c_locale))
        assert argv[0] == "/usr/bin/dpkg-query"
        return (
            1,
            "",
            "dpkg-query: no packages found matching mosquitto\n"
            "dpkg-query: no packages found matching openhab2\n",
        )

    monkeypatch.setattr(apt, "_command", command)
    result = apt.run({
        "pkg": ["mosquitto", "openhab2"],
        "state": "absent",
        "purge": True,
        "autoclean": True,
        "autoremove": True,
    })

    assert result == {"changed": False}
    assert len(calls) == 1
    assert calls[0][1] is True


def test_config_files_are_purged_and_installed_packages_removed(monkeypatch):
    _executables(monkeypatch)
    calls = []

    def command(argv, environment=None, force_c_locale=False):
        calls.append(argv)
        if argv[0].endswith("dpkg-query"):
            return 0, "mosquitto\tii \nopenhab2\trc \n", ""
        return 0, "removed\n", ""

    monkeypatch.setattr(apt, "_command", command)
    result = apt.run({
        "pkg": ["mosquitto", "openhab2"],
        "state": "absent",
        "purge": True,
        "autoclean": True,
        "autoremove": True,
    })

    assert result == {
        "changed": True,
        "stdout": "removed\n",
        "stderr": "",
        "diff": {},
    }
    remove = calls[1]
    assert remove[0] == "/usr/bin/apt-get"
    assert "--purge" in remove
    assert "--auto-remove" in remove
    assert "autoclean" not in remove
    assert remove[-3:] == ["remove", "mosquitto", "openhab2"]


def test_config_files_are_already_absent_without_purge(monkeypatch):
    _executables(monkeypatch)
    monkeypatch.setattr(
        apt,
        "_command",
        lambda *_args, **_kwargs: (0, "openhab2\trc \n", ""),
    )

    assert apt.run({
        "pkg": ["openhab2"],
        "state": "absent",
        "purge": False,
    }) == {"changed": False}


def test_check_mode_simulates_a_required_removal(monkeypatch):
    _executables(monkeypatch)
    calls = []

    def command(argv, environment=None, force_c_locale=False):
        calls.append(argv)
        if argv[0].endswith("dpkg-query"):
            return 0, "mosquitto\tii \n", ""
        return 0, "simulation\n", ""

    monkeypatch.setattr(apt, "_command", command)
    result = apt.run(
        {"pkg": ["mosquitto"], "state": "absent"},
        check_mode=True,
    )

    assert result["changed"] is True
    assert "--simulate" in calls[1]


def test_unusual_dpkg_state_falls_back_before_mutation(monkeypatch):
    _executables(monkeypatch)
    calls = []

    def command(argv, environment=None, force_c_locale=False):
        calls.append(argv)
        return 0, "mosquitto\tiU \n", ""

    monkeypatch.setattr(apt, "_command", command)

    assert apt.run({
        "pkg": ["mosquitto"],
        "state": "absent",
    }) == {"_ansiroll_fallback": True}
    assert len(calls) == 1


def test_failed_removal_does_not_report_changed(monkeypatch):
    _executables(monkeypatch)

    def command(argv, environment=None, force_c_locale=False):
        if argv[0].endswith("dpkg-query"):
            return 0, "mosquitto\tii \n", ""
        return 100, "", "dpkg is locked"

    monkeypatch.setattr(apt, "_command", command)
    result = apt.run({"pkg": ["mosquitto"], "state": "absent"})

    assert result["failed"] is True
    assert result["changed"] is False
    assert result["rc"] == 100
    assert "dpkg is locked" in result["msg"]
