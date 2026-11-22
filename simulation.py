"""Interactive what-if scenarios for the Forecast simulation studio.

Pure calculations: every function here only reads from the database (SELECT
statements) and never writes. All amounts are integer paise; dates are
datetime.date objects, serialised as ISO strings in the returned dict.
"""
import datetime

from db import format_inr, money_to_paise
from finance import (
    FORECAST_HORIZON_DAYS,
    build_cashflow_forecast,
    compute_health_score,
    load_invoice_balances,
)

DEFAULT_PARAMS = {
    "delay_days": 15,
    "collection_pct": 100,
    "revenue_change_pct": 0,
    "extra_monthly_expense_paise": 0,
}
NEUTRAL_PARAMS = {
    "delay_days": 0,
    "collection_pct": 100,
    "revenue_change_pct": 0,
    "extra_monthly_expense_paise": 0,
}
MAX_DELAY_DAYS = 180
MIN_COLLECTION_PCT = 0
MAX_COLLECTION_PCT = 100
MIN_REVENUE_CHANGE_PCT = -50
MAX_REVENUE_CHANGE_PCT = 100
MAX_EXTRA_MONTHLY_PAISE = 100000000  # Rs 10,00,000
ACTUAL_MONTHS = 6


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _as_date(value):
    """Return value as a datetime.date (accepts a date or an ISO string)."""
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value))


def _int_param(raw, key, label, default, low, high):
    """Return (value, error) for an integer request field. Blank means default."""
    text = raw.get(key)
    if text is None or str(text).strip() == "":
        return default, None
    try:
        value = int(str(text).strip())
    except ValueError:
        return None, f"{label} must be a whole number."
    if value < low or value > high:
        return None, f"{label} must be between {low} and {high}."
    return value, None


def parse_params(raw):
    """Parse simulation inputs from a mapping of optional strings.

    Returns (params_dict, None) on success or (None, message) on error.
    The extra monthly expense is given in rupees and returned in paise.
    """
    raw = raw or {}
    delay, error = _int_param(
        raw, "delay_days", "Delay", DEFAULT_PARAMS["delay_days"], 0, MAX_DELAY_DAYS
    )
    if error:
        return None, error
    collection, error = _int_param(
        raw, "collection_pct", "Collection percentage",
        DEFAULT_PARAMS["collection_pct"], MIN_COLLECTION_PCT, MAX_COLLECTION_PCT,
    )
    if error:
        return None, error
    revenue, error = _int_param(
        raw, "revenue_change_pct", "Revenue change",
        DEFAULT_PARAMS["revenue_change_pct"], MIN_REVENUE_CHANGE_PCT, MAX_REVENUE_CHANGE_PCT,
    )
    if error:
        return None, error

    extra_text = raw.get("extra_monthly_expense")
    if extra_text is None or str(extra_text).strip() == "":
        extra = DEFAULT_PARAMS["extra_monthly_expense_paise"]
    else:
        try:
            extra = int(money_to_paise(str(extra_text).strip()))
        except ValueError:
            return None, "Extra monthly expense must be a valid rupee amount."
        if extra < 0 or extra > MAX_EXTRA_MONTHLY_PAISE:
            return None, "Extra monthly expense must be between Rs 0 and Rs 10,00,000."

    return {
        "delay_days": delay,
        "collection_pct": collection,
        "revenue_change_pct": revenue,
        "extra_monthly_expense_paise": extra,
    }, None


# ---------------------------------------------------------------------------
# Health and summaries
# ---------------------------------------------------------------------------

def cash_cover_points(summary):
    """Return cash-cover points using the same rule as compute_health_score."""
    if summary.get("first_negative_date") is None:
        return 20.0
    day30 = summary.get("balance_day30_paise", summary.get("balance_day30"))
    if day30 is not None and day30 > 0:
        return 10.0
    return 0.0


def _summarise(dates, balances):
    """Summarise one daily balance series. dates are ISO strings."""
    min_balance = min(balances)
    first_negative = None
    for day, balance in zip(dates, balances):
        if balance < 0:
            first_negative = day
            break
    return {
        "closing_balance_paise": balances[-1],
        "min_balance_paise": min_balance,
        "min_balance_date": dates[balances.index(min_balance)],
        "first_negative_date": first_negative,
        "balance_day30_paise": balances[30],
        "balance_day60_paise": balances[60],
        "balance_day90_paise": balances[90],
    }


