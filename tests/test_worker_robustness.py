"""Worker/subprocess robustness: stdin, exit handlers, async jobs, framing.

Playbook checks run under both real ``ansible-playbook`` and the standalone
ansiroll agent; each asserts Ansible's observable behavior in-play.
"""
import json
import os
import re
import signal
import struct
import subprocess
import sys
import time

import pytest

pytest.importorskip("ansible")

from ansiroll import runtime as runtime_module  # noqa: E402
from ansiroll.embed import ModuleEmbedder  # noqa: E402
from ansiroll.loader import _core_version  # noqa: E402
from ansiroll.runtime import Runtime  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "ansiroll", "worker.py")


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["ANSIBLE_COLLECTIONS_PATH"] = os.path.join(ROOT, "collections")
    env["ANSIBLE_DEPRECATION_WARNINGS"] = "False"
    env.pop("ANSIBLE_ASYNC_DIR", None)
    return env


def _run_both(tmp_path, playbook_text, timeout=120, controller_env=None, diff=False):
    env = dict(_env(), **(controller_env or {}))
    play = tmp_path / "play.yml"
    play.write_text(playbook_text.replace("@JOBS@", str(tmp_path / "async-jobs")))
    compiled = tmp_path / "play_compiled.py"
    subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", str(play), "-o", str(compiled)],
        cwd=str(tmp_path), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True, timeout=timeout,
    )
    official = ["ansible-playbook", "-i", "localhost,", "-c", "local",
                "-e", "ansible_python_interpreter=" + sys.executable, str(play)]
    agent = [sys.executable, str(compiled)]
    if diff:
        official.append("--diff")
        agent = [sys.executable, "-c", """
import runpy, sys
g = runpy.run_path(sys.argv[1])
sys.exit(g["main"](g["PLAYS"], g["MODULES"], g["ANSIBALLZ_CODE"],
                   host_vars={"inventory_hostname": "localhost"},
                   diff_mode=True, pretty=True, module_bundle=g["_ansiroll_import_bytes"],
                   resource_reader=g["_ansiroll_resource_read"]))
""", str(compiled)]
    runs = {"ansible": official, "ansiroll": agent}
    for name, cmd in runs.items():
        log = tmp_path / ("%s.log" % name)
        with open(str(log), "w") as out:
            # Keep our end of stdin open for the whole run: modules must not
            # see (or block on) the caller's stdin.
            proc = subprocess.Popen(
                cmd, cwd=str(tmp_path), env=env if name == "ansible" else _env(),
                stdin=subprocess.PIPE,
                stdout=out, stderr=subprocess.STDOUT,
            )
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                returncode = "timeout"
            finally:
                proc.stdin.close()
        output = log.read_text()
        assert returncode == 0 and "failed=0" in output, (
            "%s run failed (%s):\n%s" % (name, returncode, output))


STDIN_PLAY = """
- hosts: localhost
  gather_facts: false
  tasks:
    - command: head -c 4
      register: head
    - shell: readlink /proc/self/fd/0
      register: fd0
    - command: python3 -c "import sys; print(repr(sys.stdin.read()))"
      register: py
    - command: cat
      args:
        stdin: hello
      register: cat
    - assert:
        that:
          - head.stdout == ""
          - head.rc == 0
          - fd0.stdout is match("^pipe:")
          - py.stdout == "''"
          - cat.stdout == "hello"
"""


def test_module_children_read_eof_stdin_like_ansible(tmp_path):
    # The agent's own stdin stays open; module children must still get an
    # already-closed pipe, never the agent's stdin.
    _run_both(tmp_path, STDIN_PLAY, timeout=60)


def _worker_runtime(action):
    meta = ModuleEmbedder().embed(action)
    return Runtime({action: meta}, "", {"inventory_hostname": "localhost"})


def _with_alarm(seconds):
    def handler(signum, frame):
        raise AssertionError("timed out after %ss" % seconds)
    previous = signal.signal(signal.SIGALRM, handler)
    signal.alarm(seconds)
    return previous


def _clear_alarm(previous):
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


