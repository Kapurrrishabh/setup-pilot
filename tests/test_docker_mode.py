import functools

import pytest

from setuppilot.errors import CommandBlocked
from setuppilot.guardrails import commands
from setuppilot.guardrails.docker_mode import docker_image_for, docker_user, exec_target, in_docker
from setuppilot.guardrails.paths import PathGuard

# Commands in Docker mode run in a Linux image, whatever system SetupPilot runs on.
validate_command = functools.partial(commands.validate_command, target="posix")


def test_docker_mode_mounts_only_the_repo_and_drops_privileges(tmp_path):
    (tmp_path / "repo").mkdir()
    guard = PathGuard(tmp_path / "repo")
    argv = in_docker(validate_command("npm install", guard), "node:22-slim").argv
    assert argv[:3] == ("docker", "run", "--rm")
    assert ("-v", f"{guard.workspace}:/work") == argv[argv.index("-v") : argv.index("-v") + 2]
    assert {
        ("--user", docker_user()),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--pids-limit", "512"),
    } <= set(zip(argv, argv[1:], strict=False))
    assert argv[-3:] == ("node:22-slim", "npm", "install")


def test_tool_caches_and_environments_stay_inside_the_repo(tmp_path):
    (tmp_path / "repo").mkdir()
    argv = in_docker(validate_command("bundle install", PathGuard(tmp_path / "repo")), "ruby").argv
    pairs = set(zip(argv, argv[1:], strict=False))
    assert {("-e", "HOME=/work/.setuppilot-home"), ("-e", "BUNDLE_PATH=vendor/bundle")} <= pairs


def test_workspace_program_path_is_mapped_into_the_container(tmp_path):
    (tmp_path / "repo").mkdir()
    guard = PathGuard(tmp_path / "repo")
    argv = in_docker(validate_command("./mvnw compile", guard), "maven:3").argv
    assert argv[-2:] == ("/work/mvnw", "compile")


def test_secrets_are_not_passed_into_the_container(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret")
    (tmp_path / "repo").mkdir()
    argv = in_docker(validate_command("npm ci", PathGuard(tmp_path / "repo")), "node:22").argv
    assert not any("GROQ" in a for a in argv)


def test_git_folder_is_mounted_read_only(tmp_path):
    (tmp_path / "repo").mkdir()
    guard = PathGuard(tmp_path / "repo")
    argv = in_docker(validate_command("npm ci", guard), "node:22").argv
    assert f"type=bind,source={guard.workspace / '.git'},target=/work/.git,readonly" in argv
    assert "--init" in argv


def test_container_gets_a_name_so_a_stop_can_reach_it(tmp_path):
    (tmp_path / "repo").mkdir()
    command = in_docker(validate_command("npm ci", PathGuard(tmp_path / "repo")), "node:22")
    assert command.container_name and command.argv[3:5] == ("--name", command.container_name)


@pytest.mark.parametrize(
    ("host", "use_docker", "manager", "target"),
    [("windows", True, "npm", "posix"), ("windows", True, "compose", "windows"),
     ("windows", False, "npm", "windows"), ("posix", True, "maven", "posix")],
)  # fmt: skip
def test_docker_mode_runs_linux_commands_except_for_compose(host, use_docker, manager, target):
    assert exec_target(host, use_docker, manager) == target


def test_python_projects_use_an_image_that_has_uv():
    assert "astral-sh/uv" in docker_image_for("pip")


def test_manager_without_an_image_is_refused_clearly():
    with pytest.raises(CommandBlocked, match="does not support poetry"):
        docker_image_for("poetry")


@pytest.mark.parametrize(
    ("manager", "image"),
    [("dotnet", "dotnet/sdk"), ("composer", "composer"), ("dart", "dart")],
)
def test_new_ecosystems_have_an_image_with_their_tool(manager, image):
    assert image in docker_image_for(manager)


def test_flutter_has_no_image_because_its_sdk_is_too_big_to_pull_per_run():
    with pytest.raises(CommandBlocked, match="does not support flutter"):
        docker_image_for("flutter")


def test_workspace_path_after_equals_is_mapped(tmp_path):
    (tmp_path / "repo").mkdir()
    guard = PathGuard(tmp_path / "repo")
    command = validate_command(f"npm install --prefix={guard.workspace}/web", guard)
    assert in_docker(command, "node:22").argv[-1] == "--prefix=/work/web"


def test_go_download_setting_is_passed_by_name_not_by_value(tmp_path):
    (tmp_path / "repo").mkdir()
    argv = in_docker(validate_command("go mod download", PathGuard(tmp_path / "repo")), "go").argv
    assert argv[argv.index("GOPROXY") - 1 : argv.index("GOPROXY") + 1] == ("-e", "GOPROXY")
