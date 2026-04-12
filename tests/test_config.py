"""Tests for axon config loading."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from axon.config import generate_config, generate_env_template, load_config
from axon.exceptions import ConfigError


def test_load_valid_config(tmp_path: Path) -> None:
    config_data = {
        "projectName": "test-project",
        "provider": "ionet",
        "runtime": "nodejs",
        "entryPoint": "src/index.py",
    }
    (tmp_path / "axon.json").write_text(json.dumps(config_data))

    config = load_config(tmp_path)
    assert config.project_name == "test-project"
    assert config.provider == "ionet"


def test_load_missing_config(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="axon.json not found"):
        load_config(tmp_path)


def test_load_invalid_json(tmp_path: Path) -> None:
    (tmp_path / "axon.json").write_text("{ invalid json }")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(tmp_path)


def test_generate_config() -> None:
    result = generate_config("my-project", "akash")
    data = json.loads(result)
    assert data["projectName"] == "my-project"
    assert data["provider"] == "akash"


def test_generate_env_template_includes_axon_key() -> None:
    env = generate_env_template("ionet")
    assert "AXON_SECRET_KEY" in env
    assert "IONET_API_KEY" in env
