"""Templating safety regressions, checked against real ansible-playbook.

Each playbook runs under ansible-playbook (linear) and as a compiled
standalone script; debug messages and the recap must match.
"""

import json
import os
import re
import subprocess
import sys

import pytest

pytest.importorskip("ansible")

from ansible import constants as C  # noqa: E402
from ansible.release import __version__  # noqa: E402
from packaging.version import Version

from ansiroll.compiler import compile_playbook  # noqa: E402
from ansiroll.loader import parse_playbook  # noqa: E402

MODERN_TEMPLATING = Version(__version__) >= Version("2.19")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECAP_RE = re.compile(
    r"ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)\s+"
    r"skipped=(\d+)\s+rescued=(\d+)\s+ignored=(\d+)"
)
MSG_RE = re.compile(r'^\s*"msg": (".*"),?\s*$', re.M)


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["ANSIBLE_COLLECTIONS_PATH"] = os.path.join(ROOT, "collections")
    env["ANSIBLE_DEPRECATION_WARNINGS"] = "False"
    env["ANSIBLE_NOCOLOR"] = "1"
    env.pop("ANSIBLE_STRATEGY", None)
    return env


def _summary(text):
    recap = RECAP_RE.search(text)
    assert recap, text
    return [json.loads(m) for m in MSG_RE.findall(text)], recap.groups()


def _ansible(playbook, cwd, strategy=None):
    env = _env()
    if strategy is not None:
        env["ANSIBLE_STRATEGY"] = strategy
    proc = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local",
         "-e", json.dumps({"ansible_python_interpreter": sys.executable}), str(playbook)],
        cwd=str(cwd), env=env, capture_output=True, text=True,
    )
    return _summary(proc.stdout)


def _ansiroll(playbook, cwd):
    src, _ = compile_playbook(str(playbook))
    script = cwd / "compiled.py"
    script.write_text(src)
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(cwd), env=_env(), capture_output=True, text=True,
    )
    return _summary(proc.stdout + "\n" + proc.stderr)


def _write(tmp_path, playbook, files=None):
    for name, text in (files or {}).items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    path = tmp_path / "play.yml"
    path.write_text(playbook)
    return path


def _assert_parity(tmp_path, playbook, files=None, use_strategy=False):
    path = _write(tmp_path, playbook, files)
    expected = _ansible(path, tmp_path)
    if use_strategy:
        actual = _ansible(path, tmp_path, strategy="ansiroll.core.ansiroll")
    else:
        actual = _ansiroll(path, tmp_path)
    assert actual == expected
    return expected


def test_untrusted_data_is_not_templated_again(tmp_path):
    msgs, _ = _assert_parity(tmp_path, r"""
- hosts: localhost
  gather_facts: false
  vars:
    base: BASE
    raw_var: "{% raw %}{{ base }}{% endraw %}"
    unsafe_var: !unsafe "{{ base }}"
    chain1: "{{ chain2 }}"
    chain2: "{{ base }}-2"
    nested_list: ["{{ base }}", "{{ chain1 }}"]
  tasks:
    - command: printf '%s' '{{ "{{" }} base }}'
      register: out
      changed_when: false
    - debug: msg="{{ out.stdout }}"
    - debug: msg="{{ raw_var }}"
    - debug: msg="{{ unsafe_var }}"
    - debug: msg="{{ lookup('file', 'data.txt') }}"
    - debug: msg="{{ '{{ base }}' }}"
    - debug: msg="{{ item }}"
      loop: ["{{ '{{' }} base }}"]
    - debug: msg="{{ chain1 }}"
    - debug: msg="{{ nested_list | join(',') }}"
""", {"data.txt": "{{ base }}"})
    assert msgs[:5] == ["{{ base }}"] * 5
    assert msgs[5] == ("{{ base }}" if MODERN_TEMPLATING else "BASE")
    assert msgs[-1] == "BASE,BASE-2"


def test_role_defaults_and_group_vars_still_resolve_nested_templates(tmp_path):
    _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  roles: [r]
  tasks:
    - debug: msg="{{ from_group }} {{ hostvars['localhost'].from_group }}"
""", {
        "roles/r/defaults/main.yml":
            "inner: I\nouter: '{{ inner }}-O'\n"
            "items_: ['{{ outer }}']\nmapping: {k: '{{ outer }}'}\n",
        "roles/r/tasks/main.yml":
            "- debug: msg=\"{{ outer }} {{ items_[0] }} {{ mapping.k }}\"\n",
        "group_vars/all.yml": "g: G\nfrom_group: '{{ g }}!'\n",
    })


@pytest.mark.parametrize("use_strategy", [False, True], ids=["standalone", "strategy"])
def test_controller_authored_vars_preserve_template_provenance(tmp_path, use_strategy):
    vars_files = "" if use_strategy else "  vars_files: [sample.yml, literals.yml]\n"
    register = "\n      register: inclusion" if MODERN_TEMPLATING else ""
    include = """
    - ansible.builtin.include_vars: {file: sample.yml, name: included}%s
    - ansible.builtin.include_vars: literals.yml
