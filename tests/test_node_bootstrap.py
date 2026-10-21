"""Bootstrap tests never install dependencies or start a real Codex node."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "node_bootstrap", REPOSITORY / "scripts" / "node_bootstrap.py"
)
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project with spaces and unicode 会话"
    root.mkdir()
    (root / "requirements.lock").write_text("example==1.0\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")
    return root


def make_python(root):
    python = bootstrap.environment_python(root)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b"fake interpreter; never executed")
    return python


def good_probe(python, root, *, dependencies=False):
    return {"version": [3, 12, 9], "missing": []} if Path(python).is_file() else None


def cache_environment(root):
    make_python(root)
    (root / ".venv" / bootstrap.MANIFEST).write_text(
        json.dumps({"fingerprint": bootstrap.fingerprint(root), "python": [3, 12, 9]}),
        encoding="utf-8",
    )


def test_matching_environment_starts_without_running_pip(project, monkeypatch):
    cache_environment(project)
    monkeypatch.setattr(bootstrap, "probe_python", good_probe)
    calls = []
    monkeypatch.setattr(bootstrap, "run_foreground", lambda command, root: calls.append(command) or 0)
    assert bootstrap.launch(project, ["--no-open"]) == 0
    assert calls == [[
        str(bootstrap.environment_python(project)), "-m", "codex_anywhere.cli",
        "node", "web", "--no-open",
    ]]


def test_initial_environment_installs_lock_then_editable_package(project, monkeypatch):
    monkeypatch.setattr(bootstrap, "probe_python", good_probe)
    calls = []

    def run(command, root):
        calls.append(command)
        if command[1:3] == ["-m", "venv"]:
            make_python(root)
        return 0

    monkeypatch.setattr(bootstrap, "run_foreground", run)
    result = bootstrap.prepare_environment(project)
    assert result == bootstrap.environment_python(project)
    assert calls[0] == [sys.executable, "-m", "venv", str(project / ".venv")]
    assert calls[1] == [str(result), "-m", "pip", "install", "-r", str(project / "requirements.lock")]
    assert calls[2] == [str(result), "-m", "pip", "install", "--no-deps", "-e", str(project)]
    assert json.loads((project / ".venv" / bootstrap.MANIFEST).read_text())["fingerprint"] == bootstrap.fingerprint(project)


def test_changed_lock_invalidates_cache(project, monkeypatch):
    cache_environment(project)
    (project / "requirements.lock").write_text("example==2.0\n")
    monkeypatch.setattr(bootstrap, "probe_python", good_probe)
    calls = []
    monkeypatch.setattr(bootstrap, "run_foreground", lambda command, root: calls.append(command) or 0)
    bootstrap.prepare_environment(project)
    assert len(calls) == 2
    assert calls[0][2:5] == ["pip", "install", "-r"]


def test_broken_dependencies_are_repaired_even_with_matching_cache(project, monkeypatch):
    cache_environment(project)
    checks = 0

    def probe(python, root, *, dependencies=False):
        nonlocal checks
        if dependencies:
            checks += 1
        return {"version": [3, 12, 9], "missing": ["psutil"] if checks == 1 else []}

    calls = []
    monkeypatch.setattr(bootstrap, "probe_python", probe)
    monkeypatch.setattr(bootstrap, "run_foreground", lambda command, root: calls.append(command) or 0)
    bootstrap.prepare_environment(project)
    assert len(calls) == 2
    assert checks == 2


@pytest.mark.parametrize("failed_install", [1, 2])
def test_failed_install_never_starts_service_or_writes_success_manifest(
    project, monkeypatch, capsys, failed_install
):
    make_python(project)
    monkeypatch.setattr(bootstrap, "probe_python", good_probe)
    calls = []

    def run(command, root):
        calls.append(command)
        return 1 if len(calls) == failed_install else 0

    monkeypatch.setattr(bootstrap, "run_foreground", run)
    with pytest.raises(bootstrap.BootstrapError, match="was not started"):
        bootstrap.launch(project, ["--no-open"])
    assert len(calls) == failed_install
    assert not (project / ".venv" / bootstrap.MANIFEST).exists()
    assert "Starting the local" not in capsys.readouterr().out


def test_failed_repair_removes_previous_success_marker(project, monkeypatch):
    cache_environment(project)
    monkeypatch.setattr(bootstrap, "probe_python", lambda *_args, **_kwargs: {
        "version": [3, 12, 9], "missing": ["psutil"],
    })
    monkeypatch.setattr(bootstrap, "run_foreground", lambda *_: 1)
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.prepare_environment(project)
    assert not (project / ".venv" / bootstrap.MANIFEST).exists()


def test_missing_venv_support_is_actionable(project, monkeypatch):
    monkeypatch.setattr(bootstrap, "run_foreground", lambda *_: 1)
    with pytest.raises(bootstrap.BootstrapError, match="python3-venv"):
        bootstrap.prepare_environment(project)
    assert not (project / ".venv" / bootstrap.MANIFEST).exists()


def test_help_without_dependencies_never_creates_environment_or_uses_network(project, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "probe_python", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bootstrap, "run_foreground", lambda *_: pytest.fail("No process should start"))
    assert bootstrap.launch(project, ["--help"]) == 0
    assert not (project / ".venv").exists()
    assert "--state-dir" in capsys.readouterr().out


def test_help_with_dependencies_skips_install_even_when_fingerprint_changed(project, monkeypatch):
    make_python(project)
    monkeypatch.setattr(bootstrap, "probe_python", good_probe)
    calls = []
    monkeypatch.setattr(bootstrap, "run_foreground", lambda command, root: calls.append(command) or 0)
    assert bootstrap.launch(project, ["--help"]) == 0
    assert calls[0][-3:] == ["node", "web", "--help"]
    assert len(calls) == 1
    assert not (project / ".venv" / bootstrap.MANIFEST).exists()


def test_arguments_remain_separate_and_literal(project, monkeypatch):
    cache_environment(project)
    monkeypatch.setattr(bootstrap, "probe_python", good_probe)
    arguments = ["--port", "5781", "--state-dir", "会话 with spaces & $(not-code);'", "--no-open"]
    calls = []
    monkeypatch.setattr(bootstrap, "run_foreground", lambda command, root: calls.append((command, root)) or 0)
    bootstrap.launch(project, arguments)
    assert calls[0][0][5:] == arguments
    assert calls[0][1] == project.resolve()


def test_ctrl_c_waits_for_child_to_exit_instead_of_abandoning_it(project, monkeypatch):
    class Child:
        waits = 0

        def wait(self):
            self.waits += 1
            if self.waits == 1:
                raise KeyboardInterrupt
            return 0

    child = Child()
    monkeypatch.setattr(bootstrap.subprocess, "Popen", lambda *_args, **_kwargs: child)
    assert bootstrap.run_foreground(["fake-cli"], project) == 130
    assert child.waits == 2


def test_real_interpreter_probe_has_no_install_side_effects(project):
    value = bootstrap.probe_python(Path(sys.executable), project)
    assert value["version"] == list(sys.version_info[:3])
    assert value["missing"] == []
    assert not (project / ".venv").exists()


def test_environment_is_rechecked_after_acquiring_bootstrap_lock(project, monkeypatch):
    calls = []
    locked = False

    @contextmanager
    def delayed_lock(root):
        nonlocal locked
        # Simulate another launch completing installation while we waited.
        cache_environment(root)
        locked = True
        yield
        locked = False

    def probe(*args, **kwargs):
        assert locked
        return good_probe(*args, **kwargs)

    monkeypatch.setattr(bootstrap, "bootstrap_lock", delayed_lock)
    monkeypatch.setattr(bootstrap, "probe_python", probe)
    monkeypatch.setattr(bootstrap, "run_foreground", lambda command, root: calls.append(command) or 0)
    assert bootstrap.prepare_environment(project) == bootstrap.environment_python(project)
    assert calls == []


def test_real_subprocess_bootstrap_lock_excludes_duplicate_and_releases(project, capsys):
    code = (
        "import importlib.util,pathlib,sys; "
        "spec=importlib.util.spec_from_file_location('bootstrap',sys.argv[1]); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "guard=module.bootstrap_lock(pathlib.Path(sys.argv[2]),timeout=2); "
        "guard.__enter__(); print('locked',flush=True); sys.stdin.read(); guard.__exit__(None,None,None)"
    )
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", code, str(REPOSITORY / "scripts" / "node_bootstrap.py"), str(project)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        started = time.monotonic()
        with pytest.raises(bootstrap.BootstrapError, match="was not started"):
            with bootstrap.bootstrap_lock(project, timeout=0.05):
                pytest.fail("The second launcher must not enter environment setup")
        assert time.monotonic() - started < 2
        assert "Another launch is preparing" in capsys.readouterr().out
        process.stdin.close()
        process.wait(timeout=5)
        with bootstrap.bootstrap_lock(project, timeout=0.1):
            assert bootstrap._bootstrap_lock_path(project).is_file()
    finally:
        if process.poll() is None:
            process.stdin.close()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def test_help_never_takes_bootstrap_lock(project, monkeypatch):
    monkeypatch.setattr(bootstrap, "bootstrap_lock", lambda *_: pytest.fail("Help must not lock"))
    monkeypatch.setattr(bootstrap, "probe_python", lambda *_args, **_kwargs: None)
    assert bootstrap.launch(project, ["--help"]) == 0


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PowerShell")
def test_windows_launcher_parses_and_forwards_literal_arguments_from_other_cwd(project, tmp_path):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell is not installed")
    launcher = project / "start-node.ps1"
    shutil.copyfile(REPOSITORY / "start-node.ps1", launcher)
    scripts = project / "scripts"
    scripts.mkdir()
    (scripts / "node_bootstrap.py").write_text(
        "import json, pathlib, sys; print(json.dumps({'script': str(pathlib.Path(__file__).resolve()), 'args': sys.argv[1:]}))",
        encoding="utf-8",
    )
    environment = {**os.environ, "NODE_LAUNCH_SCRIPT": str(launcher)}
    syntax = subprocess.run(
        [powershell, "-NoProfile", "-Command",
         "$tokens=$null; $errors=$null; $null=[System.Management.Automation.Language.Parser]::ParseFile($env:NODE_LAUNCH_SCRIPT,[ref]$tokens,[ref]$errors); if($errors){$errors | Out-String | Write-Error; exit 1}"],
        env=environment, capture_output=True, text=True, timeout=20,
    )
    assert syntax.returncode == 0, syntax.stderr
    arguments = ["--port", "5781", "--state-dir", "中文 space & $(not-code);'", "--no-open"]
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(launcher), *arguments],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert Path(value["script"]) == scripts / "node_bootstrap.py"
    assert value["args"] == arguments


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows cmd and PowerShell")
def test_windows_double_click_entry_invokes_sibling_script(project, tmp_path):
    if not shutil.which("powershell.exe"):
        pytest.skip("Windows PowerShell is not installed")
    for name in ("start-node.cmd", "start-node.ps1"):
        shutil.copyfile(REPOSITORY / name, project / name)
    scripts = project / "scripts"
    scripts.mkdir()
    (scripts / "node_bootstrap.py").write_text(
        "import json, sys; print(json.dumps(sys.argv[1:]))\n", encoding="utf-8",
    )
    result = subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "start-node.cmd", "--help"],
        cwd=project, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["--help"]


def _bash():
    if os.name == "nt":
        for path in (
            Path("C:/Program Files/Git/bin/bash.exe"),
            Path("C:/Program Files/Git/usr/bin/bash.exe"),
        ):
            if path.is_file():
                return str(path)
        return None
    return shutil.which("bash")


def test_shell_launcher_syntax_and_mocked_argument_forwarding(project, tmp_path):
    bash = _bash()
    if not bash:
        pytest.skip("Bash is unavailable; Linux launcher was not exercised")
    launcher = project / "start-node.sh"
    shutil.copyfile(REPOSITORY / "start-node.sh", launcher)
    syntax = subprocess.run([bash, "-n", str(launcher)], capture_output=True, text=True, timeout=20)
    assert syntax.returncode == 0, syntax.stderr
    scripts = project / "scripts"
    scripts.mkdir()
    (scripts / "node_bootstrap.py").write_text("# fake; mock interpreter does not execute it\n")
    binary = project / "mock-bin"
    binary.mkdir()
    python = binary / "python3"
    python.write_text(
        '#!/usr/bin/env bash\nif [ "$1" = "-c" ]; then exit 0; fi\nprintf "%s\\n" "$@" > "$NODE_LAUNCH_CAPTURE"\n',
        encoding="utf-8", newline="\n",
    )
    python.chmod(0o755)
    capture = project / "captured.txt"
    environment = {
        **os.environ, "NODE_LAUNCH_SCRIPT": str(launcher), "NODE_MOCK_BIN": str(binary),
        "NODE_LAUNCH_CAPTURE": str(capture),
    }
    command = (
        'export PATH="$(cygpath -u "$NODE_MOCK_BIN"):$PATH"; exec bash "$NODE_LAUNCH_SCRIPT" "$@"'
        if os.name == "nt" else
        'export PATH="$NODE_MOCK_BIN:$PATH"; exec bash "$NODE_LAUNCH_SCRIPT" "$@"'
    )
    arguments = ["--port", "5782", "--state-dir", "中文 space & literal", "--no-open"]
    result = subprocess.run(
        [bash, "-c", command, "node-launcher-test", *arguments], cwd=tmp_path,
        env=environment, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    forwarded = capture.read_text(encoding="utf-8").splitlines()
    assert forwarded[0].replace("\\", "/").endswith("/scripts/node_bootstrap.py")
    assert forwarded[1:] == arguments
