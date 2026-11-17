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


def _standalone_run(base, args):
    out_py = os.path.join(str(base), "out.py")
    comp = subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", "pb.yml", "-o", out_py]
        + list(args),
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


@pytest.mark.parametrize("topic", ["r_topic", "r : r_topic"])
def test_listen_and_include_role_handlers(tmp_path, topic):
    files = {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              tasks:
                - include_role: name=r
                - command: /bin/true
                  notify: [r_handler, "%s", plain_topic]
              handlers:
                - name: real name
                  debug: msg=PLAY_LISTEN_RAN
                  listen: plain_topic
            """ % topic,
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
    }
    if CORE_VERSION < (2, 17) and topic != "r_topic":
        _write(tmp_path, files)
        for out in [_ansible_run(tmp_path, ()), _standalone_run(tmp_path, ())]:
            assert out.returncode != 0
            assert "The requested handler 'r : r_topic' was not found" in out.stdout + out.stderr
        return
    msgs, _ = check(tmp_path, files)
    assert msgs == ['"PLAY_LISTEN_RAN"', '"R_HANDLER_RAN"', '"R_LISTEN_RAN"']


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


def test_until_exhausted_registered_result(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIBLE_DISPLAY_TRACEBACK", "never")
    msgs, _ = check(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          tasks:
            - command: /bin/true
              register: a
              until: false
              retries: 1
              delay: 0
              ignore_errors: true
            - debug:
                msg: "A {{ a.attempts }} {{ a.failed }} {{ a.exception | default('none') }}"
            - command: /bin/false
              register: e
              until: e.rc == 0
              retries: 1
              delay: 0
              failed_when: e.rc == 7
            - debug: msg="E {{ e.attempts }} {{ e.failed }}"
        """})
    exception = "(traceback unavailable)" if (2, 19) <= CORE_VERSION < (2, 21) else "none"
    assert msgs[0] == '"A 1 True %s"' % exception


def test_failed_task_facts_persist_after_rescue(tmp_path):
    msgs, _ = check(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          tasks:
            - block:
                - fail: msg=first
                  name: failing one
              rescue:
                - debug: msg="r1 {{ ansible_failed_task.name }} {{ ansible_failed_result.msg }}"
                - set_fact: {ansible_failed_result: {msg: overridden}}
                - debug: msg="r2 {{ ansible_failed_result.msg }}"
            - debug: msg="after {{ ansible_failed_task.name }} {{ ansible_failed_result.msg }}"
        """})
    assert msgs[-1] == '"after failing one overridden"'


def _run_mode(tmp_path, mode):
    if mode == "standalone":
        return _standalone_run(tmp_path, ())
    return _ansible_run(tmp_path, (), "ansiroll.core.ansiroll" if mode == "strategy" else None)


@pytest.mark.parametrize("mode", ["linear", "standalone", "strategy"])
@pytest.mark.parametrize("condition", ["missing_condition", "'false'", "1 - 1", "1 + 0"])
def test_invalid_until_does_not_repeat_side_effects(tmp_path, monkeypatch, mode, condition):
    if condition != "missing_condition" and CORE_VERSION < (2, 19):
        pytest.skip("boolean results are required from ansible-core 2.19")
    monkeypatch.setenv("ANSIBLE_ALLOW_BROKEN_CONDITIONALS", "False")
    _write(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          tasks:
            - shell: echo attempt >> attempts
              until: "%s"
              delay: 0
            - debug: msg=AFTER_UNTIL
        """ % condition})
    out = _run_mode(tmp_path, mode)
    output = out.stdout + out.stderr
    assert out.returncode != 0
    assert _summary(output)[1][3] == "1"
    assert "AFTER_UNTIL" not in output
    assert (tmp_path / "attempts").read_text().splitlines() == ["attempt"]


