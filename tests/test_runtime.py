"""Unit tests for the runtime interpreter (when/loop/register/blocks/handlers/stats).

These use only the runtime "special" actions (debug/set_fact/fail/assert) and
template/copy-with-content, so no Ansible or embedded modules are required.
"""

import io
import os
import shlex
from types import SimpleNamespace

import pytest

from ansiroll.runtime import Runtime, UntrustedConditional, _parse_output


def _task(uuid, name, special, **kw):
    node = {"type": "task", "uuid": uuid, "name": name, "action": special,
            "special": special, "args": {}, "when": [], "loop": None,
            "loop_var": "item", "vars": {}}
    node.update(kw)
    return node


def run(tasks, host_vars=None, handlers=None, gather=False):
    frames = []
    rt = Runtime({}, "", host_vars or {"inventory_hostname": "h"}, on_result=frames.append)
    play = {"tasks": tasks, "handlers": handlers or [], "gather_facts": gather, "vars": {}}
    rt.run_play(play)
    return rt, frames


def by_name(frames, name):
    return [f for f in frames if f["task"] == name]


def test_debug_and_set_fact_register():
    tasks = [
        _task("1", "sf", "set_fact", args={"foo": "{{ 1 + 1 }}"}),
        _task("2", "show", "debug", args={"msg": "foo is {{ foo }}"}),
    ]
    rt, frames = run(tasks)
    assert by_name(frames, "show")[0]["result"]["msg"] == "foo is 2"
    assert rt.stats["ok"] == 2


def test_debug_result_is_always_visible_to_ansible_callbacks():
    _rt, frames = run([
        _task("1", "show", "debug", args={"msg": "Tagged_task"}),
    ])

    assert by_name(frames, "show")[0]["result"] == {
        "changed": False,
        "msg": "Tagged_task",
        "_ansible_verbose_always": True,
    }


def test_native_only_runtime_rejects_controller_fallback_without_calling_it():
    frames = []
    rt = Runtime(
        {}, "", {"inventory_hostname": "h"},
        on_result=frames.append, native_only=True,
    )

    def unexpected_delegate(*_args, **_kwargs):
        raise AssertionError("controller fallback must not run")

    rt.delegate_fn = unexpected_delegate
    task = _task(
        "1", "unsupported task", None,
        action="example.unsupported",
        fallback=True,
        fallback_reason="module could not be embedded",
    )
    rt.run_play({
        "tasks": [task], "handlers": [], "gather_facts": False, "vars": {},
    })

    frame = by_name(frames, "unsupported task")[0]
    assert frame["status"] == "failed"
    assert "native-only mode rejected task" in frame["result"]["msg"]
    assert frame["provenance"] == {
        "mode": "controller-fallback",
        "reason": "module could not be embedded",
    }


def test_when_skips():
    tasks = [_task("1", "skip", "debug", args={"msg": "no"}, when=["false"])]
    rt, frames = run(tasks)
    assert frames[0]["status"] == "skipped"
    assert rt.stats["skipped"] == 1


def test_task_start_is_emitted_before_result():
    events = []
    rt = Runtime({}, "", {"inventory_hostname": "h"}, on_result=lambda f: events.append(("result", f["task"])))
    rt.on_task_start = lambda f: events.append(("start", f["task"]))
    play = {
        "tasks": [_task("1", "sf", "set_fact", args={"foo": "bar"})],
        "handlers": [],
        "gather_facts": False,
        "vars": {},
    }
    rt.run_play(play)
    assert events == [("start", "sf"), ("result", "sf")]


def test_parse_output_adds_command_line_fields():
    result = _parse_output('{"changed": true, "stdout": "a\\nb", "stderr": "warn\\n"}', "", 0)
    assert result["stdout_lines"] == ["a", "b"]
    assert result["stderr_lines"] == ["warn"]


def test_extra_vars_override_play_vars_and_keep_boolean_type():
    frames = []
    rt = Runtime(
        {},
        "",
        {"inventory_hostname": "h", "run_mode": "update"},
        extra_vars={"report_to_slack": False},
        on_result=frames.append,
    )
    play = {
        "tasks": [_task("1", "notify", "debug", args={"msg": "sent"},
                        when=["report_to_slack"])],
        "handlers": [],
        "gather_facts": False,
        "vars": {"report_to_slack": "{{ run_mode != 'build' }}"},
    }
    rt.run_play(play)
    assert rt.vars["report_to_slack"] is False
    assert frames[0]["status"] == "skipped"


def test_set_fact_retains_precedence_over_vars_in_later_plays():
    frames = []
    rt = Runtime(
        {}, "", {"inventory_hostname": "h"}, on_result=frames.append
    )
    rt.run_play({
        "tasks": [
            _task(
                "1", "set host fact", "set_fact",
                args={"ansible_nodename": "from_set_fact"},
            ),
        ],
        "handlers": [],
        "gather_facts": False,
        "vars": {},
    })
    rt.run_play({
        "tasks": [
            _task(
                "2", "check host fact", "assert",
                args={"that": ["ansible_nodename == 'from_set_fact'"]},
            ),
        ],
        "handlers": [],
        "gather_facts": False,
        "vars": {"ansible_nodename": "from_play_vars"},
    })

    assert by_name(frames, "check host fact")[0]["status"] == "ok"
    assert rt.vars["ansible_nodename"] == "from_set_fact"


def test_set_fact_string_results_are_trusted_as_templates():
    tasks = [
        _task(
            "1", "set fact", "set_fact",
            args={"foo": "bar"}, register="baz",
        ),
        _task(
            "2", "verify trusted result", "assert",
            args={
                "that": [
                    "baz.ansible_facts.foo is "
                    "ansible._protomatter.tagged_with('TrustedAsTemplate')"
                ]
            },
        ),
    ]
    rt, frames = run(tasks)
    assert by_name(frames, "verify trusted result")[0]["status"] == "ok"
    assert not rt.failed


def test_special_action_accepts_entire_templated_argument_dictionary():
    tasks = [
        _task(
            "1", "templated debug args", "debug",
            args={"_raw_params": "{{ debug_args }}"},
            vars={"debug_args": {"msg": "hello from mapping"}},
        ),
        _task(
            "2", "templated false assertion", "assert",
            args={"_raw_params": "{{ assert_args }}"},
            vars={"assert_args": {"that": "1 == 0"}},
            ignore_errors=True,
        ),
    ]

    _rt, frames = run(tasks)

    assert by_name(frames, "templated debug args")[0]["result"]["msg"] == (
        "hello from mapping"
    )
    assert by_name(frames, "templated false assertion")[0]["status"] == "failed"


def test_assert_evaluates_indirected_expression_lists():
    tasks = [
        _task(
            "1", "indirected false assertion", "assert",
            args={
                "that": [{
                    "__ansiroll_trusted_condition__":
                        "{{ list_of_false_expressions }}"
                }]
            },
            register="bang",
            failed_when=["bang is not failed"],
        ),
    ]

    _rt, frames = run(
        tasks,
        host_vars={
            "inventory_hostname": "h",
            "list_of_false_expressions": ["1 == 0"],
        },
    )

    assert by_name(frames, "indirected false assertion")[0]["status"] == "ok"