""" % register if use_strategy else ""
    prefix = "included." if use_strategy else ""
    playbook = r"""
- hosts: localhost
  gather_facts: false
  vars:
    label: sample
%(vars_files)s  tasks:%(include)s
    - command:
        argv: [printf, '%%s', '{{ "{{" }} label }}-module']
      delegate_to: localhost
      register: output
      changed_when: false
    - debug:
        msg: >-
          {{ %(prefix)sauthored }} / {{ %(prefix)snested[0].value }} /
          {{ literal }} / {{ output.stdout }}
""" % {"vars_files": vars_files, "include": include, "prefix": prefix}
    if use_strategy:
        playbook += r"""
    - debug:
        msg: >-
          {{ authored is defined | lower }} / {{ ansible_facts.included is defined | lower }}
"""
        if register:
            playbook += r"""
    - debug:
        msg: "{{ inclusion.ansible_facts.included.authored }}"
"""
        playbook += r"""
    - ansible.builtin.include_vars: sample.yml
    - debug:
        msg: "{{ authored }} / {{ literal }}"
- hosts: localhost
  gather_facts: false
  vars:
    label: later
  tasks:
    - debug:
        msg: "{{ included.authored }} / {{ authored }} / {{ literal }}"
"""
        if register:
            playbook += r"""
    - debug:
        msg: >-
          {{ inclusion.ansible_facts.included.nested[0].value }} /
          {{ inclusion.ansible_facts.included.nested[0].literal }} /
          {{ inclusion.ansible_facts.included.nested[0].count }} /
          {{ inclusion.ansible_facts.included.nested[0].word }} /
          {{ output.stdout }}
"""
    messages, recap = _assert_parity(tmp_path, playbook, {
        "sample.yml": (
            "authored: '{{ label }}-authored'\n"
            "nested:\n"
            "  - value: '{{ label }}-nested'\n"
            "    literal: !unsafe '{{ label }}-included-literal'\n"
            "    count: 7\n"
            "    word: plain\n"
        ),
        "literals.yml": "literal: !unsafe '{{ label }}-literal'\n",
    }, use_strategy=use_strategy)

    assert recap[3] == "0"
    assert messages[0] == (
        "sample-authored / sample-nested / {{ label }}-literal / {{ label }}-module"
    )
    if use_strategy:
        assert messages[1] == "false / false"
        expected = [
            "sample-authored / {{ label }}-literal",
            "later-authored / later-authored / {{ label }}-literal",
        ]
        if register:
            expected.insert(0, "sample-authored")
            expected.append(
                "later-nested / {{ label }}-included-literal / 7 / plain / {{ label }}-module"
            )
        assert messages[2:] == expected


@pytest.mark.skipif(not MODERN_TEMPLATING, reason="modern include_vars register provenance")
def test_looped_include_vars_keeps_each_registered_definition_across_plays(tmp_path):
    messages, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    label: first
  tasks:
    - include_vars: {file: "{{ item }}.yml", name: data}
      loop: [authored, skipped, literal]
      when: item != 'skipped'
      register: inclusion
- hosts: localhost
  gather_facts: false
  vars:
    label: second
  tasks:
    - debug:
        msg: >-
          {{ inclusion.results[0].ansible_facts.data.value }} /
          {{ inclusion.results[1] is skipped | lower }} /
          {{ inclusion.results[2].ansible_facts.data.value }}
""", {
        "authored.yml": "value: '{{ label }}'\n",
        "literal.yml": "value: !unsafe '{{ label }}'\n",
    }, use_strategy=True)
    assert messages == ["second / true / {{ label }}"]


