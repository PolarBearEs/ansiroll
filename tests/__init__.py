"""Ansiroll test package."""

import os
import sys


def ansible_playbook_command():
    import ansible

    package = os.path.realpath(os.path.dirname(ansible.__file__))
    run = "import sys; from tests import _run_ansible_playbook; _run_ansible_playbook(sys.argv.pop(1))"
    return [sys.executable, "-c", run, package]


def parity_pythonpath(root):
    import ansible

    # Pytest can add a source checkout to sys.path independently of PYTHONPATH.
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(ansible.__file__)))
    inherited = os.environ.get("PYTHONPATH")
    entries = [] if inherited is None else [os.path.abspath(path) for path in inherited.split(os.pathsep)]
    return os.pathsep.join([root, package_root] + entries)


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
    except OSError:
        return None
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


def _run_ansible_playbook(expected_package):
    import shutil
    from importlib import metadata

    import ansible
    from ansible.release import __version__

    package = os.path.realpath(os.path.dirname(ansible.__file__))
    if package != expected_package:
        raise RuntimeError("reference Ansible imported from %s, expected %s" % (package, expected_package))

    if tuple(map(int, __version__.split(".")[:2])) >= (2, 13):
        from ansible.cli import playbook

        playbook.main()
        return

    # Older releases install a script. Importing the CLI before exec would
    # leave its controller temp directory behind.
    candidates = []
    for name in ("ansible-core", "ansible-base", "ansible"):
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        if os.path.realpath(distribution.locate_file("ansible")) != package:
            continue
        for entry in distribution.files or ():
            if entry.name == "ansible-playbook" and "ansible_test" not in entry.parts:
                candidates.append(str(distribution.locate_file(entry)))

    # Source checkouts may expose ansible through PYTHONPATH without a record.
    source_root = os.path.dirname(os.path.dirname(package))
    candidates.extend((
        os.path.join(source_root, "bin", "ansible-playbook"),
        os.path.join(os.path.dirname(sys.executable), "ansible-playbook"),
    ))
    candidates.extend(shutil.which("ansible-playbook", path=directory or os.curdir)
                      for directory in os.get_exec_path())
    for script in dict.fromkeys(candidates):
        if script and os.path.isfile(script):
            python_script = _legacy_python_launcher(script, package)
            if python_script:
                os.execv(sys.executable, [sys.executable, python_script] + sys.argv[1:])
    raise FileNotFoundError("ansible-playbook script not found for %s" % package)
