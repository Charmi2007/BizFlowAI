"""Tests for the Forecast simulation studio (simulation.py and GET /forecast/simulate).

Run from the project root on Windows with:

    .venv\\Scripts\\python -m unittest tests.test_simulation

Each test class points db.DB_PATH at its own temporary SQLite file, seeds rows
with SQL, and removes the temporary files in tearDown. app.db and seed.py are
not used. No environment value (DATABASE_URL, SECRET_KEY, ...) is read or printed.
"""
import datetime
import os
import re
import shutil
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import db  # noqa: E402
import simulation  # noqa: E402
from app import create_app  # noqa: E402
from simulation import (  # noqa: E402
    DEFAULT_PARAMS,
    NEUTRAL_PARAMS,
    cash_cover_points,
    parse_params,
    run_scenario,
)
from werkzeug.security import generate_password_hash  # noqa: E402

TODAY = datetime.date(2026, 6, 1)
OPENING = 2500000  # Rs 25,000
PASSWORD = "password123"
PASSWORD_HASH = generate_password_hash(PASSWORD)
CREATED = "2026-05-01T00:00:00"

USER_A = 1
USER_B = 2
USER_C = 3
TABLES = ("users", "students", "invoices", "invoice_items", "payments", "expenses")


# ---------------------------------------------------------------------------
# Seeding helpers (SQL only, parameterised)
# ---------------------------------------------------------------------------

def _add_user(conn, user_id, email):
    conn.execute(
        "INSERT INTO users (id, name, email, password_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, f"Sim User {user_id}", email, PASSWORD_HASH, CREATED),
    )


def _add_student(conn, student_id, user_id, fee_paise):
    conn.execute(
        "INSERT INTO students (id, user_id, name, monthly_fee_paise, start_date, active, created_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (student_id, user_id, f"Student {student_id}", fee_paise, "2026-04-01", CREATED),
    )


def _add_invoice(conn, invoice_id, user_id, student_id, number, invoice_date, due_date, total_paise):
    conn.execute(
        "INSERT INTO invoices (id, user_id, student_id, invoice_number, invoice_date, "
        "due_date, description, total_paise, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (invoice_id, user_id, student_id, number, invoice_date, due_date,
         "Monthly fee", total_paise, CREATED),
    )
    conn.execute(
        "INSERT INTO invoice_items (invoice_id, description, quantity, unit_price_paise, amount_paise) "
        "VALUES (?, ?, ?, ?, ?)",
        (invoice_id, "Monthly fee", 1, total_paise, total_paise),
    )


def _add_payment(conn, user_id, invoice_id, amount_paise, payment_date):
    conn.execute(
        "INSERT INTO payments (user_id, invoice_id, amount_paise, payment_date, method, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, invoice_id, amount_paise, payment_date, "cash", "", CREATED),
    )


def _add_expense(conn, user_id, name, amount_paise, due_date, paid, paid_date, recurring):
    conn.execute(
        "INSERT INTO expenses (user_id, name, category, amount_paise, due_date, paid, paid_date, "
        "recurring_monthly, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, name, "Operations", amount_paise, due_date, paid, paid_date, recurring, CREATED),
    )


def _seed_user_a(conn):
    """User A: 2 open invoices + 1 partly paid overdue invoice, two expenses."""
    _add_user(conn, USER_A, "sim-a@example.test")
    _add_student(conn, 1, USER_A, 250000)
    _add_invoice(conn, 1, USER_A, 1, "INV-A-0001", "2026-05-10", "2026-06-10", 250000)
    _add_invoice(conn, 2, USER_A, 1, "INV-A-0002", "2026-06-10", "2026-07-10", 250000)
    _add_invoice(conn, 3, USER_A, 1, "INV-A-0003", "2026-02-10", "2026-03-10", 250000)
    _add_payment(conn, USER_A, 3, 100000, "2026-05-05")
    _add_expense(conn, USER_A, "Rent", 300000, "2026-06-15", 0, None, 1)
    _add_expense(conn, USER_A, "Internet", 50000, "2026-05-05", 1, "2026-05-05", 0)


