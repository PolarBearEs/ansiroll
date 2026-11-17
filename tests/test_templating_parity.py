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


def _ansible(playbook, cwd):
    proc = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", str(playbook)],
        cwd=str(cwd), env=_env(), capture_output=True, text=True,
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


def _assert_parity(tmp_path, playbook, files=None):
    path = _write(tmp_path, playbook, files)
    expected = _ansible(path, tmp_path)
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