@pytest.mark.skipif(CORE_VERSION < (2, 19), reason="added in ansible-core 2.19")
@pytest.mark.parametrize("source", ["env", "config"])
def test_until_allows_broken_conditionals_without_config_lookup(tmp_path, monkeypatch, source):
    files = {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          tasks:
            - shell: echo attempt >> attempts
              until: "'false'"
              delay: 0
            - shell: echo attempt >> attempts
              until: 1 + 0
              delay: 0
            - shell: echo attempt >> attempts
              until: 1 - 1
              retries: 1
              delay: 0
              register: exhausted
              ignore_errors: true
            - debug: msg="EXHAUSTED {{ exhausted.attempts }} {{ exhausted.failed }}"
        """}
    monkeypatch.delenv("ANSIBLE_ALLOW_BROKEN_CONDITIONALS", raising=False)
    if source == "env":
        monkeypatch.setenv("ANSIBLE_ALLOW_BROKEN_CONDITIONALS", "True")
    else:
        files["ansible.cfg"] = "[defaults]\nallow_broken_conditionals = True\n"
        monkeypatch.setenv("ANSIBLE_CONFIG", str(tmp_path / "ansible.cfg"))
    msgs, _ = check(tmp_path, files)
    assert msgs == ['"EXHAUSTED 1 True"']
    assert (tmp_path / "attempts").read_text().splitlines() == ["attempt"] * 12


@pytest.mark.parametrize("mode", ["linear", "standalone", "strategy"])
@pytest.mark.parametrize("loop", ["", "loop: [first, second]"])
def test_invalid_notify_fails_before_dispatch(tmp_path, mode, loop):
    _write(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          tasks:
            - shell: echo attempt >> attempts
              notify: "{{ missing_notification }}"
              when: false
            - shell: echo attempt >> attempts
              notify: "run {{ missing_notification }}"
              %s
              changed_when: false
              ignore_errors: true
            - debug: msg=AFTER_NOTIFY
        """ % loop})
    out = _run_mode(tmp_path, mode)
    output = out.stdout + out.stderr
    assert out.returncode == 0, output
    assert "missing_notification" in output
    assert not (tmp_path / "attempts").exists()
    assert _summary(output) == (['"AFTER_NOTIFY"'], ("2", "0", "0", "0", "1", "0", "1"))


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


def test_strategy_skips_filtered_setup(tmp_path, monkeypatch):
    _reject_setup_become(tmp_path, monkeypatch)
    _write(tmp_path, {"pb.yml": SETUP_FAILURE.format(ignore="")})
    args = ["--skip-tags", "always"]
    expected = _ansible(tmp_path, args)
    assert _ansible(tmp_path, args, "ansiroll.core.ansiroll") == expected
    assert expected == (['"AFTER_SETUP"', '"NEXT_PLAY"'], ("2", "0", "0", "0", "0", "0", "0"))


def test_setup_failure_uses_play_ignore_context_and_displays_result(tmp_path, monkeypatch):
    _reject_setup_become(tmp_path, monkeypatch)
    ignore = 'ignore_errors: "{{ ignore_setup_error }}"\n  vars: {ignore_setup_error: true}'
    _write(tmp_path, {"pb.yml": SETUP_FAILURE.format(ignore=ignore)})
    runs = [
        _ansible_run(tmp_path, ()),
        _standalone_run(tmp_path, ()),
        _ansible_run(tmp_path, (), "ansiroll.core.ansiroll"),
    ]
    for out in runs:
        assert out.returncode == 0, out.stdout + out.stderr
        assert "TASK [Gathering Facts]" in out.stdout
        assert "FAILED" in out.stdout
        assert "...ignoring" in out.stdout
        msgs, recap = _summary(out.stdout + out.stderr)
        assert msgs[-2:] == ['"AFTER_SETUP"', '"NEXT_PLAY"']
        assert recap == ("3", "0", "0", "0", "0", "0", "1")


def test_nested_dynamic_role_inherits_parent_params_in_tasks_and_handlers(tmp_path):
    msgs, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              roles:
                - {role: parent, value: parent_param}
            """,
        "roles/parent/tasks/main.yml": """
            - set_fact: {value: fact}
            - include_role: {name: child}
            """,
        "roles/child/tasks/main.yml": """
            - debug: msg="task {{ value }}"
              changed_when: true
              notify: child_handler
            """,
        "roles/child/handlers/main.yml": """
            - name: child_handler
              debug: msg="handler {{ value }}"
            """,
    })
    assert msgs == ['"task parent_param"', '"handler parent_param"']


def test_nested_dynamic_role_handler_resolves_invocation_with_parent_default(tmp_path):
    msgs, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              roles: [parent]
            """,
        "roles/parent/defaults/main.yml": "fallback_value: owner_default",
        "roles/parent/vars/main.yml": "outer_value: outer",
        "roles/parent/tasks/main.yml": """
            - include_role: {name: child}
              vars: {value: "{{ fallback_value }}:{{ outer_value }}"}
            """,
        "roles/child/tasks/main.yml": """
            - debug: msg="task {{ value }}"
              changed_when: true
              notify: run child
            """,
        "roles/child/handlers/main.yml": """
            - name: run {{ role_name }}
              debug: msg="handler {{ value }}"
            """,
    })
    assert msgs == ['"task owner_default:outer"', '"handler owner_default:outer"']


