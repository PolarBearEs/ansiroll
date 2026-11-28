"""
ansiroll strategy plugin.

Instead of Ansible's per-task round-trips, this strategy compiles the whole play
into one self-contained Python "agent" (via the ``ansiroll`` package) and runs it
on each host over a single persistent bidirectional channel:

  * the agent executes the whole play in one long-lived process (module worker stays
    warm -- module_utils compiled once, decode-once), and
  * streams one framed result per task back to the controller **as it happens**, so
    reporting is live and looks exactly like a normal run, and
  * when it hits a ``delegate_to`` / controller-coupled task it asks the controller
    inline over the same channel (no re-ship, no segmentation).

Transport reuses Ansible's connection plugin: ``local`` runs python directly;
``ssh`` reuses the ssh plugin's own argv (keys/args/ControlPersist/sshpass).

Requires the ``ansiroll`` package to be importable on the controller.
"""

from __future__ import annotations

import collections
import base64
import json
import inspect
import os
import re
import shlex
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, MappingView, Set

DOCUMENTATION = """
    name: ansiroll
    short_description: Compile the play to one file, ship it, run it, report normally
    description:
        - Compiles the entire play (with roles/includes inlined and modules embedded)
          into a single self-contained Python file, transfers it to each host once,
          executes it, and reconstructs per-task results for normal reporting.
    version_added: "0.1.0"
    author: PolarBearEs
"""

from ansible import constants as C, context
from ansible.errors import AnsibleError
from ansible.plugins.strategy import StrategyBase
from ansible.plugins.strategy.linear import StrategyModule as _LinearStrategy
from ansible.plugins.loader import connection_loader
from ansible.utils.display import Display
from jinja2 import StrictUndefined

_UnifiedTaskResult = None
try:  # 2.18-2.20: callbacks accept the private raw result wrapper
    from ansible.executor.task_result import _RawTaskResult as _ResultClass
except Exception:  # 2.21+ uses UnifiedTaskResult; older cores use TaskResult
    from ansible.executor.task_result import TaskResult as _ResultClass
    try:
        from ansible._internal._task import UnifiedTaskResult as _UnifiedTaskResult
    except Exception:  # pragma: no cover - older ansible-core
        pass

display = Display()


class _DelegatedPlayVariables:
    """Cache the agent's values for one controller task invocation."""

    def __init__(self, load):
        self._load = load
        self.names = []
        self.values = {}

    def __getitem__(self, index):
        if index not in self.values:
            self.values[index] = self._load(self.names[index])
        return self.values[index]


class _LegacyDelegatedValue:
    """Let Jinja load the native value when an expression uses the alias."""

    __UNSAFE__ = True

    def __init__(self, bridge, index):
        self._bridge = bridge
        self._index = index

    def resolve(self):
        return self._bridge[self._index]


class _DelegatedMemberError(StrictUndefined):
    __UNSAFE__ = True

    def __repr__(self):
        self._fail_with_undefined_error()


def _wrap_legacy_rescue_value(value):
    from ansible.utils.unsafe_proxy import wrap_var

    def wrap(member):
        # Legacy wrap_var rebuilds ranges from a generator, which range does not accept.
        if isinstance(member, range):
            return member
        if isinstance(member, Mapping):
            return {wrap(key): wrap(item) for key, item in member.items()}
        if isinstance(member, (list, tuple)):
            return type(member)(wrap(item) for item in member)
        if isinstance(member, Set):
            return {wrap(item) for item in member}
        return wrap_var(member)

    return wrap(value)


def _legacy_environment_validator(validate, aliases):
    """Keep native Ansible environment validation, with lazy Jinja name binding."""
    from ansible.errors import AnsibleUndefinedVariable
    from jinja2 import Undefined
    from jinja2.runtime import missing

    from ansiroll.runtime import _ConditionalCodeGenerator, _ConditionalName

    class DelegatedCodeGenerator(_ConditionalCodeGenerator):
        def visit_Filter(self, node, frame):
            self.write("context._check_delegated_member(")
            super().visit_Filter(node, frame)
            self.write(")")

    def check_error(member):
        if isinstance(member, _DelegatedMemberError):
            member._fail_with_undefined_error()
        if type(member) is list and any(isinstance(item, _DelegatedMemberError) for item in member):
            return CheckedList(member)
        return member

    class CheckedList(list):
        def __iter__(self):
            for member in super().__iter__():
                yield check_error(member)

        def __getitem__(self, index):
            return check_error(super().__getitem__(index))

        def __add__(self, other):
            return check_error(super().__add__(other))

        def __radd__(self, other):
            if not isinstance(other, list):
                return NotImplemented
            return check_error(list.__add__(other, self))

        def __mul__(self, count):
            return check_error(super().__mul__(count))

        __rmul__ = __mul__

    class CheckedView(MappingView):
        def __init__(self, view, items):
            super().__init__(view)
            self.items = items

        def __iter__(self):
            for member in self._mapping:
                if self.items:
                    key, value = member
                    yield key, check_error(value)
                else:
                    yield check_error(member)

        def __contains__(self, member):
            if self.items:
                # dict_items looks up only the requested key before comparing
                # its value. Iterating would trip errors on unrelated keys.
                return member in self._mapping
            return any(value == member for value in self)

    def check_member(access):
        def get_member(obj, key):
            return check_error(access(obj, key))
        return get_member

    def validate_environment(attr, value, templar):
        environment = getattr(templar, "_environment", None)
        if environment is None:
            environment = templar.environment
        variables = templar.available_variables
        template = templar.template
        instance_template = vars(templar).get("template")
        generator, context_class = environment.code_generator_class, environment.context_class
        accessors = {name: vars(environment).get(name) for name in ("getitem", "getattr")}

        class DelegatedContext(context_class):
            _check_delegated_member = staticmethod(check_error)

            def _defer_name(self, key):
                return _ConditionalName(self, key) if key in self else missing

            def _consume_name(self, value):
                if isinstance(value, _ConditionalName):
                    value = value.context.resolve_or_missing(value.key)
                if isinstance(value, _LegacyDelegatedValue):
                    value = value.resolve()
                    self._update_unsafe(value)
                return check_error(value)

            def call(self, *args, **kwargs):
                result = check_error(super().call(*args, **kwargs))
                if isinstance(result, (type({}.values()), type({}.items()))):
                    items = isinstance(result, type({}.items()))
                    if any(isinstance(member[1] if items else member, _DelegatedMemberError) for member in result):
                        return CheckedView(result, items)
                return result

        def template_variable(value, *args, **kwargs):
            # Legacy lookup plugins can read available_variables directly.
            if isinstance(value, _LegacyDelegatedValue):
                value = value.resolve()
                if isinstance(value, Undefined) and kwargs.get("fail_on_undefined"):
                    raise AnsibleUndefinedVariable(value._undefined_message)
                return value
            return template(value, *args, **kwargs)

        try:
            templar.template = template_variable
            templar.available_variables = {**variables, **aliases}
            environment.context_class = DelegatedContext
            environment.code_generator_class = DelegatedCodeGenerator
            for name in accessors:
                setattr(environment, name, check_member(getattr(environment, name)))
            return validate(attr, value, templar)
        finally:
            environment.context_class = context_class
            environment.code_generator_class = generator
            for name, accessor in accessors.items():
                if accessor is None:
                    vars(environment).pop(name, None)
                else:
                    setattr(environment, name, accessor)
            if instance_template is None:
                del templar.template
            else:
                templar.template = instance_template
            templar.available_variables = variables

    return validate_environment


