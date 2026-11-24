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


@pytest.mark.parametrize(("name", "expression"), [
    ("run_task", "group_names is defined"),
    ("run_task", "play_flag | default(false)"),
    ("dict", "group_names is defined"),
])
def test_tree_shaking_keeps_inventory_dependent_extra_var_condition(tmp_path, name, expression):
    from ansiroll import compile_playbook

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: all
  gather_facts: false
  vars:
    play_flag: '{{ group_names is defined }}'
  tasks:
    - debug: {msg: inventory is available}
      when: %s
""" % name)
    contents, _ = compile_playbook(str(playbook), tree_shake=True,
                                   extra_vars={name: "{{ " + expression + " }}"})
    compiled = tmp_path / "compiled.py"
    compiled.write_text(contents)
    env = dict(os.environ, ANSIROLL_RAW="1")
    completed = subprocess.run([sys.executable, str(compiled)], env=env, capture_output=True, text=True, check=True)
    frame, = [json.loads(line.split(" ", 1)[1]) for line in completed.stdout.splitlines()
              if line.startswith("__ANSIROLL_RESULT__ ")]
    assert frame["result"]["msg"] == "inventory is available"


@pytest.mark.parametrize("group_vars", [False, True])
def test_freezing_allows_explicit_inventory_values(tmp_path, group_vars):
    from ansiroll import compile_playbook

    playbook = tmp_path / "play.yml"
    explicit_group = "group_names: [workers]"
    if group_vars:
        (tmp_path / "group_vars").mkdir()
        (tmp_path / "group_vars" / "all.yml").write_text(explicit_group + "\n")
    playbook.write_text("""- hosts: all
  gather_facts: false
  vars:
%s    deployment_group: '{{ group_names[0] }}'
  tasks:
    - debug: {msg: '{{ deployment_group }}'}
""" % ("" if group_vars else "    " + explicit_group + "\n"))
    _, manifest = compile_playbook(str(playbook), freeze_vars=["deployment_group"])
    assert manifest["tree_shake"]["frozen_var_names"] == ["deployment_group"]


@pytest.mark.parametrize(("selector", "inventory", "fallback"), [
    ("selection-{{ 'inventory' if group_names is defined else 'fallback' }}.yml",
     "selection-inventory.yml", "selection-fallback.yml"),
    ("{{ 'config/inventory.yml' if group_names is defined else 'config/fallback.yml' }}",
     "config/inventory.yml", "config/fallback.yml"),
    ("{{ (group_names is defined) | ternary('config/inventory.yml', 'config/fallback.yml') }}",
     "config/inventory.yml", "config/fallback.yml"),
    ("{{ (group_names is defined) | ansible.builtin.ternary('config/inventory.yml', 'config/fallback.yml') }}",
     "config/inventory.yml", "config/fallback.yml"),
    ("{{ (group_names is defined) | ansible.legacy.ternary('config/inventory.yml', 'config/fallback.yml') }}",
     "config/inventory.yml", "config/fallback.yml"),
    ("{{ (group_names is defined) | ternary('config/inventory.yml', 'config/fallback.yml', 'config/unknown.yml') }}",
     "config/inventory.yml", "config/fallback.yml"),
    ("{{ (group_names is defined) | ternary('config/inventory.yml', 'config/fallback.yml', none_val='config/unknown.yml') }}",
     "config/inventory.yml", "config/fallback.yml"),
    ("{{ conditional_path }}", "config/inventory.yml", "config/fallback.yml"),
    ("{{ ternary_path }}", "config/inventory.yml", "config/fallback.yml"),
])
def test_standalone_vars_files_select_inventory_at_runtime(tmp_path, selector, inventory, fallback):
    from ansiroll import compile_playbook

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: all
  gather_facts: false
  vars:
    final_path: config/final.yml
    conditional_path: "{{ 'config/inventory.yml' if group_names is defined else 'config/fallback.yml' }}"
    ternary_path: "{{ (group_names is defined) | ternary('config/inventory.yml', 'config/fallback.yml') }}"
  vars_files:
    - 'first-{{ profile }}.yml'
    - middle.yml
    - "%s"
    - '{{ final_path }}'
  tasks:
    - debug: {msg: '{{ selected }}/{{ precedence }}/{{ first_layer }}'}
""" % selector)
    (tmp_path / inventory).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / inventory).write_text("selected: inventory\nprecedence: earlier\n")
    (tmp_path / fallback).write_text("selected: fallback\nprecedence: earlier\n")
    (tmp_path / "first-runtime.yml").write_text("first_layer: first\nfinal_path: wrong.yml\n")
    (tmp_path / "middle.yml").write_text("first_layer: middle\nfinal_path: config/final.yml\n")
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "final.yml").write_text("precedence: later\n")
    compiled = tmp_path / "compiled.py"
    compiled.write_text(compile_playbook(str(playbook))[0])
    env = dict(os.environ, ANSIROLL_RAW="1", ANSIROLL_EXTRA_VARS='{"profile": "runtime"}')
    completed = subprocess.run([sys.executable, str(compiled)], env=env, capture_output=True, text=True, check=True)
    frame, = [json.loads(line.split(" ", 1)[1]) for line in completed.stdout.splitlines()
              if line.startswith("__ANSIROLL_RESULT__ ")]
    assert frame["result"]["msg"] == "inventory/later/middle"


