"""Money and invoice-status helpers for BizFlow AI.

All amounts are integer paise. Invoice status is derived, never stored.
"""
import datetime

from db import format_inr


def compute_invoice_totals(items):
    """Return the sum of amount_paise over line items (dicts or sqlite rows)."""
    return sum(int(item["amount_paise"]) for item in items)


def paid_for_invoice(db, invoice_id):
    """Return the total paise paid against an invoice."""
    row = db.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) FROM payments WHERE invoice_id = ?",
        (invoice_id,),
    ).fetchone()
    return int(row[0])


def derive_status(total_paise, paid_paise, due_date_iso, today):
    """Return one of: "paid", "partially_paid", "overdue", "pending".

    `due_date_iso` is a YYYY-MM-DD string; `today` is a datetime.date.
    """
    balance = total_paise - paid_paise
    if balance <= 0:
        return "paid"
    due = datetime.date.fromisoformat(due_date_iso)
    if paid_paise > 0:
        return "partially_paid" if due >= today else "overdue"
    if due < today:
        return "overdue"
    return "pending"


# ---------------------------------------------------------------------------
# Cash-flow forecast, alerts and health score
# ---------------------------------------------------------------------------

FORECAST_HORIZON_DAYS = 90


def load_invoice_balances(db, user_id):
    """Return one dict per invoice owned by user_id with paid and balance in paise.

    Dates are datetime.date objects. Ordered by due date, then id.
    """
    rows = db.execute(
        "SELECT i.id, i.student_id, i.due_date, i.total_paise, s.name AS student_name, "
        "COALESCE((SELECT SUM(p.amount_paise) FROM payments p "
        "WHERE p.invoice_id = i.id AND p.user_id = i.user_id), 0) AS paid_paise "
        "FROM invoices i "
        "JOIN students s ON s.id = i.student_id AND s.user_id = i.user_id "
        "WHERE i.user_id = ? ORDER BY i.due_date, i.id",
        (user_id,),
    ).fetchall()
    result = []
    for row in rows:
        total = int(row["total_paise"])
        paid = int(row["paid_paise"])
        result.append({
            "id": row["id"],
            "student_id": row["student_id"],
            "student_name": row["student_name"],
            "due_date": datetime.date.fromisoformat(row["due_date"]),
            "total_paise": total,
            "paid_paise": paid,
            "balance_paise": total - paid,
        })
    return result


def load_unpaid_expenses(db, user_id):
    """Return unpaid expenses owned by user_id as dicts with a date due_date."""
    rows = db.execute(
        "SELECT id, name, amount_paise, due_date, recurring_monthly FROM expenses "
        "WHERE user_id = ? AND paid = 0 ORDER BY due_date, id",
        (user_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "amount_paise": int(row["amount_paise"]),
            "due_date": datetime.date.fromisoformat(row["due_date"]),
            "recurring": bool(row["recurring_monthly"]),
        }
        for row in rows
    ]


def _clamped_day(year, month, day):
    """Return the date with the given day in year/month, clamped to month length."""
    if month == 12:
        last = datetime.date(year, 12, 31)
    else:
        last = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    return datetime.date(year, month, min(day, last.day))


def _expense_occurrences(due, recurring, start, end):
    """Return the dates within [start, end] on which an unpaid expense falls due.

    A due date before start is treated as due on start. Recurring expenses also
    fall due on the same day-of-month in each later month, clamped to month length.
    """
    dates = []
    base = max(due, start)
    if base <= end:
        dates.append(base)
    if recurring:
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            candidate = _clamped_day(year, month, due.day)
            if due < candidate and start <= candidate <= end:
                dates.append(candidate)
            month += 1
            if month > 12:
                month = 1
                year += 1
    return dates


def build_cashflow_forecast(
    db,
    user_id,
    start_date,
    opening_balance_paise,
    horizon_days,
    inflow_delay_by_invoice=None,
):
    """Project a day-by-day cash balance for user_id.

    Returns (daily_entries, summary). Each daily entry has keys date (ISO string),
    inflow_paise, outflow_paise and balance_paise (running balance).

    Inflow: each invoice with a positive balance is expected on its due date, or on
    start_date if it is already past due. inflow_delay_by_invoice maps invoice id to
    extra days to add to that invoice's expected date.

    Outflow: each unpaid expense falls due on its due date (or start_date if past
    due). Recurring expenses also fall due monthly on the same day-of-month.
    """
    if horizon_days < 0:
        raise ValueError("horizon_days cannot be negative")
    delays = inflow_delay_by_invoice or {}
    end_date = start_date + datetime.timedelta(days=horizon_days)

    inflow = {}
    for inv in load_invoice_balances(db, user_id):
        if inv["balance_paise"] <= 0:
            continue
        when = max(inv["due_date"], start_date) + datetime.timedelta(
            days=int(delays.get(inv["id"], 0))
        )
        if when <= end_date:
            inflow[when] = inflow.get(when, 0) + inv["balance_paise"]

    outflow = {}
    for exp in load_unpaid_expenses(db, user_id):
        for when in _expense_occurrences(exp["due_date"], exp["recurring"], start_date, end_date):
            outflow[when] = outflow.get(when, 0) + exp["amount_paise"]

    daily_entries = []
    balance = int(opening_balance_paise)
    for offset in range(horizon_days + 1):
        day = start_date + datetime.timedelta(days=offset)
        in_paise = inflow.get(day, 0)
        out_paise = outflow.get(day, 0)
        balance += in_paise - out_paise
        daily_entries.append({
            "date": day.isoformat(),
            "inflow_paise": in_paise,
            "outflow_paise": out_paise,
            "balance_paise": balance,
        })

    balance_by_date = {entry["date"]: entry["balance_paise"] for entry in daily_entries}

    def balance_on_offset(days):
        return balance_by_date.get((start_date + datetime.timedelta(days=days)).isoformat())

    first_negative = next(
        (entry["date"] for entry in daily_entries if entry["balance_paise"] < 0),
        None,
    )
    summary = {
        "min_balance_paise": min(entry["balance_paise"] for entry in daily_entries),
        "first_negative_date": first_negative,
        "balance_day30": balance_on_offset(30),
        "balance_day60": balance_on_offset(60),
        "balance_day90": balance_on_offset(90),
    }
    return daily_entries, summary


