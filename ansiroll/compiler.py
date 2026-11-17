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
from .loader import load_adjacent_group_vars, parse_playbook
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
                     freeze_vars=None):
    """Parse, embed modules, and return (source_text, manifest_dict)."""
    plays, actions = parse_playbook(playbook_path, vault_password_file=vault_password_file)
    group_vars = load_adjacent_group_vars(playbook_path,
                                          vault_password_file=vault_password_file)
    frozen_vars, freeze_info = _resolve_frozen_vars(
        plays, group_vars, extra_vars or {}, bool(freeze_playbook_vars),
        set(freeze_vars or []),
    )
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
    source, bundle_stats = _render_source_with_stats(
        plays, modules, embedder.ansiballz_code or "", group_vars,
        specialized_vars=specialized_vars,
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


def _freeze_value(value, context):
    from .runtime import Runtime
    runtime = Runtime({}, "", {})
    try:
        resolved = runtime.template(value, context)
    except Exception:
        return None, False
    if _contains_undefined(resolved):
        return None, False
    return resolved, True


def _shadowed_var_names(plays):
    names = set()
    def walk(nodes):
        for node in nodes or []:
            names.update((node.get("vars") or {}).keys())
            names.update((node.get("role_vars") or {}).keys())
            if str(node.get("action") or "").split(".")[-1] == "set_fact":
                names.update((node.get("args") or {}).keys())
            if node.get("register"):
                names.add(node["register"])
            if node.get("loop") is not None:
                # Loop variables beat extra vars, inside included files too.
                names.add(node.get("loop_var") or "item")
                if node.get("index_var"):
                    names.add(node["index_var"])
            if node.get("type") == "block":
                walk(node.get("block")); walk(node.get("rescue")); walk(node.get("always"))
            elif node.get("type") == "include":
                walk(node.get("tasks"))
    for play in plays:
        walk(play.get("tasks")); walk(play.get("handlers"))
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

    names = set(requested)
    if freeze_playbook_vars:
        for play in plays:
            names.update((play.get("vars") or {}).keys())

    for name in sorted(names):
        explicit = name in requested
        if name in extra_vars:
            continue
        if name in shadowed:
            if explicit:
                raise ValueError(
                    "ansiroll: cannot freeze %r because a task, block, role, "
                    "set_fact, register or loop variable shadows it" % name
                )
            skipped[name] = "shadowed by task/block/role vars, set_fact, register or loop var"
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
    from .runtime import _is_undef, _to_bool
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
        referenced = jinja_meta.find_undeclared_variables(parsed)
    except Exception:
        return None
    # Templater globals (now(), lookup(), query(), ...) are evaluated when the
    # task runs; a condition that calls one is not statically known.
    # find_undeclared_variables omits environment globals, so look at the
    # names the expression actually loads.
    from jinja2 import nodes as jinja_nodes
    if any(node.name not in variables and node.name in templater.env.globals
           for node in parsed.find_all(jinja_nodes.Name)):
        return None
    if _uses_nondeterministic_filter(parsed):
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
        if TrackingUndefined.accessed or _is_undef(result):
            return None
        try:
            return bool(_to_bool(result))
        except Exception:
            return None
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
    if _is_undef(result):
        return None
    try:
        return bool(_to_bool(result))
    except Exception:
        return None


# Filters whose result depends on when/where they run.
_NONDETERMINISTIC_FILTERS = frozenset(("random", "shuffle", "strftime"))


def _uses_nondeterministic_filter(parsed):
    from jinja2 import nodes
    return any(node.name in _NONDETERMINISTIC_FILTERS
               for node in parsed.find_all(nodes.Filter))


def _loop_shadowed_names(node):
    """Names a looped task binds per item. They take precedence over extra
    vars (Ansible evaluates the task's ``when`` per item with them set)."""
    if node.get("loop") is None:
        return frozenset()
    names = {node.get("loop_var") or "item", "ansible_loop_var"}
    if node.get("index_var"):
        names.update((node["index_var"], "ansible_index_var"))
    if node.get("loop_extended"):
        names.add("ansible_loop")
    return frozenset(names)


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


def _simplify_when(conditions, variables, templater, stats, cache, shadowed=frozenset()):
    if shadowed:
        variables = {k: v for k, v in variables.items() if k not in shadowed}
    remaining = []
    for condition in conditions or []:
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
            return False, []
        if value is True:
            stats["simplified_conditions"] += 1
        else:
            remaining.append(condition)
    return True, remaining


def _tree_shake_nodes(nodes, variables, templater, stats, cache):
    kept = []
    for node in nodes or []:
        possible, remaining = _simplify_when(
            node.get("when"), variables, templater, stats, cache,
            _loop_shadowed_names(node),
        )
        if not possible:
            stats["removed_nodes"] += 1
            continue
        node["when"] = remaining
        kind = node.get("type")
        if kind == "block":
            node["block"] = _tree_shake_nodes(
                node.get("block"), variables, templater, stats, cache
            )
            node["rescue"] = _tree_shake_nodes(
                node.get("rescue"), variables, templater, stats, cache
            )
            node["always"] = _tree_shake_nodes(
                node.get("always"), variables, templater, stats, cache
            )
            if not node["block"]:
                node["rescue"] = []
            if not node["block"] and not node["always"]:
                stats["removed_nodes"] += 1
                continue
        elif kind == "include":
            node["tasks"] = _tree_shake_nodes(
                node.get("tasks"), variables, templater, stats, cache
            )
        kept.append(node)
    return kept


def _tree_shake_plays(plays, variables, stats):
    from .runtime import _Templater

    templater = _Templater()
    cache = {}
    for play in plays:
        play["tasks"] = _tree_shake_nodes(
            play.get("tasks"), variables, templater, stats, cache
        )
        play["handlers"] = _tree_shake_nodes(
            play.get("handlers"), variables, templater, stats, cache
        )


def _actions_in_plays(plays):
    actions = set()

    def walk(nodes):
        for node in nodes or []:
            if node.get("type") == "block":
                walk(node.get("block"))
                walk(node.get("rescue"))
                walk(node.get("always"))
            elif node.get("type") == "include":
                walk(node.get("tasks"))
            else:
                action = node.get("action")
                if action:
                    actions.add(action)
    for play in plays:
        walk(play.get("tasks"))
        walk(play.get("handlers"))
        if play.get("gather_facts"):
            actions.add("ansible.builtin.setup")
    return actions


def _specialize_accelerators(plays, variables):
    from .runtime import Runtime

    runtime = Runtime({}, "", {})
    count = 0

    def walk(nodes):
        nonlocal count
        for node in nodes or []:
            if node.get("type") == "block":
                walk(node.get("block"))
                walk(node.get("rescue"))
                walk(node.get("always"))
            elif node.get("type") == "include":
                walk(node.get("tasks"))
            elif node.get("accelerator") == "dynamic":
                try:
                    resolved = runtime.template(
                        node.get("args") or {}, dict(variables)
                    )
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

    for play in plays:
        walk(play.get("tasks"))
        walk(play.get("handlers"))
    return count


def _accelerated_only_actions(plays, actions):
    eligible = {
        action for action in actions
        if str(action).split(".")[-1] in TOTAL_NAMES
    }
    seen = set()

    def walk(nodes):
        for node in nodes or []:
            if node.get("type") == "block":
                walk(node.get("block"))
                walk(node.get("rescue"))
                walk(node.get("always"))
            elif node.get("type") == "include":
                walk(node.get("tasks"))
            else:
                action = node.get("action")
                if action not in eligible:
                    continue
                seen.add(action)
                if (
                        node.get("accelerator") != "always"
                        or node.get("fallback")
                        or int(node.get("async_val") or 0)):
                    eligible.discard(action)

    for play in plays:
        walk(play.get("tasks"))
        walk(play.get("handlers"))
    return eligible & seen


def _mark_required_accelerators(plays, actions):
    def walk(nodes):
        for node in nodes or []:
            if node.get("type") == "block":
                walk(node.get("block"))
                walk(node.get("rescue"))
                walk(node.get("always"))
            elif node.get("type") == "include":
                walk(node.get("tasks"))
            elif node.get("action") in actions:
                node["accelerator"] = "required"

    for play in plays:
        walk(play.get("tasks"))
        walk(play.get("handlers"))


def _mark_unsupported(plays, unsupported_actions):
    def walk(nodes):
        for n in nodes or []:
            if n.get("type") == "block":
                walk(n.get("block", []))
                walk(n.get("rescue", []))
                walk(n.get("always", []))
            elif n.get("type") == "include":
                walk(n.get("tasks", []))
            elif n.get("action") in unsupported_actions and not n.get("fallback"):
                n["fallback"] = True
                n["fallback_reason"] = "module %s could not be embedded" % n.get("action")
    for p in plays:
        walk(p.get("tasks"))
        walk(p.get("handlers"))


def _collect_fallbacks(plays):
    found = []

    def walk(nodes):
        for n in nodes or []:
            if n.get("type") == "block":
                walk(n.get("block", []))
                walk(n.get("rescue", []))
                walk(n.get("always", []))
            elif n.get("type") == "include":
                walk(n.get("tasks", []))
            elif n.get("fallback"):
                found.append({"uuid": n.get("uuid"), "name": n.get("name"),
                              "reason": n.get("fallback_reason")})
    for p in plays:
        walk(p.get("tasks"))
        walk(p.get("handlers"))
    return found


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
    for key in ("block", "rescue", "always", "tasks"):
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


def _build_bundles(plays, modules, group_vars=None, specialized_vars=None):
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
    data_entries = {
        "plays.json": json.dumps(compact_plays, default=_json_default).encode("utf-8"),
        "modules.json": json.dumps(module_meta, default=_json_default).encode("utf-8"),
        "group_vars.json": json.dumps(group_vars or {}, default=_json_default).encode("utf-8"),
        "specialized_vars.json": json.dumps(
            specialized_vars or {}, default=_json_default).encode("utf-8"),
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


def _render_source(plays, modules, ansiballz_code, group_vars=None):
    return _render_source_with_stats(
        plays, modules, ansiballz_code, group_vars
    )[0]


def _render_source_with_stats(plays, modules, ansiballz_code, group_vars=None,
                              specialized_vars=None):
    import_bundle, data_bundle, stats = _build_bundles(
        plays, modules, group_vars, specialized_vars
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
        "_ANSIROLL_BOOTSTRAP_SECONDS = (\n"
        "    _ansiroll_boot_time.monotonic() - _ANSIROLL_BOOT_STARTED\n"
        ")\n",
    ]
    parts += [
        _FOOTER,
    ]
    return "".join(parts), stats
