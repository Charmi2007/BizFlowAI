
import os
import preferences
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

from urllib.parse import urlsplit

from flask import Flask, flash, g, redirect, request, url_for
from flask_wtf.csrf import CSRFError, CSRFProtect

import assistant
import auth
import dashboard
import db
import expenses
import followup
import forecast
import invoices
import students

csrf = CSRFProtect()


def create_app():
    app = Flask(__name__)

    secret_key = os.environ.get("SECRET_KEY")

    if not secret_key:
        if os.environ.get("RENDER"):
            raise RuntimeError("SECRET_KEY must be configured in production.")
        secret_key = "dev-only-change-me"

    app.config["SECRET_KEY"] = secret_key

    csrf.init_app(app)

    @app.errorhandler(CSRFError)
    def handle_csrf_error(_error):
        flash(
            "Your session expired or the form was invalid. Please try again.",
            "error",
        )
        referrer = request.referrer
        if referrer and urlsplit(referrer).netloc == request.host:
            return redirect(referrer, code=302)
        if g.get("user") is None:
            return redirect(url_for("auth.login"), code=302)
        return redirect(url_for("dashboard.dashboard"), code=302)

    db.init_app(app)
    with app.app_context():
        db.init_db()

    app.register_blueprint(auth.bp)
    app.register_blueprint(students.bp)
    app.register_blueprint(invoices.bp)
    app.register_blueprint(followup.bp)
    app.register_blueprint(forecast.bp)
    app.register_blueprint(expenses.bp)
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(assistant.bp)
    app.register_blueprint(preferences.bp)
    app.before_request(auth.load_logged_in_user)

    @app.context_processor
    def inject_user_preferences():
        if g.get("user") is not None:
            return {
                "user_preferences": preferences.get_user_preferences(
                    g.user["id"]
                )
            }

        return {
            "user_preferences": {
                "theme": "light",
                "currency": "INR",
                "settings": {},
            }
        }

    @app.route("/")
    def index():
        if g.user is not None:
            return redirect(url_for("dashboard.dashboard"))
        return redirect(url_for("auth.login"))

    return app


if __name__ == "__main__":
    create_app().run(debug=True)
