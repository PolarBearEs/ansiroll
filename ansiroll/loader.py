"""
Parse an Ansible playbook into Ansiroll's data model.

We reuse ansible-core's own parser (static imports/roles are already inlined by the
time we see the objects), then lower each Play/Block/Task into plain dicts that the
inlined runtime can interpret. This module runs only at compile time.
"""

from __future__ import annotations

import errno
import os
import pkgutil
import re
import stat
import time
from copy import copy
from functools import lru_cache

from .compat import find_module, init_loader
from .accelerated_modules import NAMES as ACCELERATED_ACTIONS
from .accelerated_modules import can_accelerate

# Actions that are coupled to the controller and cannot run inside the single file.
CONTROLLER_COUPLED = {
    "fetch", "ansible.builtin.fetch",
    "add_host", "ansible.builtin.add_host",
    "group_by", "ansible.builtin.group_by",
    "include_tasks", "ansible.builtin.include_tasks",
    "include_role", "ansible.builtin.include_role",
    "include_vars", "ansible.builtin.include_vars",
    "reboot", "ansible.builtin.reboot",
    "synchronize", "ansible.posix.synchronize",
    "wait_for_connection", "ansible.builtin.wait_for_connection",
}

# Action plugins backed by controller-side files; handled by baking src at compile time.
BAKED_ACTIONS = {
    "template", "ansible.builtin.template",
    "copy", "ansible.builtin.copy",
    "unarchive", "ansible.builtin.unarchive",
}

# Official modules the copy/template action plugins execute on the target.
COPY_ACTION_MODULES = ("ansible.builtin.copy", "ansible.builtin.file")

_ITEM_RE = re.compile(r"{{\s*item\s*}}")
_JINJA_RE = re.compile(r"{{.*?}}")
# A src like "{{ playbook_dir }}/roles/{{ role }}/..." can match many files; past this it is left unbaked.
_ABSOLUTE_TREE_LIMIT = 4 * 1024 * 1024
_PLAYBOOK_DIR_RE = re.compile(r"{{\s*playbook_dir\s*}}")
_ROLE_PATH_RE = re.compile(r"{{\s*role_path\s*}}")
_USES_PLAYBOOK_DIR_RE = re.compile(r"({{|{%)((?!}}|%}).)*(?<![.\w])playbook_dir\b", re.S)
_ENV_LOOKUP_RE = re.compile(
    r"""lookup\s*\(\s*["'](?:ansible\.builtin\.)?env["']\s*,\s*["']"""
    r"""([A-Za-z_][A-Za-z0-9_]*)["']"""
)
_CONFIG_LOOKUP_RE = re.compile(
    r"""lookup\s*\(\s*["'](?:ansible\.builtin\.)?config["']\s*,\s*["']"""
    r"""([A-Z][A-Z0-9_]*)["']"""
)


def _vault_secrets(vault_password_file=None):
    """Vault secrets from an explicit file, $ANSIBLE_VAULT_PASSWORD_FILE, or
    ansible.cfg's vault_password_file. Empty list when none is configured."""
    paths = [vault_password_file or os.environ.get("ANSIBLE_VAULT_PASSWORD_FILE")]
    if not paths[0]:
        try:
            from ansible import constants as C
            paths = list(C.DEFAULT_VAULT_PASSWORD_FILES or [])
        except Exception:
            paths = []
    from ansible.parsing.vault import VaultSecret
    secrets = []
    for p in paths:
        if not p:
            continue
        try:
            with open(os.path.expanduser(p), "rb") as fh:
                secrets.append(("default", VaultSecret(fh.read().strip())))
        except OSError:
            pass
    return secrets


def load_adjacent_group_vars(playbook_path, vault_password_file=None):
    """Merge the playbook-adjacent group_vars that ansible-playbook would apply
    with an ad-hoc inventory (only the implicit `all` group exists there)."""
    init_loader()
    from ansible.parsing.dataloader import DataLoader

    loader = DataLoader()
    secrets = _vault_secrets(vault_password_file)
    if secrets:
        loader.set_vault_secrets(secrets)
    base = os.path.join(os.path.dirname(os.path.abspath(playbook_path)), "group_vars")
    files = []
    for ext in ("", ".yml", ".yaml", ".json"):
        candidate = os.path.join(base, "all" + ext)
        if os.path.isdir(candidate):
            files.extend(sorted(
                os.path.join(candidate, name) for name in os.listdir(candidate)
                if not name.startswith(".")
                and os.path.isfile(os.path.join(candidate, name))))
        elif os.path.isfile(candidate):
            files.append(candidate)
    merged = {}
    for path in files:
        try:
            data = loader.load_from_file(path, trusted_as_template=True)
        except TypeError:  # older ansible-core without the trust kwarg
            data = loader.load_from_file(path)
        merged.update(data or {})
    return _tag_untrusted(merged)


def parse_playbook(path, vault_password_file=None, *, extra_vars=None, on_playbook_open=None):
    """Return (plays, actions) where actions is the set of module names to embed."""
    init_loader()
    from ansible.parsing.dataloader import DataLoader
    from ansible.inventory.manager import InventoryManager
    from ansible.utils.vars import combine_vars
    from ansible.vars.manager import VariableManager
    from ansible.playbook import Playbook

    loader = _make_rewriting_loader(DataLoader)
    secrets = _vault_secrets(vault_password_file)
    if secrets:
        loader.set_vault_secrets(secrets)
    inventory = InventoryManager(loader=loader, sources=[])
    vm = VariableManager(loader=loader, inventory=inventory)
    if extra_vars:
        # Ansible can share its cached CLI extra vars across variable managers.
        vm._extra_vars = combine_vars(vm.extra_vars, extra_vars)
    loader.on_playbook_open = on_playbook_open
    pb = Playbook.load(path, variable_manager=vm, loader=loader)

    pb_dir = os.path.abspath(getattr(pb, "_basedir", None)
                             or os.path.dirname(os.path.abspath(path)))
    plays = []
    actions = set()
    uuids = {}
    runtime_writes = set()
    for play in pb.get_plays():
        loader.restore_legacy_import_fields(play)
        # Like ansible-playbook (cli/playbook.py, executor/playbook_executor.py):
        # run each play with the loader's basedir set to the directory of the
        # playbook file that defined it, so role, include and src lookups are
        # independent of the compiler's working directory.
        basedir = os.path.abspath(getattr(play, "_included_path", None) or pb_dir)
        loader.set_basedir(basedir)
        node = _convert_play(play, actions, basedir, runtime_writes=runtime_writes)
        compiled_play_vars = node.pop("_gather_compile_vars", None)
        hash_scope = node.pop("_gather_hash_scope", None)
        if compiled_play_vars is not None:
            node.update(_compile_gather_timeout(play, vm, loader, compiled_play_vars, hash_scope,
                                                replay_files=bool(node.get("ordered_vars_files"))))
        scoped_options = node.pop("_gather_setup_scoped", None)
        setup_compile_vars = node.pop("_gather_setup_compile_vars", None)
        if node.get("ordered_vars_files") or scoped_options:
            setup_layers = _gather_timeout_file_scope(
                play, node.get("ordered_vars_files") or (), node.get("gather_timeout_template"),
                scoped_options=scoped_options, setup_compile_vars=setup_compile_vars)
            lookups = setup_layers.pop("_setup_controller_lookups", {})
            env_names = lookups.get("env", ())
            if env_names:
                node.setdefault("controller_env", {}).update({
                    name: os.environ[name] for name in sorted(env_names) if name in os.environ
                })
            node["controller_config"].update(_controller_config_values(lookups.get("config", ())))
            if scoped_options:
                setup_layers["gather_setup_scope"]["controller_lookups"] = {
                    "env": sorted(set(node.pop("_gather_compile_env_names", ())) | set(env_names)),
                    "config": sorted(set(node["controller_config"]) | set(lookups.get("config", ()))),
                }
            node.update(setup_layers)
        # The standalone file never maps back to ansible-core Task objects
        # (unlike the strategy plugin), so make its identifiers reproducible.
        _assign_stable_uuids(node["tasks"], node["handlers"], uuids)
        plays.append(node)
    return plays, actions


def _compile_gather_timeout(play, variable_manager, loader, compiled_play_vars, hash_scope=None, *, replay_files=False):
    """Validate the play keyword in the same controller scope as Ansible."""
    from ansible.errors import AnsibleError, AnsibleUndefinedVariable
    from jinja2 import UndefinedError

    if _core_version() >= (2, 19):
        from ansible._internal._templating._engine import TemplateEngine as Templar
    else:
        from ansible.template import Templar

    # Validate just the timeout. The other keywords belong to the compiler's
    # normal paths and can depend on inputs it resolves separately.
    timeout_play = type(play)()
    timeout_play.hosts = ["all"]
    timeout_play.gather_timeout = play.gather_timeout
    scope_play = play.copy()
    scope_play.vars = compiled_play_vars
    scope_play.vars_files = []
    if hash_scope is None:
        variables = variable_manager.get_vars(play=scope_play)
    else:
        from ansible.utils.vars import combine_vars

        variables = hash_scope
        for export in _gather_role_exports(_gather_public_roles(play)):
            variables = combine_vars(variables, export)
        variables = combine_vars(variables, variable_manager.extra_vars)
        variables = combine_vars(variables, _gather_selector_magic(play))
        variables["vars"] = dict(variables)
    runtime = _gather_timeout_template(play.gather_timeout, variables)
    if runtime is not None and (runtime.get("controller_wildcard")
                                or runtime.get("computed_vars") and not replay_files):
        roles = list(_gather_public_roles(play))
        available = compiled_play_vars.keys() | _gather_role_defaults(roles).keys()
        for export in _gather_role_exports(roles):
            available.update(export)
        if runtime.get("controller_wildcard"):
            available.update(variable_manager.extra_vars)
            runtime["controller_inputs"].update({
                key: _tag_untrusted(variables[key], template_syntax_only=True)
                for key in sorted(available & variables.keys())
            })
        if runtime.get("computed_vars") and not replay_files:
            runtime["vars"].update({key: _tag_untrusted(variables[key])
                                    for key in sorted(available & variables.keys())})
    try:
        timeout_play.post_validate(Templar(loader=loader, variables=variables))
    except AnsibleError as error:
        cause = error
        while cause is not None and not isinstance(cause, (AnsibleUndefinedVariable, UndefinedError)):
            cause = cause.__cause__ or getattr(cause, "orig_exc", None)
        if runtime is None or cause is None or "controller_inputs" in runtime:
            raise
        runtime["required"] = True
        timeout_play.gather_timeout = None
    result = {"gather_timeout": timeout_play.gather_timeout}
    if runtime is not None:
        result["gather_timeout_template"] = runtime
    return result


def _gather_timeout_template(source, variables):
    """Keep variable expressions that can render without controller calls."""
    from ansible import constants as C
    from .runtime import _GATHER_STRING_METHODS

    if not isinstance(source, str) or not _is_templated(source) or not isinstance(_tag_untrusted(source), str):
        return None
    computed = set()
    dependencies = _gather_timeout_dependencies(source, variables, _GATHER_STRING_METHODS, computed=computed)
    controller_only = dependencies is None
    paths, unknown_lookup = set(), set()
    if controller_only:
        dependencies = _gather_timeout_dependencies(source, variables, _GATHER_STRING_METHODS,
                                                    allow_controller=True, computed=computed, paths=paths,
                                                    unknown_lookup=unknown_lookup)
    if dependencies is None:
        return None
    scope, names = dependencies
    active_scope = dict(scope)
    # Runtime overrides can choose arms that were inactive at compilation.
    alternatives = _gather_timeout_dependencies(source, variables, _GATHER_STRING_METHODS,
                                                 allow_controller=True, computed=computed, retain_mutable=True)
    if alternatives is not None:
        alternative_scope, alternative_names = alternatives
        scope = {**alternative_scope, **scope}
        names |= alternative_names
    if not names and not (computed or unknown_lookup):
        return None
    runtime = {"source": source, "vars": scope, "names": sorted(names),
               "omit_default": getattr(C, "DEFAULT_GATHER_TIMEOUT", None),
               "null_representation": getattr(C, "DEFAULT_NULL_REPRESENTATION", None)}
    if controller_only:
        runtime["controller_inputs"] = active_scope
        runtime["controller_paths"] = sorted(paths, key=repr)
        if unknown_lookup:
            runtime["controller_wildcard"] = True
    if computed:
        runtime["computed_vars"] = True
    return runtime


def _gather_public_roles(play):
    from ansible import constants as C

    core_version = _core_version()
    uses_role_public = _core_version(include_patch=True) >= (2, 16, 3)
    for role in play.get_roles():
        if uses_role_public and not role.public:
            continue
        if not uses_role_public and core_version >= (2, 15) and (C.DEFAULT_PRIVATE_ROLE_VARS or not role.static):
            continue
        yield role


def _gather_role_defaults(roles):
    from ansible.utils.vars import combine_vars

    defaults = {}
    for role in roles:
        defaults = combine_vars(defaults, role.get_default_vars())
    return defaults


def _gather_role_exports(roles):
    from ansible import constants as C

    if _core_version(include_patch=True) >= (2, 16, 3) or not C.DEFAULT_PRIVATE_ROLE_VARS:
        export_args = {"only_exports": True} if _core_version() >= (2, 15) else {}
        for role in roles:
            yield role.get_vars(include_params=False, **export_args)


def _gather_selector_magic(play):
    manager = play.get_variable_manager()
    magic_args = {"include_delegate_to": True} if _core_version() < (2, 15) else {}
    return manager._get_magic_variables(play=play, host=None, task=None, include_hostvars=True, **magic_args)


def _gather_timeout_file_scope(play, entries, runtime, *, scoped_options=None, setup_compile_vars=None):
    """Keep the play layers used by early vars-file and setup option expressions."""
    from ansible import constants as C
    from ansible.utils.vars import combine_vars
    from .runtime import _GATHER_STRING_METHODS, _selector_template_sources

    roles = list(_gather_public_roles(play))
    defaults = _gather_role_defaults(roles)
    exports = list(_gather_role_exports(roles))

    play_vars = play.vars or {}
    magic = _gather_selector_magic(play)
    selector_scope = combine_vars(combine_vars(defaults, play_vars), magic)
    timeout_scope = selector_scope
    export_replacements = set()
    for export in exports:
        timeout_scope = combine_vars(timeout_scope, export)
        export_replacements.update(key for key, value in export.items()
                                   if C.DEFAULT_HASH_BEHAVIOUR != "merge" or not isinstance(value, dict))
    selector_names = set()
    computed_selector_names = set()
    timeout_names = set(runtime["names"]) if runtime is not None else set()
    setup_names = set()
    computed = set()
    setup_lookups = {"env": set(), "config": set()}
    lookup_scope = combine_vars(defaults, setup_compile_vars or play_vars)
    for layer in (*exports, play.get_variable_manager().extra_vars, magic):
        lookup_scope = combine_vars(lookup_scope, layer)
    for source, _, _ in _selector_template_sources(scoped_options or {}):
        dependencies = _gather_timeout_dependencies(
            source, timeout_scope, _GATHER_STRING_METHODS, allow_controller=True,
            computed=computed, retain_mutable=True)
        if dependencies is not None:
            setup_names.update(dependencies[1])
        _gather_timeout_dependencies(
            source, lookup_scope, _GATHER_STRING_METHODS, allow_controller=True,
            computed=computed, lookups=setup_lookups)
    file_aliases = {}
    file_names = set()
    for entry in entries:
        for candidate in entry.get("controller_candidates", {}).values():
            selector_names.update(name for name, _ in candidate["paths"])
        for source in entry.get("names", ()):
            dependencies = _gather_timeout_dependencies(
                source, selector_scope, _GATHER_STRING_METHODS, selector=True, allow_controller=True,
                computed=computed_selector_names, retain_mutable=True)
            if dependencies is not None:
                selector_names.update(dependencies[1])
        files = [entry["static_vars"]] if "static_vars" in entry else entry["files"].values()
        for contents in files:
            if not isinstance(contents, list):
                continue
            for mapping in contents:
                file_names.update(mapping)
                for key, value in mapping.items():
                    for source, _, _ in _selector_template_sources(value):
                        file_aliases.setdefault(key, set()).add(source)
    if computed_selector_names:
        selector_names.update(defaults)
    for names, scope, shadowed in (
        (selector_names, selector_scope, {}),
        (timeout_names, timeout_scope, export_replacements),
        (setup_names, timeout_scope, export_replacements),
    ):
        pending = list(names)
        while pending:
            key = pending.pop()
            sources = set() if key in shadowed else set(file_aliases.get(key, ()))
            value = scope.get(key)
            sources.update(source for source, _, _ in _selector_template_sources(_tag_untrusted(value)))
            for source in sources:
                dependencies = _gather_timeout_dependencies(
                    source, {}, _GATHER_STRING_METHODS, allow_controller=True, retain_mutable=True)
                if dependencies is not None:
                    new_names = dependencies[1] - names
                    names.update(new_names)
                    pending.extend(new_names)
    if runtime is not None:
        runtime["names"] = sorted(timeout_names)
        runtime["file_names"] = sorted(timeout_names & file_names)
        # Vars files are replayed before setup. Do not reuse a variable from
        # the compiler's default selection when the run picks a different file.
        runtime["vars"] = {key: value for key, value in runtime["vars"].items() if key not in file_names}
        runtime["play_vars"] = _tag_untrusted(play_vars)
        runtime["timeout_defaults"] = _tag_untrusted(
            {key: value for key, value in defaults.items() if runtime.get("computed_vars") or key in timeout_names})
        runtime["role_exports"] = _tag_untrusted([
            {key: value for key, value in export.items() if runtime.get("computed_vars") or key in timeout_names}
            for export in exports
        ])
    result = {
        "vars_file_defaults": _tag_untrusted({key: defaults[key] for key in sorted(selector_names & defaults.keys())}),
        "vars_file_magic": _tag_untrusted({key: magic[key] for key in sorted(selector_names & magic.keys())}),
    }
    if scoped_options:
        result["_setup_controller_lookups"] = setup_lookups
        result["gather_setup_scope"] = _tag_untrusted({
            "defaults": {key: value for key, value in defaults.items() if computed or key in setup_names},
            "role_exports": [
                {key: value for key, value in export.items() if computed or key in setup_names}
                for export in exports
            ],
            "magic": {key: value for key, value in magic.items() if computed or key in setup_names},
            "task_search_path": (play.get_search_path() if hasattr(play, "get_search_path")
                                 else [play._loader.get_basedir()]),
        })
    return result

