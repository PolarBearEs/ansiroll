"""Persistent stdlib-only module worker embedded into compiled agents."""

import atexit
import base64
import ctypes
import importlib.abc
import importlib.util
import io
import json
import os
import random
import runpy
import selectors
import shutil
import signal
import struct
import sys
import tempfile
import time
import traceback
import zipfile

_in = sys.stdin.buffer
_RESP = os.dup(1)  # reserve the original stdout fd for responses


class _MemZip(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, d):
        self.zf = zipfile.ZipFile(io.BytesIO(d))
        self.names = set(self.zf.namelist())
        self._code = {}

    def _f(self, name):
        p = name.replace(".", "/")
        return p + "/__init__.py", p + ".py"

    def _arc(self, name):
        pi, pm = self._f(name)
        return pi if pi in self.names else pm

    def find_spec(self, name, path=None, target=None):
        pi, pm = self._f(name)
        if pi in self.names:
            s = importlib.util.spec_from_loader(name, self, is_package=True)
            s.submodule_search_locations = [name.replace(".", "/")]
            return s
        if pm in self.names:
            return importlib.util.spec_from_loader(name, self, is_package=False)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        arc = self._arc(module.__name__)
        module.__file__ = "/_ansiroll_mem/" + arc
        exec(self.get_code(module.__name__), module.__dict__)

    def get_filename(self, name):
        return "/_ansiroll_mem/" + self._arc(name)

    def is_package(self, name):
        pi, _ = self._f(name)
        return pi in self.names

    def get_source(self, name):
        return self.zf.read(self._arc(name)).decode("utf-8", "surrogateescape")

    def get_code(self, name):
        arc = self._arc(name)
        code = self._code.get(arc)
        if code is None:
            code = compile(self.zf.read(arc), "/_ansiroll_mem/" + arc, "exec")
            self._code[arc] = code
        return code


class _LegacyLoader(importlib.abc.Loader):
    def __init__(self, loader):
        self.loader = loader

    def create_module(self, spec):
        return self.loader.load_module(spec.name)

    def exec_module(self, module):
        return None


class _LegacyFinder(importlib.abc.MetaPathFinder):
    def __init__(self, finder):
        self.finder = finder

    def find_spec(self, fullname, path=None, target=None):
        loader = self.finder.find_module(fullname, path)
        if loader is None:
            return None
        return importlib.util.spec_from_loader(fullname, _LegacyLoader(loader))


def _install_legacy_finder_adapters():
    # ansible-base 2.10's vendored six.moves importer only implements the
    # pre-find_spec protocol, which Python 3.12+ no longer consults.
    try:
        __import__("ansible.module_utils.six")
    except Exception:
        return
    for finder in list(sys.meta_path):
        if not hasattr(finder, "find_spec") and hasattr(finder, "find_module"):
            sys.meta_path.insert(sys.meta_path.index(finder), _LegacyFinder(finder))


def _readn(n):
    chunks = []
    size = 0
    while size < n:
        c = _in.read(n - size)
        if not c:
            return None
        chunks.append(c)
        size += len(c)
    return b"".join(chunks)


def _recv():
    h = _readn(4)
    if not h:
        return None
    (n,) = struct.unpack(">I", h)
    return json.loads(_readn(n).decode("utf-8"))


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if not written:
            raise OSError("worker write made no progress")
        view = view[written:]


def _send(obj):
    if obj is None:
        obj = {"_ansiroll_fallback": True}
    b = json.dumps(obj).encode("utf-8")
    _write_all(_RESP, struct.pack(">I", len(b)) + b)


def _load_zip_bytes(data):
    sys.meta_path.insert(0, _MemZip(data))


def _load_zip_base64(data):
    _load_zip_bytes(base64.b64decode(data))


