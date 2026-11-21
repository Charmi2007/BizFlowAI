"""Demo seed data for BizFlow AI.

Usage (from the project root):

    python seed.py
    python seed.py --reset-only-demo

Only the demo tutor (demo@bizflow.local) is ever deleted. Its rows in students,
invoices, invoice_items and payments go with it via ON DELETE CASCADE, and every
other user is left alone. The data is deterministic: random.Random(42), and all
dates are derived from one REFERENCE_DATE. That is today's date by default, or the
ISO date in the BIZFLOW_SEED_REFERENCE_DATE environment variable if it is set.
"""
import argparse
import calendar
import datetime
import os
import random

from werkzeug.security import generate_password_hash

from app import create_app
from db import format_inr, get_db

DEMO_NAME = "Anjali Rao"
DEMO_EMAIL = "demo@bizflow.local"
DEMO_PASSWORD = "demo1234"
REFERENCE_ENV_VAR = "BIZFLOW_SEED_REFERENCE_DATE"


def _resolve_reference_date():
    """Return today's date, or the validated ISO date from BIZFLOW_SEED_REFERENCE_DATE."""
    raw = os.environ.get(REFERENCE_ENV_VAR)
    if raw is None or not raw.strip():
        return datetime.date.today()
    try:
        return datetime.date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValueError(
            f"{REFERENCE_ENV_VAR} must be an ISO date (YYYY-MM-DD), got {raw!r}"
        ) from exc


REFERENCE_DATE = _resolve_reference_date()


def _shift_month(year, month, delta):
    """Return (year, month) moved by delta months."""
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _month_date(offset, day):
    """Return `day` in the month `offset` months from the reference month (day clamped)."""
    year, month = _shift_month(REFERENCE_DATE.year, REFERENCE_DATE.month, offset)
    last_day = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, min(day, last_day))


# The 12 monthly invoice cycles end at the reference month: offsets -11 .. 0.
WINDOW_START = _month_date(-11, 1)
CREATED_AT = f"{(WINDOW_START - datetime.timedelta(days=1)).isoformat()}T09:00:00+00:00"
RANDOM_SEED = 42
PAYMENT_METHODS = ("cash", "UPI", "bank transfer")
MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

STUDENTS = (
    ("Aarav Sharma", "Rajesh Sharma"),
    ("Diya Patel", "Mehul Patel"),
    ("Ishaan Gupta", "Sunita Gupta"),
    ("Meera Iyer", "Venkat Iyer"),
    ("Kabir Singh", "Harpreet Singh"),
    ("Ananya Reddy", "Suresh Reddy"),
    ("Vihaan Joshi", "Parag Joshi"),
    ("Saanvi Nair", "Anil Nair"),
    ("Reyansh Mehta", "Kunal Mehta"),
    ("Aadhya Kulkarni", "Prasad Kulkarni"),
    ("Arjun Das", "Biswajit Das"),
    ("Pari Chopra", "Rohit Chopra"),
    ("Dhruv Menon", "Sreejith Menon"),
    ("Kiara Fernandes", "Joseph Fernandes"),
    ("Shaurya Verma", "Deepak Verma"),
    ("Navya Agarwal", "Rakesh Agarwal"),
    ("Advait Desai", "Nitin Desai"),
    ("Tara Banerjee", "Sourav Banerjee"),
)

# (student index, description, amount in rupees,
#  (invoice month offset, day), (due month offset, day),
#  days after due date the payment was made, or None if unpaid)
# Month offsets are relative to REFERENCE_DATE's month.
ADHOC_INVOICES = (
    (2, "Mock test fee", 800, (-1, 20), (-1, 1), None),
    (7, "Mock test fee", 800, (-2, 18), (-2, 28), 2),
    (13, "Exam registration fee", 500, (0, 2), (0, 12), None),
)


