"""Database helpers for BizFlow AI.

SQLite is the default. Set DATABASE_URL to a postgres:// or postgresql:// URL to
use PostgreSQL instead (requires the psycopg driver). Application code always
writes "?" placeholders; they are converted to "%s" for PostgreSQL.

Money is stored as integer paise everywhere. Dates are ISO strings (YYYY-MM-DD).
"""
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

import os
import sqlite3
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import g

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")

# Default to the SQLite driver's exception; replaced with psycopg's when a
# PostgreSQL connection is opened (see _open_connection).
IntegrityError = sqlite3.IntegrityError

_POSTGRES_PREFIXES = ("postgres://", "postgresql://")


def _database_url():
    """Return the configured DATABASE_URL, stripped; empty string if unset."""
    return os.environ.get("DATABASE_URL", "").strip()


def _resolve_dialect(url):
    """Map a DATABASE_URL value to "sqlite" or "postgresql" without exposing it."""
    lowered = url.lower()
    if not url or lowered.startswith("sqlite"):
        return "sqlite"
    if lowered.startswith(_POSTGRES_PREFIXES):
        return "postgresql"
    raise ValueError(
        "Unsupported DATABASE_URL: use sqlite (or leave unset) or a postgres:// / postgresql:// URL"
    )


def _with_sslmode(url):
    """Append sslmode=require to the URL unless it already sets sslmode."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    if not any(key == "sslmode" for key, _ in query):
        query.append(("sslmode", "require"))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


class _PgRow(tuple):
    """PostgreSQL result row supporting row[0], row["column"], iteration, len and tuple equality.

    Mirrors sqlite3.Row: integer and slice indexes behave as in a tuple, and string
    keys are resolved through the column names taken from cursor.description.
    """

    def __new__(cls, values, names, index):
        obj = super().__new__(cls, values)
        obj._names = names
        obj._index = index
        return obj

    def __getitem__(self, key):
        if isinstance(key, str):
            try:
                return tuple.__getitem__(self, self._index[key])
            except KeyError:
                raise IndexError(f"No item with name {key!r}") from None
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._names)


def _pg_row_factory(cursor):
    """psycopg row factory: build _PgRow objects using this cursor's column names."""
    names = [col.name for col in cursor.description] if cursor.description else []
    index = {name: position for position, name in enumerate(names)}

    def make_row(values):
        return _PgRow(values, names, index)

    return make_row


class DBConnection:
    """Thin wrapper over a sqlite3 or psycopg connection with one SQL dialect API."""

    def __init__(self, raw, dialect):
        self._raw = raw
        self.dialect = dialect

    def _prepare(self, sql, has_params):
        """Convert "?" placeholders to "%s" for PostgreSQL, skipping string literals.

        When parameters are passed, literal "%" is doubled so psycopg does not
        read it as a placeholder.
        """
        if self.dialect != "postgresql":
            return sql
        out = []
        in_literal = False
        for ch in sql:
            if ch == "'":
                in_literal = not in_literal
                out.append(ch)
            elif ch == "?" and not in_literal:
                out.append("%s")
            elif ch == "%" and has_params:
                out.append("%%")
            else:
                out.append(ch)
        return "".join(out)

    def execute(self, sql, params=()):
        """Execute one statement and return a cursor (fetchone/fetchall/rowcount)."""
        params = tuple(params)
        cur = self._raw.cursor()
        if self.dialect == "postgresql":
            cur.execute(self._prepare(sql, bool(params)), params if params else None)
        else:
            cur.execute(sql, params)
        return cur

    def executemany(self, sql, seq):
        """Execute one statement once per parameter tuple in seq."""
        rows = [tuple(p) for p in seq]
        cur = self._raw.cursor()
        if self.dialect == "postgresql":
            cur.executemany(self._prepare(sql, True), rows)
        else:
            cur.executemany(sql, rows)
        return cur

    def insert_returning_id(self, sql, params=()):
        """Run an INSERT and return the new row's id.

        SQLite: returns cursor.lastrowid. PostgreSQL: appends " RETURNING id"
        and returns the fetched value.
        """
        if self.dialect == "postgresql":
            stmt = sql.rstrip().rstrip(";") + " RETURNING id"
            row = self.execute(stmt, params).fetchone()
            return row["id"]
        return self.execute(sql, params).lastrowid

    def executescript(self, script):
        """Run a multi-statement script. SQLite only; PostgreSQL uses execute()."""
        if self.dialect != "sqlite":
            raise RuntimeError("executescript is only available on SQLite")
        self._raw.executescript(script)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


def _open_connection():
    """Open a new connection for the configured database."""
    global IntegrityError
    url = _database_url()
    dialect = _resolve_dialect(url)
    if dialect == "postgresql":
        try:
            import psycopg
        except ImportError:
            raise RuntimeError(
                "DATABASE_URL points at PostgreSQL but the psycopg package is not installed"
            ) from None
        IntegrityError = psycopg.IntegrityError
        try:
            raw = psycopg.connect(
                _with_sslmode(url), autocommit=False, row_factory=_pg_row_factory
            )
        except psycopg.OperationalError:
            # Do not chain the driver error: its text may contain host details.
            raise RuntimeError("Could not connect to the PostgreSQL database") from None
        return DBConnection(raw, "postgresql")

    raw = sqlite3.connect(DB_PATH)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    IntegrityError = sqlite3.IntegrityError
    return DBConnection(raw, "sqlite")


