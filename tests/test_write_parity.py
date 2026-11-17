"""copy/template write-path parity: compiled runs vs real ansible-playbook.

Each scenario runs the same playbook through `ansible-playbook -c local` and
through the compiled standalone script, each against its own tmp directory,
then compares the registered results and the resulting files.
"""

import json
import os
import re
import stat
import subprocess
import sys
import tempfile

import pytest

pytest.importorskip("ansible")

from ansiroll.compiler import compile_playbook  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ansible_playbook():
    candidate = os.path.join(os.path.dirname(sys.executable), "ansible-playbook")
    return candidate if os.path.exists(candidate) else "ansible-playbook"


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["ANSIBLE_DEPRECATION_WARNINGS"] = "False"
    env["ANSIBLE_PYTHON_INTERPRETER"] = sys.executable
    return env


def _results(stdout):
    for line in stdout.splitlines():
        match = re.match(r'\s*"msg": (.*)$', line)
        if not match:
            continue
        value = json.loads(match.group(1))
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                continue
        if isinstance(value, list):
            return value
    raise AssertionError("no registered results in output:\n%s" % stdout)


def _normalize(result, workdir):
    out = {}
    for key, value in result.items():
        if key in ("invocation", "ansible_facts", "warnings", "exception"):
            continue
        if key == "failed" and value is False:
            continue
        if isinstance(value, str):
            value = value.replace(workdir, "<D>")
            if key in ("src", "backup_file"):
                # Temp/backup names embed random parts, pids and timestamps.
                name = os.path.basename(value)
                if re.fullmatch(r"(?:\.|tmp)[a-z0-9_]{8}", name):
                    name = "<random temp name>"
                elif "~" in name:
                    name = re.sub(r"\.\d+\.[\d-]+@[\d:]+~$", ".<backup>", name)
                value = name
        if key == "diff":
            value = json.loads(json.dumps(value).replace(workdir, "<D>"))
        out[key] = value
    return out


def _tree(directory):
    entries = {}
    for root, dirs, files in os.walk(directory):
        for name in dirs + files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, directory)
            if "~" in name:
                rel = re.sub(r"\.\d+\.[\d-]+@[\d:]+~$", ".<backup>", rel)
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                entries[rel] = ("link", os.readlink(full).replace(directory, "<D>"))
            elif stat.S_ISDIR(st.st_mode):
                entries[rel] = ("dir", oct(stat.S_IMODE(st.st_mode)))
            else:
                with open(full, "rb") as fh:
                    entries[rel] = ("file", oct(stat.S_IMODE(st.st_mode)), fh.read())
    return entries


def _run_both(tmp_path, tasks, files=None, check=False):
    outcomes = {}
    for mode in ("official", "compiled"):
        base = tmp_path / mode
        work = base / "work"
        work.mkdir(parents=True)
        for rel, (content, file_mode) in (files or {}).items():
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            os.chmod(str(path), file_mode)
        registers = sorted(set(re.findall(r"register: (r\d+)", tasks)),
                           key=lambda name: int(name[1:]))
        play = base / "play.yml"
        play.write_text(
            "- hosts: localhost\n"
            "  gather_facts: false\n"
            "  vars: {d: %s, v: 1}\n"
            "  tasks:\n%s"
            "    - debug: {msg: \"{{ [%s] | to_json }}\"}\n"
            % (json.dumps(str(work)), tasks, ",".join(registers))
        )
        args = ["--check"] if check else []
        if mode == "official":
            proc = subprocess.run(
                [_ansible_playbook(), "-i", "localhost,", "-c", "local", str(play)] + args,
                cwd=str(base), env=_env(), capture_output=True, text=True)
        else:
            assert not check
            source, _manifest = compile_playbook(str(play))
            script = base / "compiled.py"
            script.write_text(source)
            proc = subprocess.run(
                [sys.executable, str(script)],
                cwd=str(base), env=_env(), capture_output=True, text=True)
        results = [_normalize(r, str(work)) for r in _results(proc.stdout)]
        outcomes[mode] = (results, _tree(str(work)))
    return outcomes


