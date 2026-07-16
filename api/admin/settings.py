"""
attendance_system/api/admin/settings.py
System settings management: edit recognition thresholds,
college name, scheduler time, etc.
"""

import logging

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, jsonify
)
from flask_login import current_user

from core.db import get_db, get_cursor
from core import db as core_db
from auth.helpers import admin_required, log_audit, get_client_ip

logger = logging.getLogger(__name__)

settings_bp = Blueprint(
    "settings", __name__,
    template_folder="../../templates/admin"
)


@settings_bp.route("/")
@admin_required
def view_settings():
    """View all system settings."""
    cursor = get_cursor()
    cursor.execute("SELECT * FROM settings ORDER BY setting_key")
    settings_list = cursor.fetchall()

    # Group settings by category for display
    categories = {
        "General": [],
        "Recognition": [],
        "Attendance": [],
        "Scheduler": [],
        "Other": [],
    }

    key_categories = {
        "college_name": "General",
        "attendance_threshold_red": "Attendance",
        "attendance_threshold_orange": "Attendance",
        "liveness_threshold": "Recognition",
        "recognition_threshold": "Recognition",
        "attendance_cooldown_min": "Attendance",
        "session_auto_open_time": "Scheduler",
        "max_stream_threads": "Recognition",
    }

    for s in settings_list:
        cat = key_categories.get(s["setting_key"], "Other")
        categories[cat].append(s)

    return render_template(
        "admin/settings.html",
        categories=categories,
        settings_list=settings_list,
    )


@settings_bp.route("/update", methods=["POST"])
@admin_required
def update_settings():
    """Update settings from form."""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)

        updated = 0
        for key in request.form:
            if key.startswith("setting_"):
                setting_key = key.replace("setting_", "", 1)
                new_value = request.form.get(key, "").strip()

                # Get old value
                cursor.execute(
                    "SELECT setting_value FROM settings WHERE setting_key = %s",
                    (setting_key,)
                )
                old_row = cursor.fetchone()
                old_value = old_row["setting_value"] if old_row else None

                if old_value != new_value:
                    cursor.execute(
                        """
                        UPDATE settings
                        SET setting_value = %s, updated_at = NOW()
                        WHERE setting_key = %s
                        """,
                        (new_value, setting_key)
                    )
                    updated += 1

                    log_audit(
                          current_user.id, "update_setting",
                        "settings", None, f"{setting_key}={old_value}",
                        f"{setting_key}={new_value}",
                        ip_address=get_client_ip()
                    )

        conn.commit()

        if updated > 0:
            flash(f"{updated} setting(s) updated.", "success")
        else:
            flash("No changes made.", "info")

    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")
        logger.error(f"update_settings error: {e}")

    return redirect(url_for("settings.view_settings"))


@settings_bp.route("/add", methods=["POST"])
@admin_required
def add_setting():
    """Add a new custom setting."""
    key = request.form.get("setting_key", "").strip().lower()
    value = request.form.get("setting_value", "").strip()
    description = request.form.get("description", "").strip()

    if not key:
        flash("Setting key is required.", "danger")
        return redirect(url_for("settings.view_settings"))

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO settings (setting_key, setting_value, description, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE
                setting_value = VALUES(setting_value),
                description = VALUES(description),
                updated_at = NOW()
            """,
            (key, value, description if description else None)
        )
        conn.commit()
        flash(f"Setting '{key}' saved.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("settings.view_settings"))


@settings_bp.route("/reload-models", methods=["POST"])
@admin_required
def reload_models():
    """Reload recognition models and embedding index."""
    try:
        from app import recognition_engine

        if not recognition_engine or not recognition_engine.is_initialized:
            return jsonify({"success": False, "message": "Engine not initialized."})

        count = recognition_engine.reload_embedding_index(core_db.connection_pool)
        recognition_engine.refresh_student_cache(core_db.connection_pool)

        return jsonify({
            "success": True,
            "message": f"Reloaded {count} embeddings.",
            "embeddings": count,
        })

    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@settings_bp.route("/update-scheduler-time", methods=["POST"])
@admin_required
def update_scheduler_time():
    """Update the auto-scheduler time and restart scheduler."""
    new_time = request.form.get("time", "08:00").strip()

    try:
        # Validate format
        parts = new_time.split(":")
        if len(parts) != 2:
            raise ValueError("Invalid time format")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("Hour/minute out of range")

        # Update in settings
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE settings
            SET setting_value = %s, updated_at = NOW()
            WHERE setting_key = 'session_auto_open_time'
            """,
            (new_time,)
        )
        conn.commit()

        flash(f"Scheduler time updated to {new_time}. Restart server to apply.", "success")

        log_audit(
              current_user.id, "update_scheduler_time",
            "settings", None, None, new_time,
            ip_address=get_client_ip()
        )

    except ValueError as e:
        flash(f"Invalid time format: {e}", "danger")
    except Exception as e:
        flash(f"Error: {e}", "danger")

    return redirect(url_for("settings.view_settings"))