@pytest.mark.skipif(not MODERN_TEMPLATING, reason="modern Templar parity")
def test_magic_variable_mappings_flatten_without_recursion(tmp_path):
    nested_vars_check = (
        "all_vars.msg.vars.label == 'sample' and 'vars' not in all_vars.msg.vars"
        if Version(__version__) >= Version("2.20") else "'vars' not in all_vars.msg"
    )
    path = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    label: sample
    __ansiroll_label: user-provided
    nested: {leaf: '{{ label }}'}
  tasks:
    - no_log: true
      block:
        - debug: {msg: '{{ hostvars }}'}
          register: all_hosts
        - debug: {msg: '{{ hostvars.localhost }}'}
          register: one_host
        - debug: {msg: '{{ vars }}'}
          register: all_vars
    - assert:
        that:
          - all_hosts.msg | type_debug == 'dict'
          - all_hosts.msg.localhost.inventory_hostname == 'localhost'
          - one_host.msg | type_debug == 'dict'
          - one_host.msg.inventory_hostname == 'localhost'
          - all_vars.msg | type_debug == 'dict'
          - all_vars.msg.hostvars is defined
          - all_vars.msg['__ansiroll_label'] == 'user-provided'
          - "'__ansiroll_controller_config' not in all_vars.msg"
          - "%s"
          - all_vars.msg.nested.leaf == 'sample'
          - all_vars.msg.one_host.msg.inventory_hostname == 'localhost'
""" % nested_vars_check)
    _, linear_recap = _ansible(path, tmp_path)
    _, ansiroll_recap = _ansible(path, tmp_path, strategy="ansiroll.core.ansiroll")
    assert ansiroll_recap == linear_recap == ("4", "0", "0", "0", "0", "0", "0")


def test_template_and_copy_content_fail_on_undefined_variables(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    path = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    out: %s
    d: fine
  tasks:
    - template: {src: bad.j2, dest: "{{ out }}/bad.txt"}
      ignore_errors: true
    - copy:
        content: "a {{ undefined_var }} b"
        dest: "{{ out }}/copy.txt"
      ignore_errors: true
    - template: {src: ok.j2, dest: "{{ out }}/ok.txt"}
""" % json.dumps(str(out)), {
        "bad.j2": "head\n{{ undefined_var }}\ntail\n",
        "ok.j2": "ok={{ d }}\n",
    })
    results = []
    for run in (_ansible, _ansiroll):
        results.append(run(path, tmp_path))
        # Only the valid template is written; nothing partial or empty.
        assert sorted(os.listdir(str(out))) == ["ok.txt"]
        assert (out / "ok.txt").read_text() == "ok=fine\n"
        (out / "ok.txt").unlink()
    assert results[1] == results[0]


def test_when_errors_fail_instead_of_skipping(tmp_path):
    _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars: {typo: {}}
  tasks:
    - debug: msg=A
      when: undefined_var
      ignore_errors: true
    - debug: msg=B
      when: typo.x > 3
      ignore_errors: true
    - debug: msg=C
      when: 1/0 == 1
      ignore_errors: true
    - debug: msg=D
      when: undefined_var is defined
    - debug: msg=L
      loop: [1]
      when: undefined_var
      ignore_errors: true
    - block:
        - debug: msg=INBLOCK
      when: undefined_var
    - debug: msg=NOT_REACHED
""")


def test_nested_render_restores_loop_context(tmp_path):
    msgs, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    base: B
    lst: ["{{ base }}-x"]
  tasks:
    - debug: msg="{{ lst[0] ~ lookup('vars', 'item') }}"
      loop: [a, b]
""")
    assert msgs == ["B-xa", "B-xb"]


def test_fileglob_paths_are_baked_and_delivered_as_data(tmp_path):
    source = tmp_path / "files" / "item.txt"
    dest = tmp_path / "copied.txt"
    playbook = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - copy:
        src: "{{ item }}"
        dest: %s
      with_fileglob: ["*.txt"]
    - debug:
        msg: "{{ item | basename }}"
      with_fileglob: ["*.txt"]
""" % json.dumps(str(dest)), {"files/item.txt": "fixture"})

    plays, _ = parse_playbook(str(playbook))
    copy_task = next(task for block in plays[0]["tasks"] for task in block.get("block", [])
                     if task.get("action", "").split(".")[-1] == "copy")
    assert copy_task["loop"] == [{"__ansiroll_unsafe__": str(source)}]
    assert copy_task["_baked"] == {str(source): "fixture"}
    expected = _ansible(playbook, tmp_path)
    assert dest.read_text() == "fixture"
    dest.unlink()
    assert _ansiroll(playbook, tmp_path) == expected
    assert dest.read_text() == "fixture"


def test_frozen_vars_are_templated_once(tmp_path):
    path = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    y: Y
    x: "{% raw %}{{ y }}{% endraw %}"
    z: "{{ y }}-z"
  tasks:
    - debug: msg="x={{ x }} z={{ z }}"
""")
    expected = _ansible(path, tmp_path)
    src, _ = compile_playbook(
        str(path), tree_shake=True, freeze_vars=["x", "z"]
    )
    script = tmp_path / "frozen.py"
    script.write_text(src)
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(tmp_path), env=_env(), capture_output=True, text=True,
    )
    assert _summary(proc.stdout + "\n" + proc.stderr) == expected
    assert expected[0] == ["x={{ y }} z=Y-z"]


