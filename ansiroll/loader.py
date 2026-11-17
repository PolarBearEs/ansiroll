"""
Parse an Ansible playbook into Ansiroll's data model.

We reuse ansible-core's own parser (static imports/roles are already inlined by the
time we see the objects), then lower each Play/Block/Task into plain dicts that the
inlined runtime can interpret. This module runs only at compile time.
"""

from __future__ import annotations

import os
import re

from .compat import init_loader
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

_ITEM_RE = re.compile(r"{{\s*item\s*}}")
_JINJA_RE = re.compile(r"{{.*?}}")
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
        merged.update(loader.load_from_file(path) or {})
    return merged


def parse_playbook(path, vault_password_file=None):
    """Return (plays, actions) where actions is the set of module names to embed."""
    init_loader()
    from ansible.parsing.dataloader import DataLoader
    from ansible.inventory.manager import InventoryManager
    from ansible.vars.manager import VariableManager
    from ansible.playbook import Playbook

    loader = _make_rewriting_loader(DataLoader)
    secrets = _vault_secrets(vault_password_file)
    if secrets:
        loader.set_vault_secrets(secrets)
    inventory = InventoryManager(loader=loader, sources=[])
    vm = VariableManager(loader=loader, inventory=inventory)
    pb = Playbook.load(path, variable_manager=vm, loader=loader)

    pb_dir = os.path.abspath(getattr(pb, "_basedir", None)
                             or os.path.dirname(os.path.abspath(path)))
    plays = []
    actions = set()
    uuids = {}
    for play in pb.get_plays():
        # Like ansible-playbook (cli/playbook.py, executor/playbook_executor.py):
        # run each play with the loader's basedir set to the directory of the
        # playbook file that defined it, so role, include and src lookups are
        # independent of the compiler's working directory.
        basedir = os.path.abspath(getattr(play, "_included_path", None) or pb_dir)
        loader.set_basedir(basedir)
        node = _convert_play(play, actions, basedir)
        # The standalone file never maps back to ansible-core Task objects
        # (unlike the strategy plugin), so make its identifiers reproducible.
        _assign_stable_uuids(node["tasks"], node["handlers"], uuids)
        plays.append(node)
    return plays, actions


def _make_rewriting_loader(DataLoader):
    """A DataLoader that revives the removed legacy ``include:`` keyword.

    Modern ansible-core rejects ``include:`` outright. As files are read we rewrite
    it to ``import_tasks:`` (static, so it inlines) or ``include_tasks:`` when the
    task loops, so playbooks written for older ansible still compile.
    """

    class RewritingDataLoader(DataLoader):
        def load_from_file(self, *args, **kwargs):
            data = super(RewritingDataLoader, self).load_from_file(*args, **kwargs)
            return _rewrite_includes(data)

    return RewritingDataLoader()


def _rewrite_includes(data):
    # Rewrite in place: ansible-core's parsed containers carry origin tags
    # (task.get_path(), hence the file search path, comes from them), so they
    # must not be replaced by plain copies.
    if isinstance(data, list):
        for index, item in enumerate(data):
            data[index] = _rewrite_includes(item)
        return data
    if isinstance(data, dict):
        rename = "include" in data and isinstance(data.get("include"), str)
        if rename:
            dynamic = any(k == "loop" or k.startswith("with_") or k == "loop_with" for k in data)
            new_key = "include_tasks" if dynamic else "import_tasks"
            items = list(data.items())
            data.clear()
            for k, v in items:
                if k == "include":
                    data[new_key] = v
                else:
                    data[k] = _rewrite_includes(v)
        else:
            for k in list(data):
                data[k] = _rewrite_includes(data[k])
        return data
    return data


