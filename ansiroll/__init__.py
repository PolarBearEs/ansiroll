"""Ansiroll: compile an Ansible playbook into a single self-contained file."""

from .compiler import compile_playbook

__all__ = ["compile_playbook"]
__version__ = "0.1.0"
