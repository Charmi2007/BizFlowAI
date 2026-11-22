"""Phase 2 PostgreSQL integration tests for BizFlow AI (stdlib unittest).

Skipped unless the environment variable BIZFLOW_RUN_POSTGRES_TESTS is "1".

Run from the project root on Windows with:

    set BIZFLOW_RUN_POSTGRES_TESTS=1
    .venv\\Scripts\\python -m unittest tests.test_postgres

The PostgreSQL URL is read from the project-root .env (or the environment) and
is never printed, logged, or included in any assertion or exception text.
tests/__init__.py forces SQLite for the other tests; setUpModule here restores
the real URL for this module only and tearDownModule puts the previous value back.

Safety rules enforced below:
  * Test users always use emails like phase2-test-<tag>-<uuid4 hex>@example.com.
  * The only DELETE is tearDownModule's delete of those test users (cascades).
  * No DROP or TRUNCATE is issued anywhere in this module.
  * init_db() runs only against the configured database, via create_app().
"""
import datetime
import os
import sys
import unittest
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import dotenv_values  # noqa: E402

import db  # noqa: E402
import expenses  # noqa: E402
import finance  # noqa: E402
from app import create_app  # noqa: E402

ENABLE_ENV = "BIZFLOW_RUN_POSTGRES_TESTS"
TEST_EMAIL_PREFIX = "phase2-test-"
TEST_EMAIL_SUFFIX = "@example.com"
TEST_EMAIL_DELETE_SQL = "DELETE FROM users WHERE email LIKE 'phase2-test-%@example.com'"

_APP = None
_SAVED_ENV = {"present": False, "value": None}


def _restore_env():
    if _SAVED_ENV["present"]:
        os.environ["DATABASE_URL"] = _SAVED_ENV["value"]
    else:
        os.environ.pop("DATABASE_URL", None)


def _new_email(tag):
    """Return a unique test email; refuse anything outside the test pattern."""
    email = f"{TEST_EMAIL_PREFIX}{tag}-{uuid.uuid4().hex}{TEST_EMAIL_SUFFIX}"
    if not (email.startswith(TEST_EMAIL_PREFIX) and email.endswith(TEST_EMAIL_SUFFIX)):
        raise ValueError("generated test email does not match the test pattern")
    return email


