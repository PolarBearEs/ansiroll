"""Unit tests for the runtime interpreter (when/loop/register/blocks/handlers/stats).

These use only the runtime "special" actions (debug/set_fact/fail/assert) and
template/copy-with-content, so no Ansible or embedded modules are required.
"""

import datetime
import io
import json
import os
import shlex
from types import SimpleNamespace

import pytest

from ansiroll.runtime import (
    _OMIT, ConditionalError, Runtime, UntrustedConditional, _parse_output,
)


@pytest.mark.parametrize(("version", "expected"), [("2.19.0", True), ("2.19.1", False)])
def test_check_mode_buffer_null_semantics_follow_core_patch(monkeypatch, version, expected):
    from ansiroll import runtime

    monkeypatch.setattr(runtime, "_ANSIROLL_CORE_VERSION", version)
    templater = Runtime({}, "", {}).templater
    expression = "{% set x %}{{ none }}{{ none }}{% endset %}{{ x is not none }}"
    assert templater.render_string(expression, {runtime._CHECK_MODE_TEMPLATE: True}) is expected


@pytest.mark.parametrize("source", ["module", "rendered"])
def test_omitted_become_does_not_render_comment_only_data(monkeypatch, source):
    from ansiroll import runtime

    monkeypatch.setattr(runtime, "_ANSIROLL_CORE_VERSION", "2.21.1")
    rt = Runtime({}, "", {"inventory_hostname": "localhost"})
    if source == "module":
        rt._absorb_facts({"action": "example"}, {"ansible_facts": {"ansible_become": "{# data #}true"}})
    else:
        rt.vars["ansible_become"] = rt.template("{{ prefix ~ 'true' }}", {"prefix": "{# data #}"})
    task = {"become_override": "{{ omit }}", "become_override_on_omit": False}
    with pytest.raises(TypeError, match="Invalid boolean value"):
        rt._execution_flag(task, "become_override", rt.ctx(), False)


@pytest.mark.parametrize("survivor", ["ordinary", {"{{ name }}": "untrusted"}])
def test_bundled_fact_keys_keep_types_and_trust_across_json_collisions(survivor):
    from ansiroll.runtime import _FactKeyTemplate, _encode_bundle_mapping_keys, _restore_bundle_mapping_keys

    source = {True: {_FactKeyTemplate("{{ name }}"): "typed"}, "true": survivor,
              "kept": {_FactKeyTemplate("{{ name }}"): "trusted"}}
    encoded, paths = _encode_bundle_mapping_keys(source)
    decoded = json.loads(json.dumps(encoded))
    restored = _restore_bundle_mapping_keys(decoded, json.loads(json.dumps(paths)))
    assert restored[True] == {"{{ name }}": "typed"}
    assert isinstance(next(iter(restored[True])), _FactKeyTemplate)
    assert restored["true"] == survivor
    if isinstance(survivor, dict):
        assert type(next(iter(restored["true"]))) is str
    assert isinstance(next(iter(restored["kept"])), _FactKeyTemplate)


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


def test_rescued_datetime_keys_preserve_fractional_offsets():
    from ansiroll import runtime

    stamp = datetime.datetime(2025, 3, 1)
    offsets = (100000, 200000, -100000)
    keys = [stamp.replace(tzinfo=datetime.timezone(datetime.timedelta(microseconds=offset))) for offset in offsets]
    original = dict(zip(keys, offsets))
    packed = runtime._pack_include_value(original)
    restored = runtime._restore_delegated_mapping_keys(packed["value"], packed["mapping_keys"])
    assert restored == original
    assert [key.isoformat() for key in restored] == [key.isoformat() for key in keys]


def test_rescued_dates_and_datetimes_roundtrip_without_fromisoformat(monkeypatch):
    from ansiroll import runtime

    values = {"day": datetime.date(2025, 3, 1), "stamp": datetime.datetime(2025, 3, 1, 8, 30),
              "aware": datetime.datetime(2025, 3, 1, 8, 30, 1, 123,
                                         tzinfo=datetime.timezone(datetime.timedelta(hours=-3, minutes=-30)))}
    sub_minute_offsets = (datetime.timedelta(seconds=30),
                          -datetime.timedelta(hours=2, seconds=15, microseconds=321))
    offset_values = {"offset_%d" % index: values["stamp"].replace(tzinfo=datetime.timezone(offset))
                     for index, offset in enumerate(sub_minute_offsets)}
    values.update(offset_values)
    extreme = datetime.timedelta(hours=23, minutes=59, seconds=30)
    bounds = {
        "near_min": datetime.datetime.min.replace(tzinfo=datetime.timezone(sub_minute_offsets[0])),
        "near_max": datetime.datetime.max.replace(tzinfo=datetime.timezone(-sub_minute_offsets[0])),
        "below_min": datetime.datetime.min.replace(tzinfo=datetime.timezone(extreme)),
        "above_max": datetime.datetime.max.replace(tzinfo=datetime.timezone(-extreme)),
    }
    packed = runtime._pack_include_value({**values, **bounds}, preserve_scalars=True)
    first_key = bounds["below_min"]
    second_key = datetime.datetime.min.replace(tzinfo=datetime.timezone(extreme - datetime.timedelta(seconds=15)))
    packed_keys = runtime._pack_include_value({first_key: "first", second_key: "second", first_key.isoformat(): "text"})
    legacy_datetime = SimpleNamespace(strptime=datetime.datetime.strptime,
                                      min=datetime.datetime.min, max=datetime.datetime.max)
    monkeypatch.setattr(runtime, "_dt", SimpleNamespace(date=datetime.date, datetime=legacy_datetime,
                                                       timedelta=datetime.timedelta, timezone=datetime.timezone))
    restored = runtime._restore_rescue_scalars(packed)
    assert {name: restored[name] for name in values} == values
    assert all(restored[name].tzinfo is datetime.timezone.utc for name in offset_values)
    assert restored["near_min"].isoformat() == "0001-01-01T00:00:30+00:01"
    assert restored["near_max"].isoformat() == "9999-12-31T23:59:29.999999-00:01"
    assert restored["below_min"].isoformat() == "0001-01-01T00:00:00+23:59"
    assert restored["above_max"].isoformat() == "9999-12-31T23:59:59.999999-23:59"
    restored_keys = runtime._restore_delegated_mapping_keys(packed_keys["value"], packed_keys["mapping_keys"])
    assert list(restored_keys.values()) == ["first", "second", "text"]
    assert runtime._pack_include_value(restored_keys)["mapping_keys"] == packed_keys["mapping_keys"]


def test_lookup_followed_by_another_template_segment_renders_as_text():
    templater = Runtime({}, "", {}).templater
    calls = []

    def lookup(name):
        calls.append(name)
        return "first"

    templater.env.globals["lookup"] = lookup
    text = "{{ lookup('source') }}/{{ 'second' }}"
    assert templater.render_string(text, {}) == "first/second"
    assert templater.render_string(" " + text + " ", {}) == " first/second "
    assert templater.render_string(text, {}) == "first/second"
    assert calls == ["source"] * 3


def test_bare_expression_honors_the_plus_opening_delimiter():
    templater = Runtime({}, "", {}).templater
    text = "{{+ 'hello' }}"
    if next(templater.env.lex(text))[2] != "{{+":
        pytest.skip("Jinja does not support the plus opening delimiter")
    assert templater.render_string(text, {}) == "hello"


@pytest.mark.parametrize(("opening", "closing"), [("{{", "}}"), ("{{-", "}}"), ("{{", "-}}"), ("{{-", "-}}")])
def test_single_lookup_with_a_quoted_template_keeps_its_native_value(opening, closing):
    templater = Runtime({}, "", {}).templater
    templater.env.globals["query"] = lambda _name, term: [term]
    assert templater.render_string("{{ query('source', '{{ key }}') }}", {}) == ["{{ key }}"]
    block = "{% if true %}x{% endif %}"
    assert templater.render_string(f"{opening} query('source', '{block}') {closing}", {}) == [block]


def test_lookup_with_literal_closing_braces_and_null_suffix():
    templater = Runtime({}, "", {}).templater
    templater.env.globals["lookup"] = lambda _name: "first"
    assert templater.render_string("{{ lookup('source') }}/literal}}", {}) == "first/literal}}"
    assert templater.render_string("{{ lookup('source') }}}", {}) == "first}"
    assert templater.render_string('{{ {"x": 1}}}', {}) == {"x": 1}
    assert templater.render_string("{{ lookup('source') }}/{{ suffix }}", {"suffix": None}) == "first/"
    assert templater.render_string("{{ suffix }}", {"suffix": None}) is None


def test_null_interpolation_respects_legacy_native_mode(monkeypatch):
    import ansiroll.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", "2.12.10")
    templater = Runtime({}, "", {}).templater
    for text in ("prefix/{{ suffix }}", "prefix/{{ none }}"):
        ctx = {"suffix": None, "__ansiroll_controller_config": {"DEFAULT_JINJA2_NATIVE": True}}
        assert templater.render_string(text, ctx) == "prefix/None"
        ctx["__ansiroll_controller_config"]["DEFAULT_JINJA2_NATIVE"] = False
        assert templater.render_string(text, ctx) == "prefix/"


def test_template_file_interpolates_null_as_empty_in_legacy_native_mode(monkeypatch):
    import ansiroll.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", "2.12.10")
    runtime = Runtime({}, "", {})
    source = "{{ lookup('env', 'ANSIROLL_TEST_PREFIX') }}/{{ empty }}"
    context = {"empty": None, "__ansiroll_controller_config": {"DEFAULT_JINJA2_NATIVE": True},
               "__ansiroll_controller_env": {"ANSIROLL_TEST_PREFIX": "controller"}}
    monkeypatch.setattr(runtime, "_copy_single_file", lambda _task, _args, data, *_rest: data)
    assert runtime._run_file_action("template", {"_baked": {"out.j2": source}},
                                    {"src": "out.j2", "dest": "/out"}, context) == b"controller/"
    assert runtime.templater.render_string(source, context) == "controller/None"


@pytest.mark.parametrize(("expression", "expected"), [
    ("vars.row.copy()[0]", 17),
    ("vars.row.copy().pop(0)", 17),
    ("[rows.remove(17), rows | length] | last", 1),
    ("vars.row.copy() < [99, 99]", True),
    ("[99, 99] > vars.row.copy()", True),
    ("vars.row.copy() != [99, 99]", True),
])
def test_copy_of_deferred_task_list_renders_only_selected_member(expression, expected):
    frames, calls = [], []
    runtime = Runtime({}, "", {"inventory_hostname": "h"}, on_result=frames.append)
    runtime.templater.env.globals["lookup"] = lambda name: calls.append(name) or "unused"
    task = _task("1", "Copy row", "debug", args={"msg": "{{ " + expression + " }}"}, vars={
        "number": 17, "row": ["{{ number }}", "{{ lookup('source') }}/tail"], "rows": "{{ vars.row.copy() }}",
    })
    runtime.run_play({"tasks": [task], "gather_facts": False, "vars": {}})
    assert frames[0]["result"]["msg"] == expected
    assert calls == []


@pytest.mark.parametrize("expression", [
    "settings.copy().used",
    "settings.copy().setdefault('used')",
    "settings.copy().pop('used')",
    "settings.copy().popitem()[1]",
])
def test_copy_of_deferred_task_mapping_renders_only_selected_member(expression):
    calls = []
    runtime = Runtime({}, "", {"inventory_hostname": "h"})
    runtime.templater.env.globals["lookup"] = lambda name: calls.append(name) or name
    task = _task("1", "Read a copied setting", "debug", loop=["sample"], vars={
        "suffix": "{{ item }}",
        "settings": {"unused": "{{ lookup('unused') }}/{{ suffix }}",
                     "used": "{{ lookup('chosen') }}/{{ suffix }}"},
    })
    assert runtime.template("{{ " + expression + " }}", runtime._task_ctx(task, {"item": "sample"})) == "chosen/sample"
    assert calls == ["chosen"]


@pytest.mark.parametrize(("mutation", "read"), [
    ("copied.append(source)", "copied[-1]"),
    ("copied.extend([source])", "copied.copy().pop()"),
    ("copied.insert(0, source)", "(copied[:1] | first)"),
])
def test_task_list_copy_keeps_written_values_literal(mutation, read):
    calls = []
    runtime = Runtime({}, "", {"inventory_hostname": "h"})
    runtime.templater.env.globals["lookup"] = lambda name: calls.append(name) or name
    task = _task("1", "Write a copied list", "debug", loop=["sample"], vars={
        "rows": ["{{ lookup('unread') }}/tail", "{{ item }}"],
    })
    template = ("{% set copied = rows.copy() %}{{ copied[1] }}|"
                "{% set source = \"{{ lookup('written') }}\" %}"
                "{% set ignored = " + mutation + " %}{{ " + read + " }}")
    assert runtime.template(template, runtime._task_ctx(task, {"item": "sample"})) == (
        "sample|{{ lookup('written') }}")
    assert not calls


@pytest.mark.parametrize("mapping", ["vars", "hostvars[inventory_hostname]", "hostvars.other"])
@pytest.mark.parametrize(("mutation", "read"), [
    ("copied.setdefault('new', source)", "copied.new"),
    ("copied.update({'original': source})", "copied.copy().pop('original')"),
])
def test_magic_mapping_copy_keeps_written_values_literal(mapping, mutation, read):
    calls = []
    runtime = Runtime({}, "", {"inventory_hostname": "h", "original": 17,
                               "hostvars": {"h": {"original": 17}, "other": {"original": 17}}})
    runtime.templater.env.globals["lookup"] = lambda name: calls.append(name) or name
    task = _task("1", "Write copied magic variables", "debug", vars={
        "unused": "{{ lookup('unread') }}/tail",
    })
    template = ("{% set copied = " + mapping + ".copy() %}{{ copied.original }}|"
                "{% set source = \"{{ lookup('written') }}\" %}"
                "{% set ignored = " + mutation + " %}{{ " + read + " }}")
    assert runtime.template(template, runtime._task_ctx(task, {})) == "17|{{ lookup('written') }}"
    assert not calls


@pytest.mark.parametrize(("nested", "mutation", "expected"), [
    (["original"], "copied.nested.append('added')", ["original", "added"]),
    ({"original": 1}, "copied.nested.update(added=2)", {"original": 1, "added": 2}),
])
@pytest.mark.parametrize("read", [
    "copied.nested",
    "copied.copy().get('nested')",
    "(copied.items() | selectattr('0', 'equalto', 'nested') | first)[1]",
])
def test_vars_copy_preserves_nested_mutations(nested, mutation, expected, read):
    runtime = Runtime({}, "", {"inventory_hostname": "h", "nested": nested})
    runtime.templater.env.globals["lookup"] = lambda name: pytest.fail("looked up " + name)
    task = _task("1", "Mutate a copied variable", "debug", vars={
        "unused": "{{ lookup('unread') }}/tail",
    })
    template = ("{% set copied = vars.copy() %}{% set ignored = " + mutation + " %}"
                "{{ " + read + " | to_json }}")
    assert json.loads(runtime.template(template, runtime._task_ctx(task, {}))) == expected


def test_legacy_vars_copy_iteration_keeps_unmodified_inventory_members_literal(monkeypatch):
    from ansiroll import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_ANSIROLL_CORE_VERSION", "2.12.10")
    literal = "{{ lookup('inventory') }}"
    runtime = Runtime({}, "", {"payload": {"child": literal, "nested": {"items": ["original"]}}})
    calls = []
    runtime.templater.env.globals["lookup"] = lambda name: calls.append(name) or name
    task = _task("1", "Iterate a copied variable", "debug", vars={"unused": "{{ lookup('unread') }}/tail"})
    template = ("{% set copied = vars.copy() %}{% set loaded = copied.payload.child %}"
                "{% set ignored = copied.payload.nested['items'].append('added') %}"
                "{% set iterated = (copied.items() | selectattr('0', 'equalto', 'payload') | first)[1] %}"
                "{{ [loaded, copied.payload.nested['items'], iterated] | to_json }}")
    assert json.loads(runtime.template(template, runtime._task_ctx(task, {}))) == [
        "inventory", ["original", "added"], {"child": literal, "nested": {"items": ["original", "added"]}},
    ]
    assert calls == ["inventory"]


def test_legacy_vars_copy_list_operations_preserve_inventory_sources(monkeypatch):
    from ansiroll import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_ANSIROLL_CORE_VERSION", "2.12.10")
    literal = "{{ lookup('number') }}"
    runtime = Runtime({}, "", {"nums": [3, 1, literal]})
    runtime.templater.env.globals["lookup"] = lambda name: 2 if name == "number" else pytest.fail(name)
    task = _task("1", "Sort inventory list", "debug", vars={"unused": "{{ lookup('unread') }}/tail"})
    template = ("{% set copied = vars.copy() %}{% set ignored = copied.nums.sort() %}"
                "{% set removed = copied.nums.remove(1) %}"
                "{% set entry = (copied.items() | selectattr('0', 'equalto', 'nums') | first)[1] %}"
                "{{ [copied.nums < [4], copied.nums, entry] | to_json }}")
    assert json.loads(runtime.template(template, runtime._task_ctx(task, {}))) == [
        True, [2, 3], [literal, 3],
    ]


@pytest.mark.parametrize("filter_name", ["to_json", "tojson"])
def test_legacy_vars_copy_iteration_and_direct_reads_share_mutations(monkeypatch, filter_name):
    from ansiroll import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_ANSIROLL_CORE_VERSION", "2.12.10")
    literal = "{{ lookup('inventory') }}"
    runtime = Runtime({}, "", {"payload": {"child": literal, "nested": [1]}})
    runtime.templater.env.globals["lookup"] = lambda name: pytest.fail(name)
    task = _task("1", "Mutate an iteration entry", "debug", vars={"unused": "{{ lookup('unread') }}/tail"})
    template = ("{% set copied = vars.copy() %}{% set payload = copied.payload %}"
                "{% set entry = (copied.items() | selectattr('0', 'equalto', 'payload') | first)[1] %}"
                "{% set changed = entry.update(added=2) %}{% set appended = entry.nested.append(3) %}"
                "{{ [copied.payload.added, payload.nested, entry] | " + filter_name + " }}")
    assert json.loads(runtime.template(template, runtime._task_ctx(task, {}))) == [
        2, [1, 3], {"added": 2, "child": literal, "nested": [1, 3]},
    ]


def test_deep_copy_of_legacy_vars_detaches_data_without_resolving_unused_inputs(monkeypatch):
    from ansiroll import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_ANSIROLL_CORE_VERSION", "2.12.10")
    runtime = Runtime({}, "", {"payload": {"nested": [1]}})
    runtime.templater.env.globals["lookup"] = lambda name: pytest.fail(name)
    runtime.templater.register_plugins([{"kind": "filter", "name": "deep_copy", "source": """
from copy import deepcopy

class FilterModule:
    def filters(self):
        return {'deep_copy': deepcopy}
"""}])
    task = _task("1", "Deep copy vars", "debug", vars={"unused": "{{ lookup('unread') }}/tail"})
    template = ("{% set copied = vars.copy() %}{% set seen = copied.payload.nested.append(2) %}"
                "{% set detached = copied | deep_copy %}{% set changed = detached.payload.nested.append(3) %}"
                "{{ [copied.payload.nested, detached.payload.nested] | to_json }}")
    assert json.loads(runtime.template(template, runtime._task_ctx(task, {}))) == [[1, 2], [1, 2, 3]]