def test_explicitly_untrusted_condition_fails_instead_of_skipping():
    tasks = [
        _task(
            "1", "unsafe condition", "debug",
            when=[{"__ansiroll_untrusted_condition__": "1 == 0"}],
            ignore_errors=True,
            register="result",
        ),
    ]

    _rt, frames = run(tasks)

    frame = by_name(frames, "unsafe condition")[0]
    assert frame["status"] == "failed"
    assert "untrusted template or expression" in frame["result"]["msg"]


def test_indirected_list_preserves_untrusted_condition():
    tasks = [
        _task(
            "1", "unsafe indirect assertion", "assert",
            args={
                "that": [{
                    "__ansiroll_trusted_condition__": "{{ unsafe_conditions }}"
                }]
            },
            vars={
                "unsafe_conditions": [
                    {"__ansiroll_unsafe__": "1 == 0"}
                ]
            },
            ignore_errors=True,
        ),
    ]

    _rt, frames = run(tasks)

    frame = by_name(frames, "unsafe indirect assertion")[0]
    assert frame["status"] == "failed"
    assert "untrusted template or expression" in frame["result"]["msg"]


def test_trusted_condition_rejects_referenced_untrusted_value():
    tasks = [
        _task(
            "1", "unsafe referenced condition", "debug",
            when=[{
                "__ansiroll_trusted_condition__":
                    "{{ untrusted_expression }}"
            }],
            vars={
                "untrusted_expression": {
                    "__ansiroll_unsafe__": "{{ 1 == 0 }}"
                }
            },
            ignore_errors=True,
        ),
    ]

    _rt, frames = run(tasks)

    frame = by_name(frames, "unsafe referenced condition")[0]
    assert frame["status"] == "failed"
    assert "untrusted template or expression" in frame["result"]["msg"]


def test_inline_template_condition_rejects_registered_result():
    tasks = [
        _task(
            "1", "produce result", "set_fact",
            args={"condition_value": True},
            register="produced",
        ),
        _task(
            "2", "consume result unsafely", "debug",
            when=[{
                "__ansiroll_trusted_condition__":
                    "{{ produced.ansible_facts.condition_value }}"
            }],
            ignore_errors=True,
        ),
    ]

    _rt, frames = run(tasks)

    frame = by_name(frames, "consume result unsafely")[0]
    assert frame["status"] == "failed"
    assert "untrusted template or expression" in frame["result"]["msg"]


def test_trusted_inline_template_constant_evaluates_remaining_expression():
    rt = Runtime({}, "", {"inventory_hostname": "h"})

    assert rt.when(
        [{
            "__ansiroll_trusted_condition__":
                "\"{{ 'hi' }}\" == \"hi\""
        }],
        rt.ctx(),
    )


def test_whole_template_returning_expression_string_is_untrusted():
    rt = Runtime({}, "", {"inventory_hostname": "h"})

    with pytest.raises(UntrustedConditional, match="untrusted"):
        rt.when(
            [{
                "__ansiroll_trusted_condition__":
                    "{{ '1 == 1' }}"
            }],
            rt.ctx(),
        )


def test_play_vars_can_read_baked_controller_config():
    frames = []
    rt = Runtime(
        {}, "", {"inventory_hostname": "h"}, on_result=frames.append
    )
    rt.run_play({
        "tasks": [
            _task(
                "1", "check config", "assert",
                args={"that": ["allow_broken_conditionals"]},
            ),
        ],
        "handlers": [],
        "gather_facts": False,
        "controller_config": {"ALLOW_BROKEN_CONDITIONALS": True},
        "vars": {
            "allow_broken_conditionals":
                "{{ lookup('config', 'ALLOW_BROKEN_CONDITIONALS') }}"
        },
    })

    assert by_name(frames, "check config")[0]["status"] == "ok"


@pytest.mark.parametrize(
    ("core_2_20_or_newer", "expected_when_enabled"),
    [(False, False), (True, True)],
)
def test_broken_conditional_config_controls_string_truthiness(
        monkeypatch, core_2_20_or_newer, expected_when_enabled):
    monkeypatch.setattr(
        "ansiroll.runtime._core_at_least",
        lambda *_version: core_2_20_or_newer,
    )
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    disabled = rt.ctx({
        "__ansiroll_controller_config": {
            "ALLOW_BROKEN_CONDITIONALS": False
        }
    })
    enabled = rt.ctx({
        "__ansiroll_controller_config": {
            "ALLOW_BROKEN_CONDITIONALS": True
        }
    })

    assert rt.when(["'broken static'"], disabled) is False
    assert rt.when(["'broken static'"], enabled) is expected_when_enabled


def test_unused_undefined_play_var_does_not_fail_empty_tag_selected_play():
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    play = {
        "tasks": [],
        "handlers": [],
        "gather_facts": False,
        "vars": {
            "unused": "{{ lookup('vars', variable_name) }}",
            "variable_name": "missing_variable",
        },
    }

    rt.run_play(play)

    assert rt.failed is False
    assert rt.stats["failures"] == 0


def test_raw_jinja_block_is_not_templated_twice():
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    rendered = rt.template(
        'docker inspect --format "{% raw %}{{.Config.Image}}{% endraw %}" x',
        rt.ctx({}),
    )
    assert rendered == 'docker inspect --format "{{.Config.Image}}" x'


def test_loop_emits_items_plus_one_task_result():
    tasks = [_task("1", "loop", "debug", args={"msg": "{{ item }}"},
                   loop=["a", "b"])]
    rt, frames = run(tasks)
    items = [f for f in frames if f.get("is_item")]
    summary = [f for f in frames if not f.get("is_item")]
    assert len(items) == 2
    assert len(summary) == 1
    assert rt.stats["ok"] == 1  # the task counts once, like Ansible


def test_loop_when_sees_item_and_task_vars():
    task = _task(
        "1",
        "loop when",
        "debug",
        args={"msg": "{{ item.path }}"},
        loop=[
            {"path": "/etc/apt/sources.list.d/download_docker_com_linux_raspbian.list"},
            {"path": "/etc/apt/sources.list.d/obsolete.list"},
        ],
        when=["item.path | basename not in keep_sources_lists"],
        vars={"keep_sources_lists": ["download_docker_com_linux_raspbian.list"]},
    )
    rt, frames = run([task])
    items = [f for f in frames if f.get("is_item")]
    assert items[0]["status"] == "skipped"
    assert items[1]["status"] == "ok"
    assert rt.stats["ok"] == 1


def test_literal_false_when_short_circuits_undefined_loop_once():
    task = _task(
        "1",
        "short circuit loop",
        "debug",
        args={"msg": "unused"},
        loop="{{ missing_loop_var }}",
        when=[False],
        register="result",
    )
    rt, frames = run([task])
    assert len(frames) == 1
    assert frames[0]["status"] == "skipped"
    assert frames[0]["is_item"] is False
    assert rt.vars["result"]["skipped"] is True


