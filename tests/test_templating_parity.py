"""Templating safety regressions, checked against real ansible-playbook.

Each playbook runs under ansible-playbook (linear) and as a compiled
standalone script; debug messages and the recap must match.
"""

import json
import os
import re
import subprocess
import sys

import pytest

from tests import ansible_playbook_command, ansiroll_compile_command, parity_pythonpath

pytest.importorskip("ansible")

from ansible import constants as C  # noqa: E402
from ansible.release import __version__  # noqa: E402
from packaging.version import Version

from ansiroll.compiler import compile_playbook  # noqa: E402
from ansiroll.loader import parse_playbook  # noqa: E402

MODERN_TEMPLATING = Version(__version__) >= Version("2.19")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECAP_RE = re.compile(
    r"ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)\s+"
    r"skipped=(\d+)\s+rescued=(\d+)\s+ignored=(\d+)"
)
MSG_RE = re.compile(r'^\s*"msg": (".*"),?\s*$', re.M)


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = parity_pythonpath(ROOT)
    env["ANSIBLE_COLLECTIONS_PATH"] = os.path.join(ROOT, "collections")
    env["ANSIBLE_DEPRECATION_WARNINGS"] = "False"
    env["ANSIBLE_NOCOLOR"] = "1"
    env.pop("ANSIBLE_STRATEGY", None)
    return env


def _summary(text, stderr=""):
    recap = RECAP_RE.search(text)
    assert recap, text + stderr
    return [json.loads(m) for m in MSG_RE.findall(text)], recap.groups()


def _ansible(playbook, cwd, strategy=None):
    env = _env()
    if strategy is not None:
        env["ANSIBLE_STRATEGY"] = strategy
    proc = subprocess.run(
        ansible_playbook_command() + ["-i", "localhost,", "-c", "local",
         "-e", json.dumps({"ansible_python_interpreter": sys.executable}), str(playbook)],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120,
    )
    return _summary(proc.stdout, proc.stderr)


def _ansiroll(playbook, cwd):
    src, _ = compile_playbook(str(playbook))
    script = cwd / "compiled.py"
    script.write_text(src)
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(cwd), env=_env(), capture_output=True, text=True,
    )
    return _summary(proc.stdout + "\n" + proc.stderr)


def _write(tmp_path, playbook, files=None):
    for name, text in (files or {}).items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    path = tmp_path / "play.yml"
    path.write_text(playbook)
    return path


def _assert_parity(tmp_path, playbook, files=None, use_strategy=False):
    path = _write(tmp_path, playbook, files)
    expected = _ansible(path, tmp_path)
    if use_strategy:
        actual = _ansible(path, tmp_path, strategy="ansiroll.core.ansiroll")
    else:
        actual = _ansiroll(path, tmp_path)
    assert actual == expected
    return expected


def test_reference_bootstrap_ignores_tests_module_in_playbook_directory(tmp_path):
    (tmp_path / "tests.py").write_text("raise RuntimeError('unexpected playbook-local import')\n")
    result = subprocess.run(
        ansible_playbook_command() + ["--version"], cwd=tmp_path, env=_env(),
        capture_output=True, text=True, timeout=30, check=True,
    )
    assert __version__ in result.stdout


@pytest.mark.parametrize("shadow_path", ["playbook", "symlink", "inherited"])
def test_reference_bootstrap_uses_stdlib_runpy(tmp_path, monkeypatch, shadow_path):
    playbook_dir = tmp_path / "playbook"
    playbook_dir.mkdir()
    shadow_dir = tmp_path / "dependencies" if shadow_path == "inherited" else playbook_dir
    shadow_dir.mkdir(exist_ok=True)
    (shadow_dir / "runpy.py").write_text("raise RuntimeError('unexpected runpy from PYTHONPATH')\n")
    import_path = shadow_dir
    if shadow_path == "symlink":
        import_path = tmp_path / "playbook-link"
        try:
            import_path.symlink_to(playbook_dir, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"directory symlinks unavailable: {exc}")
    monkeypatch.setenv("PYTHONPATH", str(import_path), prepend=os.pathsep)
    command = ansible_playbook_command()
    if sys.version_info >= (3, 11):
        command[1:1] = ["-X", "frozen_modules=off"]
    result = subprocess.run(
        command + ["--version"], cwd=playbook_dir, env=_env(),
        capture_output=True, text=True, timeout=30, check=True,
    )
    assert __version__ in result.stdout