def _memfd_source(params, data):
    if data is None:
        return params, None
    libc = ctypes.CDLL(None, use_errno=True)
    create = getattr(libc, "memfd_create", None)
    name = b"ansiroll-unarchive"
    if create is not None:
        create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        create.restype = ctypes.c_int
        fd = create(name, 0)
    else:
        machine = os.uname()[4].lower()
        numbers = {
            "x86_64": 319,
            "amd64": 319,
            "i386": 356,
            "i486": 356,
            "i586": 356,
            "i686": 356,
            "arm": 385,
            "armv6l": 385,
            "armv7l": 385,
            "aarch64": 279,
            "arm64": 279,
            "riscv64": 279,
            "ppc64": 360,
            "ppc64le": 360,
            "s390x": 350,
        }
        number = numbers.get(machine)
        if number is None:
            raise OSError("ansiroll: memfd_create is unsupported on %s" % machine)
        libc.syscall.restype = ctypes.c_long
        fd = int(libc.syscall(number, name, 0))
    if fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, "ansiroll: memfd_create failed: %s" % os.strerror(error))
    try:
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise OSError("ansiroll: short write to unarchive memfd")
            offset += written
        os.lseek(fd, 0, os.SEEK_SET)
        path = "/proc/%d/fd/%d" % (os.getpid(), fd)
        if not os.path.exists(path):
            raise OSError("ansiroll: /proc is required for in-memory unarchive")
        payload = json.loads(params)
        payload["ANSIBLE_MODULE_ARGS"]["src"] = path
        return json.dumps(payload), fd
    except BaseException:
        os.close(fd)
        raise


def _copy_stat(args):
    """The parts of the stat module the copy action plugin reads
    (ActionBase._execute_remote_stat): sha1 checksum of a readable regular
    file, '1' when missing and '' when no checksum applies."""
    import hashlib
    import stat as stat_mod

    path = os.path.expanduser(os.path.expandvars(args.get("path") or ""))
    try:
        st = os.stat(path) if args.get("follow") else os.lstat(path)
    except FileNotFoundError:
        return {"exists": False, "checksum": "1"}
    except OSError as e:
        return {"failed": True, "msg": e.strerror}
    out = {
        "exists": True,
        "isdir": stat_mod.S_ISDIR(st.st_mode),
        "islnk": stat_mod.S_ISLNK(st.st_mode),
        "checksum": "",
    }
    if out["islnk"]:
        out["lnk_source"] = os.path.realpath(path)
    if (args.get("checksum") and stat_mod.S_ISREG(st.st_mode)
            and os.access(path, os.R_OK)):
        digest = hashlib.sha1()
        with open(os.path.realpath(path), "rb") as fh:
            block = fh.read(65536)
            while block:
                digest.update(block)
                block = fh.read(65536)
        out["checksum"] = digest.hexdigest()
    return out


def _copy_stage(args):
    """Stage copy/template data in a private temp dir, like the action
    plugin's transfer to the remote tmpdir."""
    import tempfile

    data = base64.b64decode(args.get("data") or "")
    if args.get("path"):
        # copy raw=yes: the transfer itself writes dest.
        with open(args["path"], "wb") as fh:
            fh.write(data)
        return {"path": args["path"]}
    tmpdir = tempfile.mkdtemp(prefix="ansible-tmp-")
    path = os.path.join(tmpdir, args["filename"])
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return {"tmpdir": tmpdir, "path": path}


def _copy_cleanup(args):
    tmpdir = args.get("tmpdir")
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return {"ok": True}


def _copy_read(args):
    """Read a destination for the copy action's diff (the slurp module)."""
    try:
        path = os.path.expanduser(os.path.expandvars(args.get("path") or ""))
        with open(path, "rb") as fh:
            return {"content": base64.b64encode(fh.read()).decode("ascii")}
    except OSError as e:
        return {"failed": True, "msg": str(e)}


_COPY_OPS = {
    "copy_stat": _copy_stat,
    "copy_stage": _copy_stage,
    "copy_cleanup": _copy_cleanup,
    "copy_read": _copy_read,
}


def _drain_pair(r_out, r_err):
    chunks = {r_out: [], r_err: []}
    with selectors.DefaultSelector() as sel:
        sel.register(r_out, selectors.EVENT_READ)
        sel.register(r_err, selectors.EVENT_READ)
        while sel.get_map():
            for key, _ in sel.select():
                fd = key.fd
                chunk = os.read(fd, 65536)
                if chunk:
                    chunks[fd].append(chunk)
                else:
                    sel.unregister(fd)
    return (
        b"".join(chunks[r_out]).decode("utf-8", "surrogateescape"),
        b"".join(chunks[r_err]).decode("utf-8", "surrogateescape"),
    )


def _eof_stdin():
    # Connection plugins hand modules a pipe on stdin and close its write end
    # (local.py: Popen(stdin=PIPE) + communicate()), and run_command children
    # inherit it. Never leak the worker's request pipe into a module.
    r, w = os.pipe()
    os.close(w)
    os.dup2(r, 0)
    os.close(r)
    sys.stdin = sys.__stdin__ = io.open(0, "r", closefd=False)