def _gather_timeout_dependencies(source, variables, scalar_methods=(), *,
                                 selector=False, allow_controller=False, computed=None, paths=None,
                                 unknown_lookup=None, retain_mutable=False, lookups=None):
    from ansible import constants as C
    from jinja2 import Environment, TemplateSyntaxError, nodes

    from .runtime import (_is_literal_vars_lookup, _selector_input_paths, _selector_projection_cycle, _selector_scope,
                          _selector_template_sources)

    env = Environment()
    resolve_aliases = computed is not None or unknown_lookup is not None
    key_scope = {key: _tag_untrusted(value, template_syntax_only=True) if isinstance(value, str) else value
                 for key, value in variables.items()}
    key_scope["__ansiroll_controller_config"] = {"DEFAULT_JINJA2_NATIVE": getattr(C, "DEFAULT_JINJA2_NATIVE", None)}
    pending = [(source, (), None, ())]
    scope = {}
    names = set()
    seen = set()
    while pending:
        source, remaining, origin, ancestors = pending.pop()
        if _selector_projection_cycle(origin, remaining, ancestors):
            if retain_mutable:
                # Runtime checks cycles on the selected path. Keep scanning its siblings.
                continue
            return None
        ancestors = (*ancestors, (origin, remaining))
        try:
            expression = env.parse(source)
        except TemplateSyntaxError:
            if retain_mutable:
                continue
            return None
        analysis = _selector_scope(expression, key_scope, retain_mutable=retain_mutable)
        for call in analysis[2]:
            method = call.node
            if not allow_controller and not (
                isinstance(method, nodes.Getattr) and method.attr in scalar_methods
                or selector and _is_literal_vars_lookup(call)
            ):
                return None
            if (lookups is not None and isinstance(method, nodes.Name) and method.name in ("lookup", "query", "q")
                    and call.args and isinstance(call.args[0], nodes.Const) and isinstance(call.args[0].value, str)):
                plugin = call.args[0].value.rsplit(".", 1)[-1]
                if plugin in lookups:
                    lookups[plugin].update(arg.value for arg in call.args[1:]
                                           if isinstance(arg, nodes.Const) and isinstance(arg.value, str))
        for name, path in _selector_input_paths(expression, remaining, key_scope, computed, unknown_lookup,
                                                resolve_aliases=resolve_aliases, analysis=analysis,
                                                retain_mutable=retain_mutable):
            if name == "omit" or (name, path) in seen:
                continue
            if paths is not None:
                paths.add((name, path))
            seen.add((name, path))
            names.add(name)
            if name not in variables:
                continue
            value = variables[name]
            container = (selector or path or allow_controller) and isinstance(value, (dict, list, tuple))
            if not container and value is not None and not isinstance(value, (str, int, float, bool)):
                if retain_mutable:
                    continue
                return None
            scope[name] = _tag_untrusted(value, template_syntax_only=allow_controller)
            pending.extend((alias, suffix, (name, location), ancestors)
                           for alias, suffix, location in _selector_template_sources(scope[name], path))
    return scope, names


def _playbook_read_path(path):
    if _core_version() < (2, 19):
        from ansible.module_utils.common.text.converters import to_bytes

        return to_bytes(path)
    return path


def _make_rewriting_loader(DataLoader):
    """Read legacy playbook and task includes using their modern keywords."""
    import sys

    from ansible.playbook import Playbook
    from ansible.playbook.helpers import load_list_of_tasks
    from ansible.playbook.role import Role

    playbook_reader = Playbook._load_playbook_data.__code__
    role_reader = Role._load_role_yaml.__code__
    task_readers = {load_list_of_tasks.__code__, _convert_include_tasks.__code__}

    class RewritingDataLoader(DataLoader):
        def __init__(self):
            super().__init__()
            self.on_playbook_open = None
            self._deferred_import_fields = {}

        def _get_file_contents(self, file_name):
            observe = self.on_playbook_open
            if observe is None:
                return super()._get_file_contents(file_name)
            self.on_playbook_open = None

            from ansible.errors import AnsibleFileNotFound, AnsibleParserError

            # Match Ansible's filename representation and decryption on either
            # side of the loader's switch to tagged source data.
            path = _playbook_read_path(self.path_dwim(file_name))
            try:
                with open(path, "rb") as stream:
                    observe(path, os.fstat(stream.fileno()))
                    data = stream.read()
            except FileNotFoundError as ex:
                raise AnsibleFileNotFound("Unable to retrieve file contents", file_name=file_name) from ex
            except OSError as ex:
                raise AnsibleParserError("An error occurred while trying to read the file %r: %s" %
                                         (file_name, ex)) from ex
            if isinstance(path, bytes):
                return self._decrypt_if_vault_data(data, path)

            from ansible._internal._datatag._tags import Origin

            return self._decrypt_if_vault_data(Origin(path=path).tag(data))

        def load_from_file(self, *args, **kwargs):
            # Playbooks, task files, and vars files can have the same YAML shape.
            reader = sys._getframe(1).f_code
            playbook = reader is playbook_reader
            role_tasks = reader is role_reader and sys._getframe(1).f_locals.get("subdir") in ("tasks", "handlers")
            data = super(RewritingDataLoader, self).load_from_file(*args, **kwargs)
            if not (playbook or role_tasks or reader in task_readers):
                return data
            return _rewrite_includes(data, playbook=playbook, defer_import_fields=self._defer_import_fields)

        def _defer_import_fields(self, entry):
            try:
                from ansible.template import is_trusted_as_template, trust_as_template
            except ImportError:
                return
            from uuid import uuid4

            def templated(value):
                return (isinstance(value, str) and is_trusted_as_template(value)
                        and any(token in value for token in ("{{", "{%", "{#")))

            # The import label is never passed to the child play.
            if templated(entry.get("name")):
                del entry["name"]

            # Core 2.19+ validates the import before it loads the child.
            # Pass opaque values through that step and restore their original
            # trusted strings after Ansible has propagated them to the play.
            for field in ("tags", "when"):
                original = entry.get(field)
                values = original if isinstance(original, list) else [original]
                if not any(templated(value) for value in values):
                    continue
                if field == "tags" and isinstance(original, str):
                    values = [trust_as_template(tag.strip()) for tag in original.split(",")]
                deferred = []
                for value in values:
                    if templated(value):
                        marker = "__ansiroll_import_" + uuid4().hex
                        self._deferred_import_fields[marker] = value
                        deferred.append(marker)
                    else:
                        deferred.append(value)
                entry[field] = deferred

        def restore_legacy_import_fields(self, play):
            if not self._deferred_import_fields:
                return
            fields = [(play, "_tags"), (play, "_included_conditional")]
            fields.extend((node, "_when") for node in (*play.pre_tasks, *play.roles, *play.tasks, *play.post_tasks))
            for node, field in fields:
                values = getattr(node, field, None)
                if isinstance(values, list):
                    restored = [self._deferred_import_fields.get(value, value) if isinstance(value, str) else value
                                for value in values]
                    if restored != values:
                        setattr(node, field, restored)

    return RewritingDataLoader()


_TASK_LIST_KEYS = ("tasks", "pre_tasks", "post_tasks", "handlers", "block", "rescue", "always")
_PLAYBOOK_IMPORT_KEYS = ("import_playbook", "ansible.builtin.import_playbook", "ansible.legacy.import_playbook")


def _rewrite_playbook_include(item):
    from ansible.errors import AnsibleParserError
    from ansible.parsing.splitter import parse_kv, split_args

    modern = next((key for key in _PLAYBOOK_IMPORT_KEYS if key in item), None)
    if modern:
        raise AnsibleParserError(f"conflicting playbook include keys: include and {modern}", obj=item)

    include = item["include"]
    parts = split_args(include)
    filename = parts[0].strip() if parts else ""
    if not filename:
        raise AnsibleParserError("legacy playbook include must specify a filename", obj=item)

    rewritten = copy(item)
    del rewritten["include"]
    if len(parts) == 1 and filename == include:
        rewritten["import_playbook"] = include
        return rewritten

    inline = " ".join(parts[1:])
    params = parse_kv(inline)
    # Unassigned tokens overwrite even an explicit _raw_params assignment.
    if "_raw_params" in params and parse_kv(inline + " _raw_params=")["_raw_params"]:
        raise AnsibleParserError("legacy playbook include parameters must use key=value", obj=item)
    if len(parts) > 1 and "vars" in item:
        raise AnsibleParserError("conflicting inline parameters and vars on legacy playbook include", obj=item)

    # Splitting a YAML string can drop its template trust on Core 2.19+.
    try:
        from ansible.template import is_trusted_as_template, trust_as_template
    except ImportError:
        pass
    else:
        if is_trusted_as_template(include):
            filename = trust_as_template(filename)
            params = {key: trust_as_template(value) for key, value in params.items()}
    rewritten["import_playbook"] = filename
    if "tags" in params:
        inline_tags = params.pop("tags")
        # Legacy Ansible applies tag declarations in YAML key order.
        last_tag_source = next(key for key in reversed(item) if key in ("include", "tags"))
        if last_tag_source == "include":
            rewritten["tags"] = inline_tags
    if params:
        rewritten["vars"] = params
    return rewritten


def _rewrite_includes(data, *, playbook=False, defer_import_fields=None):
    # Keep Ansible's source metadata when rewriting task or playbook mappings.
    if not isinstance(data, list):
        return data
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            continue
        for k in _TASK_LIST_KEYS:
            _rewrite_includes(item.get(k))
        if "include" in item and isinstance(item.get("include"), str):
            if playbook:
                # The original mapping may also be referenced via a YAML alias.
                rewritten = _rewrite_playbook_include(item)
                if defer_import_fields is not None:
                    defer_import_fields(rewritten)
                data[index] = rewritten
                continue
            dynamic = any(k == "loop" or k.startswith("with_") or k == "loop_with" for k in item)
            new_key = "include_tasks" if dynamic else "import_tasks"
            items = [(new_key if k == "include" else k, v) for k, v in item.items()]
            item.clear()
            item.update(items)
    return data


def _convert_play(play, actions, basedir=None, only_tags=None, skip_tags=None,
                  all_vars=None, runtime_writes=None, gather_facts=None):
    from ansible import constants as C
    from .runtime import _selector_template_sources

    if basedir is None:
        try:
            basedir = play._loader.get_basedir()
        except Exception:
            basedir = os.getcwd()
    unresolved_vars_files = []
    vars_file_entries = _as_list(getattr(play, "vars_files", None))
    replay_order = any(_is_templated(filename)
                       for entry in vars_file_entries[:-1]
                       for filename in _as_list(entry))
    scoped_options = {
        key: value for key in ("fact_path", "gather_subset")
        if any(_selector_template_sources(value := _tag_untrusted(getattr(play, key, None))))
    }
    early_setup = (play.gather_facts is not False
                   and (play.gather_facts or C.DEFAULT_GATHERING != "explicit")
                   and (scoped_options or _is_templated(play.gather_timeout)))
    ordered_vars_files = [] if gather_facts is None and (replay_order or early_setup) else None
    order_deferred = {}
    hash_scope = {} if ordered_vars_files is not None and C.DEFAULT_HASH_BEHAVIOUR == "merge" else None
    play_vars = _resolved_play_vars(
        play, all_vars, basedir, unresolved_vars_files, ordered_vars_files, order_deferred, hash_scope)
    deferred = _deferred_vars_files(play, basedir, unresolved_vars_files)
    for index, (selected, checked_paths, controller_candidates) in order_deferred.items():
        deferred[index]["files"].update(selected)
        deferred[index].setdefault("checked_paths", {}).update(checked_paths)
        if controller_candidates:
            deferred[index]["controller_candidates"] = controller_candidates
        if selected:
            deferred[index]["has_compile_selection"] = True
    ordered_writes = None
    ordered_lookup_sources = []
    if ordered_vars_files and deferred:
        ordered_writes, ordered_lookup_sources = _ordered_vars_file_inputs(
            [[_tag_untrusted(play.vars or {})], *ordered_vars_files], deferred, order_deferred)
    hosts = play.hosts
    if isinstance(hosts, list):
        hosts = ",".join(str(h) for h in hosts)
    _apply_force_handlers_default(play)
    blocks = play.compile()
    handler_blocks = _handler_blocks(play)
    known_writes = set(runtime_writes or ())
    manager = play.get_variable_manager()
    # Strategy compilation has no host context. Values left by earlier plays
    # can still override these compile-time play vars on individual hosts.
    for cache_name in ("_vars_cache", "_nonpersistent_fact_cache"):
        for host_vars in getattr(manager, cache_name, {}).values():
            known_writes.update(host_vars)
    includes = _IncludeVariables(play_vars, all_vars, known_writes)
    includes.incomplete = any(index not in order_deferred or entry.get("unmodeled")
                              for index, entry in enumerate(deferred))
    includes.vars_file_writes = ordered_writes if ordered_writes is not None else {
        key for entry in deferred for data in entry["files"].values() if isinstance(data, list)
        for mapping in data for key in mapping
    }
    includes.record_writes(blocks + handler_blocks)
    while True:
        INCLUDED_TASKS.clear()
        _INCLUDE_STACK.clear()
        _INCLUDE_ROLE_PARENTS.clear()
        _INCLUDE_IDS[0] = 0
        initial_writes = includes.runtime_writes.copy()
        includes.resolved_template = False
        play_actions = set()
        tasks = []
        for block in blocks:
            node = _convert_node(block, play_actions, basedir, only_tags=only_tags,
                                 skip_tags=skip_tags, includes=includes)
            if node:
                tasks.append(node)
        handlers = _convert_handlers(handler_blocks, play_actions, basedir, includes)
        # Includes can expose additional writes, including writes that precede
        # another include on a later loop iteration. Revisit provisional paths
        # when the set grows, keeping only the final graph and task IDs.
        if not includes.resolved_template or initial_writes == includes.runtime_writes:
            break
    actions.update(play_actions)
    if runtime_writes is not None:
        runtime_writes.update(includes.runtime_writes)
    _assign_runtime_ids(tasks, handlers)
    # The strategy supplies the iterator's filtered setup decision. Standalone
    # uses the play keyword and the controller's default gathering policy.
    gather = gather_facts
    if gather is None:
        gather = play.gather_facts
        if gather is None:
            gather = C.DEFAULT_GATHERING != "explicit"
    if gather:
        actions.add("ansible.builtin.setup")  # implicit fact gathering needs the module
    core_version = _core_version()
    result = {
        "name": play.name or "",
        "hosts": hosts,
        "jinja_plugins": _collect_jinja_plugins(play, basedir),
        "gather_facts": bool(gather),
        "smart_gathering": gather_facts is None and C.DEFAULT_GATHERING == "smart",
        "gather_when": _conditions(getattr(play, "_included_conditional", None)),
        "gather_no_log": getattr(play, "no_log", False),
        "error_on_missing_handler": C.ERROR_ON_MISSING_HANDLER,
        "allow_broken_conditionals": (
            core_version >= (2, 19)
            and C.config.get_config_value("ALLOW_BROKEN_CONDITIONALS")
        ),
        "fact_schema": "%d.%d" % core_version,
        "environment": _as_list(getattr(play, "environment", None)) or None,
        "check_mode": getattr(play, "check_mode", None),
        "become": bool(play.become),
        "become_user": getattr(play, "become_user", None),
        "force_handlers": bool(getattr(play, "force_handlers", False)),
        "ignore_errors": getattr(play, "ignore_errors", None),
        "vars": _tag_untrusted(play_vars),
        "playbook_dir": basedir,
        "tasks": tasks,
        "handlers": handlers,
    }
    if deferred:
        result["deferred_vars_files"] = deferred
    if gather:
        setup_options = {
            name: value
            for name in ("gather_subset", "fact_path")
            if (value := getattr(play, name, None)) is not None
        }
        # Older cores expose their default subset on the loaded Play. Leave
        # the equivalent setup default implicit when the play did not set it.
        if setup_options.get("gather_subset") == ["all"] and "gather_subset" not in (play.get_ds() or {}):
            del setup_options["gather_subset"]
        if setup_options:
            result["gather_setup_options"] = _tag_untrusted(setup_options)
        if gather_facts is None and scoped_options:
            result["_gather_setup_scoped"] = scoped_options
            result["_gather_setup_compile_vars"] = play_vars
    # Retain authored selectors and compile-selected definitions, including
    # definitions needed during ordered replay before later files shadow them.
    lookup_sources = {
        **result,
        "deferred_vars_files": [entry["names"] for entry in deferred],
        "ordered_vars_file_inputs": ordered_lookup_sources,
    }
    lookup_sources.pop("gather_setup_options", None)
    lookup_sources.pop("_gather_setup_scoped", None)
    lookup_sources.pop("_gather_setup_compile_vars", None)
    names = _controller_env_names(lookup_sources)
    if "_gather_setup_scoped" in result:
        result["_gather_compile_env_names"] = sorted(names)
    if names:
        result["controller_env"] = {
            name: os.environ[name] for name in sorted(names) if name in os.environ
        }
    from ansible import constants as C

    config_names = _controller_config_names(lookup_sources)
    config_names.update(("ALLOW_BROKEN_CONDITIONALS", "DEFAULT_JINJA2_NATIVE",
                         "INJECT_FACTS_AS_VARS", "DEFAULT_HASH_BEHAVIOUR"))
    if core_version >= (2, 19):
        config_names.add("DISPLAY_TRACEBACK")
    controller_config = _controller_config_values(config_names)
    from ansible.plugins.loader import connection_loader

    restricted = set(C.RESTRICTED_RESULT_KEYS).union(C.COMMON_CONNECTION_VARS)
    if core_version < (2, 21):
        restricted.update(getattr(C, "INTERNAL_RESULT_KEYS", ()))
    for aliases in C.MAGIC_VARIABLE_MAPPING.values():
        restricted.update(aliases)
    controller_config["ansiroll_restricted_facts"] = sorted(restricted)
    controller_config["ansiroll_connection_fact_prefixes"] = sorted({
        "ansible_%s_" % os.path.splitext(os.path.basename(path))[0]
        for path in connection_loader.all(path_only=True)
    })
    result["controller_config"] = controller_config
    if gather_facts is None and gather and "gather_timeout" in (play.get_ds() or {}):
        result["_gather_compile_vars"] = play_vars
        result["_gather_hash_scope"] = hash_scope
    if ordered_vars_files and unresolved_vars_files:
        for entry in deferred:
            entry["glob_patterns"] = {
                name: _vars_file_glob_pattern(name) for name in entry["names"]
                if _is_templated(name) and name not in entry.get("unmodeled", ())
            }
        result["ordered_vars_files"] = [
            deferred[entry] if isinstance(entry, int) else {"static_vars": entry}
            for entry in ordered_vars_files
        ]
        result["vars_file_play_vars"] = _tag_untrusted(play.vars or {})
        result["ordered_vars_file_mutable_names"] = sorted(ordered_writes or ())
    return result


