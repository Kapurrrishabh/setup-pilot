import os
import time
from typing import get_args

import pytest

from setuppilot.tools import prereqs
from setuppilot.tools.detect import StackCandidate
from setuppilot.tools.host import HostOs, add_to_path, linux_family, with_new_entries
from setuppilot.tools.prereqs import TOOLS, Probe, SystemToolProbe, check_prerequisites
from tests.fakes import fake_executable, make_repo

HOST_OSES = get_args(HostOs)


class FakeProbe:
    """Answers version commands from a dict: tool name -> output ('' = installed but silent)."""

    def __init__(self, outputs):
        self._by_command = {TOOLS[tool].version_command: output for tool, output in outputs.items()}

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


def test_node_below_the_repos_minimum_blocks_the_run():
    (node, npm) = check(
        candidate("node", "npm", hint=">=18"), FakeProbe({"node": "v16.0.0", "npm": "8"})
    )
    assert (node.is_blocking, node.fits_version, npm.is_blocking) == (True, False, False)
    assert "asks for node >=18" in node.problem


def test_node_inside_the_repos_range_does_not_block():
    (node, _) = check(
        candidate("node", "npm", hint=">=18"), FakeProbe({"node": "v20.0.0", "npm": "10"})
    )
    assert (node.is_blocking, node.fits_version, node.problem) == (False, True, None)


def test_an_exact_pin_that_differs_only_warns():
    probe = FakeProbe({"node": "v20.11.0", "npm": "10"})
    (node, _) = check(candidate("node", "npm", hint="18.19.0"), probe)
    assert (node.is_blocking, node.fits_version) == (False, False) and node.problem


def test_go_older_than_the_minimum_only_warns_because_go_fetches_its_own_toolchain():
    (go,) = check(candidate("go", "go", ("go.mod",), "1.22"), FakeProbe({"go": "go1.21.0"}))
    assert (go.is_blocking, "asks for go >=1.22" in go.problem) == (False, True)


def test_missing_optional_tool_is_reported_but_does_not_block():
    need = prereqs.ToolNeed("docker", False, None, "the README mentions it")
    (docker,) = prereqs.check_tools([need], FakeProbe({}))
    assert (docker.is_installed, docker.is_blocking) == (False, False)


def test_a_tool_is_required_when_either_the_files_or_the_ai_say_so():
    files = [prereqs.ToolNeed("node", False, ">=18", "package.json")]
    ai = [prereqs.ToolNeed("node", True, ">=16", "the AI"), prereqs.ToolNeed("git", True, None, "")]
    merged = prereqs.merged_needs(files, ai)
    assert [(n.tool, n.is_required, n.constraint) for n in merged] == [
        ("node", True, ">=18"),
        ("git", True, None),
    ]


@pytest.mark.parametrize(("name", "tool"), [("python", "uv"), ("node", "node"), ("other", None)])
def test_ai_tool_names_map_to_tools_setuppilot_can_check(name, tool):
    assert prereqs.known_tool(name) == tool


def test_windows_offers_winget_with_an_admin_warning_for_node():
    recipe = prereqs.recipe_for("node", "windows")
    assert recipe.command == ("winget", "OpenJS.NodeJS.LTS") and recipe.needs_admin


def test_macos_offers_brew_for_go():
    assert prereqs.recipe_for("go", "macos").command == ("brew", "go")


@pytest.mark.parametrize(("os_name", "manager"), [("debian", "apt"), ("fedora", "dnf")])
def test_linux_package_installs_need_sudo_so_they_are_only_shown(os_name, manager):
    recipe = prereqs.recipe_for("node", os_name)
    assert recipe.command is None and f"sudo {manager} install" in recipe.manual


@pytest.mark.parametrize("os_name", ["windows", "macos", "debian", "linux"])
def test_poetry_installs_with_uv_tool_everywhere_without_admin(os_name):
    recipe = prereqs.recipe_for("poetry", os_name)
    assert recipe.command == ("uv_tool", "poetry") and not recipe.needs_admin


class OnlyThese:
    """A probe that finds only the programs whose commands it is given."""

    def __init__(self, *commands):
        self._commands = set(commands)

    def run(self, command):
        return Probe(command in self._commands, "1.0")


def test_brew_installs_are_offered_only_when_brew_is_there():
    found = prereqs.usable_recipe("go", "macos", OnlyThese(("brew", "--version")))
    missing = prereqs.usable_recipe("go", "macos", OnlyThese())
    assert found.command == ("brew", "go") and missing.command is None
    assert "with brew, which is not installed" in missing.manual and "go.dev" in missing.manual


@pytest.mark.parametrize("os_name", HOST_OSES)
def test_uv_tool_installs_are_offered_only_when_uv_is_there(os_name):
    assert prereqs.usable_recipe("poetry", os_name, OnlyThese()).command is None
    found = prereqs.usable_recipe("poetry", os_name, FakeProbe({"uv": "uv 0.9"}))
    assert found.command == ("uv_tool", "poetry")


@pytest.mark.parametrize(
    ("text", "target", "tools"),
    [("npm install", "posix", ["node", "npm"]), ("bundle install", "posix", ["ruby", "bundle"]),
     ("mvn compile", "posix", ["java", "mvn"]), ("./mvnw compile", "posix", ["java"]),
     (r".\mvnw.cmd compile", "windows", ["java"]),
     ("git submodule update --init", "posix", ["git"]),
     ("docker compose up -d", "posix", ["docker", "docker compose", "docker engine"]),
     (".venv/bin/python -c 'import x'", "posix", [])],
)  # fmt: skip
def test_a_command_needs_the_tools_its_program_runs_on(text, target, tools):
    assert [need.tool for need in prereqs.needs_for_command(text, target)] == tools


def test_a_folder_added_to_an_empty_path_does_not_add_the_current_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "")
    add_to_path(str(tmp_path))
    assert os.environ["PATH"] == str(tmp_path)


def test_only_a_full_path_is_added_to_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    add_to_path("relative/bin")
    add_to_path(str(tmp_path))
    assert os.environ["PATH"] == str(tmp_path)


@pytest.mark.parametrize("tool", ["docker", "docker engine", "conda", "flutter"])
def test_some_tools_are_always_installed_by_hand(tool):
    assert all(prereqs.recipe_for(tool, os_name).command is None for os_name in HOST_OSES)


@pytest.mark.parametrize(
    ("os_release", "family"),
    [('ID=ubuntu\nID_LIKE=debian\n', "debian"), ('ID="rhel"\nID_LIKE="fedora"\n', "fedora"),
     ("ID=arch\n", "linux"), ("", "linux")],
)  # fmt: skip
def test_linux_family_picks_the_install_command_style(os_release, family):
    assert linux_family(os_release) == family


def test_refreshed_windows_path_keeps_todays_order_and_adds_only_new_full_paths():
    current = r"C:\Windows;C:\Users\me\.venv\Scripts"
    saved = [r"c:\windows", r"C:\Program Files\nodejs", "relative\\bin", r"\rooted", ""]
    assert with_new_entries(current, saved) == current + r";C:\Program Files\nodejs"


def test_every_ai_tool_name_but_other_has_a_check():
    from setuppilot.analysis.schema import ToolId

    assert all(prereqs.known_tool(t) for t in get_args(ToolId) if t != "other")


def test_every_manager_with_a_plan_says_which_tools_it_needs():
    from setuppilot.setup.plans import SUPPORTED_MANAGERS

    for manager in SUPPORTED_MANAGERS:
        assert prereqs.needs_for(candidate("x", manager), "posix"), manager


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
