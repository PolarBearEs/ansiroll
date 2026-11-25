import os
import base64
import io
import json
import subprocess
import sys
import zipfile
from types import SimpleNamespace

import pytest

from ansiroll.compiler import (
    _accelerated_only_actions,
    _build_bundles,
    _deterministic_zip,
    _encode_rendered,
    _mark_required_accelerators,
    _render_source,
    _specialize_accelerators,
)
from ansiroll.loader import _classify_accelerator
from ansiroll.loader import _resolved_play_vars


def _module_zip(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    return base64.b64encode(out.getvalue()).decode("ascii")


@pytest.mark.parametrize(("hostname", "injected"), [
    ("localhost", {}),
    ("node.example.test", {}),
    ("192.0.2.7", {}),
    ("127.000.000.001", {}),
    ("::ffff:192.0.2.7", {}),
    ("::ffff:192.0.2.7%eth.1", {}),
    ("{{ 2 + 2 }}.example.test", {}),
    ("", {}),
    ("launcher.example.test", {"inventory_hostname": "selected.example.test"}),
    ("node.example.test", {"inventory_hostname_short": "alias", "group_names": ["workers", "blue"]}),
    ("node.example.test", {"inventory_hostname_short": "", "group_names": []}),
])
def test_standalone_tasks_receive_inventory_magic(tmp_path, hostname, injected):
    import socket

    from ansible.inventory.host import Host
    from ansiroll import compile_playbook

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: all
  gather_facts: false
  tasks:
    - debug:
        msg:
          name: '{{ inventory_hostname }}'
          short: '{{ inventory_hostname_short }}'
          groups: '{{ group_names }}'
""")
    compiled = tmp_path / "compiled.py"
    compiled.write_text(compile_playbook(str(playbook))[0])
    env = dict(os.environ, ANSIROLL_HOST=hostname, ANSIROLL_RAW="1")
    env.pop("ANSIROLL_EXTRA_VARS", None)
    env.pop("ANSIROLL_VARS_FILE", None)
    command = [sys.executable, str(compiled)]
    if injected:
        runner = ("import json, runpy, sys; runpy.run_path(sys.argv[1], run_name='__main__', "
                  "init_globals={'_INJECTED_VARS': json.loads(sys.argv[2])})")
        command = [sys.executable, "-c", runner, str(compiled), json.dumps(injected)]
    completed = subprocess.run(command, env=env, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    frame, = [json.loads(line.split(" ", 1)[1]) for line in completed.stdout.splitlines()
              if line.startswith("__ANSIROLL_RESULT__ ")]
    name = injected.get("inventory_hostname", hostname) or socket.gethostname()
    short = Host(name).get_magic_vars()["inventory_hostname_short"]
    assert frame["result"]["msg"] == {
        "name": name, "short": injected.get("inventory_hostname_short", short),
        "groups": injected.get("group_names", ["ungrouped"]),
    }


@pytest.mark.parametrize("short_override", [None, "override"])
def test_standalone_vars_file_uses_only_explicit_inventory_inputs(tmp_path, short_override):
    from ansiroll import compile_playbook

    play = """- hosts: all
  gather_facts: false
  vars_files:
    - "profiles/{{ inventory_hostname_short | default(profile) }}.yml"
  tasks:
    - debug:
        msg: '{{ inventory_hostname_short }}:{{ selected }}'
"""
    playbook = tmp_path / "play.yml"
    playbook.write_text(play * 2)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "deployment.yml").write_text("selected: deployment\n")
    (profiles / "override.yml").write_text("selected: override\n")
    compiled = tmp_path / "compiled.py"
    compiled.write_text(compile_playbook(str(playbook))[0])
    inputs = {"profile": "deployment"}
    if short_override:
        inputs["inventory_hostname_short"] = short_override
    env = dict(os.environ, ANSIROLL_HOST="node.example.test", ANSIROLL_RAW="1", ANSIROLL_EXTRA_VARS=json.dumps(inputs))
    env.pop("ANSIROLL_VARS_FILE", None)
    completed = subprocess.run([sys.executable, str(compiled)], env=env, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    messages = [json.loads(line.split(" ", 1)[1])["result"]["msg"] for line in completed.stdout.splitlines()
                if line.startswith("__ANSIROLL_RESULT__ ")]
    assert messages == ["override:override" if short_override else "node:deployment"] * 2


def test_freezing_keeps_inventory_dependent_play_vars_dynamic(tmp_path):
    from ansiroll import compile_playbook

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: all
  gather_facts: false
  vars:
    selected_name: "{{ inventory_hostname_short | default('fallback') }}"
  tasks:
    - debug: {msg: '{{ selected_name }}'}
""")
    compiled = tmp_path / "compiled.py"
    compiled.write_text(compile_playbook(str(playbook), freeze_playbook_vars=True)[0])
    env = dict(os.environ, ANSIROLL_HOST="node.example.test", ANSIROLL_RAW="1")
    completed = subprocess.run([sys.executable, str(compiled)], env=env, capture_output=True, text=True, check=True)
    frame, = [json.loads(line.split(" ", 1)[1]) for line in completed.stdout.splitlines()
              if line.startswith("__ANSIROLL_RESULT__ ")]
    assert frame["result"]["msg"] == "node"