@pytest.mark.parametrize("module", ["csv", "_csv"])
def test_reference_launcher_discovery_uses_stdlib_csv(tmp_path, monkeypatch, module):
    from tests import _reference_python

    (tmp_path / f"{module}.py").write_text("raise RuntimeError('unexpected playbook-local CSV module')\n")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path), prepend=os.pathsep)
    command = _reference_python(
        "script, _, _ = _parity_bootstrap['_select_playbook_launcher'](sys.argv.pop(1), sys.argv.pop(1)); "
        "print(script)",
        discover=True,
    )
    result = subprocess.run(
        command + [__version__], cwd=tmp_path, env=_env(),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stdout.strip() or os.path.isfile(result.stdout.strip())


def test_reference_launcher_ignores_archived_distribution_metadata(tmp_path, monkeypatch):
    from zipfile import ZipFile

    archive = tmp_path / "foreign.zip"
    with ZipFile(archive, "w") as zipped:
        zipped.writestr("ansible_core-0.0.dist-info/METADATA", "Name: ansible-core\nVersion: 0.0\n")
    monkeypatch.setenv("PYTHONPATH", str(archive), prepend=os.pathsep)
    result = subprocess.run(
        ansible_playbook_command() + ["--version"], cwd=tmp_path, env=_env(),
        capture_output=True, text=True, timeout=30, check=True,
    )
    assert __version__ in result.stdout


def test_reference_bootstrap_preserves_playbook_local_imports(tmp_path, monkeypatch):
    from tests import _reference_python

    monkeypatch.delenv("PYTHONPATH", raising=False)
    (tmp_path / "tests.py").write_text("raise RuntimeError('unexpected playbook-local tests module')\n")
    (tmp_path / "parity_playbook_helper.py").write_text("SOURCE = 'playbook'\n")
    command = _reference_python(
        "_parity_bootstrap['_load_reference_ansible'](sys.argv.pop(1)); "
        "from parity_playbook_helper import SOURCE; print(SOURCE)"
    )
    result = subprocess.run(
        command, cwd=tmp_path, env=_env(), capture_output=True, text=True, timeout=10, check=True,
    )
    assert result.stdout.strip() == "playbook"


def test_reference_bootstrap_rejects_a_cached_foreign_ansible_submodule(tmp_path):
    expected = tmp_path / "expected" / "ansible" / "__init__.py"
    foreign = tmp_path / "foreign" / "ansible" / "release.py"
    command = (
        "import sys, types; from tests import _load_reference_ansible; "
        "module = types.ModuleType('ansible.release'); module.__file__ = sys.argv[2]; "
        "sys.modules[module.__name__] = module; _load_reference_ansible(sys.argv[1])"
    )
    result = subprocess.run(
        [sys.executable, "-c", command, str(expected), str(foreign)],
        cwd=tmp_path, env=_env(), capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "cached ansible.release" in result.stderr
    assert str(foreign) in result.stderr and str(expected.parent) in result.stderr


@pytest.mark.parametrize("alias, external", [
    ("ansible.compat.selectors", "selectors"),
    ("ansible.module_utils.compat.selectors", "selectors"),
    ("ansible.module_utils.distro", "distro"),
])
def test_reference_bootstrap_accepts_ansible_compatibility_aliases(tmp_path, alias, external):
    import ansible

    command = (
        "import selectors, sys, types; from tests import _load_reference_ansible; "
        "_load_reference_ansible(sys.argv[1]); "
        "module = sys.modules.get(sys.argv[3]) or types.ModuleType(sys.argv[3]); "
        "module.__file__ = getattr(module, '__file__', selectors.__file__); "
        "sys.modules[sys.argv[2]] = sys.modules[sys.argv[3]] = module; "
        "_load_reference_ansible(sys.argv[1])"
    )
    subprocess.run(
        [sys.executable, "-c", command, os.path.abspath(ansible.__file__), alias, external],
        cwd=tmp_path, env=_env(),
        capture_output=True, text=True, timeout=10, check=True,
    )


@pytest.mark.parametrize("replace_alias", [False, True])
def test_reference_bootstrap_verifies_six_alias_identity(tmp_path, replace_alias):
    import ansible
    from pathlib import Path

    if not Path(ansible.__file__).with_name("module_utils").joinpath("six", "__init__.py").is_file():
        pytest.skip("Ansible does not ship vendored six")
    alias = "ansible.module_utils.six.moves.http_cookiejar"
    command = (
        "import http.cookiejar, importlib, sys, types; from tests import _load_reference_ansible; "
        "_load_reference_ansible(sys.argv[1]); "
        "module = importlib.import_module(sys.argv[2]); assert module is http.cookiejar; "
        "foreign = types.ModuleType(sys.argv[2]); foreign.__file__ = module.__file__; "
        "sys.modules[sys.argv[2]] = foreign if sys.argv[3] == 'replace' else module; "
        "_load_reference_ansible(sys.argv[1])"
    )
    result = subprocess.run(
        [sys.executable, "-c", command, os.path.abspath(ansible.__file__), alias,
         "replace" if replace_alias else "keep"],
        cwd=tmp_path, env=_env(), capture_output=True, text=True, timeout=10,
    )
    if replace_alias:
        assert result.returncode != 0
        assert f"cached {alias} came from" in result.stderr
    else:
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("version", ["2.12.10", "2.14.18"])
def test_reference_playbook_selects_the_matching_installed_launcher_lazily(tmp_path, monkeypatch, version):
    from importlib import metadata
    from pathlib import Path
    from types import ModuleType, SimpleNamespace

    import tests

    launcher = Path("bin/ansible-playbook")
    (tmp_path / launcher).parent.mkdir()
    (tmp_path / launcher).write_text("#!/usr/bin/env python3\nfrom ansible.cli.playbook import main\nmain()\n")
    ansible_package = str(tmp_path / "ansible")

    def distributions(name, path):
        assert name == "ansible-core"
        assert str(tmp_path) in path
        # Its records must not be accessed, because it is a different install.
        yield SimpleNamespace(locate_file=lambda entry: tmp_path / "foreign" / entry)
        yield SimpleNamespace(files=[launcher], locate_file=lambda entry: tmp_path / entry)
        pytest.fail("searched later distributions after selecting the pinned launcher")

    def verify_legacy(script, package, diagnostics, associated):
        assert package == ansible_package and associated
        return script, (), True

    seen = []
    cli = ModuleType("ansible.cli")
    cli.playbook = SimpleNamespace(main=lambda: seen.extend(sys.argv))
    monkeypatch.setattr(tests, "_load_reference_ansible", lambda filename: ansible_package)
    monkeypatch.setattr(tests, "_legacy_python_launcher", verify_legacy)
    monkeypatch.setattr(tests, "_run_legacy_playbook", lambda filename, script: seen.extend([script, *sys.argv[1:]]))
    monkeypatch.setattr(metadata, "distributions", distributions)
    monkeypatch.setitem(sys.modules, "ansible.cli", cli)
    monkeypatch.setattr(sys, "argv", ["-c", "--version"])
    monkeypatch.setattr(sys, "path", sys.path[:])
    monkeypatch.setenv("PATH", "")
    filename = str(tmp_path / "ansible" / "__init__.py")
    script, flags, legacy = tests._select_playbook_launcher(filename, version)
    assert not flags
    runner = tests._run_legacy_playbook if legacy else tests._run_modern_playbook
    runner(filename, script)
    assert seen == [str(tmp_path / launcher), "--version"]


@pytest.mark.parametrize("match_location", [None, "path", "sibling", "wrong-python"])
def test_modern_launcher_verifies_fallback_installations(tmp_path, monkeypatch, match_location):
    from types import ModuleType, SimpleNamespace

    import tests

    package_dir = tmp_path / "matching" / "lib" / "python3" / "dist-packages" / "ansible"
    package_dir.mkdir(parents=True)
    package = str(package_dir)
    foreign = str(tmp_path / "foreign" / "bin" / "ansible-playbook")
    matching = str(tmp_path / "matching" / "bin" / "ansible-playbook")
    wrapper = str(tmp_path / "matching" / "bin" / "custom-ansible-playbook")
    sibling = matching if match_location == "sibling" else foreign
    selected_python = os.path.join(os.path.dirname(sibling), "python")
    seen = []
    for path in (foreign, matching, wrapper):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        interpreter = "/other/bin/python3 -P" if path == matching and match_location == "wrong-python" else selected_python
        source = f"#!{interpreter}\nfrom ansible.cli.playbook import main\nmain()\n"
        if path == wrapper:
            source = source.replace("main()\n", "import os\nos.environ['ANSIBLE_CONFIG'] = '/other.cfg'\nmain()\n")
        with open(path, "w") as script:
            script.write(source)

    cli = ModuleType("ansible.cli")
    cli.playbook = SimpleNamespace(main=lambda: seen.append(sys.argv[0]))
    monkeypatch.setattr(sys, "executable", selected_python)
    monkeypatch.setitem(sys.modules, "ansible.cli", cli)
    monkeypatch.setattr(sys, "argv", ["-c", "--version"])
    monkeypatch.setattr(sys, "path", sys.path[:])
    monkeypatch.setattr(tests, "_installed_playbook_launchers", lambda package: iter(()))
    path = [wrapper, foreign, matching] if match_location in ("path", "wrong-python") else [wrapper, foreign]
    monkeypatch.setattr(tests, "_path_playbook_launchers", lambda: iter(path))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("launcher was probed"))
    monkeypatch.setattr(tests, "_load_reference_ansible", lambda filename: package)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    filename = os.path.join(package, "__init__.py")
    script, flags, legacy = tests._select_playbook_launcher(filename, "2.14.18")
    assert not flags and not legacy
    tests._run_modern_playbook(filename, script)
    if match_location in ("path", "sibling"):
        assert seen == [matching]
    else:
        assert seen == [os.path.join(package, "cli", "playbook.py")]
        assert os.path.dirname(foreign) not in sys.path


@pytest.mark.parametrize("venv", [False, True])
def test_fallback_installation_uses_the_candidate_scheme_and_abi(tmp_path, monkeypatch, venv):
    import sysconfig

    from tests import _launcher_installation_matches

    package = tmp_path / "lib" / "python3.14t" / "site-packages" / "ansible"
    package.mkdir(parents=True)
    if venv:
        (tmp_path / "pyvenv.cfg").touch()

    def install_path(kind, vars, **kwargs):
        assert kind in ("purelib", "platlib")
        assert vars == {"base": str(tmp_path), "platbase": str(tmp_path)}
        assert kwargs == ({"scheme": "venv"} if venv else {})
        return str(package.parent)

    monkeypatch.setattr(sysconfig, "get_scheme_names", lambda: ("posix_local", "venv"))
    monkeypatch.setattr(sysconfig, "get_path", install_path)
    assert _launcher_installation_matches(tmp_path / "bin" / "ansible-playbook", str(package))


@pytest.mark.parametrize("config_beside_python", [False, True])
def test_fallback_distinguishes_venvs_that_share_the_python_binary(tmp_path, monkeypatch, config_beside_python):
    from tests import _stock_playbook_flags

    system = tmp_path / "system" / "bin" / "python3"
    system.parent.mkdir(parents=True)
    system.touch()
    executables = []
    for name in ("selected", "foreign"):
        root = tmp_path / ("shared" if config_beside_python else name)
        bin_dir = f"{name}-bin" if config_beside_python else "bin"
        executable = root / bin_dir / "python3"
        executable.parent.mkdir(parents=True)
        config_dir = executable.parent if config_beside_python else root
        (config_dir / "pyvenv.cfg").touch()
        try:
            executable.symlink_to(system)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
        executables.append(executable)
    selected, foreign = executables
    monkeypatch.setattr(sys, "executable", str(selected))
    script = selected.with_name("ansible-playbook")
    body = "from ansible.cli.playbook import main\nmain()\n"
    script.write_text(f"#!{foreign} -s\n{body}")
    assert _stock_playbook_flags(script, str(tmp_path), match_python=True) is None
    script.write_text(f"#!{selected} -s\n{body}")
    assert _stock_playbook_flags(script, str(tmp_path), match_python=True) == ("-s",)
    alias = tmp_path / "alias" / "bin"
    alias.parent.mkdir()
    alias.symlink_to(selected.parent, target_is_directory=True)
    script.write_text(f"#!{alias / 'python3'} -s\n{body}")
    assert _stock_playbook_flags(script, str(tmp_path), match_python=True) is None


@pytest.mark.parametrize("has_matching_python", [False, True])
def test_legacy_launcher_prefers_matching_python_and_ignores_foreign_flags(tmp_path, monkeypatch, has_matching_python):
    import tests

    package = tmp_path / "ansible"
    stub = package / "cli" / "scripts" / "ansible_cli_stub.py"
    stub.parent.mkdir(parents=True)
    body = "start_expected_release()\n"
    stub.write_text(f"#!/usr/bin/env python\n{body}")
    selected = tmp_path / "selected" / "python3"
    monkeypatch.setattr(sys, "executable", str(selected))
    launches = []
    for name, interpreter in [("foreign-flags", "/other/bin/python3 -s"),
                              ("foreign-plain", "/other/bin/python3"), ("matching", selected)]:
        script = tmp_path / name / "ansible-playbook"
        script.parent.mkdir()
        script.write_text(f"#!{interpreter}\n{body}")
        launches.append(str(script))
    monkeypatch.setattr(tests, "_installed_playbook_launchers", lambda package: iter(()))
    candidates = launches if has_matching_python else launches[:2]
    monkeypatch.setattr(tests, "_path_playbook_launchers", lambda: iter(candidates))
    expected = launches[2] if has_matching_python else launches[1]
    assert tests._select_playbook_launcher(str(package / "__init__.py"), "2.12.10") == (expected, (), True)


def test_modern_launcher_accepts_pip_entrypoint(tmp_path):
    from tests import _stock_playbook_flags

    script = tmp_path / "ansible-playbook"
    script.write_text(
        "#!/usr/bin/python3\nimport sys\nfrom ansible.cli.playbook import main\n"
        "if __name__ == '__main__':\n"
        "    sys.argv[0] = sys.argv[0].removesuffix('.exe')\n"
        "    sys.exit(main())\n"
    )
    assert _stock_playbook_flags(str(script), str(tmp_path / "ansible")) == ()


@pytest.mark.parametrize("customized", [False, True])
def test_modern_launcher_recognizes_the_pinned_source_module(tmp_path, customized):
    from tests import _stock_playbook_flags

    package = tmp_path / "ansible"
    module = package / "cli" / "playbook.py"
    module.parent.mkdir(parents=True)
    source = "#!/usr/bin/env python3\ndef main():\n    pass\nif __name__ == '__main__':\n    main()\n"
    module.write_text(source)
    script = tmp_path / "ansible-playbook"
    script.write_text(source + ("set_custom_config()\n" if customized else ""))
    assert _stock_playbook_flags(str(script), str(package)) == (None if customized else ())


def test_modern_launcher_rejects_recorded_wrapper_initialization(tmp_path, monkeypatch):
    import tests

    wrapper = tmp_path / "ansible-playbook"
    wrapper.write_text("#!/usr/bin/env python3\nimport os\nos.environ['ANSIBLE_CONFIG'] = '/other.cfg'\n")
    monkeypatch.setattr(tests, "_installed_playbook_launchers", lambda package: iter([str(wrapper)]))
    with pytest.raises(RuntimeError, match="launcher initialization cannot be replayed"):
        tests._select_playbook_launcher(str(tmp_path / "ansible" / "__init__.py"), "2.14.18")


@pytest.mark.parametrize("version", ["2.12.10", "2.14.18"])
def test_recorded_launcher_does_not_transfer_flags_from_foreign_python(tmp_path, monkeypatch, version):
    import tests

    launcher = tmp_path / "ansible-playbook"
    launcher.write_text("#!/other/bin/python3 -s\nfrom ansible.cli.playbook import main\nmain()\n")
    monkeypatch.setattr(tests, "_installed_playbook_launchers", lambda package: iter([str(launcher)]))
    monkeypatch.setattr(tests, "_path_playbook_launchers", lambda: iter(()))
    monkeypatch.setattr(sys, "executable", str(tmp_path / "selected" / "python3"))
    filename = str(tmp_path / "ansible" / "__init__.py")
    if version == "2.12.10":
        with pytest.raises(FileNotFoundError, match="cannot replay flags from a different Python interpreter"):
            tests._select_playbook_launcher(filename, version)
    else:
        assert tests._select_playbook_launcher(filename, version) == ("", (), False)


@pytest.mark.parametrize("version", ["2.12.10", "2.14.18"])
@pytest.mark.parametrize("no_user_site", [False, True])
@pytest.mark.parametrize("safe_path", [None, "environment", "shebang", "env-shebang"])
def test_launcher_import_order_matches_safe_path(tmp_path, monkeypatch, version, no_user_site, safe_path):
    from tests import _reference_playbook_command

    if safe_path and not hasattr(sys.flags, "safe_path"):
        pytest.skip("Python does not support safe-path mode")
    # Virtual environments can disable usercustomize even without -s.
    monkeypatch.setattr(sys, "executable", os.path.realpath(sys._base_executable))
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable), prepend=os.pathsep)
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)
    monkeypatch.delenv("ANSIROLL_TEST_USERCUSTOMIZE", raising=False)
    monkeypatch.delenv("ANSIROLL_TEST_SITECUSTOMIZE", raising=False)
    if safe_path == "environment":
        monkeypatch.setenv("PYTHONSAFEPATH", "1")
    source = "inherited" if safe_path else "launcher"
    package = tmp_path / "lib" / "ansible"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "import csv, os, site, sys\nfrom parity_legacy_dependency import SOURCE\n"
        "assert csv.PARITY_TEST_MODULE\n"
        f"assert bool(sys.flags.no_user_site) is {no_user_site!r}\n"
        "assert ('ANSIROLL_TEST_USERCUSTOMIZE' in os.environ) is bool(site.ENABLE_USER_SITE)\n"
        "assert os.environ['ANSIROLL_TEST_SITECUSTOMIZE'] == '1'\n"
    )
    (package / "cli").mkdir()
    (package / "cli" / "playbook.py").write_text(
        "import ansible, sys\ndef main():\n"
        f"    assert not {no_user_site!r} or sys.flags.no_user_site\n"
        "    print(ansible.SOURCE)\n"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "parity_legacy_dependency.py").write_text("SOURCE = 'launcher'\n")
    options = ("s" if no_user_site else "") + ("P" if safe_path == "shebang" else "")
    if safe_path == "env-shebang":
        shebang = "#!/usr/bin/env -S python3 -s -P" if no_user_site else "#!/usr/bin/env -S python3 -P"
    else:
        shebang = f"#!{sys.executable} -{options}" if options else "#!/usr/bin/env python"
    legacy_body = (
        "import os, sys, ansible\n"
        f"assert ansible.SOURCE == {source!r}, ansible.SOURCE\n"
        f"assert not {no_user_site!r} or sys.flags.no_user_site\n"
        "print('ansible python module location = ' + os.path.dirname(ansible.__file__))\n"
    )
    body = legacy_body if version == "2.12.10" else "from ansible.cli.playbook import main\nmain()\n"
    (bin_dir / "ansible-playbook").write_text(f"{shebang}\n{body}")
    (tmp_path / "parity_legacy_dependency.py").write_text("SOURCE = 'inherited'\n")
    (tmp_path / "csv.py").write_text("PARITY_TEST_MODULE = True\n")
    (tmp_path / "usercustomize.py").write_text("import os\nos.environ['ANSIROLL_TEST_USERCUSTOMIZE'] = '1'\n")
    (tmp_path / "sitecustomize.py").write_text(
        "import os\n"
        "key = 'ANSIROLL_TEST_SITECUSTOMIZE'\n"
        "os.environ[key] = str(int(os.environ.get(key, '0')) + 1)\n"
        "with open('startup.log', 'a') as log:\n"
        "    log.write('sitecustomize\\n')\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path), prepend=os.pathsep)
    command = _reference_playbook_command(str(package / "__init__.py"), version)
    result = subprocess.run(
        command + ["--version"], cwd=tmp_path, env=_env(),
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    expected = f"ansible python module location = {package}" if version == "2.12.10" else source
    assert result.stdout.strip() == expected
    assert (tmp_path / "startup.log").read_text().splitlines() == ["sitecustomize"]


def test_subprocess_compile_pins_the_pytest_ansible_package(tmp_path, monkeypatch):
    foreign = tmp_path / "foreign"
    package = foreign / "ansible"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("raise RuntimeError('wrong Ansible selected')\n")
    monkeypatch.setenv("PYTHONPATH", str(foreign), prepend=os.pathsep)
    playbook = _write(tmp_path, "- hosts: localhost\n  gather_facts: false\n")
    output = tmp_path / "compiled.py"
    result = subprocess.run(
        ansiroll_compile_command() + [str(playbook), "-o", str(output)],
        cwd=tmp_path, env=_env(), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert output.is_file()


@pytest.mark.parametrize("reference", [False, True])
def test_parity_subprocess_imports_playbook_local_filter_helpers(tmp_path, monkeypatch, reference):
    dependencies = tmp_path / "dependencies"
    dependencies.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(dependencies), prepend=os.pathsep)
    monkeypatch.delenv("PYTHONSAFEPATH", raising=False)
    monkeypatch.setenv("ANSIBLE_FILTER_PLUGINS", str(tmp_path / "filter_plugins"))
    filter_imports = ""
    if reference:
        filter_imports = (
            "import sys\nfrom pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
        )
    playbook = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - import_tasks: "{{ 'included' | parity_task_file }}"
""", {
        "tests.py": "def task_file(name):\n    return name + '.yml'\n",
        "filter_plugins/parity_filters.py": (
            filter_imports + "from tests import task_file\n"
            "class FilterModule:\n"
            "    def filters(self):\n"
            "        return {'parity_task_file': task_file}\n"
        ),
        "included.yml": "- debug: msg=selected-from-playbook\n",
    })
    output = tmp_path / "compiled.py"
    if reference:
        command = ansible_playbook_command() + ["-i", "localhost,", str(playbook)]
    else:
        command = ansiroll_compile_command() + [str(playbook), "-o", str(output)]
    result = subprocess.run(
        command, cwd=tmp_path, env=_env(), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    if reference:
        assert "selected-from-playbook" in result.stdout
    else:
        assert output.is_file()


def test_legacy_launcher_reports_unsupported_wrapper(tmp_path):
    from tests import _legacy_python_launcher

    wrapper = tmp_path / "ansible-playbook"
    wrapper.write_text("#!/bin/sh\nexport ANSIBLE_CONFIG=/some/config\nexec ansible-playbook \"$@\"\n")
    diagnostics = []
    assert _legacy_python_launcher(str(wrapper), "", diagnostics, associated=True) is None
    assert "cannot replay a non-Python launcher's initialization" in diagnostics[0]


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("prefix", [
    "#!/usr/bin/python3\n",
    "#! /usr/bin/env python3\n",
    "#!/usr/bin/env -S python3\n",
    "#!/bin/sh\n'''exec' '/path with spaces/python3' \"$0\" \"$@\"\n' '''\n",
    "#!/bin/sh\n'''exec' '/user'\"'\"'s env/python3' \"$0\" \"$@\"\n' '''\n",
    "#!/bin/sh\n'''exec' \"/path with spaces/python3\" \"$0\" \"$@\"\n' '''\n",
    ("#!/bin/sh\n'''exec' /workspace/team@2/" + "long-component/" * 8
     + "venv/bin/python3 \"$0\" \"$@\"\n' '''\n"),
    ("#!/bin/sh\n'''exec' /workspace/team,2=parity:linux#1/" + "long-component/" * 8
     + "venv/bin/python3 \"$0\" \"$@\"\n' '''\n"),
    ("#!/bin/sh\n'''exec' /workspace/team\u3000two/" + "long-component/" * 8
     + "venv/bin/python3 \"$0\" \"$@\"\n' '''\n"),
    ("#!/bin/sh\n'''exec' /workspace/team[draft/" + "long-component/" * 8
     + "venv/bin/python3 \"$0\" \"$@\"\n' '''\n"),
    ("#!/bin/sh\n'''exec' /workspace/team{draft}/" + "long-component/" * 8
     + "venv/bin/python3.10d \"$0\" \"$@\"\n' '''\n"),
    "#!/bin/sh\n'''exec' \"$(dirname -- \"$(realpath -- \"$0\")\")\"/'python' \"$0\" \"$@\"\n' '''\n",
])
def test_legacy_fallback_verifies_launcher_against_selected_package(tmp_path, stale, prefix):
    import tests

    package = tmp_path / "ansible"
    scripts = package / "cli" / "scripts"
    scripts.mkdir(parents=True)
    stub = scripts / "ansible_cli_stub.py"
    stub.write_text("#!/usr/bin/env python\nstart_expected_release()\n")
    launcher = tmp_path / "ansible-playbook"
    startup = "start_stale_release()" if stale else "start_expected_release()"
    launcher.write_text(prefix + startup + "\n")
    diagnostics = []
    found = tests._legacy_python_launcher(
        str(launcher), str(package), diagnostics, associated=False)
    if stale:
        assert found is None
        assert "differs from the selected Ansible release" in diagnostics[0]
    else:
        assert found is not None and found[:2] == (str(launcher), ())


@pytest.mark.parametrize("interpreter", [
    "ANSIBLE_CONFIG=/custom /usr/bin/python3",
    "/workspace/team two/bin/python3",
    "/workspace/team\ttwo/bin/python3",
    "/workspace/$TEAM/bin/python3",
    "/workspace/{old,new}/bin/python3",
    "/workspace/{old{draft},new}/bin/python3",
    "/workspace/team{-1..+3..+2}/bin/python3",
    "/workspace/team[12]/bin/python3",
    "/workspace/team[^]/bin/python3",
    "/workspace/team[]]/bin/python3",
    "/workspace/team?2/bin/python3",
    "/workspace/team*2/bin/python3",
    "/workspace/team;other/bin/python3",
])
def test_launcher_body_keeps_unrecognized_shell_initialization(monkeypatch, interpreter):
    import tests

    monkeypatch.setattr(tests, "_shell_preserves_braces", lambda: False)
    monkeypatch.setattr(tests, "_shell_preserves_path_pattern", lambda path: False)
    wrapper = (
        f"#!/bin/sh\n'''exec' {interpreter} \"$0\" \"$@\"\n"
        "' '''\nstart_expected_release()\n"
    )
    assert tests._launcher_body(wrapper) == wrapper


def test_launcher_body_accepts_the_current_interpreter_under_a_custom_name(tmp_path, monkeypatch):
    from tests import _launcher_body

    executable = tmp_path / "custom-cpython"
    monkeypatch.setattr(sys, "executable", str(executable))
    body = "start_expected_release()\n"
    wrapper = f"#!/bin/sh\n'''exec' '{executable}' \"$0\" \"$@\"\n' '''\n{body}"
    assert _launcher_body(wrapper) == body


@pytest.mark.parametrize("literal_braces", [True, False])
@pytest.mark.parametrize("matching_python", [True, False])
def test_legacy_launcher_with_brace_alternatives_requires_a_literal_shell(
    monkeypatch, literal_braces, matching_python,
):
    import tests

    executable = "/workspace/team{dev,prod}/" + "long-component/" * 8 + "venv/bin/python3"
    monkeypatch.setattr(sys, "executable", executable if matching_python else "/other/bin/python3")
    monkeypatch.setattr(tests, "_shell_preserves_braces", lambda: literal_braces)
    body = "start_expected_release()\n"
    wrapper = f"#!/bin/sh\n'''exec' {executable} \"$0\" \"$@\"\n' '''\n{body}"
    assert tests._launcher_body(wrapper) == (body if literal_braces else wrapper)


@pytest.mark.skipif(not os.path.isfile("/bin/sh"), reason="requires a POSIX shell")
@pytest.mark.parametrize("dirname", ["team[12]", "team*", "team?"])
@pytest.mark.parametrize("alternative_exists", [True, False])
def test_shell_probe_checks_whether_pathname_patterns_stay_literal(tmp_path, monkeypatch, dirname, alternative_exists):
    from tests import _shell_preserves_path_pattern

    monkeypatch.chdir(tmp_path)
    executable = tmp_path / dirname / "python3"
    executable.parent.mkdir()
    executable.touch()
    if alternative_exists:
        alternative = tmp_path / "team1" / "python3"
        alternative.parent.mkdir()
        alternative.touch()
    assert _shell_preserves_path_pattern(f"{dirname}/python3") is not alternative_exists


@pytest.mark.parametrize("stays_literal", [True, False])
def test_legacy_launcher_uses_the_shell_probe_for_pathname_patterns(monkeypatch, stays_literal):
    import tests

    executable = "/workspace/team[12]/venv/bin/python3"
    probes = []

    def check_pattern(path):
        probes.append(path)
        return stays_literal

    monkeypatch.setattr(tests, "_shell_preserves_path_pattern", check_pattern)
    body = "start_expected_release()\n"
    wrapper = f"#!/bin/sh\n'''exec' {executable} \"$0\" \"$@\"\n' '''\n{body}"
    assert tests._launcher_body(wrapper) == (body if stays_literal else wrapper)
    assert probes == [executable]


@pytest.mark.parametrize("stub_available", [True, False])
@pytest.mark.parametrize("shebang", [
    "#!/usr/bin/env -S ANSIBLE_CONFIG=/custom/ansible.cfg python3",
    "#!/usr/bin/env -S python3 -I",
    "#!/usr/bin/python3 -I",
])
def test_legacy_launcher_rejects_shebang_settings_before_replay(
    tmp_path, stub_available, shebang,
):
    import tests

    package = tmp_path / "ansible"
    if stub_available:
        scripts = package / "cli" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "ansible_cli_stub.py").write_text("#!/usr/bin/env python\nstart_expected_release()\n")
    launcher = tmp_path / "ansible-playbook"
    launcher.write_text(shebang + "\nstart_expected_release()\n")
    diagnostics = []
    assert tests._legacy_python_launcher(str(launcher), str(package), diagnostics, associated=True) is None
    assert "launcher shebang cannot be replayed" in diagnostics[0]


@pytest.mark.parametrize("relative_path", [False, True])
def test_pinning_reference_ansible_preserves_dependency_lookup_order(tmp_path, monkeypatch, relative_path):
    checkout = tmp_path / "checkout"
    package = checkout / "ansible"
    package.mkdir(parents=True)
    init = package / "__init__.py"
    init.write_text("from parity_dependency import SOURCE\n")
    (checkout / "parity_dependency.py").write_text("SOURCE = 'checkout'\n")
    dependencies = tmp_path / "dependencies"
    dependencies.mkdir()
    (dependencies / "parity_dependency.py").write_text("SOURCE = 'inherited'\n")
    monkeypatch.setenv("PYTHONPATH", dependencies.name if relative_path else str(dependencies))

    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; from tests import _load_reference_ansible; "
         "_load_reference_ansible(sys.argv[1]); import ansible; print(ansible.SOURCE)",
         str(init)],
        cwd=tmp_path, env=_env(), capture_output=True, text=True, timeout=10, check=True,
    )
    assert result.stdout.strip() == "inherited"


def test_untrusted_data_is_not_templated_again(tmp_path):
    msgs, _ = _assert_parity(tmp_path, r"""
- hosts: localhost
  gather_facts: false
  vars:
    base: BASE
    raw_var: "{% raw %}{{ base }}{% endraw %}"
    unsafe_var: !unsafe "{{ base }}"
    chain1: "{{ chain2 }}"
    chain2: "{{ base }}-2"
    nested_list: ["{{ base }}", "{{ chain1 }}"]
  tasks:
    - command: printf '%s' '{{ "{{" }} base }}'
      register: out
      changed_when: false
    - debug: msg="{{ out.stdout }}"
    - debug: msg="{{ raw_var }}"
    - debug: msg="{{ unsafe_var }}"
    - debug: msg="{{ lookup('file', 'data.txt') }}"
    - debug: msg="{{ '{{ base }}' }}"
    - debug: msg="{{ item }}"
      loop: ["{{ '{{' }} base }}"]
    - debug: msg="{{ chain1 }}"
    - debug: msg="{{ nested_list | join(',') }}"
""", {"data.txt": "{{ base }}"})
    assert msgs[:5] == ["{{ base }}"] * 5
    assert msgs[5] == ("{{ base }}" if MODERN_TEMPLATING else "BASE")
    assert msgs[-1] == "BASE,BASE-2"


@pytest.mark.parametrize("use_strategy", [False, True], ids=["standalone", "strategy"])
def test_file_lookup_followed_by_another_template_segment(tmp_path, use_strategy):
    from ansible import constants as C

    messages, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    empty_suffix: null
  tasks:
    - debug:
        msg: "{{ lookup('file', playbook_dir ~ '/data.txt') }}/{{ 'second' }}"
    - debug:
        msg: "{{ lookup('file', playbook_dir ~ '/data.txt') }}/{{ empty_suffix }}"
""", {"data.txt": "first"}, use_strategy=use_strategy)
    null_suffix = "None" if not MODERN_TEMPLATING and C.DEFAULT_JINJA2_NATIVE else ""
    assert messages == ["first/second", "first/" + null_suffix]


def test_delegated_template_alias_uses_file_text_mode(tmp_path, monkeypatch):
    from ansiroll.loader import _core_version

    monkeypatch.setenv("ANSIBLE_JINJA2_NATIVE", "true")
    monkeypatch.setenv("ANSIROLL_TEST_PREFIX", "controller")
    path = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    empty: null
    alias: "{{ lookup('env', 'ANSIROLL_TEST_PREFIX') }}/{{ empty }}"
  tasks:
    - template:
        src: sample.j2
        dest: "{{ playbook_dir }}/rendered.txt"
      delegate_to: localhost
""", {"sample.j2": "prefix={{ alias }}\n"})
    output = tmp_path / "rendered.txt"
    expected = _ansible(path, tmp_path)
    expected_text = output.read_text()
    suffix = "None" if (2, 13) <= _core_version() < (2, 19) else ""
    assert expected_text == "prefix=controller/" + suffix + "\n"
    output.unlink()
    assert _ansible(path, tmp_path, strategy="ansiroll.core.ansiroll") == expected
    assert output.read_text() == expected_text


def test_delegated_template_reads_current_host_task_alias(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIROLL_TEST_PREFIX", "controller")
    path = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - template:
        src: sample.j2
        dest: "{{ playbook_dir }}/rendered.txt"
      delegate_to: localhost
      vars:
        alias: "{{ lookup('env', 'ANSIROLL_TEST_PREFIX') }}/tail"
        payload: "{{ {'values': hostvars[inventory_hostname].values()} }}"
""", {"sample.j2": "prefix={{ hostvars[inventory_hostname].alias }}\n"
                      "type={{ payload['values'] | type_debug }}\n"})
    _, recap = _ansible(path, tmp_path, strategy="ansiroll.core.ansiroll")
    assert recap == ("1", "1", "0", "0", "0", "0", "0")
    assert (tmp_path / "rendered.txt").read_text() == "prefix=controller/tail\ntype=list\n"


