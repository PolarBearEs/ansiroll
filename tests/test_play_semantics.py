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
from packaging.version import Version

from tests.run_official_suite import parse_task_trace

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


@pytest.mark.parametrize("skip_always", [False, True])
def test_tag_selected_role_respects_implicit_flush_tag_rules(tmp_path, skip_always):
    handler_runs = not skip_always or Version(__version__) < Version("2.19.4")
    _write(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              roles:
                - {role: tagged, tags: selected}
              post_tasks:
                - name: verify implicit flush
                  assert:
                    that: (role_handler_ran | default(false)) == %s
                  tags: selected
            - hosts: localhost
              gather_facts: false
              tasks:
                - debug: msg="handler ran {{ role_handler_ran | default(false) }}"
                  tags: selected
        """ % str(handler_runs).lower(),
        "roles/tagged/tasks/main.yml": """
            - command: /bin/true
              notify: record handler
        """,
        "roles/tagged/handlers/main.yml": """
            - name: record handler
              set_fact: {role_handler_ran: true}
        """,
    })
    args = ("--tags", "selected")
    if skip_always:
        args += ("--skip-tags", "always")
    expected = _ansible_run(tmp_path, args)
    assert expected.returncode == 0, expected.stdout + expected.stderr
    actual = _ansible_run(tmp_path, args, "ansiroll.core.ansiroll")
    assert actual.returncode == 0, actual.stdout + actual.stderr
    messages, recap = _summary(actual.stdout)
    assert recap == _summary(expected.stdout)[1]
    assert parse_task_trace(actual.stdout) == parse_task_trace(expected.stdout)
    assert messages == ['"handler ran %s"' % handler_runs]


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


def test_play_alias_uses_task_scope(tmp_path):
    msgs, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              vars:
                notification: "handle {{ source }}"
                source: "{{ missing_source }}"
                fact_notification: "{{ missing_fact }}"
                registered: "{{ missing_registered }}"
                extra_notification: "{{ missing_extra }}"
                done: "{{ current.stdout == 'ready' }}"
              handlers:
                - name: handle marker
                  debug: msg=HANDLED
              tasks:
                - debug: msg=task
                  vars: {source: marker}
                  changed_when: true
                  notify: "{{ notification }}"
                - block:
                    - debug: msg=block
                      changed_when: true
                      notify: "{{ notification }}"
                  vars: {source: marker}
                - include_role: {name: scoped}
                - set_fact: {fact_notification: handle marker}
                - command: echo handle marker
                  register: registered
                - debug: msg=facts
                  changed_when: true
                  notify: ["{{ fact_notification }}", "{{ registered.stdout }}",
                           "{{ extra_notification }}"]
                - command: echo from loop
                  loop: [marker]
                  loop_control: {loop_var: source}
                  notify: "{{ notification }}"
                - command: echo ready
                  register: current
                  until: done
                  delay: 0
        """,
        "roles/scoped/vars/main.yml": "source: marker",
        "roles/scoped/tasks/main.yml": """
            - debug: msg=role
              changed_when: true
              notify: "{{ notification }}"
        """,
    }, args=["-e", "extra_notification='handle marker'"])
    assert msgs == ['"task"', '"block"', '"role"', '"facts"', '"HANDLED"']


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


def test_include_tasks_handler_runs_its_body(tmp_path):
    messages, recap = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              tasks:
                - include_role: {name: r}
                - debug: {msg: notifying}
                  changed_when: true
                  notify: ["r : expanded handler", expanded topic, skipped handler]
        """,
        "roles/r/handlers/main.yml": """
            - name: expanded handler
              include_tasks: handler.yml
              listen: expanded topic
              vars: {handler_message: included}
            - name: skipped handler
              include_tasks: handler.yml
              when: false
        """,
        "roles/r/tasks/handler.yml": '- debug: {msg: "{{ handler_message }}"}',
    })
    assert messages == ['"notifying"', '"included"']
    assert recap[4] == "1"


def test_role_loop_registers_later_handlers_before_first_body(tmp_path):
    messages, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              tasks:
                - include_role: {name: r}
                  loop: [first, second]
                  loop_control: {loop_var: invocation}
        """,
        "roles/r/tasks/main.yml": """
            - debug: msg="{{ invocation }}"
              changed_when: true
              notify: run second
              when: invocation == 'first'
        """,
        "roles/r/handlers/main.yml": """
            - name: "run {{ invocation }}"
              debug: msg="handler {{ invocation }}"
        """,
    })
    assert messages == ['"first"', '"handler second"']


@pytest.mark.skipif(CORE_VERSION < (2, 18), reason="break_when was added in ansible-core 2.18")
def test_role_loop_stops_registering_handlers_on_break_when(tmp_path):
    messages, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              tasks:
                - include_role: {name: r}
                  loop: [first, second]
                  loop_control:
                    loop_var: invocation
                    break_when: invocation == 'first'
        """,
        "roles/r/tasks/main.yml": """
            - debug: msg="{{ invocation }}"
              changed_when: true
              notify: run
        """,
        "roles/r/handlers/main.yml": """
            - name: run
              debug: msg="handler {{ invocation }}"
        """,
    })
    assert messages == ['"first"', '"handler first"']


def test_dynamic_role_handler_inherits_apply_directives(tmp_path):
    messages, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              tasks:
                - include_role:
                    name: r
                    apply:
                      when: false
                - debug: msg=notify
                  changed_when: true
                  notify: run
        """,
        "roles/r/handlers/main.yml": "- name: run\n  debug: msg=handler",
    })
    assert messages == ['"notify"']


