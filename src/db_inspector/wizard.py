"""`dbi init`: add a named connection to the project or global config."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .connections import (
    FILE_TYPES,
    GLOBAL_FILE,
    PROJECT_FILE,
    Config,
    connection_target,
    find_project_config,
    global_dir,
    read_env_file,
    tilde,
)
from .database import ConnectError
from .detect import DetectionError, Target

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

PROJECT_HEADER = """\
# dbi connections for this project. Safe to commit: keep secrets in the env
# file and refer to them as ${VAR} or ${VAR:-default}.
env_file = ".env"
"""
GLOBAL_HEADER = """\
# dbi global connections. Secrets go in .env next to this file (chmod 600)
# and are referred to as ${VAR}.
"""


@dataclass
class Guess:
    fields: dict[str, str]
    summary: str


@dataclass
class Prompter:
    ask: Callable[[str, str | None], str]
    confirm: Callable[[str, bool], bool]
    secret: Callable[[str], str]
    say: Callable[[str], None]


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


def render_connection(name: str, fields: dict[str, str]) -> str:
    lines = [f"[connections.{name}]"]
    lines += [f"{key} = {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items()]
    return "\n".join(lines) + "\n"


def run_init(scope: str, cwd: Path, io: Prompter, check: Callable[[Target], str]) -> Path | None:
    if scope == "project":
        path = find_project_config(cwd) or cwd / PROJECT_FILE
    else:
        path = global_dir() / GLOBAL_FILE
    config = Config.load(path, scope) if path.is_file() else Config(path, scope, path.parent / ".env", {})
    env = read_env_file(config.env_file, private=scope == "global")

    io.say(f"Adding a {scope} connection to {tilde(path)}")
    if env:
        io.say(f"Read {len(env)} variables from {tilde(config.env_file)}")
    name = _ask_name(io, config)

    fields = None
    guesses = guess_connections(env)
    if guesses:
        io.say("These variables look like database settings:")
        for number, guess in enumerate(guesses, 1):
            io.say(f"  {number}) {guess.summary}")
        io.say(f"  {len(guesses) + 1}) enter the settings myself")
        choice = _ask_choice(io, len(guesses) + 1)
        if choice <= len(guesses):
            fields = dict(guesses[choice - 1].fields)
            if "url" not in fields and "type" not in fields:
                fields = {"type": _ask_type(io), **fields}
    pending_env: dict[str, str] = {}
    if fields is None:
        fields = _ask_fields(io, config, name, pending_env)

    io.say("")
    io.say(render_connection(name, fields))
    config.env().update(pending_env)
    try:
        io.say(f"Connected: {check(connection_target(fields, config, f'{config.label} [{name}]'))}")
        save = io.confirm("Save this connection?", True)
    except (DetectionError, ConnectError) as exc:
        io.say(f"Couldn't connect: {exc}")
        save = io.confirm("Save it anyway?", False)
    if not save:
        return None

    _append(path, PROJECT_HEADER if scope == "project" else GLOBAL_HEADER, render_connection(name, fields))
    if pending_env:
        _append_env(config.env_file, pending_env)
    command = "dbi" + (" -g" if scope == "global" else "") + ("" if name == "default" else f" {name}")
    io.say(f"Saved. Open it with: {command}")
    return path


def _ask_name(io: Prompter, config: Config) -> str:
    default = None if "default" in config.connections else "default"
    while True:
        name = io.ask("Connection name", default).strip()
        if not NAME_PATTERN.match(name):
            io.say("Use letters, digits, - and _ only.")
        elif name == "init":
            io.say("init is a dbi command; pick another name.")
        elif name in config.connections:
            io.say(f"{name} already exists in {tilde(config.path)}; edit the file to change it.")
        else:
            return name


def _ask_choice(io: Prompter, count: int) -> int:
    while True:
        answer = io.ask("Use", "1").strip()
        if answer.isdigit() and 1 <= int(answer) <= count:
            return int(answer)
        io.say(f"Pick a number from 1 to {count}.")


def _ask_type(io: Prompter) -> str:
    while True:
        answer = io.ask(f"Database type ({', '.join(CONNECTION_TYPES)})", None).strip().lower()
        if answer in CONNECTION_TYPES:
            return answer
        io.say(f"Pick one of: {', '.join(CONNECTION_TYPES)}")


def _ask_fields(io: Prompter, config: Config, name: str, pending_env: dict[str, str]) -> dict[str, str]:
    io.say("Each answer can be a value or ${VAR} from the env file.")
    type_ = _ask_type(io)
    if type_ in FILE_TYPES:
        return {"type": type_, "database": io.ask("Database file (relative to the config file)", None).strip()}

    fields = {"type": type_}
    for key, label, default in (
        ("host", "Host", "localhost"),
        ("port", "Port", DEFAULT_PORTS[type_]),
        ("database", "Database", None),
        ("user", "User", None),
    ):
        answer = io.ask(label, default).strip()
        if answer:
            fields[key] = answer

    if config.scope == "global":
        if io.confirm(f"Save a password in {tilde(config.env_file)}? (no: dbi asks when connecting)", False):
            variable = f"DBI_{name.upper().replace('-', '_')}_PASSWORD"
            pending_env[variable] = io.secret("Password")
            fields["password"] = f"${{{variable}}}"
        return fields

    while True:
        # .dbi.toml is meant to be committed, so it only ever names a variable.
        answer = io.ask("Password variable, like ${DB_PASSWORD} (blank: dbi asks when connecting)", "").strip()
        if not answer or "${" in answer:
            break
        io.say("Give the variable as ${NAME}; .dbi.toml is meant to be committed, so it shouldn't hold the password.")
    if answer:
        fields["password"] = answer
    return fields


def _append(path: Path, header: str, text: str) -> None:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header)
    with path.open("a") as fh:
        fh.write("\n" + text)


def _append_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        for key, value in values.items():
            # Single quotes keep python-dotenv from expanding ${...} in the value.
            quoted = f"'{value}'" if "'" not in value else json.dumps(value)
            fh.write(f"{key}={quoted}\n")