@pytest.mark.parametrize(("variables", "expression"), [
    ("", "'no' if group_names is defined else true"),
    ("  vars:\n    checksum_flag: \"{{ 'no' if group_names is defined else true }}\"\n",
     "checksum_flag | default(true)"),
])
def test_tree_shaking_keeps_official_stat_for_inventory_dependent_args(tmp_path, variables, expression):
    from ansiroll import compile_playbook

    source = tmp_path / "data"
    source.write_text("checksum input\n")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: all
  gather_facts: false
%s  tasks:
    - ansible.builtin.stat:
        path: %s
        get_checksum: "{{ %s }}"
""" % (variables, json.dumps(str(source)), expression))
    compiled = tmp_path / "compiled.py"
    contents, manifest = compile_playbook(str(playbook), tree_shake=True, freeze_playbook_vars=True)
    assert "ansible.builtin.stat" in manifest["modules"]
    assert "ansible.builtin.stat" not in manifest["accelerated_only_modules"]
    compiled.write_text(contents)
    env = dict(os.environ, ANSIROLL_RAW="1", ANSIROLL_ACCELERATED_MODULES="stat")
    completed = subprocess.run([sys.executable, str(compiled)], env=env, capture_output=True, text=True, check=True)
    frame, = [json.loads(line.split(" ", 1)[1]) for line in completed.stdout.splitlines()
              if line.startswith("__ANSIROLL_RESULT__ ")]
    assert frame["result"]["stat"]["exists"]
    assert "checksum" not in frame["result"]["stat"]


@pytest.mark.parametrize("expression", ["group_names is defined", "play_flag | default(false)"])
def test_tree_shaking_keeps_inventory_dependent_extra_var_condition(tmp_path, expression):
    from ansiroll import compile_playbook

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: all
  gather_facts: false
  vars:
    play_flag: '{{ group_names is defined }}'
  tasks:
    - debug: {msg: inventory is available}
      when: run_task
""")
    contents, _ = compile_playbook(str(playbook), tree_shake=True,
                                   extra_vars={"run_task": "{{ " + expression + " }}"})
    compiled = tmp_path / "compiled.py"
    compiled.write_text(contents)
    env = dict(os.environ, ANSIROLL_RAW="1")
    completed = subprocess.run([sys.executable, str(compiled)], env=env, capture_output=True, text=True, check=True)
    frame, = [json.loads(line.split(" ", 1)[1]) for line in completed.stdout.splitlines()
              if line.startswith("__ANSIROLL_RESULT__ ")]
    assert frame["result"]["msg"] == "inventory is available"


def test_bundle_union_deduplicates_identical_import_paths():
    shared = {"ansible/module_utils/shared.py": b"VALUE = 1\n"}
    modules = {
        "one": {"module_fqn": "ansible.modules.one", "profile": "legacy",
                "zip": _module_zip(dict(shared, **{"ansible/modules/one.py": b"pass\n"}))},
        "alias": {"module_fqn": "ansible.modules.one", "profile": "legacy",
                  "zip": _module_zip(dict(shared, **{"ansible/modules/one.py": b"pass\n"}))},
    }

    import_bundle, data_bundle, stats = _build_bundles([], modules)
    with zipfile.ZipFile(io.BytesIO(import_bundle)) as archive:
        assert archive.namelist().count("ansible/module_utils/shared.py") == 1
        assert archive.namelist().count("ansible/modules/one.py") == 1
    assert stats["unique_module_archives"] == 1
    assert stats["import_encoded_size"] == len(base64.b64encode(import_bundle))
    assert stats["data_encoded_size"] == len(base64.b64encode(data_bundle))