def _assert_same(outcomes):
    official_results, official_tree = outcomes["official"]
    compiled_results, compiled_tree = outcomes["compiled"]
    assert len(compiled_results) == len(official_results)
    for index, (official, compiled) in enumerate(
            zip(official_results, compiled_results)):
        assert compiled == official, "result r%d differs" % (index + 1)
    assert compiled_tree == official_tree


def test_copy_attributes_force_backup_and_symbolic_modes_match_official(tmp_path):
    _assert_same(_run_both(tmp_path, """
    - copy: {content: "hello\\n", dest: "{{ d }}/a"}
      register: r1
    - copy: {content: "hello\\n", dest: "{{ d }}/a", mode: "0600"}
      register: r2
    - copy: {content: "hello\\n", dest: "{{ d }}/a", mode: "0600"}
      register: r3
    - copy: {content: "x\\n", dest: "{{ d }}/a", force: no}
      register: r4
    - copy: {content: "y\\n", dest: "{{ d }}/a", backup: yes}
      register: r5
    - copy: {content: "y\\n", dest: "{{ d }}/b.conf", mode: "u=rw,g=r"}
      register: r6
    - copy: {content: "y\\n", dest: "{{ d }}/b.conf", mode: "u=rwx,g=,o="}
      register: r7
    - copy: {content: {"k": [1, 2]}, dest: "{{ d }}/json"}
      register: r8
"""))


def test_copy_validate_owner_errors_and_symlink_dest_match_official(tmp_path):
    _assert_same(_run_both(tmp_path, """
    - file: {path: "{{ d }}/real", state: touch, mode: "0644"}
    - file: {path: "{{ d }}/link", src: "{{ d }}/real", state: link}
    - copy: {content: "new\\n", dest: "{{ d }}/link"}
      register: r1
    - copy: {content: "v\\n", dest: "{{ d }}/v", validate: "grep -q nomatch %s"}
      register: r2
      ignore_errors: true
    - copy: {content: "o\\n", dest: "{{ d }}/o", owner: ansiroll-no-such-user}
      register: r3
      ignore_errors: true
    - copy: {content: "x", dest: "{{ d }}/missing/x"}
      register: r4
      ignore_errors: true
    - copy: {content: "x", dest: "{{ d }}/sub/"}
      register: r5
      ignore_errors: true
    - copy: {content: "x", dest: "{{ d }}"}
      register: r6
      ignore_errors: true
"""))


def test_copy_follow_keeps_symlink_when_requested(tmp_path):
    _assert_same(_run_both(tmp_path, """
    - copy: {content: "a\\n", dest: "{{ d }}/real"}
    - file: {path: "{{ d }}/link", src: "{{ d }}/real", state: link}
    - copy: {content: "b\\n", dest: "{{ d }}/link", follow: true}
      register: r1
    - copy: {content: "b\\n", dest: "{{ d }}/link", follow: true, mode: "0640"}
      register: r2
"""))


def test_template_and_copy_src_match_official(tmp_path):
    files = {
        "templates/t.j2": (b"v={{ v }}\n", 0o640),
        "files/plain.txt": (b"plain\n", 0o604),
    }
    _assert_same(_run_both(tmp_path, """
    - template: {src: t.j2, dest: "{{ d }}/t"}
      register: r1
    - template: {src: t.j2, dest: "{{ d }}/p", mode: preserve}
      register: r2
    - template: {src: t.j2, dest: "{{ d }}/p", mode: "0600"}
      register: r3
    - template: {src: t.j2, dest: "{{ d }}/t", state: file}
      register: r4
      ignore_errors: true
    - copy: {src: plain.txt, dest: "{{ d }}/"}
      register: r5
    - copy: {src: plain.txt, dest: "{{ d }}/preserved", mode: preserve}
      register: r6
    - copy: {src: plain.txt, dest: "{{ d }}/plain.txt"}
      register: r7
    - copy: {src: plain.txt, content: "x", dest: "{{ d }}/x"}
      register: r8
      ignore_errors: true
""", files=files))