def test_legacy_inventory_views_work_in_collection_filters_and_dict_lookup(monkeypatch):
    from ansiroll import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_ANSIROLL_CORE_VERSION", "2.12.10")
    runtime = Runtime({}, "", {"payload": {"child": {"keep": 1}, "nested": [[1], [2, [3]]]}})
    runtime.templater.env.globals["lookup"] = lambda name: pytest.fail(name)
    task = _task("1", "Read raw inventory collections", "debug", vars={"unused": "{{ lookup('unread') }}/tail"})
    template = ("{% set entry = (vars.copy().items() | selectattr('0', 'equalto', 'payload') | first)[1] %}"
                "{{ [(entry | combine({'child': {'add': 2}}, recursive=true)).child,"
                " entry.nested | flatten, query('dict', entry.child)] | to_json }}")
    assert json.loads(runtime.template(template, runtime._task_ctx(task, {}))) == [
        {"keep": 1, "add": 2}, [1, 2, 3], [{"key": "keep", "value": 1}],
    ]


@pytest.mark.parametrize("filter_name", ["to_yaml", "to_nice_yaml"])
def test_legacy_inventory_views_serialize_yaml_and_keep_bindings_when_reversed(monkeypatch, filter_name):
    from ansiroll import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_ANSIROLL_CORE_VERSION", "2.12.10")
    literal = "{{ lookup('raw') }}"
    runtime = Runtime({}, "", {"payload": {"nested": [literal, "plain"]}})
    calls = []
    runtime.templater.env.globals["lookup"] = lambda name: calls.append(name) or "rendered"
    task = _task("1", "Reverse raw inventory", "debug", vars={"unused": "{{ lookup('unread') }}/tail"})
    template = ("{% set copied = vars.copy() %}"
                "{% set entry = (copied.items() | selectattr('0', 'equalto', 'payload') | first)[1] %}"
                "{% set ignored = entry.nested.reverse() %}"
                "{{ [entry | " + filter_name + " | from_yaml, copied.payload.nested] | to_json }}")
    assert json.loads(runtime.template(template, runtime._task_ctx(task, {}))) == [
        {"nested": ["plain", literal]}, ["plain", "rendered"],
    ]
    assert calls == ["raw"]


def test_custom_plugin_receives_builtin_inventory_collections_and_keeps_writes(monkeypatch):
    from ansiroll import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_ANSIROLL_CORE_VERSION", "2.12.10")
    literal = "{{ lookup('raw') }}"
    runtime = Runtime({}, "", {"payload": {"nested": [literal, "plain"]}})
    calls = []
    runtime.templater.env.globals["lookup"] = lambda name: calls.append(name) or "rendered"
    runtime.templater.register_plugins([{"kind": "filter", "name": "inspect_inventory", "source": """
import json

def inspect_inventory(entry):
    assert type(entry) is dict
    assert type(entry['nested']) is list
    before = json.dumps(entry)
    entry.update(added=2)
    entry['nested'].reverse()
    return before

class FilterModule:
    def filters(self):
        return {'inspect_inventory': inspect_inventory}
"""}])
    task = _task("1", "Inspect raw inventory in Python", "debug", vars={"unused": "{{ lookup('unread') }}/tail"})
    template = ("{% set copied = vars.copy() %}"
                "{% set entry = (copied.items() | selectattr('0', 'equalto', 'payload') | first)[1] %}"
                "{% set before = entry | inspect_inventory | from_json %}"
                "{{ [before, copied.payload.added, copied.payload.nested] | to_json }}")
    assert json.loads(runtime.template(template, runtime._task_ctx(task, {}))) == [
        {"nested": [literal, "plain"]}, 2, ["plain", "rendered"],
    ]
    assert calls == ["raw"]


@pytest.mark.parametrize("batch, member", [("[entry]", "0"), ("{'entry': entry}", "'entry'")])
def test_custom_filter_keeps_inventory_views_in_caller_containers(monkeypatch, batch, member):
    from ansiroll import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_ANSIROLL_CORE_VERSION", "2.12.10")
    runtime = Runtime({}, "", {"payload": {"count": 1}})
    runtime.templater.env.globals["lookup"] = lambda name: pytest.fail(name)
    runtime.templater.register_plugins([{"kind": "filter", "name": "increment", "source": """
def increment(batch, key):
    assert type(batch[key]) is dict
    batch[key]['count'] += 1
    return ''

class FilterModule:
    def filters(self):
        return {'increment': increment}
"""}])
    task = _task("1", "Mutate a nested plugin argument twice", "debug",
                 vars={"unused": "{{ lookup('unread') }}/tail"})
    template = ("{% set copied = vars.copy() %}"
                "{% set entry = (copied.items() | selectattr('0', 'equalto', 'payload') | first)[1] %}"
                "{% set batch = " + batch + " %}"
                "{{ batch | increment(" + member + ") }}{{ batch | increment(" + member + ") }}"
                "{{ [copied.payload.count, entry.count, batch[" + member + "].count] | to_json }}")
    assert json.loads(runtime.template(template, runtime._task_ctx(task, {}))) == [3, 3, 3]


def test_custom_filter_keeps_inventory_writes_from_jinja_callbacks(monkeypatch):
    from ansiroll import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_ANSIROLL_CORE_VERSION", "2.12.10")
    runtime = Runtime({}, "", {"payload": [1]})
    runtime.templater.env.globals["lookup"] = lambda name: pytest.fail(name)
    runtime.templater.register_plugins([{"kind": "filter", "name": "update_inventory", "source": """
def update_inventory(values, callback):
    values.append(2)
    callback()
    values.append(4)
    return ''

class FilterModule:
    def filters(self):
        return {'update_inventory': update_inventory}
"""}])
    task = _task("1", "Update inventory around a Jinja callback", "debug",
                 vars={"unused": "{{ lookup('unread') }}/tail"})
    template = ("{% set copied = vars.copy() %}"
                "{% set entry = (copied.items() | selectattr('0', 'equalto', 'payload') | first)[1] %}"
                "{% macro update() %}{% set ignored = entry.append(3) %}{% endmacro %}"
                "{{ entry | update_inventory(update) }}"
                "{{ [entry, copied.payload] | to_json }}")
    result = runtime.template(template, runtime._task_ctx(task, {}))
    assert json.loads(str(result)) == [[1, 2, 3, 4]] * 2


def test_custom_filter_writes_inventory_keys_in_the_final_order(monkeypatch):
    from ansiroll import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_ANSIROLL_CORE_VERSION", "2.12.10")
    runtime = Runtime({}, "", {"payload": {"a": 1, "b": 2}})
    runtime.templater.env.globals["lookup"] = lambda name: pytest.fail(name)
    runtime.templater.register_plugins([{"kind": "filter", "name": "reorder_inventory", "source": """
def reorder_inventory(entry):
    entry['a'] = entry.pop('a')
    return ''

class FilterModule:
    def filters(self):
        return {'reorder_inventory': reorder_inventory}
"""}])
    task = _task("1", "Reinsert a mapping key", "debug", vars={"unused": "{{ lookup('unread') }}/tail"})
    template = ("{% set copied = vars.copy() %}"
                "{% set entry = (copied.items() | selectattr('0', 'equalto', 'payload') | first)[1] %}"
                "{{ entry | reorder_inventory }}"
                "{{ [entry.keys() | list, copied.payload.keys() | list] | to_json }}")
    result = runtime.template(template, runtime._task_ctx(task, {}))
    assert json.loads(str(result)) == [["b", "a"]] * 2


@pytest.mark.parametrize("lookup", ["lookup", "query"])
def test_custom_lookups_receive_builtin_inventory_collections(monkeypatch, lookup):
    from ansiroll import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_ANSIROLL_CORE_VERSION", "2.12.10")
    runtime = Runtime({}, "", {"count": 3, "payload": {"values": ["{{ count }}"]}})
    runtime.templater.register_plugins([{"kind": "lookup", "name": "encode_inventory", "source": """
import json

class LookupModule:
    def run(self, terms, variables, **kwargs):
        return [json.dumps([terms[0].pop('values'), kwargs['values']])]
"""}])
    task = _task("1", "Serialize an inventory lookup term", "debug",
                 vars={"unused": "{{ lookup('unread') }}/tail"})
    expression = lookup + "('encode_inventory', entry, values=entry['values'])"
    if lookup == "query":
        expression += " | first"
    template = ("{% set entry = (vars.copy().items() | selectattr('0', 'equalto', 'payload') | first)[1] %}"
                "{% set result = " + expression + " %}"
                "{{ [result | from_json, entry['values']] | to_json }}")
    result = runtime.template(template, runtime._task_ctx(task, {}))
    assert json.loads(str(result)) == [[[3], [3]], ["{{ count }}"]]


def test_generator_filter_keeps_its_yielded_collections_builtin(monkeypatch):
    from ansiroll import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_ANSIROLL_CORE_VERSION", "2.12.10")
    runtime = Runtime({}, "", {"payload": [1]})
    runtime.templater.env.globals["lookup"] = lambda name: pytest.fail(name)
    runtime.templater.register_plugins([{"kind": "filter", "name": "generate_inventory", "source": """
import json

def generate_inventory(values):
    item = [values]
    yield item
    yield json.dumps(item)

class FilterModule:
    def filters(self):
        return {'generate_inventory': generate_inventory}
"""}])
    task = _task("1", "Reuse a yielded collection", "debug", vars={"unused": "{{ lookup('unread') }}/tail"})
    template = ("{% set entry = (vars.copy().items() | selectattr('0', 'equalto', 'payload') | first)[1] %}"
                "{{ entry | generate_inventory | list | to_json }}")
    result = runtime.template(template, runtime._task_ctx(task, {}))
    assert json.loads(str(result)) == [[[1]], "[[1]]"]


def test_custom_filter_result_keeps_plugin_owned_collections(monkeypatch):
    from ansiroll import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_ANSIROLL_CORE_VERSION", "2.12.10")
    runtime = Runtime({}, "", {"payload": [1]})
    runtime.templater.env.globals["lookup"] = lambda name: pytest.fail(name)
    runtime.templater.register_plugins([{"kind": "filter", "name": "cache_inventory", "source": """
import json

cache = []

def cache_inventory(values):
    cache[:] = [values]
    return cache

def generate_independent(values):
    item = [0]
    yield item
    item[0] = 1
    yield item

class FilterModule:
    def filters(self):
        return {'cache_inventory': cache_inventory, 'read_cache': lambda _: json.dumps(cache),
                'generate_independent': generate_independent}
"""}])
    task = _task("1", "Keep plugin result identity where possible", "debug",
                 vars={"unused": "{{ lookup('unread') }}/tail"})
    template = ("{% set entry = (vars.copy().items() | selectattr('0', 'equalto', 'payload') | first)[1] %}"
                "{% set cached = entry | cache_inventory %}"
                "{{ [cached, entry | read_cache | from_json, entry | generate_independent | list] | to_json }}")
    result = runtime.template(template, runtime._task_ctx(task, {}))
    assert json.loads(str(result)) == [[[1]], [[1]], [[1], [1]]]


@pytest.mark.parametrize(("payload", "key"), [({"enabled": True}, "enabled"), ([True], 0)])
def test_raw_inventory_collection_can_cross_delegation_and_render_as_json(monkeypatch, payload, key):
    from ansiroll import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_ANSIROLL_CORE_VERSION", "2.12.10")
    runtime = Runtime({}, "", {"payload": payload})
    runtime.templater.env.globals["lookup"] = lambda name: pytest.fail(name)
    task = _task("1", "Read a raw inventory entry", "debug", vars={"unused": "{{ lookup('unread') }}/tail"})
    source = "{{ (vars.copy().items() | selectattr('0', 'equalto', 'payload') | first)[1] }}"
    entry = runtime.templater.render_string(source, runtime._task_ctx(task, {}))
    assert json.loads(runtime_module._as_text(entry)) == payload
    scope = {"alias": entry}
    resolve = runtime_module._resolve_delegated_play_var
    assert resolve("alias", scope, runtime.templater) == {"value": payload, "undefined": []}
    members = []
    slots = {key: 0} if isinstance(payload, dict) else [0]
    assert resolve("alias", scope, runtime.templater, members=members) == {
        "value": slots, "undefined": [], "lazy_members": True,
    }
    assert resolve(None, scope, runtime.templater, members=members, member=0) == {
        "value": True, "undefined": [],
    }


@pytest.mark.parametrize("body", [
    "{{ copied.used }}/{{ copied.used }}",
    "{{ copied.used }}/{{ copied.setdefault('used') }}/{{ copied.pop('used') }}",
    "{% set other = copied.copy() %}{{ copied.used }}/{{ other.used }}",
])
def test_task_mapping_copy_reuses_the_first_resolved_member(body):
    calls = []
    runtime = Runtime({}, "", {"inventory_hostname": "h"})

    def lookup(name):
        calls.append(name)
        return len(calls)

    runtime.templater.env.globals["lookup"] = lookup
    task = _task("1", "Read a copied setting twice", "debug", loop=["sample"], vars={
        "settings": {"used": "{{ lookup('chosen') }}:{{ item }}", "unused": "{{ lookup('unused') }}/tail"},
    })
    template = "{% set copied = settings.copy() %}" + body
    result = runtime.template(template, runtime._task_ctx(task, {"item": "sample"}))
    assert result.split("/") == ["1:sample"] * body.count("{{")
    assert calls == ["chosen"]


@pytest.mark.parametrize(("mutation", "expression"), [
    ("copied.setdefault('new', source)", "copied.new"),
    ("copied.update({'used': source})", "copied.used"),
    ("copied.update(used=source)", "copied.copy().pop('used')"),
    ("copied.update({'used': source})", "copied.setdefault('used')"),
    ("copied.update({'used': source})", "copied.popitem()[1]"),
])
def test_task_mapping_copy_keeps_written_values_literal(mutation, expression):
    calls = []
    runtime = Runtime({}, "", {"inventory_hostname": "h"})
    runtime.templater.env.globals["lookup"] = lambda name: calls.append(name) or name
    task = _task("1", "Write a copied setting", "debug", loop=["sample"], vars={
        "settings": {"kept": "{{ lookup('kept') }}/tail", "used": "{{ lookup('original') }}/tail"},
    })
    template = ("{% set copied = settings.copy() %}{{ copied.kept }}|"
                "{% set source = \"{{ lookup('written') }}\" %}"
                "{% set ignored = " + mutation + " %}{{ " + expression + " }}")
    assert runtime.template(template, runtime._task_ctx(task, {"item": "sample"})) == (
        "kept/tail|{{ lookup('written') }}")
    assert calls == ["kept"]


def test_task_mapping_copy_reuses_a_captured_lookup_error():
    from ansiroll.runtime import _captured, _is_undef

    calls = []
    runtime = Runtime({}, "", {"inventory_hostname": "h"})

    def lookup(name):
        calls.append(name)
        return _captured("failed lookup")

    runtime.templater.env.globals["lookup"] = lookup
    task = _task("1", "Read a failed setting twice", "debug", loop=["sample"], vars={
        "settings": {"used": "{{ lookup('chosen') }}/tail", "unused": "{{ lookup('unused') }}/tail"},
    })
    ctx = runtime._task_ctx(task, {"item": "sample"})
    copied = runtime.templater.render_string("{{ settings.copy() }}", ctx)
    first = copied["used"]
    assert _is_undef(first)
    assert copied["used"] is first
    assert calls == ["chosen"]


def test_sort_of_deferred_task_list_compares_rendered_members():
    frames, calls = [], []
    runtime = Runtime({}, "", {"inventory_hostname": "h"}, on_result=frames.append)
    runtime.templater.env.globals["lookup"] = lambda name: calls.append(name) or "unused"
    task = _task("1", "Sort row", "debug", args={"msg": "{{ [rows.sort(), rows] | last }}"}, vars={
        "number": 17, "row": ["{{ number }}", 2], "rows": "{{ vars.row.copy() }}",
        "spare": "{{ lookup('source') }}/tail",
    })
    runtime.run_play({"tasks": [task], "gather_facts": False, "vars": {}})
    assert frames[0]["result"]["msg"] == [2, 17]
    assert calls == []


@pytest.mark.parametrize(("expression", "expected"), [
    ("[row.sort(), row] | last", [2, 17]),
    ("[row.pop(), vars.row] | last", [17]),
    ("[vars.row.pop(), hostvars[inventory_hostname].row] | last", [17]),
    ("[nested.rows.pop(), nested.rows] | last", [17]),
])
def test_deferred_task_container_mutations_share_a_render_view(expression, expected):
    runtime, frames = run([_task("1", "Mutate row", "debug", args={"msg": "{{ " + expression + " }}"}, vars={
        "row": [17, 2], "nested": {"rows": [17, 2]}, "spare": "{{ lookup('unused') }}/tail",
    })])
    assert not runtime.failed
    assert frames[0]["result"]["msg"] == expected


@pytest.mark.parametrize("scope", ["hostvars", "vars.hostvars"])
def test_current_hostvars_resolve_deferred_task_aliases(scope):
    frames, calls = [], []
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": {"h": {"alias": 1}}},
                      on_result=frames.append)
    runtime.templater.env.globals["lookup"] = lambda name: calls.append(name) or 17
    task = _task("1", "Read alias", "debug", args={
        "msg": "{{ [" + scope + "[inventory_hostname].alias, alias] }}",
    }, vars={"alias": "{{ lookup('number') }}", "spare": "{{ lookup('unused') }}/tail"})
    runtime.run_play({"tasks": [task], "gather_facts": False, "vars": {}})
    assert frames[0]["result"]["msg"] == [17, 17]
    assert calls == ["number"]


def test_deferred_task_alias_can_read_its_inventory_hostvar():
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "h", "alias": 17}, on_result=frames.append)
    task = _task("1", "Read inventory fallback", "debug", args={"msg": "{{ alias }}"}, vars={
        "alias": "{{ hostvars[inventory_hostname].alias }}",
    })
    runtime.run_play({"tasks": [task], "gather_facts": False, "vars": {}})
    assert frames[0]["result"]["msg"] == 17


@pytest.mark.parametrize(("member", "template_value", "inventory_value"), [
    ("key", {"key": "{{ hostvars[inventory_hostname].alias.key }}"}, {"key": 17}),
    ("0", ["{{ hostvars[inventory_hostname].alias[0] }}"], [17]),
])
def test_deferred_task_container_can_read_its_inventory_hostvar(member, template_value, inventory_value):
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "h", "alias": inventory_value}, on_result=frames.append)
    task = _task("1", "Read inventory fallback", "debug", args={"msg": "{{ alias." + member + " }}"}, vars={
        "alias": template_value,
    })
    runtime.run_play({"tasks": [task], "gather_facts": False, "vars": {}})
    assert frames[0]["result"]["msg"] == 17


def test_deferred_task_container_can_default_a_missing_inventory_hostvar():
    runtime, frames = run([_task("1", "Default missing inventory", "debug", args={
        "msg": "{{ [alias.value, alias.available] }}",
    }, vars={"alias": {
        "value": "{{ hostvars[inventory_hostname].alias.b | default(17) }}",
        "available": "{{ hostvars[inventory_hostname].alias.b is defined }}",
    }})])
    assert not runtime.failed
    assert frames[0]["result"]["msg"] == [17, False]


