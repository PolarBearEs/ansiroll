"""Integration tests that require ansible-core (module embedding, compilation).

Skipped automatically where ansible-core is not importable.
"""

import importlib.util
import base64
import errno
import getpass
import io
import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import textwrap
import time
import zipfile
from types import SimpleNamespace

import pytest

pytest.importorskip("ansible")

from ansiroll.compiler import _build_bundles, _walk_play_nodes, compile_playbook  # noqa: E402
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


def _compiled_plays(src, tmp_path):
    artifact = tmp_path / "compiled.py"
    artifact.write_text(src, encoding="utf-8")
    probe = "import json, runpy, sys; print(json.dumps(runpy.run_path(sys.argv[1])['PLAYS']))"
    result = subprocess.run([sys.executable, "-I", "-S", "-c", probe, str(artifact)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_deferred_vars_file_does_not_embed_controller_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIROLL_TEST_DEFERRED_PREFIX", "region")
    monkeypatch.setenv("ANSIROLL_TEST_SELECTED_ENV", "selected-at-compile")
    monkeypatch.setenv("ANSIROLL_TEST_DEFERRED_ENV", "synthetic-unselected-value")
    monkeypatch.setenv("ANSIBLE_REMOTE_USER", "unselected-config-value")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars_files:
    - selected.yml
    - 'profile-{{ lookup("env", "ANSIROLL_TEST_DEFERRED_PREFIX") }}-{{ profile }}.yml'
  tasks:
    - debug:
        msg:
          selected: '{{ compile_value }}'
          runtime: '{{ runtime_value }}'
""")
    (tmp_path / "selected.yml").write_text('compile_value: "{{ lookup(\'env\', \'ANSIROLL_TEST_SELECTED_ENV\') }}"\n')
    (tmp_path / "profile-region-run.yml").write_text(
        'runtime_value: "{{ lookup(\'env\', \'ANSIROLL_TEST_DEFERRED_ENV\') }}"\n'
        'runtime_config: "{{ lookup(\'config\', \'DEFAULT_REMOTE_USER\') }}"\n')

    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    assert plan["controller_env"] == {
        "ANSIROLL_TEST_DEFERRED_PREFIX": "region",
        "ANSIROLL_TEST_SELECTED_ENV": "selected-at-compile",
    }
    assert "DEFAULT_REMOTE_USER" not in plan["controller_config"]
    for overrides, expected in (({}, ""), ({"runtime_value": "supplied-at-launch"}, "supplied-at-launch")):
        frames = []
        runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, on_result=frames.append,
                          extra_vars={"profile": "run", **overrides})
        runtime.run_play(plan)
        assert [frame["result"]["msg"] for frame in frames] == [{
            "selected": "selected-at-compile", "runtime": expected,
        }]


@pytest.mark.parametrize("function,plugin_prefix,wantlist", [
    ("lookup", "", False),
    ("lookup", "ansible.builtin.", True),
    ("query", "ansible.builtin.", False),
    ("q", "ansible.legacy.", False),
])
def test_compiled_controller_lookups_capture_selected_literal_terms(tmp_path, monkeypatch,
                                                              function, plugin_prefix, wantlist):
    controller_env = {
        "ANSIROLL_LOOKUP_FIRST": "first",
        "ANSIROLL_LOOKUP_SECOND": "second",
        "ANSIROLL_LOOKUP_WHEN_FIRST": "left",
        "ANSIROLL_LOOKUP_WHEN_SECOND": "right",
    }
    for key, value in controller_env.items():
        monkeypatch.setenv(key, value)
    for suffix in ("DEFAULT", "UNSELECTED", "LITERAL", "UNREACHABLE", "UNSAFE", "MAPPING_KEY"):
        monkeypatch.setenv("ANSIROLL_LOOKUP_" + suffix, "synthetic-unused")
    monkeypatch.setenv("ANSIBLE_REMOTE_USER", "fixture-user")
    monkeypatch.setenv("ANSIBLE_TRANSPORT", "fixture-transport")
    monkeypatch.setenv("ANSIBLE_TIMEOUT", "42")
    monkeypatch.setenv("ANSIBLE_FORKS", "17")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars_files: [selected.yml, 'profile-{{ profile }}.yml']
  tasks:
    - debug: {msg: {env: '{{ selected_env }}', config: '{{ selected_config }}'}}
      when: q('env', 'ANSIROLL_LOOKUP_WHEN_FIRST', 'ANSIROLL_LOOKUP_WHEN_SECOND') == ['left', 'right']
""")
    terms = ", wantlist=true" if wantlist else ""
    (tmp_path / "selected.yml").write_text(f"""\
selected_env: >-
  {{{{ {function}('{plugin_prefix}env', 'ANSIROLL_LOOKUP_FIRST', 'ANSIROLL_LOOKUP_MISSING',
     'ANSIROLL_LOOKUP_SECOND', default='ANSIROLL_LOOKUP_DEFAULT'{terms}) }}}}
selected_config: >-
  {{{{ {function}('{plugin_prefix}config', 'DEFAULT_REMOTE_USER', 'DEFAULT_TRANSPORT'{terms}) }}}}
literal: "q('env', 'ANSIROLL_LOOKUP_LITERAL')"
unsafe: !unsafe "{{{{ {function}('env', 'ANSIROLL_LOOKUP_UNSAFE') }}}}"
unreachable: "{{{{ 'plain' if true else {function}('env', 'ANSIROLL_LOOKUP_UNREACHABLE') }}}}"
examples:
  "{{{{ q('env', 'ANSIROLL_LOOKUP_MAPPING_KEY') }}}}": literal-key
""")
    (tmp_path / "profile-unused.yml").write_text(f"""\
unselected_env: "{{{{ {function}('env', 'ANSIROLL_LOOKUP_UNSELECTED') }}}}"
unselected_config: "{{{{ {function}('config', 'DEFAULT_TIMEOUT', 'DEFAULT_FORKS') }}}}"
""")
    monkeypatch.delenv("ANSIROLL_LOOKUP_MISSING", raising=False)

    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    assert plan["controller_env"] == controller_env
    assert plan["controller_config"]["DEFAULT_REMOTE_USER"] == "fixture-user"
    assert plan["controller_config"]["DEFAULT_TRANSPORT"] == "fixture-transport"
    assert {"DEFAULT_TIMEOUT", "DEFAULT_FORKS"}.isdisjoint(plan["controller_config"])
    for key in controller_env:
        monkeypatch.setenv(key, "launch-only-value")
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, on_result=frames.append,
                      extra_vars={"profile": "unused"})
    runtime.run_play(plan)
    expected_env = ["first", "ANSIROLL_LOOKUP_DEFAULT", "second"]
    expected_config = ["fixture-user", "fixture-transport"]
    if function == "lookup" and not wantlist:
        expected_env = ",".join(expected_env)
        expected_config = ",".join(expected_config)
    assert [frame["result"]["msg"] for frame in frames] == [{"env": expected_env, "config": expected_config}]


def test_compiled_controller_lookups_use_selected_template_delimiters(tmp_path, monkeypatch):
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "selected.j2").write_text("""\
{% set fallback = "{{ lookup('env', 'ANSIROLL_LOOKUP_NESTED') }}" %}
[[ lookup('env', 'ANSIROLL_LOOKUP_FIXED') ]]
[[ query('env', 'ANSIROLL_LOOKUP_FIRST', 'ANSIROLL_LOOKUP_SECOND') ]]
[[ lookup('env', 'ANSIROLL_LOOKUP_MISSING', default=fallback) ]]
{{ q('env', 'ANSIROLL_LOOKUP_LITERAL') }}
""")
    (templates / "part-main.j2").write_text("main\n")
    (templates / "part-alt.j2").write_text("{{ q('env', 'ANSIROLL_LOOKUP_ALTERNATE') }}\n")
    (tmp_path / "raw.txt").write_text("{{ q('env', 'ANSIROLL_LOOKUP_COPY') }}\n")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {part: main}
  tasks:
    - template:
        src: selected.j2
        dest: /unused/selected
        variable_start_string: '[['
        variable_end_string: ']]'
    - template: {src: '{{ playbook_dir }}/templates/part-{{ part }}.j2', dest: /unused/part}
    - copy: {src: raw.txt, dest: /unused/copy}
""")
    for term in ("FIXED", "FIRST", "SECOND", "NESTED", "LITERAL", "ALTERNATE", "COPY"):
        monkeypatch.setenv("ANSIROLL_LOOKUP_" + term, term.lower())
    monkeypatch.delenv("ANSIROLL_LOOKUP_MISSING", raising=False)

    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    baked_paths = {path for node in _walk_play_nodes([plan]) for path in node.get("_baked", {})}
    assert str(templates / "part-alt.j2") in baked_paths
    assert "raw.txt" in baked_paths
    assert plan["controller_env"] == {"ANSIROLL_LOOKUP_" + term: term.lower()
                                      for term in ("FIXED", "FIRST", "SECOND", "NESTED")}


def test_compiled_preserves_single_lookup_in_dynamic_assert_args(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIROLL_LOOKUP_ASSERT", "controller-value")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars:
    checks:
      that:
        - "lookup('env', 'ANSIROLL_LOOKUP_ASSERT') == 'controller-value'"
  tasks:
    - assert: {}
      args: '{{ checks }}'
""")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    monkeypatch.setenv("ANSIROLL_LOOKUP_ASSERT", "launch-value")
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"})
    runtime.run_play(plan)
    assert runtime.stats["ok"] == 1
    assert not runtime.failed


def test_unreadable_deferred_vars_file_reports_an_error_only_when_selected(tmp_path, monkeypatch):
    import builtins

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars_files: ['vars-{{ profile }}.yml']
  tasks:
    - debug: {msg: "{{ selected }}"}
""")
    blocked = tmp_path / "vars-blocked.yml"
    blocked.write_text("selected: unavailable\n")
    (tmp_path / "vars-safe.yml").write_text("selected: selected-safe-candidate\n")
    original_open = builtins.open

    def deny_candidate(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and os.fspath(path) == str(blocked):
            raise PermissionError("synthetic read denial for candidate")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", deny_candidate)
        source, _ = compile_playbook(str(playbook))
    script = tmp_path / "compiled.py"
    script.write_text(source)

    environment = os.environ.copy()
    environment.pop("ANSIROLL_VARS_FILE", None)

    def run(profile):
        return subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True,
                              env={**environment, "ANSIROLL_EXTRA_VARS": json.dumps({"profile": profile})},
                              timeout=20)

    safe = run("safe")
    assert safe.returncode == 0, safe.stdout + safe.stderr
    assert "selected-safe-candidate" in safe.stdout
    error = run("blocked")
    assert error.returncode == 1, error.stdout + error.stderr
    assert "synthetic read denial for candidate" in error.stderr


def test_deferred_vars_file_uses_validated_source_bytes(tmp_path, monkeypatch):
    import builtins
    from ansible.parsing.dataloader import DataLoader
    from ansiroll.loader import _bundled_vars_file

    source = tmp_path / "candidate.yml"
    source.write_text('value: "{{ preserved_template }}"\n')
    opens = []
    flags = []
    original_open = builtins.open
    original_os_open = os.open

    def spy_file(path, *args, **kwargs):
        if not isinstance(path, int) and os.fsdecode(path) == str(source):
            opens.append(path)
        return original_open(path, *args, **kwargs)

    def spy_descriptor(path, mode, *args, **kwargs):
        if os.fsdecode(path) == str(source):
            flags.append(mode)
        return original_os_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_file)
    monkeypatch.setattr(os, "open", spy_descriptor)
    monkeypatch.setattr(DataLoader, "load_from_file",
                        lambda *args, **kwargs: pytest.fail("candidate must not be reopened"))
    assert _bundled_vars_file(SimpleNamespace(_loader=DataLoader()), str(source)) == [
        {"value": "{{ preserved_template }}"}]
    assert len(opens) == len(flags) == 1
    assert flags[0] & os.O_NONBLOCK


def test_deferred_vars_file_rejects_nonregular_descriptor(tmp_path, monkeypatch):
    import stat
    from ansible.parsing.dataloader import DataLoader
    from ansiroll.loader import _bundled_vars_file

    source = tmp_path / "candidate.yml"
    source.write_text("value: example\n")
    with monkeypatch.context() as patch:
        patch.setattr(os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFIFO))
        result = _bundled_vars_file(SimpleNamespace(_loader=DataLoader()), str(source))
    assert "expected a regular file" in result["__ansiroll_vars_file_error__"]
    assert result["rc"] == 1


def test_deferred_vars_file_uses_controller_path_expansion(tmp_path, monkeypatch):
    from ansible.parsing.dataloader import DataLoader
    from ansiroll.loader import _bundled_vars_file

    monkeypatch.setenv("ANSIROLL_TEST_VAR_PATH", "selected")
    (tmp_path / "vars-selected.yml").write_text("value: correct\n")
    literal = tmp_path / "vars-$ANSIROLL_TEST_VAR_PATH.yml"
    literal.write_text("value: literal\n")
    assert _bundled_vars_file(SimpleNamespace(_loader=DataLoader()), str(literal)) == [{"value": "correct"}]


def test_deferred_vars_file_retains_native_json_fallback(tmp_path):
    from ansible.parsing.dataloader import DataLoader
    from ansiroll.loader import _bundled_vars_file

    source = tmp_path / "candidate.json"
    source.write_text('{"service": {"__ansible_vault": false}}')
    loader = DataLoader()
    expected = loader.load_from_file(str(source))
    if not isinstance(expected.get("service"), dict):
        pytest.skip("this Core release treats the JSON wrapper as Vault data")
    assert _bundled_vars_file(SimpleNamespace(_loader=loader), str(source)) == [expected]


def test_ordered_vars_files_capture_env_before_later_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIROLL_TEST_ORDERED_PREFIX", "compile")
    monkeypatch.setenv("ANSIROLL_TEST_ORDERED_SELECTOR", "chosen")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {timeout_seconds: 23}
  gather_timeout: '{{ timeout_seconds }}'
  vars_files:
    - 'settings-{{ profile }}.yml'
    - settings-default.yml
    - '{{ lookup("env", "ANSIROLL_TEST_ORDERED_SELECTOR") }}-{{ config.prefix }}{{ suffix | default("") }}.yml'
    - override.yml
  tasks: []
""")
    (tmp_path / "settings-default.yml").write_text(
        'config: {prefix: "{{ lookup(\'env\', \'ANSIROLL_TEST_ORDERED_PREFIX\') }}"}\n')
    (tmp_path / "settings-run.yml").write_text("suffix: '-run'\n")
    (tmp_path / "chosen-compile-run.yml").write_text("selected: expected\n")
    (tmp_path / "override.yml").write_text("config: {prefix: overridden}\n")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    assert plan["controller_env"] == {
        "ANSIROLL_TEST_ORDERED_PREFIX": "compile",
        "ANSIROLL_TEST_ORDERED_SELECTOR": "chosen",
    }
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "run"})
    monkeypatch.setattr(runtime, "run_module", lambda *args, **kwargs: {"ansible_facts": {}})
    runtime.run_play(plan)
    assert runtime.vars["selected"] == "expected"


