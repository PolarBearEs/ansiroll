#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ansiroll runtime.

This module is inlined verbatim into every compiled playbook file. It is a tiny,
self-contained Ansible-task interpreter: it templates args/conditions, evaluates
``when``, expands ``loop``, runs embedded AnsiballZ modules as subprocesses, tracks
``register``/facts, runs handlers, and emits one framed JSON result per task on
stdout so a controller (or a human) can reconstruct normal ok/changed/failed
reporting.

Generated agents bundle Jinja2 and MarkupSafe, so target hosts need only the
Python standard library. Importing this module directly uses the controller's
installed Jinja2.
"""

import datetime as _dt
import base64
import json
import os
import struct
import subprocess
import sys
import time
import hashlib
import re
import shlex
from contextlib import contextmanager
from itertools import groupby

RESULT_MARKER = "__ANSIROLL_RESULT__"
STATE_MARKER = "__ANSIROLL_STATE__"
STATS_MARKER = "__ANSIROLL_STATS__"


# ansible-core version of the controller that compiled this artifact. The
# compiler bakes it into the generated source; target hosts usually have no
# Ansible, and any Ansible they do have is irrelevant to the compiled semantics.
_ANSIROLL_CORE_VERSION = None


def _core_at_least(major, minor):
    """Feature gate on the compiling controller's ansible-core version."""
    version = _ANSIROLL_CORE_VERSION
    if not version:
        # Imported directly on the controller (not compiled): use its Ansible.
        try:
            from ansible.release import __version__ as version
        except Exception:
            return False
    match = re.match(r"^(\d+)\.(\d+)", str(version))
    return bool(match) and tuple(map(int, match.groups())) >= (major, minor)

# Persistent worker: one long-lived process per privilege level. It keeps each
# module's decoded zip in RAM (decode-once) and forks a child per task -- the fork
# inherits the already-decoded zips and compiled module_utils copy-on-write, so the
# per-task cost is just running the module body. Isolation (modules call sys.exit /
# mutate globals) comes from the fork. Protocol: length-prefixed JSON frames on
# stdin (requests) and stdout (responses); module stdout/stderr are captured per
# task via pipes so they never corrupt the response channel.
_WORKER_CODE = None
# __ANSIROLL_WORKER_CODE__


def _worker_source():
    global _WORKER_CODE
    if _WORKER_CODE is None:
        with open(os.path.join(os.path.dirname(__file__), "worker.py"), "r") as worker_file:
            _WORKER_CODE = worker_file.read()
    return _WORKER_CODE

# __ANSIROLL_JINJA_BUNDLE__
import jinja2  # type: ignore


# Sentinel for the `omit` magic variable; args/env entries equal to it are
# dropped before the module runs (mirrors Ansible's omit placeholder).
_OMIT = "__omit_place_holder__ansiroll"


class _UnsafeTemplate(str):
    """A string value whose template-looking contents must remain literal."""


class _UntrustedConditionValue(str):
    """A `!unsafe` value retaining its provenance during condition checks."""


class _TrustedAsTemplateValue(str):
    """A string produced by set_fact and trusted for later templating."""


def _trust_set_fact_value(value):
    if isinstance(value, str):
        return _TrustedAsTemplateValue(value)
    if isinstance(value, dict):
        return {
            _trust_set_fact_value(k): _trust_set_fact_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_trust_set_fact_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_trust_set_fact_value(v) for v in value)
    return value


# Overlay key holding variables that outrank set_fact/register: include
# params, include loop vars and rescue magic vars (VariableManager.get_vars
# merges include params after the nonpersistent fact cache).
_HIGH_VARS_KEY = "__ansiroll_high_vars__"
_INCLUDE_PARAMS_KEY = "__ansiroll_include_params__"


def _with_high_vars(overlay, values):
    out = dict(overlay)
    out.update(values)
    high = dict(overlay.get(_HIGH_VARS_KEY) or {})
    high.update(values)
    out[_HIGH_VARS_KEY] = high
    return out


def _override_ctx(task, res, ctx):
    """Variables visible to changed_when/failed_when.

    TaskExecutor exposes the registered result and any facts the action
    returned, never the bare result keys (``rc``, ``stdout``...).
    """
    ectx = dict(ctx)
    facts = res.get("ansible_facts")
    if isinstance(facts, dict):
        ectx.update(facts)
    if task.get("register"):
        ectx[task["register"]] = res
    return ectx


def _strip_omit(args):
    if isinstance(args, dict):
        return {k: v for k, v in args.items() if v != _OMIT}
    return args


def _fail_on_undef(value):
    """Raise the captured error of any Undefined nested in module args."""
    if isinstance(value, dict):
        for v in value.values():
            _fail_on_undef(v)
    elif isinstance(value, list):
        for v in value:
            _fail_on_undef(v)
    elif _is_undef(value):
        value._fail_with_undefined_error()
    return value


def _contains_untrusted_condition_value(value):
    if isinstance(value, _UntrustedConditionValue):
        return True
    if isinstance(value, dict):
        return any(
            _contains_untrusted_condition_value(item)
            for item in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_untrusted_condition_value(item) for item in value)
    return False


def _fail_on_direct_undef(value):
    """Raise direct/list errors without forcing every value in a mapping."""
    if isinstance(value, (list, tuple)):
        for v in value:
            _fail_on_direct_undef(v)
    elif _is_undef(value):
        value._fail_with_undefined_error()
    return value


class TaskFailed(Exception):
    """Raised when a task fails and is not ignored (drives block/rescue)."""

    def __init__(self, name, task=None, result=None):
        super(TaskFailed, self).__init__(name)
        self.task = task or {"name": name}
        self.result = result or {"failed": True, "msg": str(name)}


class MissingHandler(Exception):
    """A changed task requested a handler that has not been loaded."""


class UntrustedConditional(Exception):
    """A conditional contained an embedded template and was refused."""


class _UnsupportedLookup(Exception):
    """No built-in implementation for this lookup plugin name."""


def _to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "on", "1")
    return bool(value)


def _f_combine(d, *others, **kwargs):
    recursive = kwargs.get("recursive", False)

    def merge(a, b):
        out = dict(a)
        for k, v in b.items():
            if recursive and isinstance(out.get(k), dict) and isinstance(v, dict):
                out[k] = merge(out[k], v)
            else:
                out[k] = v
        return out
    result = dict(d or {})
    for o in others:
        result = merge(result, o or {})
    return result


def _f_dict2items(d, key_name="key", value_name="value"):
    return [{key_name: k, value_name: v} for k, v in (d or {}).items()]


def _f_items2dict(lst, key_name="key", value_name="value"):
    return {i[key_name]: i[value_name] for i in (lst or [])}


def _f_regex_replace(s, pattern, repl="", count=0):
    import re
    return re.sub(pattern, repl, str(s), count=count)


def _f_regex_search(s, pattern, *args, **kwargs):
    import re
    m = re.search(pattern, str(s))
    if not m:
        return None
    if args:
        groups = [m.group(int(a[1:]) if str(a).startswith("\\") else a) for a in args]
        return groups
    return m.group(0)


def _f_regex_findall(s, pattern, multiline=False, ignorecase=False):
    import re
    flags = 0
    if multiline:
        flags |= re.M
    if ignorecase:
        flags |= re.I
    return re.findall(pattern, str(s), flags)


def _f_mandatory(v):
    if _is_undef(v):
        raise ValueError("Mandatory variable not defined.")
    return v


def _f_b64decode(v):
    import base64
    return base64.b64decode(_to_text_str(v)).decode("utf-8", "surrogateescape")


def _f_b64encode(v):
    import base64
    return base64.b64encode(_to_text_str(v).encode("utf-8", "surrogateescape")).decode("ascii")


def _to_text_str(v):
    return v if isinstance(v, str) else ("" if v is None else str(v))


def _f_ternary(condition, true_val, false_val=None, none_val=None):
    if condition is None and none_val is not None:
        return none_val
    return true_val if _to_bool(condition) else false_val


def _f_hash(v, hashtype="sha1"):
    import hashlib
    return hashlib.new(hashtype, _to_text_str(v).encode("utf-8", "surrogateescape")).hexdigest()


def _f_to_uuid(v, namespace="361E6D51-FAEC-444A-9079-341386DA8E2E"):
    import uuid
    return str(uuid.uuid5(uuid.UUID(namespace), _to_text_str(v)))


def _f_unique(seq):
    out = []
    for x in seq or []:
        if x not in out:
            out.append(x)
    return out


def _f_flatten(seq, levels=None):
    out = []
    for x in seq or []:
        if isinstance(x, (list, tuple)):
            if levels is None or levels > 0:
                out.extend(_f_flatten(x, None if levels is None else levels - 1))
            else:
                out.append(x)
        else:
            out.append(x)
    return out


def _yaml_module():
    """Load Ansiroll's private, pure-Python YAML implementation.

    Generated agents get it from the in-memory import bundle (and are exec'd
    without ``__file__``).  Importing the source package directly puts the
    shipped ZIP first on sys.path so no other ``ansiroll_yaml`` can shadow it.
    """
    here = globals().get("__file__")
    if here:
        bundle = os.path.join(os.path.dirname(here), "vendor", "pyyaml-py35.zip")
        if os.path.isfile(bundle) and bundle not in sys.path:
            sys.path.insert(0, bundle)
    import ansiroll_yaml
    return ansiroll_yaml


def _yaml_plain_key(k, memo):
    """Convert a mapping key.  Keys are hashable, so a key tuple holds no
    mutable container and cannot be part of a cycle; keep it a tuple."""
    if isinstance(k, str):
        return "".join([k]) if type(k) is not str else k
    if not isinstance(k, tuple):
        return k
    if id(k) not in memo:
        memo[id(k)] = (k, tuple(_yaml_plain_key(x, memo) for x in k))
    return memo[id(k)][1]


def _yaml_plain(v, memo=None, key_memo=None):
    """Materialize runtime wrappers (lazy vars, trusted strings) into the
    built-in types PyYAML's SafeDumper can represent.

    Shared and self-referencing containers keep their identity: each output
    container is registered before its children are converted, so PyYAML can
    still emit aliases.  Tuple values become lists (SafeDumper writes both as
    sequences) so they can be registered up front too.
    """
    if memo is None:
        memo, key_memo = {}, {}
    if isinstance(v, str):
        return "".join([v]) if type(v) is not str else v
    if not isinstance(v, (dict, list, tuple)):
        return v
    # Lazy wrappers are rebuilt on every access; key them by the raw
    # container they wrap so recursion through them is still detected, plus
    # the context they were created under (one raw container can render
    # differently in two contexts).  ``_origin`` stays stable across accesses,
    # unlike the fresh context dict a dynamic wrapper builds each time.
    origin = getattr(v, "_origin", None)
    key = (id(getattr(v, "_raw", v)), id(origin) if origin is not None else None)
    if key in memo:
        return memo[key][1]
    if isinstance(v, dict):
        out = {}
        memo[key] = (v, out)
        for k, x in v.items():
            out[_yaml_plain_key(k, key_memo)] = _yaml_plain(x, memo, key_memo)
        return out
    out = []
    memo[key] = (v, out)
    out.extend(_yaml_plain(x, memo, key_memo) for x in v)
    return out


def _f_to_yaml(v, **kwargs):
    options = dict(kwargs)
    options.setdefault("default_flow_style", False)
    return _yaml_module().safe_dump(_yaml_plain(v), **options)


def _f_from_yaml(v):
    return _yaml_module().safe_load(v)


def _f_union(a, b):
    out = list(a or [])
    for x in (b or []):
        if x not in out:
            out.append(x)
    return _f_unique(out)


def _f_intersect(a, b):
    b = b or []
    return _f_unique([x for x in (a or []) if x in b])


def _f_difference(a, b):
    b = b or []
    return _f_unique([x for x in (a or []) if x not in b])


def _f_symmetric_difference(a, b):
    a = a or []
    b = b or []
    return _f_unique([x for x in a if x not in b] + [x for x in b if x not in a])


def _f_quote(v):
    try:
        import shlex
        return shlex.quote(_to_text_str(v))
    except Exception:
        return _to_text_str(v)


def _contains_vault(v):
    if isinstance(v, dict):
        if set(v.keys()) == {"__ansiroll_vault__"}:
            return True
        return any(_contains_vault(x) for x in v.values())
    if isinstance(v, (list, tuple)):
        return any(_contains_vault(x) for x in v)
    return False


def _f_to_json(v, **kwargs):
    if _contains_vault(v):
        raise ValueError("The variable contains an undecryptable variable")
    return json.dumps(v, **kwargs)


