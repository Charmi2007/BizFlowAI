import os

from flask import Flask, g, redirect, url_for

import assistant
import auth
import dashboard
import db
import expenses
import followup
import forecast
import invoices
import students


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-only-change-me")

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
    app.before_request(auth.load_logged_in_user)

    @app.route("/")
    def index():
        if g.user is not None:
            return redirect(url_for("dashboard.dashboard"))
        return redirect(url_for("auth.login"))

    return app


if __name__ == "__main__":
    create_app().run(debug=True)