def test_discarded_static_vars_file_lookup_is_omitted(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIROLL_TEST_SHADOWED_ENV", "already-overridden")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {timeout_seconds: 23}
  gather_timeout: '{{ timeout_seconds }}'
  vars_files: [first.yml, last.yml]
  tasks: []
""")
    (tmp_path / "first.yml").write_text('value: "{{ lookup(\'env\', \'ANSIROLL_TEST_SHADOWED_ENV\') }}"\n')
    (tmp_path / "last.yml").write_text("value: last\n")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    assert plan["vars"]["value"] == "last"
    assert "ordered_vars_files" not in plan
    assert "ANSIROLL_TEST_SHADOWED_ENV" not in plan.get("controller_env", {})


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


def _command_parity_cases(base):
    os.makedirs(os.path.join(base, "sub"))
    open(os.path.join(base, "sub", "marker"), "w").close()
    return [
        # chdir is applied (and ~-expanded) before creates/removes.
        ("command", {"_raw_params": "echo ran", "chdir": os.path.join(base, "sub"),
                     "creates": "marker"}),
        ("command", {"_raw_params": "pwd", "chdir": "~"}),
        ("command", {"_raw_params": "true", "chdir": os.path.join(base, "nope")}),
        ("command", {"_raw_params": "echo x", "creates": "~/"}),
        # argv gets ~ and $VAR expansion, the reported cmd does not.
        ("command", {"_raw_params": "echo ~ $HOME '$HOME' ~root/x"}),
        # Bytes output: no newline translation, surrogateescape decoding.
        ("shell", {"_raw_params": "printf '\\377ok\\r\\n'", "_uses_shell": True}),
        ("shell", {"_raw_params": "printf 'a\\r\\nb\\r\\n'", "_uses_shell": True,
                   "strip_empty_ends": False}),
        # python -c 'import ...' is run as a real process.
        ("command", {"_raw_params": "%s -c 'import ansiroll_no_such_mod'" % sys.executable}),
        ("command", {"_raw_params": "ansiroll_no_such_cmd_xyz --password=x -p y"}),
        ("command", {"_raw_params": "./ansiroll-relative"}),
        ("shell", {"_raw_params": "echo $0 x", "_uses_shell": True,
                   "executable": "/bin/bash"}),
        ("shell", {"_raw_params": "echo $0 x", "_uses_shell": True,
                   "executable": "/nonexistent/sh"}),
    ]


def test_accelerated_command_edge_cases_match_official(tmp_path):
    from ansiroll.loader import _accelerator_options

    cases = _command_parity_cases(str(tmp_path))
    emb = ModuleEmbedder()
    rt = Runtime({"command": emb.embed("command"), "shell": emb.embed("shell")},
                 "", {"inventory_hostname": "localhost"})
    skip = ("start", "end", "delta", "invocation", "exception")
    try:
        for action, args in cases:
            official = rt.run_module(action, dict(args))
            accelerated = run_accelerated(
                action, dict(args), options=_accelerator_options(action))
            official = {k: v for k, v in official.items() if k not in skip}
            accelerated = {k: v for k, v in accelerated.items() if k not in skip}
            assert accelerated == official, (action, args)
    finally:
        rt.cleanup()


@pytest.mark.parametrize("search_only", [False, True])
def test_accelerated_command_guards_use_task_environment_and_literal_chdir(
        tmp_path, monkeypatch, search_only):
    from ansiroll.loader import _accelerator_options

    work = tmp_path / "literal[1]"
    work.mkdir()
    (work / "guard.txt").touch()
    (tmp_path / "absolute.txt").touch()
    monkeypatch.setenv("ANSIROLL_GUARD", "missing")
    monkeypatch.setenv("ANSIROLL_COMMAND_PATH", str(tmp_path))
    environment = {
        "ANSIROLL_GUARD": "guard.*",
        "ANSIROLL_COMMAND_PATH": str(work),
    }
    cases = [
        ({"creates": "$ANSIROLL_GUARD"}, False),
        ({"removes": "$ANSIROLL_GUARD"}, True),
        ({"removes": "missing"}, False),
        ({"creates": str(tmp_path / "absolute.txt")}, False),
    ]
    start = tmp_path / "start"
    start.mkdir()
    monkeypatch.chdir(start)
    if search_only:
        if os.geteuid() == 0:
            pytest.skip("root can read a search-only working directory")
        start.chmod(0o111)
    rt = Runtime({"command": ModuleEmbedder().embed("command")},
                 "", {"inventory_hostname": "localhost"})
    rt._module_env = environment
    cwd = os.getcwd()
    try:
        for guard, changed in cases:
            args = dict(guard, chdir="$ANSIROLL_COMMAND_PATH",
                        _raw_params="printf %s $ANSIROLL_GUARD")
            official = rt.run_module("command", dict(args))
            accelerated = run_accelerated(
                "command", args, environment=environment,
                options=_accelerator_options("command"))
            ignored = ("start", "end", "delta", "invocation")
            assert ({k: v for k, v in accelerated.items() if k not in ignored}
                    == {k: v for k, v in official.items() if k not in ignored})
            assert accelerated["changed"] is changed
            if changed:
                assert accelerated["stdout"] == "guard.*"
            assert os.getcwd() == cwd
            assert os.environ["ANSIROLL_GUARD"] == "missing"
    finally:
        start.chmod(0o700)
        rt.cleanup()


def test_compiled_command_loop_restores_search_only_cwd(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can read a search-only working directory")
    start = tmp_path / "start"
    start.mkdir()
    guarded = tmp_path / "literal[1]"
    guarded.mkdir()
    (guarded / "marker").touch()
    unguarded = tmp_path / "literal[2]"
    unguarded.mkdir()
    playbook = tmp_path / "guards.yml"
    playbook.write_text(
        """- hosts: localhost
  gather_facts: false
  tasks:
    - command:
        cmd: pwd
        chdir: "{{ item }}"
        creates: mark*
      loop: %s
    - command: pwd
""" % json.dumps([str(guarded), str(unguarded)]))
    script = tmp_path / "compiled.py"
    script.write_text(compile_playbook(str(playbook))[0])
    start.chmod(0o111)
    try:
        proc = subprocess.run([sys.executable, str(script)], cwd=str(start),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, ANSIROLL_RAW="1",
                                       ANSIROLL_ACCELERATED_MODULES="command"),
                              universal_newlines=True)
    finally:
        start.chmod(0o700)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    prefix = "__ANSIROLL_RESULT__ "
    events = [json.loads(line[len(prefix):]) for line in proc.stdout.splitlines()
              if line.startswith(prefix)]
    skipped, executed = [event["result"] for event in events if event["is_item"]]
    following = [event["result"] for event in events if not event["is_item"]][-1]
    assert skipped["changed"] is False
    assert executed["changed"] is True
    assert executed["stdout"] == str(unguarded)
    assert following["stdout"] == str(start)


@pytest.mark.parametrize("action,chdir", [("command", None), ("command", 0), ("shell", False)])
def test_compiled_command_falls_back_when_cwd_loses_search_permission(tmp_path, action, chdir):
    if os.geteuid() == 0:
        pytest.skip("root can search a directory without permissions")
    start = tmp_path / "start"
    start.mkdir()
    target = tmp_path / "literal[1]"
    target.mkdir()
    (target / "marker").touch()
    playbook = tmp_path / "guard.yml"
    playbook.write_text(
        """- hosts: localhost
  gather_facts: false
  tasks:
    - file:
        path: %s
        state: directory
        mode: "0000"
    - %s:
        cmd: pwd
        chdir: %s
        creates: marker
      ignore_errors: true
""" % (json.dumps(str(start)), action,
       json.dumps(str(target) if chdir is None else chdir)))
    script = tmp_path / "compiled.py"
    script.write_text(compile_playbook(str(playbook))[0])
    try:
        proc = subprocess.run(
            [sys.executable, str(script)], cwd=str(start),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=dict(os.environ, ANSIROLL_RAW="1", ANSIROLL_ACCELERATED_MODULES=action),
            universal_newlines=True,
        )
    finally:
        start.chmod(0o700)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    prefix = "__ANSIROLL_RESULT__ "
    result = [json.loads(line[len(prefix):])["result"] for line in proc.stdout.splitlines()
              if line.startswith(prefix)][-1]
    assert result["changed"] is False
    if chdir is None:
        assert result["rc"] == 0
        assert "since marker exists" in result["stdout"]
    else:
        assert result["failed"] is True
        assert result["msg"].startswith("Unable to change directory before execution")


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


def test_accelerated_apt_remove_uses_official_apt_environment(tmp_path):
    """apt-get runs with the official module's APT_ENV_VARS; fake dpkg-query and
    apt-get on the task PATH record it, so no package state is touched."""
    import ansible
    from ansiroll.loader import _accelerator_options

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "env.json"
    (bin_dir / "dpkg-query").write_text(
        "#!/bin/sh\nprintf 'ansiroll-fake\\tii \\n'\n")
    (bin_dir / "apt-get").write_text(
        "#!%s\nimport json, os, sys\n"
        "json.dump({'argv': sys.argv[1:], 'env': dict(os.environ)}, open(%r, 'w'))\n"
        % (sys.executable, str(record)))
    for name in ("dpkg-query", "apt-get"):
        os.chmod(str(bin_dir / name), 0o755)
    environment = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "LANG": "es_ES.UTF-8", "LC_ALL": "es_ES.UTF-8", "LANGUAGE": "es",
        "DEBIAN_FRONTEND": "dialog",
    }
    result = run_accelerated(
        "apt", {"name": "ansiroll-fake", "state": "absent"},
        environment=environment, options=_accelerator_options("apt"))
    assert result.get("changed") is True, result

    class _Module(object):
        def get_bin_path(self, name):
            return shutil.which(name, path=environment["PATH"])

        def run_command(self, args):
            proc = subprocess.run(args, capture_output=True, text=True,
                                  env=dict(os.environ, **environment))
            return proc.returncode, proc.stdout, proc.stderr

        def debug(self, msg):
            pass

    source = open(os.path.join(
        os.path.dirname(ansible.__file__), "modules", "apt.py")).read()
    block = re.search(r"APT_ENV_VARS = dict\((.*?)\)", source, re.S).group(1)
    locale = None
    if re.search(r"=\s*locale\b", block):
        from ansible.module_utils.common.locale import get_best_parsable_locale
        locale = get_best_parsable_locale(_Module())
    expected = {}
    for key, value in re.findall(r"(\w+)=([^,\n]+)", block):
        expected[key] = locale if value.strip() == "locale" else value.strip("'\" ")
    assert "LANG" in expected and "DEBIAN_FRONTEND" in expected
    recorded = json.load(open(str(record)))
    assert {key: recorded["env"].get(key) for key in expected} == expected
    assert recorded["env"]["LANGUAGE"] == expected.get("LANGUAGE", "es")
    assert recorded["argv"][-2:] == ["remove", "ansiroll-fake"]


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


def _translated_locale():
    """A non-English UTF-8 locale whose strerror() differs from English."""
    try:
        names = subprocess.run(
            ["locale", "-a"], stdout=subprocess.PIPE,
            universal_newlines=True).stdout.split()
    except OSError:
        return None
    for name in names:
        if name.lower().endswith((".utf8", ".utf-8")) and \
                not name.startswith(("C.", "en_", "POSIX")):
            return name
    return None


@pytest.mark.parametrize("task_override", [False, True])
def test_accelerated_modules_apply_locale_like_ansible_module(tmp_path, task_override):
    # AnsibleModule._check_locale() (module_utils/basic.py) runs
    # setlocale(LC_ALL, ''), so official error texts follow the user's locale.
    name = _translated_locale()
    if name is None:
        pytest.skip("no translated UTF-8 locale installed")
    script = (
        "import json, sys\n"
        "from ansiroll.embed import ModuleEmbedder\n"
        "from ansiroll.runtime import Runtime\n"
        "from ansiroll.accelerated_modules import run\n"
        "args = {'src': sys.argv[1]}\n"
        "rt = Runtime({'slurp': ModuleEmbedder().embed('slurp')}, '',\n"
        "             {'inventory_hostname': 'localhost'})\n"
        "try:\n"
        "    task_env = {'LC_ALL': sys.argv[2], 'LANGUAGE': ''} if sys.argv[2] else None\n"
        "    rt._module_env = task_env\n"
        "    official = rt.run_module('slurp', args)\n"
        "finally:\n"
        "    rt.cleanup()\n"
        "official.pop('invocation', None)\n"
        "from ansible.release import __version__ as v\n"
        "new = tuple(map(int, v.split('.')[:2])) >= (2, 19)\n"
        "accelerated = run('slurp', args, environment=task_env, options={\n"
        "    'lowercase_not_found': not new, 'typed_exceptions': new})\n"
        "print(json.dumps([official, accelerated]))\n"
    )
    inherited = "C" if task_override else name
    env = dict(os.environ, LANG=inherited, LC_ALL=inherited, LANGUAGE="")
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
         env.get("PYTHONPATH", "")])
    proc = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "missing"),
         name if task_override else ""],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, check=True)
    official, accelerated = json.loads(proc.stdout.splitlines()[-1])
    assert accelerated == official


@pytest.mark.parametrize("path", [None, ""])
def test_accelerated_shell_locale_fallback_matches_official(monkeypatch, path):
    import locale
    from ansiroll.loader import _accelerator_options

    monkeypatch.setenv("LC_ALL", "C")
    original_locale = locale.setlocale(locale.LC_ALL)
    original_env = dict(os.environ)
    args = {
        "_raw_params": 'printf "%s\n" "$LANG" "$LC_ALL" "$LC_MESSAGES"',
        "_uses_shell": True,
    }
    environment = {"LC_ALL": "ansiroll_invalid_locale"}
    if path is not None:
        environment["PATH"] = path
    rt = Runtime({"shell": ModuleEmbedder().embed("shell")},
                 "", {"inventory_hostname": "localhost"})
    rt._module_env = environment
    try:
        official = rt.run_module("shell", dict(args))
        accelerated = run_accelerated(
            "shell", args, environment=environment, options=_accelerator_options("shell"))
        following = run_accelerated("shell", args)
    finally:
        rt.cleanup()
    assert accelerated["stdout_lines"] == official["stdout_lines"]
    fallback = official["stdout_lines"][0]
    assert fallback and fallback != environment["LC_ALL"]
    assert accelerated["stdout_lines"] == [fallback] * 3
    assert following["stdout_lines"][1] == "C"
    assert locale.setlocale(locale.LC_ALL) == original_locale
    assert dict(os.environ) == original_env


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
    from ansiroll.loader import _accelerator_options

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
    accelerated = run_accelerated(
        "replace",
        dict(args, path=str(accelerated_path)),
        options=_accelerator_options("replace"),
    )

    official.pop("invocation", None)
    assert accelerated == official
    assert accelerated_path.read_bytes() == official_path.read_bytes()


@pytest.mark.parametrize("action,args", [
    ("replace", {"regexp": "^old=.*$", "replace": "new=2"}),
    ("blockinfile", {"insertafter": "EOF", "block": "new=2"}),
])
def test_accelerated_atomic_write_matches_official_when_chown_is_denied(
        tmp_path, monkeypatch, action, args):
    """Non-root replacing a file it may not chown back (another owner, or a
    group it is not in): atomic_move ignores EPERM and still writes."""
    import errno as errno_module
    from ansiroll.loader import _accelerator_options

    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "sitecustomize.py").write_text(
        "import errno, os\n"
        "def _denied(*args, **kwargs):\n"
        "    raise OSError(errno.EPERM, 'Operation not permitted')\n"
        "os.chown = _denied\n"
    )
    official_path = tmp_path / "official"
    accelerated_path = tmp_path / "accelerated"
    for path in (official_path, accelerated_path):
        path.write_text("old=1\n")
        os.chmod(str(path), 0o644)
    monkeypatch.setenv("PYTHONPATH", str(hooks))
    meta = ModuleEmbedder().embed(action)
    rt = Runtime({action: meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module(action, dict(args, path=str(official_path)))
    finally:
        rt.cleanup()
    monkeypatch.delenv("PYTHONPATH")

    def denied(*args, **kwargs):
        raise OSError(errno_module.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "chown", denied)
    accelerated = run_accelerated(
        action, dict(args, path=str(accelerated_path)),
        options=_accelerator_options(action))

    official.pop("invocation", None)
    assert official["changed"] is True
    for result, path in ((official, official_path), (accelerated, accelerated_path)):
        if "diff" in result:
            result["diff"] = json.loads(json.dumps(result["diff"]).replace(str(path), "<path>"))
    assert accelerated == official
    assert accelerated_path.read_bytes() == official_path.read_bytes()
    assert b"new=2" in accelerated_path.read_bytes()
    assert (oct(os.stat(str(accelerated_path)).st_mode & 0o7777)
            == oct(os.stat(str(official_path)).st_mode & 0o7777))


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


def test_accelerated_file_results_and_created_parents_match_official(tmp_path):
    import stat as stat_module
    from ansiroll.accelerated_modules import can_accelerate
    from ansiroll.loader import _accelerator_options

    cases = [
        # mode applies to every directory mkdir -p creates.
        ({"path": "a/b/c", "state": "directory", "mode": "0750"}, False),
        ({"path": "a/b/c", "state": "directory", "mode": "0750"}, False),
        ({"path": "existing/new/../target", "state": "directory", "mode": "0700"}, False),
        ({"path": "q/r", "state": "directory", "mode": "g+w"}, False),
        # mode/owner/size/... are reported even when not requested.
        ({"path": "m/n", "state": "directory"}, False),
        ({"path": "exist", "state": "directory", "mode": "0700"}, False),
        ({"path": "exist", "state": "directory", "owner": str(os.getuid())}, False),
        # A regular file is not a directory, in check mode too.
        ({"path": "regular", "state": "directory"}, True),
        ({"path": "regular", "state": "directory"}, False),
        ({"path": "newdir", "state": "directory"}, True),
        ({"path": "t1", "state": "touch"}, False),
        ({"path": "t1", "state": "touch", "mode": "0600"}, False),
        ({"path": "t1", "state": "touch", "mode": "0644"}, True),
        ({"path": "missing-touch", "state": "touch"}, True),
        ({"path": "t3", "state": "touch", "modification_time": "preserve",
          "access_time": "preserve"}, False),
        ({"path": "lnk", "state": "absent"}, True),
        ({"path": "l2", "src": "exist", "state": "link"}, False),
        ({"path": "l2", "src": "exist", "state": "link"}, False),
        ({"path": "regular", "state": "absent"}, False),
        ({"path": "missing", "state": "absent"}, False),
    ]
    from ansible.release import __version__ as ansible_version
    if tuple(map(int, ansible_version.split(".")[:2])) < (2, 19):
        # Before 2.19 the official module raises AnsibleModuleError for a
        # directory over a regular file and relies on its sys.excepthook to
        # emit fail_json, which Runtime's embedded execution does not install;
        # ansible-playbook reports the fail_json the accelerated module returns.
        cases = [case for case in cases
                 if case[0] != {"path": "regular", "state": "directory"}]
    for side in ("official", "accelerated"):
        base = tmp_path / side
        (base / "exist").mkdir(parents=True)
        (base / "existing").mkdir()
        os.chmod(str(base / "existing"), 0o775)
        (base / "regular").write_text("")
        os.chmod(str(base / "regular"), 0o644)
        os.symlink("regular", str(base / "lnk"))

    def tree(base):
        out = {}
        for root, dirs, files in os.walk(base):
            for name in dirs + files:
                full = os.path.join(root, name)
                out[os.path.relpath(full, base)] = oct(
                    stat_module.S_IMODE(os.lstat(full).st_mode))
        return out

    def normalized(result, base):
        result = {k: v for k, v in result.items() if k not in ("invocation", "diff")}
        return json.loads(json.dumps(result).replace(base, "<D>"))

    rt = Runtime({"file": ModuleEmbedder().embed("file")},
                 "", {"inventory_hostname": "localhost"})
    try:
        for args, check in cases:
            official_base = str(tmp_path / "official")
            accelerated_base = str(tmp_path / "accelerated")
            official_args = dict(args, path=os.path.join(official_base, args["path"]))
            accelerated_args = dict(
                args, path=os.path.join(accelerated_base, args["path"]))
            assert can_accelerate("file", dict(accelerated_args), check_mode=check)
            rt.check_mode = check
            official = rt.run_module("file", official_args)
            accelerated = run_accelerated(
                "file", accelerated_args, check_mode=check,
                options=_accelerator_options("file"))
            assert not accelerated.get("_ansiroll_fallback"), args
            assert (normalized(accelerated, accelerated_base)
                    == normalized(official, official_base)), (args, check)
            assert tree(accelerated_base) == tree(official_base), (args, check)
    finally:
        rt.cleanup()


def test_accelerated_file_paths_use_task_environment(tmp_path, monkeypatch):
    from ansiroll.loader import _accelerator_options

    args = {"path": "$ANSIROLL_TARGET", "state": "absent"}
    link = {"path": "~/link", "src": "$ANSIROLL_SOURCE", "state": "link"}
    rt = Runtime({"file": ModuleEmbedder().embed("file")},
                 "", {"inventory_hostname": "localhost"})
    try:
        for side in ("official", "accelerated"):
            base = tmp_path / side
            base.mkdir()
            (base / "stale").touch()
            (base / "wanted").touch()
            monkeypatch.setenv("ANSIROLL_TARGET", str(base / "stale"))
            environment = {
                "HOME": str(base),
                "ANSIROLL_TARGET": str(base / "wanted"),
                "ANSIROLL_SOURCE": "stale",
            }
            rt._module_env = environment
            if side == "official":
                absent = rt.run_module("file", dict(args))
                linked = rt.run_module("file", dict(link))
            else:
                options = _accelerator_options("file")
                absent = run_accelerated(
                    "file", args, environment=environment, options=options)
                linked = run_accelerated(
                    "file", link, environment=environment, options=options)
            assert absent["changed"] is True
            assert linked["changed"] is True
            assert not (base / "wanted").exists()
            assert (base / "stale").is_file()
            assert os.readlink(str(base / "link")) == "stale"
            assert os.environ["ANSIROLL_TARGET"] == str(base / "stale")
    finally:
        rt.cleanup()


@pytest.mark.parametrize(
    "state", ["directory", "recursive_directory", "new_directory", "absent", "touch", "link"])
def test_file_nested_path_expansion_matches_official(tmp_path, state):
    from ansiroll.loader import _accelerator_options

    args = {"path": "$ANSIROLL_FIRST", "state": state}
    if state in ("new_directory", "recursive_directory"):
        args["state"] = "directory"
    if state in ("directory", "new_directory", "touch"):
        args["mode"] = "0700"
    if state == "recursive_directory":
        args.update(mode="g+w", recurse=True)
    if state == "touch":
        args.update(access_time="preserve", modification_time="preserve")
    if state == "link":
        args["src"] = "$ANSIROLL_SOURCE"
    rt = Runtime({"file": ModuleEmbedder().embed("file")},
                 "", {"inventory_hostname": "localhost"})
    try:
        for side in ("official", "accelerated"):
            base = tmp_path / side
            base.mkdir()
            literal = base / "$ANSIROLL_SECOND"
            actual = base / "$ANSIROLL_THIRD"
            actual.mkdir()
            actual.chmod(0o755)
            third = base / "third"
            third.mkdir()
            third.chmod(0o755)
            if state in ("directory", "recursive_directory", "absent", "touch"):
                literal.mkdir()
                literal.chmod(0o755)
            if state == "recursive_directory":
                for directory in (literal, actual):
                    child = directory / "child"
                    child.touch()
                    child.chmod(0o644)
            (base / "$ANSIROLL_TARGET").touch()
            environment = {
                "ANSIROLL_FIRST": str(literal),
                "ANSIROLL_SECOND": "$ANSIROLL_THIRD",
                "ANSIROLL_THIRD": "third",
                "ANSIROLL_SOURCE": "$ANSIROLL_TARGET",
                "ANSIROLL_TARGET": "missing",
            }
            rt._module_env = environment
            if side == "accelerated":
                result = run_accelerated("file", args, environment=environment,
                                         options=_accelerator_options("file"))
                fallback = state in ("touch", "link")
                assert bool(result.get("_ansiroll_fallback")) is fallback
                if fallback:
                    assert literal.exists() is (state == "touch")
                    assert actual.stat().st_mode & 0o777 == 0o755
                    result = rt.run_module("file", dict(args))
            else:
                result = rt.run_module("file", dict(args))
            assert result["changed"] and not result.get("failed"), result
            if state == "absent":
                assert not literal.exists()
            elif state == "link":
                assert os.readlink(str(literal)) == "$ANSIROLL_TARGET"
            else:
                assert literal.stat().st_mode & 0o777 == (
                    0o700 if state == "new_directory" else 0o755)
            if state == "recursive_directory":
                assert actual.stat().st_mode & 0o777 == 0o775
                assert (actual / "child").stat().st_mode & 0o777 == 0o644
                assert (literal / "child").stat().st_mode & 0o777 == 0o664
            else:
                assert actual.stat().st_mode & 0o777 == (
                    0o700 if state in ("directory", "touch") else 0o755)
            assert third.stat().st_mode & 0o777 == 0o755
    finally:
        rt.cleanup()


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


def test_accelerated_blockinfile_create_mode_and_line_splitting_match_official(
        tmp_path):
    import stat as stat_module
    from ansiroll.loader import _accelerator_options

    files = {
        # \x0c, \x1c and U+0085 split str lines but not bytes lines.
        "ff": b"a\x0cb\nc\xc2\x85d\x1ce\n",
        "crlf": b"one\r\ntwo\r\n",
    }
    cases = [
        {"path": "new", "create": True, "block": "x"},
        {"path": "sub/dir/new", "create": True, "block": "x"},
        {"path": "ff", "block": "x", "insertbefore": "b"},
        {"path": "ff", "block": "y"},
        {"path": "crlf", "block": "z", "insertafter": "one"},
    ]
    for side in ("official", "accelerated"):
        (tmp_path / side).mkdir()
        for name, data in files.items():
            (tmp_path / side / name).write_bytes(data)
    rt = Runtime({"blockinfile": ModuleEmbedder().embed("blockinfile")},
                 "", {"inventory_hostname": "localhost"})
    options = _accelerator_options("blockinfile")

    def outcome(result, side, rel):
        base = str(tmp_path / side)
        result = json.loads(json.dumps(
            {k: v for k, v in result.items() if k != "invocation"}
        ).replace(base, "<D>"))
        path = os.path.join(base, rel)
        mode = stat_module.S_IMODE(os.stat(path).st_mode)
        with open(path, "rb") as handle:
            return result, oct(mode), handle.read()

    try:
        for case in cases:
            official = rt.run_module("blockinfile", dict(
                case, path=str(tmp_path / "official" / case["path"])))
            accelerated = run_accelerated("blockinfile", dict(
                case, path=str(tmp_path / "accelerated" / case["path"])),
                options=options)
            assert (outcome(accelerated, "accelerated", case["path"])
                    == outcome(official, "official", case["path"])), case
    finally:
        rt.cleanup()


def test_accelerated_blockinfile_non_utf8_file_matches_official(tmp_path):
    from ansiroll.loader import _accelerator_options

    options = _accelerator_options("blockinfile") or {}
    official_path = tmp_path / "official"
    accelerated_path = tmp_path / "accelerated"
    for path in (official_path, accelerated_path):
        path.write_bytes(b"caf\xe9\nend\n")
    args = {"block": "x", "insertafter": "caf"}
    accelerated = run_accelerated(
        "blockinfile", dict(args, path=str(accelerated_path)), options=options)
    if options.get("text_mode"):
        # ansible-core >= 2.20 decodes utf-8 and the module dies; ansible-playbook
        # registers this failure (the runtime's crash wrapping differs).
        assert accelerated == {
            "failed": True, "changed": False,
            "msg": "Task failed: Module failed: 'utf-8' codec can't decode "
                   "byte 0xe9 in position 3: invalid continuation byte",
        }
        assert accelerated_path.read_bytes() == b"caf\xe9\nend\n"
        return
    rt = Runtime({"blockinfile": ModuleEmbedder().embed("blockinfile")},
                 "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("blockinfile", dict(args, path=str(official_path)))
    finally:
        rt.cleanup()
    official.pop("invocation", None)
    for result, path in ((official, official_path), (accelerated, accelerated_path)):
        result["diff"] = json.loads(json.dumps(result["diff"]).replace(str(path), "<p>"))
    assert accelerated == official
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


def test_accelerated_find_splits_comma_separated_lists_like_official(tmp_path):
    from ansiroll.loader import _accelerator_options

    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
        for name in ("x.txt", "y.log", "z.cfg"):
            (tmp_path / sub / name).write_text(name)
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    cases = [
        {"paths": "%s,%s" % (a, b), "patterns": "*.txt,*.log"},
        {"paths": a, "patterns": "*.txt,*.log", "excludes": "x*,nomatch"},
        {"path": a, "pattern": "*.cfg"},
    ]
    rt = Runtime({"find": ModuleEmbedder().embed("find")},
                 "", {"inventory_hostname": "localhost"})

    def normalized(result):
        result = {k: v for k, v in result.items() if k != "invocation"}
        result["files"] = sorted(result["files"], key=lambda item: item["path"])
        return result

    try:
        for case in cases:
            official = rt.run_module("find", dict(case))
            accelerated = run_accelerated(
                "find", dict(case), options=_accelerator_options("find"))
            assert normalized(accelerated) == normalized(official), case
            assert official["matched"] > 0
    finally:
        rt.cleanup()


@pytest.mark.parametrize("login_defs", [
    "UMASK 027\nHOME_MODE 0750\n",
    "HOME_MODE 0700\nUMASK 022\n",
    "UMASK 077\nUMASK 002\n",
    "# no mode\n",
    None,
])
def test_accelerated_user_home_creation_matches_official_create_homedir(
        tmp_path, monkeypatch, login_defs):
    import stat as stat_module
    from ansible.modules import user as official_user
    from ansiroll.accelerated_modules import user as accelerated_user
    from ansiroll.loader import _accelerator_options

    skel = tmp_path / "skel"
    (skel / "sub").mkdir(parents=True)
    (skel / ".profile").write_text("profile")
    (skel / "sub" / "f").write_text("f")
    os.chmod(str(skel / ".profile"), 0o640)
    os.symlink(".profile", str(skel / "link"))
    defs = tmp_path / "login.defs"
    if login_defs is not None:
        defs.write_text(login_defs)

    class _Module(object):
        def exit_json(self, **kwargs):
            raise AssertionError(kwargs)

    official = official_user.User.__new__(official_user.User)
    official.module = _Module()
    official.skeleton = str(skel)
    official.LOGIN_DEFS = str(defs)
    official.create_homedir(str(tmp_path / "official" / "home"))
    official.chown_homedir(os.getuid(), os.getgid(), str(tmp_path / "official" / "home"))

    monkeypatch.setattr(accelerated_user, "_SKELETON", str(skel))
    monkeypatch.setattr(accelerated_user, "_LOGIN_DEFS", str(defs))
    options = _accelerator_options("user") or {}
    accelerated_user._create_homedir(
        str(tmp_path / "accelerated" / "home"), options.get("home_mode"))
    accelerated_user._chown_homedir(
        os.getuid(), os.getgid(), str(tmp_path / "accelerated" / "home"))

    def tree(base):
        out = {}
        for root, dirs, files in os.walk(base):
            for name in dirs + files + ["."]:
                full = os.path.normpath(os.path.join(root, name))
                st = os.lstat(full)
                out[os.path.relpath(full, base)] = (
                    oct(stat_module.S_IMODE(st.st_mode)),
                    os.readlink(full) if os.path.islink(full) else None)
        return out

    assert (tree(str(tmp_path / "accelerated" / "home"))
            == tree(str(tmp_path / "official" / "home")))


def test_accelerated_user_expands_home_like_official_path_type():
    import pwd as pwd_module
    from ansiroll.loader import _accelerator_options

    me = pwd_module.getpwuid(os.getuid())
    args = {"name": me.pw_name, "home": "~", "shell": me.pw_shell}
    meta = ModuleEmbedder().embed("user")
    rt = Runtime({"user": meta}, "", {"inventory_hostname": "localhost"})
    rt.check_mode = True
    try:
        official = rt.run_module("user", dict(args))
    finally:
        rt.cleanup()
    official.pop("invocation", None)
    accelerated = run_accelerated(
        "user", dict(args), check_mode=True,
        options=_accelerator_options("user"))
    if me.pw_dir != os.path.expanduser("~"):
        pytest.skip("HOME does not match the passwd home")
    assert accelerated == official
    assert official["changed"] is False


def test_accelerated_user_home_uses_task_environment(tmp_path, monkeypatch):
    import pwd as pwd_module
    from ansiroll.loader import _accelerator_options

    me = pwd_module.getpwuid(os.getuid())
    monkeypatch.setenv("ANSIROLL_USER_HOME", str(tmp_path / "stale"))
    monkeypatch.setenv("HOME", str(tmp_path))
    args = {"name": me.pw_name, "home": "$ANSIROLL_USER_HOME", "create_home": False}
    environment = {"ANSIROLL_USER_HOME": "~", "HOME": me.pw_dir}
    rt = Runtime({"user": ModuleEmbedder().embed("user")},
                 "", {"inventory_hostname": "localhost"}, check_mode=True)
    rt._module_env = environment
    try:
        official = rt.run_module("user", dict(args))
    finally:
        rt.cleanup()
    official.pop("invocation", None)
    accelerated = run_accelerated(
        "user", args, check_mode=True, environment=environment,
        options=_accelerator_options("user"))
    assert accelerated == official
    assert official["changed"] is False
    assert os.environ["HOME"] == str(tmp_path)


def test_accelerated_user_compares_int_fields_as_strings(monkeypatch):
    """comment/shell are type='str' in the official argument_spec, so an int
    that matches the passwd field is not a change."""
    import pwd as pwd_module
    from ansiroll.accelerated_modules import user as accelerated_user

    entry = pwd_module.struct_passwd(
        ("ansiroll-fake", "x", 4242, 4242, "123", "/", "7"))
    monkeypatch.setattr(accelerated_user.pwd, "getpwnam", lambda name: entry)
    monkeypatch.setattr(accelerated_user.grp, "getgrall", lambda: [])

    def no_execute(*_args, **_kwargs):
        raise AssertionError("usermod must not run")

    monkeypatch.setattr(accelerated_user, "_execute", no_execute)
    result = accelerated_user.run(
        {"name": "ansiroll-fake", "comment": 123, "shell": 7})
    assert result["changed"] is False
    assert result["comment"] == "123"


def test_accelerated_user_creates_missing_home_parents_before_useradd(
        tmp_path, monkeypatch):
    import pwd as pwd_module
    from ansiroll.accelerated_modules import user as accelerated_user

    skel = tmp_path / "skel"
    skel.mkdir()
    (skel / ".bashrc").write_text("rc")
    defs = tmp_path / "login.defs"
    defs.write_text("UMASK 077\n")
    home = tmp_path / "missing" / "parent" / "home"
    monkeypatch.setattr(accelerated_user, "_SKELETON", str(skel))
    monkeypatch.setattr(accelerated_user, "_LOGIN_DEFS", str(defs))
    created = []
    entry = pwd_module.struct_passwd(
        ("ansiroll-fake", "x", os.getuid(), os.getgid(), "", str(home), "/bin/sh"))

    def getpwnam(name):
        if not created:
            raise KeyError(name)
        return entry

    def execute(command, environment):
        # The official module creates the home (and its parents) first.
        assert (home / ".bashrc").read_text() == "rc"
        created.append(command)
        return 0, "", ""

    monkeypatch.setattr(accelerated_user.pwd, "getpwnam", getpwnam)
    monkeypatch.setattr(accelerated_user.grp, "getgrall", lambda: [])
    monkeypatch.setattr(accelerated_user, "_execute", execute)
    result = accelerated_user.run({"name": "ansiroll-fake", "home": str(home)})
    assert result["changed"] is True
    assert created and created[0][0] == "useradd" and "-m" in created[0]
    assert oct(os.stat(str(home)).st_mode & 0o777) == oct(0o700)


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


def test_compile_bakes_controller_core_version():
    from ansible.release import __version__
    src, _ = compile_playbook(os.path.join(FIXTURES, "simple.yml"))
    assert "\n_ANSIROLL_CORE_VERSION = %r\n" % str(__version__) in src


def test_controller_synchronize_leaves_retries_to_agent(monkeypatch):
    import copy
    from ansible.executor import task_executor

    class Executor:
        def __init__(self, host, task, *args):
            self.task = task

        def run(self):
            return {"retries": self.task.retries, "until": self.task.until,
                    "changed_when": self.task.changed_when, "failed_when": self.task.failed_when}

    monkeypatch.setattr(task_executor, "TaskExecutor", Executor)
    mod = _load_strategy_module()
    strategy = SimpleNamespace(_loader=None, _variable_manager=None)
    original = SimpleNamespace(retries=2, until=["ready"], changed_when=["registered.rc == 0"],
                               failed_when=["registered.rc != 0"])
    original.copy = lambda: copy.copy(original)
    frame = {"runtime_id": "sync", "action": "ansible.posix.synchronize"}
    result = mod.StrategyModule._run_native_controller_task(strategy, frame, None, {}, {"sync": original}, None)
    assert result == {"retries": 0, "until": [], "changed_when": [], "failed_when": []}
    assert original.retries == 2 and original.until == ["ready"]


def test_controller_synchronize_uses_the_installed_task_executor(tmp_path):
    plugins = tmp_path / "action_plugins"
    plugins.mkdir()
    (plugins / "synchronize.py").write_text("""from ansible.plugins.action import ActionBase

class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        delegated = task_vars.get("ansible_delegated_vars", {}).get(self._task.delegate_to, {})
        return {
            "changed": True,
            "msg": self._task.args["label"],
            "source": self._task.args.get("src"),
            "delegate_endpoint": delegated.get("ansible_host"),
            "delegate_port": delegated.get("ansible_port"),
        }
""")
    inventory = tmp_path / "inventory.ini"
    inventory.write_text(
        "delegate_fixture ansible_host=192.0.2.42 ansible_port=2222 ansible_connection=local "
        "ansible_password=synthetic-fixture-password\n")
    result = _run_strategy_play(tmp_path, """- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    expected_label: !unsafe '{{ 1 + 1 }}'
  tasks:
    - synchronize:
        label: !unsafe '{{ 1 + 1 }}'
        src: __omit_place_holder__backup
      delegate_to: "{{ item }}"
      loop: [delegate_fixture]
      register: synced
    - assert:
        that:
          - synced.results[0].msg == expected_label
          - synced.results[0].source == '__omit_place_holder__backup'
          - synced.results[0].delegate_endpoint == '192.0.2.42'
          - synced.results[0].delegate_port | int == 2222
          - synced.results[0]._ansible_delegated_vars is undefined
    - debug:
        var: synced
""", "-i", str(inventory))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "synthetic-fixture-password" not in result.stdout + result.stderr


def test_controller_synchronize_uses_check_mode_from_include_apply(tmp_path):
    plugins = tmp_path / "action_plugins"
    plugins.mkdir()
    (plugins / "synchronize.py").write_text("""from ansible.plugins.action import ActionBase

class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        return {"changed": False, "checked": self._task.check_mode}
""")
    (tmp_path / "child.yml").write_text("- synchronize: {}\n  register: synced\n")
    result = _run_strategy_play(tmp_path, """- hosts: localhost
  connection: local
  gather_facts: false
  vars: {dry_run: true}
  tasks:
    - include_tasks:
        file: child.yml
        apply: {check_mode: '{{ dry_run }}'}
    - assert: {that: synced.checked == true}
""")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("looped", [False, True], ids=["single", "loop"])
@pytest.mark.parametrize("ignore", [False, True], ids=["fatal", "ignored"])
def test_controller_synchronize_tallies_unreachable(tmp_path, looped, ignore):
    plugins = tmp_path / "action_plugins"
    plugins.mkdir()
    (plugins / "synchronize.py").write_text("""from ansible.errors import AnsibleConnectionFailure
from ansible.plugins.action import ActionBase

class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        raise AnsibleConnectionFailure("fixture delegate was unreachable")
""")
    loop = "          loop: [first, second]" if looped else ""
    result = _run_strategy_play(tmp_path, f"""- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - block:
        - synchronize:
            src: unused
          ignore_unreachable: {str(ignore).lower()}
{loop}
      rescue:
        - debug: msg=unexpected_rescue
      always:
        - debug: msg=always_task_ran

- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - debug: msg=next_play_ran
""")
    assert result.returncode == (0 if ignore else 4), result.stdout + result.stderr
    assert "UNREACHABLE!" in result.stdout
    assert "failed=0" in result.stdout
    assert "unexpected_rescue" not in result.stdout
    assert ("always_task_ran" in result.stdout) == ignore
    assert ("next_play_ran" in result.stdout) == ignore
    if not ignore:
        assert "unreachable=1" in result.stdout


@pytest.mark.parametrize(("alias_value", "alias_member", "alias_error"), [
    ("{{ missing }}", "", None),
    ("{{ missing | regex_replace('x', 'y') }}", "", None),
    ("{{ 'value' | unknown_filter }}", "", "unknown_filter"),
    ({"nested": ["{{ 'value' | unknown_filter }}"]}, ".nested[0]", "unknown_filter"),
    ({"bad": "{{ 'value' | unknown_filter }}"}, ".get('bad')", "unknown_filter"),
    ({"bad": "{{ 'value' | unknown_filter }}"}, ".values() | first", "unknown_filter"),
    ({"bad": "{{ 'value' | unknown_filter }}"}, ".values() | map('default', 'fallback') | first", "unknown_filter"),
    ({"nested": ["{{ 'value' | unknown_filter }}"]}, ".nested | map('default', 'fallback') | first", "unknown_filter"),
    ({"nested": ["{{ 'value' | unknown_filter }}"]}, ".nested[:1] | map('default', 'fallback') | first", "unknown_filter"),
    ({"nested": ["{{ 'value' | unknown_filter }}"]}, "(ALIAS.nested + []) | map('default', 'fallback') | first", "unknown_filter"),
    ({"nested": ["{{ 'value' | unknown_filter }}"]}, "([] + ALIAS.nested) | map('default', 'fallback') | first", "unknown_filter"),
    ({"nested": ["{{ 'value' | unknown_filter }}"]}, "('opts=' ~ (ALIAS.nested[:1] | string))", "unknown_filter"),
    ({"bad": "{{ 'value' | unknown_filter }}"}, ".values()", "unknown_filter"),
    ({"nested": ["{{ 'value' | unknown_filter }}"], "ok": "optional"}, ".ok", None),
    ("{{ 1 / 0 }}", "", "division by zero"),
])
def test_synchronize_round_trips_the_rendered_loop_environment(tmp_path, monkeypatch, alias_value, alias_member, alias_error):
    import copy

    from ansible.executor import task_executor
    from ansible.parsing.dataloader import DataLoader
    from ansible.playbook import Playbook
    from ansible.release import __version__ as ansible_version
    from ansiroll.compat import make_templar
    from ansiroll.loader import _tag_untrusted

    make_templar()  # Initialize Ansible's collection loader before parsing.
    legacy = tuple(map(int, ansible_version.split(".")[:2])) < (2, 19)
    items_membership = "('ok', 'valid') in item_options.items()" if legacy else "true"
    alias_read = (alias_member.replace("ALIAS", "optional_agent_alias") if "ALIAS" in alias_member
                  else "optional_agent_alias" + alias_member)
    playbook = tmp_path / "play.yml"
    playbook.write_text("""
- hosts: localhost
  vars:
    sync_play_value: "{{ 'PLAY' | lower }}"
  environment:
    PLAY_VALUE: "{{ sync_play_value }}"
  tasks:
    - environment:
        SYNC_LABEL: "{{ ['RELEASE', 'PREVIEW'][sync_index] }}"
      block:
        - debug:
            msg: placeholder
          vars:
            sync_index: "{{ item }}"
            sync_alias: {value: "{{ ['release', 'preview'][item] | upper }}"}
            sync_list: ["{{ ['release', 'preview'][item] | upper }}"]
            sync_loop_label: "{{ ['release', 'preview'][item] | upper }}"
            unsafe_label: !unsafe "{{ item }}"
          environment:
            - SYNC_LITERAL: "{{ '{' ~ '{ placeholder }' ~ '}' }}"
              SYNC_UNSAFE: "{{ unsafe_label }}"
              SYNC_PRIOR: "{{ prior.msg }}"
              SYNC_MIRROR: "{{ sync_alias.value ~ '-' ~ ansible_delegated_vars['localhost']['mirror'] }}"
              SYNC_LIST: "{{ sync_list[0] }}"
              SYNC_FILE: "{{ file_options.label }}"
              SYNC_FILE_LIST: "{{ file_options.labels[0] ~ '-' ~ file_options.labels[2] }}"
              SYNC_FILE_MISSING: "{{ file_options.labels[1] is defined }}"
              SYNC_FILE_DEFAULT: "{{ file_options.labels[1].nested | default('fallback') }}"
              SYNC_FILE_KEYS: "{{ 'optional' in file_options and (file_options | length == 3) }}"
              SYNC_FILE_OPTIONAL: "{{ file_options.optional | default('optional') }}"
              SYNC_FILE_JSON: "json={{ file_env | to_json }}"
              SYNC_LIST_JSON: "json={{ file_list | to_json }}"
              SYNC_VARS_LOOKUP: "{{ (lookup('vars', 'file_env')).SYNC_FILE_WHOLE }}"
              SYNC_TYPED_KEYS: "{{ typed_agent_alias[8080] ~ '-' ~ typed_agent_alias['8080'] ~ '-' ~ typed_agent_alias[3][3] }}"
              SYNC_TYPED_MISSING: "{{ typed_agent_alias[4] | default('optional') }}"
              SYNC_DATE_KEY: "{{ (dated_agent_alias.keys() | list | first | type_debug) ~ '-' ~ dated_agent_alias.plain }}"
              SYNC_BINARY_KEY: "{{ (dated_agent_alias.keys() | list)[1] | type_debug }}"
              SYNC_ITEM_CONTAINS: "{{ items_membership }}"
              SYNC_UNDEFINED_ALIAS: "{{ optional_agent_alias is defined }}"
              SYNC_DEFAULT_ALIAS: "{{ optional_agent_alias | default('optional') }}"
              SYNC_UNUSED: "{{ 'skipped' if true else unused_agent_alias }}"
              SYNC_MIRRORS: "{% for mirror in ansible_delegated_vars['localhost']['mirrors'] %}{{ sync_loop_label }}-{{ mirror }}{% endfor %}"
              SYNC_DEFAULT: "{{ ansible_delegated_vars['localhost']['mirror'] | default('agent-default') }}"
              SYNC_FLAG: "{{ true }}"
              SYNC_EMPTY: "{{ none }}"
            - "{{ ansible_delegated_vars['localhost']['environment'] }}"
            - "{{ file_env }}"
""".replace("optional_agent_alias |", alias_read + " |")
       .replace("items_membership", items_membership))
    loader = DataLoader()
    play, = Playbook.load(str(playbook), loader=loader).get_plays()
    original, = play.tasks[0].block
    original_environment = original.environment
    file_vars = {
        "file_options": {
            "label": "{{ 'release' | upper }}", "optional": "{{ missing }}",
            "labels": ["{{ 'release' | upper }}", "{{ missing }}", "tail"],
        },
        "file_env": {"SYNC_FILE_WHOLE": "{{ 'mixed' | upper }}"},
        "file_list": ["{{ 'mixed' | upper }}"],
        "optional_agent_alias": alias_value,
        "typed_agent_alias": "{{ {8080: 'number', '8080': 'text', 3: {3: 'nested'}, 4: missing} }}",
        "dated_agent_alias": "{{ '{2026-01-01: dated, ? !!binary YQ== : binary, plain: regular}' | from_yaml }}",
        "item_options": {"bad": "{{ 'value' | unknown_filter }}", "ok": "valid"},
        "unused_agent_alias": "{{ missing_value }}",
    }
    agent = """
import json
import sys
from ansiroll import runtime
runtime._INJECTED_VARS = json.loads(sys.argv[3])
runtime.PLAYS = [{"vars": json.loads(sys.argv[3]), "deferred_vars_files": [{
    "names": ["/vars/{{ inventory_hostname }}.yml"],
    "files": {"/vars/localhost.yml": [json.loads(sys.argv[4])]},
}], "tasks": [{
    "type": "task", "name": "Register a literal", "action": "ansible.builtin.debug",
    "special": "debug", "register": "prior", "args": {"msg": "{{ '{' ~ '{ item }' ~ '}' }}"},
}, {
    "type": "task", "runtime_id": "sync", "name": "Synchronize",
    "action": "ansible.posix.synchronize", "fallback": True,
    "loop": [0, 1], "vars": json.loads(sys.argv[2]),
    "environment": json.loads(sys.argv[1]),
}]}]
runtime.MODULES = {}
runtime.ANSIBALLZ_CODE = ""
runtime._INJECTED_HOST = "localhost"
runtime.agent_main()
"""
    names = ("file_options", "file_env", "file_list", "typed_agent_alias", "dated_agent_alias")
    if legacy:
        names += ("item_options",)
    names += ("optional_agent_alias",)
    messages = [{"t": "delegate_play_var", "name": name} for name in names]
    messages.append({"t": "delegate_result", "result": {"changed": False}})
    encoded = [json.dumps(message).encode() for message in messages]
    replies = b"".join(len(message).to_bytes(4, "big") + message for message in encoded)
    selected_vars = {**_tag_untrusted(play.vars), **file_vars}
    selected = json.dumps({"t": "vars_files_result", "result": {
        "vars": json.dumps(selected_vars), "names": list(selected_vars), "missing": [], "mapping_keys": [],
        "controller_lookups": {},
    }}).encode()
    replies = len(selected).to_bytes(4, "big") + selected + replies * 2
    serialized_environment = json.dumps(_tag_untrusted(original_environment, template_syntax_only=True))
    serialized_vars = json.dumps(_tag_untrusted(original.get_vars()))
    serialized_play_vars = json.dumps(_tag_untrusted(play.vars))
    result = subprocess.run([sys.executable, "-c", agent, serialized_environment,
                             serialized_vars, serialized_play_vars, json.dumps(file_vars)],
                            input=replies,
                            capture_output=True, timeout=15, check=True)
    stream = io.BytesIO(result.stdout)
    frames = []
    fallbacks = []
    while header := stream.read(4):
        frame = json.loads(stream.read(int.from_bytes(header, "big")))
        if frame["t"] == "delegate":
            frames.append(frame)
        elif frame["t"] == "delegate_play_var_result":
            fallbacks.append(frame["fallback"])

    class Executor:
        def __init__(self, host, task, variables, *args):
            self.task = task
            self.variables = variables

        def run(self):
            templar = make_templar()
            templar.available_variables = dict(self.variables, ansible_delegated_vars={
                "localhost": {"mirror": "controller", "mirrors": ["controller"],
                              "environment": {"SYNC_WHOLE": "delegated"}},
            })
            environment = self.task._post_validate_environment(None, self.task.environment, templar)
            return {"environment": environment}

    monkeypatch.setattr(task_executor, "TaskExecutor", Executor)
    strategy_type = _load_strategy_module().StrategyModule
    strategy = strategy_type.__new__(strategy_type)
    strategy._loader = loader
    strategy._variable_manager = None
    empty = None if tuple(map(int, ansible_version.split(".")[:2])) >= (2, 19) else ""
    requests = []

    def resolve(name):
        assert name in names
        index = requests.count(name) * len(names) + names.index(name)
        requests.append(name)
        return fallbacks[index]

    results = [strategy._run_native_controller_task(frame, None, play.vars, {"sync": original}, None, resolve)
               for frame in frames]
    assert len(results) == 2
    assert requests == list(names) * 2
    if alias_error:
        assert all(result["failed"] and alias_error in result["msg"] for result in results)
        return
    assert results == [
        {"environment": {"PLAY_VALUE": "play", "SYNC_LABEL": label, "SYNC_LITERAL": "{{ placeholder }}",
                         "SYNC_UNSAFE": "{{ item }}", "SYNC_PRIOR": "{{ item }}",
                         "SYNC_MIRROR": label + "-controller", "SYNC_LIST": label, "SYNC_FILE": "RELEASE",
                         "SYNC_FILE_LIST": "RELEASE-tail", "SYNC_FILE_MISSING": False,
                         "SYNC_FILE_DEFAULT": "fallback", "SYNC_UNUSED": "skipped", "SYNC_FILE_WHOLE": "MIXED",
                         "SYNC_FILE_KEYS": True, "SYNC_FILE_OPTIONAL": "optional",
                         "SYNC_FILE_JSON": 'json={"SYNC_FILE_WHOLE": "MIXED"}', "SYNC_LIST_JSON": 'json=["MIXED"]',
                         "SYNC_VARS_LOOKUP": "MIXED", "SYNC_UNDEFINED_ALIAS": bool(alias_member),
                         "SYNC_TYPED_KEYS": "number-text-nested", "SYNC_TYPED_MISSING": "optional",
                         "SYNC_DATE_KEY": "date-regular",
                         "SYNC_BINARY_KEY": "bytes",
                         "SYNC_ITEM_CONTAINS": True,
                         "SYNC_DEFAULT_ALIAS": "optional",
                         "SYNC_MIRRORS": label + "-controller", "SYNC_DEFAULT": "controller",
                         "SYNC_FLAG": True, "SYNC_EMPTY": empty, "SYNC_WHOLE": "delegated"}}
        for label in ("RELEASE", "PREVIEW")
    ]
    assert all("unused_agent_alias" in frame["lazy_play_vars"] for frame in frames)
    assert all("omit" not in frame["vars"] for frame in frames)
    frame = copy.deepcopy(frames[0])
    frame["vars"]["sync_loop_label"] = "{{ 'other' | upper }}"
    rendered = strategy._run_native_controller_task(frame, None, play.vars, {"sync": original}, None,
                                                    lambda name: fallbacks[names.index(name)])
    assert rendered["environment"]["SYNC_MIRRORS"] == "{{ 'other' | upper }}-controller"
    assert original.environment == original_environment


def test_gather_embeds_setup():
    src, manifest = compile_playbook(os.path.join(FIXTURES, "gather.yml"))
    assert any(m.endswith("setup") for m in manifest["modules"])


@pytest.mark.parametrize("timeout", [None, 23])
def test_implicit_setup_passes_play_gather_timeout(tmp_path, monkeypatch, timeout):
    from ansible.parsing.dataloader import DataLoader
    from ansible.playbook import Playbook
    from ansible.vars.manager import VariableManager

    playbook = tmp_path / "play.yml"
    option = "" if timeout is None else "  gather_timeout: %d\n" % timeout
    playbook.write_text("- hosts: localhost\n  gather_facts: true\n" + option + "  tasks: []\n")
    expected = {} if timeout is None else {"gather_timeout": timeout}
    source, _ = compile_playbook(str(playbook))
    loader = DataLoader()
    manager = VariableManager(loader=loader)
    play = Playbook.load(str(playbook), variable_manager=manager, loader=loader).get_plays()[0]
    strategy_type = _load_strategy_module().StrategyModule
    strategy = strategy_type.__new__(strategy_type)
    setup_args = {} if play.gather_timeout is None else {"gather_timeout": play.gather_timeout}
    setup = SimpleNamespace(when=[], no_log=False, args=setup_args)
    strategy_source, _, _ = strategy._compile(play, manager.get_vars(play=play), setup)

    monkeypatch.setenv("ANSIROLL_FAST_FACTS", "1" if timeout is not None else "0")
    for compiled in (source, strategy_source):
        runtime = Runtime({}, "", {"inventory_hostname": "localhost"})
        calls = []

        def run_module(action, args, **kwargs):
            calls.append((action, args))
            return {"ansible_facts": {}}

        monkeypatch.setattr(runtime, "run_module", run_module)
        runtime.run_play(_compiled_plays(compiled, tmp_path)[0])
        assert calls == [("ansible.builtin.setup", expected)]


@pytest.mark.parametrize("authored_timeout", [None, 10])
def test_legacy_controller_setup_keeps_fast_facts_for_implicit_options(monkeypatch, authored_timeout):
    import ansiroll.runtime as runtime_module
    from ansiroll.accelerated_modules import facts

    monkeypatch.setattr(runtime_module, "_core_at_least", lambda major, minor: (2, 12) >= (major, minor))
    monkeypatch.setenv("ANSIROLL_FAST_FACTS", "1")
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"})
    options = {"gather_subset": ["all"], "gather_timeout": 10}
    runtime.gather_setup_fn = lambda: {"options": options, "error": None}
    calls = []

    def collect_facts(**kwargs):
        calls.append("fast")
        return {"ansible_facts": {}}

    def run_setup(action, args, **kwargs):
        calls.append((action, args))
        return {"ansible_facts": {}}

    monkeypatch.setattr(facts, "run", collect_facts)
    monkeypatch.setattr(runtime, "run_module", run_setup)
    assert runtime.gather_facts(setup_options={"fact_path": "{{ omit }}"}, gather_timeout=authored_timeout)
    assert calls == (["fast"] if authored_timeout is None else [("ansible.builtin.setup", options)])


def test_standalone_gather_options_resolve_selected_vars_file_before_setup(tmp_path, monkeypatch):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: true
  vars_files: ["vars-{{ 'east' if profile == 'east' else 'west' }}.yml"]
  gather_subset: "{{ subset }}"
  fact_path: "{{ directory }}"
  tasks: []
""")
    (tmp_path / "vars-east.yml").write_text("directory: /east\nsubset: ['!all', local]\n")
    (tmp_path / "vars-west.yml").write_text("directory: /west\nsubset: ['!all', network]\n")
    (tmp_path / "vars-other.yml").write_text(
        "directory: '{{ lookup(\"env\", \"ANSIROLL_UNSELECTED_SETUP_SECRET\") }}'\n")
    monkeypatch.setenv("ANSIROLL_UNSELECTED_SETUP_SECRET", "synthetic-setup-secret")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    bundled = plan["deferred_vars_files"][0]["files"]
    assert {os.path.basename(path) for path in bundled} == {"vars-east.yml", "vars-west.yml"}
    assert "ANSIROLL_UNSELECTED_SETUP_SECRET" not in plan.get("controller_env", {})
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "west"})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"fact_path": "/west", "gather_subset": ["!all", "network"]})]