def test_replayed_vars_file_unsafe_scalar_replaces_mapping(tmp_path, monkeypatch):
    from ansible import constants as C
    from ansiroll import compile_playbook

    monkeypatch.setenv("ANSIBLE_HASH_BEHAVIOUR", "merge")
    monkeypatch.setattr(C, "DEFAULT_HASH_BEHAVIOUR", "merge")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: all
  gather_facts: false
  vars_files:
    - "selection-{{ 'inventory' if group_names is defined else 'fallback' }}.yml"
    - final.yml
  tasks:
    - debug: {msg: '{{ auth }}'}
""")
    (tmp_path / "selection-inventory.yml").write_text("auth: {user: earlier}\n")
    (tmp_path / "final.yml").write_text("auth: !unsafe '{{ literal }}'\n")
    compiled = tmp_path / "compiled.py"
    compiled.write_text(compile_playbook(str(playbook))[0])
    env = dict(os.environ, ANSIROLL_RAW="1")
    completed = subprocess.run([sys.executable, str(compiled)], env=env, capture_output=True, text=True, check=True)
    frame, = [json.loads(line.split(" ", 1)[1]) for line in completed.stdout.splitlines()
              if line.startswith("__ANSIROLL_RESULT__ ")]
    assert frame["result"]["msg"] == "{{ literal }}"


def test_deferred_vars_file_can_change_a_later_selector(tmp_path):
    from ansiroll import compile_playbook

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: all
  gather_facts: false
  vars: {flavor: default}
  vars_files:
    - 'gather-{{ profile }}.yml'
    - '{{ flavor }}.yml'
  tasks:
    - debug: {msg: '{{ picked }}'}
""")
    (tmp_path / "gather-runtime.yml").write_text("flavor: custom\n")
    (tmp_path / "default.yml").write_text("picked: default\n")
    (tmp_path / "custom.yml").write_text("picked: custom\n")
    compiled = tmp_path / "compiled.py"
    compiled.write_text(compile_playbook(str(playbook))[0])
    env = dict(os.environ, ANSIROLL_RAW="1", ANSIROLL_EXTRA_VARS='{"profile": "runtime"}')
    completed = subprocess.run([sys.executable, str(compiled)], env=env, capture_output=True, text=True, check=True)
    frame, = [json.loads(line.split(" ", 1)[1]) for line in completed.stdout.splitlines()
              if line.startswith("__ANSIROLL_RESULT__ ")]
    assert frame["result"]["msg"] == "custom"