def _f_unvault(v, secret):
    """Decrypt an Ansible Vault AES256 scalar using its supplied secret."""
    if not (isinstance(v, dict) and set(v.keys()) == {"__ansiroll_vault__"}):
        return v
    import hashlib
    import hmac
    text = v["__ansiroll_vault__"]
    lines = text.strip().splitlines()
    if not lines or not lines[0].startswith("$ANSIBLE_VAULT;1.1;AES256"):
        raise ValueError("Unsupported vault format")
    try:
        payload = bytes.fromhex("".join(lines[1:]))
        salt_hex, digest_hex, ciphertext_hex = payload.split(b"\n", 2)
        salt = bytes.fromhex(salt_hex.decode("ascii"))
        expected_digest = bytes.fromhex(digest_hex.decode("ascii"))
        ciphertext = bytes.fromhex(ciphertext_hex.decode("ascii"))
        derived = hashlib.pbkdf2_hmac(
            "sha256", str(secret).encode("utf-8"), salt, 10000, dklen=80)
        key, hmac_key, iv = derived[:32], derived[32:64], derived[64:80]
        actual_digest = hmac.new(hmac_key, ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise ValueError("HMAC verification failed")
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            decryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
            plaintext = decryptor.update(ciphertext) + decryptor.finalize()
        except ImportError:
            proc = subprocess.run(
                ["openssl", "enc", "-aes-256-ctr", "-d", "-K", key.hex(),
                 "-iv", iv.hex(), "-nopad"], input=ciphertext,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if proc.returncode:
                raise ValueError(proc.stderr.decode("utf-8", "replace").strip())
            plaintext = proc.stdout
        pad = plaintext[-1]
        if not 1 <= pad <= 16 or plaintext[-pad:] != bytes([pad]) * pad:
            raise ValueError("Invalid vault padding")
        return _UnsafeTemplate(plaintext[:-pad].decode("utf-8"))
    except (ValueError, IndexError) as e:
        raise ValueError("Unable to decrypt vault value: %s" % e)


# A growing subset of Ansible's Jinja2 filters (v1 fidelity best-effort, pure stdlib).
_ANSIBLE_FILTERS = {
    "bool": _to_bool,
    "to_json": _f_to_json,
    "to_nice_json": lambda v, **k: _f_to_json(v, indent=4, sort_keys=True, **k),
    "from_json": lambda v: json.loads(v),
    "to_yaml": _f_to_yaml,
    "to_nice_yaml": _f_to_yaml,
    "from_yaml": _f_from_yaml,
    "unvault": _f_unvault,
    "combine": _f_combine,
    "dict2items": _f_dict2items,
    "items2dict": _f_items2dict,
    "regex_replace": _f_regex_replace,
    "regex_search": _f_regex_search,
    "regex_findall": _f_regex_findall,
    "regex_escape": lambda v, **k: __import__("re").escape(_to_text_str(v)),
    "mandatory": _f_mandatory,
    "b64decode": _f_b64decode,
    "b64encode": _f_b64encode,
    "ternary": _f_ternary,
    "hash": _f_hash,
    "checksum": lambda v: _f_hash(v, "sha1"),
    "to_uuid": _f_to_uuid,
    "unique": _f_unique,
    "union": _f_union,
    "intersect": _f_intersect,
    "difference": _f_difference,
    "symmetric_difference": _f_symmetric_difference,
    "flatten": _f_flatten,
    "quote": _f_quote,
    "type_debug": lambda v: type(v).__name__,
    "basename": lambda v: os.path.basename(str(v)),
    "dirname": lambda v: os.path.dirname(str(v)),
    "expanduser": lambda v: os.path.expanduser(str(v)),
    "realpath": lambda v: os.path.realpath(str(v)),
    "splitext": lambda v: list(os.path.splitext(str(v))),
    "split": lambda v, separator=None, maxsplit=-1: str(v).split(separator, maxsplit),
    "default": lambda v, d="", b=False: (d if ((b and not v) or _is_undef(v)) else v),
}


def _vkey(s):
    import re
    key = []
    for p in re.split(r"[.\-_+~]", str(s)):
        key.append((1, int(p)) if p.isdigit() else (0, p))
    return key


def _test_version(value, version, operator="eq", strict=None, version_type=None):
    import operator as _op
    ops = {"==": _op.eq, "=": _op.eq, "eq": _op.eq, "!=": _op.ne, "ne": _op.ne,
           ">": _op.gt, "gt": _op.gt, ">=": _op.ge, "ge": _op.ge,
           "<": _op.lt, "lt": _op.lt, "<=": _op.le, "le": _op.le}
    fn = ops.get(str(operator))
    if fn is None:
        return False
    try:
        from packaging.version import Version
        return fn(Version(str(value)), Version(str(version)))
    except Exception:
        return fn(_vkey(value), _vkey(version))


def _test_match(value, pattern):
    import re
    return re.match(pattern, str(value)) is not None


def _test_search(value, pattern):
    import re
    return re.search(pattern, str(value)) is not None


def _as_result(v):
    return v if isinstance(v, dict) else {}


def _test_success(result):
    return not _as_result(result).get("failed", False)


# A subset of Ansible's Jinja2 tests (pure stdlib).
_ANSIBLE_TESTS = {
    "version": _test_version,
    "version_compare": _test_version,
    "match": _test_match,
    "search": _test_search,
    "regex": lambda v, pattern, **k: _test_search(v, pattern),
    "succeeded": _test_success,
    "success": _test_success,
    "successful": _test_success,
    "failed": lambda r: _as_result(r).get("failed", False),
    "failure": lambda r: _as_result(r).get("failed", False),
    "changed": lambda r: _as_result(r).get("changed", False),
    "change": lambda r: _as_result(r).get("changed", False),
    "skipped": lambda r: _as_result(r).get("skipped", False),
    "skip": lambda r: _as_result(r).get("skipped", False),
    "contains": lambda seq, value: value in seq,
    "truthy": lambda v, convert_bool=False: _to_bool(v) if convert_bool else bool(v),
    "falsy": lambda v, convert_bool=False: (not _to_bool(v)) if convert_bool else (not bool(v)),
    "ansible._protomatter.tagged_with": (
        lambda value, tag: (
            str(tag) == "TrustedAsTemplate"
            and isinstance(value, _TrustedAsTemplateValue)
        )
    ),
}


def _lookup_values(name, args, kwargs, ctx=None):
    name = str(name).rsplit(".", 1)[-1]
    if name == "vars":
        vals = []
        for a in args:
            key = str(a)
            if ctx is not None and key in ctx and not _is_undef(ctx[key]):
                vals.append(ctx[key])
            elif "default" in kwargs:
                vals.append(kwargs["default"])
            else:
                raise RuntimeError("lookup vars: no variable named %r" % key)
        return vals
    if name == "env":
        default = kwargs.get("default", "")
        controller_env = (ctx or {}).get("__ansiroll_controller_env")
        if isinstance(controller_env, dict):
            return [controller_env.get(str(a), default) for a in args]
        return [os.environ.get(str(a), default) for a in args]
    if name == "config":
        controller_config = (ctx or {}).get(
            "__ansiroll_controller_config"
        )
        if isinstance(controller_config, dict):
            return [controller_config.get(str(a)) for a in args]
        return [None for _a in args]
    if name == "file":
        vals = []
        lstrip = kwargs.get("lstrip", False)
        rstrip = kwargs.get("rstrip", True)
        dirs = (ctx or {}).get("__ansiroll_search_dirs") or []
        for a in args:
            path = os.path.expanduser(str(a))
            if not os.path.isabs(path):
                for d in dirs:
                    cand = os.path.join(d, path)
                    if os.path.exists(cand):
                        path = cand
                        break
            with open(path) as f:
                text = f.read()
            if lstrip:
                text = text.lstrip()
            if rstrip:
                text = text.rstrip("\r\n")
            vals.append(text)
        return vals
    if name == "pipe":
        vals = []
        for a in args:
            proc = subprocess.run(str(a), shell=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, universal_newlines=True)
            if proc.returncode != 0:
                raise RuntimeError("lookup plugin 'pipe' failed: command %r returned "
                                   "rc=%d: %s" % (a, proc.returncode, proc.stderr.strip()))
            vals.append(proc.stdout.rstrip("\n"))
        return vals
    return _loop_lookup(name, list(args))


def _flatten_deep(terms):
    out = []
    for t in terms:
        if isinstance(t, (list, tuple)):
            out.extend(_flatten_deep(list(t)))
        elif t is not None:
            out.append(t)
    return out


def _loop_lookup(name, terms):
    """Shared semantics for the loop-shaped lookup plugins."""
    if name == "list":
        return list(terms)
    if name == "items":
        out = []
        for t in terms:
            if isinstance(t, list):
                out.extend(t)
            else:
                out.append(t)
        return out
    if name == "flattened":
        return _flatten_deep(terms)
    if name == "together":
        import itertools
        return [list(x) for x in itertools.zip_longest(*terms, fillvalue=None)]
    if name == "dict":
        if len(terms) == 1 and isinstance(terms[0], dict):
            return [{"key": k, "value": v} for k, v in terms[0].items()]
        raise RuntimeError("with_dict/dict lookup plugin expects a dictionary")
    if name == "nested":
        import itertools
        lists = [t if isinstance(t, list) else [t] for t in terms]
        return [list(x) for x in itertools.product(*lists)]
    if name == "indexed_items":
        out = []
        for t in terms:
            items = t if isinstance(t, list) else [t]
            out.extend(items)
        return [[i, item] for i, item in enumerate(out)]
    raise _UnsupportedLookup(name)


def _jinja_now(utc=False, fmt=None):
    dt = _dt.datetime.utcnow() if utc else _dt.datetime.now()
    return dt.strftime(fmt) if fmt else dt


def _lookup(name, *args, **kwargs):
    # Ansible's lookup(): single value as-is, several joined with commas
    # (unless wantlist=True).
    ctx = kwargs.pop("_ansiroll_ctx", None)
    wantlist = kwargs.pop("wantlist", False)
    vals = _lookup_values(name, args, kwargs, ctx)
    if wantlist:
        return vals
    if len(vals) == 1:
        return vals[0]
    return ",".join(str(v) for v in vals)


def _query(name, *args, **kwargs):
    ctx = kwargs.pop("_ansiroll_ctx", None)
    kwargs.pop("wantlist", None)
    return _lookup_values(name, args, kwargs, ctx)


def _lazy_container(value, rt, ctx, dynamic=False, origin=None):
    if isinstance(value, dict):
        return _LazyTplDict(value, rt, ctx, dynamic, origin)
    if isinstance(value, list):
        return _LazyTplList(value, rt, ctx, dynamic, origin)
    return value


def _lazy_wrap(value, rt, ctx, dynamic=False, origin=None):
    if isinstance(value, str):
        if getattr(rt, "_strict_var_resolution", False):
            return rt.template(value, ctx, strict=True)
        return rt.template(value, ctx)
    if isinstance(value, dict) and set(value.keys()) == {"__ansiroll_unsafe__"}:
        return _UntrustedConditionValue(value["__ansiroll_unsafe__"])
    if isinstance(value, dict) and set(value.keys()) == {"__ansiroll_vault__"}:
        return value
    return _lazy_container(value, rt, ctx, dynamic, origin)


class _LazyTplDict(dict):
    """A vars container whose values render only when accessed."""

    def __init__(self, raw, rt, ctx, dynamic=False, origin=None):
        dict.__init__(self, raw)
        self._raw = raw
        self._rt = rt
        self._ctx = ctx
        # The context this view tree was created under; children share it.
        self._origin = ctx if origin is None else origin
        self._dynamic = dynamic

    def _render_ctx(self):
        if not self._dynamic:
            return self._ctx
        ctx = dict(self._ctx)
        ctx.update(self._rt.vars)
        return ctx

    def __getitem__(self, k):
        return _lazy_wrap(
            dict.__getitem__(self, k), self._rt, self._render_ctx(), self._dynamic,
            self._origin)

    def get(self, k, default=None):
        try:
            return self[k]
        except KeyError:
            return default

    def items(self):
        return [(k, self[k]) for k in dict.keys(self)]

    def values(self):
        return [self[k] for k in dict.keys(self)]

    def copy(self):
        return {k: self[k] for k in dict.keys(self)}

    def __eq__(self, other):
        mine = {k: self[k] for k in dict.keys(self)}
        return mine == (other.copy() if isinstance(other, _LazyTplDict) else other)

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = None


class _HandlerNameVars(_LazyTplDict):
    """The host-independent vars available while matching a handler name."""

    def __init__(self, raw, rt, ctx):
        _LazyTplDict.__init__(self, raw, rt, ctx)
        self._resolving = set()

    def __getitem__(self, key):
        if key in self._resolving:
            return _captured("Recursive loop detected in handler name variable: %s" % key)
        self._resolving.add(key)
        try:
            return _LazyTplDict.__getitem__(self, key)
        finally:
            self._resolving.remove(key)


class _PlayVarTemplate(str):
    """Source of a top-level play variable, resolved in the consuming context."""


class _PlayVarScope(dict):
    """Cache referenced play templates for a single Jinja evaluation."""

    def __init__(self, ctx, templater, strict):
        super().__init__(ctx)
        self["vars"] = self
        self.templater = templater
        self.strict = strict
        self.resolving = set()
        self.resolved = {}
        self.accesses = 0

    def __getitem__(self, key):
        value = dict.__getitem__(self, key)
        if not isinstance(value, _PlayVarTemplate):
            return value
        self.accesses += 1
        if key in self.resolving:
            return _captured("Recursive loop detected in play variable: %s" % key)
        if key not in self.resolved:
            self.resolving.add(key)
            try:
                self.resolved[key] = self.templater.render_string(
                    value, self, strict=self.strict)
            finally:
                self.resolving.remove(key)
        return self.resolved[key]

    def get(self, key, default=None):
        return self[key] if key in self else default

    def items(self):
        for key in self:
            # Values from the variable mapping have already been interpreted.
            # Keep delimiter-shaped data literal in the enclosing expression.
            self.accesses += 1
            yield key, self[key]

    def values(self):
        for _, value in self.items():
            yield value


class _TemplateContext(jinja2.runtime.Context):
    def resolve_or_missing(self, key):
        value = super().resolve_or_missing(key)
        scope = self.parent.get("vars")
        if key not in self.vars and isinstance(scope, _HandlerNameVars) and key in scope:
            return scope[key]
        if isinstance(value, _PlayVarTemplate) and isinstance(scope, _PlayVarScope):
            return scope[key]
        return value


class _LazyTplList(list):
    """A vars list whose items render only when accessed."""

    def __init__(self, raw, rt, ctx, dynamic=False, origin=None):
        list.__init__(self, raw)
        self._raw = raw
        self._rt = rt
        self._ctx = ctx
        self._origin = ctx if origin is None else origin
        self._dynamic = dynamic

    def _render_ctx(self):
        if not self._dynamic:
            return self._ctx
        ctx = dict(self._ctx)
        ctx.update(self._rt.vars)
        return ctx

    def __getitem__(self, i):
        v = list.__getitem__(self, i)
        if isinstance(i, slice):
            return [
                _lazy_wrap(x, self._rt, self._render_ctx(), self._dynamic,
                           self._origin)
                for x in v
            ]
        return _lazy_wrap(v, self._rt, self._render_ctx(), self._dynamic,
                          self._origin)

    def __iter__(self):
        for v in list.__iter__(self):
            yield _lazy_wrap(v, self._rt, self._render_ctx(), self._dynamic,
                             self._origin)

    def __eq__(self, other):
        return list(iter(self)) == (list(iter(other)) if isinstance(other, _LazyTplList) else other)

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = None


def _concretize(value):
    """Consume lazy iterators (selectattr/map/... results) so errors surface at
    template time, like Ansible's templar does."""
    import types
    if isinstance(value, (types.GeneratorType, map, filter)):
        return list(value)
    return value


def _captured(msg):
    """An error captured as an Undefined: raises with `msg` when actually used
    (strict: even stringification raises), but inspectable via
    `ansible._protomatter.dump_object` (like Ansible 2.19's captured errors)."""
    return jinja2.StrictUndefined(hint=msg)


def _dump_object(v):
    if _is_undef(v):
        msg = getattr(v, "_undefined_hint", None) or "undefined value"
        return {"exception": {"message": msg}}
    return {"value": v}


def _jinja_undef(hint=None):
    if hint is not None and not isinstance(hint, str):
        return _captured("The undef() hint argument must be of type 'str', got %r"
                         % type(hint).__name__)
    return _captured(hint or "Mandatory variable has not been overridden")


def _wrap_filter(fn):
    """Filter errors are captured, not raised (they surface when used)."""
    def wrapped(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:
            return _captured(str(e))
    return wrapped


class _AnsirollUndefined(jinja2.ChainableUndefined):
    """ChainableUndefined, except `x in undefined` raises (jinja 3.1 silently
    returns False, Ansible errors)."""

    def __contains__(self, item):
        self._fail_with_undefined_error()


class _StrictAnsirollUndefined(jinja2.StrictUndefined):
    # Keep nested default filters working while rejecting undefined output.
    __getattr__ = _AnsirollUndefined.__getattr__
    __getitem__ = _AnsirollUndefined.__getitem__


class _PluginDict(dict):
    """env.filters/env.tests replacement that reports plugins which exist but
    failed to load, with Ansible's error message."""

    def __init__(self, kind, errors):
        super().__init__()
        self._kind = kind
        self._errors = errors  # name -> load error text

    def _check(self, name):
        if not dict.__contains__(self, name) and name in self._errors:
            raise jinja2.TemplateError("%s plugin '%s' failed to load: %s"
                                       % (self._kind, name, self._errors[name]))

    def __contains__(self, name):
        self._check(name)
        return dict.__contains__(self, name)

    def get(self, name, default=None):
        self._check(name)
        return dict.get(self, name, default)

    def __getitem__(self, name):
        self._check(name)
        return dict.__getitem__(self, name)


def _captured_jinja_error(error, syntax=False):
    """Return an Ansible-shaped lazy error for a Jinja compile/render failure."""
    message = str(error)
    missing = re.match(r"no (filter|test) named (.+)", message, re.I)
    if missing:
        message = "No %s named %s" % (
            missing.group(1).lower(), missing.group(2)
        )
    elif syntax:
        message = "Syntax error in template: %s" % message
    return _captured(message)


class _Templater:
    """Thin wrapper over the Jinja2 runtime."""

    def __init__(self):
        self._expr_cache = {}
        self._template_cache = {}
        self._embedded_constant_cache = {}
        self._current_ctx = {}
        self._plugin_errors = {"filter": {}, "test": {}}
        self._lookup_sources = {}   # name -> python source (loaded on demand)
        self._lookup_loaded = {}    # name -> run(terms, variables) callable
        self._lookup_errors = {}    # name -> load error text
        self.env = jinja2.Environment(
            undefined=_AnsirollUndefined,
            keep_trailing_newline=True,
            finalize=_concretize,
        )
        self.env.context_class = _TemplateContext
        filters = _PluginDict("filter", self._plugin_errors["filter"])
        filters.update(self.env.filters)
        self.env.filters = filters
        tests = _PluginDict("test", self._plugin_errors["test"])
        tests.update(self.env.tests)
        self.env.tests = tests
        # A few commonly-used Ansible filters/tests, best-effort.
        for fname, func in _ANSIBLE_FILTERS.items():
            self.env.filters.setdefault(fname, _wrap_filter(func))
        for tname, tfunc in _ANSIBLE_TESTS.items():
            self.env.tests.setdefault(tname, tfunc)
        self.env.filters["ansible._protomatter.dump_object"] = _dump_object
        # Bind the render context so context-dependent lookups ('vars') work.
        self.env.globals.setdefault("lookup", self._g_lookup)
        self.env.globals.setdefault("query", self._g_query)
        self.env.globals.setdefault("q", self._g_query)
        self.env.globals.setdefault("now", _jinja_now)
        self.env.globals.setdefault("undef", _jinja_undef)
        self._strict_env = self.env.overlay(undefined=_StrictAnsirollUndefined)

    def _g_lookup(self, name, *a, **k):
        try:
            a = tuple(self._template_lookup_arg(v) for v in a)
            k = {key: self._template_lookup_arg(value) for key, value in k.items()}
            return _lookup(name, *a, _ansiroll_ctx=self._current_ctx, **k)
        except _UnsupportedLookup:
            vals = self._plugin_lookup(name, a, k)
            if _is_undef(vals):
                return vals
            if k.pop("wantlist", False):
                return vals
            return vals[0] if len(vals) == 1 else ",".join(str(v) for v in vals)
        except Exception as e:
            return _captured(str(e))

    def _g_query(self, name, *a, **k):
        try:
            a = tuple(self._template_lookup_arg(v) for v in a)
            k = {key: self._template_lookup_arg(value) for key, value in k.items()}
            return _query(name, *a, _ansiroll_ctx=self._current_ctx, **k)
        except _UnsupportedLookup:
            return self._plugin_lookup(name, a, k)
        except Exception as e:
            return _captured(str(e))

    def _template_lookup_arg(self, value):
        """Render template constants nested inside lookup terms/options."""
        if isinstance(value, dict):
            return {self._template_lookup_arg(k): self._template_lookup_arg(v)
                    for k, v in value.items()}
        if isinstance(value, list):
            return [self._template_lookup_arg(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self._template_lookup_arg(v) for v in value)
        if isinstance(value, str):
            rendered = self.render_string(value, self._current_ctx)
            if rendered is not value and isinstance(rendered, (dict, list, tuple)):
                return self._template_lookup_arg(rendered)
            return rendered
        return value

    def register_plugins(self, plugins):
        """Load baked role/playbook-adjacent Jinja plugin sources."""
        for p in (plugins or []):
            kind, name, source = p.get("kind"), p.get("name"), p.get("source")
            if kind == "lookup":
                self._lookup_sources.setdefault(name, source)
                continue
            attr, target = (("filters", self.env.filters) if kind == "filter"
                            else ("tests", self.env.tests))
            try:
                ns = {"__name__": "ansiroll_%s_%s" % (kind, name)}
                exec(compile(source, "<%s_plugins/%s.py>" % (kind, name), "exec"), ns)
                module_cls = ns.get("FilterModule" if kind == "filter" else "TestModule")
                if module_cls is None:
                    continue
                provided = getattr(module_cls(), attr)()
            except Exception:
                continue  # a whole-file failure cannot be attributed to a name
            for pname, fn in provided.items():
                try:
                    # Mirror Ansible's registration-time plugin inspection: a
                    # plugin whose wrapper raises here "fails to load".
                    getattr(fn, "accept_args_markers", None)
                except Exception as e:
                    self._plugin_errors[kind][pname] = str(e)
                    continue
                target[pname] = _wrap_filter(fn) if kind == "filter" else fn

    def _plugin_lookup(self, name, args, kwargs):
        """Run a baked lookup plugin; errors come back captured."""
        if name in self._lookup_loaded:
            try:
                return list(self._lookup_loaded[name](list(args),
                                                      dict(self._current_ctx or {}),
                                                      **kwargs))
            except Exception as e:
                return _captured("lookup plugin '%s' failed: %s" % (name, e))
        if name in self._lookup_errors:
            return _captured("lookup plugin '%s' failed to load: %s"
                             % (name, self._lookup_errors[name]))
        source = self._lookup_sources.get(name)
        if source is None:
            return _captured("lookup plugin '%s' was not found" % name)
        try:
            ns = {"__name__": "ansiroll_lookup_%s" % name}
            exec(compile(source, "<lookup_plugins/%s.py>" % name, "exec"), ns)
            module_cls = ns["LookupModule"]
            inst = module_cls()

            def run(terms, variables, **kw):
                return inst.run(terms, variables=variables, **kw)
            self._lookup_loaded[name] = run
        except Exception as e:
            self._lookup_errors[name] = str(e)
            return _captured("lookup plugin '%s' failed to load: %s" % (name, e))
        return self._plugin_lookup(name, args, kwargs)

    def _play_var_scope(self, ctx, strict):
        if isinstance(ctx, _PlayVarScope):
            return ctx
        if any(isinstance(value, _PlayVarTemplate) for value in ctx.values()):
            return _PlayVarScope(ctx, self, strict)
        return ctx

    def render_string(self, text, ctx, _depth=0, strict=False):
        if not isinstance(text, str):
            return text
        if "{{" not in text and "{%" not in text:
            return text
        ctx = self._play_var_scope(ctx, strict)
        previous = self._current_ctx
        scope = ctx.get("vars")
        self._current_ctx = scope if isinstance(scope, _HandlerNameVars) else ctx
        try:
            return self._render_string(text, ctx, _depth, strict)
        finally:
            self._current_ctx = previous

    def _render_string(self, text, ctx, _depth, strict):
        scope = ctx.get("vars")
        play_scope = ctx if isinstance(ctx, _PlayVarScope) else None
        accesses = play_scope.accesses if play_scope is not None else 0
        env = self._strict_env if strict else self.env
        template_key = (text, strict)
        stripped = text.strip()
        # A bare "{{ expr }}" returns the native object (list/dict/int/...).
        lookup_expr = re.match(r"^\{\{\s*(?:lookup|query|q)\s*\(", stripped)
        if (stripped.startswith("{{") and stripped.endswith("}}")
                and (stripped.count("{{") == 1 or lookup_expr)
                and "{%" not in stripped):
            expr = stripped[2:-2].strip()
            expr_key = (expr, True) if strict else expr
            try:
                compiled = self._expr_cache.get(expr_key)
                if compiled is None:
                    compiled = env.compile_expression(expr, undefined_to_none=False)
                    self._expr_cache[expr_key] = compiled
                result = compiled(**ctx)
            except jinja2.TemplateSyntaxError as e:
                # Captured, not raised: surfaces when the value is used (or is
                # inspected via dump_object), like Ansible 2.19.
                return _captured_jinja_error(e, syntax=True)
            except Exception as e:
                if strict and isinstance(e, jinja2.UndefinedError):
                    return _captured_jinja_error(e)
                try:
                    tmpl = self._template_cache.get(template_key)
                    if tmpl is None:
                        tmpl = env.from_string(text)
                        self._template_cache[template_key] = tmpl
                    result = tmpl.render(**ctx)
                except jinja2.TemplateError as e:
                    return _captured_jinja_error(
                        e, syntax=isinstance(e, jinja2.TemplateSyntaxError)
                    )
        else:
            try:
                tmpl = self._template_cache.get(template_key)
                if tmpl is None:
                    tmpl = env.from_string(text)
                    self._template_cache[template_key] = tmpl
            except jinja2.TemplateSyntaxError as e:
                return _captured_jinja_error(e, syntax=True)
            try:
                result = tmpl.render(**ctx)
            except jinja2.TemplateError as e:
                return _captured_jinja_error(
                    e, syntax=isinstance(e, jinja2.TemplateSyntaxError)
                )
        try:
            result = _concretize(result)
        except jinja2.TemplateError as e:
            return _captured_jinja_error(
                e, syntax=isinstance(e, jinja2.TemplateSyntaxError)
            )
        if isinstance(result, _UnsafeTemplate):
            return str(result)
        if strict and isinstance(result, _UntrustedConditionValue):
            return result
        # Play and handler template sources resolve on access. Output from
        # those evaluations can contain delimiters from ordinary data.
        if (isinstance(result, str)
                and not isinstance(scope, _HandlerNameVars)
                and (play_scope is None or (
                    not play_scope.resolving and play_scope.accesses == accesses))
                and "{% raw" not in text and "{% endraw" not in text
                and ("{{" in result or "{%" in result)
                and not self._has_embedded_template_constant(text)):
            if result == text or _depth >= 10:
                return _captured("Recursive loop detected in template: %s" % text)
            return self.render_string(result, ctx, _depth + 1, strict=strict)
        return result

    def _has_embedded_template_constant(self, text):
        """Whether Jinja source contains a literal string that looks templated."""
        cached = self._embedded_constant_cache.get(text)
        if cached is not None:
            return cached
        try:
            parsed = self.env.parse(text)
        except jinja2.TemplateSyntaxError:
            result = False
        else:
            result = any(isinstance(node.value, str)
                         and ("{{" in node.value or "{%" in node.value)
                         for node in parsed.find_all(jinja2.nodes.Const))
        self._embedded_constant_cache[text] = result
        return result

    def evaluate(self, expr, ctx, strict=False):
        """Evaluate a single boolean-ish expression (used for when/changed_when)."""
        if isinstance(expr, bool):
            return expr
        expr = str(expr)
        ctx = self._play_var_scope(ctx, strict)
        expr_key = (expr, True) if strict else expr
        compiled = self._expr_cache.get(expr_key)
        if compiled is None:
            env = self._strict_env if strict else self.env
            compiled = env.compile_expression(expr, undefined_to_none=False)
            self._expr_cache[expr_key] = compiled
        previous = self._current_ctx
        self._current_ctx = ctx
        try:
            return compiled(**ctx)
        finally:
            self._current_ctx = previous


def _is_undef(v):
    return isinstance(v, jinja2.Undefined)


def _as_text(v):
    """Coerce a rendered value to text for writing to a file."""
    if isinstance(v, str):
        return v
    if _is_undef(v) or v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


_SEQUENCE_SHORTCUT_RE = re.compile(
    r"^((0?x?[0-9a-fA-F]+)-)?(0?x?[0-9a-fA-F]+)(/(0?x?[0-9a-fA-F]+))?(:(.+))?$"
)


def _sequence_int(value):
    text = str(value)
    if text.startswith(("0x", "0X")):
        return int(text, 16)
    if len(text) > 1 and text[0] == "0" and text[1].isdigit():
        return int(text, 8)
    return int(text, 10)


def _sequence_items(term):
    opts = {"start": 1, "stride": 1, "format": "%d"}
    match = _SEQUENCE_SHORTCUT_RE.match(term)
    if match:
        _all_start, start, end, _all_stride, stride, _all_format, fmt = match.groups()
        if start is not None:
            opts["start"] = _sequence_int(start)
        opts["end"] = _sequence_int(end)
        if stride is not None:
            opts["stride"] = _sequence_int(stride)
        if fmt is not None:
            opts["format"] = fmt
    else:
        seen = {}
        for part in shlex.split(term):
            key, sep, value = part.partition("=")
            if not sep or key not in ("start", "end", "stride", "count", "format"):
                raise ValueError("unrecognized arguments to with_sequence: %s" % term)
            seen[key] = value
        for key, value in seen.items():
            if key == "format":
                opts[key] = value
            else:
                opts[key] = _sequence_int(value)

    has_count = "count" in opts
    has_end = "end" in opts
    if not has_count and not has_end:
        raise ValueError("must specify count or end in with_sequence")
    if has_count and has_end:
        raise ValueError("can't specify both count and end in with_sequence")
    if has_count:
        count = opts["count"]
        if count == 0:
            return []
        opts["end"] = opts["start"] + count * opts["stride"] - 1

    start = opts["start"]
    end = opts["end"]
    stride = opts["stride"]
    fmt = opts["format"]
    if stride == 0:
        return []
    if stride > 0 and end < start:
        raise ValueError("to count backwards make stride negative")
    if stride < 0 and end > start:
        raise ValueError("to count forward don't make stride negative")
    if fmt.count("%") != 1:
        raise ValueError("bad formatting string: %s" % fmt)

    stop = end + (1 if stride > 0 else -1)
    return [fmt % i for i in range(start, stop, stride)]


def _accelerated_module_names(value=None):
    value = os.environ.get("ANSIROLL_ACCELERATED_MODULES", "") if value is None else value
    names = {part.strip().split(".")[-1] for part in str(value).split(",")
             if part.strip()}
    if "none" in names:
        return set()
    if "all" in names:
        from ansiroll.accelerated_modules import NAMES
        return set(NAMES)
    return names


class Runtime:
    def __init__(self, modules, ansiballz_code, host_vars, python=None,
                 check_mode=False, diff_mode=False, on_result=None, become_password=None, profile=False,
                 extra_vars=None, skip_run_once=False, pretty=False,
                 module_bundle=None, resource_reader=None,
                 host_var_overrides=None, native_only=False):
        self.modules = modules
        self.vars = dict(host_vars or {})
        self.python = python or sys.executable
        self.check_mode = check_mode
        self.diff_mode = diff_mode
        self.on_result = on_result
        self.become_password = become_password
        self.become_user = "root"
        self.profile = bool(profile)
        self.profile_totals = {}
        self.task_profile_totals = {}
        self.task_wall_totals = {}
        self.engine_profile = {
            "workers_started": 0,
            "worker_identities": {},
            "worker_auth": 0.0,
            "worker_spawn": 0.0,
            "worker_bundle": 0.0,
            "worker_startup": 0.0,
        }
        self.extra_vars = dict(extra_vars or {})
        # The controller passes its nonpersistent fact cache separately from
        # the flattened host snapshot. Those values (set_fact/register) outrank
        # play vars, a distinction that is otherwise lost after serialization.
        self._set_fact_overrides = dict(host_var_overrides or {})
        # include_vars results: above task vars, below set_fact/register.
        self._include_vars_layer = {}
        self._untrusted_runtime_vars = set()
        self.native_only = bool(native_only)
        self._task_provenance = {}
        self.skip_run_once = bool(skip_run_once)
        self.pretty = bool(pretty)
        self.module_bundle = module_bundle
        self.resource_reader = resource_reader
        self.accelerated_modules = _accelerated_module_names()
        self._pretty_last_uuid = None
        self._pretty_items_uuid = None
        self._workers = {}           # False or (True, user) -> worker state
        self._become_required_cache = {}
        self._module_env = None      # resolved `environment:` for the running task
        self._module_source_bytes = None  # baked unarchive input for the next worker call
        self._errors_cleared = False  # set by `meta: clear_host_errors`
        self.delegate_fn = None      # agent mode: run delegated/controller tasks via the channel
        self.controller_source_fn = None  # fetch runtime-created controller files in agent mode
        self.on_task_start = None
        self.on_warning = lambda message: sys.stderr.write("[WARNING]: %s\n" % message)
        self.templater = _Templater()
        self.handlers = []
        self._handler_blocks = []
        self.notified = []  # ordered, de-duplicated pending notifications
        self._notified_handlers = set()  # indexes into self.handlers
        self._flushing = False
        self._allow_broken_conditionals = None
        self._strict_var_resolution = False
        self.stats = {"ok": 0, "changed": 0, "failures": 0, "skipped": 0,
                      "unreachable": 0, "rescued": 0, "ignored": 0}
        self.host = self.vars.get("inventory_hostname") or _hostname()
        self.failed = False
        if self.become_password is None:
            # Fall back to ansible_become_pass from the vars (e.g. baked group_vars),
            # so direct runs don't need ANSIROLL_BECOME_PASS in the environment.
            bp = self.extra_vars.get("ansible_become_pass",
                                     self.vars.get("ansible_become_pass"))
            if bp is not None:
                try:
                    for _ in range(5):  # vars may point at other vars
                        if not (isinstance(bp, str) and "{{" in bp):
                            break
                        bp = self.template(bp, self.ctx(self.extra_vars))
                    self.become_password = str(bp)
                except Exception:
                    pass

    # ---- context / templating -------------------------------------------
    def ctx(self, *overlays):
        d = dict(self.vars)
        for o in overlays:
            if o:
                d.update(o)
        d.setdefault("omit", _OMIT)
        d.setdefault("hostvars", {self.host: d})
        d.setdefault("vars", d)
        return d

    def _store_register(self, task, result):
        name = task.get("register")
        if name:
            self.vars[name] = result
            self._untrusted_runtime_vars.add(name)
            # Registered vars live in the nonpersistent fact cache, like
            # set_fact: they outrank play/role/block/task vars.
            self._set_fact_overrides[name] = result

    def template(self, value, ctx, _seen=None, strict=False, _rendered=False):
        if isinstance(value, (dict, list)):
            if _seen is None:
                _seen = set()
            if id(value) in _seen:  # cycles (e.g. hostvars references the ctx)
                return value
            _seen.add(id(value))
        if isinstance(value, dict):
            if _rendered:
                return {k: self.template(v, ctx, _seen, strict, _rendered=True)
                        for k, v in value.items()}
            if set(value.keys()) == {"__ansiroll_unsafe__"}:
                return _UntrustedConditionValue(
                    value["__ansiroll_unsafe__"]
                )  # `!unsafe`: never templated
            if set(value.keys()) == {"__ansiroll_vault__"}:
                return value
            return {self.template(k, ctx, _seen, strict=strict) if isinstance(k, str) and "{{" in k else k:
                    self.template(v, ctx, _seen, strict=strict) for k, v in value.items()}
        if isinstance(value, list):
            return [self.template(v, ctx, _seen, strict, _rendered) for v in value]
        if isinstance(value, str) and not _rendered and ("{{" in value or "{%" in value):
            ctx = self.templater._play_var_scope(ctx, strict)
            accesses = ctx.accesses if isinstance(ctx, _PlayVarScope) else 0
            out = self.templater.render_string(value, ctx, strict=strict)
            if isinstance(out, (dict, list)) and out is not value:
                # Access can resolve trusted lazy members. Ordinary strings
                # inside an evaluated alias result are already data.
                rendered = isinstance(ctx, _PlayVarScope) and ctx.accesses != accesses
                out = self.template(out, ctx, _seen, strict, _rendered=rendered)
            return out
        return value

    def template_vars(self, d, ctx, dynamic=False):
        """Prepare variables with lazy containers and deferred play templates."""
        d = d or {}
        base = dict(ctx)
        base.update(d)
        out = {}
        for k, v in d.items():
            if isinstance(v, str):
                if dynamic and ("{{" in v or "{%" in v):
                    out[k] = _PlayVarTemplate(v)
                else:
                    out[k] = self.templater.render_string(v, base)
            elif isinstance(v, dict) and set(v.keys()) == {"__ansiroll_unsafe__"}:
                out[k] = _UntrustedConditionValue(v["__ansiroll_unsafe__"])
            else:
                out[k] = _lazy_container(v, self, base, dynamic)
        return out

    @contextmanager
    def _strict_vars(self):
        previous = self._strict_var_resolution
        self._strict_var_resolution = True
        try:
            yield
        finally:
            self._strict_var_resolution = previous

    def when(self, conds, ctx, strict=False, until=False):
        modern_until = until and _core_at_least(2, 19)
        for cond in (conds or []):
            if modern_until and self._empty_until(cond, ctx):
                continue
            if isinstance(cond, (_UnsafeTemplate, _UntrustedConditionValue)):
                raise UntrustedConditional(
                    "Encountered untrusted template or expression."
                )
            if (
                    isinstance(cond, dict)
                    and set(cond) == {"__ansiroll_untrusted_condition__"}):
                raise UntrustedConditional(
                    "Encountered untrusted template or expression."
                )
            trusted_template = (isinstance(cond, dict)
                                and set(cond) == {"__ansiroll_trusted_condition__"})
            if trusted_template:
                cond = cond["__ansiroll_trusted_condition__"]
            # Like Ansible, refuse conditionals with embedded templates: the
            # expression may come from untrusted data (template injection).
            if isinstance(cond, str) and ("{{" in cond or "{%" in cond):
                if not trusted_template:
                    raise UntrustedConditional(
                        "Conditional %r is an untrusted template or expression and "
                        "cannot be evaluated." % cond)
                try:
                    from jinja2 import meta as jinja_meta
                    referenced = jinja_meta.find_undeclared_variables(
                        self.templater.env.parse(cond)
                    )
                except Exception:
                    referenced = ()
                if any(
                        name in ctx
                        and (
                            name in self._untrusted_runtime_vars
                            or _contains_untrusted_condition_value(ctx[name])
                        )
                        for name in referenced):
                    raise UntrustedConditional(
                        "Encountered untrusted template or expression."
                    )
                result = self.templater.render_string(cond, ctx, strict=modern_until)
                if modern_until and self._empty_until(result, ctx):
                    continue
                # Assert accepts an indirected list of expressions (for
                # example `that: "{{ expression_list }}"`). Evaluate the
                # resolved members as conditions rather than treating the
                # non-empty list itself as truthy.
                if not until and isinstance(result, (list, tuple)):
                    if not self.when(list(result), ctx, strict=strict):
                        return False
                    continue
                if isinstance(result, (_UnsafeTemplate, _UntrustedConditionValue)):
                    raise UntrustedConditional(
                        "Encountered untrusted template or expression."
                    )
                if isinstance(result, str) and result.strip().lower() not in (
                        "true", "false"):
                    if re.match(r"^\s*\{\{.*\}\}\s*$", cond, re.S):
                        raise UntrustedConditional(
                            "Encountered untrusted template or expression."
                        )
                    result = self.templater.evaluate(result, ctx, strict=modern_until)
                if strict and _is_undef(result):
                    raise RuntimeError(
                        "The conditional check %r failed: the expression is undefined" % cond)
                if not self._condition_truth(result, ctx, until=until):
                    return False
                continue
            try:
                result = self.templater.evaluate(cond, ctx, strict=modern_until)
                if strict and _is_undef(result):
                    if modern_until:
                        result._fail_with_undefined_error()
                    raise RuntimeError(
                        "The conditional check %r failed: the expression is undefined" % cond)
                if not self._condition_truth(result, ctx, until=until):
                    return False
            except UntrustedConditional:
                raise
            except Exception:
                if strict:
                    raise
                return False
        return True

    def _broken_conditionals_allowed(self, ctx):
        if self._allow_broken_conditionals is not None:
            return self._allow_broken_conditionals
        config = ctx.get("__ansiroll_controller_config") or {}
        return _to_bool(config.get("ALLOW_BROKEN_CONDITIONALS", False))

    def _empty_until(self, value, ctx):
        if value is None or (isinstance(value, str) and not value.strip()):
            if self._broken_conditionals_allowed(ctx):
                return True
            raise RuntimeError("Empty conditional expressions are not allowed.")
        return False

    def _condition_truth(self, result, ctx, until=False):
        if isinstance(result, bool):
            return result
        if until and not _core_at_least(2, 19):
            return bool(result)
        if not until and isinstance(result, str) and result.strip().lower() in (
                "true", "false"):
            return _to_bool(result)
        if _core_at_least(2, 19 if until else 20) and not _is_undef(result):
            if self._broken_conditionals_allowed(ctx):
                return bool(result)
            raise RuntimeError(
                "Conditional result was derived from value of type '%s'. "
                "Conditionals must have a boolean result."
                % type(result).__name__
            )
        return _to_bool(result)

    # ---- result emission -------------------------------------------------
    def _node_name(self, node):
        # Ansible renders task names with host-level vars (never loop items) and
        # keeps the raw template text when a referenced var is undefined.
        name = node.get("name") or node.get("action")
        if not (isinstance(name, str) and "{{" in name):
            return name
        try:
            ctx = dict(node.get("role_defaults") or {})
            ctx.update(self.ctx())
            ctx.update(node.get("role_vars") or {})
            ctx.update(node.get("role_params") or {})
            env = jinja2.Environment(undefined=jinja2.StrictUndefined)
            env.context_class = _TemplateContext
            ctx = self.templater._play_var_scope(ctx, strict=True)
            return env.from_string(name).render(**ctx) or name
        except Exception:
            return name

    def emit_start(self, node):
        frame = {
            "_ansiroll": True,
            "runtime_id": node.get("runtime_id"),
            "uuid": node.get("uuid"),
            "task": self._node_name(node),
            "action": node.get("action"),
        }
        if self.on_task_start:
            self.on_task_start(frame)

    def _tally(self, bucket):
        self.stats[bucket] = self.stats.get(bucket, 0) + 1

    def emit(self, node, status, result, item=None, ignored=False, is_item=False):
        # Reporting only; recap tallies are applied explicitly by the caller so
        # rescued/ignored/loop semantics match Ansible.
        emitted_result = result
        if node.get("no_log"):
            emitted_result = {
                "censored": ("the output has been hidden due to the fact that "
                             "'no_log: true' was specified for this result")
            }
            if item is not None:
                emitted_result["item"] = item
            if isinstance(result, dict) and "_ansible_item_label" in result:
                emitted_result["_ansible_item_label"] = result["_ansible_item_label"]
        frame = {
            "_ansiroll": True,
            "host": self.host,
            "runtime_id": node.get("runtime_id"),
            "uuid": node.get("uuid"),
            "task": self._node_name(node),
            "action": node.get("action"),
            "status": status,
            "ignored": ignored,
            "is_item": is_item,
            "result": emitted_result,
            "provenance": self._task_provenance.get(
                id(node), {"mode": "native-agent"}
            ),
        }
        if item is not None:
            frame["item"] = item
        if self.on_result:
            self.on_result(frame)
        elif self.pretty:
            self._emit_pretty(frame)
        else:
            sys.stdout.write(RESULT_MARKER + " " + json.dumps(frame, default=str) + "\n")
            sys.stdout.flush()

    def _emit_pretty(self, frame):
        # Ansible-callback-style lines for humans running the compiled file
        # directly (controllers use on_result and never reach this).
        if frame.get("is_item"):
            self._pretty_items_uuid = frame.get("uuid")
        elif frame.get("uuid") == self._pretty_items_uuid:
            # Loop aggregate after per-item lines: Ansible does not print it.
            return
        if frame.get("uuid") != self._pretty_last_uuid:
            self._pretty_last_uuid = frame.get("uuid")
            banner = "TASK [%s] " % (frame.get("task") or frame.get("action") or "")
            sys.stdout.write("\n" + banner + "*" * max(3, 79 - len(banner)) + "\n")
        host = frame.get("host") or self.host
        status = frame.get("status")
        res = frame.get("result") if isinstance(frame.get("result"), dict) else {}
        suffix = ""
        if frame.get("is_item"):
            label = res.get("_ansible_item_label", frame.get("item"))
            if not isinstance(label, str):
                label = json.dumps(label, default=str)
            suffix = " => (item=%s)" % label
        if status in ("failed", "unreachable"):
            line = "fatal: [%s]:%s %s! => %s" % (
                host, suffix, status.upper() if status == "unreachable" else "FAILED",
                json.dumps(res, default=str))
            if frame.get("ignored"):
                line += "\n...ignoring"
        elif status == "skipped":
            line = "skipping: [%s]%s" % (host, suffix)
            if res.get("msg") in ("No items in the list", "All items skipped"):
                line += " => %s" % json.dumps({"msg": res["msg"]})
        else:
            verb = "changed" if status == "changed" or res.get("changed") else "ok"
            line = "%s: [%s]%s" % (verb, host, suffix)
            if frame.get("action") == "debug":
                shown = {k: v for k, v in res.items()
                         if not k.startswith("_") and k not in ("changed", "failed")}
                line += " => %s" % json.dumps(shown, default=str, indent=4)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    # ---- execution -------------------------------------------------------
    def run_play(self, play, defer_handlers=False, initial_notified=None):
        invocation_check_mode = self.check_mode
        play_check_mode = play.get("check_mode")
        if play_check_mode is not None:
            if isinstance(play_check_mode, str):
                play_check_mode = self.template(play_check_mode, self.ctx())
            self.check_mode = _to_bool(play_check_mode)
        if play.get("jinja_plugins"):
            plugins = []
            for plugin in play["jinja_plugins"]:
                plugin = dict(plugin)
                plugin["source"] = self._asset_text(plugin.get("source"))
                plugins.append(plugin)
            self.templater.register_plugins(plugins)
        self.handlers = []
        self._handler_blocks = []
        self._add_handlers(play.get("handlers", []), {}, False)
        self._error_on_missing_handler = play.get("error_on_missing_handler", True)
        self._allow_broken_conditionals = play.get("allow_broken_conditionals")
        self._notified_handlers = set()
        self._flushing = False
        self._play_become = play.get("become", False)
        if initial_notified:
            # Carry notifications from earlier segments (see the strategy).
            for name in initial_notified:
                if name not in self.notified:
                    self.notified.append(name)
        self.vars["__ansiroll_controller_env"] = play.get("controller_env") or {}
        self.vars["__ansiroll_controller_config"] = (
            play.get("controller_config") or {}
        )
        if play.get("gather_facts"):
            setup_ctx = self._layered_ctx(self.ctx(play.get("vars")))
            gathered = self.gather_facts(
                play.get("environment"),
                become=bool(play.get("become")),
                become_user=play.get("become_user"),
                fact_schema=play.get("fact_schema"),
                ignore_errors=play.get("ignore_errors"),
                ctx=setup_ctx,
            )
            if not gathered:
                self.failed = True
                self.check_mode = invocation_check_mode
                return
        # Play templates resolve when consumed, after task precedence is applied.
        self.vars.update(self.template_vars(
            play.get("vars"), self.ctx(), dynamic=True))
        # Host variables created by include_vars/set_fact/register have higher
        # precedence than play vars and keep it for later plays in the run.
        self.vars.update(self._include_vars_layer)
        self.vars.update(self._set_fact_overrides)
        # Extra-vars have the highest Ansible precedence; play vars must not
        # overwrite values injected via -e.
        self.vars.update(self.extra_vars)
        try:
            # Play.compile() provides the implicit flush points and wraps them
            # in always sections when force_handlers is supported there.
            self.run_nodes(play.get("tasks", []), {}, self._play_become)
        except TaskFailed as ex:
            if not getattr(ex, "counted", False):
                self._tally("failures")
            self.failed = True
        # Through 2.13, StrategyBase.run() makes one final pass, with failed
        # hosts eligible only under force_handlers.
        if not _core_at_least(2, 14) and (not self.failed or play.get("force_handlers")):
            try:
                self.flush_handlers()
            except TaskFailed as ex:
                if not getattr(ex, "counted", False):
                    self._tally("failures")
                self.failed = True
        self.check_mode = invocation_check_mode

    def run_nodes(self, nodes, overlay, become=False):
        for node in nodes:
            self.run_node(node, overlay, become)

    def run_node(self, node, overlay, become=False):
        if node.get("type") == "block":
            self.run_block(node, overlay, become)
        elif node.get("type") == "include":
            self.run_include(node, overlay, become)
        else:
            self.run_task(node, overlay, become)

    def run_include(self, node, overlay, become=False):
        """Run a compile-time-expanded include once per invocation or loop item."""
        base_ctx = self._task_ctx(node, overlay)
        self.emit_start(node)
        b = bool(node.get("become")) or become
        try:
            run_it = self.when(node.get("when"), base_ctx)
        except UntrustedConditional as ex:
            self.emit(node, "failed", {"failed": True, "changed": False, "msg": str(ex)})
            self._tally("failures")
            result = {"failed": True, "changed": False, "msg": str(ex)}
            exc = TaskFailed(node.get("name"), node, result)
            exc.counted = True
            raise exc
        if not run_it:
            self.emit(node, "skipped", {"changed": False, "skipped": True,
                                        "skip_reason": "Conditional result was False"})
            self._tally("skipped")
            return
        loop = node.get("loop")
        tally = node.get("action") != "include_role" or _core_at_least(2, 17)
        items = self._resolve_loop(loop, base_ctx) if loop is not None else [None]
        invocation_params = node.get("include_params", node.get("vars")) or {}
        for i, item in enumerate(items):
            result = {"changed": False, "include": node.get("file")}
            loop_vars = {}
            if loop is not None:
                loop_vars = self._loop_item_vars(node, items, i, base_ctx)
                result["item"] = item
            ctx = dict(base_ctx)
            ctx.update(loop_vars)
            params = self.template_vars(invocation_params, ctx)
            params.update(loop_vars)
            scope = _with_high_vars(overlay, params)
            # Handlers execute later, so retain the expressions as well as
            # the loop values from this invocation.
            include_params = dict(overlay.get(_INCLUDE_PARAMS_KEY) or {})
            include_params.update(invocation_params)
            include_params.update(loop_vars)
            scope[_INCLUDE_PARAMS_KEY] = include_params
            self.emit(node, "ok", result, item=item, is_item=loop is not None)
            if tally:
                self._tally("ok")
            self._add_handlers(node.get("handlers", []), scope, b)
            self.run_nodes(node.get("tasks", []), scope, b)

    def run_block(self, block, overlay, become=False):
        scope = dict(overlay)
        scope.update(self.template_vars(
            block.get("vars"), self._layered_ctx(self.ctx(overlay), overlay)))
        if not self.when(block.get("when"),
                         self._layered_ctx(self.ctx(scope), scope)):
            b_become = bool(block.get("become")) or become
            self._skip_nodes(block.get("block", []), scope, b_become)
            if block.get("always"):
                self._skip_nodes(block["always"], scope, b_become)
            return
        b_become = bool(block.get("become")) or become
        failed_exc = None
        try:
            try:
                self.run_nodes(block.get("block", []), scope, b_become)
            except TaskFailed as ex:
                if getattr(ex, "ends_play", False):
                    raise
                rescue = block.get("rescue") or []
                if not rescue:
                    raise
                self._tally("rescued")
                # Saved as nonpersistent facts (set_fact level) that outlive
                # the rescue (StrategyBase._process_pending_results).
                failed_facts = {
                    "ansible_failed_task": ex.task,
                    "ansible_failed_result": ex.result,
                }
                self.vars.update(failed_facts)
                self._set_fact_overrides.update(failed_facts)
                self.run_nodes(rescue, scope, b_become)
        except TaskFailed as ex:
            if getattr(ex, "ends_play", False):
                raise
            failed_exc = ex
        if block.get("always"):
            try:
                self.run_nodes(block["always"], scope, b_become)
            except TaskFailed:
                # If always replaces a pending block/rescue failure, preserve
                # the original count before propagating the new failure.
                if (
                        failed_exc is not None
                        and not getattr(failed_exc, "counted", False)):
                    self._tally("failures")
                    failed_exc.counted = True
                raise
        # `meta: clear_host_errors` (e.g. in the always section) forgives the
        # pending failure; the host still counts as failed once in the recap
        # (it was marked failed before being cleared, like Ansible).
        if failed_exc is not None:
            if self._errors_cleared:
                self._errors_cleared = False
                if not getattr(failed_exc, "counted", False):
                    self._tally("failures")
                    failed_exc.counted = True
            else:
                raise failed_exc

    def _skip_nodes(self, nodes, overlay, become=False):
        for node in nodes:
            if node.get("type") == "block":
                scope = dict(overlay)
                scope.update(self.template_vars(node.get("vars"), self.ctx(overlay)))
                b_become = bool(node.get("become")) or become
                self._skip_nodes(node.get("block", []), scope, b_become)
                if node.get("always"):
                    self._skip_nodes(node["always"], scope, b_become)
            elif node.get("type") == "include":
                # A skipped dynamic include reports one skipped task; its inner
                # tasks were never loaded in Ansible, so they don't appear.
                self._skip_task(node, overlay)
            else:
                self._skip_task(node, overlay)

    def _skip_task(self, task, overlay):
        self.emit_start(task)
        ctx = self._task_ctx(task, overlay)
        result = {"skipped": True, "changed": False, "skip_reason": "Conditional result was False"}
        loop = task.get("loop", None)
        if loop is not None:
            items = self._resolve_loop(loop, ctx)
            agg = {"results": [], "changed": False, "skipped": True, "msg": "All items skipped"}
            for it in items:
                item_ctx = dict(ctx)
                item_ctx[task.get("loop_var") or "item"] = it
                item_res = dict(result)
                item_res["item"] = it
                label = self._loop_label(task, item_ctx)
                if label is not None:
                    item_res["_ansible_item_label"] = label
                agg["results"].append(item_res)
                self.emit(task, "skipped", item_res, item=it, is_item=True)
            if task.get("register"):
                self._store_register(task, agg)
            self.emit(task, "skipped", agg)
        else:
            if task.get("register"):
                self._store_register(task, result)
            self.emit(task, "skipped", result)
        self._tally("skipped")

    def _layered_ctx(self, ctx, overlay=None, role_params=None):
        """Re-apply the layers that outrank play/role/block/task vars.

        VariableManager.get_vars order (low -> high): role defaults < host
        vars/facts < play vars < role vars < block/task vars < include_vars
        < set_fact/register < role params < include params < extra vars.
        """
        ctx.pop(_HIGH_VARS_KEY, None)
        ctx.pop(_INCLUDE_PARAMS_KEY, None)
        ctx.update(self._include_vars_layer)
        ctx.update(self._set_fact_overrides)
        ctx.update(role_params or {})
        if overlay:
            ctx.update(overlay.get(_HIGH_VARS_KEY) or {})
        ctx.update(self.extra_vars)
        return ctx

    def _task_ctx(self, task, overlay):
        # Precedence (low -> high): role defaults < host vars/facts/play vars
        # < role vars < block vars < task vars < include_vars < set_fact/
        # register < role params < include params < extra vars < loop item.
        ctx = {}
        ctx.update(task.get("role_defaults") or {})
        ctx.update(self.vars)
        ctx.update(task.get("role_vars") or {})
        ctx.update(overlay)
        role_params = task.get("role_params")
        self._layered_ctx(ctx, overlay, role_params)
        ctx.update(self.template_vars(task.get("vars"), ctx))
        self._layered_ctx(ctx, overlay, role_params)
        if task.get("role_magic"):
            ctx.update(task["role_magic"])
        ctx.setdefault("omit", _OMIT)
        if task.get("search_dirs"):
            ctx["__ansiroll_search_dirs"] = task["search_dirs"]
        if task.get("search_path"):
            ctx.setdefault("ansible_search_path", task["search_path"])
        hostvars = ctx.get("hostvars")
        if isinstance(hostvars, dict):
            # The controller snapshot predates include_vars/set_fact/register
            # changes made by this long-lived agent. Refresh this host's entry
            # for every task while preserving other hosts from the snapshot.
            hostvars = dict(hostvars)
            current = dict(hostvars.get(self.host) or {})
            current.update({
                key: value for key, value in ctx.items()
                if key not in ("hostvars", "vars")
            })
            hostvars[self.host] = current
            ctx["hostvars"] = hostvars
        else:
            ctx["hostvars"] = {self.host: ctx}
        ctx.setdefault("vars", ctx)
        return ctx

    def run_task(self, task, overlay, become=False):
        if not self.profile:
            return self._run_task(task, overlay, become)
        started = time.monotonic()
        try:
            return self._run_task(task, overlay, become)
        finally:
            elapsed = time.monotonic() - started
            key = (task.get("runtime_id") or task.get("uuid") or "",
                   task.get("name") or "", task.get("action") or "")
            total = self.task_wall_totals.setdefault(key, {
                "name": task.get("name"),
                "action": task.get("action"),
                "count": 0,
                "total": 0.0,
                "max": 0.0,
            })
            total["count"] += 1
            total["total"] += elapsed
            total["max"] = max(total["max"], elapsed)

    def _run_task(self, task, overlay, become=False):
        if task.get("special") == "meta_flush_handlers":
            self.emit_start(task)
            self.flush_handlers()
            return
        if task.get("special") == "meta_clear_host_errors":
            self._errors_cleared = True
            return
        effective_become = bool(task.get("become")) or become
        base_ctx = self._task_ctx(task, overlay)
        run_once = task.get("run_once")
        if self.skip_run_once and run_once is not None:
            try:
                if isinstance(run_once, str):
                    run_once = self.template(run_once, base_ctx)
                if _to_bool(run_once):
                    # Ansible emits no skipped event for hosts which do not
                    # execute a run_once task.
                    return
            except Exception:
                # Normal task execution will report invalid templating.
                pass
        self.emit_start(task)

        ignore = task.get("ignore_errors", False)
        if isinstance(ignore, str):
            ignore = _to_bool(self.template(ignore, base_ctx))
        else:
            ignore = _to_bool(ignore)
        loop = task.get("loop", None)
        notifications = []
        if loop is not None:
            # A literal-false task conditional short-circuits loop templating.
            # An undefined loop expression must not fabricate an undefined item
            # or emit both an item-level and aggregate skipped result.
            conditions = task.get("when") or []
            literal_false = any(
                cond is False or (
                    isinstance(cond, str)
                    and cond.strip().lower() in ("false", "no", "off", "0")
                )
                for cond in conditions
            )
            if literal_false:
                res = {"changed": False, "skipped": True,
                       "skip_reason": "Conditional result was False"}
                if task.get("register"):
                    self._store_register(task, res)
                self.emit(task, "skipped", res)
                self._tally("skipped")
                return
            try:
                res = self._run_loop(
                    task, base_ctx, effective_become, ignore, notifications)
            except TaskFailed:
                raise
            except Exception as ex:
                # A loop that fails to resolve fails the task -- unless a false
                # `when` short-circuits it to skipped (ansible/ansible#45189).
                try:
                    skipped = not self.when(task.get("when"), base_ctx)
                except Exception:
                    skipped = False
                if skipped:
                    res = {"changed": False, "skipped": True,
                           "skip_reason": "Conditional result was False"}
                else:
                    res = {"failed": True, "changed": False, "msg": str(ex)}
                if task.get("register"):
                    self._store_register(task, res)
        else:
            try:
                run_it = self.when(task.get("when"), base_ctx)
            except UntrustedConditional as ex:
                run_it = None
                res = {"failed": True, "changed": False, "msg": str(ex)}
            if run_it is False:
                skip_res = {"changed": False, "skipped": True, "skip_reason": "Conditional result was False"}
                if task.get("register"):
                    self._store_register(task, skip_res)
                self.emit(task, "skipped", skip_res)
                self._tally("skipped")
                return
            if run_it:
                res = self._exec_notifying_task(task, base_ctx, effective_become, notifications)
            if task.get("register"):
                res.setdefault("failed", False)
                res.setdefault("changed", False)
                self._store_register(task, res)
            self._absorb_facts(task, res)

        tally_changed = res.pop("_ansiroll_tally_changed", res.get("changed", False))
        if res.get("changed") and not _result_failed(res):
            self._queue_notify(notifications)
        status = _status_of(res)
        self.emit(task, status, res, ignored=ignore)

        if _result_failed(res):
            if ignore:
                self._tally("changed" if tally_changed else "ok")
                self._tally("ignored")
            else:
                raise TaskFailed(task.get("name"), task, dict(res))
        elif res.get("skipped"):
            self._tally("skipped")
        else:
            self._tally("changed" if res.get("changed") else "ok")

    def _loop_item_vars(self, task, items, i, ctx=None):
        """Per-item loop variables: loop_var, index_var and ansible_loop."""
        loop_var = task.get("loop_var") or "item"
        if "{{" in loop_var and ctx is not None:
            loop_var = _as_text(self.template(loop_var, ctx))
        out = {loop_var: items[i], "ansible_loop_var": loop_var}
        if task.get("index_var"):
            out[task["index_var"]] = i
            out["ansible_index_var"] = task["index_var"]
        if task.get("loop_extended"):
            n = len(items)
            loop_info = {
                "index": i + 1, "index0": i,
                "first": i == 0, "last": i == n - 1,
                "length": n,
                "revindex": n - i, "revindex0": n - i - 1,
            }
            if i > 0:
                loop_info["previtem"] = items[i - 1]
            if i < n - 1:
                loop_info["nextitem"] = items[i + 1]
            if task.get("loop_extended_allitems", True):
                loop_info["allitems"] = items
            out["ansible_loop"] = loop_info
        return out

    def _run_loop(self, task, base_ctx, become=False, ignore_errors=False,
                  notifications=None):
        items = self._resolve_loop(task.get("loop"), base_ctx)
        agg = {"results": [], "changed": False, "failed": False, "msg": "All items completed"}
        any_ran = False
        for i, it in enumerate(items):
            ctx = dict(base_ctx)
            ctx.update(self._loop_item_vars(task, items, i, base_ctx))
            if i > 0 and task.get("loop_pause"):
                time.sleep(float(self.template(task["loop_pause"], ctx) or 0))
            if not self.when(task.get("when"), ctx):
                res = {"skipped": True, "changed": False,
                       "skip_reason": "Conditional result was False"}
            else:
                any_ran = True
                res = self._exec_notifying_task(task, ctx, become, notifications)
                self._absorb_facts(task, res)
                if isinstance(res.get("ansible_facts"), dict):
                    base_ctx.update(res["ansible_facts"])
            res["item"] = it
            if task.get("loop_label") is not None:
                label_ctx = dict(ctx)
                if task.get("register"):
                    label_ctx[task["register"]] = res
                try:
                    label = self.template(task["loop_label"], label_ctx)
                    if _is_undef(label):
                        label._fail_with_undefined_error()
                    res["_ansible_item_label"] = label
                except Exception as ex:
                    res = {"failed": True, "changed": False, "item": it,
                           "msg": "Failed to template loop_control.label: %s" % ex}
            tally_changed = res.pop(
                "_ansiroll_tally_changed", res.get("changed", False)
            )
            agg["results"].append(res)
            agg["changed"] = agg["changed"] or res.get("changed", False)
            if tally_changed:
                agg["_ansiroll_tally_changed"] = True
            if _result_failed(res):
                agg["failed"] = True
            self.emit(task, _status_of(res), res, item=it,
                      ignored=ignore_errors, is_item=True)
            bw = task.get("loop_break_when")
            if bw:
                bctx = dict(ctx)
                if task.get("register"):
                    bctx[task["register"]] = res
                if self.when(bw, bctx):
                    break
        if not items:
            # Ansible reports a loop that resolves to no items as skipped.
            agg["skipped"] = True
            agg["msg"] = "No items in the list"
        elif not any_ran:
            agg["skipped"] = True
            agg["msg"] = "All items skipped"
        if task.get("register"):
            self._store_register(task, agg)
        return agg

    def _loop_label(self, task, ctx):
        label = task.get("loop_label")
        if label is None:
            return None
        try:
            return self.template(label, ctx)
        except Exception:
            return None

    def _resolve_loop(self, loop, ctx):
        if isinstance(loop, dict) and "__ansiroll_lookup" in loop:
            name = loop["__ansiroll_lookup"]
            if name == "sequence":
                return self._resolve_sequence_loop(loop.get("terms"), ctx)
            terms = self.template(loop.get("terms"), ctx)
            if not isinstance(terms, list):
                terms = [terms]
            try:
                return _loop_lookup(name, terms)
            except _UnsupportedLookup:
                vals = self.templater._plugin_lookup(name, terms, {})
                if _is_undef(vals):
                    vals._fail_with_undefined_error()
                return vals
        items = self.template(loop, ctx)
        if items is None:
            return []
        if not isinstance(items, list):
            items = [items]
        return items

    def _resolve_sequence_loop(self, terms, ctx):
        if terms is None:
            terms = [""]
        elif isinstance(terms, str):
            terms = [terms]
        elif not isinstance(terms, list):
            terms = [terms]

        out = []
        for term in terms:
            rendered = self.template(term, ctx) if isinstance(term, str) else term
            out.extend(_sequence_items(str(rendered)))
        return out

    def _attempt_count(self, task, ctx):
        """Total runs for a task (TaskExecutor._execute's attempt loop)."""
        until = task.get("until")
        raw = task.get("retries")
        retries = None if raw is None else int(self.template(raw, ctx) or 0)
        if _core_at_least(2, 16):
            # 2.16+: `retries` alone retries until the task succeeds; unset
            # retries default to 3 only when `until` is set.
            if retries is not None:
                return 1 + max(0, retries)
            return 4 if until else 1
        # Older cores only retry with `until`; the Task field defaults to 3.
        if not until:
            return 1
        if retries is None:
            return 3
        return 1 if retries <= 0 else retries + 1

    def _exec_notifying_task(self, task, ctx, become, notifications):
        # Like TaskExecutor.post_validate, resolve notify after when and
        # before dispatch, including on unchanged tasks and per loop item.
        try:
            names = self._notification_names(task, ctx)
        except Exception as ex:
            return {"failed": True, "changed": False,
                    "msg": "Failed to template notify: %s" % ex}
        res = self._exec_with_retries(task, ctx, become)
        if notifications is not None and not _result_failed(res):
            notifications.extend(names)
        return res

    def _exec_with_retries(self, task, ctx, become=False):
        until = task.get("until")
        attempts = self._attempt_count(task, ctx)
        if attempts <= 1 and not (until and not _core_at_least(2, 16)):
            return self._exec_one(task, ctx, become)

        delay = float(self.template(task.get("delay"), ctx) or 0)
        res = None
        eval_ctx = dict(ctx)
        for attempt in range(1, attempts + 1):
            res = self._exec_one(task, eval_ctx, become, attempt=attempt)
            if task.get("register"):
                eval_ctx[task["register"]] = res
            if attempts <= 1:
                return res
            # 2.16+: without `until` the default condition is `not failed`.
            if until:
                try:
                    if _core_at_least(2, 19):
                        with self._strict_vars():
                            done = self.when(until, eval_ctx, strict=True, until=True)
                    else:
                        done = self.when(until, eval_ctx, strict=True, until=True)
                except Exception as ex:
                    return {"failed": True, "changed": False,
                            "msg": "An 'until' expression failed. %s" % ex}
            else:
                done = not res.get("failed")
            if done:
                return res
            if attempt < attempts and delay > 0:
                time.sleep(delay)
        # Out of attempts: TaskExecutor reports attempts = retries - 1 and
        # marks the result failed. From 2.21 an evaluated failed_when result
        # still decides `failed` (UnifiedTaskResult.failed).
        res["attempts"] = attempts - 1
        fw_result = res.get("failed_when_result")
        modern_result = _core_at_least(2, 21)
        action_failed = res.get("failed")
        if (modern_result and isinstance(fw_result, bool)
                and task.get("failed_when") is not None):
            res["failed"] = fw_result
        else:
            res["failed"] = True
        if _core_at_least(2, 19) and (not modern_result or action_failed):
            # Core projects the captured task error as a traceback string.
            # From 2.21 exhaustion alone no longer creates a captured error.
            config = ctx.get("__ansiroll_controller_config") or {}
            modes = config.get("DISPLAY_TRACEBACK") or ()
            if "always" in modes or ("error" in modes and "never" not in modes):
                res.setdefault("exception", "(traceback unavailable)")
            else:
                res["exception"] = "(traceback unavailable)"
        return res

    def _exec_one(self, task, ctx, become=False, attempt=None):
        task_started = time.monotonic()
        inherited_check_mode = self.check_mode
        inherited_become_user = self.become_user
        provenance = {"mode": "native-agent"}
        try:
            check_override = task.get("check_mode")
            if check_override is not None:
                if isinstance(check_override, str):
                    check_override = self.template(check_override, ctx)
                self.check_mode = _to_bool(check_override)
            become_user = task.get("become_user")
            if become_user:
                if isinstance(become_user, str):
                    become_user = self.template(become_user, ctx)
                self.become_user = _as_text(become_user)
            self._module_env = self._task_environment(task, ctx)
            special = task.get("special")
            if special and (special == "debug"
                            or not (self.delegate_fn and task.get("delegate_to"))):
                res = self._run_special(special, task, ctx, become)
            elif self.delegate_fn and (task.get("delegate_to") or task.get("fallback")):
                # Run on the controller (or delegated host): template args here where
                # the vars live, then hand the concrete call to the controller.
                reason = (
                    "delegate_to %s" % task.get("delegate_to")
                    if task.get("delegate_to")
                    else task.get("fallback_reason") or "controller fallback"
                )
                provenance = {
                    "mode": "controller-fallback",
                    "reason": reason,
                }
                if self.native_only:
                    res = {
                        "failed": True,
                        "changed": False,
                        "msg": (
                            "ansiroll: native-only mode rejected task %r: %s"
                            % (task.get("name") or task.get("action"), reason)
                        ),
                    }
                else:
                    args = _fail_on_undef(_strip_omit(
                        self.template(task.get("args") or {}, ctx)
                    ))
                    res = self.delegate_fn(task, args, become, ctx)
            else:
                provenance = {"mode": "real-ansiballz"}
                args = _fail_on_undef(_strip_omit(self.template(task.get("args") or {}, ctx)))
                self._module_source_bytes = self._baked_module_source(task, args)
                if task.get("async_val") and int(task.get("poll") or 0) == 0:
                    res = self._run_async_module(task["action"], args, become=become)
                elif task.get("accelerator") and (
                        self.accelerated_modules
                        or task.get("accelerator") == "required"):
                    res = self._run_accelerated_action(task, args, become)
                else:
                    res = None
                if res is not None:
                    provenance = {
                        "mode": "native-agent",
                        "reason": "accelerated action",
                    }
                if res is None:
                    res = self.run_module(task["action"], args, become=become)
        except Exception as ex:
            res = {"failed": True, "changed": False,
                   "msg": "ansiroll: task raised %s: %s" % (type(ex).__name__, ex)}
        finally:
            self._task_provenance[id(task)] = provenance
            self._module_source_bytes = None
            self._module_env = None
            self.check_mode = inherited_check_mode
            self.become_user = inherited_become_user
        if attempt is not None:
            # Available early so changed_when/failed_when can use it.
            res["attempts"] = attempt
        self._apply_overrides(task, res, ctx)
        if self.profile:
            module_profile = res.get("_ansiroll_profile") or {}
            elapsed = time.monotonic() - task_started
            module_total = float(module_profile.get("worker_total") or 0.0)
            key = (task.get("runtime_id") or task.get("uuid") or "",
                   task.get("name") or "", task.get("action") or "")
            total = self.task_profile_totals.setdefault(key, {
                "name": task.get("name"),
                "action": task.get("action"),
                "count": 0,
                "total": 0.0,
                "module": 0.0,
                "engine": 0.0,
                "max": 0.0,
                "worker_identities": {},
            })
            total["count"] += 1
            total["total"] += elapsed
            total["module"] += module_total
            total["engine"] += max(0.0, elapsed - module_total)
            total["max"] = max(total["max"], elapsed)
            identity = module_profile.get("worker_identity")
            if identity:
                identities = total["worker_identities"]
                identities[identity] = identities.get(identity, 0) + 1
        return res

    def _baked_module_source(self, task, args):
        """Return baked controller-side input for worker-local memory transport."""
        name = str(task.get("action") or "").split(".")[-1]
        if name != "unarchive" or _to_bool(args.get("remote_src", False)):
            return None
        src = args.get("src")
        marker = (task.get("_baked") or {}).get(src)
        if marker is None:
            raise RuntimeError("unarchive: src %r was not baked into the compiled file" % src)
        return self._asset_bytes(marker)

    def _task_environment(self, task, ctx):
        """Resolve the `environment:` keyword to a flat {name: value} dict."""
        entries = task.get("environment")
        if not entries:
            return None
        env = {}
        for entry in entries:
            if isinstance(entry, str):
                entry = self.template(entry, ctx)
            if entry == _OMIT or _is_undef(entry) or entry is None:
                continue
            if not isinstance(entry, dict):
                raise RuntimeError(
                    "environment entries must resolve to dictionaries, got %r" % (entry,))
            for k, v in entry.items():
                try:
                    v = self.template(v, ctx)
                except Exception:
                    # e.g. a play env referencing ansible_env during fact
                    # gathering: Ansible skips that entry.
                    continue
                # Test Undefined first: comparing a captured StrictUndefined to
                # the omit sentinel raises before we can skip the entry.
                if _is_undef(v) or v == _OMIT:
                    continue
                env[str(k)] = _as_text(v)
        return env or None

    def _apply_overrides(self, task, res, ctx):
        cw = task.get("changed_when")
        fw = task.get("failed_when")
        module_failed = res.get("failed", False)
        module_exception = res.get("exception") or res.get("msg")
        changed_when_failed = False
        try:
            if cw is not None:
                ectx = _override_ctx(task, res, ctx)
                try:
                    res["changed"] = self.when(cw if isinstance(cw, list) else [cw], ectx, strict=True)
                except UntrustedConditional:
                    raise
                except Exception as ex:
                    changed_when_failed = True
                    original_changed = bool(res.get("changed"))
                    res["failed"] = True
                    res["changed_when_result"] = str(ex)
                    # Current 2.20+, linear retains a successful module's changed
                    # tally when changed_when itself errors, while displaying
                    # the final failed result as changed=false.
                    if _core_at_least(2, 20):
                        res["changed"] = False
                        if original_changed:
                            res["_ansiroll_tally_changed"] = True
                    if module_failed:
                        res["changed_when_suppressed_exception"] = (
                            module_exception or "module failed before changed_when"
                        )
                        res["msg"] = (
                            "Task failed. Module failed. A 'changed_when' expression "
                            "failed. Error rendering expression. %s" % ex
                        )
                    res["exception"] = str(ex)
            # TaskExecutor evaluates failed_when only when changed_when
            # succeeded.
            if fw is not None and not changed_when_failed:
                ectx = _override_ctx(task, res, ctx)
                try:
                    fw_result = self.when(fw if isinstance(fw, list) else [fw], ectx, strict=True)
                except UntrustedConditional:
                    raise
                except Exception as ex:
                    # An error while evaluating failed_when fails the task and
                    # is reported through failed_when_result (like Ansible).
                    res["failed"] = True
                    res["failed_when_result"] = str(ex)
                    if module_failed:
                        res["failed_when_suppressed_exception"] = (
                            module_exception or "module failed before failed_when"
                        )
                    res["exception"] = str(ex)
                else:
                    res["failed"] = fw_result
                    res["failed_when_result"] = fw_result
                    if module_failed and (not fw_result or _core_at_least(2, 21)):
                        res["failed_when_suppressed_exception"] = (
                            module_exception or "suppressed by failed_when"
                        )
                        if not fw_result:
                            res.pop("exception", None)
        except UntrustedConditional as ex:
            res["failed"] = True
            res["msg"] = str(ex)
        if res.get("failed") and "exception" not in res:
            res["exception"] = str(res.get("msg") or "Task failed")

    def _run_special(self, special, task, ctx, become=False):
        name = special.split(".")[-1]
        args = self.template(task.get("args") or {}, ctx)
        # Ansible accepts the complete module argument dictionary through a
        # single template, representing it internally as `_raw_params`.
        if (
                isinstance(args, dict)
                and set(args) == {"_raw_params"}
                and isinstance(args["_raw_params"], dict)):
            args = dict(args["_raw_params"])
        if name == "debug":
            for value in args.values():
                _fail_on_direct_undef(value)
        else:
            args = _fail_on_undef(args)
        if name == "debug":
            if "var" in args:
                key = args["var"]
                # `var:` takes an expression (often dotted), evaluated against vars.
                try:
                    val = self.templater.evaluate(key, ctx)
                except Exception:
                    val = ctx.get(key, self.vars.get(key))
                if _is_undef(val):
                    val = "VARIABLE IS NOT DEFINED!"
                return {"changed": False, key: val, "_ansible_verbose_always": True}
            return {
                "changed": False,
                "msg": args.get("msg", "Hello world!"),
                "_ansible_verbose_always": True,
            }
        if name == "set_fact":
            import keyword
            facts = {
                _trust_set_fact_value(k): _trust_set_fact_value(v)
                for k, v in args.items() if k not in ("cacheable",)
            }
            cacheable = _to_bool(args.get("cacheable", False))
            reserved = ("true", "false", "yes", "no", "on", "off", "none", "null")
            for k in facts:
                if not isinstance(k, str):
                    return {"failed": True, "changed": False,
                            "msg": "Invalid variable name '%s' of type '%s'"
                                   % (k, type(k).__name__)}
                if not k.isidentifier() or keyword.iskeyword(k) or k.lower() in reserved:
                    return {"failed": True, "changed": False,
                            "msg": "Invalid variable name '%s'" % k}
            self.vars.update(facts)
            return {
                "changed": False,
                "ansible_facts": facts,
                "_ansible_facts_cacheable": cacheable,
            }
        if name == "fail":
            return {"failed": True, "changed": False, "msg": args.get("msg", "Failed as requested from task")}
        if name in ("template", "copy"):
            return self._run_file_action(name, task, args, ctx, become)
        if name == "assert":
            # `that` entries are expressions, never pre-templated (a templated
            # string would be untrusted input to the evaluator).
            tests = (task.get("args") or {}).get("that", args.get("that", []))
            if isinstance(tests, str):
                tests = [tests]
            ok = self.when(tests, ctx)
            if ok:
                return {"changed": False, "msg": args.get("success_msg", "All assertions passed")}
            return {"failed": True, "changed": False,
                    "msg": args.get("fail_msg", args.get("msg", "Assertion failed"))}
        return {"failed": True, "changed": False, "msg": "Unsupported special action: %s" % name}

    def _run_accelerated_action(self, task, args, become=False):
        name = str(task.get("action") or "").split(".")[-1]
        classification = task.get("accelerator")
        required = classification == "required"
        if not required and name not in self.accelerated_modules:
            return None
        if classification not in ("always", "dynamic", "required"):
            return None
        from ansiroll.accelerated_modules import can_accelerate, run
        checked_started = time.monotonic()
        if classification == "dynamic" and not can_accelerate(
                name, args, check_mode=self.check_mode):
            return None
        checked_time = time.monotonic() - checked_started
        started = time.monotonic()
        worker_identity = None
        options = dict(task.get("accelerator_options") or {})
        options["diff_mode"] = bool(self.diff_mode)
        if self._needs_become(become):
            result = self._run_worker_accelerated(
                name, args, become=True,
                options=options,
            )
            worker = self._workers.get((True, self.become_user))
            if worker:
                worker_identity = worker.get("identity")
        else:
            result = run(name, args, check_mode=self.check_mode,
                         environment=self._module_env,
                         options=options)
        if result is None:
            if required:
                return {
                    "failed": True,
                    "changed": False,
                    "msg": (
                        "ansiroll: replacement-safe accelerator %s "
                        "could not start its execution worker" % name
                    ),
                }
            return None
        if isinstance(result, dict) and result.get("_ansiroll_fallback"):
            if required:
                return {
                    "failed": True,
                    "changed": False,
                    "msg": (
                        "ansiroll: replacement-safe accelerator %s "
                        "violated its no-fallback contract" % name
                    ),
                }
            return None
        if self.profile and isinstance(result, dict):
            profile = {
                "action": task.get("action"),
                "module": "accelerated.%s" % name,
                "worker_load": checked_time,
                "worker_cached": True,
                "worker_run": time.monotonic() - started,
                "worker_total": time.monotonic() - checked_started,
                "worker_identity": worker_identity,
            }
            self._record_profile(profile)
            result["_ansiroll_profile"] = profile
        return result

    def _record_profile(self, profile):
        name = str(profile.get("module") or profile.get("action") or "unknown")
        total = self.profile_totals.setdefault(name, {
            "count": 0, "worker_load": 0.0, "worker_run": 0.0,
            "worker_total": 0.0, "worker_identities": {},
        })
        total["count"] += 1
        for key in ("worker_load", "worker_run", "worker_total"):
            total[key] += float(profile.get(key) or 0.0)
        identity = profile.get("worker_identity")
        if identity:
            identities = total["worker_identities"]
            identities[identity] = identities.get(identity, 0) + 1

    def _run_worker_accelerated(self, name, args, become=False, options=None):
        try:
            w = self._get_worker(bool(become))
            req = {"op": "accelerated", "name": name, "args": args,
                   "check_mode": self.check_mode, "env": self._module_env,
                   "options": options or {}}
            self._frame_send(w["proc"], req)
            resp = self._frame_recv(w["proc"])
            if resp is None:
                raise RuntimeError("worker closed unexpectedly")
            if isinstance(resp, dict) and resp.get("_ansiroll_fallback"):
                return None
            return resp
        except Exception:
            self._close_workers()
            return None

    def _run_worker_operation(self, op, args, become=False):
        """Run an internal worker operation used by baked file writes."""
        try:
            w = self._get_worker(bool(become))
            self._frame_send(w["proc"], {
                "op": op, "args": args, "check_mode": self.check_mode,
            })
            response = self._frame_recv(w["proc"])
            if response is None:
                raise RuntimeError("worker closed unexpectedly")
            return response
        except Exception:
            self._close_workers()
            return None

    def _run_async_module(self, action, args, become=False):
        meta = self.modules.get(action)
        if meta is None:
            return None
        params = json.dumps({"ANSIBLE_MODULE_ARGS": args})
        try:
            ack = self._run_async_via_worker(meta, params, become)
        except Exception as ex:
            detail = self._worker_stderr(bool(become))
            self._close_workers()
            return {
                "failed": True,
                "changed": False,
                "msg": "ansiroll: async worker failed for %s (%s)%s"
                       % (meta["module_fqn"], ex, (": " + detail) if detail else ""),
                "module_stderr": detail,
            }
        jid = ack.get("jid") or str(ack.get("pid"))
        return {
            "changed": True,
            "ansible_job_id": jid,
            "started": 1,
            "finished": 0,
            "results_file": "",
        }

    def _run_file_action(self, name, task, args, ctx, become=False):
        """Handle template/copy using src content baked at compile time.

        With a real UID change, the persistent sudo worker writes root-owned
        destinations such as /etc/sudoers.d directly."""
        dest = args.get("dest")
        if not dest:
            return {"failed": True, "changed": False, "msg": "%s: dest is required" % name}
        baked = task.get("_baked") or {}
        src = args.get("src")
        if name == "copy" and "content" in args:
            new_content = args["content"]
            if not isinstance(new_content, str):
                new_content = json.dumps(new_content)
        else:
            if src is None:
                return {"failed": True, "changed": False, "msg": "%s: src is required" % name}
            raw = baked.get(src)
            meta = (task.get("_baked_meta") or {}).get(src) or {}
            if raw is None and self.controller_source_fn:
                try:
                    fetched = self.controller_source_fn(src)
                    raw = fetched.get("data")
                    meta = fetched.get("meta") or {}
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", "surrogateescape")
                except Exception as ex:
                    return {"failed": True, "changed": False,
                            "msg": "%s: cannot fetch controller src %r: %s"
                                   % (name, src, ex)}
            if raw is None:
                return {"failed": True, "changed": False,
                        "msg": "%s: src %r was not baked into the compiled file" % (name, src)}
            try:
                raw = self._asset_text(raw)
            except Exception as ex:
                return {"failed": True, "changed": False,
                        "msg": "%s: cannot load embedded src %r: %s" % (name, src, ex)}
            if name == "template":
                tctx = dict(ctx)
                fullpath = meta.get("fullpath", src)
                try:
                    import platform
                    import pwd
                    uid = meta.get("uid")
                    try:
                        uid = pwd.getpwuid(uid).pw_name if uid is not None else uid
                    except Exception:
                        pass
                    tctx.setdefault("template_host", platform.node())
                    tctx.setdefault("template_path", src)
                    tctx.setdefault("template_fullpath", fullpath)
                    tctx.setdefault("template_destpath", dest)
                    tctx.setdefault("template_mtime",
                                    _dt.datetime.fromtimestamp(meta["mtime"])
                                    if meta.get("mtime") is not None else None)
                    tctx.setdefault("template_uid", uid)
                    tctx.setdefault("template_run_date", _dt.datetime.now())
                    tctx.setdefault("ansible_managed", "Ansible managed")
                except Exception:
                    pass
                try:
                    new_content = _as_text(self.templater.render_string(raw, tctx))
                except Exception as ex:
                    return {"failed": True, "changed": False,
                            "msg": "template: failed to render %s: %s" % (src, ex)}
            else:
                new_content = raw

        # When dest is a directory (trailing slash or existing dir), copy into it
        # under the source's basename, like Ansible's copy/template.
        if src and (str(dest).endswith("/") or os.path.isdir(dest)):
            dest = os.path.join(dest, os.path.basename(str(src)))

        mode = _norm_mode(args.get("mode"))
        owner = args.get("owner")
        group = args.get("group")
        needs_become = self._needs_become(become)
        try:
            if needs_become:
                res = self._run_worker_operation(
                    "fast_write",
                    {"dest": dest, "content": new_content, "mode": mode,
                     "owner": owner, "group": group},
                    become=True,
                )
                if res is not None:
                    if res.get("failed"):
                        return {"failed": True, "changed": False,
                                "msg": "%s: %s" % (name, res.get("msg"))}
                    return res
                return {"failed": True, "changed": False,
                        "msg": "%s: privileged worker unavailable" % name}
            old = _read_maybe(dest, False, self)
            changed = old != new_content
            if changed and not self.check_mode:
                parent = os.path.dirname(dest)
                if parent and not os.path.isdir(parent):
                    os.makedirs(parent, exist_ok=True)
                with open(dest, "w", encoding="utf-8", errors="surrogateescape") as fh:
                    fh.write(new_content)
                _chmod_chown(dest, mode, owner, group)
            return {"changed": changed, "dest": dest}
        except OSError as ex:
            return {"failed": True, "changed": False, "msg": "%s: %s" % (name, ex)}

    def _absorb_facts(self, task, res):
        refreshed_vars = res.pop("_ansiroll_vars", None)
        if isinstance(refreshed_vars, dict):
            self.vars.update(refreshed_vars)
        facts = res.get("ansible_facts")
        if isinstance(facts, dict):
            self.vars.update(facts)
            short_action = str(task.get("action") or "").rsplit(".", 1)[-1]
            if short_action == "set_fact":
                self._set_fact_overrides.update(facts)
            elif short_action == "include_vars":
                self._include_vars_layer.update(facts)
            existing = self.vars.get("ansible_facts")
            if not isinstance(existing, dict):
                existing = self.vars["ansible_facts"] = {}
            # The `ansible_facts` magic var holds facts without the ansible_
            # prefix ('fqdn' in ansible_facts), unlike the injected top-level
            # ansible_* vars.
            for k, v in facts.items():
                key = k[8:] if isinstance(k, str) and k.startswith("ansible_") else k
                existing[key] = v

    def _notification_names(self, task, ctx):
        names = []
        for value in task.get("notify") or []:
            with self._strict_vars():
                rendered = _fail_on_undef(self.template(value, ctx, strict=True))
            values = rendered if isinstance(rendered, list) else [rendered]
            names.extend(value for value in values if value != _OMIT)
        return names

    def _queue_notify(self, names):
        for name in names:
            matches = self._handlers_for(name)
            if not matches:
                message = (
                    "The requested handler '%s' was not found in either the main "
                    "handlers list nor in the listening handlers list" % _as_text(name)
                )
                if self._error_on_missing_handler:
                    raise MissingHandler(message)
                self.on_warning(message)
            elif self._flushing or not _core_at_least(2, 15):
                # Older cores bind when notified. Newer cores also bind
                # immediately during a flush so forward notifications can
                # run in the same pass.
                self._notified_handlers.update(matches)
            elif name not in self.notified:
                self.notified.append(name)

    # ---- handlers --------------------------------------------------------
    def _add_handlers(self, handlers, scope, become):
        for _, block in groupby(handlers, key=lambda handler: handler.get("handler_block")):
            start = len(self.handlers)
            self.handlers.extend((handler, scope, become) for handler in block)
            self._handler_blocks.append(range(start, len(self.handlers)))

    def _handler_scope(self, handler, scope):
        params = scope.get(_INCLUDE_PARAMS_KEY)
        if not params:
            return scope
        ctx = dict(handler.get("role_defaults") or {})
        ctx.update(self.vars)
        ctx.update(handler.get("role_vars") or {})
        ctx.update(scope)
        self._layered_ctx(ctx, role_params=handler.get("role_params"))
        ctx.setdefault("omit", _OMIT)
        ctx.setdefault("hostvars", {self.host: ctx})
        ctx.setdefault("vars", ctx)
        return _with_high_vars(scope, self.template_vars(params, ctx))

    def _handlers_for(self, notification):
        """Indexes of the handlers a notification targets.

        Search the newest blocks first, in declaration order within each block.
        Also match listeners, deduplicated by handler name on modern cores.
        """
        def names(handler, scope):
            values = handler.get("handler_names")
            if values is None:
                values = [handler["name"]] if handler.get("name") else []
            if any("{{" in value or "{%" in value for value in values):
                ctx = dict(handler.get("handler_name_vars") or {})
                ctx.update(scope.get(_INCLUDE_PARAMS_KEY) or {})
                ctx.update(self.extra_vars)
                for key in ("__ansiroll_controller_env", "__ansiroll_controller_config"):
                    ctx[key] = self.vars.get(key) or {}
                ctx["vars"] = _HandlerNameVars(ctx, self, ctx)
                with self._strict_vars():
                    values = [self.template(value, ctx, strict=True) for value in values]
                # Through 2.11 an undefined name aborts notification lookup.
                if _core_at_least(2, 10) and not _core_at_least(2, 12):
                    try:
                        _fail_on_undef(values)
                    except jinja2.UndefinedError as ex:
                        raise MissingHandler(str(ex))
                values = [value for value in values if not _is_undef(value)]
            return values

        found = []
        indexes = [idx for block in reversed(self._handler_blocks) for idx in block]
        for idx in indexes:
            handler, scope, _ = self.handlers[idx]
            if notification in names(handler, scope):
                found.append(idx)
                break
        seen = set()
        modern = _core_at_least(2, 15)
        listener_indexes = indexes if modern else range(len(self.handlers))
        for idx in listener_indexes:
            handler, scope, _ = self.handlers[idx]
            if notification in (handler.get("listen") or []):
                if modern:
                    own = names(handler, scope)
                    own = own[0] if own else None
                    if own and own in seen:
                        continue
                    seen.add(own)
                found.append(idx)
        return found

    def flush_handlers(self):
        for name in self.notified:
            self._notified_handlers.update(self._handlers_for(name))
        self.notified = []
        if not self._notified_handlers:
            return
        # Handlers run in definition order (PlayIterator HANDLERS state); a
        # failing handler fails the host and ends the flush.
        self._flushing = True
        try:
            idx = 0
            while idx < len(self.handlers):
                if idx in self._notified_handlers:
                    self._notified_handlers.discard(idx)
                    handler, scope, become = self.handlers[idx]
                    self.run_task(handler, self._handler_scope(handler, scope),
                                  become or self._play_become)
                idx += 1
        except TaskFailed as ex:
            # In 2.14 a failed HANDLERS state goes straight to COMPLETE.
            # 2.15+ returns to the block containing the flush first.
            if _core_at_least(2, 14) and not _core_at_least(2, 15):
                ex.ends_play = True
            raise
        finally:
            self._flushing = False

    # ---- facts -----------------------------------------------------------
    def gather_facts(self, environment=None, become=False, become_user=None,
                     fact_schema=None, ignore_errors=None, ctx=None):
        # Reported as a task so the recap matches Ansible's "Gathering Facts".
        node = {"name": "Gathering Facts", "action": "gather_facts",
                "uuid": "__gather_facts__", "runtime_id": "__gather_facts__"}
        if ctx is None:
            ctx = self.ctx()
        self.emit_start(node)
        inherited_become_user = self.become_user
        try:
            if become_user:
                self.become_user = _as_text(self.template(become_user, ctx))
            self._module_env = self._task_environment(
                {"environment": environment}, ctx)
            fast_facts = os.environ.get("ANSIROLL_FAST_FACTS", "").lower()
            if fast_facts in ("1", "true", "yes", "on"):
                from ansiroll.accelerated_modules import facts
                started = time.monotonic()
                res = facts.run(fact_schema=fact_schema)
                provenance = {
                    "mode": "native-agent",
                    "reason": "accelerated facts",
                }
                if self.profile and not res.get("_ansiroll_fallback"):
                    elapsed = time.monotonic() - started
                    fact_profile = {
                        "action": "ansible.builtin.setup",
                        "module": "accelerated.setup",
                        "worker_cached": True,
                        "worker_run": elapsed,
                        "worker_total": elapsed,
                    }
                    self._record_profile(fact_profile)
                    res["_ansiroll_profile"] = fact_profile
                if res.get("_ansiroll_fallback"):
                    provenance = {"mode": "real-ansiballz"}
                    res = self.run_module("ansible.builtin.setup", {}, become=become)
            else:
                provenance = {"mode": "real-ansiballz"}
                res = self.run_module("ansible.builtin.setup", {}, become=become)
        except Exception as ex:
            provenance = {"mode": "native-agent"}
            res = {"failed": True, "changed": False,
                   "msg": "ansiroll: fact gathering raised %s: %s"
                          % (type(ex).__name__, ex)}
        finally:
            self._module_env = None
            self.become_user = inherited_become_user
        self._task_provenance[id(node)] = provenance
        if not isinstance(res, dict) or res.get("failed"):
            # Like Ansible, a failed setup is a fatal task failure for the host.
            if not isinstance(res, dict):
                res = {"failed": True, "changed": False,
                       "msg": "ansiroll: fact gathering returned no result"}
            res.setdefault("changed", False)
            # The setup task's block belongs to the play, so it inherits the
            # play's ignore_errors (PlayIterator: Block(play=self._play)).
            if isinstance(ignore_errors, str):
                ignore_errors = self.template(ignore_errors, ctx)
            if _to_bool(ignore_errors):
                self.emit(node, "failed", res, ignored=True)
                self._tally("ok")
                self._tally("ignored")
                return True
            self.emit(node, "failed", res)
            self._tally("failures")
            return False
        if isinstance(res.get("ansible_facts"), dict):
            res["ansible_facts"]["_ansible_facts_gathered"] = True
        self._absorb_facts({"action": "setup"}, res)
        self.emit(node, "ok", res if self.on_result else {"changed": False})
        self._tally("ok")
        return True

    # ---- module execution ------------------------------------------------
    def cleanup(self):
        self._close_workers()

    def _asset_bytes(self, marker):
        if not (isinstance(marker, dict)
                and set(marker) == {"__ansiroll_asset__"}):
            if isinstance(marker, bytes):
                return marker
            return marker.encode("utf-8", "surrogateescape")
        digest = marker["__ansiroll_asset__"]
        if self.resource_reader is None:
            raise RuntimeError("ansiroll: no resource bundle is available for asset %s" % digest)
        try:
            data = self.resource_reader("assets/" + digest)
        except Exception as ex:
            raise RuntimeError("ansiroll: embedded asset %s is missing: %s" % (digest, ex))
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            raise RuntimeError("ansiroll: embedded asset %s failed its digest check" % digest)
        return data

    def _asset_text(self, marker):
        if not (isinstance(marker, dict)
                and set(marker) == {"__ansiroll_asset__"}):
            return marker
        return self._asset_bytes(marker).decode("utf-8", "surrogateescape")

    def run_module(self, action, args, become=False):
        meta = self.modules.get(action)
        if meta is None:
            return {"failed": True, "changed": False,
                    "msg": "ansiroll: module %r was not embedded" % action}
        module_args = dict(args)
        module_args["_ansible_check_mode"] = bool(self.check_mode)
        module_args["_ansible_diff"] = bool(self.diff_mode)
        params = json.dumps({"ANSIBLE_MODULE_ARGS": module_args})
        # Modules run in the persistent forking worker (in-memory, decode-once).
        # If the worker DIES (OOM / bug / sudo failure), the task's outcome is
        # unknown -- so we fail loudly rather than silently retrying (which could
        # double-apply a non-idempotent change) or silently switching engines. A
        # fresh worker is started for the next task, so a one-off crash doesn't
        # poison the rest of the play.
        try:
            return self._run_via_worker(meta, params, become, action)
        except Exception as ex:
            detail = self._worker_stderr(bool(become))
            self._close_workers()
            return {
                "failed": True, "changed": False,
                "msg": ("ansiroll: module execution worker died before returning a "
                        "result for %s (%s); task outcome is unknown%s"
                        % (meta["module_fqn"], ex, (": " + detail) if detail else "")),
                "module_stderr": detail,
            }

    # ---- persistent worker -------------------------------------------------
    def _run_via_worker(self, meta, params, become, action):
        started = time.monotonic()
        w = self._get_worker(bool(become))
        fqn = meta["module_fqn"]
        profile = meta.get("profile", "legacy")
        loaded = fqn in w["loaded"]
        load_time = 0.0
        if fqn not in w["loaded"]:
            load_started = time.monotonic()
            req = {"op": "load", "fqn": fqn, "profile": profile}
            if self.module_bundle is None:
                req["zip"] = meta["zip"]
            self._frame_send(w["proc"], req)
            ack = self._frame_recv(w["proc"])
            if not isinstance(ack, dict) or not ack.get("ok"):
                raise RuntimeError("worker load failed: %r" % (ack,))
            w["loaded"].add(fqn)
            load_time = time.monotonic() - load_started
        run_started = time.monotonic()
        request = {"op": "run", "fqn": fqn, "params": params,
                   "profile": profile, "env": self._module_env}
        self._send_worker_request(w["proc"], request, self._module_source_bytes)
        resp = self._frame_recv(w["proc"])
        if resp is None:
            raise RuntimeError("worker closed unexpectedly")
        run_time = time.monotonic() - run_started
        result = _parse_output(resp.get("out", ""), resp.get("err", ""), resp.get("rc", 0))
        if self.profile:
            profile_data = {
                "action": action,
                "module": fqn,
                "worker_load": load_time,
                "worker_cached": loaded,
                "worker_run": run_time,
                "worker_total": time.monotonic() - started,
                "worker_identity": w.get("identity"),
            }
            self._record_profile(profile_data)
            result["_ansiroll_profile"] = profile_data
        return result

    def _run_async_via_worker(self, meta, params, become):
        w = self._get_worker(bool(become))
        fqn = meta["module_fqn"]
        profile = meta.get("profile", "legacy")
        if fqn not in w["loaded"]:
            req = {"op": "load", "fqn": fqn, "profile": profile}
            if self.module_bundle is None:
                req["zip"] = meta["zip"]
            self._frame_send(w["proc"], req)
            ack = self._frame_recv(w["proc"])
            if not isinstance(ack, dict) or not ack.get("ok"):
                raise RuntimeError("worker load failed: %r" % (ack,))
            w["loaded"].add(fqn)
        request = {"op": "async_run", "fqn": fqn,
                   "params": params, "profile": profile}
        self._send_worker_request(w["proc"], request, self._module_source_bytes)
        ack = self._frame_recv(w["proc"])
        if not isinstance(ack, dict) or ack.get("error"):
            raise RuntimeError("worker async failed: %r" % (ack,))
        return ack

    def _needs_become(self, become):
        """Whether sudo changes the effective UID for this task."""
        if not become:
            return False
        target = str(self.become_user or "root")
        cached = self._become_required_cache.get(target)
        if cached is not None:
            return cached
        try:
            numeric = target[1:] if target.startswith("#") else target
            if numeric.isdigit():
                target_uid = int(numeric)
            else:
                import pwd
                target_uid = pwd.getpwnam(target).pw_uid
            required = target_uid != os.geteuid()
        except Exception:
            # Unknown names must still go through sudo so it can report the
            # authoritative error instead of silently running as the wrong user.
            required = True
        self._become_required_cache[target] = required
        return required

    def _get_worker(self, become):
        become = self._needs_become(become)
        key = (True, self.become_user) if become else False
        w = self._workers.get(key)
        if w and w["proc"].poll() is None:
            return w
        startup_started = time.monotonic()
        worker_code = _worker_source()
        cmd = [self.python, "-c", worker_code]
        if become:
            if self.become_password:
                # Authenticate through a separate process. If sudo has a valid
                # timestamp it will not consume stdin; writing the password to
                # the worker's stdin in that case corrupts the framed protocol
                # (the password's first four bytes look like a huge frame size).
                auth_started = time.monotonic()
                auth = subprocess.run(
                    ["sudo", "-S", "-p", "", "-u", self.become_user, "-v"],
                    input=self.become_password + "\n",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                )
                if auth.returncode != 0:
                    raise RuntimeError(
                        "sudo authentication failed for %s: %s"
                        % (self.become_user, (auth.stderr or auth.stdout or "").strip())
                    )
                self.engine_profile["worker_auth"] += (
                    time.monotonic() - auth_started
                )
            cmd = ["sudo", "-n", "-u", self.become_user,
                   self.python, "-c", worker_code]
        spawn_started = time.monotonic()
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0)
        self.engine_profile["worker_spawn"] += time.monotonic() - spawn_started
        identity = "sudo:%s" % self.become_user if become else "current-user"
        w = {"proc": proc, "loaded": set(), "identity": identity}
        bundle_started = time.monotonic()
        if self.module_bundle is not None:
            bundle_bytes = self.module_bundle
            self._frame_send(proc, {
                "op": "bundle", "size": len(bundle_bytes),
            })
            proc.stdin.write(bundle_bytes)
            proc.stdin.flush()
            ack = self._frame_recv(proc)
            if not isinstance(ack, dict) or not ack.get("ok"):
                try:
                    proc.kill()
                except Exception:
                    pass
                raise RuntimeError("worker raw bundle load failed: %r" % (ack,))
        self.engine_profile["worker_bundle"] += time.monotonic() - bundle_started
        self.engine_profile["workers_started"] += 1
        identities = self.engine_profile.setdefault("worker_identities", {})
        identities[identity] = identities.get(identity, 0) + 1
        self.engine_profile["worker_startup"] += time.monotonic() - startup_started
        self._workers[key] = w
        return w

    def _worker_stderr(self, become):
        """Best-effort read of a dead worker's stderr (why it died)."""
        become = self._needs_become(become)
        key = (True, self.become_user) if become else False
        w = self._workers.get(key)
        if not w or not w["proc"].stderr:
            return ""
        try:
            return w["proc"].stderr.read().decode("utf-8", "surrogateescape").strip()
        except Exception:
            return ""

    def _frame_send(self, proc, obj):
        b = json.dumps(obj).encode("utf-8")
        proc.stdin.write(struct.pack(">I", len(b)))
        proc.stdin.write(b)
        proc.stdin.flush()

    def _send_worker_request(self, proc, request, source=None):
        if source is None:
            self._frame_send(proc, request)
            return
        request = dict(request)
        request["source_size"] = len(source)
        self._frame_send(proc, request)
        proc.stdin.write(source)
        proc.stdin.flush()

    def _frame_recv(self, proc):
        head = self._readn(proc.stdout, 4)
        if not head:
            return None
        (n,) = struct.unpack(">I", head)
        body = self._readn(proc.stdout, n)
        if body is None:
            return None
        return json.loads(body.decode("utf-8"))

    @staticmethod
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

    def _close_workers(self):
        for w in self._workers.values():
            proc = w["proc"]
            try:
                self._frame_send(proc, {"op": "quit"})
            except Exception:
                pass
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._workers = {}


def _norm_mode(mode):
    if mode is None:
        return None
    if isinstance(mode, int):
        return "0%o" % mode  # YAML unquoted octal arrives as int
    return str(mode)


def _read_maybe(path, become, rt):
    """Read existing file content for change detection; tolerate no-permission."""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="surrogateescape") as fh:
                return fh.read()
        return None
    except OSError:
        if become:
            try:
                cmd = ["sudo"]
                cmd += (["-S", "-p", ""] if rt.become_password else ["-n"])
                cmd += ["-u", rt.become_user]
                cmd += ["cat", path]
                stdin = (rt.become_password + "\n") if rt.become_password else None
                proc = subprocess.run(cmd, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
                if proc.returncode == 0:
                    return proc.stdout
            except Exception:
                pass
        return None  # unreadable -> treat as changed


def _chmod_chown(path, mode, owner, group):
    try:
        if mode:
            os.chmod(path, int(mode, 8))
    except Exception:
        pass
    if owner or group:
        try:
            import shutil
            shutil.chown(path, user=owner, group=group)
        except Exception:
            pass


def _parse_output(out, err, rc):
    out = out or ""
    try:
        return _normalize_module_result(json.loads(out))
    except Exception:
        pass
    # Modules sometimes emit leading noise; recover the JSON object.
    start = out.find("{")
    end = out.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return _normalize_module_result(json.loads(out[start:end + 1]))
        except Exception:
            pass
    return {
        "failed": True,
        "changed": False,
        "rc": rc,
        "msg": "ansiroll: could not parse module output",
        "module_stdout": out,
        "module_stderr": err,
    }


def _normalize_module_result(result):
    if not isinstance(result, dict):
        return result
    if "stdout" in result and "stdout_lines" not in result:
        stdout = result.get("stdout")
        result["stdout_lines"] = stdout.splitlines() if isinstance(stdout, str) else []
    if "stderr" in result and "stderr_lines" not in result:
        stderr = result.get("stderr")
        result["stderr_lines"] = stderr.splitlines() if isinstance(stderr, str) else []
    return result


def _result_failed(res):
    # TaskResult.is_failed: an evaluated failed_when decides the outcome even
    # when the result says failed (e.g. until retries exhausted before 2.21).
    if "failed_when_result" in res:
        return bool(res["failed_when_result"])
    return bool(res.get("failed"))


def _status_of(res):
    if res.get("unreachable"):
        return "unreachable"
    if _result_failed(res):
        return "failed"
    if res.get("skipped"):
        return "skipped"
    if res.get("changed"):
        return "changed"
    return "ok"


def _hostname():
    try:
        import socket
        return socket.gethostname()
    except Exception:
        return "localhost"


def main(plays, modules, ansiballz_code, host_vars=None, check_mode=False, diff_mode=False,
         become_password=None, defer_handlers=False, initial_notified=None,
         extra_vars=None, pretty=False, profile=False, module_bundle=None,
         resource_reader=None, engine_bootstrap=None, native_only=False):
    runtime_started = time.monotonic()
    host_vars = dict(host_vars or {})
    failed = False
    total = {"ok": 0, "changed": 0, "failures": 0, "skipped": 0,
             "unreachable": 0, "rescued": 0, "ignored": 0}
    host = host_vars.get("inventory_hostname") or _hostname()
    profile_totals = {}
    task_profile_totals = {}
    task_wall_totals = {}
    engine_profile = {
        "bootstrap": float(engine_bootstrap or 0.0),
        "runtime_init": 0.0,
        "play_execution": 0.0,
        "worker_cleanup": 0.0,
        "workers_started": 0,
        "worker_identities": {},
        "worker_auth": 0.0,
        "worker_spawn": 0.0,
        "worker_bundle": 0.0,
        "worker_startup": 0.0,
    }
    rt = None
    host_var_overrides = {}
    include_vars_layer = {}
    for play in plays:
        if failed:
            # A host that failed is removed from the rest of the playbook.
            break
        if pretty:
            banner = "PLAY [%s] " % (play.get("name") or play.get("hosts") or "")
            sys.stdout.write("\n" + banner + "*" * max(3, 79 - len(banner)) + "\n")
        init_started = time.monotonic()
        rt = Runtime(modules, ansiballz_code, host_vars, check_mode=check_mode,
                     diff_mode=diff_mode,
                     become_password=become_password, extra_vars=extra_vars,
                     pretty=pretty, profile=profile, module_bundle=module_bundle,
                     resource_reader=resource_reader,
                     host_var_overrides=host_var_overrides,
                     native_only=native_only)
        rt._include_vars_layer = dict(include_vars_layer)
        engine_profile["runtime_init"] += time.monotonic() - init_started
        try:
            play_started = time.monotonic()
            rt.run_play(play, defer_handlers=defer_handlers, initial_notified=initial_notified)
        except MissingHandler as ex:
            sys.stderr.write("ERROR: %s\n" % ex)
            return 1
        finally:
            engine_profile["play_execution"] += time.monotonic() - play_started
            cleanup_started = time.monotonic()
            rt.cleanup()  # shut the worker down
            engine_profile["worker_cleanup"] += time.monotonic() - cleanup_started
        host_vars = rt.vars  # carry facts/registers across plays for the same host
        host_var_overrides = rt._set_fact_overrides
        include_vars_layer = rt._include_vars_layer
        host = rt.host
        failed = failed or rt.failed
        for k in total:
            total[k] += rt.stats.get(k, 0)
        for module_name, values in rt.profile_totals.items():
            aggregate = profile_totals.setdefault(module_name, {
                "count": 0, "worker_load": 0.0, "worker_run": 0.0,
                "worker_total": 0.0, "worker_identities": {},
            })
            for key in ("count", "worker_load", "worker_run", "worker_total"):
                aggregate[key] += values.get(key, 0)
            for identity, count in values.get("worker_identities", {}).items():
                identities = aggregate["worker_identities"]
                identities[identity] = identities.get(identity, 0) + count
        for task_key, values in rt.task_profile_totals.items():
            aggregate = task_profile_totals.setdefault(task_key, {
                "name": values.get("name"),
                "action": values.get("action"),
                "count": 0,
                "total": 0.0,
                "module": 0.0,
                "engine": 0.0,
                "max": 0.0,
                "worker_identities": {},
            })
            for key in ("count", "total", "module", "engine"):
                aggregate[key] += values.get(key, 0)
            aggregate["max"] = max(aggregate["max"], values.get("max", 0))
            for identity, count in values.get("worker_identities", {}).items():
                identities = aggregate["worker_identities"]
                identities[identity] = identities.get(identity, 0) + count
        for task_key, values in rt.task_wall_totals.items():
            aggregate = task_wall_totals.setdefault(task_key, {
                "name": values.get("name"),
                "action": values.get("action"),
                "count": 0,
                "total": 0.0,
                "max": 0.0,
            })
            for key in ("count", "total"):
                aggregate[key] += values.get(key, 0)
            aggregate["max"] = max(aggregate["max"], values.get("max", 0))
        for key in ("workers_started", "worker_auth", "worker_spawn",
                    "worker_bundle", "worker_startup"):
            engine_profile[key] += rt.engine_profile.get(key, 0)
        for identity, count in rt.engine_profile.get("worker_identities", {}).items():
            identities = engine_profile["worker_identities"]
            identities[identity] = identities.get(identity, 0) + count
    if not pretty:
        # Emit the accumulated var state + pending handler notifications so a controller
        # can carry them into the next segment (segment-driven delegation).
        sys.stdout.write("%s %s\n" % (STATE_MARKER, json.dumps(
            {"vars": (rt.vars if rt else host_vars), "notified": (rt.notified if rt else [])},
            default=str)))
        # Authoritative recap line for controllers (the strategy parses this).
        sys.stdout.write("%s %s\n" % (STATS_MARKER, json.dumps({"host": host, "stats": total})))
        sys.stdout.flush()
    s = total
    if pretty:
        sys.stdout.write("\nPLAY RECAP " + "*" * 68 + "\n")
        sys.stdout.write(
            "%-26s : ok=%-4d changed=%-4d unreachable=%-4d failed=%-4d "
            "skipped=%-4d rescued=%-4d ignored=%-4d\n"
            % (host, s["ok"] + s["changed"], s["changed"], s["unreachable"],
               s["failures"], s["skipped"], s["rescued"], s["ignored"]))
        sys.stdout.flush()
    else:
        sys.stderr.write(
            "\nPLAY RECAP [%s] : ok=%d changed=%d unreachable=%d failed=%d "
            "skipped=%d rescued=%d ignored=%d\n"
            % (host, s["ok"] + s["changed"], s["changed"], s["unreachable"],
               s["failures"], s["skipped"], s["rescued"], s["ignored"])
        )
    if profile:
        engine_profile["runtime_total"] = time.monotonic() - runtime_started
        engine_profile["total"] = (
            engine_profile["bootstrap"] + engine_profile["runtime_total"]
        )
        engine_profile["profiled_module_total"] = sum(
            values.get("worker_total", 0.0)
            for values in profile_totals.values()
        )
        engine_profile["runtime_outside_profiled_modules"] = max(
            0.0,
            engine_profile["runtime_total"]
            - engine_profile["profiled_module_total"],
        )
        engine_profile["worker_tasks"] = [
            {
                "name": values.get("name"),
                "action": values.get("action"),
                "worker_identities": values.get("worker_identities", {}),
            }
            for values in task_profile_totals.values()
            if values.get("worker_identities")
        ]
        task_rows = []
        for task_key, wall in task_wall_totals.items():
            execution = task_profile_totals.get(task_key) or {}
            module_total = float(execution.get("module") or 0.0)
            wall_total = float(wall.get("total") or 0.0)
            task_rows.append({
                "name": wall.get("name"),
                "action": wall.get("action"),
                "count": wall.get("count", 0),
                "total": wall_total,
                "module": module_total,
                "engine": max(0.0, wall_total - module_total),
                "max": wall.get("max", 0.0),
            })
        engine_profile["slow_tasks"] = sorted(
            task_rows,
            key=lambda value: value.get("total", 0.0),
            reverse=True,
        )[:15]
        engine_profile["slow_engine_tasks"] = sorted(
            task_rows,
            key=lambda value: value.get("engine", 0.0),
            reverse=True,
        )[:15]
        sys.stderr.write(
            "ANSIROLL_PROFILE_SUMMARY %s\n"
            % json.dumps(profile_totals, sort_keys=True)
        )
        sys.stderr.write(
            "ANSIROLL_ENGINE_PROFILE %s\n"
            % json.dumps(engine_profile, sort_keys=True)
        )
        sys.stderr.flush()
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Agent mode: a long-lived process driven by the controller over a bidirectional
# length-prefixed JSON channel (fd 0 in, a dup of fd 1 out). Results stream live,
# the module worker stays warm for the whole play, and delegated / controller-
# coupled tasks are handed back to the controller inline (no re-ships).
# ---------------------------------------------------------------------------

# Tiny bootstrap passed via `python3 -c`: read the agent source as the first frame,
# exec it, then hand control to agent_main() which drives the channel.
AGENT_BOOTSTRAP = (
    "import os,struct\n"
    "def _r(n):\n"
    " b=b''\n"
    " while len(b)<n:\n"
    "  c=os.read(0,n-len(b))\n"
    "  if not c: os._exit(0)\n"
    "  b+=c\n"
    " return b\n"
    "_n=struct.unpack('>I',_r(4))[0]\n"
    "_src=_r(_n).decode('utf-8','surrogateescape')\n"
    "_g={'__name__':'__ansiroll_agent__'}\n"
    "exec(compile(_src,'<ansiroll-agent>','exec'),_g)\n"
    "_g['agent_main']()\n"
)


def _chan_read_exact(n):
    buf = b""
    while len(buf) < n:
        c = os.read(0, n - len(buf))
        if not c:
            return None
        buf += c
    return buf


def _chan_recv():
    head = _chan_read_exact(4)
    if head is None:
        return None
    (n,) = struct.unpack(">I", head)
    body = _chan_read_exact(n)
    if body is None:
        return None
    return json.loads(body.decode("utf-8", "surrogateescape"))


def _chan_send(out_fd, obj):
    b = json.dumps(obj, default=str).encode("utf-8", "surrogateescape")
    os.write(out_fd, struct.pack(">I", len(b)))
    os.write(out_fd, b)


def _delegate_vars(ctx):
    """Return the serializable part of a task context for controller actions."""
    safe = {}
    for key, value in (ctx or {}).items():
        if key == "vars":
            continue
        try:
            json.dumps(value, default=str)
        except (TypeError, ValueError):
            continue
        safe[key] = value
    return safe


def agent_main():
    """Entry point when launched as a controller-driven agent (see AGENT_BOOTSTRAP).

    Reads PLAYS / MODULES / ANSIBALLZ_CODE / _INJECTED_* from the module globals
    (baked into the shipped source), runs the play streaming results, and services
    delegated tasks by asking the controller."""
    g = globals()
    plays = g["PLAYS"]
    modules = g["MODULES"]
    ansiballz = g["ANSIBALLZ_CODE"]
    host = g.get("_INJECTED_HOST") or _hostname()
    host_vars = {"inventory_hostname": host}
    host_vars.update(g.get("_BAKED_GROUP_VARS") or {})
    if "_INJECTED_VARS" in g:
        host_vars.update(g["_INJECTED_VARS"])
    extra_vars = g.get("_INJECTED_EXTRA_VARS") or {}
    host_var_overrides = g.get("_INJECTED_HOST_VAR_OVERRIDES") or {}
    specialized_vars = g.get("_SPECIALIZED_VARS") or {}
    conflicts = sorted(k for k, value in specialized_vars.items()
                       if k in extra_vars and extra_vars[k] != value)
    if conflicts:
        raise RuntimeError(
            "runtime extra vars conflict with fixed build-time values: %s"
            % ", ".join(conflicts)
        )
    extra_vars.update(specialized_vars)
    become_pass = g.get("_INJECTED_BECOME_PASS")
    delegate_on_target = bool(g.get("_INJECTED_DELEGATE_ON_TARGET", False))
    skip_run_once = bool(g.get("_INJECTED_SKIP_RUN_ONCE", False))
    check_mode = bool(g.get("_INJECTED_CHECK_MODE", False))
    diff_mode = bool(g.get("_INJECTED_DIFF_MODE", False))
    os.environ["ANSIROLL_ACCELERATED_MODULES"] = str(
        g.get("_INJECTED_ACCELERATED_MODULES", "")
    )
    if g.get("_INJECTED_FAST_FACTS", False):
        os.environ["ANSIROLL_FAST_FACTS"] = "1"
    else:
        os.environ.pop("ANSIROLL_FAST_FACTS", None)

    # Reserve fd 1 for the channel; send stray stdout to stderr so it can't corrupt it.
    resp_fd = os.dup(1)
    try:
        os.dup2(2, 1)
    except OSError:
        pass
    sys.stdout = sys.stderr

    profile = bool(g.get("_INJECTED_PROFILE", False))
    native_only = bool(g.get("_INJECTED_NATIVE_ONLY", False))
    rt = Runtime(modules, ansiballz, host_vars, check_mode=check_mode,
                 diff_mode=diff_mode,
                 become_password=become_pass,
                 profile=profile, extra_vars=extra_vars,
                 host_var_overrides=host_var_overrides,
                 native_only=native_only,
                 skip_run_once=skip_run_once,
                 module_bundle=g.get("_ansiroll_import_bytes"),
                 resource_reader=g.get("_ansiroll_resource_read"))
    rt.on_result = lambda frame: _chan_send(resp_fd, {"t": "result", "frame": frame})
    rt.on_task_start = lambda frame: _chan_send(resp_fd, {"t": "task_start", "frame": frame})
    rt.on_warning = lambda message: _chan_send(resp_fd, {"t": "warning", "message": message})
    if not delegate_on_target:
        def _delegate(task, args, become, ctx):
            _chan_send(resp_fd, {"t": "delegate", "uuid": task.get("uuid"),
                                 "runtime_id": task.get("runtime_id"),
                                 "action": task.get("action"), "args": args,
                                 "vars": _delegate_vars(ctx),
                                 "become": become, "to": task.get("delegate_to"),
                                 "become_user": rt.become_user,
                                 "check_mode": bool(rt.check_mode),
                                 "diff_mode": bool(rt.diff_mode),
                                 "fallback": bool(task.get("fallback")),
                                 "search_dirs": task.get("search_dirs"),
                                 "name": task.get("name")})
            reply = _chan_recv()
            if not reply or reply.get("t") != "delegate_result":
                return {"failed": True, "changed": False,
                        "msg": "ansiroll: controller did not return a delegate result"}
            return reply.get("result", {})
        rt.delegate_fn = _delegate

        def _controller_source(path):
            _chan_send(resp_fd, {"t": "source", "path": path})
            reply = _chan_recv()
            if not reply or reply.get("t") != "source_result":
                raise RuntimeError("controller did not return a source result")
            source = reply.get("source") or {}
            if source.get("error"):
                raise RuntimeError(source["error"])
            try:
                data = base64.b64decode(source.get("data") or "")
            except Exception as ex:
                raise RuntimeError("invalid controller source data: %s" % ex)
            return {"data": data, "meta": source.get("meta") or {}}
        rt.controller_source_fn = _controller_source

    failed = False
    total = {"ok": 0, "changed": 0, "failures": 0, "skipped": 0,
             "unreachable": 0, "rescued": 0, "ignored": 0}
    try:
        for play in plays:
            if not g.get("_INJECTED_GATHER_FACTS", True):
                play = dict(play, gather_facts=False)
            rt.run_play(play)
            failed = failed or rt.failed
            for k in total:
                total[k] += rt.stats.get(k, 0)
            rt.stats = {k: 0 for k in total}
            if failed:
                # A host that failed is removed from the rest of the playbook.
                break
    except MissingHandler as ex:
        _chan_send(resp_fd, {"t": "error", "message": str(ex)})
        return
    finally:
        rt.cleanup()
    _chan_send(resp_fd, {"t": "stats", "host": rt.host, "stats": total})
    _chan_send(resp_fd, {"t": "done", "failed": failed})