def test_finite_setup_vars_selector_does_not_bundle_parent_traversal(tmp_path):
    play_dir = tmp_path / "playbook"
    play_dir.mkdir()
    playbook = play_dir / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: true
  vars_files: ["./.{{ '.' if profile == 'parent' else 'local' }}/setup.yml"]
  fact_path: "{{ directory }}"
  tasks: []
""")
    (tmp_path / "setup.yml").write_text("directory: /outside-playbook\n")
    (play_dir / ".local").mkdir()
    local = play_dir / ".local" / "setup.yml"
    local.write_text("directory: /portable\n")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], play_dir)[0]
    assert plan["deferred_vars_files"][0]["files"] == {str(local): [{"directory": "/portable"}]}


def test_standalone_gather_fact_path_captures_only_reachable_role_lookup_inputs(tmp_path, monkeypatch):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: true
  roles: [settings]
  fact_path: "{{ directory }}"
  tasks: []
""")
    defaults = tmp_path / "roles" / "settings" / "defaults"
    defaults.mkdir(parents=True)
    (defaults / "main.yml").write_text(
        """directory: '{{ lookup("env", "ANSIROLL_SELECTED_SETUP_PATH") if true else lookup("env", "ANSIROLL_UNUSED_SETUP_PATH") }}'\n""")
    monkeypatch.setenv("ANSIROLL_SELECTED_SETUP_PATH", "/controller-facts")
    monkeypatch.setenv("ANSIROLL_UNUSED_SETUP_PATH", "synthetic-unused-secret")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["controller_env"].get("ANSIROLL_SELECTED_SETUP_PATH") == "/controller-facts"
    assert "ANSIROLL_UNUSED_SETUP_PATH" not in plan["controller_env"]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"fact_path": "/controller-facts"})]


def test_standalone_setup_defaults_use_launch_selected_vars_and_final_vars_scope(tmp_path, monkeypatch):
    from ansible.release import __version__
    from packaging.version import Version

    legacy = Version(__version__) < Version("2.12")
    action = "setup" if legacy else "ansible.builtin.setup"
    fact_filter = "ansible_distribution" if legacy else ["ansible_distribution"]
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: true
  vars_files: ['vars-{{ profile }}.yml']
  module_defaults:
    SETUP_ACTION:
      filter: "{{ vars.fact_filter }}"
  tasks: []
""".replace("SETUP_ACTION", action))
    (tmp_path / "vars-active.yml").write_text("fact_filter: " + json.dumps(fact_filter) + "\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "active"})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called[0][0] == "ansible.builtin.setup"
    assert called[0][1]["filter"] == fact_filter


def test_standalone_setup_reads_captured_controller_file_after_source_removal(tmp_path, monkeypatch):
    from ansible.release import __version__
    from packaging.version import Version

    legacy = Version(__version__) < Version("2.12")
    action = "setup" if legacy else "ansible.builtin.setup"
    playbook = tmp_path / "play.yml"
    playbook.write_text(f"""- hosts: localhost
  gather_facts: true
  module_defaults:
    {action}:
      filter: '{{{{ lookup("file", "fact-filter.txt", lstrip=true) }}}}'
  tasks: []
""")
    directory = tmp_path / "files"
    directory.mkdir()
    filter_file = directory / "fact-filter.txt"
    filter_file.write_text("  ansible_distribution  \n")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    filter_file.unlink()
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"})
    calls = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: calls.append(args) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert not runtime.failed
    assert calls[0]["filter"] == "ansible_distribution"


def test_standalone_setup_captures_only_the_active_query_from_selected_vars(tmp_path, monkeypatch):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: true
  vars_files: [selected.yml]
  fact_path: "{{ directory }}"
  tasks: []
""")
    (tmp_path / "selected.yml").write_text(
        """directory: '{{ (query("env", "ANSIROLL_SELECTED_SETUP_QUERY") | first) if true else (query("env", "ANSIROLL_UNREACHABLE_SETUP_QUERY") | first) }}'\n""")
    monkeypatch.setenv("ANSIROLL_SELECTED_SETUP_QUERY", "/query-facts")
    monkeypatch.setenv("ANSIROLL_UNREACHABLE_SETUP_QUERY", "synthetic-unreachable-secret")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    assert plan["controller_env"] == {"ANSIROLL_SELECTED_SETUP_QUERY": "/query-facts"}
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"})
    calls = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: calls.append(args) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert calls == [{"fact_path": "/query-facts"}]


@pytest.mark.parametrize("cached", [False, True])
def test_standalone_setup_uses_the_host_view_and_cache_namespace(tmp_path, monkeypatch, cached):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: true
  vars:
    directory: /play
  fact_path: "{{ hostvars[inventory_hostname].directory if ansible_facts == {} else ansible_facts.ansible_local.directory }}"
  tasks: []
""")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    runtime = Runtime({}, "", {
        "inventory_hostname": "localhost", "directory": "/inventory", "ansible_facts": {"inventory": True},
    })
    if cached:
        runtime._module_fact_layer = {"ansible_local": {"directory": "/cached"}}
    calls = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: calls.append(args) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert calls == [{"fact_path": "/cached" if cached else "/inventory"}]


def test_implicit_setup_requires_explicit_input_for_an_uncaptured_controller_value(tmp_path, monkeypatch):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: true
  vars: {profile: public}
  vars_files: ['vars-{{ profile }}.yml']
  fact_path: "{{ directory }}"
  tasks: []
""")
    (tmp_path / "vars-public.yml").write_text("directory: /public\n")
    (tmp_path / "vars-alternate.yml").write_text(
        '''directory: '{{ lookup("env", "ANSIROLL_DORMANT_SETUP_DIRECTORY") }}'\n''')
    monkeypatch.setenv("ANSIROLL_DORMANT_SETUP_DIRECTORY", "/synthetic-private-directory")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert "/synthetic-private-directory" not in json.dumps(plan)
    for extra, expected in (({}, "/public"), ({"profile": "alternate"}, None),
                            ({"profile": "alternate", "directory": "/portable"}, "/portable")):
        frames, called = [], []
        runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars=extra, on_result=frames.append)
        monkeypatch.setattr(runtime, "run_module", lambda action, args, **kwargs:
                            called.append(args) or {"ansible_facts": {}})
        runtime.run_play(plan)
        if expected is None:
            assert runtime.failed
            assert "uncaptured controller env value" in frames[0]["result"]["msg"]
            assert not called
        else:
            assert not runtime.failed
            assert called == [{"fact_path": expected}]


def test_implicit_fact_path_lookup_can_run_before_unrelated_vars_file(tmp_path, monkeypatch):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: true
  vars_files: ["vars-{{ lookup('vars', 'profile') }}.yml"]
  fact_path: "{{ lookup('env', 'ANSIROLL_INDEPENDENT_FACT_PATH') }}"
  tasks: []
""")
    (tmp_path / "vars-late.yml").write_text("unrelated: value\n")
    monkeypatch.setenv("ANSIROLL_INDEPENDENT_FACT_PATH", "/independent")
    source, _ = compile_playbook(str(playbook))
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "late"})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(_compiled_plays(source, tmp_path)[0])
    assert called == [("ansible.builtin.setup", {"fact_path": "/independent"})]
    assert runtime.vars["unrelated"] == "value"


@pytest.mark.parametrize("option", ["gather_subset", "gather_timeout", "filter"])
def test_setup_defaults_do_not_capture_an_overridden_controller_lookup(tmp_path, monkeypatch, option):
    from ansible.release import __version__
    from packaging.version import Version

    if Version(__version__) < Version("2.19"):
        pytest.skip("older cores eagerly render module defaults")
    playbook = tmp_path / "play.yml"
    override = {
        "gather_subset": "  gather_subset: [all]\n",
        "gather_timeout": "  gather_timeout: 15\n",
        "filter": "",
    }[option]
    gather_default = ("    ansible.builtin.gather_facts:\n      filter: [ansible_distribution]\n"
                      if option == "filter" else "")
    playbook.write_text("""- hosts: localhost
  gather_facts: true
""" + override + """  module_defaults:
""" + gather_default + f"""    ansible.builtin.setup:
      {option}: "{{{{ lookup('env', 'ANSIROLL_SHADOWED_SETUP_DEFAULT') }}}}"
  tasks: []
""")
    monkeypatch.setenv("ANSIROLL_SHADOWED_SETUP_DEFAULT", "synthetic-private-setup-subset")
    source, _ = compile_playbook(str(playbook))
    assert "synthetic-private-setup-subset" not in json.dumps(_compiled_plays(source, tmp_path))


def test_setup_defaults_check_vars_file_dependencies_before_gathering(tmp_path, monkeypatch):
    from ansible.release import __version__
    from packaging.version import Version

    if Version(__version__) < Version("2.19"):
        pytest.skip("uses modern setup arguments")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: true
  vars:
    fact_filter: [ansible_hostname]
  vars_files: ["vars-{{ lookup('vars', selector_name) }}.yml"]
  module_defaults:
    ansible.builtin.setup:
      filter: "{{ fact_filter }}"
  tasks: []
""")
    (tmp_path / "vars-late.yml").write_text("fact_filter: [ansible_distribution]\n")
    source, _ = compile_playbook(str(playbook))
    frames, called = [], []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"selector_name": "profile", "profile": "late"},
                      on_result=frames.append)
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append(args) or {"ansible_facts": {}})
    runtime.run_play(_compiled_plays(source, tmp_path)[0])
    assert runtime.failed
    assert "setup module defaults require a vars file" in frames[0]["result"]["msg"]
    assert not called


def test_setup_defaults_keep_legacy_omit_in_the_final_setup_arguments(tmp_path, monkeypatch):
    from ansiroll.loader import _core_version
    from ansiroll.runtime import _OMIT

    version = _core_version()
    action = "setup" if version < (2, 12) else "ansible.builtin.setup"
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: true
  module_defaults:
    SETUP_ACTION:
      fact_path: "{{ omit }}"
  tasks: []
""".replace("SETUP_ACTION", action))
    source, _ = compile_playbook(str(playbook))
    calls = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"})
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: calls.append(args) or {"ansible_facts": {}})
    runtime.run_play(_compiled_plays(source, tmp_path)[0])
    assert not runtime.failed
    assert len(calls) == 1
    if version < (2, 19):
        assert calls[0]["fact_path"] == _OMIT
    else:
        assert "fact_path" not in calls[0]


def test_setup_defaults_ignore_blocked_overridden_mapping_member(tmp_path, monkeypatch):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("modern Core defers overridden module default members")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: true
  fact_path: /explicit
  vars:
    directory: /old
    my_defaults:
      fact_path: "{{ directory }}"
      filter: [ansible_distribution]
  vars_files: ["vars-{{ lookup('vars', selector_name) }}.yml"]
  module_defaults:
    ansible.builtin.setup: "{{ my_defaults }}"
  tasks: []
""")
    (tmp_path / "vars-late.yml").write_text("directory: /late\n")
    source, _ = compile_playbook(str(playbook))
    calls = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"selector_name": "profile", "profile": "late"})
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: calls.append(args) or {"ansible_facts": {}})
    runtime.run_play(_compiled_plays(source, tmp_path)[0])
    assert not runtime.failed
    assert calls == [{"fact_path": "/explicit", "filter": ["ansible_distribution"]}]


def test_implicit_fact_path_sees_empty_local_facts_when_injection_is_disabled(tmp_path, monkeypatch):
    from ansible import constants as C

    monkeypatch.setenv("ANSIBLE_INJECT_FACT_VARS", "False")
    monkeypatch.setattr(C, "INJECT_FACTS_AS_VARS", False)
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: true
  fact_path: "{{ '/empty' if ansible_local == {} else '/inventory' }}"
  tasks: []
""")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost", "ansible_local": {"stale": True}})
    calls = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: calls.append(args) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert calls == [{"fact_path": "/empty"}]


def test_implicit_fact_path_merges_cached_facts_with_set_fact(tmp_path, monkeypatch):
    from ansible import constants as C

    monkeypatch.setenv("ANSIBLE_HASH_BEHAVIOUR", "merge")
    monkeypatch.setattr(C, "DEFAULT_HASH_BEHAVIOUR", "merge")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: true
  tasks:
    - set_fact:
        ansible_local: {settings: {other: true}}
- hosts: localhost
  gather_facts: true
  fact_path: "{{ ansible_local.settings.directory }}"
  tasks: []
""")
    source, _ = compile_playbook(str(playbook))
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"})
    called = []

    def run_module(action, args, **kwargs):
        called.append((action, args))
        return {"ansible_facts": {"ansible_local": {"settings": {"directory": "/cached"}}}}

    monkeypatch.setattr(runtime, "run_module", run_module)
    for play in _compiled_plays(source, tmp_path):
        runtime.run_play(play)
    assert called == [("ansible.builtin.setup", {}), ("ansible.builtin.setup", {"fact_path": "/cached"})]


@pytest.mark.parametrize("explicit_host_value", [False, True])
def test_implicit_fact_path_reads_persistent_vars_and_respects_fact_injection(tmp_path, monkeypatch,
                                                                        explicit_host_value):
    from ansible import constants as C

    monkeypatch.setenv("ANSIBLE_INJECT_FACT_VARS", "False")
    monkeypatch.setattr(C, "INJECT_FACTS_AS_VARS", False)
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: true
  vars: {directory: /stale}
  tasks:
    - set_fact: {suffix: persistent}
- hosts: localhost
  gather_facts: true
  fact_path: "{{ directory | default('/current') }}/{{ ansible_distribution | default('fallback') }}/{{ ansible_facts.distribution }}/{{ suffix }}"
  tasks: []
""")
    source, _ = compile_playbook(str(playbook))
    host_vars = {"inventory_hostname": "localhost"}
    if explicit_host_value:
        host_vars["ansible_distribution"] = "inventory"
    runtime = Runtime({}, "", host_vars)
    called = []

    def run_module(action, args, **kwargs):
        called.append(args)
        return {"ansible_facts": {"ansible_distribution": "gathered"}}

    monkeypatch.setattr(runtime, "run_module", run_module)
    for play in _compiled_plays(source, tmp_path):
        runtime.run_play(play)
    segment = "inventory" if explicit_host_value else "fallback"
    assert called == [{}, {"fact_path": f"/current/{segment}/gathered/persistent"}]


def test_compile_resolves_gather_timeout_in_controller_play_scope(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  roles: [settings]
  gather_timeout: '{{ lookup("file", timeout_file) }}'
  tasks: []
""")
    defaults = tmp_path / "roles" / "settings" / "defaults"
    defaults.mkdir(parents=True)
    (defaults / "main.yml").write_text("timeout_file: fact-timeout\n")
    (tmp_path / "fact-timeout").write_text("29\n")

    source, _ = compile_playbook(str(playbook))
    assert _compiled_plays(source, tmp_path)[0]["gather_timeout"] == 29


def test_compile_does_not_expand_an_unsafe_gather_timeout(tmp_path):
    from ansible.errors import AnsibleError

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_timeout: !unsafe '{{ 23 }}'
  tasks: []
""")
    with pytest.raises(AnsibleError, match="gather_timeout"):
        compile_playbook(str(playbook))


def test_compile_does_not_bundle_controller_timeout_input(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIROLL_TEST_TIMEOUT_INPUT", "only-the-length-is-needed")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_timeout: '{{ lookup("env", "ANSIROLL_TEST_TIMEOUT_INPUT") | length }}'
  tasks: []
""")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == len("only-the-length-is-needed")
    assert "ANSIROLL_TEST_TIMEOUT_INPUT" not in json.dumps(plan)
    assert "only-the-length-is-needed" not in json.dumps(plan)


@pytest.mark.parametrize(("expression", "play_vars", "override"), [
    ("override_timeout", "", 45),
    ("override_timeout | default(23)", "", 45),
    ('override_timeout.split(":") | first', "", "45:override"),
    ('override_timeout.split(":") | first', "  vars: {override_timeout: '23:base'}\n", "45:override"),
    ('(override_timeout | default("23:base")).split(":") | first', "", "45:override"),
    ('override_timeout.strip().split(":") | first', "", " 45:override "),
])
def test_standalone_gather_timeout_uses_run_extra_vars(tmp_path, monkeypatch, expression, play_vars, override):
    playbook = tmp_path / "play.yml"
    playbook.write_text("- hosts: localhost\n%s  gather_timeout: '{{ %s }}'\n  tasks: []\n"
                        % (play_vars, expression))
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"override_timeout": override})
    called = []

    def run_module(action, args, **kwargs):
        called.append((action, args))
        return {"ansible_facts": {}}

    monkeypatch.setattr(runtime, "run_module", run_module)
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 45})]


@pytest.mark.parametrize(("facts_first", "profile_alias", "role_timeout"), [
    (False, None, None), (True, None, None), (True, "play", None), (True, "role", None), (True, None, 17),
])
def test_standalone_timeout_resolves_early_vars_files_and_defers_fact_names(
        tmp_path, monkeypatch, facts_first, profile_alias, role_timeout):
    playbook = tmp_path / "play.yml"
    names = ['gather-{{ profile }}.yml', 'facts-{{ ansible_facts.os_family }}.yml']
    if facts_first:
        names.reverse()
    options = ''
    if profile_alias == "play":
        options += "  vars: {profile: '{{ profile_from_cli }}'}\n"
    if profile_alias == "role" or role_timeout is not None:
        options += "  roles: [settings]\n"
    if profile_alias == "role":
        defaults = tmp_path / "roles" / "settings" / "defaults"
        defaults.mkdir(parents=True)
        (defaults / "main.yml").write_text("profile: '{{ profile_from_cli }}'\n")
    if role_timeout is not None:
        role = tmp_path / "roles" / "settings" / "vars"
        role.mkdir(parents=True)
        (role / "main.yml").write_text(f"timeout_seconds: {role_timeout}\n")
    playbook.write_text("""- hosts: localhost
%s  vars_files: %s
  gather_timeout: '{{ timeout_seconds }}'
  tasks: []
""" % (options, json.dumps(names)))
    (tmp_path / "gather-runtime.yml").write_text("timeout_seconds: 45\n")
    (tmp_path / "facts-Example.yml").write_text("loaded_after_setup: yes\ntimeout_seconds: 81\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    extra_vars = {"profile_from_cli" if profile_alias else "profile": "runtime"}
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars=extra_vars)
    called = []

    def run_module(action, args, **kwargs):
        called.append((action, args))
        assert "loaded_after_setup" not in runtime.vars
        return {"ansible_facts": {"ansible_os_family": "Example"}}

    monkeypatch.setattr(runtime, "run_module", run_module)
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": role_timeout or 45})]
    assert runtime.vars["loaded_after_setup"] is True
    assert runtime.vars["timeout_seconds"] == (45 if facts_first else 81)


def test_legacy_runtime_timeout_treats_bare_null_as_omitted(monkeypatch):
    import ansiroll.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", "2.12.10")
    runtime = Runtime({}, "", {}, extra_vars={"override_timeout": None})
    context = {"__ansiroll_controller_config": {"DEFAULT_JINJA2_NATIVE": False}}
    template = {"source": "{{ override_timeout }}", "vars": {}, "names": ["override_timeout"]}
    assert runtime._gather_timeout_for_run(23, template, context) is None
    template["null_representation"] = "42"
    assert runtime._gather_timeout_for_run(23, template, context) == 42
    template["source"] = "{{ override_timeout | default(23) }}"
    with pytest.raises(ValueError, match="gather_timeout must be an integer"):
        runtime._gather_timeout_for_run(23, template, context)


@pytest.mark.parametrize(("files", "profile", "role_alias", "expected"), [
    (['gather-{{ profile.split(":") | first }}.yml'], "runtime:extra", False, 45),
    (["gather-{{ profile }}.yml"], "runtime", True, 55),
    (["gather-{{ profile }}{{ ansible_os_family | default('') }}.yml"], "runtime", False, 45),
    (["gather-{{ profile }}.yml", "settings-{{ flavor }}.yml"], "runtime", False, 30),
    (["gather-{{ profile }}.yml", "settings-{{ flavor }}.yml", "final.yml"], "runtime", False, 20),
    (["gather-{{ profile }}.yml", "final.yml"], "runtime", False, 20),
    (["final.yml", "gather-{{ profile }}.yml"], "runtime", False, 45),
    (["gather-{{ profile }}.yml", ["final.yml", "unused.yml"]], "runtime", False, 20),
    (["gather-{{ profile }}.yml", '{{ lookup("env", "ANSIROLL_TIMEOUT_STATIC_FILE") }}'], "runtime", False, 20),
])
def test_standalone_timeout_uses_ordered_vars_files_and_resolves_aliases(
        tmp_path, monkeypatch, files, profile, role_alias, expected):
    playbook = tmp_path / "play.yml"
    roles = ""
    if role_alias:
        roles = "  roles: [settings]\n"
        role = tmp_path / "roles" / "settings" / "vars"
        role.mkdir(parents=True)
        (role / "main.yml").write_text("fallback: '{{ base_timeout }}'\nbase_timeout: 55\n")
    playbook.write_text("""- hosts: localhost
%s  vars: {fallback: 45, flavor: default}
  vars_files: %s
  gather_timeout: '{{ timeout_seconds }}'
  tasks: []
""" % (roles, json.dumps(files)))
    (tmp_path / "gather-runtime.yml").write_text("timeout_seconds: '{{ fallback }}'\nflavor: custom\n")
    reselect_after_setup = "ansible_os_family" in files[0]
    if reselect_after_setup:
        (tmp_path / "gather-runtimeExample.yml").write_text("timeout_seconds: 81\n")
    if "settings-{{ flavor }}.yml" in files:
        (tmp_path / "settings-default.yml").write_text("timeout_seconds: 15\n")
        (tmp_path / "settings-custom.yml").write_text("timeout_seconds: !unsafe '30'\n")
        (tmp_path / "settings-unused.yml").write_text(
            """unused: '{{ lookup("env", "ANSIROLL_UNUSED_GATHER_INPUT") }}'\n""")
    monkeypatch.setenv("ANSIROLL_UNUSED_GATHER_INPUT", "unused-controller-canary")
    (tmp_path / "final.yml").write_text("timeout_seconds: 20\n")
    (tmp_path / "unused.yml").write_text("test_canary: unselected-timeout-alternative\n")
    monkeypatch.setenv("ANSIROLL_TIMEOUT_STATIC_FILE", "final.yml")

    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert "unselected-timeout-alternative" not in json.dumps(plan)
    assert "unused-controller-canary" not in json.dumps(plan)
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": profile})
    called = []

    def run_module(action, args, **kwargs):
        called.append((action, args))
        return {"ansible_facts": {"ansible_os_family": "Example"}}

    monkeypatch.setattr(runtime, "run_module", run_module)
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": expected})]
    if reselect_after_setup:
        assert runtime.vars["timeout_seconds"] == 81
    if files[-2:] == ["settings-{{ flavor }}.yml", "final.yml"]:
        assert runtime.vars["timeout_seconds"] == 20


@pytest.mark.parametrize(("select_at_compile", "alias_in_play"), [
    (False, False), (False, True), (True, False),
])
def test_standalone_timeout_requires_compile_time_selection_for_lookups(
        tmp_path, monkeypatch, select_at_compile, alias_in_play):
    playbook = tmp_path / "play.yml"
    lookup = '{{ lookup("env", timeout_env_name) }}'
    play_vars = {"timeout_source": lookup} if alias_in_play else {}
    playbook.write_text(
        "- hosts: localhost\n  vars: %s\n  vars_files: ['gather-{{ profile }}.yml']\n"
        "  gather_timeout: '{{ timeout_input | length }}'\n  tasks: []\n" % json.dumps(play_vars)
    )
    (tmp_path / "gather-runtime.yml").write_text(
        "timeout_input: %s\n" % json.dumps("{{ timeout_source }}" if alias_in_play else lookup))
    hook = tmp_path / "unexpected"
    import shlex
    (tmp_path / "gather-unused.yml").write_text("timeout_input: %s\n" % json.dumps(
        "{{ lookup('pipe', " + repr("touch " + shlex.quote(str(hook))) + ") }}"))
    extra_vars = {"timeout_env_name": "ANSIROLL_TEST_TIMEOUT_INPUT"}
    if select_at_compile:
        extra_vars["profile"] = "runtime"
    monkeypatch.setenv("ANSIROLL_TEST_TIMEOUT_INPUT", "controller-only-canary")
    source, _ = compile_playbook(str(playbook), extra_vars=extra_vars)
    plan = _compiled_plays(source, tmp_path)[0]
    assert "controller-only-canary" not in json.dumps(plan)
    assert not hook.exists()
    monkeypatch.setenv("ANSIROLL_TEST_TIMEOUT_INPUT", "123")
    results = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, on_result=results.append,
                      extra_vars={**extra_vars, "profile": "runtime"})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    if select_at_compile:
        assert called == [("ansible.builtin.setup", {"gather_timeout": len("controller-only-canary")})]
    else:
        assert runtime.failed
        assert called == []
        assert "make its vars files and lookup inputs resolvable at compile time" in results[0]["result"]["msg"]
    assert not hook.exists()


@pytest.mark.parametrize("compile_extra", [True, False])
def test_compile_timeout_keeps_late_selector_with_known_directory(tmp_path, compile_extra):
    playbook = tmp_path / "play.yml"
    config = tmp_path / "config"
    config.mkdir()
    extra = {"config_dir": str(config)}
    playbook.write_text("""- hosts: localhost
  vars: %s
  vars_files: ['gather-{{ profile }}.yml', '{{ config_dir }}/final.yml']
  gather_timeout: '{{ timeout_seconds }}'
  tasks: []
""" % json.dumps({} if compile_extra else extra))
    (config / "final.yml").write_text("timeout_seconds: 20\n")
    (tmp_path / "gather-runtime.yml").write_text("timeout_seconds: 45\n")
    source, _ = compile_playbook(str(playbook), extra_vars=extra if compile_extra else None)
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 20
    assert len(plan["deferred_vars_files"]) == 1


def test_compile_timeout_reuses_nondeterministic_file_selection(tmp_path, monkeypatch):
    from itertools import cycle
    from types import SimpleNamespace

    from ansible.plugins.filter import core

    choices = cycle(("first", "second"))
    monkeypatch.setattr(core, "SystemRandom", lambda: SimpleNamespace(choice=lambda values: next(choices)))
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars_files:
    - '{{ ["first", "second"] | random }}.yml'
  gather_timeout: '{{ timeout_seconds }}'
  tasks: []
""")
    (tmp_path / "first.yml").write_text("timeout_seconds: 15\n")
    (tmp_path / "second.yml").write_text("timeout_seconds: 30\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] in (15, 30)
    assert plan["gather_timeout"] == plan["vars"]["timeout_seconds"]


def test_ordered_timeout_plan_preserves_compile_selected_vault_file_and_env(tmp_path, monkeypatch):
    from ansible.parsing.vault import VaultLib, VaultSecret

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {flavor: default}
  vars_files: ['gather-{{ profile }}.yml', 'settings-{{ flavor }}.yml']
  gather_timeout: '{{ timeout_seconds }}'
  tasks: []
""")
    (tmp_path / "gather-custom.yml").write_text("flavor: custom\n")
    (tmp_path / "gather-runtime.yml").write_text("{}\n")
    password = tmp_path / "vault-password"
    password.write_text("password\n")
    vault = VaultLib([("default", VaultSecret(b"password"))])
    selected = """timeout_seconds: 15
environment_value: '{{ lookup("env", "ANSIROLL_SELECTED_GATHER_INPUT") }}'
"""
    (tmp_path / "settings-default.yml").write_bytes(vault.encrypt(selected))
    (tmp_path / "settings-custom.yml").write_bytes(vault.encrypt("timeout_seconds: 30\n"))
    monkeypatch.setenv("ANSIROLL_SELECTED_GATHER_INPUT", "controller")
    source, _ = compile_playbook(str(playbook), vault_password_file=str(password))
    plan = _compiled_plays(source, tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "runtime"})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    monkeypatch.setenv("ANSIROLL_SELECTED_GATHER_INPUT", "target")
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 15})]
    assert runtime.template("{{ environment_value }}", runtime.ctx()) == "controller"


def test_ordered_timeout_does_not_keep_unselected_compile_value(tmp_path, monkeypatch):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {flavor: default}
  vars_files: ['gather-{{ profile }}.yml', 'settings-{{ flavor }}.yml']
  gather_timeout: '{{ timeout_seconds | default(20) }}'
  tasks: []
""")
    (tmp_path / "gather-runtime.yml").write_text("flavor: custom\n")
    (tmp_path / "settings-default.yml").write_text("timeout_seconds: 15\n")
    (tmp_path / "settings-custom.yml").write_text("{}\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "runtime"})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert plan["gather_timeout"] == 15
    assert called == [("ansible.builtin.setup", {"gather_timeout": 20})]


def test_vars_file_replay_preserves_defaults_for_launch_selected_computed_key(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  roles: [settings]
  vars: {which: first}
  vars_files:
    - 'profile-{{ vars[which] }}.yml'
    - last.yml
  tasks: []
""")
    role = tmp_path / "roles" / "settings"
    (role / "defaults").mkdir(parents=True)
    (role / "defaults" / "main.yml").write_text("first: normal\nsecond: alternate\n")
    (tmp_path / "profile-normal.yml").write_text("chosen: normal\n")
    (tmp_path / "profile-alternate.yml").write_text("chosen: alternate\n")
    (tmp_path / "last.yml").write_text("finished: true\n")
    source, _ = compile_playbook(str(playbook))
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"which": "second"})
    runtime.run_play(_compiled_plays(source, tmp_path)[0])
    assert not runtime.failed
    assert runtime.vars["chosen"] == "alternate"
    assert runtime.vars["finished"]


def test_vars_file_replay_does_not_cache_templated_vars_lookup_terms():
    runtime = Runtime({}, "", {})
    dynamic = """{{ lookup('vars', '{{ which | default("first") }}') }}"""
    literal = "{{ lookup('vars', 'first') }}"
    assert runtime._pre_setup_template_inputs(literal, {"first": "a"}, selector=True) is not None
    assert runtime._pre_setup_template_inputs(dynamic, {"first": "a"}, selector=True) is None


def test_standalone_replays_later_vars_file_after_launch_selected_earlier_file(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars:
    flavor: default
  vars_files: ['gather-{{ profile }}.yml', '{{ flavor }}.yml']
  tasks:
    - assert:
        that:
          - flavor == 'custom'
          - picked == 'custom'
""")
    (tmp_path / "gather-runtime.yml").write_text("flavor: custom\n")
    (tmp_path / "default.yml").write_text("picked: default\n")
    (tmp_path / "custom.yml").write_text("picked: custom\n")
    source, _ = compile_playbook(str(playbook))
    artifact = tmp_path / "compiled.py"
    artifact.write_text(source)
    result = subprocess.run([sys.executable, str(artifact)], cwd=tmp_path, capture_output=True, text=True,
                            env={**os.environ, "ANSIROLL_EXTRA_VARS": '{"profile": "runtime"}'}, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_ordinary_vars_replay_defers_a_first_selector_with_literal_vars_lookup(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars:
    profile: default
  vars_files:
    - "profile-{{ lookup('vars', 'profile') }}.yml"
    - "settings-{{ lookup('vars', 'tier') }}.yml"
  tasks:
    - assert: {that: picked == 'launch'}
""")
    (tmp_path / "profile-default.yml").write_text("tier: default\n")
    (tmp_path / "profile-launch.yml").write_text("tier: launch\n")
    (tmp_path / "settings-default.yml").write_text("picked: default\n")
    (tmp_path / "settings-launch.yml").write_text("picked: launch\n")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "launch"})
    runtime.run_play(plan)
    assert not runtime.failed


def test_ordinary_vars_replay_keeps_the_controller_only_compile_selection(tmp_path):
    from ansiroll.runtime import VarsFilesError

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: 'dir\\prod'}
  vars_files: ['vars-{{ profile | win_basename }}.yml', last.yml]
  tasks: []
""")
    (tmp_path / "vars-prod.yml").write_text("selected: expected\n")
    (tmp_path / "last.yml").write_text("{}\n")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"})
    runtime.run_play(plan)
    assert runtime.vars["selected"] == "expected"
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "different"})
    with pytest.raises(VarsFilesError, match="controller-only selector with changed inputs"):
        runtime.run_play(plan)


def test_ordinary_vars_replay_does_not_open_controller_lookup_on_target(tmp_path, monkeypatch):
    from ansiroll.runtime import VarsFilesError

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: prod, choice: selection.txt}
  vars_files:
    - 'vars-{{ profile }}.yml'
    - "settings-{{ lookup('file', choice) }}.yml"
  tasks: []
""")
    (tmp_path / "vars-prod.yml").write_text("{}\n")
    (tmp_path / "vars-other.yml").write_text("choice: unavailable.txt\n")
    controller_file = tmp_path / "selection.txt"
    controller_file.write_text("original\n")
    (tmp_path / "settings-original.yml").write_text("selected: original\n")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    controller_file.unlink()
    monkeypatch.chdir(tmp_path)
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"})
    runtime.run_play(plan)
    assert runtime.vars["selected"] == "original"
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "other"})
    with pytest.raises(VarsFilesError, match="requires recompilation|controller-only selector"):
        runtime.run_play(plan)


def test_ordinary_vars_replay_keeps_uniform_include_usable_and_omits_shadowed_secret(tmp_path, monkeypatch):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: prod}
  vars_files: [['vars-{{ profile }}.yml', default.yml, never.yml], last.yml]
  tasks:
    - include_tasks: '{{ task_file }}'
""")
    (tmp_path / "vars-prod.yml").write_text(
        '''task_file: unused.yml\ntoken: '{{ lookup("env", "ANSIROLL_TEST_SHADOWED_PROFILE_TOKEN") }}'\n''')
    (tmp_path / "vars-bad.yml").write_text("- [malformed\n")
    (tmp_path / "default.yml").write_text("task_file: unused.yml\n")
    (tmp_path / "never.yml").write_text("unreachable: must-not-be-bundled\n")
    (tmp_path / "last.yml").write_text("task_file: child.yml\ntoken: override\n")
    (tmp_path / "child.yml").write_text("- debug: {msg: included-expected-child}\n")
    monkeypatch.setenv("ANSIROLL_TEST_SHADOWED_PROFILE_TOKEN", "synthetic-unreachable-secret")
    source, _ = compile_playbook(str(playbook), freeze_vars=["task_file"])
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["ordered_vars_files"][0] == 0
    dumped = json.dumps(plan)
    assert "synthetic-unreachable-secret" not in dumped
    assert "must-not-be-bundled" not in dumped
    script = tmp_path / "compiled.py"
    script.write_text(source)
    result = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "included-expected-child" in result.stdout


def test_ordinary_vars_replay_does_not_use_previous_play_values(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: admin}
  tasks: []
- hosts: localhost
  gather_facts: false
  vars_files: ["vars-{{ profile | default('prod') }}.yml", last.yml]
  tasks:
    - assert: {that: "picked == 'prod'"}
""")
    (tmp_path / "vars-admin.yml").write_text("picked: admin\n")
    (tmp_path / "vars-prod.yml").write_text("picked: prod\n")
    (tmp_path / "last.yml").write_text("{}\n")
    script = tmp_path / "compiled.py"
    script.write_text(compile_playbook(str(playbook))[0])
    result = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("selector", ["tier", "lookup('vars', 'tier')"])