def test_role_defaults_and_group_vars_still_resolve_nested_templates(tmp_path):
    _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  roles: [r]
  tasks:
    - debug: msg="{{ from_group }} {{ hostvars['localhost'].from_group }}"
""", {
        "roles/r/defaults/main.yml":
            "inner: I\nouter: '{{ inner }}-O'\n"
            "items_: ['{{ outer }}']\nmapping: {k: '{{ outer }}'}\n",
        "roles/r/tasks/main.yml":
            "- debug: msg=\"{{ outer }} {{ items_[0] }} {{ mapping.k }}\"\n",
        "group_vars/all.yml": "g: G\nfrom_group: '{{ g }}!'\n",
    })


@pytest.mark.parametrize("use_strategy", [False, True], ids=["standalone", "strategy"])
def test_controller_authored_vars_preserve_template_provenance(tmp_path, use_strategy):
    vars_files = "" if use_strategy else "  vars_files: [sample.yml, literals.yml]\n"
    register = "\n      register: inclusion" if MODERN_TEMPLATING else ""
    include = """
    - ansible.builtin.include_vars: {file: sample.yml, name: included}%s
    - ansible.builtin.include_vars: literals.yml
""" % register if use_strategy else ""
    prefix = "included." if use_strategy else ""
    playbook = r"""
