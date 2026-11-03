import time

import pytest

from setuppilot.tools.compose import find_compose_files
from setuppilot.tools.scan import scan_repo
from tests.fakes import make_repo

WEB = (
    "services:\n  web:\n    build: .\n    ports: ['127.0.0.1:8000:8000']\n"
    "  db:\n    image: postgres\n"
)


def compose_files(tmp_path, files):
    scan = scan_repo(make_repo(tmp_path / "repo", files))
    return find_compose_files(scan.root, scan.files)


def test_a_long_service_name_with_many_mounts_gives_a_short_warning_list(tmp_path):
    mounts = "".join(f"      - /host{i}:/c{i}\n" for i in range(500))
    # 1000 characters: YAML refuses longer names unless they use the explicit `? key` form.
    compose = f"services:\n  {'x' * 1000}:\n    image: y\n    volumes:\n{mounts}"
    (found,) = compose_files(tmp_path, {"compose.yaml": compose})
    assert len(found.warnings) == 21 and found.warnings[-1] == "...and 480 more."
    assert max(len(w) for w in found.warnings) < 300


def test_many_port_warnings_cannot_hide_a_privileged_service_past_the_cut(tmp_path):
    ports = "".join(f"  web{i}:\n    image: x\n    ports: ['80{i:02}:80']\n" for i in range(20))
    evil = "  evil:\n    image: y\n    privileged: true\n    volumes: ['/:/host']\n"
    (found,) = compose_files(tmp_path, {"compose.yaml": "services:\n" + ports + evil})
    shown = " ".join(found.warnings)
    assert "privileged" in shown and "mounts /" in shown


def test_a_very_long_mount_path_gives_a_short_warning(tmp_path):
    compose = f"services:\n  web:\n    image: x\n    volumes: ['/{'a' * 5000}:/c']\n"
    (found,) = compose_files(tmp_path, {"compose.yaml": compose})
    assert found.warnings and max(len(w) for w in found.warnings) <= 301


def test_two_long_service_names_that_start_the_same_are_both_checked(tmp_path):
    same = "x" * 100
    first = f"  {same}a:\n    image: y\n"
    second = f"  {same}b:\n    image: y\n    privileged: true\n"
    compose = "services:\n" + first + second
    (found,) = compose_files(tmp_path, {"compose.yaml": compose})
    assert any("privileged" in w for w in found.warnings)


def test_compose_file_lists_its_services(tmp_path):
    (found,) = compose_files(tmp_path, {"docker-compose.yml": WEB})
    assert (found.path, found.services, found.warnings) == ("docker-compose.yml", ("web", "db"), ())


def test_docker_lookup_order_and_top_level_come_first(tmp_path):
    files = {
        "docker/compose.yml": WEB,
        "docker-compose.yaml": WEB,
        "docker-compose.yml": WEB,
        "compose.yaml": WEB,
        "docker-compose.override.yaml": WEB,
    }
    assert [f.path for f in compose_files(tmp_path, files)] == [
        "compose.yaml",
        "docker-compose.yml",
        "docker-compose.yaml",
        "docker-compose.override.yaml",
        "docker/compose.yml",
    ]


def test_compose_files_deeper_than_one_folder_are_ignored(tmp_path):
    assert compose_files(tmp_path, {"a/b/docker-compose.yml": WEB}) == ()


def test_risky_settings_are_flagged(tmp_path):
    risky = (
        "services:\n  app:\n    image: x\n    privileged: true\n    network_mode: host\n"
        "    volumes:\n      - /var/run/docker.sock:/var/run/docker.sock\n      - ./data:/data\n"
        "      - type: bind\n        source: /etc\n        target: /host-etc\n"
    )
    (found,) = compose_files(tmp_path, {"compose.yaml": risky})
    assert len(found.warnings) == 4, found.warnings
    assert any("docker.sock" in w for w in found.warnings)
    assert not any("./data" in w for w in found.warnings)


def test_broken_yaml_becomes_a_warning(tmp_path):
    (found,) = compose_files(tmp_path, {"compose.yaml": "services: [unclosed"})
    assert "Could not read it" in found.warnings[0]


def warnings_for(tmp_path, service_yaml, name="compose.yaml"):
    (found,) = compose_files(tmp_path, {name: "services:\n  app:\n" + service_yaml})
    return found.warnings


@pytest.mark.parametrize(
    "volume",
    ["${HOME}/.aws:/root/.aws", "$HOME/.ssh:/root/.ssh", "../..:/host", "~/.kube:/k", "/:/host"],
)
def test_mounts_that_reach_outside_the_repo_are_flagged(tmp_path, volume):
    assert warnings_for(tmp_path, f"    volumes: ['{volume}']\n")


@pytest.mark.parametrize("volume", ["./data:/data", "data_volume:/var/lib/db", "/app/node_modules"])
def test_repo_mounts_and_named_or_anonymous_volumes_are_not_flagged(tmp_path, volume):
    assert warnings_for(tmp_path, f"    volumes: ['{volume}']\n") == ()


def test_volume_entry_that_is_neither_text_nor_a_map_is_flagged(tmp_path):
    assert "could not be understood" in warnings_for(tmp_path, "    volumes: [[a, b]]\n")[0]


def test_parent_folder_mount_from_a_compose_file_in_a_subfolder_stays_in_the_repo(tmp_path):
    files = {"docker/compose.yml": "services:\n  app:\n    volumes: ['..:/app']\n"}
    (found,) = compose_files(tmp_path, files)
    assert found.warnings == ()


