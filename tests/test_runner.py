import os
import subprocess
import sys
import threading
import time
import tracemalloc

import pytest

from setuppilot.config import RunnerConfig
from setuppilot.errors import CommandBlocked, CommandTimeout, InstallFailed, PrereqMissing
from setuppilot.guardrails import process_tree
from setuppilot.guardrails.commands import SafeCommand
from setuppilot.guardrails.runner import child_env, find_program, launch_args, run_command
from tests.fakes import fake_executable

CONFIG = RunnerConfig(timeout_seconds=20, max_output_chars=1000)
TARGETS = ["posix", "windows"]
ON_WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="needs real Windows")
ON_POSIX = pytest.mark.skipif(sys.platform == "win32", reason="Windows has no exec bit or #!")
WINDOWS_ENV = {"SYSTEMROOT": r"C:\Windows", "PATH": r"C:\Program Files\nodejs"}
NPM_CMD = r"C:\Program Files\nodejs\npm.cmd"


def no_op(tree):
    return None


def python(tmp_path, code):
    """A command that runs a tiny Python script. Tests may build SafeCommand directly."""
    return SafeCommand((sys.executable, "-c", code), tmp_path, f"python -c {code!r}", None)


def run_in_background(command, config=CONFIG):
    """Starts run_command in a thread. Returns the thread, the trees it reported and its result."""
    trees, result = [], {}

    def work():
        result["out"] = run_command(command, config, on_start=trees.append)

    worker = threading.Thread(target=work)
    worker.start()
    while not trees:
        time.sleep(0.05)
    return worker, trees, result


def test_exit_code_and_output_are_returned(tmp_path):
    out = run_command(python(tmp_path, "print('hi'); raise SystemExit(3)"), CONFIG, on_start=no_op)
    assert (out.exit_code, out.output_tail.strip(), out.is_output_cut) == (3, "hi", False)


def test_command_runs_in_the_workspace(tmp_path):
    out = run_command(python(tmp_path, "import os; print(os.getcwd())"), CONFIG, on_start=no_op)
    assert os.path.realpath(out.output_tail.strip()) == os.path.realpath(tmp_path)


def test_stderr_is_kept_with_stdout(tmp_path):
    out = run_command(
        python(tmp_path, "import sys; sys.stderr.write('oops')"), CONFIG, on_start=no_op
    )
    assert "oops" in out.output_tail


def test_huge_output_keeps_only_the_end(tmp_path):
    code = "import sys\nfor i in range(200000): sys.stdout.write(f'line {i}\\n')"
    out = run_command(python(tmp_path, code), CONFIG, on_start=no_op)
    assert out.is_output_cut and len(out.output_tail) <= CONFIG.max_output_chars
    assert out.output_tail.rstrip().endswith("line 199999")


def test_huge_output_does_not_fill_memory(tmp_path):
    tracemalloc.start()
    try:
        run_command(python(tmp_path, "print('x' * 10_000_000)"), CONFIG, on_start=no_op)
        peak_bytes = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    # The output is 10 MB; keeping only the end measured about 70 KB at the peak.
    assert peak_bytes < 1_000_000


def test_hanging_command_is_stopped_at_the_time_limit(tmp_path):
    config = RunnerConfig(timeout_seconds=1, max_output_chars=1000)
    started = time.monotonic()
    with pytest.raises(CommandTimeout, match="longer than 1 seconds"):
        run_command(
            python(tmp_path, "import time; print('start', flush=True); time.sleep(60)"),
            config,
            on_start=no_op,
        )
    assert time.monotonic() - started < 15


def test_child_processes_are_stopped_with_the_command(tmp_path):
    marker = tmp_path / "child-alive"
    child = f"import time; time.sleep(3); open({str(marker)!r}, 'w')"
    start_child = f"subprocess.Popen([sys.executable, '-c', {child!r}])"
    code = f"import subprocess, sys, time\n{start_child}\ntime.sleep(60)"
    with pytest.raises(CommandTimeout):
        run_command(
            python(tmp_path, code),
            RunnerConfig(timeout_seconds=1, max_output_chars=100),
            on_start=no_op,
        )
    time.sleep(4)
    assert not marker.exists()