def test_dynamic_role_handler_resolves_include_params_from_handler_vars(tmp_path):
    messages, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              tasks:
                - include_role: {name: r}
                  vars:
                    message: "{{ suffix }}"
                - debug: msg=notify
                  changed_when: true
                  notify: run
        """,
        "roles/r/handlers/main.yml": """
            - name: run
              vars: {suffix: handler}
              debug: msg="{{ message }}"
        """,
    })
    assert messages == ['"notify"', '"handler"']


def test_expanded_task_handler_passes_apply_vars_to_children(tmp_path):
    messages, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              tasks:
                - include_role: {name: r}
                - debug: msg=notify
                  changed_when: true
                  notify: run
        """,
        "roles/r/handlers/main.yml": """
            - name: run
              include_tasks:
                file: show.yml
                apply:
                  vars: {message: applied}
        """,
        "roles/r/tasks/show.yml": '- debug: {msg: "{{ message }}"}',
    })
    assert messages == ['"notify"', '"applied"']


def test_legacy_child_handler_preserves_unsafe_play_role_defaults(tmp_path):
    if CORE_VERSION >= (2, 15):
        pytest.skip("legacy child handlers infer defaults from play roles")
    messages, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              vars: {sentinel: expanded}
              roles: [parent]
        """,
        "roles/parent/defaults/main.yml": 'literal: !unsafe "{{ sentinel }}"\n',
        "roles/parent/tasks/main.yml": """
            - include_role: {name: child}
        """,
        "roles/child/tasks/main.yml": """
            - debug: {msg: notifying}
              changed_when: true
              notify: child handler
        """,
        "roles/child/handlers/main.yml": """
            - name: child handler
              debug: {msg: "{{ literal }}"}
        """,
    })
    assert messages == ['"notifying"', '"{{ sentinel }}"']


def test_until_successful_uses_failed_when_for_each_loop_item(tmp_path):
    msgs, _ = check(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          tasks:
            - command: /bin/true
              loop: [first, second]
              register: outcome
              failed_when: true
              until: outcome is successful
              retries: 2
              delay: 0
              ignore_errors: true
            - debug: msg="attempts {{ outcome.results | map(attribute='attempts') | join(',') }}"
        """})
    assert msgs == ['"attempts 2,2"']


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


@pytest.mark.skipif(CORE_VERSION < (2, 19), reason="captured task errors start in Core 2.19")
@pytest.mark.parametrize("until", ["", "until: false"])
def test_failed_command_exception_after_retry_exhaustion(tmp_path, monkeypatch, until):
    monkeypatch.setenv("ANSIBLE_DISPLAY_TRACEBACK", "never")
    msgs, _ = check(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          tasks:
            - command: /bin/false
              %s
              retries: 1
              delay: 0
              register: outcome
              ignore_errors: true
            - debug:
                msg: "{{ outcome.attempts }} {{ outcome.failed }} {{ outcome.exception }}"
        """ % until})
    assert msgs == ['"1 True (traceback unavailable)"']


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


@pytest.mark.skipif(CORE_VERSION < (2, 19), reason="boolean results are required from Core 2.19")
@pytest.mark.parametrize("mode", ["linear", "standalone", "strategy"])
@pytest.mark.parametrize("condition", ["missing_condition ~ 'suffix'", "condition_alias"])
def test_until_rejects_undefined_operand_with_broken_conditionals(
        tmp_path, monkeypatch, mode, condition):
    monkeypatch.setenv("ANSIBLE_ALLOW_BROKEN_CONDITIONALS", "True")
    _write(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          vars:
            condition_alias: "{{ missing_condition ~ 'suffix' }}"
          tasks:
            - shell: echo attempt >> attempts
              until: %s
              delay: 0
            - debug: msg=AFTER_UNTIL
        """ % condition})
    out = _run_mode(tmp_path, mode)
    output = out.stdout + out.stderr
    assert out.returncode != 0
    assert "missing_condition" in output
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
            - shell: echo attempt >> attempts
              until: ["", null, "   ", true]
              delay: 0
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
    assert (tmp_path / "attempts").read_text().splitlines() == ["attempt"] * 15


