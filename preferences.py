
"""Per-user settings and dashboard customization for BizFlow AI."""

import json
from datetime import datetime, timezone

from flask import (
    Blueprint,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from auth import login_required
from db import get_db

bp = Blueprint("preferences", __name__)

DEFAULT_WIDGETS = {
    "metrics": "Business overview",
    "cashflow": "Income and expenses chart",
    "upcoming": "Upcoming inflows and expenses",
    "cash_impact": "Payment delay simulator",
    "alerts": "Business alerts",
    "health": "Business health score",
}


def get_user_preferences(user_id):
    row = get_db().execute(
        "SELECT theme, currency, settings_json "
        "FROM user_preferences WHERE user_id = ?",
        (user_id,),
    ).fetchone()

    if row is None:
        return {"theme": "light", "currency": "INR", "settings": {}}

    try:
        settings = json.loads(row["settings_json"] or "{}")
        if not isinstance(settings, dict):
            settings = {}
    except (ValueError, TypeError):
        settings = {}

    theme = row["theme"] if row["theme"] in ("light", "dark") else "light"
    currency = (
        row["currency"]
        if row["currency"] in ("INR", "USD", "EUR", "GBP")
        else "INR"
    )

    return {
        "theme": theme,
        "currency": currency,
        "settings": settings,
    }


def get_dashboard_widgets(user_id):
    rows = get_db().execute(
        "SELECT widget_key, visible FROM dashboard_widgets WHERE user_id = ?",
        (user_id,),
    ).fetchall()

    widgets = {key: True for key in DEFAULT_WIDGETS}

    for row in rows:
        if row["widget_key"] in widgets:
            widgets[row["widget_key"]] = bool(row["visible"])

    return widgets


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user_id = g.user["id"]

    if request.method == "POST":
        theme = request.form.get("theme", "light")
        currency = request.form.get("currency", "INR")

        if theme not in ("light", "dark"):
            flash("Invalid theme selection.", "error")
            return redirect(url_for("preferences.settings"))

        if currency not in ("INR", "USD", "EUR", "GBP"):
            flash("Invalid currency selection.", "error")
            return redirect(url_for("preferences.settings"))

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db = get_db()

        db.execute(
            """
            INSERT INTO user_preferences
                (user_id, theme, currency, settings_json, updated_at)
            VALUES (?, ?, ?, '{}', ?)
            ON CONFLICT(user_id) DO UPDATE SET
                theme = excluded.theme,
                currency = excluded.currency,
                updated_at = excluded.updated_at
            """,
            (user_id, theme, currency, now),
        )

        for position, key in enumerate(DEFAULT_WIDGETS):
            visible = int(request.form.get(f"widget_{key}") == "on")

            db.execute(
                """
                INSERT INTO dashboard_widgets
                    (user_id, widget_key, visible, position, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, widget_key) DO UPDATE SET
                    visible = excluded.visible,
                    position = excluded.position,
                    updated_at = excluded.updated_at
                """,
                (user_id, key, visible, position, now),
            )

        db.commit()
        flash("Your preferences have been saved.", "success")
        return redirect(url_for("preferences.settings"))

    preferences = get_user_preferences(user_id)
    widgets = get_dashboard_widgets(user_id)

    return render_template(
        "settings.html",
        preferences=preferences,
        widgets=widgets,
        widget_labels=DEFAULT_WIDGETS,
    )