def test_worker_children_do_not_consume_the_request_pipe():
    rt = _worker_runtime("command")
    previous = _with_alarm(30)
    try:
        # Official module forked by the worker.
        official = rt.run_module("command", {"_raw_params": "head -c 4"})
        # Accelerated command executed inside the worker process.
        accelerated = rt._run_worker_accelerated(
            "command", {"_raw_params": "head -c 4"})
        after = rt.run_module("command", {"_raw_params": "echo still-framed"})
    finally:
        _clear_alarm(previous)
        rt.cleanup()
    assert official.get("rc") == 0 and official.get("stdout") == ""
    assert accelerated.get("rc") == 0 and accelerated.get("stdout") == ""
    assert after.get("stdout") == "still-framed"


def test_worker_module_runs_atexit_cleanup(tmp_path):
    # AnsibleModule.tmpdir registers shutil.rmtree via atexit; a real
    # AnsiballZ process runs it at interpreter shutdown.
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    source = tmp_path / "source.txt"
    source.write_text("payload")
    dest = tmp_path / "dest.txt"
    rt = _worker_runtime("get_url")
    rt._module_env = {"TMPDIR": str(tmp)}
    try:
        res = rt.run_module("get_url", {
            "url": "file://%s" % source, "dest": str(dest),
        })
    finally:
        rt.cleanup()
    assert not res.get("failed"), res
    assert dest.read_text() == "payload"
    assert os.listdir(str(tmp)) == []


ASYNC_PLAY = """
- hosts: localhost
  gather_facts: false
  vars:
    ansible_async_dir: "@JOBS@"
  tasks:
    - command: sh -c 'echo $FOO; echo $ANSIBLE_ASYNC_DIR >&2'
      environment:
        FOO: bar
        ANSIBLE_ASYNC_DIR: "@JOBS@-task"
      async: 30
      poll: 0
      register: job
    - async_status:
        jid: "{{ job.ansible_job_id }}"
      environment:
        ANSIBLE_ASYNC_DIR: "@JOBS@-task"
      register: status
      until: status.finished
      retries: 20
      delay: 1
    - assert:
        that:
          - "@LAUNCH_FLAGS@"
          - job.changed
          - job.ansible_job_id is match("^@JID_PREFIX@[0-9]+[.][0-9]+$")
          - job.results_file == "@JOBDIR@/" ~ job.ansible_job_id
          - "@STATUS_FLAGS@"
          - status.stdout == "bar"
          - status.stderr == "@JOBDIR@"
          - "@STATUS_PATH@"
          - status.ansible_job_id == job.ansible_job_id
    - async_status:
        jid: "{{ job.ansible_job_id }}"
        mode: cleanup
      environment:
        ANSIBLE_ASYNC_DIR: "@JOBS@-task"
      register: cleanup
    - stat:
        path: "{{ job.results_file }}"
      register: gone
    - assert:
        that:
          - cleanup.erased == job.results_file
          - not gone.stat.exists
"""


@pytest.mark.parametrize("directory_source", ["variable", "environment", "configuration"])
def test_async_job_results_env_and_status_match_ansible(tmp_path, directory_source):
    version = _core_version()
    play = ASYNC_PLAY.replace("@JID_PREFIX@", "j" if version >= (2, 15) else "")
    play = play.replace("@JOBDIR@", "@JOBS@-task" if version < (2, 12) else "@JOBS@")
    if directory_source != "variable":
        play = play.replace('  vars:\n    ansible_async_dir: "@JOBS@"\n', "")
        # A separate task verifies the baked option even when old Ansible
        # gives the task environment precedence over shell options.
        play += """
    - command: echo configured
      async: 30
      poll: 0
      register: configured
    - assert:
        that:
          - configured.results_file == "@JOBS@/" ~ configured.ansible_job_id
    """
    controller_env = {"ANSIBLE_ASYNC_DIR": str(tmp_path / "async-jobs")}
    if directory_source == "configuration":
        config = tmp_path / "ansible.cfg"
        config.write_text("[defaults]\nasync_dir = %s\n" % (tmp_path / "async-jobs"))
        controller_env = {"ANSIBLE_CONFIG": str(config)}
    elif directory_source == "variable":
        controller_env["ANSIBLE_ASYNC_DIR"] += "-controller"
    if version >= (2, 19):
        play = play.replace("@LAUNCH_FLAGS@", "job.started is sameas true and job.finished is sameas false")
        play = play.replace("@STATUS_FLAGS@", "status.started is sameas true and status.finished is sameas true")
    else:
        play = play.replace("@LAUNCH_FLAGS@", "job.started is sameas 1 and job.finished is sameas 0")
        status_started = "status.started is sameas 1" if version >= (2, 12) else "status.started is undefined"
        play = play.replace("@STATUS_FLAGS@", "status.finished is sameas 1 and " + status_started)
    status_path = ("status.results_file == job.results_file" if version >= (2, 12)
                   else "status.results_file is undefined")
    _run_both(tmp_path, play.replace("@STATUS_PATH@", status_path), controller_env=controller_env)