class StrategyModule(StrategyBase):

    def run(self, iterator, play_context):
        play = iterator._play
        self._cur_play = play
        self._set_hosts_cache(play)
        self._profile_enabled = _truthy(os.environ.get("ANSIROLL_PROFILE"))
        self._native_only = _truthy(os.environ.get("ANSIROLL_NATIVE_ONLY"))
        self._provenance_enabled = _truthy(
            os.environ.get("ANSIROLL_PROVENANCE")
        )
        accelerated_modules = os.environ.get("ANSIROLL_ACCELERATED_MODULES", "")
        self._profile("accelerated_modules=%s" % (accelerated_modules or "none"))
        play_vars = self._variable_manager.get_vars(play=play)
        started = time.monotonic()
        failed_hosts = iterator.get_failed_hosts()
        hosts = [host for host in self.get_hosts_left(iterator) if host.name not in failed_hosts]
        gather_task = next(
            (task for task in iterator._blocks[0].block
             if task.action == "gather_facts"), None)
        gather_hosts = {
            host.name for host in hosts
            if gather_task is not None and iterator.get_next_task_for_host(host, peek=True)[1] is gather_task
        }
        try:
            source, modules, task_map = self._compile(
                play, play_vars, gather_task if gather_hosts else None)
        except Exception as ex:
            display.error("ansiroll: failed to compile play: %s" % ex)
            return self._tqm.RUN_ERROR
        self._profile("compile %.3fs, agent %.1f MiB, modules=%d, tasks=%d"
                      % (time.monotonic() - started, len(source) / 1048576.0,
                         len(modules), len(task_map)))

        self._delegate_on_target = _truthy(play_vars.get("ansiroll_delegate_on_target"))
        self._native_only = self._native_only or _truthy(
            play_vars.get("ansiroll_native_only")
        )
        self._provenance_enabled = self._provenance_enabled or _truthy(
            play_vars.get("ansiroll_provenance")
        )
        self._check_mode = bool(
            context.CLIARGS.get("check") or getattr(play_context, "check_mode", False)
        )
        self._diff_mode = bool(context.CLIARGS.get("diff"))
        self._modules = modules
        # A tag-filtered or otherwise empty play has no controller work.  In
        # particular, do not let all([]) route a remote inventory host through
        # the local channel (and consequently the controller's Python path).
        self._controller_only_play = bool(task_map) and all(
            str(getattr(task, "action", "") or "").rsplit(".", 1)[-1]
            in {"debug", "set_fact", "fail", "assert", "meta"}
            for task in task_map.values()
        )
        self._requires_linear_fallback = any(
            (
                str(getattr(task, "action", "") or "").rsplit(".", 1)[-1]
                in {"include_role", "import_role"}
            )
            or "{{" in str(getattr(task, "action", "") or "")
            or "{%" in str(getattr(task, "action", "") or "")
            for task in task_map.values()
        )
        self._local_rt = None

        controller_reasons = list(getattr(
            self, "_compiled_controller_fallbacks", ()
        ))
        if self._delegate_on_target:
            controller_reasons = [
                item for item in controller_reasons
                if item.get("kind") != "delegate"
            ]
        delegated_fact_tasks = [
            task
            for task in task_map.values()
            if str(getattr(task, "action", "") or "").rsplit(".", 1)[-1] == "set_fact"
            and _has_delegated_fact_flag(task)
        ]
        linear_reason = None
        if any(item["kind"] == "vars-file-include" for item in controller_reasons):
            self._requires_linear_fallback = True
            linear_reason = "vars-file task selection requires core linear"
        elif any(item["kind"] == "vars-file-handler" for item in controller_reasons):
            linear_reason = "templated handler names with dynamic vars files require core linear"
        elif self._requires_linear_fallback:
            linear_reason = "dynamic role or templated action requires core linear"
        elif any(item["kind"] == "vault-source" for item in controller_reasons):
            linear_reason = "Vault source selection requires core linear"
        elif any(item["kind"] == "file-source-arguments" for item in controller_reasons):
            linear_reason = "templated file source arguments require core linear"
        elif any(item["kind"] == "apply-include" for item in controller_reasons):
            linear_reason = "include_tasks with runtime apply variables requires core linear"
        elif not self._delegate_on_target and any(
                _delegated_fact_flag(task.delegate_facts) is not False for task in delegated_fact_tasks):
            linear_reason = "delegated set_fact requires task-lockstep scheduling"
        elif len(hosts) > 1 and self._controller_only_play:
            linear_reason = (
                "multi-host controller-only play requires task-lockstep scheduling"
            )
        if self._native_only and (linear_reason or controller_reasons):
            reasons = [linear_reason] if linear_reason else []
            reasons.extend(
                item.get("reason") or item.get("kind")
                for item in controller_reasons
            )
            display.error(
                "ansiroll: native-only mode rejected play %r: %s"
                % (play.name or play.get_name(), "; ".join(reasons))
            )
            return self._tqm.RUN_ERROR
        if linear_reason:
            # There is no target work to accelerate, while run_once, rescue,
            # hostvars and batch membership still require task-lockstep
            # scheduling. Reuse core linear for this compatibility-only case.
            display.vvv(
                "ansiroll: using core linear scheduling for a "
                "compatibility-only play"
            )
            self._emit_provenance({
                "scope": "play",
                "play": play.name or play.get_name(),
                "mode": "linear-fallback",
                "reason": linear_reason,
            })
            # Keep this instance's result thread and queue caches; constructing
            # a second strategy would make both consumers race on the TQM queue.
            # ansible-core 2.14's linear strategy adds this state in its own
            # constructor, which this deliberately reused instance never runs.
            if not hasattr(self, "_in_handlers"):
                self._in_handlers = False
            # Linear still needs the raw flag to identify quoted false values.
            # Dynamic roles can introduce delegated set_fact after compilation.
            routed_facts = self._requires_linear_fallback or bool(delegated_fact_tasks)
            self.__class__ = _SetFactLinearStrategy if routed_facts else _LinearStrategy
            return self.run(iterator, play_context)
        self._emit_provenance({
            "scope": "play",
            "play": play.name or play.get_name(),
            "mode": "native-agent",
        })
        if len(hosts) > 1 and _truthy(getattr(play, "any_errors_fatal", False)):
            display.error(
                "ansiroll: multi-host any_errors_fatal requires task-lockstep "
                "execution and is not supported; no hosts were started"
            )
            return self._tqm.RUN_ERROR
        if len(hosts) > 1 and getattr(play, "max_fail_percentage", None) is not None:
            display.error(
                "ansiroll: multi-host max_fail_percentage requires task-lockstep "
                "execution and is not supported; no hosts were started"
            )
            return self._tqm.RUN_ERROR

        result = self._tqm.RUN_OK
        try:
            self._run_hosts = hosts
            self._first_host_name = hosts[0].name if hosts else None
            for host in hosts:
                if self._run_host(host, source, task_map, play_context,
                                  host.name in gather_hosts):
                    iterator.mark_host_failed(host)
                    result = self._tqm.RUN_FAILED_HOSTS
                self._drain_iterator(host, iterator)
        finally:
            self._close_local_runtime()
        return super(StrategyModule, self).run(iterator, play_context, result)

    # ---- compilation -----------------------------------------------------
    def _compile(self, play, play_vars, gather_task):
        from ansiroll.loader import _conditions, _convert_play
        from ansiroll.embed import ModuleEmbedder, SPECIAL_ACTIONS
        from ansiroll.compiler import _render_source

        actions = set()
        only_tags = set(context.CLIARGS.get("tags") or [])
        skip_tags = set(context.CLIARGS.get("skip_tags") or [])
        play_node = _convert_play(play, actions, only_tags=only_tags,
                                  skip_tags=skip_tags, all_vars=play_vars,
                                  gather_facts=gather_task is not None)
        if gather_task is not None:
            play_node["gather_when"] = _conditions(gather_task.when)
            play_node["gather_no_log"] = gather_task.no_log
            if "gather_timeout" in (play.get_ds() or {}):
                play_node["gather_timeout"] = gather_task.args.get("gather_timeout")

        embedder = ModuleEmbedder()
        modules = {}
        unsupported = set()
        for action in sorted(actions):
            if action in SPECIAL_ACTIONS:
                continue
            try:
                meta = embedder.embed(action)
            except Exception as ex:
                display.warning("ansiroll: cannot embed module %s (%s); "
                                "running it on the controller" % (action, ex))
                unsupported.add(action)
                continue
            if meta is not None:
                modules[action] = meta
        if unsupported:
            from ansiroll.compiler import _mark_unsupported
            _mark_unsupported([play_node], unsupported)
        self._compiled_controller_fallbacks = _controller_fallbacks(play_node)
        self._deferred_vars_file_keys = (
            set(play.vars or {}) if play_node.get("controller_vars_files") else None
        )

        self._ansiballz = embedder.ansiballz_code or ""
        source = _render_source([play_node], modules, self._ansiballz)
        task_map = self._build_task_map(play, play_node)
        if gather_task is not None:
            task_map["__gather_facts__"] = gather_task
        return source, modules, task_map

    def _build_task_map(self, play, play_node):
        from ansible.playbook.block import Block
        from ansible.playbook.task import Task

        by_key = collections.defaultdict(collections.deque)
        by_uuid = collections.defaultdict(collections.deque)
        mapping = {}

        def walk(obj):
            if isinstance(obj, Block):
                for section in (obj.block, obj.rescue, obj.always):
                    for sub in (section or []):
                        walk(sub)
            elif isinstance(obj, Task):
                by_uuid[obj._uuid].append(obj)
                by_key[(obj._uuid, obj.name or obj.action, obj.action)].append(obj)

        for blk in play.compile():
            walk(blk)
        for hblk in (play.handlers or []):
            walk(hblk)
        # Tasks materialized by compile-time include_tasks expansion are not in
        # play.compile(); the loader keeps them in a registry.
        from ansiroll import loader as _ldr
        for t in getattr(_ldr, "INCLUDED_TASKS", {}).values():
            walk(t)

        def walk_node(node):
            if node.get("type") == "block":
                for section in ("block", "rescue", "always"):
                    for sub in node.get(section, []) or []:
                        walk_node(sub)
                return
            if node.get("type") == "include":
                for sub in node.get("tasks", []) or []:
                    walk_node(sub)
                for sub in node.get("handlers", []) or []:
                    walk_node(sub)
            rid = node.get("runtime_id")
            if not rid:
                return
            key = (node.get("uuid"), node.get("name") or node.get("action"),
                   node.get("action"))
            queue = by_key.get(key)
            if queue:
                mapping[rid] = queue.popleft()
                return
            queue = by_uuid.get(node.get("uuid"))
            if queue:
                mapping[rid] = queue.popleft()

        for node in play_node.get("tasks", []) or []:
            walk_node(node)
        for node in play_node.get("handlers", []) or []:
            walk_node(node)
        return mapping

    # ---- per-host execution over a persistent bidirectional channel ------
    def _run_host(self, host, source, task_map, play_context, gather_facts):
        """Ship the agent once, then pump: results stream live, delegated tasks are
        serviced inline on the controller, all over one persistent channel."""
        started = time.monotonic()
        hv, selected_vars_files = self._host_vars_and_lookup_sources(host)
        hv.setdefault("ansible_host", host.name)
        templar = self._mk_templar(hv)
        become_pass = self._resolve_become_password(hv, templar)
        agent_source = (
            self._agent_header(host, hv, become_pass)
            + self._controller_lookup_header(hv, selected_vars_files)
            + "_INJECTED_GATHER_FACTS = %r\n" % gather_facts
            + source
        )
        self._profile("%s prepared vars/header in %.3fs, payload %.1f MiB"
                      % (host.name, time.monotonic() - started,
                         len(agent_source) / 1048576.0))
        try:
            open_started = time.monotonic()
            chan = self._open_channel(host, play_context, hv, templar)
        except Exception as ex:
            display.error("ansiroll: cannot open channel to %s: %s" % (host.name, ex))
            return True
        self._profile("%s channel open %.3fs" % (host.name, time.monotonic() - open_started))
        try:
            send_started = time.monotonic()
            chan.send_blob(agent_source)  # frame 1: the agent source
            self._profile("%s agent send %.3fs" % (host.name, time.monotonic() - send_started))
            failed = self._pump(
                host, chan, task_map, hv, templar, become_pass, play_context,
                selected_vars_files=selected_vars_files,
                gather_task=task_map.get("__gather_facts__") if gather_facts else None)
            self._profile("%s total %.3fs" % (host.name, time.monotonic() - started))
            return failed
        finally:
            chan.close()

    def _gather_setup_options(self, host, gather_task):
        from ansiroll.loader import _core_version

        try:
            values = self._variable_manager.get_vars(play=self._cur_play, host=host, task=gather_task)
            values["ansible_search_path"] = gather_task.get_search_path()
            templar = self._mk_templar(values)
            task = gather_task.copy()
            task.args = dict(gather_task.args)
            if _core_version() >= (2, 19):
                from ansible.executor.module_common import _apply_action_arg_defaults

                args = task.post_validate_attribute("args", templar=templar._engine)
                options = {key: value for key, value in args.items() if key != "parallel" and value is not None}
                options = _apply_action_arg_defaults("ansible.builtin.setup", task, options, templar)
            else:
                from ansible.executor.module_common import get_action_args_with_defaults
                from ansible.executor.task_executor import remove_omit

                validate_args = getattr(task, "_post_validate_args", None)
                if validate_args is not None:
                    task.args = validate_args(task.fattributes["args"], task.args, templar)
                else:
                    task.args = templar.template(task.args)
                legacy = _core_version() < (2, 12)
                groups = {} if legacy else {"action_groups": getattr(self._cur_play, "_action_groups", {})}
                setup_action = "ansible.legacy.setup" if legacy else "ansible.builtin.setup"
                args = get_action_args_with_defaults(
                    task.action if legacy else "ansible.builtin.gather_facts",
                    task.args, task.module_defaults, templar, **groups)
                if omit := values.get("omit"):
                    args = remove_omit(args, omit)
                args = {key: value for key, value in args.items() if key != "parallel" and value is not None}
                options = get_action_args_with_defaults(
                    setup_action, args, task.module_defaults, templar, **groups)
        except (AnsibleError, ValueError, TypeError) as ex:
            return {"options": {}, "error": str(ex)}
        return {"options": options, "error": None}

    def _pump(self, host, chan, task_map, hv, templar, become_pass, play_context, *,
              selected_vars_files=(), gather_task=None):
        host_failed = False
        seen = set()
        included_vars_by_task = {}
        last_frame = time.monotonic()

        def resolve_play_var(name=None, *, member=None, template_vars=None):
            request = {"t": "delegate_play_var", "name": name, "member": member}
            if template_vars:
                request["template_vars"] = template_vars
            chan.send(request)
            reply = chan.recv()
            if not reply or reply.get("t") != "delegate_play_var_result":
                raise RuntimeError("ansiroll: agent did not return a delegated play variable")
            return reply.get("fallback")

        while True:
            frame = chan.recv()
            now = time.monotonic()
            if frame is None:
                display.error("ansiroll: channel to %s closed unexpectedly:\n%s"
                              % (host.name, chan.stderr()))
                return True
            t = frame.get("t")
            if t == "result":
                result_frame = frame.get("frame", {})
                result_data = result_frame.get("result") or {}
                target_profile = result_data.pop("_ansiroll_profile", None)
                detail = ""
                if target_profile:
                    detail = (" module=%s load=%.3fs run=%.3fs total=%.3fs cached=%s"
                              % (target_profile.get("module"),
                                 target_profile.get("worker_load", 0.0),
                                 target_profile.get("worker_run", 0.0),
                                 target_profile.get("worker_total", 0.0),
                                 target_profile.get("worker_cached")))
                self._profile("%s result +%.3fs %s%s"
                              % (host.name, now - last_frame,
                                 result_frame.get("task") or result_frame.get("action"),
                                 detail))
                self._emit_provenance_frame(host, result_frame)
                task_key = result_frame.get("runtime_id") or result_frame.get("uuid")
                definitions = None
                if not result_frame.get("is_item"):
                    definitions = included_vars_by_task.pop(task_key, None)
                self._emit_frame(host, result_frame, task_map, seen, templar, definitions)
            elif t == "task_start":
                self._emit_task_start(frame.get("frame", {}), task_map, seen)
            elif t == "rescue":
                task = task_map.get(frame.get("task"))
                if task is not None:
                    self._mirror_rescued_failure(host, task, frame["result"], frame.get("args"), frame.get("name"))
            elif t == "rescue_overrides":
                self._mirror_rescue_overrides(host.name, frame["writes"])
            elif t == "gather_setup":
                data = (self._gather_setup_options(host, gather_task) if gather_task is not None
                        else {"error": "implicit setup was not scheduled"})
                gather_task = None
                chan.send({"t": "gather_setup_result", "setup": data})
            elif t == "delegate":
                delegate_started = time.monotonic()
                chan.send({"t": "delegate_result",
                           "result": self._run_delegated(
                               frame, host, hv, templar, become_pass,
                               task_map, play_context, included_vars_by_task, resolve_play_var
                           )})
                self._profile("%s delegate +%.3fs %s"
                              % (host.name, time.monotonic() - delegate_started,
                                 frame.get("name") or frame.get("action")))
            elif t == "source":
                lookup_task_id = frame.get("lookup_task_id")
                role_task = task_map.get(lookup_task_id) if lookup_task_id else None
                if lookup_task_id and role_task is None:
                    source_result = {"error": "could not identify the role source's task"}
                else:
                    source_result = self._read_controller_source(frame.get("path"), role_task=role_task)
                chan.send({
                    "t": "source_result",
                    "source": source_result,
                })
            elif t == "vars_files":
                state = self._resolved_vars_file_state(
                    host, selected_vars_files, task=task_map.get("__gather_facts__"), snapshot=hv)
                chan.send({"t": "vars_files_result", "result": state})
            elif t == "warning":
                display.warning(frame["message"])
            elif t == "error":
                raise AnsibleError(frame["message"])
            elif t == "stats":
                self._apply_stats(host, frame.get("stats", {}))
            elif t == "done":
                if frame.get("unreachable"):
                    self._tqm._unreachable_hosts[host.name] = True
                    self._cur_play._removed_hosts.append(host.name)
                else:
                    host_failed = host_failed or frame.get("failed", False)
                break
            last_frame = now
        return host_failed

    def _restore_rescue_value(self, packed):
        from ansiroll.runtime import _restore_rescue_scalars

        value = _restore_rescue_scalars(packed)
        if _UnifiedTaskResult is None:
            value = _wrap_legacy_rescue_value(value)
        return self._restore_play_fallback(
            value, packed["undefined"], packed.get("errors", ()), packed.get("mapping_keys", ()))

    def _mirror_rescued_failure(self, host, task, result, args=None, name=None):
        # This is variable state for later plays. Display has already received
        # the ordinary result frame with any no_log censoring applied.
        failed_task = task.dump_attrs() if hasattr(task, "dump_attrs") else task.serialize()
        if name is not None:
            failed_task["name"] = name
        if args is not None:
            failed_task["args"] = self._restore_rescue_value(args)
        if _UnifiedTaskResult is None:
            failed_task = _wrap_legacy_rescue_value(failed_task)
        facts = {
            "ansible_failed_task": failed_task,
            "ansible_failed_result": self._restore_rescue_value(result),
        }
        self._save_rescue_facts(host.name, facts, facts.keys())

    def _mirror_rescue_overrides(self, host_name, writes):
        manager = self._variable_manager
        previously_private = set(_private_rescue_facts(manager, host_name))
        # Apply accepted writes in item order. A public item must not stop a
        # later censored item updating a fact that was private before the loop.
        for packed, no_log, cacheable in writes:
            facts = self._restore_rescue_value(packed)
            if no_log:
                accepted = {name: value for name, value in facts.items() if name in previously_private}
                if accepted:
                    self._save_rescue_facts(host_name, accepted, accepted.keys())
            else:
                if cacheable:
                    manager.set_host_facts(host_name, dict(facts))
                self._save_rescue_facts(host_name, facts, ())

    def _save_rescue_facts(self, host_name, facts, private_names):
        manager = self._variable_manager
        manager.set_nonpersistent_facts(host_name, facts)
        private = vars(manager).setdefault("_ansiroll_private_rescue_facts", {})
        tracked = private.setdefault(host_name, {})
        for key in facts:
            tracked.pop(key, None)
        stored = manager._nonpersistent_fact_cache[host_name]
        tracked.update({key: stored[key] for key in private_names})

    @staticmethod
    def _read_controller_source(path, *, role_task=None):
        """Read a runtime-created controller file for in-memory target transfer."""
        if not path or (role_task is None and not os.path.isfile(path)):
            return {"error": "controller source %r is not a regular file" % path}
        fd = None
        try:
            if role_task is None:
                source = open(path, "rb")
                fullpath = os.path.abspath(path)
            else:
                from ansiroll.loader import _open_linked_role_source, _role_source_roots, _visible_below

                loader = role_task._loader
                subdir = "templates" if role_task.action.rsplit(".", 1)[-1] == "template" else "files"
                fullpath = loader.path_dwim_relative_stack(role_task.get_search_path(), subdir, path)
                realpath = os.path.realpath(fullpath)
                absolute = os.path.abspath(fullpath)
                if realpath != absolute:
                    roots = {os.path.abspath(root): os.path.realpath(root)
                             for root in _role_source_roots(role_task, loader.get_basedir())}
                    # Declared roots may themselves be reached through links.
                    ordinary = any(
                        os.path.commonpath((absolute, root)) == root
                        and os.path.join(real_root, os.path.relpath(absolute, root)) == realpath
                        for root, real_root in roots.items()
                    )
                    if not ordinary and (not any(_visible_below(fullpath, root) for root in roots)
                                         or not any(_visible_below(realpath, root) for root in roots.values())):
                        return {"error": "controller role source %r is excluded" % path}
                fd = _open_linked_role_source(realpath)
                source = os.fdopen(fd, "rb", closefd=False)
            with source as fh:
                st = os.fstat(fh.fileno())
                if not stat.S_ISREG(st.st_mode):
                    return {"error": "controller source %r is not a regular file" % path}
                data = fh.read()
            return {
                "data": base64.b64encode(data).decode("ascii"),
                "meta": {
                    "fullpath": fullpath,
                    "mtime": st.st_mtime,
                    "uid": st.st_uid,
                    "mode": stat.S_IMODE(st.st_mode),
                },
            }
        except (OSError, AnsibleError) as ex:
            return {"error": "could not read controller source %r: %s" % (path, ex)}
        finally:
            if fd is not None:
                os.close(fd)

    def _profile(self, msg):
        if getattr(self, "_profile_enabled", False):
            display.display("ansiroll-profile: %s" % msg)

    def _emit_provenance(self, payload):
        if getattr(self, "_provenance_enabled", False):
            display.display(
                "ANSIROLL_PROVENANCE %s"
                % json.dumps(payload, sort_keys=True, default=str)
            )

    def _emit_provenance_frame(self, host, frame):
        provenance = dict(frame.get("provenance") or {"mode": "native-agent"})
        provenance.update({
            "scope": "task",
            "host": host.name,
            "task": frame.get("task") or frame.get("action"),
            "action": frame.get("action"),
        })
        self._emit_provenance(provenance)

    def _run_delegated(self, frame, host, hv, templar, become_pass,
                       task_map=None, play_context=None, included_vars_by_task=None, resolve_play_var=None):
        """Execute a delegated / controller-coupled task on the correct host and
        return its raw module result (the agent re-emits the framed result)."""
        action = str(frame.get("action") or "").rsplit(".", 1)[-1]
        if action in ("add_host", "group_by"):
            return self._run_inventory_action(action, frame, host, hv)
        if action == "include_vars":
            result = self._run_include_vars(frame)
            facts = result.pop("ansible_facts", None)
            if facts is not None:
                from ansiroll.loader import _tag_untrusted

                # Scalar values stringified for JSON have no template provenance.
                result["_ansiroll_included_vars"] = json.dumps(
                    _tag_untrusted(facts),
                    default=lambda value: {"__ansiroll_unsafe__": str(value)},
                )
                # Keep the loader's tagged values until the register is replayed.
                task_key = frame.get("runtime_id") or frame.get("uuid")
                task = (task_map or {}).get(task_key)
                task_action = str(getattr(task, "action", "") or "").rsplit(".", 1)[-1]
                if (included_vars_by_task is not None and task_action == "include_vars"
                        and _register_names(task)):
                    definitions = included_vars_by_task.setdefault(task_key, [])
                    result["_ansiroll_included_vars_reference"] = len(definitions)
                    definitions.append(facts)
                # Match Ansible's include_vars layer for later plays and hosts.
                run_once = getattr(task, "run_once", None)
                if isinstance(run_once, str):
                    run_once = self._t(templar, run_once)
                hosts = self._run_hosts if _truthy(run_once) else (host,)
                for target in hosts:
                    for key, value in facts.items():
                        self._variable_manager.set_host_variable(target.name, key, value)
            return result
        if action == "synchronize":
            return self._run_native_controller_task(
                frame, host, hv, task_map or {}, play_context, resolve_play_var)
        to = frame.get("to")
        to = self._t(templar, to) if isinstance(to, str) else to
        local = to and str(to).lower() in ("localhost", "127.0.0.1", "::1")
        if action == "template" and local:
            task_key = frame.get("runtime_id") or frame.get("uuid")
            source_task = (task_map or {}).get(task_key)
            return self._run_local_template(frame, become_pass, resolve_play_var, source_task=source_task)
        if frame.get("fallback") or not local:
            reason = ("delegate_to: %s (non-local delegation)" % to if to
                      else "controller-coupled action %s" % frame.get("action"))
            return {"failed": True, "changed": False,
                    "msg": "ansiroll: %s is not supported yet" % reason}
        rt = self._local_runtime(become_pass)
        previous_check = rt.check_mode
        previous_diff = rt.diff_mode
        previous_become_user = rt.become_user
        try:
            rt.check_mode = bool(frame.get("check_mode"))
            rt.diff_mode = bool(frame.get("diff_mode"))
            rt.become_user = str(frame.get("become_user") or "root")
            return rt.run_module(frame.get("action"), frame.get("args") or {},
                                 become=bool(frame.get("become")))
        finally:
            rt.check_mode = previous_check
            rt.diff_mode = previous_diff
            rt.become_user = previous_become_user

    def _run_native_controller_task(self, frame, host, hv, task_map, play_context, resolve_play_var=None):
        """Run an action-plugin task with Ansible's official TaskExecutor."""
        task = task_map.get(frame.get("runtime_id"))
        if task is None:
            return {
                "failed": True,
                "changed": False,
                "msg": "ansiroll: original task is unavailable for %s"
                       % frame.get("action"),
            }
        try:
            from ansible.utils.unsafe_proxy import wrap_var

            from ansiroll.loader import _core_version

            concrete = task.copy()
            omit_tokens = {"__omit_place_holder__ansiroll", (hv or {}).get("omit")}
            concrete.args = _drop_omit_values(dict(frame.get("args") or {}), omit_tokens)
            if _core_version() < (2, 19):
                # The agent rendered these arguments before JSON removed their tags.
                concrete.args = wrap_var(concrete.args)
            concrete.when = []
            concrete.loop = None
            concrete.loop_with = None
            concrete.register = None
            concrete.notify = []
            for keyword in ("check_mode", "become", "no_log"):
                if keyword in frame:
                    setattr(concrete, keyword, frame[keyword])
            if "diff_mode" in frame:
                concrete.diff = frame["diff_mode"]
            # The agent applies the original retry policy around each controller call.
            concrete.retries = 0
            concrete.until = []
            concrete.changed_when = []
            concrete.failed_when = []
            job_vars = dict(hv or {})
            job_vars.update(frame.get("vars") or {})
            if deferred := frame.get("deferred_vars"):
                from ansiroll.loader import _tag_untrusted

                # Restore the controller's tagged source for each deferred
                # alias. Values supplied only by the agent remain data.
                sources = {"task": task.get_vars(), "play": hv or {}}
                play_fallbacks = frame.get("play_var_fallbacks") or {}
                play_undefined = frame.get("play_var_undefined") or {}
                lazy_play = set(frame.get("lazy_play_vars") or ()) if resolve_play_var else set()

                def load_play_var(name):
                    resolved = resolve_play_var(name)
                    if resolved is None:
                        raise AnsibleError("unable to resolve delegated play variable %r" % name)
                    if "error" in resolved:
                        raise AnsibleError("delegated play variable %r: %s" % (name, resolved["error"]))
                    return self._restore_play_fallback(wrap_var(resolved["value"]), resolved["undefined"],
                                                       resolved.get("errors", ()),
                                                       wrap_var(resolved.get("mapping_keys", ())))

                bridge = _DelegatedPlayVariables(load_play_var)
                try:
                    from ansible.template import trust_as_template
                except ImportError:
                    trust_as_template = None
                legacy_aliases = {}

                for name, scope in deferred.items():
                    if name not in job_vars:
                        continue
                    value = job_vars[name]
                    source_vars = sources.get(scope, {})
                    if name in source_vars:
                        source = source_vars[name]
                        tagged_source = json.loads(json.dumps(_tag_untrusted(source), default=str))
                        if tagged_source == value:
                            job_vars[name] = source
                            continue
                    if scope == "play" and name in lazy_play:
                        index = len(bridge.names)
                        bridge.names.append(name)
                        expression = "{{ __ansiroll_delegated_play_vars[%d] }}" % index
                        if trust_as_template is not None:
                            job_vars[name] = trust_as_template(expression)
                        else:
                            job_vars[name] = expression
                            legacy_aliases[name] = _LegacyDelegatedValue(bridge, index)
                    elif scope == "play" and name in play_fallbacks:
                        job_vars[name] = self._restore_play_fallback(wrap_var(play_fallbacks[name]),
                                                                    play_undefined.get(name, ()))
                    else:
                        job_vars[name] = wrap_var(value)
                if bridge.names:
                    job_vars["__ansiroll_delegated_play_vars"] = bridge
                if legacy_aliases:
                    concrete._post_validate_environment = _legacy_environment_validator(
                        concrete._post_validate_environment, legacy_aliases)

            class _QuietQueue:
                @staticmethod
                def send_callback(*_args, **_kwargs):
                    pass

            from ansible.executor.task_executor import TaskExecutor
            from ansible.plugins import loader as plugin_loader
            queue = _QuietQueue()
            parameters = inspect.signature(TaskExecutor).parameters
            if "task" in parameters:
                delegate_vars = getattr(self._variable_manager, "_get_delegated_vars", None)
                if getattr(concrete, "delegate_to", None) and delegate_vars is not None:
                    job_vars["ansible_delegated_vars"], _ = delegate_vars(self._cur_play, concrete, job_vars)
                args = [host, concrete, job_vars, play_context]
                if "new_stdin" in parameters:
                    args.append(None)
                args.extend((self._loader, plugin_loader, queue))
                if "variable_manager" in parameters:
                    args.append(self._variable_manager)
                result = TaskExecutor(*args).run()
            else:
                from ansible._internal._task import TaskContext

                # Match the worker's ambient task context on newer ansible-core.
                with TaskContext.create(task=concrete, task_vars=job_vars, host_name=host.name):
                    concrete.squash()
                    executor = TaskExecutor(host, play_context, self._loader, plugin_loader,
                                            queue, self._variable_manager)
                    result = executor.run().as_result_dict(for_round_trip=True)
            if isinstance(delegated := result.get("_ansible_delegated_vars"), dict):
                safe_keys = ("ansible_delegated_host", "ansible_host", "ansible_port",
                             "ansible_user", "ansible_connection")
                result["_ansible_delegated_vars"] = {key: delegated[key] for key in safe_keys if key in delegated}
            return result
        except Exception as ex:
            return {
                "failed": True,
                "changed": False,
                "msg": "ansiroll: controller action %s failed: %s"
                       % (frame.get("action"), ex),
            }

    @staticmethod
    def _restore_play_fallback(value, undefined, errors=(), mapping_keys=(), *, runtime_marker=None):
        from ansiroll.runtime import _replace_delegated_values, _restore_delegated_mapping_keys

        if not undefined and not errors and not mapping_keys:
            return value
        if runtime_marker is not None:
            missing = runtime_marker("Undefined element in a delegated template variable", missing=True)
            error_marker = runtime_marker
        else:
            message = "Undefined element in a delegated play variable"
            try:
                from ansible._internal._templating._jinja_common import CapturedExceptionMarker, UndefinedMarker
            except ImportError:
                from ansible.template import AnsibleUndefined

                class DelegatedUndefined(AnsibleUndefined):
                    # Stop the legacy templar from evaluating the marker
                    # while loading its surrounding container.
                    __UNSAFE__ = True

                missing = DelegatedUndefined(hint=message)

                def error_marker(message):
                    return _DelegatedMemberError(hint=message, exc=AnsibleError)
            else:
                missing = UndefinedMarker(hint=message, _no_template_source=True)

                def error_marker(message):
                    from ansible._internal._templating._utils import TemplateContext

                    # A received marker has no local source template.
                    with TemplateContext(template_value=None, templar=None, options=None):
                        return CapturedExceptionMarker(AnsibleError(message))

        markers = [(path, missing) for path in undefined]
        markers.extend((path, error_marker(message)) for path, message in errors)
        value = _replace_delegated_values(value, markers)
        return _restore_delegated_mapping_keys(value, mapping_keys)

    def _restore_private_result(self, packed, *, wrap_result=False):
        from ansiroll.loader import _core_version

        value = packed["value"]
        if _core_version() < (2, 19):
            from ansible.utils.unsafe_proxy import wrap_var

            if wrap_result:
                value = wrap_var(value)
            else:
                for path in packed.get("unsafe_paths", ()):
                    if not path:
                        value = wrap_var(value)
                        continue
                    container = value
                    for key in path[:-1]:
                        container = container[key]
                    container[path[-1]] = wrap_var(container[path[-1]])
        return self._restore_play_fallback(value, packed["undefined"], packed.get("errors", ()),
                                           packed.get("mapping_keys", ()))

    def _run_local_template(self, frame, become_pass, resolve_play_var=None, *, source_task=None):
        """Resolve and render the controller's template through the in-memory runtime."""
        args = dict(frame.get("args") or {})
        src = args.get("src")
        if not isinstance(src, str) or not src:
            return {
                "failed": True,
                "changed": False,
                "msg": "template: could not find controller source %r" % src,
            }
        loader = source_task._loader if source_task is not None else self._loader
        search_path = source_task.get_search_path() if source_task is not None else []
        try:
            fullpath = loader.path_dwim_relative_stack(search_path, "templates", src)
            if not os.path.isfile(loader.path_dwim(fullpath)):
                return {"failed": True, "changed": False, "msg": "template: source is not a regular file"}
            data, _ = loader._get_file_contents(fullpath)
            raw = data.decode("utf-8", "surrogateescape")
            st = os.stat(fullpath)
        except (OSError, AnsibleError) as ex:
            return {"failed": True, "changed": False, "msg": "template: %s" % ex}
        rt = self._local_runtime(become_pass)
        previous = rt.check_mode, rt.diff_mode, rt.become_user, rt._copy_transfer_check_mode
        try:
            rt.check_mode = bool(frame.get("check_mode"))
            rt.diff_mode = bool(frame.get("diff_mode"))
            rt.become_user = str(frame.get("become_user") or "root")
            rt._copy_transfer_check_mode = frame.get("copy_transfer_check_mode")
            task = {
                "no_log": bool(frame.get("no_log")),
                "_baked": {src: raw},
                "_baked_meta": {src: {
                    "fullpath": fullpath,
                    "mtime": st.st_mtime,
                    "uid": st.st_uid,
                    "mode": stat.S_IMODE(st.st_mode),
                }},
            }
            job_vars = dict(frame.get("vars") or {})
            template_vars = {}
            if names := frame.get("lazy_template_vars"):
                from ansiroll.runtime import (
                    _DEFER_LOOKUP_NAMES,
                    _AnsirollUndefined,
                    _captured,
                    _DelegatedTemplateValue,
                    _HostVarView,
                    _lazy_container,
                    _mark_unsafe,
                )

                def runtime_marker(message, missing=False):
                    return _AnsirollUndefined(hint=message) if missing else _captured(message)

                def load_template_var(name=None, *, member=None):
                    action_vars = dict(template_vars)
                    for key in ("template_mtime", "template_run_date"):
                        if action_vars.get(key) is not None:
                            action_vars[key] = action_vars[key].isoformat()
                    template_vars.clear()
                    resolved = (resolve_play_var(name, member=member, template_vars=action_vars)
                                if resolve_play_var else None)
                    if resolved is None:
                        return _captured("unable to resolve delegated template value")
                    if "error" in resolved:
                        return _captured(resolved["error"])
                    value = self._restore_play_fallback(
                        _mark_unsafe(resolved["value"]), resolved["undefined"], resolved.get("errors", ()),
                        resolved.get("mapping_keys", ()), runtime_marker=runtime_marker,
                    )
                    if not resolved.get("lazy_members"):
                        return value

                    def defer_member(index):
                        return _DelegatedTemplateValue(lambda: load_template_var(member=index))

                    items = ({key: defer_member(index) for key, index in value.items()}
                             if isinstance(value, dict) else [defer_member(index) for index in value])
                    if resolved.get("hostvar_view"):
                        return _HostVarView(items, rt, job_vars)
                    return _lazy_container(items, rt, job_vars, delegated=True)

                if frame.get("defer_template_names"):
                    job_vars[_DEFER_LOOKUP_NAMES] = True
                for name in names:
                    job_vars[name] = _DelegatedTemplateValue(lambda name=name: load_template_var(name))
            return rt._run_file_action("template", task, args, job_vars, become=bool(frame.get("become")),
                                       template_vars=template_vars if names else None)
        finally:
            rt.check_mode, rt.diff_mode, rt.become_user, rt._copy_transfer_check_mode = previous

    def _run_include_vars(self, frame):
        """include_vars reads controller-side files; resolve and parse here."""
        args = dict(frame.get("args") or {})
        fname = args.get("_raw_params") or args.get("file")
        if not fname:
            return {"failed": True, "changed": False,
                    "msg": "include_vars: no file specified"}
        fname = str(fname)
        path = None
        if os.path.isabs(fname):
            path = fname
        else:
            candidates = []
            for d in frame.get("search_dirs") or []:
                candidates.append(os.path.join(d, "vars", fname))
                candidates.append(os.path.join(d, fname))
            for c in candidates:
                if os.path.exists(c):
                    path = c
                    break
        if not path or not os.path.exists(path):
            return {"failed": True, "changed": False,
                    "msg": "Could not find or access '%s'" % fname}
        try:
            try:
                data = self._loader.load_from_file(path, cache="none", trusted_as_template=True)
            except TypeError:  # Older Ansible uses AnsibleUnsafeText for !unsafe.
                data = self._loader.load_from_file(path)
        except Exception as ex:
            return {"failed": True, "changed": False, "msg": str(ex)}
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return {"failed": True, "changed": False,
                    "msg": "%s must be stored as a dictionary/hash" % path}
        name = args.get("name")
        facts = {name: data} if name else data
        return {"changed": False, "ansible_facts": facts,
                "ansible_included_var_files": [path]}

    def _run_inventory_action(self, action, frame, current_host, hv):
        """add_host / group_by mutate the controller's in-memory inventory;
        mirror the core action plugins + strategy result processing."""
        args = dict(frame.get("args") or {})
        raw = args.pop("_raw_params", None)
        if isinstance(raw, dict):
            args.update(raw)
        if action == "add_host":
            name = args.get("name") or args.get("hostname") or args.get("host")
            if not name:
                return {"failed": True, "changed": False,
                        "msg": "name, host or hostname needs to be provided"}
            groups = args.get("groupname") or args.get("groups") or args.get("group") or []
            if isinstance(groups, str):
                groups = [g.strip() for g in groups.split(",") if g.strip()]
            special = ("name", "hostname", "host", "groupname", "groups", "group")
            host_vars = {k: v for k, v in args.items() if k not in special}
            result = {"changed": False,
                      "add_host": {"host_name": name, "groups": list(groups),
                                   "host_vars": host_vars}}
            try:
                from ansible._internal._task import AddHost
                host_info = AddHost(
                    host_name=name,
                    host_vars=host_vars,
                    parent_group_names=list(groups),
                )
                result["changed"] = self._inventory.add_dynamic_host(host_info)
            except ImportError:
                self._inventory.add_dynamic_host(result["add_host"], result)
            cache = getattr(self, "_hosts_cache_all", None)
            if result.get("changed") and cache is not None and name not in cache:
                cache.append(name)
            return result
        # group_by
        key = args.get("key")
        if not key:
            return {"failed": True, "changed": False,
                    "msg": "the 'key' param is required when using group_by"}
        parents = args.get("parents", ["all"])
        if isinstance(parents, str):
            parents = [parents]
        result = {"changed": False, "add_group": str(key).replace(" ", "-"),
                  "parent_groups": [str(p).replace(" ", "-") for p in parents]}
        host = (self._inventory.get_host(hv.get("inventory_hostname"))
                or current_host)
        try:
            # ansible-core 2.21 replaced the executor-side Host/dict API with
            # an inventory host name and a typed immutable payload.
            from ansible._internal._task import AddGroup
            group_info = AddGroup(
                group_name=result["add_group"],
                parent_group_names=result["parent_groups"],
            )
            result["changed"] = self._inventory.add_dynamic_group(
                host.name, group_info
            )
        except ImportError:
            if hasattr(self._inventory, "add_dynamic_group"):
                self._inventory.add_dynamic_group(host, result)
            else:
                self._add_group(host, result)
        # group_by can make new group_vars and group_names visible immediately
        # to later tasks in the same play. Compare against the last snapshot
        # sent to the agent, including trust that survives serialization.
        try:
            from ansiroll.loader import _tag_untrusted

            updated = self._variable_manager.get_vars(host=host, play=self._cur_play)
            delta = {}
            nonpersistent = getattr(
                self._variable_manager, "_nonpersistent_fact_cache", {}).get(
                    host.name, {})
            for name, value in updated.items():
                if name in ("hostvars", "vars") or name in nonpersistent:
                    continue
                try:
                    changed = (name not in hv or hv[name] != value
                               or _tag_untrusted(hv[name], template_syntax_only=True)
                               != _tag_untrusted(value, template_syntax_only=True))
                except Exception:
                    changed = name not in hv
                if changed:
                    delta[name] = value
            if delta:
                refreshed = json.loads(_json_blob(delta))
                result["_ansiroll_vars"] = refreshed
                hv.update({name: delta[name] for name in refreshed})
        except Exception:
            pass
        return result

    def _local_runtime(self, become_pass):
        if self._local_rt is None:
            from ansiroll.runtime import Runtime
            self._local_rt = Runtime(self._modules, self._ansiballz,
                                     {"inventory_hostname": "localhost"},
                                     become_password=become_pass)
        return self._local_rt

    def _close_local_runtime(self):
        if getattr(self, "_local_rt", None) is not None:
            try:
                self._local_rt.cleanup()
            except Exception:
                pass
            self._local_rt = None

    def _agent_header(self, host, hv, become_pass):
        return (self._var_header(host, hv, become_pass)
                + "_INJECTED_DELEGATE_ON_TARGET = %r\n" % bool(self._delegate_on_target)
                + "_INJECTED_SKIP_RUN_ONCE = %r\n"
                % (host.name != getattr(self, "_first_host_name", host.name))
                + "_INJECTED_CHECK_MODE = %r\n" % bool(self._check_mode)
                + "_INJECTED_INVOCATION_CHECK_MODE = %r\n" % bool(context.CLIARGS.get("check"))
                + "_INJECTED_DIFF_MODE = %r\n" % bool(self._diff_mode)
                + "_INJECTED_PROFILE = %r\n" % bool(getattr(self, "_profile_enabled", False))
                + "_INJECTED_NATIVE_ONLY = %r\n"
                % bool(getattr(self, "_native_only", False))
                + "_INJECTED_ACCELERATED_MODULES = %r\n"
                % os.environ.get("ANSIROLL_ACCELERATED_MODULES", "")
                + "_INJECTED_FAST_FACTS = %r\n"
                % _truthy(os.environ.get("ANSIROLL_FAST_FACTS", "")))

    def _host_vars_and_lookup_sources(self, host, *, task=None):
        selected = []
        manager = self._variable_manager
        if self._deferred_vars_file_keys is None:
            return dict(manager.get_vars(host=host, play=self._cur_play, task=task)), selected

        # Observe the vars_files reads made directly by this host's native
        # variable manager. Inventory plugins can also use load_from_file.
        loader = self._loader
        load_from_file = loader.load_from_file
        original_override = vars(loader).get("load_from_file")
        reader = manager.get_vars.__func__.__code__

        def capture(path, *args, **kwargs):
            caller = sys._getframe(1)
            selected_file = (caller.f_code is reader and caller.f_locals.get("host") is host
                             and caller.f_locals.get("play") is self._cur_play)
            if not selected_file:
                return load_from_file(path, *args, **kwargs)
            original_reader = vars(loader).get("_get_file_contents")
            loader._get_file_contents = read_selected
            try:
                data = load_from_file(path, *args, **kwargs)
            finally:
                if original_reader is None:
                    del loader._get_file_contents
                else:
                    loader._get_file_contents = original_reader
            selected.append(data)
            return data

        def read_selected(path):
            from ansible.errors import AnsibleFileNotFound, AnsibleParserError
            from ansiroll.loader import _core_version

            path = loader.path_dwim(path)
            try:
                with open(path, "rb", opener=lambda name, flags: os.open(name, flags | os.O_NONBLOCK)) as handle:
                    opened = os.fstat(handle.fileno())
                    if stat.S_ISREG(opened.st_mode):
                        data = handle.read()
                    elif stat.S_ISCHR(opened.st_mode) and os.path.samestat(opened, os.stat(os.devnull)):
                        data = b""
                    else:
                        raise AnsibleParserError("selected vars file %r is not a regular file" % path)
            except FileNotFoundError as ex:
                raise AnsibleFileNotFound("Unable to retrieve file contents.", file_name=path) from ex
            except OSError as ex:
                raise AnsibleParserError("could not read selected vars file %r" % path) from ex
            if _core_version() < (2, 19):
                return loader._decrypt_if_vault_data(data, os.fsencode(path))
            from ansible._internal._datatag._tags import Origin

            return loader._decrypt_if_vault_data(Origin(path=path).tag(data))

        loader.load_from_file = capture
        try:
            return dict(manager.get_vars(host=host, play=self._cur_play, task=task)), selected
        finally:
            if original_override is None:
                del loader.load_from_file
            else:
                loader.load_from_file = original_override

    def _resolved_vars_file_state(self, host, initial_files, *, task=None, snapshot=None):
        if self._deferred_vars_file_keys is None:
            return {"error": "unexpected request for vars_files"}
        if task is None:
            from ansible.playbook.block import Block
            from ansible.playbook.task import Task

            # Native vars_files validation uses a task context to distinguish
            # an unresolved selector after cached setup from an early probe.
            # The parent also supplies the playbook path for vars plugins.
            task = Task(block=Block(play=self._cur_play))
        try:
            current_vars, files = self._host_vars_and_lookup_sources(host, task=task)
        except AnsibleError as ex:
            return {"error": str(ex)}
        active_keys = self._vars_file_keys(files)
        keys = self._deferred_vars_file_keys | self._vars_file_keys(initial_files) | active_keys
        refreshed = {key: current_vars[key] for key in keys & current_vars.keys()}
        missing = sorted(keys - current_vars.keys(), key=str)
        mapping_keys = []
        try:
            serialized = _json_blob(refreshed, mapping_keys, strict_keys=active_keys, play_scope=True)
        except AnsibleError as ex:
            return {"error": str(ex)}
        if snapshot is not None:
            for key in missing:
                snapshot.pop(key, None)
            snapshot.update(refreshed)
        return {
            "vars": serialized,
            "names": sorted(set(self._cur_play.vars or {}) | active_keys, key=str),
            "missing": missing,
            "mapping_keys": mapping_keys,
            "controller_lookups": self._controller_lookups(refreshed),
        }

    @staticmethod
    def _vars_file_keys(files):
        return {key for loaded in files
                for mapping in (loaded if isinstance(loaded, list) else (loaded,))
                if isinstance(mapping, dict) for key in mapping}

    @staticmethod
    def _controller_lookups(selected_vars_files):
        from ansiroll.loader import _controller_config_names, _controller_config_values, _controller_env_names

        env_names = _controller_env_names(selected_vars_files, trusted_only=True)
        config_names = _controller_config_names(selected_vars_files, trusted_only=True)
        return {
            "controller_env": {name: os.environ[name] for name in sorted(env_names) if name in os.environ},
            "controller_config": _controller_config_values(config_names),
        }

    def _controller_lookup_header(self, variables, selected_vars_files):
        keys = self._vars_file_keys(selected_vars_files)
        keys.update(self._deferred_vars_file_keys or ())
        keys.intersection_update(variables)
        if not keys:
            return ""
        sources = {key: variables[key] for key in keys}
        return "_INJECTED_CONTROLLER_LOOKUPS = __import__('json').loads(%r)\n" % json.dumps(
            self._controller_lookups(sources), default=str)

    # ---- channel transport ----------------------------------------------
    def _open_channel(self, host, play_context, hv, templar):
        from ansiroll.runtime import AGENT_BOOTSTRAP
        conn_type = self._resolve_connection(host, play_context, hv, templar)
        # Pure controller-side actions do not need a target connection. This
        # matches linear's ability to run debug/fail/assert/set_fact against
        # ad-hoc inventory names that have no resolvable SSH endpoint.
        if self._controller_only_play:
            return _Channel([sys.executable, "-c", AGENT_BOOTSTRAP])
        interp = self._t(templar, hv.get("ansible_python_interpreter"))
        if not isinstance(interp, str) or not interp.startswith("/"):
            interp = sys.executable if conn_type == "local" else "/usr/bin/python3"
        if conn_type == "local":
            return _Channel([interp, "-c", AGENT_BOOTSTRAP])
        # ssh: reuse the connection plugin's own argv (keys/args/ControlPersist/sshpass)
        conn = self._connect(host, play_context, hv, templar)
        remote = "%s -c %s" % (interp, shlex.quote(AGENT_BOOTSTRAP))
        argv = _build_ssh_command(conn, remote)
        argv = [x.decode("utf-8", "surrogateescape") if isinstance(x, bytes) else x for x in argv]
        pass_fds = ()
        sshpass = getattr(conn, "sshpass_pipe", None)
        if sshpass:
            pw = self._resolve_password(hv, templar) or ""
            os.write(sshpass[1], (pw + "\n").encode("utf-8"))
            pass_fds = (sshpass[0],)
        display.vvv("ansiroll: %s agent via ssh" % host.name)
        return _Channel(argv, pass_fds=pass_fds)

    def _var_header(self, host, hv, become_pass=None):
        """Inject this host's vars (extra-vars, inventory, group_vars) plus the
        resolved become password into the streamed run (stdin only -- never on disk)."""
        from ansiroll.runtime import _pack_include_value

        mapping_keys = {"host": [], "extra": [], "overrides": []}
        blob = _json_blob(hv, mapping_keys["host"])
        extra_blob = _json_blob(_extra_vars(self._variable_manager), mapping_keys["extra"])
        nonpersistent = getattr(
            self._variable_manager, "_nonpersistent_fact_cache", {}
        ).get(host.name, {})
        rescue = _private_rescue_facts(self._variable_manager, host.name)
        override_blob = _json_blob(
            {key: value for key, value in nonpersistent.items() if key not in rescue}, mapping_keys["overrides"])
        rescue_blob = json.dumps(_pack_include_value(rescue, preserve_scalars=True)) if rescue else "null"
        return (
            "_INJECTED_HOST = %r\n"
            "_INJECTED_VARS = __import__('json').loads(%r)\n"
            "_INJECTED_EXTRA_VARS = __import__('json').loads(%r)\n"
            "_INJECTED_HOST_VAR_OVERRIDES = __import__('json').loads(%r)\n"
            "_INJECTED_RESCUE_FACTS = __import__('json').loads(%r)\n"
            "_INJECTED_VAR_MAPPING_KEYS = __import__('json').loads(%r)\n"
            "_INJECTED_BECOME_PASS = %r\n"
            % (host.name, blob, extra_blob, override_blob, rescue_blob, json.dumps(mapping_keys), become_pass)
        )

    def _connect(self, host, play_context, hv, templar=None):
        conn_type = self._resolve_connection(host, play_context, hv, templar)
        pc = play_context
        # Best-effort population so ssh/other connections have a valid address/user.
        # Values may be Jinja templates (e.g. ansible_user: "{{ alfred_assistant_user }}")
        # and must be templated here; on 2.10 the base play_context also leaves these
        # as Sentinel (a truthy object), so accept only real, non-empty strings.
        addr = _first_str(self._t(templar, hv.get("ansible_host")),
                          self._t(templar, hv.get("ansible_ssh_host")),
                          getattr(host, "address", None), host.name)
        user = (_first_str(self._t(templar, hv.get("ansible_user")),
                           self._t(templar, hv.get("ansible_ssh_user")),
                           self._t(templar, getattr(pc, "remote_user", None))))
        for attr, val in (("connection", conn_type), ("remote_addr", addr), ("remote_user", user)):
            if isinstance(val, str) and val:
                try:
                    setattr(pc, attr, val)
                except Exception:
                    pass
        display.vvv("ansiroll: %s via %s as user=%s addr=%s"
                    % (host.name, conn_type, user or "<ssh default>", addr))
        conn = connection_loader.get(conn_type, pc, None)
        self._apply_conn_options(conn, conn_type, hv, templar, user, addr)
        return conn

    def _apply_conn_options(self, conn, conn_type, hv, templar, user, addr):
        """Apply per-host connection settings (key file, ssh args, port) like
        the task executor does, so ssh uses the configured identity rather than
        falling back to whatever the agent offers."""
        direct = {"host": addr}
        if user:
            direct["remote_user"] = user

        def setopt(opt, *keys):
            v = _first_str(*[self._t(templar, hv.get(k)) for k in keys])
            if v is not None:
                direct[opt] = v
            return v

        if conn_type == "ssh":
            private_key = setopt(
                "private_key_file",
                "ansible_ssh_private_key_file",
                "ansible_private_key_file",
            )
            ssh_common_args = setopt("ssh_common_args", "ansible_ssh_common_args")
            ssh_extra_args = setopt("ssh_extra_args", "ansible_ssh_extra_args")
            setopt("ssh_args", "ansible_ssh_args")
            pw = self._resolve_password(hv, templar)
            if pw is not None:
                direct["password"] = pw  # drives sshpass; get_option('password')
            port = hv.get("ansible_port") or hv.get("ansible_ssh_port")
            if port is not None:
                try:
                    direct["port"] = int(self._t(templar, port) if isinstance(port, str) else port)
                except Exception:
                    pass
            # ansible-base 2.10's SSH command builder reads these settings
            # directly from PlayContext instead of the plugin option store.
            # Keep both representations populated so its command includes the
            # configured identity, port, and host-key options.
            old_context_options = {
                "private_key_file": private_key,
                "ssh_common_args": ssh_common_args,
                "ssh_extra_args": ssh_extra_args,
                "port": direct.get("port"),
            }
            for attr, value in old_context_options.items():
                if value is not None:
                    try:
                        setattr(conn._play_context, attr, value)
                    except Exception:
                        pass
        try:
            # var_options lets the plugin resolve any options we didn't set explicitly;
            # direct (templated) wins for the connection-critical ones.
            conn.set_options(var_options=hv, direct=direct)
        except Exception as ex:
            try:
                conn.set_options(direct=direct)
            except Exception:
                display.vvv("ansiroll: set_options failed: %s" % ex)

    def _resolve_password(self, hv, templar, become=False):
        keys = (("ansible_become_password", "ansible_become_pass", "ansible_sudo_pass")
                if become else
                ("ansible_password", "ansible_ssh_password", "ansible_ssh_pass"))
        for k in keys:
            v = hv.get(k)
            if v is None:
                continue
            try:
                tv = templar.template(v) if templar is not None else v
            except Exception:
                tv = v
            if isinstance(tv, str) and tv:
                return tv
            if tv is not None:
                try:
                    s = str(tv)
                    if s and "AnsibleVault" not in s:
                        return s
                except Exception:
                    pass
        return None

    def _resolve_become_password(self, hv, templar):
        # Prefer explicit become creds; many setups reuse the connection password.
        return (self._resolve_password(hv, templar, become=True)
                or self._resolve_password(hv, templar, become=False))

    def _mk_templar(self, hv):
        try:
            from ansible.template import Templar
            return Templar(loader=self._loader, variables=hv)
        except Exception:
            return None

    def _t(self, templar, value):
        """Template a single value against the host's vars (controller-side)."""
        if templar is None or not isinstance(value, str) or "{{" not in value:
            return value
        try:
            return templar.template(value)
        except Exception:
            return value

    def _resolve_connection(self, host, play_context, hv, templar=None):
        conn = self._t(templar, hv.get("ansible_connection"))
        if not isinstance(conn, str) or not conn:
            play_conn = self._t(templar, getattr(self._cur_play, "connection", None))
            conn = play_conn if isinstance(play_conn, str) else None
        if not conn:
            pc_conn = getattr(play_context, "connection", None)
            conn = pc_conn if isinstance(pc_conn, str) else None
        if not conn or conn == "smart":
            conn = "ssh"
        return conn

    # ---- result replay ---------------------------------------------------
    def _emit_task_start(self, frame, task_map, seen):
        task_key = frame.get("runtime_id") or frame.get("uuid")
        task = task_map.get(task_key)
        if task is None or task_key in seen:
            if task is None and frame.get("action") != "meta":
                display.warning(
                    "ansiroll: no controller task mapping for %s (%s)"
                    % (frame.get("task") or frame.get("action"), task_key)
                )
            return False
        seen.add(task_key)
        self._tqm.send_callback("v2_playbook_on_task_start", task, is_conditional=False)
        return True

    def _emit_frame(self, host, frame, task_map, seen, templar=None,
                    included_vars=None):
        task_key = frame.get("runtime_id") or frame.get("uuid")
        task = task_map.get(task_key)
        if task is None:
            display.warning(
                "ansiroll: dropping result without controller task mapping for %s (%s)"
                % (frame.get("task") or frame.get("action"), task_key)
            )
            return False
        if task_key not in seen:
            self._emit_task_start(frame, task_map, seen)

        status = frame.get("status")
        result_data = frame.get("result", {})
        # Mirror registered vars and facts into the variable manager so they
        # survive into subsequent plays (each play runs a fresh agent).
        try:
            registers = _register_names(task)
            action = str(getattr(task, "action", "") or "").rsplit(".", 1)[-1]
            state = result_data
            if "private_result" in frame:
                state = self._restore_private_result(frame["private_result"],
                                                     wrap_result=action not in ("set_fact", "include_vars"))
            registered_result = state
            if registers and not frame.get("is_item"):
                if action in ("include_tasks", "include_role") and "include_register" in frame:
                    registered_result = self._restore_private_result(frame["include_register"])
                if action == "synchronize":
                    from ansiroll.runtime import _without_delegated_metadata
                    registered_result = _without_delegated_metadata(state)
                if action == "include_vars" and included_vars:
                    registered_result = _restore_included_vars(
                        state, included_vars, frame.get("included_vars_references"),
                    )
                self._variable_manager.set_nonpersistent_facts(
                    host.name, {reg: dict(registered_result) for reg in registers}
                )
            facts = state.get("ansible_facts")
            if (action == "set_fact" and isinstance(facts, dict)
                    and ("private_result" in frame or frame.get("is_item"))):
                # Commit rescue-name writes in item order after the task,
                # with censored writes allowed only on success.
                from ansiroll.runtime import _RESCUE_FACT_NAMES

                facts = {name: value for name, value in facts.items() if name not in _RESCUE_FACT_NAMES}
            if task_key == "__gather_facts__" and status == "ok":
                facts = frame.get("gathered_facts", facts)
                if isinstance(facts, dict):
                    # Accelerated setup does not emit the marker that
                    # Ansible's variable manager checks in the fact cache.
                    facts = dict(facts, module_setup=True)
            if action == "include_vars":
                # The controller has already stored the original tagged values.
                facts = None
            is_set_fact = action == "set_fact"
            cacheable = bool(state.get("_ansible_facts_cacheable", False))
            if isinstance(facts, dict):
                # Match StrategyBase: cacheable set_fact results populate the
                # persistent fact cache, while every set_fact result also gets
                # the higher-precedence nonpersistent host-var copy.
                if not is_set_fact or cacheable:
                    self._variable_manager.set_host_facts(
                        host.name, dict(facts)
                    )
                if is_set_fact:
                    self._variable_manager.set_nonpersistent_facts(
                        host.name, dict(facts)
                    )

            # Linear shares a run_once task's registered result and facts with
            # every host in the current batch.  Hosts are launched in order, so
            # later agents receive this state in their initial variable snapshot.
            run_once = getattr(task, "run_once", None)
            if isinstance(run_once, str) and templar is not None:
                run_once = self._t(templar, run_once)
            if _truthy(run_once) and not frame.get("is_item"):
                for other in getattr(self, "_run_hosts", ()):
                    if other.name == host.name:
                        continue
                    if registers:
                        self._variable_manager.set_nonpersistent_facts(
                            other.name,
                            {reg: dict(registered_result) for reg in registers},
                        )
                    if isinstance(facts, dict):
                        if not is_set_fact or cacheable:
                            self._variable_manager.set_host_facts(
                                other.name, dict(facts)
                            )
                        if is_set_fact:
                            self._variable_manager.set_nonpersistent_facts(
                                other.name, dict(facts)
                            )
        except Exception:
            pass
        if "no_log" in frame and task.no_log is not frame["no_log"]:
            original = task
            task = original.copy(exclude_parent=True)
            task._parent = original._parent
            task.no_log = frame["no_log"]
        tr = self._make_result(host, task, result_data)
        is_item = frame.get("is_item")
        ignored = frame.get("ignored")
        if status == "unreachable" and is_item:
            # Ansible emits the task-level unreachable callback for the loop.
            return False

        method = _callback_for(status, is_item)
        if status == "failed" and not is_item:
            self._tqm.send_callback(method, tr, ignore_errors=ignored)
            return not ignored
        if status == "changed" and frame.get("diff_mode") and not frame.get("no_log") and result_data.get("diff"):
            self._tqm.send_callback("v2_on_file_diff", tr)
        self._tqm.send_callback(method, tr)
        return False

    def _make_result(self, host, task, result_data):
        result_data = dict(result_data)
        # 2.19+ callbacks expect a structured error summary. Reconstruct one
        # from the agent's plain result so native-style diagnostics (including
        # task source context) are retained.
        if result_data.get("failed") and not _is_structured_error(
                result_data.get("exception")):
            summary = _failure_summary(task, result_data.get("msg"))
            if summary is not None:
                result_data["exception"] = summary
            else:
                result_data.pop("exception", None)
        elif isinstance(result_data.get("exception"), str):
            result_data.pop("exception")
        # Same for deprecations: the agent forwards plain dicts, which the
        # 2.19+ default callback cannot render; degrade them to warnings.
        def summary_message(value):
            if not isinstance(value, dict):
                return str(value)
            if value.get("msg"):
                return str(value["msg"])
            event = value.get("event")
            if isinstance(event, dict) and event.get("msg"):
                return str(event["msg"])
            return str(value)

        # Serialized module warnings/deprecations are plain dictionaries by
        # the time they cross the agent channel.  New callbacks expect typed
        # summary objects and otherwise abort before printing the normal ok
        # line; older callbacks receive the string fallback below.
        raw_warnings = result_data.pop("warnings", []) or []
        raw_deprecations = result_data.pop("deprecations", []) or []
        try:
            from ansible.module_utils._internal._messages import (
                DeprecationSummary, Event, WarningSummary,
            )
            if raw_warnings:
                result_data["warnings"] = [
                    WarningSummary(event=Event(msg=summary_message(w)))
                    for w in raw_warnings
                ]
            if raw_deprecations:
                result_data["deprecations"] = [
                    DeprecationSummary(
                        event=Event(msg=summary_message(dep)),
                        date=dep.get("date") if isinstance(dep, dict) else None,
                        version=dep.get("version") if isinstance(dep, dict) else None,
                    )
                    for dep in raw_deprecations
                ]
        except Exception:  # older ansible-core uses strings/dicts here
            warnings = [summary_message(w) for w in raw_warnings]
            warnings.extend(
                "[DEPRECATION WARNING]: %s" % summary_message(dep)
                for dep in raw_deprecations
            )
            if warnings:
                result_data["warnings"] = warnings
        if _UnifiedTaskResult is not None:
            unified = _UnifiedTaskResult.from_action_result_dict(dict(result_data))
            return _ResultClass(host, task, unified)
        try:
            return _ResultClass(host, task, dict(result_data), task.dump_attrs())
        except TypeError:
            # Older signature: TaskResult(host, task, return_data)
            return _ResultClass(host, task, dict(result_data))

    def _apply_stats(self, host, stats):
        # Accumulate: run() is invoked once per play and the recap covers all.
        # Linear does not add a host to PLAY RECAP when tag filtering leaves a
        # play with no reportable tasks.  Avoid manufacturing an all-zero row.
        if not any(stats.get(key, 0) for key in (
            "ok", "changed", "failures", "skipped", "unreachable",
            "rescued", "ignored",
        )):
            return
        st = self._tqm._stats
        name = host.name
        st.processed[name] = 1
        st.ok[name] = st.ok.get(name, 0) + stats.get("ok", 0) + stats.get("changed", 0)
        st.changed[name] = st.changed.get(name, 0) + stats.get("changed", 0)
        st.failures[name] = st.failures.get(name, 0) + stats.get("failures", 0)
        st.skipped[name] = st.skipped.get(name, 0) + stats.get("skipped", 0)
        st.dark[name] = st.dark.get(name, 0) + stats.get("unreachable", 0)
        if hasattr(st, "rescued"):
            st.rescued[name] = st.rescued.get(name, 0) + stats.get("rescued", 0)
        if hasattr(st, "ignored"):
            st.ignored[name] = st.ignored.get(name, 0) + stats.get("ignored", 0)

    def _drain_iterator(self, host, iterator):
        # Advance host state to COMPLETE; tasks already ran inside the compiled file.
        guard = 0
        while guard < 10000:
            state, task = iterator.get_next_task_for_host(host)
            if task is None:
                break
            guard += 1