def test_loop_set_fact_is_visible_to_next_iteration():
    tasks = [
        _task(
            "1",
            "accumulate",
            "set_fact",
            args={"acc": "{{ acc + [item] }}"},
            loop=[1, 2, 3],
            vars={"acc": []},
        ),
        _task("2", "show", "debug", args={"msg": "{{ acc }}"}),
    ]
    _rt, frames = run(tasks)
    items = [f["result"]["ansible_facts"]["acc"] for f in frames if f.get("is_item")]
    assert items == [[1], [1, 2], [1, 2, 3]]
    assert by_name(frames, "show")[0]["result"]["msg"] == [1, 2, 3]


def test_loop_control_label_is_added_to_item_results():
    task = _task(
        "1",
        "label",
        "debug",
        args={"msg": "{{ item.secret }}"},
        loop=[{"name": "public", "secret": "hidden"}],
        loop_label="{{ item.name }}",
    )
    _rt, frames = run([task])
    item = [f for f in frames if f.get("is_item")][0]["result"]
    assert item["_ansible_item_label"] == "public"
    assert item["item"] == {"name": "public", "secret": "hidden"}


def test_no_log_censors_emitted_result_but_register_keeps_value():
    tasks = [
        _task("1", "secret", "set_fact", args={"secret_value": "hidden"},
              register="secret_result", no_log=True),
        _task("2", "use", "debug", args={"msg": "{{ secret_value }}"}),
    ]
    rt, frames = run(tasks)
    assert frames[0]["result"] == {
        "censored": "the output has been hidden due to the fact that 'no_log: true' was specified for this result"
    }
    assert rt.vars["secret_value"] == "hidden"
    assert rt.vars["secret_result"]["ansible_facts"]["secret_value"] == "hidden"
    assert frames[1]["result"]["msg"] == "hidden"


def test_sequence_loop_expands_items():
    tasks = [_task("1", "sequence", "debug", args={"msg": "{{ item }}"},
                   loop={"__ansiroll_lookup": "sequence",
                         "terms": "start=1 end=3"})]
    rt, frames = run(tasks)
    items = [f["result"]["item"] for f in frames if f.get("is_item")]
    assert items == ["1", "2", "3"]
    assert rt.stats["ok"] == 1


def test_sequence_loop_supports_shortcut_form():
    tasks = [_task("1", "sequence", "debug", args={"msg": "{{ item }}"},
                   loop={"__ansiroll_lookup": "sequence",
                         "terms": "0x5-0x9/2:%02x"})]
    _rt, frames = run(tasks)
    items = [f["result"]["item"] for f in frames if f.get("is_item")]
    assert items == ["05", "07", "09"]


def test_register_loop_aggregate():
    tasks = [
        _task("1", "loop", "debug", args={"msg": "{{ item }}"}, loop=["a", "b"],
              register="r"),
        _task("2", "show", "debug", args={"msg": "count={{ r.results | length }}"}),
    ]
    rt, frames = run(tasks)
    assert by_name(frames, "show")[0]["result"]["msg"] == "count=2"


def test_changed_when_and_failed_when():
    tasks = [
        _task("1", "cw", "debug", args={"msg": "x"}, changed_when=["true"]),
        _task("2", "fw", "debug", args={"msg": "x"}, failed_when=["false"]),
    ]
    rt, frames = run(tasks)
    assert by_name(frames, "cw")[0]["status"] == "changed"
    assert by_name(frames, "fw")[0]["status"] == "ok"


def test_failed_when_sees_current_registered_result():
    task = _task(
        "1",
        "registered condition",
        "debug",
        args={"msg": [1]},
        register="result",
        failed_when=["result.msg != [1]"],
    )
    rt, frames = run([task], host_vars={
        "inventory_hostname": "h",
        "result": {"msg": "stale"},
    })
    assert frames[0]["status"] == "ok"
    assert rt.vars["result"]["msg"] == [1]


def test_trusted_conditional_template_fallback_keeps_embedded_constants():
    condition = {
        "__ansiroll_trusted_condition__": (
            "{% set a = '{{ 1 }}' %}"
            "{{ a == ('{' ~ '{ 1 }}') }}"
        ),
    }
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    assert rt.when([condition], rt.ctx()) is True


def test_conditional_template_rejects_string_expression_result():
    condition = {"__ansiroll_trusted_condition__": "{{ '1 == 1' }}"}
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    with pytest.raises(UntrustedConditional, match="untrusted template or expression"):
        rt.when([condition], rt.ctx())


def test_debug_fails_when_rendered_argument_contains_captured_undefined():
    task = _task(
        "1",
        "undefined debug",
        "debug",
        args={"msg": "{{ missing }}"},
        ignore_errors=True,
    )
    _rt, frames = run([task])
    assert frames[0]["status"] == "failed"
    assert "missing" in frames[0]["result"]["msg"]


def test_debug_does_not_force_nested_undefined_in_mapping():
    task = _task("1", "lazy debug", "debug", args={"msg": "{{ nested }}"})
    _rt, frames = run([task], host_vars={
        "inventory_hostname": "h",
        "nested": {"unused": "{{ missing }}"},
    })
    assert frames[0]["status"] == "ok"


def test_delegated_debug_remains_a_runtime_special_action():
    task = _task("1", "delegated debug", "debug", args={"msg": "hello"},
                 delegate_to="localhost")
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    rt.delegate_fn = lambda *_args: pytest.fail("debug should not be delegated")
    frames = []
    rt.on_result = frames.append
    rt.run_play({"tasks": [task], "handlers": [], "gather_facts": False, "vars": {}})
    assert frames[0]["result"]["msg"] == "hello"


def test_inventory_refresh_vars_are_absorbed_without_leaking_result_key():
    task = _task("1", "group", "group_by")
    task["special"] = None
    task["fallback"] = True
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    rt.delegate_fn = lambda *_args: {
        "changed": True,
        "add_group": "parent-child",
        "_ansiroll_vars": {"group_names": ["parent", "parent-child"], "value": 3},
    }
    frames = []
    rt.on_result = frames.append
    rt.run_play({"tasks": [task], "handlers": [], "gather_facts": False, "vars": {}})
    assert rt.vars["value"] == 3
    assert "_ansiroll_vars" not in frames[0]["result"]


def test_task_context_refreshes_current_hostvars_after_absorbing_facts():
    rt = Runtime(
        {}, "", {
            "inventory_hostname": "h",
            "hostvars": {"h": {"inventory_hostname": "h"}},
        },
    )
    rt._absorb_facts(
        {"action": "include_vars"},
        {"ansible_facts": {"included_value": "present"}},
    )

    ctx = rt._task_ctx(_task("1", "inspect", "debug"), {})

    assert ctx["hostvars"]["h"]["included_value"] == "present"


def test_environment_skips_undefined_entry_and_keeps_other_values():
    rt = Runtime({}, "", {"inventory_hostname": "h"})

    environment = rt._task_environment(
        {
            "environment": [
                {"PATH": "{{ ansible_env.PATH }}", "lola": "ido"},
            ],
        },
        rt.ctx(),
    )

    assert environment == {"lola": "ido"}