def _insert_user(db):
    # ON DELETE CASCADE removes this user's students, invoices, items and payments.
    db.execute("DELETE FROM users WHERE email = ?", (DEMO_EMAIL,))
    cur = db.execute(
        "INSERT INTO users (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (DEMO_NAME, DEMO_EMAIL, generate_password_hash(DEMO_PASSWORD), CREATED_AT),
    )
    return cur.lastrowid


def _insert_students(db, rng, uid):
    """Insert the 18 demo students; return a list of (student_id, monthly_fee_paise)."""
    rows = []
    for name, parent in STUDENTS:
        fee_paise = rng.randint(30, 80) * 5000  # Rs 1,500 to Rs 4,000 in Rs 50 steps
        # Start before the 12-month billing window so every invoice falls after the start.
        start = datetime.date(WINDOW_START.year - 1, rng.randint(1, 12), rng.randint(1, 28))
        phone = f"+91 9{rng.randint(0, 999_999_999):09d}"
        cur = db.execute(
            "INSERT INTO students (user_id, name, parent_name, phone, monthly_fee_paise, "
            "start_date, active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (uid, name, parent, phone, fee_paise, start.isoformat(), CREATED_AT),
        )
        rows.append((cur.lastrowid, fee_paise))
    return rows


def _insert_invoice(db, uid, student_id, number, inv_date, due_date, description, total_paise):
    cur = db.execute(
        "INSERT INTO invoices (user_id, student_id, invoice_number, invoice_date, due_date, "
        "description, total_paise, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uid,
            student_id,
            number,
            inv_date.isoformat(),
            due_date.isoformat(),
            description,
            total_paise,
            f"{inv_date.isoformat()}T09:00:00+00:00",
        ),
    )
    invoice_id = cur.lastrowid
    db.execute(
        "INSERT INTO invoice_items (invoice_id, description, quantity, unit_price_paise, "
        "amount_paise) VALUES (?, ?, 1, ?, ?)",
        (invoice_id, description, total_paise, total_paise),
    )
    return invoice_id


def _insert_payment(db, uid, invoice_id, amount_paise, when, method, note=None):
    db.execute(
        "INSERT INTO payments (user_id, invoice_id, amount_paise, payment_date, method, note, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            uid,
            invoice_id,
            amount_paise,
            when.isoformat(),
            method,
            note,
            f"{when.isoformat()}T10:00:00+00:00",
        ),
    )


