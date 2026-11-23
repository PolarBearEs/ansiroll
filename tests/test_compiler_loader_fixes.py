"""Regressions for compiler/loader/CLI behavior, checked against ansible-playbook."""

import base64
import io
import json
import os
import re
import subprocess
import sys
import zipfile
from datetime import datetime

import pytest

pytest.importorskip("ansible")

from ansiroll.cli import _load_extra_vars, main  # noqa: E402
from ansiroll.compiler import compile_playbook  # noqa: E402
from ansible.release import __version__ as ANSIBLE_VERSION  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECAP_RE = re.compile(
    r"ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)\s+"
    r"skipped=(\d+)\s+rescued=(\d+)\s+ignored=(\d+)"
)
MSG_RE = re.compile(r'^\s*"msg": "(.*)",?$', re.M)


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["ANSIBLE_COLLECTIONS_PATH"] = os.path.join(ROOT, "collections")
    env["ANSIBLE_DEPRECATION_WARNINGS"] = "False"
    env["ANSIBLE_NOCOLOR"] = "1"
    env["ANSIBLE_STDOUT_CALLBACK"] = "default"
    return env


def _write(base, files):
    for rel, text in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _summary(text):
    match = RECAP_RE.search(text)
    assert match, text
    return MSG_RE.findall(text), tuple(int(x) for x in match.groups())


def _ansible(playbook, *args, strategy="linear", returncode=None):
    env = dict(_env(), ANSIBLE_STRATEGY=strategy)
    proc = subprocess.run(
        [os.path.join(os.path.dirname(sys.executable), "ansible-playbook"),
         "-i", "localhost,", "-c", "local",
         "-e", "ansible_python_interpreter=" + sys.executable, str(playbook)] + list(args),
        cwd=str(playbook.parent), env=env, capture_output=True, text=True,
    )
    if returncode is not None:
        assert proc.returncode == returncode, proc.stdout + proc.stderr
    return _summary(proc.stdout)


def _ansiroll(playbook, tmp_path, *compile_args, cwd=None, returncode=None):
    out = tmp_path / "compiled.py"
    proc = subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", str(playbook),
         "-o", str(out)] + list(compile_args),
        cwd=cwd or ROOT, env=_env(), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run([sys.executable, str(out)], cwd=str(playbook.parent),
                         env=_env(), capture_output=True, text=True)
    if returncode is not None:
        assert run.returncode == returncode, run.stdout + run.stderr
    return _summary(run.stdout + run.stderr)


def _task_nodes(nodes):
    for node in nodes:
        if node.get("type") == "block":
            for key in ("block", "rescue", "always"):
                for child in _task_nodes(node.get(key) or []):
                    yield child
        else:
            yield node
            for child in _task_nodes(node.get("tasks") or []):
                yield child


def _bundle_json(source, entry="plays.json"):
    data = re.search(r"_ANSIROLL_DATA_BUNDLE = b'([^']*)'", source).group(1)
    archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(data)))
    return json.loads(archive.read(entry).decode("utf-8"))


def test_user_dicts_shaped_like_plugins_are_not_externalized(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars:
    repos:
      - {name: app, kind: git, source: "https://example.invalid/app.git"}
    inline: {kind: k, name: n, source: inline}
  tasks:
    - debug: msg="{{ repos[0].source }}"
    - debug:
        msg: "{{ inline.source }}"
""", "filter_plugins/f.py": """
class FilterModule(object):
    def filters(self):
        return {"shout": lambda v: str(v).upper()}
""", "pb2.yml": """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg="{{ 'x' | shout }}"
"""})
    expected = _ansible(tmp_path / "pb.yml")
    assert expected[0] == ["https://example.invalid/app.git", "inline"]
    assert _ansiroll(tmp_path / "pb.yml", tmp_path) == expected
    assert _ansiroll(tmp_path / "pb2.yml", tmp_path) == _ansible(tmp_path / "pb2.yml")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires FIFOs")
def test_compile_dynamic_role_sources_skips_unused_fifos(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  roles: [r]
""", "roles/r/tasks/main.yml": """
- set_fact: {selection: app}
- template: {src: "sites/{{ selection }}.j2", dest: /unused}
""", "roles/r/templates/sites/app.j2": "regular template\n"})
    sites = tmp_path / "roles/r/templates/sites"
    os.mkfifo(sites / "unused.txt")
    (sites / "linked.j2").symlink_to("unused.txt")

    artifact = tmp_path / "compiled.py"
    subprocess.run([sys.executable, "-m", "ansiroll.cli", "compile", str(tmp_path / "pb.yml"), "-o", str(artifact)],
                   cwd=ROOT, env=_env(), capture_output=True, check=True, timeout=15)
    template = next(node for node in _task_nodes(_bundle_json(artifact.read_text())[0]["tasks"])
                    if node.get("action", "").endswith("template"))
    assert set(template["_baked"]) == {"sites/app.j2"}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires FIFOs")
@pytest.mark.parametrize("replacement", ["fifo", "denied-fifo", "directory", "socket"])
def test_compile_dynamic_role_source_checks_opened_file(tmp_path, monkeypatch, replacement):
    import errno
    import socket
    from types import SimpleNamespace

    from ansiroll import loader
    from ansiroll.runtime import Runtime

    _write(tmp_path, {"earlier/sites/app.j2": "regular template\n",
                      "earlier/sites/swap.j2": "previous regular file\n",
                      "later/sites/swap.j2": "later template\n"})
    (tmp_path / "later/sites/branch").mkdir()
    target = tmp_path / "earlier/sites/swap.j2"
    open_fd = os.open
    opened = []
    if replacement == "socket":
        socket_path = target.with_name("s")
        with monkeypatch.context() as patch, socket.socket(socket.AF_UNIX) as listener:
            patch.chdir(socket_path.parent)
            listener.bind(socket_path.name)

    def swap_then_open(path, flags):
        if path == str(target):
            assert flags & os.O_NONBLOCK
            target.unlink()
            if replacement == "fifo":
                os.mkfifo(target)
            elif replacement == "denied-fifo":
                os.mkfifo(target, mode=0)
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), path)
            elif replacement == "socket":
                os.rename(socket_path, path)
            else:
                target.mkdir()
            fd = open_fd(path, flags)
            opened.append(fd)
            return fd
        return open_fd(path, flags)

    roots = [(str(tmp_path / name), True) for name in ("earlier", "later")]
    monkeypatch.setattr(loader, "_search_dirs", lambda *args, **kwargs: roots)
    monkeypatch.setattr(os, "open", swap_then_open)
    node = {}
    task = SimpleNamespace(_role=object())
    assert loader._bake_search_tree(node, task, str(tmp_path), "templates", "sites/{{ selection }}.j2")
    assert node["_baked"]["sites/app.j2"] == "regular template\n"
    direct = Runtime._baked_source_key(node, "sites/swap.j2")
    with pytest.raises(RuntimeError, match="not a regular file"):
        Runtime({}, "", {})._asset_text(node["_baked"][direct])
    traversed = Runtime._baked_source_key(node, "sites/branch/../swap.j2")
    assert node["_baked"][traversed] == "later template\n"
    if replacement in ("socket", "denied-fifo"):
        assert not opened
    else:
        assert len(opened) == 1
        with pytest.raises(OSError):
            os.fstat(opened[0])


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires FIFOs")
def test_compile_dynamic_role_source_does_not_substitute_later_file(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from ansiroll import loader
    from ansiroll.runtime import Runtime

    _write(tmp_path, {"earlier/sites/keep.j2": "keep\n", "later/sites/app.j2": "wrong template\n"})
    os.mkfifo(tmp_path / "earlier/sites/app.j2")
    roots = [(str(tmp_path / name), True) for name in ("earlier", "later")]
    monkeypatch.setattr(loader, "_search_dirs", lambda *args, **kwargs: roots)
    node = {}
    assert loader._bake_search_tree(node, SimpleNamespace(_role=object()), str(tmp_path), "templates",
                                    "sites/{{ selection }}.j2")
    assert node["_baked"]["sites/keep.j2"] == "keep\n"
    key = Runtime._baked_source_key(node, "sites/app.j2")
    with pytest.raises(RuntimeError, match="not a regular file"):
        Runtime({}, "", {})._asset_text(node["_baked"][key])


def test_compile_dynamic_role_source_retries_transient_lease(tmp_path, monkeypatch):
    import errno

    from ansiroll import loader

    source = tmp_path / "app.j2"
    source.write_text("regular template\n")
    open_fd = os.open
    attempts = []

    def busy_once(path, flags):
        attempts.append(path)
        if len(attempts) <= 2:
            raise BlockingIOError(errno.EWOULDBLOCK, "write lease is active", path)
        return open_fd(path, flags)

    monkeypatch.setattr(os, "open", busy_once)
    monkeypatch.setattr(loader.time, "sleep", lambda _seconds: None)
    os.close(loader._open_role_source(str(source)))
    assert attempts == [str(source)] * 3

    def still_busy(path, flags):
        raise BlockingIOError(errno.EWOULDBLOCK, "write lease is active", path)

    monkeypatch.setattr(os, "open", still_busy)
    with pytest.raises(ValueError, match="cannot bake role source .*app.j2"):
        loader._open_role_source(str(source))


@pytest.mark.parametrize(("template", "selected", "unrelated"), [
    ("sites/{{ selection }}.j2", "sites/app.j2", "sites/notes.txt"),
    ("sites/{{ selection -}}   .j2", "sites/app.j2", "sites/notes.txt"),
    ("sites/{{ selection | default('}}') }}.j2", "sites/app.j2", "sites/notes.txt"),
    ("sites/{{ selection }}.j2 ", "sites/app.j2 ", "sites/app.j2"),
    ("sites/{{ selection }}.j2\n", "sites/app.j2\n", "sites/app.j2"),
    ("sites/{{ selection }}/templates/app.j2", "sites/prod/templates/app.j2", "sites/notes/app.j2"),
    ("sites/{{ selection }}/keep/../templates/app.j2", "sites/prod/templates/app.j2", "sites/notes.txt"),
    ("sites/{{ selection }}/keep/../templates/app.j2", "sites/prod/templates/app.j2", "sites/notes/app.j2"),
])
def test_compile_dynamic_role_source_ignores_unrelated_locked_files(tmp_path, monkeypatch, template, selected, unrelated):
    from types import SimpleNamespace

    from ansiroll import loader

    _write(tmp_path, {selected: "usable template\n", unrelated: "unrelated\n"})
    original_open = loader._open_role_source

    def open_source(path):
        assert path == str(tmp_path / selected), "an unrelated path reached the role source reader"
        return original_open(path)

    monkeypatch.setattr(loader, "_search_dirs", lambda *args, **kwargs: [(str(tmp_path), True)])
    monkeypatch.setattr(loader, "_open_role_source", open_source)
    node = {}
    assert loader._bake_search_tree(node, SimpleNamespace(_role=object()), str(tmp_path), "templates",
                                    template)
    assert node["_baked"] == {selected: "usable template\n"}


def test_compile_dynamic_role_source_preserves_bare_expression_resolution(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  roles: [r]
""", "roles/r/tasks/main.yml": """
- set_fact: {selection: app}
- template: {src: "{{ selection }} ", dest: "rendered.txt"}
""", "roles/r/templates/app": "selected template\n"})

    _ansiroll(tmp_path / "pb.yml", tmp_path, returncode=0)
    assert (tmp_path / "rendered.txt").read_text() == "selected template\n"


@pytest.mark.parametrize("prefix", ["templates/", "./templates/"])
def test_compile_dynamic_role_source_respects_explicit_templates_prefix(tmp_path, prefix):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  roles: [r]
""", "roles/r/tasks/main.yml": """
- set_fact: {selection: app}
- template: {src: "%s{{ selection }}.j2", dest: "rendered.txt"}
""" % prefix, "roles/r/templates/app.j2": "selected template\n"})
    (tmp_path / "roles/r/templates/templates/app.j2").mkdir(parents=True)
    playbook = tmp_path / "pb.yml"
    destination = tmp_path / "rendered.txt"
    skips_subdirectory = prefix == "templates/" and _CORE >= (2, 21)
    _ansible(playbook, returncode=0 if skips_subdirectory else 2)
    if skips_subdirectory:
        assert destination.read_text() == "selected template\n"
        destination.unlink()
    _ansiroll(playbook, tmp_path, returncode=0 if skips_subdirectory else 1)
    if skips_subdirectory:
        assert destination.read_text() == "selected template\n"