def test_block_rescue_always():
    block = {
        "type": "block",
        "when": [], "become": False, "vars": {},
        "block": [_task("1", "boom", "fail", args={"msg": "boom"})],
        "rescue": [_task("2", "rescued", "debug", args={"msg": "ok"})],
        "always": [_task("3", "always", "debug", args={"msg": "always"})],
    }
    rt, frames = run([block])
    names = [f["task"] for f in frames]
    assert "rescued" in names and "always" in names
    assert rt.stats["rescued"] == 1
    assert rt.stats["failures"] == 0
    assert rt.failed is False


def test_block_rescue_receives_failed_task_and_result():
    block = {
        "type": "block",
        "when": [], "become": False, "vars": {},
        "block": [_task("1", "original failure", "fail", args={"msg": "boom"})],
        "rescue": [_task(
            "2", "report failure", "debug",
            args={"msg": "{{ ansible_failed_task.name }}: {{ ansible_failed_result.msg }}"},
        )],
        "always": [],
    }

    _rt, frames = run([block])

    assert by_name(frames, "report failure")[0]["result"]["msg"] == "original failure: boom"


def test_outer_rescue_clears_failure_from_inner_block_with_always():
    inner = {
        "type": "block", "when": [], "become": False, "vars": {},
        "block": [_task("1", "inner failure", "fail")],
        "rescue": [],
        "always": [_task("2", "inner always", "debug")],
    }
    outer = {
        "type": "block", "when": [], "become": False, "vars": {},
        "block": [inner],
        "rescue": [_task("3", "outer rescue", "debug")],
        "always": [],
    }

    rt, _frames = run([outer])

    assert rt.stats["failures"] == 0
    assert rt.stats["rescued"] == 1


def test_failure_in_always_preserves_pending_block_failure_count():
    block = {
        "type": "block", "when": [], "become": False, "vars": {},
        "block": [_task("1", "block failure", "fail")],
        "rescue": [],
        "always": [_task("2", "always failure", "fail")],
    }

    rt, _frames = run([block])

    assert rt.stats["failures"] == 2
    assert rt.failed is True


def test_skipped_block_reports_contained_tasks_and_always():
    block = {
        "type": "block",
        "when": ["false"], "become": False, "vars": {},
        "block": [_task("1", "blocked", "debug", args={"msg": "no"})],
        "rescue": [_task("2", "rescue", "debug", args={"msg": "no"})],
        "always": [_task("3", "always", "debug", args={"msg": "no"})],
    }
    _rt, frames = run([block])
    assert [f["task"] for f in frames] == ["blocked", "always"]
    assert [f["status"] for f in frames] == ["skipped", "skipped"]


def test_skipped_block_loop_reports_items_but_counts_once():
    task = _task("1", "loop", "debug", args={"msg": "{{ item }}"}, loop=[1, 2, 3])
    block = {
        "type": "block",
        "when": ["false"], "become": False, "vars": {},
        "block": [task],
        "rescue": [],
        "always": [],
    }
    rt, frames = run([block])
    assert [f["result"].get("item") for f in frames if f.get("is_item")] == [1, 2, 3]
    assert len([f for f in frames if not f.get("is_item")]) == 1
    assert rt.stats["skipped"] == 1


def test_ignore_errors_counts_as_ignored():
    tasks = [_task("1", "boom", "fail", args={"msg": "x"}, ignore_errors=True)]
    rt, frames = run(tasks)
    assert frames[0]["status"] == "failed" and frames[0]["ignored"] is True
    assert rt.stats["ignored"] == 1 and rt.stats["failures"] == 0


def test_templated_false_ignore_errors_stops_a_failing_loop():
    tasks = [
        _task(
            "1", "assert loop", "assert",
            args={"that": ["item < 2"]},
            loop=[1, 2],
            ignore_errors="{{ ignore_assertion_errors }}",
        ),
        _task("2", "after", "debug", args={"msg": "must not run"}),
    ]
    rt, frames = run(tasks, {"inventory_hostname": "h", "ignore_assertion_errors": False})
    failed_item = [frame for frame in frames if frame.get("is_item")][-1]
    assert failed_item["status"] == "failed"
    assert failed_item["ignored"] is False
    assert "after" not in [frame["task"] for frame in frames]
    assert rt.stats["failures"] == 1 and rt.stats["ignored"] == 0


def test_uncaught_failure_tallies_and_stops():
    tasks = [
        _task("1", "boom", "fail", args={"msg": "x"}),
        _task("2", "after", "debug", args={"msg": "should not run"}),
    ]
    rt, frames = run(tasks)
    assert rt.stats["failures"] == 1
    assert "after" not in [f["task"] for f in frames]


def test_handler_notify_and_flush():
    tasks = [_task("1", "trigger", "debug", args={"msg": "x"},
                   changed_when=["true"], notify=["h1"])]
    flush = {"type": "task", "uuid": "f", "name": "Flush handlers",
             "action": "meta", "special": "meta_flush_handlers"}
    handlers = [_task("h", "h1", "debug", args={"msg": "handled"})]
    rt, frames = run(tasks + [flush], handlers=handlers)
    assert "h1" in [f["task"] for f in frames]


@pytest.mark.parametrize("setup", ["failed", "raises"])
def test_gather_facts_failure_fails_host(monkeypatch, setup):
    # Ansible: a failed setup is fatal on "Gathering Facts" and the play's
    # tasks never run for that host.
    frames = []
    rt = Runtime({}, "", {"inventory_hostname": "h"}, on_result=frames.append)

    def fake_setup(*_args, **_kwargs):
        if setup == "raises":
            raise OSError("boom")
        return {"failed": True, "changed": False, "msg": "setup failed"}

    monkeypatch.setattr(rt, "run_module", fake_setup)
    monkeypatch.delenv("ANSIROLL_FAST_FACTS", raising=False)
    rt.run_play({"tasks": [_task("1", "after", "debug", args={"msg": "x"})],
                 "handlers": [], "gather_facts": True, "vars": {}})
    assert [f["status"] for f in by_name(frames, "Gathering Facts")] == ["failed"]
    assert not by_name(frames, "after")
    assert rt.failed
    assert rt.stats["failures"] == 1


def test_gather_facts_failure_honors_play_ignore_errors(monkeypatch):
    frames = []
    rt = Runtime({}, "", {"inventory_hostname": "h"}, on_result=frames.append)
    monkeypatch.setattr(rt, "run_module", lambda *_a, **_k: {
        "failed": True, "changed": False, "msg": "setup failed"})
    monkeypatch.delenv("ANSIROLL_FAST_FACTS", raising=False)
    rt.run_play({"tasks": [_task("1", "after", "debug", args={"msg": "x"})],
                 "handlers": [], "gather_facts": True, "vars": {},
                 "ignore_errors": True})
    assert by_name(frames, "after")
    assert not rt.failed
    assert rt.stats["failures"] == 0
    assert rt.stats["ignored"] == 1


