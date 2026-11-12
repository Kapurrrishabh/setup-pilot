"""The shape of the AI's answers. Groq's strict mode makes the model follow it exactly."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

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


class _Strict(BaseModel):
    # No defaults: strict mode needs every field listed as required.
    model_config = ConfigDict(extra="forbid")


class Runtime(_Strict):
    tool: ToolId
    constraint: str | None
    source: str


class ToolNeed(_Strict):
    tool: ToolId
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
    kind: Literal[
        "postgres", "mysql", "mongodb", "redis", "rabbitmq", "elasticsearch", "kafka", "other"
    ]
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
    languages: list[Language]
    frameworks: list[str]
    package_manager: Manager
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
    missing_tools: list[ToolId]
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
