import time

import pytest

from setuppilot.tools import prereqs
from setuppilot.tools.detect import StackCandidate
from setuppilot.tools.prereqs import TOOLS, Probe, SystemToolProbe, check_prerequisites
from tests.fakes import fake_executable, make_repo


class FakeProbe:
    """Answers version commands from a dict: tool name -> output ('' = installed but silent)."""

    def __init__(self, outputs):
        self._by_command = {TOOLS[tool][0]: output for tool, output in outputs.items()}

    def run(self, command):
        if command not in self._by_command:
            return Probe(False, "")
        return Probe(True, self._by_command[command])


def candidate(stack, manager, evidence=(), hint=None):
    return StackCandidate(stack, manager, evidence, hint)


def check(candidate, probe, target="posix"):
    return check_prerequisites(candidate, probe, target)


def test_installed_tools_report_their_version():
    checks = check(candidate("node", "npm"), FakeProbe({"node": "v20", "npm": "10"}))
    assert [(c.tool, c.is_installed, c.version) for c in checks] == [
        ("node", True, "v20"),
        ("npm", True, "10"),
    ]


def test_missing_tool_comes_with_install_guidance():
    (go,) = check(candidate("go", "go"), FakeProbe({}))
    assert (go.is_installed, "go.dev" in go.problem) == (False, True)


def test_installed_tool_that_does_not_answer_is_not_called_missing():
    (go,) = check(candidate("go", "go"), FakeProbe({"go": ""}))
    assert go.problem.startswith("Installed, but `go version` did not answer")


@pytest.mark.parametrize(("target", "wrapper"), [("posix", "mvnw"), ("windows", "mvnw.cmd")])
def test_maven_wrapper_means_maven_need_not_be_installed(target, wrapper):
    probe = FakeProbe({"java": "openjdk 21"})
    checks = check(candidate("java", "maven", ("pom.xml", wrapper)), probe, target)
    assert [c.tool for c in checks] == ["java"]


@pytest.mark.parametrize(("target", "wrapper"), [("posix", "gradlew"), ("windows", "gradlew.bat")])
def test_gradle_wrapper_means_gradle_need_not_be_installed(target, wrapper):
    probe = FakeProbe({"java": "openjdk 21"})
    checks = check(candidate("java", "gradle", ("build.gradle", wrapper)), probe, target)
    assert [c.tool for c in checks] == ["java"]


def test_on_windows_a_unix_only_maven_wrapper_still_needs_maven():
    probe = FakeProbe({"java": "openjdk 21"})
    checks = check(candidate("java", "maven", ("pom.xml", "mvnw")), probe, "windows")
    assert [c.tool for c in checks] == ["java", "mvn"]


def test_compose_needs_docker_the_plugin_and_a_running_engine():
    probe = FakeProbe({"docker": "Docker 27", "docker compose": "v2.29"})
    checks = check(candidate("docker", "compose"), probe)
    assert [(c.tool, c.is_installed) for c in checks] == [
        ("docker", True),
        ("docker compose", True),
        ("docker engine", False),
    ]
    assert "Start Docker Desktop" in checks[2].problem


def test_docker_in_windows_containers_mode_is_not_ready_for_linux_images():
    probe = FakeProbe({"docker": "Docker 27", "docker engine": "windows"})
    engine = check(candidate("docker", "dockerfile"), probe)[1]
    assert (engine.is_installed, "Linux containers" in engine.problem) == (False, True)


def test_python_projects_need_uv_for_the_venv():
    checks = check(candidate("python", "pip"), FakeProbe({"uv": "uv 0.13"}))
    assert [(c.tool, c.is_installed) for c in checks] == [("uv", True)]


def test_pinned_version_that_differs_from_the_installed_one_is_flagged():
    probe = FakeProbe({"ruby": "ruby 2.6.10p210", "bundle": "Bundler 1.17"})
    checks = check(candidate("ruby", "bundler", hint="3.3.0"), probe)
    assert "asks for ruby 3.3.0" in checks[0].problem


def test_go_newer_than_the_go_mod_minimum_is_not_flagged():
    probe = FakeProbe({"go": "go version go1.25.1 darwin/arm64"})
    (go,) = check(candidate("go", "go", ("go.mod",), "1.22"), probe)
    assert go.problem is None


def test_version_range_is_not_compared():
    (node, _) = check(
        candidate("node", "npm", hint=">=18"), FakeProbe({"node": "v16.0.0", "npm": "8"})
    )
    assert node.problem is None


def test_system_probe_reads_the_first_line_of_the_version(tmp_path, monkeypatch):
    fake_executable(tmp_path / "bin", "node", "print('v20.11.0'); print('extra')")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert SystemToolProbe().run(("node", "--version")) == Probe(True, "v20.11.0")


def test_system_probe_says_missing_when_tool_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert SystemToolProbe().run(("node", "--version")) == Probe(False, "")


def test_system_probe_looks_only_where_commands_look(tmp_path, monkeypatch):
    fake_executable(tmp_path / "bin", "node", "print('v20.11.0')")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "bin")
    assert SystemToolProbe().run(("node", "--version")) == Probe(False, "")


def test_system_probe_gives_no_output_when_tool_fails(tmp_path, monkeypatch):
    code = "import sys; sys.stderr.write('unknown command: compose'); sys.exit(1)"
    fake_executable(tmp_path / "bin", "docker", code)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert SystemToolProbe().run(("docker", "compose", "version")) == Probe(True, "")


def test_system_probe_time_limit_also_stops_what_the_tool_started(tmp_path, monkeypatch):
    # Like gradle.bat starting java: on Windows the probe used to wait for java with no limit.
    marker = tmp_path / "child-was-not-stopped"
    child = f"import time; time.sleep(2); open({str(marker)!r}, 'w').close()"
    tool = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); "
    fake_executable(tmp_path / "bin", "gradle", tool + "time.sleep(60)")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setattr(prereqs, "_VERSION_TIMEOUT_SECONDS", 1)
    SystemToolProbe().run(("gradle", "--version"))
    time.sleep(3)
    assert not marker.exists()


def test_system_probe_never_runs_inside_the_repo(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo", {".yarnrc.yml": "yarnPath: evil.js"})
    fake_executable(tmp_path / "bin", "yarn", "import os; print(os.getcwd())")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.chdir(repo)
    assert SystemToolProbe().run(("yarn", "--version")).output != str(repo)


def test_system_probe_gets_no_secrets(tmp_path, monkeypatch):
    fake_executable(tmp_path / "bin", "node", "import os; print(os.environ.get('GROQ_API_KEY'))")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "s" * 30)
    assert SystemToolProbe().run(("node", "--version")) == Probe(True, "None")
