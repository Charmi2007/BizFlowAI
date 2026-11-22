"""Tests for demo-date consistency, forecast arithmetic and delayed balances.

Run from the project root on Windows with:

    .venv\\Scripts\\python -m unittest tests.test_dates_and_forecast

Each database-backed test points db.DB_PATH at a temporary SQLite file, seeds one
user, one student and a few invoices/expenses with SQL, and removes the temporary
files in tearDown. app.db is not used. "Today" is datetime.date.today(), the same
value the app uses, and is passed explicitly to build_cashflow_forecast and
forecast._run_whatif.
"""
import datetime
import importlib
import os
import shutil
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import db  # noqa: E402
import forecast  # noqa: E402
import seed as seed_module  # noqa: E402
from app import create_app  # noqa: E402
from finance import (  # noqa: E402
    FORECAST_HORIZON_DAYS,
    build_cashflow_forecast,
    load_invoice_balances,
)

USER_ID = 1
STUDENT_ID = 1
OPENING_PAISE = 1000000  # Rs 10,000 opening cash for the hand calculations
REFERENCE_ENV_VAR = "BIZFLOW_SEED_REFERENCE_DATE"


def _create_temp_app(test):
    """Point db.DB_PATH at a temp file, create the app and seed one user and student."""
    test._original_db_path = db.DB_PATH
    test.tmp_dir = tempfile.mkdtemp(prefix="bizflow_dates_")
    db.DB_PATH = os.path.join(test.tmp_dir, "dates_test.db")
    test.app = create_app()  # runs db.init_db() against the temporary file
    test.app.config["WTF_CSRF_ENABLED"] = False  # legacy tests post without tokens; CSRF is covered by tests/test_csrf_and_preferences.py
    test.app.config["TESTING"] = True
    with test.app.app_context():
        conn = db.get_db()
        conn.execute(
            "INSERT INTO users (id, name, email, password_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (USER_ID, "Test Owner", "owner@example.test", "unused", "2020-01-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO students (id, user_id, name, monthly_fee_paise, start_date, active, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (STUDENT_ID, USER_ID, "Test Student", 100000, "2020-01-01", "2020-01-01T00:00:00"),
        )
        conn.commit()


def _destroy_temp_app(test):
    db.DB_PATH = test._original_db_path
    shutil.rmtree(test.tmp_dir, ignore_errors=True)


def _add_invoice(conn, invoice_id, due_date, total_paise, paid_paise=0):
    conn.execute(
        "INSERT INTO invoices (id, user_id, student_id, invoice_number, invoice_date, "
        "due_date, description, total_paise, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (invoice_id, USER_ID, STUDENT_ID, f"INV-TEST-{invoice_id:04d}",
         due_date.isoformat(), due_date.isoformat(), "Test fee", total_paise,
         "2020-01-01T00:00:00"),
    )
    if paid_paise:
        conn.execute(
            "INSERT INTO payments (user_id, invoice_id, amount_paise, payment_date, method, "
            "note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (USER_ID, invoice_id, paid_paise, due_date.isoformat(), "cash", None,
             "2020-01-01T00:00:00"),
        )


def _add_expense(conn, expense_id, due_date, amount_paise, paid=0):
    conn.execute(
        "INSERT INTO expenses (id, user_id, name, category, amount_paise, due_date, paid, "
        "paid_date, recurring_monthly, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (expense_id, USER_ID, f"Expense {expense_id}", "test", amount_paise,
         due_date.isoformat(), paid, None, "2020-01-01T00:00:00"),
    )


