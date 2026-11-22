"""Test package init.

Forces the unit tests onto SQLite regardless of any DATABASE_URL in the
environment or in the project-root .env file. An empty DATABASE_URL is set
before any project module is imported; python-dotenv (override=False) will
not replace an existing key, so the override holds. The guard below fails
fast if the database module still resolves to a non-SQLite dialect.
"""

import os

os.environ["DATABASE_URL"] = ""  # empty value forces SQLite; dotenv (override=False) will not replace it

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db as _db

if _db.get_database_info().get("dialect") != "sqlite":
    raise RuntimeError("unit tests must run on SQLite; DATABASE_URL override failed")
