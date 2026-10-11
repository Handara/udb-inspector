"""Entry point for the standalone build (PyInstaller needs a script, not a package)."""

from udb_inspector.cli import main

raise SystemExit(main())