@pytest.mark.parametrize("mode", ["linear", "standalone", "strategy"])
@pytest.mark.parametrize("loop", ["", "loop: [first, second]"])
@pytest.mark.parametrize("notify", [
    "run {{ missing_notification }}", "{{ config.handler }}",
    "{{ notification_value }}", "{{ notification_alias }}",
])
def test_invalid_notify_fails_before_dispatch(tmp_path, mode, loop, notify):
    _write(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          vars:
            config: {handler: "run {{ missing_notification }}"}
            notification_alias: "{{ notification_value }}"
            notification_value: "run {{ missing_notification }}"
          tasks:
            - shell: echo attempt >> attempts
              notify: "{{ missing_notification }}"
              when: false
            - shell: echo attempt >> attempts
              notify: "%s"
              %s
              changed_when: false
              ignore_errors: true
            - debug: msg=AFTER_NOTIFY
        """ % (notify, loop)})
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


@pytest.mark.parametrize("fast_facts", ["0", "1"])
def test_implicit_setup_honors_play_gather_options(tmp_path, monkeypatch, fast_facts):
    monkeypatch.setenv("ANSIROLL_FAST_FACTS", fast_facts)
    msgs, _ = check(tmp_path, {
        "facts/example.fact": '{"marker": "custom facts directory"}',
        "pb.yml": """
        - hosts: localhost
          gather_facts: true
          gather_subset: ['!all', '!min', local]
          gather_timeout: 10
          fact_path: "{{ playbook_dir }}/facts"
          tasks:
            - debug:
                msg: "{{ ansible_facts.distribution is defined }} / {{ ansible_local.example.marker }}"
        """,
    })
    assert msgs == ['"False / custom facts directory"']


@pytest.mark.skipif(CORE_VERSION < (2, 19), reason="requires omit support on play keywords")
@pytest.mark.parametrize("subset, expected", [
    ('"{{ omit }}"', True),
    ("['!all', '!min', '{{ omit }}']", False),
])
def test_implicit_setup_ignores_omitted_gather_options(tmp_path, subset, expected):
    msgs, _ = check(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: true
          gather_subset: SUBSET
          gather_timeout: "{{ omit }}"
          tasks:
            - debug:
                msg: "distribution defined: {{ ansible_facts.distribution is defined }}"
    """.replace("SUBSET", subset)})
    assert msgs == ['"distribution defined: %s"' % expected]


def test_compile_implicit_setup_falsey_options_follow_core_version(tmp_path):
    from ansiroll.loader import parse_playbook

    _write(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: true
          gather_timeout: 0
          fact_path: ''
    """})
    plays, _ = parse_playbook(str(tmp_path / "pb.yml"))
    options = plays[0]["gather_options"]
    expected = {} if CORE_VERSION < (2, 13) else {"gather_timeout": 0, "fact_path": ""}
    assert {key: value for key, value in options.items() if key != "gather_subset"} == expected


@pytest.mark.skipif(CORE_VERSION >= (2, 18), reason="requires legacy gather subset configuration")
def test_legacy_implicit_setup_omitted_subset_uses_controller_default(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIBLE_GATHER_SUBSET", "!all,!min")
    msgs, _ = check(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: true
          gather_subset: "{{ omit }}"
          tasks:
            - debug:
                msg: "distribution defined: {{ ansible_facts.distribution is defined }}"
    """})
    assert msgs == ['"distribution defined: False"']


def test_implicit_setup_vars_see_runtime_extra_vars(tmp_path, monkeypatch):
    _write(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: true
          gather_subset: "{{ vars.requested_subset }}"
          vars:
            requested_subset: [all]
          tasks:
            - debug:
                msg: "distribution defined: {{ ansible_facts.distribution is defined }}"
    """})
    extra_vars = json.dumps({"requested_subset": ["!all", "!min"]})
    args = ["-e", extra_vars]
    expected = _ansible(tmp_path, args)
    assert expected[0] == ['"distribution defined: False"']
    assert _ansible(tmp_path, args, "ansiroll.core.ansiroll") == expected
    monkeypatch.setenv("ANSIROLL_EXTRA_VARS", extra_vars)
    assert _standalone(tmp_path, []) == expected


def test_implicit_setup_find_vars_files_before_gathering(tmp_path, monkeypatch):
    _write(tmp_path, {
        "vars/custom.yml": "facts_dir: custom-facts\n",
        "custom-facts/example.fact": '{"marker": "from vars file"}',
        "pb.yml": """
        - hosts: localhost
          gather_facts: true
          gather_subset: ['!all', '!min', local]
          vars_files: ['vars/{{ fact_profile }}.yml']
          fact_path: "{{ ansible_search_path[-1] }}/{{ facts_dir }}"
          tasks:
            - debug:
                msg: "marker={{ ansible_local.example.marker }}"
        """,
    })
    extra_vars = json.dumps({"fact_profile": "custom"})
    args = ["-e", extra_vars]
    expected = _ansible(tmp_path, args)
    assert expected[0] == ['"marker=from vars file"']
    assert _ansible(tmp_path, args, "ansiroll.core.ansiroll") == expected
    monkeypatch.setenv("ANSIROLL_EXTRA_VARS", extra_vars)
    assert _standalone(tmp_path, []) == expected


def test_implicit_setup_subset_uses_public_role_defaults_before_host_facts(tmp_path):
    msgs, _ = check(tmp_path, {
        "roles/defaults/defaults/main.yml": "requested_subset: ['!all', '!min']\n",
        "roles/defaults/tasks/main.yml": "[]\n",
        "pb.yml": """
        - hosts: localhost
          gather_facts: false
          tasks:
            - set_fact:
                requested_subset: [all]
        - hosts: localhost
          gather_facts: true
          gather_subset: "{{ requested_subset }}"
          roles: [defaults]
          tasks:
            - debug:
                msg: "distribution defined: {{ ansible_facts.distribution is defined }}"
        """,
    })
    assert msgs == ['"distribution defined: False"']


def test_standalone_setup_subset_uses_baked_controller_environment(tmp_path, monkeypatch):
    _write(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: true
          gather_subset: "{{ lookup('env', 'ANSIROLL_SETUP_SUBSET').split(',') }}"
          tasks:
            - debug:
                msg: "distribution defined: {{ ansible_facts.distribution is defined }}"
    """})
    monkeypatch.setenv("ANSIROLL_SETUP_SUBSET", "!all,!min")
    expected = _ansible(tmp_path, [])
    assert expected[0] == ['"distribution defined: False"']
    output = tmp_path / "out.py"
    subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", "pb.yml", "-o", str(output)],
        cwd=str(tmp_path), env=_env(), capture_output=True, check=True,
    )
    monkeypatch.delenv("ANSIROLL_SETUP_SUBSET")
    result = subprocess.run([sys.executable, str(output)], cwd=str(tmp_path), env=_env(),
                            capture_output=True, text=True, check=True)
    assert _summary(result.stdout) == expected