@pytest.mark.parametrize("method", ["pop('alias')", "setdefault('alias')"])
def test_current_hostvars_methods_resolve_deferred_task_aliases(method):
    _, frames = run([_task("1", "Read hostvars", "debug", args={
        "msg": "{{ hostvars[inventory_hostname]." + method + " }}",
    }, vars={"number": 17, "alias": "{{ number }}"})])
    assert frames[0]["result"]["msg"] == 17


def test_custom_lookup_reads_deferred_task_alias_from_hostvars():
    frames = []
    runtime = Runtime({}, "", {"inventory_hostname": "h"}, on_result=frames.append)
    runtime.templater._lookup_loaded["host_alias"] = lambda terms, variables: [variables["hostvars"]["h"]["alias"]]
    task = _task("1", "Read hostvar in lookup", "debug", args={"msg": "{{ payload }}"}, vars={
        "alias": "{{ number }}", "number": 17, "payload": "{{ lookup('host_alias') }}/tail",
    })
    runtime.run_play({"tasks": [task], "gather_facts": False, "vars": {}})
    assert frames[0]["result"]["msg"] == "17/tail"


@pytest.mark.parametrize("copy", [False, True], ids=["direct", "copy"])
@pytest.mark.parametrize("expression", [
    "({view}.items() | selectattr('0', 'equalto', 'original') | first)[1]",
    "{view}.items()[0][1]",
    "{view}.values()[0]",
    "{view}.original",
])
def test_legacy_hostvars_entries_render_only_accessed_sources(monkeypatch, expression, copy):
    import ansiroll.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", "2.12.10")
    frames = []
    host_vars = {"inventory_hostname": "h", "hostvars": {"h": {"original": "{{ seed }}"}}, "seed": "ready"}
    runtime = Runtime({}, "", host_vars, on_result=frames.append)
    calls = []
    runtime.templater.env.globals["lookup"] = lambda name: calls.append(name) or "unused"
    view = "hostvars[inventory_hostname].copy()" if copy else "hostvars[inventory_hostname]"
    task = _task("1", "Iterate hostvars", "debug", args={
        "msg": "{{ " + expression.format(view=view) + " }}",
    }, vars={"unused": "{{ lookup('unused') }}/tail"})
    runtime.run_play({"tasks": [task], "gather_facts": False, "vars": {}})
    assert frames[0]["result"]["msg"] == "ready"
    assert not calls


def test_hostvars_entries_keep_deferred_bindings_in_task_order():
    calls = []
    runtime = Runtime({}, "", {"inventory_hostname": "h"})
    runtime.templater.env.globals["lookup"] = lambda name: calls.append(name) or 17
    task = _task("1", "Read task entry", "debug", args={
        "msg": "{{ hostvars[inventory_hostname].items()[1] }}",
    }, vars={"alias": "{{ lookup('number') }}", "ordinary": 2, "unused": "{{ lookup('spare') }}/tail"})
    ctx = runtime._task_ctx(task, {})
    assert runtime.template(task["args"]["msg"], ctx) == ("alias", 17)
    assert calls == ["number"]


@pytest.mark.parametrize(("expression", "expected"), [
    ("entries == []", False), ("entries != []", True), ("[] == entries", False),
    ("prefix == entries", False), ("entries != prefix", True),
    ("prefix != entries", True),
    ("entries < [18]", True), ("entries >= [18]", False), ("[18] > entries", True),
    ("entries[:1] == [17]", True), ("entries > [17]", True),
])
def test_hostvars_comparisons_leave_unneeded_values_unread(expression, expected):
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": {"h": {"original": 17}}})
    runtime.templater.env.globals["lookup"] = lambda name: pytest.fail("unrelated lookup ran")
    task = _task("1", "Compare hostvars entries", "debug", vars={
        "unused": "{{ lookup('unused') }}/tail", "prefix": ["{{ lookup('unused') }}/tail"],
    })
    template = ("{% set entries = hostvars[inventory_hostname].copy().values() %}"
                "{{ (" + expression + ") | to_json }}")
    assert json.loads(runtime.template(template, runtime._task_ctx(task, {}))) is expected


@pytest.mark.parametrize("hostvars", ["lookup('vars', 'hostvars')", "query('vars', 'hostvars')[0]"])
def test_vars_lookup_preserves_lazy_hostvars_members(hostvars):
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": {"h": {"original": "{{ seed }}"}},
                                "seed": "ready"})
    runtime.templater._lookup_loaded["unused"] = lambda *args, **kwargs: pytest.fail("unrelated lookup ran")
    task = _task("1", "Read a hostvars lookup member", "debug", vars={"unused": "{{ lookup('unused') }}/tail"})
    expression = "{{ " + hostvars + "[inventory_hostname].copy().original }}"
    assert runtime.template(expression, runtime._task_ctx(task, {})) == "ready"


@pytest.mark.parametrize("expression", [
    "lookup('vars', 'hostvars')", "query('vars', 'hostvars')[0]", "{inventory_hostname: hostvars.h.copy()}",
])
@pytest.mark.parametrize("whole_result", [True, False])
def test_vars_lookup_hostvars_resolves_each_value_once(expression, whole_result):
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": {"h": {
        "original": "{{ lookup('counting') }}",
    }}})
    calls = []

    def counting(*args, **kwargs):
        calls.append(None)
        return [len(calls)]

    runtime.templater._lookup_loaded["counting"] = counting
    task = _task("1", "Read the whole hostvars lookup", "debug", vars={
        "mixed": "{{ lookup('env', 'ANSIROLL_HOSTVARS_UNUSED') }}/tail",
    })
    ctx = runtime._task_ctx(task, {})
    if whole_result:
        result = runtime.template("{{ " + expression + " }}", ctx)
        assert type(result) is dict
        assert json.loads(json.dumps(result))["h"]["original"] == 1
    else:
        template = "{% set hosts = " + expression + " %}{{ hosts.h.original }}/{{ hosts.h.original }}"
        assert runtime.template(template, ctx) == "1/1"
    assert len(calls) == 1


@pytest.mark.parametrize("filter_name", [None, "to_json", "to_nice_json"])
def test_hostvars_lookup_is_read_once_by_legacy_debug_and_json(monkeypatch, filter_name):
    monkeypatch.setattr("ansiroll.runtime._ANSIROLL_CORE_VERSION", "2.12.10")
    frames, calls = [], []
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": {"h": {
        "original": "{{ lookup('counting') }}",
    }}}, on_result=frames.append)

    def counting(*args, **kwargs):
        calls.append(None)
        return [len(calls)]

    runtime.templater._lookup_loaded["counting"] = counting
    expression = "lookup('vars', 'hostvars')"
    args = {"msg": "{{ " + expression + " | " + filter_name + " }}"} if filter_name else {"var": expression}
    task = _task("1", "Read the whole hostvars lookup", "debug", args=args, vars={
        "mixed": "{{ lookup('env', 'ANSIROLL_HOSTVARS_UNUSED') }}/tail",
    })
    runtime.run_play({"tasks": [task], "gather_facts": False, "vars": {}})
    assert frames[0]["status"] == "ok"
    result = frames[0]["result"]
    rendered = json.loads(result["msg"]) if filter_name else result[expression]
    assert rendered["h"]["original"] == 1
    assert len(calls) == 1


@pytest.mark.parametrize("filter_name", ["to_json", "to_nice_json"])
def test_json_vault_error_does_not_resolve_later_hostvars(filter_name):
    import jinja2

    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": {"h": {
        "original": "{{ lookup('unused') }}",
    }}, "vault_value": {"__ansiroll_vault__": "ciphertext"}})
    runtime.templater._lookup_loaded["unused"] = lambda *args, **kwargs: pytest.fail("unneeded lookup ran")
    task = _task("1", "Serialize hostvars after vault", "debug", vars={
        "mixed": "{{ lookup('env', 'ANSIROLL_HOSTVARS_UNUSED') }}/tail",
    })
    expression = "{{ {'vault': vault_value, 'later': lookup('vars', 'hostvars').h} | " + filter_name + " }}"
    with pytest.raises(jinja2.UndefinedError, match="undecryptable variable"):
        str(runtime.template(expression, runtime._task_ctx(task, {})))


@pytest.mark.parametrize("container", ["list", "dict", "hostvars_copy"])
def test_custom_filter_receives_hostvars_values_as_a_list(container):
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": {"h": {"original": 17}}})
    runtime.templater.register_plugins([{"kind": "filter", "name": "dump", "source": """
import json
class FilterModule:
    def filters(self):
        return {'dump_hostvars': json.dumps}
"""}])
    runtime.templater._lookup_loaded["sample"] = lambda *args, **kwargs: ["value"]
    task = _task("1", "Serialize hostvars through a custom filter", "debug", vars={
        "alias": "{{ lookup('sample') }}/tail",
    })
    ctx = runtime._task_ctx(task, {})
    templates = {
        "list": "{{ hostvars.h.values() | dump_hostvars }}",
        "dict": "{{ {'values': hostvars.h.values()} | dump_hostvars }}",
        "hostvars_copy": ("{% set snapshot = hostvars.h.copy() %}"
                          "{% set unused = snapshot.update({'values': hostvars.h.values()}) %}"
                          "{{ snapshot | dump_hostvars }}"),
    }
    result = json.loads(runtime.template(templates[container], ctx))
    values = result if container == "list" else result["values"]
    assert values[0] == 17
    assert "value/tail" in values


def test_custom_jinja_tests_receive_hostvars_lists_without_loading_other_bindings():
    hostvars = {"h": {"original": 17}, "other": {"value": 23}}
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": hostvars})
    runtime.templater.register_plugins([{"kind": "test", "name": "python_list", "source": """
def python_list(value):
    return isinstance(value, list)

def nested_list(snapshot):
    return isinstance(snapshot['nested'], list)

class TestModule:
    def tests(self):
        return {'python_list': python_list, 'nested_list': nested_list}
"""}])
    runtime.templater.env.globals["lookup"] = lambda name: pytest.fail("unrelated lookup ran")
    task = _task("1", "Inspect hostvars inputs", "debug", vars={"unused": "{{ lookup('unused') }}/tail"})
    ctx = runtime._task_ctx(task, {})
    assert runtime.template("{{ hostvars.other.values() is python_list }}", ctx) is True
    expression = ("{% set snapshot = hostvars.h.copy() %}"
                  "{% set unused = snapshot.update({'nested': hostvars.other.values()}) %}"
                  "{{ snapshot is nested_list }}")
    assert runtime.template(expression, ctx) == "True"


def test_custom_filter_leaves_unread_hostvars_entries_lazy():
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": {"h": {"original": 17}}})
    runtime.templater.register_plugins([{"kind": "filter", "name": "original", "source": """
class FilterModule:
    def filters(self):
        return {'original_hostvar': lambda snapshot: snapshot['original']}
"""}])
    runtime.templater.env.globals["lookup"] = lambda name: pytest.fail("unrelated lookup ran")
    task = _task("1", "Read one hostvar", "debug", vars={"unused": "{{ lookup('unused') }}/tail"})
    template = ("{% set snapshot = hostvars.h.copy() %}"
                "{% set unused = snapshot.update({'nested': hostvars.h.values()}) %}"
                "{{ snapshot | original_hostvar }}")
    assert runtime.template(template, runtime._task_ctx(task, {})) == "17"


@pytest.mark.parametrize("method", ["values", "items"])
@pytest.mark.parametrize("filter_expression", ["encode_entries", "encode_entries_lazily"])
def test_custom_filter_can_serialize_hostvars_methods(method, filter_expression):
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": {"h": {"original": 17}}})
    runtime.templater.register_plugins([{"kind": "filter", "name": "dump", "source": """
import json

def encode(host, method):
    return json.dumps(getattr(host, method)())

def encode_lazily(host, method):
    yield encode(host, method)

class FilterModule:
    def filters(self):
        return {'encode_entries': encode, 'encode_entries_lazily': encode_lazily}
"""}])
    runtime.templater._lookup_loaded["sample"] = lambda *args, **kwargs: ["sample"]
    task = _task("1", "Serialize hostvars entries", "debug", vars={"mixed": "{{ lookup('sample') }}/tail"})
    expression = "hostvars.h | %s('%s')" % (filter_expression, method)
    if filter_expression == "encode_entries_lazily":
        expression += " | first"
    entries = json.loads(runtime.template("{{ " + expression + " }}", runtime._task_ctx(task, {})))
    if method == "items":
        assert dict(entries)["mixed"] == "sample/tail"
    else:
        assert "sample/tail" in entries


def test_custom_filter_can_read_a_lazy_hostvars_alias_without_eager_nested_render():
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": {"h": {"original": 17}}})
    runtime.templater.register_plugins([{"kind": "filter", "name": "selected", "source": """
class FilterModule:
    def filters(self):
        return {'selected_hostvar': lambda snapshot: snapshot['selected']}
"""}])
    runtime.templater.env.globals["lookup"] = lambda name: pytest.fail("unrelated lookup ran")
    task = _task("1", "Read one entry through an alias", "debug", vars={
        "unused": "{{ lookup('unused') }}/tail",
        "selected": ("{% set inner = hostvars.h.copy() %}"
                     "{% set ignored = inner.update({'nested': hostvars.h.values()}) %}"
                     "{{ inner.nested[0] }}"),
    })
    result = runtime.template("{{ hostvars.h.copy() | selected_hostvar }}", runtime._task_ctx(task, {}))
    assert str(result) == "17"


@pytest.mark.parametrize("template", [
    "{% macro selected() %}{{ hostvars.h.values()[0] }}{% endmacro %}{{ selected | invoke_callback }}",
    ("{% for row in [[17]] recursive %}{% if row is number %}{{ hostvars.h.values()[0] }}"
     "{% else %}{{ loop | invoke_callback(row) }}{% endif %}{% endfor %}"),
    ("{% if false %}{% block selected %}{{ hostvars.h.values()[0] }}{% endblock %}{% endif %}"
     "{{ self.selected | invoke_callback }}"),
], ids=["macro", "recursive_loop", "block"])
def test_custom_filter_jinja_callback_keeps_hostvars_entries_lazy(template):
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": {"h": {"original": 17}}})
    runtime.templater.register_plugins([{"kind": "filter", "name": "invoke", "source": """
class FilterModule:
    def filters(self):
        return {'invoke_callback': lambda callback, *args: callback(*args)}
"""}])
    runtime.templater.env.globals["lookup"] = lambda name: pytest.fail("unrelated lookup ran")
    task = _task("1", "Invoke a Jinja callback from a filter", "debug", loop=["sample"],
                 vars={"unused": "{{ lookup('unused') }}/tail"})
    result = runtime.template(template, runtime._task_ctx(task, {"item": "sample"}))
    assert result == "17"


def test_custom_filter_lookahead_keeps_filtered_loop_predicates_lazy():
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": {"h": {"original": 17}}})
    runtime.templater.register_plugins([{"kind": "filter", "name": "next_item", "source": """
class FilterModule:
    def filters(self):
        return {'next_item': lambda loop: '' if loop.index0 else loop.nextitem}
"""}])
    runtime.templater.env.globals["lookup"] = lambda name: pytest.fail("unrelated lookup ran")
    task = _task("1", "Look ahead in a filtered loop", "debug", loop=["sample"],
                 vars={"unused": "{{ lookup('unused') }}/tail"})
    template = ("{% for value in [1, 2] if hostvars.h.values()[0] == 17 recursive %}"
                "{{ loop | next_item }}{% endfor %}")
    result = runtime.template(template, runtime._task_ctx(task, {"item": "sample"}))
    assert result == "2"


def test_custom_filter_mutates_saved_hostvars_values():
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": {"h": {"original": 17}}})
    runtime.templater.register_plugins([{"kind": "filter", "name": "append", "source": """
def append(values):
    values.append('added')
    return ''

def append_nested(snapshot):
    return append(snapshot['nested'])

class FilterModule:
    def filters(self):
        return {'append_hostvar': append, 'append_nested': append_nested}
"""}])
    task = _task("1", "Mutate hostvars entries through a custom filter", "debug", vars={
        "alias": "{{ lookup('sample') }}/tail",
    })
    runtime.templater._lookup_loaded["sample"] = lambda *args, **kwargs: ["value"]
    result = runtime.template(
        "{% set entries = hostvars.h.values() %}{{ entries | append_hostvar }}{{ entries[-1] }}",
        runtime._task_ctx(task, {}),
    )
    assert result == "added"
    result = runtime.template(
        "{% set entries = hostvars.h.values() %}{% set snapshot = hostvars.h.copy() %}"
        "{% set unused = snapshot.update({'nested': entries}) %}"
        "{{ snapshot | append_nested }}{{ entries[-1] }}",
        runtime._task_ctx(task, {}),
    )
    assert result == "added"


@pytest.mark.parametrize("filter_name", ["to_json", "to_nice_json", "to_yaml", "to_nice_yaml"])
def test_serializers_materialize_nested_lazy_hostvars_entries(filter_name):
    from ansiroll.runtime import _yaml_module

    frames, calls = [], []
    runtime = Runtime({}, "", {"inventory_hostname": "h"}, on_result=frames.append)
    runtime.templater.env.globals["lookup"] = lambda name: calls.append(name) or "rendered"
    task = _task("1", "Serialize entries", "debug", args={
        "msg": "{{ {'values': hostvars[inventory_hostname].values()} | " + filter_name + " }}",
    }, vars={"alias": "{{ lookup('source') }}/tail"})
    runtime.run_play({"tasks": [task], "gather_facts": False, "vars": {}})
    assert not runtime.failed
    serialized = frames[0]["result"]["msg"]
    decoded = json.loads(serialized) if "json" in filter_name else _yaml_module().safe_load(serialized)
    assert "rendered/tail" in decoded["values"]
    assert calls == ["source"]


def test_hostvars_item_membership_resolves_only_matching_keys():
    frames, calls = [], []
    hostvars = {"h": {"unused": "{{ lookup('unused') }}/tail"}}
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": hostvars}, on_result=frames.append)
    runtime.templater.env.globals["lookup"] = lambda name: calls.append(name) or "rendered"
    task = _task("1", "Check entries", "debug", args={
        "msg": "{% set entries = hostvars[inventory_hostname].items() %}{{ ["
               "('absent', 0) in entries, ('absent', 0) in entries, ('alias', 'rendered/tail') in entries] | to_json }}",
    }, vars={"alias": "{{ lookup('source') }}/tail"})
    runtime.run_play({"tasks": [task], "gather_facts": False, "vars": {}})
    assert json.loads(frames[0]["result"]["msg"]) == [False, False, True]
    assert calls == ["source"]


@pytest.mark.parametrize(("expression", "expected"), [
    ("(hostvars[inventory_hostname].items() + [('extra', 1)])[-1]", ("extra", 1)),
    ("(['extra'] + hostvars[inventory_hostname].values())[0]", "extra"),
    ("(prefix_list + hostvars[inventory_hostname].values())[0:3]", ["extra", "h", 17]),
    ("hostvars[inventory_hostname].values().copy()[1]", 17),
    ("(hostvars[inventory_hostname].values() * 2)[1]", 17),
    ("hostvars[inventory_hostname].values() > []", True),
])
def test_lazy_hostvars_entries_support_list_operations(expression, expected):
    runtime = Runtime({}, "", {"inventory_hostname": "h"})
    runtime.templater.env.globals["lookup"] = lambda name: 17
    task = _task("1", "Use hostvar entries as lists", "debug", vars={
        "alias": "{{ lookup('source') }}", "unused": "{{ lookup('source') }}/tail", "prefix_list": ["extra"],
    })
    ctx = runtime._task_ctx(task, {})
    assert runtime.template("{{ " + expression + " }}", ctx) == expected