def _ordered_vars_file_inputs(ordered, deferred, compile_selections):
    from ansible.constants import DEFAULT_HASH_BEHAVIOUR
    from .runtime import _GATHER_STRING_METHODS, _selector_template_sources

    merge = DEFAULT_HASH_BEHAVIOUR == "merge"
    rewritten, selector_reads, mutable = set(), set(), set()
    sources = []

    def alias_reads(value):
        for source, _, _ in _selector_template_sources(value):
            dependencies = _gather_timeout_dependencies(
                source, {}, _GATHER_STRING_METHODS, allow_controller=True, retain_mutable=True)
            if dependencies is not None:
                yield from dependencies[1]

    for slot in reversed(ordered):
        if isinstance(slot, int):
            entry = deferred[slot]
            definitions = {}
            for contents in entry["files"].values():
                if isinstance(contents, list):
                    for mapping in contents:
                        for key, value in mapping.items():
                            definitions.setdefault(key, []).append(value)
                            if key not in rewritten:
                                mutable.add(key)
            # A launch-selected file can supply aliases used by later selectors.
            # Follow those references, but collect lookups only from eligible
            # mappings that were actually selected during compilation.
            pending = list(selector_reads & definitions.keys())
            while pending:
                key = pending.pop()
                for value in definitions[key]:
                    new_reads = set(alias_reads(value)) - selector_reads
                    selector_reads.update(new_reads)
                    pending.extend(new_reads & definitions.keys())
            mappings = next(iter(compile_selections.get(slot, ({},))[0].values()), [])
        else:
            mappings = slot
        for mapping in reversed(mappings):
            visible = {key: value for key, value in mapping.items()
                       if key in selector_reads or key not in rewritten}
            sources.append(visible)
            replaced = {key for key, value in mapping.items()
                        if not merge or not isinstance(value, dict) or value.keys() in (
                            {"__ansiroll_unsafe__"}, {"__ansiroll_vault__"})}
            if not isinstance(slot, int):
                selector_reads.difference_update(replaced)
                rewritten.update(replaced)
            for value in visible.values():
                selector_reads.update(alias_reads(value))
        if isinstance(slot, int):
            for source in entry["names"]:
                dependencies = _gather_timeout_dependencies(
                    source, {}, _GATHER_STRING_METHODS, selector=True, allow_controller=True, retain_mutable=True)
                if dependencies is not None:
                    selector_reads.update(dependencies[1])
    return mutable, sources


def _apply_force_handlers_default(play):
    """Resolve an unset ``force_handlers`` from Ansible's configuration.

    ``Play.force_handlers`` defaults to the ``--force-handlers`` CLI argument,
    whose own default is ``DEFAULT_FORCE_HANDLERS`` (ANSIBLE_FORCE_HANDLERS /
    ansible.cfg). ``ansiroll compile`` has no Ansible CLI context, so apply
    that configured default before ``play.compile()`` wraps each section in a
    block whose ``always`` flushes handlers.
    """
    try:
        if getattr(play, "force_handlers", None) is not None:
            return
        from ansible import constants as C
        play.force_handlers = bool(C.DEFAULT_FORCE_HANDLERS)
    except Exception:
        pass


def _controller_env_names(value):
    """Find literal controller-side env lookups that the target must reproduce."""
    return _controller_lookup_names(value, _ENV_LOOKUP_RE)


def _controller_config_names(value):
    """Find literal config lookups whose controller values must be preserved."""
    return _controller_lookup_names(value, _CONFIG_LOOKUP_RE)


def _controller_lookup_names(value, pattern):
    names = set()
    seen = set()
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            names.update(pattern.findall(item))
        elif isinstance(item, (dict, list, tuple)) and id(item) not in seen:
            seen.add(id(item))
            if isinstance(item, dict):
                pending.extend(item)
                pending.extend(item.values())
            else:
                pending.extend(item)
    return names


def _controller_config_values(names):
    from ansible import constants as C

    values = {}
    for name in sorted(names):
        try:
            values[name] = C.config.get_config_value(name)
        except Exception:
            # Config keys differ across core releases.
            pass
    return values


def _fact_schema():
    """Fact shape emitted by the ansible-core version doing the compilation."""
    try:
        from ansible.release import __version__
        match = re.match(r"^(\d+)\.(\d+)", __version__)
        if match:
            return "%s.%s" % match.groups()
    except Exception:
        pass
    return "2.10"


def _resolved_play_vars(play, all_vars, basedir, unresolved=None, ordered=None, order_deferred=None, hash_scope=None):
    """Merge static vars_files at their play-var precedence.

    Templated names are rendered like Ansible does, with play vars and extra vars. Names that stay unknown are
    appended to ``unresolved``.
    """
    from ansible import constants as C
    from ansible.errors import AnsibleFileNotFound
    from ansible.utils.vars import combine_vars

    out = dict(play.vars or {})
    use_search_stack = _core_version() >= (2, 12)
    compile_extra_vars = {}
    selector_magic = {}
    role_defaults = {}
    if ordered is not None:
        manager = play.get_variable_manager()
        compile_extra_vars = manager.extra_vars
        extra_replacements = {key for key, value in compile_extra_vars.items()
                              if C.DEFAULT_HASH_BEHAVIOUR != "merge" or not isinstance(value, dict)}
        role_defaults = _gather_role_defaults(_gather_public_roles(play))
        selector_magic = _gather_selector_magic(play)
    if hash_scope is not None:
        hash_scope.update(combine_vars(role_defaults, out))
    bundled_inputs = set()
    scanned = 0
    for entry in _as_list(getattr(play, "vars_files", None)):
        defer_for_order = False
        selected = {}
        checked_paths = {}
        controller_candidates = {}
        for filename in _as_list(entry):
            if not isinstance(filename, str):
                continue
            raw = filename
            filename = _PLAYBOOK_DIR_RE.sub(lambda _m: basedir, filename)
            candidate_source = filename
            controller_inputs = None
            if _is_templated(filename):
                # A deferred file may change a later selector's inputs.
                if ordered is not None:
                    from .runtime import _GATHER_STRING_METHODS

                    selector_vars = hash_scope if hash_scope is not None else {**role_defaults, **out}
                    inputs = combine_vars(combine_vars(selector_vars, compile_extra_vars), selector_magic)
                    portable = _gather_timeout_dependencies(filename, inputs, _GATHER_STRING_METHODS, selector=True)
                    requires_controller, required_plugins = _vars_filename_controller_requirements(filename, portable)
                    # A templated leading directory cannot be bundled by the
                    # filename glob. Keep its known choice unless order requires replay.
                    if portable is not None and not _vars_file_has_dynamic_directory(filename):
                        defer_for_order |= bool(portable[1] - extra_replacements - selector_magic.keys())
                    dependencies = portable
                    paths, unknown_lookup = set(), set()
                    if portable is None or requires_controller:
                        dependencies = _gather_timeout_dependencies(
                            filename, inputs, _GATHER_STRING_METHODS, selector=True, allow_controller=True,
                            paths=paths, unknown_lookup=unknown_lookup)
                        if dependencies is not None and not unknown_lookup:
                            controller_inputs = {"inputs": dependencies[0], "paths": sorted(paths, key=repr)}
                            if requires_controller:
                                controller_inputs["controller_only"] = True
                            if required_plugins:
                                controller_inputs["jinja_plugins"] = required_plugins
                    if unresolved and dependencies is not None:
                        earlier = _deferred_vars_files(play, basedir, unresolved[scanned:])
                        for index, previous in enumerate(earlier, start=scanned):
                            if order_deferred and index in order_deferred:
                                previous["files"].update(order_deferred[index][0])
                            for contents in previous["files"].values():
                                if isinstance(contents, list):
                                    for mapping in contents:
                                        bundled_inputs.update(mapping)
                        scanned = len(unresolved)
                        selector_names = dependencies[1] | (bundled_inputs if unknown_lookup else set())
                        mutable_names = selector_names - extra_replacements - selector_magic.keys()
                        affected = mutable_names & bundled_inputs
                        defer_for_order |= bool(affected)
                        if unknown_lookup and affected:
                            scope = dict(dependencies[0])
                            scope.update({key: _tag_untrusted(inputs[key], template_syntax_only=True)
                                          for key in affected if key in inputs})
                            paths.update((key, ()) for key in affected)
                            controller_inputs = {"inputs": scope, "paths": sorted(paths, key=repr),
                                                 "controller_only": requires_controller}
                            if required_plugins:
                                controller_inputs["jinja_plugins"] = required_plugins
                # Template the playbook's own string: it keeps the trust tag that a substituted copy loses.
                selector_vars = dict(hash_scope if hash_scope is not None else {**role_defaults, **out},
                                     playbook_dir=basedir)
                filename = _template_vars_file_name(play, raw, selector_vars, all_vars, selector_magic)
                if filename is None:
                    if unresolved is not None and not defer_for_order:
                        if ordered is not None:
                            ordered.append(len(unresolved))
                        unresolved.append(entry)
                    break
            if ordered is not None:
                checked_paths[filename] = None
                if controller_inputs is not None:
                    controller_candidates[candidate_source] = dict(controller_inputs, name=filename)
            try:
                # 2.10 and 2.11 load relative to the playbook directory.
                path = filename
                if use_search_stack:
                    path = play._loader.path_dwim_relative_stack(
                        play.get_search_path(), "vars", filename)
                loaded = _load_vars_file(play, path)
            except AnsibleFileNotFound:
                # Like Ansible: only a missing file moves on to the next candidate.
                continue
            mappings = [item for item in _as_list(loaded) if isinstance(item, dict)]
            for item in mappings:
                out = combine_vars(out, item)
                if hash_scope is not None:
                    hash_scope.update(combine_vars(hash_scope, item))
            if defer_for_order:
                selected_path = os.path.normpath(play._loader.path_dwim(path))
                checked_paths[filename] = selected_path
                selected[selected_path] = _tag_untrusted(mappings)
            elif ordered is not None:
                ordered.append(_tag_untrusted(mappings))
            break
        if defer_for_order:
            if order_deferred is not None:
                order_deferred[len(unresolved)] = (selected, checked_paths, controller_candidates)
            ordered.append(len(unresolved))
            unresolved.append(entry)
    # The variable manager has already applied extra vars and any parser-level
    # transformations. Restrict it to actual play/vars-file keys so magic vars
    # and unrelated inventory state are not baked into every play.
    if all_vars:
        for key in list(out):
            if key in all_vars:
                out[key] = all_vars[key]
        # These lists are synthesized by ansible-core's variable manager rather
        # than stored in play.vars. Role tasks consume them like normal magic
        # variables, so preserve the controller's authoritative values.
        for key in (
            "role_names",
            "ansible_play_role_names",
            "ansible_role_names",
            "ansible_dependent_role_names",
        ):
            if key in all_vars:
                out[key] = all_vars[key]
    return out


@lru_cache(maxsize=1)
def _vars_filename_template_environment():
    from .runtime import _Templater

    return _Templater().env


def _vars_filename_controller_requirements(source, portable):
    from jinja2 import nodes, TemplateSyntaxError
    from .runtime import _selector_template_sources

    env = _vars_filename_template_environment()
    values = portable[0].values() if portable is not None else ()
    sources = [source, *(alias for value in values for alias, _, _ in _selector_template_sources(value))]
    missing_filters = set()
    missing_tests = set()
    for candidate in sources:
        try:
            parsed = env.parse(candidate)
        except TemplateSyntaxError:
            return True, {}
        missing_filters.update(node.name for node in parsed.find_all(nodes.Filter) if node.name not in env.filters)
        missing_tests.update(node.name for node in parsed.find_all(nodes.Test) if node.name not in env.tests)
        for call in parsed.find_all(nodes.Call):
            if (isinstance(call.node, nodes.Name) and call.node.name in ("lookup", "query", "q")
                    and call.args and isinstance(call.args[0], nodes.Const)
                    and str(call.args[0].value).rsplit(".", 1)[-1] in ("file", "pipe")):
                return True, {}
    required = {kind: sorted(names) for kind, names in (("filters", missing_filters), ("tests", missing_tests)) if names}
    return bool(required), required


def _load_vars_file(play, path):
    try:
        return play._loader.load_from_file(path, trusted_as_template=True)
    except TypeError:
        return play._loader.load_from_file(path)


def _vars_file_glob_pattern(name):
    import glob

    return "*".join(glob.escape(part) for part in re.split(r"{{.*?}}|{%.*?%}", name, flags=re.S))


def _vars_file_has_dynamic_directory(name):
    return name.startswith(("{{", "{%")) and any(
        os.path.sep in literal for literal in re.split(r"{{.*?}}|{%.*?%}", name, flags=re.S))


def _finite_vars_file_names(source):
    """Return closed literal alternatives of inline conditional file names."""
    from jinja2 import Environment, TemplateSyntaxError, nodes

    def choices(node):
        if isinstance(node, nodes.TemplateData):
            return [node.data]
        if isinstance(node, nodes.Const) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, nodes.CondExpr):
            if isinstance(node.test, nodes.Const):
                return choices(node.expr1 if node.test.value else node.expr2)
            result = choices(node.expr1) + choices(node.expr2)
            if len(result) <= 128:
                return result
        if isinstance(node, (nodes.Template, nodes.Output, nodes.Concat)):
            parts = node.body if isinstance(node, nodes.Template) else node.nodes
            paths = [""]
            for part in parts:
                suffixes = choices(part)
                if len(paths) * len(suffixes) > 128:
                    break
                paths = [head + tail for head in paths for tail in suffixes]
            else:
                return paths
        raise ValueError("nonliteral vars file selector")

    try:
        return dict.fromkeys(choices(Environment(keep_trailing_newline=True).parse(source)))
    except (TemplateSyntaxError, ValueError):
        return None


def _deferred_vars_files(play, basedir, entries):
    """Bundle every file a vars_files entry left to the runtime could select, keyed by path.

    A templated name matches files by turning each template into a glob ``*``, so it cannot select a file
    in another directory level than the template's position. A name whose leading directory is a template
    can render to any directory, so its entry lists it under ``unmodeled``. A file the runtime must not load is bundled
    as an error marker that aborts the run if it is selected.
    """
    import glob
    from ansible.errors import AnsibleFileNotFound
    from .runtime import _vars_file_glob_covers

    if tuple(map(int, _fact_schema().split("."))) >= (2, 12):
        dirs, subdir = list(play.get_search_path()) + [basedir], "vars"
    else:
        dirs, subdir = [basedir], None
    dirs = [os.path.normpath(d) for d in dirs]
    deferred = []
    for entry in entries:
        names = [re.sub(r"{{\s*playbook_dir\s*}}", basedir, name) for name in _as_list(entry) if isinstance(name, str)]
        files = {}
        checked_paths = {}
        unmodeled = []
        for position, name in enumerate(names):
            if _vars_file_has_dynamic_directory(name):
                unmodeled.append(name)
                continue
            if not _is_templated(name):
                try:
                    path = (play._loader.path_dwim_relative_stack(play.get_search_path(), subdir, name)
                            if subdir else play._loader.path_dwim(name))
                except AnsibleFileNotFound:
                    continue
                if not os.path.exists(path):
                    continue
                path = os.path.normpath(path)
                checked_paths[name] = path
                if path not in files:
                    files[path] = _bundled_vars_file(play, path)
                # A literal match succeeds or raises a read error before
                # Ansible can consider any remaining first-found candidates.
                names = names[:position + 1]
                break
            pattern = _vars_file_glob_pattern(os.path.expanduser(name))
            finite_names = _finite_vars_file_names(name) if _is_templated(name) else None
            variants = ([(expanded, glob.escape(expanded)) for candidate in finite_names
                         if os.pardir not in candidate.split(os.path.sep)
                         and _vars_file_glob_covers(pattern, expanded := os.path.expanduser(candidate))]
                        if finite_names else [(name, pattern)])
            patterns = []
            for candidate, variant in variants:
                if os.path.isabs(os.path.expanduser(candidate)):
                    patterns.append(variant)
                    continue
                for d in dirs:
                    if subdir and (_core_version() < (2, 21) or candidate.split("/")[0] != subdir):
                        patterns.append(os.path.join(glob.escape(d), subdir, variant))
                    patterns.append(os.path.join(glob.escape(d), variant))
            for path in patterns:
                for found in glob.glob(path):
                    found = os.path.normpath(found)
                    if found not in files and (os.path.isfile(found) or os.path.isdir(found)):
                        files[found] = _bundled_vars_file(play, found)
        entry = {"names": names, "dirs": dirs, "subdir": subdir, "files": files}
        if checked_paths:
            entry["checked_paths"] = checked_paths
        if any(name.startswith("~/") for name in names):
            entry["controller_home"] = os.path.expanduser("~")
        if unmodeled:
            entry["unmodeled"] = unmodeled
        deferred.append(entry)
    return deferred


def _bundled_vars_file(play, path):
    """A candidate's vars, or an error marker: the glob also matches playbooks, task files and secrets."""
    from ansible.errors import AnsibleParserError

    if not os.path.isfile(path):
        return {"__ansiroll_vars_file_error__": "Invalid vars_files file %r: expected a regular file" % path, "rc": 1}
    try:
        loaded = _load_plain_ansible_source(play._loader.path_dwim(path))
        if isinstance(loaded, dict) or loaded is None:
            return _tag_untrusted([loaded] if loaded else [])
        from ansible.vars.manager import preprocess_vars
        return _tag_untrusted(preprocess_vars(loaded))
    except _UnbundledVaultFile:
        return {"__ansiroll_vars_file_error__": "ansiroll: vars file %r holds vault-encrypted data, so it was not "
                "bundled for a name only known at run time; make the vars_files name resolvable at compile time"
                % path}
    except Exception as ex:
        return {"__ansiroll_vars_file_error__": "Invalid vars_files file %r: %s" % (path, ex),
                "rc": 4 if isinstance(ex, AnsibleParserError) else 1}


class _UnbundledVaultFile(ValueError):
    pass


def _load_plain_ansible_source(path):
    """Parse one validated snapshot of a source that must be unencrypted."""
    from ansible.module_utils.common.text.converters import to_text
    from ansible.parsing.dataloader import DataLoader

    with open(path, "rb", opener=lambda name, _: _open_role_source(name)) as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise ValueError("expected a regular file")
        data = to_text(fh.read(), errors="surrogate_or_strict")
    if _ansible_source_contains_vault(data):
        raise _UnbundledVaultFile(path)
    try:
        from ansible._internal._datatag._tags import TrustedAsTemplate
    except ImportError:
        pass
    else:
        data = TrustedAsTemplate().tag(data)
    return DataLoader().load(data, file_name=path, show_content=False)


