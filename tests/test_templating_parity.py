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

from ansiroll.compiler import compile_playbook  # noqa: E402

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
        ["ansible-playbook", "-i", "localhost,", "-c", "local", str(playbook)],
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
    assert msgs[0] == "{{ base }}"
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
    assert msgs[0].endswith("/ 2 / 2")


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
        "{{ sentinel }}/str",
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