@pytest.mark.parametrize(("plugin", "expected"), [
    ("items", ["h", 17, "17/tail", _OMIT]),
    ("flattened", ["h", 17, "17/tail", _OMIT]),
    ("indexed_items", [[0, "h"], [1, 17], [2, "17/tail"], [3, _OMIT]]),
    ("nested", [["h"], [17], ["17/tail"], [_OMIT]]),
])
def test_loop_lookups_consume_lazy_hostvars_values(plugin, expected):
    runtime = Runtime({}, "", {"inventory_hostname": "h"})
    runtime.templater._lookup_loaded["source"] = lambda terms, variables: [17]
    task = _task("1", "Pass hostvar values to a lookup", "debug", vars={
        "alias": "{{ lookup('source') }}", "mixed": "{{ lookup('source') }}/tail",
    })
    expression = "{{ query('" + plugin + "', hostvars[inventory_hostname].values()) }}"
    assert runtime.template(expression, runtime._task_ctx(task, {})) == expected


def test_lazy_hostvars_entries_keep_mutations_on_the_list():
    runtime = Runtime({}, "", {"inventory_hostname": "h"})
    runtime.templater.env.globals["lookup"] = lambda name: 17
    task = _task("1", "Mutate materialized entries", "debug", vars={"alias": "{{ lookup('source') }}/tail"})
    ctx = runtime._task_ctx(task, {})
    expression = ("{% set current = hostvars[inventory_hostname] %}"
                  "{% set values = current.values() %}"
                  "{% set unused = values.append('extra') %}"
                  "{{ values[-1] }}/{{ current.values()[-1] == 'extra' }}")
    assert runtime.template(expression, ctx) == "extra/False"


def test_hostvars_item_membership_surfaces_matching_entry_render_failure():
    frames = []
    hostvars = {"h": {"broken": "prefix{{ 1 + 'str' }}"}}
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": hostvars}, on_result=frames.append)
    task = _task("1", "Check broken entry", "debug", args={
        "msg": "{{ ('broken', '') in hostvars[inventory_hostname].items() }}",
    }, vars={"unused": "{{ lookup('source') }}/tail"})
    runtime.run_play({"tasks": [task], "gather_facts": False, "vars": {}})
    assert runtime.failed
    assert "unsupported operand type" in frames[0]["result"]["msg"]


def test_lazy_hostvars_entries_preserve_the_mapping_snapshot():
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": {"h": {"original": 17}}})
    task = _task("1", "Save hostvar entries", "debug", vars={"unused": "{{ lookup('unused') }}/tail"})
    expression = ("{% set current = hostvars[inventory_hostname] %}"
                  "{% set entries = current.items() %}"
                  "{% set removed = current.pop('original') %}"
                  "{% set inserted = current.setdefault('added', 18) %}"
                  "{{ [entries[0], ('original', 17) in entries, ('added', 18) in entries] | to_json }}")
    assert json.loads(runtime.template(expression, runtime._task_ctx(task, {}))) == [["original", 17], True, False]


def test_lazy_hostvars_copy_uses_current_bindings_and_mutates_independently():
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": {"h": {"original": 17}}})
    task = _task("1", "Copy hostvar bindings", "debug", vars={"unused": "{{ lookup('unused') }}/tail"})
    expression = ("{% set current = hostvars[inventory_hostname] %}"
                  "{% set removed = current.pop('original') %}"
                  "{% set added = current.setdefault('new', 18) %}"
                  "{% set snapshot = current.copy() %}"
                  "{% set copied = snapshot.pop('new') %}"
                  "{{ [copied, 'original' in snapshot, 'new' in snapshot, current.new] | to_json }}")
    assert json.loads(runtime.template(expression, runtime._task_ctx(task, {}))) == [18, False, False, 18]


def test_jinja_tojson_escapes_values_in_nested_lazy_hostvars_entries():
    runtime = Runtime({}, "", {"inventory_hostname": "h"})
    task = _task("1", "Serialize hostvars", "debug", vars={"alias": "{{ '<script>' }}",
                                                         "unused": "{{ lookup('source') }}/tail"})
    runtime.templater.env.globals["lookup"] = lambda name: "unused"
    result = runtime.template("{{ {'values': hostvars[inventory_hostname].values()} | tojson }}",
                              runtime._task_ctx(task, {}))
    assert "<script>" in json.loads(result)["values"]
    assert r"\u003cscript\u003e" in result


@pytest.mark.parametrize("filter_name", ["tojson", "to_json", "to_nice_json"])
def test_json_preserves_independently_mutated_list_views(filter_name):
    runtime = Runtime({}, "", {})
    expression = ("{% set a = data['values'] %}{% set b = data['values'] %}"
                  "{% set unused = a.append(2) %}{{ [a, b] | " + filter_name + " }}")
    result = runtime.template(expression, {"data": {"values": [1]}})
    assert json.loads(result) == [[1, 2], [1]]


def test_template_writes_nested_hostvars_entries(monkeypatch):
    from ast import literal_eval

    from ansiroll.runtime import _core_at_least

    runtime = Runtime({}, "", {"inventory_hostname": "h"})
    runtime.templater.env.globals["lookup"] = lambda name: "ready"
    task = _task("1", "Write hostvars", "template", vars={"alias": "{{ lookup('source') }}/tail"})
    source = "{{ {'values': hostvars[inventory_hostname].values()} }}"
    monkeypatch.setattr(runtime, "_copy_single_file", lambda _task, _args, data, *_rest: data)
    result = runtime._run_file_action(
        "template", {**task, "_baked": {"out.j2": source}}, {"src": "out.j2", "dest": "/out"},
        runtime._task_ctx(task, {}),
    )
    rendered = literal_eval(result.decode("utf-8")) if _core_at_least(2, 19) else json.loads(result)
    assert "ready/tail" in rendered["values"]


def test_flatten_treats_lazy_hostvars_entries_as_lists():
    runtime = Runtime({}, "", {"inventory_hostname": "h"})
    runtime.templater.env.globals["lookup"] = lambda name: "ready"
    task = _task("1", "Flatten hostvars", "debug", vars={"alias": "{{ lookup('source') }}/tail"})
    result = runtime.template("{{ [hostvars[inventory_hostname].values()] | flatten }}",
                              runtime._task_ctx(task, {}))
    assert "ready/tail" in result


def test_delegated_hostvars_sequence_exposes_only_requested_members():
    from ansiroll.runtime import _resolve_delegated_play_var

    inventory = {"inventory_hostname": "h", "hostvars": {"h": {"original": 17}}}
    runtime = Runtime({}, "", inventory)
    calls = []
    runtime.templater.env.globals["lookup"] = lambda name: calls.append(name) or "unused"
    task = _task("1", "Send hostvars alias", "template", vars={
        "payload": "{{ {'values': hostvars[inventory_hostname].values()} }}",
        "unused": "{{ lookup('source') }}/tail",
    })
    scope = runtime.templater._play_var_scope(runtime._task_ctx(task, {}), strict=False)
    members = []
    payload = _resolve_delegated_play_var("payload", scope, runtime.templater, members=members)
    sequence = _resolve_delegated_play_var(None, scope, runtime.templater,
                                          members=members, member=payload["value"]["values"])
    assert sequence["lazy_members"]
    assert isinstance(sequence["value"], list)
    value = _resolve_delegated_play_var(None, scope, runtime.templater,
                                       members=members, member=sequence["value"][0])
    assert value["value"] == 17
    assert not calls


def test_lazy_hostvars_entries_compare_unordered_values_like_lists():
    runtime = Runtime({}, "", {"inventory_hostname": "h", "hostvars": {"h": {"original": float("nan")}}})
    task = _task("1", "Compare unordered values", "debug", vars={"unused": "{{ lookup('source') }}/tail"})
    runtime.templater.env.globals["lookup"] = lambda name: "unused"
    expression = "{{ hostvars[inventory_hostname].values() > [0] }}"
    assert runtime.template(expression, runtime._task_ctx(task, {})) is False


@pytest.mark.parametrize("text", [
    "{{ lookup('source') }}/{{ missing.attribute }}",
    "{{ lookup('source') }}/{{ 1 // divisor }}",
    "{{ lookup('source') ~ missing }}",
    "{{ 'prefix' ~ lookup('source') ~ missing }}",
])
def test_template_does_not_repeat_lookup_after_a_render_error(text):
    from jinja2 import Undefined

    templater = Runtime({}, "", {}).templater
    calls = []

    def lookup(source):
        calls.append(source)
        return "first"

    templater.env.globals["lookup"] = lookup
    result = templater.render_string(text, {"divisor": 0})
    assert isinstance(result, Undefined)
    assert calls == ["source"]


@pytest.mark.parametrize("plugin,expression", [
    ("filter", "'x' | broken"),
    ("test", "'x' is broken"),
])
def test_mixed_template_defers_unavailable_plugin_errors(plugin, expression):
    from jinja2 import Undefined

    templater = Runtime({}, "", {}).templater
    templater._plugin_errors[plugin]["broken"] = "load failed"
    result = templater.render_string("{{ lookup('source') }}/{{ %s }}" % expression, {})
    assert isinstance(result, Undefined)
    assert "load failed" in result._undefined_hint


@pytest.mark.parametrize(("payload", "aliased"), [
    ("{{ lookup('source') }}/{{ suffix }}", "payload"),
    ({"nested": ["{{- lookup('source') -}} /{{ suffix }}"]}, "payload.nested[0]"),
    ("{{ (lookup('source')) }}/{{ suffix }}", "payload"),
    ("{{ (lookup)('source') }}/{{ suffix }}", "payload"),
])
@pytest.mark.parametrize("from_play", [False, True])
def test_task_defers_mixed_lookup_and_its_alias_until_the_task_runs(payload, aliased, from_play):
    frames = []
    rt = Runtime({}, "", {"inventory_hostname": "h"}, on_result=frames.append)
    calls = []

    def lookup(name):
        calls.append(name)
        return "first"

    rt.templater.env.globals["lookup"] = lookup
    variables = {"alias": "{{ %s }}" % aliased, "suffix": "second"}
    play_vars = {"payload": payload} if from_play else {}
    if not from_play:
        variables["payload"] = payload
    tasks = [
        _task("1", "skipped", "debug", vars=variables, when=["false and alias"], args={"msg": "{{ alias }}"}),
        _task("2", "unused", "debug", vars=variables, args={"msg": "{{ 'untouched' if true else alias }}"}),
        _task("3", "runs", "debug", vars=variables, args={"msg": "{{ alias ~ '|' ~ alias }}"}),
    ]
    rt.run_play({"tasks": tasks, "gather_facts": False, "vars": play_vars})
    assert [frame["status"] for frame in frames] == ["skipped", "ok", "ok"]
    assert frames[1]["result"]["msg"] == "untouched"
    assert frames[2]["result"]["msg"] == "first/second|first/second"
    assert calls == ["source"]


def test_literal_tasks_do_not_scan_other_hosts_for_lookup_templates(monkeypatch):
    hostvars = {"another": {"example": "inventory-only value"}}
    rt = Runtime({}, "", {"inventory_hostname": "h", "hostvars": hostvars}, on_result=lambda frame: None)
    examined = []
    has_lookup = rt.templater._has_lookup_call

    def record(text):
        examined.append(text)
        return has_lookup(text)

    monkeypatch.setattr(rt.templater, "_has_lookup_call", record)
    task = _task("1", "literal", "debug", args={"msg": "untouched"})
    rt.run_play({"tasks": [task], "gather_facts": False,
                 "vars": {"unused": "{{ hostvars.another.example }}"}})
    assert "inventory-only value" not in examined


@pytest.mark.parametrize("layer", ["host", "role_defaults", "role_vars"])
def test_literal_tasks_scan_inherited_lookup_containers_once(monkeypatch, layer):
    host_vars = {"inventory_hostname": "h"}
    if layer == "host":
        host_vars["unused"] = ["inherited marker"]
    rt = Runtime({}, "", host_vars, on_result=lambda frame: None)
    examined = []
    has_lookup = rt.templater._has_lookup_call

    def record(text):
        if text == "inherited marker":
            examined.append(text)
        return has_lookup(text)

    monkeypatch.setattr(rt.templater, "_has_lookup_call", record)
    tasks = [_task(str(index), "literal", "debug", args={"msg": "untouched"}) for index in range(10)]
    if layer != "host":
        for task in tasks:
            task[layer] = {"unused": ["inherited marker"]}
    rt.run_play({"tasks": tasks, "gather_facts": False})
    assert examined == ["inherited marker"]


@pytest.mark.parametrize("host_vars", ["hostvars", "lookup('vars', scope_name)",
                                       "lookup(plugin_name, scope_name)"])
def test_task_defers_alias_to_mixed_lookup_in_hostvars(host_vars):
    frames, calls = [], []
    hostvars = {"another": {"payload": "{{ lookup('source') }}/{{ suffix }}"}}
    rt = Runtime({}, "", {"inventory_hostname": "h", "hostvars": hostvars}, on_result=frames.append)
    lookup = rt.templater.env.globals["lookup"]

    def record(name, *args):
        if name == "source":
            calls.append(name)
            return "first"
        return lookup(name, *args)

    rt.templater.env.globals["lookup"] = record
    variables = {"alias": "{{ %s.another.payload }}" % host_vars, "suffix": "second",
                 "scope_name": "hostvars", "plugin_name": "vars"}
    tasks = [
        _task("1", "skipped", "debug", vars=variables, when=["false"], args={"msg": "{{ alias }}"}),
        _task("2", "runs", "debug", vars=variables, args={"msg": "{{ alias }}"}),
    ]
    rt.run_play({"tasks": tasks, "gather_facts": False})
    assert [frame["status"] for frame in frames] == ["skipped", "ok"]
    assert frames[1]["result"]["msg"] == "first/second"
    assert calls == ["source"]


@pytest.mark.parametrize("loop", [None, ["loop item"]])
def test_legacy_vars_methods_resolve_deferred_task_aliases(monkeypatch, loop):
    import ansiroll.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", "2.12.10")
    frames, calls = [], []
    rt = Runtime({}, "", {"inventory_hostname": "h", "item": "original item"}, on_result=frames.append)
    rt.templater.env.globals["lookup"] = lambda name: calls.append(name) or "used"
    task = _task("task", "Show alias", "debug", loop=loop, vars={
        "alias": "{{ 'value' }}", "unused": "{{ lookup('source') }}/tail",
    }, args={"msg": "{{ vars.copy().alias }} / "
             "{{ (vars.items() | selectattr('0', 'equalto', 'alias') | first)[1] }} / {{ vars.copy().item }}"})
    rt.run_play({"tasks": [task], "gather_facts": False})
    assert frames[0]["result"]["msg"] == "value / value / original item"
    assert calls == []


@pytest.mark.parametrize("location", ["play", "inventory", "inventory_mapping", "inventory_list"])
def test_legacy_vars_iteration_preserves_unresolved_sources(monkeypatch, location):
    import ansiroll.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", "2.12.10")
    frames, calls = [], []
    source = "{{ lookup('source') }}/tail"
    host_vars = {"inventory_hostname": "h"}
    payload, suffix = {
        "play": (source, ""), "inventory": (source, ""),
        "inventory_mapping": ({"nested": source}, ".nested"),
        "inventory_list": ([source], "[0]"),
    }[location]
    if location != "play":
        host_vars["payload"] = payload
    play_vars = {"payload": payload} if location == "play" else {"another": "{{ 'value' }}"}
    rt = Runtime({}, "", host_vars, on_result=frames.append)
    rt.templater.env.globals["lookup"] = lambda name: calls.append(name) or "used"
    tasks = [
        _task("keys", "Inspect keys", "debug", args={"msg": "{{ vars.items() | map(attribute=0) | list }}"}),
        _task("source", "Inspect source", "debug", args={
            "msg": "{{ (vars.items() | selectattr('0', 'equalto', 'payload') | first)[1]%s }}" % suffix,
        }),
        _task("membership", "Inspect membership", "debug", args={"msg": "{{ 'payload' in vars.items() }}"}),
    ]
    rt.run_play({"tasks": tasks, "gather_facts": False, "vars": play_vars})
    assert "payload" in frames[0]["result"]["msg"]
    assert frames[1]["result"]["msg"] == source
    assert frames[2]["result"]["msg"] is False
    assert calls == []

    tasks = [_task("read", "Read value", "debug", args={"msg": "{{ vars.payload%s }}" % suffix})]
    rt.run_play({"tasks": tasks, "gather_facts": False, "vars": play_vars})
    assert frames[3]["result"]["msg"] == "used/tail"
    assert calls == ["source"]


@pytest.mark.parametrize("version", ["2.12.10", "2.21.0"])
def test_deferred_vars_keep_standard_mapping_view_type_names(monkeypatch, version):
    import ansiroll.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", version)
    rt = Runtime({}, "", {})
    ctx = {"alias": "{{ 'value' }}", runtime_mod._DEFER_LOOKUP_NAMES: True}
    assert rt.template("{{ vars.items() | type_debug }}", ctx) == "ItemsView"
    assert rt.template("{{ vars.values() | type_debug }}", ctx) == "ValuesView"
    assert rt.template("{{ 'alias' in vars.items() }}", ctx) is False


@pytest.mark.parametrize("argument", [
    '''"{{ lookup('source') }}/{{ suffix }}"''',
    '''"{{ lookup('source') }}" ~ "/{{ suffix }}"''',
    '''"{" ~ "{ lookup('source') }}" ~ tail''',
])
def test_task_defers_mixed_lookup_in_a_lookup_argument(argument):
    frames, calls = [], []
    rt = Runtime({}, "", {"inventory_hostname": "h"}, on_result=frames.append)
    rt.templater._lookup_loaded["source"] = lambda terms, variables: calls.append("source") or ["first"]
    rt.templater._lookup_loaded["consume"] = lambda terms, variables: terms
    variables = {
        "alias": "{{ lookup('consume', %s) }}" % argument,
        "suffix": "second",
        "tail": "/second",
    }
    tasks = [
        _task("skip", "Skip", "debug", vars=variables, when=[False], args={"msg": "{{ alias }}"}),
        _task("unused", "Unused", "debug", vars=variables, args={"msg": "untouched"}),
        _task("use", "Use", "debug", vars=variables, args={"msg": "{{ alias }}"}),
    ]
    rt.run_play({"tasks": tasks, "gather_facts": False})
    assert [frame["status"] for frame in frames] == ["skipped", "ok", "ok"]
    assert frames[-1]["result"]["msg"] == "first/second"
    assert calls == ["source"]


@pytest.mark.parametrize("method", ["items", "values"])
@pytest.mark.parametrize(("version", "value"), [("2.12.10", "{{ 'value' }}"), ("2.21.0", "value")])
def test_deferred_vars_methods_produce_loop_lists(monkeypatch, method, version, value):
    import ansiroll.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", version)
    rt = Runtime({}, "", {})
    ctx = {"alias": "{{ 'value' }}", runtime_mod._DEFER_LOOKUP_NAMES: True}
    expected = ("alias", value) if method == "items" else value
    items = rt._resolve_loop("{{ vars.%s() }}" % method, ctx)
    assert isinstance(items, list)
    assert items[0] == expected