@pytest.mark.parametrize("selector", ["final_path", "path_alias", "path_template", "absolute_path_template", "cfg.path"])
def test_deferred_vars_file_can_select_a_known_nested_path(tmp_path, monkeypatch, selector):
    from ansible import constants as C
    from ansiroll.loader import parse_playbook
    from ansiroll.runtime import Runtime

    merge_hashes = selector == "cfg.path"
    monkeypatch.setenv("ANSIBLE_HASH_BEHAVIOUR", "merge" if merge_hashes else "replace")
    monkeypatch.setattr(C, "DEFAULT_HASH_BEHAVIOUR", "merge" if merge_hashes else "replace")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: all
  gather_facts: false
  vars:
    final_path: config/default.yml
    path_alias: '{{ final_path }}'
    path_template: 'config/{{ variant }}.yml'
    absolute_path_template: '{{ playbook_dir }}/config/{{ variant }}.yml'
    variant: default
    cfg: {path: config/default.yml}
  vars_files:
    - 'first-{{ profile }}.yml'
    - '{{ %s }}'
  tasks:
    - debug: {msg: '{{ selected }}'}
""" % selector)
    (tmp_path / "first-runtime.yml").write_text(
        "final_path: config/final.yml\nvariant: final\ncfg: {path: config/final.yml}\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.yml").write_text("selected: default\n")
    (tmp_path / "config" / "final.yml").write_text("selected: final\n")
    extra = {"cfg": {"flag": True}} if merge_hashes else {}
    plays, _ = parse_playbook(str(playbook), extra_vars=extra)
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "h"}, extra_vars={**extra, "profile": "runtime"},
                      on_result=frames.append)
    runtime.run_play(plays[0])
    assert [frame["result"]["msg"] for frame in frames] == ["final"]


def test_deferred_vars_file_projects_duplicate_aliases_once(tmp_path, monkeypatch):
    from jinja2 import Environment
    from ansiroll.loader import _known_vars_file_choices

    scope = {f"alias_{index}": "{{ alias_%d }}" % (index + 1) for index in range(9)}
    scope["alias_9"] = "config/final.yml"
    parse = Environment.parse
    parsed = []

    def record_parse(self, source, *args, **kwargs):
        parsed.append(source)
        return parse(self, source, *args, **kwargs)

    monkeypatch.setattr(Environment, "parse", record_parse)
    assert _known_vars_file_choices("{{ alias_0 }}", [scope, dict(scope)], str(tmp_path)) == (
        ("config/final.yml",), False)
    assert len(parsed) == len(set(parsed)) == 10


@pytest.mark.parametrize(("selector", "adjacent_lookup"), [
    ("final.yml", False),
    ("config/{{ inventory_hostname_short | default('fallback') }}.yml", False),
    ("config/{{ inventory_hostname_short | default('fallback') | lower }}.yml", False),
    ("{{ 'config/' ~ (inventory_hostname_short | default('fallback') | lower) ~ '.yml' }}", False),
    ("{{ base_path }}/{{ inventory_hostname_short | default('fallback') }}.yml", False),
    ("config/{{ group_names | default([]) | length }}.yml", False),
    ("config/{{ group_names | count }}.yml", False),
    ("{{ 'final.yml' if group_names | default([]) | length | int else 'fallback.yml' }}", False),
    ("config/{{ group_names | default(['fallback']) | first }}.yml", True),
    ("config/{{ group_names | default(['fallback']) | ansible.builtin.join('-') }}.yml", False),
    ("{{ (group_names is defined) | ansible.builtin.ternary('final.yml', 'fallback.yml') }}", False),
])
def test_vars_file_after_a_deferred_entry_is_visible_to_setup(tmp_path, monkeypatch, selector, adjacent_lookup):
    from ansiroll.loader import parse_playbook
    from ansiroll.runtime import Runtime

    if adjacent_lookup:
        lookup_dir = tmp_path / "lookup_plugins"
        lookup_dir.mkdir()
        (lookup_dir / "unused.py").write_text("raise AssertionError('unused plugin loaded')\n")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: all
  gather_facts: true
  vars: {base_path: config}
  vars_files:
    - 'first-{{ ansible_facts.profile }}.yml'
    - "%s"
  environment:
    ANSIROLL_SETUP_PROFILE: '{{ setup_profile }}'
  tasks: []
""" % selector)
    (tmp_path / "first-runtime.yml").write_text("runtime_choice: first\n")
    (tmp_path / "config").mkdir()
    for name in ("h", "H", "1", "workers"):
        (tmp_path / "config" / (name + ".yml")).write_text("setup_profile: final\n")
    (tmp_path / "final.yml").write_text("setup_profile: final\n")
    plays, _ = parse_playbook(str(playbook))
    runtime = Runtime({}, "", {"inventory_hostname": "H", "inventory_hostname_short": "H",
                               "group_names": ["workers"]})
    captured = []
    monkeypatch.delenv("ANSIROLL_FAST_FACTS", raising=False)

    def run_setup(action, args, **kwargs):
        captured.append((action, runtime._module_env))
        return {"ansible_facts": {"profile": "runtime"}}

    monkeypatch.setattr(runtime, "run_module", run_setup)
    runtime.run_play(plays[0])
    assert captured == [("ansible.builtin.setup", {"ANSIROLL_SETUP_PROFILE": "final"})]
    assert runtime.vars["runtime_choice"] == "first"


