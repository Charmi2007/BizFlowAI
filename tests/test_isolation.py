"""Cross-user isolation tests for students, invoices, payments and reports.

Run from the project root on Windows with:

    .venv\\Scripts\\python -m unittest tests.test_isolation

The test points db.DB_PATH at a temporary SQLite file, removes DATABASE_URL
from the environment so the app uses SQLite, creates two users through the
normal HTTP routes, and removes the temporary files in tearDown. app.db and
seed.py are not used.
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
from app import create_app  # noqa: E402

USER_A_NAME = "Alice Owner"
USER_A_EMAIL = "alice.owner@example.test"
USER_B_NAME = "Bob Outsider"
USER_B_EMAIL = "bob.outsider@example.test"
PASSWORD = "isolation-pass-1"
STUDENT_NAME = "Priya Isolated Student"
TODAY = datetime.date.today()
DUE = TODAY + datetime.timedelta(days=30)
INVOICE_RUPEES = "1000"
PAYMENT_RUPEES = "400"
# Outstanding for user A after the partial payment: Rs 1,000 - Rs 400 = Rs 600.
OUTSTANDING_PAISE = 60000

_SAVED_DATABASE_URL = None


def setUpModule():
    global _SAVED_DATABASE_URL
    _SAVED_DATABASE_URL = os.environ.pop("DATABASE_URL", None)


def tearDownModule():
    if _SAVED_DATABASE_URL is not None:
        os.environ["DATABASE_URL"] = _SAVED_DATABASE_URL


class IsolationTests(unittest.TestCase):
    def setUp(self):
        self._original_db_path = db.DB_PATH
        self.tmp_dir = tempfile.mkdtemp(prefix="bizflow_isolation_")
        db.DB_PATH = os.path.join(self.tmp_dir, "isolation_test.db")
        self.app = create_app()  # runs db.init_db() against the temporary file
        self.app.config["WTF_CSRF_ENABLED"] = False  # legacy tests post without tokens; CSRF is covered by tests/test_csrf_and_preferences.py
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

        # User A registers (session is now A) and builds the data set.
        self._register(USER_A_NAME, USER_A_EMAIL)
        self.a_id = self._scalar("SELECT id FROM users WHERE email = ?", (USER_A_EMAIL,))

        response = self.client.post("/students/new", data={
            "name": STUDENT_NAME,
            "parent_name": "Parent Of Priya",
            "phone": "",
            "email": "",
            "monthly_fee": INVOICE_RUPEES,
            "start_date": TODAY.isoformat(),
            "address": "",
            "active": "1",
        })
        self.assertEqual(response.status_code, 302)
        self.student_id = self._scalar(
            "SELECT id FROM students WHERE user_id = ? AND name = ?",
            (self.a_id, STUDENT_NAME),
        )

        response = self.client.post("/invoices/new", data={
            "student_id": str(self.student_id),
            "invoice_date": TODAY.isoformat(),
            "due_date": DUE.isoformat(),
            "description": "Isolation fee",
            "item_description": ["Tuition"],
            "item_quantity": ["1"],
            "item_unit_price": [INVOICE_RUPEES],
        })
        self.assertEqual(response.status_code, 302)
        self.invoice_id = self._scalar(
            "SELECT id FROM invoices WHERE user_id = ? AND student_id = ?",
            (self.a_id, self.student_id),
        )
        self.invoice_number = self._scalar(
            "SELECT invoice_number FROM invoices WHERE id = ?", (self.invoice_id,)
        )

        response = self.client.post(
            f"/invoices/{self.invoice_id}/payments/new",
            data={
                "amount": PAYMENT_RUPEES,
                "payment_date": TODAY.isoformat(),
                "method": "cash",
                "note": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.payment_id = self._scalar(
            "SELECT id FROM payments WHERE invoice_id = ?", (self.invoice_id,)
        )

        # User B registers; the session switches to B.
        self._register(USER_B_NAME, USER_B_EMAIL)
        self.b_id = self._scalar("SELECT id FROM users WHERE email = ?", (USER_B_EMAIL,))

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # Helpers -----------------------------------------------------------

    def _register(self, name, email):
        response = self.client.post("/register", data={
            "name": name,
            "email": email,
            "password": PASSWORD,
            "confirm_password": PASSWORD,
        })
        self.assertEqual(response.status_code, 302)

    def _login(self, email):
        self.client.post("/logout")
        response = self.client.post("/login", data={"email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 302)

    def _scalar(self, sql, params=()):
        with self.app.app_context():
            return db.get_db().execute(sql, params).fetchone()[0]

    def _count(self, sql, params=()):
        return self._scalar(sql, params)

    # Tests -------------------------------------------------------------

    def test_other_user_cannot_view_student(self):
        self._login(USER_B_EMAIL)
        response = self.client.get(f"/students/{self.student_id}")
        self.assertEqual(response.status_code, 404)

    def test_other_user_cannot_edit_or_delete_student(self):
        self._login(USER_B_EMAIL)
        response = self.client.get(f"/students/{self.student_id}/edit")
        self.assertEqual(response.status_code, 404)

        response = self.client.post(f"/students/{self.student_id}/delete")
        self.assertEqual(response.status_code, 404)

        remaining = self._count(
            "SELECT COUNT(*) FROM students WHERE id = ?", (self.student_id,)
        )
        self.assertEqual(remaining, 1)

    def test_other_user_cannot_view_invoice_or_print(self):
        self._login(USER_B_EMAIL)
        for path in (
            f"/invoices/{self.invoice_id}",
            f"/invoices/{self.invoice_id}/print",
            f"/invoices/{self.invoice_id}/payments/new",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)

    def test_other_user_cannot_delete_payment(self):
        self._login(USER_B_EMAIL)
        response = self.client.post(
            f"/invoices/{self.invoice_id}/payments/{self.payment_id}/delete"
        )
        self.assertEqual(response.status_code, 404)

        remaining = self._count(
            "SELECT COUNT(*) FROM payments WHERE id = ?", (self.payment_id,)
        )
        self.assertEqual(remaining, 1)

    def test_followup_excludes_other_users_invoices(self):
        self._login(USER_B_EMAIL)
        response = self.client.get("/followup")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn(STUDENT_NAME, body)
        self.assertNotIn(self.invoice_number, body)

        # Control: the same page for the owner must show both markers.
        self._login(USER_A_EMAIL)
        response = self.client.get("/followup")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn(STUDENT_NAME, body)
        self.assertIn(self.invoice_number, body)

    def test_cannot_create_invoice_for_other_users_student(self):
        self._login(USER_B_EMAIL)
        response = self.client.post("/invoices/new", data={
            "student_id": str(self.student_id),
            "invoice_date": TODAY.isoformat(),
            "due_date": DUE.isoformat(),
            "description": "Injected fee",
            "item_description": ["Injected"],
            "item_quantity": ["1"],
            "item_unit_price": [INVOICE_RUPEES],
        })
        # Either a 200 with the form re-rendered, or a redirect that does not
        # lead to A's data. The database check below is the real assertion.
        self.assertIn(response.status_code, (200, 302))
        if response.status_code == 200:
            self.assertNotIn(STUDENT_NAME, response.get_data(as_text=True))

        b_invoices = self._count(
            "SELECT COUNT(*) FROM invoices WHERE user_id = ?", (self.b_id,)
        )
        self.assertEqual(b_invoices, 0)

        cross_rows = self._count(
            "SELECT COUNT(*) FROM invoices WHERE user_id = ? AND student_id = ?",
            (self.b_id, self.student_id),
        )
        self.assertEqual(cross_rows, 0)

    def test_dashboard_only_shows_own_totals(self):
        self._login(USER_A_EMAIL)
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn(db.format_inr(OUTSTANDING_PAISE), response.get_data(as_text=True))

        self._login(USER_B_EMAIL)
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(STUDENT_NAME, response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
