"""
Play/handler/precedence/retry semantics compared against real ansible-playbook.

Each scenario is written to tmp_path, run with ansible-playbook (linear), with
the compiled standalone script and with the ansiroll strategy; the debug
messages (in order) and the recap must match.
"""

import os
import re
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("ansible")

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


def _ansible(base, args, strategy=None):
    env = _env()
    if strategy:
        env["ANSIBLE_STRATEGY"] = strategy
    out = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", "pb.yml"] + args,
        cwd=str(base), env=env, capture_output=True, text=True,
    )
    return _summary(out.stdout)


def _standalone(base, args):
    out_py = os.path.join(str(base), "out.py")
    comp = subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", "pb.yml", "-o", out_py]
        + args,
        cwd=str(base), env=_env(), capture_output=True, text=True,
    )
    assert comp.returncode == 0, comp.stderr
    out = subprocess.run(
        [sys.executable, out_py], cwd=str(base), env=_env(),
        capture_output=True, text=True, timeout=120,
    )
    return _summary(out.stdout + "\n" + out.stderr)


def check(tmp_path, files, args=(), strategy=True):
    _write(tmp_path, files)
    args = list(args)
    expected = _ansible(tmp_path, args)
    assert _standalone(tmp_path, args) == expected
    if strategy:
        assert _ansible(tmp_path, args, "ansiroll.core.ansiroll") == expected
    return expected


FAILED_HOST_PLAYS = """
- hosts: localhost
  gather_facts: false
  {force}
  tasks:
    - command: /bin/true
      notify: h1
    - fail: msg=boom
  handlers:
    - name: h1
      debug: msg=H1_RAN
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg=PLAY2_RAN
"""


@pytest.mark.parametrize("force", ["", "force_handlers: true"])
def test_failed_host_skips_later_plays_and_honors_force_handlers(tmp_path, force):
    msgs, _ = check(tmp_path, {"pb.yml": FAILED_HOST_PLAYS.format(force=force)})
    assert ('"PLAY2_RAN"' in msgs) is False
    assert ('"H1_RAN"' in msgs) is bool(force)


def test_handlers_run_in_definition_order_with_chains_and_listen(tmp_path):
    msgs, _ = check(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          tasks:
            - command: /bin/true
              notify: [h2, h1, topic, hb]
          handlers:
            - name: h0
              debug: msg=H0_RAN
            - name: h1
              debug: msg=H1_RAN
              changed_when: true
              notify: h3
            - name: h2
              debug: msg=H2_RAN
              changed_when: true
              notify: h0
            - name: hb
              debug: msg=HB_RAN
              listen: topic
            - name: h3
              debug: msg=H3_RAN
        """})
    assert msgs == ['"H1_RAN"', '"H2_RAN"', '"HB_RAN"', '"H3_RAN"', '"H0_RAN"']


def test_handler_chain_notified_backwards_during_final_flush(tmp_path):
    check(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          post_tasks:
            - command: /bin/true
              notify: [h3]
          handlers:
            - {name: h0, debug: msg=H0}
            - {name: h1, debug: msg=H1, changed_when: true, notify: h0}
            - {name: h2, debug: msg=H2, changed_when: true, notify: h1}
            - {name: h3, debug: msg=H3, changed_when: true, notify: h2}
        """})


FAILING_HANDLER = """
- hosts: localhost
  gather_facts: false
  {force}
  tasks:
    - command: /bin/true
      notify: [ha, hb]
    - meta: flush_handlers
    - debug: msg=AFTER_FLUSH
    - command: /bin/true
      notify: [hc]
  post_tasks:
    - debug: msg=POST
  handlers:
    - name: ha
      fail: msg=HA_FAIL
    - name: hb
      debug: msg=HB_RAN
    - name: hc
      debug: msg=HC_RAN
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg=PLAY2
"""


@pytest.mark.parametrize("force", ["", "force_handlers: true"])
def test_failing_handler_fails_host_with_clean_recap(tmp_path, force):
    _, recap = check(tmp_path, {"pb.yml": FAILING_HANDLER.format(force=force)})
    assert recap[3] == "1"


def test_listen_and_include_role_handlers(tmp_path):
    check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              tasks:
                - include_role: name=r
                - command: /bin/true
                  notify: [r_handler, "r : r_topic", plain_topic]
              handlers:
                - name: real name
                  debug: msg=PLAY_LISTEN_RAN
                  listen: plain_topic
            """,
        "roles/r/tasks/main.yml": """
            - command: /bin/true
              notify: r_handler
            """,
        "roles/r/handlers/main.yml": """
            - name: r_handler
              debug: msg=R_HANDLER_RAN
            - name: r_listener
              debug: msg=R_LISTEN_RAN
              listen: r_topic
            """,
    })


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


def test_until_and_retries_semantics(tmp_path):
    msgs, _ = check(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          tasks:
            - command: /bin/true
              register: r1
              until: false
              delay: 0
              ignore_errors: true
            - debug: msg="T1 attempts={{ r1.attempts | default('none') }} failed={{ r1.failed }}"
            - command: /bin/false
              register: r2
              retries: 2
              delay: 0
              ignore_errors: true
            - debug: msg="T2 attempts={{ r2.attempts | default('none') }} failed={{ r2.failed }}"
            - command: /bin/true
              register: r3
              until: false
              retries: 1
              delay: 0
              failed_when: false
            - debug: msg="T3 attempts={{ r3.attempts | default('none') }} failed={{ r3.failed }}"
            - command: echo hi
              register: r4
              changed_when: stdout is defined
            - debug: msg="T4 changed={{ r4.changed }}"
        """})
    assert '"T4 changed=False"' in msgs
