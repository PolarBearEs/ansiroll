"""
Play/handler/precedence/retry semantics compared against real ansible-playbook.

Each scenario is written to tmp_path, run with ansible-playbook (linear), with
the compiled standalone script and with the ansiroll strategy; the debug
messages (in order) and the recap must match.
"""

import json
import os
import re
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("ansible")
from ansible.release import __version__

CORE_VERSION = tuple(map(int, re.match(r"^(\d+)\.(\d+)", __version__).groups()))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MSG_RE = re.compile(r'^\s+"msg": (.*)$', re.M)
RECAP_RE = re.compile(
    r"ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)\s+"
    r"skipped=(\d+)\s+rescued=(\d+)\s+ignored=(\d+)"
)


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["ANSIBLE_COLLECTIONS_PATH"] = os.path.join(ROOT, "collections")
    env["ANSIBLE_DEPRECATION_WARNINGS"] = "False"
    env["ANSIBLE_NOCOLOR"] = "1"
    if CORE_VERSION < (2, 11):
        # ansible-base 2.10's bundled module_utils needs its supported Python.
        env["ANSIBLE_PYTHON_INTERPRETER"] = sys.executable
    env.pop("ANSIBLE_STRATEGY", None)
    return env


def _write(base, files):
    for rel, text in files.items():
        path = os.path.join(str(base), rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(text).lstrip("\n"))


def _summary(text):
    recap = RECAP_RE.search(text)
    assert recap, "no recap found in:\n%s" % text
    return MSG_RE.findall(text), recap.groups()


def _ansible_playbook():
    # Use the ansible-core that compiles the playbook (same interpreter), not
    # whichever ansible-playbook happens to be first on PATH.
    local = os.path.join(os.path.dirname(sys.executable), "ansible-playbook")
    return local if os.path.exists(local) else "ansible-playbook"


def _ansible_run(base, args, strategy=None):
    env = _env()
    if strategy:
        env["ANSIBLE_STRATEGY"] = strategy
    return subprocess.run(
        [_ansible_playbook(), "-i", "localhost,", "-c", "local", "pb.yml"] + list(args),
        cwd=str(base), env=env, capture_output=True, text=True, timeout=120,
    )


def _ansible(base, args, strategy=None):
    return _summary(_ansible_run(base, args, strategy).stdout)


def _standalone_run(base, args, compile_args=()):
    out_py = os.path.join(str(base), "out.py")
    comp = subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", "pb.yml", "-o", out_py]
        + list(args) + list(compile_args),
        cwd=str(base), env=_env(), capture_output=True, text=True,
    )
    assert comp.returncode == 0, comp.stderr
    return subprocess.run(
        [sys.executable, out_py], cwd=str(base), env=_env(),
        capture_output=True, text=True, timeout=120,
    )

def _standalone(base, args):
    out = _standalone_run(base, args)
    return _summary(out.stdout + "\n" + out.stderr)


def check(tmp_path, files, args=(), strategy=True):
    _write(tmp_path, files)
    args = list(args)
    expected = _ansible(tmp_path, args)
    assert _standalone(tmp_path, args) == expected
    if strategy:
        assert _ansible(tmp_path, args, "ansiroll.core.ansiroll") == expected
    return expected


def test_variable_precedence(tmp_path):
    msgs, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              vars: {a: play, b: play, c: play, d: play, e: play, p: play, q: play}
              tasks:
                - set_fact: {a: setfact}
                - command: echo reg
                  register: b
                - block:
                    - debug: msg="a={{ a }} b={{ b.stdout | default(b) }} c={{ c }} e={{ e }}"
                      vars: {a: task, b: task, e: task}
                  vars: {a: block, b: block, c: block}
                - debug: msg="after b={{ b.stdout | default(b) }}"
                - set_fact: {c: setfact_c}
                - debug: msg="c2={{ c }}"
                  vars: {c: task_c}
                - include_role: {name: rv}
                - debug: msg="d_after_role={{ d }}"
                - include_tasks: inc.yml
                  vars: {p: incparam, q: incparam}
                - block:
                    - debug: msg="blockwhen p={{ p }}"
                  vars: {a: block}
                  when: a == 'setfact'
            - hosts: localhost
              gather_facts: false
              vars: {a: play2}
              tasks:
                - debug: msg="play2 a={{ a }}"
            """,
        "inc.yml": """
            - debug: msg="inc p={{ p }} q={{ q }}"
              vars: {p: task, q: task}
            """,
        "roles/rv/vars/main.yml": """
            r: rolevar
            a: rolevar
            d: rolevar
            """,
        "roles/rv/tasks/main.yml": """
            - debug: msg="in role r={{ r }} a={{ a }} d={{ d }}"
            - set_fact: {d: setfact_d}
            - debug: msg="in role after setfact d={{ d }}"
            """,
    }, args=["-e", "e=extra"], strategy=False)
    assert '"a=setfact b=reg c=block e=extra"' in msgs


def test_register_not_clobbered_by_templated_play_var(tmp_path):
    check(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          vars:
            base: hello
            x: "{{ base }}-play"
          tasks:
            - command: echo reg
              register: x
            - debug: msg="x={{ x.stdout | default(x) }}"
            - debug: msg="x2={{ x.stdout | default(x) }}"
        """})