def test_implicit_setup_uses_play_name_role_vars_and_search_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIBLE_HASH_BEHAVIOUR", "merge")
    msgs, _ = check(tmp_path, {
        "roles/scope/defaults/main.yml": "fact_profile: custom\nprofile: {subset: ['!all', '!min', local]}\n",
        "roles/scope/tasks/main.yml": "[]\n",
        "vars/custom.yml": "facts_dir: actual-facts\n",
        "files/facts-dir.txt": str(tmp_path) + "\n",
        "facts-dir.txt": str(tmp_path / "wrong-directory") + "\n",
        "actual-facts/example.fact": '{"marker": "role-selected facts"}',
        "pb.yml": """
        - name: select facts
          hosts: localhost
          gather_facts: true
          gather_subset: "{{ profile.subset if ansible_play_name == 'select facts' else ['all'] }}"
          fact_path: "{{ lookup('file', 'facts-dir.txt') ~ '/' ~ facts_dir }}"
          vars_files: ['vars/{{ fact_profile }}.yml']
          vars:
            profile: {timeout: 10}
          roles: [scope]
          tasks:
            - debug:
                msg: "{{ ansible_facts.distribution is defined }} / {{ ansible_local.example.marker }}"
        """,
    })
    assert msgs == ['"False / role-selected facts"']


def test_implicit_setup_preserves_vars_file_order_in_play_scope(tmp_path, monkeypatch):
    _write(tmp_path, {
        "vars/early.yml": "requested_subset: [all]\n",
        "vars/final.yml": "requested_subset: ['!all', '!min']\n",
        "files/setup-switch.txt": "enabled\n",
        "setup-switch.txt": "disabled\n",
        "pb.yml": """
        - hosts: localhost
          gather_facts: true
          gather_subset: "{{ requested_subset if ansible_version.major == 2 and lookup('file', 'setup-switch.txt') == 'enabled' else ['all'] }}"
          vars_files: ['vars/{{ setup_profile }}.yml', vars/final.yml]
          tasks:
            - debug: {msg: "distribution defined: {{ ansible_facts.distribution is defined }}"}
        """,
    })
    extra = json.dumps({"setup_profile": "early"})
    expected = _ansible(tmp_path, ["-e", extra])
    assert expected[0] == ['"distribution defined: False"']
    monkeypatch.setenv("ANSIROLL_EXTRA_VARS", extra)
    assert _standalone(tmp_path, []) == expected


@pytest.mark.parametrize("gather_facts", ["true", "false", "cached"])
@pytest.mark.parametrize("keyword, value, error_text", [
    ("gather_subset", '"{{ nonexistent_subset }}"', "nonexistent_subset"),
    ("gather_subset", "[all, 1]", "gather_subset"),
    pytest.param("gather_timeout", "2.5", "gather_timeout", marks=pytest.mark.skipif(
        CORE_VERSION < (2, 18), reason="older Core truncates floating timeouts")),
])
def test_invalid_gather_keyword_aborts_before_ignore_errors(
        tmp_path, monkeypatch, gather_facts, keyword, value, error_text):
    first_play = ""
    if gather_facts == "cached":
        monkeypatch.setenv("ANSIBLE_GATHERING", "smart")
        first_play = "- hosts: localhost\n  gather_facts: true\n  gather_subset: ['!all', '!min']\n"
        gather_facts = "true"
    _write(tmp_path, {"pb.yml": first_play + f"""
- hosts: localhost
  gather_facts: {gather_facts}
  ignore_errors: true
  {keyword}: {value}
  tasks:
    - debug: {{msg: reached_after_invalid_setup}}
"""})
    assert _ansible_run(tmp_path, []).returncode != 0
    result = _standalone_run(tmp_path, [])
    assert result.returncode != 0
    assert error_text in result.stderr
    assert "reached_after_invalid_setup" not in result.stdout