def _force_handlers_play(force):
    flush = {"type": "task", "uuid": "f", "name": "Flush handlers",
             "action": "meta", "special": "meta_flush_handlers"}
    return {
        "tasks": [
            _task("1", "trigger", "debug", args={"msg": "x"},
                  changed_when=["true"], notify=["h1"]),
            _task("2", "boom", "fail", args={"msg": "boom"}),
            flush,
        ],
        "handlers": [_task("h", "h1", "debug", args={"msg": "handled"})],
        "gather_facts": False, "vars": {}, "force_handlers": force,
    }


@pytest.mark.parametrize("version,force,ran", [
    ("2.13.13", True, True),
    ("2.13.13", False, False),
    # 2.14+: play.compile() wraps sections in an `always` flush instead, so
    # a bare task list without that wrapper flushes nothing after a failure.
    ("2.14.0", True, False),
])
def test_legacy_force_handlers_flushes_after_failure(monkeypatch, version, force, ran):
    import ansiroll.runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", version)
    frames = []
    rt = Runtime({}, "", {"inventory_hostname": "h"}, on_result=frames.append)
    rt.run_play(_force_handlers_play(force))
    assert rt.failed
    assert bool(by_name(frames, "h1")) is ran


@pytest.mark.parametrize("version,registered_failed,registered_exception", [
    ("2.20.1", True, "(traceback unavailable)"), ("2.21.1", False, None)])
def test_until_exhausted_failed_when_decides_outcome(
        monkeypatch, version, registered_failed, registered_exception):
    import ansiroll.runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", version)
    frames = []
    rt = Runtime({}, "", {"inventory_hostname": "h"}, on_result=frames.append)
    rt.run_play({"tasks": [
        _task("1", "retry", "debug", args={"msg": "x"}, register="r",
              until=["false"], retries=1, delay=0, failed_when=["false"]),
        _task("2", "after", "debug", args={"msg": "{{ r.failed }}"}),
    ], "handlers": [], "gather_facts": False, "vars": {}})
    assert not rt.failed
    assert rt.vars["r"]["failed"] is registered_failed
    assert rt.vars["r"].get("exception") == registered_exception


def test_core_version_gate_uses_baked_version(monkeypatch):
    # The compiled script bakes the controller's ansible-core version; any
    # ansible installed on the target must not influence version gates.
    import ansiroll.runtime as runtime_mod
    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", "2.15.3")
    assert runtime_mod._core_at_least(2, 15)
    assert not runtime_mod._core_at_least(2, 16)


def test_assert_pass_and_fail():
    rt, frames = run([_task("1", "a", "assert", args={"that": ["1 == 1"]})])
    assert frames[0]["status"] == "ok"
    rt, frames = run([_task("1", "a", "assert", args={"that": ["1 == 2"]},
                            ignore_errors=True)])
    assert frames[0]["status"] == "failed"


def test_copy_content_writes_file(tmp_path):
    dest = os.path.join(tmp_path, "out.txt")
    tasks = [_task("1", "c", "copy", args={"content": "hello", "dest": dest})]
    rt, frames = run(tasks)
    assert frames[0]["status"] == "changed"
    assert open(dest).read() == "hello"
    # Second run is idempotent (no change).
    rt2, frames2 = run(tasks)
    assert frames2[0]["status"] == "ok"


def test_copy_fetches_runtime_created_controller_source(tmp_path):
    src = tmp_path / "checkout" / "docker-compose.yml"
    src.parent.mkdir()
    src.write_bytes(b"services:\n  app:\n    image: test\xff\n")
    dest = tmp_path / "remote" / "docker-compose.yml"
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    rt.controller_source_fn = lambda path: {
        "data": open(path, "rb").read(),
        "meta": {"fullpath": path},
    }

    result = rt._run_file_action(
        "copy", {"_baked": {}},
        {"src": str(src), "dest": str(dest)}, {},
    )

    assert result["changed"] is True
    assert dest.read_bytes() == src.read_bytes()


def test_accelerated_modules_are_opt_in_by_default(monkeypatch):
    monkeypatch.delenv("ANSIROLL_ACCELERATED_MODULES", raising=False)
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    task = _task("1", "command", "command", args={"_raw_params": "true"})
    task.update({"special": None, "accelerator": "always"})
    assert rt._run_accelerated_action(task, task["args"]) is None


def test_fast_facts_are_separately_opt_in(monkeypatch):
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "all")
    monkeypatch.delenv("ANSIROLL_FAST_FACTS", raising=False)
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    calls = []
    monkeypatch.setattr(
        rt, "run_module",
        lambda action, args, become=False: calls.append(action) or {
            "changed": False, "ansible_facts": {"ansible_kernel": "official"},
        },
    )
    rt.gather_facts()
    assert calls == ["ansible.builtin.setup"]
    assert rt.vars["ansible_kernel"] == "official"


def test_fast_facts_are_absorbed_without_setup_worker(monkeypatch):
    from ansiroll.accelerated_modules import facts

    monkeypatch.setenv("ANSIROLL_FAST_FACTS", "1")
    monkeypatch.setattr(facts, "run", lambda **_kwargs: {
        "changed": False,
        "ansible_facts": {"ansible_kernel": "fast"},
    })
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    monkeypatch.setattr(
        rt, "run_module",
        lambda *_args, **_kwargs: pytest.fail("official setup should not run"),
    )
    rt.gather_facts()
    assert rt.vars["ansible_kernel"] == "fast"
    assert rt.vars["ansible_facts"]["kernel"] == "fast"


def test_fast_facts_fall_back_to_official_setup(monkeypatch):
    from ansiroll.accelerated_modules import facts

    monkeypatch.setenv("ANSIROLL_FAST_FACTS", "yes")
    monkeypatch.setattr(
        facts, "run", lambda **_kwargs: {"_ansiroll_fallback": True})
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    calls = []
    monkeypatch.setattr(
        rt, "run_module",
        lambda action, args, become=False: calls.append(action) or {
            "changed": False, "ansible_facts": {"ansible_kernel": "official"},
        },
    )
    rt.gather_facts()
    assert calls == ["ansible.builtin.setup"]
    assert rt.vars["ansible_kernel"] == "official"


def test_async_poll_zero_starts_without_sync_run(monkeypatch):
    frames = []
    task = _task(
        "1",
        "async",
        "command",
        args={"_raw_params": "sleep 10"},
        async_val=2000,
        poll=0,
    )
    task["special"] = None
    rt = Runtime({}, "", {"inventory_hostname": "h"}, on_result=frames.append)

    def async_run(action, args, become=False):
        return {
            "changed": True,
            "ansible_job_id": "123.456",
            "started": 1,
            "finished": 0,
        }

    def sync_run(*_args, **_kwargs):
        raise AssertionError("poll=0 async task should not run synchronously")

    monkeypatch.setattr(rt, "_run_async_module", async_run)
    monkeypatch.setattr(rt, "run_module", sync_run)
    rt.run_play({"tasks": [task], "handlers": [], "gather_facts": False, "vars": {}})

    assert frames[0]["status"] == "changed"
    assert frames[0]["result"]["finished"] == 0
    assert rt.stats["changed"] == 1


