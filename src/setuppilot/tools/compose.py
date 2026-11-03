"""Find Docker Compose files and flag settings that give a container power over your machine."""

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import yaml

# Copied from Docker's compose-go (cli/options.go), in its order. Overrides are applied on top.
COMPOSE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
OVERRIDE_NAMES = (
    "compose.override.yml",
    "compose.override.yaml",
    "docker-compose.override.yml",
    "docker-compose.override.yaml",
)
MAX_READ_BYTES = 1_000_000
_HOST_SHARING_KEYS = ("network_mode", "pid", "ipc", "userns_mode", "uts", "cgroup")
_TRUE_WORDS = {"true", "yes", "on", "1"}


@dataclass(frozen=True)
class ComposeFile:
    path: str
    # Services that `docker compose up` starts; ones behind `profiles` start only when asked.
    services: tuple[str, ...]
    warnings: tuple[str, ...]


def find_compose_files(root, files: tuple[str, ...]) -> tuple[ComposeFile, ...]:
    """Compose files at the top level or one folder down, best match first."""
    names = COMPOSE_NAMES + OVERRIDE_NAMES

    def name_of(path: str) -> str:
        return path.rsplit("/", 1)[-1].lower()

    found = [f for f in files if f.count("/") <= 1 and name_of(f) in names]
    found.sort(key=lambda f: (f.count("/"), names.index(name_of(f))))
    return tuple(_read_compose(root, f) for f in found)


def _read_compose(root, relative: str) -> ComposeFile:
    path = root / relative
    if path.stat().st_size > MAX_READ_BYTES:
        return ComposeFile(relative, (), ("File is too big to read; skipped.",))
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (yaml.YAMLError, UnicodeDecodeError, RecursionError) as error:
        return ComposeFile(relative, (), (f"Could not read it: {type(error).__name__}.",))
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict):
        return ComposeFile(relative, (), ("No 'services' section found.",))
    folder = PurePosixPath(relative).parent
    warnings = [w for name, svc in services.items() for w in _risky(str(name), svc, folder)]
    if {"include", "extends"} & set(data) or any(
        isinstance(s, dict) and "extends" in s for s in services.values()
    ):
        warnings.append("It pulls in other compose files (include/extends) that were not checked.")
    started = tuple(
        str(name) for name, s in services.items() if not (isinstance(s, dict) and s.get("profiles"))
    )
    return ComposeFile(relative, started, tuple(warnings))


def _risky(name: str, service: Any, folder: PurePosixPath) -> list[str]:
    if not isinstance(service, dict):
        return []
    found = []
    privileged = service.get("privileged")
    if privileged is True or (isinstance(privileged, str) and _true_or_variable(privileged)):
        found.append(f"{name}: privileged mode gives the container full access to your machine.")
    for key in _HOST_SHARING_KEYS:
        value = service.get(key)
        if isinstance(value, str) and (value == "host" or "$" in value):
            found.append(f"{name}: {key}: {value} shares part of your machine with the container.")
    for key in ("cap_add", "devices"):
        if service.get(key):
            found.append(f"{name}: {key} gives the container extra power over your machine.")
    if any("unconfined" in str(opt) for opt in _as_list(service.get("security_opt"))):
        found.append(f"{name}: security_opt turns off a container safety feature.")
    for port in _as_list(service.get("ports")):
        if _publishes_to_network(port):
            found.append(
                f"{name}: port {port} is open to your whole network; 127.0.0.1:... keeps it "
                "on this machine."
            )
    for volume in _as_list(service.get("volumes")):
        source = _bind_source(volume)
        if source is _NOT_UNDERSTOOD:
            found.append(f"{name}: a volume entry could not be understood.")
        elif source is not None and _leaves_repo(source, folder):
            found.append(f"{name}: mounts {source} from your machine into the container.")
    return found


_NOT_UNDERSTOOD = object()


def _bind_source(volume: Any) -> Any:
    """The host path of a bind mount, None for a named or anonymous volume."""
    # Only plain strings and dicts are read: str() on other YAML values can expand aliases
    # into gigabytes.
    if isinstance(volume, dict):
        source = volume.get("source")
        is_bind = volume.get("type") == "bind" or (
            isinstance(source, str) and source.startswith(("/", ".", "~", "$"))
        )
        return source if is_bind and isinstance(source, str) else None
    if isinstance(volume, str):
        if ":" not in volume:
            return None
        source = volume.split(":", 1)[0]
        return source if source.startswith(("/", ".", "~", "$")) else None
    return _NOT_UNDERSTOOD


def _leaves_repo(source: str, folder: PurePosixPath) -> bool:
    if source.startswith(("/", "~")) or "$" in source:
        return True
    depth = len(folder.parts)
    for part in PurePosixPath(source).parts:
        depth += -1 if part == ".." else 0 if part == "." else 1
        if depth < 0:
            return True
    return False


def _publishes_to_network(port: Any) -> bool:
    """A published port with no 127.0.0.1 in front listens on every network the machine is on."""
    if isinstance(port, dict):
        return "published" in port and port.get("host_ip") not in ("127.0.0.1", "::1", "localhost")
    text = str(port) if isinstance(port, int | str) else ""
    if ":" not in text:
        # "8000" alone publishes on a random port, on every network.
        return bool(text)
    return not text.startswith(("127.0.0.1:", "[::1]:", "localhost:"))


def _true_or_variable(value: str) -> bool:
    return value.strip().lower() in _TRUE_WORDS or "$" in value


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []
