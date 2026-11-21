"""Ansiroll test package."""

import os
import sys


# The playbook directory can contain an unrelated tests.py.
_BOOTSTRAP = "import sys; sys.path[:1] = [] if sys.path[:1] == [''] else sys.path[:1]; "


def ansible_playbook_command():
    import ansible

    run = _BOOTSTRAP + "from tests import _run_ansible_playbook; _run_ansible_playbook(sys.argv.pop(1))"
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
    sys.path.insert(0, os.path.dirname(os.path.realpath(script)))
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


def _launcher_body(source):
    # Installed scripts replace the package stub's interpreter line.
    return source.partition("\n")[2] if source.startswith("#!") else source


def _legacy_python_launcher(path, filename, package, diagnostics, associated):
    import tokenize

    try:
        with tokenize.open(path) as script:
            source = script.read()
        compile(source, path, "exec")
    except OSError as ex:
        diagnostics.append("%s: %s" % (path, ex))
        return None
    except (SyntaxError, UnicodeError):
        diagnostics.append("%s: cannot replay a non-Python launcher's initialization" % path)
        return None

    # The selected Ansible installation ships its own release's launcher.
    # A native --version probe alone can report the correct package even
    # when the script itself came from a different release.
    stub = os.path.join(package, "cli", "scripts", "ansible_cli_stub.py")
    try:
        with tokenize.open(stub) as expected:
            if _launcher_body(source) != _launcher_body(expected.read()):
                diagnostics.append("%s: launcher differs from the selected Ansible release" % path)
                return None
    except FileNotFoundError:
        if not associated:
            diagnostics.append("%s: selected Ansible has no launcher source to verify this script" % path)
            return None
    except (OSError, SyntaxError, UnicodeError) as ex:
        diagnostics.append("%s: %s" % (stub, ex))
        return None

    if not associated:
        # Check the script's native import before replaying it with pytest's
        # interpreter. A system-installed script can pass both probes.
        fields = _ansible_version_fields([path], path, diagnostics)
        installed = fields.get("ansible python module location", "")
        if not installed or os.path.realpath(installed) != package:
            diagnostics.append("%s: launcher did not report the expected Ansible package" % path)
            return None

    probe = (_BOOTSTRAP + "from tests import _load_reference_ansible, _run_legacy_playbook; "
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
                candidates.append((str(distribution.locate_file(entry)), True))

    # Source checkouts may expose ansible through PYTHONPATH without a record.
    library = os.path.dirname(package)
    if os.path.basename(library) == "lib":
        candidates.append((os.path.join(os.path.dirname(library), "bin", "ansible-playbook"), True))
    candidates.append((os.path.join(os.path.dirname(sys.executable), "ansible-playbook"), False))
    candidates.extend((shutil.which("ansible-playbook", path=directory or os.curdir), False)
                      for directory in os.get_exec_path())
    diagnostics = []
    seen = set()
    for script, associated in candidates:
        if script and script not in seen and os.path.isfile(script):
            seen.add(script)
            python_script = _legacy_python_launcher(script, filename, package, diagnostics, associated)
            if python_script:
                _run_legacy_playbook(python_script)
                return
    detail = "\n" + "\n".join(diagnostics) if diagnostics else ""
    raise FileNotFoundError("ansible-playbook script not found for %s%s" % (package, detail))