def _ansible_source_contains_vault(data):
    if "$ANSIBLE_VAULT" in data:
        return True
    if data.lstrip().startswith(("{", "[")):
        import json
        try:
            from ansible._internal._json._profiles._legacy import Decoder
            from ansible.parsing.vault import EncryptedString
        except ImportError:
            from ansible.parsing.ajson import AnsibleJSONDecoder as Decoder
            from ansible.parsing.yaml.objects import AnsibleVaultEncryptedUnicode as EncryptedString
        try:
            decoded = json.loads(data, cls=Decoder)
        except Exception:
            # Ansible retries the whole document as YAML if JSON decoding fails.
            pass
        else:
            pending = [decoded]
            while pending:
                value = pending.pop()
                if isinstance(value, EncryptedString):
                    return True
                if isinstance(value, dict):
                    pending.extend(value.values())
                elif isinstance(value, list):
                    pending.extend(value)
            return False
    if "!" not in data:
        return False
    import yaml

    try:
        # Inspect tags before constructing values. A Vault value used as a
        # mapping key may be decrypted by the YAML constructor itself.
        return any(
            event.tag in ("!vault", "!vault-encrypted")
            for event in yaml.parse(data, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
            if isinstance(event, yaml.ScalarEvent)
        )
    except (yaml.YAMLError, UnicodeError, RecursionError):
        # The Ansible loader supplies the diagnostic for malformed input.
        return False


def _template_vars_file_name(play, filename, play_vars, all_vars, magic_vars):
    from ansible.utils.vars import combine_vars

    variables = combine_vars(play_vars, all_vars or {})
    variables = combine_vars(variables, play.get_variable_manager().extra_vars)
    variables = combine_vars(variables, magic_vars)
    try:
        from ansible.template import Templar
        try:
            templar = Templar(loader=play._loader, variables=variables)
        except TypeError:
            templar = Templar(loader=play._loader)
            templar.available_variables = variables
        rendered = templar.template(filename, fail_on_undefined=True)
    except Exception:
        return None
    if not isinstance(rendered, str) or _is_templated(rendered):
        return None
    return rendered


def _tag_untrusted(value, template_syntax_only=False):
    """Mark strings that are explicitly not trusted (for example `!unsafe`)."""
    try:
        from ansible._internal._datatag._tags import TrustedAsTemplate
    except ImportError:
        def untrusted(v):
            return getattr(v, "__UNSAFE__", False)
    else:
        def untrusted(v):
            return not TrustedAsTemplate.is_tagged_on(v)

    def walk(v):
        if isinstance(v, str) and untrusted(v):
            if not template_syntax_only or "{{" in v or "{%" in v:
                return {"__ansiroll_unsafe__": str(v)}
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, tuple):
            return tuple(walk(x) for x in v)
        return v

    return walk(value)


def _conditions(value):
    """Lower conditions while preserving Ansible's template-trust tag."""
    values = _as_list(value)
    try:
        from ansible._internal._datatag._tags import TrustedAsTemplate
    except ImportError:
        # Before data tags, Ansible used AnsibleUnsafeText.__UNSAFE__ for
        # explicit !unsafe values and treated ordinary parser strings as
        # trusted template input. Normalize that legacy representation into
        # the same wire markers used by newer cores.
        lowered = []
        for item in values:
            if isinstance(item, str) and getattr(item, "__UNSAFE__", False):
                lowered.append({"__ansiroll_untrusted_condition__": str(item)})
            elif isinstance(item, str) and ("{{" in item or "{%" in item):
                lowered.append({"__ansiroll_trusted_condition__": str(item)})
            else:
                lowered.append(item)
        return lowered
    lowered = []
    for item in values:
        if isinstance(item, str) and not TrustedAsTemplate.is_tagged_on(item):
            lowered.append({"__ansiroll_untrusted_condition__": str(item)})
        elif isinstance(item, str) and ("{{" in item or "{%" in item):
            lowered.append({"__ansiroll_trusted_condition__": str(item)})
        else:
            lowered.append(item)
    return lowered


def _collect_jinja_plugins(play, basedir):
    """Bake playbook- and role-adjacent Jinja plugin sources (filter_plugins/,
    test_plugins/, lookup_plugins/) so the compiled runtime can load them."""
    bases = [basedir]
    for role in (getattr(play, "roles", None) or []):
        path = getattr(role, "_role_path", None)
        if path:
            bases.append(path)
    plugins = []
    for base in dict.fromkeys(bases):
        for kind, sub in (("filter", "filter_plugins"), ("test", "test_plugins"),
                          ("lookup", "lookup_plugins")):
            d = os.path.join(base, sub)
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if not fn.endswith(".py") or fn.startswith("__"):
                    continue
                try:
                    with open(os.path.join(d, fn), "r", encoding="utf-8") as fh:
                        plugins.append({"kind": kind, "name": fn[:-3],
                                        "source": fh.read()})
                except OSError:
                    continue
    return plugins or None


def _assign_runtime_ids(tasks, handlers):
    counter = [0]

    def walk(nodes):
        for node in nodes:
            if node.get("type") == "block":
                walk(node.get("block", []))
                walk(node.get("rescue", []))
                walk(node.get("always", []))
            else:
                node["runtime_id"] = "task-%d" % counter[0]
                counter[0] += 1
                if node.get("type") == "include":
                    walk(node.get("tasks", []))
                    walk(node.get("handlers", []))

    walk(tasks)
    walk(handlers)


def _assign_stable_uuids(tasks, handlers, mapping=None):
    """Replace ansible-core's random task/role UUIDs with deterministic ones.

    Only equality between the identifiers matters at runtime (output grouping,
    handler de-duplication already happened), so the first-seen order keeps
    every relation while making repeated compiles byte-identical."""
    if mapping is None:
        mapping = {}

    def stable(value):
        if value is None:
            return None
        if value not in mapping:
            mapping[value] = "00000000-0000-4000-8000-%012x" % len(mapping)
        return mapping[value]

    def walk(nodes):
        for node in nodes:
            if "uuid" in node:
                node["uuid"] = stable(node["uuid"])
            magic = node.get("role_magic")
            if isinstance(magic, dict) and "role_uuid" in magic:
                magic["role_uuid"] = stable(magic["role_uuid"])
            name_vars = node.get("handler_name_vars")
            if isinstance(name_vars, dict) and "role_uuid" in name_vars:
                role_uuid = name_vars["role_uuid"]
                if isinstance(role_uuid, dict) and "__ansiroll_unsafe__" in role_uuid:
                    role_uuid["__ansiroll_unsafe__"] = stable(role_uuid["__ansiroll_unsafe__"])
                else:
                    name_vars["role_uuid"] = stable(role_uuid)
            for key in ("block", "rescue", "always", "tasks", "handlers"):
                if node.get(key):
                    walk(node[key])

    walk(tasks)
    walk(handlers)


def _convert_handlers(blocks, actions, basedir, includes):
    seen = set()
    handlers = []
    for block_index, block in enumerate(blocks):
        for handler in block.block or []:
            # Ignore tag selection for handlers, which run when notified.
            node = _convert_node(handler, actions, basedir, includes=includes)
            if node and node.get("uuid") not in seen:
                seen.add(node.get("uuid"))
                node["handler_block"] = block_index
                if node.get("type") == "include":
                    node.update(_handler_match(handler))
                handlers.append(node)
    return handlers


def _handler_blocks(play):
    blocks = list(play.handlers or [])
    compile_roles = getattr(play, "compile_roles_handlers", None)
    if compile_roles:
        try:
            blocks = list(compile_roles()) + blocks
        except Exception:
            pass
    return blocks


def _convert_node(node, actions, basedir,
                  only_tags=None, skip_tags=None, includes=None):
    from ansible.playbook.block import Block
    from ansible.playbook.task import Task

    if isinstance(node, Block):
        block_node = {
            "type": "block",
            "when": _conditions(node.when),
            "become": bool(node.become),
            "vars": _tag_untrusted(dict(node.vars or {})),
            "block": [c for c in (_convert_node(s, actions, basedir, only_tags=only_tags,
                                                skip_tags=skip_tags, includes=includes)
                                    for s in (node.block or [])) if c],
            "rescue": [c for c in (_convert_node(s, actions, basedir, only_tags=only_tags,
                                                 skip_tags=skip_tags, includes=includes)
                                     for s in (node.rescue or [])) if c],
            "always": [c for c in (_convert_node(s, actions, basedir, only_tags=only_tags,
                                                 skip_tags=skip_tags, includes=includes)
                                     for s in (node.always or [])) if c],
        }
        if not block_node["block"] and not block_node["rescue"] and not block_node["always"]:
            return None
        return block_node

    if isinstance(node, Task):
        if not _matches_tags(node, only_tags, skip_tags, includes.all_vars):
            return None
        if node.action in ("include_tasks", "ansible.builtin.include_tasks"):
            inc = _convert_include_tasks(node, actions, basedir, only_tags,
                                         skip_tags, includes)
            if inc is not None:
                return inc
        if node.action in ("include_role", "ansible.builtin.include_role"):
            inc = _convert_include_role(node, actions, basedir, only_tags,
                                        skip_tags, includes)
            if inc is not None:
                return inc
        return _convert_task(node, actions, basedir)
    return None


# Task objects created while expanding include_tasks, keyed by uuid; the
# strategy needs them to map result frames back to displayable tasks.
INCLUDED_TASKS = {}
# Includes currently being expanded, keyed by what makes their expansion
# identical; the value is the include_id a recursive include refers back to.
_INCLUDE_STACK = {}
# id(top-level role block) -> include_role task. IncludeRole.get_block_list
# parents the role's blocks to the include_role task. Keep the equivalent
# link for compile-time include paths and variables.
_INCLUDE_ROLE_PARENTS = {}
_INCLUDE_IDS = [0]


def _is_templated(value):
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def _loop_binding_names(loop_var, index_var, extended):
    names = {loop_var or "item", "ansible_loop_var"}
    if index_var:
        names.update((index_var, "ansible_index_var"))
    if extended:
        names.add("ansible_loop")
    return names


def _runtime_write_names(action, args, register):
    """Return variable writes, using None for a write with unknown names."""
    names = set(register if isinstance(register, dict) else [register]) if register else set()
    action = str(action or "").rsplit(".", 1)[-1]
    if action == "set_fact":
        names.update(None if _is_templated(key) else key
                     for key in args if key != "cacheable")
    elif action == "include_vars":
        name = args.get("name")
        names.add(name if isinstance(name, str) and name and not _is_templated(name) else None)
    return names


def _can_write_var(writes, name):
    return name in writes or None in writes


def _depends_on_vars(value, variables, matches):
    """Follow Jinja variable references through known values."""
    from jinja2 import Environment, TemplateSyntaxError, nodes

    env = Environment()
    seen = set()

    def walk(value):
        if isinstance(value, dict):
            if set(value) in ({"__ansiroll_unsafe__"}, {"__ansiroll_vault__"}):
                return False
            return any(walk(item) for pair in value.items() for item in pair)
        if isinstance(value, (list, tuple)):
            return any(walk(item) for item in value)
        if not _is_templated(value):
            return False
        try:
            parsed = env.parse(value)
        except TemplateSyntaxError:
            return False
        for call in parsed.find_all(nodes.Call):
            if (not isinstance(call.node, nodes.Name)
                    or call.node.name not in ("lookup", "query", "q")):
                continue
            if not call.args or not isinstance(call.args[0], nodes.Const):
                return True
            if call.args[0].value not in ("vars", "ansible.builtin.vars", "ansible.legacy.vars"):
                continue
            if call.dyn_args is not None:
                return True
            for arg in call.args[1:]:
                if not isinstance(arg, nodes.Const) or not isinstance(arg.value, str):
                    return True
                if depends_on_name(arg.value):
                    return True
        return any(depends_on_name(node.name) for node in parsed.find_all(nodes.Name)
                   if node.ctx == "load")

    def depends_on_name(name):
        if name in seen:
            return False
        seen.add(name)
        return matches(name) or (name in variables and walk(variables[name]))

    return walk(value)


class _IncludeVariables:
    def __init__(self, play_vars, all_vars, runtime_writes):
        self.play_vars = play_vars
        self.all_vars = all_vars
        self.runtime_writes = runtime_writes
        self.resolved_template = False
        # Replay of a known vars file can only redefine keys found in its candidates.
        self.vars_file_writes = set()
        # When candidate contents are unknown, any play var can change.
        self.incomplete = False

    def record_writes(self, blocks):
        from ansible.playbook.block import Block
        from ansible.playbook.task import Task

        for node in blocks or ():
            if isinstance(node, Block):
                self.record_writes(node.block)
                self.record_writes(node.rescue)
                self.record_writes(node.always)
            elif isinstance(node, Task):
                self.runtime_writes.update(
                    _runtime_write_names(node.action, node.args or {}, node.register)
                )


def _include_ref(key):
    """Return the include_id of an in-progress expansion of the same content."""
    return _INCLUDE_STACK.get(key)


def _new_include_id():
    _INCLUDE_IDS[0] += 1
    return "include-%d" % _INCLUDE_IDS[0]


def _convert_include_tasks(task, actions, basedir, only_tags, skip_tags, includes):
    """Expand a compile-time-resolvable include_tasks.

    Filenames depending on runtime values (such as a loop item or a registered
    result) remain on the controller-coupled path.
    """
    args = task.args or {}
    raw_fname = fname = args.get("_raw_params") or args.get("file")
    if not isinstance(fname, str):
        return None
    if "{{" in fname or "{%" in fname:
        fname = _template_include_filename(task, fname, includes)
        if not isinstance(fname, str) or "{{" in fname or "{%" in fname:
            return None
    path = _include_tasks_path(task, fname, includes)
    if path is None or not os.path.exists(path):
        return None
    role = getattr(task, "_role", None)
    physical_path = os.path.realpath(path)
    scope = _include_scope_key(task)
    stable_template = None
    if raw_fname != fname and (getattr(task, "_ansiroll_stable_include", False)
                              or _is_literal_include_template(raw_fname)):
        stable_template = raw_fname
    key = ("tasks", physical_path, _include_path_spelling(path),
           id(role) if role is not None else None, _is_handler(task),
           _include_search_context(task, stable_template, fname), scope)
    ref = _include_ref(key)
    if ref is None and _too_many_include_contexts(key):
        return _unresolved_recursive_include(task, actions, basedir)
    include_id = None
    tasks = []
    if ref is None:
        try:
            from ansible.playbook.helpers import load_list_of_blocks
            try:
                data = task._loader.load_from_file(path, trusted_as_template=True)
            except TypeError:  # older ansible-core without the trust kwarg
                data = task._loader.load_from_file(path)
            handler = _is_handler(task)
            parent = task
            if handler and args.get("apply"):
                include = task.copy()
                include.args = dict(args, apply=dict(args["apply"]))
                parent = include.build_parent_block()
            blocks = load_list_of_blocks(
                data, play=_play_of(task), task_include=parent, role=task._role,
                use_handlers=handler, variable_manager=task.get_variable_manager(),
                loader=task._loader)
            if parent is not task:
                for block in blocks:
                    block.vars = dict(parent.vars, **(block.vars or {}))
        except Exception:
            return None
        includes.record_writes(blocks)
        nested_includes = _register_included(blocks)
        if raw_fname != fname and all(
            ((nested.args or {}).get("_raw_params") or (nested.args or {}).get("file")) == raw_fname
            and _include_scope_key(nested) == scope
            for nested in nested_includes
        ):
            # This expansion searches for the same template in the same scope
            # at every include site. A different child could rebind that
            # template and make even repeated ancestor paths significant.
            for nested in nested_includes:
                nested._ansiroll_stable_include = True
        include_id = _new_include_id()
        _INCLUDE_STACK[key] = include_id
        try:
            tasks = [c for c in (_convert_node(b, actions, basedir, only_tags=only_tags,
                                               skip_tags=skip_tags, includes=includes)
                                 for b in blocks) if c]
        finally:
            _INCLUDE_STACK.pop(key, None)
    role_defaults, role_vars = _role_vars(task)
    node = _include_node(task, "include_tasks", fname, basedir,
                         role_defaults, role_vars, tasks, include_id, ref)
    return node


def _include_node(task, action, fname, basedir, role_defaults, role_vars,
                  tasks, include_id, ref, handlers=None):
    node = {
        "type": "include",
        "uuid": task._uuid,
        "name": task.name or action,
        "action": action,
        "register": _register_of(task),
        **_ignore_errors_keywords(task),
        "no_log": bool(getattr(task, "no_log", False)),
        "file": fname,
        "when": _conditions(task.when),
        "loop": _tag_loop_values(task, _loop_of(task, basedir)),
        "loop_var": _loop_var(task),
        "index_var": _index_var(task),
        **_loop_control(task),
        "vars": _tag_untrusted(dict(task.vars or {})),
        "role_defaults": _tag_untrusted(role_defaults),
        "role_vars": _tag_untrusted(role_vars),
        "role_params": _tag_untrusted(_role_params(task)),
        "become": bool(task.become),
        "tasks": tasks,
    }
    if action == "include_role":
        node["handlers"] = handlers or []
        # The runtime overlay already has the enclosing invocations' evaluated
        # parameters and live loop bindings. Preserve this role's own params.
        node["include_params"] = _tag_untrusted(task.copy(exclude_parent=True).get_include_params())
        if _core_version() < (2, 17):
            # Older linear strategies do not count successful role includes in ok.
            node["tally_include_ok"] = False
    if include_id is not None:
        node["include_id"] = include_id
    if ref is not None:
        # A recursive include (terminated at runtime, e.g. by `when`): reuse
        # the expansion of the enclosing include instead of unrolling forever.
        node["tasks_ref"] = ref
    return node


def _is_handler(task):
    try:
        from ansible.playbook.handler import Handler
    except ImportError:
        return False
    return isinstance(task, Handler)


def _include_parent(obj):
    parent = getattr(obj, "_parent", None)
    if parent is None:
        parent = _INCLUDE_ROLE_PARENTS.get(id(obj))
    return parent


