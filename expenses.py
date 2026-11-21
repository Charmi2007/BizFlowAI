"""Expenses blueprint for BizFlow AI: list, add, mark paid/unpaid and delete tutor expenses."""
import datetime

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from auth import get_owned_or_404, login_required
from db import format_inr, get_db, money_to_paise

bp = Blueprint("expenses", __name__, url_prefix="/expenses")

CATEGORIES = ("Rent", "Electricity", "Salary", "Internet", "Transport", "Study material", "Other")


def _blank_form(today):
    return {
        "name": "",
        "category": "Other",
        "amount": "",
        "due_date": today.isoformat(),
        "recurring_monthly": False,
    }


def _parse_form(form):
    """Validate a POSTed expense. Returns (values, errors, clean)."""
    values = {
        "name": form.get("name", "").strip(),
        "category": form.get("category", "").strip(),
        "amount": form.get("amount", "").strip(),
        "due_date": form.get("due_date", "").strip(),
        "recurring_monthly": form.get("recurring_monthly") == "on",
    }
    errors = {}
    clean = {}

    if not values["name"]:
        errors["name"] = "Enter a name for the expense."
    else:
        clean["name"] = values["name"]

    if values["category"] not in CATEGORIES:
        errors["category"] = "Choose a category."
    else:
        clean["category"] = values["category"]

    try:
        amount = money_to_paise(values["amount"])
    except ValueError as exc:
        errors["amount"] = str(exc)
    else:
        if amount <= 0:
            errors["amount"] = "Amount must be greater than zero."
        else:
            clean["amount_paise"] = amount

    if not values["due_date"]:
        errors["due_date"] = "Enter a due date."
    else:
        try:
            clean["due_date"] = datetime.date.fromisoformat(values["due_date"]).isoformat()
        except ValueError:
            errors["due_date"] = "Enter a valid date in YYYY-MM-DD format."

    clean["recurring_monthly"] = 1 if values["recurring_monthly"] else 0
    return values, errors, clean


def _render_list(form, errors):
    """Render the expenses list with the given add-form values and field errors."""
    rows = get_db().execute(
        "SELECT * FROM expenses WHERE user_id = ? ORDER BY due_date, id",
        (g.user["id"],),
    ).fetchall()
    expenses = [
        dict(
            row,
            amount_label=format_inr(row["amount_paise"]),
            recurring=bool(row["recurring_monthly"]),
        )
        for row in rows
    ]
    return render_template(
        "expenses/list.html",
        expenses=expenses,
        categories=CATEGORIES,
        form=form,
        errors=errors,
    )


@bp.route("")
@login_required
def list_expenses():
    return _render_list(_blank_form(datetime.date.today()), {})


@bp.route("/new", methods=["POST"])
@login_required
def new_expense():
    values, errors, clean = _parse_form(request.form)
    if errors:
        flash("Please correct the highlighted fields.", "error")
        return _render_list(values, errors)

    created_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    db = get_db()
    db.execute(
        "INSERT INTO expenses (user_id, name, category, amount_paise, due_date, paid, "
        "paid_date, recurring_monthly, created_at) VALUES (?, ?, ?, ?, ?, 0, NULL, ?, ?)",
        (
            g.user["id"],
            clean["name"],
            clean["category"],
            clean["amount_paise"],
            clean["due_date"],
            clean["recurring_monthly"],
            created_at,
        ),
    )
    db.commit()
    flash(f"Expense '{clean['name']}' added.", "success")
    return redirect(url_for("expenses.list_expenses"))


@bp.route("/<int:expense_id>/toggle-paid", methods=["POST"])
@login_required
def toggle_paid(expense_id):
    expense = get_owned_or_404("expenses", expense_id)
    db = get_db()
    if expense["paid"]:
        db.execute(
            "UPDATE expenses SET paid = 0, paid_date = NULL WHERE id = ? AND user_id = ?",
            (expense_id, g.user["id"]),
        )
        flash("Expense marked unpaid.", "info")
    else:
        db.execute(
            "UPDATE expenses SET paid = 1, paid_date = ? WHERE id = ? AND user_id = ?",
            (datetime.date.today().isoformat(), expense_id, g.user["id"]),
        )
        flash("Expense marked paid.", "success")
    db.commit()
    return redirect(url_for("expenses.list_expenses"))


@bp.route("/<int:expense_id>/delete", methods=["POST"])
@login_required
def delete_expense(expense_id):
    get_owned_or_404("expenses", expense_id)
    db = get_db()
    db.execute(
        "DELETE FROM expenses WHERE id = ? AND user_id = ?",
        (expense_id, g.user["id"]),
    )
    db.commit()
    flash("Expense deleted.", "success")
    return redirect(url_for("expenses.list_expenses"))
