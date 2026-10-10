# db-inspector

A terminal database inspector in the spirit of Salesforce Inspector: point it at
a database, it works out what it is, and you browse it from a sidebar.

## Install

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"            # SQLite works out of the box
pip install -e ".[postgres]"       # or mysql, mssql, oracle, duckdb, all
```
