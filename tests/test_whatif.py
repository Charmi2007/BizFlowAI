"""Tests for the 15-day payment-delay what-if in forecast._run_whatif.

Run from the project root on Windows with:

    .venv\\Scripts\\python -m unittest tests.test_whatif

The test points db.DB_PATH at a temporary SQLite file, seeds one user, one
student and one unpaid invoice with SQL, and removes the temporary files in
tearDown. app.db and seed.py are not used.
"""
import datetime
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
from app import create_app  # noqa: E402
from finance import (  # noqa: E402
    FORECAST_HORIZON_DAYS,
    build_cashflow_forecast,
    load_invoice_balances,
)

USER_ID = 1
STUDENT_ID = 1
INVOICE_ID = 1
TODAY = datetime.date(2026, 6, 1)
CASH_PAISE = 2500000  # Rs 25,000, the forecast default
INVOICE_PAISE = 100000  # Rs 1,000


class WhatIfTests(unittest.TestCase):
    def setUp(self):
        self._original_db_path = db.DB_PATH
        self.tmp_dir = tempfile.mkdtemp(prefix="bizflow_whatif_")
        db.DB_PATH = os.path.join(self.tmp_dir, "whatif_test.db")
        self.app = create_app()  # runs db.init_db() against the temporary file
        self.app.config["TESTING"] = True
        with self.app.app_context():
            conn = db.get_db()
            conn.execute(
                "INSERT INTO users (id, name, email, password_hash, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (USER_ID, "Test Owner", "owner@example.test", "unused", "2026-05-01T00:00:00"),
            )
            conn.execute(
                "INSERT INTO students (id, user_id, name, monthly_fee_paise, start_date, active, created_at) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (STUDENT_ID, USER_ID, "Test Student", INVOICE_PAISE, "2026-04-01", "2026-05-01T00:00:00"),
            )
            conn.execute(
                "INSERT INTO invoices (id, user_id, student_id, invoice_number, invoice_date, "
                "due_date, description, total_paise, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (INVOICE_ID, USER_ID, STUDENT_ID, "INV-TEST-0001", "2026-05-01", "2026-06-10",
                 "Test fee", INVOICE_PAISE, "2026-05-01T00:00:00"),
            )
            conn.commit()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _run_whatif(self, delay_days):
        with self.app.app_context():
            conn = db.get_db()
            invoices = load_invoice_balances(conn, USER_ID)
            _, summary = build_cashflow_forecast(
                conn, USER_ID, TODAY, CASH_PAISE, FORECAST_HORIZON_DAYS
            )
            return forecast._run_whatif(
                conn, USER_ID, TODAY, CASH_PAISE, summary, invoices,
                STUDENT_ID, "Test Student", delay_days,
            )

    def test_fifteen_day_delay_moves_due_date_and_keeps_balance(self):
        result = self._run_whatif(15)
        self.assertEqual(len(result["invoices"]), 1)
        invoice = result["invoices"][0]
        self.assertEqual(invoice["original_expected_date"], "2026-06-10")
        self.assertEqual(invoice["revised_expected_date"], "2026-06-25")
        self.assertEqual(invoice["days_delayed"], 15)
        self.assertEqual(invoice["balance_paise"], INVOICE_PAISE)

    def test_zero_delay_has_empty_delay_period(self):
        result = self._run_whatif(0)
        self.assertEqual(result["delay_period"], [])

    def test_milestone_explanation_has_one_entry_per_milestone(self):
        result = self._run_whatif(15)
        self.assertEqual(len(result["milestone_explanation"]), 3)


if __name__ == "__main__":
    unittest.main()
