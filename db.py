"""SQLite database helpers for BizFlow AI.

Money is stored as integer paise everywhere. Dates are ISO strings (YYYY-MM-DD).
"""
import os
import sqlite3
from decimal import Decimal, InvalidOperation

from flask import g

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")


def get_db():
    """Return the per-request SQLite connection, creating it on first use."""
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(exc=None):
    """Teardown handler: close the connection stored on flask.g, if any."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


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

CREATE INDEX IF NOT EXISTS idx_students_user_id ON students(user_id);
CREATE INDEX IF NOT EXISTS idx_invoices_user_id ON invoices(user_id);
CREATE INDEX IF NOT EXISTS idx_invoice_items_invoice_id ON invoice_items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_expenses_user_id ON expenses(user_id);
"""


def init_db():
    """Create all tables and indexes if they do not already exist."""
    conn = get_db()
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