def test_async_check_mode_diff_and_status_validation(tmp_path):
    play = """
- hosts: localhost
  gather_facts: false
  vars:
    ansible_async_dir: "@JOBS@"
  tasks:
    - file:
        path: "@JOBS@/must-not-exist"
        state: directory
      check_mode: true
      async: 30
      poll: 0
      register: job
    - async_status:
        jid: "{{ job.ansible_job_id }}"
      register: status
      until: status.finished
      retries: 20
      delay: 1
    - stat:
        path: "@JOBS@/must-not-exist"
      register: touched
    - async_status:
        mode: status
      register: missing
      ignore_errors: true
    - async_status:
        jid: absent
        mode: bogus
      register: bad_mode
      ignore_errors: true
    - assert:
        that:
          - not touched.stat.exists
          - status.changed
          - status.diff.before.state == "absent"
          - status.diff.after.state == "directory"
          - missing.msg is search("jid is required|missing required arguments. jid")
          - 'bad_mode.msg == "value of mode must be one of: @MODE_CHOICES@, got: bogus"'
    - file:
        path: "@JOBS@/must-not-exist"
        state: absent
"""
    if _core_version() < (2, 17):
        play = play.replace("      check_mode: true\n", "")
        play = play.replace("    - file:\n", """
    - file:
        path: "@JOBS@/check-mode"
        state: directory
      check_mode: true
      async: 30
      poll: 0
      register: rejected
      ignore_errors: true
    - assert:
        that:
          - rejected.failed
          - rejected.msg == "check mode and async cannot be used on same task."
    - file:
""", 1)
        play = play.replace("          - not touched.stat.exists", "          - touched.stat.isdir")
    choices = "status, cleanup" if _core_version() >= (2, 13) else "cleanup, status"
    _run_both(tmp_path, play.replace("@MODE_CHOICES@", choices), diff=True)


ASYNC_TIMEOUT_PLAY = """
- hosts: localhost
  gather_facts: false
  vars:
    ansible_async_dir: "@JOBS@"
  tasks:
    - command: sleep 30
      async: 1
      poll: 0
      register: job
    - async_status:
        jid: "{{ job.ansible_job_id }}"
      register: status
      until: status.finished
      retries: 20
      delay: 1
      failed_when: false
    - assert:
        that:
          - status.finished
          - status.msg == "Timeout exceeded"
          - status.child_pid is defined
"""


def test_async_job_is_killed_at_its_time_limit(tmp_path):
    if _core_version() < (2, 12):
        pytest.skip("Ansible before 2.12 does not publish a timeout result")
    _run_both(tmp_path, ASYNC_TIMEOUT_PLAY)


def _children(pid):
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % entry) as fh:
                fields = fh.read().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if int(fields[1]) == pid:
            found.append((int(entry), fields[0]))
    return found


