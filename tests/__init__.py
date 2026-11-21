"""Ansiroll test package."""

import os
import sys


def ansible_playbook_command():
    return [sys.executable, "-c", "from tests import _run_ansible_playbook; _run_ansible_playbook()"]


def _run_ansible_playbook():
    import runpy
    from importlib import metadata

    import ansible
    from ansible.cli import playbook

    # Import the CLI in the child, with its own standard streams.
    if hasattr(playbook, "main"):
        playbook.main()
        return

    # Older releases install a Python script with no module entry point.
    package = os.path.dirname(os.path.realpath(ansible.__file__))
    for name in ("ansible-core", "ansible-base", "ansible"):
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        if os.path.realpath(distribution.locate_file("ansible")) != package:
            continue
        for entry in distribution.files or ():
            if entry.name == "ansible-playbook":
                script = distribution.locate_file(entry)
                if script.is_file():
                    runpy.run_path(str(script), run_name="__main__")
                    return

    script = os.path.join(os.path.dirname(sys.executable), "ansible-playbook")
    runpy.run_path(script, run_name="__main__")