def _shift_month(year, month, delta):
    """Return (year, month) moved by delta calendar months."""
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def _month_label(start):
    return f"{start.year:04d}-{start.month:02d}"


def _actual_month_totals(db, user_id, today):
    """Return the ACTUAL_MONTHS calendar months before today's month, oldest first."""
    labels, income, expenses = [], [], []
    for delta in range(-ACTUAL_MONTHS, 0):
        y, m = _shift_month(today.year, today.month, delta)
        start = datetime.date(y, m, 1)
        ny, nm = _shift_month(y, m, 1)
        end = datetime.date(ny, nm, 1)
        paid_row = db.execute(
            "SELECT COALESCE(SUM(amount_paise), 0) AS total FROM payments "
            "WHERE user_id = ? AND payment_date >= ? AND payment_date < ?",
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchone()
        due_row = db.execute(
            "SELECT COALESCE(SUM(amount_paise), 0) AS total FROM expenses "
            "WHERE user_id = ? AND due_date >= ? AND due_date < ?",
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchone()
        labels.append(_month_label(start))
        income.append(int(paid_row["total"]))
        expenses.append(int(due_row["total"]))
    return labels, income, expenses


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------

def run_scenario(db, user_id, today, opening_balance_paise, params):
    """Return the baseline, scenario and delay-only projections for params.

    Read-only: uses SELECT statements only and never writes to any table.
    """
    opening = int(opening_balance_paise)
    delay_days = int(params["delay_days"])
    collection_pct = int(params["collection_pct"])
    revenue_pct = int(params["revenue_change_pct"])
    extra_monthly = int(params["extra_monthly_expense_paise"])
    horizon_end = today + datetime.timedelta(days=FORECAST_HORIZON_DAYS)

    # a/b: baseline forecast
    baseline_daily, _ = build_cashflow_forecast(
        db, user_id, today, opening, FORECAST_HORIZON_DAYS
    )

    # c/d/e: delayed forecast (inflows only are delayed)
    invoices = load_invoice_balances(db, user_id)
    open_invoices = [inv for inv in invoices if inv["balance_paise"] > 0]
    delays = (
        {inv["id"]: delay_days for inv in open_invoices} if delay_days > 0 else None
    )
    delayed_daily, _ = build_cashflow_forecast(
        db, user_id, today, opening, FORECAST_HORIZON_DAYS, inflow_delay_by_invoice=delays
    )

    # f: day-by-day scenario and delay-only balances
    dates, baseline_bal, scenario_bal, delay_bal = [], [], [], []
    scenario_in_list, scenario_out_list = [], []
    bal_delay_only = opening
    bal_scenario = opening
    additional_expenses = 0
    for i, base in enumerate(baseline_daily):
        day = _as_date(base["date"])
        delayed_in = int(delayed_daily[i]["inflow_paise"])
        out = int(base["outflow_paise"])

        bal_delay_only += delayed_in - out

        scenario_in = (delayed_in * collection_pct * (100 + revenue_pct) + 5000) // 10000
        extra = extra_monthly if day.day == 1 else 0
        if extra:
            additional_expenses += extra
        scenario_out = out + extra
        bal_scenario += scenario_in - scenario_out

        dates.append(day.isoformat())
        baseline_bal.append(int(base["balance_paise"]))
        scenario_bal.append(bal_scenario)
        delay_bal.append(bal_delay_only)
        scenario_in_list.append(scenario_in)
        scenario_out_list.append(scenario_out)

    # g: summaries, all computed from the daily series
    baseline_summary = _summarise(dates, baseline_bal)
    scenario_summary = _summarise(dates, scenario_bal)
    delay_summary = _summarise(dates, delay_bal)

    # h: health points
    baseline_health = compute_health_score(db, user_id, today, opening)
    other = sum(
        component["points"]
        for component in baseline_health["components"]
        if component["name"] != "Cash cover"
    )
    baseline_cover = cash_cover_points(baseline_summary)
    scenario_cover = cash_cover_points(scenario_summary)
    baseline_total = int(baseline_health["total_points"])
    recomputed = min(max(int(round(other + baseline_cover)), 0), 100)
    if recomputed != baseline_total:
        raise RuntimeError("health recomputation mismatch")
    scenario_total = min(max(int(round(other + scenario_cover)), 0), 100)

    # i: monthly comparison, actual then projected
    act_labels, act_income, act_expenses = _actual_month_totals(db, user_id, today)
    proj_labels, proj_income, proj_expenses, proj_partial = [], [], [], []
    month_starts = []
    y, m = today.year, today.month
    while (y, m) <= (horizon_end.year, horizon_end.month):
        month_starts.append((y, m))
        y, m = _shift_month(y, m, 1)
    for idx, (y, m) in enumerate(month_starts):
        start = datetime.date(y, m, 1)
        ny, nm = _shift_month(y, m, 1)
        end = datetime.date(ny, nm, 1)
        in_month = [k for k, d in enumerate(dates)
                    if start <= datetime.date.fromisoformat(d) < end]
        proj_labels.append(_month_label(start))
        proj_income.append(sum(scenario_in_list[k] for k in in_month))
        proj_expenses.append(sum(scenario_out_list[k] for k in in_month))
        proj_partial.append(idx == 0 or idx == len(month_starts) - 1)

    monthly = {
        "labels": act_labels + proj_labels,
        "source": ["actual"] * len(act_labels) + ["projected"] * len(proj_labels),
        "income_paise": act_income + proj_income,
        "expenses_paise": act_expenses + proj_expenses,
        "partial": [False] * len(act_labels) + proj_partial,
    }

    # l: invoices collected after the window
    delayed_past_horizon = 0
    for inv in open_invoices:
        due = _as_date(inv["due_date"])
        expected = max(due, today)
        if expected + datetime.timedelta(days=delay_days) > horizon_end:
            delayed_past_horizon += 1

    negative_days = sum(1 for balance in scenario_bal if balance < 0)
    closing_diff = scenario_summary["closing_balance_paise"] - baseline_summary["closing_balance_paise"]
    min_diff = scenario_summary["min_balance_paise"] - baseline_summary["min_balance_paise"]
    delay_impact = delay_summary["closing_balance_paise"] - baseline_summary["closing_balance_paise"]

    # m: plain-language warnings
    warnings = []
    if scenario_summary["first_negative_date"] is not None:
        warnings.append(
            f"Projected cash may fall below zero on {scenario_summary['first_negative_date']} in this scenario."
        )
    if closing_diff != 0:
        warnings.append(
            f"Scenario closing balance differs from the baseline by {format_inr(abs(closing_diff))}."
        )
    if delayed_past_horizon > 0:
        warnings.append(
            f"{delayed_past_horizon} open invoice(s) would be collected after the 90-day window in this scenario."
        )
    if additional_expenses > 0:
        warnings.append(
            f"Additional expenses total {format_inr(additional_expenses)} within the horizon."
        )
    if collection_pct < 100:
        warnings.append(f"Collection assumption is {collection_pct}% of expected inflows.")
    if revenue_pct != 0:
        warnings.append(f"Expected inflows are changed by {revenue_pct}%.")

    # n: data presence (unpaid expenses due up to the horizon end, past-due included)
    unpaid_row = db.execute(
        "SELECT COUNT(*) AS n FROM expenses "
        "WHERE user_id = ? AND paid = 0 AND due_date <= ?",
        (user_id, horizon_end.isoformat()),
    ).fetchone()
    has_data = (
        bool(open_invoices)
        or int(unpaid_row["n"]) > 0
        or additional_expenses > 0
    )

    return {
        "has_data": has_data,
        "inputs": {
            "delay_days": delay_days,
            "collection_pct": collection_pct,
            "revenue_change_pct": revenue_pct,
            "extra_monthly_expense_paise": extra_monthly,
        },
        "labels": dates,
        "baseline_balance_paise": baseline_bal,
        "scenario_balance_paise": scenario_bal,
        "delay_only_balance_paise": delay_bal,
        "scenario_inflow_paise": scenario_in_list,
        "scenario_outflow_paise": scenario_out_list,
        "summary": {
            "baseline": baseline_summary,
            "scenario": scenario_summary,
            "delay_only": delay_summary,
            "difference": {
                "closing_balance_paise": closing_diff,
                "min_balance_paise": min_diff,
            },
            "delay_impact_paise": delay_impact,
            "additional_expenses_paise": additional_expenses,
            "negative_days": negative_days,
            "delayed_past_horizon": delayed_past_horizon,
            "warnings": warnings,
        },
        "health": {
            "baseline_total": baseline_total,
            "scenario_total": scenario_total,
            "baseline_cash_cover": baseline_cover,
            "scenario_cash_cover": scenario_cover,
            "other_points": round(other, 1),
        },
        "monthly": monthly,
    }
