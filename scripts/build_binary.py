"""Build `udbi` as a standalone program that needs no Python or virtualenv.

    python3 scripts/build_binary.py                      # writes the dist/udbi/ folder
    python3 scripts/build_binary.py --install            # and installs it for this user
    python3 scripts/build_binary.py --archive udbi-linux-x86_64   # and packs it for a release

The result is a folder (the udbi executable plus its libraries) rather than a
single file: a single file has to unpack ~190 MB to a temp folder on every
launch, which takes seconds on a small server. The build itself runs in its own
virtualenv (build/venv). The program only runs on the OS and CPU it was built
on, and on Linux needs a glibc at least as new as the build machine's.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / "build" / "venv"
DIST = ROOT / "dist"
WINDOWS = sys.platform == "win32"

# Drivers bundled into the program. SQLite needs nothing extra.
EXTRAS = "postgres,mysql,oracle,duckdb"
DRIVER_PACKAGES = ["psycopg", "psycopg_binary", "pymysql", "oracledb", "cryptography", "duckdb", "duckdb_engine"]

# PyInstaller follows imports; these are loaded dynamically and must be listed.
PYINSTALLER_ARGS = [
    "--onedir",
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
    parser.add_argument("--install", action="store_true", help="install into ~/.local/share/udbi, linked from ~/.local/bin/udbi")
    parser.add_argument("--archive", metavar="NAME", help="also pack the folder as NAME.tar.gz (NAME.zip on Windows)")
    args = parser.parse_args()

    python = VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")
    if not python.exists():
        print(f"Creating {VENV}")
        venv.create(VENV, with_pip=True)
    run(python, "-m", "pip", "install", "--quiet", "--upgrade", "pip")
    run(python, "-m", "pip", "install", "--quiet", "pyinstaller", f"{ROOT}[{EXTRAS}]")
    run(python, "-m", "PyInstaller", "--log-level", "WARN", *PYINSTALLER_ARGS)

    folder = DIST / "udbi"
    size = sum(path.stat().st_size for path in folder.rglob("*") if path.is_file())
    print(f"Built {folder} ({size / 1_000_000:.0f} MB)")
    if args.archive:
        archive = shutil.make_archive(str(DIST / args.archive), "zip" if WINDOWS else "gztar", root_dir=DIST, base_dir="udbi")
        print(f"Packed {archive}")
    if args.install:
        install(folder)
    return 0


def install(folder: Path) -> None:
    """Swap the new folder into place, so a udbi that's running keeps its files."""
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    target = data / "udbi"
    staged, old = target.with_name("udbi.new"), target.with_name("udbi.old")
    for leftover in (staged, old):
        shutil.rmtree(leftover, ignore_errors=True)
    shutil.copytree(folder, staged, symlinks=True)
    if target.exists():
        target.rename(old)
    staged.rename(target)
    shutil.rmtree(old, ignore_errors=True)

    link = Path.home() / ".local" / "bin" / ("udbi.exe" if WINDOWS else "udbi")
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name(".udbi.link")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target / link.name)
    temporary.replace(link)  # replaces an older single-file udbi too
    print(f"Installed {target}, linked from {link}")


def run(*command: object) -> None:
    subprocess.run([str(part) for part in command], cwd=ROOT, check=True)


if __name__ == "__main__":
    raise SystemExit(main())
