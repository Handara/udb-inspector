import stat

import pytest

from udb_inspector.connections import global_dir, resolve
from udb_inspector.database import ConnectError
from udb_inspector.wizard import Prompter, guess_connections, run_init


class Script:
    """Answers prompts in order and records everything said."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.said = []

    def next(self, question, default=None):
        assert self.answers, f"unexpected prompt: {question}"
        answer = self.answers.pop(0)
        return default if answer == "" and default is not None else answer  # "" = press enter

    def prompter(self):
        return Prompter(ask=self.next, confirm=self.next, secret=self.next, say=self.said.append)

    @property
    def output(self):
        return "\n".join(self.said)


def ok(target):
    return "SQLite 3"


def refuse(target):
    raise ConnectError("connection refused")


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


def test_project_init_from_env_url(tmp_path, sample_db):
    (tmp_path / ".env").write_text(f"DATABASE_URL=sqlite:///{sample_db}\n")
    script = Script("", "1", True)  # default name, first guess, save
    path = run_init("project", tmp_path, script.prompter(), check=ok)

    assert path == tmp_path / ".dbi.toml"
    assert '[connections.default]\nurl = "${DATABASE_URL}"' in path.read_text()
    assert "Saved. Open it with: dbi" in script.output
    assert resolve(None, cwd=tmp_path).url.database == str(sample_db)


def test_project_init_by_hand_refuses_literal_password(tmp_path):
    (tmp_path / ".env").write_text("REPLICA_PASSWORD=pw\n")
    script = Script(
        "replica", "postgres", "db.internal", "5432", "shop", "app",
        "hunter2",  # rejected: not a ${VAR}
        "${REPLICA_PASSWORD}",
        True,  # save anyway
    )
    path = run_init("project", tmp_path, script.prompter(), check=refuse)

    text = path.read_text()
    assert 'password = "${REPLICA_PASSWORD}"' in text and "hunter2" not in text
    assert "Couldn't connect: connection refused" in script.output
    assert "dbi replica" in script.output


def test_declining_saves_nothing(tmp_path):
    script = Script("", "sqlite", "db.sqlite3", False)
    assert run_init("project", tmp_path, script.prompter(), check=refuse) is None
    assert not (tmp_path / ".dbi.toml").exists()


def test_global_init_keeps_password_in_private_env(tmp_path):
    script = Script("staging", "mysql", "staging.internal", "3306", "shop", "app", True, "s3cr't", True)
    run_init("global", tmp_path, script.prompter(), check=ok)

    env_file = global_dir() / ".env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert "DBI_STAGING_PASSWORD=" in env_file.read_text()
    config = (global_dir() / "config.toml").read_text()
    assert 'password = "${DBI_STAGING_PASSWORD}"' in config and "s3cr" not in config

    target = resolve("staging", cwd=tmp_path, global_only=True)
    assert (target.url.host, target.url.password) == ("staging.internal", "s3cr't")
    assert "dbi -g staging" in script.output


def test_init_appends_and_rejects_duplicate_names(tmp_path):
    run_init("project", tmp_path, Script("", "sqlite", "a.db", True).prompter(), check=ok)
    script = Script("default", "other", "sqlite", "b.db", True)
    run_init("project", tmp_path, script.prompter(), check=ok)

    assert "default already exists" in script.output
    text = (tmp_path / ".dbi.toml").read_text()
    assert text.count("[connections.") == 2 and text.count("env_file") == 1


def test_bad_choice_is_asked_again(tmp_path, sample_db):
    (tmp_path / ".env").write_text(f"DATABASE_URL=sqlite:///{sample_db}\n")
    script = Script("", "9", "1", True)
    run_init("project", tmp_path, script.prompter(), check=ok)
    assert "Pick a number from 1 to 2." in script.output


@pytest.mark.parametrize("name", ["init", "has space"])
def test_reserved_and_invalid_names(tmp_path, name):
    script = Script(name, "ok", "sqlite", "a.db", True)
    run_init("project", tmp_path, script.prompter(), check=ok)
    assert "[connections.ok]" in (tmp_path / ".dbi.toml").read_text()
