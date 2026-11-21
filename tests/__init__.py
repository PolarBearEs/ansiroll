"""Ansiroll test package."""

import ast
import os
import re
import shutil
import subprocess
import sys
import tokenize
from importlib import util
from itertools import chain


# Prefer the standard library during bootstrap, then restore the caller's paths.
_BOOTSTRAP = (
    "import sys; _implicit_cwd = sys.path[:1] == ['']; "
    "sys.path[:1] = [] if _implicit_cwd else sys.path[:1]; "
    "import os; _playbook_cwd = os.getcwd(); _reference_path = sys.path[:]; "
    "_stdlib = os.path.dirname(os.__file__); "
    "_extensions = os.path.join(sys.base_exec_prefix, 'DLLs') if os.name == 'nt' "
    "else os.path.join(_stdlib, 'lib-dynload'); "
    "sys.path[:0] = [_stdlib, _extensions]; "
    "import runpy; "
)
_LOAD_BOOTSTRAP = (
    "_parity_bootstrap = runpy.run_path(sys.argv.pop(1), run_name='_ansiroll_parity_bootstrap'); "
    "sys.path[:] = _reference_path; "
)
_ANSIBLE_MODULE_ALIASES = {
    "ansible.compat.selectors": "selectors",
    "ansible.module_utils.compat.selectors": "selectors",
    "ansible.module_utils.distro": "distro",
}
_ANSIBLE_SIX = "ansible.module_utils.six"


def _reference_python(code, filename=None, *, discover=False):
    if filename is None:
        import ansible

        filename = ansible.__file__

    imports = ""
    if discover:
        # Supply pytest's package search paths without executing site hooks
        # in the interpreter that selects the launcher.
        paths = [os.path.abspath(path) for path in sys.path if isinstance(path, str)]
        imports = (
            "import csv, sysconfig; from importlib import metadata; sysconfig.get_config_vars(); "
            f"_reference_path.extend({paths!r}); "
        )
    flags = ["-S"] if discover else []
    return [sys.executable, *flags, "-c", _BOOTSTRAP + imports + _LOAD_BOOTSTRAP + code,
            os.path.abspath(__file__), os.path.abspath(filename)]


def ansible_playbook_command():
    from ansible.release import __version__

    return _reference_playbook_command(None, __version__)


def _reference_playbook_command(filename, version):
    return _reference_python(
        "_parity_bootstrap['_run_ansible_playbook'](sys.argv.pop(1), sys.argv.pop(1))",
        filename, discover=True,
    ) + [version]


