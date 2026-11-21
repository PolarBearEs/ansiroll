"""Ansiroll test package."""

import os
import sys


# The playbook directory can contain an unrelated tests.py.
_BOOTSTRAP = (
    "import sys; _implicit_cwd = sys.path[:1] == ['']; "
    "sys.path[:1] = [] if _implicit_cwd else sys.path[:1]; "
)
_ANSIBLE_MODULE_ALIASES = {
    "ansible.compat.selectors": "selectors",
    "ansible.module_utils.compat.selectors": "selectors",
    "ansible.module_utils.distro": "distro",
}
_ANSIBLE_SIX = "ansible.module_utils.six"


def _reference_python(code, *arguments):
    import ansible

    return [sys.executable, "-c", _BOOTSTRAP + code, os.path.abspath(ansible.__file__), *arguments]


def ansible_playbook_command():
    return _reference_python("from tests import _run_ansible_playbook; _run_ansible_playbook(sys.argv.pop(1))")


def ansiroll_compile_command():
    return _reference_python(
        "import os, runpy; filename, bootstrap = sys.argv[1:3]; del sys.argv[1:3]; "
        "load_ansible = runpy.run_path(bootstrap, run_name='_ansiroll_parity_bootstrap')['_load_reference_ansible']; "
        "sys.path[:0] = [os.getcwd()] if _implicit_cwd else []; "
        "load_ansible(filename); runpy.run_module('ansiroll.cli', run_name='__main__', alter_sys=True)",
        os.path.abspath(__file__),
    ) + ["compile"]


def parity_pythonpath(root):
    # The empty fallback adds the playbook directory after the repository.
    return os.pathsep.join([root, os.environ.get("PYTHONPATH", "")])


def _is_ansible_compatibility_alias(name, module):
    if module is None:
        return False
    original_name = _ANSIBLE_MODULE_ALIASES.get(name)
    if original_name:
        return module is sys.modules.get(original_name)
    if not name.startswith(_ANSIBLE_SIX + ".moves."):
        return False

    # Six itself is subject to the origin check. Its registered move must
    # point to this same loaded module. Do not resolve the alias via the
    # import system, which could just return the current cached value.
    six = sys.modules.get(_ANSIBLE_SIX)
    importer = getattr(six, "_importer", None)
    moved_module = getattr(six, "MovedModule", None)
    if importer is None or moved_module is None:
        return False
    move = importer.known_modules.get(name)
    return isinstance(move, moved_module) and module is sys.modules.get(move.mod)


def _load_reference_ansible(filename):
    from importlib import util

    expected = os.path.realpath(os.path.dirname(filename))
    for name, module in tuple(sys.modules.items()):
        if name == "ansible" or name.startswith("ansible."):
            origin = getattr(module, "__file__", None) or getattr(getattr(module, "__spec__", None), "origin", None)
            if origin and origin not in ("built-in", "frozen"):
                directory = os.path.realpath(os.path.dirname(origin))
                if directory != expected and not directory.startswith(expected + os.sep):
                    if _is_ansible_compatibility_alias(name, module):
                        continue
                    raise RuntimeError("cached %s came from %s, expected Ansible from %s" % (name, origin, expected))

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


def _has_brace_expansion(path):
    import re

    braces = []
    for token in re.finditer(r"[{},]", path):
        char = token.group()
        if char == "{":
            braces.append((token.end(), False))
        elif braces and char == ",":
            braces[-1] = (braces[-1][0], True)
        elif braces:
            start, has_comma = braces.pop()
            if has_comma or re.fullmatch(
                r"(?:[+-]?[0-9]+\.\.[+-]?[0-9]+|[A-Za-z]\.\.[A-Za-z])(?:\.\.[+-]?[0-9]+)?",
                path[start:token.start()],
            ):
                return True
    return False


