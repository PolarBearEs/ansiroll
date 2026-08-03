"""
Compile-time module embedding.

For each Ansible module referenced by a play, build its AnsiballZ payload (module
source + module_utils, zipped) using ansible-core's own ``modify_module``, then
extract the self-contained ``_ansiballz_main`` code and the per-module zip/profile
via AST. At runtime the inlined runtime rebuilds the wrapper with freshly templated
args -- so the compiled file needs only Python on the target, not Ansible.

This module runs only at compile time (it imports ansible-core).
"""

from __future__ import annotations

import ast
import os

from .compat import build_module, find_module, make_templar

# Actions the runtime handles itself; never embedded as modules.
SPECIAL_ACTIONS = {
    "debug", "set_fact", "fail", "assert", "meta",
    "ansible.builtin.debug", "ansible.builtin.set_fact",
    "ansible.builtin.fail", "ansible.builtin.assert", "ansible.builtin.meta",
}

class ModuleEmbedder:
    def __init__(self, python_interpreter="/usr/bin/python3"):
        self.python_interpreter = python_interpreter
        self.templar = make_templar()
        self.ansiballz_code = ""  # retained for compiler compatibility; runner is self-contained
        self._cache = {}  # action name -> meta dict
        self._canonical_cache = {}  # resolved module -> meta dict

    # Action plugins implemented on top of a different module.
    MODULE_ALIASES = {"shell": "command", "raw": "command"}

    def embed(self, action):
        """Return runtime meta for ``action`` or ``None`` for special actions."""
        if action in SPECIAL_ACTIONS:
            return None
        if action in self._cache:
            return self._cache[action]

        resolve = self.MODULE_ALIASES.get(action.split(".")[-1], action)
        path, fqcn = find_module(resolve)
        if path is None:
            raise ValueError(
                "ansiroll: cannot resolve module %r (not a module or action plugin "
                "supported in v1)" % action
            )

        cache_key = (
            self.python_interpreter,
            os.path.realpath(path),
            fqcn,
        )
        meta = self._canonical_cache.get(cache_key)
        if meta is None:
            wrapper_bytes, profile = build_module(
                module_name=fqcn,
                module_path=path,
                templar=self.templar,
                task_vars={"ansible_python_interpreter": self.python_interpreter},
            )
            wrapper_text = wrapper_bytes.decode("utf-8", errors="surrogateescape")
            zip_b64, module_fqn = _extract(wrapper_text)

            meta = {
                "module_fqn": module_fqn or fqcn,
                "profile": profile or "legacy",
                "zip": zip_b64,
            }
            self._canonical_cache[cache_key] = meta
        self._cache[action] = meta
        return meta


def _lit(node):
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _extract(wrapper_text):
    """Extract (zip_base64, module_fqn) from an AnsiballZ wrapper.

    Handles both the modern wrapper (``_ansiballz_main(zip_data=..., module_fqn=...)``)
    and the legacy 2.10-era wrapper (module-level ``ZIPDATA = "..."`` plus a
    ``runpy.run_module(mod_name=...)`` call). We only need the zip and the module's
    importable name; execution is done by our own runner (see runtime.run_module).
    """
    tree = ast.parse(wrapper_text)
    zip_b64 = None
    fqn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (getattr(tgt, "id", None) == "ZIPDATA"
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str)):
                    zip_b64 = node.value.value
        elif isinstance(node, ast.Call):
            fn = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if fn == "_ansiballz_main":
                for kw in node.keywords:
                    if kw.arg == "zip_data":
                        zip_b64 = _lit(kw.value) or zip_b64
                    elif kw.arg == "module_fqn":
                        fqn = _lit(kw.value) or fqn
            elif fn == "run_module":
                for kw in node.keywords:
                    if kw.arg in ("mod_name", "module_fqn"):
                        fqn = _lit(kw.value) or fqn
    if zip_b64 is None:
        raise ValueError("ansiroll: could not extract module zip from AnsiballZ wrapper")
    return zip_b64, fqn