def test_setup_ignores_previous_play_vars(tmp_path):
    msgs, _ = check(tmp_path, {
        "roles/scope/defaults/main.yml": "facts_dir: correct-facts\n",
        "roles/scope/tasks/main.yml": "[]\n",
        "correct-facts/example.fact": '{"marker": "role default"}',
        "wrong-facts/example.fact": '{"marker": "previous play"}',
        "pb.yml": """
        - hosts: localhost
          gather_facts: false
          vars:
            facts_dir: wrong-facts
          tasks:
            - debug: {msg: "{{ facts_dir }}"}
        - hosts: localhost
          gather_facts: true
          gather_subset: ['!all', '!min', local]
          fact_path: "{{ playbook_dir }}/{{ facts_dir }}"
          roles: [scope]
          tasks:
            - debug: {msg: "{{ ansible_local.example.marker }}"}
        """,
    })
    assert msgs == ['"wrong-facts"', '"role default"']


def test_setup_reselects_vars_file_with_runtime_extra_vars(tmp_path, monkeypatch):
    _write(tmp_path, {
        "vars/dev.yml": "requested_subset: [all]\n",
        "vars/prod.yml": "- requested_subset: [all]\n- requested_subset: ['!all', '!min']\n",
        "pb.yml": """
        - hosts: localhost
          gather_facts: true
          gather_subset: "{{ requested_subset }}"
          vars_files: ['vars/{{ setup_profile }}.yml']
          vars:
            setup_profile: dev
          tasks:
            - debug: {msg: "distribution defined: {{ ansible_facts.distribution is defined }}"}
        """,
    })
    extra = json.dumps({"setup_profile": "prod"})
    expected = _ansible(tmp_path, ["-e", extra])
    assert expected[0] == ['"distribution defined: False"']
    monkeypatch.setenv("ANSIROLL_EXTRA_VARS", extra)
    assert _standalone(tmp_path, []) == expected