def _parent_include_paths(obj):
    """Yield enclosing include directories, or raw filenames to template."""
    from ansible.playbook.role_include import IncludeRole
    from ansible.playbook.task_include import TaskInclude

    while obj is not None:
        if isinstance(obj, IncludeRole):
            yield getattr(obj, "_role_path", None) or "", False
        elif isinstance(obj, TaskInclude):
            raw = (obj.args or {}).get("_raw_params") or (obj.args or {}).get("file")
            if isinstance(raw, str) and ("{{" in raw or "{%" in raw):
                yield raw, True
            else:
                yield os.path.dirname(raw) if isinstance(raw, str) else "", False
        obj = _include_parent(obj)


def _next_include_dir(parent_dir, cumulative_path):
    if cumulative_path is not None and not os.path.isabs(cumulative_path):
        return os.path.join(parent_dir, cumulative_path)
    return parent_dir


def _include_path_spelling(path):
    # Keep aliases and parent traversals, both of which can affect resolution.
    root = "//" if path.startswith("//") and not path.startswith("///") else ""
    if not root and os.path.isabs(path):
        root = "/"
    return root + "/".join(part for part in path.split("/") if part not in ("", "."))


def _include_search_context(task, stable_template=None, fname=None):
    # Descendants search using this include's argument followed by its ancestors'
    # arguments. Preserve both the relative prefix (which a child can extend)
    # and the ordered paths after the first absolute reset.
    paths = [(path if dynamic else _include_path_spelling(path), dynamic)
             for path, dynamic in _parent_include_paths(task)]
    if any(dynamic for _, dynamic in paths):
        # A descendant can override variables in an ancestor's filename.
        # Keep the templates distinct unless the recursive expansion can
        # establish that the filename binding stays the same.
        context = []
        for entry in paths:
            path, dynamic = entry
            directory = path
            if dynamic:
                directory = _include_path_spelling(os.path.dirname(fname)) if path == stable_template else None
            if directory == "" and context[-1:] == [entry]:
                continue
            # An absolute parent resets the cumulative path. The first
            # occurrence can still consume a preceding relative suffix.
            if directory and os.path.isabs(directory) and context[-2:] == [entry, entry]:
                continue
            context.append(entry)
        return tuple(context)

    relative, remaining = {}, {}
    cumulative_path = None
    candidates = relative
    for path, _ in paths:
        cumulative_path = _next_include_dir(path, cumulative_path)
        candidates[_include_path_spelling(cumulative_path)] = None
        if os.path.isabs(path):
            candidates = remaining
    return tuple(relative), tuple(remaining)


def _include_scope(task):
    from ansible.playbook.task_include import TaskInclude

    parents = []
    while task is not None:
        parents.append(task)
        task = _include_parent(task)
    scope, params = {}, {}
    for parent in reversed(parents):
        variables = parent.vars or {}
        scope.update(variables)
        if isinstance(parent, TaskInclude):
            params.update(variables)
    return scope, params


def _include_scope_key(task):
    return tuple(repr(sorted(scope.items())) for scope in _include_scope(task))


def _too_many_include_contexts(key):
    # Relative ancestry can grow on every recursive call. If it never repeats,
    # stop expansion without substituting a task graph from a different context.
    return sum(active[:2] == key[:2] for active in _INCLUDE_STACK) >= 32


def _unresolved_recursive_include(task, actions, basedir):
    node = _convert_task(task, actions, basedir)
    node["fallback_reason"] = "recursive include search context did not stabilize"
    return node


def _include_tasks_path(task, include_target, includes):
    """Resolve an include_tasks file like IncludedFile.process_include_results
    (ansible/playbook/included_file.py) does for the controller."""
    loader = task._loader
    role = getattr(task, "_role", None)
    dirname = "handlers" if _is_handler(task) else "tasks"
    include_file = None
    cumulative_path = None
    for parent_path, dynamic in _parent_include_paths(_include_parent(task)):
        if dynamic:
            rendered = _template_include_filename(task, parent_path, includes)
            parent_path = os.path.dirname(rendered) if isinstance(rendered, str) else ""
        cumulative_path = _next_include_dir(parent_path, cumulative_path)
        if role is not None:
            new_basedir = os.path.join(role._role_path, dirname, cumulative_path)
            candidates = [
                loader.path_dwim_relative(role._role_path, dirname, include_target,
                                          is_role=True),
                loader.path_dwim_relative(new_basedir, dirname, include_target,
                                          is_role=True),
            ]
            for include_file in candidates:
                if os.path.exists(include_file):
                    break
        else:
            include_file = loader.path_dwim_relative(
                loader.get_basedir(), cumulative_path, include_target)
        if os.path.exists(include_file):
            break
    if include_file is None:
        if role is not None:
            include_file = loader.path_dwim_relative(
                role._role_path, dirname, include_target, is_role=True)
        else:
            include_file = loader.path_dwim(include_target)
    return include_file


_MAGIC_VAR_NAMES = frozenset((
    "role_path", "role_name", "playbook_dir", "inventory_dir", "inventory_file", "inventory_hostname",
    "groups", "hostvars", "ansible_play_hosts", "ansible_play_batch",
))


def _template_include_filename(task, value, includes):
    """Resolve an include filename using Ansible's own templar and precedence."""
    from ansible.constants import DEFAULT_HASH_BEHAVIOUR
    from ansible.utils.vars import combine_vars

    from ansible.vars.reserved import is_reserved_name
    from .runtime import _clear_vars_file_blocks, _vars_file_reads_blocked

    role_defaults, role_vars = _role_vars(task)
    extra_vars = task.get_variable_manager().extra_vars
    task_vars, include_params = _include_scope(task)
    role_params = _role_params(task)
    variables = {}
    for layer in (role_defaults, includes.play_vars, includes.all_vars or {}, role_vars,
                  task_vars, role_params, include_params, extra_vars):
        variables = combine_vars(variables, layer)
    # Higher mappings can inherit keys from vars_files when hashes are merged.
    higher_layers = role_vars, task_vars, role_params, include_params, extra_vars
    higher_names = set().union(*higher_layers)
    replayed_names = higher_names if includes.incomplete else higher_names & includes.vars_file_writes
    vars_file_inputs = dict.fromkeys(replayed_names, frozenset())
    for layer in higher_layers:
        _clear_vars_file_blocks(vars_file_inputs, layer, DEFAULT_HASH_BEHAVIOUR == "merge")
    include_paths = None

    def vars_file_can_change(name):
        nonlocal include_paths
        if name not in replayed_names:
            return True
        if name not in vars_file_inputs:
            return False
        if include_paths is None:
            paths, unknown_lookup = set(), set()
            dependencies = _gather_timeout_dependencies(
                value, variables, selector=True, allow_controller=True,
                paths=paths, unknown_lookup=unknown_lookup, retain_mutable=True)
            include_paths = paths if dependencies is not None and not unknown_lookup else ()
        reads = [path for key, path in include_paths if key == name]
        for path in reads or [()]:
            if None in path:
                projection = path[:path.index(None)]
                for layer in higher_layers:
                    item = layer.get(name)
                    for member in projection:
                        item = item.get(member) if isinstance(item, dict) else None
                    if isinstance(item, dict) and (path[-1] == "defined" or item):
                        # A higher mapping guarantees presence, and a nonempty
                        # one stays truthy even when it inherits other keys.
                        break
                else:
                    if _vars_file_reads_blocked(vars_file_inputs, [(name, projection)]):
                        return True
            elif _vars_file_reads_blocked(vars_file_inputs, [(name, path)]):
                return True
        return False

    loop_names = set()
    parent = task
    while parent is not None:
        if getattr(parent, "loop", None) is not None:
            loop_names.update(_loop_binding_names(
                _loop_var(parent), _index_var(parent),
                getattr(getattr(parent, "loop_control", None), "extended", False),
            ))
        parent = _include_parent(parent)

    def is_runtime(name):
        if name in loop_names:
            return True
        if ((includes.incomplete or name in includes.vars_file_writes)
                and name not in _MAGIC_VAR_NAMES and not is_reserved_name(name)
                and vars_file_can_change(name)):
            return True
        if not _can_write_var(includes.runtime_writes, name):
            return False
        # A scalar extra var replaces any runtime value. A mapping under
        # hash_behaviour=merge can still inherit keys from a runtime write.
        return name not in extra_vars or (
            DEFAULT_HASH_BEHAVIOUR == "merge" and isinstance(extra_vars[name], dict)
        )

    if _depends_on_vars(value, variables, is_runtime):
        return None
    try:
        from ansible.template import Templar
        try:
            templar = Templar(loader=task._loader, variables=variables)
        except TypeError:
            templar = Templar(loader=task._loader)
            templar.available_variables = variables
        resolved = templar.template(value, fail_on_undefined=True)
    except Exception:
        return None
    if isinstance(resolved, str) and not _is_templated(resolved):
        includes.resolved_template = True
    return resolved


def _is_literal_include_template(value):
    from jinja2 import Environment, nodes, TemplateSyntaxError

    try:
        body = Environment().parse(value).body
    except TemplateSyntaxError:
        return False
    return all(isinstance(output, nodes.Output) and all(
        isinstance(part, (nodes.TemplateData, nodes.Const)) for part in output.nodes
    ) for output in body)


def _convert_include_role(task, actions, basedir, only_tags, skip_tags, includes):
    """Expand a static-name include_role at compile time."""
    args = task.args or {}
    name = args.get("name") or args.get("_raw_params")
    if not isinstance(name, str) or "{{" in name:
        return None
    from_files = {}
    for opt in ("tasks", "defaults", "vars", "handlers"):
        v = args.get(opt + "_from")
        if v:
            from_files[opt] = v
    try:
        from ansible.playbook.role.include import RoleInclude
        from ansible.playbook.role import Role
        play = _play_of(task)
        parent_role = getattr(task, "_role", None)
        # Resolve the role exactly like IncludeRole.get_block_list
        # (ansible/playbook/role_include.py): no current_role_path, so the
        # search is collections, <playbook dir>/roles, roles_path, playbook dir.
        ri = RoleInclude.load(
            name, play=play,
            variable_manager=task.get_variable_manager(), loader=task._loader,
            collection_list=getattr(task, "collections", None))
    except Exception:
        return None
    task._role_path = getattr(ri, "_role_path", None)
    key = ("role", task._role_path or name, tuple(sorted(from_files.items())),
           _is_handler(task), _include_search_context(task), _include_scope_key(task))
    ref = _include_ref(key)
    if ref is None and _too_many_include_contexts(key):
        return _unresolved_recursive_include(task, actions, basedir)
    include_id = None
    tasks = []
    handlers = []
    if ref is None:
        try:
            role = Role.load(ri, play, parent_role=parent_role,
                             from_files=from_files or None, from_include=True)
            if _core_version() >= (2, 15):
                dep_chain = role.get_dep_chain()
                handler_dep_chain = dep_chain
            else:
                dep_chain = list(parent_role.get_parents()) + [parent_role] if parent_role else []
                # Through 2.14, IncludeRole passes the chain to tasks only.
                handler_dep_chain = None
            blocks = role.compile(play, dep_chain=dep_chain)
            handler_blocks = role.get_handler_blocks(play, dep_chain=handler_dep_chain) or []
            if handler_blocks and args.get("apply"):
                include = task.copy()
                include.args = dict(args, apply=dict(args["apply"]))
                parent = include.build_parent_block()
                for block in handler_blocks:
                    block._parent = parent
        except Exception:
            return None
        includes.record_writes(blocks + handler_blocks)
        _register_included(blocks)
        _register_included(handler_blocks)
        task._role_path = role._role_path
        role_roots = [id(block) for block in blocks + handler_blocks
                      if getattr(block, "_parent", None) is None]
        if handler_blocks and args.get("apply"):
            role_roots.append(id(parent))
        _INCLUDE_ROLE_PARENTS.update(dict.fromkeys(role_roots, task))
        include_id = _new_include_id()
        # Keep the guard while converting the role's tasks: that is where a
        # recursive include_role is reached.
        _INCLUDE_STACK[key] = include_id
        try:
            # Executing include_role appends the role's handlers to the play's
            # handlers (ansible/playbook/role_include.py IncludeRole.get_block_list).
            handlers = _convert_handlers(handler_blocks, actions, basedir, includes)
            if handlers and args.get("apply") and parent.vars:
                applied_vars = _tag_untrusted(dict(parent.vars))
                for handler in handlers:
                    handler["handler_block_vars"] = applied_vars
            if handlers and handler_dep_chain is None:
                # Through 2.14 the child handler has no dependency chain. It still
                # sees defaults from roles declared on the play via VariableManager.
                from ansible.utils.vars import combine_vars

                play_defaults = {}
                for play_role in play.get_roles():
                    play_defaults = combine_vars(play_defaults, play_role.get_default_vars())
                tagged_defaults = _tag_untrusted(play_defaults)
                for handler in handlers:
                    handler["role_defaults"] = combine_vars(tagged_defaults, handler.get("role_defaults") or {})
            tasks = [c for c in (_convert_node(b, actions, basedir, only_tags=only_tags,
                                               skip_tags=skip_tags, includes=includes)
                                 for b in blocks) if c]
        finally:
            _INCLUDE_STACK.pop(key, None)
            for root in role_roots:
                del _INCLUDE_ROLE_PARENTS[root]
    role_defaults, role_vars = _role_vars(task)
    return _include_node(task, "include_role", name, basedir,
                         role_defaults, role_vars, tasks, include_id, ref, handlers)


def _play_of(task):
    obj = task
    while obj is not None:
        play = getattr(obj, "_play", None)
        if play is not None:
            return play
        obj = getattr(obj, "_parent", None)
    return None


def _register_included(blocks):
    from ansible.playbook.block import Block
    from ansible.playbook.task import Task
    from ansible.playbook.task_include import TaskInclude

    includes = []

    def walk(obj):
        if isinstance(obj, Block):
            for section in (obj.block, obj.rescue, obj.always):
                for sub in (section or []):
                    walk(sub)
        elif isinstance(obj, Task):
            INCLUDED_TASKS[obj._uuid] = obj
            if isinstance(obj, TaskInclude):
                includes.append(obj)

    for b in blocks:
        walk(b)
    return includes


def _matches_tags(task, only_tags=None, skip_tags=None, all_vars=None):
    if not only_tags and not skip_tags:
        return True
    from ansible import constants as C

    if task.action in C._ACTION_META:
        # Core 2.10 keeps all meta tasks; later cores keep implicit ones.
        # From 2.19.4 generated flushes have "always" and use normal tag rules.
        if (_core_version() < (2, 11)
                or (getattr(task, "implicit", False) and "always" not in task.tags)):
            return True
    try:
        return bool(task.evaluate_tags(only_tags or set(), skip_tags or set(), all_vars or {}))
    except Exception:
        return True


def _convert_task(task, actions, basedir):
    action = task.action

    if action in ("meta", "ansible.builtin.meta"):
        raw = ""
        if isinstance(task.args, dict):
            raw = str(task.args.get("_raw_params", ""))
        if "flush_handlers" in raw:
            return {"type": "task", "uuid": task._uuid, "name": "Flush handlers",
                    "action": "meta", "special": "meta_flush_handlers",
                    "when": _conditions(task.when)}
        if "clear_host_errors" in raw:
            return {"type": "task", "uuid": task._uuid, "name": "Clear host errors",
                    "action": "meta", "special": "meta_clear_host_errors",
                    "when": _conditions(task.when)}
        return None  # other meta actions are no-ops in v1

    role_defaults, role_vars = _role_vars(task)

    args = _clean_args(task.args)
    unarchive = action.rsplit(".", 1)[-1] == "unarchive"
    builtin_unarchive = unarchive and _is_builtin_unarchive(action)
    if builtin_unarchive and "copy" in args:
        literal_copy = _source_flag(args["copy"])
        if literal_copy is not None:
            args["copy"] = literal_copy
    if action.split(".")[-1] == "assert" and "that" in args:
        args["that"] = _conditions(args["that"])

    node = {
        "type": "task",
        "uuid": task._uuid,
        "name": task.name or action,
        "action": action,
        "args": args,
        "when": _conditions(task.when),
        "loop": _loop_of(task, basedir),
        "loop_var": _loop_var(task),
        "index_var": _index_var(task),
        **_loop_control(task),
        "loop_label": _loop_label(task),
        "register": _register_of(task),
        "changed_when": _conditions(task.changed_when) or None,
        "failed_when": _conditions(task.failed_when) or None,
        "until": _conditions(getattr(task, "until", None)) or None,
        # None = unset: TaskExecutor applies the default (3 retries with
        # `until`; ansible-core < 2.16 bakes 3 into the field default).
        "retries": (None if getattr(task, "retries", None) is None
                    else _numeric_or_template(task.retries, int)),
        "delay": _numeric_or_template(getattr(task, "delay", 0), float),
        "environment": _as_list(getattr(task, "environment", None)) or None,
        "search_dirs": _search_dirs(task, basedir, "files"),
        "search_path": _raw_search_path(task, basedir),
        **_ignore_errors_keywords(task),
        "no_log": bool(getattr(task, "no_log", False)),
        "ignore_unreachable": _boolean_or_template(getattr(task, "ignore_unreachable", False)),
        "async_val": int(getattr(task, "async_val", 0) or 0),
        "poll": int(getattr(task, "poll", 0) or 0),
        "run_once": getattr(task, "run_once", None),
        # None inherits the invocation's --check setting; explicit values
        # (including templates) override it for this task.
        "check_mode": getattr(task, "check_mode", None),
        "become": bool(task.become),
        "become_user": getattr(task, "become_user", None),
        "vars": _tag_untrusted(dict(task.vars or {})),
        "role_defaults": _tag_untrusted(role_defaults),  # lowest precedence (below play vars)
        "role_vars": _tag_untrusted(role_vars),          # above play vars, below block/task vars
        "role_params": _tag_untrusted(_role_params(task)),  # above set_fact/register
        "role_magic": _role_magic(task),
        "notify": _tag_untrusted(task.notify),
        "special": _special_of(action),
    }
    if _is_handler(task):
        node.update(_handler_match(task))
    if unarchive:
        node["_builtin_unarchive"] = builtin_unarchive
    if node["async_val"] or action in ("async_status", "ansible.builtin.async_status",
                                       "ansible.legacy.async_status"):
        from ansible.plugins.loader import shell_loader

        node["async_options"] = {
            "version": _core_version(include_patch=True),
            "dir": shell_loader.get("sh").get_option("async_dir"),
        }
        if node["async_val"] and node["poll"] == 0:
            node["async_options"]["poll_before_timeout"] = _async_poll_before_timeout()
    accelerator = _classify_accelerator(action, node["args"], node.get("loop"))
    if accelerator:
        node["accelerator"] = accelerator
        options = _accelerator_options(action)
        if options:
            node["accelerator_options"] = options

    if action.split(".")[-1] in ("shell", "raw"):
        # `shell` (and `raw`, approximately) is the command module with shell
        # semantics enabled.
        node["args"].setdefault("_uses_shell", True)

    # Record delegation so the strategy can decide where to run it (controller vs
    # the target); the loader no longer forces a fallback for delegate_to.
    delegate = getattr(task, "delegate_to", None)
    node["delegate_to"] = str(delegate) if delegate else None
    node["delegate_facts"] = getattr(task, "delegate_facts", False)
    if delegate and action.rsplit(".", 1)[-1] == "set_fact":
        node["delegate_to"] = _tag_untrusted(delegate, template_syntax_only=True)
        node["delegate_facts"] = _boolean_or_template(node["delegate_facts"])

    if builtin_unarchive or (action in BAKED_ACTIONS and not unarchive):
        _bake_sources(node, task, basedir)
        if node["special"] is None:
            actions.add(action)
        else:
            # The runtime runs these like the copy action plugin: through the
            # official copy and file modules.
            actions.update(COPY_ACTION_MODULES)
            node.update(_copy_action_settings())
            if node["special"] == "copy" and _remote_src(node["args"]) is not False:
                # remote_src copies run the copy module on the target, like the
                # copy action plugin's remote_src path.
                actions.add(action)
    elif action in CONTROLLER_COUPLED:
        node["fallback"] = True
        node["fallback_reason"] = "controller-coupled action %s" % action
    elif node["special"] is None:
        actions.add(action)
    if node["special"] == "set_fact":
        # Only top-level fact names from the authored argument mapping may
        # be templates. Preserve their trust before serialization strips tags.
        keys = [k for k in node["args"] if isinstance(k, str)
                and ("{{" in k or "{%" in k)]
        trusted_keys = [k for k in _tag_untrusted(keys) if isinstance(k, str)]
        if trusted_keys:
            node["set_fact_key_templates"] = trusted_keys
    if action.split(".")[-1] == "assert":
        node["args"] = {k: v if k == "that" else _tag_untrusted(v)
                        for k, v in node["args"].items()}
    else:
        node["args"] = _tag_untrusted(node["args"])
    node["loop"] = _tag_loop_values(task, node["loop"])
    return node


