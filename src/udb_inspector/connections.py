"""Named connections from a project's .udbi.toml or the global config.

    # .udbi.toml
    env_file = ".env"

    [connections.default]
    url = "${DATABASE_URL}"

    [connections.replica]
    type = "postgres"
    host = "${REPLICA_HOST}"
    port = "${REPLICA_PORT:-5432}"

Values can refer to ${VAR} or ${VAR:-default}. Variables come from the
environment first, then the env file. The global config lives in
~/.config/udb-inspector/ with its own .env, which must be private (chmod 600).
"""

from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import URL

from .detect import DEFAULT_DRIVERS, DetectionError, Target, detect, parse_url

PROJECT_FILE = ".udbi.toml"
GLOBAL_FILE = "config.toml"

TYPES = {**DEFAULT_DRIVERS, "sqlite": "sqlite", "duckdb": "duckdb"}
FILE_TYPES = {"sqlite", "duckdb"}
FIELD_KEYS = {"type", "driver", "host", "port", "database", "user", "password", "options"}

_VARIABLE = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(DetectionError):
    """A config file is malformed or refers to a variable that isn't set."""


class NothingToOpen(DetectionError):
    """No target given and no default connection configured."""


def global_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "udb-inspector"


def find_project_config(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        candidate = directory / PROJECT_FILE
        if candidate.is_file():
            return candidate
    return None


@dataclass
class Config:
    path: Path
    scope: str  # "project" or "global"
    env_file: Path
    connections: dict[str, dict]
    _env: dict[str, str] | None = field(default=None, repr=False)

    @classmethod
    def load(cls, path: Path, scope: str) -> Config:
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{tilde(path)}: {exc}") from None
        unknown = set(data) - {"env_file", "connections"}
        if unknown:
            raise ConfigError(f"{tilde(path)}: unknown setting {', '.join(sorted(unknown))}")
        connections = data.get("connections", {})
        if not isinstance(connections, dict) or not all(isinstance(c, dict) for c in connections.values()):
            raise ConfigError(f"{tilde(path)}: connections must be [connections.NAME] tables")
        env_file = path.parent / data.get("env_file", ".env")
        return cls(path, scope, env_file, connections)

    @property
    def label(self) -> str:
        return f"project {tilde(self.path)}" if self.scope == "project" else "global"

    def default_name(self) -> str:
        if not self.connections:
            raise NothingToOpen(f"{tilde(self.path)} has no connections yet.")
        if "default" in self.connections:
            return "default"
        if len(self.connections) == 1:
            return next(iter(self.connections))
        names = ", ".join(self.connections)
        raise ConfigError(f"{tilde(self.path)} has no connection called default. Pick one: udbi{self._flag} NAME ({names})")

    def target(self, name: str) -> Target:
        return connection_target(self.connections[name], self, f"{self.label} [{name}]")

    def env(self) -> dict[str, str]:
        if self._env is None:
            self._env = {**read_env_file(self.env_file, private=self.scope == "global"), **os.environ}
        return self._env

    @property
    def _flag(self) -> str:
        return " -g" if self.scope == "global" else ""


def load_global() -> Config | None:
    path = global_dir() / GLOBAL_FILE
    return Config.load(path, "global") if path.is_file() else None


def read_env_file(path: Path, *, private: bool = False) -> dict[str, str]:
    if not path.is_file():
        return {}
    if private and os.name == "posix" and path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"{tilde(path)} holds passwords but other users can read it. Run: chmod 600 {tilde(path)}")
    return {key: value for key, value in dotenv_values(path).items() if value is not None}


def interpolate(value: str, env: dict[str, str], where: str, env_file: Path) -> str:
    def replace(match: re.Match) -> str:
        if match.group(0) == "$$":
            return "$"
        name, default = match.group(1), match.group(2)
        # Like Docker Compose: ${VAR:-x} also covers an empty VAR.
        if name in env and (env[name] or default is None):
            return env[name]
        if default is not None:
            return default
        source = tilde(env_file) if env_file.is_file() else f"{tilde(env_file)} (missing)"
        raise ConfigError(f"{where}: ${{{name}}} is not set in the environment or {source}")

    return _VARIABLE.sub(replace, value)


def connection_target(raw: dict, config: Config, where: str) -> Target:
    unknown = set(raw) - FIELD_KEYS - {"url", "allow_writes"}
    if unknown:
        raise ConfigError(f"{where}: unknown key {', '.join(sorted(unknown))}")
    allow_writes = raw.get("allow_writes", False)
    if not isinstance(allow_writes, bool):
        raise ConfigError(f"{where}: allow_writes must be true or false")

    def value(key: str) -> str | None:
        if key not in raw:
            return None
        return interpolate(str(raw[key]), config.env(), where, config.env_file) or None

    if "url" in raw:
        if set(raw) - {"allow_writes"} != {"url"}:
            raise ConfigError(f"{where}: use either url or type/host/port/…, not both")
        url = parse_url(value("url") or "")
    else:
        type_ = value("type")
        if type_ not in TYPES:
            raise ConfigError(f"{where}: type must be one of {', '.join(sorted(TYPES))}")
        port = value("port")
        if port is not None and not port.isdigit():
            raise ConfigError(f"{where}: port {port!r} is not a number")
        options = raw.get("options", {})
        url = URL.create(
            value("driver") or TYPES[type_],
            username=value("user"),
            password=value("password"),
            host=value("host"),
            port=int(port) if port else None,
            database=value("database"),
            query={key: interpolate(str(v), config.env(), where, config.env_file) for key, v in options.items()},
        )

    # File databases are relative to the config file, not wherever udbi runs.
    if url.get_backend_name() in FILE_TYPES and url.database and url.database != ":memory:":
        path = Path(url.database).expanduser()
        if not path.is_absolute():
            url = url.set(database=str(config.path.parent / path))
    return Target(url, where, allow_writes)


def resolve(target: str | None, *, global_only: bool = False, cwd: Path | None = None) -> Target:
    """Pick the database to open.

    An explicit URL or file wins. A name is looked up in the project config,
    then the global one. With nothing given: $UDBI_URL, the project's default
    connection, then the global default. -g skips $UDBI_URL and the project.
    """
    cwd = cwd or Path.cwd()
    if target and ("://" in target or Path(target).expanduser().exists()):
        return detect(target)

    configs = []
    if not global_only and (project := find_project_config(cwd)):
        configs.append(Config.load(project, "project"))
    if global_config := load_global():
        configs.append(global_config)

    if target:
        for config in configs:
            if target in config.connections:
                return config.target(target)
        searched = " or ".join(config.label for config in configs) or "no .udbi.toml or global config found"
        raise DetectionError(f"{target}: no such file, and no connection with that name ({searched})")

    if not global_only and (url := os.environ.get("UDBI_URL")):
        return Target(parse_url(url), "$UDBI_URL")
    if configs:
        config = configs[0]
        return config.target(config.default_name())
    raise NothingToOpen("No database given, and no saved connection to open.")


def tilde(path: Path) -> str:
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)
