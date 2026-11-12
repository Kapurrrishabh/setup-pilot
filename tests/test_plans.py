import json
from pathlib import Path

import pytest

from setuppilot.errors import StackNotFound
from setuppilot.guardrails.runner import CommandOutput
from setuppilot.setup import plans
from setuppilot.tools.detect import StackCandidate


def cand(stack, manager, evidence=()):
    return StackCandidate(stack, manager, tuple(evidence), None)


def texts(plan):
    return [p.text for p in plan]


# The tests below are for macOS and Linux unless they name a target, so they give the same
# answer on every system that runs them.
def install_plan(candidate, top, is_project, target="posix"):
    return plans.install_plan(candidate, top, is_project, target)


def code_checks(candidate, files, target="posix"):
    return plans.code_checks(candidate, files, target)


def quiet_checks(candidate, repo, run, target="posix"):
    return plans.quiet_checks(candidate, repo, run, target)


@pytest.mark.parametrize(
    ("candidate", "top", "expected"),
    [
        (cand("python", "pip", ["requirements.txt"]), {"requirements.txt"},
         ["uv venv .venv", "uv pip install --python .venv -r requirements.txt"]),
        (cand("python", "pip", ["requirements-dev.txt"]), set(),
         ["uv venv .venv", "uv pip install --python .venv -r requirements-dev.txt"]),
        (cand("python", "pip", ["pyproject.toml"]), {"pyproject.toml"},
         ["uv venv .venv", "uv pip install --python .venv -e ."]),
        (cand("python", "uv", ["uv.lock"]), set(), ["uv sync"]),
        (cand("python", "poetry", ["poetry.lock"]), set(), ["poetry install"]),
        (cand("python", "conda", ["environment.yml"]), set(),
         ["conda env create -f environment.yml -p ./.conda-env"]),
        (cand("node", "npm", ["package-lock.json"]), {"package-lock.json"}, ["npm ci"]),
        (cand("node", "npm", ["package.json"]), {"package.json"}, ["npm install"]),
        (cand("node", "pnpm", ["pnpm-lock.yaml"]), set(), ["pnpm install"]),
        (cand("go", "go", ["go.mod"]), set(), ["go mod download"]),
        (cand("rust", "cargo", ["Cargo.toml"]), set(), ["cargo fetch"]),
        (cand("ruby", "bundler", ["Gemfile"]), set(), ["bundle install"]),
        (cand("java", "maven", ["pom.xml", "mvnw"]), set(),
         ["./mvnw dependency:resolve -Dmaven.repo.local=.m2/repository"]),
        (cand("java", "gradle", ["build.gradle"]), set(), ["gradle dependencies"]),
        (cand("docker", "compose", ["compose.yaml"]), set(),
         ["docker compose -f compose.yaml up -d --build --wait"]),
        (cand("dotnet", "dotnet", ["All.sln", "global.json"]), set(), ["dotnet restore All.sln"]),
        (cand("php", "composer", ["composer.json"]), set(), ["composer install --no-interaction"]),
        (cand("dart", "dart", ["pubspec.yaml"]), set(), ["dart pub get"]),
        (cand("dart", "flutter", ["pubspec.yaml"]), set(), ["flutter pub get"]),
    ],
)  # fmt: skip
def test_install_plan_for_each_stack(candidate, top, expected):
    is_project = candidate.manager == "pip" and "pyproject.toml" in top
    assert texts(install_plan(candidate, frozenset(top), is_project)) == expected


def test_stack_without_a_plan_says_so_plainly():
    with pytest.raises(StackNotFound, match="cannot set up a docker project with dockerfile"):
        install_plan(cand("docker", "dockerfile", ["Dockerfile"]), frozenset(), False)


@pytest.mark.parametrize(
    ("candidate", "files", "expected"),
    [
        (cand("python", "pip"), ("src/mypkg/__init__.py", "tests/__init__.py"),
         [".venv/bin/python -c 'import mypkg'"]),
        (cand("python", "pip"), ("tests/__init__.py", "app.py"), []),
        (cand("go", "go"), (), ["go build ./..."]),
        (cand("rust", "cargo"), (), ["cargo build"]),
        (cand("java", "gradle", ["build.gradle", "gradlew"]), (), ["./gradlew assemble"]),
        (cand("node", "npm"), (), []),
        (cand("ruby", "bundler"), (), ["bundle check"]),
        (cand("dotnet", "dotnet", ["App.csproj"]), (), ["dotnet build App.csproj --no-restore"]),
        (cand("php", "composer"), (), []),
    ],
)  # fmt: skip
def test_checks_that_run_project_code_for_each_stack(candidate, files, expected):
    assert texts(code_checks(candidate, files)) == expected


