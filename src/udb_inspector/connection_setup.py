"""Add and remove saved connections, and suggest them from an env file."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from .connections import (
    FILE_TYPES,
    GLOBAL_FILE,
    PROJECT_FILE,
    Config,
    ConfigError,
    find_project_config,
    global_dir,
    load_global,
    tilde,
)

CONNECTION_TYPES = ["postgres", "mysql", "mariadb", "mssql", "oracle", "sqlite", "duckdb"]
DEFAULT_PORTS = {"postgres": "5432", "mysql": "3306", "mariadb": "3306", "mssql": "1433", "oracle": "1521"}
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# Variable-name endings for each field, longest first so PASSWORD beats PASS.
FIELD_SUFFIXES = [
    ("password", ("PASSWORD", "PASSWD", "PASS", "PWD")),
    ("user", ("USERNAME", "USER", "LOGIN")),
    ("database", ("DATABASE", "DBNAME", "NAME", "DB")),
    ("host", ("HOSTNAME", "HOST", "SERVER")),
    ("port", ("PORT",)),
]
FIELD_ORDER = ["type", "host", "port", "database", "user", "password"]
PREFIX_TYPES = {
    "PG": "postgres",
    "POSTGRES": "postgres",
    "POSTGRESQL": "postgres",
    "MYSQL": "mysql",
    "MARIADB": "mariadb",
    "MSSQL": "mssql",
    "SQLSERVER": "mssql",
    "ORACLE": "oracle",
}
DB_PREFIX_WORDS = {*PREFIX_TYPES, "DB", "DATABASE", "SQL"}
PORT_TYPES = {"5432": "postgres", "3306": "mysql", "1433": "mssql", "1521": "oracle"}

HEADERS = {
    "project": [
        "udbi connections for this project. Safe to commit: keep secrets in the env",
        "file and refer to them as ${VAR} or ${VAR:-default}.",
    ],
    "global": [
        "udbi global connections. Secrets go in .env next to this file (chmod 600)",
        "and are referred to as ${VAR}.",
    ],
}


@dataclass(frozen=True)
class Guess:
    fields: dict[str, str]
    summary: str


@dataclass(frozen=True)
class Saved:
    config: Config
    name: str

    @property
    def summary(self) -> str:
        raw = self.config.connections[self.name]
        if "url" in raw:
            return str(raw["url"])
        type_ = raw.get("type", "?")
        if type_ in FILE_TYPES:
            return f"{type_} {raw.get('database', '')}"
        host = raw.get("host", "")
        if raw.get("port"):
            host += f":{raw['port']}"
        return f"{type_} {host}/{raw.get('database', '')}"

    def target(self):
        return self.config.target(self.name)


def config_path(scope: str, cwd: Path) -> Path:
    if scope == "project":
        return find_project_config(cwd) or cwd / PROJECT_FILE
    return global_dir() / GLOBAL_FILE


def load_or_new(scope: str, cwd: Path) -> Config:
    path = config_path(scope, cwd)
    return Config.load(path, scope) if path.is_file() else Config(path, scope, path.parent / ".env", {})


def saved_connections(cwd: Path) -> tuple[list[Saved], list[str]]:
    """Project connections, then global ones, plus errors for configs that couldn't be read."""
    saved, errors = [], []
    for load in (lambda: _load_project(cwd), load_global):
        try:
            config = load()
        except ConfigError as exc:
            errors.append(str(exc))
            continue
        if config is not None:
            saved += [Saved(config, name) for name in config.connections]
    return saved, errors


def _load_project(cwd: Path) -> Config | None:
    path = find_project_config(cwd)
    return Config.load(path, "project") if path else None


def secret_variable(name: str) -> str:
    return f"UDBI_{name.upper().replace('-', '_')}_PASSWORD"


def validate(config: Config, name: str, fields: dict[str, str]) -> str | None:
    """Why these settings can't be saved, or None."""
    if not NAME_PATTERN.match(name):
        return "Name: use letters, digits, - and _ only."
    if name in config.connections:
        return f"{name} already exists in {tilde(config.path)}."
    if "url" in fields:
        if not fields["url"]:
            return "URL is empty."
        if config.scope == "project" and "${" not in fields["url"] and _has_password(fields["url"]):
            return "The URL holds a password, and .udbi.toml is meant to be committed. Put the URL in .env and use ${VAR}."
        return None
    if fields.get("type") in FILE_TYPES:
        return None if fields.get("database") else "File is empty."
    port = fields.get("port", "")
    if port and "${" not in port and not port.isdigit():
        return f"Port {port!r} is not a number."
    password = fields.get("password", "")
    if config.scope == "project" and password and "${" not in password:
        return "Give the password as ${VAR}: .udbi.toml is meant to be committed."
    return None