class DateConsistencyTests(unittest.TestCase):
    """seed.py derives every demo date from one REFERENCE_DATE."""

    def setUp(self):
        self._original_env = os.environ.get(REFERENCE_ENV_VAR)
        self._original_db_path = db.DB_PATH
        self.tmp_dir = tempfile.mkdtemp(prefix="bizflow_seed_dates_")

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        self._set_env(self._original_env)
        # Reload with the original environment so later tests see the default seed state.
        importlib.reload(seed_module)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @staticmethod
    def _set_env(value):
        if value is None:
            os.environ.pop(REFERENCE_ENV_VAR, None)
        else:
            os.environ[REFERENCE_ENV_VAR] = value

    def _reload_seed(self, value):
        """Set the env var (or unset it when value is None) and re-import seed.py."""
        self._set_env(value)
        return importlib.reload(seed_module)

    def test_reference_date_honours_override(self):
        module = self._reload_seed("2025-03-15")
        self.assertEqual(module.REFERENCE_DATE, datetime.date(2025, 3, 15))

    def test_reference_date_defaults_to_today_when_unset(self):
        module = self._reload_seed(None)
        self.assertEqual(module.REFERENCE_DATE, datetime.date.today())

    def test_invalid_override_raises_value_error(self):
        # seed._resolve_reference_date raises ValueError for a non-ISO override.
        with self.assertRaises(ValueError):
            self._reload_seed("not-a-date")

    def test_seeded_invoice_and_payment_dates_do_not_exceed_reference_date(self):
        # Seam: seed.seed(conn) is a public function that takes a connection, so the
        # test calls it directly against a temporary database and queries the rows.
        module = self._reload_seed("2025-03-15")
        db.DB_PATH = os.path.join(self.tmp_dir, "seed_dates.db")
        app = create_app()
        app.config["WTF_CSRF_ENABLED"] = False  # legacy tests post without tokens; CSRF is covered by tests/test_csrf_and_preferences.py
        with app.app_context():
            conn = db.get_db()
            module.seed(conn)
            conn.commit()
            invoice_count = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
            max_invoice_date = conn.execute("SELECT MAX(invoice_date) FROM invoices").fetchone()[0]
            max_payment_date = conn.execute("SELECT MAX(payment_date) FROM payments").fetchone()[0]
        reference = module.REFERENCE_DATE.isoformat()
        self.assertGreater(invoice_count, 0)
        self.assertIsNotNone(max_invoice_date)
        self.assertLessEqual(max_invoice_date, reference)
        if max_payment_date is not None:
            self.assertLessEqual(max_payment_date, reference)


class ForecastCalculationTests(unittest.TestCase):
    """Balances at day 30/60/90 and the minimum balance match a hand calculation."""

    def setUp(self):
        _create_temp_app(self)
        self.today = datetime.date.today()
        d = self.today
        with self.app.app_context():
            conn = db.get_db()
            # Inflows (day offset from today, amount in paise):
            _add_invoice(conn, 1, d + datetime.timedelta(days=10), 300000)  # day 10
            _add_invoice(conn, 2, d - datetime.timedelta(days=5), 200000, paid_paise=50000)  # overdue, balance 150000 -> day 0
            _add_invoice(conn, 3, d + datetime.timedelta(days=45), 400000)  # day 45
            _add_invoice(conn, 4, d + datetime.timedelta(days=80), 100000)  # day 80
            _add_invoice(conn, 5, d + datetime.timedelta(days=120), 900000)  # beyond horizon, excluded
            # Outflows:
            _add_expense(conn, 1, d + datetime.timedelta(days=20), 250000)  # day 20
            _add_expense(conn, 2, d + datetime.timedelta(days=60), 150000)  # day 60
            _add_expense(conn, 3, d + datetime.timedelta(days=30), 999999, paid=1)  # paid, ignored
            conn.commit()

    def tearDown(self):
        _destroy_temp_app(self)

    # (day offset, paise) for every inflow inside the 90-day horizon.
    INFLOWS = ((0, 150000), (10, 300000), (45, 400000), (80, 100000))
    # (day offset, paise) for every unpaid outflow inside the horizon.
    OUTFLOWS = ((20, 250000), (60, 150000))

    def _hand_balance(self, day):
        """Opening balance plus inflows dated on or before day, minus outflows on or before day."""
        inflow = sum(amount for offset, amount in self.INFLOWS if offset <= day)
        outflow = sum(amount for offset, amount in self.OUTFLOWS if offset <= day)
        return OPENING_PAISE + inflow - outflow

    def _forecast(self):
        with self.app.app_context():
            conn = db.get_db()
            return build_cashflow_forecast(
                conn, USER_ID, self.today, OPENING_PAISE, FORECAST_HORIZON_DAYS
            )

    def test_balance_day30_matches_hand_calculation(self):
        _, summary = self._forecast()
        self.assertEqual(summary["balance_day30"], self._hand_balance(30))
        self.assertEqual(summary["balance_day30"], 1200000)

    def test_balance_day60_matches_hand_calculation(self):
        _, summary = self._forecast()
        self.assertEqual(summary["balance_day60"], self._hand_balance(60))
        self.assertEqual(summary["balance_day60"], 1450000)

    def test_balance_day90_matches_hand_calculation(self):
        _, summary = self._forecast()
        self.assertEqual(summary["balance_day90"], self._hand_balance(90))
        self.assertEqual(summary["balance_day90"], 1550000)

    def test_min_balance_equals_minimum_of_daily_balances(self):
        daily, summary = self._forecast()
        self.assertEqual(len(daily), FORECAST_HORIZON_DAYS + 1)
        self.assertEqual(summary["min_balance_paise"], min(e["balance_paise"] for e in daily))
        hand_min = min(self._hand_balance(day) for day in range(FORECAST_HORIZON_DAYS + 1))
        self.assertEqual(summary["min_balance_paise"], hand_min)


