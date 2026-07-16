"""
attendance_system/api/admin/cameras.py
Camera (ESP32-CAM) device registry: add, edit, test, delete.
"""

import logging
import time

import cv2
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, jsonify
)

from core.db import get_db, get_cursor
from auth.helpers import admin_required

logger = logging.getLogger(__name__)

cameras_bp = Blueprint(
    "cameras", __name__,
    template_folder="../../templates/admin"
)

def _ensure_camera_assignment_column():
    """Ensure cameras.assigned_section_id exists for section binding."""
    conn = get_db()
    cursor = conn.cursor(dictionary=True, buffered=True)
    try:
        cursor.execute(
            """
            SELECT 1
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'cameras'
              AND COLUMN_NAME = 'assigned_section_id'
            LIMIT 1
            """
        )
        if cursor.fetchone():
            return

        cursor.execute("ALTER TABLE cameras ADD COLUMN assigned_section_id INT NULL")
        cursor.execute(
            """
            ALTER TABLE cameras
            ADD CONSTRAINT fk_cameras_assigned_section
            FOREIGN KEY (assigned_section_id) REFERENCES sections(id)
            ON DELETE SET NULL
            """
        )
        conn.commit()
        logger.info("Added cameras.assigned_section_id")
    except Exception as e:
        conn.rollback()
        logger.warning("Could not ensure cameras.assigned_section_id: %s", e)
    finally:
        cursor.close()


@cameras_bp.route("/")
@admin_required
def list_cameras():
    """List all registered cameras."""
    _ensure_camera_assignment_column()
    cursor = get_cursor()
    cursor.execute(
        """
        SELECT c.*, sec.section_label, d.code AS dept_code
        FROM cameras c
        LEFT JOIN sections sec ON sec.id = c.assigned_section_id
        LEFT JOIN departments d ON d.id = sec.department_id
        ORDER BY c.name
        """
    )
    cameras_list = cursor.fetchall()
    cursor.execute(
        """
        SELECT sec.id, sec.section_label, d.code AS dept_code
        FROM sections sec
        JOIN departments d ON d.id = sec.department_id
        JOIN academic_periods ap ON ap.id = sec.academic_period_id
        WHERE ap.is_active = 1
        ORDER BY d.code, sec.section_label
        """
    )
    sections = cursor.fetchall()
    return render_template("admin/cameras.html", cameras=cameras_list, sections=sections)


@cameras_bp.route("/add", methods=["POST"])
@admin_required
def add_camera():
    _ensure_camera_assignment_column()
    name     = request.form.get("name", "").strip()
    rtsp_url = request.form.get("rtsp_url", "").strip()
    location = request.form.get("location", "").strip()
    notes    = request.form.get("notes", "").strip()
    assigned_section_id = request.form.get("assigned_section_id", "").strip()

    if not name or not rtsp_url:
        flash("Name and RTSP URL are required.", "danger")
        return redirect(url_for("cameras.list_cameras"))

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO cameras
                (name, rtsp_url, location, assigned_section_id, status, notes, created_at)
            VALUES (%s, %s, %s, %s, 'inactive', %s, NOW())
            """,
            (
                name, rtsp_url, location or None,
                int(assigned_section_id) if assigned_section_id else None,
                notes or None
            )
        )
        conn.commit()
        flash(f"Camera '{name}' registered.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("cameras.list_cameras"))

@cameras_bp.route("/<int:camera_id>/edit", methods=["POST"])
@admin_required
def edit_camera(camera_id):
    _ensure_camera_assignment_column()
    name     = request.form.get("name", "").strip()
    rtsp_url = request.form.get("rtsp_url", "").strip()
    location = request.form.get("location", "").strip()
    notes    = request.form.get("notes", "").strip()
    assigned_section_id = request.form.get("assigned_section_id", "").strip()

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE cameras
            SET name = %s, rtsp_url = %s, location = %s,
                assigned_section_id = %s, notes = %s
            WHERE id = %s
            """,
            (
                name, rtsp_url, location or None,
                int(assigned_section_id) if assigned_section_id else None,
                notes or None, camera_id
            )
        )
        conn.commit()
        flash("Camera updated.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("cameras.list_cameras"))


@cameras_bp.route("/<int:camera_id>/delete", methods=["POST"])
@admin_required
def delete_camera(camera_id):
    """Delete a camera."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM cameras WHERE id = %s", (camera_id,))
        conn.commit()
        flash("Camera deleted.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("cameras.list_cameras"))


@cameras_bp.route("/<int:camera_id>/test", methods=["POST"])
@admin_required
def test_camera(camera_id):
    """Test if a camera's RTSP stream is accessible."""
    cursor = get_cursor()
    cursor.execute("SELECT rtsp_url FROM cameras WHERE id = %s", (camera_id,))
    cam = cursor.fetchone()

    if not cam:
        return jsonify({"success": False, "message": "Camera not found."})

    rtsp_url = cam["rtsp_url"]
    try:
        cap = cv2.VideoCapture(rtsp_url)
        start = time.time()
        success = False

        while time.time() - start < 5.0:
            ret, frame = cap.read()
            if ret and frame is not None:
                success = True
                break
            time.sleep(0.1)

        cap.release()

        # Update camera status
        conn = get_db()
        cur = conn.cursor()
        new_status = "active" if success else "error"
        cur.execute(
            "UPDATE cameras SET status = %s, last_seen = NOW() WHERE id = %s",
            (new_status, camera_id)
        )
        conn.commit()

        if success:
            return jsonify({
                "success": True,
                "message": f"Camera reachable. Frame size: {frame.shape[1]}x{frame.shape[0]}"
            })
        else:
            return jsonify({"success": False, "message": "Cannot read frames from stream."})

    except Exception as e:
        return jsonify({"success": False, "message": f"Connection error: {e}"})


@cameras_bp.route("/api/list")
@admin_required
def cameras_api():
    """AJAX: Get cameras list as JSON."""
    cursor = get_cursor()
    cursor.execute("SELECT id, name, rtsp_url, status, last_seen FROM cameras ORDER BY name")
    cameras = cursor.fetchall()

    # Convert datetime to string
    for c in cameras:
        if c.get("last_seen"):
            c["last_seen"] = c["last_seen"].strftime("%Y-%m-%d %H:%M:%S")

    return jsonify({"cameras": cameras})