def _role_names(task):
    role = getattr(task, "_role", None)
    if role is None:
        return []
    names = []
    for fqcn in (False, True):
        try:
            name = role.get_name(include_role_fqcn=fqcn)
        except TypeError:
            name = role.get_name()
        if name not in names:
            names.append(name)
    return names


def _handler_match(task):
    """Names and ``listen`` topics a notification can match a handler by.

    Mirrors StrategyBase.search_handlers_by_notification: a handler matches
    its own name or its role-qualified name. Since 2.17,
    Handler._validate_listen also adds role-qualified topics.
    """
    roles = _role_names(task)
    names = []
    if task.name:
        names.append(str(task.name))
        names.extend("%s : %s" % (role, task.name) for role in roles)
    # ansible-core 2.17+ includes role-qualified aliases in task.listen.
    listen = list(dict.fromkeys(map(str, _as_list(getattr(task, "listen", None)))))
    match = {"handler_names": names, "listen": listen}
    if names and _is_templated(names[0]):
        if isinstance(_tag_untrusted(task.name, template_syntax_only=True), dict):
            match["handler_name_trusted"] = False
            return match
        # StrategyBase resolves handler names with play/task vars and no host.
        variables = task.get_variable_manager().get_vars(
            play=_play_of(task), task=task, include_hostvars=False)
        variables.pop("vars", None)  # rebuilt after applying invocation vars
        variables.pop("omit", None)  # use the stable runtime omit token
        match["handler_name_vars"] = _tag_untrusted(variables)
    return match


def _contains_dynamic_value(value):
    if isinstance(value, str):
        return "{{" in value or "{%" in value
    if isinstance(value, dict):
        return any(_contains_dynamic_value(k) or _contains_dynamic_value(v)
                   for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_dynamic_value(v) for v in value)
    return False


def _classify_accelerator(action, args, loop):
    name = str(action or "").split(".")[-1]
    if name not in ACCELERATED_ACTIONS:
        return None
    # ``warn`` was a command-module argument through ansible-base 2.10 and was
    # subsequently removed.  Accepting it in a newer compiled artifact would
    # hide the official module's unsupported-parameter failure.
    if (
            name in ("command", "shell")
            and "warn" in args
            and not _legacy_command_warn_supported()):
        return None
    # A loop by itself does not make literal arguments dynamic.  Only classify
    # per invocation when templating can actually change the capability set.
    if _contains_dynamic_value(args):
        return "dynamic"
    return "always" if can_accelerate(name, args, check_mode=False) else None


def _legacy_command_warn_supported():
    try:
        from ansible.release import __version__
        match = re.match(r"^(\d+)\.(\d+)", __version__)
        return bool(match and tuple(map(int, match.groups())) < (2, 11))
    except Exception:
        return False


def _core_version(include_patch=False):
    from ansible.release import __version__

    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", __version__)
    version = tuple(int(part or "0") for part in match.groups()) if match else (0, 0, 0)
    return version if include_patch else version[:2]


@lru_cache(maxsize=1)
def _async_poll_before_timeout():
    # The watchdog loop differs across patch releases (e.g. 2.20.0 and
    # 2.20.11). Bake the installed wrapper's behavior into the agent.
    source = pkgutil.get_data("ansible", "modules/async_wrapper.py")
    return b"step = remaining / 2" in (source or b"")


def _accelerator_options(action):
    name = str(action or "").split(".")[-1]
    try:
        full_version = _core_version(include_patch=True)
        version = full_version[:2]
        options = {}
        # AnsibleModule._check_locale started selecting a UTF-8 fallback in 2.12.
        if (2, 10) <= version < (2, 12):
            options["legacy_locale"] = True
        if name == "stat" and version >= (2, 21):
            options["disk_usage_bytes"] = True
        elif name in ("command", "shell"):
            if version < (2, 12):
                options["check_always_skipped"] = True
            if version < (2, 11):
                options["omit_exec_error_output"] = True
            if version < (2, 19):
                options["chdir_error_detail"] = True
                options["exec_error_detail"] = True
            if version >= (2, 14):
                options["check_skip_unchanged"] = True
            if version >= (2, 21):
                options["verbose_nonzero"] = True
        elif name == "find" and version >= (2, 12):
            options["all_paths_examined"] = True
            options["structured_skipped_paths"] = True
            if version >= (2, 21):
                options["disk_usage_bytes"] = True
            if version >= (2, 19):
                options["typed_warnings"] = True
        elif name == "slurp":
            if version < (2, 19):
                options["lowercase_not_found"] = True
            else:
                options["typed_exceptions"] = True
        elif name == "apt":
            if version >= (2, 12):
                options["parsable_locale"] = True
            if version >= (2, 18):
                options["language_locale"] = True
        elif name == "blockinfile":
            if version < (2, 17):
                options["chmod_before_chown"] = True
            if version >= (2, 20):
                options["text_mode"] = True
        # Older file modules returned only dest for a touch in check mode.
        elif name == "file" and (
                full_version < (2, 13, 8) or (2, 14, 0) <= full_version < (2, 14, 2)):
            options["omit_touch_check_changed"] = True
        elif name == "replace":
            if version < (2, 17):
                options["chmod_before_chown"] = True
            if version < (2, 20):
                options["binary_io"] = True
            if version < (2, 13):
                options["omit_result_rc"] = True
        elif name == "user":
            if full_version >= (2, 18, 9):
                options["home_mode"] = True
            if version >= (2, 21):
                options["result_memberships"] = True
                options["placeholder_homes"] = True
        elif name in ("systemd", "systemd_service") and version >= (2, 14):
            options["modern_is_enabled"] = True
        return options or None
    except Exception:
        pass
    return None


def _copy_action_settings():
    """Controller-side copy action settings from the compiling ansible-core."""
    from ansible.release import __version__

    settings = {}
    version = tuple(map(int, __version__.split(".")[:2]))
    # 2.17 switched to hidden temporary names and kept the suffix for validate.
    if version < (2, 17):
        settings["_copy_legacy_stage"] = True
    # 2.16 started using the destination as the diff header for inline content.
    if version < (2, 16):
        settings["_copy_inline_diff_source"] = True
    try:
        from ansible.plugins.action.copy import REAL_FILE_ARGS
        settings["_copy_file_args"] = sorted(REAL_FILE_ARGS)
    except Exception:
        pass
    try:
        from ansible import constants as C
        settings["_max_diff_size"] = int(C.MAX_FILE_SIZE_FOR_DIFF)
    except Exception:
        pass
    return settings


def _special_of(action):
    name = action.split(".")[-1]
    if name in ("debug", "set_fact", "fail", "assert", "template", "copy"):
        return name
    return None


def _bake_sources(node, task, basedir):
    """Bake controller-side src file(s) for template/copy/unarchive.

    Stores node['_baked'] = {rendered_src: text}. Resolves src using Ansible's
    role-aware search path (role templates/ and files/ dirs, task dir, playbook
    dir). Handles a literal-list loop whose src contains only `{{ item }}`. Falls
    back when src is dynamic in a way we cannot resolve, or the file is missing.
    """
    args = node.get("args") or {}
    src = args.get("src")
    if not src:
        return  # copy with `content:` -- nothing to bake, templated at runtime
    name = node.get("action", "").split(".")[-1]
    remote = _remote_src(args, unarchive=name == "unarchive") if name in ("copy", "unarchive") else False
    if remote is True:
        return  # src lives on the target; the module reads it there

    subdir = "templates" if name == "template" else "files"
    newline = ""
    # Template exposes source metadata (template_fullpath, template_mtime, template_uid) and copy reads the source
    # mode for mode=preserve; otherwise it is left out, since mtimes would make compiles differ across checkouts.
    mode = args.get("mode")
    with_meta = name == "template" or (name == "copy" and (mode == "preserve" or _contains_dynamic_value(mode)))
    # playbook_dir is the controller's playbook directory, so it is known now. The runtime renders it to the
    # same path, which then matches the baked key.
    src = _PLAYBOOK_DIR_RE.sub(lambda _m: basedir, src)
    role_path = getattr(getattr(task, "_role", None), "_role_path", None)
    if role_path:
        src = _ROLE_PATH_RE.sub(lambda _m: role_path, src)

    candidates = []
    if _JINJA_RE.search(src):
        loop = node.get("loop")
        if isinstance(loop, list) and _only_item(src):
            for it in loop:
                candidates.append(_ITEM_RE.sub(str(it), src))
        elif _bake_absolute_tree(node, src, basedir, with_meta, newline=newline):
            return
        # Baked search trees are keyed relative to a search dir, so they can never match a src that renders to an
        # absolute path, which a leading Jinja dir (e.g. "{{ some_dir }}/x") may well do.
        elif (not (os.path.isabs(src) or _USES_PLAYBOOK_DIR_RE.search(src) or _starts_with_jinja_dir(src))
              and _bake_search_tree(node, task, basedir, subdir, src)):
            # src is dynamic (e.g. "services/{{ item.name }}.service.j2") and the loop is a runtime var; bake every
            # file it could resolve to.
            return
        elif remote is None:
            return  # remote_src decided at runtime; src may be a target path
        else:
            node["fallback"] = True
            node["fallback_reason"] = "dynamic baked src not resolvable at compile time: %s" % src
            return
    else:
        candidates.append(src)

    baked = {}
    meta = {}
    for rel in candidates:
        path = _find_needle(task, subdir, rel)
        if path is None:
            if remote is None:
                continue  # may be a target path if remote_src renders true
            node["fallback"] = True
            node["fallback_reason"] = "baked src not found: %s" % rel
            return
        with open(path, "r", encoding="utf-8", errors="surrogateescape", newline=newline) as fh:
            baked[rel] = fh.read()
        if not with_meta:
            continue
        try:
            st = os.stat(path)
            meta[rel] = {"fullpath": os.path.abspath(path),
                         "mtime": st.st_mtime, "uid": st.st_uid,
                         "mode": stat.S_IMODE(st.st_mode)}
        except OSError:
            pass
    node["_baked"] = baked
    if meta:
        node["_baked_meta"] = meta


def _is_builtin_unarchive(action):
    path, resolved = find_module(action)
    if resolved == "ansible.builtin.unarchive":
        return True
    if path and action in ("unarchive", "ansible.legacy.unarchive"):
        # Core 2.10 can report the requested alias as the resolved name.
        builtin, _ = find_module("ansible.builtin.unarchive")
        return bool(builtin and os.path.realpath(path) == os.path.realpath(builtin))
    return False


def _remote_src(args, *, unarchive=False):
    """Whether compilation can skip reading the source, or None until templated."""
    remote_src = _source_flag(args.get("remote_src", False))
    if not unarchive or "copy" not in args:
        return remote_src

    copy = _source_flag(args["copy"])
    if "remote_src" not in args:
        return None if copy is None else not copy

    # Both fixed options fail validation. If a templated option is omitted,
    # the other option may still guarantee the archive lives on the target.
    return True if copy is False or remote_src is True or (copy is not None and remote_src is not None) else None


def _source_flag(value):
    from ansible.parsing import vault
    from .runtime import _ansible_boolean

    try:
        if isinstance(value, getattr(vault, "EncryptedString", ())) or getattr(value, "__ENCRYPTED__", False):
            value = str(value)
        if _contains_dynamic_value(value) and isinstance(_tag_untrusted(value, template_syntax_only=True), str):
            from jinja2 import Environment, nodes

            parsed = Environment().parse(value).body
            if (len(parsed) == 1 and isinstance(parsed[0], nodes.Output) and len(parsed[0].nodes) == 1
                    and isinstance(parsed[0].nodes[0], nodes.Const) and isinstance(parsed[0].nodes[0].value, bool)):
                return parsed[0].nodes[0].value
            return None
        if isinstance(value, (dict, list, tuple)):
            return None
        return _ansible_boolean(value)
    except Exception:
        return None


def _find_needle(task, subdir, rel):
    """ActionBase._find_needle: the controller-side file a copy/template src
    names, or None when it is missing or not a regular file."""
    try:
        path = task._loader.path_dwim_relative_stack(
            task.get_search_path(), subdir, rel)
    except Exception:
        return None
    return path if os.path.isfile(path) else None


class _RoleSourceLeaseError(ValueError):
    pass


def _open_role_source(path):
    for attempt in range(5):
        try:
            return os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as ex:
            if ex.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                raise
            if attempt == 4:
                raise _RoleSourceLeaseError("cannot bake role source %r while a write lease is active" % path) from ex
            time.sleep(0.05)


def _bake_absolute_tree(node, src, basedir, with_meta, *, newline=None):
    """Bake the playbook-dir files an absolute dynamic src can name, keyed by their absolute paths with repeated
    separators collapsed (as the runtime looks them up). Each {{ ... }} matches any run of characters, "/" included,
    below the src's static directory, so "{{ playbook_dir }}/templates/{{ item }}.j2" bakes nested .j2 files too.

    The static directory must lie strictly below the playbook dir: "{{ playbook_dir }}/{{ item }}" would otherwise
    embed vault files, inventories and keys. Hidden entries (.git, vault password files), on the lexical or the
    resolved path, and anything resolving outside the playbook dir are never baked."""
    src = re.sub(r"/+", "/", src)
    if not os.path.isabs(src):
        return False
    tree_dir = os.path.dirname(_JINJA_RE.split(src, 1)[0])
    root = os.path.realpath(basedir)
    if not os.path.isdir(tree_dir) or not _visible_below(tree_dir, basedir) or not _visible_below(
            os.path.realpath(tree_dir), root):
        return False
    pattern = re.compile(".*".join(re.escape(part) for part in _JINJA_RE.split(src)) + r"\Z", re.S)
    baked = {}
    meta = {}
    size = 0
    for walk_root, dirs, files in os.walk(tree_dir, followlinks=True):
        real_root = os.path.realpath(walk_root)
        if not _visible_below(real_root, root):
            dirs[:] = []
            continue
        # A link back to this directory or an ancestor would loop forever.
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".")
            and not os.path.join(real_root, "").startswith(os.path.join(os.path.realpath(os.path.join(walk_root, d)), "")))
        for fn in sorted(files):
            path = os.path.join(walk_root, fn)
            if fn.startswith(".") or not pattern.match(path) or not os.path.isfile(path):
                continue
            if not _visible_below(os.path.realpath(path), root):
                continue
            try:
                st = os.stat(path)
                size += st.st_size
                if size > _ABSOLUTE_TREE_LIMIT:
                    return False
                with open(path, "r", encoding="utf-8", errors="surrogateescape", newline=newline) as fh:
                    baked[path] = fh.read()
            except OSError:
                continue
            if with_meta:
                meta[path] = {"fullpath": os.path.abspath(path), "mtime": st.st_mtime, "uid": st.st_uid,
                              "mode": stat.S_IMODE(st.st_mode)}
    if not baked:
        return False
    node["_baked"] = baked
    if meta:
        node["_baked_meta"] = meta
    return True


def _visible_below(path, root):
    """Whether path lies strictly below root through no hidden (dot) component."""
    rel = os.path.relpath(path, root)
    return rel != "." and not any(part.startswith(".") for part in rel.split(os.sep))


def _starts_with_jinja_dir(src):
    return _literal_head(src) == "" and "/" in _JINJA_RE.sub("", src)


def _literal_head(src):
    m = _JINJA_RE.search(src)
    return src[:m.start()] if m else src


def _literal_role_source_edges(src):
    from jinja2 import Environment, TemplateSyntaxError, nodes

    from .runtime import _bare_template_expression

    if _bare_template_expression(src) is not None:
        return "", ""
    try:
        body = Environment(keep_trailing_newline=True).parse(src).body
    except TemplateSyntaxError:
        return "", ""
    head = tail = ""
    if body and isinstance(body[0], nodes.Output):
        output = body[0].nodes
        if output and isinstance(output[0], nodes.TemplateData):
            head = output[0].data
    if body and isinstance(body[-1], nodes.Output):
        output = body[-1].nodes
        if output and isinstance(output[-1], nodes.TemplateData):
            tail = output[-1].data
    return head, tail


def _role_source_layout(base, walk):
    """Record directory traversal for a role search root."""
    real_base = os.path.realpath(base)
    directories, aliases = set(), {}
    for root, dirs, _ in walk:
        directory = os.path.relpath(root, base)
        while directory not in directories:
            directories.add(directory)
            directory = os.path.dirname(directory) or "."
        for name in dirs:
            path = os.path.join(root, name)
            if os.path.islink(path):
                aliases[os.path.relpath(path, base)] = os.path.relpath(os.path.realpath(path), real_base)
    return directories, aliases