def _seed_user_b(conn):
    """User B: one large invoice that must never leak into user A's forecast."""
    _add_user(conn, USER_B, "sim-b@example.test")
    _add_student(conn, 2, USER_B, 9999900)
    _add_invoice(conn, 4, USER_B, 2, "INV-B-0001", "2026-06-01", "2026-06-20", 9999900)


def _seed_user_c(conn):
    """User C: no rows at all beyond the user record."""
    _add_user(conn, USER_C, "sim-c@example.test")


def _table_counts(conn):
    return {
        table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
        for table in TABLES
    }


def _neutral(**overrides):
    params = dict(NEUTRAL_PARAMS)
    params.update(overrides)
    return params


# ---------------------------------------------------------------------------
# parse_params (no database)
# ---------------------------------------------------------------------------

class ParseParamsTests(unittest.TestCase):
    def test_blank_uses_defaults(self):
        params, error = parse_params({})
        self.assertIsNone(error)
        self.assertEqual(params["delay_days"], DEFAULT_PARAMS["delay_days"])
        self.assertEqual(params["collection_pct"], DEFAULT_PARAMS["collection_pct"])
        self.assertEqual(params["revenue_change_pct"], DEFAULT_PARAMS["revenue_change_pct"])
        self.assertEqual(
            params["extra_monthly_expense_paise"],
            DEFAULT_PARAMS["extra_monthly_expense_paise"],
        )

    def test_rejects_non_integer_delay(self):
        params, error = parse_params({"delay_days": "abc"})
        self.assertIsNotNone(error)
        self.assertIsNone(params)

    def test_rejects_delay_over_limit(self):
        params, error = parse_params({"delay_days": "500"})
        self.assertIsNotNone(error)
        self.assertIsNone(params)

    def test_rejects_collection_over_100(self):
        params, error = parse_params({"collection_pct": "150"})
        self.assertIsNotNone(error)
        self.assertIsNone(params)

    def test_rejects_negative_extra_expense(self):
        params, error = parse_params({"extra_monthly_expense": "-5"})
        self.assertIsNotNone(error)
        self.assertIsNone(params)

    def test_parses_indian_comma_amount(self):
        params, error = parse_params({"extra_monthly_expense": "1,000"})
        self.assertIsNone(error)
        self.assertEqual(params["extra_monthly_expense_paise"], 100000)  # Rs 1,000

    def test_rejects_revenue_change_below_minus_50(self):
        params, error = parse_params({"revenue_change_pct": "-60"})
        self.assertIsNotNone(error)
        self.assertIsNone(params)


# ---------------------------------------------------------------------------
# Scenario calculations (user A)
# ---------------------------------------------------------------------------