@pytest.mark.parametrize("mode", ["linear", "standalone", "strategy"])
def test_handler_name_uses_play_vars_without_host_facts(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("ANSIBLE_ERROR_ON_MISSING_HANDLER", "False")
    _write(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          vars: {suffix: play}
          tasks:
            - set_fact: {suffix: fact, host_only: target}
            - debug: msg=trigger
              changed_when: true
              notify: [run play, run target]
          handlers:
            - name: run {{ suffix }}
              debug: msg="play handler sees {{ suffix }}"
            - name: run {{ host_only }}
              debug: msg=HOST_HANDLER_RAN
        """})
    out = _run_mode(tmp_path, mode)
    output = out.stdout + out.stderr
    if CORE_VERSION < (2, 12):
        assert out.returncode != 0, output
        assert "'host_only' is undefined" in output
        assert "HOST_HANDLER_RAN" not in output
        return
    assert out.returncode == 0, output
    assert "The requested handler 'run target' was not found" in output
    assert _summary(output)[0] == ['"trigger"', '"play handler sees fact"']


@pytest.mark.parametrize("looped", [False, True])
def test_dynamic_role_handler_keeps_invocation_vars(tmp_path, looped):
    if looped:
        invocation = """
      vars: {value: "from_{{ role_item }}"}
      loop: [first, last]
      loop_control: {loop_var: role_item}
        """.rstrip()
        expected = '"from_last / last"'
    else:
        invocation = '\n      vars: {value: "{{ latest }}"}'
        expected = '"from_include / no item"'
    msgs, _ = check(tmp_path, {
        "pb.yml": """
- hosts: localhost
  gather_facts: false
  vars: {latest: before_include}
  tasks:
    - include_role: {name: r}%s
    - set_fact: {latest: from_include}
    - debug: msg=trigger
      changed_when: true
      notify: run_role_handler
            """ % invocation,
        "roles/r/tasks/main.yml": """
            - debug: msg=ROLE_TASK
            """,
        "roles/r/handlers/main.yml": """
            - name: run_role_handler
              debug:
                msg: "{{ value | default('missing') }} / {{ role_item | default('no item') }}"
            """,
    })
    assert msgs[-1] == expected


def test_same_named_dynamic_handler_respects_binding_version(tmp_path):
    msgs, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              tasks:
                - debug: msg=trigger
                  changed_when: true
                  notify: restart
                - include_role: name=r
              handlers:
                - name: restart
                  debug: msg=PLAY_HANDLER
            """,
        "roles/r/tasks/main.yml": "- debug: msg=ROLE_TASK",
        "roles/r/handlers/main.yml": """
            - name: restart
              debug: msg=ROLE_HANDLER
            """,
    })
    assert msgs[-1] == ('"ROLE_HANDLER"' if CORE_VERSION >= (2, 15) else '"PLAY_HANDLER"')


@pytest.mark.parametrize("error_on_missing", [True, False])
def test_notification_validated_before_role_handler_loads(tmp_path, monkeypatch, error_on_missing):
    monkeypatch.setenv("ANSIBLE_ERROR_ON_MISSING_HANDLER", str(error_on_missing))
    _write(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              vars: {topic: later_handler}
              tasks:
                - debug: msg=trigger
                  changed_when: true
                  notify: "{{ topic }}"
                - include_role: name=r
            """,
        "roles/r/tasks/main.yml": "- debug: msg=ROLE_LOADED",
        "roles/r/handlers/main.yml": """
            - name: later_handler
              debug: msg=HANDLER_RAN
            """,
    })
    # include_role uses the strategy's linear fallback. The additional playbook
    # below exercises the same immediate error through the shipped agent.
    for out in [_ansible_run(tmp_path, ()), _standalone_run(tmp_path, ())]:
        output = out.stdout + out.stderr
        assert "The requested handler 'later_handler' was not found" in output
        assert "HANDLER_RAN" not in output
        assert (out.returncode != 0) == error_on_missing
        assert ('"ROLE_LOADED"' in output) == (not error_on_missing)

    _write(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          tasks:
            - debug: msg=trigger
              changed_when: true
              notify: missing
            - debug: msg=NEXT_TASK
        """})
    for strategy in [None, "ansiroll.core.ansiroll"]:
        out = _ansible_run(tmp_path, (), strategy)
        output = out.stdout + out.stderr
        assert "The requested handler 'missing' was not found" in output
        assert (out.returncode != 0) == error_on_missing
        assert ('"NEXT_TASK"' in output) == (not error_on_missing)


def test_notification_names_resolve_for_each_loop_item(tmp_path):
    msgs, _ = check(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          tasks:
            - command: /bin/true
              loop: [second, first]
              notify: "handle {{ item }}"
          handlers:
            - name: handle first
              debug: msg=FIRST_HANDLER
            - name: handle second
              debug: msg=SECOND_HANDLER
        """})
    assert msgs[-2:] == ['"FIRST_HANDLER"', '"SECOND_HANDLER"']