@pytest.mark.parametrize("group_selector", [
    "(group_names | default(['fallback']))[0]",
    "(group_names | default(['fallback']))[-1]",
    "group_names | ansible.builtin.default(['fallback']) | ansible.builtin.first",
    "group_names | default(['fallback']) | ansible.builtin.join('-')",
])
def test_standalone_vars_file_bundles_directory_from_group_default(tmp_path, group_selector):
    from ansiroll import compile_playbook

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: all
  gather_facts: false
  vars_files:
    - "{{ %s }}/settings.yml"
  tasks:
    - debug: {msg: '{{ selected }}'}
""" % group_selector)
    (tmp_path / "ungrouped").mkdir()
    (tmp_path / "ungrouped" / "settings.yml").write_text("selected: group defaults\n")
    (tmp_path / "fallback").mkdir()
    (tmp_path / "fallback" / "settings.yml").write_text("selected: fallback defaults\n")
    compiled = tmp_path / "compiled.py"
    compiled.write_text(compile_playbook(str(playbook))[0])
    env = dict(os.environ, ANSIROLL_RAW="1")
    completed = subprocess.run([sys.executable, str(compiled)], env=env, capture_output=True, text=True, check=True)
    frame, = [json.loads(line.split(" ", 1)[1]) for line in completed.stdout.splitlines()
              if line.startswith("__ANSIROLL_RESULT__ ")]
    assert frame["result"]["msg"] == "group defaults"


def test_deferred_vars_file_projects_qualified_join_of_known_and_fallback_lists(tmp_path):
    from ansiroll.loader import _known_vars_file_choices

    scopes = [{"groups_alias": "{{ config.groups }}", "config": {"groups": ["web", "prod"]}}]
    selector = "{{ groups_alias | default(['ungrouped', 'local']) | ansible.builtin.join(d='-') }}/settings.yml"
    choices, exhaustive = _known_vars_file_choices(selector, scopes, str(tmp_path))
    assert set(choices) == {"web-prod/settings.yml", "ungrouped-local/settings.yml"}
    assert not exhaustive


@pytest.mark.parametrize(("kind", "plugin", "selector"), [
    ("filter", "first", "{{ groups | first }}"),
    ("filter", "ternary", "{{ true | ansible.legacy.ternary('a', 'b') }}"),
    ("test", "defined", "{{ groups is defined }}"),
])
def test_vars_file_setup_safety_skips_overridden_plugins(kind, plugin, selector):
    from ansiroll.runtime import Runtime

    runtime = Runtime({}, "", {})
    ctx = {"groups": ["workers"]}
    assert runtime._pre_setup_vars_file_selector(selector, ctx)
    runtime.templater.register_plugins([{"kind": kind, "name": plugin, "source": f"""
class {kind.title()}Module:
    def {kind}s(self):
        return {{'{plugin}': lambda value: value}}
