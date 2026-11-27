"""Read one YAML experiment definition; resolve base plus a named override."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "strategy_config.yaml"


def deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_config(path: str | Path = DEFAULT_CONFIG) -> dict:
    path = Path(path).resolve()
    source = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(source, dict) or not isinstance(source.get("base"), dict):
        raise ValueError("YAML must contain a mapping named base.")
    if not isinstance(source.get("variants", {}), dict):
        raise ValueError("variants must be a mapping of names to overrides.")
    return source


def list_variants(source: dict) -> list[str]:
    return ["base", *[name for name in source.get("variants", {}) if name != "base"]]


def resolve_config(source: dict, variant: str = "base") -> dict:
    variants = source.get("variants", {})
    if variant != "base" and variant not in variants:
        raise ValueError(f"Unknown variant {variant!r}; choose {list_variants(source)}")
    override = variants.get(variant, {})
    if not isinstance(override, dict):
        raise ValueError(f"Variant {variant!r} must be a mapping.")
    config = deep_merge(source["base"], override)
    config["variant_name"] = variant
    return config


def config_hash(config: dict) -> str:
    return hashlib.sha256(yaml.safe_dump(config, sort_keys=True, allow_unicode=True).encode()).hexdigest()


def project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else HERE / path