- hosts: localhost
  gather_facts: false
  vars:
    label: sample
%(vars_files)s  tasks:%(include)s
    - command:
        argv: [printf, '%%s', '{{ "{{" }} label }}-module']
      delegate_to: localhost
      register: output
      changed_when: false
    - debug:
        msg: >-
          {{ %(prefix)sauthored }} / {{ %(prefix)snested[0].value }} /
          {{ literal }} / {{ output.stdout }}
""" % {"vars_files": vars_files, "include": include, "prefix": prefix}
    if use_strategy:
        playbook += r"""
    - debug:
        msg: >-
          {{ authored is defined | lower }} / {{ ansible_facts.included is defined | lower }}
"""
        if register:
            playbook += r"""
    - debug:
        msg: "{{ inclusion.ansible_facts.included.authored }}"
"""
        playbook += r"""
    - ansible.builtin.include_vars: sample.yml
    - debug:
        msg: "{{ authored }} / {{ literal }}"
- hosts: localhost
  gather_facts: false
  vars:
    label: later
  tasks:
    - debug:
        msg: "{{ included.authored }} / {{ authored }} / {{ literal }}"
"""
        if register:
            playbook += r"""
    - debug:
        msg: >-
          {{ inclusion.ansible_facts.included.nested[0].value }} /
          {{ inclusion.ansible_facts.included.nested[0].literal }} /
          {{ inclusion.ansible_facts.included.nested[0].count }} /
          {{ inclusion.ansible_facts.included.nested[0].word }} /
          {{ output.stdout }}
"""
    messages, recap = _assert_parity(tmp_path, playbook, {
        "sample.yml": (
            "authored: '{{ label }}-authored'\n"
            "nested:\n"
            "  - value: '{{ label }}-nested'\n"
            "    literal: !unsafe '{{ label }}-included-literal'\n"
            "    count: 7\n"
            "    word: plain\n"
        ),
        "literals.yml": "literal: !unsafe '{{ label }}-literal'\n",
    }, use_strategy=use_strategy)

    assert recap[3] == "0"
    assert messages[0] == (
        "sample-authored / sample-nested / {{ label }}-literal / {{ label }}-module"
    )
    if use_strategy:
        assert messages[1] == "false / false"
        expected = [
            "sample-authored / {{ label }}-literal",
            "later-authored / later-authored / {{ label }}-literal",
        ]
        if register:
            expected.insert(0, "sample-authored")
            expected.append(
                "later-nested / {{ label }}-included-literal / 7 / plain / {{ label }}-module"
            )
        assert messages[2:] == expected


@pytest.mark.skipif(not MODERN_TEMPLATING, reason="modern include_vars register provenance")
def test_looped_include_vars_keeps_each_registered_definition_across_plays(tmp_path):
    messages, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    label: first
  tasks:
    - include_vars: {file: "{{ item }}.yml", name: data}
      loop: [authored, skipped, literal]
      when: item != 'skipped'
      register: inclusion
- hosts: localhost
  gather_facts: false
  vars:
    label: second
  tasks:
    - debug:
        msg: >-
          {{ inclusion.results[0].ansible_facts.data.value }} /
          {{ inclusion.results[1] is skipped | lower }} /
          {{ inclusion.results[2].ansible_facts.data.value }}
""", {
        "authored.yml": "value: '{{ label }}'\n",
        "literal.yml": "value: !unsafe '{{ label }}'\n",
    }, use_strategy=True)
    assert messages == ["second / true / {{ label }}"]


@pytest.mark.skipif(not MODERN_TEMPLATING, reason="modern Templar parity")
@pytest.mark.parametrize("expression", ["vars", "vars | to_json | from_json"])
def test_magic_variable_mappings_flatten_without_recursion(tmp_path, expression):
    nested_vars_check = (
        "all_vars.msg.vars.label == 'sample' and 'vars' not in all_vars.msg.vars"
        if Version(__version__) >= Version("2.20") else "'vars' not in all_vars.msg"
    )
    path = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    label: sample
    alias: '{{ label }}'
    __ansiroll_label: user-provided
    nested: {leaf: '{{ label }}'}
  tasks:
    - no_log: true
      block:
        - debug: {msg: '{{ hostvars }}'}
          register: all_hosts
        - debug: {msg: '{{ hostvars.localhost }}'}
          register: one_host
        - debug: {msg: '{{ %s }}'}
          register: all_vars
    - assert:
        that:
          - all_hosts.msg | type_debug == 'dict'
          - all_hosts.msg.localhost.inventory_hostname == 'localhost'
          - one_host.msg | type_debug == 'dict'
          - one_host.msg.inventory_hostname == 'localhost'
          - all_vars.msg | type_debug == 'dict'
          - all_vars.msg.hostvars is defined
          - all_vars.msg['__ansiroll_label'] == 'user-provided'
          - "'__ansiroll_controller_config' not in all_vars.msg"
          - "%s"
          - all_vars.msg.nested.leaf == 'sample'
          - all_vars.msg.alias == 'sample'
          - all_vars.msg.one_host.msg.inventory_hostname == 'localhost'
""" % (expression, nested_vars_check))
    _, linear_recap = _ansible(path, tmp_path)
    _, ansiroll_recap = _ansible(path, tmp_path, strategy="ansiroll.core.ansiroll")
    assert ansiroll_recap == linear_recap == ("4", "0", "0", "0", "0", "0", "0")


def test_template_and_copy_content_fail_on_undefined_variables(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    path = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    out: %s
    d: fine
  tasks:
    - template: {src: bad.j2, dest: "{{ out }}/bad.txt"}
      ignore_errors: true
    - copy:
        content: "a {{ undefined_var }} b"
        dest: "{{ out }}/copy.txt"
      ignore_errors: true
    - template: {src: ok.j2, dest: "{{ out }}/ok.txt"}
""" % json.dumps(str(out)), {
        "bad.j2": "head\n{{ undefined_var }}\ntail\n",
        "ok.j2": "ok={{ d }}\n",
    })
    results = []
    for run in (_ansible, _ansiroll):
        results.append(run(path, tmp_path))
        # Only the valid template is written; nothing partial or empty.
        assert sorted(os.listdir(str(out))) == ["ok.txt"]
        assert (out / "ok.txt").read_text() == "ok=fine\n"
        (out / "ok.txt").unlink()
    assert results[1] == results[0]


def test_when_errors_fail_instead_of_skipping(tmp_path):
    _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars: {typo: {}}
  tasks:
    - debug: msg=A
      when: undefined_var
      ignore_errors: true
    - debug: msg=B
      when: typo.x > 3
      ignore_errors: true
    - debug: msg=C
      when: 1/0 == 1
      ignore_errors: true
    - debug: msg=D
      when: undefined_var is defined
    - debug: msg=L
      loop: [1]
      when: undefined_var
      ignore_errors: true
    - block:
        - debug: msg=INBLOCK
      when: undefined_var
    - debug: msg=NOT_REACHED
""")


def test_nested_render_restores_loop_context(tmp_path):
    msgs, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    base: B
    lst: ["{{ base }}-x"]
  tasks:
    - debug: msg="{{ lst[0] ~ lookup('vars', 'item') }}"
      loop: [a, b]
""")
    assert msgs == ["B-xa", "B-xb"]


def test_fileglob_paths_are_baked_and_delivered_as_data(tmp_path):
    source = tmp_path / "files" / "item.txt"
    dest = tmp_path / "copied.txt"
    playbook = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - copy:
        src: "{{ item }}"
        dest: %s
      with_fileglob: ["*.txt"]
    - debug:
        msg: "{{ item | basename }}"
      with_fileglob: ["*.txt"]
""" % json.dumps(str(dest)), {"files/item.txt": "fixture"})

    plays, _ = parse_playbook(str(playbook))
    copy_task = next(task for block in plays[0]["tasks"] for task in block.get("block", [])
                     if task.get("action", "").split(".")[-1] == "copy")
    assert copy_task["loop"] == [{"__ansiroll_unsafe__": str(source)}]
    assert copy_task["_baked"] == {str(source): "fixture"}
    expected = _ansible(playbook, tmp_path)
    assert dest.read_text() == "fixture"
    dest.unlink()
    assert _ansiroll(playbook, tmp_path) == expected
    assert dest.read_text() == "fixture"


def test_playbook_dir_template_src_from_another_role_is_baked(tmp_path):
    dest = tmp_path / "rendered.txt"
    playbook = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  roles: [a]
""", {
        "roles/a/tasks/main.yml": """
- template:
    src: "{{ playbook_dir }}/roles/b/templates/conf.j2"
    dest: %s
- debug: msg="{{ playbook_dir }}"
""" % json.dumps(str(dest)),
        "roles/a/templates/own.j2": "unrelated",
        "roles/b/templates/conf.j2": "value={{ 1 + 1 }}",
    })
    expected = _ansible(playbook, tmp_path)
    assert expected[0] == [str(tmp_path)]
    assert dest.read_text() == "value=2"
    dest.unlink()
    src, _ = compile_playbook(str(playbook))
    # The target has no controller tree, so the template must come from the artifact.
    (tmp_path / "roles/b/templates/conf.j2").unlink()
    script = tmp_path / "compiled.py"
    script.write_text(src)
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(tmp_path), env=_env(), capture_output=True, text=True,
    )
    assert _summary(proc.stdout + "\n" + proc.stderr) == expected
    assert dest.read_text() == "value=2"


def test_looped_playbook_dir_template_src_is_baked_under_its_absolute_path(tmp_path):
    playbook = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars: {names: [conf]}
  roles: [a]
""", {
        "roles/a/tasks/main.yml": """
- template:
    src: "{{ playbook_dir }}/roles/b/templates/{{ item }}.j2"
    dest: %s/out-{{ item }}
  loop: "{{ names }}"
""" % str(tmp_path),
        "roles/a/templates/own.j2": "unrelated",
        "roles/b/templates/conf.j2": "value={{ 1 + 1 }}",
    })
    dest = tmp_path / "out-conf"
    expected = _ansible(playbook, tmp_path)
    assert dest.read_text() == "value=2"
    dest.unlink()
    src, _ = compile_playbook(str(playbook))
    (tmp_path / "roles/b/templates/conf.j2").unlink()
    script = tmp_path / "compiled.py"
    script.write_text(src)
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(tmp_path), env=_env(), capture_output=True, text=True,
    )
    assert _summary(proc.stdout + "\n" + proc.stderr) == expected
    assert dest.read_text() == "value=2"


def test_looped_playbook_dir_src_bakes_nested_items_and_doubled_separators(tmp_path):
    playbook = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars: {names: [flat, nested/conf]}
  tasks:
    - template:
        src: "{{ playbook_dir }}/templates//{{ item }}.j2"
        dest: "%s/out-{{ item | basename }}"
      loop: "{{ names }}"
""" % str(tmp_path), {
        "templates/flat.j2": "flat={{ 1 + 1 }}",
        "templates/nested/conf.j2": "nested={{ 1 + 2 }} {{ template_fullpath | basename }}",
    })
    expected = _ansible(playbook, tmp_path)
    assert (tmp_path / "out-conf").read_text() == "nested=3 conf.j2"
    src, _ = compile_playbook(str(playbook))
    for name in ("out-flat", "out-conf", "templates/flat.j2", "templates/nested/conf.j2"):
        (tmp_path / name).unlink()
    script = tmp_path / "compiled.py"
    script.write_text(src)
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(tmp_path), env=_env(), capture_output=True, text=True,
    )
    assert _summary(proc.stdout + "\n" + proc.stderr) == expected
    assert (tmp_path / "out-flat").read_text() == "flat=2"
    assert (tmp_path / "out-conf").read_text() == "nested=3 conf.j2"


def test_looped_src_directly_under_playbook_dir_falls_back_not_to_the_role_tree(tmp_path):
    playbook = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars: {names: [conf]}
  roles: [a]