def test_ordinary_vars_replay_applies_launch_values_before_setup(tmp_path, monkeypatch, selector):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: true
  vars: {profile: prod}
  vars_files: ['vars-{{ profile }}.yml', "settings-{{ SELECTOR }}.yml", last.yml]
  environment: {ANSIROLL_SETUP_PROFILE: '{{ selected }}'}
  tasks: []
""".replace("SELECTOR", selector))
    (tmp_path / "vars-prod.yml").write_text("tier: prod\n")
    (tmp_path / "vars-test.yml").write_text("tier: test\n")
    (tmp_path / "settings-prod.yml").write_text("selected: prod\n")
    (tmp_path / "settings-test.yml").write_text("selected: test\n")
    (tmp_path / "last.yml").write_text("{}\n")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "test"})
    called = []
    monkeypatch.setattr(runtime, "run_module", lambda *args, **kwargs:
                        called.append(runtime._module_env.copy()) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [{"ANSIROLL_SETUP_PROFILE": "test"}]


def test_ordinary_vars_replay_resolves_bundled_selector_filters(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: default}
  vars_files: ["vars-{{ profile | ansiroll_profile_name }}.yml", last.yml]
  tasks:
    - assert: {that: "selected == 'alternative'"}
""")
    plugins = tmp_path / "filter_plugins"
    plugins.mkdir()
    (plugins / "profiles.py").write_text("""class FilterModule:
    def filters(self):
        return {"ansiroll_profile_name": str.upper}
""")
    (tmp_path / "vars-DEFAULT.yml").write_text("selected: default\n")
    (tmp_path / "vars-ALT.yml").write_text("selected: alternative\n")
    (tmp_path / "last.yml").write_text("{}\n")
    artifact = tmp_path / "artifact.py"
    compile_result = subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", str(playbook), "-o", str(artifact)],
        cwd=tmp_path, env={**os.environ, "ANSIBLE_FILTER_PLUGINS": str(plugins), "PYTHONPATH": ROOT},
        capture_output=True, text=True, timeout=30,
    )
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
    result = subprocess.run(
        [sys.executable, str(artifact)], cwd=tmp_path,
        env={**os.environ, "ANSIROLL_EXTRA_VARS": json.dumps({"profile": "alt"})},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ordinary_vars_replay_retains_selected_inputs_used_by_alternative_aliases(tmp_path, monkeypatch):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: default}
  vars_files: [base.yml, 'mode-{{ profile }}.yml', "settings-{{ alias }}.yml", last.yml]
  tasks:
    - assert: {that: "selected == 'alternative' and token == 'final'"}
""")
    (tmp_path / "base.yml").write_text(
        """token: '{{ lookup("env", "ANSIROLL_SELECTED_ALIAS_TOKEN") }}'\n""")
    (tmp_path / "mode-default.yml").write_text("alias: default\n")
    (tmp_path / "mode-alt.yml").write_text(
        """alias: "{{ token }}"
unused: '{{ lookup("env", "ANSIROLL_UNSELECTED_ALIAS_SECRET") }}'
""")
    (tmp_path / "settings-default.yml").write_text("selected: default\n")
    (tmp_path / "settings-launch.yml").write_text("selected: alternative\n")
    (tmp_path / "last.yml").write_text("token: final\n")
    monkeypatch.setenv("ANSIROLL_SELECTED_ALIAS_TOKEN", "launch")
    monkeypatch.setenv("ANSIROLL_UNSELECTED_ALIAS_SECRET", "unused-controller-secret")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    assert "ANSIROLL_UNSELECTED_ALIAS_SECRET" not in plan["controller_env"]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "alt"})
    runtime.run_play(plan)
    assert not runtime.failed


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires FIFOs")
def test_ordinary_vars_bundling_marks_literal_fifo_without_reading_it(tmp_path):
    from ansiroll.loader import _bundled_vars_file

    fallback = tmp_path / "fallback.yml"
    os.mkfifo(fallback)
    result = _bundled_vars_file(None, fallback)
    assert "expected a regular file" in result["__ansiroll_vars_file_error__"]


def test_ordinary_vars_replay_treats_unsafe_scalar_as_a_hash_replacement(tmp_path, monkeypatch):
    from ansible import constants as C

    monkeypatch.setenv("ANSIBLE_HASH_BEHAVIOUR", "merge")
    monkeypatch.setattr(C, "DEFAULT_HASH_BEHAVIOUR", "merge")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: default, settings: {key: old}}
  vars_files: ['vars-{{ profile }}.yml', last.yml]
  tasks:
    - assert: {that: "settings == 'replacement'"}
""")
    (tmp_path / "vars-default.yml").write_text("{}\n")
    (tmp_path / "vars-run.yml").write_text("settings: !unsafe replacement\n")
    (tmp_path / "last.yml").write_text("{}\n")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "run"})
    runtime.run_play(plan)
    assert not runtime.failed


def test_ordinary_vars_replay_resolves_templates_with_literal_slashes(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: default}
  vars_files: ['vars-{{ profile }}.yml', "{{ flavor | default('normal') | replace('/', '-') }}.yml"]
  tasks: []
""")
    (tmp_path / "vars-default.yml").write_text("{}\n")
    (tmp_path / "vars-run.yml").write_text("flavor: test/run\n")
    (tmp_path / "normal.yml").write_text("selected: default\n")
    (tmp_path / "test-run.yml").write_text("selected: runtime\n")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "run"})
    runtime.run_play(plan)
    assert runtime.vars["selected"] == "runtime"


def test_ordinary_vars_replay_matches_native_vars_subdirectory_precedence(tmp_path):
    from ansible.release import __version__ as ansible_version

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: default}
  vars_files: ['vars/profile-{{ profile }}.yml', last.yml]
  tasks: []
""")
    nested = tmp_path / "vars" / "vars"
    nested.mkdir(parents=True)
    (nested.parent / "profile-default.yml").write_text("selected: default\n")
    (nested.parent / "profile-run.yml").write_text("selected: outer\n")
    (nested / "profile-run.yml").write_text("selected: inner\n")
    (tmp_path / "last.yml").write_text("{}\n")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "run"})
    runtime.run_play(plan)
    core = tuple(map(int, ansible_version.split(".")[:2]))
    assert runtime.vars["selected"] == ("inner" if (2, 12) <= core < (2, 21) else "outer")


def test_ordered_task_vars_files_merge_with_compile_only_timeout(tmp_path, monkeypatch):
    from ansible import constants as C

    monkeypatch.setenv("ANSIBLE_HASH_BEHAVIOUR", "merge")
    monkeypatch.setattr(C, "DEFAULT_HASH_BEHAVIOUR", "merge")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {flavor: default, settings: {play: true}}
  vars_files: ['first.yml', 'gather-{{ profile }}.yml', 'settings-{{ flavor }}.yml', 'final.yml']
  gather_timeout: '{{ 23 }}'
  tasks: []
""")
    (tmp_path / "first.yml").write_text("settings: {first: true}\n")
    (tmp_path / "gather-runtime.yml").write_text("flavor: custom\n")
    (tmp_path / "settings-default.yml").write_text("settings: {unselected: true}\n")
    (tmp_path / "settings-custom.yml").write_text("settings: {custom: true, winner: middle}\n")
    (tmp_path / "final.yml").write_text("settings: {final: true, winner: final}\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "runtime"})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 23})]
    assert runtime.template("{{ settings }}", runtime.ctx()) == {
        "play": True, "first": True, "custom": True, "final": True, "winner": "final",
    }


@pytest.mark.parametrize("selector", ["ansible_playbook_python | basename", "role_directory"])
def test_compile_timeout_vars_file_selector_uses_controller_scope(tmp_path, selector):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  roles: [settings]
  vars_files: ['profile-{{ %s }}.yml']
  gather_timeout: '{{ timeout_seconds }}'
  tasks: []
""" % selector)
    defaults = tmp_path / "roles" / "settings" / "defaults"
    defaults.mkdir(parents=True)
    (defaults / "main.yml").write_text("role_directory: role-default\n")
    role_vars = defaults.parent / "vars"
    role_vars.mkdir()
    (role_vars / "main.yml").write_text("role_directory: role-export\n")
    (tmp_path / ("profile-" + os.path.basename(sys.executable) + ".yml")).write_text("timeout_seconds: 23\n")
    (tmp_path / "profile-role-default.yml").write_text("timeout_seconds: 23\n")
    source, _ = compile_playbook(str(playbook))
    assert _compiled_plays(source, tmp_path)[0]["gather_timeout"] == 23


def test_ordered_timeout_defers_vars_file_error_until_after_gathering(tmp_path, monkeypatch):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {timeout_seconds: 23}
  vars_files:
    - "settings-{{ profile }}{{ ansible_os_family | default('') }}.yml"
  gather_timeout: '{{ timeout_seconds }}'
  tasks: []
""")
    (tmp_path / "settings-runtime.yml").write_text("$ANSIBLE_VAULT;1.1;AES256\n")
    (tmp_path / "settings-runtimeExample.yml").write_text("selected: after-setup\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "runtime"})
    called = []

    def run_module(action, args, **kwargs):
        called.append((action, args))
        return {"ansible_facts": {"ansible_os_family": "Example"}}

    monkeypatch.setattr(runtime, "run_module", run_module)
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 23})]
    assert runtime.vars["selected"] == "after-setup"


def test_ordered_vars_files_can_use_compile_selected_directory(tmp_path, monkeypatch):
    from ansiroll.runtime import VarsFilesError

    playbook = tmp_path / "play.yml"
    config = tmp_path / "config"
    config.mkdir()
    (config / "final.yml").write_text("selected: compile-default\n")
    playbook.write_text("""- hosts: localhost
  vars: %s
  vars_files: ['gather-{{ profile }}.yml', '{{ config_dir }}/final.yml']
  gather_timeout: '{{ 23 }}'
  tasks: []
""" % json.dumps({"config_dir": str(config)}))
    (tmp_path / "gather-normal.yml").write_text("{}\n")
    (tmp_path / "gather-other.yml").write_text("config_dir: " + str(tmp_path / "unbundled") + "\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "normal"})
    monkeypatch.setattr(runtime, "run_module", lambda *args, **kwargs: {"ansible_facts": {}})
    runtime.run_play(plan)
    assert runtime.vars["selected"] == "compile-default"
    runtime.extra_vars["profile"] = "other"
    with pytest.raises(VarsFilesError, match="path that was not bundled"):
        runtime.run_play(plan)


def test_ordered_vars_files_keep_intermediate_hash_replacement(tmp_path, monkeypatch):
    from ansible import constants as C

    monkeypatch.setenv("ANSIBLE_HASH_BEHAVIOUR", "merge")
    monkeypatch.setattr(C, "DEFAULT_HASH_BEHAVIOUR", "merge")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {settings: {play: true}}
  vars_files: ['profile-{{ profile }}.yml', 'reset.yml', 'final.yml']
  gather_timeout: '{{ 23 }}'
  tasks: []
""")
    (tmp_path / "profile-runtime.yml").write_text("{}\n")
    (tmp_path / "reset.yml").write_text("settings: reset\n")
    (tmp_path / "final.yml").write_text("settings: {final: true}\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "runtime"})
    monkeypatch.setattr(runtime, "run_module", lambda *args, **kwargs: {"ansible_facts": {}})
    runtime.run_play(plan)
    assert runtime.template("{{ settings }}", runtime.ctx()) == {"final": True}


@pytest.mark.parametrize("selector", [
    "magic", "mapping", "mapping_with_lookup", "mapping_alias", "mapping_default",
    "inline_default", "shrinking_alias", "branch_alias", "shared_branch_alias",
])
def test_ordered_timeout_replays_selectors_with_controller_or_mapping_inputs(tmp_path, monkeypatch, selector):
    playbook = tmp_path / "play.yml"
    if selector == "magic":
        suffix = "{{ ansible_playbook_python | basename }}-{{ flavor }}"
        initial, override = {"flavor": "default"}, {"flavor": "runtime"}
        prefix = "settings-" + os.path.basename(sys.executable) + "-"
    else:
        suffix = "{{ config.flavor }}"
        initial = {"config": {"flavor": "default"}}
        override = {"config": {"flavor": "runtime"}}
        prefix = "settings-"
        if selector in ("mapping_with_lookup", "mapping_alias", "mapping_default", "inline_default"):
            monkeypatch.delenv("ANSIROLL_TEST_UNUSED_SELECTOR", raising=False)
            initial["config"]["unused"] = "{{ lookup('env', 'ANSIROLL_TEST_UNUSED_SELECTOR') }}"
            override["config"]["unused"] = "{{ lookup('also_unavailable') }}"
        if selector in ("mapping_alias", "mapping_default"):
            filter_ = " | default({})" if selector == "mapping_default" else ""
            initial = {"config": "{{ settings" + filter_ + " }}", "settings": initial["config"]}
            override = {"config": "{{ runtime_settings" + filter_ + " }}", "runtime_settings": override["config"]}
        if selector == "inline_default":
            suffix = "{{ (settings | default({})).flavor }}"
            initial = {"settings": initial["config"]}
            override = {"settings": override["config"]}
        if selector == "shrinking_alias":
            suffix = "{{ config.nested.again.flavor }}"
            initial = {"config": "{{ settings }}", "settings": {
                "flavor": "default", "nested": {"again": "{{ settings }}"},
            }}
            override = {"settings": {
                "flavor": "runtime", "nested": {"again": "{{ settings }}"},
            }}
        if selector == "branch_alias":
            suffix = "{{ config.again.flavor }}"
            initial = {"config": "{{ settings }}", "other": "{{ settings }}", "settings": {
                "again": "{{ other.first.second }}", "first": {"second": {"flavor": "default"}},
            }}
            override = {"config": {"again": {"flavor": "runtime"}}}
        if selector == "shared_branch_alias":
            suffix = "{{ config.nested.again.flavor }}"
            initial = {"config": "{{ settings }}", "other": "{{ settings }}", "settings": {
                "nested": {"again": "{{ other.nested.first.again }}",
                           "first": {"again": {"flavor": "default"}}},
            }}
            override = {"settings": {
                "nested": {"again": "{{ other.nested.first.again }}",
                           "first": {"again": {"flavor": "runtime"}}},
            }}
    playbook.write_text("""- hosts: localhost
  vars: %s
  vars_files: ['gather-{{ profile }}.yml', 'settings-%s.yml']
  gather_timeout: '{{ timeout_seconds }}'
  tasks: []
""" % (json.dumps(initial), suffix))
    (tmp_path / "gather-runtime.yml").write_text(json.dumps(override))
    (tmp_path / (prefix + "default.yml")).write_text("timeout_seconds: 23\n")
    (tmp_path / (prefix + "runtime.yml")).write_text("timeout_seconds: 45\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    extras = {"profile": "runtime", "ansible_playbook_python": "incorrect-target-value"}
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars=extras)
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    if selector not in ("shrinking_alias", "branch_alias", "shared_branch_alias"):
        assert plan["gather_timeout"] == 23
    assert called == [("ansible.builtin.setup", {"gather_timeout": 45})]


def test_ordered_first_found_does_not_skip_an_unbundled_earlier_path(tmp_path, monkeypatch):
    from ansiroll.runtime import VarsFilesError

    playbook = tmp_path / "play.yml"
    default_dir = tmp_path / "compile-default"
    playbook.write_text("""- hosts: localhost
  vars: %s
  vars_files:
    - 'gather-{{ profile }}.yml'
    - ['{{ config_dir }}/final.yml', 'fallback.yml']
  gather_timeout: '{{ 23 }}'
  tasks: []
""" % json.dumps({"config_dir": str(default_dir)}))
    (tmp_path / "fallback.yml").write_text("selected: fallback\n")
    (tmp_path / "gather-normal.yml").write_text("{}\n")
    (tmp_path / "gather-other.yml").write_text("config_dir: " + str(tmp_path / "unbundled") + "\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "normal"})
    monkeypatch.setattr(runtime, "run_module", lambda *args, **kwargs: {"ansible_facts": {}})
    runtime.run_play(plan)
    assert runtime.vars["selected"] == "fallback"
    runtime.extra_vars["profile"] = "other"
    with pytest.raises(VarsFilesError, match="path that was not bundled"):
        runtime.run_play(plan)


@pytest.mark.parametrize("config_dir", ["region/production", ""])
def test_ordered_first_found_rejects_path_outside_glob_depth(tmp_path, monkeypatch, config_dir):
    from ansiroll.runtime import VarsFilesError

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {config_dir: default}
  vars_files:
    - 'gather-{{ profile }}.yml'
    - ['profiles/{{ config_dir }}/final.yml', 'fallback.yml']
  gather_timeout: '{{ 23 }}'
  tasks: []
""")
    nested = tmp_path / "profiles" / config_dir
    nested.mkdir(parents=True)
    (nested / "final.yml").write_text("selected: nested\n")
    (tmp_path / "fallback.yml").write_text("selected: fallback\n")
    (tmp_path / "gather-normal.yml").write_text("{}\n")
    (tmp_path / "gather-other.yml").write_text(json.dumps({"config_dir": config_dir}))
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "normal"})
    monkeypatch.setattr(runtime, "run_module", lambda *args, **kwargs: {"ansible_facts": {}})
    runtime.run_play(plan)
    assert runtime.vars["selected"] == "fallback"
    runtime.extra_vars["profile"] = "other"
    with pytest.raises(VarsFilesError, match="path that was not bundled"):
        runtime.run_play(plan)


@pytest.mark.parametrize("compile_dir", ["default", "missing/.."])
def test_ordered_first_found_checks_unbundled_higher_priority_path(tmp_path, monkeypatch, compile_dir):
    from ansiroll.loader import _core_version
    from ansiroll.runtime import VarsFilesError

    if _core_version() < (2, 12):
        pytest.skip("legacy vars files do not search a vars subdirectory")
    playbook = tmp_path / "play.yml"
    fallback = tmp_path / "profiles" / "final.yml"
    fallback.parent.mkdir()
    fallback.write_text("selected: bundled-lower-priority\n")
    earlier = tmp_path / "vars" / "profiles" / "final.yml"
    earlier.parent.mkdir(parents=True)
    earlier.write_text("selected: unbundled-higher-priority\n")
    playbook.write_text("""- hosts: localhost
  vars: %s
  vars_files:
    - 'gather-{{ profile }}.yml'
    - ['profiles/{{ config_dir }}/final.yml', %s]
  gather_timeout: '{{ 23 }}'
  tasks: []
""" % (json.dumps({"config_dir": compile_dir}), json.dumps(str(fallback))))
    (tmp_path / "gather-normal.yml").write_text("{}\n")
    (tmp_path / "gather-other.yml").write_text("config_dir: ''\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "normal"})
    monkeypatch.setattr(runtime, "run_module", lambda *args, **kwargs: {"ansible_facts": {}})
    runtime.run_play(plan)
    assert runtime.vars["selected"] == "bundled-lower-priority"
    runtime.extra_vars["profile"] = "other"
    with pytest.raises(VarsFilesError, match="path that was not bundled"):
        runtime.run_play(plan)


@pytest.mark.parametrize("compile_default", [False, True])
def test_ordered_timeout_reads_scalar_member_from_vars_file_mapping(tmp_path, monkeypatch, compile_default):
    playbook = tmp_path / "play.yml"
    initial = {"config": {"timeout": 23}} if compile_default else {}
    playbook.write_text("""- hosts: localhost
  vars: %s
  vars_files: ['gather-{{ profile }}.yml']
  gather_timeout: '{{ config.timeout }}'
  tasks: []
""" % json.dumps(initial))
    (tmp_path / "gather-runtime.yml").write_text("""config:
  timeout: 45
  unused: "{{ lookup('unavailable') }}"
""")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "runtime"})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 45})]


@pytest.mark.parametrize("expression", [
    "vars.timeout_seconds", "vars['timeout_seconds']", "vars[timeout_var]", "vars[timeout_var ~ '_seconds']",
])
def test_gather_timeout_reads_override_through_vars_mapping(tmp_path, monkeypatch, expression):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {timeout_seconds: 23, timeout_var: %s}
  vars_files: ['gather-{{ profile }}.yml']
  gather_timeout: "{{ %s }}"
  tasks: []
""" % ("timeout" if "~" in expression else "timeout_seconds", expression))
    (tmp_path / "gather-runtime.yml").write_text("timeout_seconds: 45\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "runtime"})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 45})]


@pytest.mark.parametrize("reset", [False, True])
def test_gather_timeout_merges_role_defaults_and_vars_file(tmp_path, monkeypatch, reset):
    from ansible import constants as C

    monkeypatch.setenv("ANSIBLE_HASH_BEHAVIOUR", "merge")
    monkeypatch.setattr(C, "DEFAULT_HASH_BEHAVIOUR", "merge")
    defaults = tmp_path / "roles" / "settings" / "defaults"
    defaults.mkdir(parents=True)
    (defaults / "main.yml").write_text("config: {timeout: 23}\n")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  roles: [settings]
  vars_files: %s
  gather_timeout: '{{ config.timeout | default(20) }}'
  tasks: []
""" % json.dumps(["reset.yml", "labels.yml", "gather-{{ profile }}.yml"] if reset
                 else ["gather-{{ profile }}.yml"]))
    (tmp_path / "reset.yml").write_text("config: reset\n")
    (tmp_path / "labels.yml").write_text("config: {label: later}\n")
    (tmp_path / "gather-runtime.yml").write_text("config: {unrelated: 1}\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "runtime"})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    expected = 20 if reset else 23
    assert plan["gather_timeout"] == expected
    assert called == [("ansible.builtin.setup", {"gather_timeout": expected})]


@pytest.mark.parametrize("provides_timeout", [True, False])
def test_gather_timeout_blocks_unresolved_selector_only_for_used_inputs(tmp_path, monkeypatch, provides_timeout):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {flavor: default}
  vars_files: ['gather-{{ profile }}.yml', 'settings-{{ flavor }}.yml']
  gather_timeout: '{{ timeout_seconds | default(20) }}'
  tasks: []
""")
    (tmp_path / "gather-runtime.yml").write_text("flavor: \"{{ lookup('choice') }}\"\n")
    key = "timeout_seconds" if provides_timeout else "unrelated"
    (tmp_path / "settings-runtime.yml").write_text(f"{key}: 45\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "runtime"},
                      on_result=frames.append)
    runtime.templater._lookup_loaded["choice"] = lambda terms, variables: ["runtime"]
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    if provides_timeout:
        assert runtime.failed
        assert "cannot be rendered before setup" in frames[0]["result"]["msg"]
        assert not called
    else:
        assert not runtime.failed
        assert called == [("ansible.builtin.setup", {"gather_timeout": 20})]
        assert runtime.vars["unrelated"] == 45


@pytest.mark.parametrize(("chained", "replacement"), [(True, False), (False, True), (True, True)])
@pytest.mark.parametrize("merge", [False, True])
def test_gather_timeout_tracks_blocked_inputs_in_file_order(tmp_path, monkeypatch, chained, replacement, merge):
    from ansible import constants as C

    behaviour = "merge" if merge else "replace"
    monkeypatch.setenv("ANSIBLE_HASH_BEHAVIOUR", behaviour)
    monkeypatch.setattr(C, "DEFAULT_HASH_BEHAVIOUR", behaviour)
    names = ["gather-{{ profile }}.yml", "settings-{{ flavor }}.yml"]
    if chained:
        names.append("timeout-{{ flavor2 | default('fallback') }}.yml")
    if replacement:
        names.append("final.yml")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {flavor: default}
  vars_files: %s
  gather_timeout: '{{ timeout_seconds | default(20) }}'
  tasks: []
""" % json.dumps(names))
    (tmp_path / "gather-runtime.yml").write_text('flavor: "{{ lookup(\'choice\') }}"\n')
    (tmp_path / "settings-runtime.yml").write_text("flavor2: runtime\n" if chained else "timeout_seconds: 45\n")
    (tmp_path / "settings-default.yml").write_text("{}\n")
    (tmp_path / "timeout-fallback.yml").write_text("timeout_seconds: 20\n")
    (tmp_path / "timeout-runtime.yml").write_text("timeout_seconds: 45\n")
    (tmp_path / "final.yml").write_text("timeout_seconds: 30\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "runtime"},
                      on_result=frames.append)
    runtime.templater._lookup_loaded["choice"] = lambda terms, variables: ["runtime"]
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    if replacement:
        assert not runtime.failed
        assert called == [("ansible.builtin.setup", {"gather_timeout": 30})]
    else:
        assert runtime.failed
        assert "cannot be rendered before setup" in frames[0]["result"]["msg"]
        assert not called


@pytest.mark.parametrize("changed", [False, True])
def test_controller_timeout_lookup_rejects_changed_input_without_running_lookup(tmp_path, monkeypatch, changed):
    monkeypatch.setenv("ANSIROLL_LOOKUP_TIMEOUT_DEFAULT", "abc")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {timeout_env: ANSIROLL_LOOKUP_TIMEOUT_DEFAULT}
  vars_files: ['gather-{{ profile }}.yml']
  gather_timeout: '{{ lookup("env", timeout_env) | length }}'
  tasks: []
""")
    (tmp_path / "gather-runtime.yml").write_text(
        "timeout_env: ANSIROLL_LOOKUP_TIMEOUT_OTHER\n" if changed else "timeout_env: ANSIROLL_LOOKUP_TIMEOUT_DEFAULT\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "runtime"},
                      on_result=frames.append)
    runtime.templater.env.globals["lookup"] = lambda *_: pytest.fail("unexpected lookup during setup")
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    if changed:
        assert runtime.failed
        assert "cannot be rendered before setup" in frames[0]["result"]["msg"]
        assert not called
    else:
        assert not runtime.failed
        assert called == [("ansible.builtin.setup", {"gather_timeout": 3})]


@pytest.mark.parametrize("compile_key", [True, False])
@pytest.mark.parametrize("projection", ["timeout_var", "timeout_var.strip()"])
def test_computed_timeout_can_select_another_play_var(tmp_path, monkeypatch, compile_key, projection):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {%sfirst: 23, second: 45}
  gather_timeout: "{{ vars[%s] }}"
  tasks: []
""" % ("timeout_var: first, " if compile_key else "", projection))
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    key = " second " if projection.endswith("strip()") else "second"
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"timeout_var": key})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 45})]


@pytest.mark.parametrize("timeout_field", [True, False])
def test_timeout_hash_merge_tracks_definite_nested_members(tmp_path, monkeypatch, timeout_field):
    from ansible import constants as C

    monkeypatch.setenv("ANSIBLE_HASH_BEHAVIOUR", "merge")
    monkeypatch.setattr(C, "DEFAULT_HASH_BEHAVIOUR", "merge")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {flavor: default, config: {options: {timeout: 20}}}
  vars_files: ['gather-{{ profile }}.yml', 'settings-{{ flavor }}.yml', final.yml]
  gather_timeout: '{{ config.options.timeout }}'
  tasks: []
""")
    (tmp_path / "gather-runtime.yml").write_text("flavor: \"{{ lookup('choice') }}\"\n")
    (tmp_path / "settings-default.yml").write_text("{}\n")
    (tmp_path / "settings-runtime.yml").write_text("config: {options: {timeout: 45}}\n")
    (tmp_path / "final.yml").write_text(
        "config: {options: {timeout: 30}}\n" if timeout_field else "config: {options: {sibling: 30}}\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "runtime"},
                      on_result=frames.append)
    runtime.templater._lookup_loaded["choice"] = lambda terms, variables: ["runtime"]
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    if timeout_field:
        assert called == [("ansible.builtin.setup", {"gather_timeout": 30})]
    else:
        assert runtime.failed
        assert "cannot be rendered before setup" in frames[0]["result"]["msg"]
        assert not called


@pytest.mark.parametrize(("expression", "play_vars", "override", "expected"), [
    ("lookup('env', 'ANSIROLL_TIMEOUT_' ~ key) | length", "{key: 1}", {"key": True}, 3),
    ("lookup('env', 'ANSIROLL_TIMEOUT_' ~ key[0]) | length", "{key: [1]}", {"key": [True]}, 3),
    ("lookup('env', 'ANSIROLL_TIMEOUT_' ~ key[0]) | length", "{key: abc}", {"key": "xbc"}, 3),
    ("lookup('vars', *timeout_names)", "{timeout_names: [timeout_seconds], timeout_seconds: 23}",
     {"timeout_seconds": 45}, 23),
    ("lookup('var' | replace('var', 'vars'), 'timeout_seconds')", "{timeout_seconds: 23}",
     {"timeout_seconds": 45}, 23),
    ("lookup('vars', 'timeout_seconds')", "{timeout_seconds: 23}", {"timeout_seconds": 45}, 23),
    ("lookup('ansible.legacy.vars', 'timeout_seconds')", "{timeout_seconds: 23}", {"timeout_seconds": 45}, 23),
    ("lookup('env', 'ANSIROLL_TIMEOUT_' ~ (settings.keys() | list | first)) | length",
     "{settings: {a: 1}}", {"settings": {"b": 1}}, 3),
    ("lookup('vars', 'TIMEOUT_SECONDS' | lower)", "{timeout_seconds: 23}", {"timeout_seconds": 45}, 23),
    ("lookup('vars', 'timeout_value' | replace('value', 'seconds'))", "{timeout_seconds: 23}",
     {"timeout_seconds": 45}, 23),
    ("lookup('vars', key)", "{key: timeout_seconds, timeout_seconds: 23}", {"timeout_seconds": 45}, 23),
    ("lookup('ansible.builtin.vars', key)", "{key: timeout_seconds, timeout_seconds: 23}",
     {"timeout_seconds": 45}, 23),
])
def test_controller_timeout_detects_type_and_vars_lookup_dependencies(
        tmp_path, monkeypatch, expression, play_vars, override, expected):
    from ansiroll.loader import _core_version

    if _core_version(include_patch=True) < (2, 11, 12) and expression.startswith("lookup('ansible.builtin.vars'"):
        pytest.skip("Ansible 2.10 and early 2.11 do not load the fully qualified vars lookup's options")
    monkeypatch.setenv("ANSIROLL_TIMEOUT_1", "abc")
    monkeypatch.setenv("ANSIROLL_TIMEOUT_a", "abc")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: %s
  gather_timeout: "{{ %s }}"
  tasks: []
""" % (play_vars, expression))
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == expected
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars=override, on_result=frames.append)
    monkeypatch.setattr(runtime, "run_module",
                        lambda *args, **kwargs: pytest.fail("setup must not run with a stale timeout"))
    runtime.run_play(plan)
    assert runtime.failed
    assert "cannot be rendered before setup" in frames[0]["result"]["msg"]


def test_timeout_hash_merge_applies_role_exports_in_order(tmp_path, monkeypatch):
    from ansible import constants as C

    monkeypatch.setenv("ANSIBLE_HASH_BEHAVIOUR", "merge")
    monkeypatch.setattr(C, "DEFAULT_HASH_BEHAVIOUR", "merge")
    for role, data in [("reset", "config: reset"), ("label", "config: {label: later}")]:
        directory = tmp_path / "roles" / role / "vars"
        directory.mkdir(parents=True)
        (directory / "main.yml").write_text(data + "\n")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {config: {timeout: 23}}
  vars_files: ['gather-{{ profile }}.yml']
  roles: [reset, label]
  gather_timeout: '{{ config.timeout | default(20) }}'
  tasks: []
""")
    (tmp_path / "gather-runtime.yml").write_text("config: {timeout: 45}\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 20
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "runtime"})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 20})]


@pytest.mark.parametrize("second_file", ["timeout_seconds: 45\n", "- timeout_seconds: 60\n- timeout_seconds: 45\n"])
def test_timeout_replays_portable_vars_file_selected_during_compilation(tmp_path, monkeypatch, second_file):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {profile: first}
  vars_files: ['gather-{{ profile }}.yml']
  gather_timeout: '{{ timeout_seconds }}'
  tasks: []
""")
    (tmp_path / "gather-first.yml").write_text("timeout_seconds: 23\n")
    (tmp_path / "gather-second.yml").write_text(second_file)
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 23
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "second"})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 45})]
    assert runtime.vars["timeout_seconds"] == 45


def test_controller_timeout_ignores_alias_from_unselected_vars_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIROLL_TIMEOUT_SELECTED", "abc")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {unrelated: unused, profile: first}
  vars_files: ['choice-{{ stage }}.yml', 'gather-{{ profile }}.yml']
  gather_timeout: '{{ lookup("env", timeout_env) | length }}'
  tasks: []
""")
    (tmp_path / "gather-first.yml").write_text("timeout_env: ANSIROLL_TIMEOUT_SELECTED\n")
    (tmp_path / "gather-other.yml").write_text('timeout_env: "{{ unrelated }}"\n')
    (tmp_path / "gather-literal.yml").write_text("timeout_env: ANSIROLL_TIMEOUT_SELECTED\n")
    (tmp_path / "choice-runtime.yml").write_text("profile: literal\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"stage": "runtime"})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 3})]


@pytest.mark.parametrize("rename", [False, True])
def test_controller_timeout_compares_only_read_mapping_members(tmp_path, monkeypatch, rename):
    monkeypatch.setenv("ANSIROLL_TIMEOUT_SELECTED", "abc")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {cfg: {name: ANSIROLL_TIMEOUT_SELECTED, label: initial}}
  gather_timeout: '{{ lookup("env", cfg.name) | length }}'
  tasks: []
""")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, on_result=frames.append, extra_vars={
        "cfg": {"name": "ANSIROLL_TIMEOUT_DIFFERENT" if rename else "ANSIROLL_TIMEOUT_SELECTED", "label": "updated"},
    })
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    if rename:
        assert runtime.failed
        assert "cannot be rendered before setup" in frames[0]["result"]["msg"]
        assert not called
    else:
        assert called == [("ansible.builtin.setup", {"gather_timeout": 3})]


@pytest.mark.parametrize("changed_input", [False, True])
@pytest.mark.parametrize("role_magic", [False, True])
def test_timeout_replay_reuses_compile_time_controller_candidate_miss(
        tmp_path, monkeypatch, changed_input, role_magic):
    monkeypatch.setenv("ANSIROLL_GATHER_PROFILE", "absent")
    monkeypatch.setenv("ANSIROLL_GATHER_OTHER", "present")
    playbook = tmp_path / "play.yml"
    magic = "{{ ansible_playbook_python | basename }}-" if role_magic else ""
    options = "  vars: {profile: first, controller_env_name: ANSIROLL_GATHER_PROFILE}"
    if role_magic:
        defaults = tmp_path / "roles" / "settings" / "defaults"
        defaults.mkdir(parents=True)
        (defaults / "main.yml").write_text("controller_env_name: ANSIROLL_GATHER_PROFILE\n")
        options = "  vars: {profile: first}\n  roles: [settings]"
    playbook.write_text("""- hosts: localhost
%s
  vars_files:
    - ["primary-%s{{ lookup('env', controller_env_name) }}.yml", "fallback-{{ profile }}.yml"]
  gather_timeout: '{{ timeout_seconds }}'
  tasks: []
""" % (options, magic))
    present = "primary-" + ((os.path.basename(sys.executable) + "-") if role_magic else "") + "present.yml"
    (tmp_path / present).write_text("timeout_seconds: 60\n")
    (tmp_path / "fallback-first.yml").write_text("timeout_seconds: 23\n")
    (tmp_path / "fallback-second.yml").write_text("timeout_seconds: 45\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 23
    overrides = {"profile": "second"}
    if changed_input:
        overrides["controller_env_name"] = "ANSIROLL_GATHER_OTHER"
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars=overrides, on_result=frames.append)
    runtime.templater.env.globals["lookup"] = lambda *_: pytest.fail("controller lookup must not run during replay")
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    if changed_input:
        assert runtime.failed
        assert "cannot be rendered before setup" in frames[0]["result"]["msg"]
        assert not called
    else:
        assert called == [("ansible.builtin.setup", {"gather_timeout": 45})]
        assert runtime.vars["timeout_seconds"] == 45


def test_computed_timeout_rechecks_value_selected_by_an_alias(tmp_path, monkeypatch):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars:
    timeout_var: '{{ actual_key }}'
    actual_key: ' first '
    first: 23
  gather_timeout: '{{ vars[timeout_var.strip()] }}'
  tasks: []
""")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 23
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"first": 45})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 45})]