def _role_source_mode(path):
    try:
        return os.stat(path).st_mode
    except OSError:
        return None


def _role_traversal_duplicates(claimed, special_paths, directories, aliases, literal_tail):
    """Find normalized files whose spelling can traverse a special file."""
    if not special_paths:
        return set()

    from collections import deque

    children, parents = {}, {}
    for path in (directories | aliases.keys()) - {"."}:
        parent, name = os.path.split(path)
        parent = parent or "."
        target = aliases.get(path, path)
        children.setdefault(parent, {})[name] = target
        parents.setdefault(target, set()).add(parent)

    def parent_of(path):
        return (os.path.dirname(path) or ".") if path != "." else None

    # A child/parent pair leaves the normalized path unchanged. Only explore
    # nontrivial pairs and ancestors that can contain them.
    affected = {parent_of(path) for path, target in aliases.items()
                if target != "." and parent_of(path) != parent_of(target)}
    if all(path.count(os.sep) == target.count(os.sep) and target != "." for path, target in aliases.items()):
        # A depth-preserving walk can only end elsewhere if an alias leaves
        # the subtree of the starting directory.
        affected = {ancestor for path, target in aliases.items()
                    for ancestor in _role_directory_parents(path)
                    if ancestor != "." and not target.startswith(ancestor + os.sep)}
    else:
        pending = list(affected)
        while pending:
            for parent in parents.get(pending.pop(), ()):
                if parent not in affected:
                    affected.add(parent)
                    pending.append(parent)

    arrivals, seen, waiting, work = {}, {}, {}, deque()

    def add(start, end):
        if end not in seen.setdefault(start, set()):
            seen[start].add(end)
            arrivals.setdefault(start, []).append(end)
            work.append((start, end))

    def destinations(start):
        if start not in affected:
            yield start
            return
        add(start, start)
        index = 0
        while index < len(arrivals[start]) or work:
            if index < len(arrivals[start]):
                yield arrivals[start][index]
                index += 1
                continue
            origin, current = work.popleft()
            if current != ".":
                for outer in waiting.get(origin, ()):
                    add(outer, parent_of(current))
            for child in children.get(current, {}).values():
                if child not in affected:
                    parent = parent_of(child)
                    if parent is not None:
                        add(origin, parent)
                    continue
                waiters = waiting.setdefault(child, set())
                if origin in waiters:
                    continue
                waiters.add(origin)
                add(child, child)
                # A child may have been visited before this query needed it.
                for reached in tuple(arrivals[child]):
                    if reached != ".":
                        add(origin, parent_of(reached))

    normalized_tail = os.path.normpath(os.sep.join(literal_tail)).split(os.sep) if literal_tail else []
    pops = 0
    while pops < len(normalized_tail) and normalized_tail[pops] == "..":
        pops += 1
    ending = normalized_tail[pops:]
    special_names = {os.path.basename(path) for path in special_paths}

    def reaches_special(parent, tail):
        for part in tail[:-1]:
            if part == "..":
                parent = parent_of(parent)
            elif part not in ("", "."):
                parent = children.get(parent, {}).get(part)
            if parent is None:
                return False
        return os.path.normpath(os.path.join(parent, tail[-1])) in special_paths

    duplicates = set()
    for path in claimed:
        parts = path.split(os.sep)
        if parts[-1] not in special_names or (ending and parts[-len(ending):] != ending):
            continue
        prefix = parts[:-(len(ending) or 1)]
        current = {"."}
        for component in prefix:
            current = {child for start in current for parent in destinations(start)
                       if (child := children.get(parent, {}).get(component))}
        # The fixed tail may pop directories supplied by the variable.
        for _ in range(pops):
            current = {child for start in current for parent in destinations(start)
                       for child in children.get(parent, {}).values()}
        tail = literal_tail or parts[-1:]
        if any(reaches_special(parent, tail) for start in current for parent in destinations(start)):
            duplicates.add(path)
    return duplicates


def _role_directory_parents(path):
    while path != ".":
        path = os.path.dirname(path) or "."
        yield path


def _bake_search_tree(node, task, basedir, subdir, src):
    """Bake every file a dynamic role src could resolve to under the action's search dirs, keyed by path relative to
    its search dir; the first dir wins, as in _find_needle. Without a literal subdir in the src, only the templates/
    or files/ dirs are walked, since a whole role, task or playbook dir would bundle the entire playbook tree. A file
    that an unwalked, earlier dir would shadow is left out so the runtime fails loudly. Returns True if anything was
    baked."""
    if getattr(task, "_role", None) is None:
        return False
    newline = ""
    prefix = _literal_head(src).rpartition("/")[0]
    if ".." in prefix.split("/"):
        if os.path.normpath(prefix).split("/")[0] == "..":
            return False
        # The runtime resolves ".." against the walked dirs, so walk what an unnarrowed src would.
        prefix = ""
    head, tail = _literal_role_source_edges(src)
    head_dir, _, basename_head = head.rpartition(os.sep)
    prefix_dir = os.path.normpath(prefix)
    if (basename_head in (".", "..") or ".." in head_dir.split(os.sep)
            or os.path.normpath(head_dir) != prefix_dir):
        basename_head = ""
    fixed_path = ""
    literal_tail = ()
    if os.sep in tail:
        trailing_components = tail.partition(os.sep)[2].split(os.sep)
        basename = trailing_components[-1]
        if basename not in ("", ".", ".."):
            # Extra separators are no-ops inside this relative suffix.
            literal_tail = tuple(part for part in trailing_components if part)
            # Parent traversal can change the starting directory, but the
            # literal components after the last ".." still constrain the path.
            start = max((index + 1 for index, part in enumerate(literal_tail) if part == ".."), default=0)
            suffix = os.sep.join(literal_tail[start:])
            fixed_path = os.sep + os.path.normpath(suffix).lstrip(os.sep)
    trees = []
    directories = set()
    aliases = {}
    # Before 2.21 Ansible compared the byte path component to a text dirname.
    explicit_subdir = _literal_head(src).startswith(subdir + "/") and _core_version() >= (2, 21)
    for base, is_subdir in _search_dirs(task, basedir, subdir, tagged=True, explicit_subdir=explicit_subdir):
        walk = []
        requires_basename_head = False
        if prefix or is_subdir:
            start = os.path.join(base, prefix)
            for root, dirs, files in os.walk(start):
                dirs.sort()
                if basename_head and root == start:
                    # Only a matching directory can let a rendered slash or
                    # parent traversal reach a file outside this basename prefix.
                    requires_basename_head = not any(name.startswith(basename_head) for name in dirs)
                if fixed_path:
                    files = [basename] if basename in files else []
                walk.append((root, dirs, files))
        root_dirs, root_aliases = _role_source_layout(base, walk)
        trees.append((base, is_subdir, walk, root_dirs, root_aliases, requires_basename_head))
        directories.update(root_dirs)
        for relative, target in root_aliases.items():
            aliases.setdefault(relative, target)
    aliases = {path: target for path, target in aliases.items() if target in directories}
    suffix = fixed_path[1:].split(os.sep) if os.sep in fixed_path[1:] else ()

    def matches_source(relative, filename, alias_candidates):
        if fixed_path and not ((os.sep + relative).endswith(fixed_path) or relative in alias_candidates):
            return False
        return os.sep in tail or filename.endswith(tail)

    baked, meta = {}, {}
    alternate_meta = {}
    claimed = set()
    skipped = []
    deferred_duplicates = {}
    blockers, shadowed, source_roots = {}, {}, []

    def bake_source(full, rel, root_index, alternate):
        _, _, root_blocks, root_files = source_roots[root_index]
        claimed.add(rel)
        key = "\0%d\0%s" % (root_index, rel) if alternate else rel
        root_files[rel] = key
        if rel in blockers:
            shadowed.setdefault(rel, set()).add(root_index)
        try:
            # The path may have become a FIFO after the file check.
            try:
                fd = _open_role_source(full)
            except OSError as ex:
                # A replacement special file can fail open before fstat.
                if ex.errno not in (errno.ENXIO, errno.EACCES):
                    raise
                mode = os.stat(full).st_mode
                if stat.S_ISREG(mode):
                    raise
            else:
                try:
                    st = os.fstat(fd)
                    mode = st.st_mode
                    if stat.S_ISREG(mode):
                        with os.fdopen(fd, "r", encoding="utf-8", errors="surrogateescape",
                                       newline=newline, closefd=False) as fh:
                            baked[key] = fh.read()
                finally:
                    os.close(fd)
        except _RoleSourceLeaseError as ex:
            baked[key] = {"__ansiroll_source_error__": str(ex)}
            shadowed.setdefault(rel, set()).add(root_index)
            return
        except OSError as ex:
            if not isinstance(ex, (FileNotFoundError, NotADirectoryError)) and ex.errno != errno.ELOOP:
                baked[key] = {"__ansiroll_source_error__": str(ex)}
                shadowed.setdefault(rel, set()).add(root_index)
                return
            if not alternate:
                claimed.remove(rel)
                shadowed.pop(rel, None)
            elif rel in shadowed:
                shadowed[rel].discard(root_index)
            del root_files[rel]
            return
        if not stat.S_ISREG(mode):
            if not alternate:
                claimed.remove(rel)
            del root_files[rel]
            root_blocks[rel] = stat.S_ISDIR(mode)
            blockers.setdefault(rel, set()).add(root_index)
            if rel in shadowed:
                shadowed[rel].discard(root_index)
            return
        if alternate:
            alternate_meta[key] = {"fullpath": os.path.abspath(full), "mtime": st.st_mtime,
                                   "uid": st.st_uid, "mode": stat.S_IMODE(st.st_mode)}
            return
        meta[rel] = {"fullpath": os.path.abspath(full), "mtime": st.st_mtime, "uid": st.st_uid,
                     "mode": stat.S_IMODE(st.st_mode)}

    for base, is_subdir, walk, root_dirs, root_aliases, requires_basename_head in trees:
        root_index = len(source_roots)
        # The blocker's flag records whether copy would recurse into it.
        root_blocks, root_files = {}, {}
        source_roots.append((root_dirs, root_aliases, root_blocks, root_files))
        if not prefix and not is_subdir:
            skipped.append((base, root_index))
            continue
        alias_candidates = set()
        if suffix and root_aliases:
            # The rendered prefix can reach any directory in this search root.
            # Follow the literal suffix with this root's aliases.
            reachable = root_dirs
            for component in suffix[:-1]:
                children = (os.path.normpath(os.path.join(directory, component)) for directory in reachable)
                reachable = {target for child in children if (target := root_aliases.get(child, child)) in root_dirs}
            alias_candidates = {os.path.normpath(os.path.join(directory, suffix[-1])) for directory in reachable}
        special_paths = set()
        # Canceling a sibling alias with ".." returns to the same parent.
        # A walked regular file can only differ from the traversed source
        # after a symlink changes the parent reached by "..".
        redirects_parent = any(os.path.dirname(path) != os.path.dirname(target)
                               for path, target in root_aliases.items())
        if redirects_parent and claimed:
            claimed_names = {os.path.basename(relative) for relative in claimed}
            # A traversed special file and its normalized payload have the
            # same basename, even when a symlink changes their parent path.
            for directory, _, files in walk:
                for filename in claimed_names.intersection(files):
                    path = os.path.join(directory, filename)
                    relative = os.path.relpath(path, base)
                    if matches_source(relative, filename, alias_candidates):
                        mode = _role_source_mode(path)
                        if mode is not None and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                            special_paths.add(relative)
        traversal_duplicates = _role_traversal_duplicates(claimed, special_paths, root_dirs, root_aliases, literal_tail)
        for root, dirs, files in walk:
            for dirname in dirs:
                path = os.path.join(root, dirname)
                mode = _role_source_mode(path)
                if mode is None:
                    continue
                if stat.S_ISREG(mode):
                    files.append(dirname)
                    continue
                relative = os.path.relpath(path, base)
                root_blocks[relative] = stat.S_ISDIR(mode)
                blockers.setdefault(relative, set()).add(root_index)
            for fn in sorted(files):
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, base)
                if not matches_source(rel, fn, alias_candidates):
                    continue
                mode = _role_source_mode(full)
                if mode is None:
                    continue
                if requires_basename_head and (os.path.normpath(os.path.dirname(rel)) != prefix_dir
                                               or not fn.startswith(basename_head)):
                    # A different root can still select this normalized name
                    # via traversal. Retain blockers without opening any payload.
                    if not stat.S_ISREG(mode):
                        root_blocks[rel] = stat.S_ISDIR(mode)
                        blockers.setdefault(rel, set()).add(root_index)
                    continue
                earlier_sources = []
                for earlier_base, earlier_index in skipped:
                    earlier_path = os.path.join(earlier_base, rel)
                    earlier_mode = _role_source_mode(earlier_path)
                    if earlier_mode is not None:
                        earlier_sources.append((earlier_base, earlier_index, earlier_path, earlier_mode))
                if any(stat.S_ISREG(earlier_mode) for _, _, _, earlier_mode in earlier_sources):
                    continue
                if not stat.S_ISREG(mode):
                    root_blocks[rel] = stat.S_ISDIR(mode)
                    blockers.setdefault(rel, set()).add(root_index)
                    continue
                alternate = rel in claimed
                if alternate and rel not in shadowed and rel not in traversal_duplicates:
                    # Traversal can select this root via a different path than
                    # the normalized path whose payload Ansible reads.
                    root_files[rel] = None
                    deferred_duplicates.setdefault(rel, []).append((root_index, full))
                    continue
                for earlier_base, earlier_index, earlier_path, earlier_mode in earlier_sources:
                    earlier_dirs, earlier_aliases, earlier_blocks, _ = source_roots[earlier_index]
                    if not earlier_dirs:
                        found_dirs, found_aliases = _role_source_layout(earlier_base, os.walk(earlier_base))
                        earlier_dirs.update(found_dirs)
                        earlier_aliases.update(found_aliases)
                    is_directory = stat.S_ISDIR(earlier_mode)
                    earlier_blocks[rel] = is_directory
                    resolved_rel = os.path.relpath(os.path.realpath(earlier_path), os.path.realpath(earlier_base))
                    earlier_blocks[resolved_rel] = is_directory
                    blockers.setdefault(rel, set()).add(earlier_index)
                bake_source(full, rel, root_index, alternate)
    # A later blocker or special traversal can make an earlier duplicate
    # selectable. Read those payloads only after the search layout is known.
    for rel, candidates in deferred_duplicates.items():
        if rel in shadowed:
            for root_index, full in candidates:
                bake_source(full, rel, root_index, alternate=True)
    assets, metadata, direct = _bake_linked_role_sources(task, basedir, subdir, src, meta)
    baked.update(assets)
    meta.update(metadata)
    if not baked:
        return False
    node["_baked"] = baked
    meta.update(alternate_meta)
    node["_baked_meta"] = meta
    node["_baked_tree"] = subdir
    if direct:
        node["_baked_direct_sources"] = direct
    # The runtime resolves ".." against these, so a prefix's parent dirs count as walked too.
    node["_baked_tree_dirs"] = sorted(directories)
    if aliases:
        node["_baked_tree_aliases"] = aliases
    if shadowed:
        node["_baked_tree_shadowed"] = {rel: sorted(blockers.get(rel, set()) | roots) for rel, roots in shadowed.items()}
    has_special_traversal = any(root_aliases and False in root_blocks.values()
                                for _, root_aliases, root_blocks, _ in source_roots)
    if shadowed or has_special_traversal:
        baked["\0blocked"] = {"__ansiroll_source_error__": "an earlier role source is not a regular file"}
        node["_baked_tree_roots"] = [
            (sorted(root_dirs), {path: target for path, target in root_aliases.items() if target in root_dirs},
             dict(sorted(root_blocks.items())), root_files)
            for root_dirs, root_aliases, root_blocks, root_files in source_roots
        ]
    return True


def _raw_search_path(task, basedir):
    """The ansible_search_path magic var: base dirs without subdir expansion."""
    try:
        stack = list(task.get_search_path() or [])
    except Exception:
        stack = []
    return stack or [basedir]


def _search_dirs(task, basedir, subdir, tagged=False, explicit_subdir=False):
    """Directories DataLoader.path_dwim_relative_stack searches, in order, for
    a file under ``subdir`` (ansible/parsing/dataloader.py) as lookup plugins
    call it: each task search path (role-aware), then the loader basedir.
    With ``tagged``, each comes paired with whether it is a ``subdir`` dir."""
    loader = getattr(task, "_loader", None)
    try:
        stack = list(task.get_search_path() or [])
    except Exception:
        stack = []
    try:
        from ansible.utils.path import unfrackpath
    except ImportError:
        def unfrackpath(path, follow=False):
            return os.path.normpath(os.path.abspath(path))
    dirs = []
    for path in stack:
        upath = unfrackpath(path, follow=False)
        pb_base_dir = os.path.dirname(upath)
        # LookupBase.find_file_in_search_path passes is_role='role_path' in
        # the task vars, i.e. any role task; otherwise dwim detects roles.
        is_role = getattr(task, "_role", None) is not None
        if not is_role:
            try:
                is_role = bool(loader._is_role(path))
            except Exception:
                pass
        if is_role and pb_base_dir.endswith("/tasks"):
            dirs.append((os.path.join(os.path.dirname(pb_base_dir), subdir), True))
            dirs.append((pb_base_dir, False))
        if not explicit_subdir:
            dirs.append((os.path.join(upath, subdir), True))
        dirs.append((upath, False))
    try:
        base = loader.get_basedir()
    except Exception:
        base = basedir
    if not explicit_subdir:
        dirs.append((os.path.join(base, subdir), True))
    dirs.append((base, False))
    return dirs if tagged else [d for d, _ in dirs]