""", {
        "roles/a/tasks/main.yml": """
- template:
    src: "{{ playbook_dir }}/{{ item }}.j2"
    dest: %s/out-{{ item }}
  loop: "{{ names }}"
""" % str(tmp_path),
        "roles/a/templates/conf.j2": "unrelated",
        "conf.j2": "value={{ 1 + 1 }}",
    })
    # Baking the playbook dir itself would embed vault files and keys, and the role's tree is unrelated.
    _src, info = compile_playbook(str(playbook))
    assert [task["reason"] for task in info["fallback_tasks"]] == [
        "dynamic baked src not resolvable at compile time: %s/{{ item }}.j2" % tmp_path]


def test_absolute_dynamic_src_bakes_only_matching_files_inside_the_playbook_dir(tmp_path):
    from ansiroll import loader

    pb = tmp_path / "pb"
    (pb / ".git").mkdir(parents=True)
    (pb / "conf.j2").write_text("conf")
    (pb / "notes.txt").write_text("other")
    (pb / ".vault.j2").write_text("secret")
    (pb / ".git" / "x.j2").write_text("object")
    (tmp_path / "outside.j2").write_text("outside")
    (pb / "link.j2").symlink_to(tmp_path / "outside.j2")
    (pb / "nested").mkdir()
    (pb / "nested" / "sub.j2").write_text("sub")
    # The static directory must lie below the playbook dir, which would otherwise expose the whole tree.
    assert not loader._bake_absolute_tree({}, str(pb) + "/{{ item }}.j2", str(pb), True)
    (pb / ".secrets").mkdir()
    (pb / ".secrets" / "password").write_text("hunter2")
    tpl = pb / "templates"
    (tpl / "real").mkdir(parents=True)
    (tpl / "conf.j2").write_text("conf")
    (tpl / "real" / "sub.j2").write_text("sub")
    (tpl / "alias").symlink_to(tpl / "real")
    (tpl / "loop").symlink_to(tpl)
    (tpl / "private.j2").symlink_to(pb / ".secrets" / "password")
    (tpl / "escape.j2").symlink_to(tmp_path / "outside.j2")
    (tpl / ".hidden.j2").write_text("hidden")
    node = {}
    assert loader._bake_absolute_tree(node, str(tpl) + "/{{ item }}.j2", str(pb), True)
    assert node["_baked"] == {
        str(tpl / "alias" / "sub.j2"): "sub", str(tpl / "conf.j2"): "conf", str(tpl / "real" / "sub.j2"): "sub"}
    assert node["_baked_meta"][str(tpl / "conf.j2")]["fullpath"] == str(tpl / "conf.j2")
    assert not loader._bake_absolute_tree({}, str(pb) + "/.secrets/{{ item }}", str(pb), False)


def test_absolute_dynamic_src_tree_over_the_size_limit_is_not_baked(tmp_path, monkeypatch):
    from ansiroll import loader

    monkeypatch.setattr(loader, "_ABSOLUTE_TREE_LIMIT", 10)
    tree = tmp_path / "big"
    tree.mkdir()
    (tree / "a.j2").write_text("x" * 20)
    node = {}
    assert not loader._bake_absolute_tree(node, str(tree) + "/{{ item }}.j2", str(tmp_path), False)
    assert "_baked" not in node


def test_playbook_dir_inside_a_larger_expression_falls_back(tmp_path):
    dest = tmp_path / "rendered.txt"
    playbook = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  roles: [a]
""", {
        "roles/a/tasks/main.yml": """
- template:
    src: "{{ playbook_dir ~ '/roles/b/templates/' ~ name }}.j2"
    dest: %s
""" % json.dumps(str(dest)),
        "roles/a/defaults/main.yml": "name: conf\n",
        "roles/a/templates/conf.j2": "wrong role",
        "roles/b/templates/conf.j2": "value={{ 1 + 1 }}",
    })
    plays, _ = parse_playbook(str(playbook))
    task = next(t for block in plays[0]["tasks"] for t in block.get("block", [])
                if t.get("action", "").split(".")[-1] == "template")
    assert task["fallback"] is True
    assert "_baked" not in task
    expected = _ansible(playbook, tmp_path)
    assert dest.read_text() == "value=2"
    dest.unlink()
    assert _ansible(playbook, tmp_path, strategy="ansiroll.core.ansiroll") == expected
    assert dest.read_text() == "value=2"


def test_dynamic_src_naming_playbook_dir_outside_jinja_bakes_role_tree(tmp_path):
    dest = tmp_path / "rendered.txt"
    playbook = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  roles: [a]
""", {
        "roles/a/tasks/main.yml": """
- template:
    src: "my_playbook_dir/{{ name }}.j2"
    dest: %s
""" % json.dumps(str(dest)),
        "roles/a/defaults/main.yml": "name: conf\n",
        "roles/a/templates/my_playbook_dir/conf.j2": "value={{ 1 + 1 }}",
    })
    plays, _ = parse_playbook(str(playbook))
    task = next(t for block in plays[0]["tasks"] for t in block.get("block", [])
                if t.get("action", "").split(".")[-1] == "template")
    assert not task.get("fallback")
    assert "my_playbook_dir/conf.j2" in task["_baked"]
    _ansiroll(playbook, tmp_path)
    assert dest.read_text() == "value=2"


def test_playbook_dir_attribute_of_another_var_bakes_role_tree(tmp_path):
    dest = tmp_path / "rendered.txt"
    playbook = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  roles: [a]
""", {
        "roles/a/tasks/main.yml": """
- template:
    src: "services/{{ svc.playbook_dir }}/{{ svc.name }}.j2"
    dest: %s
""" % json.dumps(str(dest)),
        "roles/a/defaults/main.yml": "svc: {playbook_dir: app, name: conf}\n",
        "roles/a/templates/services/app/conf.j2": "value={{ 1 + 1 }}",
    })
    plays, _ = parse_playbook(str(playbook))
    task = next(t for block in plays[0]["tasks"] for t in block.get("block", [])
                if t.get("action", "").split(".")[-1] == "template")
    assert not task.get("fallback")
    assert "services/app/conf.j2" in task["_baked"]
    _ansiroll(playbook, tmp_path)
    assert dest.read_text() == "value=2"


def _role_template_task(tmp_path, src, files):
    dest = tmp_path / "rendered.txt"
    playbook = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  roles: [a]
""", dict({
        "roles/a/tasks/main.yml": """
- template:
    src: %s
    dest: %s
""" % (json.dumps(src), json.dumps(str(dest))),
        "roles/a/defaults/main.yml": "name: conf\nmy_abs_dir: %s\n" % json.dumps(str(tmp_path / "abs")),
        "roles/a/templates/other.j2": "unused",
    }, **files))
    plays, _ = parse_playbook(str(playbook))
    task = next(t for block in plays[0]["tasks"] for t in block.get("block", [])
                if t.get("action", "").split(".")[-1] == "template")
    return playbook, dest, task


def test_dynamic_src_starting_with_a_jinja_dir_falls_back(tmp_path):
    playbook, dest, task = _role_template_task(tmp_path, "{{ my_abs_dir }}/{{ name }}.j2", {
        "abs/conf.j2": "value={{ 1 + 1 }}",
        "roles/a/templates/conf.j2": "value={{ 1 + 1 }}",
    })
    assert task["fallback"] is True
    assert "_baked" not in task
    expected = _ansible(playbook, tmp_path)
    assert dest.read_text() == "value=2"
    dest.unlink()
    assert _ansible(playbook, tmp_path, strategy="ansiroll.core.ansiroll") == expected
    assert dest.read_text() == "value=2"


def test_dynamic_role_path_src_is_baked_under_its_absolute_path(tmp_path):
    playbook, dest, task = _role_template_task(tmp_path, "{{ role_path }}/templates/{{ name }}.j2", {
        "roles/a/templates/conf.j2": "value={{ 1 + 1 }}",
    })
    assert not task.get("fallback")
    assert str(tmp_path / "roles/a/templates/conf.j2") in task["_baked"]
    expected = _ansible(playbook, tmp_path)
    dest.unlink()
    assert _ansiroll(playbook, tmp_path) == expected
    assert dest.read_text() == "value=2"


def test_role_path_src_is_resolved_at_compile_time(tmp_path):
    playbook, dest, task = _role_template_task(tmp_path, "{{ role_path }}/templates/conf.j2", {
        "roles/a/templates/conf.j2": "value={{ 1 + 1 }}",
    })
    assert not task.get("fallback")
    _ansiroll(playbook, tmp_path)
    assert dest.read_text() == "value=2"


@pytest.mark.parametrize("src, path", [
    ("{{ name }}.j2", "templates/conf.j2"),
    ("sub/{{ name }}.j2", "sub/conf.j2"),
    ("sub/{{ name }}.j2", "roles/a/tasks/sub/conf.j2"),
    ("x{{ name }}/app.j2", "roles/a/templates/xconf/app.j2"),
])
def test_dynamic_role_src_bakes_the_whole_search_path(tmp_path, src, path):
    playbook, dest, task = _role_template_task(tmp_path, src, {path: "value={{ 1 + 1 }}"})
    assert not task.get("fallback")
    expected = _ansible(playbook, tmp_path)
    assert dest.read_text() == "value=2"
    dest.unlink()
    assert _ansiroll(playbook, tmp_path) == expected
    assert dest.read_text() == "value=2"


def test_dynamic_src_with_a_parent_dir_prefix_falls_back(tmp_path):
    _, _, task = _role_template_task(tmp_path, "../{{ name }}.j2", {"roles/a/conf.j2": "value={{ 1 + 1 }}"})
    assert task["fallback"] is True
    assert "_baked" not in task


def test_dynamic_src_does_not_bake_a_playbook_dir_named_templates(tmp_path):
    _, _, task = _role_template_task(tmp_path / "templates", "{{ name }}.j2", {
        "roles/a/templates/conf.j2": "value={{ 1 + 1 }}",
    })
    assert sorted(task["_baked"]) == ["conf.j2", "other.j2"]


def test_dynamic_src_without_prefix_bakes_only_the_action_subdir(tmp_path):
    _, _, task = _role_template_task(tmp_path, "{{ name }}.j2", {
        "roles/a/templates/conf.j2": "value={{ 1 + 1 }}",
        "roles/a/files/blob.bin": "binary",
    })
    assert sorted(task["_baked"]) == ["conf.j2", "other.j2"]


def test_dynamic_src_does_not_bake_a_file_shadowed_by_an_unbaked_dir(tmp_path):
    playbook, dest, task = _role_template_task(tmp_path, "{{ name }}.j2", {
        "roles/a/conf.j2": "role root",
        "templates/conf.j2": "playbook templates",
    })
    assert "conf.j2" not in task["_baked"]
    _ansible(playbook, tmp_path)
    assert dest.read_text() == "role root"


def test_dynamic_src_template_fullpath_names_the_baked_file(tmp_path):
    playbook, dest, task = _role_template_task(tmp_path, "{{ name }}.j2", {
        "roles/a/templates/conf.j2": "{{ template_fullpath }}",
    })
    expected = _ansible(playbook, tmp_path)
    assert dest.read_text() == str(tmp_path / "roles/a/templates/conf.j2")
    dest.unlink()
    assert _ansiroll(playbook, tmp_path) == expected
    assert dest.read_text() == str(tmp_path / "roles/a/templates/conf.j2")


def test_include_key_in_role_defaults_is_data(tmp_path):
    playbook = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  roles: [a]
""", {
        "roles/a/tasks/main.yml": "- debug: msg=\"{{ nginx | to_json }}\"\n",
        "roles/a/defaults/main.yml": "nginx:\n  include: conf.d/*.conf\n",
    })
    expected = _ansible(playbook, tmp_path)
    assert expected[0] == ['{"include": "conf.d/*.conf"}']
    assert _ansiroll(playbook, tmp_path) == expected


def test_imported_playbook_dir_is_absolute_for_a_relative_playbook_path(tmp_path, monkeypatch):
    _write(tmp_path, "- import_playbook: sub/inner.yml\n", {
        "sub/inner.yml": "- hosts: localhost\n  gather_facts: false\n  tasks: []\n",
    })
    monkeypatch.chdir(tmp_path)
    plays, _ = parse_playbook("play.yml")
    assert plays[0]["playbook_dir"] == str(tmp_path / "sub")


def test_imported_playbook_uses_its_own_playbook_dir(tmp_path):
    dest = tmp_path / "rendered.txt"
    playbook = _write(tmp_path, """
- import_playbook: sub/inner.yml
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg="{{ playbook_dir }}"
""", {
        "sub/inner.yml": """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg="{{ playbook_dir }}"
    - template:
        src: "{{ playbook_dir }}/templates/t.j2"
        dest: %s
""" % json.dumps(str(dest)),
        "sub/templates/t.j2": "inner={{ 1 + 1 }}",
    })
    expected = _ansible(playbook, tmp_path)
    assert expected[0] == [str(tmp_path / "sub"), str(tmp_path)]
    assert dest.read_text() == "inner=2"
    dest.unlink()
    assert _ansiroll(playbook, tmp_path) == expected
    assert dest.read_text() == "inner=2"


