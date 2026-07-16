"""
attendance_system/api/admin/recognition_control.py
Start/stop face recognition streams per session.
MJPEG live video streaming endpoint.
Recognition log feed.
"""

import logging
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, jsonify, Response
)
from flask_login import current_user, login_required, logout_user, login_user, UserMixin

from core.db import get_db, get_cursor
from auth.helpers import admin_required, admin_or_faculty_required, log_audit, get_client_ip
from core import db as core_db 

logger = logging.getLogger(__name__)

recog_bp = Blueprint(
    "recognition", __name__,
    url_prefix="/admin/recognition",
    template_folder="../../templates/admin"
)

@recog_bp.route("/init-models", methods=["POST"])
@login_required
@admin_required
def init_models():
    """Initialize recognition engine and models via admin button."""
    try:
        from app import init_recognition, recognition_engine

        init_recognition()

        if recognition_engine:
            count = recognition_engine.reload_embedding_index(core_db.connection_pool)
            recognition_engine.refresh_student_cache(core_db.connection_pool)
            return jsonify({
                "success": True,
                "message": f"Models initialized on {recognition_engine.device}. "
                           f"{count} embeddings loaded.",
                "device": recognition_engine.device,
                "embeddings": count,
            })

        return jsonify({"success": False, "message": "Engine not initialized."})
       
    except Exception as e:
        logger.error(f"init_models error: {e}")
        return jsonify({"success": False, "message": str(e)})
    
@recog_bp.route("/")
@admin_required
def recognition_panel():
    """Recognition control panel."""
    cursor = get_cursor()

    # Active/scheduled sessions today
    today = datetime.now().strftime("%Y-%m-%d")
    cursor.execute(
        """
        SELECT s.id, s.status, s.rtsp_url,
               sub.code AS subject_code, sub.name AS subject_name,
               sec.section_label, d.code AS dept_code,
               t.start_time, t.end_time, t.room,
               u.full_name AS faculty_name,
               cam.name AS camera_name, cam.rtsp_url AS camera_rtsp
        FROM sessions s
        JOIN timetable t ON t.section_id = s.section_id
        JOIN sections sec ON sec.id = s.section_id
        JOIN departments d ON d.id = sec.department_id
        LEFT JOIN subjects sub ON sub.id = s.subject_id
        LEFT JOIN faculty f ON f.id = s.faculty_id
        LEFT JOIN users u ON u.id = f.user_id
        LEFT JOIN cameras cam ON cam.id = s.camera_id
        WHERE s.session_date = %s
          AND s.status IN ('scheduled', 'active')
        ORDER BY t.start_time
        """,
        (today,)
    )
    sessions_list = cursor.fetchall()

    # Convert timedelta
    for s in sessions_list:
        for k in ("start_time", "end_time"):
            if s.get(k) and hasattr(s[k], "total_seconds"):
                total = int(s[k].total_seconds())
                hours, remainder = divmod(total, 3600)
                minutes, _ = divmod(remainder, 60)
                s[k] = f"{hours:02d}:{minutes:02d}"

    # Active streams
    active_streams = {}
    try:
        from app import stream_manager
        if stream_manager:
            active_streams = stream_manager.list_active_streams()
    except Exception:
        pass

    # Available cameras
    cursor.execute(
        """
        SELECT c.id, c.name, c.rtsp_url, c.status, c.assigned_section_id,
               sec.section_label, d.code AS dept_code
        FROM cameras c
        LEFT JOIN sections sec ON sec.id = c.assigned_section_id
        LEFT JOIN departments d ON d.id = sec.department_id
        ORDER BY c.name
        """
    )
    cameras = cursor.fetchall()

    # Engine status
    engine_status = {"initialized": False, "device": "N/A", "embeddings": 0}
    try:
        from app import recognition_engine
        if recognition_engine:
            engine_status["initialized"] = recognition_engine.is_initialized
            engine_status["device"] = recognition_engine.device
            engine_status["embeddings"] = len(recognition_engine._emb_index_sids)
    except Exception:
        pass

    return render_template(
        "admin/recognition.html",
        sessions=sessions_list,
        active_streams=active_streams,
        cameras=cameras,
        engine_status=engine_status,
    )


