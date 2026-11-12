"""Load and check config.yaml. Secrets come from .env, not from this file."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    ValidationError,
    model_validator,
)

from setuppilot.errors import ConfigInvalid


class _Strict(BaseModel):
    # A typo in config.yaml must fail at startup, not be ignored.
    model_config = ConfigDict(extra="forbid", frozen=True)


class LoggingConfig(_Strict):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"]
    file: Path
    max_file_mb: PositiveInt
    backup_count: PositiveInt


class ModelLimits(_Strict):
    requests_per_minute: PositiveInt
    requests_per_day: PositiveInt
    tokens_per_minute: PositiveInt
    tokens_per_day: PositiveInt


class GroqModel(_Strict):
    name: str = Field(min_length=1)
    # null for models that do not reason; Groq rejects the setting for them.
    reasoning_effort: Literal["low", "medium", "high"] | None
    limits: ModelLimits


class GroqConfig(_Strict):
    base_url: str
    timeout_seconds: PositiveFloat
    max_attempts: PositiveInt
    backoff_base_seconds: PositiveFloat
    backoff_max_seconds: PositiveFloat
    big: GroqModel
    small: GroqModel

    @model_validator(mode="after")
    def _models_differ(self) -> "GroqConfig":
        if self.big.name == self.small.name:
            raise ValueError(
                f"groq.big and groq.small are both '{self.big.name}'. Groq limits are per model, "
                "so pick two different models in config.yaml."
            )
        return self


class BudgetConfig(_Strict):
    tokens_per_run: PositiveInt
    tokens_per_day: PositiveInt

    @model_validator(mode="after")
    def _run_fits_in_day(self) -> "BudgetConfig":
        if self.tokens_per_run > self.tokens_per_day:
            raise ValueError(
                f"budget.tokens_per_run ({self.tokens_per_run}) is larger than "
                f"budget.tokens_per_day ({self.tokens_per_day}). Lower it in config.yaml."
            )
        return self


class DatabaseConfig(_Strict):
    pool_max_size: PositiveInt
    connect_timeout_seconds: PositiveFloat


class RepoConfig(_Strict):
    max_size_mb: PositiveInt
    clone_timeout_seconds: PositiveFloat
    max_tree_chars: PositiveInt


class RunnerConfig(_Strict):
    timeout_seconds: PositiveFloat
    max_output_chars: PositiveInt


class AgentConfig(_Strict):
    max_steps: PositiveInt
    report_max_tokens: PositiveInt
    # The kill switch: false turns off the AI analysis and diagnosis together.
    use_ai_planning: bool
    max_evidence_chars: PositiveInt
    analyze_max_tokens: PositiveInt
    diagnose_max_tokens: PositiveInt
    max_install_rounds: PositiveInt
    max_fixes_per_step: PositiveInt
    max_fixes_per_run: PositiveInt


class ChatConfig(_Strict):
    embedding_model: str
    embedding_dimensions: PositiveInt
    repos_dir: Path
    max_file_bytes: PositiveInt
    max_chunks_per_repo: PositiveInt
    top_k: PositiveInt
    max_chunks_per_file: PositiveInt
    min_similarity: float = Field(ge=-1, le=1)
    answer_max_tokens: PositiveInt
    history_turns: int = Field(ge=0)


# This app runs commands on your machine, so the web server never listens beyond it.
WebHost = Literal["127.0.0.1", "localhost"]


class WebConfig(_Strict):
    host: WebHost
    port: int = Field(ge=1024, le=65535)
    max_request_bytes: PositiveInt
    posts_per_minute: PositiveInt


class GitHubApp(_Strict):
    """The GitHub App you registered for sign-in. Its Client ID is public, so it can live here."""

    client_id: str = Field(pattern=r"^[A-Za-z0-9.]+$")
    app_slug: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=34)

    @model_validator(mode="after")
    def _not_the_app_id(self) -> "GitHubApp":
        if self.client_id.isdigit():
            raise ValueError(
                f"github.app.client_id is '{self.client_id}', a number, so it looks like the App "
                "ID. Copy the Client ID (it starts with Iv) from your GitHub App's page instead."
            )
        return self

    @property
    def install_url(self) -> str:
        return f"https://github.com/apps/{self.app_slug}/installations/new"

    @property
    def settings_url(self) -> str:
        return f"https://github.com/settings/apps/{self.app_slug}"


class GitHubConfig(_Strict):
    # None turns sign-in off. Public repos are still read from GitHub without it.
    app: GitHubApp | None
    max_repo_pages: PositiveInt
    max_tarball_mb: PositiveInt


class Settings(_Strict):
    logging: LoggingConfig
    groq: GroqConfig
    budget: BudgetConfig
    database: DatabaseConfig
    repo: RepoConfig
    runner: RunnerConfig
    agent: AgentConfig
    chat: ChatConfig
    web: WebConfig
    github: GitHubConfig


def load_settings(config_path: Path) -> Settings:
    try:
        return Settings.model_validate(yaml.safe_load(config_path.read_text(encoding="utf-8")))
    except FileNotFoundError as error:
        raise ConfigInvalid(
            f"{config_path} was not found.",
            details="Run setuppilot from the project folder, where config.yaml is.",
        ) from error
    except yaml.YAMLError as error:
        raise ConfigInvalid(f"{config_path} is not valid YAML.", details=str(error)) from error
    except ValidationError as error:
        first = error.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "the top level"
        raise ConfigInvalid(
            f"{config_path} has a mistake at {where}: {first['msg']}.", details=str(error)
        ) from error