def test_play_keywords_see_the_plays_own_playbook_dir(tmp_path):
    msgs, _recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  check_mode: "{{ playbook_dir.endswith('/sub') }}"
  tasks:
    - command: "true"
      register: outer
    - debug: msg="outer={{ outer.skipped | default(false) }}"
- import_playbook: sub/inner.yml
""", {
        "sub/inner.yml": """
- hosts: localhost
  gather_facts: false
  check_mode: "{{ playbook_dir.endswith('/sub') }}"
  tasks:
    - command: "true"
      register: inner
    - debug: msg="inner={{ inner.skipped | default(false) }}"
""",
    })
    assert msgs == ["outer=False", "inner=True"]


def test_playbook_dir_outranks_task_vars_and_set_fact(tmp_path):
    msgs, _recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg="task={{ playbook_dir != '/other' }}"
      vars: {playbook_dir: /other}
    - set_fact: {playbook_dir: /other}
    - debug: msg="fact={{ playbook_dir != '/other' }}"
    - block:
        - debug: msg="block={{ playbook_dir != '/other' }}"
      vars: {playbook_dir: /other}
""")
    assert msgs == ["task=True", "fact=True", "block=True"]


def test_vars_derived_from_playbook_dir_ignore_a_set_fact_override(tmp_path):
    msgs, _recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    src_dir: "{{ playbook_dir }}/templates"
    chained: "{{ src_dir }}/x"
    cfg: {path: "{{ playbook_dir }}/cfg"}
  tasks:
    - set_fact: {playbook_dir: /other}
    - debug: msg="task={{ source_dir.startswith('/other') }}"
      vars: {source_dir: "{{ playbook_dir }}/templates"}
    - block:
        - debug: msg="block={{ block_dir.startswith('/other') }}"
      vars: {block_dir: "{{ playbook_dir }}/templates"}
    - debug: msg="play={{ chained.startswith('/other') }}"
    - debug: msg="container={{ cfg.path.startswith('/other') }}"
""")
    assert msgs == ["task=False", "block=False", "play=False", "container=False"]


def test_vars_derived_from_playbook_dir_ignore_an_override_in_the_same_mapping(tmp_path):
    msgs, _recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg="task={{ source_dir.startswith('/other') }}"
      vars: {playbook_dir: /other, source_dir: "{{ playbook_dir }}/templates"}
    - block:
        - debug: msg="block={{ block_dir.startswith('/other') }}"
      vars: {playbook_dir: /other, block_dir: "{{ playbook_dir }}/templates"}
""")
    assert msgs == ["task=False", "block=False"]


def test_frozen_vars_are_templated_once(tmp_path):
    path = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    y: Y
    x: "{% raw %}{{ y }}{% endraw %}"
    z: "{{ y }}-z"
  tasks:
    - debug: msg="x={{ x }} z={{ z }}"
""")
    expected = _ansible(path, tmp_path)
    src, _ = compile_playbook(
        str(path), tree_shake=True, freeze_vars=["x", "z"]
    )
    script = tmp_path / "frozen.py"
    script.write_text(src)
    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(tmp_path), env=_env(), capture_output=True, text=True,
    )
    assert _summary(proc.stdout + "\n" + proc.stderr) == expected
    assert expected[0] == ["x={{ y }} z=Y-z"]


def test_ternary_regex_replace_and_keyword_booleans(tmp_path):
    msgs, _ = _assert_parity(tmp_path, r"""
- hosts: localhost
  gather_facts: false
  vars: {s_false: "false", s_no: "no", e: "", z: 0}
  tasks:
    - debug:
        msg: "{{ [s_false | ternary('T', 'F'), s_no | ternary('T', 'F'),
                 e | ternary('T', 'F'), z | ternary('T', 'F')] | join(',') }}"
    - debug: msg="{{ ['y' | bool, 't' | bool, 'yes' | bool] | join(',') }}"
    - debug:
        msg: "{{ 'Foo\nfoo' | regex_replace('^foo', 'X', ignorecase=true,
                                            multiline=true) }}"
    - debug: msg="{{ 'Foo foo' | regex_replace('foo', 'X', true) }}"
    - fail: msg=kw-y
      ignore_errors: "{{ 'y' }}"
    - fail: msg=kw-t
      ignore_errors: t
    - debug: msg=END
""")
    assert msgs[:4] == ["T,T,F,F", "False,False,True", "X\nX", "X X"]


@pytest.mark.skipif(MODERN_TEMPLATING, reason="legacy boolean-string conditionals")
@pytest.mark.parametrize("use_strategy", [False, True], ids=["standalone", "strategy"])
def test_legacy_boolean_strings_in_inline_conditions(tmp_path, use_strategy):
    messages, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: {msg: accepted}
      when: "{{ 'true' }}"
    - debug: {msg: skipped}
      when: "{{ 'false' }}"
""", use_strategy=use_strategy)
    assert messages == ["accepted"]
    assert recap == ("1", "0", "0", "0", "1", "0", "0")


def test_lazy_container_operations_resolve_nested_values_once(tmp_path):
    msgs, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    base: 2
    alias: "{{ base }}"
    mapping: {k: "{{ alias }}"}
    nested: {m: {k: "{{ alias }}"}}
    items_: ["{{ alias }}", "tail"]
  tasks:
    - debug:
        msg: >-
          {{ mapping | combine({'z': 1}) | to_json }} /
          {{ dict(mapping).k }} /
          {{ (nested | combine({'m': {'z': 1}}, recursive=true)).m.k }}
    - debug:
        msg: >-
          {{ (items_ + ['x']) | to_json }} /
          {{ (['x'] + items_) | to_json }} /
          {{ (items_ * 2) | to_json }} /
          {{ items_ | reverse | list | to_json }} /
          {{ 2 in items_ }}
    - debug:
        msg: "{{ [mapping | type_debug, items_ | type_debug] | join(',') }}"
""")
    assert msgs[-1] == "dict,list"
    native = MODERN_TEMPLATING or C.DEFAULT_JINJA2_NATIVE
    encoded = '2' if native else '"2"'
    assert msgs[0] == '{"k": %s, "z": 1} / 2 / 2' % encoded
    assert msgs[1].endswith("/ " + str(native))


@pytest.mark.parametrize("use_strategy", [False, True], ids=["standalone", "strategy"])
def test_unsafe_role_group_loop_and_argument_values_remain_literal(tmp_path, use_strategy):
    msgs, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars: {sentinel: rendered}
  roles: [{role: r, param_value: !unsafe "{{ sentinel }}"}]
  tasks:
    - debug: msg="{{ from_group }}/{{ group_mapping.k }}"
    - debug: {msg: !unsafe "{{ sentinel }}"}
    - debug: msg="{{ item }}"
      loop: [!unsafe "{{ sentinel }}"]
    - set_fact: {fact_value: !unsafe "{{ sentinel }}"}
    - debug: msg="{{ fact_value }}/{{ fact_value | type_debug }}"
""", {
        "roles/r/defaults/main.yml": """
default_value: !unsafe "{{ sentinel }}"
default_mapping: {k: !unsafe "{{ sentinel }}"}
""",
        "roles/r/vars/main.yml": 'role_value: !unsafe "{{ sentinel }}"\n',
        "roles/r/tasks/main.yml": """
- debug: msg="{{ default_value }}/{{ default_mapping.k }}/{{ role_value }}/{{ param_value }}"
""",
        "group_vars/all.yml": """
from_group: !unsafe "{{ sentinel }}"
group_mapping: {k: !unsafe "{{ sentinel }}"}
""",
    }, use_strategy=use_strategy)
    assert msgs == [
        "{{ sentinel }}/{{ sentinel }}/{{ sentinel }}/{{ sentinel }}",
        "{{ sentinel }}/{{ sentinel }}",
        "{{ sentinel }}",
        "{{ sentinel }}",
        "{{ sentinel }}/" + ("str" if MODERN_TEMPLATING else "AnsibleUnsafeText"),
    ]


def test_inherited_conditions_are_evaluated_at_each_task(tmp_path):
    msgs, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars: {enabled: false}
  tasks:
    - block:
        - debug: msg=uses_task_vars
          vars: {enabled: true}
        - debug: msg=uses_play_vars
      when: enabled
    - block:
        - debug: msg=one
        - debug: msg=two
      when: missing_block
      ignore_errors: true
    - include_tasks: included.yml
      when: missing_include
      ignore_errors: true
    - include_role: {name: r}
      when: missing_role
      ignore_errors: true
    - import_tasks: included.yml
      when: missing_import
      ignore_errors: true
    - block:
        - debug: msg=body
      rescue:
        - debug: msg=rescue
      always:
        - debug: msg=always
      when: missing_rescue
    - debug: msg=unreachable
""", {
        "included.yml": "- debug: msg=included\n",
        "roles/r/tasks/main.yml": "- debug: msg=role\n",
    })
    assert msgs == ["uses_task_vars"]
    assert recap == ("6", "0", "0", "2", "1", "1", "5")


@pytest.mark.parametrize("action", [
    "include_tasks: included.yml", "include_role: {name: r}",
], ids=["tasks", "role"])
@pytest.mark.parametrize("use_strategy", [False, True], ids=["standalone", "strategy"])
def test_dynamic_include_loop_resolution_reports_task_results(tmp_path, action, use_strategy):
    messages, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - name: undefined loop skipped
      ACTION
      loop: "{{ missing_loop }}"
      when: false
      register: skipped_loop
    - assert: {that: skipped_loop is skipped, quiet: true}
    - name: malformed loop fails despite the condition
      ACTION
      loop: "{{ 'scalar' }}"
      when: false
      ignore_errors: true
      register: malformed_loop
    - assert: {that: malformed_loop is failed, quiet: true}
    - name: undefined lookup loop ignored
      ACTION
      with_list: "{{ missing_loop }}"
      ignore_errors: true
    - block:
        - name: undefined loop rescued
          ACTION
          loop: "{{ missing_loop }}"
      rescue:
        - assert: {that: ansible_failed_result is failed, quiet: true}
      always:
        - debug: {msg: always ran}
    - name: empty loop skipped
      ACTION
      loop: []
""".replace("ACTION", action), {
        "included.yml": "- debug: {msg: unexpected included task}\n",
        "roles/r/tasks/main.yml": "- debug: {msg: unexpected included role}\n",
    }, use_strategy=use_strategy)
    assert messages == ["always ran"]
    assert recap == ("6", "0", "0", "0", "2", "1", "2")


@pytest.mark.parametrize("action", [
    "include_tasks: included.yml", "include_role: {name: r}",
], ids=["tasks", "role"])
def test_dynamic_include_when_uses_loop_item(tmp_path, action):
    messages, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - ACTION
      loop: [allowed, skipped]
      when: item == 'allowed'
    - ACTION
      loop: [first, second]
      loop_control:
        loop_var: candidate
        index_var: position
      when: position == 1
      vars:
        item: "{{ candidate }}"
    - ACTION
      loop: [skipped, also_skipped]
      when: item == 'allowed'
""".replace("ACTION", action), {
        "included.yml": '- debug: {msg: "{{ item }}"}\n',
        "roles/r/tasks/main.yml": '- debug: {msg: "{{ item }}"}\n',
    })
    assert messages == ["allowed", "second"]


def test_include_loop_registers_results_and_tallies_ignored_failures(tmp_path):
    messages, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - include_tasks: child.yml
      loop: [1, 2]
      when: false
      register: filtered
    - assert:
        quiet: true
        that:
          - filtered is skipped
          - filtered.results | length == 2
    - include_tasks: child.yml
      loop: [1, 2]
      when: missing_condition
      ignore_errors: true
      register: ignored
    - assert:
        quiet: true
        that:
          - ignored is failed
          - EXPECTED_RESULTS
    - include_tasks: child.yml
      loop: [first, second]
      loop_control:
        extended: "{{ enable_metadata }}"
      when: ansible_loop is undefined
      vars:
        enable_metadata: false
""".replace("EXPECTED_RESULTS", "ignored.results | length == 2" if MODERN_TEMPLATING
            else "ignored.results is undefined"), {"child.yml": '- debug: {msg: "{{ item }}"}\n'})
    assert messages == ["first", "second"]


def test_include_loop_does_not_run_bodies_after_a_later_unignored_error(tmp_path):
    messages, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - include_tasks: child.yml
      loop: [1, 2]
      when: item != 2 or missing_condition
""", {"child.yml": "- debug: {msg: unexpected body}\n"})
    assert messages == []
    assert recap[3] == "1"


@pytest.mark.skipif(not MODERN_TEMPLATING, reason="older cores discard the partial include results")
@pytest.mark.parametrize("section", ["rescue", "always"])
def test_include_loop_runs_selected_body_before_block_recovery(tmp_path, section):
    messages, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - block:
        - include_tasks: child.yml
          loop: [first, second]
          when: item == 'first' or missing_condition
      SECTION:
        - debug: {msg: recovered}
""".replace("SECTION", section), {"child.yml": '- debug: {msg: "{{ item }}"}\n'})
    assert messages == ["first", "recovered"]


