"""Integration tests that require ansible-core (module embedding, compilation).

Skipped automatically where ansible-core is not importable.
"""

import importlib.util
import base64
import getpass
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from types import SimpleNamespace

import pytest

pytest.importorskip("ansible")

from ansiroll.compiler import _build_bundles, compile_playbook  # noqa: E402
from ansiroll.cli import _load_extra_vars  # noqa: E402
from ansiroll.embed import ModuleEmbedder  # noqa: E402
from ansiroll import embed as embed_module  # noqa: E402
from ansiroll.loader import _register_of, parse_playbook  # noqa: E402
from ansiroll.runtime import Runtime  # noqa: E402
from ansiroll.accelerated_modules import run as run_accelerated  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
ROOT = os.path.dirname(os.path.dirname(__file__))
STRATEGY = os.path.join(
    ROOT,
    "collections",
    "ansible_collections",
    "ansiroll",
    "core",
    "plugins",
    "strategy",
    "ansiroll.py",
)


def _load_strategy_module():
    spec = importlib.util.spec_from_file_location("ansiroll_strategy_test", STRATEGY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- module embedding (cross-version core mechanism) --------------------


def test_register_projection_normalizes_221_implicit_shape():
    task = SimpleNamespace(register={"result": "_task.polymorphic_result"})
    assert _register_of(task) == "result"


def test_register_projection_rejects_explicit_projection():
    task = SimpleNamespace(register={"result": "_task.result.rc"})
    with pytest.raises(ValueError, match="explicit register projections"):
        _register_of(task)


def test_legacy_assert_action_lowers_that_as_conditions(tmp_path):
    playbook = tmp_path / "legacy-assert.yml"
    playbook.write_text(
        """- hosts: localhost
  gather_facts: false
  tasks:
    - name: legacy assertion
      action:
        module: ansible.legacy.assert
        args:
          that: "{{ 1 + 1 == 2 }}"
"""
    )

    plays, _actions = parse_playbook(str(playbook))
    task = next(
        node
        for block in plays[0]["tasks"]
        for node in block.get("block", [])
        if node.get("name") == "legacy assertion"
    )

    assert task["special"] == "assert"
    assert task["args"]["that"] == [
        {"__ansiroll_trusted_condition__": "{{ 1 + 1 == 2 }}"}
    ]


def test_unsafe_assert_condition_remains_untrusted(tmp_path):
    playbook = tmp_path / "unsafe-assert.yml"
    playbook.write_text(
        """- hosts: localhost
  gather_facts: false
  tasks:
    - name: unsafe assertion
      ansible.builtin.assert:
        that: !unsafe "{{ dangerous_condition }}"
"""
    )

    plays, _actions = parse_playbook(str(playbook))
    task = next(
        node
        for block in plays[0]["tasks"]
        for node in block.get("block", [])
        if node.get("name") == "unsafe assertion"
    )

    assert task["args"]["that"] == [
        {"__ansiroll_untrusted_condition__": "{{ dangerous_condition }}"}
    ]


def test_embed_and_run_ping():
    emb = ModuleEmbedder()
    meta = emb.embed("ansible.builtin.ping")
    assert meta and meta.get("zip")
    rt = Runtime({"ansible.builtin.ping": meta}, "", {"inventory_hostname": "localhost"})
    res = rt.run_module("ansible.builtin.ping", {"data": "pong"})
    assert res.get("ping") == "pong"


def test_module_embedding_deduplicates_aliases_within_one_compile(monkeypatch):
    calls = []
    monkeypatch.setattr(embed_module, "make_templar", lambda: object())
    monkeypatch.setattr(
        embed_module,
        "find_module",
        lambda _action: ("/modules/command.py", "ansible.modules.command"),
    )
    monkeypatch.setattr(
        embed_module,
        "build_module",
        lambda **kwargs: calls.append(kwargs) or (b"wrapper", "legacy"),
    )
    monkeypatch.setattr(
        embed_module,
        "_extract",
        lambda _wrapper: ("encoded-zip", "ansible.modules.command"),
    )

    embedder = ModuleEmbedder()
    command = embedder.embed("command")
    shell = embedder.embed("shell")

    assert command is shell
    assert len(calls) == 1


def test_embed_and_run_command():
    emb = ModuleEmbedder()
    meta = emb.embed("command")
    rt = Runtime({"command": meta}, "", {"inventory_hostname": "localhost"})
    res = rt.run_module("command", {"_raw_params": "echo hello-ansiroll"})
    assert res.get("rc") == 0
    assert "hello-ansiroll" in res.get("stdout", "")


@pytest.mark.parametrize("action", ["command", "shell"])
def test_accelerated_command_matches_official_module_semantics(action):
    emb = ModuleEmbedder()
    meta = emb.embed(action)
    rt = Runtime({action: meta}, "", {"inventory_hostname": "localhost"})
    args = {"_raw_params": "printf accelerator-parity"}
    try:
        official = rt.run_module(action, args)
    finally:
        rt.cleanup()
    accelerated = run_accelerated(action, args)

    for key in ("changed", "failed", "rc", "stdout", "stderr",
                "stdout_lines", "stderr_lines"):
        assert accelerated.get(key) == official.get(key)


def test_accelerated_command_matches_legacy_warn_semantics():
    import ansible

    if tuple(map(int, ansible.__version__.split(".")[:2])) >= (2, 11):
        pytest.skip("the command warn argument was removed after ansible-base 2.10")
    args = {"_raw_params": "printf accelerator-warn-parity", "warn": False}
    meta = ModuleEmbedder().embed("command")
    rt = Runtime({"command": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("command", args)
    finally:
        rt.cleanup()
    accelerated = run_accelerated("command", args)

    for key in ("changed", "failed", "rc", "stdout", "stderr",
                "stdout_lines", "stderr_lines"):
        assert accelerated.get(key) == official.get(key)


def test_accelerated_apt_absent_matches_official_module_semantics():
    package = "ansiroll-package-that-does-not-exist"
    args = {
        "pkg": [package],
        "state": "absent",
        "purge": True,
        "autoclean": True,
        "autoremove": True,
    }
    system_python = "/usr/bin/python3"
    if subprocess.run(
            [system_python, "-c", "import apt"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL).returncode != 0:
        pytest.skip("system Python does not provide python3-apt")
    emb = ModuleEmbedder()
    meta = emb.embed("apt")
    rt = Runtime(
        {"apt": meta}, "", {"inventory_hostname": "localhost"},
        python=system_python,
    )
    try:
        official = rt.run_module("apt", args)
    finally:
        rt.cleanup()
    accelerated = run_accelerated("apt", args)

    assert accelerated.get("failed") == official.get("failed")
    assert accelerated["changed"] == official["changed"] is False


def test_accelerated_slurp_matches_official_module_semantics(tmp_path):
    source = tmp_path / "binary"
    source.write_bytes(b"\x00quotes'\"slashes\\\xff")
    args = {"src": str(source)}
    emb = ModuleEmbedder()
    meta = emb.embed("slurp")
    rt = Runtime({"slurp": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("slurp", args)
    finally:
        rt.cleanup()
    from ansible.release import __version__ as ansible_version
    version = tuple(map(int, ansible_version.split(".")[:2]))
    accelerated = run_accelerated(
        "slurp",
        args,
        options={
            "lowercase_not_found": version < (2, 19),
            "typed_exceptions": version >= (2, 19),
        },
    )

    expected = {
        key: value for key, value in official.items()
        if key not in ("invocation",)
    }
    assert accelerated == expected


def test_accelerated_slurp_missing_file_matches_official_failure(tmp_path):
    from ansible.release import __version__ as ansible_version

    source = tmp_path / "missing"
    args = {"src": str(source)}
    meta = ModuleEmbedder().embed("slurp")
    rt = Runtime({"slurp": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("slurp", args)
    finally:
        rt.cleanup()
    version = tuple(map(int, ansible_version.split(".")[:2]))
    accelerated = run_accelerated(
        "slurp",
        args,
        options={
            "lowercase_not_found": version < (2, 19),
            "typed_exceptions": version >= (2, 19),
        },
    )

    expected = {
        key: value for key, value in official.items()
        if key not in ("invocation",)
    }
    assert accelerated == expected


@pytest.mark.parametrize("initial,args", [
    (
        "before\nold=one\nold=two\nafter\n",
        {"regexp": r"^old=.*$", "replace": "new=value"},
    ),
    (
        "header\nSTART\nold=one\nEND\nold=outside\n",
        {
            "regexp": r"^old=.*$", "replace": "new=value",
            "after": "^START$", "before": "^END$",
        },
    ),
])
def test_accelerated_replace_matches_official_module_semantics(
        tmp_path, initial, args):
    official_path = tmp_path / "official"
    accelerated_path = tmp_path / "accelerated"
    official_path.write_text(initial)
    accelerated_path.write_text(initial)
    emb = ModuleEmbedder()
    meta = emb.embed("replace")
    rt = Runtime({"replace": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module(
            "replace", dict(args, path=str(official_path)))
    finally:
        rt.cleanup()
    from ansible.release import __version__ as ansible_version
    version = tuple(map(int, ansible_version.split(".")[:2]))
    accelerated = run_accelerated(
        "replace",
        dict(args, path=str(accelerated_path)),
        options={
            "binary_io": version < (2, 20),
            "omit_result_rc": version < (2, 14),
        },
    )

    official.pop("invocation", None)
    assert accelerated == official
    assert accelerated_path.read_bytes() == official_path.read_bytes()


def test_accelerated_file_directory_matches_official_idempotence(tmp_path):
    official_path = tmp_path / "official"
    accelerated_path = tmp_path / "accelerated"
    emb = ModuleEmbedder()
    meta = emb.embed("file")
    rt = Runtime({"file": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official_first = rt.run_module(
            "file", {"path": str(official_path), "state": "directory", "mode": "0750"}
        )
        official_second = rt.run_module(
            "file", {"path": str(official_path), "state": "directory", "mode": "0750"}
        )
    finally:
        rt.cleanup()
    accelerated_first = run_accelerated(
        "file", {"path": str(accelerated_path), "state": "directory", "mode": "0750"}
    )
    accelerated_second = run_accelerated(
        "file", {"path": str(accelerated_path), "state": "directory", "mode": "0750"}
    )

    assert accelerated_first.get("failed") == official_first.get("failed")
    assert accelerated_first["changed"] == official_first["changed"] is True
    assert accelerated_second["changed"] == official_second["changed"] is False
    assert (official_path.stat().st_mode & 0o7777) == 0o750
    assert (accelerated_path.stat().st_mode & 0o7777) == 0o750


def test_accelerated_file_attribute_free_recurse_matches_official(tmp_path):
    official_path = tmp_path / "official"
    accelerated_path = tmp_path / "accelerated"
    args = {"state": "directory", "recurse": True}
    emb = ModuleEmbedder()
    meta = emb.embed("file")
    rt = Runtime({"file": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official_first = rt.run_module(
            "file", dict(args, path=str(official_path)))
        official_second = rt.run_module(
            "file", dict(args, path=str(official_path)))
    finally:
        rt.cleanup()
    accelerated_first = run_accelerated(
        "file", dict(args, path=str(accelerated_path)))
    accelerated_second = run_accelerated(
        "file", dict(args, path=str(accelerated_path)))

    assert accelerated_first["changed"] == official_first["changed"] is True
    assert accelerated_second["changed"] == official_second["changed"] is False


def test_accelerated_file_link_and_preserved_touch_match_official(tmp_path):
    source = tmp_path / "source"
    source.write_text("source")
    trials = [
        (
            {"state": "link", "src": str(source), "force": True},
            "official-link",
            "accelerated-link",
        ),
        (
            {
                "state": "touch", "mode": "0644",
                "modification_time": "preserve", "access_time": "preserve",
            },
            "official-touch",
            "accelerated-touch",
        ),
    ]
    emb = ModuleEmbedder()
    meta = emb.embed("file")
    rt = Runtime({"file": meta}, "", {"inventory_hostname": "localhost"})
    try:
        for args, official_name, accelerated_name in trials:
            official_path = tmp_path / official_name
            accelerated_path = tmp_path / accelerated_name
            if args["state"] == "touch":
                official_path.write_text("existing")
                accelerated_path.write_text("existing")
            official = rt.run_module(
                "file", dict(args, path=str(official_path)))
            accelerated = run_accelerated(
                "file", dict(args, path=str(accelerated_path)))
            assert accelerated["changed"] == official["changed"]
    finally:
        rt.cleanup()


def test_accelerated_file_retargets_existing_symlink_without_force(tmp_path):
    official_old = tmp_path / "official-old"
    official_new = tmp_path / "official-new"
    accelerated_old = tmp_path / "accelerated-old"
    accelerated_new = tmp_path / "accelerated-new"
    for source in (
            official_old, official_new, accelerated_old, accelerated_new):
        source.mkdir()
    official_path = tmp_path / "official-current"
    accelerated_path = tmp_path / "accelerated-current"
    official_path.symlink_to(official_old)
    accelerated_path.symlink_to(accelerated_old)

    emb = ModuleEmbedder()
    meta = emb.embed("file")
    rt = Runtime({"file": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("file", {
            "path": str(official_path),
            "state": "link",
            "src": str(official_new),
        })
    finally:
        rt.cleanup()
    accelerated = run_accelerated("file", {
        "path": str(accelerated_path),
        "state": "link",
        "src": str(accelerated_new),
    })

    assert accelerated.get("failed") == official.get("failed")
    assert accelerated["changed"] == official["changed"] is True
    assert os.readlink(accelerated_path) == str(accelerated_new)


def test_accelerated_file_refuses_unsafe_symlink_conversions(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    regular_file = tmp_path / "regular"
    regular_file.write_text("keep")
    nonempty_directory = tmp_path / "directory"
    nonempty_directory.mkdir()
    (nonempty_directory / "keep").write_text("keep")

    regular_result = run_accelerated("file", {
        "path": str(regular_file),
        "state": "link",
        "src": str(source),
    })
    directory_result = run_accelerated("file", {
        "path": str(nonempty_directory),
        "state": "link",
        "src": str(source),
        "force": True,
    })

    assert regular_result["failed"] is True
    assert regular_file.read_text() == "keep"
    assert directory_result["failed"] is True
    assert (nonempty_directory / "keep").read_text() == "keep"


def test_accelerated_blockinfile_matches_official_idempotence(tmp_path):
    official_path = tmp_path / "official"
    accelerated_path = tmp_path / "accelerated"
    initial = "existing=true\n"
    official_path.write_text(initial)
    accelerated_path.write_text(initial)
    args = {"insertafter": "EOF", "block": "set linenumbers\n"}
    emb = ModuleEmbedder()
    meta = emb.embed("blockinfile")
    rt = Runtime(
        {"blockinfile": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official_first = rt.run_module(
            "blockinfile", dict(args, path=str(official_path)))
        official_second = rt.run_module(
            "blockinfile", dict(args, path=str(official_path)))
    finally:
        rt.cleanup()
    accelerated_first = run_accelerated(
        "blockinfile", dict(args, path=str(accelerated_path)))
    accelerated_second = run_accelerated(
        "blockinfile", dict(args, path=str(accelerated_path)))

    official_first.pop("invocation", None)
    official_second.pop("invocation", None)
    official_first["diff"] = json.loads(
        json.dumps(official_first["diff"]).replace(str(official_path), "<path>"))
    official_second["diff"] = json.loads(
        json.dumps(official_second["diff"]).replace(str(official_path), "<path>"))
    accelerated_first["diff"] = json.loads(
        json.dumps(accelerated_first["diff"]).replace(str(accelerated_path), "<path>"))
    accelerated_second["diff"] = json.loads(
        json.dumps(accelerated_second["diff"]).replace(str(accelerated_path), "<path>"))

    assert accelerated_first == official_first
    assert accelerated_second == official_second
    assert accelerated_path.read_bytes() == official_path.read_bytes()


def test_accelerated_user_and_group_noops_match_official():
    import grp
    import pwd

    current_user = pwd.getpwuid(os.geteuid())
    current_group = grp.getgrgid(current_user.pw_gid)
    trials = [
        (
            "group",
            {
                "name": current_group.gr_name,
                "state": "present",
                "gid": current_group.gr_gid,
            },
        ),
        (
            "user",
            {
                "name": current_user.pw_name,
                "state": "present",
                "uid": current_user.pw_uid,
                "group": current_group.gr_name,
                "shell": current_user.pw_shell,
            },
        ),
    ]
    for action, args in trials:
        emb = ModuleEmbedder()
        meta = emb.embed(action)
        rt = Runtime(
            {action: meta}, "", {"inventory_hostname": "localhost"})
        try:
            official = rt.run_module(action, args)
        finally:
            rt.cleanup()
        from ansible.release import __version__ as ansible_version
        version = tuple(map(int, ansible_version.split(".")[:2]))
        accelerated = run_accelerated(
            action,
            args,
            options={"result_memberships": action == "user" and version >= (2, 21)},
        )

        official.pop("invocation", None)
        assert accelerated == official
        assert accelerated["changed"] is False


@pytest.mark.parametrize(
    "args,expected_names",
    [
        ({"file_type": "file"}, {"plain.txt", "skip.log"}),
        (
            {"file_type": "file", "patterns": "*.txt", "excludes": "skip*"},
            {"plain.txt"},
        ),
        ({"file_type": "directory"}, {"nested"}),
        (
            {"file_type": "file", "patterns": "*.txt", "recurse": True},
            {"plain.txt", "inside.txt"},
        ),
        ({"file_type": "any", "hidden": True}, {
            "plain.txt", "skip.log", ".hidden", "nested",
        }),
    ],
)
def test_accelerated_find_matches_official_result_shape(
        tmp_path, args, expected_names):
    root = tmp_path / "find"
    root.mkdir()
    (root / "plain.txt").write_text("plain")
    (root / "skip.log").write_text("skip")
    (root / ".hidden").write_text("hidden")
    (root / "nested").mkdir()
    (root / "nested" / "inside.txt").write_text("inside")
    module_args = dict(args, paths=str(root))

    meta = ModuleEmbedder().embed("find")
    rt = Runtime({"find": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("find", module_args)
    finally:
        rt.cleanup()
    from ansible.release import __version__ as ansible_version
    version = tuple(map(int, ansible_version.split(".")[:2]))
    accelerated = run_accelerated(
        "find",
        module_args,
        options={
            "all_paths_examined": version >= (2, 12),
            "disk_usage_bytes": version >= (2, 21),
            "structured_skipped_paths": version >= (2, 12),
            "typed_warnings": version >= (2, 19),
        },
    )

    assert accelerated["changed"] == official["changed"] is False
    assert accelerated["matched"] == official["matched"]
    assert accelerated["examined"] == official["examined"]
    assert accelerated["msg"] == official["msg"]
    official_by_name = {
        os.path.basename(item["path"]): item for item in official["files"]
    }
    accelerated_by_name = {
        os.path.basename(item["path"]): item for item in accelerated["files"]
    }
    assert set(accelerated_by_name) == set(official_by_name) == expected_names
    for name, official_item in official_by_name.items():
        accelerated_item = accelerated_by_name[name]
        # These are values of the same filesystem object, so even inode and
        # timestamps must match—not merely the commonly consumed path/type.
        assert accelerated_item == official_item


def test_accelerated_find_missing_path_matches_official_result(tmp_path):
    from ansible.release import __version__ as ansible_version

    args = {"paths": str(tmp_path / "missing"), "file_type": "file"}
    version = tuple(map(int, ansible_version.split(".")[:2]))
    meta = ModuleEmbedder().embed("find")
    rt = Runtime({"find": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("find", args)
    finally:
        rt.cleanup()
    accelerated = run_accelerated(
        "find",
        args,
        options={
            "all_paths_examined": version >= (2, 12),
            "disk_usage_bytes": version >= (2, 21),
            "structured_skipped_paths": version >= (2, 12),
            "typed_warnings": version >= (2, 19),
        },
    )

    expected = {
        key: value for key, value in official.items()
        if key not in ("invocation",)
    }
    assert accelerated == expected


def test_compile_omits_total_static_official_module_and_forces_accelerator(
        tmp_path):
    destination = tmp_path / "accelerated-only-file"
    destination.write_text("present")
    playbook = tmp_path / "accelerated-only.yml"
    playbook.write_text(
        "- hosts: localhost\n"
        "  gather_facts: false\n"
        "  tasks:\n"
        "    - ansible.builtin.stat:\n"
        "        path: %s\n"
        "      register: accelerated_stat\n"
        "    - ansible.builtin.assert:\n"
        "        that: accelerated_stat.stat.exists\n" % destination
    )

    source, manifest = compile_playbook(str(playbook))
    agent = tmp_path / "accelerated-only.py"
    agent.write_text(source)
    environment = dict(os.environ)
    environment.pop("ANSIROLL_ACCELERATED_MODULES", None)
    process = subprocess.run(
        [sys.executable, "-S", str(agent)],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert process.returncode == 0, process.stdout + process.stderr
    assert destination.is_file()
    assert not any(name.endswith("stat") for name in manifest["modules"])
    assert manifest["accelerated_only_modules"] == ["ansible.builtin.stat"]


@pytest.mark.parametrize("kind", ["missing", "file", "directory", "symlink"])
def test_accelerated_stat_matches_official_result(tmp_path, kind):
    path = tmp_path / kind
    if kind == "file":
        path.write_bytes(b"accelerated stat parity\n")
    elif kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_text("target")
        path.symlink_to(target)

    emb = ModuleEmbedder()
    meta = emb.embed("stat")
    rt = Runtime({"stat": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("stat", {"path": str(path)})
    finally:
        rt.cleanup()
    accelerated = run_accelerated(
        "stat", {"path": str(path)},
        options={"disk_usage_bytes": "disk_usage_bytes" in official.get("stat", {})},
    )

    expected = {
        key: value for key, value in official.items() if key != "invocation"
    }
    if expected.get("stat", {}).get("exists"):
        expected["stat"].pop("atime", None)
        accelerated["stat"].pop("atime", None)
    assert accelerated == expected


def test_accelerated_stat_matches_official_optional_flags(tmp_path):
    path = tmp_path / "plain"
    path.write_text("plain")
    args = {
        "path": str(path), "follow": True, "get_checksum": False,
        "get_mime": False, "get_attributes": False,
    }
    emb = ModuleEmbedder()
    meta = emb.embed("stat")
    rt = Runtime({"stat": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("stat", args)
    finally:
        rt.cleanup()
    accelerated = run_accelerated(
        "stat", args,
        options={"disk_usage_bytes": "disk_usage_bytes" in official.get("stat", {})},
    )

    expected = {
        key: value for key, value in official.items() if key != "invocation"
    }
    expected["stat"].pop("atime", None)
    accelerated["stat"].pop("atime", None)
    assert accelerated == expected


def _fake_systemctl(tmp_path):
    fake_bin = tmp_path / "fake-systemctl-bin"
    fake_bin.mkdir()
    executable = fake_bin / "systemctl"
    executable.write_text(
        "#!%s\n" % sys.executable
        + "import json, os, sys\n"
        + "state_path = os.environ['FAKE_SYSTEMD_STATE']\n"
        + "log_path = os.environ['FAKE_SYSTEMD_LOG']\n"
        + "args = [arg for arg in sys.argv[1:] if arg != '-l']\n"
        + "with open(log_path, 'a') as stream:\n"
        + "    stream.write(json.dumps(args) + '\\n')\n"
        + "with open(state_path) as stream:\n"
        + "    state = json.load(stream)\n"
        + "command = args[0] if args else ''\n"
        + "if command == state.get('fail'):\n"
        + "    sys.stderr.write('injected failure\\n')\n"
        + "    sys.exit(1)\n"
        + "if command == 'daemon-reload':\n"
        + "    sys.exit(0)\n"
        + "if command == 'show':\n"
        + "    if state.get('missing'):\n"
        + "        sys.stderr.write('Unit not found\\n')\n"
        + "        sys.exit(1)\n"
        + "    print('LoadState=loaded')\n"
        + "    print('ActiveState=' + state['active'])\n"
        + "    print('UnitFileState=' + ('enabled' if state['enabled'] else 'disabled'))\n"
        + "    print('Description=Fake service')\n"
        + "    sys.exit(0)\n"
        + "if command == 'is-enabled':\n"
        + "    print('enabled' if state['enabled'] else 'disabled')\n"
        + "    sys.exit(0 if state['enabled'] else 1)\n"
        + "if command == 'list-unit-files':\n"
        + "    sys.exit(1 if state.get('missing') else 0)\n"
        + "if command in ('enable', 'disable'):\n"
        + "    state['enabled'] = command == 'enable'\n"
        + "elif command in ('start', 'restart'):\n"
        + "    state['active'] = 'active'\n"
        + "elif command == 'stop':\n"
        + "    state['active'] = 'inactive'\n"
        + "else:\n"
        + "    sys.stderr.write('unsupported fake systemctl call: %r\\n' % args)\n"
        + "    sys.exit(2)\n"
        + "with open(state_path, 'w') as stream:\n"
        + "    json.dump(state, stream, sort_keys=True)\n"
    )
    executable.chmod(0o755)
    state_path = tmp_path / "systemd-state.json"
    log_path = tmp_path / "systemd-log.jsonl"
    environment = {
        "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
        "FAKE_SYSTEMD_STATE": str(state_path),
        "FAKE_SYSTEMD_LOG": str(log_path),
    }
    return state_path, log_path, environment


def _systemd_trial(meta, args, initial, state_path, log_path, environment,
                   accelerated):
    state_path.write_text(json.dumps(initial))
    log_path.write_text("")
    if accelerated:
        result = run_accelerated(
            "systemd", args, environment=environment,
            options={"modern_is_enabled": True},
        )
    else:
        rt = Runtime({"systemd": meta}, "", {"inventory_hostname": "localhost"})
        rt._module_env = environment
        try:
            result = rt.run_module("systemd", args)
        finally:
            rt.cleanup()
    final = json.loads(state_path.read_text())
    calls = [json.loads(line) for line in log_path.read_text().splitlines()]
    return result, final, calls


@pytest.mark.parametrize(
    "args,initial",
    [
        ({"daemon_reload": True}, {"active": "inactive", "enabled": False}),
        ({"name": "parity.service", "state": "started", "enabled": True},
         {"active": "active", "enabled": True}),
        ({"name": "parity.service", "state": "started", "enabled": True},
         {"active": "inactive", "enabled": False}),
        ({"name": "parity.service", "state": "restarted"},
         {"active": "active", "enabled": True}),
        ({"name": "parity.service", "state": "stopped", "enabled": False},
         {"active": "active", "enabled": True}),
        ({"name": "parity.service", "state": "started", "enabled": True,
          "daemon_reload": True},
         {"active": "active", "enabled": True}),
    ],
)
def test_accelerated_systemd_matches_official_mutations(tmp_path, args, initial):
    state_path, log_path, environment = _fake_systemctl(tmp_path)
    meta = ModuleEmbedder().embed("systemd")
    official, official_state, official_calls = _systemd_trial(
        meta, args, initial, state_path, log_path, environment, False
    )
    accelerated, accelerated_state, accelerated_calls = _systemd_trial(
        meta, args, initial, state_path, log_path, environment, True
    )

    expected = {key: value for key, value in official.items()
                if key not in ("invocation", "warnings", "deprecations")}
    assert accelerated == expected
    assert accelerated_state == official_state
    mutations = {"daemon-reload", "enable", "disable", "start", "stop", "restart"}
    assert [call for call in accelerated_calls if call[0] in mutations] == [
        call for call in official_calls if call[0] in mutations
    ]
    # Named daemon_reload calls use one extra read-only preflight so fallback
    # can never repeat a manager mutation.
    allowance = 1 if args.get("daemon_reload") and args.get("name") else 0
    assert len(accelerated_calls) <= len(official_calls) + allowance


def test_accelerated_systemd_missing_unit_falls_back_before_reload(tmp_path):
    state_path, log_path, environment = _fake_systemctl(tmp_path)
    state_path.write_text(json.dumps({
        "active": "inactive", "enabled": False, "missing": True,
    }))
    log_path.write_text("")

    result = run_accelerated(
        "systemd",
        {"name": "missing.service", "state": "stopped", "daemon_reload": True},
        environment=environment,
        options={"modern_is_enabled": True},
    )

    assert result == {"_ansiroll_fallback": True}
    calls = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert calls == [["show", "missing.service"]]


@pytest.mark.parametrize(
    "args,initial",
    [
        ({"daemon_reload": True},
         {"active": "inactive", "enabled": False, "fail": "daemon-reload"}),
        ({"name": "parity.service", "enabled": True},
         {"active": "inactive", "enabled": False, "fail": "enable"}),
        ({"name": "parity.service", "state": "started"},
         {"active": "inactive", "enabled": False, "fail": "start"}),
    ],
)
def test_accelerated_systemd_matches_official_failures(tmp_path, args, initial):
    state_path, log_path, environment = _fake_systemctl(tmp_path)
    meta = ModuleEmbedder().embed("systemd")
    official, official_state, _ = _systemd_trial(
        meta, args, initial, state_path, log_path, environment, False
    )
    accelerated, accelerated_state, _ = _systemd_trial(
        meta, args, initial, state_path, log_path, environment, True
    )

    expected = {key: value for key, value in official.items()
                if key not in ("invocation", "warnings", "deprecations")}
    assert accelerated == expected
    assert accelerated_state == official_state


def test_worker_reuses_module_and_writes_nothing():
    # Persistent worker: module loaded once, forked per task, no disk writes.
    emb = ModuleEmbedder()
    meta = emb.embed("command")
    rt = Runtime({"command": meta}, "", {"inventory_hostname": "localhost"})
    try:
        for i in range(3):
            res = rt.run_module("command", {"_raw_params": "echo run%d" % i})
            assert res.get("rc") == 0 and ("run%d" % i) in res.get("stdout", "")
        assert rt._workers[False]["loaded"] == {"ansible.modules.command"}
    finally:
        rt.cleanup()
    assert rt._workers == {}


def test_worker_loads_accelerator_from_shared_memory_bundle(tmp_path):
    import_bundle, _, _ = _build_bundles([], {})
    rt = Runtime({}, "", {"inventory_hostname": "localhost"},
                 module_bundle=import_bundle)
    path = tmp_path / "from-worker"
    try:
        res = rt._run_worker_accelerated(
            "file", {"path": str(path), "state": "directory"}
        )
    finally:
        rt.cleanup()
    assert path.is_dir()
    assert res.get("changed") is True
    assert res.get("failed") is not True


def test_worker_runs_accelerated_apt_from_shared_memory_bundle():
    import_bundle, _, _ = _build_bundles([], {})
    rt = Runtime(
        {}, "", {"inventory_hostname": "localhost"},
        module_bundle=import_bundle,
    )
    try:
        result = rt._run_worker_accelerated(
            "apt",
            {
                "pkg": ["ansiroll-package-that-does-not-exist"],
                "state": "absent",
                "purge": True,
                "autoclean": True,
                "autoremove": True,
            },
        )
    finally:
        rt.cleanup()

    assert result == {"changed": False}


def test_worker_accepts_decoded_import_bundle_bytes():
    emb = ModuleEmbedder()
    meta = emb.embed("command")
    import_bundle, _, _ = _build_bundles([], {"command": meta})
    rt = Runtime(
        {"command": meta}, "", {"inventory_hostname": "localhost"},
        module_bundle=import_bundle,
    )
    try:
        res = rt.run_module("command", {"_raw_params": "printf raw-bundle"})
    finally:
        rt.cleanup()
    assert res.get("rc") == 0
    assert res.get("stdout") == "raw-bundle"


def test_same_user_become_reuses_normal_worker_protocol(tmp_path, monkeypatch):
    """Becoming the current UID must not create a redundant sudo worker."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-S\" ]; then\n"
        "  exit 0\n"  # cached authentication: deliberately do not read stdin
        "fi\n"
        "if [ \"$1\" = \"-n\" ] && [ \"$2\" = \"-u\" ]; then\n"
        "  shift 3\n"
        "  exec \"$@\"\n"
        "fi\n"
        "exit 2\n"
    )
    fake_sudo.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])

    emb = ModuleEmbedder()
    meta = emb.embed("command")
    rt = Runtime(
        {"command": meta},
        "",
        {"inventory_hostname": "localhost"},
        become_password="cached-password",
    )
    rt.become_user = getpass.getuser()
    try:
        res = rt.run_module("command", {"_raw_params": "printf cached-sudo-ok"}, become=True)
        assert set(rt._workers) == {False}
    finally:
        rt.cleanup()

    assert res.get("rc") == 0
    assert res.get("stdout") == "cached-sudo-ok"


def test_worker_death_reports_clear_failure(monkeypatch):
    # A dead worker must fail the task loudly (outcome unknown) -- never silently
    # retry or switch engines.
    emb = ModuleEmbedder()
    meta = emb.embed("command")
    rt = Runtime({"command": meta}, "", {"inventory_hostname": "localhost"})

    def boom(*a, **k):
        raise RuntimeError("worker closed unexpectedly")
    monkeypatch.setattr(rt, "_run_via_worker", boom)
    res = rt.run_module("command", {"_raw_params": "echo x"})
    assert res["failed"] is True
    assert "worker died" in res["msg"] and "outcome is unknown" in res["msg"]


def test_worker_recovers_for_next_task_after_crash():
    # A one-off worker crash must not poison the rest of the play: the next task
    # gets a fresh worker.
    emb = ModuleEmbedder()
    meta = emb.embed("command")
    rt = Runtime({"command": meta}, "", {"inventory_hostname": "localhost"})
    try:
        assert rt.run_module("command", {"_raw_params": "echo a"}).get("rc") == 0
        rt._workers[False]["proc"].kill()
        rt._workers[False]["proc"].wait()
        # next task: _get_worker sees the dead proc and spawns a fresh one
        assert rt.run_module("command", {"_raw_params": "echo b"}).get("rc") == 0
    finally:
        rt.cleanup()


def test_worker_drains_stdout_and_stderr_concurrently():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ansible/modules/__init__.py", "")
        zf.writestr(
            "ansible/modules/noisy.py",
            "import json, sys\n"
            "sys.stderr.write('x' * 262144)\n"
            "sys.stderr.flush()\n"
            "print(json.dumps({'changed': False, 'msg': 'ok'}))\n",
        )
    meta = {
        "zip": base64.b64encode(buf.getvalue()).decode("ascii"),
        "module_fqn": "ansible.modules.noisy",
        "profile": "legacy",
    }
    rt = Runtime({"noisy": meta}, "", {"inventory_hostname": "localhost"})
    started = time.monotonic()
    try:
        res = rt.run_module("noisy", {})
    finally:
        rt.cleanup()
    elapsed = time.monotonic() - started
    assert res["changed"] is False
    assert res["msg"] == "ok"
    assert elapsed < 5


def test_embed_setup_returns_prefixed_facts():
    emb = ModuleEmbedder()
    meta = emb.embed("ansible.builtin.setup")
    rt = Runtime({"ansible.builtin.setup": meta}, "", {})
    res = rt.run_module("ansible.builtin.setup", {})
    facts = res.get("ansible_facts", {})
    assert "ansible_distribution" in facts
    assert "ansible_system" in facts


# ---- compilation -------------------------------------------------------

def test_compile_simple_embeds_command():
    src, manifest = compile_playbook(os.path.join(FIXTURES, "simple.yml"))
    assert manifest["bundle_format"] == 2
    assert any(m.endswith("command") for m in manifest["modules"])
    assert "def main(" in src  # runtime inlined
    assert "PLAYS = " in src


def test_compile_embeds_worker_file():
    src, _ = compile_playbook(os.path.join(FIXTURES, "simple.yml"))
    with open(os.path.join(ROOT, "ansiroll", "worker.py"), "r") as fh:
        worker_src = fh.read()
    assert "_WORKER_CODE = " + repr(worker_src) in src
    assert "# __ANSIROLL_WORKER_CODE__" not in src


def test_gather_embeds_setup():
    src, manifest = compile_playbook(os.path.join(FIXTURES, "gather.yml"))
    assert any(m.endswith("setup") for m in manifest["modules"])


def test_legacy_include_is_rewritten_and_inlined():
    src, manifest = compile_playbook(os.path.join(FIXTURES, "legacy", "play.yml"))
    # The included task must be inlined (no crash on removed `include`), and not
    # left as a fallback.
    ns = {"__name__": "ansiroll_test_agent"}
    exec(compile(src, "<ansiroll-test>", "exec"), ns)
    assert "included-marker" in json.dumps(ns["PLAYS"])
    assert manifest["fallback_tasks"] == []


def test_tree_shake_uses_known_extra_vars_and_drops_module(tmp_path):
    play = tmp_path / "play.yml"
    play.write_text("""
- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.command: /bin/true
      when: run_mode == 'build'
    - ansible.builtin.debug:
        msg: retained
      when:
        - run_mode == 'update'
        - runtime_only | default(true)
""")
    src, manifest = compile_playbook(
        str(play), extra_vars={"run_mode": "update"}, tree_shake=True
    )
    ns = {"__name__": "ansiroll_test_agent"}
    exec(compile(src, "<ansiroll-test>", "exec"), ns)

    assert manifest["modules"] == []
    assert manifest["tree_shake"]["removed_nodes"] == 1
    def tasks(nodes):
        for node in nodes:
            if node.get("type") == "block":
                yield from tasks(node.get("block", []))
                yield from tasks(node.get("rescue", []))
                yield from tasks(node.get("always", []))
            elif node.get("type") == "include":
                yield from tasks(node.get("tasks", []))
            else:
                yield node
    task = next(node for node in tasks(ns["PLAYS"][0]["tasks"])
                if node.get("action", "").endswith("debug"))
    assert task["action"].endswith("debug")
    assert task["when"] == ["runtime_only | default(true)"]


def test_tree_shake_short_circuits_unknown_runtime_values(tmp_path):
    play = tmp_path / "play.yml"
    play.write_text("""
- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.command: /bin/false
      when: report_to_slack and cloud_update_id is not defined
    - ansible.builtin.debug: {msg: retained unknown}
      when: report_to_slack or runtime_only
    - ansible.builtin.debug: {msg: retained true}
      when: not report_to_slack or runtime_only
""")

    src, manifest = compile_playbook(
        str(play), extra_vars={"report_to_slack": False}, tree_shake=True
    )
    ns = {"__name__": "ansiroll_test_agent"}
    exec(compile(src, "<ansiroll-test>", "exec"), ns)
    def leaf_tasks(nodes):
        for node in nodes:
            if node.get("type") == "block":
                yield from leaf_tasks(node.get("block", []))
                yield from leaf_tasks(node.get("rescue", []))
                yield from leaf_tasks(node.get("always", []))
            elif node.get("type") == "include":
                yield from leaf_tasks(node.get("tasks", []))
            elif node.get("action") != "meta":
                yield node
    tasks = list(leaf_tasks(ns["PLAYS"][0]["tasks"]))

    assert manifest["modules"] == []
    assert manifest["tree_shake"]["removed_nodes"] == 1
    assert tasks[0]["when"] == ["report_to_slack or runtime_only"]
    assert tasks[1]["when"] == []


def test_tree_shake_can_freeze_playbook_vars(tmp_path):
    play = tmp_path / "play.yml"
    play.write_text("""
- hosts: localhost
  gather_facts: false
  vars:
    run_mode: update
    derived: '{{ run_mode == "update" }}'
  tasks:
    - ansible.builtin.command: /bin/true
      when: run_mode == 'build'
    - ansible.builtin.debug: {msg: retained}
      when: derived
""")

    src, manifest = compile_playbook(
        str(play), tree_shake=True, freeze_playbook_vars=True
    )

    assert manifest["modules"] == []
    tree = manifest["tree_shake"]
    assert tree["freeze_playbook_vars"] is True
    assert tree["frozen_var_names"] == ["derived", "run_mode"]
    assert tree["removed_nodes"] == 1
    assert "run_mode" not in src  # play/data values remain inside the encoded bundle


def test_freeze_var_uses_group_vars_and_rejects_unknown_or_shadowed(tmp_path):
    group_vars = tmp_path / "group_vars" / "all"
    group_vars.mkdir(parents=True)
    (group_vars / "main.yml").write_text("deployment_mode: update\n")
    play = tmp_path / "play.yml"
    play.write_text("""
- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.debug: {msg: retained}
      when: deployment_mode == 'update'
""")

    _, manifest = compile_playbook(
        str(play), tree_shake=True, freeze_vars=["deployment_mode"]
    )
    assert manifest["tree_shake"]["frozen_var_names"] == ["deployment_mode"]
    assert manifest["tree_shake"]["frozen_var_sources"] == {
        "deployment_mode": ["group_vars"]
    }

    with pytest.raises(ValueError, match="explicitly frozen variable 'missing' is undefined"):
        compile_playbook(str(play), freeze_vars=["missing"])

    shadowed = tmp_path / "shadowed.yml"
    shadowed.write_text("""
- hosts: localhost
  gather_facts: false
  vars: {run_mode: update}
  tasks:
    - ansible.builtin.debug: {msg: x}
      vars: {run_mode: build}
""")
    with pytest.raises(ValueError, match="cannot freeze 'run_mode'.*shadows"):
        compile_playbook(str(shadowed), freeze_vars=["run_mode"])


def test_compiled_copy_reads_content_addressed_binary_asset(tmp_path):
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"a\x00b\xffc")
    dest = tmp_path / "out.bin"
    play = tmp_path / "copy.yml"
    play.write_text("""
- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.copy:
        src: payload.bin
        dest: %s
""" % dest)
    src, manifest = compile_playbook(str(play))
    generated = tmp_path / "compiled.py"
    generated.write_text(src)

    proc = subprocess.run([sys.executable, str(generated)], capture_output=True, text=True)

    assert proc.returncode == 0, proc.stderr
    assert dest.read_bytes() == payload.read_bytes()
    assert manifest["bundles"]["asset_count"] == 1


def test_compiled_unarchive_uses_baked_controller_source_from_memory(tmp_path):
    archive = tmp_path / "payload.zip"
    with zipfile.ZipFile(str(archive), "w") as zipped:
        zipped.writestr("inside.txt", b"embedded archive payload\x00")
    destination = tmp_path / "extracted"
    destination.mkdir()
    play = tmp_path / "unarchive.yml"
    play.write_text("""
- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.unarchive:
        src: payload.zip
        dest: %s
""" % destination)

    src, manifest = compile_playbook(str(play))
    assert "ansiroll-unarchive-" not in src
    assert "memfd_create" in src
    generated = tmp_path / "compiled.py"
    generated.write_text(src)
    proc = subprocess.run(
        [sys.executable, str(generated)], capture_output=True, text=True
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (destination / "inside.txt").read_bytes() == b"embedded archive payload\x00"
    assert any(name.endswith("unarchive") for name in manifest["modules"])
    assert manifest["bundles"]["asset_count"] == 1


def test_async_unarchive_keeps_memory_source_alive_until_extraction(tmp_path):
    archive = tmp_path / "async-payload.zip"
    with zipfile.ZipFile(str(archive), "w") as zipped:
        zipped.writestr("async.txt", b"async memory payload")
    destination = tmp_path / "async-extracted"
    destination.mkdir()
    play = tmp_path / "async-unarchive.yml"
    play.write_text("""
- hosts: localhost
  gather_facts: false
  vars:
    ansible_async_dir: %s
  tasks:
    - ansible.builtin.unarchive:
        src: async-payload.zip
        dest: %s
      async: 30
      poll: 0
""" % (tmp_path / "async-jobs", destination))

    src, _ = compile_playbook(str(play))
    generated = tmp_path / "async-compiled.py"
    generated.write_text(src)
    proc = subprocess.run(
        [sys.executable, str(generated)], capture_output=True, text=True
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    extracted = destination / "async.txt"
    deadline = time.monotonic() + 10
    while not extracted.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert extracted.read_bytes() == b"async memory payload"


def test_specialized_artifact_rejects_conflicting_runtime_vars(tmp_path):
    play = tmp_path / "play.yml"
    play.write_text("""
- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.debug: {msg: ok}
      when: run_mode == 'update'
""")
    src, _ = compile_playbook(
        str(play), extra_vars={"run_mode": "update"}, tree_shake=True
    )
    generated = tmp_path / "compiled.py"
    generated.write_text(src)
    env = dict(os.environ, ANSIROLL_EXTRA_VARS='{"run_mode":"build"}')

    proc = subprocess.run([sys.executable, str(generated)], env=env,
                          capture_output=True, text=True)

    assert proc.returncode == 2
    assert "conflict" in proc.stderr and "run_mode" in proc.stderr


def test_extra_vars_support_key_value_json_and_file(tmp_path):
    values = tmp_path / "vars.yml"
    values.write_text("from_file: 3\noverride: file\n")

    result = _load_extra_vars([
        "name=example enabled=true",
        '{"typed": true, "override": "json"}',
        "@" + str(values),
    ])

    assert result == {
        "name": "example", "enabled": "true", "typed": True,
        "from_file": 3, "override": "file",
    }


def test_compiled_simple_runs_and_matches(tmp_path):
    src, _ = compile_playbook(os.path.join(FIXTURES, "simple.yml"))
    path = os.path.join(tmp_path, "compiled.py")
    with open(path, "w") as fh:
        fh.write(src)
    import subprocess
    import sys
    proc = subprocess.run([sys.executable, path], capture_output=True, text=True)
    assert proc.returncode == 0
    # Direct runs default to Ansible-like output on stdout.
    assert "PLAY RECAP" in proc.stdout
    assert "ok=6" in proc.stdout and "changed=2" in proc.stdout
    # ANSIROLL_RAW=1 restores the framed JSON protocol.
    env = dict(os.environ, ANSIROLL_RAW="1")
    proc = subprocess.run([sys.executable, path], capture_output=True, text=True, env=env)
    assert proc.returncode == 0
    assert "__ANSIROLL_RESULT__" in proc.stdout
    assert "ok=6 changed=2" in proc.stderr

    env = dict(os.environ, ANSIROLL_PROFILE="1")
    proc = subprocess.run([sys.executable, path], capture_output=True, text=True, env=env)
    assert proc.returncode == 0
    match = re.search(r"ANSIROLL_ENGINE_PROFILE (\{.*\})", proc.stderr)
    assert match, proc.stderr
    engine = json.loads(match.group(1))
    assert engine["bootstrap"] > 0
    assert engine["runtime_total"] > 0
    assert engine["total"] >= engine["runtime_total"]
    assert engine["workers_started"] >= 1
    assert engine["worker_bundle"] > 0
    assert engine["slow_tasks"]
    assert engine["slow_engine_tasks"]
    assert all({"name", "action", "count", "total", "module", "engine", "max"}
               <= set(task) for task in engine["slow_tasks"])


def test_ssh_command_builder_handles_ansible_210_and_newer_signatures():
    strategy = _load_strategy_module()
    assert not hasattr(strategy, "_build_tsh_command")

    class PlayContext:
        remote_addr = "10.140.0.108"
        ssh_executable = "ssh-from-context"

    class Ansible210Conn:
        _play_context = PlayContext()

        def _build_command(self, binary, *other_args):
            return [binary] + list(other_args)

        def get_option(self, name):
            raise KeyError(name)

    class NewerConn:
        _play_context = PlayContext()

        def _build_command(self, binary, subsystem, *other_args):
            return [binary, subsystem] + list(other_args)

        def get_option(self, name):
            return {
                "host": "10.140.0.109",
                "ssh_executable": "ssh-from-option",
            }.get(name)

    remote = "/usr/bin/python3 -c 'bootstrap'"
    assert strategy._build_ssh_command(Ansible210Conn(), remote) == [
        "ssh",
        "10.140.0.108",
        remote,
    ]
    assert strategy._build_ssh_command(NewerConn(), remote) == [
        "ssh-from-option",
        "ssh",
        "10.140.0.109",
        remote,
    ]


def test_strategy_does_not_override_an_unconfigured_ssh_user():
    strategy = _load_strategy_module()
    plugin = strategy.StrategyModule.__new__(strategy.StrategyModule)
    plugin._t = lambda _templar, value: value
    plugin._resolve_password = lambda _hv, _templar: None

    class Connection:
        options = None

        def set_options(self, **kwargs):
            self.options = kwargs

    connection = Connection()
    plugin._apply_conn_options(
        connection, "ssh", {}, None, None, "testing7.tp.alfredsmartdata.com"
    )

    assert connection.options["direct"] == {
        "host": "testing7.tp.alfredsmartdata.com"
    }


def test_strategy_injects_fast_facts_into_remote_agent(monkeypatch):
    strategy = _load_strategy_module()
    plugin = strategy.StrategyModule.__new__(strategy.StrategyModule)
    plugin._variable_manager = SimpleNamespace(_extra_vars={})
    plugin._delegate_on_target = False
    plugin._first_host_name = "testing7"
    plugin._check_mode = False
    plugin._diff_mode = False
    plugin._profile_enabled = True
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "all")
    monkeypatch.setenv("ANSIROLL_FAST_FACTS", "1")

    header = plugin._agent_header(
        SimpleNamespace(name="testing7"),
        {"inventory_hostname": "testing7"},
        None,
    )

    assert "_INJECTED_ACCELERATED_MODULES = 'all'" in header
    assert "_INJECTED_FAST_FACTS = True" in header


def test_strategy_extra_vars_helper_preserves_json_booleans():
    strategy = _load_strategy_module()

    class VariableManager:
        _extra_vars = {"report_to_slack": False}

    assert strategy._extra_vars(VariableManager()) == {"report_to_slack": False}
    blob = strategy._json_blob(strategy._extra_vars(VariableManager()))
    assert json.loads(blob)["report_to_slack"] is False


def test_strategy_reads_runtime_created_controller_source_losslessly(tmp_path):
    strategy = _load_strategy_module()
    source = tmp_path / "runtime.bin"
    source.write_bytes(b"\x00\xff'\\\n")

    result = strategy.StrategyModule._read_controller_source(str(source))

    assert base64.b64decode(result["data"]) == source.read_bytes()
    assert result["meta"]["fullpath"] == str(source)


def test_strategy_does_not_create_an_empty_recap_row():
    strategy = _load_strategy_module()
    plugin = strategy.StrategyModule.__new__(strategy.StrategyModule)
    stats = SimpleNamespace(
        processed={}, ok={}, changed={}, failures={}, skipped={}, dark={},
        rescued={}, ignored={},
    )
    plugin._tqm = SimpleNamespace(_stats=stats)
    plugin._apply_stats(SimpleNamespace(name="testhost"), {})
    assert stats.processed == {}


def test_strategy_empty_tag_filtered_play_does_not_use_local_python(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: all
  gather_facts: false
  tasks:
    - debug:
        msg: excluded
      tags: excluded
""",
        "--tags", "selected",
        "-i", "remote.invalid,",
        environment={"ANSIROLL_PROVENANCE": "1"},
    )

    assert proc.returncode != 0
    output = proc.stdout + proc.stderr
    assert "No such file or directory: '/usr/bin/python3'" not in output


def test_strategy_controller_only_play_uses_controller_python(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: all
  gather_facts: false
  vars:
    ansible_python_interpreter: /usr/bin/python3
  tasks:
    - debug:
        msg: controller only
""",
        "-i", "remote.invalid,",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "controller only" in proc.stdout


def _run_strategy_play(tmp_path, playbook, *args, environment=None):
    path = tmp_path / "play.yml"
    path.write_text(playbook)
    env = os.environ.copy()
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["ANSIBLE_COLLECTIONS_PATH"] = os.path.join(ROOT, "collections")
    env["ANSIBLE_STRATEGY"] = "ansiroll.core.ansiroll"
    env["ANSIBLE_NOCOLOR"] = "1"
    env.update(environment or {})
    return subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(path), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )


def test_strategy_honors_play_level_local_connection(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - command: /bin/true
""",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok=1" in proc.stdout


def test_strategy_preserves_set_fact_precedence_across_plays(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - set_fact:
        ansible_nodename: from_set_fact

- hosts: localhost
  gather_facts: false
  connection: local
  vars:
    ansible_nodename: from_play_vars
  tasks:
    - assert:
        that: ansible_nodename == 'from_set_fact'
""",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "failed=0" in proc.stdout


def test_strategy_native_only_rejects_linear_fallback(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: all
  gather_facts: false
  tasks:
    - debug:
        msg: controller only
""",
        "-i", "host1,host2",
        environment={"ANSIROLL_NATIVE_ONLY": "1"},
    )

    assert proc.returncode != 0
    assert "native-only mode rejected play" in proc.stderr
    assert "task-lockstep scheduling" in proc.stderr


def test_strategy_native_only_rejects_controller_fallback(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - set_fact:
        delegated_value: present
      delegate_to: localhost
""",
        environment={"ANSIROLL_NATIVE_ONLY": "1"},
    )

    assert proc.returncode != 0
    assert "native-only mode rejected play" in proc.stderr
    assert "delegate_to localhost" in " ".join(proc.stderr.split())


def test_strategy_reports_native_and_ansiballz_provenance(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - name: native debug
      debug:
        msg: native
    - name: real module
      command: /bin/true
""",
        environment={
            "ANSIROLL_NATIVE_ONLY": "1",
            "ANSIROLL_PROVENANCE": "1",
        },
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    records = [
        json.loads(line.split(" ", 1)[1])
        for line in proc.stdout.splitlines()
        if line.startswith("ANSIROLL_PROVENANCE ")
    ]
    assert any(
        record.get("scope") == "play"
        and record.get("mode") == "native-agent"
        for record in records
    )
    assert any(
        record.get("task") == "native debug"
        and record.get("mode") == "native-agent"
        for record in records
    )
    assert any(
        record.get("task") == "real module"
        and record.get("mode") == "real-ansiballz"
        for record in records
    )
    assert not any(
        record.get("mode", "").endswith("fallback")
        for record in records
    )


def test_strategy_reports_linear_fallback_provenance(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: all
  gather_facts: false
  tasks:
    - debug:
        msg: controller only
""",
        "-i", "host1,host2",
        environment={"ANSIROLL_PROVENANCE": "1"},
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert '"mode": "linear-fallback"' in proc.stdout


def test_strategy_reports_controller_fallback_provenance(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - name: delegated fact
      set_fact:
        delegated_value: present
      delegate_to: localhost
""",
        environment={"ANSIROLL_PROVENANCE": "1"},
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert '"mode": "controller-fallback"' in proc.stdout
    assert '"reason": "delegate_to localhost"' in proc.stdout


def test_strategy_debug_callback_displays_message_payload(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - debug:
        msg: Tagged_task
""",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Tagged_task" in proc.stdout


def test_strategy_check_mode_does_not_write(tmp_path):
    copy_destination = tmp_path / "copy-must-not-exist"
    file_destination = tmp_path / "file-must-not-exist"
    forced_destination = tmp_path / "forced-write"
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - copy:
        content: check-mode
        dest: %s
    - file:
        path: %s
        state: touch
    - copy:
        content: explicit-override
        dest: %s
      check_mode: false
""" % (copy_destination, file_destination, forced_destination),
        "--check",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not copy_destination.exists()
    assert not file_destination.exists()
    assert forced_destination.read_text() == "explicit-override"


def test_strategy_task_can_force_check_mode(tmp_path):
    destination = tmp_path / "must-not-exist"
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: false
  connection: local
  vars:
    force_check: true
  tasks:
    - copy:
        content: forced-check-mode
        dest: %s
      check_mode: "{{ force_check }}"
""" % destination,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not destination.exists()


def test_strategy_honors_become_user(tmp_path):
    user = getpass.getuser()
    probe = subprocess.run(
        ["sudo", "-n", "-u", user, "true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if probe.returncode != 0:
        pytest.skip("passwordless sudo to the current user is unavailable")
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: true
  connection: local
  become: true
  become_user: %s
  tasks:
    - command: id -un
      register: identity
    - assert:
        that: identity.stdout == %r
""" % (user, user),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok=3" in proc.stdout


def test_strategy_run_once_shares_result_and_facts_across_hosts(tmp_path):
    inventory = tmp_path / "inventory"
    inventory.write_text(
        "first ansible_connection=local\n"
        "second ansible_connection=local\n"
    )
    play = tmp_path / "run-once.yml"
    play.write_text(
        """- hosts: all
  gather_facts: false
  tasks:
    - set_fact:
        elected_host: "{{ inventory_hostname }}"
      run_once: true
      register: election
    - assert:
        that:
          - elected_host == 'first'
          - election.ansible_facts.elected_host == 'first'
"""
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["ANSIBLE_COLLECTIONS_PATH"] = os.path.join(ROOT, "collections")
    env["ANSIBLE_STRATEGY"] = "ansiroll.core.ansiroll"
    env["ANSIBLE_NOCOLOR"] = "1"
    proc = subprocess.run(
        ["ansible-playbook", "-i", str(inventory), str(play)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert re.search(r"^first\s*: ok=2\b", proc.stdout, re.MULTILINE)
    assert re.search(r"^second\s*: ok=1\b", proc.stdout, re.MULTILINE)


def test_strategy_rejects_multi_host_any_errors_fatal_before_running(tmp_path):
    inventory = tmp_path / "inventory"
    inventory.write_text(
        "first ansible_connection=local\n"
        "second ansible_connection=local\n"
    )
    destination = tmp_path / "must-not-run"
    play = tmp_path / "fatal.yml"
    play.write_text(
        """- hosts: all
  gather_facts: false
  any_errors_fatal: true
  tasks:
    - copy:
        content: unsafe
        dest: %s
""" % destination
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["ANSIBLE_COLLECTIONS_PATH"] = os.path.join(ROOT, "collections")
    env["ANSIBLE_STRATEGY"] = "ansiroll.core.ansiroll"
    env["ANSIBLE_NOCOLOR"] = "1"
    proc = subprocess.run(
        ["ansible-playbook", "-i", str(inventory), str(play)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "multi-host any_errors_fatal" in proc.stderr
    assert not destination.exists()


def test_strategy_runs_over_a_real_ssh_channel(tmp_path):
    sshd = shutil.which("sshd")
    ssh_keygen = shutil.which("ssh-keygen")
    if not sshd or not ssh_keygen:
        pytest.skip("OpenSSH server and key generator are required")

    client_key = tmp_path / "client"
    host_key = tmp_path / "host"
    subprocess.run(
        [ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(client_key)],
        check=True,
    )
    subprocess.run(
        [ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)],
        check=True,
    )
    authorized_keys = tmp_path / "authorized_keys"
    authorized_keys.write_bytes(client_key.with_suffix(".pub").read_bytes())

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = tmp_path / "sshd_config"
    config.write_text(
        "\n".join(
            [
                f"Port {port}",
                "ListenAddress 127.0.0.1",
                f"HostKey {host_key}",
                f"PidFile {tmp_path / 'sshd.pid'}",
                f"AuthorizedKeysFile {authorized_keys}",
                "StrictModes no",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "PubkeyAuthentication yes",
                "UsePAM no",
                "PermitRootLogin no",
                "LogLevel ERROR",
                "",
            ]
        )
    )
    server = subprocess.Popen(
        [sshd, "-D", "-e", "-f", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if server.poll() is not None:
                pytest.skip("sshd could not start: %s" % server.stderr.read())
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.skip("sshd did not become ready")

        inventory = tmp_path / "ssh-inventory"
        inventory.write_text(
            "sshhost ansible_connection=ssh ansible_host=127.0.0.1 "
            f"ansible_port={port} ansible_user={getpass.getuser()} "
            f"ansible_ssh_private_key_file={client_key} "
            "ansible_ssh_common_args='-o StrictHostKeyChecking=no "
            "-o UserKnownHostsFile=/dev/null -o BatchMode=yes'\n"
        )
        play = tmp_path / "ssh-play.yml"
        play.write_text(
            """- hosts: sshhost
  gather_facts: false
  tasks:
    - command: printf ansiroll-over-ssh
      register: command_result
    - assert:
        that: command_result.stdout == 'ansiroll-over-ssh'
"""
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["ANSIBLE_COLLECTIONS_PATH"] = os.path.join(ROOT, "collections")
        env["ANSIBLE_STRATEGY"] = "ansiroll.core.ansiroll"
        env["ANSIBLE_NOCOLOR"] = "1"
        proc = subprocess.run(
            ["ansible-playbook", "-i", str(inventory), str(play)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert re.search(r"^sshhost\s*: ok=2\b", proc.stdout, re.MULTILINE)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
