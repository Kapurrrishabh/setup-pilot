"""Optional Docker mode: run a checked command in a throwaway container that sees only the repo."""

import os

from setuppilot.errors import CommandBlocked
from setuppilot.guardrails.commands import SafeCommand
from setuppilot.guardrails.runner import GO_DOWNLOAD_ENV, IN_REPO_ENV

# PROVISIONAL images, by package manager: each must contain the tool the plan runs.
# Full (not slim) images, because many packages build native code while installing.
DOCKER_IMAGES: dict[str, str] = {
    "pip": "ghcr.io/astral-sh/uv:python3.12-bookworm",
    "uv": "ghcr.io/astral-sh/uv:python3.12-bookworm",
    "npm": "node:22",
    "yarn": "node:22",
    "bun": "oven/bun:1",
    "go": "golang:1",
    "cargo": "rust:1",
    "bundler": "ruby:3.3",
    "maven": "maven:3-eclipse-temurin-21",
    "gradle": "gradle:jdk21",
}
_WORKDIR = "/work"
# The container runs as you, not root, so tool caches live in the repo where it can write.
_HOME = f"{_WORKDIR}/.setuppilot-home"
_CONTAINER_ENV = IN_REPO_ENV | {
    "HOME": _HOME,
    "GOPATH": f"{_HOME}/go",
    "GOTOOLCHAIN": "auto",
    # Go makes its module cache read-only, so you could not simply delete the repo folder later.
    "GOFLAGS": "-modcacherw",
    "CARGO_HOME": f"{_HOME}/cargo",
    "GRADLE_USER_HOME": f"{_HOME}/gradle",
}


def docker_image_for(manager: str) -> str:
    if manager not in DOCKER_IMAGES:
        raise CommandBlocked(
            f"Docker mode does not support {manager} yet.",
            details=f"Supported: {', '.join(sorted(DOCKER_IMAGES))}. Turn Docker mode off to "
            "install on your machine instead.",
        )
    return DOCKER_IMAGES[manager]


def in_docker(command: SafeCommand, image: str) -> SafeCommand:
    host_root = str(command.cwd)
    inner = [_to_container_path(arg, host_root) for arg in command.argv]
    env_flags = [flag for key, value in _CONTAINER_ENV.items() for flag in ("-e", f"{key}={value}")]
    # A bare name copies the value from docker's own environment, and only if it is set there.
    env_flags += [flag for key in GO_DOWNLOAD_ENV for flag in ("-e", key)]
    argv = (
        "docker",
        "run",
        "--rm",
        # A small init process passes our stop signal on, so a timeout really stops the command.
        "--init",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "512",
        "-v",
        f"{host_root}:{_WORKDIR}",
        # Read-only, so the container cannot plant git hooks that would run on your machine.
        "--mount",
        f"type=bind,source={host_root}/.git,target={_WORKDIR}/.git,readonly",
        "-w",
        _WORKDIR,
        *env_flags,
        image,
        *inner,
    )
    return SafeCommand(argv, command.cwd, f"[in Docker, {image}] {command.text}")


def _to_container_path(arg: str, host_root: str) -> str:
    head, equals, value = arg.rpartition("=") if "=" in arg else ("", "", arg)
    if value == host_root or value.startswith(host_root + "/"):
        value = _WORKDIR + value.removeprefix(host_root)
    return head + equals + value