def test_async_jobs_detach_from_the_worker(tmp_path):
    rt = _worker_runtime("command")
    ctx = {"ansible_async_dir": str(tmp_path / "jobs")}
    try:
        res = rt._run_async_module(
            "command", {"_raw_params": "sleep 3"}, async_val=30, ctx=ctx)
        assert res["started"] is True and not res.get("failed"), res
        worker = rt._workers[False]["proc"]
        # The launcher was reaped and the job re-parented: no zombie or live
        # child of the worker holds its pipes.
        assert _children(worker.pid) == []
        # A worker that dies mid-job must not leave _worker_stderr (or the
        # response pipe) blocked until the job finishes.
        worker.kill()
        worker.wait()
        started = time.monotonic()
        rt._worker_stderr(False)
        assert time.monotonic() - started < 2.5
        assert worker.stdout.read() == b""
        with open(res["results_file"]) as fh:
            assert json.load(fh)["started"] is True
    finally:
        rt.cleanup()


def test_worker_stderr_does_not_block_on_a_live_worker():
    meta = dict(ModuleEmbedder().embed("ping"))
    meta["zip"] = "!!not-base64!!"
    rt = Runtime({"ping": meta}, "", {"inventory_hostname": "localhost"})
    previous = _with_alarm(30)
    started = time.monotonic()
    try:
        res = rt.run_module("ping", {})
    finally:
        elapsed = time.monotonic() - started
        _clear_alarm(previous)
        rt.cleanup()
    assert elapsed < 10
    assert res["failed"] is True
    assert "worker load failed" in res["msg"]


def test_worker_startup_stderr_can_exceed_pipe_capacity(monkeypatch):
    def noisy_worker():
        return (
            "import sys\n"
            "sys.stderr.write('x' * (2 * 1024 * 1024) + '\\nworker startup failure\\n')\n"
            "sys.stderr.flush()\n"
        )

    monkeypatch.setattr(runtime_module, "_worker_source", noisy_worker)
    rt = _worker_runtime("ping")
    rt.module_bundle = b"x" * (2 * 1024 * 1024)
    previous = _with_alarm(30)
    try:
        res = rt.run_module("ping", {})
    finally:
        _clear_alarm(previous)
        rt.cleanup()
    assert res["failed"] is True
    assert res["module_stderr"].endswith("worker startup failure")
    assert "worker startup failure" in res["msg"]


def test_worker_send_loops_over_short_writes():
    script = (
        "import os\n"
        "_write = os.write\n"
        "os.write = lambda fd, data: _write(fd, bytes(data[:3]))\n"
        "exec(compile(open(%r).read(), 'worker', 'exec'))\n" % WORKER
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    body = json.dumps({"op": "no-such-op"}).encode("utf-8")
    out, err = proc.communicate(struct.pack(">I", len(body)) + body, timeout=30)
    (size,) = struct.unpack(">I", out[:4])
    assert json.loads(out[4:4 + size].decode("utf-8")) == {"error": "unknown op"}, err


def test_chan_send_loops_over_short_writes(monkeypatch):
    read_fd, write_fd = os.pipe()
    real_write = os.write
    monkeypatch.setattr(
        runtime_module.os, "write", lambda fd, data: real_write(fd, bytes(data[:2])))
    try:
        runtime_module._chan_send(write_fd, {"t": "done", "failed": False})
    finally:
        monkeypatch.undo()
        os.close(write_fd)
    data = b""
    while True:
        chunk = os.read(read_fd, 4096)
        if not chunk:
            break
        data += chunk
    os.close(read_fd)
    (size,) = struct.unpack(">I", data[:4])
    assert json.loads(data[4:4 + size].decode("utf-8")) == {"t": "done", "failed": False}


def test_frame_send_loops_over_short_writes():
    class ShortStdin(object):
        def __init__(self):
            self.data = b""

        def write(self, data):
            chunk = bytes(data[:3])
            self.data += chunk
            return len(chunk)

        def flush(self):
            pass

    class Proc(object):
        stdin = ShortStdin()

    proc = Proc()
    Runtime({}, "", {})._frame_send(proc, {"op": "quit"})
    (size,) = struct.unpack(">I", proc.stdin.data[:4])
    assert json.loads(proc.stdin.data[4:4 + size].decode("utf-8")) == {"op": "quit"}
    assert re.match(br"^.{4}\{", proc.stdin.data, re.S)
