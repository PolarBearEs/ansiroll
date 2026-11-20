import json
import os
import stat
import subprocess
import sys
from argparse import Namespace

import pytest

from ansiroll import cli


def test_compile_writes_source_to_stdout(monkeypatch, tmp_path, capfdbinary):
    source = "print('olá')\n"
    manifest = {"modules": [], "fallback_tasks": []}

    def compile_playbook(*args, **kwargs):
        print("compiler diagnostic")
        subprocess.run([sys.executable, "-c", "print('helper diagnostic')"], check=True)
        return source, manifest

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "compile_playbook", compile_playbook)

    assert cli.main(["compile", "play.yml", "-o", "-", "-m", "manifest.json"]) == 0

    captured = capfdbinary.readouterr()
    assert captured.out == source.encode("utf-8")
    assert b"compiler diagnostic\n" in captured.err
    assert b"helper diagnostic\n" in captured.err
    assert b"ansiroll: compiled play.yml -> stdout" in captured.err
    assert sorted(path.name for path in tmp_path.iterdir()) == ["manifest.json"]
    assert json.loads((tmp_path / "manifest.json").read_text()) == manifest


def test_compile_stdout_is_utf8_with_a_different_stdout_encoding(tmp_path):
    pytest.importorskip("ansible")
    playbook = tmp_path / "play.yml"
    playbook.write_text("- hosts: localhost\n  gather_facts: false\n  tasks: []\n")

    result = subprocess.run(
        [sys.executable, "-m", "ansiroll.cli", "compile", str(playbook), "-o", "-"],
        env=dict(os.environ, PYTHONIOENCODING="utf-16le"),
        capture_output=True,
        check=True,
    )

    assert result.stdout.startswith(b"#!")
    compile(result.stdout, "<stdout>", "exec")


def test_compile_stdout_stays_empty_if_manifest_write_fails(monkeypatch, tmp_path, capfdbinary):
    manifest = {"modules": [], "fallback_tasks": []}
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("print('ok')\n", manifest))

    with pytest.raises(IsADirectoryError):
        cli.main(["compile", "play.yml", "-o", "-", "-m", str(tmp_path)])

    assert capfdbinary.readouterr().out == b""


def test_compile_writes_executable_file(monkeypatch, tmp_path, capsys):
    source = "print('ok')\n"
    manifest = {"modules": [], "fallback_tasks": []}
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: (source, manifest))
    output = tmp_path / "compiled.py"
    output.write_text("old source\n")
    output.chmod(0o640)

    assert cli.main(["compile", "play.yml", "-o", str(output)]) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ansiroll: compiled play.yml -> %s" % output in captured.err
    assert output.read_text() == source
    assert stat.S_IMODE(output.stat().st_mode) == 0o750


def test_run_streams_compiled_source_without_creating_a_file(monkeypatch):
    monkeypatch.setattr(cli, "compile_playbook", lambda *a, **k: ("print('ok')\n", {}))
    monkeypatch.setattr(
        cli.tempfile, "mkstemp",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected temp file")),
    )
    called = {}

    def fake_run(command, **kwargs):
        called["command"] = command
        called.update(kwargs)
        return Namespace(returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    args = Namespace(playbook="play.yml", interpreter="python3", keep=False,
                     vault_password_file=None)

    assert cli._cmd_run(args) == 0
    assert called == {
        "command": ["python3", "-"],
        "input": "print('ok')\n",
        "universal_newlines": True,
    }
