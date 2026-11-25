"""
Compile a playbook into a single self-contained Python file (+ optional manifest).

Output file layout:

    #!/usr/bin/env python3
    <inlined runtime and in-memory import-bundle finder>
    _ANSIROLL_DATA_BUNDLE = <Base64-encoded ZIP>
    MODULES, PLAYS, ... = <JSON loaded from the data bundle>
    if __name__ == "__main__":
        sys.exit(main(PLAYS, MODULES, ANSIBALLZ_CODE))
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import zipfile

from .embed import ModuleEmbedder
from .loader import (
    COPY_ACTION_MODULES,
    _can_write_var,
    _depends_on_vars,
    _loop_binding_names,
    _runtime_write_names,
    load_adjacent_group_vars,
    parse_playbook,
)
from .accelerated_modules import TOTAL_NAMES, can_accelerate

_RUNTIME_PATH = os.path.join(os.path.dirname(__file__), "runtime.py")
_WORKER_PATH = os.path.join(os.path.dirname(__file__), "worker.py")
_JINJA_BUNDLE_PATH = os.path.join(
    os.path.dirname(__file__), "vendor", "jinja2-py35.zip"
)
_YAML_BUNDLE_PATH = os.path.join(
    os.path.dirname(__file__), "vendor", "pyyaml-py35.zip"
)
_ACCELERATED_MODULES_PATH = os.path.join(
    os.path.dirname(__file__), "accelerated_modules"
)
_JINJA_BUNDLE_MARKER = "# __ANSIROLL_JINJA_BUNDLE__"
_WORKER_CODE_MARKER = "# __ANSIROLL_WORKER_CODE__"
_STANDALONE_INVENTORY_NAMES = frozenset(("inventory_hostname", "inventory_hostname_short", "group_names"))
_RUNTIME_INVENTORY_NAMES = _STANDALONE_INVENTORY_NAMES | {"vars", "hostvars"}

_FOOTER = '''

if __name__ == "__main__":
    import sys as _sys
    _host = globals().get("_INJECTED_HOST") or os.environ.get("ANSIROLL_HOST", _ANSIROLL_HOST)
    _host_vars = {"inventory_hostname": _host}
    _host_vars.update(_BAKED_GROUP_VARS)
    # ANSIROLL_VARS_FILE follows `ansible-playbook -e` semantics: extra vars,
    # highest precedence (play vars must not override them).
    _extra_vars = {}
    _vars_file = os.environ.get("ANSIROLL_VARS_FILE")
    if _vars_file:
        try:
            with open(_vars_file) as _vf:
                _extra_vars.update(json.load(_vf))
        except Exception as _e:
            _sys.stderr.write("ansiroll: cannot load ANSIROLL_VARS_FILE %r: %s\\n"
                              % (_vars_file, _e))
            _sys.exit(2)
    # ANSIROLL_EXTRA_VARS: the same, as an inline JSON object (handy for a few
    # per-run values without shipping a file).
    _inline_vars = os.environ.get("ANSIROLL_EXTRA_VARS")
    if _inline_vars:
        try:
            _extra_vars.update(json.loads(_inline_vars))
        except Exception as _e:
            _sys.stderr.write("ansiroll: cannot parse ANSIROLL_EXTRA_VARS: %s\\n" % _e)
            _sys.exit(2)
    if "_INJECTED_VARS" in globals():
        _host_vars.update(_INJECTED_VARS)
    _become_pass = globals().get("_INJECTED_BECOME_PASS") or os.environ.get("ANSIROLL_BECOME_PASS")
    # Direct runs are for humans: Ansible-like output by default. Controllers use
    # agent mode and never come through here; ANSIROLL_RAW=1 restores the framed
    # JSON protocol for anything that does drive a direct run programmatically.
    _pretty = os.environ.get("ANSIROLL_RAW", "").lower() not in ("1", "true", "yes")
    _profile = os.environ.get("ANSIROLL_PROFILE", "").lower() in ("1", "true", "yes", "on")
    _native_only = os.environ.get("ANSIROLL_NATIVE_ONLY", "").lower() in ("1", "true", "yes", "on")
    _SPECIALIZED_VARS.update(_FROZEN_VARS)
    _conflicts = sorted(k for k, v in _SPECIALIZED_VARS.items()
                        if k in _extra_vars and _extra_vars[k] != v)
    if _conflicts:
        _sys.stderr.write(
            "ansiroll: runtime extra vars conflict with fixed build-time values: %s\\n"
            % ", ".join(_conflicts)
        )
        _sys.exit(2)
    _extra_vars.update(_SPECIALIZED_VARS)
    _sys.exit(main(PLAYS, MODULES, ANSIBALLZ_CODE, host_vars=_host_vars,
                   become_password=_become_pass, extra_vars=_extra_vars,
                   pretty=_pretty, profile=_profile, native_only=_native_only,
                   module_bundle=_ansiroll_import_bytes,
                   resource_reader=_ansiroll_resource_read,
                   engine_bootstrap=_ANSIROLL_BOOTSTRAP_SECONDS))
'''


def compile_playbook(playbook_path, python_interpreter="/usr/bin/python3",
                     vault_password_file=None, *, extra_vars=None,
                     tree_shake=False, freeze_playbook_vars=False,
                     freeze_vars=None, on_playbook_open=None):
    """Parse, embed modules, and return (source_text, manifest_dict)."""
    plays, actions = parse_playbook(playbook_path, vault_password_file=vault_password_file,
                                   extra_vars=extra_vars, on_playbook_open=on_playbook_open)
    for play in plays:
        for entry in play.get("deferred_vars_files") or ():
            if entry.get("unmodeled") and not entry.get("has_compile_selection"):
                raise ValueError(
                    "ansiroll: vars_files name %r starts with a template that is only known at run time, so its "
                    "files cannot be bundled; make it resolvable at compile time (e.g. with -e)"
                    % entry["unmodeled"][0])
    group_vars = load_adjacent_group_vars(playbook_path,
                                          vault_password_file=vault_password_file)
    frozen_vars, freeze_info = _resolve_frozen_vars(
        plays, group_vars, extra_vars or {}, bool(freeze_playbook_vars),
        set(freeze_vars or []),
    )
    for play in plays:
        play.pop("ordered_vars_file_mutable_names", None)
    specialized_vars = dict(frozen_vars)
    specialized_vars.update(extra_vars or {})
    original_actions = set(actions)
    shake_stats = {"removed_nodes": 0, "simplified_conditions": 0}
    if tree_shake:
        _tree_shake_plays(plays, specialized_vars, shake_stats)
        shake_stats["specialized_accelerators"] = _specialize_accelerators(
            plays, specialized_vars
        )
        present = _actions_in_plays(plays)
        actions = {action for action in actions if action in present}
    surviving_actions = set(actions)
    accelerated_only_actions = _accelerated_only_actions(plays, actions)
    if accelerated_only_actions:
        _mark_required_accelerators(plays, accelerated_only_actions)
        actions = actions - accelerated_only_actions
    embedder = ModuleEmbedder(python_interpreter=python_interpreter)
    modules = {}
    unsupported = []
    for action in sorted(actions):
        try:
            meta = embedder.embed(action)
        except Exception as ex:
            unsupported.append({"action": action, "error": str(ex)})
            continue
        if meta is not None:
            modules[action] = meta

    unsupported_actions = {u["action"] for u in unsupported}
    if unsupported_actions:
        _mark_unsupported(plays, unsupported_actions)

    fallbacks = _collect_fallbacks(plays)
    # Frozen values were already rendered here; ship them as data so the
    # runtime does not template them a second time.
    shipped_vars = {name: _encode_rendered(value)
                    for name, value in frozen_vars.items()}
    source, bundle_stats = _render_source_with_stats(
        plays, modules, embedder.ansiballz_code or "", group_vars,
        specialized_vars=extra_vars, frozen_vars=shipped_vars,
    )
    manifest = {
        "version": 2,
        "bundle_format": 2,
        "playbook": os.path.abspath(playbook_path),
        "plays": [{"name": p["name"], "hosts": p["hosts"]} for p in plays],
        "modules": sorted(modules.keys()),
        "accelerated_only_modules": sorted(accelerated_only_actions),
        "unsupported_modules": unsupported,
        "fallback_tasks": fallbacks,
        "baked_group_vars": sorted(group_vars.keys()),
        "python_interpreter": python_interpreter,
        "bundles": bundle_stats,
        "tree_shake": {
            "enabled": bool(tree_shake),
            "specialized_var_names": sorted(specialized_vars),
            "freeze_playbook_vars": bool(freeze_playbook_vars),
            "explicit_frozen_vars": sorted(set(freeze_vars or [])),
            "frozen_var_names": sorted(frozen_vars),
            "frozen_var_sources": freeze_info["sources"],
            "skipped_playbook_vars": freeze_info["skipped"],
            "removed_nodes": shake_stats["removed_nodes"],
            "simplified_conditions": shake_stats["simplified_conditions"],
            "specialized_accelerators": shake_stats.get(
                "specialized_accelerators", 0
            ),
            "removed_modules": sorted(original_actions - surviving_actions),
        },
    }
    return source, manifest


def _contains_undefined(value):
    from .runtime import _is_undef
    if _is_undef(value):
        return True
    if isinstance(value, dict):
        return any(_contains_undefined(k) or _contains_undefined(v)
                   for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_undefined(v) for v in value)
    return False


def _encode_rendered(value):
    """Wire form of a rendered value, including literal string provenance."""
    from .runtime import _UntrustedConditionValue

    if isinstance(value, _UntrustedConditionValue):
        return {"untrusted": str(value)}
    if isinstance(value, str):
        if "{{" in value or "{%" in value:
            return {"text": str(value)}
        return value
    if isinstance(value, dict):
        return {"mapping": [[_encode_rendered(k), _encode_rendered(v)]
                            for k, v in value.items()]}
    if isinstance(value, tuple):
        return {"tuple": [_encode_rendered(v) for v in value]}
    if isinstance(value, list):
        return [_encode_rendered(v) for v in value]
    return value


def _freeze_value(value, context):
    from .runtime import Runtime
    runtime = Runtime({}, "", {})
    if _runtime_dependent(value, context, runtime.templater.env):
        return None, False
    try:
        resolved = runtime.template(value, context)
    except Exception:
        return None, False
    if _contains_undefined(resolved):
        return None, False
    return resolved, True


def _walk_play_nodes(plays):
    def walk(nodes):
        for node in nodes or []:
            if node.get("tree_shaken_handler"):
                continue
            yield node
            if node.get("type") == "block":
                for section in ("block", "rescue", "always"):
                    yield from walk(node.get(section))
            elif node.get("type") == "include":
                yield from walk(node.get("tasks"))
                yield from walk(node.get("handlers"))

    for play in plays:
        yield from walk(play.get("tasks"))
        yield from walk(play.get("handlers"))


def _shadowed_var_names(plays):
    names = set()
    for node in _walk_play_nodes(plays):
        names.update(node.get("vars") or {})
        names.update(node.get("handler_block_vars") or {})
        names.update(node.get("role_vars") or {})
        names.update(node.get("role_params") or {})
        names.update(_runtime_write_names(
            node.get("action"), node.get("args") or {}, node.get("register"),
        ))
        names.update(_loop_shadowed_names(node))
    for play in plays:
        if "ordered_vars_file_mutable_names" in play:
            names.update(play["ordered_vars_file_mutable_names"])
            continue
        for entry in play.get("deferred_vars_files") or ():
            for data in entry["files"].values():
                if isinstance(data, list):
                    for item in data:
                        names.update(item)
    return names


def _resolve_frozen_vars(plays, group_vars, extra_vars,
                         freeze_playbook_vars, requested):
    """Resolve immutable values and return (values, non-secret manifest metadata).

    A frozen value is injected for the whole run (with extra-var precedence),
    so a name is only frozen when it resolves to the same value in every play:
    a play that does not define it must still see it undefined, as in Ansible.
    """
    group_vars = group_vars or {}
    extra_vars = extra_vars or {}
    sources = {}
    skipped = {}
    frozen = {}
    shadowed = _shadowed_var_names(plays)
    runtime_context = _RUNTIME_INVENTORY_NAMES - extra_vars.keys()

    def is_shadowed(name):
        return _can_write_var(shadowed, name)

    names = set(requested)
    if freeze_playbook_vars:
        for play in plays:
            names.update((play.get("vars") or {}).keys())

    for name in sorted(names):
        explicit = name in requested
        if name in extra_vars:
            continue
        if is_shadowed(name):
            if explicit:
                raise ValueError(
                    "ansiroll: cannot freeze %r because a task, block, role, set_fact, "
                    "include_vars, register, loop variable or run-time vars_files shadows it" % name
                )
            skipped[name] = (
                "shadowed by task/block/role vars, set_fact, include_vars, register, loop var or run-time vars_files"
            )
            continue
        values = []
        origins = []
        reason = None
        for index, play in enumerate(plays):
            play_vars = play.get("vars") or {}
            if name in play_vars:
                raw, origin = play_vars[name], "play[%d].vars" % index
            elif name in group_vars:
                raw, origin = group_vars[name], "group_vars"
            else:
                reason = "undefined"
                break
            context = dict(group_vars)
            context.update(play_vars)
            context.update(extra_vars)
            runtime_inventory = runtime_context - (_STANDALONE_INVENTORY_NAMES & context.keys())
            if _depends_on_vars(raw, context, lambda dependency: is_shadowed(dependency)
                                or dependency in runtime_inventory):
                reason = "shadowed dependency"
                break
            value, known = _freeze_value(raw, context)
            if not known:
                reason = "unresolved"
                break
            values.append(value)
            origins.append(origin)
        if reason is None and any(v != values[0] for v in values[1:]):
            reason = "conflicting"
        if reason is None and not values:
            reason = "undefined"
        if reason is not None:
            if explicit:
                if reason == "conflicting":
                    raise ValueError(
                        "ansiroll: frozen variable %r has conflicting values "
                        "across plays" % name)
                if reason == "shadowed dependency":
                    raise ValueError(
                        "ansiroll: frozen variable %r depends on a runtime variable" % name)
                if reason == "unresolved":
                    raise ValueError(
                        "ansiroll: explicitly frozen variable %r is unresolved" % name)
                if not any(name in (p.get("vars") or {}) for p in plays):
                    raise ValueError(
                        "ansiroll: explicitly frozen variable %r is undefined" % name)
                raise ValueError(
                    "ansiroll: cannot freeze %r because it is not defined in "
                    "every play" % name)
            skipped[name] = {
                "undefined": "not defined in every play",
                "unresolved": "value is not resolvable at compile time",
                "conflicting": "plays define different values",
                "shadowed dependency": "depends on a runtime variable",
            }[reason]
            continue
        frozen[name] = values[0]
        sources[name] = sorted(set(origins))
    return frozen, {
        "sources": {name: sources[name] for name in sorted(sources)},
        "skipped": {name: skipped[name] for name in sorted(skipped)},
    }


def _condition_value(condition, variables, templater):
    """Return True/False, or None when a conditional is not statically known."""
    import jinja2

    trusted = (isinstance(condition, dict)
               and set(condition) == {"__ansiroll_trusted_condition__"})
    if trusted:
        condition = condition["__ansiroll_trusted_condition__"]
    if isinstance(condition, bool):
        return condition
    parsed = None
    try:
        from jinja2 import meta as jinja_meta
        source = str(condition) if trusted else "{{ (%s) }}" % condition
        parsed = templater.env.parse(source)
        # Jinja's undeclared-variable scan compiles and can fold constant
        # filter operands in this AST. Check for runtime filters first.
        if _uses_nondeterministic_filter(parsed):
            return None
        referenced = jinja_meta.find_undeclared_variables(parsed)
    except Exception:
        return None
    # Time-dependent globals and controller lookups in conditions run when
    # the task runs.
    # find_undeclared_variables omits environment globals, so look at the
    # names the expression actually loads.
    from jinja2 import nodes as jinja_nodes
    global_names = {node.name for node in parsed.find_all(jinja_nodes.Name)
                    if node.ctx == "load" and node.name in templater.env.globals}
    if global_names - variables.keys() - _DETERMINISTIC_GLOBALS:
        return None
    referenced.update(global_names & variables.keys())
    # A fixed variable can still contain a template. Its defaults and tests
    # must see the runtime scope when a referenced name is absent here.
    def is_runtime_input(name):
        return name not in variables

    if any(_depends_on_vars(variables[name], variables, is_runtime_input)
           for name in referenced & variables.keys()):
        return None
    if not referenced.issubset(variables):
        # A tracking StrictUndefined distinguishes an unknown value that Jinja
        # never evaluates due to boolean short-circuiting from one consumed by
        # the expression (including via ``default`` or ``is defined``).  Only
        # the former is safe to simplify under our conservative contract.
        if trusted:
            return None
        try:
            class TrackingUndefined(jinja2.StrictUndefined):
                accessed = False

                def __init__(self, *args, **kwargs):
                    type(self).accessed = True
                    super(TrackingUndefined, self).__init__(*args, **kwargs)

            strict_env = templater.env.overlay(undefined=TrackingUndefined)
            result = strict_env.compile_expression(
                str(condition), undefined_to_none=False
            )(**variables)
        except Exception:
            return None
        if TrackingUndefined.accessed:
            return None
        return result if isinstance(result, bool) else None
    if isinstance(condition, str) and ("{{" in condition or "{%" in condition):
        if not trusted:
            return None
        try:
            result = templater.render_string(condition, variables)
        except Exception:
            return None
    else:
        try:
            result = templater.evaluate(condition, variables)
        except Exception:
            return None
    # Leave non-boolean results to the runtime's core/config policy.
    return result if isinstance(result, bool) else None


# Filters whose result depends on when/where they run.
_NONDETERMINISTIC_FILTERS = frozenset(
    ("random", "shuffle", "strftime", "expanduser", "realpath"))
# Lookups run on the controller, so compile time is the right time for them.
_CONTROLLER_GLOBALS = frozenset(("lookup", "query", "q"))
_DETERMINISTIC_GLOBALS = frozenset(("range", "dict", "cycler", "joiner", "namespace"))


def _runtime_dependent(value, variables, env, _seen=None):
    """True when templating ``value`` (or a variable it references) calls a
    runtime-dependent global (now(), ...) or a host/time dependent filter, so its
    result is not known when compiling."""
    seen = set() if _seen is None else _seen
    if isinstance(value, dict):
        if set(value) in ({"__ansiroll_unsafe__"}, {"__ansiroll_vault__"}):
            return False
        return any(_runtime_dependent(item, variables, env, seen)
                   for pair in value.items() for item in pair)
    if isinstance(value, (list, tuple)):
        return any(_runtime_dependent(v, variables, env, seen) for v in value)
    if not isinstance(value, str) or ("{{" not in value and "{%" not in value):
        return False
    from jinja2 import nodes
    try:
        parsed = env.parse(value)
    except Exception:
        return False
    def depends(name):
        if name in seen:
            return False
        seen.add(name)
        return _runtime_dependent(variables[name], variables, env, seen)

    for call in parsed.find_all(nodes.Call):
        if not (isinstance(call.node, nodes.Name) and call.node.name in _CONTROLLER_GLOBALS
                and call.args and isinstance(call.args[0], nodes.Const)
                and call.args[0].value in ("vars", "ansible.builtin.vars", "ansible.legacy.vars")):
            continue
        if call.dyn_args is not None:
            return True
        for arg in call.args[1:]:
            if not isinstance(arg, nodes.Const) or not isinstance(arg.value, str):
                return True
            if arg.value in variables and depends(arg.value):
                return True
    for node in parsed.find_all(nodes.Name):
        if node.ctx != "load":
            continue
        if node.name in variables:
            if depends(node.name):
                return True
        elif (node.name in env.globals and node.name not in _CONTROLLER_GLOBALS
              and node.name not in _DETERMINISTIC_GLOBALS):
            return True
    return _uses_nondeterministic_filter(parsed)


def _uses_nondeterministic_filter(parsed):
    from jinja2 import nodes
    for node in parsed.find_all(nodes.Filter):
        name = node.name.rsplit(".", 1)[-1]
        args = iter(node.args)
        while name == "map":
            # map(attribute=...) does not call a filter. For positional map,
            # each mapped filter consumes its name before forwarding arguments.
            arg = next(args, None)
            if arg is None:
                if node.dyn_args is not None:
                    return True
                break
            if not isinstance(arg, nodes.Const) or not isinstance(arg.value, str):
                return True
            name = arg.value.rsplit(".", 1)[-1]
        if name in _NONDETERMINISTIC_FILTERS:
            return True
    return False


def _loop_shadowed_names(node):
    """Names a looped task binds per item. They take precedence over extra
    vars (Ansible evaluates the task's ``when`` per item with them set)."""
    if node.get("loop") is None:
        return frozenset()
    return frozenset(_loop_binding_names(
        node.get("loop_var"), node.get("index_var"), node.get("loop_extended"),
    ))


def _condition_cache_key(condition):
    if isinstance(condition, (str, bool, int, float, type(None))):
        return type(condition).__name__, condition
    if isinstance(condition, dict):
        try:
            return "dict", json.dumps(
                condition, sort_keys=True, default=_json_default
            )
        except Exception:
            return None
    return None


def _condition_source(condition):
    if isinstance(condition, dict) and set(condition) == {"__ansiroll_trusted_condition__"}:
        return condition["__ansiroll_trusted_condition__"]
    return "{{ (%s) }}" % condition if isinstance(condition, str) else condition


def _simplify_when(conditions, variables, templater, stats, cache, shadowed=frozenset(),
                   preserve_errors=False):
    unmasked = variables
    if shadowed:
        variables = {k: v for k, v in variables.items() if k not in shadowed}
    remaining = []
    for condition in conditions or []:
        # Masking a loop binding is not enough when another variable reads it, e.g. alias: "{{ env }}".
        if shadowed and _depends_on_vars(_condition_source(condition), unmasked, shadowed.__contains__):
            remaining.append(condition)
            continue
        key = _condition_cache_key(condition)
        if key is not None:
            key = (key, tuple(sorted(shadowed)))
        if key is not None and key in cache:
            value = cache[key]
        else:
            value = _condition_value(condition, variables, templater)
            if key is not None:
                cache[key] = value
        if value is False:
            if preserve_errors and remaining:
                # An unresolved earlier condition can fail before this one skips.
                return True, remaining + [False]
            return False, []
        if value is True:
            stats["simplified_conditions"] += 1
        else:
            remaining.append(condition)
    return True, remaining


def _recursive_include_loops(nodes):
    """Collect loop bindings that reach an include body on recursive entry."""
    bodies = {}
    pending = []
    inherited = {}

    def walk(nodes, enclosing_loops):
        for node in nodes or []:
            kind = node.get("type")
            if kind == "block":
                for key in ("block", "rescue", "always"):
                    walk(node.get(key), enclosing_loops)
            elif kind == "include":
                loop_names = enclosing_loops | _loop_shadowed_names(node)
                if node.get("include_id"):
                    bodies[node["include_id"]] = node.get("tasks")
                if node.get("tasks_ref") and loop_names:
                    pending.append((node["tasks_ref"], loop_names))
                walk(node.get("tasks"), loop_names)

    walk(nodes, frozenset())
    while pending:
        target, loop_names = pending.pop()
        previous = inherited.get(target, frozenset())
        if not loop_names.issubset(previous):
            inherited[target] = previous | loop_names
            walk(bodies.get(target), inherited[target])
    return inherited


def _tree_shake_nodes(nodes, variables, templater, stats, cache, recursive_loops,
                      enclosing_loops=frozenset(), handlers=False):
    kept = []
    for node in nodes or []:
        loop_names = enclosing_loops | _loop_shadowed_names(node)
        possible, remaining = _simplify_when(
            node.get("when"), variables, templater, stats, cache,
            loop_names, preserve_errors=handlers,
        )
        if not possible:
            stats["removed_nodes"] += 1
            if handlers:
                # Notification matching ignores conditions. Keep the entry so
                # it can still match and report a skipped handler at flush time.
                node["when"] = [False]
                node["tree_shaken_handler"] = True
                kept.append(node)
            continue
        node["when"] = remaining
        kind = node.get("type")
        if kind == "block":
            node["block"] = _tree_shake_nodes(
                node.get("block"), variables, templater, stats, cache, recursive_loops, loop_names
            )
            node["rescue"] = _tree_shake_nodes(
                node.get("rescue"), variables, templater, stats, cache, recursive_loops, loop_names
            )
            node["always"] = _tree_shake_nodes(
                node.get("always"), variables, templater, stats, cache, recursive_loops, loop_names
            )
            if not node["block"]:
                node["rescue"] = []
            if not node["block"] and not node["always"] and "include_variant" not in node:
                stats["removed_nodes"] += 1
                continue
        elif kind == "include":
            loop_names |= recursive_loops.get(node.get("include_id"), frozenset())
            node["tasks"] = _tree_shake_nodes(
                node.get("tasks"), variables, templater, stats, cache, recursive_loops, loop_names
            )
            if "handlers" in node:
                node["handlers"] = _tree_shake_nodes(
                    node["handlers"], variables, templater, stats, cache, recursive_loops, loop_names,
                    handlers=True,
                )
        kept.append(node)
    return kept


def _tree_shake_plays(plays, variables, stats):
    from .runtime import _Templater

    templater = _Templater()
    cache = {}
    for play in plays:
        for section in ("tasks", "handlers"):
            nodes = play.get(section)
            play[section] = _tree_shake_nodes(
                nodes, variables, templater, stats, cache, _recursive_include_loops(nodes),
                handlers=section == "handlers",
            )


def _actions_in_plays(plays):
    actions = {
        node["action"] for node in _walk_play_nodes(plays)
        if node.get("action") and node.get("type") != "include"
    }
    if any(node.get("special") in ("template", "copy") for node in _walk_play_nodes(plays)):
        actions.update(COPY_ACTION_MODULES)
    if any(play.get("gather_facts") for play in plays):
        actions.add("ansible.builtin.setup")
    return actions


def _specialize_accelerators(plays, variables):
    from .runtime import Runtime

    runtime = Runtime({}, "", {})
    count = 0

    def is_runtime_input(name):
        return name not in variables

    for node in _walk_play_nodes(plays):
        if node.get("accelerator") != "dynamic":
            continue
        args = node.get("args") or {}
        # An unresolved alias can render to a valid default at build time
        # even though the runtime arguments need the official module.
        if _depends_on_vars(args, variables, is_runtime_input):
            continue
        try:
            resolved = runtime.template(args, dict(variables))
        except Exception:
            continue
        if (
                not _contains_undefined(resolved)
                and can_accelerate(
                    str(node.get("action") or "").split(".")[-1],
                    resolved,
                    check_mode=False,
                )):
            node["accelerator"] = "always"
            count += 1
    return count


def _accelerated_only_actions(plays, actions):
    eligible = {
        action for action in actions
        if str(action).split(".")[-1] in TOTAL_NAMES
    }
    seen = set()
    for node in _walk_play_nodes(plays):
        action = node.get("action")
        if action not in eligible:
            continue
        seen.add(action)
        if (
                node.get("accelerator") != "always"
                or node.get("fallback")
                # The command guard cannot always restore the agent's cwd.
                or (str(action).split(".")[-1] in ("command", "shell")
                    and (node.get("args") or {}).get("chdir") is not None)
                or int(node.get("async_val") or 0)):
            eligible.discard(action)
    return eligible & seen


def _mark_required_accelerators(plays, actions):
    for node in _walk_play_nodes(plays):
        if node.get("action") in actions:
            node["accelerator"] = "required"


def _mark_unsupported(plays, unsupported_actions):
    for node in _walk_play_nodes(plays):
        if node.get("action") in unsupported_actions and not node.get("fallback"):
            node["fallback"] = True
            node["fallback_reason"] = "module %s could not be embedded" % node["action"]


def _collect_fallbacks(plays):
    return [
        {"uuid": node.get("uuid"), "name": node.get("name"),
         "reason": node.get("fallback_reason")}
        for node in _walk_play_nodes(plays) if node.get("fallback")
    ]


def _json_default(o):
    # Ansible tagged types (EncryptedString, trusted strings, ...) reach us from
    # parsed vars; carry them as plain text, or a marker when undecryptable.
    ciphertext = getattr(o, "_ciphertext", None)
    if ciphertext is not None:
        try:
            return str(o)  # decrypts when the loader has vault secrets
        except Exception:
            if isinstance(ciphertext, bytes):
                ciphertext = ciphertext.decode("utf-8", "replace")
            if isinstance(ciphertext, str) and ciphertext.startswith("$ANSIBLE_VAULT;"):
                return {"__ansiroll_vault__": ciphertext}
    try:
        return str(o)
    except Exception:
        return "__ansiroll_undecryptable__"


def _zip_info(name):
    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _deterministic_zip(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as archive:
        for name in sorted(entries):
            archive.writestr(_zip_info(name), entries[name])
    return out.getvalue()


def _add_import_entry(entries, name, data, origin):
    old = entries.get(name)
    if old is not None and old != data:
        raise RuntimeError(
            "ansiroll: conflicting import bundle entry %r from %s" % (name, origin)
        )
    entries[name] = data


def _asset_marker(data, assets):
    digest = hashlib.sha256(data).hexdigest()
    assets.setdefault(digest, data)
    return {"__ansiroll_asset__": digest}


def _externalize_baked(node, assets):
    baked = node.get("_baked")
    if isinstance(baked, dict):
        out = {}
        for name, body in baked.items():
            if isinstance(body, str):
                out[name] = _asset_marker(body.encode("utf-8", "surrogateescape"), assets)
            else:
                out[name] = body
        node["_baked"] = out
    for key in ("block", "rescue", "always", "tasks", "handlers"):
        for child in node.get(key) or []:
            if isinstance(child, dict):
                _externalize_baked(child, assets)


def _externalize_assets(plays, assets):
    """Move baked file and plugin bodies out of the JSON task graph.

    Only the loader's own fields are touched: task ``_baked`` bodies and the
    play's ``jinja_plugins`` sources. User data (vars, args, messages) that
    happens to use the same key names is left alone."""
    for play in plays:
        for plugin in play.get("jinja_plugins") or []:
            source = plugin.get("source")
            if isinstance(source, str):
                plugin["source"] = _asset_marker(
                    source.encode("utf-8", "surrogateescape"), assets)
        for key in ("tasks", "handlers"):
            for node in play.get(key) or []:
                _externalize_baked(node, assets)
    return plays


def _base_import_entries():
    entries = {}
    for path, origin in (
        (_JINJA_BUNDLE_PATH, "Jinja bundle"),
        (_YAML_BUNDLE_PATH, "YAML bundle"),
    ):
        with zipfile.ZipFile(path, "r") as archive:
            for info in archive.infolist():
                if not info.is_dir():
                    _add_import_entry(
                        entries, info.filename, archive.read(info.filename), origin
                    )
    # Generated agents import accelerators lazily from the same shared ZIP.
    # Use a minimal package root: the controller's ansiroll.__init__ imports
    # compiler dependencies which intentionally do not exist on the target.
    _add_import_entry(entries, "ansiroll/__init__.py", b"", "accelerators")
    for name in sorted(os.listdir(_ACCELERATED_MODULES_PATH)):
        if not name.endswith(".py"):
            continue
        path = os.path.join(_ACCELERATED_MODULES_PATH, name)
        with open(path, "rb") as fh:
            _add_import_entry(
                entries, "ansiroll/accelerated_modules/" + name,
                fh.read(), "accelerators",
            )
    return entries


def _module_archive_entries(raw):
    entries = {}
    with zipfile.ZipFile(io.BytesIO(raw), "r") as archive:
        for info in archive.infolist():
            if not info.is_dir():
                entries[info.filename] = archive.read(info.filename)
    return entries


def _build_bundles(plays, modules, group_vars=None, specialized_vars=None,
                   frozen_vars=None):
    module_meta = {}
    module_archives = {}
    for action in sorted(modules):
        meta = modules[action]
        raw = base64.b64decode(meta["zip"])
        digest = hashlib.sha256(raw).digest()
        module_archives.setdefault(digest, (raw, action))
        module_meta[action] = {
            "module_fqn": meta["module_fqn"],
            "profile": meta.get("profile", "legacy"),
        }

    import_entries = _base_import_entries()
    for digest in sorted(module_archives):
        raw, origin = module_archives[digest]
        for name, data in _module_archive_entries(raw).items():
            _add_import_entry(import_entries, name, data, origin)
    import_bundle = _deterministic_zip(import_entries)

    assets = {}
    compact_plays = _externalize_assets(copy.deepcopy(plays), assets)
    for play in compact_plays:
        entries = play.get("deferred_vars_files", ())
        if entries and play.get("ordered_vars_files"):
            index = {id(entry): position for position, entry in enumerate(entries)}
            play["ordered_vars_files"] = [index.get(id(entry), entry) for entry in play["ordered_vars_files"]]
    data_entries = {
        "plays.json": json.dumps(compact_plays, default=_json_default).encode("utf-8"),
        "modules.json": json.dumps(module_meta, default=_json_default).encode("utf-8"),
        "group_vars.json": json.dumps(group_vars or {}, default=_json_default).encode("utf-8"),
        "specialized_vars.json": json.dumps(
            specialized_vars or {}, default=_json_default).encode("utf-8"),
        "frozen_vars.json": json.dumps(
            frozen_vars or {}, default=_json_default).encode("utf-8"),
    }
    for digest, data in assets.items():
        data_entries["assets/" + digest] = data

    data_bundle = _deterministic_zip(data_entries)
    stats = {
        "import_binary_size": len(import_bundle),
        "import_encoded_size": len(base64.b64encode(import_bundle)),
        "data_binary_size": len(data_bundle),
        "data_encoded_size": len(base64.b64encode(data_bundle)),
        "unique_module_archives": len(module_archives),
        "unique_import_paths": len(import_entries),
        "asset_count": len(assets),
        "asset_bytes": sum(len(v) for v in assets.values()),
    }
    return import_bundle, data_bundle, stats


def _bundle_bootstrap(import_bundle):
    """Build source that exposes the shared import bundle without filesystem I/O."""
    return '''\
import base64 as _bundle_base64
import importlib.abc as _bundle_importlib_abc
import importlib.util as _bundle_importlib_util
import io as _bundle_io
import sys as _bundle_sys
import zipfile as _bundle_zipfile

_ANSIROLL_IMPORT_BUNDLE_B64 = %r
_ansiroll_import_bytes = _bundle_base64.b64decode(_ANSIROLL_IMPORT_BUNDLE_B64)

class _AnsirollImportBundle(_bundle_importlib_abc.MetaPathFinder,
                              _bundle_importlib_abc.Loader):
    def __init__(self, data):
        self.archive = _bundle_zipfile.ZipFile(_bundle_io.BytesIO(data))
        self.names = set(self.archive.namelist())
        self.code = {}

    @staticmethod
    def _paths(fullname):
        path = fullname.replace(".", "/")
        return path + "/__init__.py", path + ".py"

    def _entry(self, fullname):
        package, module = self._paths(fullname)
        return package if package in self.names else module

    def find_spec(self, fullname, path=None, target=None):
        package, module = self._paths(fullname)
        if package in self.names:
            spec = _bundle_importlib_util.spec_from_loader(
                fullname, self, is_package=True
            )
            spec.submodule_search_locations = [fullname.replace(".", "/")]
            return spec
        if module in self.names:
            return _bundle_importlib_util.spec_from_loader(
                fullname, self, is_package=False
            )
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        entry = self._entry(module.__name__)
        module.__file__ = "/_ansiroll_imports/" + entry
        exec(self.get_code(module.__name__), module.__dict__)

    def get_filename(self, fullname):
        return "/_ansiroll_imports/" + self._entry(fullname)

    def is_package(self, fullname):
        package, _ = self._paths(fullname)
        return package in self.names

    def get_source(self, fullname):
        return self.archive.read(self._entry(fullname)).decode(
            "utf-8", "surrogateescape"
        )

    def get_code(self, fullname):
        entry = self._entry(fullname)
        code = self.code.get(entry)
        if code is None:
            code = compile(
                self.archive.read(entry),
                "/_ansiroll_imports/" + entry,
                "exec",
            )
            self.code[entry] = code
        return code

    def get_data(self, path):
        marker = "/_ansiroll_imports/"
        if path.startswith(marker):
            path = path[len(marker):]
        return self.archive.read(path)

_ansiroll_import_finder = _AnsirollImportBundle(
    _ansiroll_import_bytes
)
_bundle_sys.meta_path.insert(0, _ansiroll_import_finder)
''' % base64.b64encode(import_bundle)


def _controller_core_version():
    """ansible-core version doing the compilation (baked into the artifact)."""
    try:
        from ansible.release import __version__
        return str(__version__)
    except Exception:
        return None


def _render_source(plays, modules, ansiballz_code, group_vars=None):
    return _render_source_with_stats(
        plays, modules, ansiballz_code, group_vars
    )[0]


def _controller_inventory_address_patterns():
    from ansible.parsing.utils.addresses import patterns

    return tuple((patterns[name].pattern, int(patterns[name].flags)) for name in ("ipv4", "ipv6"))


def _render_source_with_stats(plays, modules, ansiballz_code, group_vars=None,
                              specialized_vars=None, frozen_vars=None):
    import_bundle, data_bundle, stats = _build_bundles(
        plays, modules, group_vars, specialized_vars, frozen_vars
    )
    with open(_RUNTIME_PATH, "r") as fh:
        runtime_src = fh.read()
    with open(_WORKER_PATH, "r") as fh:
        worker_src = fh.read()
    if runtime_src.count(_WORKER_CODE_MARKER) != 1:
        raise RuntimeError("Worker code marker missing or duplicated in runtime.py")
    runtime_src = runtime_src.replace(
        _WORKER_CODE_MARKER, "_WORKER_CODE = " + repr(worker_src)
    )
    if runtime_src.count(_JINJA_BUNDLE_MARKER) != 1:
        raise RuntimeError("Jinja bundle marker missing or duplicated in runtime.py")
    runtime_src = runtime_src.replace(
        _JINJA_BUNDLE_MARKER, _bundle_bootstrap(import_bundle)
    )

    parts = [
        "#!/usr/bin/env python3\n",
        "# -*- coding: utf-8 -*-\n",
        "# Generated by Ansiroll. Do not edit by hand.\n",
        "# Runs the whole play on this host using only Python (Ansible not required).\n",
        "import time as _ansiroll_boot_time\n",
        "_ANSIROLL_BOOT_STARTED = _ansiroll_boot_time.monotonic()\n",
        runtime_src,
        "\n\n# ===== embedded data =====\n",
        '_ANSIROLL_HOST = "localhost"\n',
        "_ANSIROLL_CORE_VERSION = " + repr(_controller_core_version()) + "\n",
        "_ANSIROLL_INVENTORY_ADDRESS_PATTERNS = " + repr(_controller_inventory_address_patterns()) + "\n",
        "ANSIBALLZ_CODE = " + repr(ansiballz_code) + "\n",
        "_ANSIROLL_DATA_BUNDLE = " + repr(base64.b64encode(data_bundle)) + "\n",
        "_ansiroll_data_zip = _bundle_zipfile.ZipFile(_bundle_io.BytesIO("
        "base64.b64decode(_ANSIROLL_DATA_BUNDLE)))\n",
        "def _ansiroll_resource_read(name):\n"
        "    return _ansiroll_data_zip.read(name)\n",
        "def _ansiroll_bundle_json(name):\n"
        "    return json.loads(_ansiroll_resource_read(name).decode('utf-8'))\n",
        "MODULES = _ansiroll_bundle_json('modules.json')\n",
        "PLAYS = _ansiroll_bundle_json('plays.json')\n",
        "_BAKED_GROUP_VARS = _ansiroll_bundle_json('group_vars.json')\n",
        "_SPECIALIZED_VARS = _ansiroll_bundle_json('specialized_vars.json')\n",
        "_FROZEN_VARS = {k: _decode_frozen_value(v) for k, v in "
        "_ansiroll_bundle_json('frozen_vars.json').items()}\n",
        "_ANSIROLL_BOOTSTRAP_SECONDS = (\n"
        "    _ansiroll_boot_time.monotonic() - _ANSIROLL_BOOT_STARTED\n"
        ")\n",
    ]
    parts += [
        _FOOTER,
    ]
    return "".join(parts), stats