class ScenarioCalculationTests(unittest.TestCase):
    def setUp(self):
        self._original_db_path = db.DB_PATH
        self.tmp_dir = tempfile.mkdtemp(prefix="bizflow_sim_calc_")
        db.DB_PATH = os.path.join(self.tmp_dir, "simulation_calc.db")
        self.app = create_app()
        self.app.config["TESTING"] = True
        with self.app.app_context():
            conn = db.get_db()
            _seed_user_a(conn)
            conn.commit()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _run(self, params, today=TODAY, opening=OPENING):
        with self.app.app_context():
            return run_scenario(db.get_db(), USER_A, today, opening, params)

    def _baseline(self, today=TODAY, opening=OPENING):
        import finance
        with self.app.app_context():
            daily, summary = finance.build_cashflow_forecast(db.get_db(), USER_A, today, opening, 90)
        return daily, summary

    def test_neutral_matches_existing_forecast(self):
        import finance
        result = self._run(_neutral())
        with self.app.app_context():
            daily, _ = finance.build_cashflow_forecast(db.get_db(), USER_A, TODAY, OPENING, 90)
        self.assertEqual(
            result["baseline_balance_paise"],
            [e["balance_paise"] for e in daily],
        )
        self.assertEqual(result["scenario_balance_paise"], result["baseline_balance_paise"])

    def test_collection_halves_inflows(self):
        daily, _ = self._baseline()
        baseline_inflow = sum(e["inflow_paise"] for e in daily)
        result = self._run(_neutral(collection_pct=50))
        self.assertLessEqual(
            abs(sum(result["scenario_inflow_paise"]) - baseline_inflow / 2), 91
        )

    def test_revenue_change_scales_inflows(self):
        daily, _ = self._baseline()
        baseline_total = sum(e["inflow_paise"] for e in daily)
        result = self._run(_neutral(revenue_change_pct=10))
        self.assertLessEqual(
            abs(sum(result["scenario_inflow_paise"]) - baseline_total * 1.10), 91
        )

    def test_extra_expense_charged_on_first_of_month(self):
        result = self._run(_neutral(extra_monthly_expense_paise=100000))
        n = sum(1 for label in result["labels"] if label.endswith("-01"))
        self.assertGreater(n, 0)
        summary = result["summary"]
        self.assertEqual(summary["additional_expenses_paise"], 100000 * n)
        self.assertEqual(
            result["scenario_balance_paise"][-1],
            result["baseline_balance_paise"][-1] - 100000 * n,
        )

    def test_scenario_closing_difference_matches_summary(self):
        result = self._run({"delay_days": 15, "collection_pct": 80,
                            "revenue_change_pct": 0, "extra_monthly_expense_paise": 50000})
        summary = result["summary"]
        scenario_closing = result["scenario_balance_paise"][-1]
        baseline_closing = result["baseline_balance_paise"][-1]
        self.assertEqual(
            summary["difference"]["closing_balance_paise"],
            scenario_closing - baseline_closing,
        )

    def test_delay_only_leaves_expenses_unchanged(self):
        daily, _ = self._baseline()
        baseline_outflow = sum(e["outflow_paise"] for e in daily)
        result = self._run(_neutral(delay_days=15))
        self.assertEqual(sum(result["scenario_outflow_paise"]), baseline_outflow)

    def test_health_matches_compute_health_score(self):
        import finance
        result = self._run(_neutral())
        with self.app.app_context():
            health = finance.compute_health_score(db.get_db(), USER_A, TODAY, OPENING)
        self.assertEqual(result["health"]["baseline_total"], health["total_points"])
        self.assertEqual(result["health"]["scenario_total"], result["health"]["baseline_total"])

    def test_health_recomputation_on_real_date(self):
        # Regression: this raised "health recomputation mismatch" before the
        # int-truncation fix. It must not raise on the real current date.
        result = self._run(_neutral(), today=datetime.date.today())
        self.assertIn("health", result)

    def test_warnings_empty_when_neutral(self):
        result = self._run(_neutral())
        self.assertEqual(result["summary"]["warnings"], [])

    def test_warning_mentions_collection_when_reduced(self):
        result = self._run(_neutral(collection_pct=50))
        self.assertTrue(any("50%" in w for w in result["summary"]["warnings"]))

    def test_warnings_flag_negative_scenario(self):
        # Rs 1,00,000 extra per month drives the scenario below zero.
        result = self._run(_neutral(extra_monthly_expense_paise=10000000))
        self.assertGreater(result["summary"]["negative_days"], 0)
        self.assertTrue(any("fall below zero" in w for w in result["summary"]["warnings"]))


# ---------------------------------------------------------------------------
# Read-only guarantee
# ---------------------------------------------------------------------------

class NoWritesTests(unittest.TestCase):
    def setUp(self):
        self._original_db_path = db.DB_PATH
        self.tmp_dir = tempfile.mkdtemp(prefix="bizflow_sim_nowrite_")
        db.DB_PATH = os.path.join(self.tmp_dir, "simulation_nowrite.db")
        self.app = create_app()
        self.app.config["TESTING"] = True
        with self.app.app_context():
            conn = db.get_db()
            _seed_user_a(conn)
            _seed_user_b(conn)
            conn.commit()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_simulation_never_writes(self):
        with self.app.app_context():
            conn = db.get_db()
            before = _table_counts(conn)
            run_scenario(conn, USER_A, TODAY, OPENING, {
                "delay_days": 15,
                "collection_pct": 50,
                "revenue_change_pct": 0,
                "extra_monthly_expense_paise": 100000,
            })
            after = _table_counts(conn)
            rent_paid = conn.execute(
                "SELECT paid FROM expenses WHERE user_id = ? AND name = ?",
                (USER_A, "Rent"),
            ).fetchone()["paid"]
        self.assertEqual(before, after)
        self.assertEqual(int(rent_paid), 0)


