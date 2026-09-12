"""Explicit local CLI for exporting Harness configuration artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..database import create_database_engine
from ..models import ModelRegistry
from .cline import generate_cline_config
from .opencode import generate_opencode_config


def export_config(
    *,
    kind: str,
    database_url: str,
    output: Path,
    base_url: str = "http://127.0.0.1:8020/v1",
    model: str | None = None,
    provider_id: str = "personal-ai-gateway",
    api_key_env: str = "AIPG_GATEWAY_API_KEY",
) -> Path:
    """Write one new config file without embedding or reading the API-key value."""

    records = ModelRegistry(create_database_engine(database_url)).list_models()
    payload: dict[str, Any]
    if kind == "opencode":
        payload = generate_opencode_config(
            records,
            base_url=base_url,
            provider_id=provider_id,
            default_model=model,
            api_key_env=api_key_env,
        )
    elif kind == "cline":
        payload = generate_cline_config(
            records,
            base_url=base_url,
            model=model,
            api_key_env=api_key_env,
        )
    else:
        raise ValueError("Unsupported Harness config kind.")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output.open("x", encoding="utf-8") as config_file:
            json.dump(payload, config_file, ensure_ascii=False, indent=2)
            config_file.write("\n")
    except FileExistsError as error:
        raise FileExistsError("Refusing to overwrite an existing config file.") from error
    return output.resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a local Harness configuration.")
    parser.add_argument("kind", choices=("opencode", "cline"))
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8020/v1")
    parser.add_argument("--model")
    parser.add_argument("--provider-id", default="personal-ai-gateway")
    parser.add_argument("--api-key-env", default="AIPG_GATEWAY_API_KEY")
    arguments = parser.parse_args()
    path = export_config(
        kind=arguments.kind,
        database_url=arguments.database_url,
        output=arguments.output,
        base_url=arguments.base_url,
        model=arguments.model,
        provider_id=arguments.provider_id,
        api_key_env=arguments.api_key_env,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