@pytest.mark.parametrize("target", ["timeout_var.strip()", "timeout_var | reverse | join"])
def test_controller_timeout_detects_changed_computed_vars_target(tmp_path, monkeypatch, target):
    monkeypatch.setenv("ANSIROLL_TIMEOUT_SUFFIX", "x")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars:
    timeout_var: '{{ actual_key }}'
    actual_key: %s
    first: 23
  gather_timeout: "{{ vars[%s] + (lookup('env', 'ANSIROLL_TIMEOUT_SUFFIX') | length) }}"
  tasks: []
""" % ("' first '" if target.endswith("strip()") else "tsrif", target))
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 24
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"first": 45}, on_result=frames.append)
    monkeypatch.setattr(runtime, "run_module",
                        lambda *args, **kwargs: pytest.fail("setup must not run with a stale timeout"))
    runtime.run_play(plan)
    assert runtime.failed
    assert "cannot be rendered before setup" in frames[0]["result"]["msg"]


@pytest.mark.parametrize(("selector", "selector_vars"), [
    ("'first_x' | replace('_x', '')", ""),
    ("timeout_var | default('first')", ""),
    ("timeout_var | default(default_value='first')", ""),
    ("timeout_var | default('first', true)", ", timeout_var: null"),
    ("timeout_var | default(default_value='first', boolean=true)", ", timeout_var: null"),
])
def test_computed_timeout_with_constant_key_replays_target_override(tmp_path, monkeypatch, selector, selector_vars):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {first: 23%s}
  gather_timeout: "{{ vars[%s] }}"
  tasks: []
""" % (selector_vars, selector))
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 23
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"first": 45})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 45})]


@pytest.mark.parametrize("template", [
    "{{ vars[key] }}{% for key in [] %}{% endfor %}",
    "{% if flag %}{% set key = 'second' %}{% else %}{{ vars[key] }}{% endif %}",
    "{% if flag %}{% set key = 'second' %}{% endif %}{{ vars[key] }}",
    "{% if flag == true %}{% set key = 'second' %}{% endif %}{{ vars[key] }}",
    "{% if flag_alias == true %}{% set key = 'second' %}{% endif %}{{ vars[key] }}",
])
def test_computed_timeout_uses_outer_key_despite_other_bindings(tmp_path, monkeypatch, template):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars:
    key: first
    first: 23
    flag: false
    flag_alias: '{{ flag }}'
    unused: "{{ lookup('irrelevant') }}"
  gather_timeout: >-
    %s
  tasks: []
""" % template)
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 23
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"first": 45})
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 45})]


def test_timeout_dependency_scan_skips_unreachable_elif(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_timeout: >-
    {% if true %}23{% elif 'a'.split(' ', maxsplit=99999999999999999999) %}42{% endif %}
  tasks: []
""")
    source, _ = compile_playbook(str(playbook))
    assert _compiled_plays(source, tmp_path)[0]["gather_timeout"] == 23


@pytest.mark.parametrize("source_text", [
    "{% if true %}{{ first }}{% elif lookup('irrelevant') %}42{% endif %}",
    "{{ first if true else lookup('irrelevant') }}",
    "{{ (flag and (lookup('env', 'COMPILED_TIMEOUT') | int)) or first }}",
    "{{ first or (lookup('env', 'COMPILED_TIMEOUT') | int) }}",
])
def test_timeout_override_ignores_unreachable_lookup(tmp_path, monkeypatch, source_text):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {first: 23, flag: false}
  gather_timeout: >-
    %s
  tasks: []
""" % source_text)
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 23
    called = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"first": 45})
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 45})]


@pytest.mark.parametrize("expression", ["cfg[key]", "cfg_alias[key]", "array[index]", "vars['fast' if flag else 'slow']"])
def test_timeout_override_can_change_a_computed_subscript(tmp_path, monkeypatch, expression):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars:
    cfg: {slow: 23, fast: '{{ fast_seconds }}'}
    cfg_alias: '{{ cfg }}'
    fast_seconds: 45
    key: slow
    array: [23, 45]
    index: 0
    slow: 23
    fast: 45
    flag: false
  gather_timeout: "{{ %s }}"
  tasks: []
""" % expression)
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 23
    overrides = {"key": "fast", "index": 1, "flag": True}
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars=overrides)
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 45})]


@pytest.mark.parametrize(("expression", "initial", "override"), [
    ("cfg or fallback", "{}", {"cfg": {"unrelated": 1}}),
    ("cfg_alias or fallback", "{}", {"cfg": {"unrelated": 1}}),
    ("cfg and fallback", "{unrelated: 1}", {"cfg": {}}),
    ("cfg | default(fallback, true)", "{}", {"cfg": {"unrelated": 1}}),
    ("cfg_alias | d(default_value=fallback, boolean=flag)", "{}", {"cfg": {"unrelated": 1}}),
    ("cfg | default(fallback, flag)", "{}", {"flag": False}),
    ("cfg | default(fallback)", None, {"cfg": {"unrelated": 1}}),
])
def test_controller_timeout_rechecks_projected_branch(tmp_path, monkeypatch, expression, initial, override):
    monkeypatch.setenv("COMPILED_TIMEOUT", "23")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars:
%s    flag: true
    cfg_alias: '{{ cfg }}'
    fallback: {timeout: "{{ lookup('env', 'COMPILED_TIMEOUT') | int }}"}
  gather_timeout: "{{ (%s).timeout | default(45) }}"
  tasks: []
""" % (f"    cfg: {initial}\n" if initial is not None else "", expression))
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 23
    called = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars=override)
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 45})]


@pytest.mark.parametrize("alternate_vars, alternate_error", [
    ("seconds: 45\n", None),
    ("seconds: 45\ntask_file: other.yml\n", "no bundled task file matches 'other.yml'"),
    ("seconds: [\n", "Invalid vars_files file"),
])
def test_ordered_timeout_only_errors_for_the_selected_unavailable_include(tmp_path, alternate_vars, alternate_error):
    from ansiroll.runtime import VarsFilesError

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {profile: default, task_file: tasks.yml}
  vars_files:
    - "settings-{{ profile }}.yml"
  gather_timeout: "{{ seconds }}"
  tasks:
    - include_tasks: "{{ task_file }}"
""")
    (tmp_path / "settings-default.yml").write_text("seconds: 23\n")
    (tmp_path / "settings-alternate.yml").write_text(alternate_vars)
    (tmp_path / "tasks.yml").write_text("- debug: {msg: included-marker}\n")
    source, manifest = compile_playbook(str(playbook))
    assert not manifest["fallback_tasks"]
    plan = _compiled_plays(source, tmp_path)[0]
    for profile in ("default", "alternate"):
        frames = []
        runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": profile},
                          on_result=frames.append)
        runtime.run_module = lambda *args, **kwargs: {"ansible_facts": {}}
        if profile == "alternate" and alternate_error == "Invalid vars_files file":
            try:
                runtime.run_play(plan)
            except VarsFilesError as error:
                assert alternate_error in str(error)
            else:
                assert runtime.failed
                assert all(frame["action"] == "gather_facts" for frame in frames)
        else:
            runtime.run_play(plan)
            messages = [frame["result"].get("msg") for frame in frames]
            assert (alternate_error if profile == "alternate" and alternate_error else "included-marker") in messages


@pytest.mark.parametrize(("task_vars", "expected"), [
    ("{unrelated: 1}", "included-alternate"),
    ("{task_file: default.yml}", "included-default"),
])
@pytest.mark.parametrize("selector", ["cfg.task_file", "(cfg | default({})).task_file",
                                       "(cfg | default({}, true)).task_file", "(cfg or {}).task_file"])
def test_ordered_timeout_checks_which_merged_members_an_include_reads(tmp_path, monkeypatch,
                                                                    task_vars, expected, selector):
    from ansible import constants as C

    monkeypatch.setenv("ANSIBLE_HASH_BEHAVIOUR", "merge")
    monkeypatch.setattr(C, "DEFAULT_HASH_BEHAVIOUR", "merge")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {profile: default}
  vars_files:
    - "settings-{{ profile }}.yml"
  gather_timeout: "{{ seconds }}"
  tasks:
    - include_tasks: "{{ %s }}"
      vars:
        cfg: %s
""" % (selector, task_vars))
    (tmp_path / "settings-default.yml").write_text("seconds: 23\ncfg: {task_file: default.yml}\n")
    (tmp_path / "settings-alternate.yml").write_text("seconds: 45\ncfg: {task_file: alternate.yml}\n")
    (tmp_path / "default.yml").write_text("- debug: {msg: included-default}\n")
    (tmp_path / "alternate.yml").write_text("- debug: {msg: included-alternate}\n")
    source, manifest = compile_playbook(str(playbook))
    assert not manifest["fallback_tasks"]
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": "alternate"},
                      on_result=frames.append)
    monkeypatch.setattr(runtime, "run_module", lambda *args, **kwargs: {"ansible_facts": {}})
    runtime.run_play(_compiled_plays(source, tmp_path)[0])
    assert expected in [frame["result"].get("msg") for frame in frames]


@pytest.mark.parametrize(("template", "overrides", "expected"), [
    ("{% set seconds = base %}{{ seconds }}", {}, 45),
    ("{% set seconds = base %}{% block timeout %}{{ seconds }}{% endblock %}", {}, 45),
    ("{% set flag = true %}{{ base if flag else seconds }}", {"flag": False}, 45),
    ("{% set flag = true %}{% set use_base = flag %}{% set flag = false %}{{ base if use_base else seconds }}",
     {}, 45),
    ("{% set local_cfg = cfg %}{{ local_cfg.seconds }}", {}, 45),
    ("{% if false %}{% macro unused() %}{{ local_cfg }}{% endmacro %}{% endif %}"
     "{% set local_cfg = cfg %}{{ local_cfg.seconds }}", {}, 45),
    ("{% macro unused() %}{{ base }}{% endmacro %}{% set local_cfg = cfg %}{{ local_cfg.seconds }}", {}, 45),
    ("{% macro unused() %}{{ base }}{% endmacro %}{% set key = member %}{{ cfg[key] }}", {"member": "seconds"}, 45),
    ("{% set member = 'unused' %}{{ cfg[member_alias] }}", {}, 45),
    ("{% set use_base = flag %}{{ base if use_base else seconds }}", {"flag": True}, 45),
    ("{% set use_base = flag %}{{ base if use_base else seconds }}", {"flag": False}, None),
    ("{% set local_cfg = cfg.group %}{{ local_cfg.seconds }}", {}, 45),
    ("{% set local_cfg = cfg['group'] %}{{ local_cfg.seconds }}", {}, 45),
    ("{% set local_cfg = cfg %}{% set alias = local_cfg %}{{ alias.seconds }}", {}, 45),
    ("{% if flag %}{% set seconds = base %}{% else %}{% set seconds = base %}{% endif %}{{ seconds }}",
     {"flag": False}, 45),
    ("{% if flag %}{% set seconds = base %}{% endif %}{{ seconds }}", {"flag": False}, None),
])
def test_ordered_timeout_ignores_vars_file_inputs_shadowed_by_a_local(tmp_path, monkeypatch,
                                                                    template, overrides, expected):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {profile: default, base: 23, flag: true, member: seconds, member_alias: '{{ member }}'}
  vars_files: ['settings-{{ profile }}.yml']
  gather_timeout: >-
    %s
  tasks: []
""" % template)
    (tmp_path / "settings-default.yml").write_text("seconds: 23\ncfg: {seconds: 23, group: {seconds: 23}}\n")
    (tmp_path / "settings-runtime.yml").write_text("""seconds: "{{ lookup('env', 'UNUSED_TIMEOUT') | int }}"
cfg:
  seconds: 45
  unused: "{{ lookup('env', 'UNUSED_TIMEOUT') | int }}"
  group:
    seconds: 45
    unused: "{{ lookup('env', 'UNUSED_TIMEOUT') | int }}"
""")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 23
    frames = []
    called = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, on_result=frames.append,
                      extra_vars={"profile": "runtime", "base": 45, **overrides})
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    if expected is None:
        assert "cannot be rendered before setup" in frames[0]["result"]["msg"]
        assert not called
    else:
        assert called == [("ansible.builtin.setup", {"gather_timeout": expected})]


@pytest.mark.parametrize("native, expected", [(False, 45), (True, None)])
def test_runtime_timeout_folds_aliases_using_legacy_rendering_mode(tmp_path, monkeypatch, native, expected):
    from ansiroll import runtime as runtime_mod

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {profile: default, base: 23}
  vars_files: ['settings-{{ profile }}.yml']
  gather_timeout: >-
    {% set use_base = flag %}{{ base if use_base else seconds }}
  tasks: []
""")
    (tmp_path / "settings-default.yml").write_text("flag: true\nseconds: 23\n")
    (tmp_path / "settings-runtime.yml").write_text(
        """flag: "{{ 0 }}"\nseconds: "{{ lookup('env', 'UNUSED_TIMEOUT') | int }}"\n"""
    )
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 23
    plan["controller_config"]["DEFAULT_JINJA2_NATIVE"] = native
    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", "2.12.10")
    frames = []
    called = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, on_result=frames.append,
                      extra_vars={"profile": "runtime", "base": 45})
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    if expected is None:
        assert "cannot be rendered before setup" in frames[0]["result"]["msg"]
        assert not called
    else:
        assert called == [("ansible.builtin.setup", {"gather_timeout": expected})]


def test_controller_timeout_cache_tracks_macro_captures_at_call_time(tmp_path, monkeypatch):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {base: 23, seconds: 60}
  gather_timeout: >-
    {% set flag = false %}{% macro timeout() %}{{ base if flag else seconds }}{% endmacro %}
    {% set flag = true %}{{ timeout() }}
  tasks: []
""")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 23
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, on_result=frames.append,
                      extra_vars={"base": 45})
    monkeypatch.setattr(runtime, "run_module",
                        lambda *args, **kwargs: pytest.fail("setup must not run with the stale timeout"))
    runtime.run_play(plan)
    assert "cannot be rendered before setup" in frames[0]["result"]["msg"]


@pytest.mark.parametrize("source_text", [
    "{% if flag %}{{ first }}{% else %}{{ second }}{% endif %}",
    "{{ first if flag else second }}",
    "{{ lookup('env', 'COMPILED_TIMEOUT') | int if flag else second }}",
    "{{ first if flag else (bad.x if other else second) }}",
    "{{ (primary if flag else secondary).timeout }}",
    "{{ lookup('env', 'COMPILED_TIMEOUT') | int if flag else vars[key] }}",
])
def test_timeout_override_can_select_a_branch_unused_at_compile_time(tmp_path, monkeypatch, source_text):
    from ansiroll.loader import _core_version

    if "bad.x if" in source_text and _core_version() < (2, 19):
        pytest.skip("Legacy Ansible eagerly resolves recursive aliases during compiler validation")
    monkeypatch.setenv("COMPILED_TIMEOUT", "23")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars:
    flag: true
    first: 23
    second: 45
    other: false
    bad: '{{ bad.x }}'
    primary: {timeout: 23}
    secondary: {timeout: 45}
    key: alpha
    alpha: 25
    beta: 45
  gather_timeout: >-
    %s
  tasks: []
""" % source_text)
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 23
    called = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"flag": False, "key": "beta"})
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 45})]


def test_timeout_vars_file_retains_role_defaults_for_alternate_selector_arm(tmp_path, monkeypatch):
    defaults = tmp_path / "roles" / "settings" / "defaults"
    defaults.mkdir(parents=True)
    (defaults / "main.yml").write_text("first_profile: first\nsecond_profile: second\n")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  roles: [settings]
  vars: {flag: true}
  vars_files:
    - 'timeouts-{{ first_profile if flag else second_profile }}.yml'
  gather_timeout: '{{ secs }}'
  tasks: []
""")
    (tmp_path / "timeouts-first.yml").write_text("secs: 23\n")
    (tmp_path / "timeouts-second.yml").write_text("secs: 45\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 23
    called = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"flag": False})
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    assert called == [("ansible.builtin.setup", {"gather_timeout": 45})]


def test_controller_timeout_uses_local_computed_key_invalidation(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIROLL_TIMEOUT_SUFFIX", "x")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {key: second, first: 23, second: 80}
  gather_timeout: >-
    {% set key = 'first' %}{{ vars[key] + (lookup('env', 'ANSIROLL_TIMEOUT_SUFFIX') | length) }}
  tasks: []
""")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 24
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"first": 45}, on_result=frames.append)
    monkeypatch.setattr(runtime, "run_module",
                        lambda *args, **kwargs: pytest.fail("setup must not run with a stale timeout"))
    runtime.run_play(plan)
    assert runtime.failed
    assert "cannot be rendered before setup" in frames[0]["result"]["msg"]


@pytest.mark.parametrize("profile", ["default", "runtime"])
def test_indirect_timeout_selector_tracks_earlier_vars_file_writes(tmp_path, monkeypatch, profile):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {profile: default, layer: default}
  vars_files:
    - "settings-{{ profile }}.yml"
    - "timeout-{{ lookup('vars', 'reyal' | reverse | join) }}.yml"
  gather_timeout: '{{ timeout_seconds }}'
  tasks: []
""")
    (tmp_path / "settings-default.yml").write_text("{}\n")
    (tmp_path / "settings-runtime.yml").write_text("layer: runtime\n")
    (tmp_path / "timeout-default.yml").write_text("timeout_seconds: 23\n")
    (tmp_path / "timeout-runtime.yml").write_text("timeout_seconds: 45\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 23
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, extra_vars={"profile": profile},
                      on_result=frames.append)
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    if profile == "runtime":
        assert runtime.failed
        assert "cannot be rendered before setup" in frames[0]["result"]["msg"]
        assert not called
    else:
        assert called == [("ansible.builtin.setup", {"gather_timeout": 23})]


@pytest.mark.parametrize("profile", ["default", "runtime"])
def test_indirect_timeout_selector_tracks_hash_merged_vars_file_writes(tmp_path, monkeypatch, profile):
    from ansible import constants as C

    monkeypatch.setenv("ANSIBLE_HASH_BEHAVIOUR", "merge")
    monkeypatch.setattr(C, "DEFAULT_HASH_BEHAVIOUR", "merge")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  vars: {profile: default, config: {layer: default}}
  vars_files:
    - "settings-{{ profile }}.yml"
    - "timeout-{{ lookup('vars', 'gifnoc' | reverse | join).layer }}.yml"
  gather_timeout: '{{ timeout_seconds }}'
  tasks: []
""")
    (tmp_path / "settings-default.yml").write_text("{}\n")
    (tmp_path / "settings-runtime.yml").write_text("config: {layer: runtime}\n")
    (tmp_path / "timeout-default.yml").write_text("timeout_seconds: 23\n")
    (tmp_path / "timeout-runtime.yml").write_text("timeout_seconds: 45\n")
    extras = {"config": {"unrelated": 1}}
    source, _ = compile_playbook(str(playbook), extra_vars=extras)
    plan = _compiled_plays(source, tmp_path)[0]
    assert plan["gather_timeout"] == 23
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"},
                      extra_vars={**extras, "profile": profile}, on_result=frames.append)
    called = []
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, **kwargs: called.append((action, args)) or {"ansible_facts": {}})
    runtime.run_play(plan)
    if profile == "runtime":
        assert runtime.failed
        assert "cannot be rendered before setup" in frames[0]["result"]["msg"]
        assert not called
    else:
        assert called == [("ansible.builtin.setup", {"gather_timeout": 23})]


def test_legacy_include_is_rewritten_and_inlined(tmp_path):
    import jinja2

    environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
    src, manifest = compile_playbook(os.path.join(FIXTURES, "legacy", "play.yml"))
    # The included task must be inlined (no crash on removed `include`), and not
    # left as a fallback.
    importers = tuple(sys.meta_path)
    assert "included-marker" in json.dumps(_compiled_plays(src, tmp_path))
    assert tuple(sys.meta_path) == importers
    assert manifest["fallback_tasks"] == []
    with pytest.raises(jinja2.UndefinedError):
        environment.from_string("{{ missing }}").render()


@pytest.mark.parametrize("root_include", ["include", "import_playbook"])
def test_legacy_playbook_includes_preserve_nested_task_includes(tmp_path, root_include):
    playbook = tmp_path / "play.yml"
    playbook.write_text("- %s: sub/wrapper.yml\n" % root_include)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "wrapper.yml").write_text("- include: leaf.yml\n")
    (sub / "leaf.yml").write_text("""
- hosts: localhost
  gather_facts: false
  tasks:
    - include: tasks.yml
""")
    (sub / "tasks.yml").write_text("- include: final.yml\n")
    (sub / "final.yml").write_text("- debug: {msg: legacy-playbook-leaf}\n")

    source, manifest = compile_playbook(str(playbook))
    assert "legacy-playbook-leaf" in json.dumps(_compiled_plays(source, tmp_path))
    assert manifest["fallback_tasks"] == []


@pytest.mark.parametrize(("include", "greeting"), [
    ("""- include: "{{ child_playbook }} greeting='hello world' tags=imported"\n""", "hello world"),
    ("- include: >\n    {{ child_playbook }}\n", "original"),
    ("- include: |\n    {{ child_playbook }}\n    greeting=hello\n", "hello"),
])
def test_legacy_playbook_include_preserves_inline_variables(tmp_path, include, greeting):
    playbook = tmp_path / "play.yml"
    playbook.write_text(include)
    (tmp_path / "child.yml").write_text("""
- hosts: localhost
  gather_facts: false
  vars: {greeting: original}
  tasks: []
""")

    plays, _ = parse_playbook(str(playbook), extra_vars={"child_playbook": "child.yml"})
    assert plays[0]["vars"]["greeting"] == greeting
    assert "tags" not in plays[0]["vars"]


def test_legacy_playbook_include_preserves_explicit_raw_params(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("- include: child.yml _raw_params=hello\n")
    (tmp_path / "child.yml").write_text("- hosts: localhost\n  gather_facts: false\n  tasks: []\n")

    plays, _ = parse_playbook(str(playbook))
    assert plays[0]["vars"]["_raw_params"] == "hello"


@pytest.mark.parametrize("parameters", ["trailing text", "_raw_params=hello trailing text"])
def test_legacy_playbook_include_rejects_unassigned_parameters(tmp_path, parameters):
    from ansible.errors import AnsibleParserError

    playbook = tmp_path / "play.yml"
    playbook.write_text(f"- include: child.yml {parameters}\n")

    with pytest.raises(AnsibleParserError, match="must use key=value"):
        parse_playbook(str(playbook))


@pytest.mark.parametrize(("include", "tag"), [
    ("- include: child.yml tags=inline\n  tags: explicit\n", "explicit"),
    ("- tags: explicit\n  include: child.yml tags=inline\n", "inline"),
])
def test_legacy_playbook_include_uses_last_tag_declaration(tmp_path, include, tag):
    from ansible.parsing.dataloader import DataLoader
    from ansible.playbook import Playbook

    from ansiroll.loader import _make_rewriting_loader

    playbook = tmp_path / "play.yml"
    playbook.write_text(include)
    (tmp_path / "child.yml").write_text("- hosts: localhost\n  tasks: []\n")

    plays = Playbook.load(str(playbook), loader=_make_rewriting_loader(DataLoader)).get_plays()
    assert plays[0].tags == [tag]


@pytest.mark.parametrize("inline_tags", [False, True])
def test_legacy_playbook_include_defers_import_fields(tmp_path, monkeypatch, inline_tags):
    from ansiroll import loader

    include = "child.yml tags='{{ deployment_tag }}'" if inline_tags else "child.yml"
    tags = "" if inline_tags else '  tags: ["{{ deployment_tag }}"]\n'
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        f'- include: "{include}"\n' + tags
        + """  when: "{{ inventory_hostname == 'localhost' }}"\n"""
    )
    (tmp_path / "child.yml").write_text("""
- hosts: localhost
  gather_facts: false
  vars: {deployment_tag: rollout}
  tasks:
    - debug: {msg: imported-task}
""")
    original_convert = loader._convert_play

    def check_tags(play, *args, **kwargs):
        assert play.tags == ["{{ deployment_tag }}"]
        return original_convert(play, *args, **kwargs)

    monkeypatch.setattr(loader, "_convert_play", check_tags)
    source, _ = compile_playbook(str(playbook))
    plans = _compiled_plays(source, tmp_path)
    condition = {"__ansiroll_trusted_condition__": "{{ inventory_hostname == 'localhost' }}"}
    assert plans[0]["gather_when"] == [condition]
    task = next(task for block in plans[0]["tasks"] for task in block["block"] if task.get("action") == "debug")
    assert task["when"] == [condition]
    result = subprocess.run([sys.executable, str(tmp_path / "compiled.py")],
                            capture_output=True, text=True, timeout=30, check=True)
    assert "imported-task" in result.stdout


def test_legacy_playbook_include_ignores_templated_import_name(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""
- name: "{{ deployment_name }}"
  include: child.yml
""")
    (tmp_path / "child.yml").write_text("""
- name: Child play
  hosts: localhost
  gather_facts: false
  vars: {deployment_name: rollout}
  tasks: []
""")

    plays, _ = parse_playbook(str(playbook))
    assert [play["name"] for play in plays] == ["Child play"]


@pytest.mark.parametrize("modern", ["import_playbook", "ansible.builtin.import_playbook", "ansible.legacy.import_playbook"])
def test_legacy_playbook_include_rejects_conflicting_imports(tmp_path, modern):
    from ansible.errors import AnsibleParserError

    playbook = tmp_path / "play.yml"
    playbook.write_text(f"- include: child.yml\n  {modern}: child.yml\n")
    (tmp_path / "child.yml").write_text("- hosts: localhost\n  tasks: []\n")

    with pytest.raises(AnsibleParserError, match="conflicting playbook include"):
        parse_playbook(str(playbook))


def test_legacy_playbook_include_keeps_aliased_mapping_in_later_vars(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""
- &imported
  include: child.yml
- hosts: localhost
  gather_facts: false
  vars: {settings: *imported}
  tasks: []
""")
    (tmp_path / "child.yml").write_text("- hosts: localhost\n  tasks: []\n")

    plays, _ = parse_playbook(str(playbook))
    assert plays[1]["vars"]["settings"] == {"include": "child.yml"}


def test_legacy_include_rewrite_preserves_list_form_vars_file_keys(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""
- hosts: localhost
  gather_facts: false
  vars_files: [vars.yml]
  tasks:
    - include: tasks.yml
""")
    (tmp_path / "vars.yml").write_text("- include: preserved\n")
    (tmp_path / "tasks.yml").write_text('- debug: {msg: "{{ include }}"}\n')

    source, manifest = compile_playbook(str(playbook))
    assert not manifest["fallback_tasks"]
    assert _compiled_plays(source, tmp_path)[0]["vars"]["include"] == "preserved"
    result = subprocess.run([sys.executable, str(tmp_path / "compiled.py")],
                            capture_output=True, text=True, timeout=30, check=True)
    assert "preserved" in result.stdout


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
    plays = _compiled_plays(src, tmp_path)

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
    task = next(node for node in tasks(plays[0]["tasks"])
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
    plays = _compiled_plays(src, tmp_path)
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
    tasks = list(leaf_tasks(plays[0]["tasks"]))

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


@pytest.mark.parametrize("legacy_copy", [False, True])
def test_compiled_unarchive_uses_baked_controller_source_from_memory(tmp_path, legacy_copy):
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
        dest: %s%s
""" % (destination, "\n        copy: true" if legacy_copy else ""))

    src, manifest = compile_playbook(str(play))
    archive.unlink()
    # copy/template stage their data in a temp dir like the action plugin;
    # unarchive must keep using the in-memory source.
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


@pytest.mark.parametrize("link", [None, "file", "directory"])
def test_strategy_reads_runtime_created_controller_source_losslessly(tmp_path, link):
    strategy = _load_strategy_module()
    directory = tmp_path / "sources"
    directory.mkdir()
    source = directory / "runtime.bin"
    source.write_bytes(b"\x00\xff'\\\n")
    source.chmod(0o641)
    path = source
    if link == "file":
        path = tmp_path / "linked.bin"
        path.symlink_to(source.relative_to(tmp_path))
    elif link == "directory":
        alias = tmp_path / "linked-dir"
        alias.symlink_to(directory.name, target_is_directory=True)
        path = alias / source.name

    result = strategy.StrategyModule._read_controller_source(str(path))

    metadata = source.stat()
    assert base64.b64decode(result["data"]) == source.read_bytes()
    assert result["meta"] == {
        "fullpath": str(path),
        "mode": 0o641,
        "mtime": metadata.st_mtime,
        "uid": metadata.st_uid,
    }


def test_strategy_skips_open_for_nonregular_runtime_created_controller_source(tmp_path, monkeypatch):
    strategy = _load_strategy_module()

    def unexpected_open(*args, **kwargs):
        pytest.fail("the controller should skip known non-regular paths")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", unexpected_open)
        result = strategy.StrategyModule._read_controller_source(str(tmp_path))

    assert result == {"error": "controller source %r is not a regular file" % str(tmp_path)}


def test_strategy_reports_runtime_created_controller_source_open_error(tmp_path, monkeypatch):
    strategy = _load_strategy_module()
    source = tmp_path / "runtime.bin"
    source.write_bytes(b"fixture")

    def deny_open(path, flags):
        assert path == str(source)
        assert flags & os.O_NONBLOCK
        raise PermissionError("mocked access denial")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", deny_open)
        result = strategy.StrategyModule._read_controller_source(str(source))

    assert result == {"error": "could not read controller source %r: mocked access denial" % str(source)}


@pytest.mark.parametrize("busy_attempts", [1, 5])
def test_strategy_retries_busy_runtime_created_controller_source(tmp_path, monkeypatch, busy_attempts):
    strategy = _load_strategy_module()
    source = tmp_path / "runtime.bin"
    source.write_bytes(b"fixture")
    real_open = os.open
    attempts = []
    delays = []

    def open_source(path, flags):
        assert flags & os.O_NONBLOCK
        attempts.append(path)
        if len(attempts) <= busy_attempts:
            raise BlockingIOError(errno.EAGAIN, "mocked busy source")
        return real_open(path, flags)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", open_source)
        patch.setattr(time, "sleep", delays.append)
        result = strategy.StrategyModule._read_controller_source(str(source))

    if busy_attempts == 1:
        assert attempts == [str(source)] * 2
        assert delays == [0.05]
        assert base64.b64decode(result["data"]) == b"fixture"
    else:
        assert attempts == [str(source)] * 5
        assert delays == [0.05] * 4
        assert "mocked busy source" in result["error"]


def test_strategy_checks_runtime_created_controller_source_descriptor_before_reading(tmp_path, monkeypatch):
    strategy = _load_strategy_module()
    source = tmp_path / "runtime.bin"
    source.write_bytes(b"fixture")
    fd = os.open(source, os.O_RDONLY)

    def check_descriptor(opened_fd):
        assert opened_fd == fd
        return SimpleNamespace(st_mode=stat.S_IFDIR)

    def unexpected_reader(*args, **kwargs):
        pytest.fail("the descriptor must be validated before opening the reader")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", lambda path, flags: fd)
        patch.setattr(os, "fstat", check_descriptor)
        patch.setattr(os, "fdopen", unexpected_reader)
        result = strategy.StrategyModule._read_controller_source(str(source))

    with pytest.raises(OSError):
        os.fstat(fd)
    assert result == {"error": "controller source %r is not a regular file" % str(source)}


def test_strategy_reports_runtime_created_controller_source_close_error(tmp_path, monkeypatch):
    strategy = _load_strategy_module()
    source = tmp_path / "runtime.bin"
    source.write_bytes(b"fixture")
    real_close = os.close

    def report_close_error(fd):
        real_close(fd)
        raise OSError("mocked close error")

    with monkeypatch.context() as patch:
        patch.setattr(os, "close", report_close_error)
        result = strategy.StrategyModule._read_controller_source(str(source))

    assert result == {"error": "could not read controller source %r: mocked close error" % str(source)}


@pytest.mark.parametrize("action", ["copy", "template"])
def test_strategy_preserves_runtime_created_source_mode(tmp_path, action):
    delegation = "      delegate_to: localhost" if action == "template" else ""
    source = tmp_path / "runtime.txt"
    destination = tmp_path / "result.txt"
    playbook = """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - copy:
        content: fixture
        dest: %s
        mode: '0641'
    - %s:
        src: %s
        dest: %s
        mode: preserve
%s
""" % (source, action, source, destination, delegation)
    result = _run_strategy_play(tmp_path, playbook)
    assert result.returncode == 0, result.stdout + result.stderr
    assert destination.read_text() == "fixture"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o641


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


def test_strategy_excludes_failed_hosts_from_later_native_plays(tmp_path):
    result = _run_strategy_play(
        tmp_path,
        """- hosts: bad:good
  gather_facts: false
  connection: local
  tasks:
    - command: /bin/true
    - fail: {msg: expected host failure}
      when: inventory_hostname == 'bad'
- hosts: bad:good
  gather_facts: false
  connection: local
  tasks:
    - copy:
        content: reached
        dest: "{{ playbook_dir }}/{{ inventory_hostname }}.txt"
""",
        "-i", "bad,good,",
    )

    assert result.returncode != 0
    assert (tmp_path / "good.txt").read_text() == "reached", result.stdout + result.stderr
    assert not (tmp_path / "bad.txt").exists()


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


def _run_strategy_play(tmp_path, playbook, *args, environment=None, inventory="localhost,", timeout=None):
    path = tmp_path / "play.yml"
    path.write_text(playbook)
    env = os.environ.copy()
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["ANSIBLE_COLLECTIONS_PATH"] = os.path.join(ROOT, "collections")
    env["ANSIBLE_STRATEGY"] = "ansiroll.core.ansiroll"
    env["ANSIBLE_NOCOLOR"] = "1"
    env.update(environment or {})
    return subprocess.run(
        ["ansible-playbook", "-i", inventory, str(path), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_standalone_task_omit_preserves_invocation_check_mode(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  tasks:
    - command: /bin/true
      check_mode: "{{ omit }}"
""")
    source, _ = compile_playbook(str(playbook))
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, check_mode=True)
    runtime.run_play(_compiled_plays(source, tmp_path)[0])
    assert runtime.stats["skipped"] == 1
    assert not runtime.failed


@pytest.mark.parametrize("scope", ["invocation", "parent", "tasks-apply", "role-apply", "play", "raw-parent"])
def test_strategy_preserves_check_mode_on_omit(tmp_path, scope):
    task = """- file:
    path: "{{ playbook_dir }}/result"
    state: touch
  check_mode: "{{ omit }}"
"""
    playbook = """- hosts: localhost
  gather_facts: false
"""
    if scope == "tasks-apply":
        (tmp_path / "child.yml").write_text(task)
        tasks = "- include_tasks:\n    file: child.yml\n    apply: {check_mode: false}\n"
    elif scope == "role-apply":
        role_tasks = tmp_path / "roles" / "child" / "tasks"
        role_tasks.mkdir(parents=True)
        (role_tasks / "main.yml").write_text(task)
        tasks = "- include_role:\n    name: child\n    apply: {check_mode: false}\n"
    elif scope in ("parent", "raw-parent"):
        parent_mode = 'false' if scope == "parent" else '"{{ simulate }}"'
        tasks = "- block:\n" + textwrap.indent(task, "    ") + "  check_mode: " + parent_mode + "\n"
    else:
        tasks = task
    if scope == "play":
        playbook += "  check_mode: true\n"
    playbook += "  tasks:\n" + textwrap.indent(tasks, "    ")
    opts = ["-c", "local", "-e", "ansible_python_interpreter=" + sys.executable]
    if scope not in ("play", "raw-parent"):
        opts.append("--check")
    output = tmp_path / "result"
    expected = _run_strategy_play(tmp_path, playbook, *opts,
                                  environment={"ANSIBLE_STRATEGY": "linear"})
    if scope != "raw-parent":
        assert expected.returncode == 0, expected.stdout + expected.stderr
    native_wrote_file = output.exists()
    if native_wrote_file:
        output.unlink()
    actual = _run_strategy_play(tmp_path, playbook, *opts)
    assert actual.returncode == expected.returncode, actual.stdout + actual.stderr
    assert output.exists() == native_wrote_file