def _run_exit_handlers():
    # A real AnsiballZ process ends with normal interpreter shutdown: join
    # non-daemon threads, then run atexit callbacks (AnsibleModule.tmpdir
    # registers shutil.rmtree for its ansible-moduletmp-* directory).
    threading = sys.modules.get("threading")
    if threading is not None:
        try:
            threading._shutdown()
        except BaseException:
            pass
    try:
        atexit._run_exitfuncs()
    except BaseException:
        pass


def _run(fqn, params, profile, env=None):
    r_out, w_out = os.pipe()
    try:
        r_err, w_err = os.pipe()
        try:
            pid = os.fork()
        except OSError:
            os.close(r_err)
            os.close(w_err)
            raise
    except OSError:
        os.close(r_out)
        os.close(w_out)
        raise
    if pid == 0:
        os.close(r_out)
        os.close(r_err)
        os.dup2(w_out, 1)
        os.dup2(w_err, 2)
        os.close(w_out)
        os.close(w_err)
        try:
            # Callbacks inherited from the worker belong to the worker.
            atexit._clear()
            logging = sys.modules.get("logging")
            if logging is not None:
                atexit.register(logging.shutdown)
        except BaseException:
            pass
        try:
            _eof_stdin()
            if env:
                os.environ.update({str(k): str(v) for k, v in env.items()})
            from ansible.module_utils import basic

            basic._ANSIBLE_ARGS = params.encode("utf-8")
            try:
                basic._ANSIBLE_PROFILE = profile
            except Exception:
                pass
            runpy.run_module(fqn, run_name="__main__", alter_sys=True)
        except SystemExit:
            pass
        except BaseException as e:
            try:
                sys.stderr.write("ansiroll-worker: %r\n" % (e,))
            except Exception:
                pass
        finally:
            _run_exit_handlers()
            try:
                sys.stdout.flush()
            except Exception:
                pass
            try:
                sys.stderr.flush()
            except Exception:
                pass
            os._exit(0)
    os.close(w_out)
    os.close(w_err)
    try:
        out, err = _drain_pair(r_out, r_err)
    finally:
        os.close(r_out)
        os.close(r_err)
        os.waitpid(pid, 0)
    return {"out": out, "err": err, "rc": 0}


def _filter_non_json_lines(data):
    # Copied from ansible/modules/async_wrapper.py.
    warnings = []
    lines = data.splitlines()
    for start, line in enumerate(lines):
        line = line.strip()
        if line.startswith(u"{"):
            break
    else:
        raise ValueError("No start of json char found")
    lines = lines[start:]
    for reverse_end_offset, line in enumerate(reversed(lines)):
        if line.strip().endswith(u"}"):
            break
    else:
        raise ValueError("No end of json char found")
    if reverse_end_offset > 0:
        trailing_junk = lines[len(lines) - reverse_end_offset:]
        for line in trailing_junk:
            if line.strip():
                warnings.append(
                    "Module invocation had junk after the JSON data: %s"
                    % "\n".join(trailing_junk)
                )
                break
    lines = lines[: (len(lines) - reverse_end_offset)]
    return ("\n".join(lines), warnings)


def _jwrite(job_path, info):
    jobfile = job_path + ".tmp"
    with open(jobfile, "w") as tjob:
        json.dump(info, tjob)
    os.rename(jobfile, job_path)


def _async_module(jid, job_path, fqn, params, profile, source_fd, notify,
                  version):
    """Run one async job and leave its result where async_status reads it.

    Mirrors async_wrapper._run_module: write the started marker, signal the
    launcher, run the module, then replace the marker with the parsed result."""
    modern_flags = version >= (2, 19)
    started = True if modern_flags else 1
    finished = False if modern_flags else 0
    _jwrite(job_path, {"started": started, "finished": finished, "ansible_job_id": jid})
    try:
        os.write(notify, b"1")
    except OSError:
        pass
    os.close(notify)
    outdata = ""
    stderr = ""
    try:
        if source_fd is not None:
            payload = json.loads(params)
            payload["ANSIBLE_MODULE_ARGS"]["src"] = "/proc/%d/fd/%d" % (
                os.getpid(),
                source_fd,
            )
            params = json.dumps(payload)
        response = _run(fqn, params, profile)
        outdata = response["out"]
        stderr = response["err"]
        filtered_outdata, json_warnings = _filter_non_json_lines(outdata)
        result = json.loads(filtered_outdata)
        if json_warnings:
            module_warnings = result.get("warnings", [])
            if not isinstance(module_warnings, list):
                module_warnings = [module_warnings]
            module_warnings.extend(json_warnings)
            result["warnings"] = module_warnings
        if stderr:
            result["stderr"] = stderr
        _jwrite(job_path, result)
    except OSError as e:
        _jwrite(job_path, {
            "failed": started,
            "cmd": fqn,
            "msg": str(e),
            "outdata": outdata,
            "stderr": stderr,
            "ansible_job_id": jid,
        })
    except Exception:
        _jwrite(job_path, {
            "failed": started,
            "cmd": fqn,
            "data": outdata,
            "stderr": stderr,
            "msg": traceback.format_exc(),
            "ansible_job_id": jid,
        })


