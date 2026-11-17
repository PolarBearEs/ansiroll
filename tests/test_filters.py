"""Unit tests for the runtime's vendored Jinja2 filters and tests. No Ansible needed."""

import pytest

from ansiroll.runtime import _Templater, _UnsafeTemplate, _f_to_yaml, _yaml_module, _yaml_plain


@pytest.fixture(scope="module")
def t():
    return _Templater()


def render(t, expr, **ctx):
    return t.render_string(expr, ctx)


# A bare "{{ expr }}" returns the native type (matching Ansible native types).
@pytest.mark.parametrize("expr,ctx,expected", [
    ("{{ 'aGVsbG8=' | b64decode }}", {}, "hello"),
    ("{{ 'hi' | b64encode }}", {}, "aGk="),
    ("{{ d | dict2items | length }}", {"d": {"a": 1, "b": 2}}, 2),
    ("{{ [{'key':'k','value':'v'}] | items2dict }}", {}, {"k": "v"}),
    ("{{ a | combine(b) }}", {"a": {"x": 1}, "b": {"y": 2}}, {"x": 1, "y": 2}),
    ("{{ 'a.b.c' | regex_replace('[.]', '_') }}", {}, "a_b_c"),
    ("{{ 'foobar' | regex_search('o+') }}", {}, "oo"),
    ("{{ (1 > 0) | ternary('yes', 'no') }}", {}, "yes"),
    ("{{ [1,2,2,3] | unique | join(',') }}", {}, "1,2,3"),
    ("{{ [[1,2],[3]] | flatten | join(',') }}", {}, "1,2,3"),
    ("{{ '/etc/foo.conf' | basename }}", {}, "foo.conf"),
    ("{{ '/etc/foo.conf' | dirname }}", {}, "/etc"),
    ("{{ 'one  two three' | split }}", {}, ["one", "two", "three"]),
    ("{{ 'one:two:three' | split(':', 1) }}", {}, ["one", "two:three"]),
    ("{{ {'a':1} | type_debug }}", {}, "dict"),
])
def test_filters(t, expr, ctx, expected):
    assert render(t, expr, **ctx) == expected


def test_b64_roundtrip(t):
    enc = render(t, "{{ s | b64encode }}", s="hello world")
    assert render(t, "{{ s | b64decode }}", s=enc) == "hello world"


def test_yaml_filters_parse_and_emit_real_yaml(t):
    assert _yaml_module().__version__ == "5.3.1"
    assert _yaml_module().__with_libyaml__ is False
    parsed = render(
        t,
        "{{ value | from_yaml }}",
        value="enabled: true\nitems:\n  - one\n  - two\nempty: null\n",
    )
    assert parsed == {"enabled": True, "items": ["one", "two"], "empty": None}

    emitted = render(t, "{{ value | to_yaml }}", value={"items": ["one", "two"]})
    assert "items:" in emitted
    assert "- one" in emitted
    assert render(t, "{{ value | from_yaml }}", value=emitted) == {
        "items": ["one", "two"]
    }

    nice = render(t, "{{ value | to_nice_yaml(indent=4) }}", value={
        "nested": {"enabled": True}
    })
    assert "nested:" in nice and "    enabled: true" in nice


def test_from_yaml_uses_the_safe_loader(t, tmp_path):
    marker = tmp_path / "unsafe-yaml-ran"
    expression = "{{ value | from_yaml }}"
    value = "!!python/object/apply:os.mkdir ['%s']" % marker

    result = render(t, expression, value=value)

    import jinja2
    assert isinstance(result, jinja2.Undefined)
    assert not marker.exists()


def test_to_uuid_stable(t):
    a = render(t, "{{ 'x' | to_uuid }}")
    b = render(t, "{{ 'x' | to_uuid }}")
    assert a == b and len(a) == 36


@pytest.mark.parametrize("expr,ctx,expected", [
    ("{{ '10' is version('9', '>=') }}", {}, True),
    ("{{ '8' is version('10', '>=') }}", {}, False),
    ("{{ '10' is version('10', '==') }}", {}, True),
    ("{{ 'buster' is match('bus') }}", {}, True),
    ("{{ 'buster' is search('ste') }}", {}, True),
    ("{{ r is changed }}", {"r": {"changed": True}}, True),
    ("{{ r is succeeded }}", {"r": {"failed": False}}, True),
    ("{{ r is failed }}", {"r": {"failed": True}}, True),
    ("{{ r is skipped }}", {"r": {"skipped": True}}, True),
])
def test_tests(t, expr, ctx, expected):
    assert render(t, expr, **ctx) == expected


def test_native_object_returned(t):
    # A bare expression should return the native object, not a string.
    out = t.render_string("{{ items }}", {"items": [1, 2, 3]})
    assert out == [1, 2, 3]


def test_when_evaluation(t):
    assert t.evaluate("x == 1 and y > 2", {"x": 1, "y": 3}) is True
    assert t.evaluate("x == 2", {"x": 1}) is False


def test_recursive_templating(t):
    # A var whose value is itself a template must resolve fully (like Ansible).
    ctx = {"full": "{{ d }}/{{ f }}", "d": "/opt/x", "f": "c.yml"}
    assert t.render_string("run {{ full }}", ctx) == "run /opt/x/c.yml"
    assert t.render_string("{{ full }}", ctx) == "/opt/x/c.yml"