@pytest.mark.parametrize("directory_target", [False, True])
def test_compile_dynamic_role_source_selects_root_from_traversal_target(tmp_path, directory_target):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  roles: [r]
""", "roles/r/tasks/main.yml": """
- set_fact: {selection: link/../app}
- template: {src: "sites/{{ selection }}.j2", dest: "rendered.txt"}
""", "roles/r/templates/sites/other/app.j2": "earlier traversal target\n",
        "roles/r/sites/app.j2": "selected template\n"})
    (tmp_path / "roles/r/templates/sites/app.j2").mkdir()
    later = tmp_path / "roles/r/sites"
    (later / "other/branch").mkdir(parents=True)
    target = later / "other/app.j2"
    if directory_target:
        target.mkdir()
    else:
        target.write_text("later traversal target\n")
    (later / "link").symlink_to("other/branch", target_is_directory=True)

    playbook = tmp_path / "pb.yml"
    destination = tmp_path / "rendered.txt"
    expected = _ansible(playbook, returncode=0)
    assert destination.read_text() == "selected template\n"
    destination.unlink()
    assert _ansiroll(playbook, tmp_path, returncode=0) == expected
    assert destination.read_text() == "selected template\n"


@pytest.mark.parametrize("later_root", [False, True])
def test_compile_dynamic_role_source_uses_nonregular_traversal_target_without_baking_it(tmp_path, later_root):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  roles: [r]
""", "roles/r/tasks/main.yml": """
- set_fact: {selection: link/../app, direct: app}
- template: {src: "sites/{{ selection }}.j2", dest: "rendered.txt"}
- template: {src: "sites/{{ direct }}.j2", dest: "direct.txt"}
""", "roles/r/templates/sites/app.j2": "first template\n"})
    sites = tmp_path / ("templates/sites" if later_root else "roles/r/templates/sites")
    if later_root:
        _write(sites, {"app.j2": "later template\n"})
    (sites / "other/branch").mkdir(parents=True)
    (sites / "other/app.j2").symlink_to(os.devnull)
    (sites / "link").symlink_to("other/branch", target_is_directory=True)

    playbook = tmp_path / "pb.yml"
    destination = tmp_path / "rendered.txt"
    selected = "later template\n" if later_root else "first template\n"
    expected = _ansible(playbook, returncode=0)
    assert destination.read_text() == selected
    destination.unlink()
    direct = tmp_path / "direct.txt"
    assert direct.read_text() == "first template\n"
    direct.unlink()
    assert _ansiroll(playbook, tmp_path, returncode=0) == expected
    assert destination.read_text() == selected
    assert direct.read_text() == "first template\n"
    template = next(node for node in _task_nodes(_bundle_json((tmp_path / "compiled.py").read_text())[0]["tasks"])
                    if node.get("action", "").endswith("template"))
    assert "sites/other/app.j2" not in template["_baked"]


@pytest.mark.parametrize("traversed_type", ["directory", "socket"])
def test_compile_dynamic_role_source_copy_distinguishes_traversed_file_types(tmp_path, monkeypatch, traversed_type):
    destination = tmp_path / "output"
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  roles: [r]
""", "roles/r/tasks/main.yml": """
- set_fact: {selection: link/../app}
- copy: {src: "sites/{{ selection }}.j2", dest: "%s/", local_follow: true}
""" % destination, "roles/r/sites/app.j2": "regular file at the normalized path\n",
        "roles/r/sites/copied-content.txt": "from the traversed directory\n"})
    (tmp_path / "roles/r/files/sites/app.j2").mkdir(parents=True)
    later = tmp_path / "roles/r/sites"
    (later / "other/branch").mkdir(parents=True)
    if traversed_type == "directory":
        (later / "other/app.j2").mkdir()
        (later / "other/app.j2/contents.txt").symlink_to("../../copied-content.txt")
    else:
        import socket

        with monkeypatch.context() as patch, socket.socket(socket.AF_UNIX) as source_socket:
            patch.chdir(later / "other")
            source_socket.bind("app.j2")
    (later / "link").symlink_to("other/branch", target_is_directory=True)

    playbook = tmp_path / "pb.yml"
    expected = _ansible(playbook, returncode=0)
    copied = destination / "app.j2"
    if traversed_type == "directory":
        contents = copied / "contents.txt"
        assert contents.read_text() == "from the traversed directory\n"
        contents.unlink()
        copied.rmdir()
        _ansiroll(playbook, tmp_path, returncode=1)
        assert not copied.exists()
    else:
        assert copied.read_text() == "regular file at the normalized path\n"
        copied.unlink()
        assert _ansiroll(playbook, tmp_path, returncode=0) == expected
        assert copied.read_text() == "regular file at the normalized path\n"


def test_compile_dynamic_role_source_uses_alias_suffix_targets_from_each_root(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  roles: [r]
""", "roles/r/tasks/main.yml": """
- set_fact: {selection: base/link/..}
- template: {src: "sites/{{ selection }}/live/app.j2", dest: "rendered.txt"}
""", "roles/r/templates/sites/first/app.j2": "earlier traversal target\n",
        "templates/sites/second/app.j2": "later traversal target\n",
        "templates/sites/base/live/app.j2": "selected template\n"})
    earlier = tmp_path / "roles/r/templates/sites"
    (earlier / "base/live/app.j2").mkdir(parents=True)
    (earlier / "other").mkdir()
    (earlier / "other/live").symlink_to("../first", target_is_directory=True)
    later = tmp_path / "templates/sites"
    (later / "other/branch").mkdir(parents=True)
    (later / "other/live").symlink_to("../second", target_is_directory=True)
    (later / "base/link").symlink_to("../other/branch", target_is_directory=True)

    playbook = tmp_path / "pb.yml"
    destination = tmp_path / "rendered.txt"
    expected = _ansible(playbook, returncode=0)
    assert destination.read_text() == "selected template\n"
    destination.unlink()
    assert _ansiroll(playbook, tmp_path, returncode=0) == expected
    assert destination.read_text() == "selected template\n"


@pytest.mark.parametrize("suffix", ["live", "prod/live", "keep/../prod/live"])
def test_compile_dynamic_role_source_retains_alias_traversal_dependencies(tmp_path, suffix):
    resolved_suffix = os.path.normpath(suffix)
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  roles: [r]
""", "roles/r/tasks/main.yml": """
- set_fact: {selection: area/keep/..}
- template: {src: "sites/{{ selection }}/%s/app.j2", dest: "rendered.txt"}
""" % suffix, "roles/r/templates/sites/actual/app.j2": "selected template\n",
        "templates/sites/area/" + resolved_suffix + "/app.j2": "selected template\n",
        "roles/r/templates/sites/unrelated/app.j2": "unrelated template\n"})
    area = tmp_path / "roles/r/templates/sites/area"
    (area / "keep").mkdir(parents=True)
    if resolved_suffix == "prod/live":
        sites = area.parent
        (sites / "staging").mkdir()
        (area / "prod").symlink_to("../staging", target_is_directory=True)
        (sites / "staging/live").symlink_to("../actual", target_is_directory=True)
        _write(sites, {"private/app.j2": "unreachable template\n", "dev/keep.txt": "data\n"})
        (sites / "dev/live").symlink_to("../private", target_is_directory=True)
    else:
        (area / "live").symlink_to("../actual", target_is_directory=True)
    playbook = tmp_path / "pb.yml"

    _ansiroll(playbook, tmp_path, returncode=0)
    assert (tmp_path / "rendered.txt").read_text() == "selected template\n"
    template = next(node for node in _task_nodes(_bundle_json((tmp_path / "compiled.py").read_text())[0]["tasks"])
                    if node.get("action", "").endswith("template"))
    assert "sites/actual/app.j2" in template["_baked"]
    assert "sites/unrelated/app.j2" not in template["_baked"]
    assert "sites/private/app.j2" not in template["_baked"]


@pytest.mark.parametrize("earlier_has_branch", [False, True])
@pytest.mark.parametrize("walks_earlier_root", [False, True])
def test_compile_dynamic_role_source_checks_parent_traversal_per_root(tmp_path, earlier_has_branch, walks_earlier_root):
    prefix = "sites/" if walks_earlier_root else ""
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  roles: [r]
""", "roles/r/tasks/main.yml": """
- set_fact: {selection: branch/../app}
- template: {src: "%s{{ selection }}.j2", dest: "rendered.txt"}
""" % prefix, "templates/" + prefix + "app.j2": "later template\n"})
    earlier = tmp_path / ("roles/r/templates/sites" if walks_earlier_root else "roles/r")
    (earlier / "app.j2").mkdir(parents=True)
    if earlier_has_branch:
        (earlier / "branch").mkdir()
    (tmp_path / "templates" / prefix / "branch").mkdir()

    playbook = tmp_path / "pb.yml"
    _ansible(playbook, returncode=2 if earlier_has_branch else 0)
    destination = tmp_path / "rendered.txt"
    if destination.exists():
        destination.unlink()
    _ansiroll(playbook, tmp_path, returncode=1 if earlier_has_branch else 0)
    if earlier_has_branch:
        assert not destination.exists()
    else:
        assert destination.read_text() == "later template\n"