def test_frozen_encoding_preserves_tuple_and_literal_string_types():
    from ansiroll.runtime import (
        _UnsafeText, _UntrustedConditionValue, _decode_frozen_value,
    )

    value = {"mapping": ("{{ label }}", _UntrustedConditionValue("literal"))}
    decoded = _decode_frozen_value(json.loads(json.dumps(_encode_rendered(value))))

    assert decoded == value
    assert isinstance(decoded["mapping"][0], _UnsafeText)
    assert isinstance(decoded["mapping"][1], _UntrustedConditionValue)


def test_resolved_play_vars_preserve_ansible_role_magic_lists():
    play = SimpleNamespace(vars={"ordinary": "play"}, vars_files=[], _loader=None)
    role_magic = {
        "role_names": ["app"],
        "ansible_play_role_names": ["app"],
        "ansible_role_names": ["app", "dependency"],
        "ansible_dependent_role_names": ["dependency"],
    }

    resolved = _resolved_play_vars(
        play, {"ordinary": "resolved", **role_magic}, "/unused"
    )

    assert resolved == {"ordinary": "resolved", **role_magic}


def test_bundle_union_rejects_conflicting_import_paths():
    modules = {
        "one": {"module_fqn": "x.one", "zip": _module_zip({"x/shared.py": b"one"})},
        "two": {"module_fqn": "x.two", "zip": _module_zip({"x/shared.py": b"two"})},
    }

    with pytest.raises(RuntimeError, match="conflicting import bundle entry.*x/shared.py"):
        _build_bundles([], modules)


def test_deterministic_zip_has_stable_bytes_and_metadata():
    entries = {"b": b"two", "a": b"one"}
    first = _deterministic_zip(entries)
    second = _deterministic_zip(entries)

    assert first == second
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.namelist() == ["a", "b"]
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0)
                   for info in archive.infolist())


def test_generated_agent_bundles_template_runtimes_for_stdlib_only_target(tmp_path):
    agent = tmp_path / "agent.py"
    agent.write_text(_render_source([], {}, ""))
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    probe = (
        "import runpy, sys, tempfile; "
        "tempfile.mkstemp = lambda *a, **k: "
        "(_ for _ in ()).throw(RuntimeError('temporary files forbidden')); "
        "ns = runpy.run_path(sys.argv[1]); "
        "print(ns['jinja2'].__version__); "
        "print(ns['_yaml_module']().__version__); "
        "print(ns['_yaml_module']().__with_libyaml__); "
        "print(ns['_Templater']().render_string('hello {{ name }}', {'name': 'world'})); "
        "print(ns['_f_from_yaml']('enabled: true\\nitems:\\n  - one\\n  - two'))"
    )

    proc = subprocess.run(
        [sys.executable, "-S", "-c", probe, str(agent)],
        capture_output=True,
        text=True,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "2.11.3",
        "5.3.1",
        "False",
        "hello world",
        "{'enabled': True, 'items': ['one', 'two']}",
    ]


def test_accelerator_classification_is_compile_time_when_possible():
    assert _classify_accelerator(
        "ansible.builtin.command", {"_raw_params": "printf ok"}, None
    ) == "always"
    assert _classify_accelerator(
        "command", {"_raw_params": "printf {{ value }}"}, None
    ) == "dynamic"
    assert _classify_accelerator(
        "file", {"path": "/tmp/x", "state": "directory", "recurse": True}, None
    ) == "always"
    assert _classify_accelerator(
        "ansible.builtin.systemd",
        {"name": "cron.service", "state": "started", "enabled": True}, None,
    ) == "always"
    assert _classify_accelerator(
        "systemd", {"name": "cron", "state": "started", "daemon_reload": True},
        None,
    ) is None
    assert _classify_accelerator(
        "apt",
        {
            "pkg": ["mosquitto", "openhab2"],
            "state": "absent",
            "purge": True,
            "autoclean": True,
            "autoremove": True,
        },
        None,
    ) == "always"
    assert _classify_accelerator(
        "apt", {"pkg": ["mosquitto"], "state": "present"}, None
    ) is None
    # A literal loop does not change the supported argument shape.
    assert _classify_accelerator(
        "command", {"_raw_params": "printf ok"}, [1, 2]
    ) == "always"