@pytest.mark.parametrize("mode", ["linear", "standalone", "strategy"])
def test_unused_play_template_does_not_run_lookup(tmp_path, mode):
    _write(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          vars:
            unused: "{{ lookup('pipe', 'echo looked_up >> lookup_marker') }}"
          tasks:
            - debug: msg=UNRELATED
        """})
    out = _run_mode(tmp_path, mode)
    output = out.stdout + out.stderr
    assert out.returncode == 0, output
    assert "UNRELATED" in output
    assert not (tmp_path / "lookup_marker").exists()


@pytest.mark.parametrize("mode", ["linear", "standalone", "strategy"])
def test_play_alias_keeps_registered_list_data_literal(tmp_path, mode):
    _write(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          vars:
            marker_value: interpolated
            marker_lines: "{{ marker.stdout_lines }}"
          tasks:
            - command: cat marker.txt
              register: marker
            - debug:
                msg: "{{ marker_lines }}"
        """, "marker.txt": "{{ marker_value }}\n"})
    out = _run_mode(tmp_path, mode)
    output = out.stdout + out.stderr
    assert out.returncode == 0, output
    assert '"{{ marker_value }}"' in output
    assert '"interpolated"' not in output


@pytest.mark.parametrize("mode", ["linear", "standalone", "strategy"])
def test_deferred_play_vars_resolve_in_mapping_iteration(tmp_path, mode):
    _write(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          vars:
            source: play-value
            alias: "resolved-{{ source }}"
            active: "{{ source == 'task-value' }}"
          tasks:
            - debug:
                msg: >-
                  {{ (vars | dict2items(key_name='name', value_name='data') |
                      selectattr('name', 'equalto', 'active') | first).data is sameas true }}
                  {{ vars.items() | selectattr('0', 'equalto', 'alias') |
                     map(attribute='1') | first }}
                  {{ vars.values() | select('equalto', 'resolved-task-value') | first | default('missing') }}
              vars:
                source: task-value
        """})
    result = _run_mode(tmp_path, mode)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = "True\nresolved-task-value\nresolved-task-value"
    if CORE_VERSION < (2, 19):
        # Legacy Ansible exposes the raw values in the vars mapping.
        expected = "False\nresolved-{{ source }}\nmissing"
    assert _summary(result.stdout)[0] == [json.dumps(expected)]


@pytest.mark.parametrize("mode", ["linear", "standalone", "strategy"])
def test_mapping_copies_resolve_deferred_play_vars_on_modern_core(tmp_path, mode):
    if CORE_VERSION < (2, 19):
        pytest.skip("legacy Ansible exposes raw values in vars mapping copies")
    _write(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          vars:
            source: 5
            alias: "{{ source }}"
          tasks:
            - debug:
                msg: >-
                  {{ (vars | combine({})).alias == 5 }}
                  {{ (vars | combine({'alias': 6})).alias == 6 }}
                  {{ ({'known': true} | combine(vars)).alias == 5 }}
                  {{ vars.copy().alias == 5 }}
                  {{ dict(vars).alias == 5 }}
                  {{ dict(vars, alias=6).alias == 6 }}
                  {{ dict(plain=7).plain == 7 }}
        """})
    result = _run_mode(tmp_path, mode)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _summary(result.stdout)[0] == [json.dumps("True True True True True True True")]