def test_ternary_regex_replace_and_keyword_booleans(tmp_path):
    msgs, _ = _assert_parity(tmp_path, r"""
- hosts: localhost
  gather_facts: false
  vars: {s_false: "false", s_no: "no", e: "", z: 0}
  tasks:
    - debug:
        msg: "{{ [s_false | ternary('T', 'F'), s_no | ternary('T', 'F'),
                 e | ternary('T', 'F'), z | ternary('T', 'F')] | join(',') }}"
    - debug: msg="{{ ['y' | bool, 't' | bool, 'yes' | bool] | join(',') }}"
    - debug:
        msg: "{{ 'Foo\nfoo' | regex_replace('^foo', 'X', ignorecase=true,
                                            multiline=true) }}"
    - debug: msg="{{ 'Foo foo' | regex_replace('foo', 'X', true) }}"
    - fail: msg=kw-y
      ignore_errors: "{{ 'y' }}"
    - fail: msg=kw-t
      ignore_errors: t
    - debug: msg=END
""")
    assert msgs[:4] == ["T,T,F,F", "False,False,True", "X\nX", "X X"]


@pytest.mark.skipif(MODERN_TEMPLATING, reason="legacy boolean-string conditionals")
@pytest.mark.parametrize("use_strategy", [False, True], ids=["standalone", "strategy"])
def test_legacy_boolean_strings_in_inline_conditions(tmp_path, use_strategy):
    messages, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: {msg: accepted}
      when: "{{ 'true' }}"
    - debug: {msg: skipped}
      when: "{{ 'false' }}"
""", use_strategy=use_strategy)
    assert messages == ["accepted"]
    assert recap == ("1", "0", "0", "0", "1", "0", "0")


def test_lazy_container_operations_resolve_nested_values_once(tmp_path):
    msgs, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    base: 2
    alias: "{{ base }}"
    mapping: {k: "{{ alias }}"}
    nested: {m: {k: "{{ alias }}"}}
    items_: ["{{ alias }}", "tail"]
  tasks:
    - debug:
        msg: >-
          {{ mapping | combine({'z': 1}) | to_json }} /
          {{ dict(mapping).k }} /
          {{ (nested | combine({'m': {'z': 1}}, recursive=true)).m.k }}
    - debug:
        msg: >-
          {{ (items_ + ['x']) | to_json }} /
          {{ (['x'] + items_) | to_json }} /
          {{ (items_ * 2) | to_json }} /
          {{ items_ | reverse | list | to_json }} /
          {{ 2 in items_ }}
    - debug:
        msg: "{{ [mapping | type_debug, items_ | type_debug] | join(',') }}"
""")
    assert msgs[-1] == "dict,list"
    native = MODERN_TEMPLATING or C.DEFAULT_JINJA2_NATIVE
    encoded = '2' if native else '"2"'
    assert msgs[0] == '{"k": %s, "z": 1} / 2 / 2' % encoded
    assert msgs[1].endswith("/ " + str(native))


@pytest.mark.parametrize("use_strategy", [False, True], ids=["standalone", "strategy"])
def test_unsafe_role_group_loop_and_argument_values_remain_literal(tmp_path, use_strategy):
    msgs, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars: {sentinel: rendered}
  roles: [r]
  tasks:
    - debug: msg="{{ from_group }}/{{ group_mapping.k }}"
    - debug: {msg: !unsafe "{{ sentinel }}"}
    - debug: msg="{{ item }}"
      loop: [!unsafe "{{ sentinel }}"]
    - set_fact: {fact_value: !unsafe "{{ sentinel }}"}
    - debug: msg="{{ fact_value }}/{{ fact_value | type_debug }}"
""", {
        "roles/r/defaults/main.yml": """
default_value: !unsafe "{{ sentinel }}"
default_mapping: {k: !unsafe "{{ sentinel }}"}
""",
        "roles/r/vars/main.yml": 'role_value: !unsafe "{{ sentinel }}"\n',
        "roles/r/tasks/main.yml": """
- debug: msg="{{ default_value }}/{{ default_mapping.k }}/{{ role_value }}"
""",
        "group_vars/all.yml": """
