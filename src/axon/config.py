"""Config loader for axon.json project files."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from axon.exceptions import ConfigError
from axon.types import ProviderName, RuntimeType


CONFIG_FILENAME = "axon.json"


class AxonConfig(BaseModel):
    """Parsed axon.json configuration."""

    project_name: str = Field(alias="projectName")
    provider: ProviderName
    runtime: RuntimeType = RuntimeType.NODEJS
    entry_point: str = Field(default="src/index.ts", alias="entryPoint")
    env: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


def load_config(cwd: str | Path | None = None) -> AxonConfig:
    """Load and validate axon.json from the given directory (default: cwd)."""

    cwd = Path(cwd) if cwd else Path.cwd()
    config_path = cwd / CONFIG_FILENAME

    if not config_path.exists():
        raise ConfigError(
            f"{CONFIG_FILENAME} not found in {cwd}. "
            "Run `axon init` to create one."
        )

    try:
        raw = config_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{CONFIG_FILENAME} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{CONFIG_FILENAME} must be a JSON object")

    return AxonConfig.model_validate(data)


def generate_config(
    project_name: str,
    provider: ProviderName,
    runtime: RuntimeType = RuntimeType.NODEJS,
    entry_point: str = "src/index.ts",
) -> str:
    """Generate axon.json content as a JSON string."""

    config = {
        "projectName": project_name,
        "provider": provider,
        "runtime": runtime.value,
        "entryPoint": entry_point,
    }
    return json.dumps(config, indent=2)


def generate_env_template(provider: ProviderName) -> str:
    """Generate a .env template for the given provider."""

    lines = [
        "# Run: axon auth to fill these in interactively.",
        f"AXON_SECRET_KEY=",
    ]

    provider_vars: dict[ProviderName, list[str]] = {
        "ionet": ["IONET_API_KEY="],
        "akash": ["AKASH_MNEMONIC=", "AKASH_KEY_NAME=axon"],
        "acurast": ["ACURAST_MNEMONIC="],
        "fluence": ["FLUENCE_PRIVATE_KEY="],
        "koii": ["KOII_WALLET_PATH=~/.config/koii/id.json"],
    }

    if provider in provider_vars:
        lines.append("")
        lines.extend(provider_vars[provider])

    return os.linesep.join(lines)