@pytest.mark.parametrize(
    "expression", [" + ".join(["term"] * 1100), "(" * 1100 + "term" + ")" * 1100],
    ids=["long_addition", "nested_parentheses"],
)
def test_unused_deep_lookup_template_does_not_abort_task_setup(expression):
    frames = []
    rt = Runtime({}, "", {"inventory_hostname": "h"}, on_result=frames.append)
    task = _task("1", "literal", "debug", args={"msg": "untouched"})
    lookup = "{{ " + expression + " }}/{{ lookup('source') }}"
    rt.run_play({"tasks": [task], "gather_facts": False, "vars": {"unused": lookup}})
    assert frames[0]["result"]["msg"] == "untouched"


@pytest.mark.parametrize("expression", ["value | default('fallback')", "value | d('fallback')",
                                         "value is defined", "value is undefined"])
@pytest.mark.parametrize(("source", "message"), [
    ("{{ 1 // divisor }}", "by zero"),
    ("{{ 'x' | unknown_filter }}", "No filter named"),
])
def test_captured_errors_do_not_count_as_missing_variables(expression, source, message):
    from ansiroll.runtime import _dump_object

    templater = Runtime({}, "", {}).templater
    text = "{{ %s }}" % expression
    result = templater.render_string(text, {"value": source, "divisor": 0})
    assert message in _dump_object(result)["exception"]["message"]

    missing = templater.render_string(text, {})
    assert missing == {"value is defined": False, "value is undefined": True}.get(expression, "fallback")


@pytest.mark.parametrize("version,resolve", [("2.18.20", False), ("2.19.15", True)])
def test_play_var_mapping_iteration_only_resolves_consumed_values(monkeypatch, version, resolve):
    import ansiroll.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", version)
    rt = Runtime({}, "", {})
    called = []

    def measure(value):
        called.append(value)
        return value

    rt.templater.env.globals["measure"] = measure
    values = {
        "first": "{{ measure('first') }}",
        "second": "{{ measure('second') }}",
    }
    prepared = rt.template_vars(values, {}, dynamic=True)

    expected = "first" if resolve else values["first"]
    assert rt.template("{{ vars.items() | first }}", prepared) == ("first", expected)
    assert called == (["first"] if resolve else [])
    assert rt.template("{{ vars.values() | first }}", prepared) == expected
    assert called == (["first", "first"] if resolve else [])
    assert rt.template("{{ (vars | dict2items | first).value }}", prepared) == expected
    assert called == (["first", "first", "first", "second"] if resolve else [])


def test_debug_and_set_fact_register():
    tasks = [
        _task("1", "sf", "set_fact", args={"foo": "{{ 1 + 1 }}"}),
        _task("2", "show", "debug", args={"msg": "foo is {{ foo }}"}),
    ]
    rt, frames = run(tasks)
    assert by_name(frames, "show")[0]["result"]["msg"] == "foo is 2"
    assert rt.stats["ok"] == 2


def test_delegated_set_fact_is_visible_through_hostvars_only():
    rt, frames = run([
        _task("1", "route", "set_fact", args={"marker": "target value"},
              delegate_to="recipient", delegate_facts=True, register="issued"),
        _task("2", "show", "debug", args={"msg": "{{ hostvars.recipient.marker }}"}),
    ], host_vars={"inventory_hostname": "origin", "hostvars": {"recipient": {}}})
    assert "marker" not in rt.vars
    assert rt.vars["issued"]["ansible_facts"]["marker"] == "target value"
    assert by_name(frames, "show")[0]["result"]["msg"] == "target value"


def test_delegated_set_fact_publishes_only_after_a_successful_loop():
    rt, frames = run([
        _task("1", "assign", "set_fact", args={"marker": "{{ item }}"},
              delegate_to="recipient", delegate_facts="y", register="issued",
              loop=[1, 2], failed_when=["item == 2"], ignore_errors=True),
        _task("2", "local", "set_fact", args={"marker": "local"},
              delegate_to="{{ missing | default(omit) }}", delegate_facts=True),
    ], host_vars={"inventory_hostname": "origin", "hostvars": {"recipient": {}}})
    assert rt.vars["hostvars"]["recipient"] == {}
    assert rt.vars["issued"]["failed"]
    assert rt.vars["marker"] == "local"
    assert by_name(frames, "local")[0]["status"] == "ok"


def test_delegated_set_fact_preserves_loop_order_when_target_is_current_host():
    rt, _ = run([
        _task("1", "assign", "set_fact", args={"marker": "{{ item.value }}"},
              delegate_to="{{ item.host | default(omit) }}", delegate_facts=True,
              loop=[{"host": "origin", "value": "first"}, {"value": "last"}]),
    ], host_vars={"inventory_hostname": "origin"})
    assert rt.vars["marker"] == "last"


def test_delegated_set_fact_routes_each_loop_item():
    rt, _ = run([
        _task("1", "assign", "set_fact", args={"marker": "{{ item.value }}"},
              delegate_to="{{ item.host | default(omit) }}", delegate_facts="{{ item.route }}",
              loop=[
                  {"value": "remote", "host": "recipient", "route": True},
                  {"value": "local", "host": "recipient", "route": False},
              ]),
    ], host_vars={"inventory_hostname": "origin", "hostvars": {"recipient": {}}})
    assert rt.vars["hostvars"]["recipient"] == {"marker": "remote"}
    assert rt.vars["marker"] == "local"


def test_local_cacheable_facts_feed_later_delegated_loop_items():
    rt, _ = run([
        _task("1", "increment", "set_fact",
              args={"marker": "{{ (ansible_facts.marker | default(0) | int) + 1 }}", "cacheable": True},
              delegate_to="recipient", delegate_facts=False, loop=[1, 2]),
    ], host_vars={"inventory_hostname": "origin", "ansible_facts": {}, "hostvars": {"recipient": {}}})
    assert rt.vars["ansible_facts"]["marker"] == 2
    assert rt.vars["hostvars"]["recipient"] == {}


def test_delegated_cacheable_fact_preserves_explicit_ansible_facts():
    rt, _ = run([
        _task("1", "explicit", "set_fact", args={"ansible_facts": "explicit"},
              delegate_to="recipient", delegate_facts=True),
        _task("2", "cached", "set_fact", args={"marker": "written", "cacheable": True},
              delegate_to="recipient", delegate_facts=True),
    ], host_vars={"inventory_hostname": "origin", "hostvars": {"recipient": {}}})
    assert rt.vars["hostvars"]["recipient"]["ansible_facts"] == "explicit"
    assert rt.vars["hostvars"]["recipient"]["marker"] == "written"


def test_delegated_fact_writes_share_implicit_localhost_aliases():
    rt, _ = run([
        _task("1", "send", "set_fact", args={"first": "shared"},
              delegate_to="127.0.0.1", delegate_facts=True),
        _task("2", "send", "set_fact", args={"last": "shared"},
              delegate_to="localhost", delegate_facts=True),
        _task("3", "send", "set_fact", args={"explicit": "kept"},
              delegate_to="::1", delegate_facts=True),
    ], host_vars={"inventory_hostname": "origin", "hostvars": {"localhost": {"inventory_hostname": "localhost"},
                                                               "::1": {"inventory_hostname": "::1"}}})
    assert rt.vars["hostvars"]["localhost"]["first"] == "shared"
    assert rt.vars["hostvars"]["127.0.0.1"]["last"] == "shared"
    assert rt.vars["hostvars"]["::1"] == {"inventory_hostname": "::1", "explicit": "kept"}


def test_set_fact_keeps_completed_local_writes_after_a_later_loop_error():
    rt, _ = run([
        _task("1", "assign", "set_fact", args={"marker": "{{ item }}"},
              delegate_to="recipient", delegate_facts=False,
              loop=["first", "second"], loop_pause="{{ undefined_pause }}", ignore_errors=True),
    ], host_vars={"inventory_hostname": "origin"})
    assert rt.vars["marker"] == "first"


def test_deferred_play_vars_share_scope_with_nested_mapping_members(monkeypatch):
    import ansiroll.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", "2.20.1")
    rt = Runtime({}, "", {})
    calls = []
    rt.templater.env.globals["measure"] = lambda: calls.append("value") or "sample"
    prepared = rt.template_vars({"alias": "{{ measure() }}", "nested": {"leaf": "{{ alias }}"}},
                                {}, dynamic=True)

    assert rt.template("{{ [alias, nested.leaf] }}", prepared) == ["sample", "sample"]
    assert calls == ["value"]
    assert rt.template("{{ vars == dict(vars) and dict(**vars).alias == 'sample' }}", prepared)
    assert "{{ measure() }}" not in rt.template("{{ vars | string }}", prepared)

    cycle = rt.template_vars({"alias": "{{ nested.leaf }}", "nested": {"leaf": "{{ alias }}"}},
                             {}, dynamic=True)
    with pytest.raises(Exception, match="Recursive loop"):
        str(rt.template("{{ alias }}", cycle))


def test_rescue_variables_use_the_current_failure():
    failed = _task("new", "Current failure", "fail", args={"msg": "current"})
    report = _task("show", "Report", "debug", args={"msg": "{{ ansible_failed_result.msg }}"})
    block = {"type": "block", "block": [failed], "rescue": [report], "always": [report]}
    seed = _task("seed", "Seed", "set_fact", args={"ansible_failed_result": {"msg": "old"}})

    _, frames = run([seed, block])

    assert [frame["result"]["msg"] for frame in by_name(frames, "Report")] == ["current", "current"]


def test_ordinary_facts_do_not_overwrite_play_aliases(monkeypatch):
    rt = Runtime({}, "", {})
    monkeypatch.setattr(rt, "run_module", lambda *args, **kwargs: {
        "ansible_facts": {"ansible_distribution": "Facts"}, "changed": False,
    })
    frames = []
    rt.on_result = frames.append
    rt.run_play({"tasks": [
        _task("setup", "Gather", None, action="ansible.builtin.setup"),
        _task("show", "Use {{ ansible_distribution }}", "debug",
              args={"msg": "{{ ansible_distribution }}"}),
    ], "vars": {"ansible_distribution": "{{ replacement }}", "replacement": "Play"}})

    assert by_name(frames, "Use Play")[0]["result"]["msg"] == "Play"
    assert rt.vars["ansible_facts"]["distribution"] == "Facts"
    rt.run_play({"tasks": [
        _task("later", "Use {{ ansible_distribution }}", "debug", args={"msg": "{{ ansible_distribution }}"})
    ]})
    assert by_name(frames, "Use Facts")[0]["result"]["msg"] == "Facts"


def test_task_names_render_role_parameters_and_keep_text_type():
    rt = Runtime({}, "", {})
    assert rt._node_name({"name": "Deploy {{ region }}", "role_params": {"region": "west"}}) == "Deploy west"
    assert rt._node_name({"name": "{{ 0 }}"}) == "0"
    assert rt._node_name({"name": "{{ 42 }}"}) == "42"
    assert rt._node_name({"name": "{{ true }}"}) == "True"
    assert rt._node_name({"name": "{{ none }}"}) == "None"


def test_start_and_result_share_one_rendered_task_name():
    rt = Runtime({}, "", {})
    sequence = iter([1, 2])
    rt.templater._strict_env.globals["next_name"] = lambda: next(sequence)
    starts = []
    results = []
    rt.on_task_start = starts.append
    rt.on_result = results.append
    rt.run_play({"tasks": [_task("one", "{{ next_name() }}", "debug", when=[False])]})
    assert starts[0]["task"] == results[0]["task"] == "1"
    assert next(sequence) == 2


def test_looped_include_keeps_its_name_after_children_start():
    rt = Runtime({}, "", {})
    sequence = iter([1, 2])
    rt.templater._strict_env.globals["next_name"] = lambda: next(sequence)
    starts = []
    results = []
    rt.on_task_start = starts.append
    rt.on_result = results.append
    include = {"type": "include", "uuid": "include", "name": "{{ next_name() }}",
               "loop": ["a", "b"], "tasks": [_task("child", "Child", "debug")]}
    rt.run_play({"tasks": [include]})
    assert [frame["task"] for frame in results if frame["uuid"] == "include"] == ["1", "1"]
    assert starts[0]["task"] == "1"
    assert next(sequence) == 2


def test_legacy_vars_items_and_values_are_sized(monkeypatch):
    import ansiroll.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", "2.18.10")
    rt = Runtime({}, "", {})
    values = rt.template_vars({"source": "sample", "alias": "{{ source }}"}, {}, dynamic=True)
    assert rt.template("{{ vars.items() | length }}", values) == 2
    assert rt.template("{{ vars.values() | length }}", values) == 2
    assert rt.template("{{ (vars.items() | list)[1][1] | type_debug }}", values) == "str"


def test_delegated_template_alias_uses_agent_filters():
    from ansiroll.runtime import _delegate_vars

    rt = Runtime({}, "", {})
    rt.templater.env.filters["local_filter"] = lambda value: value.upper()
    values = rt.template_vars({
        "source": "play",
        "alias": "{{ source | local_filter }}",
        "nested": {"value": "{{ source | local_filter }}", "literal": "{{ '{{ untouched }}' }}"},
        "sequence": ["{{ source | local_filter }}"],
    }, {}, dynamic=True)
    values["source"] = "task"
    sent = _delegate_vars(values, rt.templater)
    assert sent["alias"] == "TASK"
    assert sent["nested"] == {"value": "TASK", "literal": {"__ansiroll_unsafe__": "{{ untouched }}"}}
    assert sent["sequence"] == ["TASK"]


def test_delegation_resolves_selected_action_variables():
    from ansiroll.runtime import _delegate_vars

    rt = Runtime({}, "", {})
    values = rt.template_vars({
        "ansible_rsync_path": "{{ rsync_binary }}",
        "rsync_binary": "/usr/bin/rsync",
        "unused": "{{ rsync_binary }}",
    }, {}, dynamic=True)
    sent = _delegate_vars(values, rt.templater, {"ansible_rsync_path"})
    assert sent["ansible_rsync_path"] == "/usr/bin/rsync"
    assert sent["unused"] == "{{ rsync_binary }}"


def test_delegated_play_fallback_renders_each_member_once():
    from ansiroll.runtime import _delegate_vars, _resolve_delegated_play_var

    rt = Runtime({}, "", {})
    calls = []

    def label():
        calls.append(True)
        return "ready"

    rt.templater.env.globals["label"] = label
    values = rt.template_vars({"settings": {"label": "{{ label() }}", "optional": "{{ missing }}"}},
                              {}, dynamic=True)
    deferred = {}
    lazy_play = set()
    _delegate_vars(values, rt.templater, set(), deferred, lazy_play=lazy_play)
    assert lazy_play == {"settings"}
    assert calls == []
    assert _resolve_delegated_play_var("settings", values, rt.templater) == {
        "value": {"label": "ready", "optional": None}, "undefined": [("optional",)],
    }
    assert calls == [True]


def test_delegated_play_fallback_preserves_typed_keys_and_undefined_paths():
    from ansiroll.runtime import _resolve_delegated_play_var

    rt = Runtime({}, "", {})
    values = rt.template_vars({"settings": {1: ["{{ missing }}"], "1": "winner"}}, {}, dynamic=True)
    assert _resolve_delegated_play_var("settings", values, rt.templater) == {
        "value": {"0": [None], "1": "winner"}, "undefined": [("0", 0)],
        "mapping_keys": [[[], [1, "1"]]],
    }


def test_delegated_play_requests_share_lookup_results():
    from ansiroll.runtime import _resolve_delegated_play_var

    rt = Runtime({}, "", {})
    calls = []

    def load_profile():
        calls.append(True)
        return "prod"

    rt.templater.env.globals["load_profile"] = load_profile
    variables = rt.template_vars({
        "profile": "{{ load_profile() }}",
        "first": "{{ profile ~ '-first' }}",
        "second": "{{ profile ~ '-second' }}",
    }, {}, dynamic=True)
    scope = rt.templater._play_var_scope(variables, strict=False)
    for name in ("first", "second"):
        assert _resolve_delegated_play_var(name, scope, rt.templater) == {
            "value": "prod-" + name, "undefined": [],
        }
    assert calls == [True]


def test_delegated_loop_task_alias_uses_agent_filters():
    from ansiroll.runtime import _delegate_vars

    rt = Runtime({}, "", {})
    rt.templater.env.filters["agent_filter"] = str.upper
    task = _task("template", "Template", None, loop=["sample"],
                 vars={"alias": "{{ item | agent_filter }}"})
    ctx = rt._task_ctx(task, {})
    ctx.update(rt._loop_item_vars(task, ["sample"], 0, ctx))
    assert _delegate_vars(ctx, rt.templater)["alias"] == "SAMPLE"


def test_delegated_synchronize_loop_sends_rendered_ssh_options():
    from ansiroll.runtime import _SYNCHRONIZE_VARS, _delegate_vars

    rt = Runtime({}, "", {})
    rt.templater.env.filters["agent_filter"] = str.upper

    def unexpected_lookup():
        pytest.fail("unused synchronize alias was rendered")

    rt.templater.env.globals["unexpected_lookup"] = unexpected_lookup
    task = _task("sync", "Synchronize", None, loop=["sample"], vars={
        "ansible_connection": "{{ item | lower }}",
        "ansible_ssh_common_args": "-o HostKeyAlias={{ item | agent_filter }}",
        "ansible_ssh_private_key_file": "/keys/{{ item | agent_filter }}",
        "ansible_ssh_port": "{{ 2201 }}",
        "ansible_ssh_user": "{{ item | agent_filter }}",
        "ansible_ssh_password": "{{ item | reverse | join }}",
        "ansible_ssh_timeout": "{{ 10 }}",
        "ansible_ssh_retries": "{{ 2 }}",
        "ansible_ssh_use_tty": "{{ item != 'sample' }}",
        "ansible_podman_executable": "/usr/bin/{{ item | agent_filter }}",
        "ansible_podman_extra_args": "--name {{ item | agent_filter }}",
        "ansible_docker_extra_args": "--context {{ item | agent_filter }}",
        "ansible_become": "{{ item == 'sample' }}",
        "ansible_become_user": "{{ item | agent_filter }}",
        "ansible_sudo_flags": "-H -u {{ item }}",
        "ansible_private_key_file": "/other-keys/{{ item | agent_filter }}",
        "unused": "{{ unexpected_lookup() }}",
    })
    ctx = rt._task_ctx(task, {})
    ctx.update(rt._loop_item_vars(task, ["sample"], 0, ctx))
    sent = _delegate_vars(ctx, rt.templater, _SYNCHRONIZE_VARS)
    assert sent["ansible_connection"] == "sample"
    assert sent["ansible_ssh_common_args"] == "-o HostKeyAlias=SAMPLE"
    assert sent["ansible_ssh_private_key_file"] == "/keys/SAMPLE"
    assert sent["ansible_ssh_port"] == 2201
    assert sent["ansible_ssh_user"] == "SAMPLE"
    assert sent["ansible_ssh_password"] == "elpmas"
    assert sent["ansible_ssh_timeout"] == 10
    assert sent["ansible_ssh_retries"] == 2
    assert sent["ansible_ssh_use_tty"] is False
    assert sent["ansible_podman_executable"] == "/usr/bin/SAMPLE"
    assert sent["ansible_podman_extra_args"] == "--name SAMPLE"
    assert sent["ansible_docker_extra_args"] == "--context SAMPLE"
    assert sent["ansible_become"] is True
    assert sent["ansible_become_user"] == "SAMPLE"
    assert sent["ansible_sudo_flags"] == "-H -u sample"
    assert sent["ansible_private_key_file"] == "/other-keys/SAMPLE"
    assert sent["unused"] == "{{ unexpected_lookup() }}"


def test_loop_task_magic_vars_keep_original_values_when_facts_change():
    rt = Runtime({}, "", {"counter": 0})
    rt.vars.update(rt.template_vars({"play_alias": "{{ 1 }}"}, {}, dynamic=True))
    task = _task("increment", "Increment", None, loop=["first", "second"],
                 vars={"alias": "{{ vars.counter }} / {{ counter }} / {{ item }}"})
    ctx = rt._task_ctx(task, {})
    ctx.update(item="second", counter=1)
    assert rt.template("{{ alias }}", ctx) == "0 / 1 / second"


@pytest.mark.parametrize("play_alias", [False, True])
def test_loop_task_magic_vars_cache_captured_alias_during_render(play_alias):
    rt = Runtime({}, "", {})
    readings = iter([1, 2])
    rt.templater.env.globals["next_reading"] = lambda: next(readings)
    variables = {"alias": "{{ next_reading() }}"}
    if play_alias:
        rt.vars.update(rt.template_vars(variables, {}, dynamic=True))
    task = _task("task", "Task", None, loop=["sample"], vars={} if play_alias else variables)
    ctx = rt._task_ctx(task, {})
    ctx["item"] = "sample"
    assert rt.template("{{ vars.alias }} / {{ vars.alias }}", ctx) == "1 / 1"
    assert rt.template("{{ vars.alias }}", ctx) == 2


def test_loop_snapshot_shares_live_play_alias_cache_for_same_binding():
    rt = Runtime({}, "", {})
    readings = iter([1, 2])
    rt.templater.env.globals["next_reading"] = lambda: next(readings)
    rt.vars.update(rt.template_vars({"source": "{{ next_reading() }}"}, {}, dynamic=True))
    task = _task("task", "Task", None, loop=["sample"], vars={"alias": "{{ item }}"})
    ctx = rt._task_ctx(task, {})
    ctx["item"] = "sample"
    assert rt.template("{{ source }} / {{ vars.source }}", ctx) == "1 / 1"
    assert rt.template("{{ vars.source }}", ctx) == 2


@pytest.mark.parametrize("unsafe", [False, True])
@pytest.mark.parametrize("alias, expected", [
    ("{{ source }}", "sample"),
    ("prefix-{{ source }}", "prefix-sample"),
    ("{{ 'prefix-' ~ nested.source }}", "prefix-sample"),
])
def test_loop_task_conditional_interpolation_preserves_alias_trust(unsafe, alias, expected):
    from ansiroll.runtime import UntrustedConditional

    rt = Runtime({}, "", {})
    value = {"__ansiroll_unsafe__": "sample"} if unsafe else "sample"
    task = _task("task", "Task", None, loop=[expected],
                 vars={"source": value, "nested": {"source": value}, "alias": alias})
    ctx = rt._task_ctx(task, {})
    ctx["item"] = expected
    condition = {"__ansiroll_trusted_condition__": "'{{ alias }}' == item"}
    if unsafe:
        with pytest.raises(UntrustedConditional):
            rt.when([condition], ctx)
    else:
        assert rt.when([condition], ctx)


@pytest.mark.parametrize("flag, expected", [("yes", True), ("no", False)])
@pytest.mark.parametrize("condition", ["{{ enabled }}", "{{ enabled }} and true"])
def test_loop_task_conditional_accepts_boolean_computed_from_unsafe_data(flag, expected, condition):
    rt = Runtime({}, "", {})
    task = _task("task", "Task", None, loop=[flag], vars={
        "flag": {"__ansiroll_unsafe__": flag},
        "enabled": "{{ flag == 'yes' }}",
    })
    ctx = rt._task_ctx(task, {})
    assert rt.when([{"__ansiroll_trusted_condition__": condition}], ctx) is expected


@pytest.mark.parametrize("reference", ["previous.stdout", "vars.previous.stdout"])
def test_loop_task_condition_rejects_text_derived_from_registered_result(reference):
    rt = Runtime({}, "", {})
    rt._store_register({"register": "previous"}, {"stdout": "sample"})
    task = _task("task", "Task", None, loop=["sample"], vars={
        "alias": "'{{ " + reference + " }}' == '{{ item }}'",
    })
    ctx = rt._task_ctx(task, {})
    ctx["item"] = "sample"
    with pytest.raises(UntrustedConditional):
        rt.when([{"__ansiroll_trusted_condition__": "{{ alias }} and true"}], ctx)


@pytest.mark.parametrize("loop", ["{{ previous.stdout_lines }}", "{{ previous.data | list }}"])
def test_loop_items_keep_provenance_of_registered_collections(loop):
    rt = Runtime({}, "", {})
    rt._store_register({"register": "previous"}, {"stdout_lines": ["sample"], "data": {"sample": True}})
    task = _task("task", "Task", None, loop=loop, vars={
        "alias": "'{{ item }}' == 'sample'",
    })
    ctx = rt._task_ctx(task, {})
    items = rt._resolve_loop(task["loop"], ctx)
    ctx.update(rt._loop_item_vars(task, items, 0, ctx))
    with pytest.raises(UntrustedConditional):
        rt.when([{"__ansiroll_trusted_condition__": "{{ alias }} and true"}], ctx)


@pytest.mark.parametrize("keys", ["result.data", "result.data.keys()", "result.data.items() | map('first')"])
def test_conditional_rendering_tracks_registered_mapping_keys(keys):
    rt = Runtime({}, "", {})
    rt._store_register({"register": "result"}, {"data": {"sample": True}})
    task = _task("task", "Task", None, loop=["sample"], vars={
        "alias": "{{ (" + keys + " | list | first) ~ ' value' }}",
    })
    ctx = rt._task_ctx(task, {})
    ctx["item"] = "sample"
    with pytest.raises(UntrustedConditional):
        rt.when([{"__ansiroll_trusted_condition__": "'{{ alias }}' == 'sample value'"}], ctx)


@pytest.mark.parametrize("expression", [
    "result.data.pop('sample')",
    "result.data.setdefault('sample', 'fallback')",
    "result.data.popitem()[0]",
    "result.data.popitem()[1]",
    "result.pop('data').sample",
])
def test_conditional_rendering_tracks_marked_mapping_mutations(expression):
    rt = Runtime({}, "", {})
    rt._store_register({"register": "result"}, {"data": {"sample": "sample"}})
    task = _task("task", "Task", None, loop=["sample"], vars={
        "alias": "{{ " + expression + " ~ ' value' }}",
    })
    ctx = rt._task_ctx(task, {})
    ctx["item"] = "sample"
    with pytest.raises(UntrustedConditional):
        rt.when([{"__ansiroll_trusted_condition__": "'{{ alias }}' == 'sample value'"}], ctx)


@pytest.mark.parametrize("expression", [
    "result.data.pop()",
    "result.data.copy()[0]",
    "result.data | reverse | first",
    "result.nested.pop().value",
])
def test_conditional_rendering_tracks_marked_list_reads(expression):
    rt = Runtime({}, "", {})
    rt._store_register({"register": "result"}, {"data": ["sample"], "nested": [{"value": "sample"}]})
    task = _task("task", "Task", None, loop=["sample"], vars={
        "alias": "{{ " + expression + " ~ ' value' }}",
    })
    ctx = rt._task_ctx(task, {})
    ctx["item"] = "sample"
    with pytest.raises(UntrustedConditional):
        rt.when([{"__ansiroll_trusted_condition__": "'{{ alias }}' == 'sample value'"}], ctx)


@pytest.mark.parametrize("mapping", [False, True])
def test_delegated_container_copies_keep_written_values_literal(mapping):
    from ansiroll.runtime import _DelegatedTemplateValue, _lazy_container

    runtime = Runtime({}, "", {})
    reads = []
    runtime.templater.env.globals["lookup"] = lambda name: pytest.fail("looked up " + name)
    member = _DelegatedTemplateValue(lambda: reads.append("source") or 17)
    literal = "{{ lookup('written') }}"
    raw = {"source": member} if mapping else [member]
    copied = _lazy_container(raw, runtime, {}, delegated=True).copy()
    if mapping:
        copied.update(written=literal)
        assert copied.copy()["written"] == literal
    else:
        copied.append(literal)
        assert copied.copy()[-1] == literal
    assert not reads
    assert copied["source" if mapping else 0] == 17
    assert reads == ["source"]


def test_delegated_list_mutations_compare_resolved_values():
    from ansiroll.runtime import _DelegatedTemplateValue, _lazy_container

    rt = Runtime({}, "", {})
    reads = []
    rows = _lazy_container([
        _DelegatedTemplateValue(lambda text=text: reads.append(text) or text)
        for text in ("remove", "b", "a")
    ], rt, {}, delegated=True)
    rows.remove("remove")
    assert reads == ["remove"]
    rows.sort()
    assert reads == ["remove", "b", "a"]
    assert rows == ["a", "b"]


@pytest.mark.parametrize(("expression", "expected", "reads"), [
    ("rows < ['z']", True, ["a"]),
    ("rows.copy() <= ['z']", True, ["a"]),
    ("['0'] < rows", True, ["a"]),
    ("rows > ['a']", True, ["a"]),
    ("rows >= []", True, []),
    ("rows == ['a']", False, []),
    ("rows != ['z', 'z']", True, ["a"]),
])
def test_delegated_list_comparisons_read_only_needed_items(expression, expected, reads):
    from ansiroll.runtime import _DelegatedTemplateValue, _lazy_container

    rt = Runtime({}, "", {})
    requested = []
    rows = _lazy_container([
        _DelegatedTemplateValue(lambda text=text: requested.append(text) or text)
        for text in ("a", "b")
    ], rt, {}, delegated=True)
    assert rt.templater.render_string("{{ %s }}" % expression, {"rows": rows}) is expected
    assert requested == reads


@pytest.mark.parametrize("expression", [
    "result.data[0]",
    "result.data | first",
    "(result.data + ('other',))[0]",
    "(('other',) + result.data)[1]",
    "(result.data * 2)[1]",
])
def test_conditional_rendering_tracks_marked_tuple_reads(expression):
    rt = Runtime({}, "", {})
    rt._store_register({"register": "result"}, {"data": ("sample",)})
    task = _task("task", "Task", None, loop=["sample"], vars={
        "alias": "{{ (" + expression + ") ~ ' value' }}",
    })
    ctx = rt._task_ctx(task, {})
    with pytest.raises(UntrustedConditional):
        rt.when([{"__ansiroll_trusted_condition__": "'{{ alias }}' == 'sample value'"}], ctx)


def test_conditional_tuple_operations_render_members_and_keep_tuple_type():
    rt = Runtime({}, "", {"value": "sample", "outer": {"data": ("{{ value }}",)}})
    assert rt.templater.render_conditional_template(
        "{{ outer.data == ('sample',) and (outer.data | type_debug) == 'tuple' }}", rt.ctx())


@pytest.mark.parametrize("collection", ["('ok', result.text)", "['ok', result.text]"])
def test_conditional_membership_only_observes_examined_members(collection):
    rt = Runtime({}, "", {})
    rt._store_register({"register": "result"}, {"text": "registered"})
    task = _task("task", "Task", None, loop=[1], vars={
        "checks": "{{ " + collection + " }}",
        "alias": "{{ 'ok' if 'ok' in checks else 'missing' }}",
    })
    ctx = rt._task_ctx(task, {})
    assert rt.when([{"__ansiroll_trusted_condition__": "'{{ alias }}' == 'ok'"}], ctx)


def test_lookup_plugin_reads_nested_task_alias_with_loop_binding():
    rt = Runtime({}, "", {})
    rt.templater._lookup_loaded["read_settings"] = lambda terms, variables, **kwargs: [
        variables["settings"]["name"],
    ]
    task = _task("task", "Task", None, loop=["alpha"], vars={
        "settings": {"name": "{{ item | default('pre-loop') }}"},
    })
    ctx = rt._task_ctx(task, {})
    ctx["item"] = "alpha"
    rendered = rt.template("{{ [settings.name, lookup('read_settings')] }}", ctx)
    assert rendered == ["alpha", "alpha"]


@pytest.mark.parametrize("expression", [
    "'sample' if true else item",
    "true and 'sample' or item",
    "'sample' if true else unused_alias",
])
def test_loop_conditional_uses_only_selected_alias_branches(expression):
    rt = Runtime({}, "", {})
    task = _task("task", "Task", None, loop=["sample"], vars={
        "alias": "{{ " + expression + " }}",
        "unused_alias": "{{ item ~ ' value' }}",
    })
    ctx = rt._task_ctx(task, {})
    ctx["item"] = rt.template({"__ansiroll_unsafe__": "sample"}, {})
    assert rt.when([{"__ansiroll_trusted_condition__": "'{{ alias }}' == 'sample'"}], ctx)


def test_boolean_loop_conditions_read_only_the_selected_values():
    rt = Runtime({}, "", {})
    rt._store_register({"register": "previous"}, {"stdout_lines": ["sample"]})
    task = _task("task", "Task", None, loop="{{ previous.stdout_lines }}", vars={"label": "sample"})
    ctx = rt._task_ctx(task, {})
    items = rt._resolve_loop(task["loop"], ctx)
    ctx.update(rt._loop_item_vars(task, items, 0, ctx))

    assert rt.when([{"__ansiroll_trusted_condition__": "{{ vars.label == 'sample' }}"}], ctx)
    assert rt.when([{"__ansiroll_trusted_condition__": "{{ item == 'sample' }}"}], ctx)
    with pytest.raises(UntrustedConditional):
        rt.when([{"__ansiroll_trusted_condition__": "'{{ item }}' == 'sample'"}], ctx)


@pytest.mark.parametrize("keyword", ["changed_when", "failed_when", "until", "loop_break_when"])
def test_loop_alias_keeps_provenance_of_current_result(keyword, monkeypatch):
    monkeypatch.setattr("ansiroll.runtime._ANSIROLL_CORE_VERSION", "2.21.1")
    condition = {"__ansiroll_trusted_condition__": "{{ alias }} and true"}
    task = _task("task", "Task", "debug", loop=["sample"], register="result",
                 args={"msg": "sample"}, vars={"alias": "'{{ result.msg }}' == '{{ item }}'"},
                 **{keyword: [condition]})
    _rt, frames = run([task])
    assert any(frame["status"] == "failed" and "untrusted template or expression" in str(frame["result"])
               for frame in frames)


@pytest.mark.parametrize("keyword, expected", [
    ("changed_when", "changed"),
    ("failed_when", "ok"),
    ("until", "ok"),
    ("loop_break_when", "ok"),
])
def test_boolean_condition_can_read_current_registered_result(keyword, expected, monkeypatch):
    monkeypatch.setattr("ansiroll.runtime._ANSIROLL_CORE_VERSION", "2.21.1")
    expected_msg = "unmatched" if keyword == "failed_when" else "sample"
    condition = {"__ansiroll_trusted_condition__": "{{ result.msg == '" + expected_msg + "' }}"}
    task = _task("task", "Task", "debug", loop=["sample"], register="result",
                 args={"msg": "sample"}, **{keyword: [condition]})
    _rt, frames = run([task])
    assert any(frame["status"] == expected for frame in frames)
    assert all(frame["status"] != "failed" for frame in frames)


def test_loop_alias_reads_trusted_replacement_of_a_register():
    tasks = [
        _task("produce", "Produce", "debug", args={"msg": "initial"}, register="value"),
        _task("replace", "Replace", "set_fact", args={"value": "sample"}),
        _task("read", "Read", "debug", loop=["sample"],
              vars={"alias": "'{{ value }}' == '{{ item }}'"},
              when=[{"__ansiroll_trusted_condition__": "{{ alias }} and true"}]),
    ]
    _rt, frames = run(tasks)
    assert by_name(frames, "Read")[0]["status"] == "ok"


@pytest.mark.parametrize("second, trusted", [("'item == 1'", True), ("'item == ' ~ result.stdout", False)])
def test_loop_assertion_tracks_trust_for_each_expression(second, trusted):
    rt = Runtime({}, "", {})
    rt._store_register({"register": "result"}, {"stdout": "1"})
    task = _task("task", "Task", None, loop=[1], vars={
        "alias": "{{ [result.stdout == '1', " + second + "] }}",
    })
    ctx = rt._task_ctx(task, {})
    ctx["item"] = 1
    condition = {"__ansiroll_trusted_condition__": "{{ alias }}"}
    if trusted:
        assert rt.when([condition], ctx, keyword=None)
    else:
        with pytest.raises(UntrustedConditional):
            rt.when([condition], ctx, keyword=None)


@pytest.mark.parametrize("composition", [
    "[result.stdout == '1'] + checks",
    "checks + [result.stdout == '1']",
    "([result.stdout == '1'] + checks) + ['item is number']",
])
@pytest.mark.parametrize("trusted", [True, False])
def test_loop_assertion_tracks_members_through_list_composition(composition, trusted):
    rt = Runtime({}, "", {})
    rt._store_register({"register": "result"}, {"stdout": "1"})
    values = ["item == 1"] if trusted else ["item == {{ result.stdout }}"]
    task = _task("task", "Task", None, loop=[1], vars={
        "checks": values,
        "alias": "{{ " + composition + " }}",
    })
    ctx = rt._task_ctx(task, {})
    ctx["item"] = 1
    condition = {"__ansiroll_trusted_condition__": "{{ alias }}"}
    if trusted:
        assert rt.when([condition], ctx, keyword=None)
    else:
        with pytest.raises(UntrustedConditional):
            rt.when([condition], ctx, keyword=None)


def test_loop_assertion_members_share_resolved_task_aliases():
    rt = Runtime({}, "", {})
    reads = []

    def next_reading():
        reads.append(True)
        return len(reads)

    rt.templater.env.globals["next_reading"] = next_reading
    task = _task("task", "Task", None, loop=[1], vars={"alias": "{{ next_reading() }}"})
    ctx = rt._task_ctx(task, {})
    cond = {"__ansiroll_trusted_condition__": "{{ [alias == 1, alias == 1] }}"}
    assert rt.when([cond], ctx, keyword=None)
    assert reads == [True]


@pytest.mark.parametrize("take_branch", [True, False])
@pytest.mark.parametrize("unselected", ["[unexpected_lookup()]", "[item | missing_branch_filter]"])
def test_loop_assertion_tracks_only_members_of_selected_branch(take_branch, unselected):
    rt = Runtime({}, "", {})
    rt._store_register({"register": "result"}, {"msg": "sample"})

    def unexpected_lookup():
        pytest.fail("an unselected assertion branch was rendered")

    rt.templater.env.globals["unexpected_lookup"] = unexpected_lookup
    selected = "[result.msg == 'sample', check]"
    alternate = unselected
    if not take_branch:
        selected, alternate = alternate, selected
    task = _task("task", "Task", None, loop=[1], vars={
        "take_branch": take_branch,
        "check": "item == 1",
        "alias": "{{ " + selected + " if take_branch else " + alternate + " }}",
    })
    ctx = rt._task_ctx(task, {})
    ctx["item"] = 1
    assert rt.when([{"__ansiroll_trusted_condition__": "{{ alias }}"}], ctx, keyword=None)