def test_setup_keywords_see_check_and_diff_mode(tmp_path):
    msgs, _ = check(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: true
          gather_subset: "{{ ['all'] if ansible_check_mode or ansible_diff_mode else ['!all', '!min'] }}"
          tasks:
            - debug: {msg: "distribution defined: {{ ansible_facts.distribution is defined }}"}
        """})
    assert msgs == ['"distribution defined: False"']


def test_setup_vars_file_replay_uses_controller_search_path_and_magic(tmp_path):
    msgs, _ = check(tmp_path, {
        "files/profile.txt": "facts-profile\n",
        "vars/facts-profile.yml": "subset: ['!all', '!min']\n",
        "vars/shadowed.yml": "subset: [all]\n",
        "vars/checked.yml": "subset: [all]\n",
        "pb.yml": """
        - name: actual play
          hosts: localhost
          check_mode: true
          gather_facts: true
          gather_subset: "{{ subset }}"
          vars_files:
            - "vars/{{ 'checked' if ansible_check_mode else lookup('file', 'profile.txt') if ansible_play_name == 'actual play' else 'shadowed' }}.yml"
          tasks:
            - debug: {msg: "distribution defined: {{ ansible_facts.distribution is defined }}"}
        """,
    }, args=("-e", "ansible_play_name=shadowed"), strategy=False)
    assert msgs == ['"distribution defined: False"']


def test_setup_fact_path_sees_empty_fact_namespace(tmp_path):
    msgs, _ = check(tmp_path, {
        "facts/example.fact": '{"marker": "default path"}',
        "pb.yml": """
        - hosts: localhost
          gather_facts: true
          gather_subset: ['!all', '!min', local]
          fact_path: "{{ playbook_dir }}/{{ ansible_facts.get('profile', 'facts') }}"
          tasks:
            - debug: {msg: "{{ ansible_local.example.marker }}"}
        """,
    }, strategy=False)
    assert msgs == ['"default path"']


@pytest.mark.parametrize("fallback", ["", " | default('missing')"])
def test_setup_uses_baked_vars_when_controller_lookup_file_is_missing(tmp_path, fallback):
    _write(tmp_path, {
        "files/profile.txt": "minimal\n",
        "vars/minimal.yml": "subset: ['!all', '!min']\n",
        "pb.yml": """
        - hosts: localhost
          gather_facts: true
          gather_subset: "{{ subset }}"
          vars_files: ["vars/{{ lookup('file', playbook_dir ~ '/files/profile.txt')%s }}.yml"]
          tasks:
            - debug: {msg: "distribution defined: {{ ansible_facts.distribution is defined }}"}
        """ % fallback,
    })
    output = tmp_path / "out.py"
    subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", "pb.yml", "-o", str(output)],
        cwd=str(tmp_path), env=_env(), capture_output=True, check=True,
    )
    (tmp_path / "files/profile.txt").unlink()
    result = subprocess.run([sys.executable, str(output)], cwd=str(tmp_path),
                            env=_env(), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert _summary(result.stdout)[0] == ['"distribution defined: False"']


def test_setup_fact_path_uses_standalone_inventory_magic(tmp_path, monkeypatch):
    _write(tmp_path, {
        "localhost/example.fact": '{"marker": "host path"}',
        "pb.yml": """
        - hosts: localhost
          gather_facts: true
          gather_subset: ['!all', '!min', local]
          fact_path: >-
            {{ playbook_dir }}/{{ inventory_hostname_short
            if group_names is defined and ansible_play_hosts_all == [inventory_hostname]
            and ansible_play_hosts == ansible_play_batch and play_hosts == ansible_play_batch else 'missing' }}
          tasks:
            - debug: {msg: "{{ ansible_local.example.marker }}"}
        """,
    })
    expected = _ansible(tmp_path, [])
    assert expected[0] == ['"host path"']
    monkeypatch.setenv("ANSIROLL_HOST", "localhost.example.test")
    assert _standalone(tmp_path, []) == expected


def test_setup_applies_static_vars_files_in_hash_merge_order(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIBLE_HASH_BEHAVIOUR", "merge")
    msgs, _ = check(tmp_path, {
        "group_vars/all.yml": "profile: {directory: wrong}\n",
        "vars/final.yml": "profile: {other: value}\n",
        "correct/example.fact": '{"marker": "correct merge"}',
        "wrong/example.fact": '{"marker": "wrong merge"}',
        "pb.yml": """
        - hosts: localhost
          gather_facts: true
          gather_subset: ['!all', '!min', local]
          vars:
            profile: cleared
          vars_files: [vars/final.yml]
          fact_path: "{{ playbook_dir }}/{{ profile.directory | default('correct') }}"
          tasks:
            - debug: {msg: "{{ ansible_local.example.marker }}"}
        """,
    }, strategy=False)
    assert msgs == ['"correct merge"']


def test_setup_reselection_preserves_standalone_task_includes(tmp_path, monkeypatch):
    from ansiroll.compiler import _shadowed_var_names
    from ansiroll.loader import parse_playbook

    _write(tmp_path, {
        "vars/dev.yml": "task_file: dev.yml\nrequested_subset: [all]\n",
        "vars/prod.yml": "task_file: prod.yml\nrequested_subset: ['!all', '!min']\n",
        "dev.yml": "- debug: {msg: dev}\n",
        "prod.yml": "- debug: {msg: prod}\n",
        "stable.yml": "- debug: {msg: stable}\n",
        "pb.yml": """
        - hosts: localhost
          gather_facts: true
          gather_subset: "{{ requested_subset }}"
          vars:
            profile: dev
            stable_file: stable.yml
          vars_files: ['vars/{{ profile }}.yml']
          tasks:
            - include_tasks: "{{ task_file }}"
            - include_tasks: "{{ stable_file }}"
            - debug: {msg: "distribution defined: {{ ansible_facts.distribution is defined }}"}
        """,
    })
    plays, _ = parse_playbook(str(tmp_path / "pb.yml"))
    assert "requested_subset" in _shadowed_var_names(plays)
    assert _standalone(tmp_path, [])[0] == ['"dev"', '"stable"', '"distribution defined: True"']
    monkeypatch.setenv("ANSIROLL_EXTRA_VARS", json.dumps({"profile": "prod"}))
    assert _standalone(tmp_path, [])[0] == ['"dev"', '"stable"', '"distribution defined: False"']


def test_setup_sees_cached_local_facts_when_fact_injection_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIBLE_INJECT_FACT_VARS", "false")
    msgs, _ = check(tmp_path, {
        "first/profile.fact": '{"next": "second"}',
        "second/example.fact": '{"marker": "second path"}',
        "bad/example.fact": '{"marker": "wrong path"}',
        "pb.yml": """
        - hosts: localhost
          gather_facts: true
          fact_path: "{{ playbook_dir }}/first"
        - hosts: localhost
          gather_facts: true
          gather_subset: ['!all', '!min', local]
          fact_path: "{{ playbook_dir }}/{{ 'bad' if ansible_distribution is defined else ansible_facts.ansible_local.profile.next }}"
          tasks:
            - debug: {msg: "{{ ansible_local.example.marker }}"}
        """,
    }, strategy=False)
    assert msgs == ['"second path"']


def test_setup_vars_file_can_read_shadowed_inventory_selector(tmp_path):
    messages, _ = check(tmp_path, {
        "group_vars/all.yml": "setup_profile: inventory\n",
        "vars/inventory.yml": "facts_dir: actual-facts\n",
        "actual-facts/example.fact": '{"marker": "inventory selected"}',
        "pb.yml": """
        - hosts: localhost
          gather_facts: false
          tasks:
            - set_fact: {setup_profile: wrong}
        - hosts: localhost
          gather_facts: true
          gather_subset: ['!all', '!min', local]
          vars_files: ['vars/{{ setup_profile }}.yml']
          fact_path: "{{ playbook_dir }}/{{ facts_dir }}"
          tasks:
            - debug: {msg: "marker={{ ansible_local.example.marker }}"}
        """,
    }, strategy=False)
    assert messages == ['"marker=inventory selected"']


def test_strategy_setup_uses_controller_skip_result(tmp_path):
    _write(tmp_path, {
        "pb.yml": """
        - import_playbook: child.yml
          when: ansible_search_path is not defined
        """,
        "child.yml": """
        - hosts: localhost
          gather_facts: true
          gather_subset: ['!all', '!min']
          tasks:
            - debug: {msg: skipped}
        """,
    })
    expected = _ansible_run(tmp_path, [])
    actual = _ansible_run(tmp_path, [], "ansiroll.core.ansiroll")
    assert expected.returncode == actual.returncode == 0
    assert _summary(expected.stdout) == _summary(actual.stdout)


def test_implicit_setup_fact_path_uses_controller_task_vars(tmp_path):
    _write(tmp_path, {
        "location.yml": "facts_dir: actual-facts\n",
        "location.txt": str(tmp_path) + "\n",
        "actual-facts/example.fact": '{"marker": "controller value"}',
        "pb.yml": """
        - hosts: localhost
          gather_facts: false
          tasks:
            - include_vars: location.yml
        - hosts: localhost
          gather_facts: true
          gather_subset: ['!all', '!min', local]
          vars:
            facts_dir: wrong-facts
          fact_path: "{{ lookup('file', playbook_dir + '/location.txt') }}/{{ facts_dir }}"
          tasks:
            - debug:
                msg: "marker={{ ansible_local.example.marker }}"
        """,
    })
    expected = _ansible(tmp_path, [])
    assert expected[0] == ['"marker=controller value"']
    assert _ansible(tmp_path, [], "ansiroll.core.ansiroll") == expected


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
          vars:
            suffix: "{{ alias }}"
            alias: play
          tasks:
            - set_fact: {suffix: fact, host_only: target}
            - debug: msg=trigger
              changed_when: true
              notify: [run PLAY, run TARGET]
          handlers:
            - name: run {{ suffix | upper }}
              debug: msg="play handler sees {{ suffix }}"
            - name: run {{ host_only | upper }}
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
    assert "The requested handler 'run TARGET' was not found" in output
    assert _summary(output)[0] == ['"trigger"', '"play handler sees fact"']


def test_handler_name_can_explicitly_read_live_hostvars(tmp_path):
    _write(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          tasks:
            - set_fact: {suffix: live}
            - debug: msg=trigger
              changed_when: true
              notify: run live
          handlers:
            - name: run {{ hostvars['localhost'].suffix }}
              debug: msg=handler
        """})
    expected = _ansible(tmp_path, [])
    assert expected[0] == ['"trigger"', '"handler"']
    assert _ansible(tmp_path, [], "ansiroll.core.ansiroll") == expected


