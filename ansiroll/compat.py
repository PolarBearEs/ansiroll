"""
Ansible-version compatibility shims (2.10 -> 2.20+).

All version-fragile ansible-core calls used at compile time are funnelled through
here so the rest of the package stays version-agnostic. Each function feature-
detects rather than branching on a version string.
"""

from __future__ import annotations

_loader_initialized = False


def init_loader():
    global _loader_initialized
    if _loader_initialized:
        return
    try:
        from ansible.plugins.loader import init_plugin_loader
        init_plugin_loader()
    except Exception:
        # Older ansible-core initialised the loader on import.
        pass
    _loader_initialized = True


def make_templar():
    init_loader()
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar
    return Templar(loader=DataLoader())


def find_module(action):
    """Resolve an action/module name to (filesystem path, fqcn) or (None, None)."""
    init_loader()
    from ansible.plugins.loader import module_loader
    try:
        ctx = module_loader.find_plugin_with_context(action)
    except Exception:
        ctx = None
    if ctx is not None and getattr(ctx, "resolved", True):
        path = getattr(ctx, "plugin_resolved_path", None)
        fqcn = getattr(ctx, "resolved_fqcn", None) or action
        if path:
            return path, fqcn
    # Fallback for very old loaders without find_plugin_with_context.
    path = module_loader.find_plugin(action)
    if path:
        return path, action
    return None, None


def build_module(module_name, module_path, templar, task_vars):
    """Call modify_module across signature variants; return (wrapper_bytes, profile)."""
    from ansible.executor import module_common

    modify = module_common.modify_module
    # ansible-core 2.18+ uses keyword-only args and returns a _BuiltModule.
    try:
        built = modify(
            module_name=module_name,
            module_path=module_path,
            module_args={},
            templar=templar,
            task_vars=task_vars,
            module_compression="ZIP_DEFLATED",
        )
    except TypeError:
        # Older positional signature: modify_module(name, path, args, templar=..., ...)
        built = modify(module_name, module_path, {}, templar=templar, task_vars=task_vars)

    if hasattr(built, "b_module_data"):
        return built.b_module_data, getattr(built, "serialization_profile", "legacy")
    # Oldest variants returned (data, style, shebang).
    data = built[0] if isinstance(built, tuple) else built
    return data, "legacy"