@pytest.mark.parametrize("is_restored", [True, False])
def test_dotnet_check_looks_for_restored_packages_in_any_project_folder(tmp_path, is_restored):
    if is_restored:
        (tmp_path / "src/App/obj").mkdir(parents=True)
        (tmp_path / "src/App/obj/project.assets.json").write_text("{}", encoding="utf-8")
    (check,) = quiet_checks(cand("dotnet", "dotnet", ["All.sln"]), tmp_path, lambda t: output())
    assert check.is_passed is is_restored


def test_composer_check_needs_the_autoloader_and_fitting_php_extensions(tmp_path):
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor/autoload.php").write_text("<?php", encoding="utf-8")
    ran = []

    def run(text):
        ran.append(text)
        return output(1, "ext-intl is missing")

    checks = quiet_checks(cand("php", "composer"), tmp_path, run)
    assert [c.is_passed for c in checks] == [True, False]
    assert ran == ["composer check-platform-reqs --no-plugins"]


@pytest.mark.parametrize("manager", ["dart", "flutter"])
def test_dart_check_needs_the_package_config(tmp_path, manager):
    (check,) = quiet_checks(cand("dart", manager), tmp_path, lambda t: output())
    assert (check.is_passed, ".dart_tool/package_config.json" in check.detail) == (False, True)


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [(cand("java", "maven", ["pom.xml", "mvnw", "mvnw.cmd"]),
      ["./mvnw.cmd dependency:resolve -Dmaven.repo.local=.m2/repository"]),
     (cand("java", "maven", ["pom.xml", "mvnw"]),
      ["mvn dependency:resolve -Dmaven.repo.local=.m2/repository"]),
     (cand("java", "gradle", ["build.gradle", "gradlew", "gradlew.bat"]),
      ["./gradlew.bat dependencies"])],
)  # fmt: skip
def test_windows_uses_the_wrapper_made_for_windows(candidate, expected):
    assert texts(install_plan(candidate, frozenset(), False, "windows")) == expected


def test_windows_import_check_uses_the_windows_venv_layout():
    checks = code_checks(cand("python", "pip"), ("mypkg/__init__.py",), "windows")
    assert texts(checks) == [".venv/Scripts/python.exe -c 'import mypkg'"]


def output(exit_code=0, text=""):
    return CommandOutput(("x",), exit_code, text, False, 1, False)


def test_python_checks_need_the_venv_and_a_clean_pip_check(tmp_path):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    checks = quiet_checks(cand("python", "pip"), tmp_path, lambda text: output(1, "conflict!"))
    assert [(c.name, c.is_passed) for c in checks] == [
        ("Python environment exists", True),
        ("Installed packages fit together", False),
    ]


@pytest.mark.parametrize(
    ("manager", "made", "expected"),
    [("pip", ".venv/Scripts/python.exe", True), ("pip", ".venv/bin/python", False),
     ("conda", ".conda-env/python.exe", True), ("conda", ".conda-env/bin/python", False)],
)  # fmt: skip
def test_windows_environment_check_looks_where_windows_puts_python(
    tmp_path, manager, made, expected
):
    (tmp_path / made).parent.mkdir(parents=True)
    (tmp_path / made).write_text("", encoding="utf-8")
    checks = quiet_checks(cand("python", manager), tmp_path, lambda text: output(), "windows")
    assert checks[0].is_passed is expected


def test_missing_node_modules_fails_the_node_check(tmp_path):
    checks = quiet_checks(cand("node", "pnpm"), tmp_path, lambda text: output())
    assert [(c.name, c.is_passed) for c in checks] == [("node_modules exists", False)]


