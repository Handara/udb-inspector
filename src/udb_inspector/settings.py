"""User preferences in ~/.config/udb-inspector/settings.toml."""

from __future__ import annotations

import tomllib

import tomlkit

from .connections import global_dir

SETTINGS_FILE = "settings.toml"


def load_settings() -> dict:
    path = global_dir() / SETTINGS_FILE
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}  # preferences are never worth refusing to start over


def save_setting(key: str, value: object) -> None:
    path = global_dir() / SETTINGS_FILE
    try:
        doc = tomlkit.parse(path.read_text()) if path.is_file() else tomlkit.document()
    except tomlkit.exceptions.ParseError:
        doc = tomlkit.document()
    doc[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc))