@pytest.mark.parametrize("middle_has_branch", [False, True])
def test_compile_dynamic_role_source_continues_past_unreachable_candidates(tmp_path, middle_has_branch):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  roles: [r]
""", "roles/r/tasks/main.yml": """
- set_fact: {selection: branch/../app}
- template: {src: "sites/{{ selection }}.j2", dest: "rendered.txt"}
""", "roles/r/sites/app.j2": "middle template\n",
        "templates/sites/app.j2": "last template\n"})
    (tmp_path / "roles/r/templates/sites/app.j2").mkdir(parents=True)
    if middle_has_branch:
        (tmp_path / "roles/r/sites/branch").mkdir()
    (tmp_path / "templates/sites/branch").mkdir()

    playbook = tmp_path / "pb.yml"
    expected = _ansible(playbook, returncode=0)
    destination = tmp_path / "rendered.txt"
    expected_content = "middle template\n" if middle_has_branch else "last template\n"
    assert destination.read_text() == expected_content
    destination.unlink()
    assert _ansiroll(playbook, tmp_path, returncode=0) == expected
    assert destination.read_text() == expected_content


def test_compile_dynamic_role_source_checks_a_traversed_directory_without_normalized_input(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  roles: [r]
""", "roles/r/tasks/main.yml": """
- set_fact: {selection: link/../app}
- template: {src: "sites/{{ selection }}.j2", dest: "rendered.txt"}
""", "templates/sites/app.j2": "last template\n"})
    (tmp_path / "roles/r/templates/sites/app.j2").mkdir(parents=True)
    middle = tmp_path / "roles/r/sites"
    (middle / "nested/branch").mkdir(parents=True)
    (middle / "nested/app.j2").mkdir()
    (middle / "link").symlink_to("nested/branch", target_is_directory=True)
    last = tmp_path / "templates/sites"
    (last / "branch").mkdir()
    (last / "link").symlink_to("branch", target_is_directory=True)

    playbook = tmp_path / "pb.yml"
    _ansible(playbook, returncode=2)
    _ansiroll(playbook, tmp_path, returncode=1)
    assert not (tmp_path / "rendered.txt").exists()


@pytest.mark.parametrize("replacement", ["missing", "parent-file", "symlink-loop", "missing-on-recheck"])
def test_compile_dynamic_role_source_uses_later_file_after_concurrent_removal(tmp_path, monkeypatch, replacement):
    import errno
    from types import SimpleNamespace

    from ansiroll import loader

    _write(tmp_path, {"earlier/sites/app.j2": "removed\n", "later/sites/app.j2": "available\n"})
    earlier = tmp_path / "earlier/sites/app.j2"
    original_open = loader._open_role_source

    def open_source(path):
        if path == str(earlier):
            if replacement == "parent-file":
                earlier.parent.rename(tmp_path / "removed")
                earlier.parent.write_text("no longer a directory")
            else:
                earlier.unlink()
                if replacement == "symlink-loop":
                    earlier.symlink_to(earlier.name)
                elif replacement == "missing-on-recheck":
                    raise OSError(errno.ENXIO, os.strerror(errno.ENXIO), path)
        return original_open(path)

    roots = [(str(tmp_path / name), True) for name in ("earlier", "later")]
    monkeypatch.setattr(loader, "_search_dirs", lambda *args, **kwargs: roots)
    monkeypatch.setattr(loader, "_open_role_source", open_source)
    node = {}
    assert loader._bake_search_tree(node, SimpleNamespace(_role=object()), str(tmp_path), "templates",
                                    "sites/{{ selection }}.j2")
    assert node["_baked"] == {"sites/app.j2": "available\n"}


@pytest.mark.parametrize("earlier_has_branch", [False, True])
def test_compile_dynamic_role_source_can_bypass_an_unreadable_earlier_file(tmp_path, monkeypatch, earlier_has_branch):
    import errno
    from types import SimpleNamespace

    from ansiroll import loader
    from ansiroll.runtime import Runtime

    _write(tmp_path, {"earlier/sites/app.j2": "unreadable\n", "later/sites/app.j2": "available\n"})
    earlier = tmp_path / "earlier/sites/app.j2"
    (tmp_path / "later/sites/branch").mkdir()
    if earlier_has_branch:
        (earlier.parent / "branch").mkdir()
    original_open = loader._open_role_source

    def open_source(path):
        if path == str(earlier):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), path)
        return original_open(path)

    roots = [(str(tmp_path / name), True) for name in ("earlier", "later")]
    monkeypatch.setattr(loader, "_search_dirs", lambda *args, **kwargs: roots)
    monkeypatch.setattr(loader, "_open_role_source", open_source)
    node = {}
    assert loader._bake_search_tree(node, SimpleNamespace(_role=object()), str(tmp_path), "templates",
                                    "sites/{{ selection }}.j2")
    runtime = Runtime({}, "", {})
    direct = runtime._baked_source_key(node, "sites/app.j2")
    with pytest.raises(RuntimeError, match="Permission denied"):
        runtime._asset_text(node["_baked"][direct])
    traversed = runtime._baked_source_key(node, "sites/branch/../app.j2")
    if earlier_has_branch:
        with pytest.raises(RuntimeError, match="Permission denied"):
            runtime._asset_text(node["_baked"][traversed])
    else:
        assert node["_baked"][traversed] == "available\n"


@pytest.mark.parametrize("scenario", ["suffix_mismatch", "sibling_alias", "remote_redirect", "disappears", "chained"])
def test_compile_dynamic_role_source_keeps_only_eligible_special_traversal_duplicates(tmp_path, monkeypatch, scenario):
    from types import SimpleNamespace

    from ansiroll import loader
    from ansiroll.runtime import Runtime

    disappears = scenario == "disappears"
    _write(tmp_path, {"earlier/sites/prod/app.j2": "first\n", "later/sites/prod/app.j2": "later\n"})
    later = tmp_path / "later/sites"
    if scenario == "chained":
        special = later / "prod/middle/other"
        alias = later / "prod/middle/link"
        target = special / "branch"
        (later / "prod/middle/branch").mkdir(parents=True)
        (later / "prod/link").symlink_to("middle/branch", target_is_directory=True)
    elif disappears:
        special = later / "prod/other"
        alias = later / "prod/link"
        target = special / "branch"
    elif scenario == "remote_redirect":
        special = later / "isolated/prod"
        alias = later / "elsewhere/from/link"
        alias.parent.mkdir(parents=True)
        target = later / "elsewhere/to/branch"
    elif scenario == "sibling_alias":
        special = later / "isolated/prod"
        alias = later / "link"
        target = later / "peer"
    else:
        special = later / "unrelated"
        alias = later / "link"
        target = special / "branch"
    special.mkdir(parents=True)
    target.mkdir(parents=True)
    (special / "app.j2").symlink_to(os.devnull)
    alias.symlink_to(target, target_is_directory=True)
    duplicate = later / "prod/app.j2"
    original_open = loader._open_role_source

    def open_source(path):
        if disappears and path == str(duplicate):
            duplicate.unlink()
        return original_open(path)

    roots = [(str(tmp_path / name), True) for name in ("earlier", "later")]
    monkeypatch.setattr(loader, "_search_dirs", lambda *args, **kwargs: roots)
    monkeypatch.setattr(loader, "_open_role_source", open_source)
    node = {}
    source = ("sites/{{ selection }}.j2" if scenario in ("disappears", "chained")
              else "sites/{{ selection }}/prod/app.j2")
    assert loader._bake_search_tree(node, SimpleNamespace(_role=object()), str(tmp_path), "templates", source)
    assert node["_baked"]["sites/prod/app.j2"] == "first\n"
    if scenario == "chained":
        key = Runtime._baked_source_key(node, "sites/prod/link/../link/../app.j2")
        assert node["_baked"][key] == "later\n"
    else:
        assert "later\n" not in node["_baked"].values()
    assert duplicate.exists() is not disappears


@pytest.mark.parametrize(("tail", "required_dir_exists", "retains_duplicate"), [
    ("missing/../prod/app.j2", False, False),
    ("missing/../prod/app.j2", True, True),
    ("../prod/app.j2", False, True),
])
def test_compile_dynamic_role_source_checks_literal_traversal_before_retaining_duplicates(
        tmp_path, monkeypatch, tail, required_dir_exists, retains_duplicate):
    from types import SimpleNamespace

    from ansiroll import loader
    from ansiroll.runtime import Runtime

    _write(tmp_path, {"earlier/sites/prod/app.j2": "first\n", "later/sites/prod/app.j2": "later\n"})
    later = tmp_path / "later/sites"
    (later / "other/branch").mkdir(parents=True)
    (later / "other/prod").mkdir()
    (later / "other/prod/app.j2").symlink_to(os.devnull)
    (later / "link").symlink_to("other/branch", target_is_directory=True)
    if required_dir_exists:
        (later / "other/missing").mkdir()

    roots = [(str(tmp_path / name), True) for name in ("earlier", "later")]
    monkeypatch.setattr(loader, "_search_dirs", lambda *args, **kwargs: roots)
    node = {}
    source = "sites/{{ selection }}/" + tail
    assert loader._bake_search_tree(node, SimpleNamespace(_role=object()), str(tmp_path), "templates", source)
    assert ("later\n" in node["_baked"].values()) is retains_duplicate
    if retains_duplicate:
        traversal = "sites/link/" + (tail if tail.startswith("../") else "../" + tail)
        key = Runtime._baked_source_key(node, traversal)
        assert node["_baked"][key] == "later\n"


