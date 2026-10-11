import stat

import pytest

from udb_inspector.connection_setup import (
    delete_connection,
    guess_connections,
    load_or_new,
    save_connection,
    saved_connections,
    validate,
)
from udb_inspector.connections import global_dir, resolve


def test_guesses_from_common_variable_names():
    env = {
        "DATABASE_URL": "postgres://u@h/app",
        "DB_HOST": "h",
        "DB_PORT": "3306",
        "DB_NAME": "app",
        "DB_USER": "u",
        "DB_PASSWORD": "p",
        "POSTGRES_DB": "x",
        "POSTGRES_PASSWORD": "y",
        "PGHOST": "h",
        "PGDATABASE": "z",
        "EMAIL_HOST": "smtp",
        "EMAIL_PORT": "25",
        "EMAIL_HOST_USER": "me",
        "EMAIL_HOST_PASSWORD": "pw",
        "REDIS_HOST": "r",
        "SECRET_KEY": "k",
    }
    guesses = {guess.summary: guess.fields for guess in guess_connections(env)}
    assert guesses == {
        "url from DATABASE_URL": {"url": "${DATABASE_URL}"},
        "mysql from DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD": {
            "type": "mysql",
            "host": "${DB_HOST}",
            "port": "${DB_PORT}",
            "database": "${DB_NAME}",
            "user": "${DB_USER}",
            "password": "${DB_PASSWORD}",
        },
        "postgres from POSTGRES_DB, POSTGRES_PASSWORD": {
            "type": "postgres",
            "database": "${POSTGRES_DB}",
            "password": "${POSTGRES_PASSWORD}",
        },
        "postgres from PGHOST, PGDATABASE": {"type": "postgres", "host": "${PGHOST}", "database": "${PGDATABASE}"},
    }


def test_save_creates_a_project_file(tmp_path, sample_db):
    (tmp_path / ".env").write_text(f"DATABASE_URL=sqlite:///{sample_db}\n")
    config = save_connection(load_or_new("project", tmp_path), "default", {"url": "${DATABASE_URL}"}, {})

    text = (tmp_path / ".udbi.toml").read_text()
    assert text.startswith("# udbi connections for this project.")
    assert 'env_file = ".env"' in text
    assert list(config.connections) == ["default"]
    assert resolve(None, cwd=tmp_path).url.database == str(sample_db)


def test_save_keeps_comments_and_appends(tmp_path):
    path = tmp_path / ".udbi.toml"
    path.write_text('# keep me\n[connections.a]\nurl = "sqlite://"  # and me\n')
    save_connection(load_or_new("project", tmp_path), "b", {"type": "postgres", "host": "db"}, {})

    text = path.read_text()
    assert "# keep me" in text and "# and me" in text
    assert '[connections.b]\ntype = "postgres"\nhost = "db"' in text


def test_global_secret_goes_to_a_private_env_file(tmp_path):
    fields = {"type": "mysql", "host": "h", "password": "${UDBI_STAGING_PASSWORD}"}
    save_connection(load_or_new("global", tmp_path), "staging", fields, {"UDBI_STAGING_PASSWORD": "s3cr't"})

    env_file = global_dir() / ".env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert "s3cr" not in (global_dir() / "config.toml").read_text()
    assert resolve("staging", cwd=tmp_path).url.password == "s3cr't"


def test_delete_removes_the_connection_and_its_saved_password(tmp_path):
    global_config = load_or_new("global", tmp_path)
    global_config = save_connection(global_config, "a", {"type": "mysql", "password": "${UDBI_A_PASSWORD}"}, {"UDBI_A_PASSWORD": "x"})
    global_config = save_connection(global_config, "b", {"type": "mysql", "password": "${UDBI_B_PASSWORD}"}, {"UDBI_B_PASSWORD": "y"})
    delete_connection(global_config, "a")

    assert "[connections.a]" not in (global_dir() / "config.toml").read_text()
    env = (global_dir() / ".env").read_text()
    assert "UDBI_A_PASSWORD" not in env and "UDBI_B_PASSWORD='y'" in env
    assert stat.S_IMODE((global_dir() / ".env").stat().st_mode) == 0o600


def test_saved_connections_lists_project_then_global(tmp_path):
    save_connection(load_or_new("global", tmp_path), "g", {"url": "sqlite://"}, {})
    save_connection(load_or_new("project", tmp_path), "p", {"url": "sqlite://"}, {})
    saved, errors = saved_connections(tmp_path)
    assert [(s.config.scope, s.name, s.summary) for s in saved] == [
        ("project", "p", "sqlite://"),
        ("global", "g", "sqlite://"),
    ]
    assert errors == []


def test_saved_connections_reports_broken_configs(tmp_path):
    (tmp_path / ".udbi.toml").write_text("[connections.x\n")
    saved, errors = saved_connections(tmp_path)
    assert saved == [] and ".udbi.toml" in errors[0]


@pytest.mark.parametrize(
    ("scope", "name", "fields", "problem"),
    [
        ("project", "has space", {"url": "sqlite://"}, "Name"),
        ("project", "ok", {"url": ""}, "URL is empty"),
        ("project", "ok", {"url": "postgres://u:pw@h/db"}, "holds a password"),
        ("project", "ok", {"url": "postgres://u:${PW}@h/db"}, None),
        ("project", "ok", {"type": "postgres", "password": "hunter2"}, "${VAR}"),
        ("global", "ok", {"type": "postgres", "password": "${X}"}, None),
        ("project", "ok", {"type": "postgres", "port": "abc"}, "not a number"),
        ("project", "ok", {"type": "postgres", "port": "${PORT}"}, None),
        ("project", "ok", {"type": "sqlite"}, "File is empty"),
    ],
)
def test_validate(tmp_path, scope, name, fields, problem):
    result = validate(load_or_new(scope, tmp_path), name, fields)
    assert (result is None) if problem is None else (problem in result)


def test_validate_rejects_duplicates(tmp_path):
    config = save_connection(load_or_new("project", tmp_path), "a", {"url": "sqlite://"}, {})
    assert "already exists" in validate(config, "a", {"url": "sqlite://"})


def test_set_allow_writes_keeps_the_rest(tmp_path):
    from udb_inspector.connection_setup import set_allow_writes

    path = tmp_path / ".udbi.toml"
    path.write_text('# mine\n[connections.a]\nurl = "sqlite://"  # keep\n')
    config = set_allow_writes(load_or_new("project", tmp_path), "a", True)
    assert config.connections["a"]["allow_writes"] is True
    assert "# mine" in path.read_text() and "# keep" in path.read_text()
    config = set_allow_writes(config, "a", False)
    assert "allow_writes" not in config.connections["a"]