def test_secrets_are_not_passed_to_the_command(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secretsecretsecretsecret")
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://u:p@h/db")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_x")
    names = "('GROQ', 'SUPABASE', 'LANGSMITH')"
    code = f"import os; print(sorted(k for k in os.environ if k.startswith({names})))"
    out = run_command(python(tmp_path, code), CONFIG, on_start=no_op)
    assert out.output_tail.strip() == "[]"


def test_secret_printed_by_a_command_is_hidden(tmp_path, monkeypatch):
    out = run_command(
        python(tmp_path, "print('token gsk_abcdefghijklmnopqrstuvwxyz12')"), CONFIG, on_start=no_op
    )
    assert "gsk_" not in out.output_tail


def test_command_gets_no_input(tmp_path):
    # pytest already points fd 0 at /dev/null, so give it real input that must not get through.
    read_end, write_end = os.pipe()
    os.write(write_end, b"typed input")
    os.close(write_end)
    saved_stdin = os.dup(0)
    os.dup2(read_end, 0)
    try:
        out = run_command(
            python(tmp_path, "import sys; print(repr(sys.stdin.read()))"), CONFIG, on_start=no_op
        )
    finally:
        os.dup2(saved_stdin, 0)
        os.close(saved_stdin)
        os.close(read_end)
    assert out.output_tail.strip() == "''"


def test_missing_program_is_a_prerequisite_error(tmp_path):
    command = SafeCommand(
        ("no-such-tool-xyz", "install"), tmp_path, "no-such-tool-xyz install", None
    )
    with pytest.raises(PrereqMissing, match="not installed"):
        run_command(command, CONFIG, on_start=no_op)


def test_a_secret_typed_into_a_command_is_hidden_in_its_error(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_typedintoanedit123456")
    text = "no-such-tool-xyz install gsk_typedintoanedit123456"
    command = SafeCommand(tuple(text.split()), tmp_path, text, None)
    with pytest.raises(PrereqMissing) as raised:
        run_command(command, CONFIG, on_start=no_op)
    assert "gsk_typedintoanedit123456" not in raised.value.details


@pytest.mark.parametrize("target", TARGETS)
def test_child_env_turns_off_prompts(target):
    env = child_env({}, target)
    assert (env["GIT_TERMINAL_PROMPT"], env["PIP_NO_INPUT"]) == ("0", "1")


@pytest.mark.parametrize("target", TARGETS)
def test_child_env_keeps_project_environments_in_the_repo(target):
    in_repo = {
        "POETRY_VIRTUALENVS_IN_PROJECT": "true",
        "PIPENV_VENV_IN_PROJECT": "1",
        "BUNDLE_PATH": "vendor/bundle",
    }
    assert child_env({}, target).items() >= in_repo.items()


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("name", ["DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"])
def test_docker_engine_setting_reaches_the_command(name, target):
    assert child_env({name: "from-your-shell"}, target).get(name) == "from-your-shell"


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize(
    "name",
    ["VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN",
     "OPENAI_API_KEY", "SSH_AUTH_SOCK", "NPM_TOKEN"],
)  # fmt: skip
def test_only_safe_environment_reaches_the_command(name, target):
    assert name not in child_env({name: "something-private"}, target)


@pytest.mark.parametrize(
    ("target", "path", "kept"),
    [("posix", "node_modules/.bin:/usr/bin:.", "/usr/bin"),
     ("windows", r"node_modules\.bin;C:\Windows;\Windows;.;C:\Tools", r"C:\Windows;C:\Tools")],
)  # fmt: skip
def test_relative_path_entries_are_dropped(target, path, kept):
    assert child_env({"PATH": path}, target)["PATH"] == kept


@pytest.mark.parametrize(
    "name", ["SYSTEMROOT", "PATHEXT", "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE"]
)
def test_windows_command_gets_the_system_folders_it_needs(name):
    assert child_env({name: r"C:\somewhere"}, "windows").get(name) == r"C:\somewhere"


def test_cmd_exe_in_a_windows_command_does_not_look_in_the_repo_folder_first():
    assert child_env({}, "windows")["NoDefaultCurrentDirectoryInExePath"] == "1"


def test_git_in_a_windows_command_never_opens_a_sign_in_window():
    assert child_env({}, "windows")["GCM_INTERACTIVE"] == "never"


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("name", ["http_proxy", "HTTP_PROXY", "Https_Proxy"])
def test_proxy_setting_reaches_the_command_whatever_its_case(name, target):
    assert child_env({name: "http://proxy:3128"}, target)[name] == "http://proxy:3128"


def test_batch_file_runs_through_cmd_exe_without_autorun():
    line, executable = launch_args(NPM_CMD, ["ci"], WINDOWS_ENV, "windows")
    assert executable == r"C:\Windows\System32\cmd.exe"
    assert line == rf'"{executable}" /d /s /c ""{NPM_CMD}" ci"'


@pytest.mark.parametrize(
    "arg", ["pkg@^1.2", "%PATH%", "a&b", "a|b", "a<b", "a>b", 'a"b', "!x!", "(a)", "a b", "~x", ""]
)
def test_batch_file_refuses_characters_cmd_exe_would_read(arg):
    with pytest.raises(CommandBlocked, match="not allowed on Windows"):
        launch_args(NPM_CMD, ["install", arg], WINDOWS_ENV, "windows")


@pytest.mark.parametrize(
    "args",
    [["ci"], ["ls", "--depth=0"], ["install", "--prefix=./web"], ["install", "--frozen-lockfile"],
     ["dependency:resolve", "-Dmaven.repo.local=.m2/repository"], ["dependencies"],
     ["env", "create", "-f", "environment.yml", "-p", "./.conda-env"]],
)  # fmt: skip
def test_planned_commands_pass_the_batch_file_rule(args):
    line, _ = launch_args(NPM_CMD, args, WINDOWS_ENV, "windows")
    assert line.endswith(" ".join(args) + '"')


def test_batch_file_in_a_folder_with_a_percent_sign_is_refused():
    with pytest.raises(CommandBlocked, match="cannot be run safely"):
        launch_args(r"C:\work\100%\demo\mvnw.cmd", ["compile"], WINDOWS_ENV, "windows")


def test_windows_starts_an_exe_directly():
    program = r"C:\Tools\uv.exe"
    assert launch_args(program, ["sync"], WINDOWS_ENV, "windows") == ([program, "sync"], None)


@pytest.mark.parametrize("program", [r"C:\repo\gradlew", r"C:\Tools\tool.js", r"C:\x\tool.vbs"])
def test_file_windows_cannot_start_is_refused_before_it_runs(program):
    with pytest.raises(PrereqMissing, match=r"only \.exe, \.bat and \.cmd files run"):
        launch_args(program, ["dependencies"], WINDOWS_ENV, "windows")


def test_extensionless_wrapper_on_windows_says_to_use_gradlew_bat():
    with pytest.raises(PrereqMissing) as error:
        launch_args(r"C:\repo\gradlew", ["dependencies"], WINDOWS_ENV, "windows")
    assert "gradlew.bat" in error.value.details


def test_program_found_in_the_current_folder_is_refused(tmp_path, monkeypatch):
    fake_executable(tmp_path, "npm", "print('planted')")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(CommandBlocked, match="current folder"):
        find_program("npm", {"PATH": "."})


def test_ctrl_c_while_waiting_still_stops_the_command(tmp_path, monkeypatch):
    marker = tmp_path / "still-running"
    child = f"import time; time.sleep(2); open({str(marker)!r}, 'w')"
    real_wait = subprocess.Popen.wait
    calls = {"n": 0}

    def interrupted_wait(self, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        return real_wait(self, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", interrupted_wait)
    with pytest.raises(KeyboardInterrupt):
        run_command(python(tmp_path, child), CONFIG, on_start=no_op)
    time.sleep(3)
    assert not marker.exists()


@ON_POSIX
def test_program_that_is_not_executable_says_to_chmod(tmp_path):
    script = tmp_path / "gradlew"
    script.write_text("#!/bin/sh\necho hi\n")
    command = SafeCommand((str(script), "dependencies"), tmp_path, "./gradlew dependencies", None)
    with pytest.raises(PrereqMissing) as error:
        run_command(command, CONFIG, on_start=no_op)
    assert "chmod +x" in error.value.details


@ON_POSIX
@pytest.mark.parametrize(
    "script", [b"#!/bin/sh\r\necho hi\r\n", b"echo hi\n"], ids=["windows-line-endings", "no-#!"]
)
def test_repo_script_that_will_not_start_says_to_check_its_first_line(tmp_path, script):
    wrapper = tmp_path / "gradlew"
    wrapper.write_bytes(script)
    wrapper.chmod(0o755)
    command = SafeCommand((str(wrapper), "dependencies"), tmp_path, "./gradlew dependencies", None)
    with pytest.raises(PrereqMissing) as error:
        run_command(command, CONFIG, on_start=no_op)
    assert "line endings" in error.value.details


@ON_POSIX
def test_background_process_that_holds_the_output_is_reported(tmp_path):
    code = (
        "import os, sys, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid(); time.sleep(8); sys.exit()\n"
        "print('leader done')"
    )
    with pytest.raises(InstallFailed, match="still running"):
        run_command(python(tmp_path, code), CONFIG, on_start=no_op)


def test_secret_split_by_the_cut_is_still_hidden(tmp_path, monkeypatch):
    secret = "gsk_" + "s" * 40
    monkeypatch.setenv("GROQ_API_KEY", secret)
    code = f"print('x' * 970 + {secret!r} + 'tail')"
    out = run_command(python(tmp_path, code), CONFIG, on_start=no_op)
    assert "ssssssss" not in out.output_tail


def test_terminal_control_codes_are_removed(tmp_path):
    out = run_command(
        python(tmp_path, r"print('\x1b]0;evil title\x07ok\x00')"), CONFIG, on_start=no_op
    )
    assert "\x1b" not in out.output_tail and "\x00" not in out.output_tail


def test_cancel_stops_a_running_command_from_another_thread(tmp_path):
    started = time.monotonic()
    worker, trees, result = run_in_background(python(tmp_path, "import time; time.sleep(60)"))
    trees[0].stop()
    worker.join(timeout=15)
    assert not worker.is_alive() and time.monotonic() - started < 15
    assert result["out"].is_stopped


def test_a_command_that_ends_by_itself_is_not_called_stopped(tmp_path):
    assert not run_command(python(tmp_path, "print('done')"), CONFIG, on_start=no_op).is_stopped


# A Cancel between two commands stops the last tree, which has already been closed.
@pytest.mark.skipif(sys.platform == "win32", reason="process groups are macOS and Linux only")
def test_stop_after_the_command_ended_signals_no_process_group(tmp_path, monkeypatch):
    trees, signals = [], []
    run_command(python(tmp_path, "pass"), CONFIG, on_start=trees.append)
    monkeypatch.setattr(process_tree.os, "killpg", lambda group, sig: signals.append(sig))
    trees[0].stop()
    assert signals == []


@ON_WINDOWS
def test_stop_after_the_command_ended_does_not_use_the_closed_job(tmp_path, monkeypatch):
    trees, uses = [], []
    run_command(python(tmp_path, "pass"), CONFIG, on_start=trees.append)
    monkeypatch.setattr(process_tree, "_terminate_job", lambda job, code: uses.append(job))
    trees[0].stop()
    assert uses == []


def fake_docker(tmp_path, monkeypatch):
    """A docker on PATH that writes down each call; `docker run` hangs. Returns the call log."""
    calls = tmp_path / "docker-calls.txt"
    code = (
        "import sys, time\n"
        f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'run': time.sleep(60)\n"
    )
    fake_executable(tmp_path / "bin", "docker", code)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    argv = ("docker", "run", "--rm", "--name", "setuppilot-test", "node:22", "npm", "ci")
    return calls, SafeCommand(argv, tmp_path, "[in Docker] npm ci", "setuppilot-test")


def test_stopping_a_docker_mode_command_stops_its_container_by_name(tmp_path, monkeypatch):
    calls, command = fake_docker(tmp_path, monkeypatch)
    worker, trees, _ = run_in_background(command)
    trees[0].stop()
    worker.join(timeout=30)
    assert "stop --time 3 setuppilot-test" in calls.read_text(encoding="utf-8")


def test_ctrl_c_in_docker_mode_stops_the_container_too(tmp_path, monkeypatch):
    calls, command = fake_docker(tmp_path, monkeypatch)
    real_wait, waits = subprocess.Popen.wait, []

    def interrupted_wait(self, timeout=None):
        waits.append(timeout)
        if len(waits) == 1:
            raise KeyboardInterrupt
        return real_wait(self, timeout)

    monkeypatch.setattr(subprocess.Popen, "wait", interrupted_wait)
    with pytest.raises(KeyboardInterrupt):
        run_command(command, CONFIG, on_start=no_op)
    assert "stop --time 3 setuppilot-test" in calls.read_text(encoding="utf-8")


@pytest.mark.parametrize("target", TARGETS)
def test_go_download_settings_pass_through_but_goflags_does_not(target):
    env = child_env({"GOPROXY": "direct", "GOFLAGS": "-toolexec=/bin/evil"}, target)
    assert env["GOPROXY"] == "direct" and "GOFLAGS" not in env


@ON_WINDOWS
def test_windows_command_can_open_a_network_socket(tmp_path):
    code = "import socket; socket.socket().close(); print('ok')"
    assert run_command(python(tmp_path, code), CONFIG, on_start=no_op).output_tail.strip() == "ok"


@ON_WINDOWS
def test_batch_file_on_path_really_runs_on_windows(tmp_path, monkeypatch):
    fake_executable(tmp_path / "bin", "tool", "import sys; print('got', *sys.argv[1:])")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    command = SafeCommand(("tool", "install", "--prefix=./web"), tmp_path, "tool install", None)
    out = run_command(command, CONFIG, on_start=no_op)
    assert out.output_tail.strip() == "got install --prefix=./web"


@ON_WINDOWS
def test_detached_grandchild_is_stopped_with_the_job(tmp_path):
    marker = tmp_path / "grandchild-alive"
    child = f"import time; time.sleep(3); open({str(marker)!r}, 'w')"
    code = (
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}],"
        " creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)"
    )
    started = time.monotonic()
    run_command(python(tmp_path, code), CONFIG, on_start=no_op)
    assert time.monotonic() - started < 5
    time.sleep(4)
    assert not marker.exists()


@ON_WINDOWS
def test_command_cannot_break_away_from_its_job(tmp_path):
    # Not the venv's python.exe: that launcher runs the real python in a job of its own, which
    # may pass the break-away up to SetupPilot's job instead of refusing it.
    base_python = getattr(sys, "_base_executable", sys.executable)
    code = (
        "import subprocess, sys\n"
        "try:\n"
        "    subprocess.Popen([sys.executable, '-c', 'pass'],"
        " creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB).wait()\n"
        "    print('escaped')\n"
        "except PermissionError:\n"
        "    print('denied')\n"
    )
    command = SafeCommand((base_python, "-c", code), tmp_path, "python -c", None)
    assert run_command(command, CONFIG, on_start=no_op).output_tail.strip() == "denied"
