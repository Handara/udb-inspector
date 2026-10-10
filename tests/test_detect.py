import pytest

from db_inspector.detect import DetectionError, resolve


@pytest.mark.parametrize(
    ("raw", "driver"),
    [
        ("postgres://u:p@localhost/app", "postgresql+psycopg"),
        ("postgresql://localhost/app", "postgresql+psycopg"),
        ("postgresql+psycopg2://localhost/app", "postgresql+psycopg2"),
        ("mysql://root@db/app", "mysql+pymysql"),
        ("mariadb://root@db/app", "mariadb+pymysql"),
        ("sqlserver://sa@db/app", "mssql+pyodbc"),
        ("sqlite:///x.db", "sqlite"),
    ],
)
def test_url_gets_default_driver(raw, driver):
    assert resolve(raw).url.drivername == driver


def test_display_url_hides_password():
    assert "secret" not in resolve("postgres://u:secret@localhost/app").display_url


def test_sqlite_file_detected_by_header(sample_db):
    target = resolve(str(sample_db))
    assert target.url.drivername == "sqlite"
    assert target.url.database == str(sample_db.resolve())


def test_empty_sqlite_file_by_suffix(tmp_path):
    path = tmp_path / "new.sqlite3"
    path.touch()
    assert resolve(str(path)).url.drivername == "sqlite"


def test_non_database_file_rejected(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    with pytest.raises(DetectionError, match="not a SQLite or DuckDB"):
        resolve(str(path))


def test_missing_file_rejected(tmp_path):
    with pytest.raises(DetectionError, match="no such file"):
        resolve(str(tmp_path / "nope.db"))


def test_directory_rejected(tmp_path):
    with pytest.raises(DetectionError, match="no such file"):
        resolve(str(tmp_path))


def test_env_vars_in_order(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "mysql://db/app")
    assert resolve(None).source == "$DATABASE_URL"
    monkeypatch.setenv("DBI_URL", "postgres://db/app")
    assert resolve(None).source == "$DBI_URL"


def test_nothing_to_detect():
    with pytest.raises(DetectionError, match="No database given"):
        resolve(None)
