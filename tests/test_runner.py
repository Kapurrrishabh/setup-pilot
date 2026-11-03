import os
import subprocess
import sys
import time
import tracemalloc

import pytest

from setuppilot.config import RunnerConfig
from setuppilot.errors import CommandTimeout, InstallFailed, PrereqMissing
from setuppilot.guardrails.commands import SafeCommand
from setuppilot.guardrails.runner import child_env, run_command

CONFIG = RunnerConfig(timeout_seconds=20, max_output_chars=1000)


def no_op(group_id):
    return None


def python(tmp_path, code):
    """A command that runs a tiny Python script. Tests may build SafeCommand directly."""
    return SafeCommand((sys.executable, "-c", code), tmp_path, f"python -c {code!r}")


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
    command = SafeCommand(("no-such-tool-xyz", "install"), tmp_path, "no-such-tool-xyz install")
    with pytest.raises(PrereqMissing, match="not installed"):
        run_command(command, CONFIG, on_start=no_op)


def test_a_secret_typed_into_a_command_is_hidden_in_its_error(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_typedintoanedit123456")
    text = "no-such-tool-xyz install gsk_typedintoanedit123456"
    command = SafeCommand(tuple(text.split()), tmp_path, text)
    with pytest.raises(PrereqMissing) as raised:
        run_command(command, CONFIG, on_start=no_op)
    assert "gsk_typedintoanedit123456" not in raised.value.details


def test_child_env_turns_off_prompts():
    env = child_env()
    assert (env["GIT_TERMINAL_PROMPT"], env["PIP_NO_INPUT"]) == ("0", "1")


def test_child_env_keeps_project_environments_in_the_repo():
    in_repo = {
        "POETRY_VIRTUALENVS_IN_PROJECT": "true",
        "PIPENV_VENV_IN_PROJECT": "1",
        "BUNDLE_PATH": "vendor/bundle",
    }
    assert child_env().items() >= in_repo.items()


@pytest.mark.parametrize("name", ["DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"])
def test_docker_engine_setting_reaches_the_command(monkeypatch, name):
    monkeypatch.setenv(name, "from-your-shell")
    assert child_env().get(name) == "from-your-shell"


@pytest.mark.parametrize(
    "name",
    ["VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONPATH", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN",
     "OPENAI_API_KEY", "SSH_AUTH_SOCK", "NPM_TOKEN"],
)  # fmt: skip
def test_only_safe_environment_reaches_the_command(monkeypatch, name):
    monkeypatch.setenv(name, "something-private")
    assert name not in child_env()


def test_relative_path_entries_are_dropped(monkeypatch):
    monkeypatch.setenv("PATH", "node_modules/.bin:/usr/bin:.")
    assert child_env()["PATH"] == "/usr/bin"


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


def test_program_that_is_not_executable_says_to_chmod(tmp_path):
    script = tmp_path / "gradlew"
    script.write_text("#!/bin/sh\necho hi\n")
    command = SafeCommand((str(script), "dependencies"), tmp_path, "./gradlew dependencies")
    with pytest.raises(PrereqMissing) as error:
        run_command(command, CONFIG, on_start=no_op)
    assert "chmod +x" in error.value.details


@pytest.mark.parametrize(
    "script", [b"#!/bin/sh\r\necho hi\r\n", b"echo hi\n"], ids=["windows-line-endings", "no-#!"]
)
def test_repo_script_that_will_not_start_says_to_check_its_first_line(tmp_path, script):
    wrapper = tmp_path / "gradlew"
    wrapper.write_bytes(script)
    wrapper.chmod(0o755)
    command = SafeCommand((str(wrapper), "dependencies"), tmp_path, "./gradlew dependencies")
    with pytest.raises(PrereqMissing) as error:
        run_command(command, CONFIG, on_start=no_op)
    assert "line endings" in error.value.details


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
    import threading

    from setuppilot.guardrails.runner import cancel_group

    groups = []
    command = python(tmp_path, "import time; time.sleep(60)")
    worker_result = {}

    def work():
        worker_result["out"] = run_command(command, CONFIG, on_start=groups.append)

    worker = threading.Thread(target=work)
    started = time.monotonic()
    worker.start()
    while not groups:
        time.sleep(0.05)
    cancel_group(groups[0])
    worker.join(timeout=15)
    assert not worker.is_alive() and time.monotonic() - started < 15
    assert worker_result["out"].exit_code != 0


def test_go_download_settings_pass_through_but_goflags_does_not(monkeypatch):
    monkeypatch.setenv("GOPROXY", "direct")
    monkeypatch.setenv("GOFLAGS", "-toolexec=/bin/evil")
    env = child_env()
    assert env["GOPROXY"] == "direct" and "GOFLAGS" not in env
