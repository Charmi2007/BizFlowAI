"""Payment follow-up blueprint for BizFlow AI: outstanding invoices and reminder messages."""
import datetime

from flask import Blueprint, g, render_template, request

from auth import login_required
from db import format_inr, get_db
from finance import derive_status, paid_for_invoice
from invoices import STATUS_BADGES, STATUS_LABELS

bp = Blueprint("followup", __name__, url_prefix="/followup")

UPI_PLACEHOLDER = "[your UPI ID]"
ONLY_OPTIONS = ("overdue",)


def build_reminder(
    parent_name,
    student_name,
    amount_text,
    invoice_number,
    days_overdue,
    due_date_text,
    tutor_name=None,
):
    """Return a personalised fee reminder for a parent.

    Overdue invoices get a firm reminder; anything not yet overdue gets a friendly
    pre-due note. No real payment details are included; UPI_PLACEHOLDER stands in.
    """
    if days_overdue > 0:
        body = (
            f"Dear {parent_name}, the fee of {amount_text} for {student_name} "
            f"(invoice {invoice_number}, due {due_date_text}) is {days_overdue} days overdue. "
            "Kindly pay at the earliest."
        )
    else:
        body = (
            f"Dear {parent_name}, a friendly reminder that the fee of {amount_text} for "
            f"{student_name} (invoice {invoice_number}) is due on {due_date_text}. "
            "Kindly pay by then."
        )
    signoff = f"Thank you - {tutor_name}." if tutor_name else "Thank you."
    return f"{body} Pay via UPI to {UPI_PLACEHOLDER}. {signoff}"


@bp.route("")
@login_required
def list_followup():
    only = request.args.get("only", "")
    if only not in ONLY_OPTIONS:
        only = ""

    db = get_db()
    today = datetime.date.today()
    tutor_name = g.user["name"]
    rows = db.execute(
        "SELECT i.id, i.invoice_number, i.due_date, i.total_paise, "
        "s.name AS student_name, s.parent_name, s.phone "
        "FROM invoices i JOIN students s ON s.id = i.student_id AND s.user_id = i.user_id "
        "WHERE i.user_id = ? ORDER BY i.due_date, i.id",
        (g.user["id"],),
    ).fetchall()

    items = []
    for row in rows:
        paid = paid_for_invoice(db, row["id"])
        balance = row["total_paise"] - paid
        if balance <= 0:
            continue
        due = datetime.date.fromisoformat(row["due_date"])
        days_overdue = max((today - due).days, 0)
        status = derive_status(row["total_paise"], paid, row["due_date"], today)
        balance_label = format_inr(balance)
        student_name = row["student_name"]
        parent_name = row["parent_name"] or "Parent"
        items.append(
            dict(
                id=row["id"],
                textarea_id=f"reminder-{row['id']}",
                student_name=student_name,
                parent_name=parent_name,
                phone=row["phone"] or "",
                invoice_number=row["invoice_number"],
                due_date=row["due_date"],
                balance_paise=balance,
                balance_label=balance_label,
                days_overdue=days_overdue,
                status=status,
                status_label=STATUS_LABELS[status],
                badge_class=STATUS_BADGES[status],
                needs_attention=due <= today,
                reminder=build_reminder(
                    parent_name,
                    student_name,
                    balance_label,
                    row["invoice_number"],
                    days_overdue,
                    row["due_date"],
                    tutor_name=tutor_name,
                ),
            )
        )

    items.sort(key=lambda item: item["days_overdue"], reverse=True)

    total_outstanding = sum(item["balance_paise"] for item in items)
    overdue_count = sum(1 for item in items if item["status"] == "overdue")
    attention_count = sum(1 for item in items if item["needs_attention"])

    visible = items
    if only == "overdue":
        visible = [item for item in items if item["status"] == "overdue"]

    return render_template(
        "followup/list.html",
        items=visible,
        only=only,
        total_count=len(items),
        total_outstanding_label=format_inr(total_outstanding),
        overdue_count=overdue_count,
        attention_count=attention_count,
    )
