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


def test_installed_tools_report_their_version():
    checks = check_prerequisites(candidate("node", "npm"), FakeProbe({"node": "v20", "npm": "10"}))
    assert [(c.tool, c.is_installed, c.version) for c in checks] == [
        ("node", True, "v20"),
        ("npm", True, "10"),
    ]


def test_missing_tool_comes_with_install_guidance():
    (check,) = check_prerequisites(candidate("go", "go"), FakeProbe({}))
    assert (check.is_installed, "go.dev" in check.problem) == (False, True)


def test_installed_tool_that_does_not_answer_is_not_called_missing():
    (check,) = check_prerequisites(candidate("go", "go"), FakeProbe({"go": ""}))
    assert check.problem.startswith("Installed, but `go version` did not answer")


def test_maven_wrapper_means_maven_need_not_be_installed():
    probe = FakeProbe({"java": "openjdk 21"})
    checks = check_prerequisites(candidate("java", "maven", ("pom.xml", "mvnw")), probe)
    assert [c.tool for c in checks] == ["java"]


def test_gradle_wrapper_means_gradle_need_not_be_installed():
    probe = FakeProbe({"java": "openjdk 21"})
    checks = check_prerequisites(candidate("java", "gradle", ("build.gradle", "gradlew")), probe)
    assert [c.tool for c in checks] == ["java"]


def test_compose_needs_docker_the_plugin_and_a_running_engine():
    probe = FakeProbe({"docker": "Docker 27", "docker compose": "v2.29"})
    checks = check_prerequisites(candidate("docker", "compose"), probe)
    assert [(c.tool, c.is_installed) for c in checks] == [
        ("docker", True),
        ("docker compose", True),
        ("docker engine", False),
    ]
    assert "Start Docker Desktop" in checks[2].problem


def test_python_projects_need_uv_for_the_venv():
    checks = check_prerequisites(candidate("python", "pip"), FakeProbe({"uv": "uv 0.13"}))
    assert [(c.tool, c.is_installed) for c in checks] == [("uv", True)]


def test_pinned_version_that_differs_from_the_installed_one_is_flagged():
    probe = FakeProbe({"ruby": "ruby 2.6.10p210", "bundle": "Bundler 1.17"})
    checks = check_prerequisites(candidate("ruby", "bundler", hint="3.3.0"), probe)
    assert "asks for ruby 3.3.0" in checks[0].problem


def test_version_range_is_not_compared():
    (node, _) = check_prerequisites(
        candidate("node", "npm", hint=">=18"), FakeProbe({"node": "v16.0.0", "npm": "8"})
    )
    assert node.problem is None


def test_system_probe_reads_the_first_line_of_the_version(tmp_path, monkeypatch):
    fake_executable(tmp_path / "bin", "node", "echo v20.11.0; echo extra")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert SystemToolProbe().run(("node", "--version")) == Probe(True, "v20.11.0")


def test_system_probe_says_missing_when_tool_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert SystemToolProbe().run(("node", "--version")) == Probe(False, "")


def test_system_probe_gives_no_output_when_tool_fails(tmp_path, monkeypatch):
    fake_executable(tmp_path / "bin", "docker", "echo 'unknown command: compose' >&2; exit 1")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert SystemToolProbe().run(("docker", "compose", "version")) == Probe(True, "")


def test_system_probe_never_runs_inside_the_repo(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo", {".yarnrc.yml": "yarnPath: evil.js"})
    fake_executable(tmp_path / "bin", "yarn", "pwd")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.chdir(repo)
    assert SystemToolProbe().run(("yarn", "--version")).output != str(repo)
