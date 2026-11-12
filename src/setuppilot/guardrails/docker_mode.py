"""Optional Docker mode: run a checked command in a throwaway container that sees only the repo."""

import os
import secrets
import sys
from pathlib import Path, PurePosixPath

from setuppilot.errors import CommandBlocked
from setuppilot.guardrails.commands import ExecTarget, SafeCommand
from setuppilot.guardrails.runner import GO_DOWNLOAD_ENV, IN_REPO_ENV

# Compose stacks start their own containers, so Docker mode runs their commands on the host.
RUNS_ON_HOST = frozenset({"compose"})

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


def exec_target(host: ExecTarget, use_docker: bool, manager: str) -> ExecTarget:
    """Where a stack's commands run: Docker mode runs them in a Linux image on any host."""
    return "posix" if use_docker and manager not in RUNS_ON_HOST else host


def docker_user() -> str:
    """Run as you, not root. Windows has no user ids, so any id but root's (0) will do."""
    if sys.platform == "win32":
        return "1000:1000"  # PROVISIONAL: not yet tried with Docker Desktop on Windows.
    return f"{os.getuid()}:{os.getgid()}"


def docker_image_for(manager: str) -> str:
    if manager not in DOCKER_IMAGES:
        raise CommandBlocked(
            f"Docker mode does not support {manager} yet.",
            details=f"Supported: {', '.join(sorted(DOCKER_IMAGES))}. Turn Docker mode off to "
            "install on your machine instead.",
        )
    return DOCKER_IMAGES[manager]


def in_docker(command: SafeCommand, image: str) -> SafeCommand:
    inner = [_to_container_path(arg, command.cwd) for arg in command.argv]
    env_flags = [flag for key, value in _CONTAINER_ENV.items() for flag in ("-e", f"{key}={value}")]
    # A bare name copies the value from docker's own environment, and only if it is set there.
    env_flags += [flag for key in GO_DOWNLOAD_ENV for flag in ("-e", key)]
    name = f"setuppilot-{secrets.token_hex(6)}"
    argv = (
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        # A small init process passes our stop signal on, so a timeout really stops the command.
        "--init",
        "--user",
        docker_user(),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "512",
        "-v",
        f"{command.cwd}:{_WORKDIR}",
        # Read-only, so the container cannot plant git hooks that would run on your machine.
        "--mount",
        f"type=bind,source={command.cwd / '.git'},target={_WORKDIR}/.git,readonly",
        "-w",
        _WORKDIR,
        *env_flags,
        image,
        *inner,
    )
    return SafeCommand(argv, command.cwd, f"[in Docker, {image}] {command.text}", name)


def _to_container_path(arg: str, host_root: Path) -> str:
    head, equals, value = arg.rpartition("=") if "=" in arg else ("", "", arg)
    # Path, not text: on Windows the repo is C:\...\repo and its files use backslashes.
    if Path(value).is_relative_to(host_root):
        value = PurePosixPath(_WORKDIR, *Path(value).relative_to(host_root).parts).as_posix()
    return head + equals + value