def test_compile_dynamic_role_source_uses_later_file_when_candidate_disappears_after_stat(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from ansible.parsing.dataloader import DataLoader

    from ansiroll import loader
    from ansiroll.runtime import Runtime

    _write(tmp_path, {"earlier/sites/app.j2": "removed\n", "later/sites/app.j2": "available\n"})
    earlier = tmp_path / "earlier/sites/app.j2"
    original_stat = os.stat

    def stat_then_remove(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path == str(earlier):
            earlier.unlink()
        return info

    roots = [(str(tmp_path / name), True) for name in ("earlier", "later")]
    monkeypatch.setattr(loader, "_search_dirs", lambda *args, **kwargs: roots)
    task = SimpleNamespace(_role=object(), _loader=DataLoader(), get_search_path=lambda: [base for base, _ in roots])
    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", stat_then_remove)
        node = {}
        assert loader._bake_search_tree(node, task, str(tmp_path), "templates", "sites/{{ selection }}.j2")
    assert not earlier.exists()
    key = Runtime._baked_source_key(node, "sites/app.j2")
    assert node["_baked"][key] == "available\n"


@pytest.mark.parametrize("replace_with_file", [False, True])
def test_compile_dynamic_role_source_rechecks_walked_directory(tmp_path, monkeypatch, replace_with_file):
    from types import SimpleNamespace

    from ansible.parsing.dataloader import DataLoader

    from ansiroll import loader
    from ansiroll.runtime import Runtime

    _write(tmp_path, {"later/sites/app.j2": "later file\n"})
    earlier = tmp_path / "earlier/sites/app.j2"
    earlier.mkdir(parents=True)
    original_walk = os.walk

    def walk_then_replace(path, *args, **kwargs):
        for directory, dirs, files in original_walk(path, *args, **kwargs):
            if directory == str(earlier.parent) and earlier.name in dirs and earlier.is_dir():
                earlier.rmdir()
                if replace_with_file:
                    earlier.write_text("replacement file\n")
            yield directory, dirs, files

    roots = [(str(tmp_path / name), True) for name in ("earlier", "later")]
    task = SimpleNamespace(_role=object(), _loader=DataLoader(), get_search_path=lambda: [base for base, _ in roots])
    monkeypatch.setattr(loader, "_search_dirs", lambda *args, **kwargs: roots)
    with monkeypatch.context() as patch:
        patch.setattr(os, "walk", walk_then_replace)
        node = {}
        assert loader._bake_search_tree(node, task, str(tmp_path), "templates", "sites/{{ selection }}.j2")
    key = Runtime._baked_source_key(node, "sites/app.j2")
    assert node["_baked"][key] == ("replacement file\n" if replace_with_file else "later file\n")


@pytest.mark.parametrize("leased_source_exists_at_runtime", [False, True])
def test_compile_dynamic_role_source_defers_lease_errors(tmp_path, monkeypatch, leased_source_exists_at_runtime):
    from ansiroll import loader

    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  roles: [r]
""", "roles/r/tasks/main.yml": """
- template: {src: "sites/{{ selection }}/keep/../templates/app.j2", dest: "rendered.txt"}
""", "roles/r/templates/sites/prod/templates/app.j2": "usable template\n",
        "roles/r/templates/sites/notes/templates/app.j2": "leased template\n"})
    sites = tmp_path / "roles/r/templates/sites"
    (sites / "prod/keep").mkdir()
    if leased_source_exists_at_runtime:
        (sites / "notes/keep").mkdir()
    leased = str(sites / "notes/templates/app.j2")
    original_open = loader._open_role_source

    def open_source(path):
        if path == leased:
            raise loader._RoleSourceLeaseError("write lease is active")
        return original_open(path)

    monkeypatch.setattr(loader, "_open_role_source", open_source)
    source, _manifest = compile_playbook(str(tmp_path / "pb.yml"))
    artifact = tmp_path / "compiled.py"
    artifact.write_text(source)

    result = subprocess.run([sys.executable, str(artifact)], cwd=tmp_path, capture_output=True, text=True,
                            env=dict(_env(), ANSIROLL_EXTRA_VARS='{"selection":"prod"}'), timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "rendered.txt").read_text() == "usable template\n"

    result = subprocess.run([sys.executable, str(artifact)], cwd=tmp_path, capture_output=True, text=True,
                            env=dict(_env(), ANSIROLL_EXTRA_VARS='{"selection":"notes"}'), timeout=15)
    assert result.returncode == 1, result.stdout + result.stderr
    expected = "write lease is active" if leased_source_exists_at_runtime else "was not baked"
    assert expected in result.stdout + result.stderr


@pytest.mark.parametrize("remote_src", ["true", "yes", "'1'"])
def test_copy_remote_src_reads_target_at_runtime(tmp_path, remote_src):
    src = tmp_path / "remote.txt"
    dest = tmp_path / "dest.txt"
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  tasks:
    - copy: src=%s dest=%s remote_src=%s
    - debug: msg="{{ lookup('file', '%s') }}"
""" % (src, dest, remote_src, dest)})
    src.write_text("AT_COMPILE\n")
    compiled = tmp_path / "compiled.py"
    proc = subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", str(tmp_path / "pb.yml"),
         "-o", str(compiled)], cwd=ROOT, env=_env(), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    src.write_text("ON_TARGET\n")
    run = subprocess.run([sys.executable, str(compiled)], env=_env(),
                         capture_output=True, text=True)
    got = _summary(run.stdout)
    dest.unlink()
    assert got == _ansible(tmp_path / "pb.yml")
    assert got[0] == ["ON_TARGET"]


def test_unarchive_remote_src_key_value_is_not_baked(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  tasks:
    - unarchive: src=/nonexistent/a.tgz dest=/tmp remote_src=yes
"""})
    _source, manifest = compile_playbook(str(tmp_path / "pb.yml"))
    assert manifest["fallback_tasks"] == []


def test_recursive_include_tasks_and_include_role(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars: {n: 0, m: 0}
  tasks:
    - include_tasks: rec.yml
    - include_role: name=rr
""", "rec.yml": """
- debug: msg="depth {{ n }}"
- set_fact: n="{{ n|int + 1 }}"
- include_tasks: rec.yml
  when: n|int < 3
""", "roles/rr/tasks/main.yml": """
- debug: msg="role depth {{ m }}"
- set_fact: m="{{ m|int + 1 }}"
- include_role: name=rr
  when: m|int < 2
"""})
    expected = _ansible(tmp_path / "pb.yml")
    assert expected[0] == ["depth 0", "depth 1", "depth 2", "role depth 0", "role depth 1"]
    assert _ansiroll(tmp_path / "pb.yml", tmp_path) == expected


def test_generated_agent_finishes_deep_recursive_include(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars: {iterations: 0}
  tasks:
    - include_tasks: count.yml
    - debug: msg="Completed {{ iterations }} iterations"
""", "count.yml": """
- set_fact: {iterations: "{{ iterations | int + 1 }}"}
- include_tasks: count.yml
  when: iterations | int < 425
"""})
    messages, recap = _ansiroll(tmp_path / "pb.yml", tmp_path, returncode=0)
    assert messages == ["Completed 425 iterations"]
    assert recap == (851, 0, 0, 0, 1, 0, 0)


def test_recursive_include_failure_unwinds_nested_blocks(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars: {iterations: 0}
  tasks:
    - block:
        - block:
            - include_tasks: count.yml
          rescue:
            - debug: msg="inner rescue"
          always:
            - debug: msg="inner always"
      always:
        - debug: msg="outer always"
    - debug: msg="Completed {{ iterations }} iterations"
""", "count.yml": """
- set_fact: {iterations: "{{ iterations | int + 1 }}"}
- include_tasks: count.yml
  when: iterations | int < 12
- name: stop
  fail: msg="at iteration {{ iterations }}"
"""})
    playbook = tmp_path / "pb.yml"
    expected = _ansible(playbook, returncode=0)
    assert expected[0] == [
        "inner rescue", "inner always", "outer always",
        "Completed 12 iterations",
    ]
    assert _ansiroll(playbook, tmp_path, returncode=0) == expected
    assert _ansible(playbook, strategy="ansiroll.core.ansiroll", returncode=0) == expected


@pytest.mark.parametrize("include", [
    "- include_tasks: ./rec.yml\n",
    '- include_tasks: "{{ selected_file }}"\n  vars: {selected_file: rec.yml}\n',
])
def test_recursive_include_reuses_stable_search_context(tmp_path, include):
    playbook = tmp_path / "pb.yml"
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars: {depth: 0}
  tasks:
""" + "".join("    " + line for line in include.splitlines(keepends=True)),
        "rec.yml": """
- debug: msg="depth {{ depth }}"
- set_fact: depth="{{ depth | int + 1 }}"
""" + include + "  when: depth | int < 34\n"})
    expected = _ansible(playbook)
    assert expected[0] == ["depth %d" % depth for depth in range(34)]
    source, manifest = compile_playbook(str(playbook))
    assert manifest["fallback_tasks"] == []
    assert any(node.get("tasks_ref") for node in _task_nodes(_bundle_json(source)[0]["tasks"]))
    assert _ansiroll(playbook, tmp_path) == expected
    assert _ansible(playbook, strategy="ansiroll.core.ansiroll") == expected


def test_constant_recursive_include_with_sibling(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars: {depth: 0}
  tasks:
    - include_tasks: "{{ 'rec.yml' }}"
""", "rec.yml": """
- include_tasks: leaf.yml
- set_fact: depth="{{ depth | int + 1 }}"
- include_tasks: "{{ 'rec.yml' }}"
  when: depth | int < 34
""", "leaf.yml": '- debug: msg="depth {{ depth }}"\n'})
    playbook = tmp_path / "pb.yml"
    expected = _ansible(playbook)
    assert expected[0] == ["depth %d" % depth for depth in range(34)]
    assert compile_playbook(str(playbook))[1]["fallback_tasks"] == []
    assert _ansiroll(playbook, tmp_path) == expected
    assert _ansible(playbook, strategy="ansiroll.core.ansiroll") == expected