@pytest.mark.skipif(CORE_VERSION < (2, 19), reason="modern template trust and handler matching")
def test_handler_matching_skips_render_errors_and_respects_unsafe_names(tmp_path):
    messages, _ = check(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          tasks:
            - debug: msg=trigger
              changed_when: true
              notify: [run good, literal topic]
          handlers:
            - name: "run {{ 1 // 0 }}"
              debug: msg=broken
            - name: !unsafe "run {{ 'good' }}"
              listen: literal topic
              debug: msg=literal
            - name: run good
              debug: msg=good
    """})
    assert messages == ['"trigger"', '"literal"', '"good"']


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


def test_duplicate_handler_names_follow_block_order(tmp_path):
    msgs, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              roles: [static_role]
              handlers:
                - name: direct
                  debug: msg=PLAY_FIRST
                - name: direct
                  debug: msg=PLAY_SECOND
                - name: listener
                  listen: topic
                  debug: msg=PLAY_LISTENER_FIRST
                - name: listener
                  listen: topic
                  debug: msg=PLAY_LISTENER_SECOND
              tasks:
                - name: Notify play handler
                  debug: msg=NOTIFY_PLAY
                  changed_when: true
                  notify: direct
                - meta: flush_handlers
                - include_role: {name: dynamic_role}
                - name: Notify dynamic handler and listeners
                  debug: msg=NOTIFY_DYNAMIC
                  changed_when: true
                  notify: [direct, topic]
        """,
        "roles/static_role/handlers/main.yml": """
            - name: direct
              debug: msg=STATIC_DIRECT
            - name: listener
              listen: topic
              debug: msg=STATIC_LISTENER
        """,
        "roles/dynamic_role/handlers/main.yml": """
            - name: direct
              debug: msg=DYNAMIC_FIRST
            - name: direct
              debug: msg=DYNAMIC_SECOND
            - name: listener
              listen: topic
              debug: msg=DYNAMIC_LISTENER_FIRST
            - name: listener
              listen: topic
              debug: msg=DYNAMIC_LISTENER_SECOND
        """,
    })
    expected = ['"NOTIFY_PLAY"', '"PLAY_FIRST"', '"NOTIFY_DYNAMIC"']
    if CORE_VERSION < (2, 15):
        expected += ['"STATIC_LISTENER"', '"PLAY_LISTENER_FIRST"', '"PLAY_LISTENER_SECOND"']
    expected += ['"DYNAMIC_FIRST"', '"DYNAMIC_LISTENER_FIRST"']
    if CORE_VERSION < (2, 15):
        expected.append('"DYNAMIC_LISTENER_SECOND"')
    assert msgs == expected