from_group: !unsafe "{{ sentinel }}"
group_mapping: {k: !unsafe "{{ sentinel }}"}
""",
    }, use_strategy=use_strategy)
    assert msgs == [
        "{{ sentinel }}/{{ sentinel }}/{{ sentinel }}",
        "{{ sentinel }}/{{ sentinel }}",
        "{{ sentinel }}",
        "{{ sentinel }}",
        "{{ sentinel }}/" + ("str" if MODERN_TEMPLATING else "AnsibleUnsafeText"),
    ]


def test_inherited_conditions_are_evaluated_at_each_task(tmp_path):
    msgs, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars: {enabled: false}
  tasks:
    - block:
        - debug: msg=uses_task_vars
          vars: {enabled: true}
        - debug: msg=uses_play_vars
      when: enabled
    - block:
        - debug: msg=one
        - debug: msg=two
      when: missing_block
      ignore_errors: true
    - include_tasks: included.yml
      when: missing_include
      ignore_errors: true
    - include_role: {name: r}
      when: missing_role
      ignore_errors: true
    - import_tasks: included.yml
      when: missing_import
      ignore_errors: true
    - block:
        - debug: msg=body
      rescue:
        - debug: msg=rescue
      always:
        - debug: msg=always
      when: missing_rescue
    - debug: msg=unreachable
""", {
        "included.yml": "- debug: msg=included\n",
        "roles/r/tasks/main.yml": "- debug: msg=role\n",
    })
    assert msgs == ["uses_task_vars"]
    assert recap == ("6", "0", "0", "2", "1", "1", "5")


@pytest.mark.parametrize("action", [
    "include_tasks: included.yml", "include_role: {name: r}",
], ids=["tasks", "role"])
@pytest.mark.parametrize("use_strategy", [False, True], ids=["standalone", "strategy"])
def test_dynamic_include_loop_resolution_reports_task_results(tmp_path, action, use_strategy):
    messages, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - name: undefined loop skipped
      ACTION
      loop: "{{ missing_loop }}"
      when: false
      register: skipped_loop
    - assert: {that: skipped_loop is skipped, quiet: true}
    - name: malformed loop fails despite the condition
      ACTION
      loop: "{{ 'scalar' }}"
      when: false
      ignore_errors: true
      register: malformed_loop
    - assert: {that: malformed_loop is failed, quiet: true}
    - name: undefined lookup loop ignored
      ACTION
      with_list: "{{ missing_loop }}"
      ignore_errors: true
    - block:
        - name: undefined loop rescued
          ACTION
          loop: "{{ missing_loop }}"
      rescue:
        - assert: {that: ansible_failed_result is failed, quiet: true}
      always:
        - debug: {msg: always ran}
    - name: empty loop skipped
      ACTION
      loop: []
""".replace("ACTION", action), {
        "included.yml": "- debug: {msg: unexpected included task}\n",
        "roles/r/tasks/main.yml": "- debug: {msg: unexpected included role}\n",
    }, use_strategy=use_strategy)
    assert messages == ["always ran"]
    assert recap == ("6", "0", "0", "0", "2", "1", "2")


def test_undefined_and_false_loop_conditions(tmp_path):
    _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg=unreachable
      loop: "{{ missing_loop }}"
      when: false
    - debug: msg=unreachable
      loop: ["{{ missing_item }}"]
      ignore_errors: true
    - debug: msg=unreachable
      loop: "{{ 'scalar' }}"
      when: false
      ignore_errors: true
    - debug: msg=unreachable
      loop: [1, 2]
      when: false
      register: skipped_loop
    - debug: msg="{{ skipped_loop.results | length }} items skipped"
""")


@pytest.mark.parametrize("use_strategy", [False, True])
def test_lookup_loop_undefined_is_deferred_until_when(tmp_path, use_strategy):
    _, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg=unreachable
      delegate_to: localhost
      with_list: "{{ missing_loop }}"
      when: false
      register: skipped_loop
    - assert:
        quiet: true
        that: skipped_loop is skipped
    - debug: msg=unreachable
      with_list: "{{ missing_loop }}"
      ignore_errors: true
    - debug: msg=unreachable
      with_dict: ["ordinary"]
      when: false
      ignore_errors: true
""", use_strategy=use_strategy)
    assert recap == ("3", "0", "0", "0", "1", "0", "2")


def test_delegated_task_ignores_unrelated_undefined_context_values(tmp_path):
    messages, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    unused: "{{ missing_scalar }}"
    unused_mapping: {nested: "{{ missing_mapping_value }}"}
    unused_list: ["{{ missing_list_value }}"]
    used: fixture
  tasks:
    - command: {argv: [printf, '%s', "{{ used }}"]}
      delegate_to: localhost
      changed_when: false
      register: delegated
    - debug: {msg: "{{ delegated.stdout }}"}
""", use_strategy=True)
    assert messages == ["fixture"]
    assert recap == ("2", "0", "0", "0", "0", "0", "0")


