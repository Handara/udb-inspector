# db-inspector

A terminal database inspector in the spirit of Salesforce Inspector: point it at
a database, it works out what it is, and you browse it from a sidebar.

## Install

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # SQLite works out of the box
pip install -e ".[postgres]"       # or mysql, mssql, oracle, duckdb, all
```

## Use

```sh
dbi postgres://user:pass@localhost/shop   # a URL
dbi db.sqlite3                            # a SQLite or DuckDB file
dbi                                       # $DBI_URL, then $DATABASE_URL
dbi db.sqlite3 --info                     # print what was detected, no UI
```

Try it with the demo database:

```sh
python scripts/make_demo_db.py demo.sqlite3 && dbi demo.sqlite3
```

| Key | Action |
| --- | --- |
| `↑` `↓`, `enter` | Move through the sidebar; `enter` opens a table or expands a schema |
| `/` | Filter tables by name (`enter` jumps to the first match, `esc` clears) |
| `i` | Show the connection details again |
| `r` | Reload the schema |
| `q` | Quit |
| `ctrl+p` | Command palette |

Opening a table shows its columns (type, nullability, default, primary key,
foreign key target), keys and indexes, the foreign keys it holds, and the
tables whose foreign keys point at it. Opening a view shows its columns and
its SQL definition.

## How the database type is detected

| Target | Detection |
| --- | --- |
| URL | The scheme. `postgres://`, `mysql://`, `mariadb://`, `mssql://`/`sqlserver://` and `oracle://` get a default driver. |
| File | The file header: `SQLite format 3` or DuckDB's `DUCK` magic. An empty `.sqlite3`/`.db` file counts as SQLite. |

After connecting, the server's own version string refines the answer, so
CockroachDB, Redshift and YugabyteDB are not reported as PostgreSQL, and
MariaDB and TiDB are not reported as MySQL.

## Tests

```sh
pytest
```