def test_recursive_include_preserves_ancestry_for_rebound_sibling(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars: {depth: 0, selected_file: rec.yml}
  tasks:
    - include_tasks: "{{ selected_file }}"
""", "rec.yml": """
- set_fact: depth="{{ depth | int + 1 }}"
- include_tasks: "{{ selected_file }}"
  when: depth | int < 3
- include_tasks: leaf.yml
  vars: {selected_file: sub/rec.yml}
  when: depth | int == 3
""", "sub/sub/leaf.yml": "- debug: msg=REBOUND\n"})
    # Retain the compiler's search ancestry when a child rebinds a template.
    source, _manifest = compile_playbook(str(tmp_path / "pb.yml"))
    nodes = list(_task_nodes(_bundle_json(source)[0]["tasks"]))
    recursively_included = [node for node in nodes if node.get("file") == "rec.yml"]
    assert len(recursively_included) >= 3
    assert all(not node.get("tasks_ref") for node in recursively_included[:3])
    assert any(node.get("args", {}).get("msg") == "REBOUND" for node in nodes)


def test_recursive_include_keeps_symlink_alias_directory(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars: {depth: 0}
  tasks:
    - include_tasks: left/shared.yml
""", "shared.yml": """
- include_tasks: leaf.yml
- set_fact: depth="{{ depth | int + 1 }}"
- include_tasks: "%s"
  when: depth | int < 2
""" % (tmp_path / "right" / "shared.yml"),
        "left/leaf.yml": "- debug: msg=LEFT\n",
        "right/leaf.yml": "- debug: msg=RIGHT\n"})
    for side in ("left", "right"):
        (tmp_path / side / "shared.yml").symlink_to("../shared.yml")
    playbook = tmp_path / "pb.yml"
    expected = _ansible(playbook)
    assert expected[0] == ["LEFT", "RIGHT"]
    assert _ansiroll(playbook, tmp_path) == expected


def test_recursive_include_keeps_inherited_search_directories(tmp_path):
    right = tmp_path / "roles/r/tasks/right/prefix.yml"
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars: {depth: 0}
  roles: [r]
""", "roles/r/tasks/main.yml": "- include_tasks: left/prefix.yml\n",
        "roles/r/tasks/left/prefix.yml": "- include_tasks: shared.yml\n",
        "roles/r/tasks/right/prefix.yml": "- include_tasks: shared.yml\n",
        "roles/r/tasks/left/leaf.yml": "- debug: msg=LEFT\n",
        "roles/r/tasks/right/leaf.yml": "- debug: msg=RIGHT\n",
        "roles/r/tasks/shared.yml": """
- include_tasks: leaf.yml
- set_fact: depth="{{ depth | int + 1 }}"
- include_tasks: "%s"
  when: depth | int < 3
""" % right})
    playbook = tmp_path / "pb.yml"
    expected = _ansible(playbook)
    assert expected[0] == ["LEFT", "RIGHT", "RIGHT"]
    source, manifest = compile_playbook(str(playbook))
    assert manifest["fallback_tasks"] == []
    assert any(node.get("tasks_ref") for node in _task_nodes(_bundle_json(source)[0]["tasks"]))
    assert _ansiroll(playbook, tmp_path) == expected


def test_include_role_resolves_from_playbook_dir_not_cwd(tmp_path):
    _write(tmp_path / "pb", {"pb.yml": """
- hosts: localhost
  gather_facts: false
  tasks:
    - include_role: name=r
""", "roles/r/tasks/main.yml": """
- debug: msg=FROM_ROLE
"""})
    other = tmp_path / "elsewhere"
    other.mkdir()
    playbook = tmp_path / "pb" / "pb.yml"
    expected = _ansible(playbook)
    assert expected[0] == ["FROM_ROLE"]
    assert _ansiroll(playbook, tmp_path, cwd=str(other)) == expected


def test_lookup_order_matches_ansible(tmp_path):
    dest = tmp_path / "dest.txt"
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  tasks:
    - include_tasks: foo.yml
    - copy: src=cfg.txt dest=%s
    - debug: msg="{{ lookup('file', '%s') }}"
    - include_role: name=r
""" % (dest, dest), "foo.yml": "- debug: msg=TOP_LEVEL_FOO\n",
        "tasks/foo.yml": "- debug: msg=TASKS_DIR_FOO\n",
        "cfg.txt": "from-playbook-dir\n",
        "templates/cfg.txt": "from-templates-dir\n",
        "roles/r/tasks/main.yml": """
- template: src=t.txt dest=%s
- debug: msg="{{ lookup('file', '%s') }}"
""" % (dest, dest),
        "roles/r/files/t.txt": "role-files\n",
        "roles/r/templates/t.txt": "role-templates\n",
        "roles/r/t.txt": "role-root\n"})
    expected = _ansible(tmp_path / "pb.yml")
    dest.unlink()
    assert expected[0] == ["TOP_LEVEL_FOO", "from-playbook-dir", "role-templates"]
    assert _ansiroll(tmp_path / "pb.yml", tmp_path) == expected


def test_tree_shake_keeps_loop_shadowed_and_runtime_conditions(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg="LOOPVAR env={{ env }}"
      loop: [dev]
      loop_control: {loop_var: env}
      when: env == 'dev'
    - debug: msg="IDX"
      loop: [a]
      loop_control: {index_var: env}
      when: env == 0
    - debug: msg="TIME"
      when: now().year > 2000
    - debug: msg="RANDOM"
      when: "[true, true] | random"
    - debug: msg="SHAKEN"
      when: env == 'dev'
"""})
    expected = _ansible(tmp_path / "pb.yml", "-e", "env=prod")
    assert expected[0] == ["LOOPVAR env=dev", "IDX", "TIME", "RANDOM"]
    _source, manifest = compile_playbook(str(tmp_path / "pb.yml"),
                                         extra_vars={"env": "prod"}, tree_shake=True)
    assert manifest["tree_shake"]["removed_nodes"] == 1
    # Messages match; the recap differs only by the removed (skipped) task.
    got = _ansiroll(tmp_path / "pb.yml", tmp_path, "--tree-shake", "-e", "env=prod")
    assert got[0] == expected[0]


def test_tree_shake_keeps_enclosing_include_loop_bindings(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  tasks:
    - include_tasks: outer.yml
      loop: [dev]
      loop_control: {loop_var: env, index_var: position}
    - debug: msg=OUTSIDE
      when: env == 'dev'
""", "outer.yml": """
- block:
    - include_tasks: inner.yml
""", "inner.yml": """
- debug: msg=INHERITED_LOOP
  when:
    - env == 'dev'
    - position == 0
- debug: msg=LINEAR_EXTRA_VARS
  when: env == 'prod'
"""})
    playbook = tmp_path / "pb.yml"
    args = ("-e", "env=prod position=3")
    extra = {"env": "prod", "position": "3"}
    # Extra vars outrank the include parameters that carry the loop values, in linear and in ansiroll alike.
    expected = _ansible(playbook, *args)
    assert expected[0] == ["LINEAR_EXTRA_VARS"]
    assert _ansible(playbook, *args, strategy="ansiroll.core.ansiroll") == expected
    assert _ansiroll(playbook, tmp_path, *args) == expected
    source, manifest = compile_playbook(str(playbook), extra_vars=extra, tree_shake=True)
    assert manifest["tree_shake"]["removed_nodes"] == 1
    assert manifest["tree_shake"]["simplified_conditions"] == 0
    assert "OUTSIDE" not in json.dumps(_bundle_json(source))
    assert _ansiroll(playbook, tmp_path, "--tree-shake", *args)[0] == expected[0]


def test_tree_shake_keeps_recursive_include_loop_bindings(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars: {depth: 0}
  tasks:
    - include_tasks: recursive.yml
""", "recursive.yml": """
- debug: msg=RECURSIVE_LOOP
  when: env == 'dev'
- set_fact: {depth: "{{ depth | int + 1 }}"}
- include_tasks: recursive.yml
  loop: [dev]
  loop_control: {loop_var: env}
  when: depth | int < 2
"""})
    playbook = tmp_path / "pb.yml"
    args = ("-e", "env=prod")
    source, manifest = compile_playbook(
        str(playbook), extra_vars={"env": "prod"}, tree_shake=True,
    )
    nodes = _task_nodes(_bundle_json(source)[0]["tasks"])
    assert any(node.get("tasks_ref") for node in nodes)
    assert manifest["tree_shake"]["removed_nodes"] == 0
    # Extra vars outrank the include's loop_var, so the recursion never sees env == 'dev'.
    assert _ansible(playbook, *args)[0] == []
    assert _ansiroll(playbook, tmp_path, "--tree-shake", *args)[0] == []


def test_freeze_playbook_vars_follows_runtime_dependencies(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars:
    selected: initial
    intermediate: "{{ selected }}"
    rendered: "{{ intermediate }}"
    collection: ["{{ rendered }}"]
    scoped: outer
    scoped_alias: "{{ scoped }}"
    stable: constant
  tasks:
    - set_fact: {selected: updated}
    - debug:
        msg: "{{ collection[0] }} {{ stable }}"
    - debug: msg=SCOPED
      vars: {scoped: inner}
"""})
    playbook = tmp_path / "pb.yml"
    _source, manifest = compile_playbook(str(playbook), freeze_playbook_vars=True)
    info = manifest["tree_shake"]
    assert info["frozen_var_names"] == ["stable"]
    for name in ("intermediate", "rendered", "collection", "scoped_alias"):
        assert info["skipped_playbook_vars"][name] == "depends on a runtime variable"
        with pytest.raises(ValueError, match="depends on a runtime variable"):
            compile_playbook(str(playbook), freeze_vars=[name])
    expected = _ansible(playbook)
    assert expected[0] == ["updated constant", "SCOPED"]
    assert _ansible(playbook, strategy="ansiroll.core.ansiroll") == expected
    assert _ansiroll(playbook, tmp_path, "--freeze-playbook-vars") == expected


def test_frozen_vars_do_not_leak_or_conflict_across_plays(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars: {x: one, pkg: nginx, same: s}
  tasks: [{debug: {msg: "{{ x }} {{ pkg }} {{ same }}"}}]
- hosts: localhost
  gather_facts: false
  vars: {pkg: redis, same: s}
  tasks: [{debug: {msg: "{{ pkg }} {{ same }}"}}]
"""})
    _source, manifest = compile_playbook(str(tmp_path / "pb.yml"),
                                         freeze_playbook_vars=True)
    info = manifest["tree_shake"]
    assert info["frozen_var_names"] == ["same"]
    assert info["skipped_playbook_vars"] == {
        "pkg": "plays define different values",
        "x": "not defined in every play",
    }
    for name in ("x", "pkg"):
        with pytest.raises(ValueError):
            compile_playbook(str(tmp_path / "pb.yml"), freeze_vars=[name])
    assert _ansiroll(tmp_path / "pb.yml", tmp_path,
                     "--freeze-playbook-vars") == _ansible(tmp_path / "pb.yml")


def test_unsupported_module_inside_include_and_handler_is_marked(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  tasks:
    - include_tasks: inc.yml
  handlers:
    - name: h
      bad:
""", "inc.yml": "- bad:\n", "library/bad.py": "def main(:\n    pass\n"})
    _source, manifest = compile_playbook(str(tmp_path / "pb.yml"))
    assert [u["action"] for u in manifest["unsupported_modules"]] == ["bad"]
    assert len(manifest["fallback_tasks"]) == 2
    play = _bundle_json(_source)[0]
    bad = [n for n in _task_nodes(play["tasks"]) if n.get("action") == "bad"]
    assert len(bad) == 1 and bad[0]["fallback"] is True
    handlers = list(_task_nodes(play["handlers"]))
    assert len(handlers) == 1 and handlers[0]["fallback"] is True