def build_alerts(db, user_id, today, opening_balance_paise=0):
    """Return a list of alert dicts for user_id as of the date `today`.

    Each dict has keys severity ('danger' | 'warning' | 'info'), message,
    action_label and action_url.
    """
    alerts = []

    invoices = load_invoice_balances(db, user_id)
    overdue = [inv for inv in invoices if inv["balance_paise"] > 0 and inv["due_date"] < today]

    _, summary = build_cashflow_forecast(
        db, user_id, today, opening_balance_paise, FORECAST_HORIZON_DAYS
    )
    if summary["first_negative_date"] is not None:
        alerts.append({
            "severity": "danger",
            "message": (
                "Your projected cash balance may become negative on "
                f"{summary['first_negative_date']}. Follow up on pending invoices."
            ),
            "action_label": "Follow up",
            "action_url": "/followup",
        })

    if len(overdue) >= 3:
        alerts.append({
            "severity": "warning",
            "message": f"{len(overdue)} customer payments are overdue. Prioritise these follow-ups.",
            "action_label": "Open follow-ups",
            "action_url": "/followup",
        })

    for inv in overdue:
        days = (today - inv["due_date"]).days
        alerts.append({
            "severity": "danger",
            "message": (
                f"{inv['student_name']}'s {format_inr(inv['balance_paise'])} payment is "
                f"overdue by {days} days. Send a reminder today."
            ),
            "action_label": "Send reminder",
            "action_url": "/followup",
        })

    for exp in load_unpaid_expenses(db, user_id):
        days = (exp["due_date"] - today).days
        if 0 <= days <= 7:
            alerts.append({
                "severity": "warning",
                "message": (
                    f"Your {exp['name']} of {format_inr(exp['amount_paise'])} is due in {days} days."
                ),
                "action_label": "View expenses",
                "action_url": "/expenses",
            })

    return alerts


def compute_health_score(db, user_id, today, opening_balance_paise):
    """Return a 0-100 business health score for user_id with per-component detail.

    Components: collection (40), outstanding (25), cash_cover (20),
    expense_management (15). Each component's points are rounded to 1 decimal place.
    """
    components = []

    invoices = load_invoice_balances(db, user_id)

    due_so_far = [inv for inv in invoices if inv["due_date"] <= today]
    due_total = sum(inv["total_paise"] for inv in due_so_far)
    due_paid = sum(inv["paid_paise"] for inv in due_so_far)
    if due_total > 0:
        ratio = min(max(due_paid / due_total, 0.0), 1.0)
        collection_points = ratio * 40
    else:
        collection_points = 0.0
    components.append({
        "name": "Collection",
        "max_points": 40,
        "points": round(collection_points, 1),
        "detail": f"{format_inr(due_paid)} collected of {format_inr(due_total)} invoiced for due invoices",
    })

    invoiced_total = sum(inv["total_paise"] for inv in invoices)
    outstanding = sum(max(inv["balance_paise"], 0) for inv in invoices)
    if invoiced_total > 0:
        ratio = min(max(1 - outstanding / invoiced_total, 0.0), 1.0)
        outstanding_points = ratio * 25
    else:
        outstanding_points = 25.0
    components.append({
        "name": "Outstanding",
        "max_points": 25,
        "points": round(outstanding_points, 1),
        "detail": f"{format_inr(outstanding)} outstanding of {format_inr(invoiced_total)} invoiced",
    })

    _, summary = build_cashflow_forecast(
        db, user_id, today, opening_balance_paise, FORECAST_HORIZON_DAYS
    )
    if summary["first_negative_date"] is None:
        cover_points = 20
        cover_detail = "Projected balance stays non-negative for 90 days"
    elif summary["balance_day30"] is not None and summary["balance_day30"] > 0:
        cover_points = 10
        cover_detail = f"Projected balance at day 30 is {format_inr(summary['balance_day30'])}"
    else:
        cover_points = 0
        cover_detail = f"Projected balance may go negative on {summary['first_negative_date']}"
    components.append({
        "name": "Cash cover",
        "max_points": 20,
        "points": round(float(cover_points), 1),
        "detail": cover_detail,
    })

    due_expense_rows = db.execute(
        "SELECT paid FROM expenses WHERE user_id = ? AND due_date <= ?",
        (user_id, today.isoformat()),
    ).fetchall()
    due_expense_count = len(due_expense_rows)
    paid_expense_count = sum(1 for row in due_expense_rows if int(row["paid"]) == 1)
    if due_expense_count > 0:
        expense_points = (paid_expense_count / due_expense_count) * 15
    else:
        expense_points = 15.0
    components.append({
        "name": "Expense management",
        "max_points": 15,
        "points": round(expense_points, 1),
        "detail": f"{paid_expense_count} of {due_expense_count} expenses due so far are paid",
    })

    total = sum(component["points"] for component in components)
    total_points = min(max(int(round(total)), 0), 100)
    return {"total_points": total_points, "components": components}