def _convert_play(play, actions, basedir=None, only_tags=None, skip_tags=None, all_vars=None):
    INCLUDED_TASKS.clear()
    _INCLUDE_STACK.clear()
    _INCLUDE_ROLE_PARENTS.clear()
    _INCLUDE_IDS[0] = 0
    if basedir is None:
        try:
            basedir = play._loader.get_basedir()
        except Exception:
            basedir = os.getcwd()
    hosts = play.hosts
    if isinstance(hosts, list):
        hosts = ",".join(str(h) for h in hosts)
    tasks = []
    for block in play.compile():
        n = _convert_node(block, actions, basedir, only_tags=only_tags,
                          skip_tags=skip_tags, all_vars=all_vars)
        if n:
            tasks.append(n)
    handlers = []
    for hblock in _handler_blocks(play):
        # No tag filtering: a notified handler runs regardless of --tags.
        _convert_node(hblock, actions, basedir, flatten_handler=True, sink=handlers,
                      all_vars=all_vars)
    handlers = _dedup_by_uuid(handlers)
    _assign_runtime_ids(tasks, handlers)
    # An unset gather_facts follows the configured gathering policy: implicit
    # and smart gather, explicit does not.
    gather = play.gather_facts
    if gather is None:
        try:
            from ansible import constants as C
            gather = C.DEFAULT_GATHERING != "explicit"
        except Exception:
            gather = True
    if gather:
        actions.add("ansible.builtin.setup")  # implicit fact gathering needs the module
    result = {
        "name": play.name or "",
        "hosts": hosts,
        "jinja_plugins": _collect_jinja_plugins(play, basedir),
        "gather_facts": bool(gather),
        "fact_schema": _fact_schema(),
        "environment": _as_list(getattr(play, "environment", None)) or None,
        "check_mode": getattr(play, "check_mode", None),
        "become": bool(play.become),
        "become_user": getattr(play, "become_user", None),
        "vars": _tag_untrusted(_resolved_play_vars(play, all_vars, basedir)),
        "tasks": tasks,
        "handlers": handlers,
    }
    names = _controller_env_names(result)
    if names:
        result["controller_env"] = {
            name: os.environ[name] for name in sorted(names) if name in os.environ
        }
    config_names = _controller_config_names(result)
    if config_names:
        try:
            from ansible import constants as C
            result["controller_config"] = {
                name: C.config.get_config_value(name)
                for name in sorted(config_names)
            }
        except Exception:
            pass
    return result