def test_two_compiles_are_byte_identical(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  roles: [r]
  tasks:
    - copy: src=a.txt dest=/tmp/never
      when: false
    - include_role: name=r
""", "files/a.txt": "a\n", "roles/r/tasks/main.yml": """
- template: src="{{ item.name }}" dest=/tmp/never
  loop: "{{ things | default([]) }}"
""", "roles/r/templates/z/b.j2": "b\n", "roles/r/templates/a.j2": "a\n"})
    first = compile_playbook(str(tmp_path / "pb.yml"))[0]
    second = compile_playbook(str(tmp_path / "pb.yml"))[0]
    assert first == second
    nodes = list(_task_nodes(_bundle_json(first)[0]["tasks"]))
    copy_task = [n for n in nodes if n.get("action") == "copy"][0]
    assert copy_task["_baked"] and "_baked_meta" not in copy_task
    tree = [n for n in nodes if n.get("_baked_tree")][0]
    assert list(tree["_baked"]) == ["a.j2", "z/b.j2"]


def test_extra_vars_follow_ansible_parse_order(tmp_path):
    assert _load_extra_vars(["msg=hello: world"]) == {
        "msg": "hello:", "_raw_params": "world"}
    assert _load_extra_vars(['{"a": 1}', "b=2"]) == {"a": 1, "b": "2"}
    with pytest.raises(ValueError):
        _load_extra_vars(["./vars.yml"])
    with pytest.raises(ValueError):
        _load_extra_vars(["[1, 2]"])


def test_cli_reports_freeze_errors_cleanly(tmp_path, capsys):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars: {x: one}
  tasks: [{debug: {msg: "{{ x }}"}}]
- hosts: localhost
  gather_facts: false
  tasks: []
"""})
    rc = main(["compile", str(tmp_path / "pb.yml"), "-o", str(tmp_path / "o.py"),
               "--freeze-var", "x"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "Traceback" not in err
    assert "ansiroll: cannot freeze 'x'" in err


def test_tree_shake_keeps_conditions_on_jinja_globals(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg=NEVER
      when: now().year == 1999
    - debug: msg=STAMP
      when: "'%Y' | strftime == '1999'"
"""})
    _source, manifest = compile_playbook(str(tmp_path / "pb.yml"), tree_shake=True)
    assert manifest["tree_shake"]["removed_nodes"] == 0


def test_unarchive_remote_src_y_reads_target_at_runtime(tmp_path):
    archive = tmp_path / "a.tgz"
    out = tmp_path / "out"
    out.mkdir()
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  tasks:
    - unarchive: src=%s dest=%s remote_src=y
    - debug: msg="{{ lookup('file', '%s/f.txt') }}"
""" % (archive, out, out)})
    compiled = tmp_path / "compiled.py"
    proc = subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", str(tmp_path / "pb.yml"),
         "-o", str(compiled)], cwd=ROOT, env=_env(), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "f.txt").write_text("ON_TARGET\n")
    subprocess.check_call(["tar", "czf", str(archive), "-C", str(payload), "f.txt"])
    run = subprocess.run([sys.executable, str(compiled)], env=_env(),
                         capture_output=True, text=True)
    got = _summary(run.stdout + run.stderr)
    (out / "f.txt").unlink()
    assert got == _ansible(tmp_path / "pb.yml")
    assert got[0] == ["ON_TARGET"]


def test_copy_content_wins_over_remote_src(tmp_path):
    dest = tmp_path / "dest.txt"
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  tasks:
    - copy: content=INLINE dest=%s remote_src=yes
    - debug: msg="{{ lookup('file', '%s') }}"
""" % (dest, dest)})
    expected = _ansible(tmp_path / "pb.yml")
    dest.unlink()
    assert expected[0] == ["INLINE"]
    assert _ansiroll(tmp_path / "pb.yml", tmp_path) == expected


def test_included_file_lookups_do_not_depend_on_cwd(tmp_path):
    dest = tmp_path / "pb" / "dest.txt"
    _write(tmp_path / "pb", {"pb.yml": """
- hosts: localhost
  gather_facts: false
  tasks:
    - import_tasks: inc/t.yml
    - include_tasks: inc/t.yml
""", "inc/t.yml": """
- debug: msg="{{ lookup('file', 'data.txt') }}"
- copy: src=data.txt dest=%s
- debug: msg="{{ lookup('file', '%s') }}"
""" % (dest, dest), "inc/files/data.txt": "INC_FILES\n",
        "files/data.txt": "PB_FILES\n"})
    _write(tmp_path / "decoy", {"files/data.txt": "DECOY\n", "data.txt": "DECOY\n"})
    playbook = tmp_path / "pb" / "pb.yml"
    expected = _ansible(playbook)
    dest.unlink()
    assert expected[0] == ["INC_FILES"] * 4
    assert _ansiroll(playbook, tmp_path, cwd=str(tmp_path / "decoy")) == expected


def test_register_and_loop_vars_are_not_frozen(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars: {res: play, it: play, idx: play}
  tasks:
    - command: echo hi
      register: res
    - include_tasks: inc.yml
      loop: [a]
      loop_control: {loop_var: it, index_var: idx}
""", "inc.yml": '- debug: msg="{{ res.stdout }} {{ it }} {{ idx }}"\n'})
    _source, manifest = compile_playbook(str(tmp_path / "pb.yml"),
                                         freeze_playbook_vars=True)
    assert manifest["tree_shake"]["frozen_var_names"] == []
    assert sorted(manifest["tree_shake"]["skipped_playbook_vars"]) == ["idx", "it", "res"]
    for name in ("res", "it", "idx"):
        with pytest.raises(ValueError):
            compile_playbook(str(tmp_path / "pb.yml"), freeze_vars=[name])
    expected = _ansible(tmp_path / "pb.yml")
    assert expected[0] == ["hi a 0"]
    assert _ansiroll(tmp_path / "pb.yml", tmp_path, "--freeze-playbook-vars") == expected


def test_time_and_host_dependent_values_are_not_frozen_or_shaken(tmp_path, monkeypatch):
    compile_home = tmp_path / "compile-home"
    runtime_home = tmp_path / "runtime-home"
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "compile-target")
    monkeypatch.setenv("HOME", str(compile_home))
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars:
    stamp: "{{ now().year }}"
    home: "{{ '~' | expanduser }}"
    target: "{{ '%s' | realpath }}"
    alias: "{{ stamp }}"
    fixed: "{{ 'a' | upper }}"
  tasks:
    - debug: msg="{{ stamp }} {{ home }} {{ target }} {{ alias }} {{ fixed }}"
    - debug: msg=HOME
      when: "'~' | expanduser == '%s'"
    - debug: msg=LINK
      when: "'%s' | realpath == '%s'"
""" % (link, compile_home, link, tmp_path / "runtime-target")})
    playbook = tmp_path / "pb.yml"
    source, manifest = compile_playbook(str(playbook),
                                        freeze_playbook_vars=True, tree_shake=True)
    info = manifest["tree_shake"]
    assert info["frozen_var_names"] == ["fixed"]
    assert sorted(info["skipped_playbook_vars"]) == ["alias", "home", "stamp", "target"]
    assert info["removed_nodes"] == 0
    assert info["simplified_conditions"] == 0

    compiled = tmp_path / "compiled.py"
    compiled.write_text(source)
    link.unlink()
    link.symlink_to(tmp_path / "runtime-target")
    monkeypatch.setenv("HOME", str(runtime_home))
    run = subprocess.run([sys.executable, str(compiled)], cwd=tmp_path, env=_env(),
                         capture_output=True, text=True, check=True)
    expected = _ansible(playbook)
    year = datetime.now().year
    assert expected[0] == ["%s %s %s %s A" % (year, runtime_home, link.resolve(), year),
                           "LINK"]
    assert _summary(run.stdout) == expected


def test_explicit_freeze_allows_deterministic_jinja_globals(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars:
    ports: "{{ range(8000, 8003) | list }}"
  tasks:
    - debug: msg="{{ ports | join(',') }}"
"""})
    playbook = tmp_path / "pb.yml"
    source, manifest = compile_playbook(str(playbook), freeze_vars=["ports"])
    assert manifest["tree_shake"]["frozen_var_names"] == ["ports"]
    assert _bundle_json(source, "frozen_vars.json")["ports"] == [8000, 8001, 8002]
    expected = _ansible(playbook)
    assert expected[0] == ["8000,8001,8002"]
    assert _ansiroll(playbook, tmp_path, "--freeze-var", "ports") == expected


def test_runtime_filters_passed_to_map_stay_dynamic(tmp_path, monkeypatch):
    compile_home = tmp_path / "compile-home"
    runtime_home = tmp_path / "runtime-home"
    monkeypatch.setenv("HOME", str(compile_home))
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars:
    filter_name: expanduser
    home: "{{ ['~'] | map('expanduser') | first }}"
    indirect_home: "{{ ['~'] | map(filter_name) | first }}"
    fixed: "{{ ['a'] | map('upper') | first }}"
  tasks:
    - debug: msg="{{ home }} {{ indirect_home }} {{ fixed }}"
    - debug: msg=RUNTIME
      when: "(['~'] | map('expanduser') | first) == '%s'"
