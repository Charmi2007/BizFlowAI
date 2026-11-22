"""Cash-flow forecast blueprint for BizFlow AI.

Shows a 90-day projected cash balance from the manually entered cash balance,
plus a what-if that delays one student's invoice payments.
"""
import datetime

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, session, url_for

from auth import login_required
from db import format_inr, get_db, money_to_paise
from finance import (
    FORECAST_HORIZON_DAYS,
    build_cashflow_forecast,
    load_invoice_balances,
    load_unpaid_expenses,
)
from simulation import parse_params, run_scenario

bp = Blueprint("forecast", __name__, url_prefix="/forecast")

DEFAULT_CASH_BALANCE_PAISE = 2500000  # Rs 25,000
DEFAULT_DELAY_DAYS = 15
MAX_DELAY_DAYS = 180
MILESTONE_DAYS = (30, 60, 90)


@bp.app_template_filter("inr")
def inr_filter(paise):
    """Jinja filter: format integer paise as an Indian rupee string."""
    return format_inr(paise)


def get_cash_balance_paise():
    """Return the cash balance in paise stored in the session (default Rs 25,000)."""
    return int(session.get("cash_balance_paise", DEFAULT_CASH_BALANCE_PAISE))


def _students_with_outstanding(invoices):
    """Return sorted (student_id, student_name) pairs that have an unpaid balance."""
    found = {}
    for inv in invoices:
        if inv["balance_paise"] > 0:
            found.setdefault(inv["student_id"], inv["student_name"])
    return sorted(found.items(), key=lambda pair: pair[1].lower())


def _parse_delay(raw):
    """Return (days, error). Blank input means the default delay."""
    text = (raw or "").strip()
    if text == "":
        return DEFAULT_DELAY_DAYS, None
    try:
        value = int(text)
    except ValueError:
        return None, "Delay must be a whole number of days."
    if not 0 <= value <= MAX_DELAY_DAYS:
        return None, f"Delay must be between 0 and {MAX_DELAY_DAYS} days."
    return value, None


def _milestone_rows(summary, start):
    """Return the day 30/60/90 balance cards as dicts."""
    return [
        {
            "days": days,
            "date": (start + datetime.timedelta(days=days)).isoformat(),
            "balance_paise": summary[f"balance_day{days}"],
        }
        for days in MILESTONE_DAYS
    ]