def _async_timed_out(pid, time_limit, poll_before_timeout):
    step = 5
    remaining = int(time_limit)
    if poll_before_timeout and 0 < remaining <= step:
        step = remaining / 2
    time.sleep(step)
    while os.waitpid(pid, os.WNOHANG) == (0, 0):
        if not poll_before_timeout:
            # The original watchdog charges this interval even if the job
            # exits during it. It checks for exit on the next iteration.
            time.sleep(step)
        remaining -= step
        if remaining <= 0:
            return True
        if poll_before_timeout:
            time.sleep(step)
    return False


def _async_supervise(jid, job_path, time_limit, fqn, params, profile, env,
                     source_fd, notify, version, poll_before_timeout):
    # async_wrapper's daemonized supervisor: detach from the worker's pipes,
    # run the module in its own process group and kill it at the time limit.
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    os.close(devnull)
    os.close(_RESP)
    # Re-evaluate the temp location in the task environment. Only remove
    # this private directory, even if multiple jobs share the temp parent.
    os.environ.update({str(k): str(v) for k, v in env.items()})
    tempfile.tempdir = None
    tmpdir = tempfile.mkdtemp(prefix="ansiroll-async-")
    try:
        payload = json.loads(params)
        payload["ANSIBLE_MODULE_ARGS"]["_ansible_tmpdir"] = tmpdir
        params = json.dumps(payload)
        sub_pid = os.fork()
        if sub_pid == 0:
            try:
                os.setpgid(0, 0)
            except OSError:
                pass
            try:
                _async_module(jid, job_path, fqn, params, profile, source_fd, notify,
                              version)
            finally:
                os._exit(0)
        os.close(notify)
        if source_fd is not None:
            os.close(source_fd)
        try:
            os.setpgid(sub_pid, sub_pid)
        except OSError:
            pass
        timed_out = _async_timed_out(sub_pid, time_limit, poll_before_timeout)
        if timed_out:
            # Reap the writer before replacing its result. Otherwise a
            # concurrent write can overwrite the timeout or its temp file.
            try:
                os.killpg(sub_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(sub_pid, 0)
        if timed_out and version >= (2, 12):
            if poll_before_timeout:
                # This watchdog publishes the timeout one second after killing.
                time.sleep(1)
            _jwrite(job_path, {
                "msg": "Timeout exceeded", "failed": True, "child_pid": sub_pid,
            })
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _async_run(fqn, params, profile, env, time_limit, source_fd, version,
               poll_before_timeout):
    async_dir = env.get("ANSIBLE_ASYNC_DIR", "~/.ansible_async")
    if "HOME" in env and (async_dir == "~" or async_dir.startswith("~/")):
        jobdir = env["HOME"].rstrip("/") + async_dir[1:]
    else:
        jobdir = os.path.expanduser(async_dir)
    modern_flags = version >= (2, 19)
    try:
        os.makedirs(jobdir, exist_ok=True)
    except OSError as e:
        return {
            "failed": True if modern_flags else 1,
            "msg": "%s: %s - %s" % (
                "could not create directory" if version >= (2, 12) else "could not create",
                jobdir, e,
            ),
            "exception": traceback.format_exc(),
        }
    if version >= (2, 18):
        random_id = random.SystemRandom().randrange(999999999999)
    else:
        random_id = random.randint(0, 999999999999)
    prefix = ("j" if version >= (2, 14, 5) else "") + str(random_id)
    r, w = os.pipe()
    try:
        pid = os.fork()
    except OSError:
        os.close(r)
        os.close(w)
        raise
    if pid == 0:
        try:
            os.close(r)
            jid = "%s.%d" % (prefix, os.getpid())
            job_path = os.path.join(jobdir, jid)
            os.setsid()
            os.umask(0o022)
            # Daemonize: the worker reaps this process immediately and the
            # supervisor is re-parented, so finished jobs never linger as
            # zombies of the worker.
            if os.fork() == 0:
                try:
                    _async_supervise(jid, job_path, time_limit, fqn, params,
                                     profile, env, source_fd, w, version,
                                     poll_before_timeout)
                finally:
                    os._exit(0)
        finally:
            os._exit(0)
    os.close(w)
    try:
        os.waitpid(pid, 0)
        # async_wrapper waits up to 2.5s for the job to report that it started.
        with selectors.DefaultSelector() as sel:
            sel.register(r, selectors.EVENT_READ)
            if sel.select(2.5) and not os.read(r, 1):
                raise RuntimeError("async job exited before writing its start marker")
    finally:
        os.close(r)
    jid = "%s.%d" % (prefix, pid)
    ack = {
        "started": True if modern_flags else 1,
        "finished": False if modern_flags else 0,
        "ansible_job_id": jid,
        "results_file": os.path.join(jobdir, jid),
    }
    if version >= (2, 12):
        ack["failed"] = False if modern_flags else 0
    return ack


def main():
    while True:
        req = _recv()
        if req is None or req.get("op") == "quit":
            break
        op = req.get("op")
        if op == "bundle":
            try:
                bundle_bytes = _readn(req["size"])
                if bundle_bytes is None:
                    raise EOFError("worker bundle bytes ended early")
                _load_zip_bytes(bundle_bytes)
                _install_legacy_finder_adapters()
                try:
                    __import__("ansible.module_utils.basic")
                except Exception:
                    pass
                _send({"ok": True})
            except Exception as e:
                _send({"ok": False, "error": repr(e)})
        elif op == "load":
            try:
                if req.get("zip"):
                    _load_zip_base64(req["zip"])
                _install_legacy_finder_adapters()
                try:
                    __import__("ansible.module_utils.basic")  # precompile once
                except Exception:
                    pass
                _send({"ok": True})
            except Exception as e:
                _send({"ok": False, "error": repr(e)})
        elif op == "run":
            source_fd = None
            try:
                source = (
                    _readn(req["source_size"])
                    if req.get("source_size") is not None
                    else None
                )
                if req.get("source_size") is not None and source is None:
                    raise EOFError("worker module source ended early")
                params, source_fd = _memfd_source(req["params"], source)
                _send(_run(req["fqn"], params, req["profile"], req.get("env")))
            except Exception as e:
                _send({"out": "", "err": repr(e), "rc": 1})
            finally:
                if source_fd is not None:
                    os.close(source_fd)
        elif op == "async_run":
            source_fd = None
            try:
                source = (
                    _readn(req["source_size"])
                    if req.get("source_size") is not None
                    else None
                )
                if req.get("source_size") is not None and source is None:
                    raise EOFError("worker module source ended early")
                params, source_fd = _memfd_source(req["params"], source)
                response = _async_run(
                    req["fqn"],
                    params,
                    req["profile"],
                    req.get("env") or {},
                    req.get("time_limit") or 0,
                    source_fd,
                    tuple(req.get("version", (2, 21))),
                    req.get("poll_before_timeout", False),
                )
                _send(response)
            except Exception as e:
                _send({"error": repr(e)})
            finally:
                if source_fd is not None:
                    os.close(source_fd)
        elif op in _COPY_OPS:
            try:
                args = req.get("args") or {}
                if op in ("copy_stat", "copy_read"):
                    from ansiroll.accelerated_modules.environment import module_environment

                    with module_environment(req.get("env")):
                        result = _COPY_OPS[op](args)
                else:
                    result = _COPY_OPS[op](args)
                _send(result)
            except Exception as e:
                _send({"failed": True, "msg": str(e)})
        elif op == "accelerated":
            try:
                from ansiroll.accelerated_modules import run as run_accelerated

                _send(
                    run_accelerated(
                        req.get("name"),
                        req.get("args") or {},
                        bool(req.get("check_mode")),
                        req.get("env"),
                        req.get("options") or {},
                    )
                )
            except Exception as e:
                _send(
                    {
                        "failed": True,
                        "changed": False,
                        "msg": "accelerated module failed: %s" % e,
                    }
                )
        else:
            _send({"error": "unknown op"})


if __name__ == "__main__":
    main()