@pytest.mark.parametrize(
    "setting",
    [
        "    privileged: 'true'\n",
        "    cap_add: [SYS_ADMIN]\n",
        "    devices: ['/dev/sda:/dev/sda']\n",
        "    security_opt: ['seccomp:unconfined']\n",
        "    pid: host\n",
        "    userns_mode: host\n",
    ],
)
def test_settings_that_give_host_power_are_flagged(tmp_path, setting):
    assert warnings_for(tmp_path, setting)


def test_override_file_docker_applies_by_itself_is_checked_too(tmp_path):
    files = {"docker-compose.yml": WEB, "docker-compose.override.yaml": RISKY_OVERRIDE}
    flagged = [f for f in compose_files(tmp_path, files) if f.warnings]
    assert [f.path for f in flagged] == ["docker-compose.override.yaml"]


RISKY_OVERRIDE = "services:\n  web:\n    privileged: true\n"


def test_include_of_unchecked_files_is_flagged(tmp_path):
    files = {"compose.yaml": "include: [other.yml]\nservices:\n  web:\n    image: x\n"}
    (found,) = compose_files(tmp_path, files)
    assert "include/extends" in found.warnings[0]


NESTED_ALIASES = "\n".join(
    ["x0: &x0 [a, a, a, a, a, a, a, a, a]"]
    + [f"x{i}: &x{i} [" + ", ".join([f"*x{i - 1}"] * 9) + "]" for i in range(1, 9)]
)
SHARED_SERVICES = "".join(
    ["x: &v '/x:/y'\nservices:\n  a: &svc\n    volumes: [", ", ".join(["*v"] * 2000), "]\n"]
    + [f"  s{i}: *svc\n" for i in range(1000)]
)


@pytest.mark.parametrize(
    "bomb",
    [
        NESTED_ALIASES + "\nservices:\n  app:\n    volumes: [*x8]\n",
        NESTED_ALIASES + "\nservices:\n  app:\n    security_opt: [*x8]\n",
        NESTED_ALIASES + "\nservices:\n  app:\n    ports: [{published: 8080, x: *x8}]\n",
        SHARED_SERVICES,
    ],
    ids=["volumes", "security_opt", "port", "shared-services"],
)
def test_yaml_alias_bomb_is_read_quickly(tmp_path, bomb):
    start = time.perf_counter()
    (found,) = compose_files(tmp_path, {"compose.yaml": bomb})
    assert time.perf_counter() - start < 2
    assert found.warnings == ("Its YAML aliases make it too big to read; skipped.",)


def test_settings_merged_in_from_a_yaml_anchor_are_still_checked(tmp_path):
    compose = "x-common: &common {privileged: true}\nservices:\n  app: {<<: *common, image: x}\n"
    (found,) = compose_files(tmp_path, {"compose.yaml": compose})
    assert "privileged" in found.warnings[0]


def test_number_too_long_for_python_becomes_a_warning(tmp_path):
    warnings = warnings_for(tmp_path, "    mem_limit: " + "1" * 5000 + "\n")
    assert warnings == ("Could not read it: ValueError.",)


IMAGE_ONLY = "services: {app: {image: x}}\n"


@pytest.mark.parametrize(
    ("compose", "source"),
    [
        (IMAGE_ONLY + "secrets: {creds: {file: ~/.aws/credentials}}", "~/.aws/credentials"),
        (IMAGE_ONLY + "configs: {hosts: {file: ../../etc/hosts}}", "../../etc/hosts"),
        (IMAGE_ONLY + "volumes: {home: {driver_opts: {o: bind, device: /Users}}}", "/Users"),
        ("services: {app: {build: ../..}}", "../.."),
        ("services: {app: {build: {context: ~/.ssh}}}", "~/.ssh"),
        ("services: {app: {build: {additional_contexts: {keys: ~/.ssh}}}}", "~/.ssh"),
        ("services: {app: {build: {additional_contexts: ['keys=~/.ssh']}}}", "~/.ssh"),
    ],
    ids=["secret", "config", "volume-device", "build", "context", "extra-map", "extra-list"],
)
def test_host_paths_reached_through_secrets_configs_volumes_or_build_are_flagged(
    tmp_path, compose, source
):
    (found,) = compose_files(tmp_path, {"compose.yaml": compose})
    (warning,) = found.warnings
    assert source in warning


def test_secrets_volumes_and_build_folders_inside_the_repo_are_not_flagged(tmp_path):
    compose = (
        "services: {app: {build: {context: ., additional_contexts: {assets: ./assets}}}}\n"
        "secrets: {key: {file: ./secret.txt}}\nvolumes: {data: {}}\n"
    )
    (found,) = compose_files(tmp_path, {"compose.yaml": compose})
    assert found.warnings == ()


@pytest.mark.parametrize(
    "port",
    ["'8000:8000'", "'0.0.0.0:8000:8000'", "8000", "{target: 80, published: 8080}", "{target: 80}"],
)
def test_ports_open_to_the_whole_network_are_flagged(tmp_path, port):
    assert any("whole network" in w for w in warnings_for(tmp_path, f"    ports: [{port}]\n"))


def test_long_form_port_warning_names_only_the_port(tmp_path):
    (warning,) = warnings_for(tmp_path, "    ports: [{target: 80, published: 8080, x: [a, b]}]\n")
    assert warning.startswith("app: port 8080 is open")


@pytest.mark.parametrize(
    "port",
    [
        "'127.0.0.1:8000:8000'",
        "{target: 80, published: 8080, host_ip: 127.0.0.1}",
        "{target: 80, host_ip: 127.0.0.1}",
    ],
)
def test_ports_kept_on_this_machine_are_not_flagged(tmp_path, port):
    assert warnings_for(tmp_path, f"    ports: [{port}]\n") == ()