@pytest.mark.parametrize("access", [lambda values: values["alias"], lambda values: values.get("alias")])
def test_custom_lookup_resolves_deferred_task_alias_with_current_loop_item(access):
    rt = Runtime({}, "", {})

    def read_alias(terms, variables, **kwargs):
        return [access(variables)]

    rt.templater._lookup_loaded["read_alias"] = read_alias
    task = _task("task", "Task", None, loop=["sample"], vars={"alias": "{{ item | upper }}"})
    ctx = rt._task_ctx(task, {})
    ctx.update(rt._loop_item_vars(task, ["sample"], 0, ctx))
    assert rt.template("{{ lookup('read_alias') }}", ctx) == "SAMPLE"


@pytest.mark.parametrize("method", ["pop", "popitem", "setdefault"])
def test_custom_lookup_mapping_mutations_resolve_existing_aliases(method):
    rt = Runtime({}, "", {})

    def read_alias(terms, variables, **kwargs):
        if method == "setdefault":
            value = variables.setdefault("alias", "fallback")
            assert "alias" in variables
            assert variables.setdefault("missing", "fallback") == "fallback"
        elif method == "popitem":
            for key in list(variables):
                if key != "alias":
                    del variables[key]
            key, value = variables.popitem()
            assert key == "alias" and not variables
        else:
            value = variables.pop("alias")
            assert "alias" not in variables
            assert variables.pop("missing", "fallback") == "fallback"
        return [value]

    rt.templater._lookup_loaded["read_alias"] = read_alias
    task = _task("task", "Task", None, loop=["sample"], vars={"alias": "{{ item | upper }}"})
    ctx = rt._task_ctx(task, {})
    ctx.update(rt._loop_item_vars(task, ["sample"], 0, ctx))
    assert rt.template("{{ lookup('read_alias') }}", ctx) == "SAMPLE"
    assert "alias" in ctx


def test_loop_assertion_observes_cached_play_alias_marker_on_each_read():
    rt = Runtime({}, "", {})
    rt._store_register({"register": "result"}, {"stdout": "1"})
    rt.vars.update(rt.template_vars({"play_alias": "item == {{ result.stdout }}"}, {}, dynamic=True))
    task = _task("task", "Task", None, loop=[1], vars={
        "checks": "{{ [play_alias == 'item == 1', play_alias ~ ' and true'] }}",
    })
    ctx = rt._task_ctx(task, {})
    ctx["item"] = 1
    with pytest.raises(UntrustedConditional):
        rt.when([{"__ansiroll_trusted_condition__": "{{ checks }}"}], ctx, keyword=None)


def test_loop_snapshot_preserves_provenance_when_a_play_alias_is_shadowed():
    rt = Runtime({}, "", {})
    rt._store_register({"register": "result"}, {"stdout": "1"})
    rt.vars.update(rt.template_vars({"entry": "entry == {{ result.stdout }}"}, {}, dynamic=True))
    task = _task("task", "Task", None, loop=[1], loop_var="entry", vars={
        "checks": "{{ [vars.entry == 'entry == 1', vars.entry ~ ' and true'] }}",
    })
    ctx = rt._task_ctx(task, {})
    ctx.update(rt._loop_item_vars(task, [1], 0, ctx))
    assert ctx["entry"] == 1
    with pytest.raises(UntrustedConditional):
        rt.when([{"__ansiroll_trusted_condition__": "{{ checks }}"}], ctx, keyword=None)


def test_conditional_assertion_materializes_iterator_from_selected_branch():
    rt = Runtime({}, "", {})
    condition = {"__ansiroll_trusted_condition__": "{{ [false] if false else [true] | map('bool') }}"}
    assert rt.when([condition], rt.ctx(), keyword=None)


def test_conditional_list_does_not_replay_branch_that_returns_none():
    rt = Runtime({}, "", {})
    reads = []

    def choose_branch():
        reads.append(True)
        return len(reads) > 1

    rt.templater.env.globals["choose_branch"] = choose_branch
    assert rt.templater.render_conditional_template(
        "{{ [true] if choose_branch() else none }}", rt.ctx()) is None
    assert reads == [True]


def test_loop_assertion_preserves_unsafe_marker_in_cached_alias():
    rt = Runtime({}, "", {})
    rt._store_register({"register": "result"}, {"stdout": "1"})
    task = _task("task", "Task", None, loop=[1], vars={
        "text": "item == {{ result.stdout }}",
        "alias": "{{ [vars.text == 'item == 1', vars.text ~ ' and true'] }}",
    })
    ctx = rt._task_ctx(task, {})
    ctx["item"] = 1
    with pytest.raises(UntrustedConditional):
        rt.when([{"__ansiroll_trusted_condition__": "{{ alias }}"}], ctx, keyword=None)


@pytest.mark.parametrize("expression", ["expressions[0]", "expressions[0] ~ ' and true'"])
@pytest.mark.parametrize("expressions", [
    "{{ result.parts | map('upper') | map('lower') | list }}",
    "{{ [result.parts[0] | lower] }}",
    "{{ (result.parts[0] | lower,) }}",
])
def test_loop_assertion_preserves_unsafe_marker_in_cached_alias_collection(expression, expressions):
    rt = Runtime({}, "", {})
    rt._store_register({"register": "result"}, {"parts": ["item == 1"]})
    task = _task("task", "Task", None, loop=[1], vars={
        "expressions": expressions,
        "alias": "{{ [expressions[0] is string, " + expression + "] }}",
    })
    ctx = rt._task_ctx(task, {})
    ctx["item"] = 1
    with pytest.raises(UntrustedConditional):
        rt.when([{"__ansiroll_trusted_condition__": "{{ alias }}"}], ctx, keyword=None)


@pytest.mark.parametrize("mapping", [
    "{'computed': result.text is string, 'trusted': 'item == 1', 'derived': result.text | lower}",
    "{'computed': result.text is string, 'trusted': 'item == 1', 'derived': result.text | lower}.copy()",
    "dict(computed=result.text is string, trusted='item == 1', derived=result.text | lower)",
    "dict({'computed': result.text is string}, trusted='item == 1', derived=result.text | lower)",
    "dict(**{'computed': result.text is string, 'trusted': 'item == 1', 'derived': result.text | lower})",
])
def test_loop_assertion_keeps_trusted_literal_fields_in_cached_mapping(mapping):
    rt = Runtime({}, "", {})
    rt._store_register({"register": "result"}, {"text": "item == 1"})
    task = _task("task", "Task", None, loop=[1], vars={
        "checks": "{{ " + mapping + " }}",
        "alias": "{{ [checks.computed, checks.trusted] }}",
    })
    ctx = rt._task_ctx(task, {})
    ctx["item"] = 1
    assert rt.when([{"__ansiroll_trusted_condition__": "{{ alias }}"}], ctx, keyword=None)
    with pytest.raises(UntrustedConditional):
        rt.when([{"__ansiroll_trusted_condition__": "{{ [checks.computed, checks.derived ~ ' and true'] }}"}],
                ctx, keyword=None)


def test_assertion_does_not_template_a_rendered_collection_member_again():
    rt = Runtime({}, "", {})
    condition = {"__ansiroll_trusted_condition__": "{{ ['{' ~ '{ true }' ~ '}'] }}"}
    with pytest.raises(UntrustedConditional):
        rt.when([condition], rt.ctx(), keyword=None)


@pytest.mark.parametrize("reference", ["alias", "settings"])
def test_loop_task_conditional_reads_only_used_mapping_members(reference):
    rt = Runtime({}, "", {})

    def unexpected_lookup():
        pytest.fail("an unused mapping member was rendered")

    rt.templater.env.globals["unexpected_lookup"] = unexpected_lookup
    task = _task("task", "Task", None, loop=["sample"], vars={
        "settings": {
            "enabled": True,
            "unused": "{{ unexpected_lookup() }}",
            "text": {"__ansiroll_unsafe__": "sample"},
        },
        "alias": "{{ settings }}",
    })
    ctx = rt._task_ctx(task, {})
    assert rt.when([{"__ansiroll_trusted_condition__": "{{ " + reference + ".enabled }}"}], ctx)
    condition = {"__ansiroll_trusted_condition__": "'{{ " + reference + ".text }}' == 'sample'"}
    with pytest.raises(UntrustedConditional):
        rt.when([condition], ctx)


@pytest.mark.parametrize("alias, access", [
    ({"expression": "'prefix-{{ item }}' == 'prefix-sample'"}, "alias.expression"),
    (["'prefix-{{ item }}' == 'prefix-sample'"], "alias[0]"),
    ({"expressions": ["'prefix-{{ item }}' == 'prefix-sample'"]}, "alias.expressions[0]"),
])
def test_loop_task_container_members_keep_unsafe_interpolation_marker(alias, access):
    rt = Runtime({}, "", {})
    task = _task("task", "Task", None, loop=["sample"], vars={"alias": alias})
    ctx = rt._task_ctx(task, {})
    ctx["item"] = rt.template({"__ansiroll_unsafe__": "sample"}, {})
    condition = {"__ansiroll_trusted_condition__": "{{ " + access + " }} and true"}
    with pytest.raises(UntrustedConditional):
        rt.when([condition], ctx)


@pytest.mark.parametrize("alias, expected", [
    ("{{ [flag == 'yes'] }}", True),
    ("{{ [flag == 'no'] }}", False),
    (["{{ flag == 'yes' }}"], True),
    (["{{ flag == 'no' }}"], False),
])
def test_loop_task_assertion_accepts_lists_of_computed_booleans(alias, expected):
    rt = Runtime({}, "", {})
    task = _task("task", "Task", None, loop=["yes"], vars={
        "flag": {"__ansiroll_unsafe__": "yes"},
        "alias": alias,
    })
    ctx = rt._task_ctx(task, {})
    assert rt.when([{"__ansiroll_trusted_condition__": "{{ alias }}"}], ctx, keyword=None) is expected


def test_loop_task_assertion_rejects_lazy_list_of_untrusted_expressions():
    rt = Runtime({}, "", {})
    task = _task("task", "Task", None, loop=["yes"], vars={
        "flag": {"__ansiroll_unsafe__": "yes"},
        "alias": ["'{{ flag }}' == 'yes'"],
    })
    ctx = rt._task_ctx(task, {})
    with pytest.raises(UntrustedConditional):
        rt.when([{"__ansiroll_trusted_condition__": "{{ alias }}"}], ctx, keyword=None)


def test_legacy_loop_task_vars_iteration_exposes_plain_strings(monkeypatch):
    monkeypatch.setattr("ansiroll.runtime._ANSIROLL_CORE_VERSION", "2.18.20")
    rt = Runtime({}, "", {})
    task = _task("task", "Task", None, loop=["sample"], vars={"alias": "{{ item | upper }}"})
    ctx = rt._task_ctx(task, {})
    ctx["item"] = "sample"
    assert rt.template("{{ vars.alias }}", ctx) == "SAMPLE"
    scope = rt.templater._play_var_scope(ctx, strict=False)
    assert type(dict(scope["vars"].items())["alias"]) is str
    assert any(type(value) is str and value == "{{ item | upper }}" for value in scope["vars"].values())


@pytest.mark.parametrize("controller_snapshot", [False, True])
def test_play_alias_can_read_shadowed_inventory_value(controller_snapshot):
    frames = []
    host_vars = {"inventory_hostname": "h", "foo": "inventory_value"}
    if controller_snapshot:
        host_vars["hostvars"] = {"h": {"foo": "inventory_value"}}
    rt = Runtime({}, "", host_vars, on_result=frames.append)
    rt.run_play({"vars": {"foo": "{{ hostvars[inventory_hostname].foo }}"},
                 "tasks": [_task("read", "Read", "debug", args={"msg": "{{ foo }}"})]})
    assert frames[0]["result"]["msg"] == "inventory_value"


def test_overridden_include_var_does_not_eagerly_consume_its_lookup():
    rt = Runtime({}, "", {})
    calls = []
    rt.templater.env.globals["measure"] = lambda: calls.append("read") or True
    include = {"type": "include", "vars": {"enabled": "{{ measure() }}"}, "when": ["enabled"]}
    ctx = rt._task_ctx(include, {})
    assert calls == []
    assert rt._task_gate(include, ctx)[0] is None
    assert calls == ["read"]


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


def test_set_fact_expanded_args_leave_generated_mapping_values_literal(monkeypatch):
    import ansiroll.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", "2.21.1")
    runtime = Runtime({}, "", {"inventory_hostname": "localhost"})
    task = _task("1", "set fact", "set_fact", args={
        "_variable_params": "{{ {'generated': {'text': '{' ~ '{ sentinel }}'}} }}",
    })
    result = runtime._run_set_fact(task, {"sentinel": "rendered"}, None)
    ctx = {"sentinel": "rendered", "hostvars": {"localhost": result["ansible_facts"]}}
    assert runtime.template("{{ hostvars.localhost.generated.text }}", ctx) == "{{ sentinel }}"


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


