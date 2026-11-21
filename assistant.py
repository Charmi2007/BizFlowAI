"""AI financial assistant blueprint for BizFlow AI.

Answers owner questions with rule-based, data-verified figures computed from the
existing finance and forecast helpers. An optional OpenAI answer is added only when
OPENAI_API_KEY is set in the environment; it is given the same computed figures.
"""
import datetime
import difflib
import json
import os
import re
import urllib.request

from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for

from auth import login_required
from dashboard import CHART_MONTHS, _month_keys, _monthly_sums
from db import format_inr, get_db
from finance import (
    FORECAST_HORIZON_DAYS,
    build_alerts,
    build_cashflow_forecast,
    compute_health_score,
    load_invoice_balances,
    load_unpaid_expenses,
)
from forecast import DEFAULT_DELAY_DAYS, MAX_DELAY_DAYS, _run_whatif, get_cash_balance_paise

bp = Blueprint("assistant", __name__, url_prefix="/assistant")

CHAT_SESSION_KEY = "assistant_chat"
MAX_QUESTION_CHARS = 500
MAX_TRANSCRIPT_ENTRIES = 20
UPCOMING_BILL_DAYS = 30
MILESTONE_DAYS = (30, 60, 90)
TOP_RECOMMENDATIONS = 3

RULE_LABEL = "Rule-based (data-verified)"
AI_LABEL = "AI-generated (OpenAI)"
NO_KEY_NOTE = "AI model not configured — answers are calculated directly from your records."

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL = "gpt-4o-mini"
OPENAI_SYSTEM_PROMPT = "Answer ONLY from the figures provided. Do not invent numbers."
OPENAI_TIMEOUT_SECONDS = 20

