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
import selectors
import subprocess
import sys
import time
import hashlib
import re
import shlex
from numbers import Number

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


def _legacy_templating():
    return _core_at_least(2, 10) and not _core_at_least(2, 19)


def _legacy_non_native(ctx):
    config = ctx.get("__ansiroll_controller_config") or {}
    return config.get("DEFAULT_JINJA2_NATIVE") is False and _legacy_templating()


def _vars_snapshot(ctx):
    # Ansible exposes a shallow copy of the task context under the magic
    # variable. From 2.20 onward it also exposes one nested copy under 'vars'.
    excluded = {"vars", "__ansiroll_controller_config", "__ansiroll_controller_env",
                "__ansiroll_search_dirs"}
    snapshot = {key: value for key, value in ctx.items() if key not in excluded}
    if _core_at_least(2, 20):
        snapshot["vars"] = dict(snapshot)
    return snapshot


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

class _WorkerStderr:
    """Drain diagnostics alongside request writes and response reads."""

    def __init__(self, proc):
        self.stream = proc.stderr
        self.tail = b""
        os.set_blocking(proc.stdin.fileno(), False)
        os.set_blocking(self.stream.fileno(), False)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.stream, selectors.EVENT_READ)

    def _drain(self):
        try:
            data = os.read(self.stream.fileno(), 65536)
        except BlockingIOError:
            return
        if data:
            self.tail = (self.tail + data)[-65536:]
        elif self.selector.get_map().get(self.stream.fileno()) is not None:
            self.selector.unregister(self.stream)

    def wait(self, stream, event):
        self.selector.register(stream, event)
        try:
            while True:
                ready = False
                for key, _ in self.selector.select():
                    if key.fileobj is self.stream:
                        self._drain()
                    else:
                        ready = True
                if ready:
                    return
        finally:
            self.selector.unregister(stream)

    def read(self):
        self._drain()
        return self.tail.decode("utf-8", "surrogateescape").strip()

    def close(self):
        self.selector.close()
        self.stream.close()


# __ANSIROLL_JINJA_BUNDLE__
import jinja2  # type: ignore


# Sentinel for the `omit` magic variable; args/env entries equal to it are
# dropped before the module runs (mirrors Ansible's omit placeholder).
class _Omit(str):
    pass


_OMIT = _Omit("__omit_place_holder__ansiroll")


class _UnsafeTemplate(str):
    """A string value whose template-looking contents must remain literal."""


class _UntrustedConditionValue(str):
    """A `!unsafe` value retaining its provenance during condition checks."""


class _TrustedAsTemplateValue(str):
    """A string produced by set_fact and trusted for later templating."""


class _UnsafeText(str):
    """Text that must never be templated again.

    Mirrors Ansible: module results, facts, lookup output and the output of a
    render are data, not templates (AnsibleUnsafeText before 2.19, a missing
    TrustedAsTemplate tag since).  Only strings that look like templates are
    wrapped, so ordinary values keep their plain ``str`` type."""


# Strings the templater returns verbatim.
_NO_TEMPLATE = (_UnsafeText, _UnsafeTemplate, _UntrustedConditionValue)


class _IncludedVars(dict):
    """Controller-loaded include_vars definitions carried in a task result."""


