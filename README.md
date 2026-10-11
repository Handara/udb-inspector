# udb-inspector

A unified database inspector for the terminal: point it at PostgreSQL, MySQL,
MariaDB, SQLite, SQL Server, Oracle or DuckDB, and it works out what it is. Browse
the schema from a sidebar, write queries with suggestions and explanations for
that database, follow records through their foreign keys, and export to CSV or
Excel.

## Install

**Linux and macOS (Apple Silicon):**

```sh
curl -fsSL https://raw.githubusercontent.com/Handara/udb-inspector/main/install.sh | sh
```

This puts a single `udbi` file in `~/.local/bin` (set `UDBI_INSTALL_DIR` to
choose another folder). It carries its own Python and the drivers for SQLite,
PostgreSQL, MySQL/MariaDB, Oracle and DuckDB, so there is nothing else to
install. Run the same command again to update.

**Windows:** download `udbi-windows-x86_64.exe` from the
[latest release](https://github.com/Handara/udb-inspector/releases/latest),
rename it to `udbi.exe` and put it in a folder on your PATH. The executables
aren't signed, so Windows SmartScreen may warn the first time.

**With Python** (any platform, and the only way to get SQL Server, which also
needs Microsoft's ODBC driver):

```sh
pipx install "udb-inspector[all] @ git+https://github.com/Handara/udb-inspector"
```

### Building it yourself

```sh
python3 scripts/build_binary.py             # writes dist/udbi (about 60 MB)
python3 scripts/build_binary.py --install   # and copies it to ~/.local/bin/udbi
```

The build makes its own virtualenv in `build/`. The executable runs on the OS
and CPU it was built on, and on Linux needs a glibc at least as new as the
build machine's.

### Working on udbi

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev,postgres]"
pytest
```

Pushing a tag like `v0.2.0` makes GitHub build the executables for every
platform and publish them as a release.

## Use

```sh
udbi                     # opens the app: this project's connection, else the global one, else the chooser
udbi staging             # a saved connection by name
udbi -g                  # the global connections only, even inside a project
udbi postgres://u@h/db   # a URL
udbi db.sqlite3          # a SQLite or DuckDB file
udbi --info              # print the connection details instead of opening the app
```

Everything else happens in the app. With nothing to open it starts on the
connection chooser.

| Key | Action |
| --- | --- |
| `F2` | Schema tab, with the tables focused |
| `F3`, `e` | Query tab, with the editor focused |
| `ctrl+t` | Jump to the tables sidebar from anywhere, even the editor |
| `s` | Query the highlighted table (`SELECT * FROM …`) |
| `c` | Choose a saved connection (`enter` opens, `a` adds, `d` deletes) |
| `a` | Add a connection |
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

### Queries

The Query tab (`F3` or `e`) has an SQL editor. `ctrl+enter` runs the statement
under the cursor, or the selected text. Some terminals (VS Code's, for one) send
ctrl+enter as a plain enter; there `ctrl+r` or `F5` run it instead. Results show up to 10,000 rows in a grid:

| Key | Action |
| --- | --- |
| `[` `]` | Previous / next page; the page size is picked next to the search box |
| `/` | Search the results (all columns, case-insensitive) |
| click a header | Sort by that column; again to reverse. NULLs stay last |
| `enter` | Show the cell in full, with JSON pretty-printed; `c` copies it |
| `ctrl+s`, `x` | Export the results |
| `o` | Open the row as a record (see below) |

Exports go to CSV, Excel (.xlsx), JSON, or the clipboard as tab-separated
text that pastes into a spreadsheet. By default an export runs the query again
and streams every row to the file, so it isn't limited to the 10,000 rows on
screen (a server-side cursor keeps big results out of memory where the driver
supports one). After searching or sorting you can export just the rows shown,
in that order. Excel stops at its 1,048,575-row limit and says how many rows
were left out.

As you type, the bar under the editor suggests what fits at the cursor:
statement keywords at the start, tables after
`FROM` and `JOIN`, the columns (with their types) of the tables in the
statement, `alias.` for one table's columns, then functions and keywords for
the connected database, such as `LIMIT` for PostgreSQL and `TOP` or
`OFFSET … FETCH NEXT` for SQL Server.

| Key | Action |
| --- | --- |
| `tab` | Open the suggestions as a list (indents when there are none) |
| `↑` `↓`, `tab` `shift+tab` | In the list: move the highlight |
| `enter` | In the list: insert the highlighted suggestion. Typing a letter goes back to the editor |
| `esc` | Walks outward: hides the suggestions, then leaves the editor for the results, then the tables |
| `F1` | Cheat sheet for the connected database: limiting rows, paging, quoting, dates, text, catalog queries |

The bar also warns about syntax from other databases (`LIMIT` on SQL Server,
`||` on MySQL, `"bob"` meaning a column in PostgreSQL, `NOW()` on SQL Server,
`IFNULL` outside MySQL and SQLite, …) and, after a short pause in typing,
about syntax errors.

### Records

`o` on a row opens it as a record: every field of the row with its type,
fetched fresh by primary key.

- Enter on a foreign key field (shown as `→ users`) opens the record it points
  to; `esc` goes back, one record at a time.
- The Related section lists the tables whose foreign keys point at this record,
  with how many rows do (`← orders   6 rows`). Enter queries those rows in the
  Query tab, ready to search, sort or export.
- Enter on any other field shows its value in full; `c` copies it.

Records open from queries on a single table. If the query left out the primary
key, the record shows the row as the query returned it.

A line in the bar also explains what the clause at the cursor will do, with
the statement's own table names: what an `INNER`, `LEFT`, `RIGHT`, `FULL`,
`CROSS` or `NATURAL JOIN` keeps and where NULLs appear, what `GROUP BY` and
`HAVING` require, `DISTINCT`, `UNION` versus `UNION ALL`, where this database
sorts NULLs in `ORDER BY`, and that `LIMIT` without `ORDER BY` picks undefined
rows. And it warns about common mistakes: a comma right before `FROM`, a table
joined to itself without aliases, a `JOIN` without `ON`, `= NULL`, the
`NOT IN (SELECT …)` NULL trap, `FULL JOIN` on MySQL, and selected columns that
are neither grouped nor aggregated.

Queries are read-only. Each statement is checked before it runs, and anything
that writes (including a `DELETE` inside a `WITH`, `SELECT … INTO` and
`EXPLAIN ANALYZE` of a write) is refused. On top of that the transaction is
made read-only where the database supports it (PostgreSQL, MySQL/MariaDB,
Oracle, and SQLite's `query_only`) and is always rolled back.

Try it with the demo database:

```sh
python scripts/make_demo_db.py demo.sqlite3 && udbi demo.sqlite3
```

## Connections

Add connections in the app with `a`. The form can fill itself in from the
variables in `.env` that look like database settings (`DATABASE_URL`,
`DB_HOST`, `POSTGRES_*`, `PG*`, …), test the connection, and save it either for
this project or globally.

A project keeps its connections in `.udbi.toml` at its root, which is safe to
commit: it only names the variables that hold your settings, and the values
stay in `.env`. The form won't save a password into it.

```toml
# .udbi.toml
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

Global connections live in `~/.config/udb-inspector/config.toml`. A password
typed into the form for a global connection is saved in
`~/.config/udb-inspector/.env`, which udbi keeps private (`chmod 600`) and
refuses to read otherwise. Deleting the connection removes it again.

Which database `udbi` opens:

1. A URL or file you pass.
2. A name you pass: the project's connection with that name, else the global one.
3. Nothing passed: `$UDBI_URL`, then the project's `default` connection, then the global `default`.
   A config with a single connection uses it as the default.
4. `-g` skips `$UDBI_URL` and the project.

The header and `--info` show where the connection came from, such as
`project ~/code/shop/.udbi.toml [default]` or `global [staging]`.

`${VAR}` is looked up in the environment first, then the env file. When no
password is configured, Postgres tries `~/.pgpass`, and otherwise the app asks
for it.

## How the database type is detected

| Target | Detection |
| --- | --- |
| URL | The scheme. `postgres://`, `mysql://`, `mariadb://`, `mssql://`/`sqlserver://` and `oracle://` get a default driver. |
| File | The file header: `SQLite format 3` or DuckDB's `DUCK` magic. An empty `.sqlite3`/`.db` file counts as SQLite. |

After connecting, the server's own version string refines the answer, so
CockroachDB, Redshift and YugabyteDB are not reported as PostgreSQL, and
MariaDB and TiDB are not reported as MySQL.