""" % runtime_home})
    playbook = tmp_path / "pb.yml"
    source, manifest = compile_playbook(str(playbook), freeze_playbook_vars=True,
                                        tree_shake=True)
    info = manifest["tree_shake"]
    assert info["frozen_var_names"] == ["filter_name", "fixed"]
    assert sorted(info["skipped_playbook_vars"]) == ["home", "indirect_home"]
    assert info["removed_nodes"] == 0
    with pytest.raises(ValueError, match="'home' is unresolved"):
        compile_playbook(str(playbook), freeze_vars=["home"])

    compiled = tmp_path / "compiled.py"
    compiled.write_text(source)
    monkeypatch.setenv("HOME", str(runtime_home))
    run = subprocess.run([sys.executable, str(compiled)], cwd=tmp_path, env=_env(),
                         capture_output=True, text=True, check=True)
    expected = _ansible(playbook)
    assert expected[0] == ["%s %s A" % (runtime_home, runtime_home), "RUNTIME"]
    assert _summary(run.stdout) == expected


def test_include_tasks_under_include_role_resolves_like_ansible(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  tasks:
    - include_role: name=r
""", "roles/r/tasks/main.yml": "- include_tasks: sub/a.yml\n",
        "roles/r/tasks/sub/a.yml": "- include_tasks: x.yml\n",
        "roles/r/sub/x.yml": "- debug: msg=ROLE_SUB_X\n"})
    expected = _ansible(tmp_path / "pb.yml")
    assert expected[0] == ["ROLE_SUB_X"]
    assert _ansiroll(tmp_path / "pb.yml", tmp_path) == expected


def test_nested_include_filename_uses_role_invocation_vars(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars: {selection: leaf}
  tasks:
    - include_role: name=r
      vars: {part: "{{ selection }}"}
""", "roles/r/tasks/main.yml": "- include_tasks: sub/entry.yml\n",
        "roles/r/tasks/sub/entry.yml": '- include_tasks: "{{ part }}.yml"\n',
        "roles/r/tasks/sub/leaf.yml": "- debug: msg=SCOPED\n",
        "roles/r/tasks/sub/extra.yml": "- debug: msg=EXTRA\n",
        "roles/r/defaults/main.yml": "part: decoy\n"})
    playbook = tmp_path / "pb.yml"
    expected = _ansible(playbook)
    assert expected[0] == ["SCOPED"]
    assert _ansiroll(playbook, tmp_path) == expected
    args = ("-e", "part=extra")
    expected = _ansible(playbook, *args)
    assert expected[0] == ["EXTRA"]
    assert _ansiroll(playbook, tmp_path, *args) == expected
    assert _ansible(playbook, *args, strategy="ansiroll.core.ansiroll") == expected


@pytest.mark.parametrize("writer, filename", [
    ("set_fact: {selected: second.yml}", "{{ selected }}"),
    ("set_fact: {selected: second.yml}", "{{ lookup('vars', 'selected') }}"),
    ("set_fact: {selected: second.yml}", "{{ query('ansible.builtin.vars', 'selected')[0] }}"),
    ("set_fact: {selected: second.yml}", "{{ q('ansible.legacy.vars', 'selected')[0] }}"),
    ("command: printf second.yml\n  register: selected", "{{ alias }}"),
])
def test_runtime_include_filename_is_left_as_fallback(tmp_path, writer, filename):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars:
    selected: first.yml
    intermediate: "{{ selected.stdout | default(selected) }}"
    alias: "{{ intermediate }}"
  tasks:
    - include_tasks: writer.yml
    - name: Select task file
      include_tasks: "%s"
""" % filename,
        "writer.yml": "- " + writer + "\n",
        "first.yml": "- debug: msg=STALE\n",
        "second.yml": "- debug: msg=RUNTIME\n",
        "extra.yml": "- debug: msg=EXTRA\n"})
    playbook = tmp_path / "pb.yml"
    assert _ansible(playbook)[0] == ["RUNTIME"]
    source, manifest = compile_playbook(str(playbook))
    assert [item["name"] for item in manifest["fallback_tasks"]] == ["Select task file"]
    assert "STALE" not in json.dumps(_bundle_json(source))
    # Dynamic include_tasks currently fail through the controller path. The
    # compiled artifact must fail too, with neither execution choosing STALE.
    strategy = _ansible(playbook, strategy="ansiroll.core.ansiroll", returncode=2)
    standalone = _ansiroll(playbook, tmp_path, returncode=1)
    assert "STALE" not in strategy[0] + standalone[0]
    assert strategy[1][3] == standalone[1][3] == 1

    args = ("-e", "selected=extra.yml")
    expected = _ansible(playbook, *args)
    assert expected[0] == ["EXTRA"]
    from ansible.release import __version__

    if __version__.startswith("2.10.") and "ansible.builtin.vars" in filename:
        # The 2.10 standalone Templar cannot load this qualified lookup.
        return
    assert compile_playbook(str(playbook), extra_vars={"selected": "extra.yml"})[1][
        "fallback_tasks"] == []
    assert _ansible(playbook, *args, strategy="ansiroll.core.ansiroll") == expected
    assert _ansiroll(playbook, tmp_path, *args) == expected


@pytest.mark.parametrize("lookup", ["lookup('vars', selector)", "lookup(plugin, 'selected')"])
def test_dynamic_vars_lookup_include_filename_is_left_as_fallback(tmp_path, lookup):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars:
    selector: selected
    plugin: vars
    selected: first.yml
  tasks:
    - set_fact: {selected: second.yml}
    - name: Select task file
      include_tasks: "{{ %s }}"
""" % lookup, "first.yml": "- debug: msg=STALE\n", "second.yml": "- debug: msg=RUNTIME\n"})
    playbook = tmp_path / "pb.yml"
    assert _ansible(playbook)[0] == ["RUNTIME"]
    source, manifest = compile_playbook(str(playbook))
    assert [item["name"] for item in manifest["fallback_tasks"]] == ["Select task file"]
    assert "STALE" not in json.dumps(_bundle_json(source))


@pytest.mark.parametrize("entry", ["config.yml", "[absent.yml, config.yml]"])
def test_include_filename_uses_vars_files_before_play_vars(tmp_path, entry):
    from ansible.release import __version__

    major_minor = tuple(int(part) for part in __version__.split(".")[:2])
    # Ansible started searching the vars/ subdirectory in 2.12.
    config = "vars/config.yml" if major_minor >= (2, 12) else "config.yml"
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars:
    selected: first.yml
    alias: "{{ selected }}"
  vars_files:
    - %s
  tasks:
    - include_tasks: "{{ alias }}"
""" % entry,
        config: "selected: second.yml\n",
        "first.yml": "- debug: msg=PLAY\n",
        "second.yml": "- debug: msg=VARS_FILE\n",
        "extra.yml": "- debug: msg=EXTRA\n"})
    playbook = tmp_path / "pb.yml"
    for args, marker in [((), "VARS_FILE"), (("-e", "selected=extra.yml"), "EXTRA")]:
        expected = _ansible(playbook, *args)
        assert expected[0] == [marker]
        assert _ansible(playbook, *args, strategy="ansiroll.core.ansiroll") == expected
        assert _ansiroll(playbook, tmp_path, *args) == expected


def test_include_filename_is_rechecked_after_discovering_nested_writes(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars:
    selected: first.yml
  tasks:
    - name: Select task file
      include_tasks: "{{ selected }}"
      when: false
    - include_tasks: writer.yml
""", "first.yml": "- debug: msg=STALE\n",
        "writer.yml": "- set_fact: {selected: second.yml}\n"})
    source, manifest = compile_playbook(str(tmp_path / "pb.yml"))
    assert [item["name"] for item in manifest["fallback_tasks"]] == ["Select task file"]
    assert "STALE" not in json.dumps(_bundle_json(source))


def test_nested_include_filename_merges_extra_vars(tmp_path, monkeypatch):
    from ansible import constants as C

    monkeypatch.setattr(C, "DEFAULT_HASH_BEHAVIOUR", "merge")
    monkeypatch.setenv("ANSIBLE_HASH_BEHAVIOUR", "merge")
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  tasks:
    - include_role: name=r
      vars: {selection: {part: left}}
""", "roles/r/tasks/main.yml": "- include_tasks: sub/entry.yml\n",
        "roles/r/tasks/sub/entry.yml": '- include_tasks: "{{ selection.part }}.yml"\n',
        "roles/r/tasks/sub/left.yml": "- debug: msg=MERGED\n"})
    playbook = tmp_path / "pb.yml"
    extra = {"selection": {"other": "extra"}}
    args = ("-e", json.dumps(extra))
    expected = _ansible(playbook, *args)
    assert expected[0] == ["MERGED"]
    assert compile_playbook(str(playbook), extra_vars=extra)[1]["fallback_tasks"] == []
    assert _ansiroll(playbook, tmp_path, *args) == expected
    assert _ansible(playbook, *args, strategy="ansiroll.core.ansiroll") == expected


@pytest.mark.parametrize("prefix", ["", "./", "existing/../", "alias/../", "missing/../", "loop/../"])
def test_role_copy_with_dynamic_src_and_remote_src_bakes_role_files(tmp_path, prefix):
    dest = tmp_path / "dest.txt"
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars: {name: app, flag: false}
  roles: [r]
