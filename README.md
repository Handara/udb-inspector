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
dbi postgres://user@localhost/shop        # a URL (asks for the password if needed)
dbi db.sqlite3                            # a SQLite or DuckDB file
dbi init                                  # save a connection for this project
dbi                                       # open this project's connection
dbi staging                               # open a saved connection by name
dbi -g                                    # use the global connections, not the project's
dbi --info                                # print the connection details, no UI
```

## Connections

Save connections once instead of typing URLs. A project keeps them in
`.dbi.toml` at its root, which is safe to commit: it only names the variables
that hold your settings, and the values stay in `.env`.

```toml
# .dbi.toml
env_file = ".env"                 # the default

[connections.default]
url = "${DATABASE_URL}"

[connections.replica]
type = "postgres"                 # postgres, mysql, mariadb, mssql, oracle, sqlite, duckdb
host = "${REPLICA_HOST}"
port = "${REPLICA_PORT:-5432}"    # fallback when unset or empty
database = "shop"
user = "${REPLICA_USER}"
password = "${REPLICA_PASSWORD}"
options = { sslmode = "require" } # passed to the driver
```

`dbi init` writes this for you. It reads `.env`, suggests the variables that
look like database settings (`DATABASE_URL`, `DB_HOST`, `POSTGRES_*`, `PG*`, …),
tests the connection, and appends it.

Connections that don't belong to a project go in the global config with
`dbi init -g`: `~/.config/db-inspector/config.toml`, with secrets in
`~/.config/db-inspector/.env`. dbi refuses to read that file unless only you
can (`chmod 600`).

Which database `dbi` opens:

1. A URL or file you pass.
2. A name you pass: the project's connection with that name, else the global one.
3. Nothing passed: `$DBI_URL`, then the project's `default` connection, then the global `default`.
   A config with a single connection uses it as the default.
4. `-g` skips `$DBI_URL` and the project.

The app's header and `--info` show where the connection came from, such as
`project ~/code/shop/.dbi.toml [default]` or `global [staging]`.

`${VAR}` is looked up in the environment first, then the env file. When no
password is configured, Postgres falls back to `~/.pgpass`, and otherwise dbi
asks for it.

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
