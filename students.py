"""Student (customer) management blueprint for BizFlow AI."""
import datetime

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from auth import get_owned_or_404, login_required
from db import format_inr, get_db, money_to_paise

bp = Blueprint("students", __name__, url_prefix="/students")

TEXT_FIELDS = ("name", "parent_name", "phone", "email", "address")


def _escape_like(text):
    """Escape LIKE wildcards so user input matches literally (used with ESCAPE '!')."""
    return text.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _paise_to_input(paise):
    """Render integer paise as a plain rupee string for a form field, e.g. "1500.00"."""
    return f"{paise // 100}.{paise % 100:02d}"


def _blank_values():
    return {
        "name": "",
        "parent_name": "",
        "phone": "",
        "email": "",
        "address": "",
        "monthly_fee": "",
        "start_date": datetime.date.today().isoformat(),
        "active": True,
    }


def _values_from_row(row):
    return {
        "name": row["name"],
        "parent_name": row["parent_name"] or "",
        "phone": row["phone"] or "",
        "email": row["email"] or "",
        "address": row["address"] or "",
        "monthly_fee": _paise_to_input(row["monthly_fee_paise"]),
        "start_date": row["start_date"] or "",
        "active": bool(row["active"]),
    }


def _parse_form(form):
    """Validate POSTed student fields.

    Returns (values, errors, clean): values are the raw strings for re-rendering,
    errors maps field name to message, clean holds parsed DB-ready values.
    """
    values = {field: form.get(field, "").strip() for field in TEXT_FIELDS}
    values["monthly_fee"] = form.get("monthly_fee", "").strip()
    values["start_date"] = form.get("start_date", "").strip()
    values["active"] = "active" in form

    errors = {}
    clean = {}

    if not values["name"]:
        errors["name"] = "Name is required."
    if not values["parent_name"]:
        errors["parent_name"] = "Parent name is required."

    try:
        fee = money_to_paise(values["monthly_fee"])
    except ValueError as exc:
        errors["monthly_fee"] = str(exc)
    else:
        if fee < 0:
            errors["monthly_fee"] = "Monthly fee must be zero or more."
        clean["monthly_fee_paise"] = fee

    if not values["start_date"]:
        values["start_date"] = datetime.date.today().isoformat()
    try:
        start = datetime.date.fromisoformat(values["start_date"])
    except ValueError:
        errors["start_date"] = "Enter a valid date in YYYY-MM-DD format."
    else:
        clean["start_date"] = start.isoformat()

    clean["active"] = 1 if values["active"] else 0
    return values, errors, clean


@bp.route("")
@login_required
def list_students():
    q = request.args.get("q", "").strip()
    pattern = f"%{_escape_like(q.lower())}%"
    rows = get_db().execute(
        "SELECT * FROM students WHERE user_id = ? "
        "AND (LOWER(name) LIKE ? ESCAPE '!' OR LOWER(parent_name) LIKE ? ESCAPE '!') "
        "ORDER BY name COLLATE NOCASE",
        (g.user["id"], pattern, pattern),
    ).fetchall()
    students = [dict(row, fee_label=format_inr(row["monthly_fee_paise"])) for row in rows]
    return render_template("students/list.html", students=students, q=q)


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new_student():
    if request.method == "POST":
        values, errors, clean = _parse_form(request.form)
        if not errors:
            db = get_db()
            created_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
            db.execute(
                "INSERT INTO students (user_id, name, parent_name, phone, email, address, "
                "monthly_fee_paise, start_date, active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    g.user["id"],
                    values["name"],
                    values["parent_name"],
                    values["phone"],
                    values["email"],
                    values["address"],
                    clean["monthly_fee_paise"],
                    clean["start_date"],
                    clean["active"],
                    created_at,
                ),
            )
            db.commit()
            flash(f"Student {values['name']} added.", "success")
            return redirect(url_for("students.list_students"))
        flash("Please correct the highlighted fields.", "error")
    else:
        values, errors = _blank_values(), {}
    return render_template(
        "students/form.html", title="Add student", student=None, form=values, errors=errors
    )


@bp.route("/<int:student_id>")
@login_required
def student_detail(student_id):
    student = get_owned_or_404("students", student_id)
    uid = g.user["id"]
    db = get_db()

    payment_rows = db.execute(
        "SELECT p.payment_date, i.invoice_number, p.amount_paise, p.method "
        "FROM payments p JOIN invoices i ON i.id = p.invoice_id "
        "WHERE i.student_id = ? AND i.user_id = ? AND p.user_id = ? "
        "ORDER BY p.payment_date DESC, p.id DESC",
        (student_id, uid, uid),
    ).fetchall()
    payments = [dict(row, amount_label=format_inr(row["amount_paise"])) for row in payment_rows]

    invoiced = db.execute(
        "SELECT COALESCE(SUM(total_paise), 0) FROM invoices WHERE student_id = ? AND user_id = ?",
        (student_id, uid),
    ).fetchone()[0]
    paid = db.execute(
        "SELECT COALESCE(SUM(p.amount_paise), 0) FROM payments p "
        "JOIN invoices i ON i.id = p.invoice_id "
        "WHERE i.student_id = ? AND i.user_id = ? AND p.user_id = ?",
        (student_id, uid, uid),
    ).fetchone()[0]
    outstanding = invoiced - paid

    return render_template(
        "students/detail.html",
        student=student,
        fee_label=format_inr(student["monthly_fee_paise"]),
        payments=payments,
        invoiced_label=format_inr(invoiced),
        paid_label=format_inr(paid),
        outstanding_label=format_inr(outstanding),
        outstanding_paise=outstanding,
    )


@bp.route("/<int:student_id>/edit", methods=["GET", "POST"])
@login_required
def edit_student(student_id):
    student = get_owned_or_404("students", student_id)
    if request.method == "POST":
        values, errors, clean = _parse_form(request.form)
        if not errors:
            db = get_db()
            db.execute(
                "UPDATE students SET name = ?, parent_name = ?, phone = ?, email = ?, "
                "address = ?, monthly_fee_paise = ?, start_date = ?, active = ? "
                "WHERE id = ? AND user_id = ?",
                (
                    values["name"],
                    values["parent_name"],
                    values["phone"],
                    values["email"],
                    values["address"],
                    clean["monthly_fee_paise"],
                    clean["start_date"],
                    clean["active"],
                    student_id,
                    g.user["id"],
                ),
            )
            db.commit()
            flash(f"Student {values['name']} updated.", "success")
            return redirect(url_for("students.student_detail", student_id=student_id))
        flash("Please correct the highlighted fields.", "error")
    else:
        values, errors = _values_from_row(student), {}
    return render_template(
        "students/form.html",
        title=f"Edit {student['name']}",
        student=student,
        form=values,
        errors=errors,
    )


@bp.route("/<int:student_id>/delete", methods=["POST"])
@login_required
def delete_student(student_id):
    student = get_owned_or_404("students", student_id)
    db = get_db()
    db.execute(
        "DELETE FROM students WHERE id = ? AND user_id = ?",
        (student["id"], g.user["id"]),
    )
    db.commit()
    flash(f"Student {student['name']} deleted.", "success")
    return redirect(url_for("students.list_students"))