def _decode_included_var(value):
    if isinstance(value, dict):
        if set(value) == {"__ansiroll_unsafe__"}:
            return _UntrustedConditionValue(value["__ansiroll_unsafe__"])
        return {key: _decode_included_var(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_included_var(item) for item in value]
    return value


def _looks_templated(value):
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def _mark_unsafe(value, _seen=None, preserve_included_vars=False):
    """Deep-copy `value`, marking template-looking strings as never-templated."""
    if preserve_included_vars and isinstance(value, _IncludedVars):
        return value
    if isinstance(value, str):
        if _looks_templated(value) and not isinstance(value, _NO_TEMPLATE):
            return _UnsafeText(value)
        return value
    if isinstance(value, (dict, list, tuple)):
        if _seen is None:
            _seen = {}
        # Keep source containers alive because lazy views create temporary
        # wrappers whose object IDs could be reused during this traversal.
        if id(value) in _seen:
            return _seen[id(value)][1]
        if isinstance(value, dict):
            out = {}
            _seen[id(value)] = (value, out)
            # Lazy views render on access; plain containers are read raw.
            getter = (value.__getitem__ if isinstance(value, _LazyTplDict)
                      else lambda key: dict.__getitem__(value, key))
            for k in list(dict.keys(value)):
                out[_mark_unsafe(k, _seen)] = _mark_unsafe(
                    getter(k), _seen, preserve_included_vars)
            return out
        if isinstance(value, list):
            out = []
            _seen[id(value)] = (value, out)
            items = (iter(value) if isinstance(value, _LazyTplList)
                     else list.__iter__(value))
            out.extend(_mark_unsafe(v, _seen, preserve_included_vars) for v in items)
            return out
        return tuple(_mark_unsafe(v, _seen, preserve_included_vars) for v in value)
    return value


def _decode_frozen_value(value):
    """Decode a value from the compiler's frozen-vars section."""
    if isinstance(value, dict):
        if "mapping" in value:
            return {_decode_frozen_value(k): _decode_frozen_value(v)
                    for k, v in value["mapping"]}
        if "tuple" in value:
            return tuple(_decode_frozen_value(v) for v in value["tuple"])
        if "text" in value:
            return _UnsafeText(value["text"])
        if "untrusted" in value:
            return _UntrustedConditionValue(value["untrusted"])
    if isinstance(value, list):
        return [_decode_frozen_value(v) for v in value]
    return value


def _trust_set_fact_value(value):
    if isinstance(value, _NO_TEMPLATE):
        return value
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


def _strip_omit(args):
    if isinstance(args, dict):
        return {k: v for k, v in args.items() if v != _OMIT}
    return args


def _fail_on_undef(value):
    """Raise the captured error of any Undefined nested in a value."""
    if isinstance(value, dict):
        for k, v in value.items():
            _fail_on_undef(k)
            _fail_on_undef(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _fail_on_undef(v)
    elif _is_undef(value):
        value._fail_with_undefined_error()
    return value


def _debug_var_value(value):
    if not _core_at_least(2, 19):
        try:
            return _fail_on_undef(value)
        except jinja2.UndefinedError:
            return "VARIABLE IS NOT DEFINED!"

    error_count = 0

    def replace(v):
        nonlocal error_count
        if _is_undef(v):
            try:
                v._fail_with_undefined_error()
            except jinja2.UndefinedError as ex:
                error_count += 1
                return "<< error %d - %s >>" % (error_count, ex)
        if isinstance(v, dict):
            return {k: replace(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [replace(x) for x in v]
        return v

    return replace(value)


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


class TaskFailed(Exception):
    """Raised when a task fails and is not ignored (drives block/rescue)."""

    def __init__(self, name, task=None, result=None):
        super(TaskFailed, self).__init__(name)
        self.task = task or {"name": name}
        self.result = result or {"failed": True, "msg": str(name)}


class ConditionalError(Exception):
    """Evaluating a `when` conditional failed (undefined variable, error)."""


class UntrustedConditional(ConditionalError):
    """A conditional contained an embedded template and was refused."""


class _BrokenConditional(RuntimeError):
    """A conditional violated ansible-core's expression or result requirements."""


def _conditional_error_message(cond, error, keyword="when"):
    """Ansible's message for a conditional that raised while being evaluated.
    `keyword` names the failing expression (`when`, `until`); None for
    assert, whose message carries no expression prefix."""
    if isinstance(cond, dict) and len(cond) == 1:
        cond = list(cond.values())[0]
    if keyword is None or not _core_at_least(2, 21):
        prefix = "Task failed: "
    else:
        prefix = "Task failed: %s '%s' expression failed: " % (
            "An" if keyword[0] in "aeiou" else "A", keyword)
    if isinstance(error, _BrokenConditional):
        return prefix + str(error)
    if _core_at_least(2, 19):
        kind = ("Error while evaluating conditional"
                if isinstance(error, jinja2.UndefinedError)
                else "Error rendering expression")
        return "%s%s: %s" % (prefix, kind, error)
    return ("The conditional check '%s' failed. The error was: error while "
            "evaluating conditional (%s): %s" % (cond, cond, error))


class _UnsupportedLookup(Exception):
    """No built-in implementation for this lookup plugin name."""


def _ansible_boolean(value):
    """ansible.module_utils.parsing.convert_bool.boolean(value, strict=False)."""
    if isinstance(value, bool):
        return value
    normalized = value
    if isinstance(value, bytes):
        normalized = value.decode("utf-8", "surrogateescape")
    if isinstance(normalized, str):
        normalized = normalized.lower().strip()
    try:
        return normalized in ("y", "yes", "on", "1", "true", "t", 1, 1.0)
    except TypeError:
        return False


def _to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "on", "1")
    return bool(value)


_BOOLEANS_TRUE = frozenset(("y", "yes", "on", "1", "true", "t", 1))
_BOOLEANS_FALSE = frozenset(("n", "no", "off", "0", "false", "f", 0))


def _boolean(value, strict=False):
    """Keyword/action-arg coercion: ansible.module_utils.parsing.convert_bool
    .boolean, which (unlike the `bool` filter) also accepts y/t and n/f."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (str, bytes)):
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        value = value.lower().strip()
    try:
        if value in _BOOLEANS_TRUE:
            return True
        if not strict or value in _BOOLEANS_FALSE:
            return False
    except TypeError:
        if not strict:
            return False
    raise TypeError("Invalid boolean value")


def _f_combine(d, *others, **kwargs):
    recursive = kwargs.get("recursive", False)

    # Copy through items(): dict(lazy_view) bypasses rendering on Python 3.5.
    def merge(a, b):
        out = dict(a.items())
        for k, v in b.items():
            if recursive and isinstance(out.get(k), dict) and isinstance(v, dict):
                out[k] = merge(out[k], v)
            else:
                out[k] = v
        return out
    result = dict((d or {}).items())
    for o in others:
        result = merge(result, o or {})
    return result


def _f_dict2items(d, key_name="key", value_name="value"):
    return [{key_name: k, value_name: v} for k, v in (d or {}).items()]


def _f_items2dict(lst, key_name="key", value_name="value"):
    return {i[key_name]: i[value_name] for i in (lst or [])}


def _f_regex_replace(s="", pattern="", repl="", ignorecase=False,
                     multiline=False, count=0, mandatory_count=0):
    # Mirrors ansible.plugins.filter.core.regex_replace.
    import re
    flags = 0
    if ignorecase:
        flags |= re.I
    if multiline:
        flags |= re.M
    text = s if isinstance(s, str) else str(s)
    output, subs = re.compile(pattern, flags).subn(repl, text, count=count)
    if mandatory_count and mandatory_count != subs:
        raise ValueError("'%s' should match %d times, but matches %d times "
                         "in '%s'" % (pattern, mandatory_count, count, s))
    return output


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
    # Ansible uses plain Python truthiness here, not boolean coercion.
    return true_val if condition else false_val


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
    "type_debug": lambda v: _type_debug(v),
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


def _test_succeeded(result):
    return not _as_result(result).get("failed", False)


# A subset of Ansible's Jinja2 tests (pure stdlib).
_ANSIBLE_TESTS = {
    "version": _test_version,
    "version_compare": _test_version,
    "match": _test_match,
    "search": _test_search,
    "regex": lambda v, pattern, **k: _test_search(v, pattern),
    "succeeded": _test_succeeded,
    "success": _test_succeeded,
    "successful": _test_succeeded,
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
    if isinstance(value, (str, tuple)):
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

    def __iter__(self):
        # Overriding __iter__ makes dict(view) and {**view} read through
        # __getitem__ instead of the raw storage on Python 3.6+ (Ansible's
        # _AnsibleLazyTemplateDict does the same).
        return dict.__iter__(self)

    def __repr__(self):
        # str()/repr() inside a template shows the rendered values.
        return repr(self.copy())

    __str__ = __repr__

    def __eq__(self, other):
        mine = {k: self[k] for k in dict.keys(self)}
        return mine == (other.copy() if isinstance(other, _LazyTplDict) else other)

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = None


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

    # list's C-level operations read the raw storage; render first, like
    # Ansible's _AnsibleLazyTemplateList.
    def __contains__(self, item):
        return item in list(iter(self))

    def __reversed__(self):
        return reversed(list(iter(self)))

    def __add__(self, other):
        if isinstance(other, _LazyTplList):
            other = list(iter(other))
        return list.__add__(list(iter(self)), other)

    def __radd__(self, other):
        if not isinstance(other, list):
            return NotImplemented
        return list(other) + list(iter(self))

    def __mul__(self, other):
        return list.__mul__(list(iter(self)), other)

    __rmul__ = __mul__

    def index(self, *args):
        return list(iter(self)).index(*args)

    def count(self, item):
        return list(iter(self)).count(item)

    def __repr__(self):
        return repr(list(iter(self)))

    __str__ = __repr__

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = None


def _jinja_dict(*args, **kwargs):
    # Python 3.5's dict(view) may copy a dict subclass's raw storage.
    if args and isinstance(args[0], _LazyTplDict):
        args = (args[0].items(),) + args[1:]
    return dict(*args, **kwargs)


def _type_name(value):
    """type_debug: internal wrappers report the builtin type they stand for."""
    for internal, name in ((_LazyTplDict, "dict"), (_LazyTplList, "list")):
        if isinstance(value, internal):
            return name
    if type(value) in (_UnsafeText, _UnsafeTemplate, _UntrustedConditionValue,
                       _TrustedAsTemplateValue):
        return "str"
    return type(value).__name__


def _type_debug(value):
    if (isinstance(value, (_UnsafeTemplate, _UntrustedConditionValue))
            and _legacy_templating()):
        return "AnsibleUnsafeText"
    return _type_name(value)


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
    """Ansible's undefined: attribute/item access chains (keeping the first
    failure), but using the value -- str(), iteration, truth, comparison,
    `x in undefined` -- raises, like StrictUndefined (AnsibleUndefined)."""

    __str__ = __iter__ = __len__ = __eq__ = __ne__ = __bool__ = __hash__ = \
        jinja2.Undefined._fail_with_undefined_error
    __nonzero__ = __bool__

    def __init__(self, hint=None, obj=jinja2.utils.missing, name=None,
                 exc=jinja2.exceptions.UndefinedError):
        if hint is None and obj is not jinja2.utils.missing:
            # Name the builtin type, never an internal wrapper class.
            type_name = _type_name(obj)
            if _core_at_least(2, 19):
                hint = "object of type %r has no attribute %r" % (type_name, name)
            elif isinstance(name, str):
                hint = "'%s object' has no attribute %r" % (type_name, name)
            else:
                hint = "%s object has no element %r" % (type_name, name)
        jinja2.ChainableUndefined.__init__(self, hint, obj, name, exc)

    def __html__(self):
        self._fail_with_undefined_error()

    def __contains__(self, item):
        self._fail_with_undefined_error()


class _AnsirollContext(jinja2.runtime.Context):
    """Template variables are templated lazily when a template looks them up.

    Like Ansible's AnsibleJ2Vars (<= 2.18) and lazy templating (2.19+), a
    trusted variable value that is itself a template renders on access; data
    marked as never-templated (results, facts, rendered output) is returned
    verbatim.  Names bound inside the template itself are never touched."""

    def resolve_or_missing(self, key):
        if key in self.vars:
            return self.vars[key]
        if key not in self.parent:
            return jinja2.runtime.missing
        value = self.parent[key]
        templater = getattr(self.environment, "_ansiroll_templater", None)
        if templater is None or str(key).startswith("__ansiroll"):
            return value
        return templater.resolve_variable(key, value, self.parent)


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
        self.env.context_class = _AnsirollContext
        self.env._ansiroll_templater = self
        self._resolving = []        # variable templates being rendered (cycles)
        self._runtime = None        # bound by Runtime for lazy containers
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
        self.env.globals["dict"] = _jinja_dict

    def _lookup_ctx(self, name, terms):
        """The vars lookup templates trusted values like a variable reference."""
        ctx = self._current_ctx
        if str(name).rsplit(".", 1)[-1] != "vars" or not isinstance(ctx, dict):
            return ctx
        view = dict(ctx)
        for term in terms:
            key = str(term)
            if key in ctx and not key.startswith("__ansiroll"):
                view[key] = self.resolve_variable(key, ctx[key], ctx)
        return view

    def _g_lookup(self, name, *a, **k):
        try:
            a = tuple(self._template_lookup_arg(v) for v in a)
            k = {key: self._template_lookup_arg(value) for key, value in k.items()}
            # Lookup output is data and is never templated again.
            return _mark_unsafe(
                _lookup(name, *a, _ansiroll_ctx=self._lookup_ctx(name, a), **k))
        except _UnsupportedLookup:
            vals = self._plugin_lookup(name, a, k)
            if _is_undef(vals):
                return vals
            vals = _mark_unsafe(vals)
            if k.pop("wantlist", False):
                return vals
            return vals[0] if len(vals) == 1 else ",".join(str(v) for v in vals)
        except Exception as e:
            return _captured(str(e))

    def _g_query(self, name, *a, **k):
        try:
            a = tuple(self._template_lookup_arg(v) for v in a)
            k = {key: self._template_lookup_arg(value) for key, value in k.items()}
            return _mark_unsafe(
                _query(name, *a, _ansiroll_ctx=self._lookup_ctx(name, a), **k))
        except _UnsupportedLookup:
            vals = self._plugin_lookup(name, a, k)
            return vals if _is_undef(vals) else _mark_unsafe(vals)
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

    def resolve_variable(self, key, value, ctx):
        """Template a variable's trusted value when a template references it."""
        if type(value) is str and ("{{" in value or "{%" in value):
            marker = (key, value)
            if marker in self._resolving:
                return _captured("Recursive loop detected in template: %s" % value)
            self._resolving.append(marker)
            try:
                return self.render_string(value, ctx)
            finally:
                self._resolving.pop()
        if self._runtime is not None and type(value) in (dict, list):
            return _lazy_wrap(value, self._runtime, ctx)
        return value

    def render_string(self, text, ctx, _depth=0):
        if not isinstance(text, str) or isinstance(text, _NO_TEMPLATE):
            return text
        if "{{" not in text and "{%" not in text:
            return text
        previous = self._current_ctx
        self._current_ctx = ctx
        try:
            result = self._render_string(text, ctx)
        finally:
            # A nested render (a lazily templated variable, a lookup term)
            # must not leave its context behind for the outer template.
            self._current_ctx = previous
        if (isinstance(result, Number) and not isinstance(result, bool)
                and _legacy_non_native(ctx)):
            # Legacy non-native concatenation stringifies numbers unless the
            # whole template names a variable whose raw value is a number.
            single = re.match(r"^\{\{\s*(\w+)\s*\}\}$", text)
            if single is None or not isinstance(ctx.get(single.group(1)), Number):
                return str(result)
        if isinstance(result, _UntrustedConditionValue):
            return result
        if isinstance(result, str):
            # Rendered output is data: like Ansible, it is never templated
            # again (trusted variable values are templated when looked up).
            return (_UnsafeText(result) if _looks_templated(result)
                    else str(result) if isinstance(result, _UnsafeTemplate)
                    else result)
        return result

    def _render_string(self, text, ctx):
        stripped = text.strip()
        # A bare "{{ expr }}" returns the native object (list/dict/int/...).
        lookup_expr = re.match(r"^\{\{\s*(?:lookup|query|q)\s*\(", stripped)
        if (stripped.startswith("{{") and stripped.endswith("}}")
                and (stripped.count("{{") == 1 or lookup_expr)
                and "{%" not in stripped):
            expr = stripped[2:-2].strip()
            try:
                compiled = self._expr_cache.get(expr)
                if compiled is None:
                    compiled = self.env.compile_expression(expr, undefined_to_none=False)
                    self._expr_cache[expr] = compiled
                result = compiled(**ctx)
            except jinja2.TemplateSyntaxError as e:
                # Captured, not raised: surfaces when the value is used (or is
                # inspected via dump_object), like Ansible 2.19.
                return _captured_jinja_error(e, syntax=True)
            except Exception:
                try:
                    tmpl = self._template_cache.get(text)
                    if tmpl is None:
                        tmpl = self.env.from_string(text)
                        self._template_cache[text] = tmpl
                    result = tmpl.render(**ctx)
                except jinja2.TemplateError as e:
                    return _captured_jinja_error(
                        e, syntax=isinstance(e, jinja2.TemplateSyntaxError)
                    )
        else:
            try:
                tmpl = self._template_cache.get(text)
                if tmpl is None:
                    tmpl = self.env.from_string(text)
                    self._template_cache[text] = tmpl
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
        return result

    def evaluate(self, expr, ctx):
        """Evaluate a single boolean-ish expression (used for when/changed_when)."""
        if isinstance(expr, bool):
            return expr
        expr = str(expr)
        previous = self._current_ctx
        self._current_ctx = ctx
        try:
            compiled = self._expr_cache.get(expr)
            if compiled is None:
                compiled = self.env.compile_expression(expr, undefined_to_none=False)
                self._expr_cache[expr] = compiled
            return compiled(**ctx)
        finally:
            self._current_ctx = previous


def _is_undef(v):
    return isinstance(v, jinja2.Undefined)


def _as_text(v):
    """Coerce a rendered value to text for writing to a file."""
    if isinstance(v, str):
        return v
    if _is_undef(v):
        # A captured render error or undefined value fails, never writes "".
        v._fail_with_undefined_error()
    if v is None:
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
        self._play_var_templates = {}
        self._playbook_dir = None
        self._runtime_var_overrides = set(self.extra_vars)
        # The controller passes its nonpersistent fact cache separately from
        # the flattened host snapshot. Those values (set_fact/register) outrank
        # play vars, a distinction that is otherwise lost after serialization.
        self._set_fact_overrides = dict(host_var_overrides or {})
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
        self.templater = _Templater()
        self.templater._runtime = self
        self.handlers = []
        # Hand-built runtime nodes can supply block conditions independently;
        # parsed tasks already carry the accumulated conditions from Ansible.
        self._inherited_when = []
        self.notified = []  # ordered, de-duplicated handler names
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
        if "hostvars" not in d:
            d["hostvars"] = {self.host: dict(d)}
        if "vars" not in d:
            d["vars"] = _vars_snapshot(d)
        return d

    def _store_register(self, task, result):
        name = task.get("register")
        if name:
            include_vars = task.get("action", "").rsplit(".", 1)[-1] == "include_vars"
            self.vars[name] = _mark_unsafe(result, preserve_included_vars=include_vars)
            self._untrusted_runtime_vars.add(name)

    def template(self, value, ctx, _seen=None):
        if isinstance(value, (dict, list, tuple)):
            if _seen is None:
                _seen = set()
            if id(value) in _seen:  # cycles (e.g. hostvars references the ctx)
                return value
            _seen.add(id(value))
        if isinstance(value, dict):
            if set(value.keys()) == {"__ansiroll_unsafe__"}:
                return _UntrustedConditionValue(
                    value["__ansiroll_unsafe__"]
                )  # `!unsafe`: never templated
            if set(value.keys()) == {"__ansiroll_vault__"}:
                return value
            # Ansible leaves mapping keys literal.
            return {_mark_unsafe(k): self.template(v, ctx, _seen)
                    for k, v in value.items()}
        if isinstance(value, list):
            return [self.template(v, ctx, _seen) for v in value]
        if isinstance(value, tuple):
            return tuple(self.template(v, ctx, _seen) for v in value)
        if isinstance(value, str):
            out = self.templater.render_string(value, ctx)
            if isinstance(out, (dict, list, tuple)) and out is not value:
                # A bare expression returned a container that may hold lazy
                # variable views -- resolve them now that it is used.  The
                # result is data and is never templated again.
                out = _mark_unsafe(out)
            return out
        return value

    def template_vars(self, d, ctx, dynamic=False):
        """Template a vars mapping lazily: top-level strings render now (with
        sibling vars visible), but containers become lazy wrappers that render
        values only on access. Legacy non-native templating also keeps scalar
        sources so its raw single-variable type check sees the original value.

        Dynamic play vars may legitimately refer to variables that are only
        available to a later, tag-selected task.  Preserve those expressions
        until the task context refreshes them instead of failing while merely
        entering a play with no executable tasks.
        """
        d = d or {}
        base = dict(ctx)
        base.update(d)
        out = {}
        legacy = _legacy_non_native(base)
        for k, v in d.items():
            if isinstance(v, str) and not legacy:
                try:
                    out[k] = self.templater.render_string(v, base)
                except Exception:
                    if not dynamic:
                        raise
                    out[k] = v
            elif isinstance(v, dict) and set(v.keys()) == {"__ansiroll_unsafe__"}:
                out[k] = _UntrustedConditionValue(v["__ansiroll_unsafe__"])
            else:
                out[k] = _lazy_container(v, self, base, dynamic)
        return out

    def when(self, conds, ctx, strict=False, keyword="when"):
        """All conditions true?  An evaluation error raises: the raw error in
        strict mode (changed_when/failed_when), else ConditionalError, which
        fails the task like Ansible instead of silently skipping it."""
        for cond in (conds or []):
            try:
                if not self._condition(cond, ctx, strict, keyword):
                    return False
            except ConditionalError:
                raise
            except Exception as ex:
                if strict:
                    raise
                raise ConditionalError(_conditional_error_message(cond, ex, keyword))
        return True

    def _condition(self, cond, ctx, strict=False, keyword="when"):
        modern = _core_at_least(2, 19)
        config = ctx.get("__ansiroll_controller_config") or {}
        allow_broken = modern and _to_bool(
            config.get("ALLOW_BROKEN_CONDITIONALS", False))
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
            result = self.templater.render_string(cond, ctx)
            # Assert accepts an indirected list of expressions (for
            # example `that: "{{ expression_list }}"`). Evaluate the
            # resolved members as conditions rather than treating the
            # non-empty list itself as truthy.
            if (keyword is None or not modern) and isinstance(result, (list, tuple)):
                return self.when(list(result), ctx, strict=strict,
                                 keyword=keyword)
            if modern and (result is None or
                           isinstance(result, str) and not result.strip()):
                if allow_broken:
                    return True
                raise _BrokenConditional("Empty conditional expressions are not allowed.")
            if isinstance(result, (_UnsafeTemplate, _UntrustedConditionValue)):
                raise UntrustedConditional(
                    "Encountered untrusted template or expression."
                )
            if isinstance(result, str):
                whole_template = re.match(r"^\s*\{\{.*\}\}\s*$", cond, re.S)
                boolean_text = result.strip().lower() in ("true", "false")
                if whole_template and (modern or not boolean_text):
                    raise UntrustedConditional(
                        "Encountered untrusted template or expression."
                    )
                if boolean_text:
                    result = _to_bool(result)
                else:
                    result = self.templater.evaluate(result, ctx)
            if strict and _is_undef(result):
                raise RuntimeError(
                    "The conditional check %r failed: the expression is undefined" % cond)
            return self._condition_truth(result, modern, allow_broken)
        if cond is None or (isinstance(cond, str) and not cond.strip()):
            if cond is not None and modern and not allow_broken:
                raise _BrokenConditional("Empty conditional expressions are not allowed.")
            return True
        if not isinstance(cond, (bool, str)) and modern:
            if not allow_broken:
                raise _BrokenConditional("Conditional expressions must be strings.")
            return bool(_fail_on_undef(self.template(cond, ctx)))
        result = self.templater.evaluate(cond, ctx)
        if strict and _is_undef(result):
            raise RuntimeError(
                "The conditional check %r failed: the expression is undefined" % cond)
        return self._condition_truth(result, modern, allow_broken)

    def _condition_truth(self, result, modern, allow_broken):
        if isinstance(result, bool):
            return result
        if modern and not _is_undef(result):
            if allow_broken:
                return bool(result)
            raise _BrokenConditional(
                "Conditional result (%s) was derived from value of type '%s'. "
                "Conditionals must have a boolean result."
                % (bool(result), _type_name(result))
            )
        return bool(result)

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
            env = jinja2.Environment(undefined=jinja2.StrictUndefined)
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
            if (not is_item and not node.get("no_log") and node.get("register")
                    and node.get("action", "").rsplit(".", 1)[-1] == "include_vars"):
                entries = result["results"] if "results" in result else [result]
                references = {}
                for index, entry in enumerate(entries):
                    facts = entry.get("ansible_facts")
                    reference = getattr(facts, "controller_reference", None)
                    if isinstance(facts, _IncludedVars) and reference is not None:
                        references[str(index)] = reference
                if references:
                    frame["included_vars_references"] = references
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
        self._playbook_dir = play.get("playbook_dir")
        if self._playbook_dir:
            self.vars["playbook_dir"] = self._playbook_dir
        play_check_mode = play.get("check_mode")
        if play_check_mode is not None:
            if isinstance(play_check_mode, str):
                play_check_mode = self.template(play_check_mode, self.ctx())
            self.check_mode = _boolean(play_check_mode)
        if play.get("jinja_plugins"):
            plugins = []
            for plugin in play["jinja_plugins"]:
                plugin = dict(plugin)
                plugin["source"] = self._asset_text(plugin.get("source"))
                plugins.append(plugin)
            self.templater.register_plugins(plugins)
        self.handlers = play.get("handlers", [])
        self._play_become = play.get("become", False)
        if initial_notified:
            # Carry notifications from earlier segments (see the strategy).
            for name in initial_notified:
                if name not in self.notified:
                    self.notified.append(name)
        if play.get("gather_facts"):
            self.gather_facts(
                play.get("environment"),
                become=bool(play.get("become")),
                become_user=play.get("become_user"),
                fact_schema=play.get("fact_schema"),
            )
        # Play vars (templated after facts are available; containers lazily).
        self.vars["__ansiroll_controller_env"] = play.get("controller_env") or {}
        self.vars["__ansiroll_controller_config"] = (
            play.get("controller_config") or {}
        )
        self._runtime_var_overrides = (
            set(self.extra_vars) | set(self._set_fact_overrides)
        )
        self._play_var_templates = {}
        if not _legacy_non_native(self.vars):
            self._play_var_templates = {
                key: value for key, value in (play.get("vars") or {}).items()
                if _looks_templated(value)
            }
        self.vars.update(self.template_vars(
            play.get("vars"), self.ctx(), dynamic=True))
        # Host variables created by set_fact have higher precedence than play
        # vars and retain that precedence for later plays in the same run.
        self.vars.update(self._set_fact_overrides)
        # Extra-vars have the highest Ansible precedence; play vars must not
        # overwrite values injected via -e.
        self.vars.update(self.extra_vars)
        if self._playbook_dir:
            self.vars["playbook_dir"] = self._playbook_dir
        try:
            self.run_nodes(play.get("tasks", []), {}, play.get("become", False))
        except TaskFailed as ex:
            # An uncaught failure ends the play for this host.
            if not getattr(ex, "counted", False):
                self._tally("failures")
            self.failed = True
        # Defer the flush when this is a non-final segment; the final segment runs
        # handlers with the accumulated notifications.
        if not defer_handlers:
            self.flush_handlers()
        self.check_mode = invocation_check_mode

    def run_nodes(self, nodes, overlay, become=False):
        # Each suspended generator resumes after its child sequence. Advancing
        # one at a time keeps dynamic include and block depth off the call stack.
        stack = [self._node_steps(nodes, overlay, become)]
        failure = None
        while stack:
            frame = stack[-1]
            try:
                if failure is None:
                    child = next(frame)
                else:
                    # Resume at the yield so enclosing blocks can handle failure.
                    child = frame.throw(failure)
            except StopIteration:
                stack.pop()
                failure = None
            except TaskFailed as ex:
                stack.pop()
                failure = ex
            else:
                stack.append(child)
                failure = None
        if failure is not None:
            raise failure

    def _node_steps(self, nodes, overlay, become):
        for node in nodes:
            kind = node.get("type")
            if kind == "block":
                yield from self._block_steps(node, overlay, become)
            elif kind == "include":
                yield from self._include_steps(node, overlay, become)
            else:
                self.run_task(node, overlay, become)

    def _when_of(self, node):
        own = node.get("when") or []
        if own[:len(self._inherited_when)] == self._inherited_when:
            return own
        return self._inherited_when + list(own)

    def _include_steps(self, node, overlay, become):
        """Run a compile-time-expanded include_tasks or include_role, once per loop item if looped."""
        base_ctx = self._task_ctx(node, overlay)
        self.emit_start(node)
        b = bool(node.get("become")) or become
        targets = self.__dict__.setdefault("_include_targets", {})
        if node.get("include_id"):
            targets[node["include_id"]] = node.get("tasks", [])
        tasks = node.get("tasks", [])
        if node.get("tasks_ref"):
            # Recursive include: run the enclosing include's expansion again.
            tasks = targets.get(node["tasks_ref"], tasks)
        items, result, ignore = self._resolve_task_loop(node, base_ctx)
        if items is not None and not items:
            result = {"changed": False, "skipped": True, "results": [],
                      "msg": "No items in the list"}
        if result is None:
            result, ignore = self._task_gate(node, base_ctx)
        if result is not None:
            self._store_register(node, result)
            self.emit(node, _status_of(result), result, ignored=ignore)
            if result.get("skipped"):
                self._tally("skipped")
            elif ignore:
                self._tally("ok")
                self._tally("ignored")
            else:
                raise TaskFailed(node.get("name"), node, result)
            return
        scope = dict(overlay)
        scope.update(self.template_vars(node.get("vars"), base_ctx))
        tally_ok = node.get("tally_include_ok", True)
        if items is None:
            self.emit(node, "ok", {"changed": False, "include": node.get("file")})
            if tally_ok:
                self._tally("ok")
            yield self._node_steps(tasks, scope, b)
            return
        for i, it in enumerate(items):
            item_scope = dict(scope)
            item_scope.update(self._loop_item_vars(node, items, i, base_ctx))
            self.emit(node, "ok", {"changed": False, "include": node.get("file"),
                                   "item": it}, item=it, is_item=True)
            if tally_ok:
                self._tally("ok")
            yield self._node_steps(tasks, item_scope, b)

    def _block_steps(self, block, overlay, become):
        scope = dict(overlay)
        scope.update(self.template_vars(block.get("vars"), self.ctx(overlay)))
        # Frames run strictly nested, so the block's conditions stay in force while its children are on the stack.
        inherited = self._inherited_when
        self._inherited_when = self._when_of(block)
        try:
            yield from self._block_body_steps(block, scope, become)
        finally:
            self._inherited_when = inherited

    def _block_body_steps(self, block, scope, become):
        b_become = bool(block.get("become")) or become
        failed_exc = None
        try:
            try:
                yield self._node_steps(block.get("block", []), scope, b_become)
            except TaskFailed as ex:
                rescue = block.get("rescue") or []
                if not rescue:
                    raise
                self._tally("rescued")
                scope["ansible_failed_task"] = ex.task
                scope["ansible_failed_result"] = ex.result
                yield self._node_steps(rescue, scope, b_become)
        except TaskFailed as ex:
            failed_exc = ex
        if block.get("always"):
            try:
                yield self._node_steps(block["always"], scope, b_become)
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

    def _task_ctx(self, task, overlay):
        # Precedence (low -> high): role defaults < play/host/facts/registers
        # < role vars < block/parent overlay < task vars < loop item.
        self._refresh_play_vars()
        ctx = {}
        ctx.update(task.get("role_defaults") or {})
        ctx.update(self.vars)
        ctx.update(task.get("role_vars") or {})
        ctx.update(overlay)
        ctx.update(self.template_vars(task.get("vars"), ctx))
        if task.get("role_magic"):
            ctx.update(task["role_magic"])
        if self._playbook_dir:
            ctx["playbook_dir"] = self._playbook_dir
        ctx.setdefault("omit", _OMIT)
        if task.get("search_dirs"):
            ctx["__ansiroll_search_dirs"] = task["search_dirs"]
        if task.get("search_path"):
            ctx.setdefault("ansible_search_path", task["search_path"])
        hostvars = ctx.get("hostvars")
        # The controller snapshot predates include_vars/set_fact/register
        # changes made by this long-lived agent. Refresh this host's entry
        # for every task while preserving other hosts from the snapshot.
        hostvars = dict(hostvars) if isinstance(hostvars, dict) else {}
        current = dict(hostvars.get(self.host) or {})
        current.update({
            key: value for key, value in ctx.items()
            if key not in ("hostvars", "vars")
        })
        hostvars[self.host] = current
        ctx["hostvars"] = hostvars
        if "vars" not in ctx:
            ctx["vars"] = _vars_snapshot(ctx)
        return ctx

    def _refresh_play_vars(self):
        """Re-evaluate lazy play vars against facts produced by earlier tasks."""
        if not self._play_var_templates:
            return
        for _ in range(3):
            ctx = dict(self.vars)
            if self._playbook_dir:
                ctx["playbook_dir"] = self._playbook_dir
            changed = False
            for key, raw in self._play_var_templates.items():
                if key in self._runtime_var_overrides:
                    continue
                rendered = self.templater.render_string(raw, ctx)
                try:
                    differs = key not in self.vars or self.vars[key] != rendered
                except Exception:
                    differs = True
                if differs:
                    self.vars[key] = rendered
                    changed = True
                ctx[key] = rendered
            if not changed:
                break

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
        special = task.get("special")
        if special in ("meta_flush_handlers", "meta_clear_host_errors"):
            conditions = self._when_of(task)
            if conditions:
                try:
                    if not self.when(conditions, self._task_ctx(task, overlay)):
                        return
                except ConditionalError as ex:
                    result = {"failed": True, "changed": False, "msg": str(ex)}
                    self.emit_start(task)
                    self.emit(task, "failed", result)
                    raise TaskFailed(task.get("name"), task, result)
            if special == "meta_flush_handlers":
                self.emit_start(task)
                inherited = self._inherited_when
                self._inherited_when = []
                try:
                    self.flush_handlers()
                finally:
                    self._inherited_when = inherited
            else:
                self._errors_cleared = True
            return
        effective_become = bool(task.get("become")) or become
        base_ctx = self._task_ctx(task, overlay)
        run_once = task.get("run_once")
        if self.skip_run_once and run_once is not None:
            try:
                if isinstance(run_once, str):
                    run_once = self.template(run_once, base_ctx)
                if _boolean(run_once):
                    # Ansible emits no skipped event for hosts which do not
                    # execute a run_once task.
                    return
            except Exception:
                # Normal task execution will report invalid templating.
                pass
        self.emit_start(task)

        if task.get("loop") is not None:
            items, res, ignore = self._resolve_task_loop(task, base_ctx)
            if res is None:
                try:
                    res, ignore = self._run_loop(task, items, base_ctx, effective_become)
                except TaskFailed:
                    raise
                except Exception as ex:
                    res = {"failed": True, "changed": False, "msg": str(ex)}
                    ignore = self._ignore_before_validation(task)
        else:
            res, ignore = self._task_gate(task, base_ctx)
            if res is None:
                res = self._exec_with_retries(task, base_ctx, effective_become)
                self._absorb_facts(task, res)
        if task.get("register"):
            res.setdefault("failed", False)
            res.setdefault("changed", False)
            self._store_register(task, res)

        tally_changed = res.pop("_ansiroll_tally_changed", res.get("changed", False))
        status = _status_of(res)
        self.emit(task, status, res, ignored=ignore)

        if res.get("failed"):
            if ignore:
                self._tally("changed" if tally_changed else "ok")
                self._tally("ignored")
            else:
                raise TaskFailed(task.get("name"), task, dict(res))
        elif res.get("skipped"):
            self._tally("skipped")
        else:
            self._tally("changed" if res.get("changed") else "ok")
            self._queue_notify(task, res)

    def _conditional_skip(self):
        return {"changed": False, "skipped": True,
                "skip_reason": "Conditional result was False"}

    def _ignore_before_validation(self, task):
        ignore = task.get("ignore_errors")
        # Older executors pass through the inherited keyword on a validation
        # error. In 2.21+ only a literal bool can take effect this early.
        return ignore is True if _core_at_least(2, 21) else bool(ignore)

    def _task_gate(self, task, ctx):
        """Evaluate the task condition, then resolve its ignore_errors keyword."""
        ignore = task.get("ignore_errors")
        try:
            if not self.when(self._when_of(task), ctx):
                return self._conditional_skip(), self._ignore_before_validation(task)
        except ConditionalError as ex:
            result = {"failed": True, "changed": False, "msg": str(ex)}
            return result, self._ignore_before_validation(task)
        if ignore is None or isinstance(ignore, bool):
            return None, bool(ignore)
        try:
            value = _fail_on_undef(self.template(ignore, ctx))
            if isinstance(value, _Omit):
                value = task.get("ignore_errors_on_omit", False)
                if not _core_at_least(2, 19):
                    # These executors restore context and skip boolean validation.
                    return None, bool(value)
        except jinja2.UndefinedError as ex:
            detail = str(ex)
        except Exception as ex:
            detail = "Error rendering template: %s" % ex
        else:
            try:
                return None, _boolean(value, strict=True)
            except TypeError:
                detail = "The value %r could not be converted to 'bool'." % value
        if _core_at_least(2, 19):
            message = "Task failed: Error processing keyword 'ignore_errors': " + detail
        else:
            message = "The field 'ignore_errors' has an invalid value: " + detail
        result = {"failed": True, "changed": False, "msg": message}
        return result, self._ignore_before_validation(task)

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

    def _run_loop(self, task, items, base_ctx, become=False):
        agg = {"results": [], "changed": False, "failed": False, "msg": "All items completed"}
        ignore_loop = False
        failed_items_ignored = True
        for i, it in enumerate(items):
            ctx = dict(base_ctx)
            ctx.update(self._loop_item_vars(task, items, i, base_ctx))
            if i > 0 and task.get("loop_pause"):
                time.sleep(float(self.template(task["loop_pause"], ctx) or 0))
            res, ignore = self._task_gate(task, ctx)
            if res is None:
                res = self._exec_with_retries(task, ctx, become)
                self._absorb_facts(task, res)
                facts = res.get("ansible_facts")
                if isinstance(facts, _IncludedVars):
                    base_ctx.update({key: self.vars[key] for key in facts})
                elif isinstance(facts, dict):
                    base_ctx.update(facts)
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
            stop = False
            bw = task.get("loop_break_when")
            if bw:
                bctx = dict(ctx)
                if task.get("register"):
                    bctx[task["register"]] = res
                try:
                    stop = self.when(bw, bctx, keyword="break_when")
                except ConditionalError as ex:
                    if not _core_at_least(2, 21):
                        # Older executors emitted the item before evaluating
                        # break_when, then returned the task-level error.
                        self.emit(task, _status_of(res), res, item=it,
                                  ignored=ignore, is_item=True)
                        return {"failed": True, "changed": False, "msg": str(ex)}, ignore
                    res["failed"] = True
                    res["break_when_result"] = str(ex)
                    stop = True
            tally_changed = res.pop(
                "_ansiroll_tally_changed", res.get("changed", False)
            )
            agg["results"].append(res)
            agg["changed"] = agg["changed"] or res.get("changed", False)
            if tally_changed:
                agg["_ansiroll_tally_changed"] = True
            if res.get("failed"):
                agg["failed"] = True
                failed_items_ignored = failed_items_ignored and ignore
            ignore_loop = ignore_loop or ignore
            self.emit(task, _status_of(res), res, item=it, ignored=ignore, is_item=True)
            if stop:
                break
        if not items:
            agg["skipped"] = True
            agg["msg"] = "No items in the list"
        elif all(res.get("skipped") for res in agg["results"]):
            agg["skipped"] = True
            agg["msg"] = "All items skipped"
        elif agg["failed"]:
            agg["msg"] = "One or more items failed"
        return agg, ignore_loop if _core_at_least(2, 21) else failed_items_ignored

    def _resolve_task_loop(self, task, ctx):
        loop = task.get("loop")
        if loop is None:
            return None, None, False
        try:
            return self._resolve_loop(loop, ctx), None, False
        except jinja2.UndefinedError as ex:
            # Only undefined loop values are deferred until the task's
            # condition has had a chance to skip it.
            try:
                skipped = not self.when(self._when_of(task), ctx)
            except ConditionalError:
                skipped = False
            if skipped:
                result = self._conditional_skip()
            else:
                message = "Task failed: %s" % ex if _core_at_least(2, 19) else str(ex)
                result = {"failed": True, "changed": False, "msg": message}
        except Exception as ex:
            result = {"failed": True, "changed": False, "msg": str(ex)}
        return None, result, self._ignore_before_validation(task)

    def _resolve_loop(self, loop, ctx):
        items = _mark_unsafe(self._resolve_loop_items(loop, ctx))
        if not isinstance(loop, list) or not _legacy_templating():
            return items
        # Legacy core can template an inline item again when the task reads it.
        # Limit this to output authored entirely as literals in the playbook,
        # so values flowing from vars, lookups or filters stay data.
        for i, source in enumerate(loop):
            if type(source) is not str or type(items[i]) is not _UnsafeText:
                continue
            parsed = self.templater.env.parse(source)
            if all(isinstance(node, jinja2.nodes.Output) and
                   all(isinstance(part, (jinja2.nodes.Const, jinja2.nodes.TemplateData))
                       for part in node.nodes)
                   for node in parsed.body):
                items[i] = str(items[i])
        return items

    def _resolve_loop_items(self, loop, ctx):
        if isinstance(loop, dict) and "__ansiroll_lookup" in loop:
            name = loop["__ansiroll_lookup"]
            if name == "sequence":
                return self._resolve_sequence_loop(loop.get("terms"), ctx)
            # Defer undefined lookup terms to the task condition, before a
            # lookup can turn them into loop items or different errors.
            terms = _fail_on_undef(self.template(loop.get("terms"), ctx))
            if not isinstance(terms, list):
                terms = [terms]
            try:
                return _loop_lookup(name, terms)
            except _UnsupportedLookup:
                vals = self.templater._plugin_lookup(name, terms, {})
                if _is_undef(vals):
                    vals._fail_with_undefined_error()
                return vals
        items = _fail_on_undef(self.template(loop, ctx))
        if not isinstance(items, list):
            if _core_at_least(2, 19):
                message = "The `loop` value must resolve to a 'list', not %r." % _type_name(items)
            else:
                message = "Invalid data passed to 'loop', it requires a list, got this instead: %s" % items
            raise ValueError(message)
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
            rendered = self.template(term, ctx)
            out.extend(_sequence_items(str(rendered)))
        return out

    def _exec_with_retries(self, task, ctx, become=False):
        until = task.get("until")
        retries = int(self.template(task.get("retries"), ctx) or 0)
        if not until:
            return self._exec_one(task, ctx, become)

        attempts = retries + 1
        delay = float(self.template(task.get("delay"), ctx) or 0)
        last = None
        eval_ctx = dict(ctx)
        for attempt in range(1, attempts + 1):
            res = self._exec_one(task, eval_ctx, become)
            res["attempts"] = attempt
            if task.get("register"):
                eval_ctx[task["register"]] = res
            last = res
            try:
                done = self.when(until, eval_ctx, keyword="until")
            except ConditionalError as ex:
                # Ansible replaces the module result with the error.
                return {"failed": True, "changed": False, "msg": str(ex)}
            if done:
                return res
            if attempt < attempts and delay > 0:
                time.sleep(delay)
        # `until` never came true: the task fails (like Ansible's retry logic),
        # but failed_when/changed_when get the last word on the final result
        # (which now carries `attempts`).
        if last is None:
            last = {"changed": False, "attempts": attempts}
        last["failed"] = True
        if task.get("failed_when") is not None or task.get("changed_when") is not None:
            self._apply_overrides(task, last, eval_ctx)
        return last

    def _exec_one(self, task, ctx, become=False):
        task_started = time.monotonic()
        inherited_check_mode = self.check_mode
        inherited_become_user = self.become_user
        provenance = {"mode": "native-agent"}
        refreshed_vars = None
        included_vars = None
        included_vars_reference = None
        try:
            check_override = task.get("check_mode")
            if check_override is not None:
                if isinstance(check_override, str):
                    check_override = self.template(check_override, ctx)
                self.check_mode = _boolean(check_override)
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
                    action = task["action"].rsplit(".", 1)[-1]
                    if action == "group_by":
                        # Preserve trust only for controller inventory metadata.
                        refreshed_vars = res.pop("_ansiroll_vars", None)
                    elif action == "include_vars":
                        payload = res.pop("_ansiroll_included_vars", None)
                        included_vars_reference = res.pop("_ansiroll_included_vars_reference", None)
                        if payload is not None:
                            included_vars = json.loads(payload)
            else:
                provenance = {"mode": "real-ansiballz"}
                args = _fail_on_undef(_strip_omit(self.template(task.get("args") or {}, ctx)))
                self._module_source_bytes = self._baked_module_source(task, args)
                if task.get("async_val") and int(task.get("poll") or 0) == 0:
                    res = self._run_async_module(
                        task["action"], args, become=become,
                        async_val=int(task.get("async_val") or 0), ctx=ctx,
                        options=task.get("async_options"))
                elif task.get("action") in ("async_status", "ansible.builtin.async_status",
                                            "ansible.legacy.async_status"):
                    res = self._run_async_status(task["action"], args, become, ctx,
                                                 task.get("async_options"))
                elif task.get("accelerator") and (
                        self.accelerated_modules
                        or task.get("accelerator") == "required"):
                    res = self._run_accelerated_action(task, args, become)
                    if res is not None:
                        provenance = {
                            "mode": "native-agent",
                            "reason": "accelerated action",
                        }
                else:
                    res = None
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
        # Module output is data, never a template (Ansible marks it unsafe).
        res = _mark_unsafe(res)
        if isinstance(included_vars, dict):
            # Only the include_vars controller reply carries authored definitions.
            res["ansible_facts"] = _IncludedVars(
                (key, _decode_included_var(value))
                for key, value in included_vars.items()
            )
            res["ansible_facts"].controller_reference = included_vars_reference
        self._apply_overrides(task, res, ctx)
        if refreshed_vars is not None:
            res["_ansiroll_vars"] = refreshed_vars
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

    @staticmethod
    def _baked_source_key(task, src):
        # Role trees are baked under paths relative to files/ or templates/.
        if not task.get("_baked_tree") or not isinstance(src, str) or os.path.isabs(src):
            return src
        parts = src.split("/")
        if parts[-1] in ("", ".", ".."):
            return src
        if ".." in parts:
            # Relative Ansible source paths traverse the controller's actual
            # directories. A missing or unbaked component can't be canceled.
            directories = set(task.get("_baked_tree_dirs") or ())
            aliases = task.get("_baked_tree_aliases") or {}
            prefix = []
            for part in parts[:-1]:
                if part in ("", "."):
                    continue
                if part == "..":
                    if not prefix:
                        return src
                    prefix.pop()
                else:
                    prefix.append(part)
                    directory = "/".join(prefix)
                    if directory in aliases:
                        target = aliases[directory]
                        prefix = [] if target == "." else target.split("/")
                    elif directory not in directories:
                        return src
            if "/".join(prefix + [parts[-1]]) not in (task.get("_baked") or {}):
                return src
        # Ansible checks the original relative path, then normalizes it when reading.
        return os.path.normpath(src)

    def _baked_module_source(self, task, args):
        """Return baked controller-side input for worker-local memory transport."""
        name = str(task.get("action") or "").split(".")[-1]
        if name != "unarchive" or _ansible_boolean(args.get("remote_src", False)):
            return None
        src = args.get("src")
        key = self._baked_source_key(task, src)
        marker = (task.get("_baked") or {}).get(key)
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
        try:
            if cw is not None:
                ectx = dict(ctx)
                ectx.update(res)
                if task.get("register"):
                    ectx[task["register"]] = res
                try:
                    res["changed"] = self.when(cw if isinstance(cw, list) else [cw], ectx, strict=True)
                except UntrustedConditional:
                    raise
                except Exception as ex:
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
            if fw is not None:
                ectx = dict(ctx)
                ectx.update(res)
                if task.get("register"):
                    ectx[task["register"]] = res
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
        raw_args = task.get("args") or {}
        if name == "assert":
            # Its condition expressions are interpreted only by when().
            raw_args = {k: v for k, v in raw_args.items() if k != "that"}
        args = self.template(raw_args, ctx)
        # Ansible accepts the complete module argument dictionary through a
        # single template, representing it internally as `_raw_params`.
        if (
                isinstance(args, dict)
                and set(args) == {"_raw_params"}
                and isinstance(args["_raw_params"], dict)):
            args = dict(args["_raw_params"])
        if name == "debug":
            for key, value in args.items():
                try:
                    _fail_on_undef(value)
                except jinja2.UndefinedError as ex:
                    if _core_at_least(2, 19):
                        return {
                            "changed": False, "failed": True,
                            "msg": ("Task failed: Finalization of task args for "
                                    "'ansible.builtin.debug' failed: Error while "
                                    "resolving value for %r: %s" % (key, ex)),
                        }
                    raise
            if "var" in args:
                key = args["var"]
                # `var:` takes an expression (often dotted), evaluated against vars.
                try:
                    val = self.templater.evaluate(key, ctx)
                except jinja2.UndefinedError as ex:
                    val = _captured(str(ex))
                except Exception:
                    val = ctx.get(key, self.vars.get(key))
                return {"changed": False, key: _debug_var_value(val),
                        "_ansible_verbose_always": True}
            return {
                "changed": False,
                "msg": args.get("msg", "Hello world!"),
                "_ansible_verbose_always": True,
            }
        args = _fail_on_undef(args)
        if name == "set_fact":
            import keyword
            facts = {}
            key_templates = set(task.get("set_fact_key_templates", ()))
            cacheable = _boolean(args.get("cacheable", False))
            reserved = ("true", "false", "yes", "no", "on", "off", "none", "null")
            for k, value in args.items():
                if k == "cacheable":
                    continue
                if k in key_templates:
                    k = _fail_on_undef(self.template(str(k), ctx))
                if not isinstance(k, str):
                    return {"failed": True, "changed": False,
                            "msg": "Invalid variable name '%s' of type '%s'"
                                   % (k, type(k).__name__)}
                if not k.isidentifier() or keyword.iskeyword(k) or k.lower() in reserved:
                    return {"failed": True, "changed": False,
                            "msg": "Invalid variable name '%s'" % k}
                facts[_trust_set_fact_value(k)] = _trust_set_fact_value(value)
            self.vars.update(facts)
            return {
                "changed": False,
                "ansible_facts": facts,
                "_ansible_facts_cacheable": cacheable,
            }
        if name == "fail":
            return {"failed": True, "changed": False, "msg": args.get("msg", "Failed as requested from task")}
        if (name == "copy" and isinstance(args, dict)
                and args.get("src") is not None and args.get("content") is None
                and args.get("dest")
                and _ansible_boolean(args.get("remote_src", False))):
            # The copy action plugin (after its argument checks and the
            # content branch) hands remote_src copies straight to the copy
            # module, which reads src on the target.
            return self.run_module(
                task["action"], _fail_on_undef(_strip_omit(args)), become=become)
        if name in ("template", "copy"):
            return self._run_file_action(name, task, args, ctx, become)
        if name == "assert":
            # `that` entries are expressions, never pre-templated (a templated
            # string would be untrusted input to the evaluator).
            tests = (task.get("args") or {}).get("that", args.get("that", []))
            if isinstance(tests, str):
                tests = [tests]
            try:
                ok = self.when(tests, ctx, keyword=None)
            except ConditionalError as ex:
                return {"failed": True, "changed": False, "msg": str(ex)}
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

    def _async_dir_option(self, ctx, options, version):
        """Resolve the configured shell option and its task overrides."""
        env = self._module_env or {}
        if version < (2, 12) and "ANSIBLE_ASYNC_DIR" in env:
            return env["ANSIBLE_ASYNC_DIR"]
        value = (ctx or {}).get("ansible_async_dir")
        if value is None or _is_undef(value):
            value = options.get("dir", "~/.ansible_async")
        return _as_text(self.template(value, ctx))

    def _remote_expand_user(self, path, become):
        """ActionBase._remote_expand_user: a bare ~ means the become user's home."""
        if not path.startswith("~"):
            return path
        head, sep, rest = path.partition(os.path.sep)
        if head == "~" and become and self.become_user:
            head = "~%s" % self.become_user
        return os.path.expanduser(head) + sep + rest

    def _run_async_status(self, action, args, become, ctx, options=None):
        """ansible.builtin.async_status action plugin around its module."""
        options = options or {}
        version = tuple(options.get("version", (2, 21)))
        unsupported = sorted(set(args) - {"jid", "mode"})
        if unsupported:
            if version >= (2, 13):
                msg = ("Unsupported parameters for (%s) module: %s. "
                       "Supported parameters include: jid, mode." %
                       (action, ", ".join(unsupported)))
            else:
                msg = "Invalid options for %s: %s" % (action, ",".join(unsupported))
            return {"failed": True, "changed": False, "msg": msg}
        async_dir = self._async_dir_option(ctx, options, version)
        module_args = dict(args, _async_dir=async_dir)
        if version < (2, 12):
            if "jid" not in args:
                return {"failed": True, "msg": "jid is required"}
            return self.run_module(action, module_args, become=become)

        module_args["_async_dir"] = self._remote_expand_user(async_dir, become)
        modern_flags = version >= (2, 19)
        stopped = False if modern_flags else 0
        results = {
            "started": stopped, "finished": stopped, "stdout": "", "stderr": "",
            "stdout_lines": [], "stderr_lines": [],
        }
        if "jid" not in args:
            if version[:2] == (2, 12):
                results.update(failed=True, msg="jid is required")
                return results
            return {"failed": True, "changed": False, "msg": "missing required arguments: jid"}
        mode = args.get("mode", "status")
        if version >= (2, 13) and mode not in ("status", "cleanup"):
            message = "value of mode must be one of: status, cleanup, got: %s" % _as_text(mode)
            return {"failed": True, "changed": False, "msg": message.rstrip()}

        jid = _as_text(args["jid"])
        results["ansible_job_id"] = jid
        log_path = os.path.join(module_args["_async_dir"], jid)
        if mode == "cleanup":
            results["erased"] = log_path
        else:
            results["results_file"] = log_path
            results["started"] = True if modern_flags else 1
        results = _f_combine(results, self.run_module(action, module_args, become=become),
                             recursive=True)
        if modern_flags:
            for convert in ("started", "finished"):
                results[convert] = bool(results[convert])
        return results

    def _run_async_module(self, action, args, become=False, async_val=0, ctx=None,
                          options=None):
        meta = self.modules.get(action)
        if meta is None:
            return None
        options = options or {}
        version = tuple(options.get("version", (2, 21)))
        if self.check_mode and version < (2, 17):
            return {"failed": True, "changed": False,
                    "msg": "check mode and async cannot be used on same task."}
        params = self._module_params(args)
        # The wrapper and its module receive the same task environment.
        env = dict(self._module_env or {})
        env["ANSIBLE_ASYNC_DIR"] = self._async_dir_option(ctx, options, version)
        try:
            ack = self._run_async_via_worker(
                meta, params, become, env=env,
                time_limit=async_val, version=version,
                poll_before_timeout=options.get("poll_before_timeout", False))
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
        if not ack.get("failed"):
            ack["changed"] = True
        return ack

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
            key = self._baked_source_key(task, src)
            raw = baked.get(key)
            meta = (task.get("_baked_meta") or {}).get(key) or {}
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
                    rendered = self.templater.render_string(raw, tctx)
                    if _core_at_least(2, 19) and isinstance(rendered, _Omit):
                        return {"failed": True, "changed": False,
                                "msg": "Task failed: A template was resolved to an Omit scalar."}
                    new_content = _as_text(rendered)
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
        if isinstance(facts, _IncludedVars):
            for key, value in facts.items():
                if (key not in self.extra_vars and key not in self._set_fact_overrides
                        and key not in self._untrusted_runtime_vars):
                    self.vars[key] = value
                    self._runtime_var_overrides.add(key)
        elif isinstance(facts, dict):
            # Facts are module output: never templated (Ansible marks them unsafe).
            facts = _mark_unsafe(facts)
            self.vars.update(facts)
            if str(task.get("action") or "").rsplit(".", 1)[-1] == "set_fact":
                self._runtime_var_overrides.update(facts)
                self._set_fact_overrides.update(facts)
            existing = self.vars.get("ansible_facts")
            if not isinstance(existing, dict):
                existing = self.vars["ansible_facts"] = {}
            # The `ansible_facts` magic var holds facts without the ansible_
            # prefix ('fqdn' in ansible_facts), unlike the injected top-level
            # ansible_* vars.
            for k, v in facts.items():
                key = k[8:] if isinstance(k, str) and k.startswith("ansible_") else k
                existing[key] = v

    def _queue_notify(self, task, res):
        if not res.get("changed"):
            return
        for name in (task.get("notify") or []):
            if name not in self.notified:
                self.notified.append(name)

    # ---- handlers --------------------------------------------------------
    def flush_handlers(self):
        if not self.notified:
            return
        pending, self.notified = self.notified, []
        for name in pending:
            for h in self.handlers:
                if h.get("name") == name or name in (h.get("listen") or []):
                    self.run_task(h, {}, getattr(self, "_play_become", False))

    # ---- facts -----------------------------------------------------------
    def gather_facts(self, environment=None, become=False, become_user=None,
                     fact_schema=None):
        # Reported as a task so the recap matches Ansible's "Gathering Facts".
        node = {"name": "Gathering Facts", "action": "gather_facts",
                "runtime_id": "__gather_facts__"}
        self.emit_start(node)
        inherited_become_user = self.become_user
        try:
            if become_user:
                self.become_user = _as_text(become_user)
            self._module_env = self._task_environment(
                {"environment": environment}, self.ctx())
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
        except Exception as ex:  # pragma: no cover
            sys.stderr.write("ansiroll: fact gathering failed: %s\n" % ex)
            return
        finally:
            self._module_env = None
            self.become_user = inherited_become_user
        self._absorb_facts({"action": "setup"}, res)
        self._task_provenance[id(node)] = provenance
        self.emit(node, "ok", {"changed": False})
        self._tally("ok")

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

    def _module_params(self, args):
        module_args = dict(args)
        module_args["_ansible_check_mode"] = bool(self.check_mode)
        module_args["_ansible_diff"] = bool(self.diff_mode)
        return json.dumps({"ANSIBLE_MODULE_ARGS": module_args})

    def run_module(self, action, args, become=False):
        meta = self.modules.get(action)
        if meta is None:
            return {"failed": True, "changed": False,
                    "msg": "ansiroll: module %r was not embedded" % action}
        params = self._module_params(args)
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

    def _run_async_via_worker(self, meta, params, become, env, time_limit, version,
                              poll_before_timeout):
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
                   "params": params, "profile": profile, "env": env,
                   "time_limit": time_limit, "version": version,
                   "poll_before_timeout": poll_before_timeout}
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
        if w:
            if w["proc"].poll() is None:
                return w
            self._stop_worker(self._workers.pop(key))
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
        try:
            proc._ansiroll_stderr = _WorkerStderr(proc)
        except BaseException:
            self._stop_worker(w)
            raise
        self._workers[key] = w
        bundle_started = time.monotonic()
        if self.module_bundle is not None:
            bundle_bytes = self.module_bundle
            self._frame_send(proc, {
                "op": "bundle", "size": len(bundle_bytes),
            })
            self._write_all(proc, bundle_bytes)
            ack = self._frame_recv(proc)
            if not isinstance(ack, dict) or not ack.get("ok"):
                raise RuntimeError("worker raw bundle load failed: %r" % (ack,))
        self.engine_profile["worker_bundle"] += time.monotonic() - bundle_started
        self.engine_profile["workers_started"] += 1
        identities = self.engine_profile.setdefault("worker_identities", {})
        identities[identity] = identities.get(identity, 0) + 1
        self.engine_profile["worker_startup"] += time.monotonic() - startup_started
        return w

    def _worker_stderr(self, become):
        """Return up to the last 64 KiB of worker diagnostics."""
        become = self._needs_become(become)
        key = (True, self.become_user) if become else False
        w = self._workers.get(key)
        if not w:
            return ""
        try:
            return w["proc"]._ansiroll_stderr.read()
        except OSError:
            return ""

    def _frame_send(self, proc, obj):
        b = json.dumps(obj).encode("utf-8")
        self._write_all(proc, struct.pack(">I", len(b)) + b)

    @staticmethod
    def _write_all(proc, data):
        # The worker pipes are unbuffered (bufsize=0): write() may be short.
        view = memoryview(data)
        diagnostics = getattr(proc, "_ansiroll_stderr", None)
        while view:
            if diagnostics is not None:
                diagnostics.wait(proc.stdin, selectors.EVENT_WRITE)
            written = proc.stdin.write(view)
            if not written:
                raise OSError("worker request write made no progress")
            view = view[written:]
        proc.stdin.flush()

    def _send_worker_request(self, proc, request, source=None):
        if source is None:
            self._frame_send(proc, request)
            return
        request = dict(request)
        request["source_size"] = len(source)
        self._frame_send(proc, request)
        self._write_all(proc, source)

    def _frame_recv(self, proc):
        diagnostics = getattr(proc, "_ansiroll_stderr", None)
        head = self._readn(proc.stdout, 4, diagnostics)
        if not head:
            return None
        (n,) = struct.unpack(">I", head)
        body = self._readn(proc.stdout, n, diagnostics)
        if body is None:
            return None
        return json.loads(body.decode("utf-8"))

    @staticmethod
    def _readn(stream, n, diagnostics=None):
        chunks = []
        got = 0
        while got < n:
            if diagnostics is not None:
                diagnostics.wait(stream, selectors.EVENT_READ)
            c = stream.read(n - got)
            if not c:
                return None
            chunks.append(c)
            got += len(c)
        return b"".join(chunks)

    def _stop_worker(self, worker):
        proc = worker["proc"]
        # EOF ends the worker's request loop. Closing avoids waiting for a
        # worker whose request pipe is full.
        proc.stdin.close()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            proc.wait()
        finally:
            proc.stdout.close()
            capture = getattr(proc, "_ansiroll_stderr", None)
            if capture is not None:
                capture.close()
            else:
                proc.stderr.close()

    def _close_workers(self):
        for worker in self._workers.values():
            self._stop_worker(worker)
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


def _status_of(res):
    if res.get("unreachable"):
        return "unreachable"
    if res.get("failed"):
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


def _state_json_default(value):
    # The final state can include lazy play vars which were never resolved.
    return None if _is_undef(value) else str(value)


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
    for play in plays:
        if pretty:
            banner = "PLAY [%s] " % (play.get("name") or play.get("hosts") or "")
            sys.stdout.write("\n" + banner + "*" * max(3, 79 - len(banner)) + "\n")
        init_started = time.monotonic()
        rt = Runtime(modules, ansiballz_code, host_vars, check_mode=check_mode,
                     diff_mode=diff_mode,
                     become_password=become_password, extra_vars=extra_vars,
                     pretty=pretty, profile=profile, module_bundle=module_bundle,
                     resource_reader=resource_reader,
                     native_only=native_only)
        engine_profile["runtime_init"] += time.monotonic() - init_started
        try:
            play_started = time.monotonic()
            rt.run_play(play, defer_handlers=defer_handlers, initial_notified=initial_notified)
        finally:
            engine_profile["play_execution"] += time.monotonic() - play_started
            cleanup_started = time.monotonic()
            rt.cleanup()  # shut the worker down
            engine_profile["worker_cleanup"] += time.monotonic() - cleanup_started
        host_vars = rt.vars  # carry facts/registers across plays for the same host
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
            default=_state_json_default)))
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
    view = memoryview(struct.pack(">I", len(b)) + b)
    while view:
        written = os.write(out_fd, view)
        if not written:
            raise OSError("agent channel write made no progress")
        view = view[written:]


def _delegate_vars(ctx):
    """Return the serializable part of a task context for controller actions."""
    safe = {}
    for key, value in (ctx or {}).items():
        if key == "vars":
            continue
        try:
            # Materialize lazy values once so sending the frame cannot
            # resolve the same context a second time.
            safe[key] = json.loads(json.dumps(value, default=str))
        except (TypeError, ValueError, jinja2.UndefinedError):
            continue
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
    extra_vars = dict(g.get("_INJECTED_EXTRA_VARS") or {})
    host_var_overrides = g.get("_INJECTED_HOST_VAR_OVERRIDES") or {}
    specialized_vars = dict(g.get("_SPECIALIZED_VARS") or {})
    specialized_vars.update(g.get("_FROZEN_VARS") or {})
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
            rt.run_play(play)
            failed = failed or rt.failed
            for k in total:
                total[k] += rt.stats.get(k, 0)
            rt.stats = {k: 0 for k in total}
    finally:
        rt.cleanup()
    _chan_send(resp_fd, {"t": "stats", "host": rt.host, "stats": total})
    _chan_send(resp_fd, {"t": "done", "failed": failed})
