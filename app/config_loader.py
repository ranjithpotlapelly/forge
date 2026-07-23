"""Composition root helper: load config.yaml and expand ${ENV} refs from .env.

This is the ONLY place that knows environment-variable names. Everything else
receives plain values, so no module is coupled to how config is stored.
"""
from __future__ import annotations
import os
import re
from pathlib import Path
import yaml
from dotenv import load_dotenv

_ENV_PATTERN = re.compile(r"\$\{([^}{]+)\}")

def _expand(value):
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value

def load_config(path: str | Path = "config.yaml") -> dict:
    load_dotenv()  # read .env if present
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return _expand(raw)