@pytest.mark.parametrize("include", ["tasks", "role"])
@pytest.mark.parametrize(("apply_mode", "check", "writes"), [
    ("omit", True, False), ("none", True, True), ("safe", False, False),
])
def test_strategy_restores_resolved_include_apply_check_mode(tmp_path, include, apply_mode, check, writes,
                                                            monkeypatch):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("requires native include-time apply rendering")
    if include == "role":
        child = tmp_path / "roles" / "child" / "tasks" / "main.yml"
        child.parent.mkdir(parents=True)
        target = "name: child"
    else:
        child = tmp_path / "child.yml"
        target = "file: child.yml"
    child.write_text("""- file: {path: '{{ playbook_dir }}/result', state: touch}
  check_mode: '{{ omit }}'
""")
    play = """- hosts: localhost
  connection: local
  gather_facts: false
  vars: {safe: true}
  tasks:
    - include_KIND:
        TARGET
        apply:
          check_mode: '{{ MODE }}'
""".replace("KIND", include).replace("TARGET", target).replace("MODE", apply_mode)
    options = ["-e", "ansible_python_interpreter=" + sys.executable]
    if check:
        options.append("--check")
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        result = _run_strategy_play(tmp_path, play, *options, environment={"ANSIBLE_STRATEGY": strategy})
        assert result.returncode == 0, result.stdout + result.stderr
        output = tmp_path / "result"
        assert output.exists() is writes, result.stdout + result.stderr
        if writes:
            output.unlink()

    source, _ = compile_playbook(str(tmp_path / "play.yml"))
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, check_mode=check)
    observed = []
    monkeypatch.setattr(runtime, "_run_accelerated_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "run_module", lambda *args, **kwargs: observed.append(runtime.check_mode)
                        or {"changed": False})
    runtime.run_play(_compiled_plays(source, tmp_path)[0])
    assert not runtime.failed
    assert observed == [not writes]


@pytest.mark.parametrize("case", [
    "looped", "buffered", "vars", "vars-native", "vars-nested-omit", "raw-parent", "tasks-raw-parent",
])
def test_role_apply_check_mode_matches_native_include_scope(tmp_path, monkeypatch, case):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("requires native role apply rendering")
    role = tmp_path / "roles" / "child"
    (role / "tasks").mkdir(parents=True)
    (role / "vars").mkdir()
    (role / "vars" / "main.yml").write_text("toggle: false\n")
    child_mode = {
        "vars": "mode",
        "vars-native": "mode is boolean",
        "vars-nested-omit": "label == '[]-suffix'",
    }.get(case, "omit")
    task_file = tmp_path / "child.yml" if case == "tasks-raw-parent" else role / "tasks" / "main.yml"
    task_file.write_text("""- file:
    path: "{{ playbook_dir }}/result-{{ item | default('only') }}"
    state: touch
  check_mode: "{{ CHILD_MODE }}"
""".replace("CHILD_MODE", child_mode))
    apply = {
        "looped": "check_mode: '{{ item }}'",
        "buffered": "check_mode: '{% if simulate %}{{ true }}{% else %}{{ omit }}{% endif %}'",
        "vars": "vars: {mode: '{{ toggle }}'}",
        "vars-native": "vars: {mode: '{% if true %}{{ true }}{% endif %}'}",
        "vars-nested-omit": "vars: {label: '{{ [omit] }}-suffix'}",
        "raw-parent": "check_mode: '{{ omit }}'",
        "tasks-raw-parent": "check_mode: '{% if true %}{{ omit }}{% endif %}'",
    }[case]
    task = "- include_role:\n    name: child\n    apply:\n      " + apply + "\n"
    if case == "tasks-raw-parent":
        task = task.replace("include_role:", "include_tasks:").replace("name: child", "file: child.yml")
    if case == "looped":
        task += "  loop: [true, false]\n"
    rejects_parent = case in ("raw-parent", "tasks-raw-parent")
    if rejects_parent:
        task = "- block:\n" + textwrap.indent(task, "    ") + "  check_mode: '{{ simulate }}'\n"
    play = """- hosts: localhost
  connection: local
  gather_facts: false
  vars: {mode: true, toggle: true, simulate: false}
  tasks:
""" + textwrap.indent(task, "    ")
    native = _run_strategy_play(tmp_path, play, "--check", "-e", "ansible_python_interpreter=" + sys.executable,
                                environment={"ANSIBLE_STRATEGY": "linear"})
    expected = [True, False] if case == "looped" else [] if rejects_parent else [True]
    assert (native.returncode == 0) is not rejects_parent, native.stdout + native.stderr
    assert len(list(tmp_path.glob("result-*"))) == expected.count(False), native.stdout + native.stderr

    source, _ = compile_playbook(str(tmp_path / "play.yml"))
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, check_mode=True)
    observed = []
    monkeypatch.setattr(runtime, "_run_accelerated_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "run_module", lambda *args, **kwargs: observed.append(runtime.check_mode)
                        or {"changed": False})
    runtime.run_play(_compiled_plays(source, tmp_path)[0])
    assert runtime.failed is rejects_parent
    assert observed == expected


@pytest.mark.parametrize("nested", [False, True])
def test_compile_skips_role_apply_lookups(tmp_path, monkeypatch, nested):
    from ansible.plugins.loader import lookup_loader
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("requires native role apply rendering")
    calls = []
    lookup_type = type(lookup_loader.get("env"))
    monkeypatch.setattr(lookup_type, "run", lambda *args, **kwargs: calls.append(True) or ["child.yml"])
    tasks = tmp_path / "roles" / "child" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text("- include_tasks: '{{ source_file }}'\n" if nested else "- debug: {msg: child}\n")
    (tasks / "child.yml").write_text("- debug: {msg: nested}\n")
    play = tmp_path / "play.yml"
    play.write_text("""- hosts: localhost
  gather_facts: false
  vars:
    file_alias: "{{ lookup('env', 'ANSIROLL_ROLE_APPLY_FILENAME') }}"
  tasks:
    - include_role:
        name: child
        apply:
          vars: {source_file: '{{ file_alias }}'}
      when: false
""")
    compile_playbook(str(play))
    assert not calls


@pytest.mark.parametrize(("action", "invocation_check", "delegated"), [
    ("copy", True, False),
    ("template", True, False),
    ("template", True, True),
    ("raw_copy", False, False),
])
def test_strategy_restores_copy_transfer_check_mode_on_omit(tmp_path, action, invocation_check, delegated):
    (tmp_path / "input.txt").write_text("payload\n")
    file_action = "copy" if action == "raw_copy" else action
    source_arg = "content: payload" if action == "copy" else "src: input.txt"
    tasks = f"""- {file_action}:
    {source_arg}
    dest: "{{{{ playbook_dir }}}}/result"
"""
    if action == "raw_copy":
        tasks += "    raw: true\n"
    tasks += '  check_mode: "{{ omit }}"\n'
    if delegated:
        tasks += "  delegate_to: localhost\n"
    playbook = ("- hosts: localhost\n  gather_facts: false\n  tasks:\n    - block:\n"
                + textwrap.indent(tasks, "        ")
                + f"      check_mode: {str(not invocation_check).lower()}\n")
    opts = ["-c", "local", "-e", "ansible_python_interpreter=" + sys.executable]
    if invocation_check:
        opts.append("--check")
    output = tmp_path / "result"
    native = _run_strategy_play(tmp_path, playbook, *opts, environment={"ANSIBLE_STRATEGY": "linear"})
    assert native.returncode == 0, native.stdout + native.stderr
    native_wrote_file = output.exists()
    if native_wrote_file:
        output.unlink()
    actual = _run_strategy_play(tmp_path, playbook, *opts)
    assert actual.returncode == 0, actual.stdout + actual.stderr
    assert output.exists() == native_wrote_file


def test_standalone_role_inherits_check_mode_without_apply(tmp_path):
    tasks = tmp_path / "roles" / "child" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text("""- file:
    path: "{{ playbook_dir }}/result"
    state: touch
  check_mode: "{{ omit }}"
""")
    playbook = """- hosts: localhost
  gather_facts: false
  tasks:
    - block:
        - include_role: {name: child}
      check_mode: true
"""
    output = tmp_path / "result"
    native = _run_strategy_play(tmp_path, playbook, "-c", "local",
                                "-e", "ansible_python_interpreter=" + sys.executable,
                                environment={"ANSIBLE_STRATEGY": "linear"})
    assert native.returncode == 0, native.stdout + native.stderr
    native_wrote_file = output.exists()
    if native_wrote_file:
        output.unlink()

    source, _ = compile_playbook(str(tmp_path / "play.yml"), python_interpreter=sys.executable)
    compiled = tmp_path / "compiled.py"
    compiled.write_text(source)
    result = subprocess.run([sys.executable, str(compiled)], cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.exists() == native_wrote_file


@pytest.mark.parametrize("simulate", [False, True])
def test_role_check_mode_can_read_its_applied_vars(tmp_path, simulate):
    tasks = tmp_path / "roles" / "child" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text("""- file: {path: '{{ playbook_dir }}/result', state: touch}
  check_mode: '{{ simulate }}'
""")
    playbook = """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - include_role:
        name: child
        apply:
          vars: {simulate: SIMULATE}
""".replace("SIMULATE", json.dumps(simulate))
    output = tmp_path / "result"
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        result = _run_strategy_play(tmp_path, playbook, "-e", "ansible_python_interpreter=" + sys.executable,
                                    environment={"ANSIBLE_STRATEGY": strategy})
        assert result.returncode == 0, result.stdout + result.stderr
        assert output.exists() is (not simulate), result.stdout + result.stderr
        if output.exists():
            output.unlink()
    compiled = tmp_path / "compiled.py"
    compiled.write_text(compile_playbook(str(tmp_path / "play.yml"), python_interpreter=sys.executable)[0])
    result = subprocess.run([sys.executable, str(compiled)], cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.exists() is (not simulate)


def test_role_handler_check_mode_keeps_buffered_omit_parameter(tmp_path, monkeypatch):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("modern Core preserves a native omit value in template buffers")
    handlers = tmp_path / "roles" / "child" / "handlers"
    handlers.mkdir(parents=True)
    (handlers / "main.yml").write_text("""- name: record dry-run
  file: {path: '{{ playbook_dir }}/result', state: touch}
  check_mode: '{{ fallback }}'
  register: handled
""")
    playbook = """- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - include_role: {name: child}
      vars: {fallback: '{% if true %}{{ omit }}{% endif %}'}
    - debug: {msg: notify}
      changed_when: true
      notify: record dry-run
    - meta: flush_handlers
    - assert: {that: handled is changed}
"""
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        result = _run_strategy_play(tmp_path, playbook, "--check", "-e", "ansible_python_interpreter=" + sys.executable,
                                    environment={"ANSIBLE_STRATEGY": strategy})
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (tmp_path / "result").exists()
    source, _ = compile_playbook(str(tmp_path / "play.yml"))
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, check_mode=True)
    observed = []
    monkeypatch.setattr(runtime, "_run_accelerated_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "run_module", lambda *args, **kwargs: observed.append(runtime.check_mode)
                        or {"changed": True})
    runtime.run_play(_compiled_plays(source, tmp_path)[0])
    assert not runtime.failed
    assert observed == [True]


@pytest.mark.parametrize("expression", ["{{ none }}", "{{ omit }}{{ false }}"])
def test_strategy_preserves_rendered_false_check_mode(tmp_path, expression):
    from ansible.release import __version__
    from packaging.version import Version

    if Version(__version__) < Version("2.19"):
        pytest.skip("uses modern omit and null templating")
    playbook = """- hosts: localhost
  gather_facts: false
  tasks:
    - file:
        path: "{{ playbook_dir }}/result"
        state: touch
      check_mode: CHECK_MODE
""".replace("CHECK_MODE", json.dumps(expression))
    output = tmp_path / "result"
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        proc = _run_strategy_play(tmp_path, playbook, "--check", "-c", "local",
                                  environment={"ANSIBLE_STRATEGY": strategy})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert output.exists()
        output.unlink()


@pytest.mark.parametrize("expression", [
    "{% if true %}{{ omit }}{% endif %}",
    "{% if true %}\n{{ omit }}{% endif %}",
    " {{ omit }} ",
    " {{- omit -}} ",
    "{{ omit }}\n",
    "{% macro fallback() %}{{ omit }}{% endmacro %}{{ fallback() }}",
])
def test_strategy_full_check_mode_omit_matches_native(tmp_path, expression):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("requires native omit values")
    playbook = """- hosts: localhost
  gather_facts: false
  tasks:
    - file:
        path: "{{ playbook_dir }}/result"
        state: touch
      check_mode: CHECK_MODE
""".replace("CHECK_MODE", json.dumps(expression))
    opts = ["--check", "-c", "local", "-e", "ansible_python_interpreter=" + sys.executable]
    native = _run_strategy_play(tmp_path, playbook, *opts, environment={"ANSIBLE_STRATEGY": "linear"})
    actual = _run_strategy_play(tmp_path, playbook, *opts)
    assert actual.returncode == native.returncode, actual.stdout + actual.stderr + native.stdout + native.stderr
    assert not (tmp_path / "result").exists()


def test_strategy_check_mode_resolves_task_local_omit_alias(tmp_path):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("requires native omit values")
    playbook = """- hosts: localhost
  gather_facts: false
  tasks:
    - file:
        path: "{{ playbook_dir }}/result"
        state: touch
      vars:
        fallback: '{% if true %}{{ omit }}{% endif %}'
      check_mode: '{{ fallback }}'
"""
    opts = ["--check", "-c", "local", "-e", "ansible_python_interpreter=" + sys.executable]
    native = _run_strategy_play(tmp_path, playbook, *opts, environment={"ANSIBLE_STRATEGY": "linear"})
    actual = _run_strategy_play(tmp_path, playbook, *opts)
    assert native.returncode == actual.returncode == 0, actual.stdout + actual.stderr + native.stdout + native.stderr
    assert not (tmp_path / "result").exists()


@pytest.mark.parametrize(("expression", "invocation_check", "valid"), [
    ('{% macro m() %}{{ omit }}{% endmacro %}{{ m() != "" }}', True, True),
    ("{{ omit | string }}", False, False),
    ("{# explanation #}true", False, True),
])
def test_strategy_validates_native_check_mode_buffers(tmp_path, expression, invocation_check, valid):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("requires native keyword values")
    playbook = """- hosts: localhost
  gather_facts: false
  tasks:
    - file:
        path: "{{ playbook_dir }}/result"
        state: touch
      check_mode: CHECK_MODE
""".replace("CHECK_MODE", json.dumps(expression))
    opts = ["-c", "local", "-e", "ansible_python_interpreter=" + sys.executable]
    if invocation_check:
        opts.append("--check")
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        result = _run_strategy_play(tmp_path, playbook, *opts, environment={"ANSIBLE_STRATEGY": strategy})
        assert (result.returncode == 0) == valid, result.stdout + result.stderr
        assert not (tmp_path / "result").exists()


@pytest.mark.parametrize("access", ["lookup('vars', 'fallback')", "query('vars', 'fallback')[0]",
                                   "vars.fallback", "vars['fallback']", "vars.get('fallback')",
                                   "mapping.values() | first", "(mapping.items() | first)[1]",
                                   "buffered", "mapping.buffered", "copy_of_fallback",
                                   "vars.copy_of_fallback", "lookup('vars', 'copy_of_buffered')"])
def test_strategy_check_mode_keeps_include_omit_alias_consistent(tmp_path, access):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("requires native omit values")
    (tmp_path / "child.yml").write_text("""- file:
    path: '{{ playbook_dir }}/result'
    state: touch
  check_mode: CHECK_MODE
  vars:
    copy_of_fallback: '{{ fallback }}'
    copy_of_buffered: '{{ buffered }}'
""".replace("CHECK_MODE", json.dumps("{{ fallback == " + access + " }}")))
    playbook = """- hosts: localhost
  gather_facts: false
  tasks:
    - include_tasks: child.yml
      vars:
        fallback: '{{ omit }}'
        buffered: '{% if true %}{{ omit }}{% endif %}'
        mapping:
          value: '{{ omit }}'
          buffered: '{% if true %}{{ omit }}{% endif %}'
"""
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        result = _run_strategy_play(tmp_path, playbook, "--check", "-c", "local",
                                    "-e", "ansible_python_interpreter=" + sys.executable,
                                    environment={"ANSIBLE_STRATEGY": strategy})
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (tmp_path / "result").exists(), result.stdout + result.stderr


def test_strategy_check_mode_bool_filter_treats_omit_as_false(tmp_path):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("requires native omit values")
    playbook = """- hosts: localhost
  gather_facts: false
  tasks:
    - file:
        path: '{{ playbook_dir }}/result'
        state: touch
      check_mode: '{{ omit | bool }}'
"""
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        result = _run_strategy_play(tmp_path, playbook, "-c", "local",
                                    "-e", "ansible_python_interpreter=" + sys.executable,
                                    environment={"ANSIBLE_STRATEGY": strategy})
        assert result.returncode == 0, result.stdout + result.stderr
        assert (tmp_path / "result").is_file(), result.stdout + result.stderr
        (tmp_path / "result").unlink()


def test_strategy_keeps_explicit_omit_like_variable_as_a_string(tmp_path):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("requires native omit values")
    playbook = """- hosts: localhost
  gather_facts: false
  tasks:
    - file:
        path: "{{ playbook_dir }}/result"
        state: touch
      vars: {omit: __omit_place_holder__user_value}
      check_mode: "{{ omit is string }}"
"""
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        result = _run_strategy_play(tmp_path, playbook, "--check", "-c", "local",
                                    "-e", "ansible_python_interpreter=" + sys.executable,
                                    environment={"ANSIBLE_STRATEGY": strategy})
        assert result.returncode == 0, result.stdout + result.stderr
        assert not (tmp_path / "result").exists()


@pytest.mark.parametrize("expression", ["{{ fallback }}{{ false }}", "{{ fallback is string }}"])
def test_strategy_preserves_native_omit_from_block_alias(tmp_path, expression):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("requires native omit values")
    playbook = """- hosts: localhost
  gather_facts: false
  tasks:
    - vars:
        fallback: "{{ omit }}"
      block:
        - file:
            path: "{{ playbook_dir }}/result"
            state: touch
          check_mode: CHECK_MODE
""".replace("CHECK_MODE", json.dumps(expression))
    output = tmp_path / "result"
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        proc = _run_strategy_play(tmp_path, playbook, "--check", "-c", "local",
                                  "-e", "ansible_python_interpreter=" + sys.executable,
                                  environment={"ANSIBLE_STRATEGY": strategy})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert output.exists()
        output.unlink()


def test_strategy_rejects_padded_legacy_check_mode_omit(tmp_path):
    from ansiroll.loader import _core_version

    if _core_version() >= (2, 19):
        pytest.skip("requires the legacy omit token")
    playbook = """- hosts: localhost
  gather_facts: false
  tasks:
    - file:
        path: "{{ playbook_dir }}/result"
        state: touch
      check_mode: " {{ omit }} "
"""
    opts = ["-c", "local", "-e", "ansible_python_interpreter=" + sys.executable]
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        actual = _run_strategy_play(tmp_path, playbook, *opts, environment={"ANSIBLE_STRATEGY": strategy})
        assert actual.returncode != 0, actual.stdout + actual.stderr
        assert not (tmp_path / "result").exists()


def test_compiled_check_mode_omit_fallback_does_not_capture_parent_lookup(tmp_path, monkeypatch):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  tasks:
    - block:
        - command: /bin/true
          check_mode: "{{ false }}"
      check_mode: "{{ lookup('env', 'ANSIROLL_UNUSED_PARENT_MODE') }}"
""")
    monkeypatch.setenv("ANSIROLL_UNUSED_PARENT_MODE", "synthetic-unneeded-mode")
    source, _ = compile_playbook(str(playbook))
    assert "synthetic-unneeded-mode" not in json.dumps(_compiled_plays(source, tmp_path))


def test_recursive_include_preserves_its_inherited_check_mode(tmp_path):
    (tmp_path / "child.yml").write_text("""- file:
    path: "{{ playbook_dir }}/result-{{ depth | default(0) }}"
    state: touch
  check_mode: "{{ omit }}"
- set_fact:
    depth: "{{ (depth | default(0) | int) + 1 }}"
- block:
    - include_tasks: child.yml
      when: depth | int < 2
  check_mode: false
""")
    playbook = """- hosts: localhost
  gather_facts: false
  tasks:
    - include_tasks: child.yml
"""
    opts = ["--check", "-c", "local", "-e", "ansible_python_interpreter=" + sys.executable]
    expected = None
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        proc = _run_strategy_play(tmp_path, playbook, *opts, environment={"ANSIBLE_STRATEGY": strategy})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        files = sorted(tmp_path.glob("result-*"))
        written = [file.name for file in files]
        if expected is None:
            expected = written
        else:
            assert written == expected
        for file in files:
            file.unlink()


def test_standalone_recursive_include_preserves_null_check_mode(tmp_path):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("requires Core's native check mode inheritance")
    child = tmp_path / "child.yml"
    child.write_text("""- file:
    path: "{{ playbook_dir }}/result-{{ depth | default(0) }}"
    state: touch
  check_mode: "{{ omit }}"
- set_fact:
    depth: "{{ (depth | default(0) | int) + 1 }}"
- block:
    - include_tasks: child.yml
      when: depth | int < 2
  check_mode: null
""")
    playbook = """- hosts: localhost
  gather_facts: false
  tasks:
    - include_tasks: child.yml
"""
    native = _run_strategy_play(tmp_path, playbook, "--check", "-c", "local",
                                "-e", "ansible_python_interpreter=" + sys.executable,
                                environment={"ANSIBLE_STRATEGY": "linear"})
    assert native.returncode == 0, native.stdout + native.stderr
    assert sorted(file.name for file in tmp_path.glob("result-*")) == ["result-1"]
    (tmp_path / "result-1").unlink()
    artifact = tmp_path / "compiled.py"
    source, _ = compile_playbook(str(tmp_path / "play.yml"), python_interpreter=sys.executable)
    artifact.write_text(source)
    run = """import runpy, sys
compiled = runpy.run_path(sys.argv[1])
sys.exit(compiled['main'](
    compiled['PLAYS'], compiled['MODULES'], compiled['ANSIBALLZ_CODE'], check_mode=True,
    module_bundle=compiled['_ansiroll_import_bytes'], resource_reader=compiled['_ansiroll_resource_read'],
))
"""
    actual = subprocess.run([sys.executable, "-c", run, str(artifact)], cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    assert actual.returncode == 0, actual.stdout + actual.stderr
    assert sorted(file.name for file in tmp_path.glob("result-*")) == ["result-1"]


def test_strategy_captures_env_from_host_selected_vars_file(tmp_path):
    (tmp_path / "settings-localhost.yml").write_text("""\
value: >-
  {{ query('env', 'ANSIROLL_TEST_HOST_SELECTED_ENV', 'ANSIROLL_TEST_HOST_SELECTED_SECOND',
           'ANSIROLL_TEST_HOST_SELECTED_MISSING',
           default={'value': "{{ lookup('env', 'ANSIROLL_TEST_HOST_SELECTED_DEFAULT') }}"}) }}
prefix: "{{ lookup('env', 'ANSIROLL_TEST_HOST_SELECTED_PREFIX', default='wrong') }}"
fact_args:
  "{{ lookup('env', 'ANSIROLL_TEST_HOST_SELECTED_FACT') }}": hello
""")
    (tmp_path / "settings-other.yml").write_text(
        'value: "{{ q(\'env\', \'ANSIROLL_TEST_OTHER_HOST_ENV\') }}"\n')
    (tmp_path / "chosen-right.yml").write_text("prefix: overridden\nchoice: right\n")
    (tmp_path / "chosen-wrong.yml").write_text("prefix: overridden\nchoice: wrong\n")
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: false
  connection: local
  vars_files: ['settings-{{ inventory_hostname }}.yml', 'chosen-{{ prefix }}.yml']
  tasks:
    - set_fact: {}
      args: '{{ fact_args }}'
    - assert:
        that:
          - "value == ['host-selected', 'second', {'value': 'fallback'}]"
          - choice == 'right'
          - prefix == 'overridden'
          - greeting == 'hello'
          - "'ANSIROLL_TEST_OTHER_HOST_ENV' not in __ansiroll_controller_env"
""",
        environment={
            "ANSIROLL_TEST_HOST_SELECTED_ENV": "host-selected",
            "ANSIROLL_TEST_HOST_SELECTED_SECOND": "second",
            "ANSIROLL_TEST_HOST_SELECTED_DEFAULT": "fallback",
            "ANSIROLL_TEST_HOST_SELECTED_FACT": "greeting",
            "ANSIROLL_TEST_HOST_SELECTED_PREFIX": "right",
            "ANSIROLL_TEST_OTHER_HOST_ENV": "other",
        },
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


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


@pytest.mark.parametrize("no_log", [False, True])
def test_strategy_preserves_rescued_failure_facts_in_later_plays(tmp_path, no_log):
    failure = "rescued-failure-marker"
    result = _run_strategy_play(tmp_path, """- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    reason: first-play
  tasks:
    - block:
        - name: Original failure in {{ reason }}
          fail:
            msg: FAILURE_MARKER
          no_log: NO_LOG
      rescue:
        - debug: {msg: rescued}
- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    reason: later-play
  tasks:
    - assert:
        that:
          - ansible_failed_result.msg == 'FAILURE_MARKER'
          - ansible_failed_task.name == 'Original failure in first-play'
""".replace("NO_LOG", str(no_log).lower()).replace("FAILURE_MARKER", failure))
    assert result.returncode == 0, result.stdout + result.stderr
    if no_log:
        assert failure not in result.stdout + result.stderr


def test_rescued_loop_failure_retains_task_arguments(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  tasks:
    - block:
        - debug:
            msg: "{{ item }}"
          loop: [failed-first, succeeded-last]
          failed_when: item == 'failed-first'
      rescue:
        - debug: {msg: rescued}
""")
    source, _ = compile_playbook(str(playbook))
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"})
    dispatches = []
    runtime.on_rescue = lambda task, result, args: dispatches.append(args)
    runtime.run_play(_compiled_plays(source, tmp_path)[0])
    assert not runtime.failed
    assert runtime.vars["ansible_failed_task"]["args"]["msg"] == "{{ item }}"
    assert dispatches == [None]


def test_strategy_retains_rescued_assert_conditions_from_dynamic_args(tmp_path):
    proc = _run_strategy_play(tmp_path, """- hosts: localhost
  connection: local
  gather_facts: false
  vars:
    checks:
      that: ['1 == 2']
  tasks:
    - block:
        - assert: "{{ checks }}"
      rescue:
        - debug: {msg: rescued}
- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - assert:
        that: ansible_failed_task.args.that == ['1 == 2']
""")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_strategy_preserves_finalized_set_fact_args_after_rescue(tmp_path):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 21):
        pytest.skip("checks native finalized failed-task arguments on Core 2.21")
    playbook = """- hosts: localhost
  gather_facts: false
  vars: {source_value: finalized}
  tasks:
    - block:
        - set_fact:
            bad-key: '{{ source_value }}'
      rescue:
        - debug: {msg: rescued}
- hosts: localhost
  gather_facts: false
  vars: {source_value: changed}
  tasks:
    - assert:
        that: ansible_failed_task.args['bad-key'] == 'finalized'
"""
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        result = _run_strategy_play(tmp_path, playbook, environment={"ANSIBLE_STRATEGY": strategy})
        assert result.returncode == 0, result.stdout + result.stderr


def test_strategy_preserves_undefined_rescued_arguments_in_later_agents(tmp_path):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("uses modern Core undefined argument markers")
    result = _run_strategy_play(tmp_path, """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - block:
        - debug:
            msg: "{{ missing_argument }}"
      rescue:
        - assert:
            that: ansible_failed_task.args.msg is undefined
- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - assert:
        that: ansible_failed_task.args.msg is undefined
""")
    assert result.returncode == 0, result.stdout + result.stderr


def test_strategy_preserves_captured_argument_errors_in_later_agents(tmp_path):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("uses modern Core captured argument errors")
    result = _run_strategy_play(tmp_path, """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - block:
        - debug:
            msg: "{{ 1 / 0 }}"
      rescue:
        - debug: {msg: rescued}
- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - block:
        - debug:
            msg: "{{ ansible_failed_task.args.msg }}"
        - fail: {msg: Expected a captured argument error}
      rescue:
        - assert:
            that: "'division by zero' in ansible_failed_result.msg"
""")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(("looped", "no_log"), [(False, True), (True, True), (True, False)])
def test_strategy_preserves_only_public_rescue_overrides_on_failure(tmp_path, looped, no_log):
    playbook = """- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - block:
        - fail: {msg: saved-failure}
          no_log: true
      rescue:
        - set_fact:
            ansible_failed_result: {msg: override}
          no_log: CENSORED
          ignore_errors: true
          failed_when: FAILURE_TEST
LOOP
- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - assert:
        that: ansible_failed_result.msg == 'EXPECTED'
"""
    result = _run_strategy_play(tmp_path, playbook.replace(
        "FAILURE_TEST", "item == 1" if looped else "true"
    ).replace("LOOP", "          loop: [0, 1]" if looped else "").replace(
        "CENSORED", json.dumps(no_log)).replace("EXPECTED", "saved-failure" if no_log else "override"))
    assert result.returncode == 0, result.stdout + result.stderr
    if no_log:
        assert "override" not in result.stdout + result.stderr


@pytest.mark.parametrize("final", ["public-override", "hidden-override"])
def test_strategy_commits_final_rescue_write_with_per_item_no_log(tmp_path, final):
    first = "hidden-override" if final == "public-override" else "public-override"
    playbook = """- hosts: origin
  connection: local
  gather_facts: false
  tasks:
    - block:
        - fail: {msg: saved-failure}
          no_log: true
      rescue:
        - set_fact:
            ansible_failed_result: {msg: '{{ item }}'}
          no_log: "{{ item == 'hidden-override' }}"
          loop: [FIRST, FINAL]
- hosts: origin
  gather_facts: false
  tasks:
    - assert:
        that: ansible_failed_result.msg == 'FINAL'
      no_log: true
- hosts: recipient
  gather_facts: false
  tasks:
    - assert:
        that: EXPECTED_PUBLIC
"""
    expected_public = ("hostvars.origin.ansible_failed_result is undefined" if final == "hidden-override"
                       else "hostvars.origin.ansible_failed_result.msg == 'public-override'")
    result = _run_strategy_play(tmp_path, playbook.replace("FIRST", first).replace("FINAL", final)
                                .replace("EXPECTED_PUBLIC", expected_public), inventory="origin,recipient,")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hidden-override" not in result.stdout + result.stderr


def test_strategy_does_not_publish_censored_fact_under_an_untracked_rescue_name(tmp_path):
    proc = _run_strategy_play(tmp_path, """- hosts: origin
  gather_facts: false
  tasks:
    - set_fact:
        ansible_failed_result: {msg: untracked-private-value}
      no_log: true
- hosts: recipient
  gather_facts: false
  tasks:
    - assert:
        that: hostvars.origin.ansible_failed_result is undefined
""", inventory="origin,recipient,")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "untracked-private-value" not in proc.stdout + proc.stderr


def test_strategy_hostvars_preserve_extra_vars_shadowing_rescue_facts(tmp_path):
    inventory = tmp_path / "inventory.yml"
    inventory.write_text("""all:
  hosts:
    origin:
      failure_alias: "{{ ansible_failed_result.msg }}"
    recipient:
""")
    proc = _run_strategy_play(tmp_path, """- hosts: origin
  gather_facts: false
  tasks:
    - block:
        - fail: {msg: private-rescue-value}
          no_log: true
      rescue:
        - debug: {msg: rescued}
- hosts: recipient
  gather_facts: false
  tasks:
    - assert:
        that:
          - hostvars.origin.ansible_failed_result.msg == 'explicit-value'
          - hostvars.origin.failure_alias == 'explicit-value'
""", "-e", json.dumps({"ansible_failed_result": {"msg": "explicit-value"}}), inventory=str(inventory))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "private-rescue-value" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("write", ["inventory", "set_fact", "register", "replace-rescue"])
def test_strategy_keeps_ordinary_rescue_names_in_hostvars(tmp_path, write):
    inventory = tmp_path / "inventory.yml"
    inventory.write_text("""all:
  hosts:
    origin:
      ansible_failed_result: {msg: inventory-value}
    recipient:
""")
    task = {
        "inventory": "",
        "set_fact": "    - set_fact: {ansible_failed_result: {msg: ordinary-value}}\n",
        "register": "    - debug: {msg: ordinary-value}\n      register: ansible_failed_result\n",
        "replace-rescue": """    - block:
        - fail: {msg: private}
          no_log: true
      rescue:
        - debug: {msg: recovered}
    - set_fact: {ansible_failed_result: {msg: ordinary-value}}
""",
    }[write]
    first_play = """- hosts: origin
  connection: local
  gather_facts: false
  tasks:
""" + task if task else ""
    expected = "inventory-value" if write == "inventory" else "ordinary-value"
    result = _run_strategy_play(tmp_path, first_play + f"""- hosts: recipient
  connection: local
  gather_facts: false
  tasks:
    - assert:
        that: hostvars.origin.ansible_failed_result.msg == '{expected}'
""", inventory=str(inventory))
    assert result.returncode == 0, result.stdout + result.stderr


def test_strategy_transfers_rescue_state_only_to_originating_host(tmp_path):
    inventory = tmp_path / "inventory.yml"
    inventory.write_text("""all:
  hosts:
    origin:
      rescue_summary: "{{ ansible_failed_result.msg | default('redacted') }}"
      rescue_summary_via_vars: "{{ vars.ansible_failed_result.msg | default('redacted') }}"
    recipient:
""")
    result = _run_strategy_play(tmp_path, """- hosts: origin
  connection: local
  gather_facts: false
  vars:
    reason: private-original-marker
  tasks:
    - block:
        - fail:
            msg: "{{ reason }}"
          no_log: true
      rescue:
        - set_fact:
            ansible_failed_result: {msg: private-replacement-marker}
          no_log: true
- hosts: recipient
  connection: local
  gather_facts: false
  tasks:
    - assert:
        that:
          - hostvars.origin.ansible_failed_task is undefined
          - hostvars.origin.ansible_failed_result is undefined
          - hostvars.origin.rescue_summary | default('redacted') == 'redacted'
          - hostvars.origin.rescue_summary_via_vars | default('redacted') == 'redacted'
- hosts: all
  connection: local
  gather_facts: false
  vars:
    reason: changed
  tasks:
    - assert:
        that:
          - ansible_failed_result.msg == 'private-replacement-marker'
          - ansible_failed_task.args.msg == 'private-original-marker'
      when: inventory_hostname == 'origin'
      no_log: true
""", inventory=str(inventory))
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "private-original-marker" not in output
    assert "private-replacement-marker" not in output


@pytest.mark.parametrize("later_hosts", ["origin", "all"], ids=["later-agent", "later-linear"])
def test_strategy_preserves_native_rescued_result_values(tmp_path, later_hosts):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("uses native datetime task results")
    result = _run_strategy_play(tmp_path, """- hosts: origin
  connection: local
  gather_facts: false
  tasks:
    - block:
        - debug:
            msg: "{{ now() }}"
          failed_when: true
          no_log: true
      rescue:
        - debug: {msg: rescued}
- hosts: LATER_HOSTS
  connection: local
  gather_facts: false
  tasks:
    - assert:
        that: ansible_failed_result.msg.year == now().year
      when: inventory_hostname == 'origin'
""".replace("LATER_HOSTS", later_hosts), inventory="origin,recipient,")
    assert result.returncode == 0, result.stdout + result.stderr


def test_strategy_preserves_legacy_native_ranges_in_rescue(tmp_path):
    from ansiroll.loader import _core_version

    if not (2, 12) <= _core_version() < (2, 19):
        pytest.skip("checks legacy native range values")
    result = _run_strategy_play(tmp_path, """- hosts: localhost
  gather_facts: false
  tasks:
    - block:
        - fail: {msg: '{{ range(3) }}'}
          no_log: true
      rescue:
        - assert:
            that: ansible_failed_result.msg | list == [0, 1, 2]
- hosts: localhost
  gather_facts: false
  tasks:
    - assert:
        that:
          - ansible_failed_result.msg | list == [0, 1, 2]
          - ansible_failed_task.args.msg | list == [0, 1, 2]
""", environment={"ANSIBLE_JINJA2_NATIVE": "true"})
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("runner", ["standalone", "strategy", "linear"])
def test_expanded_include_applies_task_keywords(tmp_path, runner):
    (tmp_path / "sample.j2").write_text("template diff\n")
    (tmp_path / "child.yml").write_text("""
- debug: {msg: private_debug_message}
- name: Write dry-run file
  register: dry_result
  copy:
    content: secret-dry-run
    dest: "{{ playbook_dir }}/dry.txt"
- name: Write with explicit overrides
  register: real_result
  check_mode: false
  diff: true
  no_log: false
  copy:
    content: visible-diff
    dest: "{{ playbook_dir }}/real.txt"
- name: Delegate template with diff
  register: template_result
  delegate_to: localhost
  check_mode: false
  diff: true
  no_log: false
  template:
    src: sample.j2
    dest: "{{ playbook_dir }}/template.txt"
""")
    playbook = """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - include_tasks:
        file: child.yml
        apply:
          check_mode: true
          diff: false
          no_log: true
    - assert:
        that:
          - not (dry_result.diff | default([]))
          - real_result.diff | length > 0
          - template_result.diff | length > 0
"""
    if runner == "standalone":
        play = tmp_path / "play.yml"
        play.write_text(playbook)
        script = tmp_path / "compiled.py"
        script.write_text(compile_playbook(str(play))[0])
        proc = subprocess.run([sys.executable, str(script)], cwd=tmp_path,
                              text=True, capture_output=True)
    else:
        strategy = "linear" if runner == "linear" else "ansiroll.core.ansiroll"
        proc = _run_strategy_play(tmp_path, playbook, "--diff", "-e",
                                  "ansible_python_interpreter=" + sys.executable,
                                  environment={"ANSIBLE_STRATEGY": strategy})

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (tmp_path / "dry.txt").exists()
    assert (tmp_path / "real.txt").read_text() == "visible-diff"
    assert "private_debug_message" not in proc.stdout
    if runner != "standalone":
        assert "+visible-diff" in proc.stdout
        assert "+template diff" in proc.stdout


@pytest.mark.parametrize("looped", [False, True], ids=["single", "looped"])
def test_expanded_include_resolves_templated_apply_flags_in_native_scope(tmp_path, looped):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("modern Core resolves apply arguments at inclusion")
    (tmp_path / "child.yml").write_text("""- name: Child write
  vars: {safe: false}
  copy:
    content: child-content
    dest: '{{ playbook_dir }}/{{ "dry" if item else "real" }}.txt'
- debug:
    msg: '{{ "hidden-apply-marker" if item else "visible-apply-marker" }}'
  vars: {safe: false}
- name: Explicit overrides
  check_mode: false
  no_log: false
  copy:
    content: override-content
    dest: '{{ playbook_dir }}/override.txt'
""")
    playbook = """- hosts: localhost
  gather_facts: false
  connection: local
  vars: {safe: true}
  tasks:
    - include_tasks:
        file: child.yml
        apply:
          check_mode: '{{ item and safe }}'
          no_log: '{{ item and safe }}'
      ITEM_SCOPE
""".replace("ITEM_SCOPE", "loop: [true, false]" if looped else "vars: {item: true}")
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        result = _run_strategy_play(tmp_path, playbook, environment={"ANSIBLE_STRATEGY": strategy})
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert (tmp_path / "dry.txt").exists() == looped, output
        assert (tmp_path / "override.txt").read_text() == "override-content"
        assert ("hidden-apply-marker" in output) == looped, output
        if looped:
            assert "visible-apply-marker" in output
            assert (tmp_path / "real.txt").read_text() == "child-content"
            (tmp_path / "dry.txt").unlink()
            (tmp_path / "real.txt").unlink()
        (tmp_path / "override.txt").unlink()


def test_expanded_nested_include_honors_inherited_run_once(tmp_path):
    (tmp_path / "outer.yml").write_text("- include_tasks: child.yml\n")
    (tmp_path / "child.yml").write_text("""- copy:
    content: selected
    dest: '{{ playbook_dir }}/{{ inventory_hostname }}.txt'
  run_once: false
""")
    playbook = """- hosts: all
  gather_facts: false
  connection: local
  tasks:
    - include_tasks:
        file: outer.yml
        apply: {run_once: true}
"""
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        result = _run_strategy_play(tmp_path, playbook, "-e", "ansible_python_interpreter=" + sys.executable,
                                    inventory="origin,recipient,",
                                    environment={"ANSIBLE_STRATEGY": strategy})
        assert result.returncode == 0, result.stdout + result.stderr
        assert (tmp_path / "origin.txt").read_text() == "selected"
        assert not (tmp_path / "recipient.txt").exists(), result.stdout + result.stderr
        (tmp_path / "origin.txt").unlink()


@pytest.mark.parametrize("omit_once", [False, True])
def test_recursive_include_rebinds_templated_apply_flags(tmp_path, monkeypatch, omit_once):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("modern Core resolves apply arguments at inclusion")
    applied = "{{ omit if (depth | default(0)) == 1 else safe }}" if omit_once else "{{ safe }}"
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {safe: true}
  tasks:
    - include_tasks:
        file: recursive.yml
        apply: {check_mode: APPLIED}
    - assert: {that: depth == 40}
""".replace("APPLIED", json.dumps(applied)))
    (tmp_path / "recursive.yml").write_text("""- command: step
- set_fact:
    depth: '{{ (depth | default(0) | int) + 1 }}'
    safe: '{{ not safe }}'
- include_tasks:
    file: recursive.yml
    apply: {check_mode: APPLIED}
  when: depth < 40
""".replace("APPLIED", json.dumps(applied)))
    source, manifest = compile_playbook(str(playbook))
    assert not manifest["fallback_tasks"]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"})
    modes = []
    monkeypatch.setattr(runtime, "_run_accelerated_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "run_module", lambda *args, **kwargs: modes.append(runtime.check_mode)
                        or {"changed": False})
    runtime.run_play(_compiled_plays(source, tmp_path)[0])
    assert not runtime.failed
    assert modes == [True, omit_once] + [True, False] * 19


@pytest.mark.parametrize("child_check", [False, True])
def test_expanded_include_omit_inherits_templated_block_mode_in_child_scope(tmp_path, child_check):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("modern Core drops omitted apply fields")
    (tmp_path / "child.yml").write_text("""- file:
    path: '{{ playbook_dir }}/marker'
    state: touch
  vars: {safe: CHILD_CHECK}
""".replace("CHILD_CHECK", json.dumps(child_check)))
    playbook = """- hosts: localhost
  connection: local
  gather_facts: false
  vars: {safe: OUTER_CHECK}
  tasks:
    - block:
        - include_tasks:
            file: child.yml
            apply: {check_mode: '{{ omit }}'}
      check_mode: '{{ safe }}'
""".replace("OUTER_CHECK", json.dumps(not child_check))
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        result = _run_strategy_play(tmp_path, playbook, "-e", "ansible_python_interpreter=" + sys.executable,
                                    environment={"ANSIBLE_STRATEGY": strategy})
        assert result.returncode == 0, result.stdout + result.stderr
        created = tmp_path / "marker"
        assert created.exists() is (not child_check), result.stdout + result.stderr
        if created.exists():
            created.unlink()


def test_expanded_include_honors_explicit_and_templated_false_become(tmp_path, monkeypatch):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {escalate: false}
  tasks:
    - include_tasks:
        file: child.yml
        apply: {become: true}
""")
    (tmp_path / "child.yml").write_text("""- command: first
  become: false
- command: second
  become: "{{ escalate }}"
""")
    source, _ = compile_playbook(str(playbook))
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"})
    recorded = []
    monkeypatch.setattr(runtime, "_run_accelerated_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "run_module",
                        lambda action, args, become=False: recorded.append((args["_raw_params"], become))
                        or {"changed": False})
    runtime.run_play(_compiled_plays(source, tmp_path)[0])
    assert not runtime.failed
    assert recorded == [("first", False), ("second", False)]


@pytest.mark.parametrize("runner", ["linear", "standalone", "strategy"])
def test_expanded_include_matches_apply_var_scope_and_resolves_no_log_per_item(tmp_path, runner):
    from ansiroll.loader import _core_version

    playbook = """- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - include_tasks:
        file: child.yml
        apply:
          vars:
            chosen: "{{ flavor | default('fallback') }}"
          no_log: "{{ false }}"
"""
    (tmp_path / "child.yml").write_text("""- debug:
    msg: "{{ chosen }}-visible-applied-keyword"
- debug:
    msg: "{{ chosen }}-{{ 'visible-marker' if item == 'show' else 'private-marker' }}"
  vars:
    flavor: child
  no_log: "{{ item == 'hide' }}"
  loop: [show, hide]
""")
    if runner == "standalone":
        path = tmp_path / "play.yml"
        path.write_text(playbook)
        artifact = tmp_path / "compiled.py"
        artifact.write_text(compile_playbook(str(path))[0])
        proc = subprocess.run([sys.executable, str(artifact)], cwd=tmp_path, text=True, capture_output=True)
    else:
        strategy = "linear" if runner == "linear" else "ansiroll.core.ansiroll"
        proc = _run_strategy_play(tmp_path, playbook, environment={"ANSIBLE_STRATEGY": strategy})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "fallback-visible-applied-keyword" in proc.stdout
    prefix = "child" if _core_version() < (2, 19) else "fallback"
    assert f"{prefix}-visible-marker" in proc.stdout
    assert "private-marker" not in proc.stdout + proc.stderr


def test_recursive_include_keeps_inherited_no_log(tmp_path):
    (tmp_path / "a.yml").write_text("""- set_fact:
    depth: "{{ depth | default(0) | int + 1 }}"
- include_tasks: b.yml
""")
    (tmp_path / "b.yml").write_text("""- debug:
    msg: "{{ 'initial-visible-marker' if depth | int == 1 else 'recursive-private-marker' }}"
- include_tasks:
    file: a.yml
    apply: {no_log: true}
  when: depth | int == 1
""")
    proc = _run_strategy_play(tmp_path, """- hosts: localhost
  connection: local
  gather_facts: false
  tasks:
    - include_tasks:
        file: a.yml
        apply: {no_log: false}
""")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "initial-visible-marker" in proc.stdout
    assert "recursive-private-marker" not in proc.stdout + proc.stderr


def test_expanded_include_censors_results_and_preserves_run_once_state(tmp_path):
    (tmp_path / "child.yml").write_text("""- set_fact:
    protected_value: hidden-fact-marker
    cacheable: true
  register: protected_result
  run_once: true
""")
    proc = _run_strategy_play(tmp_path, """- hosts: all
  connection: local
  gather_facts: false
  tasks:
    - include_tasks:
        file: child.yml
        apply: {no_log: true}
- hosts: all
  connection: local
  gather_facts: false
  tasks:
    - assert:
        quiet: true
        that:
          - protected_value.startswith('hidden-')
          - protected_result.ansible_facts.protected_value == protected_value
          - ansible_facts.protected_value == protected_value
""", inventory="origin,recipient,")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "hidden-fact-marker" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("mode", ["omit", "invalid"])
def test_expanded_include_resolves_execution_mode_before_dispatch(tmp_path, monkeypatch, mode):
    from ansiroll.loader import _core_version

    if mode == "omit" and _core_version() < (2, 19):
        pytest.skip("modern Core restores the parent keyword after omission")
    path = tmp_path / "play.yml"
    path.write_text("""- hosts: localhost
  gather_facts: false
  vars: {invalid: typo}
  tasks:
    - include_tasks:
        file: child.yml
        apply: {check_mode: true, diff: true}
""")
    (tmp_path / "child.yml").write_text(f"""- command: child
  check_mode: "{{{{ {mode} }}}}"
  diff: "{{{{ omit }}}}"
  failed_when: false
""")
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, check_mode=False)
    calls = []
    monkeypatch.setattr(runtime, "_run_accelerated_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "run_module",
                        lambda *args, **kwargs: calls.append((runtime.check_mode, runtime.diff_mode))
                        or {"changed": False})
    runtime.run_play(_compiled_plays(compile_playbook(str(path))[0], tmp_path)[0])
    assert runtime.failed == (mode == "invalid")
    assert calls == ([(True, True)] if mode == "omit" else [])


@pytest.mark.parametrize("diff", [False, True])
def test_expanded_include_only_reports_module_diffs_when_enabled(tmp_path, diff):
    library = tmp_path / "library"
    library.mkdir()
    (library / "diff_module.py").write_text("""
from ansible.module_utils.basic import AnsibleModule

module = AnsibleModule(argument_spec={})
module.exit_json(changed=True, diff={"before": "", "after": "unconditional-diff-marker\\n"})
""")
    (tmp_path / "child.yml").write_text("- diff_module:\n")
    play = """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - include_tasks:
        file: child.yml
        apply:
          diff: """ + json.dumps(diff)
    result = _run_strategy_play(tmp_path, play, "-e", "ansible_python_interpreter=" + sys.executable)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ("+unconditional-diff-marker" in result.stdout) == diff


def test_strategy_selects_vars_file_using_inventory_before_set_fact(tmp_path):
    group_vars = tmp_path / "group_vars"
    group_vars.mkdir()
    (group_vars / "all.yml").write_text("setup_profile: inventory\n")
    (tmp_path / "settings-inventory.yml").write_text("chosen: inventory\n")
    (tmp_path / "settings-host-fact.yml").write_text("chosen: host-fact\n")
    playbook = """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - set_fact:
        setup_profile: host-fact

- hosts: localhost
  gather_facts: true
  connection: local
  vars_files: ['settings-{{ setup_profile }}.yml']
  tasks:
    - assert:
        that:
          - chosen == 'inventory'
          - setup_profile == 'host-fact'
"""

    for strategy in ("linear", "ansiroll.core.ansiroll"):
        result = _run_strategy_play(tmp_path, playbook, "-e", "ansible_python_interpreter=" + sys.executable,
                                    environment={"ANSIBLE_STRATEGY": strategy})
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("no_log", [False, True])
@pytest.mark.parametrize("alias", [False, True], ids=["devnull", "devnull-symlink"])
def test_strategy_refreshes_vars_file_selection_after_gathering_facts(tmp_path, no_log, alias):
    empty_file = os.devnull
    if alias:
        empty_file = tmp_path / "empty.yml"
        empty_file.symlink_to(os.devnull)
    (tmp_path / "settings-before.yml").write_text("before_gathering: old\n")
    (tmp_path / ("settings-" + platform.system() + ".yml")).write_text(
        'after_gathering: "{{ lookup(\'env\', \'ANSIROLL_TEST_FACT_SELECTED_ENV\') }}"\n')
    playbook = """- hosts: localhost
  gather_facts: true
  no_log: NO_LOG
  connection: local
  vars_files:
    - DEVNULL
    - ['settings-{{ ansible_facts.get(''system'', ''missing'') }}.yml', 'settings-before.yml']
  tasks:
    - assert:
        that:
          - before_gathering is undefined
          - after_gathering == 'fact-selected'
""".replace("NO_LOG", str(no_log).lower()).replace("DEVNULL", json.dumps(str(empty_file)))
    proc = _run_strategy_play(
        tmp_path, playbook, "-e", "ansible_python_interpreter=" + sys.executable,
        environment={"ANSIROLL_TEST_FACT_SELECTED_ENV": "fact-selected"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_strategy_refreshes_cached_vars_file_with_playbook_relative_inventory_vars(tmp_path):
    nested = tmp_path / "nested"
    group_vars = nested / "group_vars"
    group_vars.mkdir(parents=True)
    (group_vars / "all.yml").write_text("profile: nested\n")
    (nested / f"settings-nested-{platform.system()}.yml").write_text("selected: nested\n")
    (nested / "play.yml").write_text("""- hosts: localhost
  gather_facts: false
  connection: local
  vars_files: ['settings-{{ profile }}-{{ ansible_facts.system }}.yml']
  tasks:
    - assert: {that: selected == 'nested'}
""")
    playbook = """- hosts: localhost
  gather_facts: true
  connection: local
  tasks: []
- import_playbook: nested/play.yml
"""
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        result = _run_strategy_play(
            tmp_path, playbook, "-e", "ansible_python_interpreter=" + sys.executable,
            environment={"ANSIBLE_STRATEGY": strategy, "ANSIBLE_PLAYBOOK_VARS_ROOT": "bottom"}, timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("invalid", ["fifo", "vault"])
def test_strategy_rejects_invalid_vars_file_selected_after_setup(tmp_path, invalid):
    from textwrap import indent

    from ansible.parsing.vault import VaultLib, VaultSecret

    path = tmp_path / ("settings-" + platform.system() + ".yml")
    if invalid == "fifo":
        os.mkfifo(path)
        expected = "not a regular file"
    else:
        key = VaultSecret(b"synthetic-test-password")
        encrypted = VaultLib([("default", key)]).encrypt(b"synthetic content", secret=key)
        path.write_text("selected: !vault |\n" + indent(encrypted.decode(), "  "))
        expected = "could not serialize selected vars file value"
    (tmp_path / "settings-before.yml").write_text("{}\n")
    result = _run_strategy_play(tmp_path, """- hosts: localhost
  gather_facts: true
  connection: local
  vars_files:
    - ['settings-{{ ansible_facts.get(''system'', ''missing'') }}.yml', 'settings-before.yml']
  tasks:
    - debug: {msg: "{{ selected | default('value-was-dropped') }}"}
""", "-e", "ansible_python_interpreter=" + sys.executable, timeout=45)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert expected in output, output
    assert "value-was-dropped" not in output


@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize("cached", [False, True])
def test_strategy_rejects_undefined_vars_file_after_successful_setup(tmp_path, fast, cached):
    gather = """- hosts: localhost
  gather_facts: true
  connection: local
  tasks:
    - debug: {msg: gathered}
"""
    verify = """- hosts: localhost
  gather_facts: GATHER
  connection: local
  vars_files: ['settings-{{ missing_file_selector }}.yml']
  tasks:
    - debug: {msg: should-not-run}
""".replace("GATHER", str(not cached).lower())
    playbook = (gather if cached else "") + verify
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        proc = _run_strategy_play(
            tmp_path, playbook, "-e", "ansible_python_interpreter=" + sys.executable,
            environment={"ANSIBLE_STRATEGY": strategy, "ANSIROLL_FAST_FACTS": str(int(fast))},
        )
        output = proc.stdout + proc.stderr
        assert proc.returncode != 0, output
        assert "an undefined variable was found" in output, output
        assert "should-not-run" not in output


def test_strategy_does_not_capture_lookup_source_from_untrusted_fact(tmp_path):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("requires Ansible template trust tags")
    (tmp_path / "settings-localhost.yml").write_text("replacement: safe\n")
    playbook = """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - set_fact:
        replacement: !unsafe "{{ lookup('env', 'ANSIROLL_UNTRUSTED_FACT_LOOKUP') }}"

- hosts: localhost
  gather_facts: true
  connection: local
  vars_files: ['settings-{{ inventory_hostname }}.yml']
  tasks:
    - assert:
        that:
          - "'ANSIROLL_UNTRUSTED_FACT_LOOKUP' in replacement"
          - "'ANSIROLL_UNTRUSTED_FACT_LOOKUP' not in __ansiroll_controller_env"
"""
    proc = _run_strategy_play(
        tmp_path, playbook, "-e", "ansible_python_interpreter=" + sys.executable,
        environment={"ANSIROLL_UNTRUSTED_FACT_LOOKUP": "controller-only test value"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


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


@pytest.mark.parametrize("strategy", ["linear", "ansiroll.core.ansiroll"])
def test_delegated_set_fact_normalizes_arguments_on_invoking_host(tmp_path, strategy):
    proc = _run_strategy_play(
        tmp_path, """- hosts: origin
  gather_facts: false
  connection: local
  vars:
    fact_name: rendered_fact
    templated_facts: {incoming: arrived, rendered_fact: replaced}
  tasks:
    - set_fact:
        "{{ fact_name }}": here
        'on': quoted
        ignored_fact: "{{ missing | default(omit) }}"
        cacheable: "false"
      args: "{{ templated_facts }}"
      delegate_to: recipient
      delegate_facts: false
    - set_fact:
        loop_value: "{{ ansible_facts.loop_value | default(0) | int + 1 }}"
      delegate_to: recipient
      loop: [first, second]
    - assert:
        that:
          - loop_value | int == 1
          - ansible_facts.loop_value is undefined
          - rendered_fact == 'here'
          - incoming == 'arrived'
          - vars['on'] == 'quoted'
          - ignored_fact is undefined
          - ansible_facts.rendered_fact is undefined
          - hostvars.recipient.rendered_fact is undefined
""", inventory="origin,recipient,", environment={"ANSIBLE_STRATEGY": strategy},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_expanded_set_fact_args_prevent_conflicting_freeze(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars:
    status: before
    inputs: {status: after}
  tasks:
    - set_fact: {}
      args: '{{ inputs }}'
      delegate_to: localhost
""")
    with pytest.raises(ValueError, match="cannot freeze 'status'"):
        compile_playbook(str(playbook), freeze_vars=["status"])


