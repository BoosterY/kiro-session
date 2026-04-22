"""Configuration management for kiro-session."""
from pathlib import Path
import yaml

CONFIG_PATH = Path.home() / ".kiro" / "session-config.yml"

DEFAULTS = {
    "llm": {
        "provider": "auto",
        "auto_enrich": True,
    },
    "privacy": {
        "exclude_dirs": [],
        "exclude_sessions": [],
    },
    "resume": {
        "extra_args": "",
        "ui": "",
    },
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            user = yaml.safe_load(f) or {}
    else:
        user = {}
    return _merge(DEFAULTS, user)


def save_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)


def get(cfg: dict, dotted_key: str):
    """Get a value by dotted key, e.g. 'llm.provider'."""
    keys = dotted_key.split(".")
    node = cfg
    for k in keys:
        if isinstance(node, dict) and k in node:
            node = node[k]
        else:
            return None
    return node


def set_value(cfg: dict, dotted_key: str, value) -> dict:
    """Set a value by dotted key. Auto-converts booleans and lists."""
    keys = dotted_key.split(".")
    node = cfg
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = _coerce(value) if isinstance(value, str) else value
    return cfg


def _coerce(value: str):
    if value.lower() in ("true", "yes"):
        return True
    if value.lower() in ("false", "no"):
        return False
    try:
        return int(value)
    except ValueError:
        return value


def _merge(defaults: dict, overrides: dict) -> dict:
    result = dict(defaults)
    for k, v in overrides.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result
