"""CSRF protection and user preference tests for BizFlow AI.

Run from the project root on Windows with:

    .venv\\Scripts\\python -m unittest tests.test_csrf_and_preferences

Each test points db.DB_PATH at its own temporary SQLite file and creates the app
through create_app(). CSRF stays enabled, so every state-changing POST below
either carries a real token fetched from the page or is deliberately sent
without one (or with a bogus one) to check that it is rejected. app.db is not
used. No secret or environment value is printed or logged.
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
from app import create_app  # noqa: E402

PASSWORD = "csrf-prefs-pass-1"
WIDGET_KEYS = ("metrics", "cashflow", "upcoming", "cash_impact", "alerts", "health")
ALL_WIDGETS = {f"widget_{key}": "on" for key in WIDGET_KEYS}


def csrf_token_from(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client, email, password):
    page = client.get("/login")
    token = csrf_token_from(page.get_data(as_text=True))
    return client.post("/login", data={
        "email": email,
        "password": password,
        "csrf_token": token,
    })


def register(client, name, email, password):
    page = client.get("/register")
    token = csrf_token_from(page.get_data(as_text=True))
    return client.post("/register", data={
        "name": name,
        "email": email,
        "password": password,
        "confirm_password": password,
        "csrf_token": token,
    })


def post_with_token(client, url, data):
    page = client.get("/settings")
    token = csrf_token_from(page.get_data(as_text=True))
    merged = dict(data)
    merged["csrf_token"] = token
    return client.post(url, data=merged)


class _BaseTests(unittest.TestCase):
    def setUp(self):
        self._original_db_path = db.DB_PATH
        self.tmp_dir = tempfile.mkdtemp(prefix="bizflow_csrf_prefs_")
        db.DB_PATH = os.path.join(self.tmp_dir, "csrf_prefs_test.db")
        self.app = create_app()  # runs db.init_db() against the temporary file
        self.app.config["TESTING"] = True
        # WTF_CSRF_ENABLED is deliberately NOT set here: CSRF stays on.
        self.client = self.app.test_client()

    def tearDown(self):
        db.DB_PATH = self._original_db_path
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    # Helpers -----------------------------------------------------------

    def _signed_in(self, client, email, name="Test Owner"):
        response = register(client, name, email, PASSWORD)
        self.assertEqual(response.status_code, 302)
        return self._user_id(email)

    def _user_id(self, email):
        return self._scalar("SELECT id FROM users WHERE email = ?", (email,))

    def _scalar(self, sql, params=()):
        with self.app.app_context():
            return db.get_db().execute(sql, params).fetchone()[0]

    def _dashboard_body(self, client):
        response = client.get("/dashboard")
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)


class CsrfTests(_BaseTests):
    def test_login_post_without_token_is_rejected(self):
        email = "csrf-no-token@example.test"
        register(self.client, "No Token", email, PASSWORD)

        anonymous = self.app.test_client()
        response = anonymous.post("/login", data={"email": email, "password": PASSWORD})
        self.assertEqual(response.status_code, 302)

        response = anonymous.get("/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_login_post_with_token_is_accepted(self):
        email = "csrf-login-ok@example.test"
        register(self.client, "Login Ok", email, PASSWORD)

        response = login(self.client, email, PASSWORD)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/dashboard"))

    def test_logout_without_token_is_rejected(self):
        email = "csrf-logout@example.test"
        self._signed_in(self.client, email)

        response = self.client.post("/logout", data={})
        self.assertEqual(response.status_code, 302)

        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 200)

    def test_settings_post_without_token_changes_nothing(self):
        email = "csrf-settings-none@example.test"
        user_id = self._signed_in(self.client, email)

        response = self.client.post("/settings", data={"theme": "dark", "currency": "INR"})
        self.assertEqual(response.status_code, 302)

        dark_rows = self._scalar(
            "SELECT COUNT(*) FROM user_preferences WHERE user_id = ? AND theme = 'dark'",
            (user_id,),
        )
        self.assertEqual(dark_rows, 0)

    def test_invalid_token_is_rejected(self):
        email = "csrf-settings-bogus@example.test"
        user_id = self._signed_in(self.client, email)

        response = self.client.post("/settings", data={
            "csrf_token": "bogus",
            "theme": "dark",
            "currency": "INR",
        })
        self.assertEqual(response.status_code, 302)

        dark_rows = self._scalar(
            "SELECT COUNT(*) FROM user_preferences WHERE user_id = ? AND theme = 'dark'",
            (user_id,),
        )
        self.assertEqual(dark_rows, 0)


class PreferenceTests(_BaseTests):
    def test_default_theme_is_light(self):
        self._signed_in(self.client, "pref-default-theme@example.test")

        body = self._dashboard_body(self.client)
        self.assertRegex(body, r'<body[^>]*data-theme="light"')

    def test_saved_dark_theme_is_applied(self):
        self._signed_in(self.client, "pref-dark@example.test")

        response = post_with_token(self.client, "/settings", {
            "theme": "dark",
            "currency": "INR",
            **ALL_WIDGETS,
        })
        self.assertEqual(response.status_code, 302)

        body = self._dashboard_body(self.client)
        self.assertRegex(body, r'<body[^>]*data-theme="dark"')

    def test_unticked_widgets_are_hidden(self):
        self._signed_in(self.client, "pref-only-metrics@example.test")

        response = post_with_token(self.client, "/settings", {
            "theme": "light",
            "currency": "INR",
            "widget_metrics": "on",
        })
        self.assertEqual(response.status_code, 302)

        body = self._dashboard_body(self.client)
        self.assertIn('class="kpi-grid stat-grid"', body)
        self.assertNotIn("Income vs expenses (last 6 months)", body)
        self.assertNotIn("Next ", body)
        self.assertNotIn("Cash impact of a", body)
        self.assertNotIn("Business health score", body)
        self.assertNotIn("<h2>Alerts</h2>", body)

    def test_all_widgets_hidden_shows_message(self):
        self._signed_in(self.client, "pref-none-visible@example.test")

        response = post_with_token(self.client, "/settings", {
            "theme": "light",
            "currency": "INR",
        })
        self.assertEqual(response.status_code, 302)

        body = self._dashboard_body(self.client)
        self.assertIn("All dashboard sections are hidden", body)

    def test_all_widgets_visible_when_saved(self):
        self._signed_in(self.client, "pref-all-visible@example.test")

        response = post_with_token(self.client, "/settings", {
            "theme": "light",
            "currency": "INR",
            **ALL_WIDGETS,
        })
        self.assertEqual(response.status_code, 302)

        body = self._dashboard_body(self.client)
        self.assertIn("Business health score", body)
        self.assertIn('class="kpi-grid stat-grid"', body)

    def test_preferences_persist_across_logout_login(self):
        email = "pref-persist@example.test"
        user_id = self._signed_in(self.client, email)

        response = post_with_token(self.client, "/settings", {
            "theme": "dark",
            "currency": "GBP",
            "widget_metrics": "on",
        })
        self.assertEqual(response.status_code, 302)

        response = post_with_token(self.client, "/logout", {})
        self.assertEqual(response.status_code, 302)

        response = login(self.client, email, PASSWORD)
        self.assertEqual(response.status_code, 302)

        page = self.client.get("/settings")
        self.assertEqual(page.status_code, 200)
        self.assertRegex(
            page.get_data(as_text=True),
            r'value="dark"\s*\n?\s*selected',
        )

        currency = self._scalar(
            "SELECT currency FROM user_preferences WHERE user_id = ?", (user_id,)
        )
        self.assertEqual(currency, "GBP")

    def test_repeated_save_updates_without_duplicates(self):
        user_id = self._signed_in(self.client, "pref-repeat@example.test")

        for theme in ("dark", "dark", "light"):
            response = post_with_token(self.client, "/settings", {
                "theme": theme,
                "currency": "INR",
                **ALL_WIDGETS,
            })
            self.assertEqual(response.status_code, 302)

        pref_rows = self._scalar(
            "SELECT COUNT(*) FROM user_preferences WHERE user_id = ?", (user_id,)
        )
        self.assertEqual(pref_rows, 1)

        theme = self._scalar(
            "SELECT theme FROM user_preferences WHERE user_id = ?", (user_id,)
        )
        self.assertEqual(theme, "light")

        widget_rows = self._scalar(
            "SELECT COUNT(*) FROM dashboard_widgets WHERE user_id = ?", (user_id,)
        )
        self.assertEqual(widget_rows, 6)

    def test_preferences_are_isolated_between_users(self):
        self._signed_in(self.client, "pref-user-a@example.test", name="User A")

        response = post_with_token(self.client, "/settings", {
            "theme": "dark",
            "currency": "INR",
            **ALL_WIDGETS,
        })
        self.assertEqual(response.status_code, 302)
        self.assertRegex(
            self._dashboard_body(self.client),
            r'<body[^>]*data-theme="dark"',
        )

        other = self.app.test_client()
        self._signed_in(other, "pref-user-b@example.test", name="User B")
        body = self._dashboard_body(other)
        self.assertRegex(body, r'<body[^>]*data-theme="light"')

    def test_currency_is_display_only(self):
        user_id = self._signed_in(self.client, "pref-currency@example.test")

        today = datetime.date.today()
        with self.app.app_context():
            conn = db.get_db()
            student_id = conn.insert_returning_id(
                "INSERT INTO students (user_id, name, monthly_fee_paise, start_date, "
                "active, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, "Currency Test Student", 100000, today.isoformat(), 1,
                 today.isoformat()),
            )
            invoice_id = conn.insert_returning_id(
                "INSERT INTO invoices (user_id, student_id, invoice_number, invoice_date, "
                "due_date, description, total_paise, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, student_id, "CSRF-CURRENCY-0001", today.isoformat(),
                 (today + datetime.timedelta(days=30)).isoformat(),
                 "Currency display test", 100000, today.isoformat()),
            )
            conn.commit()

        # Keep every dashboard section visible so the summary cards are rendered.
        response = post_with_token(self.client, "/settings", {
            "theme": "light",
            "currency": "INR",
            **ALL_WIDGETS,
        })
        self.assertEqual(response.status_code, 302)

        def outstanding_text(body):
            match = re.search(
                r'<div class="kpi-label">Total outstanding</div>\s*<div class="kpi-value">([^<]*)</div>',
                body,
            )
            self.assertIsNotNone(match, "Total outstanding value not found on dashboard")
            return match.group(1)

        before = outstanding_text(self._dashboard_body(self.client))

        response = post_with_token(self.client, "/settings", {
            "theme": "light",
            "currency": "USD",
            **ALL_WIDGETS,
        })
        self.assertEqual(response.status_code, 302)
        currency = self._scalar(
            "SELECT currency FROM user_preferences WHERE user_id = ?", (user_id,)
        )
        self.assertEqual(currency, "USD")

        after = outstanding_text(self._dashboard_body(self.client))
        self.assertEqual(before, after)

        total_paise = self._scalar(
            "SELECT total_paise FROM invoices WHERE id = ?", (invoice_id,)
        )
        self.assertEqual(total_paise, 100000)


if __name__ == "__main__":
    unittest.main()