def _pick_payment(rng, total_paise, inv_date, due_date):
    """Decide one invoice's payment: (amount_paise, payment_date, note) or None if unpaid.

    About 60% are paid in full (some after the due date), about 10% part-paid, the rest unpaid.
    Every payment date is clamped to lie on or before REFERENCE_DATE.
    """
    roll = rng.random()
    if roll < 0.60:
        when = due_date + datetime.timedelta(days=rng.randint(-6, 22))
        amount = total_paise
        note = None
    elif roll < 0.70:
        fraction = rng.choice((0.25, 0.4, 0.5))
        amount = (int(total_paise * fraction) // 100) * 100
        when = inv_date + datetime.timedelta(days=rng.randint(2, 12))
        note = "Part payment"
    else:
        return None
    when = min(max(when, inv_date), REFERENCE_DATE)
    return amount, when, note


def _seed_invoice(db, rng, uid, invoice_id, total_paise, inv_date, due_date, stats):
    """Maybe add a payment for an invoice, then update the summary counters."""
    paid = 0
    outcome = _pick_payment(rng, total_paise, inv_date, due_date)
    if outcome is not None:
        amount, when, note = outcome
        method = rng.choice(PAYMENT_METHODS)
        _insert_payment(db, uid, invoice_id, amount, when, method, note)
        paid = amount
        stats["payments"] += 1
        if when > due_date:
            stats["paid_late"] += 1

    stats["invoices"] += 1
    stats["billed"] += total_paise
    stats["collected"] += paid
    if paid >= total_paise:
        stats["fully_paid"] += 1
    elif paid > 0:
        stats["partially_paid"] += 1
    else:
        stats["unpaid"] += 1
    if total_paise - paid > 0 and due_date < REFERENCE_DATE:
        stats["overdue"] += 1


def seed(db):
    """Reset the demo tutor and insert fresh demo data. Returns a summary dict."""
    rng = random.Random(RANDOM_SEED)
    stats = dict(
        students=0,
        invoices=0,
        adhoc=0,
        fully_paid=0,
        partially_paid=0,
        unpaid=0,
        payments=0,
        paid_late=0,
        billed=0,
        collected=0,
        overdue=0,
    )

    uid = _insert_user(db)
    students = _insert_students(db, rng, uid)
    stats["students"] = len(students)

    invoice_no = 0
    for offset in range(-11, 1):
        inv_date = _month_date(offset, 1)
        due_date = _month_date(offset, 10)
        description = f"Tuition - {MONTH_NAMES[inv_date.month - 1]} {inv_date.year}"
        for student_id, fee_paise in students:
            invoice_no += 1
            invoice_id = _insert_invoice(
                db, uid, student_id, f"INV-{inv_date.year}-{invoice_no:04d}",
                inv_date, due_date, description, fee_paise,
            )
            _seed_invoice(db, rng, uid, invoice_id, fee_paise, inv_date, due_date, stats)

    for index, description, rupees, inv_at, due_at, paid_after in ADHOC_INVOICES:
        invoice_no += 1
        student_id, _fee = students[index]
        inv_date = min(_month_date(*inv_at), REFERENCE_DATE)
        due_date = _month_date(*due_at)
        total_paise = rupees * 100
        invoice_id = _insert_invoice(
            db, uid, student_id, f"INV-{inv_date.year}-{invoice_no:04d}",
            inv_date, due_date, description, total_paise,
        )
        stats["adhoc"] += 1
        paid = 0
        if paid_after is not None:
            paid_on = due_date + datetime.timedelta(days=paid_after)
            paid_on = min(max(paid_on, inv_date), REFERENCE_DATE)
            _insert_payment(db, uid, invoice_id, total_paise, paid_on, "UPI")
            paid = total_paise
            stats["payments"] += 1
            if paid_on > due_date:
                stats["paid_late"] += 1
        stats["invoices"] += 1
        stats["billed"] += total_paise
        stats["collected"] += paid
        if paid >= total_paise:
            stats["fully_paid"] += 1
        else:
            stats["unpaid"] += 1
            if due_date < REFERENCE_DATE:
                stats["overdue"] += 1

    return stats


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Reset and seed the demo tutor account (only that user is affected)."
    )
    parser.add_argument(
        "--reset-only-demo",
        action="store_true",
        help="Reset only the demo tutor. This is always what happens; the flag is optional.",
    )
    parser.parse_args(argv)

    app = create_app()
    with app.app_context():
        db = get_db()
        try:
            stats = seed(db)
            db.commit()
        except Exception:
            db.rollback()
            raise

    outstanding = stats["billed"] - stats["collected"]
    print(f"Seeded demo tutor {DEMO_NAME} <{DEMO_EMAIL}> (password: {DEMO_PASSWORD})")
    print(f"  students:               {stats['students']}")
    print(f"  invoices total:         {stats['invoices']}")
    print(f"  ad-hoc invoices:        {stats['adhoc']}")
    print(f"  fully paid:             {stats['fully_paid']}")
    print(f"  partially paid:         {stats['partially_paid']}")
    print(f"  unpaid:                 {stats['unpaid']}")
    print(f"  payments recorded:      {stats['payments']}")
    print(f"  paid after due date:    {stats['paid_late']}")
    print(f"  billed:                 {format_inr(stats['billed'])}")
    print(f"  collected:              {format_inr(stats['collected'])}")
    print(f"  outstanding:            {format_inr(outstanding)}")
    print(f"  overdue as of {REFERENCE_DATE.isoformat()}: {stats['overdue']}")
    print("  note: the app compares due dates with today's real date, so on a later day "
          "more invoices show as overdue.")


if __name__ == "__main__":
    main()