""", "roles/r/tasks/main.yml": """
- copy: {src: "%s{{ name }}.conf", dest: "%s", remote_src: "{{ flag }}"}
- debug: msg="{{ lookup('file', '%s') }}"
""" % (prefix, dest, dest), "roles/r/files/app.conf": "CONTROLLER\n"})
    files = tmp_path / "roles/r/files"
    (files / "existing").mkdir()
    (files / "alias").symlink_to("existing", target_is_directory=True)
    (files / "loop").symlink_to("loop", target_is_directory=True)
    (files / "tree").symlink_to(".", target_is_directory=True)
    playbook = tmp_path / "pb.yml"
    expected = _ansible(playbook)
    if prefix in ("missing/../", "loop/../"):
        assert expected[1][3] == 1
        assert not dest.exists()
        result = _ansiroll(tmp_path / "pb.yml", tmp_path)
        assert result[1] == expected[1]
        assert not dest.exists()
    else:
        assert expected[0] == ["CONTROLLER"]
        assert dest.read_text() == "CONTROLLER\n"
        dest.unlink()
        assert _ansiroll(playbook, tmp_path) == expected
        assert dest.read_text() == "CONTROLLER\n"
        if prefix == "alias/../":
            source, _manifest = compile_playbook(str(playbook))
            copy = next(node for node in _task_nodes(_bundle_json(source)[0]["tasks"])
                        if node.get("action") == "copy")
            assert list(copy["_baked"]) == ["app.conf"]
            dest.unlink()
            assert _ansible(playbook, strategy="ansiroll.core.ansiroll") == expected


def test_compiles_are_identical_across_hash_seeds_and_cwds(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars: {a: 1, b: 2, c: 3}
  roles: [r]
""", "roles/r/tasks/main.yml": """
- template: src="{{ item }}" dest=/tmp/never
  loop: "{{ things | default([]) }}"
- include_tasks: t.yml
""", "roles/r/tasks/t.yml": "- debug: msg=x\n",
        "roles/r/templates/b.j2": "b\n", "roles/r/templates/a.j2": "a\n"})
    outputs = []
    for seed, cwd in (("1", ROOT), ("2", str(tmp_path))):
        out = tmp_path / ("o%s.py" % seed)
        env = _env()
        env["PYTHONHASHSEED"] = seed
        subprocess.check_call(
            [sys.executable, "-m", "ansiroll.cli", "compile", str(tmp_path / "pb.yml"),
             "-o", str(out)], cwd=cwd, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        outputs.append(out.read_bytes())
    assert outputs[0] == outputs[1]


def test_freeze_follows_vars_lookups_to_runtime_values(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars:
    home: "{{ '~' | expanduser }}"
    alias: "{{ lookup('vars', 'home') }}"
  tasks:
    - debug: msg="{{ alias }}"
"""})
    playbook = tmp_path / "pb.yml"
    _source, manifest = compile_playbook(str(playbook), freeze_playbook_vars=True)
    assert "alias" not in manifest["tree_shake"]["frozen_var_names"]
    with pytest.raises(ValueError):
        compile_playbook(str(playbook), freeze_vars=["alias"])


def test_tree_shake_keeps_conditions_reading_loop_bindings_through_aliases(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  tasks:
    - debug: msg=ALIAS
      loop: [dev]
      loop_control: {loop_var: env}
      when: alias == 'dev'
"""})
    playbook = tmp_path / "pb.yml"
    extra = {"env": "prod", "alias": "{{ env | default('prod') }}"}
    expected = _ansible(playbook, "-e", json.dumps(extra))
    assert expected[0] == ["ALIAS"]
    _source, manifest = compile_playbook(str(playbook), extra_vars=extra, tree_shake=True)
    assert manifest["tree_shake"]["removed_nodes"] == 0
    assert _ansiroll(playbook, tmp_path, "--tree-shake", "-e", json.dumps(extra)) == expected


def test_vars_files_parse_error_does_not_try_next_candidate(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars_files:
    - [broken.yml, fallback.yml]
  tasks:
    - debug: msg=RAN
""", "broken.yml": "key: [unclosed\n", "fallback.yml": "key: value\n",
        "vars/broken.yml": "key: [unclosed\n", "vars/fallback.yml": "key: value\n"})
    from ansible.errors import AnsibleParserError

    with pytest.raises(AnsibleParserError):
        compile_playbook(str(tmp_path / "pb.yml"))


def test_include_filename_merges_role_defaults_and_play_vars(tmp_path, monkeypatch):
    from ansible import constants as C

    monkeypatch.setattr(C, "DEFAULT_HASH_BEHAVIOUR", "merge")
    monkeypatch.setenv("ANSIBLE_HASH_BEHAVIOUR", "merge")
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars:
    selection: {other: value}
  roles: [r]
""", "roles/r/defaults/main.yml": "selection: {part: left}\n",
        "roles/r/tasks/main.yml": '- include_tasks: "{{ selection.part }}.yml"\n',
        "roles/r/tasks/left.yml": "- debug: msg=MERGED\n"})
    playbook = tmp_path / "pb.yml"
    expected = _ansible(playbook)
    assert expected[0] == ["MERGED"]
    assert compile_playbook(str(playbook))[1]["fallback_tasks"] == []
    assert _ansiroll(playbook, tmp_path) == expected


def test_vars_files_name_from_extra_vars_selects_include(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars:
    selected: old.yml
  vars_files:
    - "{{ config_file }}"
  tasks:
    - include_tasks: "{{ selected }}"
""", "selection.yml": "selected: new.yml\n", "vars/selection.yml": "selected: new.yml\n",
        "old.yml": "- debug: msg=OLD\n", "new.yml": "- debug: msg=NEW\n"})
    playbook = tmp_path / "pb.yml"
    args = ("-e", "config_file=selection.yml")
    expected = _ansible(playbook, *args)
    assert expected[0] == ["NEW"]
    assert _ansiroll(playbook, tmp_path, *args) == expected


def test_unresolved_vars_files_name_leaves_templated_include_to_runtime(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  vars:
    selected: old.yml
  vars_files:
    - "{{ ansible_system | lower }}.yml"
  tasks:
    - name: Select task file
      include_tasks: "{{ selected }}"
""", "old.yml": "- debug: msg=OLD\n"})
    _source, manifest = compile_playbook(str(tmp_path / "pb.yml"))
    assert [item["name"] for item in manifest["fallback_tasks"]] == ["Select task file"]


def test_unresolved_vars_files_name_keeps_includes_named_by_extra_vars_or_literals(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: false
  vars_files:
    - "{{ ansible_system | lower }}.yml"
  tasks:
    - name: From extra vars
      include_tasks: "{{ flavor }}.yml"
    - name: From a literal
      include_tasks: "{{ 'x' }}.yml"
""", "x.yml": "- debug: msg=X\n"})
    _source, manifest = compile_playbook(str(tmp_path / "pb.yml"), extra_vars={"flavor": "x"})
    assert manifest["fallback_tasks"] == []


_CORE = tuple(int(part) for part in ANSIBLE_VERSION.split(".")[:2])


def _run_both(playbook, tmp_path, *compile_args):
    """Return (returncode, output) of ansible-playbook and of the compiled script, for runs that may abort."""
    ansible = subprocess.run(
        [os.path.join(os.path.dirname(sys.executable), "ansible-playbook"), "-i", "localhost,", "-c", "local",
         "-e", "ansible_python_interpreter=" + sys.executable, str(playbook)],
        cwd=str(playbook.parent), env=_env(), capture_output=True, text=True)
    out = tmp_path / "compiled.py"
    proc = subprocess.run([sys.executable, "-m", "ansiroll.cli", "compile", str(playbook), "-o", str(out)]
                          + list(compile_args), cwd=ROOT, env=_env(), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run([sys.executable, str(out)], cwd=str(playbook.parent), env=_env(),
                         capture_output=True, text=True)
    return ((ansible.returncode, ansible.stdout + ansible.stderr), (run.returncode, run.stdout + run.stderr))


@pytest.mark.parametrize("name, gather, aborts", [
    ("vars/{{ nope_undefined }}.yml", "true", True),
    ("vars/{{ nope_undefined }}.yml", "false", False),
    ("vars/{{ ansible_nope | default('a') + 1 }}.yml", "false", True),
    # Before 2.15 get_vars defaulted to include_delegate_to=True, which makes a missing vars file fatal.
    ("vars/{{ ansible_system | lower }}-missing.yml", "true", _CORE < (2, 15)),
])
def test_deferred_vars_files_abort_like_ansible(tmp_path, name, gather, aborts):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  gather_facts: %s
  vars_files:
    - "%s"
  tasks:
    - debug: msg=reached
""" % (gather, name), "vars/other.yml": "x: 1\n"})
    (ansible_rc, ansible_out), (rc, out) = _run_both(tmp_path / "pb.yml", tmp_path)
    assert (ansible_rc != 0, "reached" not in ansible_out) == (aborts, aborts), ansible_out
    assert (rc != 0, "reached" not in out) == (aborts, aborts), out
    assert "Traceback" not in out


def test_selected_deferred_vars_file_that_fails_to_parse_aborts(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  vars_files:
    - ["vars/{{ ansible_system | lower }}.yml", "vars/default.yml"]
  tasks:
    - debug: msg=reached
""", "vars/linux.yml": "a: [1\n", "vars/darwin.yml": "a: [1\n", "vars/default.yml": "x: 1\n"})
    (ansible_rc, ansible_out), (rc, out) = _run_both(tmp_path / "pb.yml", tmp_path)
    assert ansible_rc == 4 and "reached" not in ansible_out
    assert rc == 4 and "reached" not in out, out


def test_freezing_skips_names_set_by_deferred_vars_files(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  vars:
    os_name: generic
  vars_files:
    - "vars/{{ ansible_system | lower }}.yml"
  tasks:
    - debug: msg="{{ os_name }}"
""", "vars/linux.yml": "os_name: linux\n", "vars/darwin.yml": "os_name: darwin\n"})
    playbook = tmp_path / "pb.yml"
    _source, manifest = compile_playbook(str(playbook), freeze_playbook_vars=True)
    assert "os_name" not in manifest["tree_shake"]["frozen_var_names"]
    with pytest.raises(ValueError, match="shadows it"):
        compile_playbook(str(playbook), freeze_vars=["os_name"])
    expected = _ansible(playbook)
    assert expected[0] == ["linux"]
    assert _ansiroll(playbook, tmp_path, "--freeze-playbook-vars") == expected


def test_deferred_vars_files_bundle_only_plain_mappings(tmp_path):
    from ansible.parsing.vault import VaultLib, VaultSecret
    from ansiroll.loader import parse_playbook

    secret = VaultLib([("default", VaultSecret(b"pw"))]).encrypt("token: hunter2\n")
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  vars_files:
    - "{{ ansible_system | lower }}.yml"
  tasks:
    - debug: msg=reached
""", "tasks.yml": "- debug: msg=task\n", "plain.yml": "x: 1\n", "pw.txt": "pw\n"})
    (tmp_path / "vault.yml").write_bytes(secret)
    inline = VaultLib([("default", VaultSecret(b"pw"))]).encrypt("hunter3").decode()
    (tmp_path / "inline.yml").write_text("token: !vault |\n" + "".join("  %s\n" % line for line in inline.splitlines()))
    plays, _actions = parse_playbook(str(tmp_path / "pb.yml"), vault_password_file=str(tmp_path / "pw.txt"))
    files = {os.path.basename(path): data for path, data in plays[0]["deferred_vars_files"][0]["files"].items()}
    assert files["plain.yml"] == [{"x": 1}]
    for name in ("pb.yml", "tasks.yml", "vault.yml", "inline.yml"):
        assert set(files[name]) == {"__ansiroll_vars_file_error__"}, name
    assert "hunter2" not in json.dumps(files) and "hunter3" not in json.dumps(files)


def test_deferred_vars_files_name_with_leading_template_fails_to_compile(tmp_path):
    _write(tmp_path, {"pb.yml": """
- hosts: localhost
  vars_files:
    - "{{ ansible_env.HOME }}/vars.yml"
  tasks:
    - debug: msg=reached
"""})
    with pytest.raises(ValueError, match="starts with a template"):
        compile_playbook(str(tmp_path / "pb.yml"))
