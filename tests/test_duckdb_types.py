import pytest

from udb_inspector.database import Database
from udb_inspector.detect import detect


def test_times_with_a_zone(tmp_path):
    """DuckDB hands back TIMESTAMP WITH TIME ZONE through pytz; without it SELECT now() fails."""
    duckdb = pytest.importorskip("duckdb")
    pytest.importorskip("duckdb_engine")
    path = tmp_path / "t.duckdb"
    duckdb.connect(str(path)).close()
    db = Database(detect(str(path)))
    result = db.run_query("SELECT now() AS at, to_timestamp(1767225600) AS epoch")
    assert result.rows[0][0].tzinfo is not None and result.rows[0][1].year in (2025, 2026)