def test_until_and_assert_errors_and_invalid_ignore_errors(tmp_path):
    from ansible.release import __version__
    from packaging.version import Version

    if Version(__version__) < Version("2.19"):
        pytest.skip("checks ansible-core 2.19+ registered error results")
    msgs, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - command: "true"
      register: outcome
      until: outcome.missing.field
      ignore_errors: true
    - debug:
        msg: >-
          {{ outcome.changed }}/{{ outcome.rc is defined }}/{{ outcome.attempts is defined }} /
          {{ outcome.msg }}
    - assert: {that: [missing_assertion]}
      register: assertion_result
      ignore_errors: true
    - debug: msg="{{ assertion_result.msg }}"
    - debug: msg=skipped
      when: false
      ignore_errors: "{{ 'maybe' }}"
    - debug: msg=unreachable
      ignore_errors: "{{ 'maybe' }}"
""")
    modern = Version(__version__) >= Version("2.21")
    until_prefix = "An 'until' expression failed: " if modern else ""
    assert msgs == [
        "False/False/False / Task failed: " + until_prefix
        + "Error while evaluating conditional: object of type 'dict' has no attribute 'missing'",
        "Task failed: Error while evaluating conditional: 'missing_assertion' is undefined",
    ]
    if modern:
        assert recap == ("4", "0", "0", "1", "1", "0", "2")
    else:
        assert recap == ("5", "0", "0", "0", "1", "0", "3")


def test_break_when_error_fails_the_item(tmp_path):
    from ansible.release import __version__
    from packaging.version import Version

    if Version(__version__) < Version("2.21"):
        pytest.skip("checks ansible-core 2.21 loop result semantics")
    path = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg="{{ item }}"
      loop: [1, 2, 3]
      loop_control: {break_when: [item.missing]}
      register: outcome
      ignore_errors: true
    - debug:
        msg: "{{ outcome.msg }}/{{ outcome.results | length }}/{{ outcome.results[0].failed }}"
""")
    src, _ = compile_playbook(str(path))
    script = tmp_path / "compiled.py"
    script.write_text(src)
    commands = [
        ["ansible-playbook", "-i", "localhost,", "-c", "local", str(path)],
        [sys.executable, str(script)],
    ]
    for command in commands:
        proc = subprocess.run(command, cwd=str(tmp_path), env=_env(),
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert re.search(r"^(?:failed|fatal): .*\(item=1\)", proc.stdout, re.M)
        assert _summary(proc.stdout)[0] == ["One or more items failed/1/True"]


def test_template_omit_does_not_replace_existing_destination(tmp_path):
    from ansible.release import __version__
    from packaging.version import Version

    if Version(__version__) < Version("2.19"):
        pytest.skip("checks ansible-core 2.19+ Omit handling")
    out = tmp_path / "existing.txt"
    out.write_text("unchanged")
    _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - template: {src: omit.j2, dest: %s}
      ignore_errors: true
""" % json.dumps(str(out)), {"omit.j2": "{{ missing | default(omit) }}\n"})
    assert out.read_text() == "unchanged"


def test_compiled_condition_uses_controller_core_without_target_ansible(tmp_path):
    from ansible.release import __version__
    from packaging.version import Version

    strict = Version(__version__) >= Version("2.19")
    playbook = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg=unreachable
      when: "'text sentinel'"
      ignore_errors: true
""")
    script = tmp_path / "compiled.py"
    script.write_text(compile_playbook(str(playbook))[0])
    without_ansible = """
import runpy
import sys

class NoAnsible:
    def find_spec(self, fullname, *args):
        if fullname == 'ansible' or fullname.startswith('ansible.'):
            raise ImportError(fullname)

sys.meta_path.insert(0, NoAnsible())
runpy.run_path(sys.argv[1], run_name='__main__')
"""
    proc = subprocess.run(
        [sys.executable, "-c", without_ansible, str(script)],
        cwd=str(tmp_path), env=_env(), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ("Conditionals must have a boolean result." in proc.stdout) == strict
    assert _summary(proc.stdout) == _ansible(playbook, tmp_path)


def test_debug_nested_undefined_survives_generated_result_reporting(tmp_path):
    from ansible.release import __version__
    from packaging.version import Version

    path = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    details: {items: ["{{ missing_first }}", {other: "{{ missing_second }}"}]}
  tasks:
    - block:
        - debug: {msg: "{{ details }}"}
          register: outcome
      rescue:
        - debug: {msg: rescued}
      always:
        - debug: {msg: always}
    - debug: {var: details}
    - debug: {msg: "continued: {{ outcome.failed }}"}
""")
    script = tmp_path / "compiled.py"
    script.write_text(compile_playbook(str(path))[0])
    ansible = ["ansible-playbook", "-i", "localhost,", "-c", "local", str(path)]
    modern = Version(__version__) >= Version("2.19")
    runs = [(ansible, _env()), ([sys.executable, str(script)], _env())]
    if modern:
        runs.append((ansible, dict(_env(), ANSIBLE_STRATEGY="ansiroll.core.ansiroll")))
    for command, env in runs:
        proc = subprocess.run(command, cwd=tmp_path, env=env,
                              capture_output=True, text=True, check=True)
        msgs, recap = _summary(proc.stdout)
        assert msgs == ["rescued", "always", "continued: True"]
        assert recap == ("4", "0", "0", "0", "0", "1", "0")
        assert "'missing_first' is undefined" in proc.stdout
        if modern:
            assert "<< error 1 - 'missing_first' is undefined >>" in proc.stdout
            assert "<< error 2 - 'missing_second' is undefined >>" in proc.stdout
        else:
            assert "VARIABLE IS NOT DEFINED!" in proc.stdout

    raw = subprocess.run([sys.executable, str(script)], cwd=tmp_path,
                         env=dict(_env(), ANSIROLL_RAW="1"),
                         capture_output=True, text=True, check=True)
    state = next(line for line in raw.stdout.splitlines()
                 if line.startswith("__ANSIROLL_STATE__ "))
    assert json.loads(state.split(" ", 1)[1])["vars"]["outcome"]["failed"]


def test_group_by_refresh_preserves_trusted_templates(tmp_path):
    msgs, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars: {label: rendered}
  tasks:
    - group_by: {key: added}
    - debug: {msg: "{{ group_text }} / {{ group_literal }}"}
""", {"group_vars/added.yml": """
group_text: 'value={{ label }}'
group_literal: !unsafe 'value={{ label }}'
"""}, use_strategy=True)
    assert msgs == ["value=rendered / value={{ label }}"]


