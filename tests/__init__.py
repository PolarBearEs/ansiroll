"""Ansiroll test package."""

import os
import sys


def ansible_playbook_command():
    return [sys.executable, "-c", "from tests import _run_ansible_playbook; _run_ansible_playbook()"]


def _run_ansible_playbook():
    import runpy
    import shutil
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

    # Source checkouts may expose ansible through PYTHONPATH without a record.
    source_root = os.path.dirname(os.path.dirname(package))
    for script in (
        os.path.join(source_root, "bin", "ansible-playbook"),
        os.path.join(os.path.dirname(sys.executable), "ansible-playbook"),
        shutil.which("ansible-playbook"),
    ):
        if script and os.path.isfile(script):
            runpy.run_path(script, run_name="__main__")
            return
    raise FileNotFoundError("ansible-playbook script not found for %s" % package)
