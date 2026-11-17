"""Regressions for compiler/loader/CLI behavior, checked against ansible-playbook."""

import base64
import io
import json
import os
import re
import subprocess
import sys
import zipfile

import pytest

pytest.importorskip("ansible")

from ansiroll.cli import _load_extra_vars, main  # noqa: E402
from ansiroll.compiler import compile_playbook  # noqa: E402

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


def _ansible(playbook, *args):
    proc = subprocess.run(
        [os.path.join(os.path.dirname(sys.executable), "ansible-playbook"),
         "-i", "localhost,", "-c", "local", str(playbook)] + list(args),
        cwd=str(playbook.parent), env=_env(), capture_output=True, text=True,
    )
    return _summary(proc.stdout)


def _ansiroll(playbook, tmp_path, *compile_args, cwd=None):
    out = tmp_path / "compiled.py"
    proc = subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", str(playbook),
         "-o", str(out)] + list(compile_args),
        cwd=cwd or ROOT, env=_env(), capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    run = subprocess.run([sys.executable, str(out)], cwd=str(playbook.parent),
                         env=_env(), capture_output=True, text=True)
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


def _plays_json(source):
    data = re.search(r"_ANSIROLL_DATA_BUNDLE = b'([^']*)'", source).group(1)
    archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(data)))
    return json.loads(archive.read("plays.json").decode("utf-8"))


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
    play = _plays_json(_source)[0]
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
    nodes = list(_task_nodes(_plays_json(first)[0]["tasks"]))
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