class DelayedBalanceTests(unittest.TestCase):
    """A 15-day delay moves one student's expected inflow and changes the balances."""

    INVOICE_PAISE = 100000
    DUE_OFFSET = 10
    DELAY_DAYS = 15
    EXPENSE_PAISE = 250000

    def setUp(self):
        _create_temp_app(self)
        self.today = datetime.date.today()
        with self.app.app_context():
            conn = db.get_db()
            _add_invoice(conn, 1, self.today + datetime.timedelta(days=self.DUE_OFFSET),
                         self.INVOICE_PAISE)
            conn.commit()

    def tearDown(self):
        _destroy_temp_app(self)

    def _add_expense_due_day(self, offset):
        with self.app.app_context():
            conn = db.get_db()
            _add_expense(conn, 1, self.today + datetime.timedelta(days=offset), self.EXPENSE_PAISE)
            conn.commit()

    def _whatif(self, delay_days):
        with self.app.app_context():
            conn = db.get_db()
            invoices = load_invoice_balances(conn, USER_ID)
            _, summary = build_cashflow_forecast(
                conn, USER_ID, self.today, OPENING_PAISE, FORECAST_HORIZON_DAYS
            )
            return forecast._run_whatif(
                conn, USER_ID, self.today, OPENING_PAISE, summary, invoices,
                STUDENT_ID, "Test Student", delay_days,
            )

    def test_revised_inflow_moves_to_revised_date(self):
        revised_date = self.today + datetime.timedelta(days=self.DUE_OFFSET + self.DELAY_DAYS)
        with self.app.app_context():
            conn = db.get_db()
            original_daily, _ = build_cashflow_forecast(
                conn, USER_ID, self.today, OPENING_PAISE, FORECAST_HORIZON_DAYS
            )
            revised_daily, _ = build_cashflow_forecast(
                conn, USER_ID, self.today, OPENING_PAISE, FORECAST_HORIZON_DAYS,
                inflow_delay_by_invoice={1: self.DELAY_DAYS},
            )
        original_by_date = {e["date"]: e for e in original_daily}
        original_due = original_by_date[(self.today + datetime.timedelta(days=self.DUE_OFFSET)).isoformat()]
        self.assertEqual(original_due["inflow_paise"], self.INVOICE_PAISE)

        for entry in revised_daily:
            day = datetime.date.fromisoformat(entry["date"])
            if day < revised_date:
                # Before the revised date the invoice contributes nothing: the balance
                # is still the opening balance and there is no inflow that day.
                self.assertEqual(entry["inflow_paise"], 0, entry["date"])
                self.assertEqual(entry["balance_paise"], OPENING_PAISE, entry["date"])
            else:
                self.assertEqual(entry["balance_paise"], OPENING_PAISE + self.INVOICE_PAISE,
                                 entry["date"])
        revised_on_date = next(e for e in revised_daily if e["date"] == revised_date.isoformat())
        self.assertEqual(revised_on_date["inflow_paise"], self.INVOICE_PAISE)

    def test_min_balance_revised_not_above_original_when_expense_present(self):
        # Expense due on day 25 (after the original due date, before the revised one).
        self._add_expense_due_day(25)
        result = self._whatif(self.DELAY_DAYS)
        self.assertLessEqual(result["min_balance_revised_paise"], result["min_balance_original_paise"])
        # Hand values (opening 1,000,000): the original invoice lands on today+10 and the
        # expense on today+25, so the original min is opening + invoice - expense = 850000.
        # The revised invoice lands on today+25 (same day as the expense), so the revised
        # min is also opening + invoice - expense = 850000.
        self.assertEqual(result["min_balance_original_paise"],
                         OPENING_PAISE + self.INVOICE_PAISE - self.EXPENSE_PAISE)
        self.assertEqual(result["min_balance_revised_paise"],
                         OPENING_PAISE + self.INVOICE_PAISE - self.EXPENSE_PAISE)

    def test_delay_period_shows_balance_differences_between_dates(self):
        result = self._whatif(self.DELAY_DAYS)
        period = result["delay_period"]
        self.assertTrue(period)
        original_date = self.today + datetime.timedelta(days=self.DUE_OFFSET)
        revised_date = original_date + datetime.timedelta(days=self.DELAY_DAYS)
        self.assertEqual(period[0]["date"], original_date.isoformat())
        differing = [row for row in period if row["original_balance_paise"] != row["revised_balance_paise"]]
        self.assertTrue(differing)
        for row in period:
            day = datetime.date.fromisoformat(row["date"])
            difference = row["original_balance_paise"] - row["revised_balance_paise"]
            if original_date <= day < revised_date:
                self.assertEqual(difference, self.INVOICE_PAISE, row["date"])
            else:
                self.assertEqual(difference, 0, row["date"])


if __name__ == "__main__":
    unittest.main()
