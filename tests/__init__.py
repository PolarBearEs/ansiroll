"""Ansiroll test package."""

import os
import sys


def ansible_playbook_command():
    return [sys.executable, "-c", "from tests import _run_ansible_playbook; _run_ansible_playbook()"]


def parity_pythonpath(root):
    inherited = os.environ.get("PYTHONPATH")
    entries = [] if inherited is None else [os.path.abspath(path) for path in inherited.split(os.pathsep)]
    return os.pathsep.join([root] + entries)


def _ansible_version_fields(command):
    import subprocess

    try:
        result = subprocess.run(command + ["--version"], capture_output=True, text=True)
    except OSError:
        return {}
    if result.returncode:
        return {}
    return dict(line.strip().split(" = ", 1) for line in result.stdout.splitlines() if " = " in line)


def _legacy_python_launcher(path, package):
    import tokenize

    try:
        with tokenize.open(path) as script:
            compile(script.read(), path, "exec")
    except (SyntaxError, UnicodeError):
        # A shell or native launcher can report the Python script it invokes.
        fields = _ansible_version_fields([path])
        script = fields.get("executable location", "")
        if not (os.path.isabs(script) and os.path.realpath(script) != os.path.realpath(path)
                and os.path.isfile(script)):
            return None
        path = script

    fields = _ansible_version_fields([sys.executable, path])
    installed = fields.get("ansible python module location", "")
    if installed and os.path.realpath(installed) == package:
        return path
    return None


def _run_ansible_playbook():
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
            if entry.name == "ansible-playbook" and "ansible_test" not in entry.parts:
                script = distribution.locate_file(entry)
                if script.is_file():
                    os.execv(sys.executable, [sys.executable, str(script)] + sys.argv[1:])

    # Source checkouts may expose ansible through PYTHONPATH without a record.
    source_root = os.path.dirname(os.path.dirname(package))
    for script in (
        os.path.join(source_root, "bin", "ansible-playbook"),
        os.path.join(os.path.dirname(sys.executable), "ansible-playbook"),
        shutil.which("ansible-playbook"),
    ):
        if script and os.path.isfile(script):
            python_script = _legacy_python_launcher(script, package)
            if python_script:
                os.execv(sys.executable, [sys.executable, python_script] + sys.argv[1:])
    raise FileNotFoundError("ansible-playbook script not found for %s" % package)