def _restore_included_vars(result, definitions, references):
    """Restore controller-loaded facts only in the registered copy of a result."""
    if not isinstance(references, dict):
        return result
    looped = isinstance(result.get("results"), list)
    entries = result["results"] if looped else [result]
    restored = []
    for index, entry in enumerate(entries):
        reference = references.get(str(index))
        if (type(reference) is int and 0 <= reference < len(definitions)
                and isinstance(entry, dict) and "ansible_facts" in entry):
            entry = dict(entry, ansible_facts=definitions[reference])
        restored.append(entry)
    if looped:
        return dict(result, results=restored)
    return restored[0]


def _register_names(task):
    """Return legacy and 2.21 register-projection variable names."""
    value = getattr(task, "register", None)
    if isinstance(value, dict):
        return [str(name) for name in value]
    return [str(value)] if value else []


def _controller_fallbacks(play_node):
    found = []

    def walk(nodes):
        for node in nodes or []:
            if node.get("type") == "block":
                walk(node.get("block"))
                walk(node.get("rescue"))
                walk(node.get("always"))
                continue
            if node.get("type") == "include":
                walk(node.get("tasks"))
                walk(node.get("handlers"))
            if node.get("controller_handler_name"):
                found.append({"kind": "vars-file-handler", "task": node.get("name"),
                              "reason": "handler name requires hostless vars-file selection"})
            if node.get("fallback"):
                kind = "controller-fallback"
                if node.get("_deferred_vault_source"):
                    kind = "vault-source"
                elif node.get("_file_source_argument_layers"):
                    kind = "file-source-arguments"
                elif node.get("vars_file_include_fallback"):
                    kind = "vars-file-include"
                elif node.get("_deferred_apply_include"):
                    kind = "apply-include"
                found.append({
                    "kind": kind,
                    "task": node.get("name") or node.get("action"),
                    "reason": node.get("fallback_reason")
                              or "controller fallback",
                })
            if node.get("delegate_to"):
                found.append({
                    "kind": "delegate",
                    "task": node.get("name") or node.get("action"),
                    "reason": "delegate_to %s" % node.get("delegate_to"),
                })

    walk(play_node.get("tasks"))
    walk(play_node.get("handlers"))
    return found


