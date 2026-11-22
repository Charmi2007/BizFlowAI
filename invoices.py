"""Invoice blueprint for BizFlow AI: list, create, detail and printable views."""
import datetime

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from auth import get_owned_or_404, login_required
from db import format_inr, get_db, money_to_paise
from finance import compute_invoice_totals, derive_status, paid_for_invoice

bp = Blueprint("invoices", __name__, url_prefix="/invoices")

MAX_ITEMS = 10
DEFAULT_DUE_DAYS = 10
STATUS_FILTER_OPTIONS = [
    ("all", "All"),
    ("paid", "Paid"),
    ("partially_paid", "Partially paid"),
    ("pending", "Pending"),
    ("overdue", "Overdue"),
]
STATUS_LABELS = {
    "paid": "Paid",
    "partially_paid": "Partially paid",
    "pending": "Pending",
    "overdue": "Overdue",
}
STATUS_BADGES = {
    "paid": "badge-paid",
    "partially_paid": "badge-partial",
    "pending": "badge-pending",
    "overdue": "badge-overdue",
}


def _invoice_view(row, paid_paise, today):
    """Wrap an invoice row with derived status and display labels."""
    total = row["total_paise"]
    balance = total - paid_paise
    status = derive_status(total, paid_paise, row["due_date"], today)
    return dict(
        row,
        status=status,
        status_label=STATUS_LABELS[status],
        badge_class=STATUS_BADGES[status],
        paid_paise=paid_paise,
        balance_paise=balance,
        total_label=format_inr(total),
        paid_label=format_inr(paid_paise),
        balance_label=format_inr(balance),
    )


def _next_invoice_number(db, user_id, year):
    """Return INV-<year>-NNNN with NNNN one more than the user's highest sequence that year."""
    prefix = f"INV-{year}-"
    rows = db.execute(
        "SELECT invoice_number FROM invoices WHERE user_id = ? AND invoice_number LIKE ?",
        (user_id, prefix + "%"),
    ).fetchall()
    highest = 0
    for row in rows:
        suffix = row["invoice_number"][len(prefix):]
        if suffix.isdigit():
            highest = max(highest, int(suffix))
    return f"{prefix}{highest + 1:04d}"


def _blank_item():
    return {"description": "", "quantity": "1", "unit_price": ""}


def _blank_values(today):
    return {
        "student_id": "",
        "invoice_date": today.isoformat(),
        "due_date": (today + datetime.timedelta(days=DEFAULT_DUE_DAYS)).isoformat(),
        "description": "",
        "items": [],
    }


def _read_item_rows(form):
    """Return raw line-item rows as stripped strings, dropping fully blank rows."""
    descriptions = form.getlist("item_description")
    quantities = form.getlist("item_quantity")
    prices = form.getlist("item_unit_price")

    def at(values, index):
        return values[index].strip() if index < len(values) else ""

    rows = []
    for index in range(max(len(descriptions), len(quantities), len(prices))):
        row = {
            "description": at(descriptions, index),
            "quantity": at(quantities, index),
            "unit_price": at(prices, index),
        }
        if any(row.values()):
            rows.append(row)
    return rows


def _parse_date(values, field, default, errors):
    """Parse values[field] as ISO date; fall back to default when blank."""
    raw = values[field]
    if not raw:
        values[field] = default.isoformat()
        return default
    try:
        parsed = datetime.date.fromisoformat(raw)
    except ValueError:
        errors[field] = "Enter a valid date in YYYY-MM-DD format."
        return None
    return parsed


def _parse_form(form, student_ids, today):
    """Validate a POSTed invoice.

    Returns (values, errors, clean). The client total is ignored; totals are recomputed here.
    """
    values = {
        "student_id": form.get("student_id", "").strip(),
        "invoice_date": form.get("invoice_date", "").strip(),
        "due_date": form.get("due_date", "").strip(),
        "description": form.get("description", "").strip(),
        "items": _read_item_rows(form),
    }
    errors = {}
    clean = {}

    try:
        student_id = int(values["student_id"])
    except ValueError:
        student_id = None
    if student_id in student_ids:
        clean["student_id"] = student_id
    else:
        errors["student_id"] = "Choose a student."

    invoice_date = _parse_date(values, "invoice_date", today, errors)
    base = invoice_date or today
    due_date = _parse_date(
        values, "due_date", base + datetime.timedelta(days=DEFAULT_DUE_DAYS), errors
    )
    if invoice_date is not None:
        clean["invoice_date"] = invoice_date.isoformat()
    if due_date is not None and invoice_date is not None:
        if due_date < invoice_date:
            errors["due_date"] = "Due date must be on or after the invoice date."
        else:
            clean["due_date"] = due_date.isoformat()

    if len(values["items"]) > MAX_ITEMS:
        errors["items"] = f"Use at most {MAX_ITEMS} line items."

    line_items = []
    for number, row in enumerate(values["items"], start=1):
        if not row["description"]:
            errors.setdefault("items", f"Line {number}: enter a description.")
            continue
        try:
            quantity = int(row["quantity"])
        except ValueError:
            quantity = 0
        if quantity < 1:
            errors.setdefault(
                "items", f"Line {number}: quantity must be a whole number of at least 1."
            )
            continue
        try:
            unit_price = money_to_paise(row["unit_price"])
        except ValueError as exc:
            errors.setdefault("items", f"Line {number}: {exc}")
            continue
        line_items.append(
            {
                "description": row["description"],
                "quantity": quantity,
                "unit_price_paise": unit_price,
                "amount_paise": quantity * unit_price,
            }
        )

    if not line_items and "items" not in errors:
        errors["items"] = "Add at least one line item with a description."

    clean["items"] = line_items
    clean["total_paise"] = compute_invoice_totals(line_items)
    return values, errors, clean