@pytest.mark.skipif(not MODERN_TEMPLATING, reason="older cores discard partial include results")
def test_outer_rescue_drops_partial_include_bodies_but_keeps_handlers(tmp_path):
    messages, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - block:
        - block:
            - include_role: {name: role_probe}
              loop: [first, second]
              when: item == 'first' or missing_condition
      rescue:
        - command: /bin/true
          notify: included handler
""", {
        "roles/role_probe/tasks/main.yml": "- debug: {msg: unexpected body}\n",
        "roles/role_probe/handlers/main.yml": "- name: included handler\n  debug: {msg: handler ran}\n",
    })
    assert messages == ["handler ran"]


@pytest.mark.skipif(Version(__version__) < Version("2.21"), reason="older cores merge loop metadata into item status")
def test_include_loop_keeps_failed_loop_var_separate_from_item_status(tmp_path):
    messages, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - include_tasks: child.yml
      loop: [allowed, skipped]
      loop_control: {loop_var: failed}
      when: failed == 'allowed'
      register: selected
    - assert:
        quiet: true
        that:
          - selected is succeeded
          - selected.results | map(attribute='failed') | list == ['allowed', 'skipped']
""", {"child.yml": '- debug: {msg: "{{ failed }}"}\n'})
    assert messages == ["allowed"]


def test_include_loop_validates_extended_metadata_options(tmp_path):
    allitems = Version(__version__) >= Version("2.14")
    option = ', extended_allitems: "f"' if allitems else ""
    condition = " and ansible_loop.allitems is undefined" if allitems else ""
    messages, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - include_tasks: child.yml
      loop: [unused]
      loop_control: {extended: "{{ missing_extended }}"}
      ignore_errors: true
    - include_tasks: child.yml
      loop: [selected]
      loop_control: {extended: "y"OPTION}
      when: ansible_loop.index0 == 0CONDITION
""".replace("OPTION", option).replace("CONDITION", condition),
                                 {"child.yml": '- debug: {msg: "{{ item }}"}\n'})
    assert messages == ["selected"]


@pytest.mark.skipif(Version(__version__) < Version("2.18"), reason="break_when requires ansible-core 2.18")
def test_include_loop_break_when_reads_registered_item_metadata(tmp_path):
    messages, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - include_tasks: child.yml
      loop: [first, second]
      loop_control:
        loop_var: entry
        break_when: selected.ansible_loop_var != 'entry'
      register: selected
""", {"child.yml": '- debug: {msg: "{{ entry }}"}\n'})
    assert messages == ["first", "second"]


@pytest.mark.skipif(not Version("2.18") <= Version(__version__) < Version("2.21"),
                    reason="legacy break_when errors replace the loop result")
def test_include_loop_discards_selected_bodies_after_legacy_break_when_error(tmp_path):
    messages, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - include_tasks: child.yml
      loop: [first, second]
      loop_control:
        break_when: item == 'second' and missing_condition
      register: selected
      ignore_errors: true
    - assert:
        quiet: true
        that:
          - selected is failed
          - selected.results is undefined
    - debug: {msg: continued}
""", {"child.yml": "- debug: {msg: unexpected body}\n"})
    assert messages == ["continued"]


def test_include_loop_censors_no_log_item_values(tmp_path):
    path = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - include_tasks: child.yml
      loop: [synthetic-private-item]
      when: false
      no_log: true
""", {"child.yml": "- debug: {msg: unexpected body}\n"})
    script = tmp_path / "compiled.py"
    script.write_text(compile_playbook(str(path))[0])
    result = subprocess.run([sys.executable, str(script)], cwd=tmp_path,
                            env=_env(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "synthetic-private-item" not in result.stdout + result.stderr


def test_no_log_include_register_remains_available_in_next_strategy_play(tmp_path):
    messages, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - include_tasks: child.yml
      loop: [synthetic-private-item]
      when: false
      register: selected
      no_log: true
- hosts: localhost
  gather_facts: false
  tasks:
    - assert:
        quiet: true
        that: selected is skipped
""", {"child.yml": "- debug: {msg: unexpected body}\n"}, use_strategy=True)
    assert messages == []


def test_empty_ordinary_loop_skips_validation_of_extended_metadata(tmp_path):
    _, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: {msg: unexpected}
      loop: []
      loop_control: {extended: "{{ undefined_extended }}"}
""")
    assert recap[4] == "1"


def test_undefined_and_false_loop_conditions(tmp_path):
    _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg=unreachable
      loop: "{{ missing_loop }}"
      when: false
    - debug: msg=unreachable
      loop: ["{{ missing_item }}"]
      ignore_errors: true
    - debug: msg=unreachable
      loop: "{{ 'scalar' }}"
      when: false
      ignore_errors: true
    - debug: msg=unreachable
      loop: [1, 2]
      when: false
      register: skipped_loop
    - debug: msg="{{ skipped_loop.results | length }} items skipped"
""")


@pytest.mark.parametrize("use_strategy", [False, True])
def test_lookup_loop_undefined_is_deferred_until_when(tmp_path, use_strategy):
    _, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg=unreachable
      delegate_to: localhost
      with_list: "{{ missing_loop }}"
      when: false
      register: skipped_loop
    - assert:
        quiet: true
        that: skipped_loop is skipped
    - debug: msg=unreachable
      with_list: "{{ missing_loop }}"
      ignore_errors: true
    - debug: msg=unreachable
      with_dict: ["ordinary"]
      when: false
      ignore_errors: true
""", use_strategy=use_strategy)
    assert recap == ("3", "0", "0", "0", "1", "0", "2")


def test_delegated_task_ignores_unrelated_undefined_context_values(tmp_path):
    messages, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    unused: "{{ missing_scalar }}"
    unused_mapping: {nested: "{{ missing_mapping_value }}"}
    unused_list: ["{{ missing_list_value }}"]
    used: fixture
  tasks:
    - command: {argv: [printf, '%s', "{{ used }}"]}
      delegate_to: localhost
      changed_when: false
      register: delegated
    - debug: {msg: "{{ delegated.stdout }}"}
""", use_strategy=True)
    assert messages == ["fixture"]
    assert recap == ("2", "0", "0", "0", "0", "0", "0")


def test_delegated_runtime_template_resolves_only_used_lookup_variables(tmp_path):
    playbook = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - copy:
        dest: "{{ playbook_dir }}/runtime.j2"
        content: !unsafe >-
          {{ (vars.copy().settings.rows.copy() | first).copy().used }}|
          {{ (vars.items() | selectattr('0', 'equalto', 'settings') | first)[1].rows.copy().pop(0).setdefault('used') }}|
          {{ settings.rows[0].pop('used') }}|{{ 'untouched' if true else unused }}|
          {{ alias.child | default('fallback') }}|
          {%- set current = hostvars[inventory_hostname] -%}
          {%- set first_key = current.keys() | first -%}
          {{ current.values()[0] == current[first_key] }}|{{ current.items()[0][0] == first_key }}|
          {%- set snapshot = current.copy() -%}
          {{ snapshot.values()[0] == current[first_key] }}|{{ snapshot.items()[0][0] == first_key }}
    - template:
        src: "{{ playbook_dir }}/runtime.j2"
        dest: "{{ playbook_dir }}/output.txt"
      delegate_to: localhost
      vars:
        settings:
          rows:
            - used: >-
                {{ lookup('pipe', 'printf used >> ' ~ ((playbook_dir ~ '/lookups') | quote) ~ '; printf hello') }}/tail
              unused: >-
                {{ lookup('pipe', 'printf sibling >> ' ~ ((playbook_dir ~ '/lookups') | quote)) }}/tail
            - >-
              {{ lookup('pipe', 'printf later >> ' ~ ((playbook_dir ~ '/lookups') | quote)) }}/tail
        unused: >-
          {{ lookup('pipe', 'printf unused >> ' ~ ((playbook_dir ~ '/lookups') | quote)) }}/tail
        alias: "{{ missing }}"
""")
    _, recap = _ansible(playbook, tmp_path, strategy="ansiroll.core.ansiroll")
    assert recap == ("2", "2", "0", "0", "0", "0", "0")
    assert (tmp_path / "output.txt").read_text() == (
        "hello/tail| hello/tail| hello/tail|untouched| fallback|True|True|True|True"
    )
    assert (tmp_path / "lookups").read_text() == "used"


def test_until_and_assert_errors_and_invalid_ignore_errors(tmp_path):
    from ansible.release import __version__
    from packaging.version import Version

    if Version(__version__) < Version("2.19"):
        pytest.skip("checks ansible-core 2.19+ registered error results")
    msgs, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - command: "true"
      register: outcome
      until: outcome.missing.field
      ignore_errors: true
    - debug:
        msg: >-
          {{ outcome.changed }}/{{ outcome.rc is defined }}/{{ outcome.attempts is defined }} /
          {{ outcome.msg }}
    - assert: {that: [missing_assertion]}
      register: assertion_result
      ignore_errors: true
    - debug: msg="{{ assertion_result.msg }}"
    - debug: msg=skipped
      when: false
      ignore_errors: "{{ 'maybe' }}"
    - debug: msg=unreachable
      ignore_errors: "{{ 'maybe' }}"
""")
    modern = Version(__version__) >= Version("2.21")
    until_prefix = "An 'until' expression failed: " if modern else ""
    assert msgs == [
        "False/False/False / Task failed: " + until_prefix
        + "Error while evaluating conditional: object of type 'dict' has no attribute 'missing'",
        "Task failed: Error while evaluating conditional: 'missing_assertion' is undefined",
    ]
    if modern:
        assert recap == ("4", "0", "0", "1", "1", "0", "2")
    else:
        assert recap == ("5", "0", "0", "0", "1", "0", "3")


def test_break_when_error_fails_the_item(tmp_path):
    from ansible.release import __version__
    from packaging.version import Version

    if Version(__version__) < Version("2.21"):
        pytest.skip("checks ansible-core 2.21 loop result semantics")
    path = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg="{{ item }}"
      loop: [1, 2, 3]
      loop_control: {break_when: [item.missing]}
      register: outcome
      ignore_errors: true
    - debug:
        msg: "{{ outcome.msg }}/{{ outcome.results | length }}/{{ outcome.results[0].failed }}"
""")
    src, _ = compile_playbook(str(path))
    script = tmp_path / "compiled.py"
    script.write_text(src)
    commands = [
        ansible_playbook_command() + ["-i", "localhost,", "-c", "local", str(path)],
        [sys.executable, str(script)],
    ]
    for command in commands:
        proc = subprocess.run(command, cwd=str(tmp_path), env=_env(),
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert re.search(r"^(?:failed|fatal): .*\(item=1\)", proc.stdout, re.M)
        assert _summary(proc.stdout)[0] == ["One or more items failed/1/True"]


def test_template_omit_does_not_replace_existing_destination(tmp_path):
    from ansible.release import __version__
    from packaging.version import Version

    if Version(__version__) < Version("2.19"):
        pytest.skip("checks ansible-core 2.19+ Omit handling")
    out = tmp_path / "existing.txt"
    out.write_text("unchanged")
    _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - template: {src: omit.j2, dest: %s}
      ignore_errors: true
""" % json.dumps(str(out)), {"omit.j2": "{{ missing | default(omit) }}\n"})
    assert out.read_text() == "unchanged"


def test_compiled_condition_uses_controller_core_without_target_ansible(tmp_path):
    from ansible.release import __version__
    from packaging.version import Version

    strict = Version(__version__) >= Version("2.19")
    playbook = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg=unreachable
      when: "'text sentinel'"
      ignore_errors: true
""")
    script = tmp_path / "compiled.py"
    script.write_text(compile_playbook(str(playbook))[0])
    without_ansible = """
import runpy
import sys

class NoAnsible:
    def find_spec(self, fullname, *args):
        if fullname == 'ansible' or fullname.startswith('ansible.'):
            raise ImportError(fullname)

sys.meta_path.insert(0, NoAnsible())
runpy.run_path(sys.argv[1], run_name='__main__')
"""
    proc = subprocess.run(
        [sys.executable, "-c", without_ansible, str(script)],
        cwd=str(tmp_path), env=_env(), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ("Conditionals must have a boolean result." in proc.stdout) == strict
    assert _summary(proc.stdout) == _ansible(playbook, tmp_path)


def test_debug_nested_undefined_survives_generated_result_reporting(tmp_path):
    from ansible.release import __version__
    from packaging.version import Version

    path = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    details: {items: ["{{ missing_first }}", {other: "{{ missing_second }}"}]}
  tasks:
    - block:
        - debug: {msg: "{{ details }}"}
          register: outcome
      rescue:
        - debug: {msg: rescued}
      always:
        - debug: {msg: always}
    - debug: {var: details}
    - debug: {msg: "continued: {{ outcome.failed }}"}
""")
    script = tmp_path / "compiled.py"
    script.write_text(compile_playbook(str(path))[0])
    ansible = ansible_playbook_command() + ["-i", "localhost,", "-c", "local", str(path)]
    modern = Version(__version__) >= Version("2.19")
    runs = [(ansible, _env()), ([sys.executable, str(script)], _env())]
    if modern:
        runs.append((ansible, dict(_env(), ANSIBLE_STRATEGY="ansiroll.core.ansiroll")))
    for command, env in runs:
        proc = subprocess.run(command, cwd=tmp_path, env=env,
                              capture_output=True, text=True, check=True)
        msgs, recap = _summary(proc.stdout)
        assert msgs == ["rescued", "always", "continued: True"]
        assert recap == ("4", "0", "0", "0", "0", "1", "0")
        assert "'missing_first' is undefined" in proc.stdout
        if modern:
            assert "<< error 1 - 'missing_first' is undefined >>" in proc.stdout
            assert "<< error 2 - 'missing_second' is undefined >>" in proc.stdout
        else:
            assert "VARIABLE IS NOT DEFINED!" in proc.stdout

    raw = subprocess.run([sys.executable, str(script)], cwd=tmp_path,
                         env=dict(_env(), ANSIROLL_RAW="1"),
                         capture_output=True, text=True, check=True)
    state = next(line for line in raw.stdout.splitlines()
                 if line.startswith("__ANSIROLL_STATE__ "))
    assert json.loads(state.split(" ", 1)[1])["vars"]["outcome"]["failed"]


def test_group_by_refresh_preserves_trusted_templates(tmp_path):
    msgs, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars: {label: rendered}
  tasks:
    - group_by: {key: added}
    - debug: {msg: "{{ group_text }} / {{ group_literal }}"}
""", {"group_vars/added.yml": """
group_text: 'value={{ label }}'
group_literal: !unsafe 'value={{ label }}'
"""}, use_strategy=True)
    assert msgs == ["value=rendered / value={{ label }}"]


