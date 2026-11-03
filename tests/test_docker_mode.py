import pytest

from setuppilot.errors import CommandBlocked
from setuppilot.guardrails.commands import validate_command
from setuppilot.guardrails.docker_mode import docker_image_for, in_docker
from setuppilot.guardrails.paths import PathGuard


def test_docker_mode_mounts_only_the_repo_and_drops_privileges(tmp_path):
    (tmp_path / "repo").mkdir()
    guard = PathGuard(tmp_path / "repo")
    argv = in_docker(validate_command("npm install", guard), "node:22-slim").argv
    assert argv[:3] == ("docker", "run", "--rm")
    assert ("-v", f"{guard.workspace}:/work") == argv[argv.index("-v") : argv.index("-v") + 2]
    assert "--cap-drop" in argv and "no-new-privileges" in argv
    assert argv[-3:] == ("node:22-slim", "npm", "install")


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
    assert f"type=bind,source={guard.workspace}/.git,target=/work/.git,readonly" in argv
    assert "--init" in argv


def test_python_projects_use_an_image_that_has_uv():
    assert "astral-sh/uv" in docker_image_for("pip")


def test_manager_without_an_image_is_refused_clearly():
    with pytest.raises(CommandBlocked, match="does not support poetry"):
        docker_image_for("poetry")


def test_workspace_path_after_equals_is_mapped(tmp_path):
    (tmp_path / "repo").mkdir()
    guard = PathGuard(tmp_path / "repo")
    command = validate_command(f"npm install --prefix={guard.workspace}/web", guard)
    assert in_docker(command, "node:22").argv[-1] == "--prefix=/work/web"


def test_go_download_setting_is_passed_by_name_not_by_value(tmp_path):
    (tmp_path / "repo").mkdir()
    argv = in_docker(validate_command("go mod download", PathGuard(tmp_path / "repo")), "go").argv
    assert argv[argv.index("GOPROXY") - 1 : argv.index("GOPROXY") + 1] == ("-e", "GOPROXY")