def ansiroll_compile_command():
    return _reference_python(
        "sys.path[:0] = [_playbook_cwd] if _implicit_cwd else []; "
        "_parity_bootstrap['_load_reference_ansible'](sys.argv.pop(1)); "
        "runpy.run_module('ansiroll.cli', run_name='__main__', alter_sys=True)"
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


def _set_playbook_launcher(script):
    # Match direct execution of the launcher, including sibling imports.
    if not getattr(sys.flags, "safe_path", False):
        sys.path.insert(0, os.path.dirname(os.path.realpath(script)))
    sys.argv[0] = script


def _run_legacy_playbook(filename, script):
    import runpy

    _set_playbook_launcher(script)
    _load_reference_ansible(filename)
    runpy.run_path(script, run_name="__main__")


def _playbook_replay_command(filename, script, flags, *, legacy=True):
    runner = "_run_legacy_playbook" if legacy else "_run_modern_playbook"
    command = _reference_python(
        f"_parity_bootstrap[{runner!r}](sys.argv.pop(1), sys.argv.pop(1))",
        filename,
    )
    if getattr(sys.flags, "safe_path", False) and "-P" not in flags:
        flags = (*flags, "-P")
    command[1:1] = flags
    return command + [script]


def _has_brace_expansion(path):
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
    return (
        re.fullmatch(r"(?:python|pypy)[0-9.]*t?d?", os.path.basename(executable)) is not None
        or (os.path.isabs(executable) and os.path.realpath(executable) == os.path.realpath(sys.executable))
    )


def _python_environment(executable):
    # Resolve directory aliases without following the Python symlink out of
    # its virtual environment. CPython checks both directories for the config.
    directory = os.path.realpath(os.path.dirname(os.path.abspath(executable)))
    for parent in (directory, os.path.dirname(directory)):
        if os.path.isfile(os.path.join(parent, "pyvenv.cfg")):
            return parent
    return None


def _selected_python_matches(executable):
    return (
        executable is not None
        and os.path.realpath(executable) == os.path.realpath(sys.executable)
        and _python_environment(executable) == _python_environment(sys.executable)
    )


def _launcher_body(source, *, match_script=None):
    if not source.startswith("#!"):
        return source
    shebang, _, body = source.partition("\n")
    command = shebang[2:].strip(" \t")
    if command != "/bin/sh":
        # Direct shebangs may choose Python, but replay cannot reproduce
        # interpreter flags or environment changes made by the launcher.
        env_python = re.fullmatch(r"/(?:usr/)?bin/env[ \t]+(?:-S[ \t]+)?([^/ \t]+)", command)
        if env_python and _is_python_interpreter(env_python[1]):
            if match_script is not None and not _selected_python_matches(shutil.which(env_python[1])):
                return source
            return body
        if re.fullmatch(r"/[^ \x00-\x1f\x7f]+", command) and _is_python_interpreter(command):
            if match_script is not None and not _selected_python_matches(command):
                return source
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
        if match_script is not None:
            interpreter_path = os.path.join(os.path.dirname(os.path.realpath(match_script)), executable)
            if not _selected_python_matches(interpreter_path):
                return source
        return remaining.partition("\n")[2]
    return source


def _launcher_body_and_flags(source, *, match_script=None):
    shebang, newline, code = source.partition("\n")
    match = re.fullmatch(
        r"#![ \t]*(?:(?P<env>/(?:usr/)?bin/env[ \t]+-S[ \t]+[^ \t]+)|(?P<direct>/[^ \t]+))"
        r"(?P<flags>(?:[ \t]+-(?:sP|Ps|s|P))+)[ \t]*",
        shebang,
    )
    flags = ()
    if match:
        options = match["flags"].split()
        if len(options) > 1 and not match["env"]:
            return None, ()
        flags = tuple(dict.fromkeys(f"-{option}" for token in options for option in token[1:]))
        source = f"#!{match['env'] or match['direct']}{newline}{code}"
    body = _launcher_body(source, match_script=match_script)
    if source.startswith("#!") and body == source:
        return None, ()
    return body, flags


def _legacy_python_launcher(path, package, diagnostics, associated):
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

    body, flags = _launcher_body_and_flags(source)
    if body is None:
        diagnostics.append("%s: launcher shebang cannot be replayed" % path)
        return None

    # The selected Ansible installation ships its own release's launcher.
    stub = os.path.join(package, "cli", "scripts", "ansible_cli_stub.py")
    try:
        with tokenize.open(stub) as expected:
            if body == _launcher_body(expected.read()):
                # The pinned process will execute the same startup code; a
                # separate --version run cannot add evidence of provenance.
                return path, flags
        diagnostics.append("%s: launcher differs from the selected Ansible release" % path)
        return None
    except FileNotFoundError:
        # An install record or source checkout already associates this script
        # with the pinned package. Replay verifies the package before startup.
        if associated:
            return path, flags
        diagnostics.append("%s: selected Ansible has no launcher source to verify this script" % path)
        return None
    except (OSError, SyntaxError, UnicodeError) as ex:
        diagnostics.append("%s: %s" % (stub, ex))
        return None


def _installed_playbook_launchers(package):
    from importlib import metadata

    # The pinned package may be outside the subprocess's import path.
    search_path = list(dict.fromkeys([*sys.path, os.path.dirname(package)]))
    for name in ("ansible-core", "ansible-base", "ansible"):
        for distribution in metadata.distributions(name=name, path=search_path):
            try:
                location = os.fspath(distribution.locate_file("ansible"))
            except TypeError:
                # Archive metadata cannot identify a filesystem launcher.
                continue
            if os.path.realpath(location) != package:
                continue
            for entry in distribution.files or ():
                if entry.name == "ansible-playbook" and "ansible_test" not in entry.parts:
                    yield str(distribution.locate_file(entry))

    # Source checkouts may expose ansible through PYTHONPATH without a record.
    library = os.path.dirname(package)
    if os.path.basename(library) == "lib":
        yield os.path.join(os.path.dirname(library), "bin", "ansible-playbook")


def _path_playbook_launchers():
    seen = set()
    for directory in os.get_exec_path():
        script = shutil.which("ansible-playbook", path=directory or os.curdir)
        if script:
            script = os.path.abspath(script)
            if script not in seen:
                seen.add(script)
                yield script


def _stock_playbook_flags(script, package, *, match_python=False):
    # These stock wrappers only dispatch to main(). A custom wrapper may
    # change the environment or CLI arguments before invoking Ansible.
    bodies = (
        "from ansible.cli.playbook import main\nmain()\n",
        "import sys\nfrom ansible.cli.playbook import main\n"
        "if __name__ == '__main__':\n"
        "    if sys.argv[0].endswith('-script.pyw'):\n"
        "        sys.argv[0] = sys.argv[0][:-11]\n"
        "    elif sys.argv[0].endswith('.exe'):\n"
        "        sys.argv[0] = sys.argv[0][:-4]\n"
        "    sys.exit(main())\n",
        "import re\nimport sys\nfrom ansible.cli.playbook import main\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
        "    sys.exit(main())\n",
        "import sys\nfrom ansible.cli.playbook import main\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = sys.argv[0].removesuffix('.exe')\n"
        "    sys.exit(main())\n",
    )
    try:
        with tokenize.open(script) as launcher:
            source = launcher.read()
        if match_python and not source.startswith("#!"):
            return None
        body, flags = _launcher_body_and_flags(source, match_script=script if match_python else None)
        if body is None:
            return None
        parsed = ast.dump(ast.parse(body))
    except (OSError, SyntaxError, UnicodeError):
        return None
    if any(parsed == ast.dump(ast.parse(body)) for body in bodies):
        return flags
    # A source checkout can expose cli/playbook.py itself as its launcher.
    try:
        with tokenize.open(os.path.join(package, "cli", "playbook.py")) as module:
            if parsed == ast.dump(ast.parse(module.read())):
                return flags
    except (OSError, SyntaxError, UnicodeError):
        pass
    return None


def _launcher_installation_matches(script, package):
    import sysconfig

    directory = os.path.dirname(os.path.realpath(script))
    prefix, scripts_dir = os.path.split(directory)
    if scripts_dir.lower() not in ("bin", "scripts"):
        return False
    prefix_vars = {"base": prefix, "platbase": prefix}
    scheme = {}
    if os.path.isfile(os.path.join(prefix, "pyvenv.cfg")) and "venv" in sysconfig.get_scheme_names():
        scheme = {"scheme": "venv"}
    sites = [sysconfig.get_path(kind, vars=prefix_vars.copy(), **scheme) for kind in ("purelib", "platlib")]
    if scripts_dir == "bin":
        # Debian's distro packages use an unversioned python3 directory.
        version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        sites.extend(os.path.join(prefix, "lib", name, "dist-packages") for name in (version, "python3"))
    return any(os.path.realpath(os.path.join(site, "ansible")) == package for site in sites)


def _matching_path_launcher(package, environment_script):
    # Check package ownership without starting another Python interpreter.
    path_launchers = (script for script in _path_playbook_launchers() if script != environment_script)
    for script in chain((environment_script,), path_launchers):
        if _launcher_installation_matches(script, package):
            flags = _stock_playbook_flags(script, package, match_python=True)
            if flags is not None:
                return script, flags
    return None, ()


def _run_modern_playbook(filename, script):
    if script:
        _set_playbook_launcher(script)
    else:
        sys.argv[0] = os.path.join(os.path.dirname(filename), "cli", "playbook.py")
    _load_reference_ansible(filename)
    from ansible.cli import playbook

    playbook.main()


def _select_playbook_launcher(filename, version):
    package = os.path.realpath(os.path.dirname(filename))
    installed_launchers = _installed_playbook_launchers(package)
    environment_script = os.path.join(os.path.dirname(sys.executable), "ansible-playbook")
    if tuple(map(int, version.split(".")[:2])) >= (2, 13):
        # The CLI looks for sibling helpers next to argv[0] before PATH.
        script = next((script for script in installed_launchers if os.path.isfile(script)), None)
        if script is None:
            script, flags = _matching_path_launcher(package, environment_script)
        else:
            flags = _stock_playbook_flags(script, package)
            if flags is None:
                raise RuntimeError(f"launcher initialization cannot be replayed: {script}")
        return script or "", flags, False

    # Older releases install a script instead of a module entry point.
    candidates = chain(
        ((script, True) for script in installed_launchers),
        [(environment_script, False)],
        ((script, False) for script in _path_playbook_launchers()),
    )
    diagnostics = []
    seen = set()
    for script, associated in candidates:
        if script and script not in seen and os.path.isfile(script):
            seen.add(script)
            verified = _legacy_python_launcher(script, package, diagnostics, associated)
            if verified:
                python_script, flags = verified
                return python_script, flags, True
    detail = "\n" + "\n".join(diagnostics) if diagnostics else ""
    raise FileNotFoundError("ansible-playbook script not found for %s%s" % (package, detail))


def _run_ansible_playbook(filename, version):
    script, flags, legacy = _select_playbook_launcher(filename, version)
    if "-P" in flags and not hasattr(sys.flags, "safe_path"):
        raise RuntimeError("the selected Python cannot reproduce the launcher's -P flag")
    command = _playbook_replay_command(filename, script, flags, legacy=legacy)
    os.execv(command[0], [*command, *sys.argv[1:]])