def setUpModule():
    global _APP
    if os.environ.get(ENABLE_ENV) != "1":
        raise unittest.SkipTest(f"set {ENABLE_ENV}=1 to run the PostgreSQL tests")

    values = dotenv_values(Path(__file__).resolve().parent.parent / ".env")
    url = (values.get("DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
    if not url.startswith(("postgres://", "postgresql://")):
        raise unittest.SkipTest("no PostgreSQL DATABASE_URL configured")

    _SAVED_ENV["present"] = "DATABASE_URL" in os.environ
    _SAVED_ENV["value"] = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url

    try:
        if db.get_database_info().get("dialect") != "postgresql":
            raise unittest.SkipTest("configured database is not PostgreSQL")
        _APP = create_app()  # init_db() runs against the configured database only
        _APP.config["TESTING"] = True
    except unittest.SkipTest:
        _restore_env()
        raise
    except Exception as e:
        _restore_env()
        raise RuntimeError("PostgreSQL connection failed: " + type(e).__name__) from None


def tearDownModule():
    try:
        if _APP is not None:
            with _APP.app_context():
                conn = db.get_db()
                conn.execute(TEST_EMAIL_DELETE_SQL)  # only phase2-test users; cascades to child rows
                conn.commit()
    finally:
        _restore_env()


@unittest.skipUnless(
    os.environ.get(ENABLE_ENV) == "1",
    f"set {ENABLE_ENV}=1 to run the PostgreSQL tests",
)
class PostgresPhase2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _APP
        cls.client = cls.app.test_client()
        cls.password = "phase2-pass-" + uuid.uuid4().hex
        cls.today = datetime.date.today()
        cls.due = cls.today + datetime.timedelta(days=30)
        suffix = uuid.uuid4().hex[:8]

        cls.a_email = _new_email("a")
        cls.b_email = _new_email("b")
        cls.a_name = f"Phase2 Owner A {suffix}"
        cls.b_name = f"Phase2 Owner B {suffix}"
        cls.student_name = f"Phase2 Student {suffix}"
        cls.expense_name = f"Phase2 Expense {suffix}"

        cls._post_register(cls.a_name, cls.a_email)
        cls._post_register(cls.b_name, cls.b_email)
        cls.a_id = cls._query_one("SELECT id FROM users WHERE email = ?", (cls.a_email,))["id"]
        cls.b_id = cls._query_one("SELECT id FROM users WHERE email = ?", (cls.b_email,))["id"]

        cls._flow_built = False
        cls.student_id = None
        cls.invoice_id = None
        cls.invoice_number = None

    # Helpers -----------------------------------------------------------

    @classmethod
    def _post_register(cls, name, email):
        cls.client.post("/logout")
        response = cls.client.post("/register", data={
            "name": name,
            "email": email,
            "password": cls.password,
            "confirm_password": cls.password,
        })
        assert response.status_code == 302, f"register returned {response.status_code}"

    @classmethod
    def _query_one(cls, sql, params=()):
        with cls.app.app_context():
            return db.get_db().execute(sql, params).fetchone()

    def _login(self, email):
        self.client.post("/logout")
        response = self.client.post("/login", data={"email": email, "password": self.password})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/dashboard"))

    def _build_flow(self):
        """Create A's student, invoice, payment and expense once per class run."""
        cls = type(self)
        if cls._flow_built:
            return
        self._login(self.a_email)

        response = self.client.post("/students/new", data={
            "name": self.student_name,
            "parent_name": "Phase2 Parent",
            "phone": "",
            "email": "",
            "monthly_fee": "1000",
            "start_date": self.today.isoformat(),
            "address": "",
            "active": "1",
        })
        self.assertEqual(response.status_code, 302)
        cls.student_id = self._query_one(
            "SELECT id FROM students WHERE user_id = ? AND name = ?",
            (self.a_id, self.student_name),
        )["id"]

        response = self.client.post("/invoices/new", data={
            "student_id": str(cls.student_id),
            "invoice_date": self.today.isoformat(),
            "due_date": self.due.isoformat(),
            "description": "Phase2 fee",
            "item_description": ["Tuition"],
            "item_quantity": ["1"],
            "item_unit_price": ["1000"],
        })
        self.assertEqual(response.status_code, 302)
        invoice = self._query_one(
            "SELECT id, invoice_number FROM invoices WHERE user_id = ? AND student_id = ?",
            (self.a_id, cls.student_id),
        )
        cls.invoice_id = invoice["id"]
        cls.invoice_number = invoice["invoice_number"]

        response = self.client.post(f"/invoices/{cls.invoice_id}/payments/new", data={
            "amount": "400",
            "payment_date": self.today.isoformat(),
            "method": "cash",
            "note": "",
        })
        self.assertEqual(response.status_code, 302)

        response = self.client.post("/expenses/new", data={
            "name": self.expense_name,
            "category": expenses.CATEGORIES[0],
            "amount": "250",
            "due_date": self.today.isoformat(),
        })
        self.assertEqual(response.status_code, 302)

        cls._flow_built = True

    # Tests -------------------------------------------------------------

    def test_connectivity(self):
        row = self._query_one("SELECT 1 AS one")
        self.assertEqual(row["one"], 1)

        with self.app.app_context():
            connection = db.get_db()
            try:
                self.assertTrue(
                    connection._raw.pgconn.ssl_in_use,
                    "this PostgreSQL client connection is not encrypted",
                )
            finally:
                connection.close()

    def test_schema_present(self):
        with self.app.app_context():
            rows = db.get_db().execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            ).fetchall()
        present = {row["table_name"] for row in rows}
        for name in ("users", "students", "invoices", "invoice_items", "payments", "expenses"):
            with self.subTest(table=name):
                self.assertIn(name, present)

    def test_register_login_logout(self):
        email = _new_email("c")
        self.client.post("/logout")
        response = self.client.post("/register", data={
            "name": "Phase2 Owner C",
            "email": email,
            "password": self.password,
            "confirm_password": self.password,
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/dashboard"))

        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)

        response = self.client.post("/logout")
        self.assertEqual(response.status_code, 302)

        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.location)

        response = self.client.post("/login", data={"email": email, "password": self.password})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/dashboard"))

    def test_students_invoices_payments_expenses_flow(self):
        self._build_flow()
        self._login(self.a_email)

        response = self.client.get("/students")
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.student_name, response.get_data(as_text=True))

        response = self.client.get("/invoices")
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.invoice_number, response.get_data(as_text=True))

        response = self.client.get(f"/invoices/{self.invoice_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(db.format_inr(40000), response.get_data(as_text=True))

        response = self.client.get("/expenses")
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.expense_name, response.get_data(as_text=True))

        student_count = self._query_one(
            "SELECT COUNT(*) AS n FROM students WHERE user_id = ? AND name = ?",
            (self.a_id, self.student_name),
        )["n"]
        self.assertEqual(student_count, 1)

        invoice = self._query_one(
            "SELECT COUNT(*) AS n, MAX(total_paise) AS total FROM invoices WHERE id = ?",
            (self.invoice_id,),
        )
        self.assertEqual(invoice["n"], 1)
        self.assertEqual(invoice["total"], 100000)

        payment = self._query_one(
            "SELECT COUNT(*) AS n, MAX(amount_paise) AS amount FROM payments WHERE invoice_id = ?",
            (self.invoice_id,),
        )
        self.assertEqual(payment["n"], 1)
        self.assertEqual(payment["amount"], 40000)

        expense = self._query_one(
            "SELECT COUNT(*) AS n, MAX(amount_paise) AS amount FROM expenses "
            "WHERE user_id = ? AND name = ?",
            (self.a_id, self.expense_name),
        )
        self.assertEqual(expense["n"], 1)
        self.assertEqual(expense["amount"], 25000)

    def test_followup_and_forecast_pages(self):
        self._build_flow()
        self._login(self.a_email)

        response = self.client.get("/followup")
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.student_name, response.get_data(as_text=True))

        for path in ("/forecast", "/forecast/"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)

        with self.app.app_context():
            _, summary = finance.build_cashflow_forecast(
                db.get_db(), self.a_id, self.today, 0, 90
            )
        for key in ("balance_day30", "balance_day60", "balance_day90"):
            with self.subTest(key=key):
                self.assertIn(key, summary)
                self.assertIsInstance(summary[key], int)

    def test_foreign_key_enforced(self):
        self._build_flow()
        with self.app.app_context():
            conn = db.get_db()
            with self.assertRaises(db.IntegrityError):
                conn.execute(
                    "INSERT INTO payments (user_id, invoice_id, amount_paise, payment_date, "
                    "method, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (self.a_id, 999999999, 100, "2026-01-01", "cash", "", "2026-01-01T00:00:00"),
                )
            conn.rollback()

    def test_transaction_rollback(self):
        self._build_flow()
        with self.app.app_context():
            conn = db.get_db()
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM students WHERE user_id = ?", (self.a_id,)
            ).fetchone()["n"]
            conn.execute(
                "INSERT INTO students (user_id, name, parent_name, phone, email, address, "
                "monthly_fee_paise, start_date, active, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.a_id,
                    f"Phase2 Rollback Probe {uuid.uuid4().hex[:8]}",
                    "",
                    "",
                    "",
                    "",
                    0,
                    self.today.isoformat(),
                    1,
                    "2026-01-01T00:00:00",
                ),
            )
            conn.rollback()
            after = conn.execute(
                "SELECT COUNT(*) AS n FROM students WHERE user_id = ?", (self.a_id,)
            ).fetchone()["n"]
        self.assertEqual(before, after)

    def test_user_isolation_on_postgres(self):
        self._build_flow()
        self._login(self.b_email)

        response = self.client.get(f"/students/{self.student_id}")
        self.assertEqual(response.status_code, 404)

        response = self.client.get(f"/invoices/{self.invoice_id}")
        self.assertEqual(response.status_code, 404)

        response = self.client.get("/followup")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.student_name, response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