def test_until_retries_until_condition_passes(monkeypatch):
    task = _task(
        "1",
        "retry",
        "command",
        args={"_raw_params": "unused"},
        register="r",
        until=["r.rc == 0"],
        retries=3,
        delay=0,
    )
    task["special"] = None
    rt = Runtime({"command": {"module_fqn": "ansible.modules.command", "zip": "x"}},
                 "", {"inventory_hostname": "h"})
    attempts = []

    def run_once(_task, _ctx, _become, attempt=None):
        attempts.append(attempt)
        rc = 1 if len(attempts) == 1 else 0
        return {"changed": True, "failed": rc != 0, "rc": rc}

    monkeypatch.setattr(rt, "_exec_one", run_once)
    res = rt._exec_with_retries(task, rt.ctx({}), False)

    assert attempts == [1, 2]
    assert res["rc"] == 0


def test_accelerated_command_runs_without_embedded_module(monkeypatch):
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "command")
    tasks = [_task("1", "cmd", "command", args={"_raw_params": "printf hello"})]
    tasks[0].update({"special": None, "accelerator": "always"})
    rt, frames = run(tasks)
    res = frames[0]["result"]
    assert frames[0]["status"] == "changed"
    assert res["rc"] == 0
    assert res["stdout"] == "hello"
    assert rt._workers == {}


def test_accelerated_command_expands_argument_vars_when_requested(
        monkeypatch):
    monkeypatch.setenv("ANSIROLL_COMMAND_VALUE", "expanded")
    from ansiroll.accelerated_modules import run as run_accelerated

    result = run_accelerated(
        "command",
        {
            "_raw_params": [
                "/bin/sh", "-c", 'test "$1" = expanded', "sh",
                "$ANSIROLL_COMMAND_VALUE",
            ],
        },
        options={"expand_argument_vars": True},
    )

    assert result["rc"] == 0


def test_accelerated_python_import_uses_isolated_fork():
    from ansiroll.accelerated_modules import run as run_accelerated

    success = run_accelerated(
        "command", {"_raw_params": [os.sys.executable, "-c", "import json"]})
    failure = run_accelerated(
        "command",
        {
            "_raw_params": [
                os.sys.executable, "-c",
                "import ansiroll_module_that_does_not_exist",
            ],
        },
    )

    assert success["rc"] == 0
    assert failure["rc"] != 0
    assert "ModuleNotFoundError" in failure["stderr"]


def test_accelerated_shell_python_import_uses_isolated_fork():
    from ansiroll.accelerated_modules import run as run_accelerated

    result = run_accelerated(
        "shell",
        {"_raw_params": "%s -c 'import json'" % shlex.quote(os.sys.executable)},
    )

    assert result["rc"] == 0


def test_accelerated_shell_runs_without_embedded_module(monkeypatch):
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "shell")
    tasks = [_task("1", "shell", "shell", args={"_raw_params": "printf h && printf i"})]
    tasks[0].update({"special": None, "accelerator": "always"})
    rt, frames = run(tasks)
    assert frames[0]["result"]["stdout"] == "hi"
    assert rt._workers == {}


def test_accelerated_slurp_runs_without_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "slurp")
    source = tmp_path / "binary"
    source.write_bytes(b"\x00ansiroll\xff")
    tasks = [_task("1", "slurp", "slurp", args={"src": str(source)})]
    tasks[0].update({"special": None, "accelerator": "always"})

    rt, frames = run(tasks)

    assert frames[0]["status"] == "ok"
    assert frames[0]["result"]["content"] == "AGFuc2lyb2xs/w=="
    assert frames[0]["result"]["encoding"] == "base64"
    assert rt._workers == {}


def test_accelerated_replace_runs_without_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "replace")
    path = tmp_path / "compose.yml"
    path.write_text("x-versions\n  app: ${AASS_VERSION:-latest}\nservices:\n")
    tasks = [_task(
        "1", "replace", "replace",
        args={
            "path": str(path),
            "regexp": r"\${AASS_VERSION:-latest}$",
            "replace": "3.36.1",
            "after": "x-versions",
            "before": "services:",
        },
    )]
    tasks[0].update({"special": None, "accelerator": "always"})

    rt, frames = run(tasks)

    assert frames[0]["status"] == "changed"
    assert "${AASS_VERSION" not in path.read_text()
    assert rt._workers == {}


def test_accelerated_file_absent_and_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "file")
    path = tmp_path / "dir" / "file.txt"
    tasks = [
        _task("1", "dir", "file", args={"path": str(path.parent), "state": "directory"}),
        _task("2", "touch", "file", args={"path": str(path), "state": "touch"}),
        _task("3", "absent", "file", args={"path": str(path.parent), "state": "absent"}),
    ]
    for task in tasks:
        task.update({"special": None, "accelerator": "always"})
    rt, frames = run(tasks)
    assert [f["status"] for f in frames] == ["changed", "changed", "changed"]
    assert not path.parent.exists()
    assert rt._workers == {}


def test_accelerated_file_attribute_free_recurse_needs_no_worker(
        tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "file")
    path = tmp_path / "ssh"
    tasks = [_task(
        "1", "directory", "file",
        args={"path": str(path), "state": "directory", "recurse": True},
    )]
    tasks[0].update({"special": None, "accelerator": "always"})

    rt, frames = run(tasks)

    assert frames[0]["status"] == "changed"
    assert path.is_dir()
    assert rt._workers == {}


def test_accelerated_file_touch_with_current_identity_needs_no_worker(
        tmp_path, monkeypatch):
    import grp
    import pwd

    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "file")
    path = tmp_path / "nanorc"
    tasks = [_task(
        "1", "touch", "file",
        args={
            "path": str(path), "state": "touch",
            "owner": pwd.getpwuid(os.geteuid()).pw_name,
            "group": grp.getgrgid(os.getegid()).gr_name,
        },
    )]
    tasks[0].update({"special": None, "accelerator": "always"})

    rt, frames = run(tasks)

    assert frames[0]["status"] == "changed"
    assert path.is_file()
    assert rt._workers == {}


def test_accelerated_file_links_and_preserved_touch(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "file")
    source = tmp_path / "source"
    source.write_text("source")
    link = tmp_path / "link"
    preserved = tmp_path / "preserved"
    preserved.write_text("existing")
    preserved.chmod(0o644)
    before = preserved.stat().st_mtime_ns
    tasks = [
        _task(
            "1", "link", "file",
            args={"src": str(source), "dest": str(link),
                  "state": "link", "force": True},
        ),
        _task(
            "2", "preserve", "file",
            args={"path": str(preserved), "state": "touch", "mode": "0644",
                  "modification_time": "preserve",
                  "access_time": "preserve"},
        ),
    ]
    for task in tasks:
        task.update({"special": None, "accelerator": "always"})

    rt, frames = run(tasks)

    assert [frame["status"] for frame in frames] == ["changed", "ok"]
    assert link.resolve() == source
    assert preserved.stat().st_mtime_ns == before
    assert rt._workers == {}