def test_group_by_refresh_detects_trust_changes_without_text_changes(tmp_path):
    msgs, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars: {label: rendered}
  tasks:
    - debug: {msg: "{{ direct }} / {{ nested.values_[0] }}"}
    - group_by: {key: added}
    - debug: {msg: "{{ direct }} / {{ nested.values_[0] }}"}
    - group_by: {key: restored}
    - debug: {msg: "{{ direct }} / {{ nested.values_[0] }}"}
""", {
        "group_vars/all.yml": """
direct: !unsafe '{{ label }}'
nested: {values_: ['{{ label }}']}
""",
        "group_vars/added.yml": """
direct: '{{ label }}'
nested: {values_: [!unsafe '{{ label }}']}
""",
        "group_vars/restored.yml": """
direct: !unsafe '{{ label }}'
nested: {values_: ['{{ label }}']}
""",
    }, use_strategy=True)
    assert msgs == ["{{ label }} / rendered", "rendered / {{ label }}", "{{ label }} / rendered"]


def test_omit_ignore_errors_uses_core_keyword_default(tmp_path):
    from ansible.release import __version__
    from packaging.version import Version

    msgs, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - block:
        - fail: {msg: first}
          ignore_errors: "{{ omit }}"
      rescue:
        - debug: {msg: rescued first}
    - block:
        - fail: {msg: second}
          ignore_errors: "{{ omit }}"
        - debug: {msg: after second}
      ignore_errors: true
      rescue:
        - debug: {msg: rescued second}
""")
    if Version(__version__) >= Version("2.14"):
        assert msgs == ["rescued first", "after second"]
        assert recap[-2:] == ("1", "1")
    else:
        assert msgs == ["rescued first", "rescued second"]
        assert recap[-2:] == ("2", "0")


def test_frozen_values_and_extra_vars_preserve_literal_mappings(tmp_path):
    path = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    label: rendered
    fixed:
      __ansiroll_data__: {'{{ label }}': literal}
  tasks:
    - debug:
        msg: "{{ fixed['__ansiroll_data__'].keys() | first }}/{{ ordinary['__ansiroll_data__'] }}"
