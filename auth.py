"""Authentication blueprint and ownership helpers for BizFlow AI."""
import functools
import re
import sqlite3
from datetime import datetime, timezone

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_db

bp = Blueprint("auth", __name__)

MIN_PASSWORD_LENGTH = 8
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Tables that carry a user_id column and may be fetched via get_owned_or_404.
# invoice_items has no user_id; reach it through its invoice instead.
OWNED_TABLES = frozenset({"students", "invoices", "payments", "expenses"})


def load_logged_in_user():
    """before_request hook: populate g.user from the session (None if logged out)."""
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
    else:
        g.user = get_db().execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()


def login_required(view):
    """Redirect to the login page unless a user is loaded into g.user."""

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped


def get_owned_or_404(table, record_id):
    """Return the row from `table` only if it belongs to g.user; otherwise abort(404)."""
    if table not in OWNED_TABLES:
        raise ValueError(f"Table {table!r} does not support ownership lookups")
    if g.get("user") is None:
        abort(404)
    row = get_db().execute(
        f"SELECT * FROM {table} WHERE id = ? AND user_id = ?",
        (record_id, g.user["id"]),
    ).fetchone()
    if row is None:
        abort(404)
    return row


@bp.route("/register", methods=["GET", "POST"])
def register():
    form = {"name": "", "email": ""}
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        form = {"name": name, "email": email}

        error = None
        if not name:
            error = "Name is required."
        elif not EMAIL_RE.match(email):
            error = "Enter a valid email address."
        elif len(password) < MIN_PASSWORD_LENGTH:
            error = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        elif password != confirm:
            error = "Passwords do not match."

        db = get_db()
        if error is None and db.execute(
            "SELECT 1 FROM users WHERE email = ?", (email,)
        ).fetchone():
            error = "An account with this email already exists."

        if error is None:
            created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            try:
                cur = db.execute(
                    "INSERT INTO users (name, email, password_hash, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (name, email, generate_password_hash(password), created_at),
                )
                db.commit()
            except sqlite3.IntegrityError:
                error = "An account with this email already exists."
            else:
                session.clear()
                session["user_id"] = cur.lastrowid
                return redirect(url_for("dashboard.dashboard"))

        flash(error, "error")

    return render_template("register.html", form=form)


@bp.route("/login", methods=["GET", "POST"])
def login():
    email = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()
        if user is not None and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard.dashboard"))
        flash("Invalid email or password", "error")

    return render_template("login.html", email=email)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))