def test_explicit_raw_params_fact_allows_unrelated_freeze(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars:
    status: before
    _raw_params: before
    inputs: {status: after}
  tasks:
    - set_fact: {_raw_params: '{{ inputs }}'}
""")
    compile_playbook(str(playbook), freeze_vars=["status"])
    with pytest.raises(ValueError, match="cannot freeze '_raw_params'"):
        compile_playbook(str(playbook), freeze_vars=["_raw_params"])


@pytest.mark.parametrize("args_default", ["{}", "none"], ids=["empty-mapping", "null"])
def test_legacy_delegated_set_fact_preserves_explicit_and_nested_values(tmp_path, args_default):
    from ansiroll.loader import _core_version

    if _core_version() >= (2, 19):
        pytest.skip("checks legacy argument and omit semantics")
    playbook = """- hosts: origin
  gather_facts: false
  tasks:
    - set_fact:
        explicit_marker: applied
        deep: [[{field: '{{ omit }}'}]]
        direct: [{field: '{{ omit }}'}]
      args: '{{ optional_facts | default(ARGS_DEFAULT) }}'
      delegate_to: recipient
    - assert:
        that:
          - explicit_marker == 'applied'
          - deep[0][0].field is defined
          - direct[0].field is undefined
""".replace("ARGS_DEFAULT", args_default)
    native, agent = (
        _run_strategy_play(tmp_path, playbook, inventory="origin,recipient,",
                           environment={"ANSIBLE_STRATEGY": strategy})
        for strategy in ("linear", "ansiroll.core.ansiroll")
    )
    if args_default == "none" and native.returncode:
        # Some legacy Core releases reject a non-mapping args expansion.
        assert "invalid or malformed argument" in native.stdout
        assert "invalid or malformed argument" in agent.stdout
    else:
        assert native.returncode == 0, native.stdout + native.stderr
    assert agent.returncode == native.returncode, agent.stdout + agent.stderr


@pytest.mark.parametrize("payload, expression", [("scalar", "'scalar'"), ("{inner: keep}", "{'inner': 'keep'}")])
def test_delegated_set_fact_keeps_free_form_argument(tmp_path, payload, expression):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 21):
        pytest.skip("checks free-form argument processing on Core 2.21")
    playbook = """- hosts: origin
  gather_facts: false
  vars: {payload: %s, inner: untouched}
  tasks:
    - set_fact: '{{ payload }}'
      delegate_to: recipient
    - assert:
        that:
          - "vars['_raw_params'] == %s"
          - inner == 'untouched'
""" % (payload, expression)
    for strategy in ("linear", "ansiroll.core.ansiroll"):
        result = _run_strategy_play(tmp_path, playbook, inventory="origin,recipient,",
                                    environment={"ANSIBLE_STRATEGY": strategy})
        assert result.returncode == 0, result.stdout + result.stderr


def test_strategy_routes_delegated_set_fact_to_the_target_host(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: origin
  gather_facts: false
  connection: local
  tasks:
    - set_fact:
        routed_fact: one
        cacheable: true
      delegate_to: localhost
      delegate_facts: true
      no_log: true
      register: issued
    - set_fact:
        loop_fact: "{{ item.value }}"
      loop:
        - {host: localhost, value: local}
        - {host: recipient, value: remote}
      delegate_to: "{{ item.host }}"
      delegate_facts: true
    - set_fact:
        local_fact: stays_local
      delegate_to: localhost
      delegate_facts: false
    - assert:
        that:
          - routed_fact is undefined
          - loop_fact is undefined
          - issued.ansible_facts.routed_fact == 'one'
          - local_fact == 'stays_local'

- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - assert:
        that:
          - routed_fact == 'one'
          - ansible_facts.routed_fact == 'one'
          - loop_fact == 'local'
          - issued is undefined
          - local_fact is undefined

- hosts: recipient
  gather_facts: false
  connection: local
  tasks:
    - assert:
        that:
          - routed_fact is undefined
          - loop_fact == 'remote'
          - hostvars['localhost'].routed_fact == 'one'
          - hostvars['127.0.0.1'].routed_fact == 'one'
          - hostvars['::1'].routed_fact == 'one'
""",
        inventory="origin,recipient,",
        environment={"ANSIROLL_PROVENANCE": "1"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert '"reason": "delegated set_fact requires task-lockstep scheduling"' in proc.stdout


def test_strategy_routes_cross_host_facts_at_the_actual_task_boundary(tmp_path):
    proc = _run_strategy_play(
        tmp_path, """- hosts: all
  gather_facts: false
  connection: local
  tasks:
    - command: /bin/true
    - assert:
        that: seeded is undefined
    - set_fact:
        seeded: during_task
      when: inventory_hostname == 'alpha'
      delegate_to: beta
      delegate_facts: true
    - assert:
        that: seeded == 'during_task'
      when: inventory_hostname == 'beta'
""", "-e", json.dumps({"ansible_python_interpreter": sys.executable}), inventory="alpha,beta,",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("mixed", [False, True], ids=["all-local", "mixed"])
@pytest.mark.parametrize("retry", [False, True], ids=["plain", "retry"])
def test_strategy_routes_each_set_fact_loop_result_using_its_resolved_flags(tmp_path, mixed, retry):
    retry_options = ("      retries: 2\n      delay: 0\n"
                     "      until: entry.key != 'local_flag' or loop_result.attempts >= 2\n") if retry else ""
    proc = _run_strategy_play(
        tmp_path, """- hosts: origin
  gather_facts: false
  connection: local
  tasks:
    - set_fact:
        "{{ entry.key }}": "{{ entry.value }}"
        cacheable: true
      loop:
        - {key: marker, value: first, target: recipient, route: ROUTE}
        - {key: local_flag, value: second, target: recipient, route: false}
        - {key: local_omit, value: third, route: true}
      loop_control: {loop_var: entry}
      delegate_to: "{{ entry.target | default(omit) }}"
      delegate_facts: "{{ entry.route }}"
      register: loop_result
      no_log: true
RETRY_OPTIONS    - assert:
        that:
          - local_flag == 'second'
          - local_omit == 'third'
          - CACHED_FACT_CHECK
          - loop_result.results | length == 3
          - hostvars.recipient.local_flag is undefined
          - hostvars.recipient.local_omit is undefined
          - EXPECTED
- hosts: recipient
  gather_facts: false
  connection: local
  tasks:
    - assert: {that: RECIPIENT}
""".replace("RETRY_OPTIONS", retry_options).replace("CACHED_FACT_CHECK",
            "loop_result.results[1].attempts == 2" if retry else "ansible_facts.local_flag == 'second'"
        ).replace("ROUTE", "true" if mixed else "false").replace(
            "EXPECTED", "marker is undefined" if mixed else "marker == 'first'"
        ).replace("RECIPIENT", "marker == 'first'" if mixed else "marker is undefined"),
        inventory="origin,recipient,",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("templated_action", [False, True])
def test_strategy_routes_delegated_facts_in_a_dynamically_selected_role(tmp_path, templated_action):
    from ansiroll.loader import _core_version

    if templated_action and _core_version() < (2, 19):
        pytest.skip("modern Core supports templated task actions")
    tasks = tmp_path / "roles" / "route_sample" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text("""ACTION:
    "{{ item.key }}": "{{ item.value }}"
  delegate_to: recipient
  delegate_facts: "{{ item.route }}"
  loop:
    - {key: local_marker, value: local, route: false}
    - {key: remote_marker, value: remote, route: true}
  register: fact_results
  retries: 2
  delay: 0
  until: item.route is false or fact_results.attempts >= 2
""".replace("ACTION", '- action: "{{ fact_action }}"\n  args' if templated_action else "- set_fact"))
    proc = _run_strategy_play(tmp_path, """- hosts: origin
  gather_facts: false
  connection: local
  vars:
    selected_role: route_sample
    fact_action: set_fact
  tasks:
    - include_role:
        name: "{{ selected_role }}"
    - assert:
        that:
          - local_marker == 'local'
          - remote_marker is undefined
          - hostvars.recipient.local_marker is undefined
          - hostvars.recipient.remote_marker == 'remote'
          - fact_results.results[1].attempts == 2
""", inventory="origin,recipient,")
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("flag", ['"false"', '"{{ false }}"'])
def test_strategy_routes_literal_false_delegation_in_dynamic_roles(tmp_path, flag):
    tasks = tmp_path / "roles" / "route_sample" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "main.yml").write_text("""- set_fact:
    local_marker: "{{ item }}"
  delegate_to: recipient
  delegate_facts: FLAG
  loop: [first, second]
""".replace("FLAG", flag))
    proc = _run_strategy_play(tmp_path, """- hosts: origin
  gather_facts: false
  connection: local
  vars:
    selected_role: route_sample
  tasks:
    - include_role:
        name: "{{ selected_role }}"
    - assert:
        that:
          - local_marker == 'second'
          - hostvars.recipient.local_marker is undefined
""", inventory="origin,recipient,")
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("flag", ['"false"', '"{{ false }}"'])
def test_strategy_keeps_false_string_fact_on_origin_in_multihost_fallback(tmp_path, flag):
    proc = _run_strategy_play(tmp_path, """- hosts: all
  gather_facts: false
  connection: local
  tasks:
    - set_fact:
        local_marker: "{{ item }}"
      delegate_to: recipient
      delegate_facts: FLAG
      loop: [first, second]
      when: inventory_hostname == 'origin'
    - assert:
        that:
          - local_marker == 'second'
          - hostvars.recipient.local_marker is undefined
      when: inventory_hostname == 'origin'
""".replace("FLAG", flag), inventory="origin,recipient,")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_strategy_routes_templated_set_fact_after_a_skipped_item(tmp_path):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("modern Core supports templated task actions")
    proc = _run_strategy_play(tmp_path, """- hosts: origin
  gather_facts: false
  connection: local
  vars:
    fact_action: set_fact
  tasks:
    - action: "{{ fact_action }}"
      args:
        "{{ item.key }}": "{{ item.value }}"
      delegate_to: recipient
      delegate_facts: "{{ item.route }}"
      loop:
        - {key: skipped_marker, value: skipped, route: true, run: false}
        - {key: local_marker, value: local, route: false, run: true}
        - {key: remote_marker, value: remote, route: true, run: true}
      when: item.run
      register: fact_results
    - assert:
        that:
          - fact_results.results[0].skipped
          - local_marker == 'local'
          - remote_marker is undefined
          - hostvars.recipient.local_marker is undefined
          - hostvars.recipient.remote_marker == 'remote'
          - hostvars.recipient.skipped_marker is undefined
""", inventory="origin,recipient,")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_strategy_keeps_delegated_run_once_fact_writes_in_loop_order(tmp_path):
    proc = _run_strategy_play(
        tmp_path, """- hosts: all
  gather_facts: false
  connection: local
  tasks:
    - set_fact: {marker: "{{ item.value }}"}
      loop:
        - {value: batch_value, route: false}
        - {value: recipient_value, route: true}
      run_once: true
      delegate_to: recipient
      delegate_facts: "{{ item.route }}"
    - assert:
        that: marker == ('recipient_value' if inventory_hostname == 'recipient' else 'batch_value')
""", inventory="origin,recipient,",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_strategy_keeps_mixed_run_once_fact_writes_in_loop_order(tmp_path):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 21):
        pytest.skip("checks the unified fact layers used by Ansible 2.21")
    proc = _run_strategy_play(tmp_path, """- hosts: all
  gather_facts: false
  connection: local
  tasks:
    - action: "{{ item.action }}"
      args: "{{ item.args }}"
      loop:
        - action: set_fact
          args: {ansible_distribution: batch_value, cacheable: true}
          route: false
        - action: setup
          args: {gather_subset: [min], filter: [ansible_distribution]}
          route: true
      run_once: true
      delegate_to: recipient
      delegate_facts: "{{ item.route }}"
    - assert:
        that: (ansible_facts.distribution == 'batch_value') == (inventory_hostname == 'origin')
""", "-e", json.dumps({"ansible_python_interpreter": sys.executable}), inventory="origin,recipient,")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_strategy_keeps_registered_variables_in_mixed_run_once_loop_order(tmp_path):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 21):
        pytest.skip("requires the unified register variable layer")
    plugins = tmp_path / "action_plugins"
    plugins.mkdir()
    (plugins / "write_registered_value.py").write_text("""
from ansible.plugins.action import ActionBase

class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        self.register_host_variables({"marker": "registered"})
        return {"changed": False}
""")
    proc = _run_strategy_play(tmp_path, """- hosts: all
  gather_facts: false
  connection: local
  tasks:
    - action: "{{ item.action }}"
      args: "{{ item.args }}"
      loop:
        - {action: set_fact, args: {marker: earlier}}
        - {action: write_registered_value, args: {}}
      run_once: true
      delegate_to: recipient
      delegate_facts: "{{ inventory_hostname == 'recipient' }}"
    - assert:
        that: marker == 'registered'
""", inventory="origin,recipient,")
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_strategy_ignores_deferred_group_callbacks_in_later_handler_fact_routing(tmp_path):
    from ansiroll.loader import _core_version

    if not (2, 19) <= _core_version() < (2, 21):
        pytest.skip("checks legacy result publication for templated actions")
    proc = _run_strategy_play(tmp_path, """- hosts: origin
  gather_facts: false
  connection: local
  tasks:
    - set_fact: {scheduled_action: group_by}
    - debug: {msg: first}
      changed_when: true
      notify: route
    - meta: flush_handlers
    - set_fact: {scheduled_action: set_fact}
    - debug: {msg: second}
      changed_when: true
      notify: route
    - meta: flush_handlers
    - assert:
        that:
          - local_marker == 'first'
          - hostvars.recipient.local_marker is undefined
          - hostvars.recipient.remote_marker == 'second'
  handlers:
    - name: route
      action: "{{ scheduled_action }}"
      args: "{{ item[scheduled_action] }}"
      loop:
        - {group_by: {key: first}, set_fact: {local_marker: first}, route: false}
        - {group_by: {key: second}, set_fact: {remote_marker: second}, route: true}
      delegate_to: recipient
      delegate_facts: "{{ item.route }}"
""", inventory="origin,recipient,")
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize(("action", "looped"), [("debug", True), ("set_fact", True), ("debug", False)])
def test_strategy_discards_unpublished_legacy_fact_routes(monkeypatch, action, looped):
    from types import SimpleNamespace

    module = _load_strategy_module()
    strategy = object.__new__(module._SetFactLinearStrategy)
    host = SimpleNamespace(name="origin")
    task = SimpleNamespace(_uuid="loop", action=action)
    item = {"ansible_facts": {"marker": "value"}}
    result = SimpleNamespace(_result={"results": [item] if looped else None}, _host=host, _task=task,
                             is_failed=lambda: False, is_unreachable=lambda: False, is_skipped=lambda: False)
    strategy._fact_routes = {("origin", "loop"): ["recipient"]} if looped else {}
    strategy._legacy_fact_items = {}
    monkeypatch.setattr(module, "_UnifiedTaskResult", None)
    monkeypatch.setattr(module._LinearStrategy, "_process_pending_results", lambda *_: [result])

    strategy._record_legacy_routes(result)
    assert (id(item) in strategy._legacy_fact_items) == (action == "set_fact")
    assert not strategy._fact_routes
    assert strategy._process_pending_results(None) == [result]
    assert not strategy._legacy_fact_items


def test_strategy_rejects_delegated_facts_with_on_target_delegation(tmp_path):
    proc = _run_strategy_play(
        tmp_path, """- hosts: origin
  gather_facts: false
  connection: local
  vars:
    ansiroll_delegate_on_target: true
  tasks:
    - set_fact: {marker: value}
      delegate_to: localhost
      delegate_facts: true
""", inventory="origin,",
    )
    assert proc.returncode != 0
    assert "with ansiroll_delegate_on_target" in " ".join((proc.stdout + proc.stderr).split())


@pytest.mark.parametrize("on_target", [False, True])
def test_strategy_reports_unavailable_on_target_delegation_in_linear_fallback(tmp_path, on_target):
    proc = _run_strategy_play(
        tmp_path, """- name: Delegation by batch
  hosts: all
  serial: 2
  gather_facts: false
  connection: local
  tasks:
    - set_fact: {marker: skipped}
      when: false
      delegate_to: recipient
      delegate_facts: true
    - set_fact: {expected_marker: reached}
      when: inventory_hostname == 'origin'
      delegate_to: recipient
      delegate_facts: true
    - assert:
        that:
          - hostvars.recipient.marker is undefined
          - hostvars.recipient.expected_marker == 'reached'
""", "-e", json.dumps({"ansiroll_delegate_on_target": on_target}),
        inventory="origin,recipient,last,",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    warning = "ansiroll_delegate_on_target has no effect during core linear fallback"
    warnings = " ".join(proc.stderr.split())
    assert warnings.count(warning) == int(on_target)
    if on_target:
        assert "for play 'Delegation by batch' on batch hosts origin, recipient" in warnings
        assert "batch hosts last" not in warnings


def test_strategy_warns_for_each_unnamed_play_in_linear_fallback(tmp_path):
    play = """- hosts: all
  gather_facts: false
  connection: local
  vars:
    ansiroll_delegate_on_target: true
  tasks:
    - debug: {msg: controller only}
"""
    proc = _run_strategy_play(tmp_path, play * 2, inventory="origin,recipient,")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    warnings = " ".join(proc.stderr.split())
    assert warnings.count("ansiroll_delegate_on_target has no effect during core linear fallback") == 2
    locations = set(re.findall(r"play\.yml:(\d+),", "".join(proc.stderr.split())))
    assert {"1", str(len(play.splitlines()) + 1)} <= locations


def test_strategy_warns_for_each_import_of_a_play_in_linear_fallback(tmp_path):
    (tmp_path / "imported.yml").write_text("""- hosts: all
  gather_facts: false
  connection: local
  vars:
    ansiroll_delegate_on_target: true
  tasks:
    - debug: {msg: controller only}
""")
    proc = _run_strategy_play(
        tmp_path, "- import_playbook: imported.yml\n" * 2, inventory="origin,recipient,",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    warnings = " ".join(proc.stderr.split())
    assert warnings.count("ansiroll_delegate_on_target has no effect during core linear fallback") == 2
    assert "".join(proc.stderr.split()).count("imported.yml:1,") == 2


def test_strategy_skips_on_target_fact_delegation_when_condition_is_false(tmp_path):
    proc = _run_strategy_play(
        tmp_path, """- hosts: origin
  gather_facts: false
  connection: local
  vars:
    ansiroll_delegate_on_target: true
  tasks:
    - set_fact: {marker: value}
      when: false
      delegate_to: localhost
      delegate_facts: true
    - assert: {that: marker is undefined}
""", inventory="origin,",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("flag", ["false", "route_facts"])
def test_strategy_allows_on_target_delegation_when_fact_flag_is_false(tmp_path, flag):
    proc = _run_strategy_play(
        tmp_path, """- hosts: origin
  gather_facts: false
  connection: local
  vars:
    ansiroll_delegate_on_target: true
    route_facts: false
  tasks:
    - set_fact: {marker: value}
      delegate_to: "{{ missing_host }}"
      delegate_facts: "{{ FLAG }}"
    - assert: {that: "marker == 'value'"}
""".replace("FLAG", flag), inventory="origin,",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


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
    - name: delegated command
      command: 'true'
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


@pytest.mark.parametrize("tree_shake", [False, True])
@pytest.mark.parametrize("shared_parent", [False, True])
def test_standalone_selects_include_from_launch_selected_vars_file(tmp_path, monkeypatch, tree_shake, shared_parent):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: dev}
  vars_files: ['vars/profile-{{ profile }}.yml']
  tasks:
    - include_tasks: '{{ task_file }}'
""")
    vars_dir = tmp_path / "vars"
    vars_dir.mkdir()
    if shared_parent:
        (tmp_path / "shared").mkdir()
        (tmp_path / "shared" / "entry.yml").write_text("- include_tasks: '{{ nested_file }}'\n")
    for profile in ("dev", "prod"):
        directory = tmp_path / profile
        directory.mkdir()
        if shared_parent:
            variant = f"task_file: shared/entry.yml\nnested_file: ../{profile}/nested.yml\n"
        else:
            variant = f"task_file: {profile}/entry.yml\nnested_file: nested.yml\n"
            (directory / "entry.yml").write_text("- include_tasks: '{{ nested_file }}'\n")
        (vars_dir / f"profile-{profile}.yml").write_text(variant)
        (directory / "nested.yml").write_text(f"- debug: {{msg: selected-profile-{profile}}}\n")
    with (tmp_path / "prod" / "nested.yml").open("a") as handle:
        handle.write('''# $ANSIBLE_VAULT in a comment is ordinary text.
- debug: {msg: '{{ lookup("env", "ANSIROLL_ALT_INCLUDE_SECRET", default="unavailable") }}'}
''')
    monkeypatch.setenv("ANSIROLL_ALT_INCLUDE_SECRET", "synthetic-alt-include-secret")
    script = tmp_path / "compiled.py"
    source, _ = compile_playbook(str(playbook), tree_shake=tree_shake)
    assert "ANSIROLL_ALT_INCLUDE_SECRET" not in _compiled_plays(source, tmp_path)[0].get("controller_env", {})
    script.write_text(source)
    for path in tmp_path.glob("*/*.yml"):
        path.unlink()
    for extra, expected in (({}, "dev"), ({"profile": "prod"}, "prod")):
        result = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True, text=True,
                                env={**os.environ, "ANSIROLL_EXTRA_VARS": json.dumps(extra)}, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        assert f"selected-profile-{expected}" in result.stdout


@pytest.mark.parametrize("nested_role", [False, True])
def test_vars_file_alternate_include_does_not_embed_encrypted_task_bodies(tmp_path, nested_role):
    from ansible.parsing.vault import VaultLib, VaultSecret

    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normal}
  vars_files: ['profile-{{ profile }}.yml']
  tasks:
    - include_tasks: '{{ task_file }}'
""")
    secret = VaultSecret(b"password")
    vault = VaultLib([("default", secret)])
    password = tmp_path / "vault-password"
    password.write_text("password\n")
    (tmp_path / "profile-normal.yml").write_text("task_file: normal.yml\n")
    (tmp_path / "profile-private.yml").write_text("task_file: sealed.yml\n")
    (tmp_path / "normal.yml").write_text("- debug: {msg: ordinary-body}\n")
    encrypted = vault.encrypt(b"- debug: {msg: synthetic-private-body}\n", secret=secret)
    if nested_role:
        (tmp_path / "sealed.yml").write_text("- include_role: {name: sealed_role}\n")
        tasks = tmp_path / "roles" / "sealed_role" / "tasks"
        tasks.mkdir(parents=True)
        (tasks / "main.yml").write_bytes(encrypted)
    else:
        (tmp_path / "sealed.yml").write_bytes(encrypted)
    source, _ = compile_playbook(str(playbook), vault_password_file=str(password))
    plan = _compiled_plays(source, tmp_path)[0]
    assert "synthetic-private-body" not in json.dumps(plan)
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"})
    runtime.run_play(plan)
    assert not runtime.failed


@pytest.mark.parametrize("selection_error", ["missing-variant", "render-error"])
def test_vars_file_include_selection_failure_is_censored_and_rescued(tmp_path, selection_error):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normal}
  vars_files: ['profile-{{ profile }}.yml']
  tasks:
    - block:
        - include_tasks: '{{ task_file }}'
          no_log: true
      rescue:
        - debug: {msg: rescued-include-selection}
""")
    for profile in ("normal", "alternate"):
        (tmp_path / f"profile-{profile}.yml").write_text(f"task_file: {profile}.yml\n")
        (tmp_path / f"{profile}.yml").write_text(f"- debug: {{msg: {profile}}}\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    if selection_error == "render-error":
        node = next(node for node in _walk_play_nodes([plan]) if "include_selector" in node)
        node["include_selector"] = "{{ 1 // divisor }}{{ task_file }}"
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, on_result=frames.append,
                      extra_vars={"task_file": "synthetic-private-launch-value", "divisor": 0})
    runtime.run_play(plan)
    assert not runtime.failed
    assert "rescued-include-selection" in json.dumps(frames)
    assert "censored" in json.dumps(frames)
    assert "synthetic-private-launch-value" not in json.dumps(frames)


@pytest.mark.parametrize("invalid", ["body", "nul", "lookup"])
def test_vars_file_alternate_include_conversion_error_leaves_default_usable(tmp_path, monkeypatch, invalid):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normal}
  vars_files: ['profile-{{ profile }}.yml']
  tasks:
    - include_tasks: '{{ task_file }}'
""")
    (tmp_path / "profile-normal.yml").write_text("task_file: normal.yml\n")
    alternate = {
        "body": "task_file: alternate.yml\n",
        "nul": 'task_file: "invalid\\0.yml"\n',
        "lookup": """task_file: '{{ lookup("env", "ANSIROLL_ALT_FILENAME") }}'\n""",
    }
    monkeypatch.setenv("ANSIROLL_ALT_FILENAME", "synthetic-private-filename.yml")
    (tmp_path / "profile-alternate.yml").write_text(alternate[invalid])
    (tmp_path / "normal.yml").write_text("- debug: {msg: default-body}\n")
    (tmp_path / "alternate.yml").write_text(
        "- set_fact: {task_file: future.yml}\n- copy: {src: [invalid], dest: /unused}\n")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    node = next(node for node in _walk_play_nodes([plan]) if "include_selector" in node)
    assert [variant["include_variant"] for variant in node["tasks"]] == ["normal.yml"]

    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, on_result=frames.append)
    runtime.run_play(plan)
    assert not runtime.failed
    assert "default-body" in json.dumps(frames)
    assert "synthetic-private-filename.yml" not in json.dumps(plan)


def test_vars_file_include_combines_launch_mapping_and_checks_each_loop_item(tmp_path, monkeypatch):
    from ansible import constants as C

    monkeypatch.setenv("ANSIBLE_HASH_BEHAVIOUR", "merge")
    monkeypatch.setattr(C, "DEFAULT_HASH_BEHAVIOUR", "merge")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normal}
  vars_files: ['profile-{{ profile }}.yml']
  tasks:
    - include_tasks: '{{ cfg.task_file }}'
      loop: [false, true]
      when: item