def _controller_env_names(value):
    """Find literal controller-side env lookups that the target must reproduce."""
    names = set()
    if isinstance(value, str):
        names.update(_ENV_LOOKUP_RE.findall(value))
    elif isinstance(value, dict):
        for key, item in value.items():
            names.update(_controller_env_names(key))
            names.update(_controller_env_names(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            names.update(_controller_env_names(item))
    return names


def _controller_config_names(value):
    """Find literal config lookups whose controller values must be preserved."""
    names = set()
    if isinstance(value, str):
        names.update(_CONFIG_LOOKUP_RE.findall(value))
    elif isinstance(value, dict):
        for key, item in value.items():
            names.update(_controller_config_names(key))
            names.update(_controller_config_names(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            names.update(_controller_config_names(item))
    return names


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


def _resolved_play_vars(play, all_vars, basedir):
    """Merge static vars_files at their play-var precedence."""
    out = dict(play.vars or {})
    for entry in (_as_list(getattr(play, "vars_files", None)) or []):
        if not isinstance(entry, str):
            continue
        entry = re.sub(
            r"{{\s*playbook_dir\s*}}", basedir, entry
        )
        if "{{" in entry or "{%" in entry:
            continue
        path = entry if os.path.isabs(entry) else os.path.join(basedir, entry)
        try:
            loaded = play._loader.load_from_file(path, trusted_as_template=True) or {}
        except TypeError:
            loaded = play._loader.load_from_file(path) or {}
        except Exception:
            continue
        if isinstance(loaded, dict):
            out.update(loaded)
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


def _tag_untrusted(value):
    """Mark strings that are explicitly not trusted (for example `!unsafe`)."""
    try:
        from ansible._internal._datatag._tags import TrustedAsTemplate
    except ImportError:
        return value

    def walk(v):
        if isinstance(v, str) and not TrustedAsTemplate.is_tagged_on(v):
            return {"__ansiroll_unsafe__": str(v)}
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
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
            for key in ("block", "rescue", "always", "tasks"):
                if node.get(key):
                    walk(node[key])

    walk(tasks)
    walk(handlers)


def _dedup_by_uuid(nodes):
    seen = set()
    out = []
    for n in nodes:
        uid = n.get("uuid")
        if uid in seen:
            continue
        seen.add(uid)
        out.append(n)
    return out


def _handler_blocks(play):
    blocks = list(play.handlers or [])
    compile_roles = getattr(play, "compile_roles_handlers", None)
    if compile_roles:
        try:
            blocks = list(compile_roles()) + blocks
        except Exception:
            pass
    return blocks


def _convert_node(node, actions, basedir, flatten_handler=False, sink=None,
                  only_tags=None, skip_tags=None, all_vars=None):
    from ansible.playbook.block import Block
    from ansible.playbook.task import Task

    if isinstance(node, Block):
        if flatten_handler and sink is not None:
            # Handlers are flat; emit each contained task into the sink.
            for sub in (node.block or []):
                conv = _convert_node(sub, actions, basedir, only_tags=only_tags,
                                     skip_tags=skip_tags, all_vars=all_vars)
                if conv:
                    sink.append(conv)
            return None
        block_node = {
            "type": "block",
            "when": _conditions(node.when),
            "become": bool(node.become),
            "vars": _tag_untrusted(dict(node.vars or {})),
            "block": [c for c in (_convert_node(s, actions, basedir, only_tags=only_tags,
                                                skip_tags=skip_tags, all_vars=all_vars)
                                    for s in (node.block or [])) if c],
            "rescue": [c for c in (_convert_node(s, actions, basedir, only_tags=only_tags,
                                                 skip_tags=skip_tags, all_vars=all_vars)
                                     for s in (node.rescue or [])) if c],
            "always": [c for c in (_convert_node(s, actions, basedir, only_tags=only_tags,
                                                 skip_tags=skip_tags, all_vars=all_vars)
                                     for s in (node.always or [])) if c],
        }
        if not block_node["block"] and not block_node["rescue"] and not block_node["always"]:
            return None
        return block_node

    if isinstance(node, Task):
        if not _matches_tags(node, only_tags, skip_tags, all_vars):
            return None
        if node.action in ("include_tasks", "ansible.builtin.include_tasks"):
            inc = _convert_include_tasks(node, actions, basedir, only_tags,
                                         skip_tags, all_vars)
            if inc is not None:
                return inc
        if node.action in ("include_role", "ansible.builtin.include_role"):
            inc = _convert_include_role(node, actions, basedir, only_tags,
                                        skip_tags, all_vars)
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


def _include_ref(key):
    """Return the include_id of an in-progress expansion of the same content."""
    return _INCLUDE_STACK.get(key)


def _new_include_id():
    _INCLUDE_IDS[0] += 1
    return "include-%d" % _INCLUDE_IDS[0]


def _convert_include_tasks(task, actions, basedir, only_tags, skip_tags, all_vars):
    """Expand a compile-time-resolvable include_tasks.

    Filenames depending on runtime values (such as a loop item or a registered
    result) remain on the controller-coupled path.
    """
    args = task.args or {}
    fname = args.get("_raw_params") or args.get("file")
    if not isinstance(fname, str):
        return None
    if "{{" in fname or "{%" in fname:
        fname = _template_include_filename(task, fname, all_vars)
        if not isinstance(fname, str) or "{{" in fname or "{%" in fname:
            return None
    path = _include_tasks_path(task, fname, all_vars)
    if path is None or not os.path.exists(path):
        return None
    role = getattr(task, "_role", None)
    physical_path = os.path.realpath(path)
    key = ("tasks", physical_path, path, id(role) if role is not None else None,
           _is_handler(task), _include_search_context(task), _include_scope_key(task))
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
            blocks = load_list_of_blocks(
                data, play=_play_of(task), task_include=task, role=task._role,
                variable_manager=task.get_variable_manager(), loader=task._loader)
        except Exception:
            return None
        _register_included(blocks)
        include_id = _new_include_id()
        _INCLUDE_STACK[key] = include_id
        try:
            tasks = [c for c in (_convert_node(b, actions, basedir, only_tags=only_tags,
                                               skip_tags=skip_tags, all_vars=all_vars)
                                 for b in blocks) if c]
        finally:
            _INCLUDE_STACK.pop(key, None)
    role_defaults, role_vars = _role_vars(task)
    node = _include_node(task, "include_tasks", fname, basedir,
                         role_defaults, role_vars, tasks, include_id, ref)
    return node


def _include_node(task, action, fname, basedir, role_defaults, role_vars,
                  tasks, include_id, ref):
    node = {
        "type": "include",
        "uuid": task._uuid,
        "name": task.name or action,
        "action": action,
        "file": fname,
        "when": _as_list(task.when),
        "loop": _loop_of(task, basedir),
        "loop_var": _loop_var(task),
        "index_var": _index_var(task),
        **_loop_control(task),
        "vars": _tag_untrusted(dict(task.vars or {})),
        "role_defaults": role_defaults,
        "role_vars": role_vars,
        "become": bool(task.become),
        "tasks": tasks,
    }
    if action == "include_role" and tuple(map(int, _fact_schema().split("."))) < (2, 17):
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


def _include_search_context(task):
    # Descendants search using this include's argument followed by its ancestors'
    # arguments. Preserve both the relative prefix (which a child can extend)
    # and the ordered paths after the first absolute reset.
    paths = list(_parent_include_paths(task))
    if any(dynamic for _, dynamic in paths):
        # A descendant can override variables in an ancestor's filename.
        # Preserve those templates and their positions in the search.
        context = []
        for path, dynamic in paths:
            entry = (path, dynamic)
            if not dynamic:
                if not path and context[-1:] == [entry]:
                    continue
                # An absolute parent resets the cumulative path. The first
                # occurrence can still consume a preceding relative suffix.
                if os.path.isabs(path) and context[-2:] == [entry, entry]:
                    continue
            context.append(entry)
        return tuple(context)

    relative, remaining = {}, {}
    cumulative_path = None
    candidates = relative
    for path, _ in paths:
        cumulative_path = _next_include_dir(path, cumulative_path)
        candidates[cumulative_path] = None
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


def _include_tasks_path(task, include_target, all_vars):
    """Resolve an include_tasks file like IncludedFile.process_include_results
    (ansible/playbook/included_file.py) does for the controller."""
    loader = task._loader
    role = getattr(task, "_role", None)
    dirname = "handlers" if _is_handler(task) else "tasks"
    include_file = None
    cumulative_path = None
    for parent_path, dynamic in _parent_include_paths(_include_parent(task)):
        if dynamic:
            rendered = _template_include_filename(task, parent_path, all_vars)
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


def _template_include_filename(task, value, all_vars):
    """Resolve an include filename using Ansible's own templar and precedence."""
    role_defaults, role_vars = _role_vars(task)
    variables = {}
    variables.update(role_defaults)
    play = _play_of(task)
    if play is not None:
        variables.update(play.vars or {})
    variables.update(all_vars or {})
    variables.update(role_vars)
    for scope in _include_scope(task):
        variables.update(scope)
    try:
        from ansible.template import Templar
        try:
            templar = Templar(loader=task._loader, variables=variables)
        except TypeError:
            templar = Templar(loader=task._loader)
            templar.available_variables = variables
        return templar.template(value, fail_on_undefined=True)
    except Exception:
        return None


def _convert_include_role(task, actions, basedir, only_tags, skip_tags, all_vars):
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
    if ref is None:
        try:
            role = Role.load(ri, play, parent_role=parent_role,
                             from_files=from_files or None, from_include=True)
            blocks = role.compile(play)
        except Exception:
            return None
        _register_included(blocks)
        task._role_path = role._role_path
        role_roots = [id(block) for block in blocks if getattr(block, "_parent", None) is None]
        _INCLUDE_ROLE_PARENTS.update(dict.fromkeys(role_roots, task))
        include_id = _new_include_id()
        # Keep the guard while converting the role's tasks: that is where a
        # recursive include_role is reached.
        _INCLUDE_STACK[key] = include_id
        try:
            tasks = [c for c in (_convert_node(b, actions, basedir, only_tags=only_tags,
                                               skip_tags=skip_tags, all_vars=all_vars)
                                 for b in blocks) if c]
        finally:
            _INCLUDE_STACK.pop(key, None)
            for root in role_roots:
                del _INCLUDE_ROLE_PARENTS[root]
    role_defaults, role_vars = _role_vars(task)
    return _include_node(task, "include_role", name, basedir,
                         role_defaults, role_vars, tasks, include_id, ref)


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

    def walk(obj):
        if isinstance(obj, Block):
            for section in (obj.block, obj.rescue, obj.always):
                for sub in (section or []):
                    walk(sub)
        elif isinstance(obj, Task):
            INCLUDED_TASKS[obj._uuid] = obj

    for b in blocks:
        walk(b)


def _matches_tags(task, only_tags=None, skip_tags=None, all_vars=None):
    if not only_tags and not skip_tags:
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
                    "action": "meta", "special": "meta_flush_handlers"}
        if "clear_host_errors" in raw:
            return {"type": "task", "uuid": task._uuid, "name": "Clear host errors",
                    "action": "meta", "special": "meta_clear_host_errors"}
        return None  # other meta actions are no-ops in v1

    role_defaults, role_vars = _role_vars(task)

    args = _clean_args(task.args)
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
        "retries": _numeric_or_template(getattr(task, "retries", 0), int),
        "delay": _numeric_or_template(getattr(task, "delay", 0), float),
        "environment": _as_list(getattr(task, "environment", None)) or None,
        "search_dirs": _search_dirs(task, basedir, "files"),
        "search_path": _raw_search_path(task, basedir),
        "ignore_errors": _boolean_or_template(task.ignore_errors),
        "no_log": bool(getattr(task, "no_log", False)),
        "async_val": int(getattr(task, "async_val", 0) or 0),
        "poll": int(getattr(task, "poll", 0) or 0),
        "run_once": getattr(task, "run_once", None),
        # None inherits the invocation's --check setting; explicit values
        # (including templates) override it for this task.
        "check_mode": getattr(task, "check_mode", None),
        "become": bool(task.become),
        "become_user": getattr(task, "become_user", None),
        "vars": _tag_untrusted(dict(task.vars or {})),
        "role_defaults": role_defaults,  # lowest precedence (below play vars)
        "role_vars": role_vars,          # above play vars, below block/task vars
        "role_magic": _role_magic(task),
        "notify": _as_list(task.notify),
        "special": _special_of(action),
    }
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

    if action in BAKED_ACTIONS:
        _bake_sources(node, task, basedir)
        if node["special"] is None:
            actions.add(action)
        elif node["special"] == "copy" and _remote_src(node["args"]) is not False:
            # remote_src copies run the copy module on the target, like the
            # copy action plugin's remote_src path.
            actions.add(action)
    elif action in CONTROLLER_COUPLED:
        node["fallback"] = True
        node["fallback_reason"] = "controller-coupled action %s" % action
    elif node["special"] is None:
        actions.add(action)
    return node


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


def _accelerator_options(action):
    name = str(action or "").split(".")[-1]
    try:
        from ansible.release import __version__
        match = re.match(r"^(\d+)\.(\d+)", __version__)
        version = tuple(map(int, match.groups())) if match else (0, 0)
        if name == "stat" and version >= (2, 21):
            return {"disk_usage_bytes": True}
        if name in ("command", "shell"):
            options = {}
            if version < (2, 12):
                options["check_always_skipped"] = True
            if version >= (2, 16):
                options["expand_argument_vars"] = True
            if version >= (2, 14):
                options["check_skip_unchanged"] = True
            if version >= (2, 21):
                options["verbose_nonzero"] = True
            return options or None
        if name == "find" and version >= (2, 12):
            options = {
                "all_paths_examined": True,
                "structured_skipped_paths": True,
            }
            if version >= (2, 21):
                options["disk_usage_bytes"] = True
            if version >= (2, 19):
                options["typed_warnings"] = True
            return options
        if name == "slurp":
            if version < (2, 19):
                return {"lowercase_not_found": True}
            return {"typed_exceptions": True}
        if name == "replace":
            options = {}
            if version < (2, 20):
                options["binary_io"] = True
            if version < (2, 14):
                options["omit_result_rc"] = True
            return options or None
        if name == "user" and version >= (2, 21):
            return {"result_memberships": True}
        if name in ("systemd", "systemd_service") and version >= (2, 14):
            return {"modern_is_enabled": True}
    except Exception:
        pass
    return None


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
    remote = _remote_src(args) if name in ("copy", "unarchive") else False
    if remote is True:
        return  # src lives on the target; the module reads it there

    subdir = "templates" if name == "template" else "files"

    candidates = []
    if _JINJA_RE.search(src):
        loop = node.get("loop")
        if isinstance(loop, list) and _only_item(src):
            for it in loop:
                candidates.append(_ITEM_RE.sub(str(it), src))
        elif _bake_role_tree(node, task, subdir):
            # src is dynamic (e.g. "services/{{ item.name }}.service.j2") and the
            # loop is a runtime var; bake the whole role tree so the runtime can
            # resolve whichever file the rendered src points to.
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
                return  # may be a target path if remote_src renders true
            node["fallback"] = True
            node["fallback_reason"] = "baked src not found: %s" % rel
            return
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as fh:
            baked[rel] = fh.read()
        if name != "template":
            continue
        # Only template exposes source metadata (template_fullpath,
        # template_mtime, template_uid); copy/unarchive never read it, so it
        # is not embedded for them.
        try:
            st = os.stat(path)
            meta[rel] = {"fullpath": os.path.abspath(path),
                         "mtime": st.st_mtime, "uid": st.st_uid}
        except OSError:
            pass
    node["_baked"] = baked
    if meta:
        node["_baked_meta"] = meta


def _remote_src(args):
    """remote_src with Ansible's boolean(strict=False) conversion (as the copy
    and unarchive action plugins do); None when only known at runtime."""
    value = args.get("remote_src", False)
    if _contains_dynamic_value(value):
        return None
    try:
        from ansible.module_utils.parsing.convert_bool import boolean
        return boolean(value, strict=False)
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


def _bake_role_tree(node, task, subdir):
    """Bake every file under the role's templates/ (or files/) dir, keyed by path
    relative to that dir, so a dynamic src like "services/{{ item.name }}.service.j2"
    resolves at runtime. Returns True if anything was baked."""
    role = getattr(task, "_role", None)
    role_path = getattr(role, "_role_path", None) if role else None
    if not role_path:
        return False
    tree_dir = os.path.join(role_path, subdir)
    if not os.path.isdir(tree_dir):
        return False
    baked = {}
    for root, dirs, files in os.walk(tree_dir):
        dirs.sort()
        for fn in sorted(files):
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, tree_dir)
            try:
                with open(full, "r", encoding="utf-8", errors="surrogateescape") as fh:
                    baked[rel] = fh.read()
            except OSError:
                continue
    if not baked:
        return False
    node["_baked"] = baked
    node["_baked_tree"] = subdir
    return True


def _raw_search_path(task, basedir):
    """The ansible_search_path magic var: base dirs without subdir expansion."""
    try:
        stack = list(task.get_search_path() or [])
    except Exception:
        stack = []
    return stack or [basedir]


def _search_dirs(task, basedir, subdir):
    """Directories DataLoader.path_dwim_relative_stack searches, in order, for
    a file under ``subdir`` (ansible/parsing/dataloader.py) as lookup plugins
    call it: each task search path (role-aware), then the loader basedir."""
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
            dirs.append(os.path.join(os.path.dirname(pb_base_dir), subdir))
            dirs.append(pb_base_dir)
        dirs.append(os.path.join(upath, subdir))
        dirs.append(upath)
    try:
        base = loader.get_basedir()
    except Exception:
        base = basedir
    dirs.append(os.path.join(base, subdir))
    dirs.append(base)
    return dirs


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
        defaults = dict(role.get_default_vars() or {})
    except Exception:
        pass
    try:
        # Dependency roles inherit the including role's parameters, while
        # their own vars still take precedence over the parent's role vars.
        for parent in (getattr(role, "_parents", None) or []):
            rvars.update(parent.get_vars(include_params=True) or {})
        rvars.update(role.get_vars(include_params=True) or {})
    except Exception:
        pass
    return defaults, rvars


def _clean_args(args):
    if not isinstance(args, dict):
        return {}
    return dict(args)


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
    if getattr(lc, "extended", None):
        out["loop_extended"] = True
        out["loop_extended_allitems"] = getattr(lc, "extended_allitems", True) is not False
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


def _boolean_or_template(value):
    """Preserve task-keyword expressions instead of coercing strings eagerly."""
    return value if isinstance(value, str) else bool(value)


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