def test_accelerated_blockinfile_needs_no_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "blockinfile")
    path = tmp_path / "nanorc"
    path.write_text("existing\n")
    tasks = [_task(
        "1", "block", "blockinfile",
        args={
            "path": str(path), "insertafter": "EOF",
            "block": "set linenumbers\n",
        },
    )]
    tasks[0].update({"special": None, "accelerator": "always"})

    rt, frames = run(tasks)

    assert frames[0]["status"] == "changed"
    assert "set linenumbers" in path.read_text()
    assert rt._workers == {}


def test_accelerated_user_and_group_noops_need_no_worker(monkeypatch):
    import grp
    import pwd

    current_user = pwd.getpwuid(os.geteuid())
    current_group = grp.getgrgid(current_user.pw_gid)
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "user,group")
    tasks = [
        _task(
            "1", "group", "group",
            args={"name": current_group.gr_name, "state": "present",
                  "gid": current_group.gr_gid},
        ),
        _task(
            "2", "user", "user",
            args={"name": current_user.pw_name, "state": "present",
                  "uid": current_user.pw_uid, "group": current_group.gr_name,
                  "shell": current_user.pw_shell},
        ),
    ]
    for task in tasks:
        task.update({"special": None, "accelerator": "always"})

    rt, frames = run(tasks)

    assert [frame["status"] for frame in frames] == ["ok", "ok"]
    assert rt._workers == {}


def test_worker_accelerated_file_op(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "file")
    path = tmp_path / "worker-dir"
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    try:
        res = rt._run_worker_accelerated(
            "file", {"path": str(path), "state": "directory"}
        )
        assert res["changed"] is True
        assert path.is_dir()
        res = rt._run_worker_accelerated(
            "file", {"path": str(path), "state": "absent"}
        )
        assert res["changed"] is True
        assert not path.exists()
    finally:
        rt.cleanup()


def test_become_worker_authenticates_without_writing_password_to_protocol(monkeypatch):
    calls = {"run": [], "popen": []}

    class FakeProc:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()

        def poll(self):
            return None

    def fake_run(cmd, **kwargs):
        calls["run"].append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_popen(cmd, **kwargs):
        calls["popen"].append((cmd, kwargs))
        return FakeProc()

    monkeypatch.setattr("ansiroll.runtime.subprocess.run", fake_run)
    monkeypatch.setattr("ansiroll.runtime.subprocess.Popen", fake_popen)
    rt = Runtime(
        {}, "", {"inventory_hostname": "h"}, become_password="secret-password"
    )
    rt.become_user = "openhabian"

    worker = rt._get_worker(True)

    auth_cmd, auth_kwargs = calls["run"][0]
    assert auth_cmd == ["sudo", "-S", "-p", "", "-u", "openhabian", "-v"]
    assert auth_kwargs["input"] == "secret-password\n"
    worker_cmd, _worker_kwargs = calls["popen"][0]
    assert worker_cmd[:4] == ["sudo", "-n", "-u", "openhabian"]
    assert worker["proc"].stdin.getvalue() == b""


def test_become_worker_stops_when_separate_sudo_authentication_fails(monkeypatch):
    def fake_run(_cmd, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="incorrect password")

    def unexpected_popen(*_args, **_kwargs):
        raise AssertionError("worker must not start after failed sudo authentication")

    monkeypatch.setattr("ansiroll.runtime.subprocess.run", fake_run)
    monkeypatch.setattr("ansiroll.runtime.subprocess.Popen", unexpected_popen)
    rt = Runtime({}, "", {"inventory_hostname": "h"}, become_password="wrong")

    with pytest.raises(RuntimeError, match="sudo authentication failed"):
        rt._get_worker(True)


def test_same_uid_become_uses_the_normal_worker(monkeypatch):
    calls = []

    class FakeProc:
        stdin = io.BytesIO()
        stdout = io.BytesIO()
        stderr = io.BytesIO()

        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return FakeProc()

    monkeypatch.setattr("ansiroll.runtime.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "ansiroll.runtime.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected sudo")),
    )
    rt = Runtime({}, "", {"inventory_hostname": "h"}, profile=True)
    import getpass
    rt.become_user = getpass.getuser()

    worker = rt._get_worker(True)

    assert calls[0][0][:2] == [rt.python, "-c"]
    assert rt._workers == {False: worker}
    assert rt.engine_profile["worker_identities"] == {"current-user": 1}


def test_worker_baked_write_op(tmp_path):
    dest = tmp_path / "nested" / "out.txt"
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    try:
        res = rt._run_worker_operation(
            "fast_write",
            {"dest": str(dest), "content": "hello", "mode": "0644"},
        )
        assert res["changed"] is True
        assert dest.read_text() == "hello"
        res = rt._run_worker_operation(
            "fast_write",
            {"dest": str(dest), "content": "hello", "mode": "0644"},
        )
        assert res["changed"] is False
    finally:
        rt.cleanup()


def test_privileged_baked_write_does_not_fall_back_to_a_temp_file(
        monkeypatch, tmp_path):
    destination = tmp_path / "must-not-be-written"
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    monkeypatch.setattr(rt, "_needs_become", lambda become: bool(become))
    monkeypatch.setattr(rt, "_run_worker_operation", lambda *a, **k: None)

    result = rt._run_file_action(
        "copy", {"_baked": {}},
        {"content": "hello", "dest": str(destination)}, {}, become=True,
    )

    assert result == {
        "failed": True,
        "changed": False,
        "msg": "copy: privileged worker unavailable",
    }
    assert not destination.exists()


def test_unsupported_accelerated_file_arguments_fall_back(monkeypatch):
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "file")
    task = _task(
        "1", "recursive mode", "file",
        args={"path": "/tmp/x", "state": "directory", "recurse": True,
              "mode": "g+r"},
    )
    task.update({"special": None, "accelerator": "dynamic"})
    rt = Runtime({}, "", {"inventory_hostname": "h"})

    assert rt._run_accelerated_action(task, task["args"]) is None