def test_yarn_plug_n_play_install_passes_without_node_modules(tmp_path):
    package = {"packageManager": "yarn@4.5.0", "dependencies": {"is-number": "7.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(package))
    for name in ("yarn.lock", ".yarnrc.yml", ".pnp.cjs"):
        (tmp_path / name).write_text("")
    (check,) = quiet_checks(cand("node", "yarn"), tmp_path, lambda text: output())
    assert check.is_passed


def ps_lines(*rows):
    return "\n".join(json.dumps(r) for r in rows)


COMPOSE = "services:\n  web:\n    image: x\n"


def compose_check(tmp_path, ps_output, exit_code=0):
    (tmp_path / "compose.yaml").write_text(COMPOSE)
    (check,) = quiet_checks(
        cand("docker", "compose", ["compose.yaml"]),
        tmp_path,
        lambda t: output(exit_code, ps_output),
    )
    return check


@pytest.mark.parametrize(
    ("ps_output", "is_passed"),
    [
        (ps_lines({"Service": "web", "State": "running", "Health": ""}), True),
        (json.dumps([{"Service": "web", "State": "running", "Health": "healthy"}]), True),
        (
            "level=warning msg=version is obsolete\n"
            + ps_lines({"Service": "web", "State": "running"}),
            True,
        ),
        (ps_lines({"Service": "web", "State": "running", "Health": "unhealthy"}), False),
        (ps_lines({"Service": "web", "State": "exited", "Health": "", "ExitCode": 1}), False),
        ("", False),
        ("not json", False),
    ],
)
def test_compose_check_needs_every_service_running_and_healthy(tmp_path, ps_output, is_passed):
    assert compose_check(tmp_path, ps_output).is_passed is is_passed


def test_compose_check_fails_when_a_service_has_no_container(tmp_path):
    (tmp_path / "compose.yaml").write_text(COMPOSE + "  db:\n    image: postgres\n")
    out = ps_lines({"Service": "web", "State": "running"})
    (check,) = quiet_checks(
        cand("docker", "compose", ["compose.yaml"]), tmp_path, lambda t: output(0, out)
    )
    assert not check.is_passed and "db" in check.detail


def test_compose_check_passes_when_a_one_shot_job_finished_with_exit_0(tmp_path):
    (tmp_path / "compose.yaml").write_text(COMPOSE + "  migrate:\n    image: x\n")
    out = ps_lines(
        {"Service": "web", "State": "running", "Health": ""},
        {"Service": "migrate", "State": "exited", "Health": "", "ExitCode": 0},
    )
    (check,) = quiet_checks(
        cand("docker", "compose", ["compose.yaml"]), tmp_path, lambda t: output(0, out)
    )
    assert check.is_passed


def test_compose_check_fails_when_docker_compose_ps_fails(tmp_path):
    check = compose_check(tmp_path, "daemon down", exit_code=1)
    assert (check.is_passed, check.detail) == (False, "daemon down")


def test_compose_status_asks_for_stopped_containers_too(tmp_path):
    (tmp_path / "compose.yaml").write_text(COMPOSE)
    asked = []
    quiet_checks(
        cand("docker", "compose", ["compose.yaml"]), tmp_path, lambda t: asked.append(t) or output()
    )
    assert asked == ["docker compose -f compose.yaml ps --all --format json"]


@pytest.mark.parametrize(
    ("evidence", "top", "expected"),
    [
        (["requirements-dev.txt", "requirements.txt"], {"requirements.txt", "requirements-dev.txt"},
         "uv pip install --python .venv -r requirements.txt"),
        (["pyproject.toml", "requirements-dev.txt"], {"pyproject.toml", "requirements-dev.txt"},
         "uv pip install --python .venv -r requirements-dev.txt -e ."),
        (["requirements/base.txt", "requirements/dev.txt"], set(),
         "uv pip install --python .venv -r requirements/base.txt"),
    ],
)  # fmt: skip
def test_pip_installs_the_main_requirements_and_the_project(evidence, top, expected):
    plan = install_plan(cand("python", "pip", evidence), frozenset(top), "pyproject.toml" in top)
    assert plan[1].text == expected


def test_bundle_check_runs_the_gemfile_so_it_needs_approval():
    assert texts(code_checks(cand("ruby", "bundler"), ())) == ["bundle check"]
    assert quiet_checks(cand("ruby", "bundler"), Path("/nowhere"), lambda t: output()) == []


def test_node_project_without_dependencies_passes_without_node_modules(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "x"}')
    (check,) = quiet_checks(cand("node", "npm"), tmp_path, lambda t: output())
    assert check.is_passed


def test_venv_python_that_is_a_link_into_a_container_still_counts(tmp_path, can_symlink):
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").symlink_to("/usr/local/bin/python-in-container")
    checks = quiet_checks(cand("python", "uv"), tmp_path, lambda t: output())
    assert checks[0].is_passed


def test_pyproject_with_only_tool_settings_is_not_installed_as_a_project(tmp_path):
    from setuppilot.setup.plans import is_installable_project

    (tmp_path / "pyproject.toml").write_text("[tool.black]\nline-length = 100\n")
    (tmp_path / "setup.cfg").write_text("[flake8]\n")
    assert not is_installable_project(tmp_path)


@pytest.mark.parametrize(
    "files",
    [{"setup.py": ""}, {"pyproject.toml": "[project]\nname = 'x'\n"},
     {"pyproject.toml": "[build-system]\nrequires = ['hatchling']\n"}],
)  # fmt: skip
def test_real_python_projects_are_installable(tmp_path, files):
    from setuppilot.setup.plans import is_installable_project

    for name, text in files.items():
        (tmp_path / name).write_text(text)
    assert is_installable_project(tmp_path)


def test_python_repo_with_nothing_to_install_says_so():
    with pytest.raises(StackNotFound, match="nothing to install"):
        install_plan(cand("python", "pip", ["setup.cfg"]), frozenset({"setup.cfg"}), False)


def test_compose_services_behind_profiles_are_not_expected_to_run(tmp_path):
    compose = "services:\n  web:\n    image: x\n  debug:\n    image: y\n    profiles: [debug]\n"
    (tmp_path / "compose.yaml").write_text(compose)
    out = ps_lines({"Service": "web", "State": "running"})
    (check,) = quiet_checks(
        cand("docker", "compose", ["compose.yaml"]), tmp_path, lambda t: output(0, out)
    )
    assert check.is_passed