"""}])
    assert not runtime._pre_setup_vars_file_selector(selector, ctx)


@pytest.mark.parametrize("runtime_vars", ["variant: runtime\n", "- variant: ignored\n- variant: runtime\n"])
def test_vars_file_uses_available_values_before_setup_and_replays_afterward(tmp_path, monkeypatch, runtime_vars):
    from ansiroll.loader import parse_playbook
    from ansiroll.runtime import Runtime

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: all
  gather_facts: true
  vars: {variant: default}
  vars_files:
    - 'first-{{ ansible_facts.profile }}.yml'
    - 'config/{{ variant }}.yml'
  environment:
    ANSIROLL_SETUP_PROFILE: '{{ setup_profile }}'
  tasks: []
""")
    (tmp_path / "first-runtime.yml").write_text(runtime_vars)
    (tmp_path / "config").mkdir()
    for variant in ("default", "runtime"):
        (tmp_path / "config" / (variant + ".yml")).write_text(f"setup_profile: {variant}\n")
    plays, _ = parse_playbook(str(playbook))
    runtime = Runtime({}, "", {"inventory_hostname": "h"})
    captured = []
    monkeypatch.delenv("ANSIROLL_FAST_FACTS", raising=False)

    def run_setup(action, args, **kwargs):
        captured.append((action, runtime._module_env))
        return {"ansible_facts": {"profile": "runtime"}}

    monkeypatch.setattr(runtime, "run_module", run_setup)
    runtime.run_play(plays[0])
    assert captured == [("ansible.builtin.setup", {"ANSIROLL_SETUP_PROFILE": "default"})]
    assert runtime.vars["setup_profile"] == "runtime"


@pytest.mark.parametrize("static_override", [True, False])
def test_include_baking_accounts_for_inventory_vars_in_replay(tmp_path, static_override):
    from ansiroll import compile_playbook

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: all
  gather_facts: false
  vars: {inventory_hostname: earlier}
  vars_files:
    - 'first-{{ profile }}.yml'
%s  tasks:
    - include_tasks: '{{ inventory_hostname }}.yml'
""" % ("    - final.yml\n" if static_override else ""))
    (tmp_path / "first-runtime.yml").write_text("inventory_hostname: dynamic\n")
    (tmp_path / "final.yml").write_text("inventory_hostname: later\n")
    (tmp_path / "earlier.yml").write_text("- debug: {msg: earlier}\n")
    (tmp_path / "later.yml").write_text("- debug: {msg: later}\n")
    source, manifest = compile_playbook(str(playbook))
    if not static_override:
        assert any("include_tasks" in task["reason"] for task in manifest["fallback_tasks"])
        return
    assert manifest["fallback_tasks"] == []
    compiled = tmp_path / "compiled.py"
    compiled.write_text(source)
    env = dict(os.environ, ANSIROLL_RAW="1", ANSIROLL_EXTRA_VARS='{"profile": "runtime"}')
    completed = subprocess.run([sys.executable, str(compiled)], env=env, capture_output=True, text=True, check=True)
    frames = [json.loads(line.split(" ", 1)[1]) for line in completed.stdout.splitlines()
              if line.startswith("__ANSIROLL_RESULT__ ")]
    assert [frame["result"]["msg"] for frame in frames if frame["action"] == "debug"] == ["later"]


def test_standalone_include_tasks_does_not_bake_inventory_fallback(tmp_path):
    from ansiroll import compile_playbook

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: all
  gather_facts: false
  tasks:
    - include_tasks: "{{ 'inventory.yml' if group_names is defined else 'fallback.yml' }}"
""")
    (tmp_path / "inventory.yml").write_text("- debug: {msg: inventory}\n")
    (tmp_path / "fallback.yml").write_text("- debug: {msg: fallback}\n")
    _, manifest = compile_playbook(str(playbook))
    assert len(manifest["fallback_tasks"]) == 1
    assert "include_tasks" in manifest["fallback_tasks"][0]["reason"]


def test_standalone_include_tasks_does_not_use_role_default_as_inventory(tmp_path):
    from ansiroll import compile_playbook

    role = tmp_path / "roles" / "fixture"
    (role / "defaults").mkdir(parents=True)
    (role / "tasks").mkdir()
    (role / "defaults" / "main.yml").write_text("group_names: [workers]\n")
    (role / "tasks" / "main.yml").write_text("- include_tasks: '{{ group_names[0] }}.yml'\n")
    (role / "tasks" / "workers.yml").write_text("- debug: {msg: workers}\n")
    playbook = tmp_path / "play.yml"
    playbook.write_text("- hosts: all\n  gather_facts: false\n  roles: [fixture]\n")
    _, manifest = compile_playbook(str(playbook))
    assert len(manifest["fallback_tasks"]) == 1
    assert "include_tasks" in manifest["fallback_tasks"][0]["reason"]


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