def get_db():
    """Return the per-request database connection, creating it on first use."""
    if "db" not in g:
        g.db = _open_connection()
    return g.db


def close_db(exc=None):
    """Teardown handler: close the connection stored on flask.g, if any."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def get_database_info():
    """Return non-sensitive database details. Never includes the URL."""
    return {"dialect": _resolve_dialect(_database_url())}


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    parent_name TEXT,
    phone TEXT,
    email TEXT,
    address TEXT,
    monthly_fee_paise INTEGER NOT NULL DEFAULT 0,
    start_date TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    invoice_number TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    description TEXT,
    total_paise INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_items (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    unit_price_paise INTEGER NOT NULL,
    amount_paise INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    amount_paise INTEGER NOT NULL,
    payment_date TEXT NOT NULL,
    method TEXT,
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    category TEXT,
    amount_paise INTEGER NOT NULL,
    due_date TEXT NOT NULL,
    paid INTEGER NOT NULL DEFAULT 0,
    paid_date TEXT,
    recurring_monthly INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    theme TEXT NOT NULL DEFAULT 'light' CHECK (theme IN ('light', 'dark')),
    currency TEXT NOT NULL DEFAULT 'INR' CHECK (currency IN ('INR', 'USD', 'EUR', 'GBP')),
    settings_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dashboard_widgets (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    widget_key TEXT NOT NULL,
    visible INTEGER NOT NULL DEFAULT 1 CHECK (visible IN (0, 1)),
    position INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, widget_key)
);

CREATE INDEX IF NOT EXISTS idx_students_user_id ON students(user_id);
CREATE INDEX IF NOT EXISTS idx_invoices_user_id ON invoices(user_id);
CREATE INDEX IF NOT EXISTS idx_invoice_items_invoice_id ON invoice_items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_expenses_user_id ON expenses(user_id);
"""

# PostgreSQL equivalent, one statement per entry (run with execute(), never executescript).
SCHEMA_POSTGRES = [
    """CREATE TABLE IF NOT EXISTS users (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS students (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    parent_name TEXT,
    phone TEXT,
    email TEXT,
    address TEXT,
    monthly_fee_paise INTEGER NOT NULL DEFAULT 0,
    start_date TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS invoices (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    student_id INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    invoice_number TEXT NOT NULL,
    invoice_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    description TEXT,
    total_paise INTEGER NOT NULL,
    created_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS invoice_items (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    unit_price_paise INTEGER NOT NULL,
    amount_paise INTEGER NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS payments (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    amount_paise INTEGER NOT NULL,
    payment_date TEXT NOT NULL,
    method TEXT,
    note TEXT,
    created_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS expenses (
    id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    category TEXT,
    amount_paise INTEGER NOT NULL,
    due_date TEXT NOT NULL,
    paid INTEGER NOT NULL DEFAULT 0,
    paid_date TEXT,
    recurring_monthly INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS user_preferences (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    theme TEXT NOT NULL DEFAULT 'light' CHECK (theme IN ('light', 'dark')),
    currency TEXT NOT NULL DEFAULT 'INR' CHECK (currency IN ('INR', 'USD', 'EUR', 'GBP')),
    settings_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS dashboard_widgets (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    widget_key TEXT NOT NULL,
    visible INTEGER NOT NULL DEFAULT 1 CHECK (visible IN (0, 1)),
    position INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, widget_key)
)""",
    "CREATE INDEX IF NOT EXISTS idx_students_user_id ON students(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_invoices_user_id ON invoices(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_invoice_items_invoice_id ON invoice_items(invoice_id)",
    "CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_expenses_user_id ON expenses(user_id)",
]


def init_db():
    """Create all tables and indexes if they do not already exist."""
    conn = get_db()
    if conn.dialect == "postgresql":
        for statement in SCHEMA_POSTGRES:
            conn.execute(statement)
    else:
        conn.executescript(SCHEMA)
    conn.commit()


def init_app(app):
    """Register the teardown handler that closes the connection per app context."""
    app.teardown_appcontext(close_db)


def money_to_paise(text):
    """Parse a rupee amount such as "1234.50" or "1,234" into integer paise.

    Raises ValueError for empty, malformed, negative, or sub-paise input.
    """
    if text is None:
        raise ValueError("Amount is required")
    cleaned = str(text).strip().replace(",", "")
    if not cleaned:
        raise ValueError("Amount is required")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(f"Invalid amount: {text!r}")
    if not value.is_finite():
        raise ValueError(f"Invalid amount: {text!r}")
    if value < 0:
        raise ValueError("Amount cannot be negative")
    paise_value = value * 100
    if paise_value != paise_value.to_integral_value():
        raise ValueError("Amount cannot have more than 2 decimal places")
    return int(paise_value)


def format_inr(paise):
    """Format integer paise as an Indian-style rupee string, e.g. "₹1,234.50"."""
    sign = "-" if paise < 0 else ""
    rupees, frac = divmod(abs(int(paise)), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return f"{sign}\u20b9{digits}.{frac:02d}"
