"""Integration tests that require ansible-core (module embedding, compilation).

Skipped automatically where ansible-core is not importable.
"""

import importlib.util
import base64
import getpass
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import zipfile
from types import SimpleNamespace

import pytest

pytest.importorskip("ansible")

from ansiroll.compiler import _build_bundles, compile_playbook  # noqa: E402
from ansiroll.cli import _load_extra_vars  # noqa: E402
from ansiroll.embed import ModuleEmbedder  # noqa: E402
from ansiroll import embed as embed_module  # noqa: E402
from ansiroll.loader import _register_of, parse_playbook  # noqa: E402
from ansiroll.runtime import Runtime  # noqa: E402
from ansiroll.accelerated_modules import run as run_accelerated  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
ROOT = os.path.dirname(os.path.dirname(__file__))
STRATEGY = os.path.join(
    ROOT,
    "collections",
    "ansible_collections",
    "ansiroll",
    "core",
    "plugins",
    "strategy",
    "ansiroll.py",
)


def _load_strategy_module():
    spec = importlib.util.spec_from_file_location("ansiroll_strategy_test", STRATEGY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _compiled_plays(src, tmp_path):
    artifact = tmp_path / "compiled.py"
    artifact.write_text(src, encoding="utf-8")
    probe = "import json, runpy, sys; print(json.dumps(runpy.run_path(sys.argv[1])['PLAYS']))"
    result = subprocess.run([sys.executable, "-I", "-S", "-c", probe, str(artifact)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ---- module embedding (cross-version core mechanism) --------------------


def test_register_projection_normalizes_221_implicit_shape():
    task = SimpleNamespace(register={"result": "_task.polymorphic_result"})
    assert _register_of(task) == "result"


def test_register_projection_rejects_explicit_projection():
    task = SimpleNamespace(register={"result": "_task.result.rc"})
    with pytest.raises(ValueError, match="explicit register projections"):
        _register_of(task)


def test_legacy_assert_action_lowers_that_as_conditions(tmp_path):
    playbook = tmp_path / "legacy-assert.yml"
    playbook.write_text(
        """- hosts: localhost
  gather_facts: false
  tasks:
    - name: legacy assertion
      action:
        module: ansible.legacy.assert
        args:
          that: "{{ 1 + 1 == 2 }}"
"""
    )

    plays, _actions = parse_playbook(str(playbook))
    task = next(
        node
        for block in plays[0]["tasks"]
        for node in block.get("block", [])
        if node.get("name") == "legacy assertion"
    )

    assert task["special"] == "assert"
    assert task["args"]["that"] == [
        {"__ansiroll_trusted_condition__": "{{ 1 + 1 == 2 }}"}
    ]


def test_unsafe_assert_condition_remains_untrusted(tmp_path):
    playbook = tmp_path / "unsafe-assert.yml"
    playbook.write_text(
        """- hosts: localhost
  gather_facts: false
  tasks:
    - name: unsafe assertion
      ansible.builtin.assert:
        that: !unsafe "{{ dangerous_condition }}"
"""
    )

    plays, _actions = parse_playbook(str(playbook))
    task = next(
        node
        for block in plays[0]["tasks"]
        for node in block.get("block", [])
        if node.get("name") == "unsafe assertion"
    )

    assert task["args"]["that"] == [
        {"__ansiroll_untrusted_condition__": "{{ dangerous_condition }}"}
    ]


def test_embed_and_run_ping():
    emb = ModuleEmbedder()
    meta = emb.embed("ansible.builtin.ping")
    assert meta and meta.get("zip")
    rt = Runtime({"ansible.builtin.ping": meta}, "", {"inventory_hostname": "localhost"})
    res = rt.run_module("ansible.builtin.ping", {"data": "pong"})
    assert res.get("ping") == "pong"


def test_module_embedding_deduplicates_aliases_within_one_compile(monkeypatch):
    calls = []
    monkeypatch.setattr(embed_module, "make_templar", lambda: object())
    monkeypatch.setattr(
        embed_module,
        "find_module",
        lambda _action: ("/modules/command.py", "ansible.modules.command"),
    )
    monkeypatch.setattr(
        embed_module,
        "build_module",
        lambda **kwargs: calls.append(kwargs) or (b"wrapper", "legacy"),
    )
    monkeypatch.setattr(
        embed_module,
        "_extract",
        lambda _wrapper: ("encoded-zip", "ansible.modules.command"),
    )

    embedder = ModuleEmbedder()
    command = embedder.embed("command")
    shell = embedder.embed("shell")

    assert command is shell
    assert len(calls) == 1


def test_embed_and_run_command():
    emb = ModuleEmbedder()
    meta = emb.embed("command")
    rt = Runtime({"command": meta}, "", {"inventory_hostname": "localhost"})
    res = rt.run_module("command", {"_raw_params": "echo hello-ansiroll"})
    assert res.get("rc") == 0
    assert "hello-ansiroll" in res.get("stdout", "")


@pytest.mark.parametrize("action", ["command", "shell"])
def test_accelerated_command_matches_official_module_semantics(action):
    emb = ModuleEmbedder()
    meta = emb.embed(action)
    rt = Runtime({action: meta}, "", {"inventory_hostname": "localhost"})
    args = {"_raw_params": "printf accelerator-parity"}
    try:
        official = rt.run_module(action, args)
    finally:
        rt.cleanup()
    accelerated = run_accelerated(action, args)

    for key in ("changed", "failed", "rc", "stdout", "stderr",
                "stdout_lines", "stderr_lines"):
        assert accelerated.get(key) == official.get(key)


def _command_parity_cases(base):
    os.makedirs(os.path.join(base, "sub"))
    open(os.path.join(base, "sub", "marker"), "w").close()
    return [
        # chdir is applied (and ~-expanded) before creates/removes.
        ("command", {"_raw_params": "echo ran", "chdir": os.path.join(base, "sub"),
                     "creates": "marker"}),
        ("command", {"_raw_params": "pwd", "chdir": "~"}),
        ("command", {"_raw_params": "true", "chdir": os.path.join(base, "nope")}),
        ("command", {"_raw_params": "echo x", "creates": "~/"}),
        # argv gets ~ and $VAR expansion, the reported cmd does not.
        ("command", {"_raw_params": "echo ~ $HOME '$HOME' ~root/x"}),
        # Bytes output: no newline translation, surrogateescape decoding.
        ("shell", {"_raw_params": "printf '\\377ok\\r\\n'", "_uses_shell": True}),
        ("shell", {"_raw_params": "printf 'a\\r\\nb\\r\\n'", "_uses_shell": True,
                   "strip_empty_ends": False}),
        # python -c 'import ...' is run as a real process.
        ("command", {"_raw_params": "%s -c 'import ansiroll_no_such_mod'" % sys.executable}),
        ("command", {"_raw_params": "ansiroll_no_such_cmd_xyz --password=x -p y"}),
        ("command", {"_raw_params": "./ansiroll-relative"}),
        ("shell", {"_raw_params": "echo $0 x", "_uses_shell": True,
                   "executable": "/bin/bash"}),
        ("shell", {"_raw_params": "echo $0 x", "_uses_shell": True,
                   "executable": "/nonexistent/sh"}),
    ]


def test_accelerated_command_edge_cases_match_official(tmp_path):
    from ansiroll.loader import _accelerator_options

    cases = _command_parity_cases(str(tmp_path))
    emb = ModuleEmbedder()
    rt = Runtime({"command": emb.embed("command"), "shell": emb.embed("shell")},
                 "", {"inventory_hostname": "localhost"})
    skip = ("start", "end", "delta", "invocation", "exception")
    try:
        for action, args in cases:
            official = rt.run_module(action, dict(args))
            accelerated = run_accelerated(
                action, dict(args), options=_accelerator_options(action))
            official = {k: v for k, v in official.items() if k not in skip}
            accelerated = {k: v for k, v in accelerated.items() if k not in skip}
            assert accelerated == official, (action, args)
    finally:
        rt.cleanup()


@pytest.mark.parametrize("search_only", [False, True])
def test_accelerated_command_guards_use_task_environment_and_literal_chdir(
        tmp_path, monkeypatch, search_only):
    from ansiroll.loader import _accelerator_options

    work = tmp_path / "literal[1]"
    work.mkdir()
    (work / "guard.txt").touch()
    (tmp_path / "absolute.txt").touch()
    monkeypatch.setenv("ANSIROLL_GUARD", "missing")
    monkeypatch.setenv("ANSIROLL_COMMAND_PATH", str(tmp_path))
    environment = {
        "ANSIROLL_GUARD": "guard.*",
        "ANSIROLL_COMMAND_PATH": str(work),
    }
    cases = [
        ({"creates": "$ANSIROLL_GUARD"}, False),
        ({"removes": "$ANSIROLL_GUARD"}, True),
        ({"removes": "missing"}, False),
        ({"creates": str(tmp_path / "absolute.txt")}, False),
    ]
    start = tmp_path / "start"
    start.mkdir()
    monkeypatch.chdir(start)
    if search_only:
        if os.geteuid() == 0:
            pytest.skip("root can read a search-only working directory")
        start.chmod(0o111)
    rt = Runtime({"command": ModuleEmbedder().embed("command")},
                 "", {"inventory_hostname": "localhost"})
    rt._module_env = environment
    cwd = os.getcwd()
    try:
        for guard, changed in cases:
            args = dict(guard, chdir="$ANSIROLL_COMMAND_PATH",
                        _raw_params="printf %s $ANSIROLL_GUARD")
            official = rt.run_module("command", dict(args))
            accelerated = run_accelerated(
                "command", args, environment=environment,
                options=_accelerator_options("command"))
            ignored = ("start", "end", "delta", "invocation")
            assert ({k: v for k, v in accelerated.items() if k not in ignored}
                    == {k: v for k, v in official.items() if k not in ignored})
            assert accelerated["changed"] is changed
            if changed:
                assert accelerated["stdout"] == "guard.*"
            assert os.getcwd() == cwd
            assert os.environ["ANSIROLL_GUARD"] == "missing"
    finally:
        start.chmod(0o700)
        rt.cleanup()


def test_compiled_command_loop_restores_search_only_cwd(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can read a search-only working directory")
    start = tmp_path / "start"
    start.mkdir()
    guarded = tmp_path / "literal[1]"
    guarded.mkdir()
    (guarded / "marker").touch()
    unguarded = tmp_path / "literal[2]"
    unguarded.mkdir()
    playbook = tmp_path / "guards.yml"
    playbook.write_text(
        """- hosts: localhost
  gather_facts: false
  tasks:
    - command:
        cmd: pwd
        chdir: "{{ item }}"
        creates: mark*
      loop: %s
    - command: pwd
""" % json.dumps([str(guarded), str(unguarded)]))
    script = tmp_path / "compiled.py"
    script.write_text(compile_playbook(str(playbook))[0])
    start.chmod(0o111)
    try:
        proc = subprocess.run([sys.executable, str(script)], cwd=str(start),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, ANSIROLL_RAW="1",
                                       ANSIROLL_ACCELERATED_MODULES="command"),
                              universal_newlines=True)
    finally:
        start.chmod(0o700)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    prefix = "__ANSIROLL_RESULT__ "
    events = [json.loads(line[len(prefix):]) for line in proc.stdout.splitlines()
              if line.startswith(prefix)]
    skipped, executed = [event["result"] for event in events if event["is_item"]]
    following = [event["result"] for event in events if not event["is_item"]][-1]
    assert skipped["changed"] is False
    assert executed["changed"] is True
    assert executed["stdout"] == str(unguarded)
    assert following["stdout"] == str(start)


@pytest.mark.parametrize("action,chdir", [("command", None), ("command", 0), ("shell", False)])
def test_compiled_command_falls_back_when_cwd_loses_search_permission(tmp_path, action, chdir):
    if os.geteuid() == 0:
        pytest.skip("root can search a directory without permissions")
    start = tmp_path / "start"
    start.mkdir()
    target = tmp_path / "literal[1]"
    target.mkdir()
    (target / "marker").touch()
    playbook = tmp_path / "guard.yml"
    playbook.write_text(
        """- hosts: localhost
  gather_facts: false
  tasks:
    - file:
        path: %s
        state: directory
        mode: "0000"
    - %s:
        cmd: pwd
        chdir: %s
        creates: marker
      ignore_errors: true
""" % (json.dumps(str(start)), action,
       json.dumps(str(target) if chdir is None else chdir)))
    script = tmp_path / "compiled.py"
    script.write_text(compile_playbook(str(playbook))[0])
    try:
        proc = subprocess.run(
            [sys.executable, str(script)], cwd=str(start),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=dict(os.environ, ANSIROLL_RAW="1", ANSIROLL_ACCELERATED_MODULES=action),
            universal_newlines=True,
        )
    finally:
        start.chmod(0o700)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    prefix = "__ANSIROLL_RESULT__ "
    result = [json.loads(line[len(prefix):])["result"] for line in proc.stdout.splitlines()
              if line.startswith(prefix)][-1]
    assert result["changed"] is False
    if chdir is None:
        assert result["rc"] == 0
        assert "since marker exists" in result["stdout"]
    else:
        assert result["failed"] is True
        assert result["msg"].startswith("Unable to change directory before execution")


def test_accelerated_command_matches_legacy_warn_semantics():
    import ansible

    if tuple(map(int, ansible.__version__.split(".")[:2])) >= (2, 11):
        pytest.skip("the command warn argument was removed after ansible-base 2.10")
    args = {"_raw_params": "printf accelerator-warn-parity", "warn": False}
    meta = ModuleEmbedder().embed("command")
    rt = Runtime({"command": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("command", args)
    finally:
        rt.cleanup()
    accelerated = run_accelerated("command", args)

    for key in ("changed", "failed", "rc", "stdout", "stderr",
                "stdout_lines", "stderr_lines"):
        assert accelerated.get(key) == official.get(key)


def test_accelerated_apt_absent_matches_official_module_semantics():
    package = "ansiroll-package-that-does-not-exist"
    args = {
        "pkg": [package],
        "state": "absent",
        "purge": True,
        "autoclean": True,
        "autoremove": True,
    }
    system_python = "/usr/bin/python3"
    if subprocess.run(
            [system_python, "-c", "import apt"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL).returncode != 0:
        pytest.skip("system Python does not provide python3-apt")
    emb = ModuleEmbedder()
    meta = emb.embed("apt")
    rt = Runtime(
        {"apt": meta}, "", {"inventory_hostname": "localhost"},
        python=system_python,
    )
    try:
        official = rt.run_module("apt", args)
    finally:
        rt.cleanup()
    accelerated = run_accelerated("apt", args)

    assert accelerated.get("failed") == official.get("failed")
    assert accelerated["changed"] == official["changed"] is False


def test_accelerated_apt_remove_uses_official_apt_environment(tmp_path):
    """apt-get runs with the official module's APT_ENV_VARS; fake dpkg-query and
    apt-get on the task PATH record it, so no package state is touched."""
    import ansible
    from ansiroll.loader import _accelerator_options

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "env.json"
    (bin_dir / "dpkg-query").write_text(
        "#!/bin/sh\nprintf 'ansiroll-fake\\tii \\n'\n")
    (bin_dir / "apt-get").write_text(
        "#!%s\nimport json, os, sys\n"
        "json.dump({'argv': sys.argv[1:], 'env': dict(os.environ)}, open(%r, 'w'))\n"
        % (sys.executable, str(record)))
    for name in ("dpkg-query", "apt-get"):
        os.chmod(str(bin_dir / name), 0o755)
    environment = {
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        "LANG": "es_ES.UTF-8", "LC_ALL": "es_ES.UTF-8", "LANGUAGE": "es",
        "DEBIAN_FRONTEND": "dialog",
    }
    result = run_accelerated(
        "apt", {"name": "ansiroll-fake", "state": "absent"},
        environment=environment, options=_accelerator_options("apt"))
    assert result.get("changed") is True, result

    class _Module(object):
        def get_bin_path(self, name):
            return shutil.which(name, path=environment["PATH"])

        def run_command(self, args):
            proc = subprocess.run(args, capture_output=True, text=True,
                                  env=dict(os.environ, **environment))
            return proc.returncode, proc.stdout, proc.stderr

        def debug(self, msg):
            pass

    source = open(os.path.join(
        os.path.dirname(ansible.__file__), "modules", "apt.py")).read()
    block = re.search(r"APT_ENV_VARS = dict\((.*?)\)", source, re.S).group(1)
    locale = None
    if re.search(r"=\s*locale\b", block):
        from ansible.module_utils.common.locale import get_best_parsable_locale
        locale = get_best_parsable_locale(_Module())
    expected = {}
    for key, value in re.findall(r"(\w+)=([^,\n]+)", block):
        expected[key] = locale if value.strip() == "locale" else value.strip("'\" ")
    assert "LANG" in expected and "DEBIAN_FRONTEND" in expected
    recorded = json.load(open(str(record)))
    assert {key: recorded["env"].get(key) for key in expected} == expected
    assert recorded["env"]["LANGUAGE"] == expected.get("LANGUAGE", "es")
    assert recorded["argv"][-2:] == ["remove", "ansiroll-fake"]


def test_accelerated_slurp_matches_official_module_semantics(tmp_path):
    source = tmp_path / "binary"
    source.write_bytes(b"\x00quotes'\"slashes\\\xff")
    args = {"src": str(source)}
    emb = ModuleEmbedder()
    meta = emb.embed("slurp")
    rt = Runtime({"slurp": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("slurp", args)
    finally:
        rt.cleanup()
    from ansible.release import __version__ as ansible_version
    version = tuple(map(int, ansible_version.split(".")[:2]))
    accelerated = run_accelerated(
        "slurp",
        args,
        options={
            "lowercase_not_found": version < (2, 19),
            "typed_exceptions": version >= (2, 19),
        },
    )

    expected = {
        key: value for key, value in official.items()
        if key not in ("invocation",)
    }
    assert accelerated == expected


def test_accelerated_slurp_missing_file_matches_official_failure(tmp_path):
    from ansible.release import __version__ as ansible_version

    source = tmp_path / "missing"
    args = {"src": str(source)}
    meta = ModuleEmbedder().embed("slurp")
    rt = Runtime({"slurp": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("slurp", args)
    finally:
        rt.cleanup()
    version = tuple(map(int, ansible_version.split(".")[:2]))
    accelerated = run_accelerated(
        "slurp",
        args,
        options={
            "lowercase_not_found": version < (2, 19),
            "typed_exceptions": version >= (2, 19),
        },
    )

    expected = {
        key: value for key, value in official.items()
        if key not in ("invocation",)
    }
    assert accelerated == expected


def _translated_locale():
    """A non-English UTF-8 locale whose strerror() differs from English."""
    try:
        names = subprocess.run(
            ["locale", "-a"], stdout=subprocess.PIPE,
            universal_newlines=True).stdout.split()
    except OSError:
        return None
    for name in names:
        if name.lower().endswith((".utf8", ".utf-8")) and \
                not name.startswith(("C.", "en_", "POSIX")):
            return name
    return None


@pytest.mark.parametrize("task_override", [False, True])
def test_accelerated_modules_apply_locale_like_ansible_module(tmp_path, task_override):
    # AnsibleModule._check_locale() (module_utils/basic.py) runs
    # setlocale(LC_ALL, ''), so official error texts follow the user's locale.
    name = _translated_locale()
    if name is None:
        pytest.skip("no translated UTF-8 locale installed")
    script = (
        "import json, sys\n"
        "from ansiroll.embed import ModuleEmbedder\n"
        "from ansiroll.runtime import Runtime\n"
        "from ansiroll.accelerated_modules import run\n"
        "args = {'src': sys.argv[1]}\n"
        "rt = Runtime({'slurp': ModuleEmbedder().embed('slurp')}, '',\n"
        "             {'inventory_hostname': 'localhost'})\n"
        "try:\n"
        "    task_env = {'LC_ALL': sys.argv[2], 'LANGUAGE': ''} if sys.argv[2] else None\n"
        "    rt._module_env = task_env\n"
        "    official = rt.run_module('slurp', args)\n"
        "finally:\n"
        "    rt.cleanup()\n"
        "official.pop('invocation', None)\n"
        "from ansible.release import __version__ as v\n"
        "new = tuple(map(int, v.split('.')[:2])) >= (2, 19)\n"
        "accelerated = run('slurp', args, environment=task_env, options={\n"
        "    'lowercase_not_found': not new, 'typed_exceptions': new})\n"
        "print(json.dumps([official, accelerated]))\n"
    )
    inherited = "C" if task_override else name
    env = dict(os.environ, LANG=inherited, LC_ALL=inherited, LANGUAGE="")
    env["PYTHONPATH"] = os.pathsep.join(
        [os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
         env.get("PYTHONPATH", "")])
    proc = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "missing"),
         name if task_override else ""],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, check=True)
    official, accelerated = json.loads(proc.stdout.splitlines()[-1])
    assert accelerated == official


@pytest.mark.parametrize("path", [None, ""])
def test_accelerated_shell_locale_fallback_matches_official(monkeypatch, path):
    import locale
    from ansiroll.loader import _accelerator_options

    monkeypatch.setenv("LC_ALL", "C")
    original_locale = locale.setlocale(locale.LC_ALL)
    original_env = dict(os.environ)
    args = {
        "_raw_params": 'printf "%s\n" "$LANG" "$LC_ALL" "$LC_MESSAGES"',
        "_uses_shell": True,
    }
    environment = {"LC_ALL": "ansiroll_invalid_locale"}
    if path is not None:
        environment["PATH"] = path
    rt = Runtime({"shell": ModuleEmbedder().embed("shell")},
                 "", {"inventory_hostname": "localhost"})
    rt._module_env = environment
    try:
        official = rt.run_module("shell", dict(args))
        accelerated = run_accelerated(
            "shell", args, environment=environment, options=_accelerator_options("shell"))
        following = run_accelerated("shell", args)
    finally:
        rt.cleanup()
    assert accelerated["stdout_lines"] == official["stdout_lines"]
    fallback = official["stdout_lines"][0]
    assert fallback and fallback != environment["LC_ALL"]
    assert accelerated["stdout_lines"] == [fallback] * 3
    assert following["stdout_lines"][1] == "C"
    assert locale.setlocale(locale.LC_ALL) == original_locale
    assert dict(os.environ) == original_env


@pytest.mark.parametrize("initial,args", [
    (
        "before\nold=one\nold=two\nafter\n",
        {"regexp": r"^old=.*$", "replace": "new=value"},
    ),
    (
        "header\nSTART\nold=one\nEND\nold=outside\n",
        {
            "regexp": r"^old=.*$", "replace": "new=value",
            "after": "^START$", "before": "^END$",
        },
    ),
])
def test_accelerated_replace_matches_official_module_semantics(
        tmp_path, initial, args):
    from ansiroll.loader import _accelerator_options

    official_path = tmp_path / "official"
    accelerated_path = tmp_path / "accelerated"
    official_path.write_text(initial)
    accelerated_path.write_text(initial)
    emb = ModuleEmbedder()
    meta = emb.embed("replace")
    rt = Runtime({"replace": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module(
            "replace", dict(args, path=str(official_path)))
    finally:
        rt.cleanup()
    accelerated = run_accelerated(
        "replace",
        dict(args, path=str(accelerated_path)),
        options=_accelerator_options("replace"),
    )

    official.pop("invocation", None)
    assert accelerated == official
    assert accelerated_path.read_bytes() == official_path.read_bytes()


@pytest.mark.parametrize("action,args", [
    ("replace", {"regexp": "^old=.*$", "replace": "new=2"}),
    ("blockinfile", {"insertafter": "EOF", "block": "new=2"}),
])
def test_accelerated_atomic_write_matches_official_when_chown_is_denied(
        tmp_path, monkeypatch, action, args):
    """Non-root replacing a file it may not chown back (another owner, or a
    group it is not in): atomic_move ignores EPERM and still writes."""
    import errno as errno_module
    from ansiroll.loader import _accelerator_options

    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "sitecustomize.py").write_text(
        "import errno, os\n"
        "def _denied(*args, **kwargs):\n"
        "    raise OSError(errno.EPERM, 'Operation not permitted')\n"
        "os.chown = _denied\n"
    )
    official_path = tmp_path / "official"
    accelerated_path = tmp_path / "accelerated"
    for path in (official_path, accelerated_path):
        path.write_text("old=1\n")
        os.chmod(str(path), 0o644)
    monkeypatch.setenv("PYTHONPATH", str(hooks))
    meta = ModuleEmbedder().embed(action)
    rt = Runtime({action: meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module(action, dict(args, path=str(official_path)))
    finally:
        rt.cleanup()
    monkeypatch.delenv("PYTHONPATH")

    def denied(*args, **kwargs):
        raise OSError(errno_module.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "chown", denied)
    accelerated = run_accelerated(
        action, dict(args, path=str(accelerated_path)),
        options=_accelerator_options(action))

    official.pop("invocation", None)
    assert official["changed"] is True
    for result, path in ((official, official_path), (accelerated, accelerated_path)):
        if "diff" in result:
            result["diff"] = json.loads(json.dumps(result["diff"]).replace(str(path), "<path>"))
    assert accelerated == official
    assert accelerated_path.read_bytes() == official_path.read_bytes()
    assert b"new=2" in accelerated_path.read_bytes()
    assert (oct(os.stat(str(accelerated_path)).st_mode & 0o7777)
            == oct(os.stat(str(official_path)).st_mode & 0o7777))


def test_accelerated_file_directory_matches_official_idempotence(tmp_path):
    official_path = tmp_path / "official"
    accelerated_path = tmp_path / "accelerated"
    emb = ModuleEmbedder()
    meta = emb.embed("file")
    rt = Runtime({"file": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official_first = rt.run_module(
            "file", {"path": str(official_path), "state": "directory", "mode": "0750"}
        )
        official_second = rt.run_module(
            "file", {"path": str(official_path), "state": "directory", "mode": "0750"}
        )
    finally:
        rt.cleanup()
    accelerated_first = run_accelerated(
        "file", {"path": str(accelerated_path), "state": "directory", "mode": "0750"}
    )
    accelerated_second = run_accelerated(
        "file", {"path": str(accelerated_path), "state": "directory", "mode": "0750"}
    )

    assert accelerated_first.get("failed") == official_first.get("failed")
    assert accelerated_first["changed"] == official_first["changed"] is True
    assert accelerated_second["changed"] == official_second["changed"] is False
    assert (official_path.stat().st_mode & 0o7777) == 0o750
    assert (accelerated_path.stat().st_mode & 0o7777) == 0o750


def test_accelerated_file_results_and_created_parents_match_official(tmp_path):
    import stat as stat_module
    from ansiroll.accelerated_modules import can_accelerate
    from ansiroll.loader import _accelerator_options

    cases = [
        # mode applies to every directory mkdir -p creates.
        ({"path": "a/b/c", "state": "directory", "mode": "0750"}, False),
        ({"path": "a/b/c", "state": "directory", "mode": "0750"}, False),
        ({"path": "existing/new/../target", "state": "directory", "mode": "0700"}, False),
        ({"path": "q/r", "state": "directory", "mode": "g+w"}, False),
        # mode/owner/size/... are reported even when not requested.
        ({"path": "m/n", "state": "directory"}, False),
        ({"path": "exist", "state": "directory", "mode": "0700"}, False),
        ({"path": "exist", "state": "directory", "owner": str(os.getuid())}, False),
        # A regular file is not a directory, in check mode too.
        ({"path": "regular", "state": "directory"}, True),
        ({"path": "regular", "state": "directory"}, False),
        ({"path": "newdir", "state": "directory"}, True),
        ({"path": "t1", "state": "touch"}, False),
        ({"path": "t1", "state": "touch", "mode": "0600"}, False),
        ({"path": "t1", "state": "touch", "mode": "0644"}, True),
        ({"path": "missing-touch", "state": "touch"}, True),
        ({"path": "t3", "state": "touch", "modification_time": "preserve",
          "access_time": "preserve"}, False),
        ({"path": "lnk", "state": "absent"}, True),
        ({"path": "l2", "src": "exist", "state": "link"}, False),
        ({"path": "l2", "src": "exist", "state": "link"}, False),
        ({"path": "regular", "state": "absent"}, False),
        ({"path": "missing", "state": "absent"}, False),
    ]
    from ansible.release import __version__ as ansible_version
    if tuple(map(int, ansible_version.split(".")[:2])) < (2, 19):
        # Before 2.19 the official module raises AnsibleModuleError for a
        # directory over a regular file and relies on its sys.excepthook to
        # emit fail_json, which Runtime's embedded execution does not install;
        # ansible-playbook reports the fail_json the accelerated module returns.
        cases = [case for case in cases
                 if case[0] != {"path": "regular", "state": "directory"}]
    for side in ("official", "accelerated"):
        base = tmp_path / side
        (base / "exist").mkdir(parents=True)
        (base / "existing").mkdir()
        os.chmod(str(base / "existing"), 0o775)
        (base / "regular").write_text("")
        os.chmod(str(base / "regular"), 0o644)
        os.symlink("regular", str(base / "lnk"))

    def tree(base):
        out = {}
        for root, dirs, files in os.walk(base):
            for name in dirs + files:
                full = os.path.join(root, name)
                out[os.path.relpath(full, base)] = oct(
                    stat_module.S_IMODE(os.lstat(full).st_mode))
        return out

    def normalized(result, base):
        result = {k: v for k, v in result.items() if k not in ("invocation", "diff")}
        return json.loads(json.dumps(result).replace(base, "<D>"))

    rt = Runtime({"file": ModuleEmbedder().embed("file")},
                 "", {"inventory_hostname": "localhost"})
    try:
        for args, check in cases:
            official_base = str(tmp_path / "official")
            accelerated_base = str(tmp_path / "accelerated")
            official_args = dict(args, path=os.path.join(official_base, args["path"]))
            accelerated_args = dict(
                args, path=os.path.join(accelerated_base, args["path"]))
            assert can_accelerate("file", dict(accelerated_args), check_mode=check)
            rt.check_mode = check
            official = rt.run_module("file", official_args)
            accelerated = run_accelerated(
                "file", accelerated_args, check_mode=check,
                options=_accelerator_options("file"))
            assert not accelerated.get("_ansiroll_fallback"), args
            assert (normalized(accelerated, accelerated_base)
                    == normalized(official, official_base)), (args, check)
            assert tree(accelerated_base) == tree(official_base), (args, check)
    finally:
        rt.cleanup()


def test_accelerated_file_paths_use_task_environment(tmp_path, monkeypatch):
    from ansiroll.loader import _accelerator_options

    args = {"path": "$ANSIROLL_TARGET", "state": "absent"}
    link = {"path": "~/link", "src": "$ANSIROLL_SOURCE", "state": "link"}
    rt = Runtime({"file": ModuleEmbedder().embed("file")},
                 "", {"inventory_hostname": "localhost"})
    try:
        for side in ("official", "accelerated"):
            base = tmp_path / side
            base.mkdir()
            (base / "stale").touch()
            (base / "wanted").touch()
            monkeypatch.setenv("ANSIROLL_TARGET", str(base / "stale"))
            environment = {
                "HOME": str(base),
                "ANSIROLL_TARGET": str(base / "wanted"),
                "ANSIROLL_SOURCE": "stale",
            }
            rt._module_env = environment
            if side == "official":
                absent = rt.run_module("file", dict(args))
                linked = rt.run_module("file", dict(link))
            else:
                options = _accelerator_options("file")
                absent = run_accelerated(
                    "file", args, environment=environment, options=options)
                linked = run_accelerated(
                    "file", link, environment=environment, options=options)
            assert absent["changed"] is True
            assert linked["changed"] is True
            assert not (base / "wanted").exists()
            assert (base / "stale").is_file()
            assert os.readlink(str(base / "link")) == "stale"
            assert os.environ["ANSIROLL_TARGET"] == str(base / "stale")
    finally:
        rt.cleanup()


@pytest.mark.parametrize(
    "state", ["directory", "recursive_directory", "new_directory", "absent", "touch", "link"])
def test_file_nested_path_expansion_matches_official(tmp_path, state):
    from ansiroll.loader import _accelerator_options

    args = {"path": "$ANSIROLL_FIRST", "state": state}
    if state in ("new_directory", "recursive_directory"):
        args["state"] = "directory"
    if state in ("directory", "new_directory", "touch"):
        args["mode"] = "0700"
    if state == "recursive_directory":
        args.update(mode="g+w", recurse=True)
    if state == "touch":
        args.update(access_time="preserve", modification_time="preserve")
    if state == "link":
        args["src"] = "$ANSIROLL_SOURCE"
    rt = Runtime({"file": ModuleEmbedder().embed("file")},
                 "", {"inventory_hostname": "localhost"})
    try:
        for side in ("official", "accelerated"):
            base = tmp_path / side
            base.mkdir()
            literal = base / "$ANSIROLL_SECOND"
            actual = base / "$ANSIROLL_THIRD"
            actual.mkdir()
            actual.chmod(0o755)
            third = base / "third"
            third.mkdir()
            third.chmod(0o755)
            if state in ("directory", "recursive_directory", "absent", "touch"):
                literal.mkdir()
                literal.chmod(0o755)
            if state == "recursive_directory":
                for directory in (literal, actual):
                    child = directory / "child"
                    child.touch()
                    child.chmod(0o644)
            (base / "$ANSIROLL_TARGET").touch()
            environment = {
                "ANSIROLL_FIRST": str(literal),
                "ANSIROLL_SECOND": "$ANSIROLL_THIRD",
                "ANSIROLL_THIRD": "third",
                "ANSIROLL_SOURCE": "$ANSIROLL_TARGET",
                "ANSIROLL_TARGET": "missing",
            }
            rt._module_env = environment
            if side == "accelerated":
                result = run_accelerated("file", args, environment=environment,
                                         options=_accelerator_options("file"))
                fallback = state in ("touch", "link")
                assert bool(result.get("_ansiroll_fallback")) is fallback
                if fallback:
                    assert literal.exists() is (state == "touch")
                    assert actual.stat().st_mode & 0o777 == 0o755
                    result = rt.run_module("file", dict(args))
            else:
                result = rt.run_module("file", dict(args))
            assert result["changed"] and not result.get("failed"), result
            if state == "absent":
                assert not literal.exists()
            elif state == "link":
                assert os.readlink(str(literal)) == "$ANSIROLL_TARGET"
            else:
                assert literal.stat().st_mode & 0o777 == (
                    0o700 if state == "new_directory" else 0o755)
            if state == "recursive_directory":
                assert actual.stat().st_mode & 0o777 == 0o775
                assert (actual / "child").stat().st_mode & 0o777 == 0o644
                assert (literal / "child").stat().st_mode & 0o777 == 0o664
            else:
                assert actual.stat().st_mode & 0o777 == (
                    0o700 if state in ("directory", "touch") else 0o755)
            assert third.stat().st_mode & 0o777 == 0o755
    finally:
        rt.cleanup()


def test_accelerated_file_attribute_free_recurse_matches_official(tmp_path):
    official_path = tmp_path / "official"
    accelerated_path = tmp_path / "accelerated"
    args = {"state": "directory", "recurse": True}
    emb = ModuleEmbedder()
    meta = emb.embed("file")
    rt = Runtime({"file": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official_first = rt.run_module(
            "file", dict(args, path=str(official_path)))
        official_second = rt.run_module(
            "file", dict(args, path=str(official_path)))
    finally:
        rt.cleanup()
    accelerated_first = run_accelerated(
        "file", dict(args, path=str(accelerated_path)))
    accelerated_second = run_accelerated(
        "file", dict(args, path=str(accelerated_path)))

    assert accelerated_first["changed"] == official_first["changed"] is True
    assert accelerated_second["changed"] == official_second["changed"] is False


def test_accelerated_file_link_and_preserved_touch_match_official(tmp_path):
    source = tmp_path / "source"
    source.write_text("source")
    trials = [
        (
            {"state": "link", "src": str(source), "force": True},
            "official-link",
            "accelerated-link",
        ),
        (
            {
                "state": "touch", "mode": "0644",
                "modification_time": "preserve", "access_time": "preserve",
            },
            "official-touch",
            "accelerated-touch",
        ),
    ]
    emb = ModuleEmbedder()
    meta = emb.embed("file")
    rt = Runtime({"file": meta}, "", {"inventory_hostname": "localhost"})
    try:
        for args, official_name, accelerated_name in trials:
            official_path = tmp_path / official_name
            accelerated_path = tmp_path / accelerated_name
            if args["state"] == "touch":
                official_path.write_text("existing")
                accelerated_path.write_text("existing")
            official = rt.run_module(
                "file", dict(args, path=str(official_path)))
            accelerated = run_accelerated(
                "file", dict(args, path=str(accelerated_path)))
            assert accelerated["changed"] == official["changed"]
    finally:
        rt.cleanup()


def test_accelerated_file_retargets_existing_symlink_without_force(tmp_path):
    official_old = tmp_path / "official-old"
    official_new = tmp_path / "official-new"
    accelerated_old = tmp_path / "accelerated-old"
    accelerated_new = tmp_path / "accelerated-new"
    for source in (
            official_old, official_new, accelerated_old, accelerated_new):
        source.mkdir()
    official_path = tmp_path / "official-current"
    accelerated_path = tmp_path / "accelerated-current"
    official_path.symlink_to(official_old)
    accelerated_path.symlink_to(accelerated_old)

    emb = ModuleEmbedder()
    meta = emb.embed("file")
    rt = Runtime({"file": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("file", {
            "path": str(official_path),
            "state": "link",
            "src": str(official_new),
        })
    finally:
        rt.cleanup()
    accelerated = run_accelerated("file", {
        "path": str(accelerated_path),
        "state": "link",
        "src": str(accelerated_new),
    })

    assert accelerated.get("failed") == official.get("failed")
    assert accelerated["changed"] == official["changed"] is True
    assert os.readlink(accelerated_path) == str(accelerated_new)


def test_accelerated_file_refuses_unsafe_symlink_conversions(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    regular_file = tmp_path / "regular"
    regular_file.write_text("keep")
    nonempty_directory = tmp_path / "directory"
    nonempty_directory.mkdir()
    (nonempty_directory / "keep").write_text("keep")

    regular_result = run_accelerated("file", {
        "path": str(regular_file),
        "state": "link",
        "src": str(source),
    })
    directory_result = run_accelerated("file", {
        "path": str(nonempty_directory),
        "state": "link",
        "src": str(source),
        "force": True,
    })

    assert regular_result["failed"] is True
    assert regular_file.read_text() == "keep"
    assert directory_result["failed"] is True
    assert (nonempty_directory / "keep").read_text() == "keep"


def test_accelerated_blockinfile_matches_official_idempotence(tmp_path):
    official_path = tmp_path / "official"
    accelerated_path = tmp_path / "accelerated"
    initial = "existing=true\n"
    official_path.write_text(initial)
    accelerated_path.write_text(initial)
    args = {"insertafter": "EOF", "block": "set linenumbers\n"}
    emb = ModuleEmbedder()
    meta = emb.embed("blockinfile")
    rt = Runtime(
        {"blockinfile": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official_first = rt.run_module(
            "blockinfile", dict(args, path=str(official_path)))
        official_second = rt.run_module(
            "blockinfile", dict(args, path=str(official_path)))
    finally:
        rt.cleanup()
    accelerated_first = run_accelerated(
        "blockinfile", dict(args, path=str(accelerated_path)))
    accelerated_second = run_accelerated(
        "blockinfile", dict(args, path=str(accelerated_path)))

    official_first.pop("invocation", None)
    official_second.pop("invocation", None)
    official_first["diff"] = json.loads(
        json.dumps(official_first["diff"]).replace(str(official_path), "<path>"))
    official_second["diff"] = json.loads(
        json.dumps(official_second["diff"]).replace(str(official_path), "<path>"))
    accelerated_first["diff"] = json.loads(
        json.dumps(accelerated_first["diff"]).replace(str(accelerated_path), "<path>"))
    accelerated_second["diff"] = json.loads(
        json.dumps(accelerated_second["diff"]).replace(str(accelerated_path), "<path>"))

    assert accelerated_first == official_first
    assert accelerated_second == official_second
    assert accelerated_path.read_bytes() == official_path.read_bytes()


def test_accelerated_blockinfile_create_mode_and_line_splitting_match_official(
        tmp_path):
    import stat as stat_module
    from ansiroll.loader import _accelerator_options

    files = {
        # \x0c, \x1c and U+0085 split str lines but not bytes lines.
        "ff": b"a\x0cb\nc\xc2\x85d\x1ce\n",
        "crlf": b"one\r\ntwo\r\n",
    }
    cases = [
        {"path": "new", "create": True, "block": "x"},
        {"path": "sub/dir/new", "create": True, "block": "x"},
        {"path": "ff", "block": "x", "insertbefore": "b"},
        {"path": "ff", "block": "y"},
        {"path": "crlf", "block": "z", "insertafter": "one"},
    ]
    for side in ("official", "accelerated"):
        (tmp_path / side).mkdir()
        for name, data in files.items():
            (tmp_path / side / name).write_bytes(data)
    rt = Runtime({"blockinfile": ModuleEmbedder().embed("blockinfile")},
                 "", {"inventory_hostname": "localhost"})
    options = _accelerator_options("blockinfile")

    def outcome(result, side, rel):
        base = str(tmp_path / side)
        result = json.loads(json.dumps(
            {k: v for k, v in result.items() if k != "invocation"}
        ).replace(base, "<D>"))
        path = os.path.join(base, rel)
        mode = stat_module.S_IMODE(os.stat(path).st_mode)
        with open(path, "rb") as handle:
            return result, oct(mode), handle.read()

    try:
        for case in cases:
            official = rt.run_module("blockinfile", dict(
                case, path=str(tmp_path / "official" / case["path"])))
            accelerated = run_accelerated("blockinfile", dict(
                case, path=str(tmp_path / "accelerated" / case["path"])),
                options=options)
            assert (outcome(accelerated, "accelerated", case["path"])
                    == outcome(official, "official", case["path"])), case
    finally:
        rt.cleanup()


def test_accelerated_blockinfile_non_utf8_file_matches_official(tmp_path):
    from ansiroll.loader import _accelerator_options

    options = _accelerator_options("blockinfile") or {}
    official_path = tmp_path / "official"
    accelerated_path = tmp_path / "accelerated"
    for path in (official_path, accelerated_path):
        path.write_bytes(b"caf\xe9\nend\n")
    args = {"block": "x", "insertafter": "caf"}
    accelerated = run_accelerated(
        "blockinfile", dict(args, path=str(accelerated_path)), options=options)
    if options.get("text_mode"):
        # ansible-core >= 2.20 decodes utf-8 and the module dies; ansible-playbook
        # registers this failure (the runtime's crash wrapping differs).
        assert accelerated == {
            "failed": True, "changed": False,
            "msg": "Task failed: Module failed: 'utf-8' codec can't decode "
                   "byte 0xe9 in position 3: invalid continuation byte",
        }
        assert accelerated_path.read_bytes() == b"caf\xe9\nend\n"
        return
    rt = Runtime({"blockinfile": ModuleEmbedder().embed("blockinfile")},
                 "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("blockinfile", dict(args, path=str(official_path)))
    finally:
        rt.cleanup()
    official.pop("invocation", None)
    for result, path in ((official, official_path), (accelerated, accelerated_path)):
        result["diff"] = json.loads(json.dumps(result["diff"]).replace(str(path), "<p>"))
    assert accelerated == official
    assert accelerated_path.read_bytes() == official_path.read_bytes()


def test_accelerated_user_and_group_noops_match_official():
    import grp
    import pwd

    current_user = pwd.getpwuid(os.geteuid())
    current_group = grp.getgrgid(current_user.pw_gid)
    trials = [
        (
            "group",
            {
                "name": current_group.gr_name,
                "state": "present",
                "gid": current_group.gr_gid,
            },
        ),
        (
            "user",
            {
                "name": current_user.pw_name,
                "state": "present",
                "uid": current_user.pw_uid,
                "group": current_group.gr_name,
                "shell": current_user.pw_shell,
            },
        ),
    ]
    for action, args in trials:
        emb = ModuleEmbedder()
        meta = emb.embed(action)
        rt = Runtime(
            {action: meta}, "", {"inventory_hostname": "localhost"})
        try:
            official = rt.run_module(action, args)
        finally:
            rt.cleanup()
        from ansible.release import __version__ as ansible_version
        version = tuple(map(int, ansible_version.split(".")[:2]))
        accelerated = run_accelerated(
            action,
            args,
            options={"result_memberships": action == "user" and version >= (2, 21)},
        )

        official.pop("invocation", None)
        assert accelerated == official
        assert accelerated["changed"] is False


@pytest.mark.parametrize(
    "args,expected_names",
    [
        ({"file_type": "file"}, {"plain.txt", "skip.log"}),
        (
            {"file_type": "file", "patterns": "*.txt", "excludes": "skip*"},
            {"plain.txt"},
        ),
        ({"file_type": "directory"}, {"nested"}),
        (
            {"file_type": "file", "patterns": "*.txt", "recurse": True},
            {"plain.txt", "inside.txt"},
        ),
        ({"file_type": "any", "hidden": True}, {
            "plain.txt", "skip.log", ".hidden", "nested",
        }),
    ],
)
def test_accelerated_find_matches_official_result_shape(
        tmp_path, args, expected_names):
    root = tmp_path / "find"
    root.mkdir()
    (root / "plain.txt").write_text("plain")
    (root / "skip.log").write_text("skip")
    (root / ".hidden").write_text("hidden")
    (root / "nested").mkdir()
    (root / "nested" / "inside.txt").write_text("inside")
    module_args = dict(args, paths=str(root))

    meta = ModuleEmbedder().embed("find")
    rt = Runtime({"find": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("find", module_args)
    finally:
        rt.cleanup()
    from ansible.release import __version__ as ansible_version
    version = tuple(map(int, ansible_version.split(".")[:2]))
    accelerated = run_accelerated(
        "find",
        module_args,
        options={
            "all_paths_examined": version >= (2, 12),
            "disk_usage_bytes": version >= (2, 21),
            "structured_skipped_paths": version >= (2, 12),
            "typed_warnings": version >= (2, 19),
        },
    )

    assert accelerated["changed"] == official["changed"] is False
    assert accelerated["matched"] == official["matched"]
    assert accelerated["examined"] == official["examined"]
    assert accelerated["msg"] == official["msg"]
    official_by_name = {
        os.path.basename(item["path"]): item for item in official["files"]
    }
    accelerated_by_name = {
        os.path.basename(item["path"]): item for item in accelerated["files"]
    }
    assert set(accelerated_by_name) == set(official_by_name) == expected_names
    for name, official_item in official_by_name.items():
        accelerated_item = accelerated_by_name[name]
        # These are values of the same filesystem object, so even inode and
        # timestamps must match—not merely the commonly consumed path/type.
        assert accelerated_item == official_item


def test_accelerated_find_missing_path_matches_official_result(tmp_path):
    from ansible.release import __version__ as ansible_version

    args = {"paths": str(tmp_path / "missing"), "file_type": "file"}
    version = tuple(map(int, ansible_version.split(".")[:2]))
    meta = ModuleEmbedder().embed("find")
    rt = Runtime({"find": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("find", args)
    finally:
        rt.cleanup()
    accelerated = run_accelerated(
        "find",
        args,
        options={
            "all_paths_examined": version >= (2, 12),
            "disk_usage_bytes": version >= (2, 21),
            "structured_skipped_paths": version >= (2, 12),
            "typed_warnings": version >= (2, 19),
        },
    )

    expected = {
        key: value for key, value in official.items()
        if key not in ("invocation",)
    }
    assert accelerated == expected


def test_accelerated_find_splits_comma_separated_lists_like_official(tmp_path):
    from ansiroll.loader import _accelerator_options

    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
        for name in ("x.txt", "y.log", "z.cfg"):
            (tmp_path / sub / name).write_text(name)
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    cases = [
        {"paths": "%s,%s" % (a, b), "patterns": "*.txt,*.log"},
        {"paths": a, "patterns": "*.txt,*.log", "excludes": "x*,nomatch"},
        {"path": a, "pattern": "*.cfg"},
    ]
    rt = Runtime({"find": ModuleEmbedder().embed("find")},
                 "", {"inventory_hostname": "localhost"})

    def normalized(result):
        result = {k: v for k, v in result.items() if k != "invocation"}
        result["files"] = sorted(result["files"], key=lambda item: item["path"])
        return result

    try:
        for case in cases:
            official = rt.run_module("find", dict(case))
            accelerated = run_accelerated(
                "find", dict(case), options=_accelerator_options("find"))
            assert normalized(accelerated) == normalized(official), case
            assert official["matched"] > 0
    finally:
        rt.cleanup()


@pytest.mark.parametrize("login_defs", [
    "UMASK 027\nHOME_MODE 0750\n",
    "HOME_MODE 0700\nUMASK 022\n",
    "UMASK 077\nUMASK 002\n",
    "# no mode\n",
    None,
])
def test_accelerated_user_home_creation_matches_official_create_homedir(
        tmp_path, monkeypatch, login_defs):
    import stat as stat_module
    from ansible.modules import user as official_user
    from ansiroll.accelerated_modules import user as accelerated_user
    from ansiroll.loader import _accelerator_options

    skel = tmp_path / "skel"
    (skel / "sub").mkdir(parents=True)
    (skel / ".profile").write_text("profile")
    (skel / "sub" / "f").write_text("f")
    os.chmod(str(skel / ".profile"), 0o640)
    os.symlink(".profile", str(skel / "link"))
    defs = tmp_path / "login.defs"
    if login_defs is not None:
        defs.write_text(login_defs)

    class _Module(object):
        def exit_json(self, **kwargs):
            raise AssertionError(kwargs)

    official = official_user.User.__new__(official_user.User)
    official.module = _Module()
    official.skeleton = str(skel)
    official.LOGIN_DEFS = str(defs)
    official.create_homedir(str(tmp_path / "official" / "home"))
    official.chown_homedir(os.getuid(), os.getgid(), str(tmp_path / "official" / "home"))

    monkeypatch.setattr(accelerated_user, "_SKELETON", str(skel))
    monkeypatch.setattr(accelerated_user, "_LOGIN_DEFS", str(defs))
    options = _accelerator_options("user") or {}
    accelerated_user._create_homedir(
        str(tmp_path / "accelerated" / "home"), options.get("home_mode"))
    accelerated_user._chown_homedir(
        os.getuid(), os.getgid(), str(tmp_path / "accelerated" / "home"))

    def tree(base):
        out = {}
        for root, dirs, files in os.walk(base):
            for name in dirs + files + ["."]:
                full = os.path.normpath(os.path.join(root, name))
                st = os.lstat(full)
                out[os.path.relpath(full, base)] = (
                    oct(stat_module.S_IMODE(st.st_mode)),
                    os.readlink(full) if os.path.islink(full) else None)
        return out

    assert (tree(str(tmp_path / "accelerated" / "home"))
            == tree(str(tmp_path / "official" / "home")))


def test_accelerated_user_expands_home_like_official_path_type():
    import pwd as pwd_module
    from ansiroll.loader import _accelerator_options

    me = pwd_module.getpwuid(os.getuid())
    args = {"name": me.pw_name, "home": "~", "shell": me.pw_shell}
    meta = ModuleEmbedder().embed("user")
    rt = Runtime({"user": meta}, "", {"inventory_hostname": "localhost"})
    rt.check_mode = True
    try:
        official = rt.run_module("user", dict(args))
    finally:
        rt.cleanup()
    official.pop("invocation", None)
    accelerated = run_accelerated(
        "user", dict(args), check_mode=True,
        options=_accelerator_options("user"))
    if me.pw_dir != os.path.expanduser("~"):
        pytest.skip("HOME does not match the passwd home")
    assert accelerated == official
    assert official["changed"] is False


def test_accelerated_user_home_uses_task_environment(tmp_path, monkeypatch):
    import pwd as pwd_module
    from ansiroll.loader import _accelerator_options

    me = pwd_module.getpwuid(os.getuid())
    monkeypatch.setenv("ANSIROLL_USER_HOME", str(tmp_path / "stale"))
    monkeypatch.setenv("HOME", str(tmp_path))
    args = {"name": me.pw_name, "home": "$ANSIROLL_USER_HOME", "create_home": False}
    environment = {"ANSIROLL_USER_HOME": "~", "HOME": me.pw_dir}
    rt = Runtime({"user": ModuleEmbedder().embed("user")},
                 "", {"inventory_hostname": "localhost"}, check_mode=True)
    rt._module_env = environment
    try:
        official = rt.run_module("user", dict(args))
    finally:
        rt.cleanup()
    official.pop("invocation", None)
    accelerated = run_accelerated(
        "user", args, check_mode=True, environment=environment,
        options=_accelerator_options("user"))
    assert accelerated == official
    assert official["changed"] is False
    assert os.environ["HOME"] == str(tmp_path)


def test_accelerated_user_compares_int_fields_as_strings(monkeypatch):
    """comment/shell are type='str' in the official argument_spec, so an int
    that matches the passwd field is not a change."""
    import pwd as pwd_module
    from ansiroll.accelerated_modules import user as accelerated_user

    entry = pwd_module.struct_passwd(
        ("ansiroll-fake", "x", 4242, 4242, "123", "/", "7"))
    monkeypatch.setattr(accelerated_user.pwd, "getpwnam", lambda name: entry)
    monkeypatch.setattr(accelerated_user.grp, "getgrall", lambda: [])

    def no_execute(*_args, **_kwargs):
        raise AssertionError("usermod must not run")

    monkeypatch.setattr(accelerated_user, "_execute", no_execute)
    result = accelerated_user.run(
        {"name": "ansiroll-fake", "comment": 123, "shell": 7})
    assert result["changed"] is False
    assert result["comment"] == "123"


def test_accelerated_user_creates_missing_home_parents_before_useradd(
        tmp_path, monkeypatch):
    import pwd as pwd_module
    from ansiroll.accelerated_modules import user as accelerated_user

    skel = tmp_path / "skel"
    skel.mkdir()
    (skel / ".bashrc").write_text("rc")
    defs = tmp_path / "login.defs"
    defs.write_text("UMASK 077\n")
    home = tmp_path / "missing" / "parent" / "home"
    monkeypatch.setattr(accelerated_user, "_SKELETON", str(skel))
    monkeypatch.setattr(accelerated_user, "_LOGIN_DEFS", str(defs))
    created = []
    entry = pwd_module.struct_passwd(
        ("ansiroll-fake", "x", os.getuid(), os.getgid(), "", str(home), "/bin/sh"))

    def getpwnam(name):
        if not created:
            raise KeyError(name)
        return entry

    def execute(command, environment):
        # The official module creates the home (and its parents) first.
        assert (home / ".bashrc").read_text() == "rc"
        created.append(command)
        return 0, "", ""

    monkeypatch.setattr(accelerated_user.pwd, "getpwnam", getpwnam)
    monkeypatch.setattr(accelerated_user.grp, "getgrall", lambda: [])
    monkeypatch.setattr(accelerated_user, "_execute", execute)
    result = accelerated_user.run({"name": "ansiroll-fake", "home": str(home)})
    assert result["changed"] is True
    assert created and created[0][0] == "useradd" and "-m" in created[0]
    assert oct(os.stat(str(home)).st_mode & 0o777) == oct(0o700)


def test_compile_omits_total_static_official_module_and_forces_accelerator(
        tmp_path):
    destination = tmp_path / "accelerated-only-file"
    destination.write_text("present")
    playbook = tmp_path / "accelerated-only.yml"
    playbook.write_text(
        "- hosts: localhost\n"
        "  gather_facts: false\n"
        "  tasks:\n"
        "    - ansible.builtin.stat:\n"
        "        path: %s\n"
        "      register: accelerated_stat\n"
        "    - ansible.builtin.assert:\n"
        "        that: accelerated_stat.stat.exists\n" % destination
    )

    source, manifest = compile_playbook(str(playbook))
    agent = tmp_path / "accelerated-only.py"
    agent.write_text(source)
    environment = dict(os.environ)
    environment.pop("ANSIROLL_ACCELERATED_MODULES", None)
    process = subprocess.run(
        [sys.executable, "-S", str(agent)],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert process.returncode == 0, process.stdout + process.stderr
    assert destination.is_file()
    assert not any(name.endswith("stat") for name in manifest["modules"])
    assert manifest["accelerated_only_modules"] == ["ansible.builtin.stat"]


@pytest.mark.parametrize("kind", ["missing", "file", "directory", "symlink"])
def test_accelerated_stat_matches_official_result(tmp_path, kind):
    path = tmp_path / kind
    if kind == "file":
        path.write_bytes(b"accelerated stat parity\n")
    elif kind == "directory":
        path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_text("target")
        path.symlink_to(target)

    emb = ModuleEmbedder()
    meta = emb.embed("stat")
    rt = Runtime({"stat": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("stat", {"path": str(path)})
    finally:
        rt.cleanup()
    accelerated = run_accelerated(
        "stat", {"path": str(path)},
        options={"disk_usage_bytes": "disk_usage_bytes" in official.get("stat", {})},
    )

    expected = {
        key: value for key, value in official.items() if key != "invocation"
    }
    if expected.get("stat", {}).get("exists"):
        expected["stat"].pop("atime", None)
        accelerated["stat"].pop("atime", None)
    assert accelerated == expected


def test_accelerated_stat_matches_official_optional_flags(tmp_path):
    path = tmp_path / "plain"
    path.write_text("plain")
    args = {
        "path": str(path), "follow": True, "get_checksum": False,
        "get_mime": False, "get_attributes": False,
    }
    emb = ModuleEmbedder()
    meta = emb.embed("stat")
    rt = Runtime({"stat": meta}, "", {"inventory_hostname": "localhost"})
    try:
        official = rt.run_module("stat", args)
    finally:
        rt.cleanup()
    accelerated = run_accelerated(
        "stat", args,
        options={"disk_usage_bytes": "disk_usage_bytes" in official.get("stat", {})},
    )

    expected = {
        key: value for key, value in official.items() if key != "invocation"
    }
    expected["stat"].pop("atime", None)
    accelerated["stat"].pop("atime", None)
    assert accelerated == expected


def _fake_systemctl(tmp_path):
    fake_bin = tmp_path / "fake-systemctl-bin"
    fake_bin.mkdir()
    executable = fake_bin / "systemctl"
    executable.write_text(
        "#!%s\n" % sys.executable
        + "import json, os, sys\n"
        + "state_path = os.environ['FAKE_SYSTEMD_STATE']\n"
        + "log_path = os.environ['FAKE_SYSTEMD_LOG']\n"
        + "args = [arg for arg in sys.argv[1:] if arg != '-l']\n"
        + "with open(log_path, 'a') as stream:\n"
        + "    stream.write(json.dumps(args) + '\\n')\n"
        + "with open(state_path) as stream:\n"
        + "    state = json.load(stream)\n"
        + "command = args[0] if args else ''\n"
        + "if command == state.get('fail'):\n"
        + "    sys.stderr.write('injected failure\\n')\n"
        + "    sys.exit(1)\n"
        + "if command == 'daemon-reload':\n"
        + "    sys.exit(0)\n"
        + "if command == 'show':\n"
        + "    if state.get('missing'):\n"
        + "        sys.stderr.write('Unit not found\\n')\n"
        + "        sys.exit(1)\n"
        + "    print('LoadState=loaded')\n"
        + "    print('ActiveState=' + state['active'])\n"
        + "    print('UnitFileState=' + ('enabled' if state['enabled'] else 'disabled'))\n"
        + "    print('Description=Fake service')\n"
        + "    sys.exit(0)\n"
        + "if command == 'is-enabled':\n"
        + "    print('enabled' if state['enabled'] else 'disabled')\n"
        + "    sys.exit(0 if state['enabled'] else 1)\n"
        + "if command == 'list-unit-files':\n"
        + "    sys.exit(1 if state.get('missing') else 0)\n"
        + "if command in ('enable', 'disable'):\n"
        + "    state['enabled'] = command == 'enable'\n"
        + "elif command in ('start', 'restart'):\n"
        + "    state['active'] = 'active'\n"
        + "elif command == 'stop':\n"
        + "    state['active'] = 'inactive'\n"
        + "else:\n"
        + "    sys.stderr.write('unsupported fake systemctl call: %r\\n' % args)\n"
        + "    sys.exit(2)\n"
        + "with open(state_path, 'w') as stream:\n"
        + "    json.dump(state, stream, sort_keys=True)\n"
    )
    executable.chmod(0o755)
    state_path = tmp_path / "systemd-state.json"
    log_path = tmp_path / "systemd-log.jsonl"
    environment = {
        "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
        "FAKE_SYSTEMD_STATE": str(state_path),
        "FAKE_SYSTEMD_LOG": str(log_path),
    }
    return state_path, log_path, environment


def _systemd_trial(meta, args, initial, state_path, log_path, environment,
                   accelerated):
    state_path.write_text(json.dumps(initial))
    log_path.write_text("")
    if accelerated:
        result = run_accelerated(
            "systemd", args, environment=environment,
            options={"modern_is_enabled": True},
        )
    else:
        rt = Runtime({"systemd": meta}, "", {"inventory_hostname": "localhost"})
        rt._module_env = environment
        try:
            result = rt.run_module("systemd", args)
        finally:
            rt.cleanup()
    final = json.loads(state_path.read_text())
    calls = [json.loads(line) for line in log_path.read_text().splitlines()]
    return result, final, calls


@pytest.mark.parametrize(
    "args,initial",
    [
        ({"daemon_reload": True}, {"active": "inactive", "enabled": False}),
        ({"name": "parity.service", "state": "started", "enabled": True},
         {"active": "active", "enabled": True}),
        ({"name": "parity.service", "state": "started", "enabled": True},
         {"active": "inactive", "enabled": False}),
        ({"name": "parity.service", "state": "restarted"},
         {"active": "active", "enabled": True}),
        ({"name": "parity.service", "state": "stopped", "enabled": False},
         {"active": "active", "enabled": True}),
        ({"name": "parity.service", "state": "started", "enabled": True,
          "daemon_reload": True},
         {"active": "active", "enabled": True}),
    ],
)
def test_accelerated_systemd_matches_official_mutations(tmp_path, args, initial):
    state_path, log_path, environment = _fake_systemctl(tmp_path)
    meta = ModuleEmbedder().embed("systemd")
    official, official_state, official_calls = _systemd_trial(
        meta, args, initial, state_path, log_path, environment, False
    )
    accelerated, accelerated_state, accelerated_calls = _systemd_trial(
        meta, args, initial, state_path, log_path, environment, True
    )

    expected = {key: value for key, value in official.items()
                if key not in ("invocation", "warnings", "deprecations")}
    assert accelerated == expected
    assert accelerated_state == official_state
    mutations = {"daemon-reload", "enable", "disable", "start", "stop", "restart"}
    assert [call for call in accelerated_calls if call[0] in mutations] == [
        call for call in official_calls if call[0] in mutations
    ]
    # Named daemon_reload calls use one extra read-only preflight so fallback
    # can never repeat a manager mutation.
    allowance = 1 if args.get("daemon_reload") and args.get("name") else 0
    assert len(accelerated_calls) <= len(official_calls) + allowance


def test_accelerated_systemd_missing_unit_falls_back_before_reload(tmp_path):
    state_path, log_path, environment = _fake_systemctl(tmp_path)
    state_path.write_text(json.dumps({
        "active": "inactive", "enabled": False, "missing": True,
    }))
    log_path.write_text("")

    result = run_accelerated(
        "systemd",
        {"name": "missing.service", "state": "stopped", "daemon_reload": True},
        environment=environment,
        options={"modern_is_enabled": True},
    )

    assert result == {"_ansiroll_fallback": True}
    calls = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert calls == [["show", "missing.service"]]


@pytest.mark.parametrize(
    "args,initial",
    [
        ({"daemon_reload": True},
         {"active": "inactive", "enabled": False, "fail": "daemon-reload"}),
        ({"name": "parity.service", "enabled": True},
         {"active": "inactive", "enabled": False, "fail": "enable"}),
        ({"name": "parity.service", "state": "started"},
         {"active": "inactive", "enabled": False, "fail": "start"}),
    ],
)
def test_accelerated_systemd_matches_official_failures(tmp_path, args, initial):
    state_path, log_path, environment = _fake_systemctl(tmp_path)
    meta = ModuleEmbedder().embed("systemd")
    official, official_state, _ = _systemd_trial(
        meta, args, initial, state_path, log_path, environment, False
    )
    accelerated, accelerated_state, _ = _systemd_trial(
        meta, args, initial, state_path, log_path, environment, True
    )

    expected = {key: value for key, value in official.items()
                if key not in ("invocation", "warnings", "deprecations")}
    assert accelerated == expected
    assert accelerated_state == official_state


def test_worker_reuses_module_and_writes_nothing():
    # Persistent worker: module loaded once, forked per task, no disk writes.
    emb = ModuleEmbedder()
    meta = emb.embed("command")
    rt = Runtime({"command": meta}, "", {"inventory_hostname": "localhost"})
    try:
        for i in range(3):
            res = rt.run_module("command", {"_raw_params": "echo run%d" % i})
            assert res.get("rc") == 0 and ("run%d" % i) in res.get("stdout", "")
        assert rt._workers[False]["loaded"] == {"ansible.modules.command"}
    finally:
        rt.cleanup()
    assert rt._workers == {}


def test_worker_loads_accelerator_from_shared_memory_bundle(tmp_path):
    import_bundle, _, _ = _build_bundles([], {})
    rt = Runtime({}, "", {"inventory_hostname": "localhost"},
                 module_bundle=import_bundle)
    path = tmp_path / "from-worker"
    try:
        res = rt._run_worker_accelerated(
            "file", {"path": str(path), "state": "directory"}
        )
    finally:
        rt.cleanup()
    assert path.is_dir()
    assert res.get("changed") is True
    assert res.get("failed") is not True


def test_worker_runs_accelerated_apt_from_shared_memory_bundle():
    import_bundle, _, _ = _build_bundles([], {})
    rt = Runtime(
        {}, "", {"inventory_hostname": "localhost"},
        module_bundle=import_bundle,
    )
    try:
        result = rt._run_worker_accelerated(
            "apt",
            {
                "pkg": ["ansiroll-package-that-does-not-exist"],
                "state": "absent",
                "purge": True,
                "autoclean": True,
                "autoremove": True,
            },
        )
    finally:
        rt.cleanup()

    assert result == {"changed": False}


def test_worker_accepts_decoded_import_bundle_bytes():
    emb = ModuleEmbedder()
    meta = emb.embed("command")
    import_bundle, _, _ = _build_bundles([], {"command": meta})
    rt = Runtime(
        {"command": meta}, "", {"inventory_hostname": "localhost"},
        module_bundle=import_bundle,
    )
    try:
        res = rt.run_module("command", {"_raw_params": "printf raw-bundle"})
    finally:
        rt.cleanup()
    assert res.get("rc") == 0
    assert res.get("stdout") == "raw-bundle"


def test_same_user_become_reuses_normal_worker_protocol(tmp_path, monkeypatch):
    """Becoming the current UID must not create a redundant sudo worker."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-S\" ]; then\n"
        "  exit 0\n"  # cached authentication: deliberately do not read stdin
        "fi\n"
        "if [ \"$1\" = \"-n\" ] && [ \"$2\" = \"-u\" ]; then\n"
        "  shift 3\n"
        "  exec \"$@\"\n"
        "fi\n"
        "exit 2\n"
    )
    fake_sudo.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])

    emb = ModuleEmbedder()
    meta = emb.embed("command")
    rt = Runtime(
        {"command": meta},
        "",
        {"inventory_hostname": "localhost"},
        become_password="cached-password",
    )
    rt.become_user = getpass.getuser()
    try:
        res = rt.run_module("command", {"_raw_params": "printf cached-sudo-ok"}, become=True)
        assert set(rt._workers) == {False}
    finally:
        rt.cleanup()

    assert res.get("rc") == 0
    assert res.get("stdout") == "cached-sudo-ok"


def test_worker_death_reports_clear_failure(monkeypatch):
    # A dead worker must fail the task loudly (outcome unknown) -- never silently
    # retry or switch engines.
    emb = ModuleEmbedder()
    meta = emb.embed("command")
    rt = Runtime({"command": meta}, "", {"inventory_hostname": "localhost"})

    def boom(*a, **k):
        raise RuntimeError("worker closed unexpectedly")
    monkeypatch.setattr(rt, "_run_via_worker", boom)
    res = rt.run_module("command", {"_raw_params": "echo x"})
    assert res["failed"] is True
    assert "worker died" in res["msg"] and "outcome is unknown" in res["msg"]


def test_worker_recovers_for_next_task_after_crash():
    # A one-off worker crash must not poison the rest of the play: the next task
    # gets a fresh worker.
    emb = ModuleEmbedder()
    meta = emb.embed("command")
    rt = Runtime({"command": meta}, "", {"inventory_hostname": "localhost"})
    try:
        assert rt.run_module("command", {"_raw_params": "echo a"}).get("rc") == 0
        rt._workers[False]["proc"].kill()
        rt._workers[False]["proc"].wait()
        # next task: _get_worker sees the dead proc and spawns a fresh one
        assert rt.run_module("command", {"_raw_params": "echo b"}).get("rc") == 0
    finally:
        rt.cleanup()


def test_worker_drains_stdout_and_stderr_concurrently():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ansible/modules/__init__.py", "")
        zf.writestr(
            "ansible/modules/noisy.py",
            "import json, sys\n"
            "sys.stderr.write('x' * 262144)\n"
            "sys.stderr.flush()\n"
            "print(json.dumps({'changed': False, 'msg': 'ok'}))\n",
        )
    meta = {
        "zip": base64.b64encode(buf.getvalue()).decode("ascii"),
        "module_fqn": "ansible.modules.noisy",
        "profile": "legacy",
    }
    rt = Runtime({"noisy": meta}, "", {"inventory_hostname": "localhost"})
    started = time.monotonic()
    try:
        res = rt.run_module("noisy", {})
    finally:
        rt.cleanup()
    elapsed = time.monotonic() - started
    assert res["changed"] is False
    assert res["msg"] == "ok"
    assert elapsed < 5


def test_embed_setup_returns_prefixed_facts():
    emb = ModuleEmbedder()
    meta = emb.embed("ansible.builtin.setup")
    rt = Runtime({"ansible.builtin.setup": meta}, "", {})
    res = rt.run_module("ansible.builtin.setup", {})
    facts = res.get("ansible_facts", {})
    assert "ansible_distribution" in facts
    assert "ansible_system" in facts


# ---- compilation -------------------------------------------------------

def test_compile_simple_embeds_command():
    src, manifest = compile_playbook(os.path.join(FIXTURES, "simple.yml"))
    assert manifest["bundle_format"] == 2
    assert any(m.endswith("command") for m in manifest["modules"])
    assert "def main(" in src  # runtime inlined
    assert "PLAYS = " in src


def test_compile_embeds_worker_file():
    src, _ = compile_playbook(os.path.join(FIXTURES, "simple.yml"))
    with open(os.path.join(ROOT, "ansiroll", "worker.py"), "r") as fh:
        worker_src = fh.read()
    assert "_WORKER_CODE = " + repr(worker_src) in src
    assert "# __ANSIROLL_WORKER_CODE__" not in src


def test_compile_bakes_controller_core_version():
    from ansible.release import __version__
    src, _ = compile_playbook(os.path.join(FIXTURES, "simple.yml"))
    assert "\n_ANSIROLL_CORE_VERSION = %r\n" % str(__version__) in src


def test_controller_synchronize_leaves_retries_to_agent(monkeypatch):
    import copy
    from ansible.executor import task_executor

    class Executor:
        def __init__(self, host, task, *args):
            self.task = task

        def run(self):
            return {"retries": self.task.retries, "until": self.task.until,
                    "changed_when": self.task.changed_when, "failed_when": self.task.failed_when}

    monkeypatch.setattr(task_executor, "TaskExecutor", Executor)
    mod = _load_strategy_module()
    strategy = SimpleNamespace(_loader=None, _variable_manager=None)
    original = SimpleNamespace(retries=2, until=["ready"], changed_when=["registered.rc == 0"],
                               failed_when=["registered.rc != 0"])
    original.copy = lambda: copy.copy(original)
    frame = {"runtime_id": "sync", "action": "ansible.posix.synchronize"}
    result = mod.StrategyModule._run_native_controller_task(strategy, frame, None, {}, {"sync": original}, None)
    assert result == {"retries": 0, "until": [], "changed_when": [], "failed_when": []}
    assert original.retries == 2 and original.until == ["ready"]


@pytest.mark.parametrize(("alias_value", "alias_member", "alias_error"), [
    ("{{ missing }}", "", None),
    ("{{ missing | regex_replace('x', 'y') }}", "", None),
    ("{{ 'value' | unknown_filter }}", "", "unknown_filter"),
    ({"nested": ["{{ 'value' | unknown_filter }}"]}, ".nested[0]", "unknown_filter"),
    ({"bad": "{{ 'value' | unknown_filter }}"}, ".get('bad')", "unknown_filter"),
    ({"bad": "{{ 'value' | unknown_filter }}"}, ".values() | first", "unknown_filter"),
    ({"bad": "{{ 'value' | unknown_filter }}"}, ".values() | map('default', 'fallback') | first", "unknown_filter"),
    ({"nested": ["{{ 'value' | unknown_filter }}"]}, ".nested | map('default', 'fallback') | first", "unknown_filter"),
    ({"nested": ["{{ 'value' | unknown_filter }}"]}, ".nested[:1] | map('default', 'fallback') | first", "unknown_filter"),
    ({"nested": ["{{ 'value' | unknown_filter }}"]}, "(ALIAS.nested + []) | map('default', 'fallback') | first", "unknown_filter"),
    ({"nested": ["{{ 'value' | unknown_filter }}"]}, "([] + ALIAS.nested) | map('default', 'fallback') | first", "unknown_filter"),
    ({"nested": ["{{ 'value' | unknown_filter }}"]}, "('opts=' ~ (ALIAS.nested[:1] | string))", "unknown_filter"),
    ({"bad": "{{ 'value' | unknown_filter }}"}, ".values()", "unknown_filter"),
    ({"nested": ["{{ 'value' | unknown_filter }}"], "ok": "optional"}, ".ok", None),
    ("{{ 1 / 0 }}", "", "division by zero"),
])
def test_synchronize_round_trips_the_rendered_loop_environment(tmp_path, monkeypatch, alias_value, alias_member, alias_error):
    import copy

    from ansible.executor import task_executor
    from ansible.parsing.dataloader import DataLoader
    from ansible.playbook import Playbook
    from ansible.release import __version__ as ansible_version
    from ansiroll.compat import make_templar
    from ansiroll.loader import _tag_untrusted

    make_templar()  # Initialize Ansible's collection loader before parsing.
    legacy = tuple(map(int, ansible_version.split(".")[:2])) < (2, 19)
    items_membership = "('ok', 'valid') in item_options.items()" if legacy else "true"
    alias_read = (alias_member.replace("ALIAS", "optional_agent_alias") if "ALIAS" in alias_member
                  else "optional_agent_alias" + alias_member)
    playbook = tmp_path / "play.yml"
    playbook.write_text("""
- hosts: localhost
  vars:
    sync_play_value: "{{ 'PLAY' | lower }}"
  environment:
    PLAY_VALUE: "{{ sync_play_value }}"
  tasks:
    - environment:
        SYNC_LABEL: "{{ ['RELEASE', 'PREVIEW'][sync_index] }}"
      block:
        - debug:
            msg: placeholder
          vars:
            sync_index: "{{ item }}"
            sync_alias: {value: "{{ ['release', 'preview'][item] | upper }}"}
            sync_list: ["{{ ['release', 'preview'][item] | upper }}"]
            sync_loop_label: "{{ ['release', 'preview'][item] | upper }}"
            unsafe_label: !unsafe "{{ item }}"
          environment:
            - SYNC_LITERAL: "{{ '{' ~ '{ placeholder }' ~ '}' }}"
              SYNC_UNSAFE: "{{ unsafe_label }}"
              SYNC_PRIOR: "{{ prior.msg }}"
              SYNC_MIRROR: "{{ sync_alias.value ~ '-' ~ ansible_delegated_vars['localhost']['mirror'] }}"
              SYNC_LIST: "{{ sync_list[0] }}"
              SYNC_FILE: "{{ file_options.label }}"
              SYNC_FILE_LIST: "{{ file_options.labels[0] ~ '-' ~ file_options.labels[2] }}"
              SYNC_FILE_MISSING: "{{ file_options.labels[1] is defined }}"
              SYNC_FILE_DEFAULT: "{{ file_options.labels[1].nested | default('fallback') }}"
              SYNC_FILE_KEYS: "{{ 'optional' in file_options and (file_options | length == 3) }}"
              SYNC_FILE_OPTIONAL: "{{ file_options.optional | default('optional') }}"
              SYNC_FILE_JSON: "json={{ file_env | to_json }}"
              SYNC_LIST_JSON: "json={{ file_list | to_json }}"
              SYNC_VARS_LOOKUP: "{{ (lookup('vars', 'file_env')).SYNC_FILE_WHOLE }}"
              SYNC_TYPED_KEYS: "{{ typed_agent_alias[8080] ~ '-' ~ typed_agent_alias['8080'] ~ '-' ~ typed_agent_alias[3][3] }}"
              SYNC_TYPED_MISSING: "{{ typed_agent_alias[4] | default('optional') }}"
              SYNC_DATE_KEY: "{{ (dated_agent_alias.keys() | list | first | type_debug) ~ '-' ~ dated_agent_alias.plain }}"
              SYNC_BINARY_KEY: "{{ (dated_agent_alias.keys() | list)[1] | type_debug }}"
              SYNC_ITEM_CONTAINS: "{{ items_membership }}"
              SYNC_UNDEFINED_ALIAS: "{{ optional_agent_alias is defined }}"
              SYNC_DEFAULT_ALIAS: "{{ optional_agent_alias | default('optional') }}"
              SYNC_UNUSED: "{{ 'skipped' if true else unused_agent_alias }}"
              SYNC_MIRRORS: "{% for mirror in ansible_delegated_vars['localhost']['mirrors'] %}{{ sync_loop_label }}-{{ mirror }}{% endfor %}"
              SYNC_DEFAULT: "{{ ansible_delegated_vars['localhost']['mirror'] | default('agent-default') }}"
              SYNC_FLAG: "{{ true }}"
              SYNC_EMPTY: "{{ none }}"
            - "{{ ansible_delegated_vars['localhost']['environment'] }}"
            - "{{ file_env }}"
""".replace("optional_agent_alias |", alias_read + " |")
       .replace("items_membership", items_membership))
    loader = DataLoader()
    play, = Playbook.load(str(playbook), loader=loader).get_plays()
    original, = play.tasks[0].block
    original_environment = original.environment
    agent = """
import json
import sys
from ansiroll import runtime
runtime._INJECTED_VARS = json.loads(sys.argv[3])
runtime.PLAYS = [{"vars": json.loads(sys.argv[3]), "deferred_vars_files": [{
    "names": ["/vars/{{ inventory_hostname }}.yml"],
    "files": {"/vars/localhost.yml": [{"file_options": {
        "label": "{{ 'release' | upper }}", "optional": "{{ missing }}",
        "labels": ["{{ 'release' | upper }}", "{{ missing }}", "tail"],
    }, "file_env": {"SYNC_FILE_WHOLE": "{{ 'mixed' | upper }}"},
        "file_list": ["{{ 'mixed' | upper }}"], "optional_agent_alias": json.loads(sys.argv[4]),
        "typed_agent_alias": "{{ {8080: 'number', '8080': 'text', 3: {3: 'nested'}, 4: missing} }}",
        "dated_agent_alias": "{{ '{2026-01-01: dated, ? !!binary YQ== : binary, plain: regular}' | from_yaml }}",
        "item_options": {"bad": "{{ 'value' | unknown_filter }}", "ok": "valid"},
        "unused_agent_alias": "{{ missing_value }}"}]},
}], "tasks": [{
    "type": "task", "name": "Register a literal", "action": "ansible.builtin.debug",
    "special": "debug", "register": "prior", "args": {"msg": "{{ '{' ~ '{ item }' ~ '}' }}"},
}, {
    "type": "task", "runtime_id": "sync", "name": "Synchronize",
    "action": "ansible.posix.synchronize", "fallback": True,
    "loop": [0, 1], "vars": json.loads(sys.argv[2]),
    "environment": json.loads(sys.argv[1]),
}]}]
runtime.MODULES = {}
runtime.ANSIBALLZ_CODE = ""
runtime._INJECTED_HOST = "localhost"
runtime.agent_main()
"""
    names = ("file_options", "file_env", "file_list", "typed_agent_alias", "dated_agent_alias")
    if legacy:
        names += ("item_options",)
    names += ("optional_agent_alias",)
    messages = [{"t": "delegate_play_var", "name": name} for name in names]
    messages.append({"t": "delegate_result", "result": {"changed": False}})
    encoded = [json.dumps(message).encode() for message in messages]
    replies = b"".join(len(message).to_bytes(4, "big") + message for message in encoded)
    serialized_environment = json.dumps(_tag_untrusted(original_environment, template_syntax_only=True))
    serialized_vars = json.dumps(_tag_untrusted(original.get_vars()))
    serialized_play_vars = json.dumps(_tag_untrusted(play.vars))
    result = subprocess.run([sys.executable, "-c", agent, serialized_environment,
                             serialized_vars, serialized_play_vars, json.dumps(alias_value)],
                            input=replies * 2,
                            capture_output=True, timeout=15, check=True)
    stream = io.BytesIO(result.stdout)
    frames = []
    fallbacks = []
    while header := stream.read(4):
        frame = json.loads(stream.read(int.from_bytes(header, "big")))
        if frame["t"] == "delegate":
            frames.append(frame)
        elif frame["t"] == "delegate_play_var_result":
            fallbacks.append(frame["fallback"])

    class Executor:
        def __init__(self, host, task, variables, *args):
            self.task = task
            self.variables = variables

        def run(self):
            templar = make_templar()
            templar.available_variables = dict(self.variables, ansible_delegated_vars={
                "localhost": {"mirror": "controller", "mirrors": ["controller"],
                              "environment": {"SYNC_WHOLE": "delegated"}},
            })
            environment = self.task._post_validate_environment(None, self.task.environment, templar)
            return {"environment": environment}

    monkeypatch.setattr(task_executor, "TaskExecutor", Executor)
    strategy_type = _load_strategy_module().StrategyModule
    strategy = strategy_type.__new__(strategy_type)
    strategy._loader = loader
    strategy._variable_manager = None
    empty = None if tuple(map(int, ansible_version.split(".")[:2])) >= (2, 19) else ""
    requests = []

    def resolve(name):
        assert name in names
        index = requests.count(name) * len(names) + names.index(name)
        requests.append(name)
        return fallbacks[index]

    results = [strategy._run_native_controller_task(frame, None, play.vars, {"sync": original}, None, resolve)
               for frame in frames]
    assert len(results) == 2
    assert requests == list(names) * 2
    if alias_error:
        assert all(result["failed"] and alias_error in result["msg"] for result in results)
        return
    assert results == [
        {"environment": {"PLAY_VALUE": "play", "SYNC_LABEL": label, "SYNC_LITERAL": "{{ placeholder }}",
                         "SYNC_UNSAFE": "{{ item }}", "SYNC_PRIOR": "{{ item }}",
                         "SYNC_MIRROR": label + "-controller", "SYNC_LIST": label, "SYNC_FILE": "RELEASE",
                         "SYNC_FILE_LIST": "RELEASE-tail", "SYNC_FILE_MISSING": False,
                         "SYNC_FILE_DEFAULT": "fallback", "SYNC_UNUSED": "skipped", "SYNC_FILE_WHOLE": "MIXED",
                         "SYNC_FILE_KEYS": True, "SYNC_FILE_OPTIONAL": "optional",
                         "SYNC_FILE_JSON": 'json={"SYNC_FILE_WHOLE": "MIXED"}', "SYNC_LIST_JSON": 'json=["MIXED"]',
                         "SYNC_VARS_LOOKUP": "MIXED", "SYNC_UNDEFINED_ALIAS": bool(alias_member),
                         "SYNC_TYPED_KEYS": "number-text-nested", "SYNC_TYPED_MISSING": "optional",
                         "SYNC_DATE_KEY": "date-regular",
                         "SYNC_BINARY_KEY": "bytes",
                         "SYNC_ITEM_CONTAINS": True,
                         "SYNC_DEFAULT_ALIAS": "optional",
                         "SYNC_MIRRORS": label + "-controller", "SYNC_DEFAULT": "controller",
                         "SYNC_FLAG": True, "SYNC_EMPTY": empty, "SYNC_WHOLE": "delegated"}}
        for label in ("RELEASE", "PREVIEW")
    ]
    assert all("unused_agent_alias" in frame["lazy_play_vars"] for frame in frames)
    assert all("omit" not in frame["vars"] for frame in frames)
    frame = copy.deepcopy(frames[0])
    frame["vars"]["sync_loop_label"] = "{{ 'other' | upper }}"
    rendered = strategy._run_native_controller_task(frame, None, play.vars, {"sync": original}, None,
                                                    lambda name: fallbacks[names.index(name)])
    assert rendered["environment"]["SYNC_MIRRORS"] == "{{ 'other' | upper }}-controller"
    assert original.environment == original_environment


def test_gather_embeds_setup():
    src, manifest = compile_playbook(os.path.join(FIXTURES, "gather.yml"))
    assert any(m.endswith("setup") for m in manifest["modules"])


@pytest.mark.parametrize("timeout", [None, 23])
def test_implicit_setup_passes_play_gather_timeout(tmp_path, monkeypatch, timeout):
    from ansible.parsing.dataloader import DataLoader
    from ansible.playbook import Playbook
    from ansible.vars.manager import VariableManager

    playbook = tmp_path / "play.yml"
    option = "" if timeout is None else "  gather_timeout: %d\n" % timeout
    playbook.write_text("- hosts: localhost\n  gather_facts: true\n" + option + "  tasks: []\n")
    expected = {} if timeout is None else {"gather_timeout": timeout}
    source, _ = compile_playbook(str(playbook))
    loader = DataLoader()
    manager = VariableManager(loader=loader)
    play = Playbook.load(str(playbook), variable_manager=manager, loader=loader).get_plays()[0]
    strategy_type = _load_strategy_module().StrategyModule
    strategy = strategy_type.__new__(strategy_type)
    setup_args = {} if play.gather_timeout is None else {"gather_timeout": play.gather_timeout}
    setup = SimpleNamespace(when=[], no_log=False, args=setup_args)
    strategy_source, _, _ = strategy._compile(play, manager.get_vars(play=play), setup)

    monkeypatch.setenv("ANSIROLL_FAST_FACTS", "1" if timeout is not None else "0")
    for compiled in (source, strategy_source):
        runtime = Runtime({}, "", {"inventory_hostname": "localhost"})
        calls = []

        def run_module(action, args, **kwargs):
            calls.append((action, args))
            return {"ansible_facts": {}}

        monkeypatch.setattr(runtime, "run_module", run_module)
        runtime.run_play(_compiled_plays(compiled, tmp_path)[0])
        assert calls == [("ansible.builtin.setup", expected)]


def test_legacy_include_is_rewritten_and_inlined(tmp_path):
    import jinja2

    environment = jinja2.Environment(undefined=jinja2.StrictUndefined)
    src, manifest = compile_playbook(os.path.join(FIXTURES, "legacy", "play.yml"))
    # The included task must be inlined (no crash on removed `include`), and not
    # left as a fallback.
    importers = tuple(sys.meta_path)
    assert "included-marker" in json.dumps(_compiled_plays(src, tmp_path))
    assert tuple(sys.meta_path) == importers
    assert manifest["fallback_tasks"] == []
    with pytest.raises(jinja2.UndefinedError):
        environment.from_string("{{ missing }}").render()


@pytest.mark.parametrize("root_include", ["include", "import_playbook"])
def test_legacy_playbook_includes_preserve_nested_task_includes(tmp_path, root_include):
    playbook = tmp_path / "play.yml"
    playbook.write_text("- %s: sub/wrapper.yml\n" % root_include)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "wrapper.yml").write_text("- include: leaf.yml\n")
    (sub / "leaf.yml").write_text("""
- hosts: localhost
  gather_facts: false
  tasks:
    - include: tasks.yml
""")
    (sub / "tasks.yml").write_text("- include: final.yml\n")
    (sub / "final.yml").write_text("- debug: {msg: legacy-playbook-leaf}\n")

    source, manifest = compile_playbook(str(playbook))
    assert "legacy-playbook-leaf" in json.dumps(_compiled_plays(source, tmp_path))
    assert manifest["fallback_tasks"] == []


@pytest.mark.parametrize(("include", "greeting"), [
    ("""- include: "{{ child_playbook }} greeting='hello world' tags=imported"\n""", "hello world"),
    ("- include: >\n    {{ child_playbook }}\n", "original"),
    ("- include: |\n    {{ child_playbook }}\n    greeting=hello\n", "hello"),
])
def test_legacy_playbook_include_preserves_inline_variables(tmp_path, include, greeting):
    playbook = tmp_path / "play.yml"
    playbook.write_text(include)
    (tmp_path / "child.yml").write_text("""
- hosts: localhost
  gather_facts: false
  vars: {greeting: original}
  tasks: []
""")

    plays, _ = parse_playbook(str(playbook), extra_vars={"child_playbook": "child.yml"})
    assert plays[0]["vars"]["greeting"] == greeting
    assert "tags" not in plays[0]["vars"]


def test_legacy_playbook_include_preserves_explicit_raw_params(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("- include: child.yml _raw_params=hello\n")
    (tmp_path / "child.yml").write_text("- hosts: localhost\n  gather_facts: false\n  tasks: []\n")

    plays, _ = parse_playbook(str(playbook))
    assert plays[0]["vars"]["_raw_params"] == "hello"


@pytest.mark.parametrize("parameters", ["trailing text", "_raw_params=hello trailing text"])
def test_legacy_playbook_include_rejects_unassigned_parameters(tmp_path, parameters):
    from ansible.errors import AnsibleParserError

    playbook = tmp_path / "play.yml"
    playbook.write_text(f"- include: child.yml {parameters}\n")

    with pytest.raises(AnsibleParserError, match="must use key=value"):
        parse_playbook(str(playbook))


@pytest.mark.parametrize(("include", "tag"), [
    ("- include: child.yml tags=inline\n  tags: explicit\n", "explicit"),
    ("- tags: explicit\n  include: child.yml tags=inline\n", "inline"),
])
def test_legacy_playbook_include_uses_last_tag_declaration(tmp_path, include, tag):
    from ansible.parsing.dataloader import DataLoader
    from ansible.playbook import Playbook

    from ansiroll.loader import _make_rewriting_loader

    playbook = tmp_path / "play.yml"
    playbook.write_text(include)
    (tmp_path / "child.yml").write_text("- hosts: localhost\n  tasks: []\n")

    plays = Playbook.load(str(playbook), loader=_make_rewriting_loader(DataLoader)).get_plays()
    assert plays[0].tags == [tag]


@pytest.mark.parametrize("inline_tags", [False, True])
def test_legacy_playbook_include_defers_import_fields(tmp_path, monkeypatch, inline_tags):
    from ansiroll import loader

    include = "child.yml tags='{{ deployment_tag }}'" if inline_tags else "child.yml"
    tags = "" if inline_tags else '  tags: ["{{ deployment_tag }}"]\n'
    playbook = tmp_path / "play.yml"
    playbook.write_text(
        f'- include: "{include}"\n' + tags
        + """  when: "{{ inventory_hostname == 'localhost' }}"\n"""
    )
    (tmp_path / "child.yml").write_text("""
- hosts: localhost
  gather_facts: false
  vars: {deployment_tag: rollout}
  tasks:
    - debug: {msg: imported-task}
""")
    original_convert = loader._convert_play

    def check_tags(play, *args, **kwargs):
        assert play.tags == ["{{ deployment_tag }}"]
        return original_convert(play, *args, **kwargs)

    monkeypatch.setattr(loader, "_convert_play", check_tags)
    source, _ = compile_playbook(str(playbook))
    plans = _compiled_plays(source, tmp_path)
    condition = {"__ansiroll_trusted_condition__": "{{ inventory_hostname == 'localhost' }}"}
    assert plans[0]["gather_when"] == [condition]
    task = next(task for block in plans[0]["tasks"] for task in block["block"] if task.get("action") == "debug")
    assert task["when"] == [condition]
    result = subprocess.run([sys.executable, str(tmp_path / "compiled.py")],
                            capture_output=True, text=True, timeout=30, check=True)
    assert "imported-task" in result.stdout


def test_legacy_playbook_include_ignores_templated_import_name(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""
- name: "{{ deployment_name }}"
  include: child.yml
""")
    (tmp_path / "child.yml").write_text("""
- name: Child play
  hosts: localhost
  gather_facts: false
  vars: {deployment_name: rollout}
  tasks: []
""")

    plays, _ = parse_playbook(str(playbook))
    assert [play["name"] for play in plays] == ["Child play"]


@pytest.mark.parametrize("modern", ["import_playbook", "ansible.builtin.import_playbook", "ansible.legacy.import_playbook"])
def test_legacy_playbook_include_rejects_conflicting_imports(tmp_path, modern):
    from ansible.errors import AnsibleParserError

    playbook = tmp_path / "play.yml"
    playbook.write_text(f"- include: child.yml\n  {modern}: child.yml\n")
    (tmp_path / "child.yml").write_text("- hosts: localhost\n  tasks: []\n")

    with pytest.raises(AnsibleParserError, match="conflicting playbook include"):
        parse_playbook(str(playbook))


def test_legacy_playbook_include_keeps_aliased_mapping_in_later_vars(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""
- &imported
  include: child.yml
- hosts: localhost
  gather_facts: false
  vars: {settings: *imported}
  tasks: []
""")
    (tmp_path / "child.yml").write_text("- hosts: localhost\n  tasks: []\n")

    plays, _ = parse_playbook(str(playbook))
    assert plays[1]["vars"]["settings"] == {"include": "child.yml"}


def test_legacy_include_rewrite_preserves_list_form_vars_file_keys(tmp_path):
    playbook = tmp_path / "play.yml"
    playbook.write_text("""
- hosts: localhost
  gather_facts: false
  vars_files: [vars.yml]
  tasks:
    - include: tasks.yml
""")
    (tmp_path / "vars.yml").write_text("- include: preserved\n")
    (tmp_path / "tasks.yml").write_text('- debug: {msg: "{{ include }}"}\n')

    source, manifest = compile_playbook(str(playbook))
    assert not manifest["fallback_tasks"]
    assert _compiled_plays(source, tmp_path)[0]["vars"]["include"] == "preserved"
    result = subprocess.run([sys.executable, str(tmp_path / "compiled.py")],
                            capture_output=True, text=True, timeout=30, check=True)
    assert "preserved" in result.stdout


def test_tree_shake_uses_known_extra_vars_and_drops_module(tmp_path):
    play = tmp_path / "play.yml"
    play.write_text("""
- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.command: /bin/true
      when: run_mode == 'build'
    - ansible.builtin.debug:
        msg: retained
      when:
        - run_mode == 'update'
        - runtime_only | default(true)
""")
    src, manifest = compile_playbook(
        str(play), extra_vars={"run_mode": "update"}, tree_shake=True
    )
    plays = _compiled_plays(src, tmp_path)

    assert manifest["modules"] == []
    assert manifest["tree_shake"]["removed_nodes"] == 1
    def tasks(nodes):
        for node in nodes:
            if node.get("type") == "block":
                yield from tasks(node.get("block", []))
                yield from tasks(node.get("rescue", []))
                yield from tasks(node.get("always", []))
            elif node.get("type") == "include":
                yield from tasks(node.get("tasks", []))
            else:
                yield node
    task = next(node for node in tasks(plays[0]["tasks"])
                if node.get("action", "").endswith("debug"))
    assert task["action"].endswith("debug")
    assert task["when"] == ["runtime_only | default(true)"]


def test_tree_shake_short_circuits_unknown_runtime_values(tmp_path):
    play = tmp_path / "play.yml"
    play.write_text("""
- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.command: /bin/false
      when: report_to_slack and cloud_update_id is not defined
    - ansible.builtin.debug: {msg: retained unknown}
      when: report_to_slack or runtime_only
    - ansible.builtin.debug: {msg: retained true}
      when: not report_to_slack or runtime_only
""")

    src, manifest = compile_playbook(
        str(play), extra_vars={"report_to_slack": False}, tree_shake=True
    )
    plays = _compiled_plays(src, tmp_path)
    def leaf_tasks(nodes):
        for node in nodes:
            if node.get("type") == "block":
                yield from leaf_tasks(node.get("block", []))
                yield from leaf_tasks(node.get("rescue", []))
                yield from leaf_tasks(node.get("always", []))
            elif node.get("type") == "include":
                yield from leaf_tasks(node.get("tasks", []))
            elif node.get("action") != "meta":
                yield node
    tasks = list(leaf_tasks(plays[0]["tasks"]))

    assert manifest["modules"] == []
    assert manifest["tree_shake"]["removed_nodes"] == 1
    assert tasks[0]["when"] == ["report_to_slack or runtime_only"]
    assert tasks[1]["when"] == []


def test_tree_shake_can_freeze_playbook_vars(tmp_path):
    play = tmp_path / "play.yml"
    play.write_text("""
- hosts: localhost
  gather_facts: false
  vars:
    run_mode: update
    derived: '{{ run_mode == "update" }}'
  tasks:
    - ansible.builtin.command: /bin/true
      when: run_mode == 'build'
    - ansible.builtin.debug: {msg: retained}
      when: derived
""")

    src, manifest = compile_playbook(
        str(play), tree_shake=True, freeze_playbook_vars=True
    )

    assert manifest["modules"] == []
    tree = manifest["tree_shake"]
    assert tree["freeze_playbook_vars"] is True
    assert tree["frozen_var_names"] == ["derived", "run_mode"]
    assert tree["removed_nodes"] == 1
    assert "run_mode" not in src  # play/data values remain inside the encoded bundle


def test_freeze_var_uses_group_vars_and_rejects_unknown_or_shadowed(tmp_path):
    group_vars = tmp_path / "group_vars" / "all"
    group_vars.mkdir(parents=True)
    (group_vars / "main.yml").write_text("deployment_mode: update\n")
    play = tmp_path / "play.yml"
    play.write_text("""
- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.debug: {msg: retained}
      when: deployment_mode == 'update'
""")

    _, manifest = compile_playbook(
        str(play), tree_shake=True, freeze_vars=["deployment_mode"]
    )
    assert manifest["tree_shake"]["frozen_var_names"] == ["deployment_mode"]
    assert manifest["tree_shake"]["frozen_var_sources"] == {
        "deployment_mode": ["group_vars"]
    }

    with pytest.raises(ValueError, match="explicitly frozen variable 'missing' is undefined"):
        compile_playbook(str(play), freeze_vars=["missing"])

    shadowed = tmp_path / "shadowed.yml"
    shadowed.write_text("""
- hosts: localhost
  gather_facts: false
  vars: {run_mode: update}
  tasks:
    - ansible.builtin.debug: {msg: x}
      vars: {run_mode: build}
""")
    with pytest.raises(ValueError, match="cannot freeze 'run_mode'.*shadows"):
        compile_playbook(str(shadowed), freeze_vars=["run_mode"])


def test_compiled_copy_reads_content_addressed_binary_asset(tmp_path):
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"a\x00b\xffc")
    dest = tmp_path / "out.bin"
    play = tmp_path / "copy.yml"
    play.write_text("""
- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.copy:
        src: payload.bin
        dest: %s
""" % dest)
    src, manifest = compile_playbook(str(play))
    generated = tmp_path / "compiled.py"
    generated.write_text(src)

    proc = subprocess.run([sys.executable, str(generated)], capture_output=True, text=True)

    assert proc.returncode == 0, proc.stderr
    assert dest.read_bytes() == payload.read_bytes()
    assert manifest["bundles"]["asset_count"] == 1


def test_compiled_unarchive_uses_baked_controller_source_from_memory(tmp_path):
    archive = tmp_path / "payload.zip"
    with zipfile.ZipFile(str(archive), "w") as zipped:
        zipped.writestr("inside.txt", b"embedded archive payload\x00")
    destination = tmp_path / "extracted"
    destination.mkdir()
    play = tmp_path / "unarchive.yml"
    play.write_text("""
- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.unarchive:
        src: payload.zip
        dest: %s
""" % destination)

    src, manifest = compile_playbook(str(play))
    # copy/template stage their data in a temp dir like the action plugin;
    # unarchive must keep using the in-memory source.
    assert "ansiroll-unarchive-" not in src
    assert "memfd_create" in src
    generated = tmp_path / "compiled.py"
    generated.write_text(src)
    proc = subprocess.run(
        [sys.executable, str(generated)], capture_output=True, text=True
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (destination / "inside.txt").read_bytes() == b"embedded archive payload\x00"
    assert any(name.endswith("unarchive") for name in manifest["modules"])
    assert manifest["bundles"]["asset_count"] == 1


def test_async_unarchive_keeps_memory_source_alive_until_extraction(tmp_path):
    archive = tmp_path / "async-payload.zip"
    with zipfile.ZipFile(str(archive), "w") as zipped:
        zipped.writestr("async.txt", b"async memory payload")
    destination = tmp_path / "async-extracted"
    destination.mkdir()
    play = tmp_path / "async-unarchive.yml"
    play.write_text("""
- hosts: localhost
  gather_facts: false
  vars:
    ansible_async_dir: %s
  tasks:
    - ansible.builtin.unarchive:
        src: async-payload.zip
        dest: %s
      async: 30
      poll: 0
""" % (tmp_path / "async-jobs", destination))

    src, _ = compile_playbook(str(play))
    generated = tmp_path / "async-compiled.py"
    generated.write_text(src)
    proc = subprocess.run(
        [sys.executable, str(generated)], capture_output=True, text=True
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    extracted = destination / "async.txt"
    deadline = time.monotonic() + 10
    while not extracted.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert extracted.read_bytes() == b"async memory payload"


def test_specialized_artifact_rejects_conflicting_runtime_vars(tmp_path):
    play = tmp_path / "play.yml"
    play.write_text("""
- hosts: localhost
  gather_facts: false
  tasks:
    - ansible.builtin.debug: {msg: ok}
      when: run_mode == 'update'
""")
    src, _ = compile_playbook(
        str(play), extra_vars={"run_mode": "update"}, tree_shake=True
    )
    generated = tmp_path / "compiled.py"
    generated.write_text(src)
    env = dict(os.environ, ANSIROLL_EXTRA_VARS='{"run_mode":"build"}')

    proc = subprocess.run([sys.executable, str(generated)], env=env,
                          capture_output=True, text=True)

    assert proc.returncode == 2
    assert "conflict" in proc.stderr and "run_mode" in proc.stderr


def test_extra_vars_support_key_value_json_and_file(tmp_path):
    values = tmp_path / "vars.yml"
    values.write_text("from_file: 3\noverride: file\n")

    result = _load_extra_vars([
        "name=example enabled=true",
        '{"typed": true, "override": "json"}',
        "@" + str(values),
    ])

    assert result == {
        "name": "example", "enabled": "true", "typed": True,
        "from_file": 3, "override": "file",
    }


def test_compiled_simple_runs_and_matches(tmp_path):
    src, _ = compile_playbook(os.path.join(FIXTURES, "simple.yml"))
    path = os.path.join(tmp_path, "compiled.py")
    with open(path, "w") as fh:
        fh.write(src)
    import subprocess
    import sys
    proc = subprocess.run([sys.executable, path], capture_output=True, text=True)
    assert proc.returncode == 0
    # Direct runs default to Ansible-like output on stdout.
    assert "PLAY RECAP" in proc.stdout
    assert "ok=6" in proc.stdout and "changed=2" in proc.stdout
    # ANSIROLL_RAW=1 restores the framed JSON protocol.
    env = dict(os.environ, ANSIROLL_RAW="1")
    proc = subprocess.run([sys.executable, path], capture_output=True, text=True, env=env)
    assert proc.returncode == 0
    assert "__ANSIROLL_RESULT__" in proc.stdout
    assert "ok=6 changed=2" in proc.stderr

    env = dict(os.environ, ANSIROLL_PROFILE="1")
    proc = subprocess.run([sys.executable, path], capture_output=True, text=True, env=env)
    assert proc.returncode == 0
    match = re.search(r"ANSIROLL_ENGINE_PROFILE (\{.*\})", proc.stderr)
    assert match, proc.stderr
    engine = json.loads(match.group(1))
    assert engine["bootstrap"] > 0
    assert engine["runtime_total"] > 0
    assert engine["total"] >= engine["runtime_total"]
    assert engine["workers_started"] >= 1
    assert engine["worker_bundle"] > 0
    assert engine["slow_tasks"]
    assert engine["slow_engine_tasks"]
    assert all({"name", "action", "count", "total", "module", "engine", "max"}
               <= set(task) for task in engine["slow_tasks"])


def test_ssh_command_builder_handles_ansible_210_and_newer_signatures():
    strategy = _load_strategy_module()
    assert not hasattr(strategy, "_build_tsh_command")

    class PlayContext:
        remote_addr = "10.140.0.108"
        ssh_executable = "ssh-from-context"

    class Ansible210Conn:
        _play_context = PlayContext()

        def _build_command(self, binary, *other_args):
            return [binary] + list(other_args)

        def get_option(self, name):
            raise KeyError(name)

    class NewerConn:
        _play_context = PlayContext()

        def _build_command(self, binary, subsystem, *other_args):
            return [binary, subsystem] + list(other_args)

        def get_option(self, name):
            return {
                "host": "10.140.0.109",
                "ssh_executable": "ssh-from-option",
            }.get(name)

    remote = "/usr/bin/python3 -c 'bootstrap'"
    assert strategy._build_ssh_command(Ansible210Conn(), remote) == [
        "ssh",
        "10.140.0.108",
        remote,
    ]
    assert strategy._build_ssh_command(NewerConn(), remote) == [
        "ssh-from-option",
        "ssh",
        "10.140.0.109",
        remote,
    ]


def test_strategy_does_not_override_an_unconfigured_ssh_user():
    strategy = _load_strategy_module()
    plugin = strategy.StrategyModule.__new__(strategy.StrategyModule)
    plugin._t = lambda _templar, value: value
    plugin._resolve_password = lambda _hv, _templar: None

    class Connection:
        options = None

        def set_options(self, **kwargs):
            self.options = kwargs

    connection = Connection()
    plugin._apply_conn_options(
        connection, "ssh", {}, None, None, "testing7.tp.alfredsmartdata.com"
    )

    assert connection.options["direct"] == {
        "host": "testing7.tp.alfredsmartdata.com"
    }


def test_strategy_injects_fast_facts_into_remote_agent(monkeypatch):
    strategy = _load_strategy_module()
    plugin = strategy.StrategyModule.__new__(strategy.StrategyModule)
    plugin._variable_manager = SimpleNamespace(_extra_vars={})
    plugin._delegate_on_target = False
    plugin._first_host_name = "testing7"
    plugin._check_mode = False
    plugin._diff_mode = False
    plugin._profile_enabled = True
    monkeypatch.setenv("ANSIROLL_ACCELERATED_MODULES", "all")
    monkeypatch.setenv("ANSIROLL_FAST_FACTS", "1")

    header = plugin._agent_header(
        SimpleNamespace(name="testing7"),
        {"inventory_hostname": "testing7"},
        None,
    )

    assert "_INJECTED_ACCELERATED_MODULES = 'all'" in header
    assert "_INJECTED_FAST_FACTS = True" in header


def test_strategy_extra_vars_helper_preserves_json_booleans():
    strategy = _load_strategy_module()

    class VariableManager:
        _extra_vars = {"report_to_slack": False}

    assert strategy._extra_vars(VariableManager()) == {"report_to_slack": False}
    blob = strategy._json_blob(strategy._extra_vars(VariableManager()))
    assert json.loads(blob)["report_to_slack"] is False


def test_strategy_reads_runtime_created_controller_source_losslessly(tmp_path):
    strategy = _load_strategy_module()
    source = tmp_path / "runtime.bin"
    source.write_bytes(b"\x00\xff'\\\n")

    result = strategy.StrategyModule._read_controller_source(str(source))

    assert base64.b64decode(result["data"]) == source.read_bytes()
    assert result["meta"]["fullpath"] == str(source)


@pytest.mark.parametrize("action", ["copy", "template"])
def test_strategy_preserves_runtime_created_source_mode(tmp_path, action):
    delegation = "      delegate_to: localhost" if action == "template" else ""
    source = tmp_path / "runtime.txt"
    destination = tmp_path / "result.txt"
    playbook = """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - copy:
        content: fixture
        dest: %s
        mode: '0641'
    - %s:
        src: %s
        dest: %s
        mode: preserve
%s
""" % (source, action, source, destination, delegation)
    result = _run_strategy_play(tmp_path, playbook)
    assert result.returncode == 0, result.stdout + result.stderr
    assert destination.read_text() == "fixture"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o641


def test_strategy_does_not_create_an_empty_recap_row():
    strategy = _load_strategy_module()
    plugin = strategy.StrategyModule.__new__(strategy.StrategyModule)
    stats = SimpleNamespace(
        processed={}, ok={}, changed={}, failures={}, skipped={}, dark={},
        rescued={}, ignored={},
    )
    plugin._tqm = SimpleNamespace(_stats=stats)
    plugin._apply_stats(SimpleNamespace(name="testhost"), {})
    assert stats.processed == {}


def test_strategy_empty_tag_filtered_play_does_not_use_local_python(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: all
  gather_facts: false
  tasks:
    - debug:
        msg: excluded
      tags: excluded
""",
        "--tags", "selected",
        "-i", "remote.invalid,",
        environment={"ANSIROLL_PROVENANCE": "1"},
    )

    assert proc.returncode != 0
    output = proc.stdout + proc.stderr
    assert "No such file or directory: '/usr/bin/python3'" not in output


def test_strategy_excludes_failed_hosts_from_later_native_plays(tmp_path):
    result = _run_strategy_play(
        tmp_path,
        """- hosts: bad:good
  gather_facts: false
  connection: local
  tasks:
    - command: /bin/true
    - fail: {msg: expected host failure}
      when: inventory_hostname == 'bad'
- hosts: bad:good
  gather_facts: false
  connection: local
  tasks:
    - copy:
        content: reached
        dest: "{{ playbook_dir }}/{{ inventory_hostname }}.txt"
""",
        "-i", "bad,good,",
    )

    assert result.returncode != 0
    assert (tmp_path / "good.txt").read_text() == "reached", result.stdout + result.stderr
    assert not (tmp_path / "bad.txt").exists()


def test_strategy_controller_only_play_uses_controller_python(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: all
  gather_facts: false
  vars:
    ansible_python_interpreter: /usr/bin/python3
  tasks:
    - debug:
        msg: controller only
""",
        "-i", "remote.invalid,",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "controller only" in proc.stdout


def _run_strategy_play(tmp_path, playbook, *args, environment=None):
    path = tmp_path / "play.yml"
    path.write_text(playbook)
    env = os.environ.copy()
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["ANSIBLE_COLLECTIONS_PATH"] = os.path.join(ROOT, "collections")
    env["ANSIBLE_STRATEGY"] = "ansiroll.core.ansiroll"
    env["ANSIBLE_NOCOLOR"] = "1"
    env.update(environment or {})
    return subprocess.run(
        ["ansible-playbook", "-i", "localhost,", str(path), *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )


def test_strategy_honors_play_level_local_connection(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - command: /bin/true
""",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok=1" in proc.stdout


def test_strategy_preserves_set_fact_precedence_across_plays(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - set_fact:
        ansible_nodename: from_set_fact

- hosts: localhost
  gather_facts: false
  connection: local
  vars:
    ansible_nodename: from_play_vars
  tasks:
    - assert:
        that: ansible_nodename == 'from_set_fact'
""",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "failed=0" in proc.stdout


def test_strategy_native_only_rejects_linear_fallback(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: all
  gather_facts: false
  tasks:
    - debug:
        msg: controller only
""",
        "-i", "host1,host2",
        environment={"ANSIROLL_NATIVE_ONLY": "1"},
    )

    assert proc.returncode != 0
    assert "native-only mode rejected play" in proc.stderr
    assert "task-lockstep scheduling" in proc.stderr


def test_strategy_native_only_rejects_controller_fallback(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - set_fact:
        delegated_value: present
      delegate_to: localhost
""",
        environment={"ANSIROLL_NATIVE_ONLY": "1"},
    )

    assert proc.returncode != 0
    assert "native-only mode rejected play" in proc.stderr
    assert "delegate_to localhost" in " ".join(proc.stderr.split())


def test_strategy_reports_native_and_ansiballz_provenance(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - name: native debug
      debug:
        msg: native
    - name: real module
      command: /bin/true
""",
        environment={
            "ANSIROLL_NATIVE_ONLY": "1",
            "ANSIROLL_PROVENANCE": "1",
        },
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    records = [
        json.loads(line.split(" ", 1)[1])
        for line in proc.stdout.splitlines()
        if line.startswith("ANSIROLL_PROVENANCE ")
    ]
    assert any(
        record.get("scope") == "play"
        and record.get("mode") == "native-agent"
        for record in records
    )
    assert any(
        record.get("task") == "native debug"
        and record.get("mode") == "native-agent"
        for record in records
    )
    assert any(
        record.get("task") == "real module"
        and record.get("mode") == "real-ansiballz"
        for record in records
    )
    assert not any(
        record.get("mode", "").endswith("fallback")
        for record in records
    )


def test_strategy_reports_linear_fallback_provenance(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: all
  gather_facts: false
  tasks:
    - debug:
        msg: controller only
""",
        "-i", "host1,host2",
        environment={"ANSIROLL_PROVENANCE": "1"},
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert '"mode": "linear-fallback"' in proc.stdout


def test_strategy_reports_controller_fallback_provenance(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - name: delegated fact
      set_fact:
        delegated_value: present
      delegate_to: localhost
""",
        environment={"ANSIROLL_PROVENANCE": "1"},
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert '"mode": "controller-fallback"' in proc.stdout
    assert '"reason": "delegate_to localhost"' in proc.stdout


def test_strategy_debug_callback_displays_message_payload(tmp_path):
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - debug:
        msg: Tagged_task
""",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Tagged_task" in proc.stdout


def test_strategy_check_mode_does_not_write(tmp_path):
    copy_destination = tmp_path / "copy-must-not-exist"
    file_destination = tmp_path / "file-must-not-exist"
    forced_destination = tmp_path / "forced-write"
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: false
  connection: local
  tasks:
    - copy:
        content: check-mode
        dest: %s
    - file:
        path: %s
        state: touch
    - copy:
        content: explicit-override
        dest: %s
      check_mode: false
""" % (copy_destination, file_destination, forced_destination),
        "--check",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not copy_destination.exists()
    assert not file_destination.exists()
    assert forced_destination.read_text() == "explicit-override"


def test_strategy_task_can_force_check_mode(tmp_path):
    destination = tmp_path / "must-not-exist"
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: false
  connection: local
  vars:
    force_check: true
  tasks:
    - copy:
        content: forced-check-mode
        dest: %s
      check_mode: "{{ force_check }}"
""" % destination,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not destination.exists()


def test_strategy_honors_become_user(tmp_path):
    user = getpass.getuser()
    probe = subprocess.run(
        ["sudo", "-n", "-u", user, "true"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if probe.returncode != 0:
        pytest.skip("passwordless sudo to the current user is unavailable")
    proc = _run_strategy_play(
        tmp_path,
        """- hosts: localhost
  gather_facts: true
  connection: local
  become: true
  become_user: %s
  tasks:
    - command: id -un
      register: identity
    - assert:
        that: identity.stdout == %r
""" % (user, user),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok=3" in proc.stdout


def test_strategy_run_once_shares_result_and_facts_across_hosts(tmp_path):
    inventory = tmp_path / "inventory"
    inventory.write_text(
        "first ansible_connection=local\n"
        "second ansible_connection=local\n"
    )
    play = tmp_path / "run-once.yml"
    play.write_text(
        """- hosts: all
  gather_facts: false
  tasks:
    - set_fact:
        elected_host: "{{ inventory_hostname }}"
      run_once: true
      register: election
    - assert:
        that:
          - elected_host == 'first'
          - election.ansible_facts.elected_host == 'first'
"""
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["ANSIBLE_COLLECTIONS_PATH"] = os.path.join(ROOT, "collections")
    env["ANSIBLE_STRATEGY"] = "ansiroll.core.ansiroll"
    env["ANSIBLE_NOCOLOR"] = "1"
    proc = subprocess.run(
        ["ansible-playbook", "-i", str(inventory), str(play)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert re.search(r"^first\s*: ok=2\b", proc.stdout, re.MULTILINE)
    assert re.search(r"^second\s*: ok=1\b", proc.stdout, re.MULTILINE)


def test_strategy_rejects_multi_host_any_errors_fatal_before_running(tmp_path):
    inventory = tmp_path / "inventory"
    inventory.write_text(
        "first ansible_connection=local\n"
        "second ansible_connection=local\n"
    )
    destination = tmp_path / "must-not-run"
    play = tmp_path / "fatal.yml"
    play.write_text(
        """- hosts: all
  gather_facts: false
  any_errors_fatal: true
  tasks:
    - copy:
        content: unsafe
        dest: %s
""" % destination
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["ANSIBLE_COLLECTIONS_PATH"] = os.path.join(ROOT, "collections")
    env["ANSIBLE_STRATEGY"] = "ansiroll.core.ansiroll"
    env["ANSIBLE_NOCOLOR"] = "1"
    proc = subprocess.run(
        ["ansible-playbook", "-i", str(inventory), str(play)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "multi-host any_errors_fatal" in proc.stderr
    assert not destination.exists()


def test_strategy_runs_over_a_real_ssh_channel(tmp_path):
    sshd = shutil.which("sshd")
    ssh_keygen = shutil.which("ssh-keygen")
    if not sshd or not ssh_keygen:
        pytest.skip("OpenSSH server and key generator are required")

    client_key = tmp_path / "client"
    host_key = tmp_path / "host"
    subprocess.run(
        [ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(client_key)],
        check=True,
    )
    subprocess.run(
        [ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)],
        check=True,
    )
    authorized_keys = tmp_path / "authorized_keys"
    authorized_keys.write_bytes(client_key.with_suffix(".pub").read_bytes())

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = tmp_path / "sshd_config"
    config.write_text(
        "\n".join(
            [
                f"Port {port}",
                "ListenAddress 127.0.0.1",
                f"HostKey {host_key}",
                f"PidFile {tmp_path / 'sshd.pid'}",
                f"AuthorizedKeysFile {authorized_keys}",
                "StrictModes no",
                "PasswordAuthentication no",
                "KbdInteractiveAuthentication no",
                "PubkeyAuthentication yes",
                "UsePAM no",
                "PermitRootLogin no",
                "LogLevel ERROR",
                "",
            ]
        )
    )
    server = subprocess.Popen(
        [sshd, "-D", "-e", "-f", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if server.poll() is not None:
                pytest.skip("sshd could not start: %s" % server.stderr.read())
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.skip("sshd did not become ready")

        inventory = tmp_path / "ssh-inventory"
        inventory.write_text(
            "sshhost ansible_connection=ssh ansible_host=127.0.0.1 "
            f"ansible_port={port} ansible_user={getpass.getuser()} "
            f"ansible_ssh_private_key_file={client_key} "
            "ansible_ssh_common_args='-o StrictHostKeyChecking=no "
            "-o UserKnownHostsFile=/dev/null -o BatchMode=yes'\n"
        )
        play = tmp_path / "ssh-play.yml"
        play.write_text(
            """- hosts: sshhost
  gather_facts: false
  tasks:
    - command: printf ansiroll-over-ssh
      register: command_result
    - assert:
        that: command_result.stdout == 'ansiroll-over-ssh'
"""
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env["ANSIBLE_COLLECTIONS_PATH"] = os.path.join(ROOT, "collections")
        env["ANSIBLE_STRATEGY"] = "ansiroll.core.ansiroll"
        env["ANSIBLE_NOCOLOR"] = "1"
        proc = subprocess.run(
            ["ansible-playbook", "-i", str(inventory), str(play)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert re.search(r"^sshhost\s*: ok=2\b", proc.stdout, re.MULTILINE)
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
