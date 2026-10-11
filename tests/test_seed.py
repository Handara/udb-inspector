import importlib.util
import sqlite3
from pathlib import Path

from udb_inspector.connections import resolve
from udb_inspector.database import Database, Relation


def load_seed_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "seed_dev_databases.py"
    spec = importlib.util.spec_from_file_location("seed_dev_databases", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_fills_and_saves_a_database(tmp_path, monkeypatch):
    seed = load_seed_script()
    path = tmp_path / "shop.sqlite3"
    monkeypatch.setattr("sys.argv", ["seed", "--rows", "200", f"tiny={path}"])
    assert seed.main() == 0

    conn = sqlite3.connect(path)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("customers", "products", "orders", "order_items")}
    assert counts == {"customers": 20, "products": 1000, "orders": 200, "order_items": 200}
    assert conn.execute("SELECT SUM(order_count) FROM customer_totals").fetchone()[0] == 200

    target = resolve("dev-tiny", cwd=tmp_path)
    assert target.allow_writes
    details = Database(target).describe(Relation("main", "orders", "table"))
    assert [c.references for c in details.columns if c.name == "customer_id"] == ["customers.id"]