""")
    src, _ = compile_playbook(
        str(path), freeze_vars=["fixed"],
        extra_vars={"ordinary": {"__ansiroll_data__": "ordinary"}},
    )
    script = tmp_path / "compiled.py"
    script.write_text(src)
    proc = subprocess.run([sys.executable, str(script)], cwd=tmp_path,
                          env=_env(), capture_output=True, text=True, check=True)
    assert _summary(proc.stdout)[0] == ["{{ label }}/ordinary"]


@pytest.mark.parametrize("enabled", [False, True])
def test_broken_conditional_policy_is_baked(tmp_path, enabled):
    from ansible.release import __version__
    from packaging.version import Version

    if Version(__version__) < Version("2.19"):
        pytest.skip("ALLOW_BROKEN_CONDITIONALS was introduced in 2.19")

    path = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: {msg: truthy expression}
      when: "'false'"
      ignore_errors: true
    - debug: {msg: literal number}
      when: 1
      ignore_errors: true
    - debug: {msg: whitespace}
      when: '  '
      ignore_errors: true
    - debug: {msg: rendered empty}
      when: "{{ '' }}"
      ignore_errors: true
    - debug: {msg: skipped}
      when: "[]"
      ignore_errors: true
""")
    env = dict(_env(), ANSIBLE_ALLOW_BROKEN_CONDITIONALS=str(enabled))
    expected = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", str(path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
    )
    script = tmp_path / "compiled.py"
    subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", str(path), "-o",
         str(script), "--tree-shake"], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=True,
    )
    env["ANSIBLE_ALLOW_BROKEN_CONDITIONALS"] = str(not enabled)
    compiled = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=True,
    )
    assert _summary(compiled.stdout) == _summary(expected.stdout)
    assert _summary(expected.stdout)[0] == (
        ["truthy expression", "literal number", "whitespace", "rendered empty"]
        if enabled else []
    )


@pytest.mark.parametrize("use_strategy", [False, True])
def test_set_fact_templates_authored_fact_names(tmp_path, use_strategy):
    _, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    fact_key: greeting
    fact_value: hello
  tasks:
    - set_fact:
        "{{ fact_key }}": "{{ fact_value }}"
      register: result
    - ansible.builtin.set_fact:
        "fruit_{{ idx }}": "{{ item }}"
      loop: [apple, pear]
      loop_control:
        index_var: idx
    - set_fact:
        !unsafe "{{ fact_key }}": goodbye
      ignore_errors: true
    - assert:
        quiet: true
        that:
          - greeting == 'hello'
          - result.ansible_facts.greeting == 'hello'
          - fruit_0 == 'apple'
          - fruit_1 == 'pear'
""", use_strategy=use_strategy)
    assert recap == ("4", "0", "0", "0", "0", "0", "1")


@pytest.mark.parametrize("use_strategy", [False, True])
def test_loop_preserves_distinct_registered_results(tmp_path, use_strategy):
    _, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    fruit_names: [apple, pear, orange, plum]
  tasks:
    - shell: printf '%s' "{{ item }}"
      loop: "{{ fruit_names }}"
      register: fruit
    - assert:
        quiet: true
        that: item.stdout == fruit_names[idx]
      loop: "{{ fruit.results }}"
      loop_control:
        index_var: idx
""", use_strategy=use_strategy)
    assert recap == ("2", "1", "0", "0", "0", "0", "0")


def test_mapping_keys_stay_literal(tmp_path):
    msgs, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars: {label: rendered}
  tasks:
    - set_fact:
        mapping:
          '{{ label }}': trusted
          !unsafe '{{ label }}-literal': untrusted
    - debug:
        msg: "{{ mapping.keys() | list | sort | join(',') }}"
""")
    assert msgs == ["{{ label }},{{ label }}-literal"]


def test_play_vars_see_task_vars_include_params_and_loop_bindings(tmp_path):
    msgs, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    env: prod
    alias: "{{ env }}"
    chained: "{{ alias }}-x"
  tasks:
    - debug: msg="{{ alias }} {{ chained }}"
    - debug: msg="{{ alias }} {{ chained }}"
      vars: {env: task}
    - debug: msg="{{ alias }} {{ chained }}"
      loop: [loop]
      loop_control: {loop_var: env}
    - debug: msg="{{ alias }} {{ chained }}"
      vars: {env: task}
      loop: [a]
      loop_control: {loop_var: env}
    - debug: msg="{{ alias }}"
      vars: {alias: own}
      loop: [b]
      loop_control: {loop_var: env}
    - include_tasks: inc.yml
      vars: {env: param}
    - include_tasks: inc.yml
      loop: [incloop]
      loop_control: {loop_var: env}
    - debug: msg="{{ alias }} {{ chained }}"
""", {"inc.yml": '- debug: msg="{{ alias }} {{ chained }}"\n'})
    assert msgs == ["prod prod-x", "task task-x", "loop loop-x", "a a-x", "own",
                    "param param-x", "incloop incloop-x", "prod prod-x"]