def _owned_students(db, user_id):
    return db.execute(
        "SELECT id, name FROM students WHERE user_id = ? ORDER BY LOWER(name)",
        (user_id,),
    ).fetchall()


def _load_invoice(invoice_id):
    """Return (invoice_view, student, items, payments) for an invoice owned by g.user."""
    invoice = get_owned_or_404("invoices", invoice_id)
    student = get_owned_or_404("students", invoice["student_id"])
    db = get_db()
    uid = g.user["id"]

    item_rows = db.execute(
        "SELECT * FROM invoice_items WHERE invoice_id = ? ORDER BY id",
        (invoice_id,),
    ).fetchall()
    items = [
        dict(
            row,
            unit_price_label=format_inr(row["unit_price_paise"]),
            amount_label=format_inr(row["amount_paise"]),
        )
        for row in item_rows
    ]

    payment_rows = db.execute(
        "SELECT * FROM payments WHERE invoice_id = ? AND user_id = ? "
        "ORDER BY payment_date, id",
        (invoice_id, uid),
    ).fetchall()
    payments = [dict(row, amount_label=format_inr(row["amount_paise"])) for row in payment_rows]

    paid = paid_for_invoice(db, invoice_id)
    view = _invoice_view(invoice, paid, datetime.date.today())
    return view, student, items, payments


@bp.route("")
@login_required
def list_invoices():
    status_filter = request.args.get("status", "all")
    valid = {value for value, _ in STATUS_FILTER_OPTIONS}
    if status_filter not in valid:
        status_filter = "all"

    today = datetime.date.today()
    rows = get_db().execute(
        "SELECT i.*, s.name AS student_name, "
        "COALESCE((SELECT SUM(p.amount_paise) FROM payments p "
        "WHERE p.invoice_id = i.id AND p.user_id = i.user_id), 0) AS paid_paise "
        "FROM invoices i JOIN students s ON s.id = i.student_id AND s.user_id = i.user_id "
        "WHERE i.user_id = ? ORDER BY i.invoice_date DESC, i.id DESC",
        (g.user["id"],),
    ).fetchall()
    invoices = [_invoice_view(row, row["paid_paise"], today) for row in rows]
    if status_filter != "all":
        invoices = [inv for inv in invoices if inv["status"] == status_filter]

    return render_template(
        "invoices/list.html",
        invoices=invoices,
        status_filter=status_filter,
        status_options=STATUS_FILTER_OPTIONS,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new_invoice():
    db = get_db()
    uid = g.user["id"]
    today = datetime.date.today()
    students = _owned_students(db, uid)
    student_ids = {row["id"] for row in students}

    if request.method == "POST":
        values, errors, clean = _parse_form(request.form, student_ids, today)
        if not errors:
            created_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            try:
                if db.dialect == "sqlite":
                    db.execute("BEGIN IMMEDIATE")
                number = _next_invoice_number(db, uid, clean["invoice_date"][:4])
                invoice_id = db.insert_returning_id(
                    "INSERT INTO invoices (user_id, student_id, invoice_number, invoice_date, "
                    "due_date, description, total_paise, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        uid,
                        clean["student_id"],
                        number,
                        clean["invoice_date"],
                        clean["due_date"],
                        values["description"],
                        clean["total_paise"],
                        created_at,
                    ),
                )
                db.executemany(
                    "INSERT INTO invoice_items (invoice_id, description, quantity, "
                    "unit_price_paise, amount_paise) VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            invoice_id,
                            item["description"],
                            item["quantity"],
                            item["unit_price_paise"],
                            item["amount_paise"],
                        )
                        for item in clean["items"]
                    ],
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            flash(f"Invoice {number} created.", "success")
            return redirect(url_for("invoices.invoice_detail", invoice_id=invoice_id))
        flash("Please correct the highlighted fields.", "error")
    else:
        values, errors = _blank_values(today), {}
        preselect = request.args.get("student_id", "")
        try:
            if int(preselect) in student_ids:
                values["student_id"] = str(int(preselect))
        except ValueError:
            pass

    if not values["items"]:
        values["items"] = [_blank_item()]
    return render_template(
        "invoices/form.html",
        form=values,
        errors=errors,
        students=students,
    )