@pytest.mark.parametrize("core_2_19_or_newer", [False, True])
def test_broken_conditional_config_controls_string_truthiness(
        monkeypatch, core_2_19_or_newer):
    monkeypatch.setattr(
        "ansiroll.runtime._core_at_least",
        lambda *_version: core_2_19_or_newer,
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

    if core_2_19_or_newer:
        # ansible-core 2.19+ fails the task on a non-boolean conditional.
        with pytest.raises(ConditionalError, match="must have a boolean"):
            rt.when(["'broken static'"], disabled)
    else:
        assert rt.when(["'broken static'"], disabled) is True
    assert rt.when(["'broken static'"], enabled) is True


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


def test_literal_false_when_does_not_hide_loop_evaluation_error():
    task = _task("1", "broken loop", "debug", loop="{{ [1 / divisor] }}",
                 vars={"divisor": 0}, when=[False])
    _rt, frames = run([task])
    assert frames[0]["status"] == "failed"
    assert "division by zero" in frames[0]["result"]["msg"]


@pytest.mark.parametrize("expression", [
    "query('file', missing_path)", "query('vars', 'missing_items')",
    "query('dict', missing_mapping)", "lookup('dict', missing_mapping, wantlist=true)",
])
def test_literal_false_when_can_skip_loop_with_undefined_lookup_inputs(expression):
    task = _task("1", "undefined lookup", "debug", loop="{{ %s }}" % expression, when=[False])
    _rt, frames = run([task])
    assert frames[0]["status"] == "skipped"


def test_literal_false_when_does_not_hide_lookup_type_errors():
    task = _task("1", "broken dict lookup", "debug", loop="{{ query('dict', 42) }}", when=[False])
    _rt, frames = run([task])
    assert frames[0]["status"] == "failed"
    assert "expects a dictionary" in frames[0]["result"]["msg"]


@pytest.mark.parametrize("call", ["query('list', ['ok', missing])",
                                  "lookup('list', ['ok', missing], wantlist=true)"])
def test_lookup_can_leave_unused_nested_terms_undefined(call):
    templater = Runtime({}, "", {}).templater
    assert templater.render_string("{{ (%s | first)[0] }}" % call, {}) == "ok"


def test_literal_false_when_does_not_hide_lookup_file_errors(tmp_path):
    task = _task("1", "broken lookup", "debug", loop="{{ query('file', path) }}",
                 vars={"path": str(tmp_path / "missing")}, when=[False])
    _rt, frames = run([task])
    assert frames[0]["status"] == "failed"
    assert "No such file" in frames[0]["result"]["msg"]


def test_vars_lookup_default_preserves_evaluation_errors():
    from ansiroll.runtime import _CapturedTemplateError, _fail_on_undef

    templater = Runtime({}, "", {}).templater
    text = "{{ lookup('vars', 'value', default='fallback') }}"
    assert templater.render_string(text, {}) == "fallback"
    result = templater.render_string(text, {"value": "{{ 1 / divisor }}", "divisor": 0})
    with pytest.raises(_CapturedTemplateError, match="division by zero"):
        _fail_on_undef(result)


@pytest.mark.parametrize(("call", "expected"), [("lookup", "present"), ("query", ["present"])])
def test_vars_lookup_preserves_captured_default_errors(call, expected, tmp_path):
    from ansiroll.runtime import _CapturedTemplateError, _fail_on_undef

    templater = Runtime({}, "", {}).templater
    text = "{{ " + call + "('vars', 'value', default=fallback) }}"
    assert templater.render_string(text, {"value": "present"}) == expected
    assert templater.render_string(text, {"value": "present", "fallback": ["{{ missing }}"]}) == expected
    context = {"value": "present", "divisor": 0}
    broken = "{{ 1 // divisor }}"
    for fallback in (broken, [broken], {"outer": (broken,)}):
        context["fallback"] = fallback
        with pytest.raises(_CapturedTemplateError, match="by zero"):
            _fail_on_undef(templater.render_string(text, context))
    context.update(fallback="{{ lookup('file', path) }}", path=str(tmp_path / "missing"))
    with pytest.raises(_CapturedTemplateError, match="No such file"):
        _fail_on_undef(templater.render_string(text, context))


@pytest.mark.parametrize("call", ["query('list', term)", "lookup('list', term, wantlist=true)"])
def test_lookup_preserves_captured_nested_term_errors(call):
    from ansiroll.runtime import _CapturedTemplateError, _fail_on_undef

    templater = Runtime({}, "", {}).templater
    text = "{{ (%s | first)[0] }}" % call
    context = {"term": ["ok", "{{ 1 // divisor }}"], "divisor": 0}
    with pytest.raises(_CapturedTemplateError, match="by zero"):
        _fail_on_undef(templater.render_string(text, context))


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


@pytest.mark.parametrize("modern", [False, True])
def test_debug_finalizes_nested_undefined_before_reporting(monkeypatch, modern):
    monkeypatch.setattr("ansiroll.runtime._core_at_least", lambda *_: modern)
    tasks = [
        _task("1", "message", "debug", args={"msg": "{{ nested }}"},
              ignore_errors=True),
        _task("2", "diagnostic", "debug", args={"var": "nested"}),
    ]
    _rt, frames = run(tasks, host_vars={
        "inventory_hostname": "h",
        "nested": {"values": ("{{ missing }}", "{{ also_missing }}")},
    })
    assert frames[0]["status"] == "failed"
    assert "'missing' is undefined" in frames[0]["result"]["msg"]
    assert frames[1]["status"] == "ok"
    assert frames[1]["result"]["nested"] == ({
        "values": ["<< error 1 - 'missing' is undefined >>",
                   "<< error 2 - 'also_missing' is undefined >>"]
    } if modern else "VARIABLE IS NOT DEFINED!")


def test_delegated_debug_remains_a_runtime_special_action():
    task = _task("1", "delegated debug", "debug", args={"msg": "hello"},
                 delegate_to="localhost")
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    rt.delegate_fn = lambda *_args: pytest.fail("debug should not be delegated")
    frames = []
    rt.on_result = frames.append
    rt.run_play({"tasks": [task], "handlers": [], "gather_facts": False, "vars": {}})
    assert frames[0]["result"]["msg"] == "hello"


def test_modern_debug_var_formats_undefined_lazy_hostvars_entries(monkeypatch):
    monkeypatch.setattr("ansiroll.runtime._ANSIROLL_CORE_VERSION", "2.21.1")
    monkeypatch.setenv("ANSIROLL_DEBUG_HOSTVARS", "ready")
    expression = "{'entries': hostvars[inventory_hostname].values()}"
    task = _task("1", "Inspect hostvars entries", "debug", args={"var": expression}, vars={
        "alias": "{{ lookup('env', 'ANSIROLL_DEBUG_HOSTVARS') }}/tail",
    })
    _, frames = run([task], host_vars={
        "inventory_hostname": "h", "hostvars": {"h": {"broken": "{{ missing }}"}},
    })
    assert frames[0]["status"] == "ok"
    assert "<< error 1 - 'missing' is undefined >>" in frames[0]["result"][expression]["entries"]


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


def test_handler_name_is_cached_for_all_aliases_until_next_play(monkeypatch):
    import ansiroll.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", "2.20.1")
    frames = []
    rt = Runtime({}, "", {"inventory_hostname": "h"}, on_result=frames.append)
    rendered = iter(("first", "second"))
    rt.templater._strict_env.globals["next_name"] = lambda: next(rendered)
    raw_name = "run {{ next_name() }}"
    handlers = [_task("h", raw_name, "debug", args={"msg": "handled"},
                      handler_names=[raw_name, "r : " + raw_name])]

    for name in ("first", "second"):
        tasks = [_task("notify", "trigger", "debug", args={"msg": "notifying"},
                       changed_when=["true"], notify=["r : run " + name, "run " + name])]
        tasks.append(_task("flush", "Flush handlers", "meta_flush_handlers", action="meta"))
        rt.run_play({"tasks": tasks, "handlers": handlers, "gather_facts": False})

    handled = [frame["task"] for frame in frames if frame["result"].get("msg") == "handled"]
    assert handled == ["run first", "run second"]


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


@pytest.mark.parametrize("version,ignored", [("2.14.18", True), ("2.21.1", False)])
def test_setup_condition_and_ignore_errors_gate_the_module(monkeypatch, version, ignored):
    import ansiroll.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", version)
    rt = Runtime({}, "", {})
    calls = []
    monkeypatch.setattr(rt, "run_module", lambda *args, **kwargs: calls.append(args))
    assert rt.gather_facts(when=[False])
    assert rt.gather_facts(ignore_errors="{{ missing_value }}") is ignored
    assert calls == []
    assert rt.stats["skipped"] == 1
    assert rt.stats["failures"] == int(not ignored)
    assert rt.stats["ignored"] == int(ignored)


def test_setup_censors_failures_and_hides_success_details(monkeypatch):
    frames = []
    rt = Runtime({}, "", {}, on_result=frames.append)
    monkeypatch.setattr(rt, "run_module", lambda *args, **kwargs: {
        "failed": True, "msg": "private failure",
    })
    assert not rt.gather_facts(no_log=True)
    assert "private failure" not in repr(frames[-1]["result"])

    monkeypatch.setattr(rt, "run_module", lambda *args, **kwargs: {
        "ansible_facts": {"ansible_system": "Linux"}, "changed": False,
    })
    assert rt.gather_facts()
    result = frames[-1]["result"]
    assert result["_ansible_verbose_override"] is True
    assert rt.vars["ansible_facts"]["system"] == "Linux"


def test_smart_gathering_uses_existing_setup_marker(monkeypatch):
    rt = Runtime({}, "", {"ansible_facts": {"_ansible_facts_gathered": True}})
    calls = []
    monkeypatch.setattr(rt, "run_module", lambda *args, **kwargs: calls.append(args))
    rt.run_play({"tasks": [], "gather_facts": True, "smart_gathering": True})
    assert calls == []
    assert not rt.failed


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


def test_returned_module_facts_are_visible_to_task_conditions(monkeypatch):
    import ansiroll.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", "2.21.1")
    rt = Runtime({}, "", {})
    monkeypatch.setattr(rt, "run_module", lambda *args, **kwargs: {
        "ansible_facts": {"ansible_system": "Linux"}, "changed": False,
    })
    task = _task("setup", "Gather", None, action="ansible.builtin.setup",
                 changed_when=["ansible_facts.system == 'Linux'"],
                 failed_when=["ansible_facts.previous is not defined"])
    result = rt._exec_one(task, {"ansible_facts": {"previous": 1}})
    assert result["changed"]
    assert not result["failed"]


def test_legacy_templated_false_until_retries_to_exhaustion(monkeypatch):
    import ansiroll.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", "2.18.10")
    rt = Runtime({}, "", {})
    task = _task("retry", "Retry", "debug", until=[
        {"__ansiroll_trusted_condition__": "{{ 'false' }}"}
    ], retries=2, delay=0)
    result = rt._exec_with_retries(task, {})
    assert result["failed"]
    assert result["attempts"] == 2


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


def _copy_modules():
    pytest.importorskip("ansible")
    from ansiroll.embed import ModuleEmbedder
    from ansiroll.loader import COPY_ACTION_MODULES

    embedder = ModuleEmbedder()
    return {name: embedder.embed(name) for name in COPY_ACTION_MODULES}


def test_copy_content_writes_file(tmp_path):
    modules = _copy_modules()
    dest = os.path.join(tmp_path, "out.txt")
    tasks = [_task("1", "c", "copy", args={"content": "hello", "dest": dest})]

    def run_copy():
        frames = []
        rt = Runtime(modules, "", {"inventory_hostname": "h"}, on_result=frames.append)
        try:
            rt.run_play({"tasks": tasks, "handlers": [], "gather_facts": False, "vars": {}})
        finally:
            rt.cleanup()
        return frames

    assert run_copy()[0]["status"] == "changed"
    assert open(dest).read() == "hello"
    # Second run is idempotent (no change).
    assert run_copy()[0]["status"] == "ok"


@pytest.mark.parametrize(("src", "expected"), [
    ("./templates//app.j2", "/role/./templates//app.j2"),
    ("sub/../templates/app.j2", "/role/templates/templates/app.j2"),
])
def test_role_template_fullpath_uses_lookup_root_only_for_direct_sources(monkeypatch, src, expected):
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    key = "templates/app.j2"
    task = {
        "_baked_tree": "templates",
        "_baked_tree_dirs": ["nested/templates"],
        "_baked_tree_aliases": {"sub": "nested/sub"},
        "_baked_direct_sources": {"./templates/app.j2": key},
        "_baked": {key: "rendered from {{ template_fullpath }}", "nested/templates/app.j2": "other asset"},
        "_baked_meta": {key: {"fullpath": "/role/templates/templates/app.j2",
                              "lookup_roots": {"./templates/app.j2": "/role/"}}},
    }

    def copy_file(task, args, data, source_rel, source_display, *rest):
        return {"body": data.decode(), "src": source_display}

    monkeypatch.setattr(rt, "_copy_single_file", copy_file)
    assert rt._run_file_action("template", task, {"src": src, "dest": "/out"}, {}) == {
        "body": "rendered from " + expected,
        "src": expected,
    }


def test_copy_without_embedded_modules_fails_clearly(tmp_path):
    dest = tmp_path / "out.txt"
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    try:
        result = rt._run_file_action(
            "copy", {}, {"content": "hello", "dest": str(dest)}, {})
    finally:
        rt.cleanup()
    assert result["failed"] is True
    assert "was not embedded" in result["msg"]
    assert not dest.exists()


@pytest.mark.parametrize("modules, module, receives_decrypt", [
    ({"copy": {"module_fqn": "custom.copy"}}, "copy", True),
    ({"copy": {"module_fqn": "ansible.modules.copy"}}, "copy", False),
    ({}, "ansible.builtin.copy", False),
])
def test_remote_copy_prefers_the_embedded_task_module(monkeypatch, modules, module, receives_decrypt):
    modules = {"ansible.builtin.copy": {"module_fqn": "ansible.modules.copy"}, **modules}
    rt = Runtime(modules, "", {"inventory_hostname": "h"})
    monkeypatch.setattr(rt, "run_module", lambda action, args, become: {"action": action, "args": args})
    result = rt._run_file_action("copy", {"action": "copy"},
                                 {"src": "/source", "dest": "/output", "remote_src": True, "decrypt": False}, {})
    assert result["action"] == module
    assert ("decrypt" in result["args"]) is receives_decrypt


def test_copy_fetches_runtime_created_controller_source(tmp_path):
    src = tmp_path / "checkout" / "docker-compose.yml"
    src.parent.mkdir()
    src.write_bytes(b"services:\n  app:\n    image: test\xff\n")
    dest = tmp_path / "remote" / "docker-compose.yml"
    dest.parent.mkdir()
    rt = Runtime(_copy_modules(), "", {"inventory_hostname": "h"})
    rt.controller_source_fn = lambda path: {
        "data": open(path, "rb").read(),
        "meta": {"fullpath": path},
    }

    try:
        result = rt._run_file_action(
            "copy", {"_baked": {}},
            {"src": str(src), "dest": str(dest)}, {},
        )
    finally:
        rt.cleanup()

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

    def async_run(action, args, become=False, async_val=0, ctx=None, options=None):
        assert async_val == 2000
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

    def run_once(_task, _ctx, _become, attempt=None, args_cache=None):
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


def test_accelerated_command_expands_argument_vars(
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
    )

    assert result["rc"] == 0


def test_accelerated_python_import_probe_runs_as_a_process():
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


def test_accelerated_shell_python_import_probe_runs_as_a_process():
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
    monkeypatch.setattr("ansiroll.runtime._WorkerStderr", lambda proc: None)
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
    monkeypatch.setattr("ansiroll.runtime._WorkerStderr", lambda proc: None)
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


def test_worker_copy_stat_and_stage_ops(tmp_path):
    import base64
    import hashlib

    dest = tmp_path / "out.txt"
    rt = Runtime({}, "", {"inventory_hostname": "h"})
    try:
        assert rt._run_worker_operation(
            "copy_stat", {"path": str(dest), "follow": False, "checksum": True},
        ) == {"exists": False, "checksum": "1"}
        dest.write_bytes(b"hello")
        stat = rt._run_worker_operation(
            "copy_stat", {"path": str(dest), "follow": False, "checksum": True})
        assert stat["checksum"] == hashlib.sha1(b"hello").hexdigest()
        staged = rt._run_worker_operation("copy_stage", {
            "data": base64.b64encode(b"data").decode("ascii"), "filename": ".source.conf"})
        assert staged["path"].endswith("/.source.conf")
        with open(staged["path"], "rb") as fh:
            assert fh.read() == b"data"
        rt._run_worker_operation("copy_cleanup", {"tmpdir": staged["tmpdir"]})
        assert not os.path.exists(staged["tmpdir"])
    finally:
        rt.cleanup()


def test_privileged_copy_does_not_fall_back_to_an_unprivileged_write(
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
        "msg": "ansiroll: worker unavailable for privileged file transfer",
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


def test_retry_conditions_see_current_facts_and_arguments_are_stable(monkeypatch):
    rt = Runtime({}, "", {})
    rendered = []
    rt.templater.env.globals["once"] = lambda: rendered.append("args") or "original"
    calls = []

    def action(_name, args, **_kwargs):
        calls.append(args["value"])
        return {"ansible_facts": {"ready": True} if len(calls) == 1 else {}, "rc": 2}

    monkeypatch.setattr(rt, "run_module", action)
    task = _task("retry", "Retry", None, action="test", register="result",
                 args={"value": "{{ once() }} {{ result.value }}"}, retries=3, delay=0,
                 until=["ready and result.failed and result.attempts == 2"])
    result = rt._exec_with_retries(task, {"result": {"value": "seed"}})

    assert result["attempts"] == 2
    assert calls == ["original seed", "original seed"]
    assert rendered == ["args"]


@pytest.mark.parametrize("result", [
    {"rc": 2, "_ansiroll_unknown_outcome": True},
    {"skipped": True},
])
def test_retries_stop_for_unknown_or_skipped_outcome(monkeypatch, result):
    calls = []
    rt = Runtime({}, "", {})
    monkeypatch.setattr(rt, "run_module", lambda *args, **kwargs: calls.append(args) or dict(result))
    task = _task("retry", "Retry", None, action="test", retries=3, delay=0,
                 failed_when=["true"])
    final = rt._exec_with_retries(task, {})
    assert len(calls) == 1
    assert "_ansiroll_unknown_outcome" not in final
    if result.get("skipped"):
        assert not final.get("failed")


def test_explicit_until_retries_module_skip_and_sees_result_defaults(monkeypatch):
    import ansiroll.runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", "2.19.15")
    rt = Runtime({}, "", {})
    calls = []
    monkeypatch.setattr(rt, "run_module", lambda *args, **kwargs: calls.append(args) or {"skipped": True})
    task = _task("retry", "Retry", None, action="test", register="result", retries=1, delay=0,
                 until=["result.failed or result.changed"])
    result = rt._exec_with_retries(task, {})
    assert len(calls) == 2
    assert result["failed"] is True
    assert result["attempts"] == 1


def test_async_acknowledgment_loss_stops_retries(monkeypatch):
    rt = Runtime({"test": {"module_fqn": "test"}}, "", {})
    calls = []

    def lose_ack(*args, **kwargs):
        calls.append(args)
        raise EOFError("worker closed")

    monkeypatch.setattr(rt, "_run_async_via_worker", lose_ack)
    task = _task("async", "Async", None, action="test", retries=3, delay=0,
                 async_val=1, poll=0, until=[False])
    result = rt._exec_with_retries(task, {})
    assert len(calls) == 1
    assert result["failed"]
    assert "launch outcome is unknown" in result["msg"]
    assert "_ansiroll_unknown_outcome" not in result


@pytest.mark.parametrize("version", ["2.19.15", "2.20.11", "2.21.1"])
def test_conditions_keep_namespaced_facts_and_controller_precedence(monkeypatch, version):
    import ansiroll.runtime as runtime_mod
    from ansiroll.runtime import _override_ctx

    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", version)
    modern = version.startswith("2.21")
    rt = Runtime({}, "", {})
    ctx = {
        "inventory_hostname": "real",
        "route_facts": True,
        "ansible_facts": {"ansible_local": {"first": 1}},
        "__ansiroll_controller_config": {
            "DEFAULT_HASH_BEHAVIOUR": "merge",
            "ansiroll_restricted_facts": ["inventory_hostname", "add_host", "add_group"],
        },
    }
    result = {"ansible_facts": {
        "ansible_local": {"second": 2},
        "inventory_hostname": "fake",
        "add_host": {},
        "_ready": [{"_ansible_hidden": True, "data": 1}],
    }}
    effective = _override_ctx({"action": "setup"}, result, ctx)
    assert effective["inventory_hostname"] == "real"
    assert effective["_ready"] == [{"data": 1}]
    assert "add_host" not in effective
    if modern:
        assert effective["ansible_facts"]["ansible_local"] == {"first": 1, "second": 2}
    else:
        assert effective["ansible_facts"]["ansible_local"] == {"first": 1}
        assert effective["ansible_facts"]["ansible_facts"]["ansible_local"] == {"second": 2}
    assert _override_ctx({"action": "debug"}, result, ctx) == ctx
    delegated = {"action": "setup", "delegate_to": "other", "delegate_facts": "{{ route_facts }}"}
    assert _override_ctx(delegated, result, ctx, rt.template) == (ctx if modern else effective)




def _target_side_sources():
    from ansiroll import compiler, runtime as runtime_module

    package = os.path.dirname(compiler.__file__)
    paths = [os.path.join(package, "worker.py")]
    modules = os.path.join(package, "accelerated_modules")
    paths += sorted(os.path.join(modules, name) for name in os.listdir(modules) if name.endswith(".py"))
    for path in paths:
        with open(path, "rb") as source:
            yield os.path.relpath(path, package), source.read()
    yield "runtime.AGENT_BOOTSTRAP", runtime_module.AGENT_BOOTSTRAP


def _assembled_agent():
    from ansiroll import compiler

    # The two controller lookups need Ansible, which the target never has
    patches = {"_controller_core_version": lambda: "2.10.17", "_controller_inventory_address_patterns": lambda: []}
    originals = {name: getattr(compiler, name) for name in patches}
    try:
        for name, replacement in patches.items():
            setattr(compiler, name, replacement)
        return compiler._render_source([], {}, "")
    finally:
        for name, original in originals.items():
            setattr(compiler, name, original)


_TARGET_SIDE_SOURCES = list(_target_side_sources()) + [("assembled agent", None)]


@pytest.mark.parametrize("name, source", _TARGET_SIDE_SOURCES, ids=[name for name, _ in _TARGET_SIDE_SOURCES])
def test_target_side_code_parses_on_python_35(name, source):
    # Gateways still run Python 3.5 (stretch) and 3.7 (buster); one walrus broke every deploy to them. Best-effort, not
    # exhaustive: the 3.5 grammar of older parsers still takes some 3.6+ forms, and 3.6+ APIs get through.
    import ast

    if source is None:
        source = _assembled_agent()
    tree = ast.parse(source, feature_version=(3, 5))
    too_new = tuple(
        getattr(ast, node, ())
        for node in ("NamedExpr", "JoinedStr", "AnnAssign", "Match", "TryStar", "TypeAlias")
    )
    offenders = []
    for node in ast.walk(tree):
        arguments = getattr(node, "args", None)
        if (
            isinstance(node, too_new)
            or (isinstance(arguments, ast.arguments) and arguments.posonlyargs)
            or getattr(node, "type_params", None)
            or (isinstance(node, ast.comprehension) and node.is_async)
            or (isinstance(node, ast.ImportFrom) and node.module == "__future__"
                and any(alias.name == "annotations" for alias in node.names))
        ):
            offenders.append("{}:{} {}".format(name, getattr(node, "lineno", "?"), type(node).__name__))
    assert offenders == []


def test_legacy_bare_name_rendering_reads_regex_groups_without_subscripts(monkeypatch):
    # Python 3.5's match objects have no __getitem__, so `match[1]` broke legacy templating on stretch gateways
    import re
    import ansiroll.runtime as runtime_mod

    class Python35Match:
        def __init__(self, match):
            self._match = match

        def __getattr__(self, name):
            return getattr(self._match, name)

    def without_subscripts(find):
        def wrapped(*args, **kwargs):
            found = find(*args, **kwargs)
            return Python35Match(found) if found else found
        return wrapped

    python35_re = SimpleNamespace(**{name: getattr(re, name) for name in dir(re) if not name.startswith("__")})
    python35_re.match = without_subscripts(re.match)
    python35_re.fullmatch = without_subscripts(re.fullmatch)
    monkeypatch.setattr(runtime_mod, "re", python35_re)
    monkeypatch.setattr(runtime_mod, "_ANSIROLL_CORE_VERSION", "2.12.10")
    legacy = {"__ansiroll_controller_config": {"DEFAULT_JINJA2_NATIVE": False}}

    assert runtime_mod._rendered_number(5, "{{ count }}", dict(legacy, count=5)) == 5
    assert runtime_mod._rendered_number(5, "{{ count }}", dict(legacy, count="5")) == "5"
    runtime = Runtime({}, "", {}, extra_vars={"override_timeout": None})
    template = {"source": "{{ override_timeout }}", "vars": {}, "names": ["override_timeout"]}
    assert runtime._gather_timeout_for_run(23, template, legacy) is None
