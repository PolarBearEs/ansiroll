"""Ansiroll test package."""

import os
import sys


def ansible_playbook_command():
    import ansible

    run = "import sys; from tests import _run_ansible_playbook; _run_ansible_playbook(sys.argv.pop(1))"
    return [sys.executable, "-c", run, os.path.abspath(ansible.__file__)]


def parity_pythonpath(root):
    inherited = os.environ.get("PYTHONPATH")
    entries = [] if inherited is None else [os.path.abspath(path) for path in inherited.split(os.pathsep)]
    return os.pathsep.join([root] + entries)


def _load_reference_ansible(filename):
    from importlib import util

    expected = os.path.realpath(os.path.dirname(filename))
    spec = util.find_spec("ansible")
    if spec is None or not spec.origin or os.path.realpath(os.path.dirname(spec.origin)) != expected:
        # Pin only Ansible. A checkout added by pytest need not change the
        # reference subprocess's lookup order for any other dependency.
        spec = util.spec_from_file_location(
            "ansible", filename, submodule_search_locations=[os.path.dirname(filename)])
        package = util.module_from_spec(spec)
        sys.modules["ansible"] = package
        spec.loader.exec_module(package)
    else:
        import ansible as package

    actual = os.path.realpath(os.path.dirname(package.__file__))
    if actual != expected:
        raise RuntimeError("reference Ansible imported from %s, expected %s" % (actual, expected))
    return actual


def _run_legacy_playbook(script):
    import runpy

    # Match direct execution of the launcher, including sibling imports.
    sys.path[0] = os.path.dirname(os.path.realpath(script))
    sys.argv[0] = script
    runpy.run_path(script, run_name="__main__")


def _ansible_version_fields(command, candidate, diagnostics):
    import subprocess

    try:
        result = subprocess.run(command + ["--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as ex:
        diagnostics.append("%s: %s" % (candidate, ex))
        return {}
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "exit status %d" % result.returncode
        diagnostics.append("%s: %s" % (candidate, detail[-2000:]))
        return {}
    return dict(line.strip().split(" = ", 1) for line in result.stdout.splitlines() if " = " in line)


def _legacy_python_launcher(path, filename, package, diagnostics):
    import tokenize

    try:
        with tokenize.open(path) as script:
            compile(script.read(), path, "exec")
    except OSError as ex:
        diagnostics.append("%s: %s" % (path, ex))
        return None
    except (SyntaxError, UnicodeError):
        # A shell or native launcher can report the Python script it invokes.
        fields = _ansible_version_fields([path], path, diagnostics)
        script = fields.get("executable location", "")
        if not (os.path.isabs(script) and os.path.realpath(script) != os.path.realpath(path)
                and os.path.isfile(script)):
            return None
        path = script

    probe = ("import sys; from tests import _load_reference_ansible, _run_legacy_playbook; "
             "_load_reference_ansible(sys.argv.pop(1)); _run_legacy_playbook(sys.argv.pop(1))")
    fields = _ansible_version_fields([sys.executable, "-c", probe, filename, path], path, diagnostics)
    installed = fields.get("ansible python module location", "")
    if installed and os.path.realpath(installed) == package:
        return path
    return None


def _run_ansible_playbook(filename):
    import shutil
    from importlib import metadata

    package = _load_reference_ansible(filename)
    from ansible.release import __version__

    if tuple(map(int, __version__.split(".")[:2])) >= (2, 13):
        from ansible.cli import playbook

        playbook.main()
        return

    # Older releases install a script instead of a module entry point.
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
    diagnostics = []
    for script in dict.fromkeys(candidates):
        if script and os.path.isfile(script):
            python_script = _legacy_python_launcher(script, filename, package, diagnostics)
            if python_script:
                _run_legacy_playbook(python_script)
                return
    detail = "\n" + "\n".join(diagnostics) if diagnostics else ""
    raise FileNotFoundError("ansible-playbook script not found for %s%s" % (package, detail))