# ---------------------------------------------------------------------------
# Isolation between users and empty data
# ---------------------------------------------------------------------------

class IsolationTests(unittest.TestCase):
    def setUp(self):
        self._original_db_path = db.DB_PATH
        self.tmp_dir = tempfile.mkdtemp(prefix="bizflow_sim_iso_")
        db.DB_PATH = os.path.join(self.tmp_dir, "simulation_iso.db")
        self.app = create_app()
        self.app.config["TESTING"] = True
        with self.app.app_context():
            conn = db.get_db()
            _seed_user_a(conn)
            _seed_user_b(conn)
            _seed_user_c(conn)
            conn.commit()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _run(self, user_id):
        with self.app.app_context():
            return run_scenario(db.get_db(), user_id, TODAY, OPENING, _neutral())

    def test_other_users_invoices_excluded(self):
        # User A alone: opening Rs 25,000 + overdue balance Rs 1,500 collected
        # on day 0 = Rs 26,500 = 2650000 paise. User B's Rs 99,999 invoice
        # (9999900 paise) is due 2026-06-20 and must not appear at all.
        result = self._run(USER_A)
        self.assertEqual(result["baseline_balance_paise"][0], 2650000)
        self.assertTrue(all(b < 2500000 + 9999900 for b in result["baseline_balance_paise"]))

    def test_empty_user_returns_flat_balance(self):
        result = self._run(USER_C)  # no exception expected
        self.assertFalse(result["has_data"])
        self.assertTrue(all(b == OPENING for b in result["baseline_balance_paise"]))


# ---------------------------------------------------------------------------
# GET /forecast/simulate
# ---------------------------------------------------------------------------

class RouteTests(unittest.TestCase):
    def setUp(self):
        self._original_db_path = db.DB_PATH
        self.tmp_dir = tempfile.mkdtemp(prefix="bizflow_sim_route_")
        db.DB_PATH = os.path.join(self.tmp_dir, "simulation_route.db")
        self.app = create_app()  # CSRF stays enabled: login is exercised with a token
        self.app.config["TESTING"] = True
        with self.app.app_context():
            conn = db.get_db()
            _seed_user_a(conn)
            conn.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _login_a(self):
        page = self.client.get("/login")
        match = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True))
        token = match.group(1) if match else ""
        self.client.post("/login", data={
            "email": "sim-a@example.test",
            "password": PASSWORD,
            "csrf_token": token,
        })

    def _counts(self):
        with self.app.app_context():
            conn = db.get_db()
            return {
                "invoices": _table_counts(conn)["invoices"],
                "payments": _table_counts(conn)["payments"],
                "expenses": _table_counts(conn)["expenses"],
            }

    def test_logged_in_returns_json(self):
        self._login_a()
        response = self.client.get("/forecast/simulate?delay_days=15&collection_pct=80")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("summary", data)
        self.assertIn("labels", data)
        self.assertEqual(len(data["labels"]), 91)

    def test_invalid_input_returns_400(self):
        self._login_a()
        response = self.client.get("/forecast/simulate?delay_days=abc")
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

    def test_logged_out_redirects(self):
        anonymous = self.app.test_client()
        response = anonymous.get("/forecast/simulate")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))

    def test_route_does_not_write(self):
        self._login_a()
        before = self._counts()
        response = self.client.get("/forecast/simulate?delay_days=15&collection_pct=50&extra_monthly_expense=1,000")
        self.assertEqual(response.status_code, 200)
        after = self._counts()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
