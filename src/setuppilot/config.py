"""Load and check config.yaml. Secrets come from .env, not from this file."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt, model_validator


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
    max_readme_chars: PositiveInt
    plan_max_tokens: PositiveInt
    report_max_tokens: PositiveInt


class ChatConfig(_Strict):
    embedding_model: str
    embedding_dimensions: PositiveInt
    repos_dir: Path
    chunk_chars: PositiveInt
    chunk_overlap_chars: int = Field(ge=0)
    max_file_bytes: PositiveInt
    max_chunks_per_repo: PositiveInt
    top_k: PositiveInt
    max_chunks_per_file: PositiveInt
    min_similarity: float = Field(ge=-1, le=1)
    answer_max_tokens: PositiveInt
    history_turns: int = Field(ge=0)

    @model_validator(mode="after")
    def _overlap_fits(self) -> "ChatConfig":
        if self.chunk_overlap_chars * 2 >= self.chunk_chars:
            raise ValueError(
                f"chat.chunk_overlap_chars ({self.chunk_overlap_chars}) must be less than half "
                f"of chat.chunk_chars ({self.chunk_chars}). Lower it in config.yaml."
            )
        return self


# This app runs commands on your machine, so the web server never listens beyond it.
WebHost = Literal["127.0.0.1", "localhost", "::1"]


class WebConfig(_Strict):
    host: WebHost
    port: int = Field(ge=1024, le=65535)
    max_request_bytes: PositiveInt
    posts_per_minute: PositiveInt


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


def load_settings(config_path: Path) -> Settings:
    return Settings.model_validate(yaml.safe_load(config_path.read_text()))
