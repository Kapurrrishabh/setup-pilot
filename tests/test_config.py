from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from setuppilot.config import load_settings

REAL_CONFIG = Path(__file__).parents[1] / "config.yaml"


def write_config(tmp_path: Path, change) -> Path:
    data = yaml.safe_load(REAL_CONFIG.read_text())
    change(data)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_real_config_loads():
    load_settings(REAL_CONFIG)


def test_unknown_key_is_rejected(tmp_path):
    path = write_config(tmp_path, lambda d: d["groq"].update(bigg={}))
    with pytest.raises(ValidationError, match="bigg"):
        load_settings(path)


def test_zero_rate_limit_is_rejected(tmp_path):
    path = write_config(tmp_path, lambda d: d["groq"]["small"]["limits"].update(tokens_per_day=0))
    with pytest.raises(ValidationError, match="tokens_per_day"):
        load_settings(path)


def test_same_model_for_big_and_small_is_rejected(tmp_path):
    path = write_config(
        tmp_path, lambda d: d["groq"]["small"].update(name=d["groq"]["big"]["name"])
    )
    with pytest.raises(ValidationError, match="limits are per model"):
        load_settings(path)


def test_run_budget_larger_than_day_budget_is_rejected(tmp_path):
    path = write_config(tmp_path, lambda d: d["budget"].update(tokens_per_run=10, tokens_per_day=5))
    with pytest.raises(ValidationError, match="tokens_per_run"):
        load_settings(path)