@recog_bp.route("/start/<int:session_id>", methods=["POST"])
@admin_or_faculty_required
def start_recognition(session_id):
    """Start face recognition for a session."""
    camera_id = request.form.get("camera_id")

    cursor = get_cursor()
    cursor.execute(
        "SELECT rtsp_url, assigned_section_id FROM cameras WHERE id = %s",
        (camera_id,)
    )
    cam = cursor.fetchone()

    if not cam:
        flash("Camera not found.", "danger")
        return redirect(url_for("recognition.recognition_panel"))

    rtsp_url = cam["rtsp_url"]

    try:
        from app import stream_manager, recognition_engine, init_recognition

        # Initialize if needed
        if not recognition_engine or not recognition_engine.is_initialized:
            init_recognition()
            recognition_engine.reload_embedding_index(core_db.connection_pool)
            recognition_engine.refresh_student_cache(core_db.connection_pool)

        if not stream_manager:
            flash("Stream manager not initialized.", "danger")
            return redirect(url_for("recognition.recognition_panel"))

        # Get RTSP URL
        if not rtsp_url and camera_id:
            cursor = get_cursor()
            cursor.execute("SELECT rtsp_url FROM cameras WHERE id = %s", (int(camera_id),))
            cam = cursor.fetchone()
            if cam:
                rtsp_url = cam["rtsp_url"]

        if not rtsp_url:
            flash("RTSP URL is required.", "danger")
            return redirect(url_for("recognition.recognition_panel"))

        # Get section_id
        cursor = get_cursor()
        cursor.execute("SELECT section_id FROM sessions WHERE id = %s", (session_id,))
        sess = cursor.fetchone()
        if not sess:
            flash("Session not found.", "danger")
            return redirect(url_for("recognition.recognition_panel"))
        if cam and cam.get("assigned_section_id") and cam["assigned_section_id"] != sess["section_id"]:
            flash("Selected camera is assigned to a different section.", "danger")
            return redirect(url_for("recognition.recognition_panel"))

        # Update session with RTSP URL and camera
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE sessions
            SET rtsp_url = %s, camera_id = %s, status = 'active',
                opened_by = %s, opened_at = NOW()
            WHERE id = %s
            """,
            (rtsp_url,
             int(camera_id) if camera_id else None,
             current_user.id, session_id)
        )
        conn.commit()

        # Start stream
        success, message = stream_manager.start_stream(
            session_id, rtsp_url, sess["section_id"], core_db.connection_pool
        )

        if success:
            flash(f"Recognition started for session {session_id}.", "success")
            log_audit(
                  current_user.id, "start_recognition",
                "sessions", session_id, None, f"rtsp={rtsp_url}",
                ip_address=get_client_ip()
            )
        else:
            flash(f"Failed to start: {message}", "danger")

    except Exception as e:
        flash(f"Error: {e}", "danger")
        logger.error(f"start_recognition error: {e}")

    # Check if request is AJAX
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": success, "message": message})

    return redirect(url_for("recognition.recognition_panel"))


@recog_bp.route("/stop/<int:session_id>", methods=["POST"])
@admin_or_faculty_required
def stop_recognition(session_id):
    """Stop recognition for a session."""
    try:
        from app import stream_manager

        if stream_manager:
            success, message = stream_manager.stop_stream(session_id)
            if success:
                flash("Recognition stopped.", "info")
            else:
                flash(f"Stop failed: {message}", "warning")
        else:
            flash("Stream manager not available.", "warning")

    except Exception as e:
        flash(f"Error: {e}", "danger")

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": True})

    return redirect(url_for("recognition.recognition_panel"))


@recog_bp.route("/stream/<int:session_id>")
@admin_or_faculty_required
def mjpeg_stream(session_id):
    """MJPEG video stream endpoint for a session."""
    def generate():
        while True:
            try:
                from app import stream_manager
                if not stream_manager:
                    break

                frame_bytes = stream_manager.get_frame(session_id)
                if frame_bytes:
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n" +
                        frame_bytes +
                        b"\r\n"
                    )
                else:
                    # Send a placeholder frame
                    import numpy as np
                    import cv2
                    blank = np.zeros((480, 640, 3), dtype=np.uint8)
                    cv2.putText(blank, "Waiting for stream...",
                                (100, 240), cv2.FONT_HERSHEY_SIMPLEX,
                                1, (255, 255, 255), 2)
                    _, jpeg = cv2.imencode(".jpg", blank)
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n" +
                        jpeg.tobytes() +
                        b"\r\n"
                    )
            except GeneratorExit:
                break
            except Exception:
                break

            import time
            time.sleep(0.05)  # ~20 fps max for MJPEG

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )


@recog_bp.route("/api/status/<int:session_id>")
@admin_or_faculty_required
def stream_status_api(session_id):
    """AJAX: Get recognition stream status."""
    try:
        from app import stream_manager
        if stream_manager:
            status = stream_manager.get_stream_status(session_id)
            return jsonify(status)
    except Exception:
        pass
    return jsonify({"status": "not_running", "session_id": session_id})


@recog_bp.route("/api/log/<int:session_id>")
@admin_or_faculty_required
def stream_log_api(session_id):
    """AJAX: Get recognition log for a session."""
    last_n = request.args.get("n", 50, type=int)
    try:
        from app import stream_manager
        if stream_manager:
            log = stream_manager.get_log(session_id, last_n)
            return jsonify({"log": log})
    except Exception:
        pass
    return jsonify({"log": []})


@recog_bp.route("/api/active-streams")
@admin_or_faculty_required
def active_streams_api():
    """AJAX: Get all active streams."""
    try:
        from app import stream_manager
        if stream_manager:
            return jsonify(stream_manager.list_active_streams())
    except Exception:
        pass
    return jsonify({})