@bp.route("/<int:invoice_id>")
@login_required
def invoice_detail(invoice_id):
    invoice, student, items, payments = _load_invoice(invoice_id)
    return render_template(
        "invoices/detail.html",
        invoice=invoice,
        student=student,
        items=items,
        payments=payments,
    )


@bp.route("/<int:invoice_id>/print")
@login_required
def print_invoice(invoice_id):
    invoice, student, items, _payments = _load_invoice(invoice_id)
    return render_template(
        "invoices/print.html",
        invoice=invoice,
        student=student,
        items=items,
        tutor_name=g.user["name"],
    )


PAYMENT_METHODS = ("cash", "UPI", "bank transfer", "cheque", "other")
MAX_NOTE_LENGTH = 200


@bp.route("/<int:invoice_id>/payments/new", methods=["GET", "POST"])
@login_required
def new_payment(invoice_id):
    db = get_db()
    uid = g.user["id"]
    invoice = get_owned_or_404("invoices", invoice_id)
    today = datetime.date.today()
    balance = invoice["total_paise"] - paid_for_invoice(db, invoice_id)

    if balance <= 0:
        flash("This invoice has no balance left to pay.", "error")
        return redirect(url_for("invoices.invoice_detail", invoice_id=invoice_id))

    if request.method == "POST":
        values = {
            "amount": request.form.get("amount", "").strip(),
            "payment_date": request.form.get("payment_date", "").strip(),
            "method": request.form.get("method", "").strip(),
            "note": request.form.get("note", "").strip(),
        }
        errors = {}
        clean = {}

        try:
            amount = money_to_paise(values["amount"])
        except ValueError as exc:
            errors["amount"] = str(exc)
        else:
            if amount <= 0:
                errors["amount"] = "Amount must be greater than zero."
            elif amount > balance:
                errors["amount"] = f"Amount cannot exceed the balance of {format_inr(balance)}."
            else:
                clean["amount_paise"] = amount

        if not values["payment_date"]:
            errors["payment_date"] = "Enter a payment date."
        else:
            try:
                clean["payment_date"] = datetime.date.fromisoformat(
                    values["payment_date"]
                ).isoformat()
            except ValueError:
                errors["payment_date"] = "Enter a valid date in YYYY-MM-DD format."

        if values["method"] not in PAYMENT_METHODS:
            errors["method"] = "Choose a payment method."
        if len(values["note"]) > MAX_NOTE_LENGTH:
            errors["note"] = f"Keep the note under {MAX_NOTE_LENGTH} characters."

        if not errors:
            created_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            db.execute(
                "INSERT INTO payments (user_id, invoice_id, amount_paise, payment_date, "
                "method, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    uid,
                    invoice_id,
                    clean["amount_paise"],
                    clean["payment_date"],
                    values["method"],
                    values["note"] or None,
                    created_at,
                ),
            )
            db.commit()
            flash("Payment recorded", "success")
            return redirect(url_for("invoices.invoice_detail", invoice_id=invoice_id))
        flash("Please correct the highlighted fields.", "error")
    else:
        values = {
            "amount": f"{balance // 100}.{balance % 100:02d}",
            "payment_date": today.isoformat(),
            "method": "cash",
            "note": "",
        }
        errors = {}

    return render_template(
        "invoices/payment_form.html",
        invoice=invoice,
        balance_label=format_inr(balance),
        form=values,
        errors=errors,
        methods=PAYMENT_METHODS,
    )


@bp.route("/<int:invoice_id>/payments/<int:payment_id>/delete", methods=["POST"])
@login_required
def delete_payment(invoice_id, payment_id):
    get_owned_or_404("invoices", invoice_id)
    db = get_db()
    cur = db.execute(
        "DELETE FROM payments WHERE id = ? AND invoice_id = ? AND user_id = ?",
        (payment_id, invoice_id, g.user["id"]),
    )
    if cur.rowcount == 0:
        db.rollback()
        abort(404)
    db.commit()
    flash("Payment deleted.", "success")
    return redirect(url_for("invoices.invoice_detail", invoice_id=invoice_id))
