import json
import stat
from argparse import Namespace

from ansiroll import cli


def test_compile_writes_source_to_stdout(monkeypatch, tmp_path, capsys):
    source = "print('ok')\n"
    manifest = {"modules": [], "fallback_tasks": []}

    def compile_playbook(*args, **kwargs):
        print("compiler diagnostic")
        return source, manifest

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "compile_playbook", compile_playbook)

    assert cli.main(["compile", "play.yml", "-o", "-", "-m", "manifest.json"]) == 0

    captured = capsys.readouterr()
    assert captured.out == source
    assert "compiler diagnostic\n" in captured.err
    assert "ansiroll: compiled play.yml -> stdout" in captured.err
    assert sorted(path.name for path in tmp_path.iterdir()) == ["manifest.json"]
    assert json.loads((tmp_path / "manifest.json").read_text()) == manifest


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