def test_copy_inline_content_preserve_and_remote_src_match_official(tmp_path):
    previous = os.umask(0o002)
    try:
        outcomes = _run_both(tmp_path, """
    - copy: {content: inline, dest: "{{ d }}/remote", remote_src: yes, mode: preserve}
      register: r1
    - copy: {content: inline, dest: "{{ d }}/attrs", mode: preserve}
      register: r2
    - copy: {content: inline, dest: "{{ d }}/attrs", mode: "0664"}
      register: r3
    - copy: {content: inline, dest: "{{ d }}/attrs", mode: preserve}
      register: r4
    - copy: {src: "{{ d }}/remote", dest: "{{ d }}/from_remote", remote_src: yes}
      register: r5
""")
    finally:
        os.umask(previous)
    _assert_same(outcomes)
    for name in ("remote", "attrs"):
        assert outcomes["compiled"][1][name] == ("file", "0o600", b"inline")


def test_copy_force_false_stats_destination_in_task_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ANSIROLL_COPY_DEST", "work/stale")
    outcomes = _run_both(tmp_path, """
    - copy: {content: wanted, dest: "$ANSIROLL_COPY_DEST", force: no}
      environment: {ANSIROLL_COPY_DEST: "{{ d }}/actual"}
      register: r1
""", files={"work/stale": (b"stale", 0o644)})
    _assert_same(outcomes)
    assert outcomes["compiled"][1]["actual"][2] == b"wanted"
    assert outcomes["compiled"][1]["stale"][2] == b"stale"


@pytest.mark.parametrize("task_environment", [False, True])
def test_copy_diff_mode_matches_official(tmp_path, monkeypatch, task_environment):
    pytest.importorskip("ansible")
    from ansiroll.embed import ModuleEmbedder
    from ansiroll.loader import COPY_ACTION_MODULES, _copy_action_settings
    from ansiroll.runtime import Runtime

    official_dir = tmp_path / "official"
    official_dir.mkdir()
    (official_dir / "f").write_text("old\n")
    play = tmp_path / "play.yml"
    controller_tmp = tmp_path / "controller-tmp"
    task_env = ""
    dest = str(official_dir / "f")
    if task_environment:
        stale = tmp_path / "stale"
        stale.write_text("stale\n")
        monkeypatch.setenv("ANSIROLL_COPY_DIFF_DEST", str(stale))
        dest = "$ANSIROLL_COPY_DIFF_DEST"
        task_env = "      environment: {ANSIROLL_COPY_DIFF_DEST: %s}\n" % (
            json.dumps(str(official_dir / "f")))
    play.write_text(
        "- hosts: localhost\n  gather_facts: false\n  tasks:\n"
        "    - copy: {content: \"new\\n\", dest: %s}\n      register: r\n"
        "%s"
        "    - debug: {msg: \"{{ [r] | to_json }}\"}\n"
        % (json.dumps(dest), task_env))
    proc = subprocess.run(
        [_ansible_playbook(), "-i", "localhost,", "-c", "local", "--diff", str(play)],
        env=dict(_env(), ANSIBLE_LOCAL_TEMP=str(controller_tmp)),
        capture_output=True, text=True)
    official = _normalize(_results(proc.stdout)[0], str(official_dir))

    compiled_dir = tmp_path / "compiled"
    compiled_dir.mkdir()
    (compiled_dir / "f").write_text("old\n")
    embedder = ModuleEmbedder()
    modules = {name: embedder.embed(name) for name in COPY_ACTION_MODULES}
    rt = Runtime(modules, "", {"inventory_hostname": "localhost"}, diff_mode=True)
    settings = _copy_action_settings()
    compiled_dest = str(compiled_dir / "f")
    if task_environment:
        rt._module_env = {"ANSIROLL_COPY_DIFF_DEST": compiled_dest}
    try:
        result = rt._run_file_action(
            "copy", settings, {"content": "new\n",
                               "dest": dest if task_environment else compiled_dest}, {})
    finally:
        rt.cleanup()
    compiled = _normalize(result, str(compiled_dir))
    if settings.get("_copy_inline_diff_source"):
        for side, directory in ((official, str(controller_tmp)),
                                (compiled, tempfile.gettempdir())):
            diff = side["diff"][0]
            header = diff["after_header"]
            assert os.path.commonpath([header, directory]) == directory
            assert re.fullmatch(r"tmp[a-z0-9_]{8}", os.path.basename(header))
            diff["after_header"] = "<controller temporary source>"
    assert compiled == official
    assert official["diff"][0]["before"] == "old\n"