def _is_structured_error(value):
    return value is not None and not isinstance(value, (str, dict))


def _failure_summary(task, message):
    try:
        from ansible.module_utils._internal._messages import ErrorSummary, Event
    except Exception:
        return None
    message = str(message or "Task failed")
    if message.startswith("Task failed"):
        event_message = message
    else:
        event_message = "Task failed: Action failed: %s" % message
    return ErrorSummary(event=Event(
        msg=event_message,
        formatted_source_context=_task_source_context(task),
    ))


def _task_source_context(task):
    try:
        task_path = str(task.get_path())
    except Exception:
        return None
    match = __import__("re").match(r"^(.*):(\d+)$", task_path)
    if not match:
        return None
    path, line_text = match.groups()
    line = int(line_text)
    try:
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as source:
            lines = source.read().splitlines()
    except OSError:
        return None
    start = max(1, line - 2)
    excerpt = "\n".join(
        "%d %s" % (number, lines[number - 1])
        for number in range(start, min(line, len(lines)) + 1)
    )
    return "Origin: %s:%d\n\n%s" % (path, line, excerpt)


def _callback_for(status, is_item):
    if is_item:
        return {
            "ok": "v2_runner_item_on_ok",
            "changed": "v2_runner_item_on_ok",
            "failed": "v2_runner_item_on_failed",
            "skipped": "v2_runner_item_on_skipped",
        }.get(status, "v2_runner_item_on_ok")
    return {
        "ok": "v2_runner_on_ok",
        "changed": "v2_runner_on_ok",
        "failed": "v2_runner_on_failed",
        "skipped": "v2_runner_on_skipped",
        "unreachable": "v2_runner_on_unreachable",
    }.get(status, "v2_runner_on_ok")