def _linked_role_source_patterns(src):
    from jinja2 import Environment, TemplateSyntaxError, nodes

    try:
        parsed = Environment(keep_trailing_newline=True).parse(src)
    except TemplateSyntaxError:
        return None
    body = parsed.body
    assignments = (nodes.Assign, nodes.AssignBlock)
    output_nodes = [node for node in body if not isinstance(node, assignments)]
    if not output_nodes:
        return None

    def literal(node, index):
        if isinstance(node, nodes.Output) and node.nodes and isinstance(node.nodes[index], nodes.TemplateData):
            return node.nodes[index].data
        return ""

    head = literal(output_nodes[0], 0)
    if (not (len(output_nodes) == 1 and isinstance(output_nodes[0], nodes.Output))
            and not literal(output_nodes[-1], -1)):
        return None
    referenced = {name.name for name in parsed.find_all(nodes.Name) if name.ctx == "load"}

    def expand(sequence, bindings):
        paths = [("", bindings)]
        for node in sequence:
            variants = []
            for prefix, current in paths:
                choices = expand_node(node, current)
                if len(variants) + len(choices) > 128:
                    raise ValueError("too many linked source alternatives")
                for suffix, updated in choices:
                    if len(prefix) + len(suffix) > 4096:
                        raise OverflowError("expanded linked source pattern is too long")
                    variants.append((prefix + suffix, updated))
            paths = variants
        return paths

    def expand_assignment(node, bindings):
        target = node.target
        if not isinstance(target, nodes.Name):
            unknown = {name.name: "\0" for name in target.find_all(nodes.Name)}
            return [("", {**bindings, **unknown})]
        if target.name not in referenced:
            return [("", bindings)]
        if isinstance(node, nodes.Assign):
            values = expand_node(node.node, bindings)
        else:
            values = [("\0", bindings)]
            if node.filter is None:
                try:
                    values = expand(node.body, bindings)
                except ValueError:
                    # Captured output does not otherwise affect the path.
                    pass
        return [("", {**bindings, target.name: value}) for value, _ in values]

    def expand_node(node, bindings):
        if node is None:
            # An inline conditional without an else emits an empty string
            # when combined with the rest of a path.
            return [("", bindings)]
        if isinstance(node, nodes.TemplateData):
            return [(node.data, bindings)]
        if isinstance(node, nodes.Const) and isinstance(node.value, str):
            return [(node.value, bindings)]
        if isinstance(node, nodes.Name):
            return [(bindings.get(node.name, "\0"), bindings)]
        if isinstance(node, nodes.Add):
            # When an operand is a string, a successful addition preserves its
            # literal text even if the other operand is dynamic.
            return expand((node.left, node.right), bindings)
        if isinstance(node, (nodes.Output, nodes.Concat)):
            return expand(node.nodes, bindings)
        if isinstance(node, assignments):
            return expand_assignment(node, bindings)
        if isinstance(node, nodes.If):
            choices = []
            for branch in (node, *node.elif_):
                if not isinstance(branch.test, nodes.Const) or branch.test.value:
                    choices.extend(expand(branch.body, bindings))
                if isinstance(branch.test, nodes.Const) and branch.test.value:
                    return choices
            return choices + expand(node.else_, bindings)
        if isinstance(node, nodes.CondExpr):
            branches = (node.expr1, node.expr2)
            if isinstance(node.test, nodes.Const):
                branches = (node.expr1 if node.test.value else node.expr2,)
            return [path for branch in branches for path in expand_node(branch, bindings)]
        if isinstance(node, nodes.Expr):
            return [("\0", bindings)]
        raise ValueError("unsupported linked source statement")

    try:
        candidates = expand(body, {})
    except (ValueError, OverflowError):
        return None
    patterns = []
    for candidate in dict.fromkeys(path for path, _ in candidates):
        if not candidate or os.path.isabs(candidate) or ".." in candidate.split(os.sep):
            continue
        # These spellings require a directory, even after the variables render.
        if candidate.endswith((os.sep, os.sep + ".")):
            continue
        patterns.append((candidate.partition("\0")[0], _linked_role_source_pattern(candidate)))
    return (head, patterns) if patterns else None


def _linked_role_source_pattern(candidate):
    segments = os.path.normpath(re.sub("\0+", "\0", candidate)).split(os.sep)
    pattern = []
    for index, segment in enumerate(segments):
        directory = index < len(segments) - 1
        # A substitution can supply a separator before or after a literal dot.
        segment = re.sub(r"\x00(?:\.\x00)+", "\0", segment)
        if segment.startswith(".\0"):
            segment = segment[1:]
        if directory and segment.endswith("\0."):
            segment = segment[:-1]
        optional = directory and "\0" in segment and segment.replace("\0", "") in ("", ".")
        if optional:
            # A wildcard may emit a directory followed by the dot component
            # that normalization removes, or leave the whole component empty.
            tokens = ("\0", "/")
        else:
            tokens = tuple(part for part in re.split("(\0)", segment) if part)
            if directory:
                tokens += ("/",)
        pattern.append((tokens, optional))
    return pattern


def _linked_role_source_matches(pattern, relative):
    # At most one position per character is retained after each token.
    positions = {0}
    for tokens, optional in pattern:
        # Empty and dot components can vanish on normalization.
        skipped = positions if optional else ()
        for token in tokens:
            if token == "\0":
                positions = set(range(min(positions), len(relative) + 1))
            else:
                positions = {index + len(token) for index in positions if relative.startswith(token, index)}
            if not positions:
                break
        positions.update(skipped)
        if not positions:
            return False
    return len(relative) in positions


def _linked_role_source_prefix(pattern):
    """Return the literal prefix and whether the pattern can extend it."""
    prefix = ""
    for tokens, optional in pattern:
        if optional:
            return prefix, True
        literal, dynamic, _ = "".join(tokens).partition("\0")
        prefix += literal
        if dynamic:
            return prefix, True
    return prefix, False


def _open_linked_role_source(resolved_path):
    """Open a validated real path without following replacement symlinks."""
    *directories, filename = resolved_path.split(os.sep)[1:]
    directory_flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW
    parent = os.open(os.sep, directory_flags)
    try:
        for directory in directories:
            child = os.open(directory, directory_flags, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(filename, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)


def _role_source_roots(task, basedir):
    magic = _role_magic(task) or {}
    return tuple(root for root in
                 (basedir, magic.get("role_path"), *(magic.get("ansible_parent_role_paths") or ())) if root)


def _bake_linked_role_sources(task, basedir, subdir, src, ordinary):
    """Bake direct lookup candidates separately from the legacy parent-path map."""
    from collections import deque
    from pathlib import Path

    # A direct override requires Ansible to resolve the task's search path.
    if getattr(task, "_loader", None) is None:
        return {}, {}, {}
    parsed = _linked_role_source_patterns(src)
    if parsed is None:
        return {}, {}, {}
    head, patterns = parsed
    possible_prefixes = {_linked_role_source_prefix(pattern) for _, pattern in patterns}
    prefix = os.path.normpath(head.rpartition(os.sep)[0])
    if prefix == ".":
        prefix = ""
    source_roots = _role_source_roots(task, basedir)
    real_roots = tuple(os.path.realpath(root) for root in source_roots)

    def visible(path):
        return any(_visible_below(path, root) for root in real_roots)

    winners = {}

    def source_for(relative):
        if relative not in winners:
            winners[relative] = _find_needle(task, subdir, relative)
        winner = winners[relative]
        return os.path.normpath(winner) if winner else None

    def lookup_paths(relative):
        matching_heads = [prefix for prefix, pattern in patterns if _linked_role_source_matches(pattern, relative)]
        if not matching_heads:
            return
        if relative.startswith("~") or relative.split(os.sep, 1)[0] == subdir:
            # A leading "." changes action-subdir and home-path handling.
            if any(not prefix.startswith(".") for prefix in matching_heads):
                yield relative
            if any(not prefix or prefix.startswith(".") for prefix in matching_heads):
                yield "./" + relative
        else:
            yield relative

    def source_spellings(relative, is_subdir):
        yield relative
        if is_subdir:
            yield os.path.join(subdir, relative)

    direct, pending = {}, {}
    ordinary_by_path = {os.path.normpath(meta["fullpath"]): relative for relative, meta in ordinary.items()}
    for relative, meta in ordinary.items():
        for lookup_path in lookup_paths(relative):
            winner = source_for(lookup_path)
            if os.path.normpath(meta["fullpath"]) != winner:
                direct[lookup_path] = ordinary_by_path.get(winner)
                if winner is not None and direct[lookup_path] is None:
                    pending.setdefault(winner, []).append(lookup_path)

    # An ordinary spelling may resolve to a known winner under a different
    # spelling. Keep its ancestors even when that path misses the pattern.
    pending_parents = {str(parent) for winner in pending for parent in Path(winner).parents}

    def may_have_sources(path, base, is_subdir):
        if ("", True) in possible_prefixes or os.path.normpath(path) in pending_parents:
            return True
        relative = os.path.relpath(path, base)
        if relative == ".":
            return True
        for spelling in source_spellings(relative, is_subdir):
            directory = spelling + os.sep
            if any(prefix.startswith(directory) or (can_extend and directory.startswith(prefix))
                   for prefix, can_extend in possible_prefixes):
                return True
        return False

    assets, metadata, contents = {}, {}, {}
    newline = ""
    remaining_linked_visits = 4096
    # Finish paths with the current number of symlinks before expanding paths
    # with one more, even when a path has many ordinary directories.
    pending_dirs, next_hop_dirs = deque(), deque()
    for base, is_subdir in dict.fromkeys(_search_dirs(task, basedir, subdir, tagged=True)):
        if not prefix and not is_subdir:
            continue
        tree = os.path.join(base, prefix)
        if not may_have_sources(tree, base, is_subdir):
            continue
        real_tree = os.path.realpath(tree)
        if not any(_visible_below(tree, root) for root in source_roots) or not visible(real_tree):
            continue
        # The fixed prefix can require linked lookup before the walk expands
        # any additional symlinks.
        pending_dirs.append((tree, base, is_subdir, real_tree != os.path.abspath(tree)))
    expanding_aliases = False
    while pending_dirs or next_hop_dirs:
        if not pending_dirs:
            pending_dirs, next_hop_dirs = next_hop_dirs, pending_dirs
            expanding_aliases = True
        if expanding_aliases:
            if not remaining_linked_visits:
                break
            remaining_linked_visits -= 1
        root, base, is_subdir, linked = pending_dirs.popleft()
        step = next(os.walk(root), None)
        if step is None:
            continue
        _, dirs, files = step
        for name in sorted(dirs):
            path = os.path.join(root, name)
            if name.startswith(".") or not may_have_sources(path, base, is_subdir):
                continue
            target = os.path.realpath(path)
            if not visible(target):
                continue
            child_is_link = os.path.islink(path)
            queue = next_hop_dirs if child_is_link else pending_dirs
            if (expanding_aliases or child_is_link) and len(queue) >= remaining_linked_visits:
                continue
            queue.append((path, base, is_subdir, linked or child_is_link))
        for name in sorted(files):
            if name.startswith("."):
                continue
            path = os.path.normpath(os.path.join(root, name))
            relative = os.path.relpath(path, base)
            # A linked root may need an action-prefixed spelling for an asset
            # already baked by the ordinary walk.
            if not linked and path in ordinary_by_path:
                continue
            paths_to_bake = list(pending.get(path, ()))
            if linked:
                for spelling in source_spellings(relative, is_subdir):
                    for lookup_path in lookup_paths(spelling):
                        winner = source_for(lookup_path)
                        if winner is None:
                            continue
                        winner_key = ordinary_by_path.get(winner)
                        direct.setdefault(lookup_path, winner_key)
                        if path == winner and winner_key is None:
                            paths_to_bake.append(lookup_path)
            if not paths_to_bake:
                continue
            resolved_path = os.path.realpath(path)
            if not visible(resolved_path) or not os.path.isfile(resolved_path):
                continue
            key = "\0" + paths_to_bake[0]  # Cannot collide with a filesystem pathname.
            try:
                fd = _open_linked_role_source(resolved_path)
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                    if identity not in contents:
                        with os.fdopen(fd, "r", encoding="utf-8", errors="surrogateescape", newline=newline,
                                       closefd=False) as file:
                            contents[identity] = file.read()
                    assets[key] = contents[identity]
                finally:
                    os.close(fd)
            except OSError:
                continue
            metadata[key] = {"fullpath": os.path.abspath(path), "mtime": info.st_mtime, "uid": info.st_uid,
                             "mode": stat.S_IMODE(info.st_mode)}
            direct.update(dict.fromkeys(paths_to_bake, key))
    if subdir == "templates":
        for spelling, key in direct.items():
            if key is not None and winners[spelling].endswith(os.sep + spelling):
                meta = metadata[key] if key in metadata else ordinary[key]
                meta.setdefault("lookup_roots", {})[spelling] = winners[spelling][:-len(spelling)]
    return assets, metadata, direct


def _only_item(src):
    """True if every Jinja expression in src is exactly `item`."""
    for m in _JINJA_RE.finditer(src):
        if m.group(0)[2:-2].strip() != "item":
            return False
    return True


def _role_magic(task):
    """Role-context magic vars (role_name, role_path, ...) for a role task."""
    role = getattr(task, "_role", None)
    if role is None:
        return None
    out = {}
    try:
        out["role_name"] = role._role_name
        out["ansible_role_name"] = role.get_name()
        out["role_path"] = role._role_path
        out["role_uuid"] = str(role._uuid)
        out["ansible_collection_name"] = getattr(role, "_role_collection", None) or None
        parents = []
        parent_paths = []
        seen = set()
        stack = list(getattr(role, "_parents", None) or [])
        while stack:
            p = stack.pop(0)
            if id(p) in seen:
                continue
            seen.add(id(p))
            parents.append(p._role_name)
            parent_paths.append(p._role_path)
            stack.extend(getattr(p, "_parents", None) or [])
        if parents:  # unset entirely for roles that were not included/imported
            out["ansible_parent_role_names"] = parents
            out["ansible_parent_role_paths"] = parent_paths
    except Exception:
        pass
    return out or None


def _role_vars(task):
    """Return (role_defaults, role_vars) for a role task, or ({}, {})."""
    role = getattr(task, "_role", None)
    if role is None:
        return {}, {}
    defaults, rvars = {}, {}
    try:
        defaults = dict(role.get_default_vars(dep_chain=task.get_dep_chain()) or {})
    except Exception:
        pass
    try:
        # Dependency roles inherit the including role's parameters, while
        # their own vars still take precedence over the parent's role vars.
        for parent in (getattr(role, "_parents", None) or []):
            rvars.update(parent.get_vars(include_params=False) or {})
        rvars.update(role.get_vars(include_params=False) or {})
    except Exception:
        pass
    return defaults, rvars


def _role_params(task):
    """Role params ({role: x, var: value}) for a role task.

    VariableManager.get_vars merges them after the set_fact/register cache
    (ansible/vars/manager.py: "role params"), unlike role vars.
    """
    role = getattr(task, "_role", None)
    if role is None:
        return {}
    try:
        return dict(role.get_role_params(task.get_dep_chain()) or {})
    except Exception:
        return {}


def _clean_args(args):
    if not isinstance(args, dict):
        return {}
    return dict(args)


def _tag_loop_values(task, loop):
    if getattr(task, "loop_with", None) == "fileglob" and isinstance(loop, list):
        # The compiler uses raw paths for baking, but lookup results must
        # reach the runtime as data, just like results of runtime lookups.
        return [{"__ansiroll_unsafe__": path} for path in loop]
    if isinstance(loop, dict) and "__ansiroll_lookup" in loop:
        return dict(loop, terms=_tag_untrusted(loop["terms"]))
    return _tag_untrusted(loop)


def _loop_of(task, basedir):
    loop = getattr(task, "loop", None)
    # with_fileglob is a controller-side lookup: expand the globs now (the
    # controller has the files) so the matched paths can be baked.
    if getattr(task, "loop_with", None) == "fileglob" and isinstance(loop, list):
        return _expand_fileglob(task, loop, basedir)
    loop_with = getattr(task, "loop_with", None)
    if loop_with in ("sequence", "list", "items", "flattened", "together",
                     "dict", "nested", "cartesian", "indexed_items"):
        if loop_with == "cartesian":
            loop_with = "nested"
        return {"__ansiroll_lookup": loop_with, "terms": loop}
    return loop


def _expand_fileglob(task, patterns, basedir):
    # Mirrors Ansible's fileglob lookup: returns matching FILES only (no dirs).
    import glob as _glob
    dirs = _search_dirs(task, basedir, "files")
    matched = []
    for pat in patterns:
        if not isinstance(pat, str):
            continue
        if os.path.isabs(pat):
            matched.extend(sorted(_glob.glob(pat)))
            continue
        for base in dirs:
            found = sorted(_glob.glob(os.path.join(base, pat)))
            if found:
                matched.extend(os.path.abspath(p) for p in found)
                break
    seen, out = set(), []
    for m in matched:
        if m not in seen and os.path.isfile(m):
            seen.add(m)
            out.append(m)
    return out


def _loop_control(task):
    lc = getattr(task, "loop_control", None)
    if lc is None:
        return {}
    out = {}
    if getattr(lc, "extended", None) is not None:
        out["loop_extended"] = _boolean_or_template(lc.extended)
    if getattr(lc, "extended_allitems", None) is not None:
        out["loop_extended_allitems"] = _boolean_or_template(lc.extended_allitems)
    bw = getattr(lc, "break_when", None)
    if bw:
        out["loop_break_when"] = _conditions(bw)
    pause = getattr(lc, "pause", None)
    if pause:
        out["loop_pause"] = _numeric_or_template(pause, float)
    return out


def _index_var(task):
    lc = getattr(task, "loop_control", None)
    if lc is not None and getattr(lc, "index_var", None):
        return lc.index_var
    return None


def _loop_var(task):
    lc = getattr(task, "loop_control", None)
    if lc is not None and getattr(lc, "loop_var", None):
        return lc.loop_var
    return "item"


def _loop_label(task):
    lc = getattr(task, "loop_control", None)
    if lc is not None and getattr(lc, "label", None):
        return lc.label
    return None


def _numeric_or_template(value, cast):
    if value is None:
        return 0
    try:
        return cast(value)
    except (TypeError, ValueError):
        return value  # templated value; the runtime resolves it per-host


def _ignore_errors_keywords(task):
    value = _boolean_or_template(task.ignore_errors)
    result = {"ignore_errors": value}
    if isinstance(value, str):
        try:
            from ansible.playbook.base import Sentinel

            inherited = task._get_parent_attribute("ignore_errors", omit=True)
        except (ImportError, TypeError):  # Older cores use the keyword default.
            return result
        if inherited is not Sentinel and inherited is not None:
            result["ignore_errors_on_omit"] = _boolean_or_template(inherited)
    return result


def _boolean_or_template(value):
    """Defer validation and preserve the trust of keyword expressions."""
    return False if value is None else _tag_untrusted(value)


def _register_of(task):
    """Normalize 2.21's implicit register projection to the legacy var name."""
    value = getattr(task, "register", None)
    if not isinstance(value, dict):
        return str(value) if value else None
    if len(value) == 1:
        name, expression = next(iter(value.items()))
        if str(expression) == "_task.polymorphic_result":
            return str(name)
    raise ValueError(
        "ansiroll: explicit register projections are not supported yet: %r"
        % value
    )


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]