def _has_password(url: str) -> bool:
    try:
        return make_url(url).password is not None
    except ArgumentError:
        return False


def save_connection(config: Config, name: str, fields: dict[str, str], secrets: dict[str, str]) -> Config:
    """Add a connection to the config file (creating it if needed) and return the reloaded config."""
    path = config.path
    doc = tomlkit.parse(path.read_text()) if path.is_file() else _new_document(config.scope)
    if "connections" not in doc:
        doc.add("connections", tomlkit.table(is_super_table=True))
    if name in doc["connections"]:
        raise ConfigError(f"{name} already exists in {tilde(path)}")
    table = tomlkit.table()
    for key, value in fields.items():
        table.add(key, value)
    doc["connections"].add(name, table)
    _write(path, tomlkit.dumps(doc))
    if secrets:
        append_env(config.env_file, secrets)
    return Config.load(path, config.scope)


def delete_connection(config: Config, name: str) -> None:
    doc = tomlkit.parse(config.path.read_text())
    password = str(doc["connections"][name].get("password", ""))
    del doc["connections"][name]
    _write(config.path, tomlkit.dumps(doc))
    # Drop the password udbi saved for it, if any.
    variable = secret_variable(name)
    if config.scope == "global" and password == f"${{{variable}}}" and config.env_file.is_file():
        lines = config.env_file.read_text().splitlines(keepends=True)
        _write(config.env_file, "".join(line for line in lines if not line.startswith(f"{variable}=")), mode=0o600)


def append_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        for key, value in values.items():
            # Single quotes keep python-dotenv from expanding ${...} in the value.
            quoted = f"'{value}'" if "'" not in value else json.dumps(value)
            fh.write(f"{key}={quoted}\n")


def _new_document(scope: str) -> tomlkit.TOMLDocument:
    doc = tomlkit.document()
    for line in HEADERS[scope]:
        doc.add(tomlkit.comment(line))
    if scope == "project":
        doc.add("env_file", ".env")
    return doc


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    """Replace the file in one step so a crash can't leave half a config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(temp, path)


def guess_connections(env: dict[str, str]) -> list[Guess]:
    """Database settings that the env file's variable names suggest."""
    guesses = []
    for name, value in env.items():
        scheme = value.split("://", 1)[0].split("+", 1)[0] if "://" in value else ""
        if scheme in (*CONNECTION_TYPES, "postgresql", "sqlserver") or name.endswith(("DATABASE_URL", "DB_URL")):
            guesses.append(Guess({"url": f"${{{name}}}"}, f"url from {name}"))

    groups: dict[str, dict[str, str]] = {}
    for name in env:
        match = _field_for(name)
        if match:
            field, prefix = match
            groups.setdefault(prefix, {}).setdefault(field, name)
    for prefix, variables in groups.items():
        words = set(prefix.strip("_").split("_"))
        if "database" not in variables and not words & DB_PREFIX_WORDS:
            continue  # EMAIL_HOST, REDIS_PORT and friends
        if not {"host", "database"} & set(variables):
            continue
        type_ = next((PREFIX_TYPES[word] for word in words if word in PREFIX_TYPES), None)
        if type_ is None and "port" in variables:
            type_ = PORT_TYPES.get(env[variables["port"]])
        fields = {"type": type_} if type_ else {}
        fields.update({field: f"${{{variables[field]}}}" for field in FIELD_ORDER if field in variables})
        names = ", ".join(variables[field] for field in FIELD_ORDER if field in variables)
        guesses.append(Guess(fields, f"{type_ or 'database'} from {names}"))
    return guesses


def _field_for(variable: str) -> tuple[str, str] | None:
    best = None
    for field, suffixes in FIELD_SUFFIXES:
        for suffix in suffixes:
            if variable.endswith(suffix) and (best is None or len(suffix) > len(best[2])):
                best = (field, variable[: -len(suffix)], suffix)
    return (best[0], best[1]) if best else None
