"""Dashboard blueprint for BizFlow AI: headline numbers, 6-month chart, alerts and health score."""
import datetime

from flask import Blueprint, g, render_template

from auth import login_required
from db import format_inr, get_db
from finance import (
    FORECAST_HORIZON_DAYS,
    build_alerts,
    build_cashflow_forecast,
    compute_health_score,
    load_invoice_balances,
)
from forecast import DEFAULT_DELAY_DAYS, _run_whatif, get_cash_balance_paise

bp = Blueprint("dashboard", __name__)

CHART_MONTHS = 6
UPCOMING_DAYS = 30


def _largest_outstanding_impact(db, uid, today, cash_paise, invoices):
    """Return the delay impact for the student with the largest outstanding balance, or None.

    Balances are summed per student over invoices with a positive balance. Ties go to the
    lower student id. The minimum projected balances come from the same 90-day forecast
    and what-if used on the forecast page.
    """
    totals = {}
    names = {}
    for inv in invoices:
        if inv["balance_paise"] > 0:
            sid = inv["student_id"]
            totals[sid] = totals.get(sid, 0) + inv["balance_paise"]
            names[sid] = inv["student_name"]
    if not totals:
        return None
    student_id = max(totals, key=lambda sid: (totals[sid], -sid))

    _, summary = build_cashflow_forecast(db, uid, today, cash_paise, FORECAST_HORIZON_DAYS)
    whatif = _run_whatif(
        db, uid, today, cash_paise, summary, invoices,
        student_id, names[student_id], DEFAULT_DELAY_DAYS,
    )
    return {
        "student_id": student_id,
        "student_name": names[student_id],
        "outstanding": format_inr(totals[student_id]),
        "delay_days": DEFAULT_DELAY_DAYS,
        "original_min": format_inr(whatif["min_balance_original_paise"]),
        "revised_min": format_inr(whatif["min_balance_revised_paise"]),
    }


def _month_keys(today, count):
    """Return (year, month) tuples for the last `count` calendar months, oldest first."""
    keys = []
    year, month = today.year, today.month
    for _ in range(count):
        keys.append((year, month))
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    keys.reverse()
    return keys


def _monthly_sums(rows):
    """Turn (YYYY-MM, total) rows into a dict keyed by YYYY-MM."""
    return {row["ym"]: int(row["total"]) for row in rows}


@bp.route("/dashboard")
@login_required
def dashboard():
    db = get_db()
    uid = g.user["id"]
    today = datetime.date.today()
    today_iso = today.isoformat()
    cash_paise = get_cash_balance_paise()

    revenue_paise = int(db.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) FROM payments WHERE user_id = ?",
        (uid,),
    ).fetchone()[0])

    expenses_paid_paise = int(db.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) FROM expenses WHERE user_id = ? AND paid = 1",
        (uid,),
    ).fetchone()[0])

    invoices = load_invoice_balances(db, uid)
    outstanding_paise = sum(max(inv["balance_paise"], 0) for inv in invoices)
    overdue_count = sum(
        1 for inv in invoices if inv["balance_paise"] > 0 and inv["due_date"] < today
    )

    # Chart: last 6 calendar months, income by payment month, expenses by due month.
    month_keys = _month_keys(today, CHART_MONTHS)
    first_y, first_m = month_keys[0]
    window_start = datetime.date(first_y, first_m, 1).isoformat()

    income_by_month = _monthly_sums(db.execute(
        "SELECT substr(payment_date, 1, 7) AS ym, COALESCE(SUM(amount_paise), 0) AS total "
        "FROM payments WHERE user_id = ? AND payment_date >= ? GROUP BY ym",
        (uid, window_start),
    ).fetchall())
    expenses_by_month = _monthly_sums(db.execute(
        "SELECT substr(due_date, 1, 7) AS ym, COALESCE(SUM(amount_paise), 0) AS total "
        "FROM expenses WHERE user_id = ? AND due_date >= ? GROUP BY ym",
        (uid, window_start),
    ).fetchall())

    month_strings = [f"{year:04d}-{month:02d}" for year, month in month_keys]
    chart_data = {
        "labels": [
            datetime.date(year, month, 1).strftime("%b %Y") for year, month in month_keys
        ],
        "income": [round(income_by_month.get(ym, 0) / 100, 2) for ym in month_strings],
        "expenses": [round(expenses_by_month.get(ym, 0) / 100, 2) for ym in month_strings],
    }

    # Next 30 days: invoices with a balance due in the window, and unpaid expenses due in it.
    horizon = today + datetime.timedelta(days=UPCOMING_DAYS)
    expected_inflow_paise = sum(
        inv["balance_paise"]
        for inv in invoices
        if inv["balance_paise"] > 0 and today <= inv["due_date"] <= horizon
    )
    upcoming_expense = db.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(amount_paise), 0) AS total FROM expenses "
        "WHERE user_id = ? AND paid = 0 AND due_date BETWEEN ? AND ?",
        (uid, today_iso, horizon.isoformat()),
    ).fetchone()

    alerts = build_alerts(db, uid, today, opening_balance_paise=cash_paise)
    health = compute_health_score(db, uid, today, cash_paise)
    cash_impact = _largest_outstanding_impact(db, uid, today, cash_paise, invoices)

    summary = {
        "revenue": format_inr(revenue_paise),
        "expenses_paid": format_inr(expenses_paid_paise),
        "cash_balance": format_inr(cash_paise),
        "outstanding": format_inr(outstanding_paise),
        "overdue_count": overdue_count,
        "expected_inflow": format_inr(expected_inflow_paise),
        "upcoming_expense": format_inr(int(upcoming_expense["total"])),
        "upcoming_expense_count": int(upcoming_expense["n"]),
    }

    return render_template(
        "dashboard.html",
        today=today_iso,
        horizon=horizon.isoformat(),
        summary=summary,
        chart_data=chart_data,
        alerts=alerts,
        health=health,
        upcoming_days=UPCOMING_DAYS,
        cash_impact=cash_impact,
        impact_delay_days=DEFAULT_DELAY_DAYS,
    )