def test_legacy_command_warn_classification_tracks_ansible_support(monkeypatch):
    from ansiroll import loader

    args = {"_raw_params": "apt-get clean", "warn": False}
    monkeypatch.setattr(loader, "_legacy_command_warn_supported", lambda: True)
    assert loader._classify_accelerator("command", args, None) == "always"

    monkeypatch.setattr(loader, "_legacy_command_warn_supported", lambda: False)
    assert loader._classify_accelerator("command", args, None) is None


def test_generated_agent_loads_accelerator_from_bundle_under_python_s(tmp_path):
    destination = tmp_path / "created-by-accelerator"
    task = {
        "type": "task", "uuid": "accelerated-1", "name": "accelerated file",
        "action": "ansible.builtin.file", "special": None,
        "args": {"path": str(destination), "state": "directory"},
        "accelerator": "always", "when": [], "loop": None,
        "loop_var": "item", "vars": {}, "become": False,
    }
    stat_task = {
        "type": "task", "uuid": "accelerated-2", "name": "accelerated stat",
        "action": "ansible.builtin.stat", "special": None,
        "args": {"path": str(destination / "missing")},
        "accelerator": "always", "when": [], "loop": None,
        "loop_var": "item", "vars": {}, "become": False,
    }
    play = {
        "name": "accelerated", "hosts": "localhost", "vars": {},
        "tasks": [task, stat_task], "handlers": [], "gather_facts": False,
    }
    agent = tmp_path / "accelerated-agent.py"
    agent.write_text(_render_source([play], {}, ""))
    env = dict(os.environ)
    env.update({"PYTHONPATH": "", "ANSIROLL_ACCELERATED_MODULES": "file,stat"})

    proc = subprocess.run(
        [sys.executable, "-S", str(agent)], capture_output=True, text=True, env=env
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert destination.is_dir()


@pytest.mark.parametrize("action", ["ansible.builtin.stat", "shell"])
def test_total_static_accelerator_replaces_official_module(action):
    task = {
        "type": "task",
        "action": action,
        "accelerator": "always",
        "args": {"path": "/tmp/example"},
        "async_val": 0,
    }
    play = {"tasks": [task], "handlers": [], "gather_facts": False}

    actions = _accelerated_only_actions(
        [play], {action}
    )
    _mark_required_accelerators([play], actions)

    assert actions == {action}
    assert task["accelerator"] == "required"


@pytest.mark.parametrize(
    "task_update",
    [
        {"accelerator": "dynamic"},
        {"async_val": 10},
        {"fallback": True},
    ],
)
def test_non_total_accelerator_call_keeps_official_module(task_update):
    task = {
        "type": "task",
        "action": "ansible.builtin.stat",
        "accelerator": "always",
        "args": {"path": "/tmp/example"},
        "async_val": 0,
    }
    task.update(task_update)
    play = {"tasks": [task], "handlers": [], "gather_facts": False}

    assert _accelerated_only_actions(
        [play], {"ansible.builtin.stat"}
    ) == set()


def test_fixed_variables_specialize_dynamic_accelerator_without_rewriting_args():
    task = {
        "type": "task",
        "action": "ansible.builtin.stat",
        "accelerator": "dynamic",
        "args": {"path": "{{ root }}/{{ filename }}"},
    }
    play = {"tasks": [task], "handlers": []}

    count = _specialize_accelerators(
        [play], {"root": "/tmp", "filename": "example"}
    )

    assert count == 1
    assert task["accelerator"] == "always"
    assert task["args"] == {"path": "{{ root }}/{{ filename }}"}


def test_generated_agent_yaml_filters_work_under_agent_bootstrap():
    # Drive the real AGENT_BOOTSTRAP: it reads a length-prefixed source frame
    # from stdin, execs it without __file__, then calls agent_main().
    import struct
    from ansiroll.runtime import AGENT_BOOTSTRAP

    source = _render_source([], {}, "") + (
        "\n\ndef agent_main():\n"
        "    print(_f_from_yaml('a: 1'))\n"
        "    print(_f_to_yaml({'a': 1}), end='')\n"
    )
    payload = source.encode("utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    proc = subprocess.run(
        [sys.executable, "-S", "-c", AGENT_BOOTSTRAP],
        input=struct.pack(">I", len(payload)) + payload,
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout.decode().splitlines() == ["{'a': 1}", "a: 1"]