def _shell_preserves_braces():
    import subprocess

    # Probe the trampoline's shell with fixed words. Never run the launcher
    # or pass its interpreter path to the shell for parsing.
    try:
        result = subprocess.run(
            ["/bin/sh", "-c", 'printf "%s\\n" {ansiroll_a,ansiroll_b} {1..2}'],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout == "{ansiroll_a,ansiroll_b}\n{1..2}\n"


def _shell_preserves_path_pattern(path):
    import subprocess

    # Pass the path as an argument to fixed shell code. Expand only its
    # pathname pattern, without evaluating it as a command or splitting it.
    try:
        result = subprocess.run(
            ["/bin/sh", "-c", 'IFS=; literal=$1; set -- $literal; [ "$#" -eq 1 ] && [ "$1" = "$literal" ]',
             "ansiroll-launcher-check", path],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _is_python_interpreter(executable):
    import re

    return (
        re.fullmatch(r"(?:python|pypy)[0-9.]*t?d?", os.path.basename(executable)) is not None
        or (os.path.isabs(executable) and os.path.realpath(executable) == os.path.realpath(sys.executable))
    )


def _launcher_body(source):
    import re

    if not source.startswith("#!"):
        return source
    shebang, _, body = source.partition("\n")
    command = shebang[2:].strip(" \t")
    if command != "/bin/sh":
        # Direct shebangs may choose Python, but replay cannot reproduce
        # interpreter flags or environment changes made by the launcher.
        env_python = re.fullmatch(r"/(?:usr/)?bin/env[ \t]+(?:-S[ \t]+)?([^/ \t]+)", command)
        if env_python and _is_python_interpreter(env_python[1]):
            return body
        if re.fullmatch(r"/[^ \x00-\x1f\x7f]+", command) and _is_python_interpreter(command):
            return body
        return source

    # uv relocatable scripts and long Python shebangs use a shell/Python
    # trampoline that only chooses the interpreter.
    command, _, remaining = body.partition("\n")
    prefix, suffix = "'''exec' ", ' "$0" "$@"'
    if not (command.startswith(prefix) and command.endswith(suffix) and remaining.startswith("' '''\n")):
        return source
    interpreter = command[len(prefix):-len(suffix)]
    relocatable = '"$(dirname -- "$(realpath -- "$0")")"/'
    if interpreter.startswith(relocatable):
        target = interpreter[len(relocatable):]
        if not re.fullmatch(r"'[^']+'", target):
            return source
        executable = target[1:-1]
    elif re.fullmatch(r"'/[^']*'(?:\"'\"'[^']*')*", interpreter):
        # uv joins single-quoted pieces with a double-quoted apostrophe.
        executable = interpreter[1:-1].replace("'\"'\"'", "'")
    elif re.fullmatch(r'"/[^"$\x60\\]+"', interpreter):
        executable = interpreter[1:-1]
    # Shell token delimiters are ASCII. Unicode spaces can be literal
    # filename characters, like punctuation that has no shell meaning.
    elif re.fullmatch(r"""/[^ \x00-\x1f\x7f'"`$\\|&;<>()]+""", interpreter):
        if _has_brace_expansion(interpreter) and not _shell_preserves_braces():
            return source
        if any(char in interpreter for char in "*?[") and not _shell_preserves_path_pattern(interpreter):
            return source
        executable = interpreter
    else:
        return source
    if _is_python_interpreter(executable):
        return remaining.partition("\n")[2]
    return source


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

    body = _launcher_body(source)
    if source.startswith("#!") and body == source:
        diagnostics.append("%s: launcher shebang cannot be replayed" % path)
        return None

    # The selected Ansible installation ships its own release's launcher.
    # A native --version probe alone can report the correct package even
    # when the script itself came from a different release.
    stub = os.path.join(package, "cli", "scripts", "ansible_cli_stub.py")
    try:
        with tokenize.open(stub) as expected:
            if body == _launcher_body(expected.read()):
                # The pinned process will execute the same startup code; a
                # separate --version run cannot add evidence of provenance.
                return path
        diagnostics.append("%s: launcher differs from the selected Ansible release" % path)
        return None
    except FileNotFoundError:
        if not associated:
            diagnostics.append("%s: selected Ansible has no launcher source to verify this script" % path)
            return None
    except (OSError, SyntaxError, UnicodeError) as ex:
        diagnostics.append("%s: %s" % (stub, ex))
        return None

    probe = (_BOOTSTRAP + "from tests import _load_reference_ansible, _run_legacy_playbook; "
             "_load_reference_ansible(sys.argv.pop(1)); _run_legacy_playbook(sys.argv.pop(1))")
    fields = _ansible_version_fields([sys.executable, "-c", probe, filename, path], path, diagnostics)
    installed = fields.get("ansible python module location", "")
    if installed and os.path.realpath(installed) == package:
        return path
    return None


def _installed_playbook_launchers(package):
    from importlib import metadata

    # The pinned package may be outside the subprocess's import path.
    search_path = list(dict.fromkeys([*sys.path, os.path.dirname(package)]))
    for name in ("ansible-core", "ansible-base", "ansible"):
        for distribution in metadata.distributions(name=name, path=search_path):
            if os.path.realpath(distribution.locate_file("ansible")) != package:
                continue
            for entry in distribution.files or ():
                if entry.name == "ansible-playbook" and "ansible_test" not in entry.parts:
                    yield str(distribution.locate_file(entry))

    # Source checkouts may expose ansible through PYTHONPATH without a record.
    library = os.path.dirname(package)
    if os.path.basename(library) == "lib":
        yield os.path.join(os.path.dirname(library), "bin", "ansible-playbook")


def _run_ansible_playbook(filename):
    import shutil
    from itertools import chain

    package = _load_reference_ansible(filename)
    from ansible.release import __version__

    installed_launchers = _installed_playbook_launchers(package)
    environment_script = os.path.join(os.path.dirname(sys.executable), "ansible-playbook")
    if tuple(map(int, __version__.split(".")[:2])) >= (2, 13):
        # The CLI looks for sibling helpers next to argv[0] before PATH.
        sys.argv[0] = next((script for script in installed_launchers if os.path.isfile(script)), environment_script)
        from ansible.cli import playbook

        playbook.main()
        return

    # Older releases install a script instead of a module entry point.
    candidates = chain(
        ((script, True) for script in installed_launchers),
        [(environment_script, False)],
        ((shutil.which("ansible-playbook", path=directory or os.curdir), False)
         for directory in os.get_exec_path()),
    )
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