def _run_whatif(db, user_id, today, cash_paise, original_summary, invoices, student_id,
                student_name, delay_days):
    """Recompute the forecast with one student's open invoices delayed.

    Returns a dict with comparison rows, unpayable upcoming expenses, and the
    revised daily balances for the chart.
    """
    delays = {
        inv["id"]: delay_days
        for inv in invoices
        if inv["student_id"] == student_id and inv["balance_paise"] > 0
    }
    revised_daily, revised_summary = build_cashflow_forecast(
        db,
        user_id,
        today,
        cash_paise,
        FORECAST_HORIZON_DAYS,
        inflow_delay_by_invoice=delays,
    )
    revised_by_date = {entry["date"]: entry["balance_paise"] for entry in revised_daily}

    rows = []
    for days in MILESTONE_DAYS:
        original = original_summary[f"balance_day{days}"]
        revised = revised_summary[f"balance_day{days}"]
        rows.append({
            "days": days,
            "date": (today + datetime.timedelta(days=days)).isoformat(),
            "original_paise": original,
            "revised_paise": revised,
            "difference_paise": revised - original,
        })

    horizon_end = today + datetime.timedelta(days=FORECAST_HORIZON_DAYS)
    unpayable = []
    for exp in load_unpaid_expenses(db, user_id):
        if not (today <= exp["due_date"] <= horizon_end):
            continue
        balance = revised_by_date.get(exp["due_date"].isoformat())
        if balance is not None and balance < 0:
            unpayable.append({
                "name": exp["name"],
                "due_date": exp["due_date"].isoformat(),
                "amount_paise": exp["amount_paise"],
                "balance_paise": balance,
            })

    def _as_date(value):
        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, datetime.date):
            return value
        return datetime.date.fromisoformat(str(value))

    delayed = []  # (invoice dict, original_expected, revised_expected) as dates
    for inv in invoices:
        if inv["student_id"] != student_id or inv["balance_paise"] <= 0:
            continue
        due = _as_date(inv["due_date"])
        original_expected = max(due, today)
        revised_expected = original_expected + datetime.timedelta(days=delay_days)
        delayed.append((inv, due, original_expected, revised_expected))

    invoice_details = [
        {
            "invoice_id": inv["id"],
            "balance_paise": inv["balance_paise"],
            "due_date": due.isoformat(),
            "original_expected_date": original_expected.isoformat(),
            "revised_expected_date": revised_expected.isoformat(),
            "days_delayed": delay_days,
        }
        for inv, due, original_expected, revised_expected in delayed
    ]

    milestones_identical = [
        row["days"] for row in rows if row["revised_paise"] == row["original_paise"]
    ]

    milestone_explanation = []
    for row in rows:
        days = row["days"]
        milestone = today + datetime.timedelta(days=days)
        m_text = milestone.isoformat()
        if not delayed:
            milestone_explanation.append(
                f"Day {days} ({m_text}): no open invoices are delayed, so the balance is unchanged."
            )
        elif row["days"] in milestones_identical:
            if all(o <= milestone and r <= milestone for _, _, o, r in delayed):
                latest_o = max(o for _, _, o, _ in delayed)
                latest_r = max(r for _, _, _, r in delayed)
                milestone_explanation.append(
                    f"Day {days} ({m_text}): the cash arrives before this milestone in both "
                    f"scenarios (original expected {latest_o.isoformat()}, revised "
                    f"{latest_r.isoformat()}), so the balance is the same."
                )
            elif all(o > milestone and r > milestone for _, _, o, r in delayed):
                earliest_o = min(o for _, _, o, _ in delayed)
                earliest_r = min(r for _, _, _, r in delayed)
                milestone_explanation.append(
                    f"Day {days} ({m_text}): the cash has not arrived yet in either scenario "
                    f"(original expected {earliest_o.isoformat()}, revised "
                    f"{earliest_r.isoformat()}), so the balance is the same."
                )
            else:
                milestone_explanation.append(
                    f"Day {days} ({m_text}): some delayed cash moves, but the net balance "
                    f"at this milestone is the same."
                )
        else:
            crossing = next(
                ((o, r) for _, _, o, r in delayed if o <= milestone < r or r <= milestone < o),
                None,
            )
            if crossing is not None:
                o, r = crossing
                milestone_explanation.append(
                    f"Day {days} ({m_text}): the delayed payment moves across this milestone "
                    f"(original expected {o.isoformat()}, revised {r.isoformat()}), so the "
                    f"balance differs."
                )
            else:
                milestone_explanation.append(
                    f"Day {days} ({m_text}): the delayed payment moves across this milestone, "
                    f"so the balance differs."
                )

    delayed_past_horizon = sum(1 for _, _, _, r in delayed if r > horizon_end)

    # Lowest balance before/after the delay, and the first day each occurs.
    original_daily, _ = build_cashflow_forecast(db, user_id, today, cash_paise, FORECAST_HORIZON_DAYS)
    min_original = original_summary["min_balance_paise"]
    min_revised = revised_summary["min_balance_paise"]
    min_original_date = next(
        (entry["date"] for entry in original_daily if entry["balance_paise"] == min_original), None
    )
    min_revised_date = next(
        (entry["date"] for entry in revised_daily if entry["balance_paise"] == min_revised), None
    )

    # Days between the earliest original and latest revised expected date, clipped to horizon.
    delay_period = []
    if delay_days > 0 and delayed:
        original_by_date = {entry["date"]: entry["balance_paise"] for entry in original_daily}
        period_start = min(o for _, _, o, _ in delayed)
        period_end = min(max(r for _, _, _, r in delayed), horizon_end)
        day = period_start
        while day <= period_end:
            key = day.isoformat()
            if key in original_by_date and key in revised_by_date:
                delay_period.append({
                    "date": key,
                    "original_balance_paise": original_by_date[key],
                    "revised_balance_paise": revised_by_date[key],
                })
            day += datetime.timedelta(days=1)

    return {
        "student_name": student_name,
        "delay_days": delay_days,
        "rows": rows,
        "unpayable": unpayable,
        "revised_series": [round(entry["balance_paise"] / 100, 2) for entry in revised_daily],
        "invoices": invoice_details,
        "milestones_identical": milestones_identical,
        "milestone_explanation": milestone_explanation,
        "delayed_past_horizon": delayed_past_horizon,
        "min_balance_original_paise": min_original,
        "min_balance_revised_paise": min_revised,
        "min_balance_original_date": min_original_date,
        "min_balance_revised_date": min_revised_date,
        "delay_period": delay_period,
    }