def _first_str(*values):
    """Return the first value that is a non-empty plain string (ignores Sentinel)."""
    for v in values:
        if isinstance(v, str) and v:
            return v
    return None


def _truthy(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class _LegacyFactResultQueue(collections.deque):
    """Inspect 2.10 results at dequeue time, before linear publishes facts."""

    def __init__(self, results, record):
        super().__init__(results)
        self._record = record

    def popleft(self):
        result = super().popleft()
        self._record(result)
        return result


_UNROUTED_FACT_RESULT = object()


class _SetFactLinearStrategy(_LinearStrategy):
    """Use the worker's resolved delegation flags when linear publishes facts."""

    def run(self, iterator, play_context):
        from ansiroll.runtime import _looks_templated

        self._fact_iterator = iterator
        self._fact_routes = {}
        self._legacy_fact_items = {}
        self._run_once_fact_results = {}
        send_callback = self._tqm.send_callback
        original = vars(self._tqm).get("send_callback")
        threaded_callbacks = (_UnifiedTaskResult is not None
                              or hasattr(_LinearStrategy, "_convert_wire_task_result_to_raw"))

        def capture_item(method, *args, **kwargs):
            if method in ("v2_runner_item_on_ok", "v2_runner_item_on_skipped", "v2_runner_item_on_failed"):
                if threaded_callbacks and threading.current_thread() is not self._results_thread:
                    # Linear may emit deferred callbacks after consuming the aggregate.
                    return send_callback(method, *args, **kwargs)
                result = args[0]
                host, task = ((result.host, result.task) if _UnifiedTaskResult is not None else
                              (result._host, result._task))
                key = (host.name, task._uuid)
                original = self._queued_task_cache.get(key, {}).get("task")
                if _has_delegated_fact_flag(original):
                    if task.action in C._ACTION_SET_FACT or original.action in C._ACTION_SET_FACT:
                        target = task.delegate_to if _delegated_fact_flag(task.delegate_facts) is True else None
                        self._fact_routes.setdefault(key, []).append(target if isinstance(target, str) else None)
                    elif _looks_templated(original.action):
                        # Keep the place of items that don't resolve to set_fact,
                        # including skipped items that never resolve the action.
                        self._fact_routes.setdefault(key, []).append(_UNROUTED_FACT_RESULT)
            return send_callback(method, *args, **kwargs)

        # Core 2.10 consumes raw queues inline without a conversion hook.
        raw_queues = ("_results", "_handler_results") if not (
            hasattr(_LinearStrategy, "normalize_task_result")
            or hasattr(_LinearStrategy, "_convert_wire_task_result_to_raw")
            or _UnifiedTaskResult is not None
        ) else ()
        if raw_queues:
            with self._results_lock:
                for name in raw_queues:
                    setattr(self, name, _LegacyFactResultQueue(getattr(self, name), self._record_legacy_routes))
        self._tqm.send_callback = capture_item
        try:
            return super().run(iterator, play_context)
        finally:
            if raw_queues:
                with self._results_lock:
                    for name in raw_queues:
                        setattr(self, name, collections.deque(getattr(self, name)))
            if original is None:
                del self._tqm.send_callback
            else:
                self._tqm.send_callback = original

    def _routed_fact_task(self, key, resolved):
        original = self._queued_task_cache.get(key, {}).get("task")
        return (_has_delegated_fact_flag(original)
                and (resolved.action in C._ACTION_SET_FACT or original.action in C._ACTION_SET_FACT))

    def _fact_recipient(self, name):
        if not isinstance(name, str):
            return name
        host = self._inventory.get_host(name)
        return host.name if host is not None else name

    def normalize_task_result(self, task_result):
        result = super().normalize_task_result(task_result)
        self._record_legacy_routes(result)
        return result

    def _convert_wire_task_result_to_raw(self, wire_task_result):
        result = super()._convert_wire_task_result_to_raw(wire_task_result)
        self._record_legacy_routes(result)
        return result

    def _record_legacy_routes(self, result):
        data = result._result if hasattr(result, "_result") else result._return_data
        if data.get("_ansible_item_result") or data.get("_ansible_retry"):
            return
        host, task = ((result._host, result._task) if hasattr(result, "_task") else (result.host, result.task))
        if isinstance(task, str):
            task_uuid = task
            host = self._inventory.get_host(host)
        else:
            task_uuid = task._uuid
        if host is None:
            return
        routes = self._fact_routes.pop((host.name, task_uuid), ())
        items = data.get("results")
        if (routes and isinstance(items, list) and len(routes) == len(items)
                and getattr(task, "action", None) not in C._ACTION_DEBUG
                and not (result.is_failed() or result.is_unreachable() or result.is_skipped())):
            # Hold the item itself so its identity cannot be reused before
            # linear consumes the route or finishes the aggregate.
            self._legacy_fact_items.update((id(item), (item, host, target)) for item, target in zip(items, routes)
                                           if target is not _UNROUTED_FACT_RESULT and "ansible_facts" in item)

    def get_delegated_hosts(self, result, task):
        route = self._legacy_fact_items.pop(id(result), None)
        if route is not None:
            _, host, target = route
            if target is None:
                return self.get_task_hosts(self._fact_iterator, host, task)
            return [self._fact_recipient(target)]
        hosts = super().get_delegated_hosts(result, task)
        return [self._fact_recipient(name) for name in hosts] if task.action in C._ACTION_SET_FACT else hosts

    def _convert_wire_task_result_to_host_task_result(self, wire_task_result):
        result = super()._convert_wire_task_result_to_host_task_result(wire_task_result)
        host, task, data = result.host, result.task, result.utr
        if data.loop_var is not None:
            return result
        key = (host.name, task._uuid)
        routes = self._fact_routes.pop(key, ())
        if not routes and not self._routed_fact_task(key, task):
            return result
        if data.loop_results is None:
            if data.delegated_host:
                data.delegated_host = self._fact_recipient(data.delegated_host)
            return result
        if len(routes) == len(data.loop_results):
            for item, target in zip(data.loop_results, routes):
                if target is not _UNROUTED_FACT_RESULT:
                    item.delegated_host = self._fact_recipient(target) if target else host.name
            if task.run_once and None in routes:
                # Older 2.21 patches may retain a write's dictionary in the
                # cache, where later writes can mutate it in place.
                writes = [
                    (target, item.delegated_host, {
                        layer: dict(values) for layer, values in item.pending_changes.register_host_variables.items()
                    })
                    for item, target in zip(data.loop_results, routes)
                ]
                self._run_once_fact_results[id(result)] = (writes, dict(data.registered_values or {}))
        return result

    def _process_pending_results(self, iterator, *args, **kwargs):
        results = super()._process_pending_results(iterator, *args, **kwargs)
        if _UnifiedTaskResult is None:
            for result in results:
                data = result._result if hasattr(result, "_result") else result._return_data
                items = data.get("results")
                if isinstance(items, list):
                    for item in items:
                        self._legacy_fact_items.pop(id(item), None)
            return results
        for result in results:
            replay = self._run_once_fact_results.pop(id(result), None)
            if replay is None or result.utr.failed or result.utr.unreachable or result.utr.skipped:
                continue
            from ansible._internal._task import VariableLayer

            # Core 2.21 accepts one delegated host per item, while a local
            # run_once fact belongs to the entire batch. Replay in item order
            # so a later delegated assignment wins for its own recipient.
            cache = self._variable_manager
            writes, registered = replay
            task = result.task
            original_hosts = self.get_task_hosts(iterator, result.host, task)
            for target, delegated_host, changes in writes:
                if target is _UNROUTED_FACT_RESULT:
                    recipients = ([delegated_host or task.delegate_to]
                                  if task.delegate_to and task.delegate_facts else original_hosts)
                else:
                    recipients = [delegated_host] if target else original_hosts
                for layer, values in sorted(changes.items()):
                    if layer == VariableLayer.REGISTER_VARS:
                        for host in original_hosts:
                            cache.set_nonpersistent_facts(host, dict(values))
                    elif layer == VariableLayer.CACHEABLE_FACT:
                        for host in recipients:
                            cache.set_host_facts(host, dict(values))
                    elif layer == VariableLayer.EPHEMERAL_FACT:
                        for host in recipients:
                            cache.set_nonpersistent_facts(host, dict(values))
                    elif layer == VariableLayer.INCLUDE_VARS:
                        for host in recipients:
                            for name, value in values.items():
                                cache.set_host_variable(host, name, value)
            if registered:
                for host in original_hosts:
                    cache.set_nonpersistent_facts(host, dict(registered))
        return results


def _has_delegated_fact_flag(task):
    return (task is not None and task.delegate_to
            and task.delegate_facts is not None and task.delegate_facts is not False)


def _delegated_fact_flag(value):
    """Resolve literal flags without evaluating templates that depend on runtime vars."""
    from ansiroll.runtime import _boolean

    if value is None:
        return False
    if isinstance(value, str) and re.fullmatch(r"\s*\{\{-?\s*(?:false|False|0)\s*-?\}\}\s*", value):
        return False
    try:
        return _boolean(value, strict=True)
    except TypeError:
        return None


def _extra_vars(variable_manager):
    for attr in ("extra_vars", "_extra_vars"):
        value = getattr(variable_manager, attr, None)
        if isinstance(value, dict):
            return value
    return {}


def _json_blob(values, mapping_keys=None, *, strict_keys=(), play_scope=False):
    from ansiroll.loader import _tag_untrusted
    from ansiroll.runtime import _contains_typed_mapping_keys, _pack_delegated_value

    # hostvars/vars are live controller objects that stringify uselessly; the
    # agent synthesizes ``vars``. Materialize hostvars without their recursive
    # magic members so cross-host templates remain available to delegated
    # controller actions.
    if isinstance(values, dict):
        hostvars = values.get("hostvars")
        values = {k: v for k, v in values.items() if k not in ("hostvars", "vars")}
        snapshot = _hostvars_snapshot(hostvars, preserve_keys=mapping_keys is not None)
        if snapshot:
            values["hostvars"] = snapshot
    # Keep literal template-looking data tagged across the JSON boundary.
    # Other host values also serve as connection metadata and identifiers.
    try:
        tagged = _tag_untrusted(values, template_syntax_only=not play_scope, preserve_vault=play_scope)
        if mapping_keys is None or not _contains_typed_mapping_keys(tagged):
            return json.dumps(tagged, default=str)
    except Exception:
        pass
    safe = {}
    for k, v in (values or {}).items():
        try:
            v = _tag_untrusted(v, template_syntax_only=not play_scope, preserve_vault=play_scope)
            if mapping_keys is not None and _contains_typed_mapping_keys(v):
                packed = _pack_delegated_value(v)
                if packed["undefined"] or packed.get("errors") or not packed.get("mapping_keys"):
                    raise ValueError("mapping keys could not be serialized")
                mapping_keys.extend(([k, *path], keys) for path, keys in packed["mapping_keys"])
                v = packed["value"]
            else:
                json.dumps(v, default=str)
        except Exception as ex:
            if k in strict_keys:
                raise AnsibleError("could not serialize selected vars file value for %r" % k) from ex
            continue
        safe[k] = v
    return json.dumps(safe, default=str)


def _private_rescue_facts(manager, host_name):
    private = getattr(manager, "_ansiroll_private_rescue_facts", {})
    tracked = private.get(host_name, {})
    if not tracked:
        return {}
    current = manager._nonpersistent_fact_cache.get(host_name, {})
    active = {key: value for key, value in tracked.items() if key in current and current[key] is value}
    if active:
        private[host_name] = active
    else:
        private.pop(host_name, None)
    return active


def _hostvars_snapshot(hostvars, *, preserve_keys=False):
    from ansiroll.loader import _core_version
    from ansiroll.runtime import _contains_typed_mapping_keys, _pack_delegated_value

    if hostvars is None:
        return {}
    out = {}
    try:
        names = list(hostvars)
        implicit_local = getattr(getattr(hostvars, "_inventory", None), "localhost", None)
        if implicit_local is not None:
            names.extend(alias for alias in C.LOCALHOST if alias not in names)
    except Exception:
        return out
    raw_get = getattr(hostvars, "raw_get", None)
    manager = getattr(hostvars, "_variable_manager", None)
    inventory = getattr(hostvars, "_inventory", None)
    for name in names:
        try:
            host = inventory.get_host(name) if inventory is not None else None
            private_names = _private_rescue_facts(manager, host.name if host else name)
            if private_names:
                from ansible.vars.hostvars import HostVarsVars

                if host is None:
                    continue
                # Remove only the private fact layer before precedence is
                # applied. This also keeps aliases from resolving it.
                public_manager = object.__new__(type(manager))
                vars(public_manager).update(vars(manager))
                cache = manager._nonpersistent_fact_cache.copy()
                cache[host.name] = {key: value for key, value in cache[host.name].items() if key not in private_names}
                public_manager._nonpersistent_fact_cache = cache
                raw = public_manager.get_vars(host=host, include_hostvars=False)
                raw = {key: value for key, value in raw.items()
                       if key not in ("vars", "hostvars")}
                raw["vars"] = dict(raw)
                host_kw = {"host": name} if _core_version() >= (2, 19) else {}
                source = HostVarsVars(raw, loader=hostvars._loader, **host_kw)
            else:
                raw = raw_get(name) if raw_get is not None and preserve_keys else {}
                source = hostvars[name]
            keys = list(source)
        except Exception:
            continue
        safe = {}
        for key in keys:
            if key in ("hostvars", "vars"):
                continue
            try:
                value = source[key]
                if not preserve_keys or not _contains_typed_mapping_keys(value):
                    json.dumps(value, default=str)
            except Exception:
                # Core can reject typed keys when reading a register. Keep
                # raw data only when its ordinary source-host view failed.
                value = raw.get(key) if isinstance(raw, dict) else None
                if not preserve_keys or not _contains_typed_mapping_keys(value):
                    continue
                try:
                    packed = _pack_delegated_value(value)
                    if packed["undefined"] or packed.get("errors"):
                        continue
                except Exception:
                    continue
            safe[key] = value
        out[str(name)] = safe
    return out


def _build_ssh_command(conn, remote):
    build = conn._build_command
    host = _conn_option(conn, "host")
    if not host:
        host = getattr(getattr(conn, "_play_context", None), "remote_addr", None)
    if not host:
        host = getattr(conn, "host", None)
    ssh_executable = _conn_option(conn, "ssh_executable")
    if not ssh_executable:
        ssh_executable = getattr(getattr(conn, "_play_context", None), "ssh_executable", None)
    if not ssh_executable:
        ssh_executable = "ssh"
    try:
        params = inspect.signature(build).parameters
    except (TypeError, ValueError):
        params = {}
    if "subsystem" in params:
        return build(ssh_executable, "ssh", host, remote)
    return build("ssh", host, remote)


def _conn_option(conn, name):
    try:
        return conn.get_option(name)
    except Exception:
        return None


def _drop_omit_values(value, omit_tokens):
    if isinstance(value, dict):
        return {
            key: _drop_omit_values(item, omit_tokens)
            for key, item in value.items()
            if not (isinstance(item, str) and item in omit_tokens)
        }
    if isinstance(value, list):
        return [_drop_omit_values(item, omit_tokens) for item in value]
    return value


class _Channel:
    """A persistent bidirectional length-prefixed frame channel to an agent process
    (local python or ssh-to-remote-python). Frame 1 out is the agent source; after
    that it's JSON frames both ways."""

    def __init__(self, argv, pass_fds=()):
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0, pass_fds=pass_fds)

    def send_blob(self, text):
        self._write(text.encode("utf-8", "surrogateescape"))

    def send(self, obj):
        self._write(json.dumps(obj, default=str).encode("utf-8", "surrogateescape"))

    def _write(self, b):
        self.proc.stdin.write(struct.pack(">I", len(b)))
        self.proc.stdin.write(b)
        self.proc.stdin.flush()

    def recv(self):
        head = _readn(self.proc.stdout, 4)
        if not head:
            return None
        (n,) = struct.unpack(">I", head)
        body = _readn(self.proc.stdout, n)
        if body is None:
            return None
        try:
            return json.loads(body.decode("utf-8", "surrogateescape"))
        except Exception:
            return None

    def stderr(self):
        try:
            return (self.proc.stderr.read() or b"").decode("utf-8", "surrogateescape").strip()
        except Exception:
            return ""

    def close(self):
        for f in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                f.close()
            except Exception:
                pass
        try:
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def _readn(stream, n):
    chunks = []
    got = 0
    while got < n:
        c = stream.read(n - got)
        if not c:
            return None
        chunks.append(c)
        got += len(c)
    return b"".join(chunks)