HELP_TEXT = (
    "I can answer questions about your records. Try asking:\n"
    "- What is my health score?\n"
    "- What if Asha pays 15 days late?\n"
    "- Which invoices are overdue?\n"
    "- Who has not paid?\n"
    "- What bills are due in the next 30 days?\n"
    "- What was my monthly income and expenses?\n"
    "- What is my 30, 60 and 90 day cash forecast?\n"
    "- What should I follow up on today?\n"
    "- What is my expected cash balance?"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sum_by_student(invoices):
    """Return students with a positive balance across invoices, largest first."""
    totals = {}
    for inv in invoices:
        if inv["balance_paise"] <= 0:
            continue
        entry = totals.setdefault(
            inv["student_id"],
            {"id": inv["student_id"], "name": inv["student_name"], "balance_paise": 0},
        )
        entry["balance_paise"] += inv["balance_paise"]
    return sorted(totals.values(), key=lambda entry: entry["balance_paise"], reverse=True)


# Words that can follow "what if" or precede "pays"/"late" without naming a person.
_NON_NAME_WORDS = frozenset({
    "it", "they", "he", "she", "i", "we", "you", "me", "us", "them", "him", "her",
    "someone", "somebody", "everyone", "everybody", "nobody", "anyone", "who", "what",
    "which", "this", "that", "there", "the", "a", "an", "one", "payment", "invoice",
    "customer", "student", "client", "pays", "pay", "paid", "is", "will", "does",
    "do", "did", "late", "delay", "days", "day",
})


def _named_person(question):
    """Return the lowercased word the question appears to name as a person, or None."""
    patterns = (
        r"\bwhat if (?:the )?([a-z][a-z'-]*)\b",
        r"\b([a-z][a-z'-]*) (?:pays|pay|paid|is|will)\b",
    )
    for pattern in patterns:
        for found in re.finditer(pattern, question):
            word = found.group(1)
            if word not in _NON_NAME_WORDS:
                return word
    return None


def _unknown_student_answer(db, user_id, person):
    """Explain that the named person is not a student of this user, with close-name hints."""
    names = [
        row["name"].strip() for row in db.execute(
            "SELECT name FROM students WHERE user_id = ?", (user_id,)
        ).fetchall()
        if row["name"].strip()
    ]
    text = f"No student named {person.title()} was found in your records, so no what-if figures are shown."
    name_by_lower = {name.lower(): name for name in names}
    close = difflib.get_close_matches(person, list(name_by_lower), n=3, cutoff=0.6)
    if close:
        text += " Nearest student names: " + ", ".join(name_by_lower[c] for c in close) + "."
    return text


def _pick_student(db, user_id, question, invoices, today):
    """Return (student_id, student_name) named in the question, else the largest overdue balance.

    If the question names a person who is not a student of this user, returns (None, None)
    instead of falling back to another student.
    """
    students = db.execute(
        "SELECT id, name FROM students WHERE user_id = ?", (user_id,)
    ).fetchall()
    matches = [
        s for s in students
        if s["name"].strip() and s["name"].strip().lower() in question
    ]
    if matches:
        best = max(matches, key=lambda s: len(s["name"]))
        return int(best["id"]), best["name"]
    if _named_person(question):
        return None, None
    overdue = _sum_by_student(
        [inv for inv in invoices if inv["due_date"] < today]
    )
    if overdue:
        return overdue[0]["id"], overdue[0]["name"]
    return None, None


# ---------------------------------------------------------------------------
# Intent handlers. Each returns (answer_text, figures_used, recommendations).
# ---------------------------------------------------------------------------

def _handle_health(db, user_id, question, today, cash_paise):
    health = compute_health_score(db, user_id, today, cash_paise)
    components = health["components"]
    lines = [f"Your business health score is {health['total_points']} out of 100."]
    figures = [f"Health score: {health['total_points']}/100"]
    for comp in components:
        lines.append(
            f"{comp['name']}: {comp['points']} of {comp['max_points']} points ({comp['detail']})."
        )
        figures.append(f"{comp['name']}: {comp['points']}/{comp['max_points']} points")
    weakest = min(components, key=lambda c: c["points"] / c["max_points"])
    recs = [f"Improve {weakest['name'].lower()} first: {weakest['detail']}."]
    return "\n".join(lines), figures, recs


def _handle_whatif(db, user_id, question, today, cash_paise):
    match = re.search(r"\d+", question)
    delay = min(int(match.group()), MAX_DELAY_DAYS) if match else DEFAULT_DELAY_DAYS
    invoices = load_invoice_balances(db, user_id)
    person = _named_person(question)
    student_id, student_name = _pick_student(db, user_id, question, invoices, today)
    if student_id is None and person:
        return _unknown_student_answer(db, user_id, person), [], []
    if student_id is None:
        return (
            "No student has an overdue balance, so there is no late-payment scenario to model. "
            "Name a student in your question to test a delay.",
            [],
            [],
        )

    _, original_summary = build_cashflow_forecast(
        db, user_id, today, cash_paise, FORECAST_HORIZON_DAYS
    )
    result = _run_whatif(
        db, user_id, today, cash_paise, original_summary, invoices,
        student_id, student_name, delay,
    )

    lines = [f"If {student_name} pays {delay} days after the due date:"]
    if student_name.lower() not in question:
        lines.insert(
            0,
            f"No student was named, so this uses {student_name}, who has the largest overdue balance.",
        )
    figures = []
    for row in result["rows"]:
        lines.append(
            f"Day {row['days']} ({row['date']}): original {format_inr(row['original_paise'])}, "
            f"revised {format_inr(row['revised_paise'])}, "
            f"difference {format_inr(row['difference_paise'])}."
        )
        figures.append(f"Day {row['days']} original balance: {format_inr(row['original_paise'])}")
        figures.append(f"Day {row['days']} revised balance: {format_inr(row['revised_paise'])}")
        figures.append(f"Day {row['days']} difference: {format_inr(row['difference_paise'])}")

    recs = []
    if result["unpayable"]:
        for item in result["unpayable"]:
            lines.append(
                f"Unpayable: {item['name']} due {item['due_date']} "
                f"({format_inr(item['amount_paise'])}) would leave a balance of "
                f"{format_inr(item['balance_paise'])}."
            )
            figures.append(f"Unpayable {item['name']}: {format_inr(item['amount_paise'])}")
            recs.append(
                f"Arrange cash for {item['name']} ({format_inr(item['amount_paise'])}) "
                f"due {item['due_date']} before the delay."
            )
    else:
        lines.append("No upcoming expense becomes unpayable under this scenario.")

    student_balance = sum(
        inv["balance_paise"] for inv in invoices
        if inv["student_id"] == student_id and inv["balance_paise"] > 0
    )
    if student_balance > 0:
        recs.insert(0, f"Send a reminder to {student_name} for {format_inr(student_balance)}.")
    return "\n".join(lines), figures, recs


def _handle_overdue(db, user_id, question, today, cash_paise):
    invoices = load_invoice_balances(db, user_id)
    overdue = [inv for inv in invoices if inv["balance_paise"] > 0 and inv["due_date"] < today]
    if not overdue:
        return (
            f"You have no overdue invoices as of {today.isoformat()}.",
            ["Overdue invoices: 0"],
            [],
        )
    total = sum(inv["balance_paise"] for inv in overdue)
    lines = [
        f"You have {len(overdue)} overdue invoice(s) totalling {format_inr(total)}.",
    ]
    figures = [
        f"Overdue invoices: {len(overdue)}",
        f"Overdue amount: {format_inr(total)}",
    ]
    recs = []
    for entry in _sum_by_student(overdue)[:TOP_RECOMMENDATIONS]:
        amount = format_inr(entry["balance_paise"])
        lines.append(f"{entry['name']}: {amount} overdue.")
        recs.append(f"Send a reminder to {entry['name']} for {amount}.")
    return "\n".join(lines), figures, recs


def _handle_owing(db, user_id, question, today, cash_paise):
    invoices = load_invoice_balances(db, user_id)
    owing = _sum_by_student(invoices)
    if not owing:
        return "No student has an unpaid balance.", ["Total outstanding: " + format_inr(0)], []
    total = sum(entry["balance_paise"] for entry in owing)
    lines = [
        f"{len(owing)} student(s) have not paid. Total outstanding: {format_inr(total)}.",
    ]
    for entry in owing:
        lines.append(f"{entry['name']}: {format_inr(entry['balance_paise'])}")
    figures = [
        f"Total outstanding: {format_inr(total)}",
        f"Students with unpaid balances: {len(owing)}",
    ]
    recs = [
        f"Send a reminder to {entry['name']} for {format_inr(entry['balance_paise'])}."
        for entry in owing[:TOP_RECOMMENDATIONS]
    ]
    return "\n".join(lines), figures, recs


def _handle_expenses(db, user_id, question, today, cash_paise):
    horizon = today + datetime.timedelta(days=UPCOMING_BILL_DAYS)
    bills = [
        exp for exp in load_unpaid_expenses(db, user_id)
        if exp["due_date"] <= horizon
    ]
    if not bills:
        return (
            f"No unpaid bills are due in the next {UPCOMING_BILL_DAYS} days.",
            [f"Upcoming bills ({UPCOMING_BILL_DAYS} days): 0"],
            [],
        )
    total = sum(exp["amount_paise"] for exp in bills)
    lines = [f"Unpaid bills due within {UPCOMING_BILL_DAYS} days (including past due): "
             f"{format_inr(total)} across {len(bills)} item(s)."]
    recs = []
    for exp in bills:
        amount = format_inr(exp["amount_paise"])
        due = exp["due_date"].isoformat()
        status = " (past due)" if exp["due_date"] < today else ""
        lines.append(f"{exp['name']}: {amount} due {due}{status}.")
    for exp in bills[:TOP_RECOMMENDATIONS]:
        recs.append(
            f"Pay {exp['name']} ({format_inr(exp['amount_paise'])}) due {exp['due_date'].isoformat()}."
        )
    figures = [
        f"Upcoming bills ({UPCOMING_BILL_DAYS} days): {format_inr(total)}",
        f"Upcoming bill items: {len(bills)}",
    ]
    return "\n".join(lines), figures, recs


def _handle_income(db, user_id, question, today, cash_paise):
    month_keys = _month_keys(today, CHART_MONTHS)
    first_year, first_month = month_keys[0]
    window_start = datetime.date(first_year, first_month, 1).isoformat()

    income_by_month = _monthly_sums(db.execute(
        "SELECT substr(payment_date, 1, 7) AS ym, COALESCE(SUM(amount_paise), 0) AS total "
        "FROM payments WHERE user_id = ? AND payment_date >= ? GROUP BY ym",
        (user_id, window_start),
    ).fetchall())
    expenses_by_month = _monthly_sums(db.execute(
        "SELECT substr(due_date, 1, 7) AS ym, COALESCE(SUM(amount_paise), 0) AS total "
        "FROM expenses WHERE user_id = ? AND due_date >= ? GROUP BY ym",
        (user_id, window_start),
    ).fetchall())

    lines = []
    total_income = 0
    total_expenses = 0
    for year, month in month_keys:
        ym = f"{year:04d}-{month:02d}"
        label = datetime.date(year, month, 1).strftime("%b %Y")
        income = income_by_month.get(ym, 0)
        expenses = expenses_by_month.get(ym, 0)
        total_income += income
        total_expenses += expenses
        lines.append(
            f"{label}: income {format_inr(income)}, expenses {format_inr(expenses)} (by due month)."
        )
    net = total_income - total_expenses
    figures = [
        f"Income, last {CHART_MONTHS} months: {format_inr(total_income)}",
        f"Expenses, last {CHART_MONTHS} months: {format_inr(total_expenses)}",
        f"Net, last {CHART_MONTHS} months: {format_inr(net)}",
    ]
    if net < 0:
        recs = [f"Expenses exceeded income by {format_inr(-net)} over the last {CHART_MONTHS} months; review recurring costs."]
    else:
        recs = [f"Income exceeded expenses by {format_inr(net)} over the last {CHART_MONTHS} months."]
    lines.append(f"Total: income {format_inr(total_income)}, expenses {format_inr(total_expenses)}.")
    return "\n".join(lines), figures, recs


def _handle_forecast(db, user_id, question, today, cash_paise):
    _, summary = build_cashflow_forecast(db, user_id, today, cash_paise, FORECAST_HORIZON_DAYS)
    lines = [f"Starting from your cash balance of {format_inr(cash_paise)}:"]
    figures = [f"Cash balance: {format_inr(cash_paise)}"]
    for days in MILESTONE_DAYS:
        balance = summary[f"balance_day{days}"]
        date = (today + datetime.timedelta(days=days)).isoformat()
        lines.append(f"Day {days} ({date}): {format_inr(balance)}.")
        figures.append(f"Projected balance day {days}: {format_inr(balance)}")

    first_negative = summary["first_negative_date"]
    recs = []
    if first_negative is not None:
        lines.append(f"Cash first goes negative on {first_negative}.")
        figures.append(f"First negative date: {first_negative}")
        recs.append(
            f"Collect payments or defer bills before {first_negative} to keep cash positive."
        )
    else:
        lines.append(f"Projected cash stays positive for the full {FORECAST_HORIZON_DAYS} days.")
        recs.append(
            f"Projected minimum balance over {FORECAST_HORIZON_DAYS} days is "
            f"{format_inr(summary['min_balance_paise'])}."
        )
    return "\n".join(lines), figures, recs


def _handle_followup(db, user_id, question, today, cash_paise):
    alerts = build_alerts(db, user_id, today, cash_paise)
    if not alerts:
        return "Nothing needs follow-up today.", ["Alerts: 0"], []
    urgent = sum(1 for alert in alerts if alert["severity"] == "danger")
    lines = [f"{len(alerts)} item(s) need attention:"]
    lines.extend(f"- {alert['message']}" for alert in alerts)
    figures = [f"Alerts: {len(alerts)}", f"Urgent alerts: {urgent}"]
    recs = [alert["message"] for alert in alerts[:5]]
    return "\n".join(lines), figures, recs


def _handle_cash(db, user_id, question, today, cash_paise):
    _, summary = build_cashflow_forecast(db, user_id, today, cash_paise, FORECAST_HORIZON_DAYS)
    day90 = summary["balance_day90"]
    lines = [f"Expected cash balance today: {format_inr(cash_paise)}."]
    figures = [
        f"Cash balance: {format_inr(cash_paise)}",
        f"Projected lowest balance ({FORECAST_HORIZON_DAYS} days): {format_inr(summary['min_balance_paise'])}",
        f"Projected balance day 90: {format_inr(day90)}",
    ]
    first_negative = summary["first_negative_date"]
    if first_negative is None:
        lines.append(f"Projected balance stays positive for {FORECAST_HORIZON_DAYS} days.")
        recs = [f"Projected balance at day 90 is {format_inr(day90)}."]
    else:
        lines.append(f"Projected balance may go negative on {first_negative}.")
        figures.append(f"First negative date: {first_negative}")
        recs = [f"Collect outstanding payments before {first_negative}; cash may run short."]
    return "\n".join(lines), figures, recs


def _handle_help(db, user_id, question, today, cash_paise):
    return HELP_TEXT, [], []


_HANDLERS = {
    "health": _handle_health,
    "whatif": _handle_whatif,
    "overdue": _handle_overdue,
    "owing": _handle_owing,
    "expenses": _handle_expenses,
    "income": _handle_income,
    "forecast": _handle_forecast,
    "followup": _handle_followup,
    "cash": _handle_cash,
    "help": _handle_help,
}


def _intent(q):
    """Return the intent name for a lowercased question, checking keywords in priority order."""
    if "health" in q or "score" in q:
        return "health"
    if any(k in q for k in ("what if", "late", "delay", "days late")):
        return "whatif"
    if "overdue" in q:
        return "overdue"
    if any(k in q for k in ("not paid", "who owes", "unpaid", "outstanding")):
        return "owing"
    if any(k in q for k in ("expense", "bill", "upcoming", "rent")):
        return "expenses"
    if any(k in q for k in ("income", "revenue", "month")):
        return "income"
    if any(k in q for k in ("forecast", "30", "60", "90")):
        return "forecast"
    if any(k in q for k in ("follow up", "today", "remind")):
        return "followup"
    if any(k in q for k in ("cash", "balance", "afford")):
        return "cash"
    return "help"


def answer_question(db, user_id, question, today, cash_balance_paise):
    """Answer one question from computed figures only. Returns a dict with answer,
    source, figures_used and recommendations."""
    q = (question or "").strip().lower()
    intent = _intent(q)
    answer, figures, recs = _HANDLERS[intent](db, user_id, q, today, int(cash_balance_paise))
    return {
        "answer": answer,
        "source": "rules",
        "figures_used": figures,
        "recommendations": recs,
    }


def llm_answer(question, context_text):
    """Ask OpenAI to phrase an answer from context_text. Returns None if no key or on any failure."""
    try:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        payload = {
            "model": OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": OPENAI_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Figures:\n{context_text}\n\nQuestion: {question}",
                },
            ],
        }
        request_obj = urllib.request.Request(
            OPENAI_URL,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        with urllib.request.urlopen(request_obj, timeout=OPENAI_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
        text = (data["choices"][0]["message"]["content"] or "").strip()
        return text or None
    except Exception:
        return None


def _ai_configured():
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def _format_rule_entry(result):
    text = result["answer"]
    if result["recommendations"]:
        text += "\n" + "\n".join(f"• {rec}" for rec in result["recommendations"])
    return text


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("", strict_slashes=False)
@bp.route("/", strict_slashes=False)
@login_required
def index():
    transcript = session.get(CHAT_SESSION_KEY, [])
    return render_template(
        "assistant/chat.html",
        transcript=transcript,
        max_chars=MAX_QUESTION_CHARS,
        ai_configured=_ai_configured(),
    )


@bp.route("/ask", methods=["POST"])
@login_required
def ask():
    question = request.form.get("question", "").strip()
    if not question:
        flash("Please type a question.", "error")
    elif len(question) > MAX_QUESTION_CHARS:
        flash(f"Questions must be {MAX_QUESTION_CHARS} characters or fewer.", "error")
    else:
        db = get_db()
        user_id = g.user["id"]
        today = datetime.date.today()
        cash_paise = get_cash_balance_paise()

        result = answer_question(db, user_id, question, today, cash_paise)
        context_text = "\n".join(result["figures_used"] + [result["answer"]])

        transcript = list(session.get(CHAT_SESSION_KEY, []))
        transcript.append({"role": "user", "text": question, "source_label": ""})

        ai_text = llm_answer(question, context_text)
        if ai_text:
            transcript.append({"role": "assistant", "text": ai_text, "source_label": AI_LABEL})

        transcript.append({
            "role": "assistant",
            "text": _format_rule_entry(result),
            "source_label": RULE_LABEL,
        })

        if not _ai_configured():
            transcript.append({"role": "note", "text": NO_KEY_NOTE, "source_label": ""})

        session[CHAT_SESSION_KEY] = transcript[-MAX_TRANSCRIPT_ENTRIES:]
    return redirect(url_for("assistant.index"))


@bp.route("/clear", methods=["POST"])
@login_required
def clear():
    session.pop(CHAT_SESSION_KEY, None)
    return redirect(url_for("assistant.index"))
