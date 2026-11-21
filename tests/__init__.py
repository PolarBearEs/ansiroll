"""Ansiroll test package."""

import os
import shutil
import sys


def ansible_playbook_command():
    from ansible.cli import playbook

    # The console script's shebang can point to a different Python.
    if hasattr(playbook, "main"):
        return [sys.executable, "-c", "from ansible.cli.playbook import main; main()"]

    # Older releases provide their CLI only as a Python script.
    local = os.path.join(os.path.dirname(sys.executable), "ansible-playbook")
    script = local if os.path.isfile(local) else shutil.which("ansible-playbook")
    if script is None:
        raise FileNotFoundError("ansible-playbook script not found")
    return [sys.executable, script]