def test_group_by_refresh_detects_trust_changes_without_text_changes(tmp_path):
    msgs, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars: {label: rendered}
  tasks:
    - debug: {msg: "{{ direct }} / {{ nested.values_[0] }}"}
    - group_by: {key: added}
    - debug: {msg: "{{ direct }} / {{ nested.values_[0] }}"}
    - group_by: {key: restored}
    - debug: {msg: "{{ direct }} / {{ nested.values_[0] }}"}
""", {
        "group_vars/all.yml": """
direct: !unsafe '{{ label }}'
nested: {values_: ['{{ label }}']}
""",
        "group_vars/added.yml": """
direct: '{{ label }}'
nested: {values_: [!unsafe '{{ label }}']}
""",
        "group_vars/restored.yml": """
direct: !unsafe '{{ label }}'
nested: {values_: ['{{ label }}']}
""",
    }, use_strategy=True)
    assert msgs == ["{{ label }} / rendered", "rendered / {{ label }}", "{{ label }} / rendered"]


def test_omit_ignore_errors_uses_core_keyword_default(tmp_path):
    from ansible.release import __version__
    from packaging.version import Version

    msgs, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - block:
        - fail: {msg: first}
          ignore_errors: "{{ omit }}"
      rescue:
        - debug: {msg: rescued first}
    - block:
        - fail: {msg: second}
          ignore_errors: "{{ omit }}"
        - debug: {msg: after second}
      ignore_errors: true
      rescue:
        - debug: {msg: rescued second}
""")
    if Version(__version__) >= Version("2.14"):
        assert msgs == ["rescued first", "after second"]
        assert recap[-2:] == ("1", "1")
    else:
        assert msgs == ["rescued first", "rescued second"]
        assert recap[-2:] == ("2", "0")


def test_frozen_values_and_extra_vars_preserve_literal_mappings(tmp_path):
    path = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    label: rendered
    fixed:
      __ansiroll_data__: {'{{ label }}': literal}
  tasks:
    - debug:
        msg: "{{ fixed['__ansiroll_data__'].keys() | first }}/{{ ordinary['__ansiroll_data__'] }}"
""")
    src, _ = compile_playbook(
        str(path), freeze_vars=["fixed"],
        extra_vars={"ordinary": {"__ansiroll_data__": "ordinary"}},
    )
    script = tmp_path / "compiled.py"
    script.write_text(src)
    proc = subprocess.run([sys.executable, str(script)], cwd=tmp_path,
                          env=_env(), capture_output=True, text=True, check=True)
    assert _summary(proc.stdout)[0] == ["{{ label }}/ordinary"]


@pytest.mark.parametrize("enabled", [False, True])
def test_broken_conditional_policy_is_baked(tmp_path, enabled):
    from ansible.release import __version__
    from packaging.version import Version

    if Version(__version__) < Version("2.19"):
        pytest.skip("ALLOW_BROKEN_CONDITIONALS was introduced in 2.19")

    path = _write(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: {msg: truthy expression}
      when: "'false'"
      ignore_errors: true
    - debug: {msg: literal number}
      when: 1
      ignore_errors: true
    - debug: {msg: whitespace}
      when: '  '
      ignore_errors: true
    - debug: {msg: rendered empty}
      when: "{{ '' }}"
      ignore_errors: true
    - debug: {msg: skipped}
      when: "[]"
      ignore_errors: true
""")
    env = dict(_env(), ANSIBLE_ALLOW_BROKEN_CONDITIONALS=str(enabled))
    expected = subprocess.run(
        ansible_playbook_command() + ["-i", "localhost,", "-c", "local", str(path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=True,
    )
    script = tmp_path / "compiled.py"
    subprocess.run(
        ansiroll_compile_command() + [str(path), "-o",
         str(script), "--tree-shake"], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=True,
    )
    env["ANSIBLE_ALLOW_BROKEN_CONDITIONALS"] = str(not enabled)
    compiled = subprocess.run(
        [sys.executable, str(script)], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=True,
    )
    assert _summary(compiled.stdout) == _summary(expected.stdout)
    assert _summary(expected.stdout)[0] == (
        ["truthy expression", "literal number", "whitespace", "rendered empty"]
        if enabled else []
    )


@pytest.mark.parametrize("use_strategy", [False, True])
def test_set_fact_templates_authored_fact_names(tmp_path, use_strategy):
    _, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    fact_key: greeting
    fact_value: hello
  tasks:
    - set_fact:
        "{{ fact_key }}": "{{ fact_value }}"
      register: result
    - ansible.builtin.set_fact:
        "fruit_{{ idx }}": "{{ item }}"
      loop: [apple, pear]
      loop_control:
        index_var: idx
    - set_fact:
        !unsafe "{{ fact_key }}": goodbye
      ignore_errors: true
    - assert:
        quiet: true
        that:
          - greeting == 'hello'
          - result.ansible_facts.greeting == 'hello'
          - fruit_0 == 'apple'
          - fruit_1 == 'pear'
""", use_strategy=use_strategy)
    assert recap == ("4", "0", "0", "0", "0", "0", "1")


@pytest.mark.parametrize("use_strategy", [False, True])
def test_loop_preserves_distinct_registered_results(tmp_path, use_strategy):
    _, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    fruit_names: [apple, pear, orange, plum]
  tasks:
    - shell: printf '%s' "{{ item }}"
      loop: "{{ fruit_names }}"
      register: fruit
    - assert:
        quiet: true
        that: item.stdout == fruit_names[idx]
      loop: "{{ fruit.results }}"
      loop_control:
        index_var: idx
""", use_strategy=use_strategy)
    assert recap == ("2", "1", "0", "0", "0", "0", "0")


def test_mapping_keys_stay_literal(tmp_path):
    msgs, _ = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars: {label: rendered}
  tasks:
    - set_fact:
        mapping:
          '{{ label }}': trusted
          !unsafe '{{ label }}-literal': untrusted
    - debug:
        msg: "{{ mapping.keys() | list | sort | join(',') }}"
""")
    assert msgs == ["{{ label }},{{ label }}-literal"]


def test_play_vars_see_task_vars_include_params_and_loop_bindings(tmp_path):
    msgs, recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    env: prod
    alias: "{{ env }}"
    chained: "{{ alias }}-x"
  tasks:
    - debug: msg="{{ alias }} {{ chained }}"
    - debug: msg="{{ alias }} {{ chained }}"
      vars: {env: task}
    - debug: msg="{{ alias }} {{ chained }}"
      loop: [loop]
      loop_control: {loop_var: env}
    - debug: msg="{{ alias }} {{ chained }}"
      vars: {env: task}
      loop: [a]
      loop_control: {loop_var: env}
    - debug: msg="{{ alias }}"
      vars: {alias: own}
      loop: [b]
      loop_control: {loop_var: env}
    - include_tasks: inc.yml
      vars: {env: param}
    - include_tasks: inc.yml
      loop: [incloop]
      loop_control: {loop_var: env}
    - debug: msg="{{ alias }} {{ chained }}"
""", {"inc.yml": '- debug: msg="{{ alias }} {{ chained }}"\n'})
    assert msgs == ["prod prod-x", "task task-x", "loop loop-x", "a a-x", "own",
                    "param param-x", "incloop incloop-x", "prod prod-x"]


def test_rebound_play_vars_keep_loop_vars_order_explicit_overrides_and_indirect_reads(tmp_path):
    msgs, _recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    full: "{{ env }}-{{ alias }}"
    pkg: "{{ env }}"
    alias: "{{ env }}"
    debug_mode: "{{ env != 'prod' }}"
    cfg: {name: "{{ env }}"}
    indirect: "{{ vars['env'] }}"
    env: prod
  tasks:
    - debug: msg="{{ full }} {{ cfg.name }} {{ indirect }}"
      vars: {env: task}
    - debug: msg="{{ pkg }}"
      vars: {env: task}
      loop: [a, b]
      loop_control: {loop_var: pkg}
    - debug: msg="debug={{ debug_mode }}"
      vars: {env: dev, debug_mode: false}
""")
    assert msgs == ["task-task task task", "a", "b", "debug=False"]


def test_rebound_indirect_and_container_play_vars_follow_chains_declared_before_them(tmp_path):
    msgs, _recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    outer: "{{ lookup('vars', 'via_indirect') }}"
    via_indirect: "{{ indirect }}"
    indirect: "{{ lookup('vars', 'alias') }}"
    cfg: {name: "{{ alias }}"}
    alias: "{{ env }}"
    env: prod
    a: "{{ a_ind }}"
    a_ind: "{{ lookup('vars', 'b') }}"
    b: "{{ b_ind }}"
    b_ind: "{{ lookup('vars', 'env') }}"
  tasks:
    - debug: msg="{{ indirect }} {{ cfg.name }} {{ via_indirect }} {{ outer }} {{ a }}"
      vars: {env: task}
""")
    assert msgs == ["task task task task task"]


def test_task_vars_read_rebound_play_vars(tmp_path):
    msgs, _recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    env: prod
    alias: "{{ env }}"
    full: "{{ alias }}-{{ suffix }}"
  tasks:
    - debug: msg="{{ alias }} {{ label }} {{ full }}"
      vars: {env: task, label: "{{ alias }}", suffix: "{{ label }}"}
""")
    assert msgs == ["task task task-task"]


def test_registered_result_outranks_templated_play_var(tmp_path):
    msgs, _recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    env: prod
    out: "{{ env }}"
  tasks:
    - command: echo hi
      register: out
      changed_when: false
    - debug: msg="{{ out.stdout }}"
    - debug: msg="{{ out.stdout }}"
      vars: {env: task}
- hosts: localhost
  gather_facts: false
  vars:
    out: literal
  tasks:
    - debug: msg="{{ out.stdout }}"
""")
    assert msgs == ["hi", "hi", "hi"]


def test_vars_files_named_from_facts_load_at_runtime(tmp_path):
    msgs, _recap = _assert_parity(tmp_path, """
- hosts: localhost
  vars:
    from_play: play
    os_name: play
  vars_files:
    - "vars/{{ ansible_system | lower }}.yml"
    - ["missing-{{ ansible_system }}.yml", "fallback.yml"]
  tasks:
    - debug: msg="{{ os_name }} {{ derived }} {{ fb }}"
""", {"vars/linux.yml": 'os_name: linux\nderived: "{{ from_play }}-{{ ansible_facts.system }}"\n',
      "vars/darwin.yml": "os_name: darwin\nderived: darwin\n", "fallback.yml": "fb: fallback\n"})
    assert msgs == ["linux play-Linux fallback"]


def test_rebound_play_vars_render_in_order_through_raw_task_vars(tmp_path):
    msgs, _recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  vars:
    env: prod
    x: "{{ label }}"
    alias: "{{ env }}"
  tasks:
    - debug: msg="{{ x }}"
      vars: {env: task, label: "{{ alias }}"}
""")
    assert msgs == ["task"]


def test_vars_files_names_ignore_set_fact_and_register_from_earlier_plays(tmp_path):
    msgs, _recap = _assert_parity(tmp_path, """
- hosts: localhost
  gather_facts: false
  tasks:
    - set_fact: {tier: sf}
    - command: echo hi
      register: reg
      changed_when: false
- hosts: localhost
  gather_facts: false
  vars_files:
    - "vars/{{ reg.stdout }}.yml"
  tasks:
    - debug: msg="{{ pick | default('none') }}"
- hosts: localhost
  vars:
    tier: pv
  vars_files:
    - "vars/{{ tier }}{{ ansible_system[:0] }}.yml"
  tasks:
    - debug: msg="{{ pick }} {{ tier }}"
""", {"vars/sf.yml": "pick: from_set_fact\n", "vars/pv.yml": "pick: from_play\n", "vars/hi.yml": "pick: from_reg\n"})
    assert msgs == ["none", "from_play sf"]
