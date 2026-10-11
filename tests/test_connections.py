import os

import pytest

from udb_inspector.connections import ConfigError, global_dir, resolve
from udb_inspector.detect import DetectionError


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "shop"
    write(root / ".env", "DB_HOST=db.internal\nDB_USER=app\nDB_PASSWORD='p@ss$word'\nEMPTY=\n")
    write(
        root / ".udbi.toml",
        """
        env_file = ".env"

        [connections.default]
        type = "postgres"
        host = "${DB_HOST}"
        port = "${DB_PORT:-5433}"
        database = "shop"
        user = "${DB_USER}"
        password = "${DB_PASSWORD}"
        options = { sslmode = "require" }

        [connections.local]
        url = "sqlite:///data/local.sqlite3"
        """.replace("\n        ", "\n"),
    )
    return root


def write_global(text, env=None):
    write(global_dir() / "config.toml", text)
    if env is not None:
        path = write(global_dir() / ".env", env)
        path.chmod(0o600)


def test_project_default_from_env_file(project):
    target = resolve(None, cwd=project)
    url = target.url
    assert (url.drivername, url.host, url.port, url.database) == ("postgresql+psycopg", "db.internal", 5433, "shop")
    assert (url.username, url.password) == ("app", "p@ss$word")
    assert dict(url.query) == {"sslmode": "require"}
    assert target.source == f"project {project / '.udbi.toml'} [default]"


def test_project_found_from_a_subdirectory(project):
    sub = project / "src" / "app"
    sub.mkdir(parents=True)
    assert resolve(None, cwd=sub).url.host == "db.internal"


def test_environment_beats_env_file(project, monkeypatch):
    monkeypatch.setenv("DB_HOST", "override")
    assert resolve(None, cwd=project).url.host == "override"


def test_file_database_is_relative_to_the_config(project):
    assert resolve("local", cwd=project).url.database == str(project / "data" / "local.sqlite3")


def test_missing_variable_names_the_env_file(project):
    write(project / ".udbi.toml", '[connections.default]\nurl = "${NOPE}"\n')
    with pytest.raises(ConfigError, match=r"\$\{NOPE\} is not set in the environment or .*\.env"):
        resolve(None, cwd=project)


def test_interpolation_rules(project):
    write(
        project / ".udbi.toml",
        '[connections.default]\ntype = "postgres"\nhost = "${EMPTY:-fallback}"\ndatabase = "a$$b"\nuser = "${EMPTY}"\n',
    )
    url = resolve(None, cwd=project).url
    assert (url.host, url.database, url.username) == ("fallback", "a$b", None)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('type = "postgres"\nhots = "x"', "unknown key hots"),
        ('type = "mongo"', "type must be one of"),
        ('type = "postgres"\nport = "abc"', "is not a number"),
        ('url = "sqlite://"\nhost = "x"', "either url or"),
    ],
)
def test_bad_connections(project, body, message):
    write(project / ".udbi.toml", f"[connections.default]\n{body}\n")
    with pytest.raises(ConfigError, match=message):
        resolve(None, cwd=project)


def test_several_connections_without_default(project):
    write(project / ".udbi.toml", '[connections.a]\nurl = "sqlite://"\n[connections.b]\nurl = "sqlite://"\n')
    with pytest.raises(ConfigError, match=r"Pick one: udbi NAME \(a, b\)"):
        resolve(None, cwd=project)


def test_single_connection_is_the_default(project):
    write(project / ".udbi.toml", '[connections.only]\nurl = "sqlite://"\n')
    assert resolve(None, cwd=project).source.endswith("[only]")


def test_global_when_no_project(tmp_path):
    write_global('[connections.staging]\nurl = "postgres://staging/app"\n')
    target = resolve(None, cwd=tmp_path)
    assert target.url.host == "staging"
    assert target.source == "global [staging]"


def test_project_beats_global_unless_g(project):
    write_global('[connections.default]\nurl = "postgres://global-host/app"\n')
    assert resolve(None, cwd=project).url.host == "db.internal"
    assert resolve(None, cwd=project, global_only=True).url.host == "global-host"


def test_names_fall_back_to_global(project):
    write_global('[connections.staging]\nurl = "postgres://staging/app"\n')
    assert resolve("staging", cwd=project).source == "global [staging]"
    with pytest.raises(DetectionError, match="no connection with that name"):
        resolve("local", cwd=project, global_only=True)


def test_global_env_file_must_be_private(tmp_path):
    write_global('[connections.default]\nurl = "postgres://u:${PW}@h/app"\n', env="PW=secret\n")
    assert resolve(None, cwd=tmp_path).url.password == "secret"
    (global_dir() / ".env").chmod(0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        resolve(None, cwd=tmp_path)


def test_dbi_url_beats_configs_but_not_g(project, monkeypatch):
    write_global('[connections.default]\nurl = "postgres://global-host/app"\n')
    monkeypatch.setenv("UDBI_URL", "mysql://env-host/app")
    assert resolve(None, cwd=project).source == "$UDBI_URL"
    assert resolve(None, cwd=project, global_only=True).url.host == "global-host"


def test_database_url_is_not_used_implicitly(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://somewhere/app")
    with pytest.raises(DetectionError, match="No database given"):
        resolve(None, cwd=tmp_path)


def test_explicit_url_and_file_win(project, sample_db):
    assert resolve("mysql://x/y", cwd=project).source == "URL"
    assert resolve(str(sample_db), cwd=project).source == "SQLite file header"


def test_unknown_name_without_configs(tmp_path):
    with pytest.raises(DetectionError, match="no .udbi.toml or global config found"):
        resolve("staging", cwd=tmp_path)


def test_broken_toml(project):
    write(project / ".udbi.toml", "[connections.default\n")
    with pytest.raises(ConfigError, match=".udbi.toml"):
        resolve(None, cwd=project)


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_project_env_file_needs_no_special_mode(project):
    (project / ".env").chmod(0o644)
    assert resolve(None, cwd=project).url.username == "app"


def test_info_shows_where_the_connection_came_from(tmp_path, sample_db, capsys):
    from udb_inspector.cli import main

    write_global(f'[connections.staging]\nurl = "sqlite:///{sample_db}"\n')
    assert main(["--info"]) == 0
    assert "global [staging]" in capsys.readouterr().out


def test_allow_writes(project):
    write(project / ".udbi.toml", '[connections.default]\nurl = "sqlite://"\nallow_writes = true\n\n[connections.ro]\nurl = "sqlite://"\n')
    assert resolve(None, cwd=project).allow_writes is True
    assert resolve("ro", cwd=project).allow_writes is False
    write(project / ".udbi.toml", '[connections.default]\nurl = "sqlite://"\nallow_writes = "yes"\n')
    with pytest.raises(ConfigError, match="allow_writes must be true or false"):
        resolve(None, cwd=project)