def test_role_alias_resolves_in_vars_with_a_deferred_play_var(tmp_path):
    if CORE_VERSION < (2, 19):
        pytest.skip("legacy Ansible exposes raw values in vars mapping copies")
    messages, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              vars:
                play_alias: "{{ source }}"
                source: sample
              roles: [r]
        """,
        "roles/r/vars/main.yml": 'role_alias: "{{ source }}"\n',
        "roles/r/tasks/main.yml": """
            - debug:
                msg: "{{ play_alias }} {{ role_alias }} {{ vars.role_alias }} {{ dict(vars).role_alias }}"
        """,
    })
    assert messages == ['"sample sample sample sample"']


@pytest.mark.parametrize("mode", ["linear", "standalone", "strategy"])
def test_deferred_play_vars_keep_literal_mapping_values(tmp_path, mode):
    _write(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          vars:
            alias: "{{ marker.stdout }}"
            marker_value: interpolated
          tasks:
            - command: cat marker.txt
              register: marker
            - debug:
                msg: >-
                  {{ (vars | dict2items | selectattr('key', 'equalto', 'alias') | first).value }}
            - debug:
                msg: >-
                  {{ vars.items() | selectattr('0', 'equalto', 'alias') | map(attribute='1') | first }}
        """, "marker.txt": "{{ marker_value }}\n"})
    result = _run_mode(tmp_path, mode)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = "{{ marker_value }}"
    if CORE_VERSION < (2, 19):
        expected = "{{ marker.stdout }}"
    assert _summary(result.stdout)[0] == [json.dumps(expected)] * 2


def test_role_params_outrank_set_fact(tmp_path):
    msgs, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              tasks:
                - set_fact: {b: sf, c: sf}
            - hosts: localhost
              gather_facts: false
              roles:
                - {role: r, b: roleparam}
            """,
        "roles/r/vars/main.yml": """
            c: rolevar
            """,
        "roles/r/tasks/main.yml": """
            - debug: msg="b={{ b }} c={{ c }}"
            """,
    })
    assert msgs == ['"b=roleparam c=sf"']


def test_task_aliases_see_role_parameter_and_include_precedence(tmp_path):
    messages, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              roles:
                - {role: r, value: param, file: role.yml}
        """,
        "roles/r/tasks/main.yml": """
            - debug: msg="{{ value }} {{ alias }}"
              vars: {value: task, alias: "{{ value }}"}
            - include_tasks: "{{ file }}"
              vars: {file: site.yml}
            - include_tasks: "{{ file }}"
        """,
        "roles/r/tasks/role.yml": "- debug: {msg: ROLE_FILE}",
        "roles/r/tasks/site.yml": "- debug: {msg: INCLUDE_FILE}",
    })
    assert messages == ['"param param"', '"INCLUDE_FILE"', '"ROLE_FILE"']


def _run_mode(tmp_path, mode):
    if mode == "standalone":
        return _standalone_run(tmp_path, ())
    return _ansible_run(tmp_path, (), "ansiroll.core.ansiroll" if mode == "strategy" else None)


def _reject_setup_become(tmp_path, monkeypatch):
    # Fail setup without invoking real sudo, even on a machine with sudo access.
    sudo = tmp_path / "sudo"
    sudo.write_text("#!/bin/sh\necho 'setup cannot become requested user' >&2\nexit 1\n")
    sudo.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("ANSIROLL_FAST_FACTS", raising=False)


SETUP_FAILURE = """
- hosts: localhost
  become: true
  become_user: ansiroll_nonexistent_setup_user
  {ignore}
  tasks:
    - debug: msg=AFTER_SETUP
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg=NEXT_PLAY
"""


def test_include_uses_own_parameters_before_its_condition(tmp_path):
    messages, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              tasks:
                - set_fact: {enabled: false}
                - include_tasks: child.yml
                  vars: {enabled: true}
                  when: enabled
        """,
        "child.yml": "- debug: {msg: included}",
    })
    assert messages == ['"included"']


def test_role_magic_is_visible_when_resolving_task_aliases(tmp_path):
    messages, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              vars:
                role_directory: "{{ role_path | basename }}"
              roles: [r]
        """,
        "roles/r/tasks/main.yml": """
            - debug: {msg: "{{ task_alias }}"}
              vars: {task_alias: "{{ role_directory }}"}
        """,
    })
    assert messages == ['"r"']