@pytest.mark.parametrize("action,args", [
    ("command", {"_raw_params": "true", "warn": "false"}),
    ("command", {"_raw_params": "true", "stdin_add_newline": "false"}),
    ("command", {"_raw_params": "true", "cmd": "false"}),
    ("file", {"path": "/tmp/x", "state": "directory", "mode": 755}),
    ("file", {"path": "/tmp/x", "dest": "/tmp/y", "state": "directory"}),
    ("file", {"path": "/tmp/x", "state": "directory",
              "mode": "0755", "recurse": True}),
    ("find", {"paths": ["/tmp"], "age": "1d"}),
    ("find", {"paths": ["/tmp"], "use_regex": True}),
    ("stat", {"path": "/tmp/x", "follow": "false"}),
    ("stat", {"path": "/tmp/x", "dest": "/tmp/y"}),
    ("stat", {"path": "/tmp/x:ambiguous"}),
    ("slurp", {"src": "/tmp/x", "armor": False}),
    ("slurp", {"src": "/tmp/x", "path": "/tmp/y"}),
    ("replace", {"path": "/tmp/x", "regexp": "x", "backup": True}),
    ("replace", {"path": "/tmp/x", "regexp": "x", "validate": "check %s"}),
    ("blockinfile", {"path": "/tmp/x", "block": "x", "backup": True}),
    ("blockinfile", {"path": "/tmp/x", "block": "x",
                     "marker": "# missing placeholder"}),
    ("systemd", {"name": "cron.service", "state": "reloaded"}),
    ("systemd", {"name": "cron.service", "masked": False}),
    ("systemd", {"name": "cron", "state": "started", "daemon_reload": True}),
    ("service", {"name": "chrony", "state": "started", "use": "sysvinit"}),
    ("apt", {"pkg": ["mosquitto"], "state": "present"}),
    ("apt", {"pkg": ["mosquitto*"], "state": "absent"}),
])
def test_ambiguous_accelerated_arguments_fall_back(action, args, monkeypatch):
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", action)
    task = _task("1", "ambiguous", action, args=args)
    task.update({"special": None, "accelerator": "dynamic"})
    rt = Runtime({}, "", {"inventory_hostname": "h"})

    assert rt._run_accelerated_action(task, args) is None


def test_required_accelerator_does_not_fall_through_when_worker_fails(
        monkeypatch):
    task = _task(
        "1", "required replace", "replace",
        args={"path": "/tmp/x", "regexp": "x", "replace": "y"},
    )
    task.update({"special": None, "accelerator": "required"})
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    monkeypatch.setattr(rt, "_needs_become", lambda _become: True)
    monkeypatch.setattr(
        rt, "_run_worker_accelerated",
        lambda *_args, **_kwargs: None,
    )

    result = rt._run_accelerated_action(task, task["args"], become=True)

    assert result["failed"] is True
    assert "could not start its execution worker" in result["msg"]


def test_accelerated_service_delegates_safe_subset_to_systemd(monkeypatch):
    from ansiroll import accelerated_modules

    args = {"name": "chrony", "state": "restarted", "enabled": True}
    expected = {"changed": False, "name": "chrony"}
    calls = []

    monkeypatch.setattr(
        accelerated_modules.service.systemd,
        "run",
        lambda *positional, **keyword: (
            calls.append((positional, keyword)) or expected
        ),
    )

    assert accelerated_modules.can_accelerate("service", args)
    assert accelerated_modules.run("service", args) == expected
    assert calls[0][0] == (args,)


def test_accelerated_find_returns_ansible_compatible_entries(tmp_path):
    from ansiroll import accelerated_modules

    (tmp_path / "keep.list").write_text("deb example")
    (tmp_path / "skip.list").write_text("deb old")
    (tmp_path / "directory").mkdir()
    (tmp_path / ".hidden.list").write_text("hidden")

    result = accelerated_modules.run(
        "find",
        {
            "paths": str(tmp_path),
            "patterns": "*.list",
            "excludes": "skip.list",
            "file_type": "file",
        },
    )

    assert result["changed"] is False
    assert result["matched"] == 1
    assert result["examined"] == 4
    assert result["files"][0]["path"] == str(tmp_path / "keep.list")
    assert result["files"][0]["isreg"] is True
    assert result["files"][0]["mode"]


def test_static_accelerator_skips_repeated_capability_check(monkeypatch):
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "command")
    task = _task(
        "1", "literal command", "command", args={"_raw_params": "printf ok"}
    )
    task.update({"special": None, "accelerator": "always"})
    rt = Runtime({}, "", {"inventory_hostname": "h"})

    def unexpected_check(*_args, **_kwargs):
        raise AssertionError("static calls must not repeat capability checks")

    monkeypatch.setattr(
        "ansiroll.accelerated_modules.can_accelerate", unexpected_check
    )
    result = rt._run_accelerated_action(task, task["args"])

    assert result["rc"] == 0
    assert result["stdout"] == "ok"


def test_accelerator_profile_totals_are_aggregated(monkeypatch):
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "command")
    task = _task(
        "1", "profile command", "command", args={"_raw_params": "printf ok"}
    )
    task.update({"special": None, "accelerator": "always"})
    rt = Runtime({}, "", {"inventory_hostname": "h"}, profile=True)

    result = rt._run_accelerated_action(task, task["args"])

    assert result["_ansiroll_profile"]["module"] == "accelerated.command"
    aggregate = rt.profile_totals["accelerated.command"]
    assert aggregate["count"] == 1
    assert aggregate["worker_total"] >= aggregate["worker_run"] >= 0


def test_accelerator_profile_attributes_privileged_worker(monkeypatch):
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "file")
    task = _task(
        "1", "privileged file", "file",
        args={"path": "/tmp/x", "state": "directory"},
    )
    task.update({"special": None, "accelerator": "always"})
    rt = Runtime({}, "", {"inventory_hostname": "h"}, profile=True)
    worker = {"identity": "sudo:root"}
    monkeypatch.setattr(rt, "_needs_become", lambda _become: True)
    monkeypatch.setattr(
        rt, "_run_worker_accelerated",
        lambda *args, **kwargs: (
            rt._workers.setdefault((True, "root"), worker)
            and {"changed": False}
        ),
    )

    result = rt._run_accelerated_action(task, task["args"], become=True)

    assert result["_ansiroll_profile"]["worker_identity"] == "sudo:root"
    assert (
        rt.profile_totals["accelerated.file"]["worker_identities"]
        == {"sudo:root": 1}
    )


def test_accelerated_command_handles_check_mode_without_worker(monkeypatch):
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "command")
    task = _task(
        "1", "check command", "command", args={"_raw_params": "printf no"}
    )
    task.update({"special": None, "accelerator": "always"})
    rt = Runtime({}, "", {"inventory_hostname": "h"}, check_mode=True)

    result = rt._run_accelerated_action(task, task["args"])

    assert result["rc"] == 0
    assert result["skipped"] is True
    assert result["msg"] == "Command would have run if not in check mode"


def test_to_yaml_filters_accept_lazy_vars_and_set_fact_values():
    frames = []
    rt = Runtime({}, "", {"inventory_hostname": "h"}, on_result=frames.append)
    play = {
        "tasks": [
            _task("1", "sf", "set_fact", args={"greeting": "hi", "items": ["a", "b"]}),
            _task("2", "cfg", "debug", args={"msg": "{{ config | to_yaml }}"}),
            _task("3", "nice", "debug", args={"msg": "{{ greeting | to_nice_yaml }}"}),
            _task("4", "lst", "debug", args={"msg": "{{ items | to_yaml }}"}),
        ],
        "handlers": [],
        "gather_facts": False,
        "vars": {"config": {"enabled": True, "nested": ["{{ 1 + 1 }}"]}},
    }
    rt.run_play(play)
    assert [f["status"] for f in frames] == ["ok"] * 4
    assert by_name(frames, "cfg")[0]["result"]["msg"] == "enabled: true\nnested:\n- 2\n"
    assert by_name(frames, "nice")[0]["result"]["msg"] == "hi\n...\n"
    assert by_name(frames, "lst")[0]["result"]["msg"] == "- a\n- b\n"
