"""The shape of the AI's answers. Groq's strict mode makes the model follow it exactly."""

from collections.abc import Callable
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, BeforeValidator, ConfigDict, WithJsonSchema

from setuppilot.llm.transport import JsonSchema

Manager = Literal[
    "pip", "uv", "poetry", "pipenv", "pdm", "conda", "npm", "yarn", "pnpm", "bun", "go", "cargo",
    "bundler", "maven", "gradle", "compose", "dockerfile", "dotnet", "composer", "dart",
    "flutter", "none", "other",
]  # fmt: skip
ToolId = Literal[
    "uv", "python", "poetry", "pipenv", "pdm", "conda", "node", "npm", "yarn", "pnpm", "bun",
    "go", "cargo", "ruby", "bundle", "java", "mvn", "gradle", "docker", "docker compose", "git",
    "dotnet", "php", "composer", "dart", "flutter", "other",
]  # fmt: skip
Language = Literal[
    "python", "javascript", "typescript", "go", "rust", "ruby", "java", "kotlin", "csharp",
    "php", "dart", "elixir", "swift", "c", "cpp", "other",
]  # fmt: skip


# Names the model often writes for the same thing; anything else unknown becomes "other".
_SYNONYMS = {
    "maven": "mvn", "apache maven": "mvn", "nodejs": "node", "node.js": "node",
    "golang": "go", "rust": "cargo", "rustc": "cargo", "rustup": "cargo", "python3": "python",
    "docker-compose": "docker compose", "compose": "docker compose", "bundler": "bundle",
    "gem": "ruby", "jdk": "java", "openjdk": "java", "gradlew": "gradle", ".net": "dotnet",
    "dotnet-sdk": "dotnet", "js": "javascript", "ts": "typescript", "c#": "csharp",
    "c++": "cpp", "postgresql": "postgres", "mongo": "mongodb", "elastic": "elasticsearch",
}  # fmt: skip
_MANAGER_SYNONYMS = {"mvn": "maven", "bundle": "bundler", "docker compose": "compose",
                     "docker-compose": "compose", "pip-tools": "pip"}  # fmt: skip


def _pick_from(names: Any, synonyms: dict[str, str]) -> Callable[[Any], Any]:
    """Groq's strict mode refuses a whole answer for one name off an enum, so these fields are
    plain strings to the model, and the name is mapped to the fixed list here instead."""
    known = set(get_args(names))

    def pick(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        name = value.strip().lower()
        # A name already on this field's list wins: "maven" is a manager here, not the tool mvn.
        name = name if name in known else synonyms.get(name, name)
        return name if name in known else "other"

    return pick


ServiceKind = Literal[
    "postgres", "mysql", "mongodb", "redis", "rabbitmq", "elasticsearch", "kafka", "other"
]  # fmt: skip
_AS_TEXT = WithJsonSchema({"type": "string"})
ToolName = Annotated[ToolId, BeforeValidator(_pick_from(ToolId, _SYNONYMS)), _AS_TEXT]
ManagerName = Annotated[
    Manager, BeforeValidator(_pick_from(Manager, {**_SYNONYMS, **_MANAGER_SYNONYMS})), _AS_TEXT
]
LanguageName = Annotated[Language, BeforeValidator(_pick_from(Language, _SYNONYMS)), _AS_TEXT]
ServiceName = Annotated[ServiceKind, BeforeValidator(_pick_from(ServiceKind, _SYNONYMS)), _AS_TEXT]


class _Strict(BaseModel):
    # No defaults: strict mode needs every field listed as required.
    model_config = ConfigDict(extra="forbid")


class Runtime(_Strict):
    tool: ToolName
    constraint: str | None
    source: str


class ToolNeed(_Strict):
    tool: ToolName
    name: str
    is_required: bool
    version: str | None
    reason: str


class EnvNeed(_Strict):
    name: str
    kind: Literal["env_var", "config_file"]
    source: str
    is_required: bool


class Service(_Strict):
    kind: ServiceName
    version: str | None
    how: Literal["compose", "manual", "external"]


class Step(_Strict):
    phase: Literal["prepare", "install", "build", "verify"]
    command: str
    reason: str
    is_manual: bool


class Project(_Strict):
    path: str
    name: str
    role: Literal["app", "library", "cli", "service", "docs", "example", "other"]
    languages: list[LanguageName]
    frameworks: list[str]
    package_manager: ManagerName
    runtime: Runtime | None
    tools: list[ToolNeed]
    env: list[EnvNeed]
    services: list[Service]
    steps: list[Step]
    start_command: str | None
    evidence: list[str]


class RepoAnalysis(_Strict):
    summary: str
    projects: list[Project]
    notes: list[str]


class FixCommand(_Strict):
    command: str
    reason: str


class Diagnosis(_Strict):
    category: Literal[
        "missing_tool", "wrong_tool_version", "network", "dependency_conflict",
        "build_toolchain", "permissions", "needs_config", "repo_problem", "unknown",
    ]  # fmt: skip
    explanation: str
    needs_user: bool
    user_action: str | None
    missing_tools: list[ToolName]
    commands: list[FixCommand]


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The model's JSON Schema in the form Groq's strict mode takes: no titles, and every
    property required, with no others allowed."""
    schema = model.model_json_schema()
    _check_strict(schema)
    return schema


def _check_strict(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _check_strict(item)
        return
    if not isinstance(node, dict):
        return
    if isinstance(node.get("title"), str):
        del node["title"]
    is_open = node.get("additionalProperties") is not False
    if node.get("type") == "object" and (
        node.get("required") != list(node.get("properties", {})) or is_open
    ):
        raise ValueError(f"Strict mode needs every property required and no others: {node}")
    for value in node.values():
        _check_strict(value)


ANALYSIS_FORMAT = JsonSchema("repo_analysis", strict_schema(RepoAnalysis))
DIAGNOSIS_FORMAT = JsonSchema("diagnosis", strict_schema(Diagnosis))