""")
    for profile in ("normal", "alternate"):
        (tmp_path / f"profile-{profile}.yml").write_text(f"cfg: {{task_file: {profile}.yml}}\n")
        (tmp_path / f"{profile}.yml").write_text(f"- debug: {{msg: selected-{profile}}}\n")
    source, _ = compile_playbook(str(playbook))
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, on_result=frames.append,
                      extra_vars={"profile": "alternate", "cfg": {"unrelated": 1}})
    runtime.run_play(_compiled_plays(source, tmp_path)[0])
    assert not runtime.failed, json.dumps(frames)
    assert [frame["result"]["msg"] for frame in frames
            if frame.get("result", {}).get("msg", "").startswith("selected-")] == ["selected-alternate"]


@pytest.mark.parametrize("shared_parent", [False, True])
def test_vars_file_include_matches_native_nested_parent_path(tmp_path, shared_parent):
    playbook = """- hosts: localhost
  gather_facts: false
  vars: {profile: normal}
  vars_files: ['profile-{{ profile }}.yml']
  tasks:
    - include_tasks: '{{ task_file }}'
"""
    for profile in ("normal", "alternate"):
        directory = tmp_path / profile
        directory.mkdir()
        if shared_parent:
            (tmp_path / f"profile-{profile}.yml").write_text(
                f"task_file: shared/entry.yml\nchild_directory: {profile}\n")
            (directory / "child.yml").write_text(f"- debug: {{msg: {profile}-child}}\n")
        else:
            (tmp_path / f"profile-{profile}.yml").write_text(f"task_file: {profile}/entry.yml\n")
            (directory / "entry.yml").write_text("""- include_tasks: child.yml
  vars: {task_file: rebound/entry.yml}
""")
            (directory / "child.yml").write_text("- debug: {msg: original-child}\n")
    if shared_parent:
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "entry.yml").write_text("""- include_tasks: child.yml
  vars: {task_file: '{{ child_directory }}/entry.yml'}
""")
        (shared / "child.yml").write_text("- debug: {msg: original-child}\n")
    rebound = tmp_path / "rebound"
    rebound.mkdir()
    (rebound / "child.yml").write_text("- debug: {msg: rebound-child}\n")
    native = _run_strategy_play(tmp_path, playbook, "-e", "profile=alternate",
                                environment={"ANSIBLE_STRATEGY": "linear"})
    assert native.returncode == 0, native.stdout + native.stderr
    native_messages = [message for message in ("original-child", "rebound-child", "alternate-child")
                       if message in native.stdout]
    assert len(native_messages) == 1
    source, _ = compile_playbook(str(tmp_path / "play.yml"))
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, on_result=frames.append,
                      extra_vars={"profile": "alternate"})
    runtime.run_play(_compiled_plays(source, tmp_path)[0])
    assert not runtime.failed, json.dumps(frames)
    messages = [frame["result"]["msg"] for frame in frames if "msg" in frame.get("result", {})]
    assert messages == native_messages


@pytest.mark.parametrize("dependency", [False, True])
def test_vars_file_alternate_can_load_a_role_from_roles_path(tmp_path, monkeypatch, dependency):
    from ansible import constants as C

    role_tasks = tmp_path / "external" / "roles" / "path_role" / "tasks"
    role_tasks.mkdir(parents=True)
    (role_tasks / "main.yml").write_text("- debug: {msg: external-role-body}\n")
    if dependency:
        metadata = role_tasks.parent / "meta"
        metadata.mkdir()
        (metadata / "main.yml").write_text("dependencies: [sibling_role]\n")
        sibling = role_tasks.parents[1] / "sibling_role" / "tasks"
        sibling.mkdir(parents=True)
        (sibling / "main.yml").write_text("- debug: {msg: sibling-role-body}\n")
    monkeypatch.setattr(C, "DEFAULT_ROLES_PATH", [str(role_tasks.parents[1])])
    project = tmp_path / "project"
    project.mkdir()
    playbook = project / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normal}
  vars_files: ['profile-{{ profile }}.yml']
  tasks:
    - include_tasks: '{{ task_file }}'
""")
    for profile in ("normal", "alternate"):
        (project / f"profile-{profile}.yml").write_text(f"task_file: {profile}.yml\n")
    (project / "normal.yml").write_text("- debug: {msg: default-body}\n")
    (project / "alternate.yml").write_text("- include_role: {name: path_role}\n")
    source, _ = compile_playbook(str(playbook))
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, on_result=frames.append,
                      extra_vars={"profile": "alternate"})
    runtime.run_play(_compiled_plays(source, tmp_path)[0])
    assert not runtime.failed, json.dumps(frames)
    assert "external-role-body" in json.dumps(frames)
    assert ("sibling-role-body" in json.dumps(frames)) == dependency


def _run_vars_file_include_profile(plan, profile, **extra_vars):
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, on_result=frames.append,
                      extra_vars={"profile": profile, **extra_vars})
    runtime.run_play(plan)
    return runtime, [frame["result"]["msg"] for frame in frames if "msg" in frame.get("result", {})]


def test_vars_file_include_uses_controller_fallback_for_host_selection(tmp_path):
    (tmp_path / "profile-localhost.yml").write_text("task_file: selected.yml\n")
    (tmp_path / "default.yml").write_text("- fail: {msg: compile-default-chosen}\n")
    (tmp_path / "selected.yml").write_text("- debug: {msg: host-selected-include}\n")
    result = _run_strategy_play(tmp_path, """- hosts: localhost
  gather_facts: false
  connection: local
  vars: {task_file: default.yml}
  vars_files: ['profile-{{ inventory_hostname }}.yml']
  tasks:
    - include_tasks: '{{ task_file }}'
""")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "host-selected-include" in result.stdout


def test_vars_file_include_replays_inside_a_literal_include_role(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normal}
  vars_files: ['profile-{{ profile }}.yml']
  tasks:
    - include_role: {name: wrapper}
""")
    role_tasks = tmp_path / "roles" / "wrapper" / "tasks"
    role_tasks.mkdir(parents=True)
    (role_tasks / "main.yml").write_text("- include_tasks: '{{ task_file }}'\n")
    for profile in ("normal", "alternate"):
        (tmp_path / f"profile-{profile}.yml").write_text(f"task_file: {profile}.yml\n")
        (role_tasks / f"{profile}.yml").write_text(f"- debug: {{msg: chosen-{profile}}}\n")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    for profile in ("normal", "alternate"):
        runtime, messages = _run_vars_file_include_profile(plan, profile)
        assert not runtime.failed, messages
        assert messages == [f"chosen-{profile}"]


@pytest.mark.parametrize("child_action", [None, "import_tasks", "include_tasks"],
                         ids=["changed-entry", "nested-import", "nested-include"])
def test_vars_file_include_rejects_changed_inputs_for_build_only_body(tmp_path, child_action):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normal}
  vars_files: ['profile-{{ profile }}.yml']
  tasks:
    - include_tasks: '{{ [task_file] | path_join }}'
""")
    for profile in ("normal", "alternate"):
        entry = "entry.yml" if child_action else f"{profile}.yml"
        (tmp_path / f"profile-{profile}.yml").write_text(f"task_file: {entry}\nchild: {profile}.yml\n")
        (tmp_path / f"{profile}.yml").write_text(f"- debug: {{msg: chosen-{profile}}}\n")
    if child_action:
        (tmp_path / "entry.yml").write_text(f"- {child_action}: '{{{{ child }}}}'\n")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    runtime, messages = _run_vars_file_include_profile(plan, "normal")
    assert not runtime.failed, messages
    assert messages == ["chosen-normal"]

    runtime, messages = _run_vars_file_include_profile(plan, "alternate")
    assert runtime.failed
    assert messages == ["include_tasks selector inputs changed and no alternate task file was bundled"]


@pytest.mark.parametrize("child_action", ["include_tasks", "import_tasks"])
def test_vars_file_include_build_only_parent_ignores_shadowed_child_input(tmp_path, child_action):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normal}
  vars_files: ['profile-{{ profile }}.yml']
  tasks:
    - include_tasks: '{{ [task_file] | path_join }}'
""")
    for profile in ("normal", "alternate"):
        (tmp_path / f"profile-{profile}.yml").write_text(f"task_file: entry.yml\nchild: {profile}.yml\n")
    (tmp_path / "entry.yml").write_text(f"- {child_action}: '{{{{ child }}}}'\n  vars: {{child: fixed.yml}}\n")
    (tmp_path / "fixed.yml").write_text("- debug: {msg: fixed-child}\n")
    (tmp_path / "alternate.yml").write_text("- debug: {msg: alternate-child}\n")

    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    for profile in ("normal", "alternate"):
        runtime, messages = _run_vars_file_include_profile(plan, profile)
        assert not runtime.failed, messages
        assert messages == ["fixed-child"]

    runtime, messages = _run_vars_file_include_profile(plan, "alternate", child="alternate.yml")
    assert runtime.failed
    assert messages == ["include_tasks selector inputs changed and no alternate task file was bundled"]


@pytest.mark.parametrize("nested", [False, True], ids=["shared-entry", "different-children"])
def test_vars_file_include_keeps_compile_snapshot_for_shared_variant_key(tmp_path, nested):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normal}
  vars_files: ['profile-{{ profile }}.yml']
  tasks:
    - include_tasks: '{{ task_file }}'
""")
    (tmp_path / "profile-normal.yml").write_text("task_file: '{{ [\"entry.yml\"] | path_join }}'\nchild: normal.yml\n")
    (tmp_path / "profile-alternate.yml").write_text("task_file: entry.yml\nchild: alternate.yml\n")
    entry = "- include_tasks: '{{ child }}'\n" if nested else "- debug: {msg: shared-entry}\n"
    (tmp_path / "entry.yml").write_text(entry)
    for profile in ("normal", "alternate"):
        (tmp_path / f"{profile}.yml").write_text(f"- debug: {{msg: chosen-{profile}}}\n")

    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    for profile in ("normal", "alternate"):
        runtime, messages = _run_vars_file_include_profile(plan, profile)
        assert not runtime.failed, messages
        assert messages == [f"chosen-{profile}" if nested else "shared-entry"]


def test_vars_file_include_keeps_binding_when_optional_file_is_missing(tmp_path):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 15):
        pytest.skip("Ansible Core before 2.15 requires the vars file")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normal}
  vars_files:
    - 'profile-{{ profile }}.yml'
    - 'optional-{{ profile }}.yml'
  tasks:
    - include_tasks: '{{ task_file }}'
""")
    (tmp_path / "profile-normal.yml").write_text("task_file: unused.yml\n")
    (tmp_path / "profile-alternate.yml").write_text("task_file: keep.yml\n")
    (tmp_path / "optional-normal.yml").write_text("task_file: override.yml\n")
    for name in ("keep", "override"):
        (tmp_path / f"{name}.yml").write_text(f"- debug: {{msg: {name}}}\n")

    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    for profile, expected in (("normal", "override"), ("alternate", "keep")):
        runtime, messages = _run_vars_file_include_profile(plan, profile)
        assert not runtime.failed, messages
        assert messages == [expected]


@pytest.mark.parametrize("selector", ["mapping-member", "controller-default", "literal-wrapper", "many-profiles"])
def test_vars_file_include_retains_portable_choices(tmp_path, selector):
    instruction = "- include_tasks: '{{ cfg.task_file }}'\n" if selector == "mapping-member" else "- include_tasks: '{{ task_file }}'\n"
    if selector == "literal-wrapper":
        (tmp_path / "wrapper.yml").write_text(instruction)
        instruction = "- include_tasks: wrapper.yml\n"
    profiles = ["normal", "alternate"]
    if selector == "many-profiles":
        profiles.extend(f"other-{number}" for number in range(257))
    for number, profile in enumerate(profiles):
        filename = "normal.yml" if profile == "normal" else "alternate.yml"
        if selector == "mapping-member":
            values = f"""cfg:
  task_file: {filename}
  unused: '{{{{ ["other"] | path_join }}}}'
"""
        elif selector == "controller-default" and profile == "normal":
            values = """task_file: '{{ ["normal.yml"] | path_join }}'\n"""
        else:
            values = f"task_file: {filename}\nunrelated: {number}\n"
        (tmp_path / f"profile-{profile}.yml").write_text(values)
    for profile in ("normal", "alternate"):
        (tmp_path / f"{profile}.yml").write_text(f"- debug: {{msg: chosen-{profile}}}\n")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normal}
  vars_files: ['profile-{{ profile }}.yml']
  tasks:
""" + textwrap.indent(instruction, "    "))
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    for profile in ("normal", "alternate"):
        runtime, messages = _run_vars_file_include_profile(plan, profile)
        assert not runtime.failed, messages
        assert messages == [f"chosen-{profile}"]


@pytest.mark.parametrize("first_found", [False, True])
def test_vars_file_include_excludes_unreachable_bodies(tmp_path, first_found):
    extra = ""
    selection = "'{{ prefix }}-{{ suffix }}.yml'"
    files = """- 'prefix-{{ profile }}.yml'
    - 'suffix-{{ profile }}.yml'"""
    if first_found:
        extra = ", task_file: unreachable.yml"
        selection = "'{{ task_file }}'"
        files = """- ['profile-{{ profile | default("normal") }}.yml', fallback.yml]"""
        (tmp_path / "fallback.yml").write_text("task_file: normal-normal.yml\n")
    for profile in ("normal", "alternate"):
        (tmp_path / f"prefix-{profile}.yml").write_text(f"prefix: {profile}\n")
        (tmp_path / f"suffix-{profile}.yml").write_text(f"suffix: {profile}\n")
        (tmp_path / f"profile-{profile}.yml").write_text(f"task_file: {profile}-{profile}.yml\n")
        (tmp_path / f"{profile}-{profile}.yml").write_text(f"- debug: {{msg: chosen-{profile}}}\n")
    for filename in ("normal-alternate.yml", "alternate-normal.yml", "unreachable.yml"):
        (tmp_path / filename).write_text("- debug: {msg: unreachable-body-marker}\n")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normalEXTRA}
  vars_files:
    FILES
  tasks:
    - include_tasks: SELECTION
""".replace("EXTRA", extra).replace("FILES", files).replace("SELECTION", selection))
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    assert "unreachable-body-marker" not in json.dumps(plan)
    runtime, messages = _run_vars_file_include_profile(plan, "alternate")
    assert not runtime.failed, messages
    assert messages == ["chosen-alternate"]


@pytest.mark.parametrize("alias", ["vars", "ansible.legacy.vars", "ansible.builtin.vars"])
def test_vars_file_alternate_selector_rejects_shadowed_vars_lookup(tmp_path, monkeypatch, alias):
    from types import SimpleNamespace

    from ansible.plugins.loader import lookup_loader
    from ansible.plugins.lookup import vars as builtin_vars
    from ansiroll.loader import _core_version, _portable_include_scope

    plugin = tmp_path / "vars.py"
    resolved = SimpleNamespace(plugin_resolved_path=str(plugin))
    monkeypatch.setattr(lookup_loader, "find_plugin_with_context", lambda name: resolved)
    task = SimpleNamespace(get_search_path=lambda: [str(tmp_path)],
                           _loader=SimpleNamespace(get_basedir=lambda: str(tmp_path)))
    source = ["{{ filename }}"]
    filename = "{{ lookup('" + alias + "', 'child') }}"
    if _core_version() >= (2, 19):
        from ansible.template import trust_as_template

        filename = trust_as_template(filename)
    variables = {"filename": filename, "child": "child.yml"}
    assert _portable_include_scope(task, source, variables) is None

    resolved.plugin_resolved_path = builtin_vars.__file__
    assert _portable_include_scope(task, source, variables) == variables
    directory = tmp_path / "lookup_plugins"
    directory.mkdir()
    (directory / "vars.py").touch()
    result = _portable_include_scope(task, source, variables)
    assert (result is not None) is (alias == "ansible.builtin.vars")


@pytest.mark.parametrize(("kind", "expression"), [("filter", "child | lower"), ("test", "child is string")])
@pytest.mark.parametrize("location", ["playbook", "configured"])
def test_vars_file_alternate_selector_rejects_controller_jinja_plugins(tmp_path, monkeypatch, kind, expression, location):
    from types import SimpleNamespace

    from ansible.plugins.loader import filter_loader, test_loader
    from ansiroll.loader import _core_version, _portable_include_scope

    loader = filter_loader if kind == "filter" else test_loader
    plugin_dir = tmp_path / (kind + "_plugins" if location == "playbook" else "configured")
    paths = [SimpleNamespace(path=str(plugin_dir), internal=False)] if location == "configured" else []
    monkeypatch.setattr(loader, "_get_paths_with_context", lambda: paths)
    task = SimpleNamespace(get_search_path=lambda: [str(tmp_path)],
                           _loader=SimpleNamespace(get_basedir=lambda: str(tmp_path)))
    filename = "{{ " + expression + " }}"
    if _core_version() >= (2, 19):
        from ansible.template import trust_as_template

        filename = trust_as_template(filename)
    variables = {"filename": filename, "child": "child.yml"}
    assert _portable_include_scope(task, ["{{ filename }}"], variables) == variables
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").touch()
    assert _portable_include_scope(task, ["{{ filename }}"], variables) is None


@pytest.mark.parametrize("controller_lookup", [False, True])
def test_vars_file_alternate_static_import_uses_safe_candidate_scope(tmp_path, monkeypatch, controller_lookup):
    for profile in ("normal", "alternate"):
        directory = tmp_path / profile
        directory.mkdir()
        (tmp_path / f"profile-{profile}.yml").write_text(f"task_file: {profile}/entry.yml\n")
        child = "child.yml"
        if controller_lookup and profile == "alternate":
            monkeypatch.setenv("ANSIROLL_ALT_IMPORT", "child.yml")
            child = '{{ lookup("env", "ANSIROLL_ALT_IMPORT") }}'
        (directory / "entry.yml").write_text(f"- import_tasks: '{child}'\n")
        (directory / "child.yml").write_text(f"- debug: {{msg: imported-{profile}}}\n")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normal}
  vars_files: ['profile-{{ profile }}.yml']
  tasks:
    - include_tasks: '{{ task_file }}'
""")
    source, _ = compile_playbook(str(playbook))
    plan = _compiled_plays(source, tmp_path)[0]
    runtime, messages = _run_vars_file_include_profile(plan, "normal")
    assert not runtime.failed, messages
    assert messages == ["imported-normal"]
    if controller_lookup:
        assert "imported-alternate" not in json.dumps(plan)
    else:
        runtime, messages = _run_vars_file_include_profile(plan, "alternate")
        assert not runtime.failed, messages
        assert messages == ["imported-alternate"]


@pytest.mark.parametrize("shared_entry", [True, False])
@pytest.mark.parametrize("alias, unsafe", [
    ("{{ child }}", False), ("{{ child_alias }}", False), ("{{ cfg.child }}", False), ("{{ child }}", True),
])
def test_vars_file_static_import_checks_child_binding(tmp_path, alias, unsafe, shared_entry, monkeypatch):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normal, child_alias: '{{ child }}'}
  vars_files: ['profile-{{ profile }}.yml']
  tasks:
    - include_tasks: '{{ task_file }}'
""")
    for profile in ("normal", "alternate"):
        child = "{{ child }}" if profile == "alternate" else "normal.yml"
        tag = "!unsafe " if unsafe and profile == "alternate" else ""
        entry = "entry.yml" if shared_entry else f"entry-{profile}.yml"
        (tmp_path / f"profile-{profile}.yml").write_text(
            f"task_file: {entry}\nchild: {tag}{profile}.yml\ncfg: {{child: '{child}'}}\n")
        (tmp_path / entry).write_text(f"- import_tasks: '{alias}'\n")
        (tmp_path / f"{profile}.yml").write_text(f"- debug: {{msg: imported-{profile}}}\n")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    for profile in ("normal", "alternate"):
        runtime, messages = _run_vars_file_include_profile(plan, profile)
        assert not runtime.failed, messages
        assert messages == [f"imported-{profile}"]

    if not shared_entry:
        (tmp_path / "override.yml").write_text("- debug: {msg: imported-override}\n")
        runtime, messages = _run_vars_file_include_profile(
            plan, "alternate", child="override.yml", cfg={"child": "override.yml"})
        assert runtime.failed, messages
        assert messages == ["no bundled task file matches 'entry-alternate.yml'"]

    secret = "ANSIROLL_TEST_ALT_IMPORT_SECRET"
    monkeypatch.setenv(secret, "must-not-be-captured")
    (tmp_path / "alternate.yml").write_text(f'''- debug:
    msg: "{{{{ lookup('env', '{secret}') }}}}"
''')
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    assert "must-not-be-captured" not in json.dumps(plan)


def test_vars_file_include_variant_retains_applied_keywords(tmp_path):
    from ansiroll.loader import _core_version

    if _core_version() < (2, 19):
        pytest.skip("requires include-time apply keyword rendering")
    for profile, hidden in (("normal", False), ("alternate", True)):
        (tmp_path / f"profile-{profile}.yml").write_text(
            f"task_file: {profile}.yml\nhide: {json.dumps(hidden)}\n")
        (tmp_path / f"{profile}.yml").write_text(f"- debug: {{msg: '{{{{ chosen }}}}-{profile}'}}\n")
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normal}
  vars_files: ['profile-{{ profile }}.yml']
  tasks:
    - include_tasks:
        file: '{{ task_file }}'
        apply:
          vars: {chosen: applied}
          no_log: '{{ hide }}'
""")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    for profile, hidden in (("normal", False), ("alternate", True)):
        frames = []
        runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, on_result=frames.append,
                          extra_vars={"profile": profile})
        runtime.run_play(plan)
        assert not runtime.failed, frames
        result = next(frame["result"] for frame in frames if frame["action"] == "debug")
        if hidden:
            assert "censored" in result
        else:
            assert result["msg"] == "applied-normal"


def test_vars_file_include_retains_native_static_import_magic(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normal}
  vars_files: ['profile-{{ profile }}.yml']
  tasks:
    - include_tasks: '{{ task_file }}'
""")
    for profile in ("normal", "alternate"):
        (tmp_path / f"profile-{profile}.yml").write_text(f"task_file: {profile}.yml\n")
        (tmp_path / f"{profile}.yml").write_text(
            """- import_tasks: "{{ 'child.yml' if ansible_playbook_python is defined else 'missing.yml' }}"\n""")
    (tmp_path / "child.yml").write_text("- debug: {msg: imported-child}\n")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    for profile in ("normal", "alternate"):
        runtime, messages = _run_vars_file_include_profile(plan, profile)
        assert not runtime.failed, messages
        assert messages == ["imported-child"]


def test_vars_file_include_rebinds_defaulted_filename_variable(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {choice: build}
  vars_files:
    - 'transition-{{ choice }}.yml'
    - "first-{{ profile | default('normal') }}.yml"
    - 'second-{{ profile }}.yml'
  tasks:
    - include_tasks: '{{ task_file }}'
""")
    files = {
        "transition-build.yml": "profile: build",
        "transition-launch.yml": "{}",
        "first-build.yml": "{}",
        "first-normal.yml": "profile: next",
        "second-build.yml": "task_file: build.yml",
        "second-next.yml": "task_file: launch.yml",
        "build.yml": "- debug: {msg: build-body}",
        "launch.yml": "- debug: {msg: launch-body}",
    }
    for name, source in files.items():
        (tmp_path / name).write_text(source + "\n")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, on_result=frames.append,
                      extra_vars={"choice": "launch"})
    runtime.run_play(plan)
    assert not runtime.failed, frames
    assert [frame["result"]["msg"] for frame in frames if frame.get("action") == "debug"] == ["launch-body"]


def test_vars_file_variant_writes_invalidate_an_earlier_include_path(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normal, subsequent: original.yml}
  vars_files: ['profile-{{ profile }}.yml']
  tasks:
    - include_tasks: wrapper.yml
      loop: [1, 2]
""")
    sources = {
        "profile-normal.yml": "variant: normal.yml",
        "profile-alternate.yml": "variant: alternate.yml",
        "wrapper.yml": "- name: earlier include\n  include_tasks: '{{ subsequent }}'\n- include_tasks: '{{ variant }}'",
        "normal.yml": "- set_fact: {subsequent: updated.yml}",
        "alternate.yml": "- debug: {msg: alternate}",
        "original.yml": "- debug: {msg: original}",
        "updated.yml": "- debug: {msg: updated}",
    }
    for name, source in sources.items():
        (tmp_path / name).write_text(source + "\n")
    _, manifest = compile_playbook(str(playbook))
    assert any(task["name"] == "earlier include" for task in manifest["fallback_tasks"])


def test_skipped_include_with_non_string_filename_compiles(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {task_file: 1}
  tasks:
    - include_tasks: '{{ task_file }}'
      when: false
""")
    plan = _compiled_plays(compile_playbook(str(playbook))[0], tmp_path)[0]
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"})
    runtime.run_play(plan)
    assert not runtime.failed


@pytest.mark.parametrize("prefix", ["", "{{ playbook_dir }}/"])
def test_vars_file_include_resolves_unsafe_filename_as_literal(tmp_path, prefix):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""- hosts: localhost
  gather_facts: false
  vars: {profile: normal}
  vars_files: ['profile-{{ profile }}.yml']
  tasks:
    - include_tasks: 'PREFIX{{ task_file }}'
""".replace("PREFIX", prefix))
    (tmp_path / "profile-normal.yml").write_text("task_file: normal.yml\n")
    (tmp_path / "profile-alternate.yml").write_text("task_file: !unsafe alternate.yml\n")
    (tmp_path / "normal.yml").write_text("- debug: {msg: default-body}\n")
    (tmp_path / "alternate.yml").write_text("- debug: {msg: alternate-body}\n")
    source, _ = compile_playbook(str(playbook))
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"}, on_result=frames.append,
                      extra_vars={"profile": "alternate"})
    runtime.run_play(_compiled_plays(source, tmp_path)[0])
    assert not runtime.failed
    assert "alternate-body" in json.dumps(frames)
