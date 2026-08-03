"""Persistent stdlib-only module worker embedded into compiled agents."""

import base64
import ctypes
import datetime
import importlib.abc
import importlib.util
import io
import json
import os
import runpy
import selectors
import shutil
import struct
import sys
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


def _send(obj):
    if obj is None:
        obj = {"_ansiroll_fallback": True}
    b = json.dumps(obj).encode("utf-8")
    os.write(_RESP, struct.pack(">I", len(b)))
    os.write(_RESP, b)


def _load_zip(zip_text, encoding="base64"):
    data = (
        base64.b85decode(zip_text)
        if encoding == "base85"
        else base64.b64decode(zip_text)
    )
    _load_zip_bytes(data)


def _load_zip_bytes(data):
    sys.meta_path.insert(0, _MemZip(data))


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


def _norm_mode(mode):
    if mode is None:
        return None
    if isinstance(mode, int):
        return "0%o" % mode
    return str(mode)


def _chmod_chown(path, mode, owner, group):
    try:
        if mode:
            os.chmod(path, int(mode, 8))
    except Exception:
        pass
    if owner or group:
        try:
            shutil.chown(path, user=owner, group=group)
        except Exception:
            pass


def _fast_write(args, check_mode):
    dest = args.get("dest")
    content = args.get("content", "")
    if not dest:
        return {"failed": True, "changed": False, "msg": "write: dest is required"}
    mode = _norm_mode(args.get("mode"))
    owner = args.get("owner")
    group = args.get("group")
    try:
        old = None
        if os.path.exists(dest):
            try:
                with open(dest, "r", encoding="utf-8", errors="surrogateescape") as fh:
                    old = fh.read()
            except OSError:
                old = None
        changed = old != content
        if changed and not check_mode:
            parent = os.path.dirname(dest)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(dest, "w", encoding="utf-8", errors="surrogateescape") as fh:
                fh.write(content)
            _chmod_chown(dest, mode, owner, group)
        return {"changed": changed, "dest": dest}
    except Exception as e:
        return {"failed": True, "changed": False, "dest": dest, "msg": str(e)}


def _drain_pair(r_out, r_err):
    chunks = {r_out: [], r_err: []}
    sel = selectors.DefaultSelector()
    sel.register(r_out, selectors.EVENT_READ)
    sel.register(r_err, selectors.EVENT_READ)
    while sel.get_map():
        for key, _ in sel.select():
            fd = key.fd
            try:
                c = os.read(fd, 65536)
            except OSError:
                c = b""
            if c:
                chunks[fd].append(c)
            else:
                try:
                    sel.unregister(fd)
                except Exception:
                    pass
    return (
        b"".join(chunks[r_out]).decode("utf-8", "surrogateescape"),
        b"".join(chunks[r_err]).decode("utf-8", "surrogateescape"),
    )


def _run(fqn, params, profile, env=None):
    r_out, w_out = os.pipe()
    r_err, w_err = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r_out)
        os.close(r_err)
        os.dup2(w_out, 1)
        os.dup2(w_err, 2)
        os.close(w_out)
        os.close(w_err)
        try:
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
                sys.stderr.write("ansiroll-worker: %r\\n" % (e,))
            except Exception:
                pass
        finally:
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
    out, err = _drain_pair(r_out, r_err)
    os.close(r_out)
    os.close(r_err)
    os.waitpid(pid, 0)
    return {"out": out, "err": err, "rc": 0}


def _async_run(fqn, params, profile, source_fd=None):
    pid = os.fork()
    if pid == 0:
        try:
            if source_fd is not None:
                payload = json.loads(params)
                payload["ANSIBLE_MODULE_ARGS"]["src"] = "/proc/%d/fd/%d" % (
                    os.getpid(),
                    source_fd,
                )
                params = json.dumps(payload)
            _run(fqn, params, profile)
        except BaseException:
            pass
        finally:
            if source_fd is not None:
                try:
                    os.close(source_fd)
                except OSError:
                    pass
            os._exit(0)
    return {
        "pid": pid,
        "jid": "%s.%s" % (int(datetime.datetime.utcnow().timestamp()), pid),
    }


def main():
    while True:
        req = _recv()
        if req is None or req.get("op") == "quit":
            break
        op = req.get("op")
        if op == "bundle":
            try:
                _load_zip(req["bundle"], "base85")
                _install_legacy_finder_adapters()
                try:
                    __import__("ansible.module_utils.basic")
                except Exception:
                    pass
                _send({"ok": True})
            except Exception as e:
                _send({"ok": False, "error": repr(e)})
        elif op == "bundle_bytes":
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
                    _load_zip(req["zip"])
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
                response = _async_run(req["fqn"], params, req["profile"], source_fd)
                _send(response)
            except Exception as e:
                _send({"error": repr(e)})
            finally:
                if source_fd is not None:
                    os.close(source_fd)
        elif op == "fast_write":
            _send(_fast_write(req.get("args") or {}, bool(req.get("check_mode"))))
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
