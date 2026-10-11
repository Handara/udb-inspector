"""Build `udbi` as a single executable that needs no Python or virtualenv.

    python3 scripts/build_binary.py            # writes dist/udbi
    python3 scripts/build_binary.py --install  # and copies it to ~/.local/bin

The build itself runs in its own virtualenv (build/venv). The executable only
runs on the OS and CPU it was built on, and on Linux needs a glibc at least as
new as the build machine's.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / "build" / "venv"
DIST = ROOT / "dist"

# Drivers bundled into the executable. SQLite needs nothing extra.
EXTRAS = "postgres,mysql,oracle,duckdb"
DRIVER_PACKAGES = ["psycopg", "psycopg_binary", "pymysql", "oracledb", "cryptography", "duckdb", "duckdb_engine"]

# PyInstaller follows imports; these are loaded dynamically and must be listed.
PYINSTALLER_ARGS = [
    "--onefile",
    "--name", "udbi",
    "--clean",
    "--noconfirm",
    "--distpath", str(DIST),
    "--workpath", str(ROOT / "build" / "pyinstaller"),
    "--specpath", str(ROOT / "build"),
    "--collect-data", "udb_inspector",  # app.tcss
    "--collect-submodules", "textual",  # widgets are imported lazily
    "--collect-data", "textual",
    "--collect-submodules", "rich",  # unicode tables are imported by name
    "--collect-submodules", "sqlalchemy.dialects",
    "--collect-submodules", "sqlglot",  # SQL dialects are imported by name
    "--collect-all", "tree_sitter_sql",  # the editor's highlighting grammar
    "--copy-metadata", "duckdb_engine",  # registers its dialect through an entry point
    *[arg for package in DRIVER_PACKAGES for arg in ("--collect-all", package)],
    str(ROOT / "scripts" / "udbi_entry.py"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--install", action="store_true", help="copy the result to ~/.local/bin/udbi")
    args = parser.parse_args()

    python = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.exists():
        print(f"Creating {VENV}")
        venv.create(VENV, with_pip=True)
    run(python, "-m", "pip", "install", "--quiet", "--upgrade", "pip")
    run(python, "-m", "pip", "install", "--quiet", "pyinstaller", f"{ROOT}[{EXTRAS}]")
    run(python, "-m", "PyInstaller", "--log-level", "WARN", *PYINSTALLER_ARGS)

    binary = DIST / ("udbi.exe" if sys.platform == "win32" else "udbi")
    print(f"Built {binary} ({binary.stat().st_size / 1_000_000:.0f} MB)")
    if args.install:
        target = Path.home() / ".local" / "bin" / binary.name
        target.parent.mkdir(parents=True, exist_ok=True)
        # Copy beside it and rename over it: a udbi that's running keeps its
        # old file, where copying onto it would fail with "text file busy".
        staged = target.with_name(f".{binary.name}.new")
        shutil.copy2(binary, staged)
        staged.replace(target)
        print(f"Installed {target}")
    return 0


def run(*command: object) -> None:
    subprocess.run([str(part) for part in command], cwd=ROOT, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