def test_tree_shake_preserves_dynamic_role_handlers(tmp_path):
    _write(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              vars:
                shadowed: play
              tasks:
                - include_tasks: nested.yml
                - debug: msg=BEFORE_NOTIFY
                - debug:
                    msg: notifying
                  changed_when: true
                  notify: [ping handler, slurp handler, disabled handler]
                - meta: flush_handlers
                - debug:
                    msg: "{{ ping_result.ping }} {{ slurp_result.content | b64decode }} {{ shadowed }}"
        """,
        "nested.yml": "- include_role: {name: dynamic_role}",
        "roles/dynamic_role/handlers/main.yml": """
            - name: ping handler
              ping: {data: PONG}
              register: ping_result
            - name: slurp handler
              slurp:
                src: "{{ fixture_path }}"
              register: slurp_result
            - name: disabled handler
              find: {paths: .}
              when: disabled
            - name: unused handler
              set_fact: {shadowed: handler}
        """,
        "payload.txt": "PAYLOAD",
    })
    args = ["-e", json.dumps({"fixture_path": str(tmp_path / "payload.txt"), "disabled": False})]
    expected = _ansible_run(tmp_path, args)
    assert expected.returncode == 0, expected.stdout + expected.stderr
    actual = _standalone_run(tmp_path, args, [
        "--tree-shake", "--freeze-playbook-vars", "--manifest", "manifest.json",
    ])
    assert actual.returncode == 0, actual.stdout + actual.stderr
    assert _summary(actual.stdout) == _summary(expected.stdout)
    handler_calls = re.findall(r"^RUNNING HANDLER \[([^\]]+)\]", expected.stdout, re.M)
    assert [name.split(" : ")[-1] for name in handler_calls] == [
        "ping handler", "slurp handler", "disabled handler",
    ]
    trace = parse_task_trace(actual.stdout)
    for name, status in (("ping handler", "ok"), ("slurp handler", "ok"),
                         ("disabled handler", "skipped")):
        assert trace[(name, "localhost", status)] == 1
    assert '"PONG PAYLOAD play"' in _summary(actual.stdout)[0]
    assert "unused handler" not in actual.stdout
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert any(name.split(".")[-1] == "ping" for name in manifest["modules"])
    assert any(name.split(".")[-1] == "slurp" for name in manifest["accelerated_only_modules"])
    assert any(name.split(".")[-1] == "find" for name in manifest["tree_shake"]["removed_modules"])
    assert "shadowed" in manifest["tree_shake"]["skipped_playbook_vars"]


def test_tree_shake_preserves_unresolved_handler_condition_before_false(tmp_path):
    _write(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              tasks:
                - include_role: {name: r}
                - debug: {msg: notifying}
                  changed_when: true
                  notify: conditional handler
        """,
        "roles/r/handlers/main.yml": """
            - name: conditional handler
              debug: {msg: unexpected}
              when: [missing_flag, false]
        """,
    })
    for result in (_ansible_run(tmp_path, ()),
                   _standalone_run(tmp_path, (), ["--tree-shake", "--freeze-playbook-vars"])):
        assert result.returncode != 0
        assert "missing_flag" in result.stdout + result.stderr


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


@pytest.mark.skipif(CORE_VERSION < (2, 19), reason="modern notification template trust")
def test_notify_respects_unsafe_literals_and_null_results(tmp_path):
    messages, _ = check(tmp_path, {"pb.yml": """
        - hosts: localhost
          gather_facts: false
          vars: {suffix: rendered}
          tasks:
            - debug: {msg: no notification}
              changed_when: true
              notify: "{{ none }}"
            - debug: {msg: literal notification}
              changed_when: true
              notify: !unsafe "run {{ suffix }}"
          handlers:
            - name: !unsafe "run {{ suffix }}"
              debug: {msg: literal handler}
            - name: run rendered
              debug: {msg: wrong handler}
    """})
    assert messages == ['"no notification"', '"literal notification"', '"literal handler"']


def test_role_loop_registers_only_matching_items(tmp_path):
    messages, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              vars: {item: allowed}
              tasks:
                - include_role: {name: r}
                  vars: {suffix: local, message: "{{ suffix }}"}
                  loop: [allowed, skipped]
                  when: item != 'skipped'
                - debug: {msg: trigger}
                  changed_when: true
                  notify: show item
        """,
        "roles/r/handlers/main.yml": """
            - name: show item
              debug: {msg: "{{ item }} / {{ message }}"}
        """,
    }, args=["-e", "suffix=extra"])
    assert messages == ['"trigger"', '"allowed / extra"']


def test_nested_role_include_keeps_enclosing_loop_binding(tmp_path):
    messages, _ = check(tmp_path, {
        "pb.yml": """
            - hosts: localhost
              gather_facts: false
              tasks:
                - include_tasks: outer.yml
                  vars: {entry: declaration}
                  loop: [live]
                  loop_control: {loop_var: entry}
        """,
        "outer.yml": "- include_role: {name: nested}",
        "roles/nested/tasks/main.yml": '- debug: {msg: "{{ entry }}"}',
    })
    assert messages == ['"live"']