@bp.route("/", strict_slashes=False)
@login_required
def index():
    db = get_db()
    user_id = g.user["id"]
    today = datetime.date.today()
    cash_paise = get_cash_balance_paise()

    daily, summary = build_cashflow_forecast(db, user_id, today, cash_paise, FORECAST_HORIZON_DAYS)
    invoices = load_invoice_balances(db, user_id)
    students_with_balance = _students_with_outstanding(invoices)
    student_names = dict(students_with_balance)

    delay_days, delay_error = _parse_delay(request.args.get("delay_days"))
    if delay_days is None:
        delay_days = DEFAULT_DELAY_DAYS

    selected_student_id = None
    whatif = None
    raw_student = request.args.get("student_id", "").strip()
    if raw_student:
        try:
            candidate = int(raw_student)
        except ValueError:
            candidate = None
        if candidate not in student_names:
            flash("Choose a student who has outstanding invoices.", "error")
        elif delay_error is not None:
            flash(delay_error, "error")
        else:
            selected_student_id = candidate
            whatif = _run_whatif(
                db,
                user_id,
                today,
                cash_paise,
                summary,
                invoices,
                candidate,
                student_names[candidate],
                delay_days,
            )

    chart_data = {
        "labels": [entry["date"] for entry in daily],
        "original": [round(entry["balance_paise"] / 100, 2) for entry in daily],
        "revised": whatif["revised_series"] if whatif else None,
        "revised_label": (
            f"Revised: {whatif['student_name']} pays {whatif['delay_days']} days later"
            if whatif else None
        ),
        "delay": None,
    }
    delay_period = whatif["delay_period"] if whatif else []
    if delay_period:
        chart_data["delay"] = {
            "labels": [entry["date"] for entry in delay_period],
            "original": [round(entry["original_balance_paise"] / 100, 2) for entry in delay_period],
            "revised": [round(entry["revised_balance_paise"] / 100, 2) for entry in delay_period],
        }

    return render_template(
        "forecast/index.html",
        today=today.isoformat(),
        cash_balance_paise=cash_paise,
        cash_balance_input=f"{cash_paise // 100}.{cash_paise % 100:02d}",
        milestones=_milestone_rows(summary, today),
        first_negative_date=summary["first_negative_date"],
        negative_days=[entry for entry in daily if entry["balance_paise"] < 0],
        outstanding_students=students_with_balance,
        selected_student_id=selected_student_id,
        delay_days=delay_days,
        whatif=whatif,
        chart_data=chart_data,
    )


@bp.route("/balance", methods=["POST"])
@login_required
def update_balance():
    try:
        paise = money_to_paise(request.form.get("cash_balance", ""))
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        session["cash_balance_paise"] = paise
        flash("Cash balance updated.", "success")
    return redirect(url_for("forecast.index"))


@bp.route("/simulate", methods=["GET"])
@login_required
def simulate():
    params, error = parse_params(request.args)
    if error:
        return jsonify({"error": error}), 400
    db = get_db()
    user_id = g.user["id"]
    today = datetime.date.today()
    result = run_scenario(db, user_id, today, get_cash_balance_paise(), params)
    return jsonify(result)