def test_recursion_does_not_loop_forever(t):
    # A self-referential template terminates as a captured recursion error
    # (raises where used), like Ansible's "Recursive loop detected".
    import jinja2
    ctx = {"a": "{{ a }}"}
    out = t.render_string("{{ a }}", ctx)
    assert isinstance(out, jinja2.Undefined)
    assert "Recursive loop detected" in getattr(out, "_undefined_hint", "")


@pytest.mark.parametrize(
    "expression, expected",
    [
        ("{{ '' | missing_filter }}", "No filter named 'missing_filter'"),
        ("{{ '' is missing_test }}", "No test named 'missing_test'"),
        (
            "{{ [{}] | selectattr('failed', 'asdf ', true) }}",
            "No test named 'asdf '",
        ),
    ],
)
def test_missing_jinja_plugin_errors_match_ansible(t, expression, expected):
    import jinja2

    out = t.render_string(expression, {})

    assert isinstance(out, jinja2.Undefined)
    assert expected in getattr(out, "_undefined_hint", "")


def test_lookup_templates_nested_constant_terms(t):
    assert render(t, "{{ lookup('list', ['{{ 1 }}']) }}") == [1]
    assert render(t, "{{ lookup('dict', {'key': '{{ 1 }}'}) }}") == {
        "key": "key", "value": 1,
    }


def test_lookup_templates_embedded_command(t):
    assert render(t, "{{ lookup('pipe', 'printf \\\"{{ value }}\\\"') }}",
                  value="hello") == "hello"


def test_embedded_template_constants_are_not_rendered_outside_lookups(t):
    assert render(t, 'echo {{ "{{ 1 }}" }}') == "echo {{ 1 }}"
    assert render(t, "{% set a = '{{ 1 }}' %}{{ a }}") == "{{ 1 }}"


def test_embedded_template_constant_is_not_rendered_again(monkeypatch):
    templater = _Templater()
    source = '{{ prefix }} {{ "{{ service }}" }}'
    parse = templater.env.parse
    calls = []

    def counted_parse(value, *args, **kwargs):
        calls.append(value)
        return parse(value, *args, **kwargs)

    monkeypatch.setattr(templater.env, "parse", counted_parse)
    ctx = {"prefix": "one", "service": "running"}
    assert templater.render_string(source, ctx) == "one {{ service }}"
    ctx["prefix"] = "two"
    assert templater.render_string(source, ctx) == "two {{ service }}"
    # Rendered output is never templated again, so no AST scan is needed.
    assert calls == []


def test_normal_render_skips_embedded_constant_ast_scan(monkeypatch):
    templater = _Templater()
    monkeypatch.setattr(
        templater.env, "parse",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected AST scan")),
    )
    assert templater.render_string("service={{ service }}", {
        "service": "running",
    }) == "service=running"


def test_to_json_captures_undecryptable_vault(t):
    import jinja2
    out = render(t, "{{ value | to_json }}", value={
        "nested": {"__ansiroll_vault__": "$ANSIBLE_VAULT;1.1;AES256\n00"},
    })
    assert isinstance(out, jinja2.Undefined)
    assert "undecryptable variable" in getattr(out, "_undefined_hint", "")


def test_unsafe_filter_result_is_not_templated_again(t):
    assert render(t, "{{ value }}", value=_UnsafeTemplate("{{ 1 }}")) == "{{ 1 }}"


def test_to_yaml_keeps_tuple_keys_hashable():
    assert _f_to_yaml({("a", "b"): 1}) == "? - a\n  - b\n: 1\n"


def test_to_yaml_preserves_shared_and_recursive_containers():
    x = ["leaf"]
    for _ in range(30):
        x = [x, x]
    assert len(_f_to_yaml(x)) < 10000

    loop = []
    loop.append(loop)
    assert _f_to_yaml(loop) == "&id001\n- *id001\n"


def test_to_yaml_detects_recursion_through_lazy_containers():
    from ansiroll.runtime import _LazyTplList

    loop = []
    loop.append(loop)
    assert _f_to_yaml(_LazyTplList(loop, None, {})) == "&id001\n- *id001\n"


class _StubRuntime(object):
    def __init__(self, vars=None):
        self.vars = vars or {}

    def template(self, value, ctx):
        return _Templater().render_string(value, ctx)


def test_to_yaml_renders_aliased_lazy_list_per_context():
    from ansiroll.runtime import _LazyTplList

    rt = _StubRuntime()
    shared = ["{{ name }}"]
    value = [_LazyTplList(shared, rt, {"name": "play"}),
             _LazyTplList(shared, rt, {"name": "task"})]
    assert _f_to_yaml(value) == "- - play\n- - task\n"


def test_to_yaml_detects_recursion_through_dynamic_lazy_containers():
    from ansiroll.runtime import _LazyTplList

    loop = []
    loop.append(loop)
    view = _LazyTplList(loop, _StubRuntime({"x": 1}), {}, dynamic=True)
    assert _f_to_yaml(view) == "&id001\n- *id001\n"


def test_yaml_plain_preserves_cycles_through_tuples():
    items = []
    value = (items,)
    items.append(value)

    plain = _yaml_plain(value)

    assert plain[0][0] is plain
    assert _f_to_yaml(value) == "&id001\n- - *id001\n"
