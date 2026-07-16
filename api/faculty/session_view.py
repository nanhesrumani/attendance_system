"""
attendance_system/api/faculty/session_view.py
Session attendance view: student list (green/red), manual overrides,
mark all present/absent, dismiss class, start/stop recognition.
"""

import logging
from datetime import datetime
import uuid
import os
from werkzeug.utils import secure_filename
from api.faculty.sheet_ocr import extract_identifiers_from_image, match_students
from recognition.classroom_photo import recognize_classroom_photos
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, jsonify
)
from flask_login import current_user
from core.db import get_db, get_cursor
from auth.helpers import (
    admin_or_faculty_required, log_audit, get_client_ip, profile_completed_required
)

logger = logging.getLogger(__name__)

session_view_bp = Blueprint(
    "session_view", __name__,
    template_folder="../../templates/faculty"
)


# ============================================
# SESSION DETAIL VIEW
# ============================================

@session_view_bp.route("/<int:session_id>")
@admin_or_faculty_required
@profile_completed_required
def view_session(session_id):
    """View session attendance details."""
    cursor = get_cursor()

    # Session info
    cursor.execute(
        """
        SELECT s.*, t.start_time, t.end_time, t.slot_type, t.room,
               t.day_of_week,
               sub.code AS subject_code, sub.name AS subject_name,
               sec.id AS section_id, sec.section_label,
               d.code AS dept_code, d.name AS dept_name,
               u.full_name AS faculty_name,
               su.full_name AS substitute_name,
               cam.name AS camera_name, cam.rtsp_url AS camera_rtsp
        FROM sessions s
        LEFT JOIN timetable t 
        ON t.section_id = s.section_id
        AND t.subject_id = s.subject_id
        JOIN sections sec ON sec.id = s.section_id
        JOIN departments d ON d.id = sec.department_id
        LEFT JOIN subjects sub ON sub.id = s.subject_id
        LEFT JOIN faculty f ON f.id = s.faculty_id
        LEFT JOIN users u ON u.id = f.user_id
        LEFT JOIN faculty sf ON sf.id = s.substitute_faculty_id
        LEFT JOIN users su ON su.id = sf.user_id
        LEFT JOIN cameras cam ON cam.id = s.camera_id
        WHERE s.id = %s
        """,
        (session_id,)
    )
    session_data = cursor.fetchone()

    if not session_data:
        flash("Session not found.", "danger")
        return redirect(url_for("faculty.dashboard"))

    # Convert timedelta
    for k in ("start_time", "end_time"):
        if session_data.get(k) and hasattr(session_data[k], "total_seconds"):
            total = int(session_data[k].total_seconds())
            hours, remainder = divmod(total, 3600)
            minutes, _ = divmod(remainder, 60)
            session_data[k] = f"{hours:02d}:{minutes:02d}"

    # Get attendance list
    # Get section_id first
    cursor.execute("SELECT section_id FROM sessions WHERE id = %s", (session_id,))
    sess = cursor.fetchone()
    section_id = sess["section_id"]

    # Fetch ALL students with attendance
    cursor.execute(
        """
        SELECT 
            st.id AS student_id,
            st.usn,
            u.full_name,
            COALESCE(a.status, 'absent') AS status,
            a.method,
            a.recognition_score,
            a.marked_at
        FROM students st
        JOIN users u ON u.id = st.user_id
        LEFT JOIN attendance a 
            ON a.student_id = st.id AND a.session_id = %s
        WHERE st.section_id = %s
        ORDER BY st.usn
        """,
        (session_id, section_id)
    )

    attendance_list = cursor.fetchall()

    # Counts
    present_count = sum(1 for a in attendance_list if a["status"] == "present")
    absent_count = sum(1 for a in attendance_list if a["status"] == "absent")
    total_count = len(attendance_list)

    # Check if recognition stream is running
    stream_running = False
    stream_status = {}
    try:
        from app import stream_manager
        if stream_manager:
            stream_status = stream_manager.get_stream_status(session_id)
            stream_running = stream_status.get("is_running", False)
    except Exception:
        pass

    # Available cameras for dropdown
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

    return render_template(
        "faculty/session_view.html",
        session=session_data,
        attendance=attendance_list,
        present_count=present_count,
        absent_count=absent_count,
        total_count=total_count,
        stream_running=stream_running,
        stream_status=stream_status,
        cameras=cameras,
    )


# ============================================
# INDIVIDUAL ATTENDANCE TOGGLE
# ============================================

@session_view_bp.route("/<int:session_id>/toggle/<int:student_id>", methods=["POST"])
@admin_or_faculty_required
@profile_completed_required
def toggle_attendance(session_id, student_id):
    """Toggle a student's attendance (present ↔ absent)."""
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)

        # Get current status
        # Get current status
        cursor.execute(
            "SELECT id, status FROM attendance WHERE session_id = %s AND student_id = %s",
            (session_id, student_id)
        )
        record = cursor.fetchone()

        if record:
            old_status = record["status"]
            new_status = "absent" if old_status == "present" else "present"

            cursor.execute(
                """
                UPDATE attendance
                SET status = %s, method = 'manual_individual',
                    marked_by = %s, marked_at = NOW(),
                    notes = %s
                WHERE id = %s
                """,
                (
                    new_status, current_user.id,
                    f"Manually changed from {old_status} to {new_status} by {current_user.full_name}",
                    record["id"]
                )
            )
        else:
            # ✅ CREATE attendance record if not exists
            new_status = "present"
            old_status = None
            cursor.execute("SELECT usn FROM students WHERE id = %s", (student_id,))
            stu = cursor.fetchone()
            usn = stu["usn"] if stu else None
            cursor.execute(
                """
                INSERT INTO attendance
                (session_id, student_id, usn, status, method, marked_by, marked_at, notes)
                VALUES (%s, %s, %s, %s, 'manual_individual', %s, NOW(), %s)
                """,
                (
                    session_id,
                    student_id,
                    usn,
                    new_status,
                    current_user.id,
                    f"Manually marked present by {current_user.full_name}"
                )
            )
            record_id = cursor.lastrowid

        conn.commit()
        # Get student info for audit log
        cursor.execute("SELECT usn FROM students WHERE id = %s", (student_id,))
        stu = cursor.fetchone()
        usn = stu["usn"] if stu else str(student_id)
        
        attendance_id = record["id"] if record else record_id

        log_audit(
            current_user.id, "toggle_attendance",
            "attendance", attendance_id, old_status, new_status,
            reason=f"Session {session_id}, Student {usn}",
            ip_address=get_client_ip()
        )

        flash(f"Student {usn} marked as {new_status}.", "success")

    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")

    # Handle AJAX vs normal request
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": True, "new_status": new_status, "student_id": student_id})

    return redirect(url_for("session_view.view_session", session_id=session_id))


# ============================================
# MARK ALL PRESENT
# ============================================

@session_view_bp.route("/<int:session_id>/mark-all-present", methods=["POST"])
@admin_or_faculty_required
@profile_completed_required
def mark_all_present(session_id):
    """Mark all students in this session as present."""
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE attendance
            SET status = 'present', method = 'manual_bulk_present',
                marked_by = %s, marked_at = NOW(),
                notes = %s
            WHERE session_id = %s AND status = 'absent'
            """,
            (
                current_user.id,
                f"Bulk marked present by {current_user.full_name}",
                session_id
            )
        )
        affected = cursor.rowcount
        conn.commit()

        log_audit(
              current_user.id, "mark_all_present",
            "attendance", session_id, None,
            f"{affected} students marked present",
            ip_address=get_client_ip()
        )

        flash(f"{affected} students marked as present.", "success")

    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("session_view.view_session", session_id=session_id))


# ============================================
# MARK ALL ABSENT
# ============================================

@session_view_bp.route("/<int:session_id>/mark-all-absent", methods=["POST"])
@admin_or_faculty_required
@profile_completed_required
def mark_all_absent(session_id):
    """Mark all students in this session as absent."""
    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE attendance
            SET status = 'absent', method = 'manual_bulk_absent',
                marked_by = %s, marked_at = NOW(),
                notes = %s
            WHERE session_id = %s AND status = 'present'
            """,
            (
                current_user.id,
                f"Bulk marked absent by {current_user.full_name}",
                session_id
            )
        )
        affected = cursor.rowcount
        conn.commit()

        log_audit(
              current_user.id, "mark_all_absent",
            "attendance", session_id, None,
            f"{affected} students marked absent",
            ip_address=get_client_ip()
        )

        flash(f"{affected} students marked as absent.", "success")

    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("session_view.view_session", session_id=session_id))


# ============================================
# DISMISS CLASS
# ============================================

@session_view_bp.route("/<int:session_id>/dismiss", methods=["POST"])
@admin_or_faculty_required
@profile_completed_required
def dismiss_class(session_id):
    """Faculty dismisses the class."""
    reason = request.form.get("reason", "").strip()

    try:
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            """
            UPDATE sessions
            SET status = 'dismissed',
                dismiss_reason = %s,
                closed_at = NOW()
            WHERE id = %s AND status IN ('scheduled', 'active')
            """,
            (reason if reason else f"Dismissed by {current_user.full_name}", session_id)
        )
        conn.commit()

        # Stop recognition stream
        try:
            from app import stream_manager
            if stream_manager:
                stream_manager.stop_stream(session_id)
        except Exception:
            pass

        log_audit(
              current_user.id, "dismiss_class",
            "sessions", session_id, None, reason,
            ip_address=get_client_ip()
        )

        flash("Class dismissed.", "info")

    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("faculty.dashboard"))


# ============================================
# START/STOP RECOGNITION (Faculty)
# ============================================

@session_view_bp.route("/<int:session_id>/start-recognition", methods=["POST"])
@admin_or_faculty_required
@profile_completed_required
def faculty_start_recognition(session_id):
    """Faculty starts recognition for their session."""
    rtsp_url  = request.form.get("rtsp_url", "").strip()
    camera_id = request.form.get("camera_id", "").strip()

    cam = None

    # Ensure attendance rows exist
    cursor = get_cursor()
    cursor.execute("SELECT section_id FROM sessions WHERE id = %s", (session_id,))
    sess = cursor.fetchone()

    if not sess:
        flash("Session not found.", "danger")
        return redirect(url_for("faculty.dashboard"))
    cursor.execute(
        """
        INSERT IGNORE INTO attendance (session_id, student_id, usn, status, method, marked_at)
        SELECT %s, st.id, st.usn, 'absent', 'system', NOW()
        FROM students st
        WHERE st.section_id = %s
        """,
        (session_id, sess["section_id"])
    )   

    try:
        import app as _app

        # Force-initialize if not done yet
        if not _app.recognition_engine or not _app.recognition_engine.is_initialized:
            _app.init_recognition()

        recognition_engine = _app.recognition_engine
        stream_manager     = _app.stream_manager

        if not recognition_engine or not recognition_engine.is_initialized:
            flash("Recognition engine failed to initialize.", "danger")
            return redirect(url_for("session_view.view_session", session_id=session_id))

        # Load embeddings using the connection pool directly
        from core.db import connection_pool
        recognition_engine.reload_embedding_index(connection_pool)
        recognition_engine.refresh_student_cache(connection_pool)

        if not stream_manager:
            flash("Stream manager not available.", "danger")
            return redirect(url_for("session_view.view_session", session_id=session_id))

        # Get RTSP URL from camera if not provided directly
        if not rtsp_url and camera_id:
            cursor = get_cursor()
            cursor.execute(
                "SELECT rtsp_url, assigned_section_id FROM cameras WHERE id = %s",
                (int(camera_id),)
            )
            cam = cursor.fetchone()
            if not cam:
                flash("Invalid camera selected.", "danger")
                return redirect(url_for("session_view.view_session", session_id=session_id))

            if not rtsp_url:
                rtsp_url = cam.get("rtsp_url")

        if not rtsp_url:
            flash("Please enter an RTSP URL or select a camera.", "danger")
            return redirect(url_for("session_view.view_session", session_id=session_id))

        # Get section_id for this session
        cursor = get_cursor()
        cursor.execute("SELECT section_id FROM sessions WHERE id = %s", (session_id,))
        sess = cursor.fetchone()
        if not sess:
            flash("Session not found.", "danger")
            return redirect(url_for("faculty.dashboard"))
        if camera_id and cam and cam.get("assigned_section_id") and cam["assigned_section_id"] != sess["section_id"]:
            flash("Selected camera is assigned to a different section.", "danger")
            return redirect(url_for("session_view.view_session", session_id=session_id))

        # Mark session as active
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE sessions
            SET rtsp_url = %s, camera_id = %s,
                status = 'active', opened_by = %s, opened_at = NOW()
            WHERE id = %s
            """,
            (
                rtsp_url,
                int(camera_id) if camera_id else None,
                current_user.id,
                session_id
            )
        )
        conn.commit()

        # Start the recognition stream
        success, message = stream_manager.start_stream(
            session_id, rtsp_url, sess["section_id"], connection_pool
        )

        if success:
            flash("Recognition started successfully.", "success")
        else:
            flash(f"Stream failed to start: {message}", "danger")

    except Exception as e:
        flash(f"Error: {e}", "danger")
        logger.error(f"faculty_start_recognition error: {e}")

    return redirect(url_for("session_view.view_session", session_id=session_id))


@session_view_bp.route("/<int:session_id>/stop-recognition", methods=["POST"])
@admin_or_faculty_required
@profile_completed_required
def faculty_stop_recognition(session_id):
    """Faculty stops recognition for their session."""
    try:
        from app import stream_manager
        if stream_manager:
            stream_manager.stop_stream(session_id)
            flash("Recognition stopped.", "info")
        else:
            flash("Stream manager not available.", "warning")
    except Exception as e:
        flash(f"Error: {e}", "danger")
    return redirect(url_for("session_view.view_session", session_id=session_id))


# ============================================
# ATTENDANCE DATA API (for AJAX refresh)
# ============================================

@session_view_bp.route("/<int:session_id>/api/attendance")
@admin_or_faculty_required
@profile_completed_required
def attendance_data_api(session_id):
    """AJAX: Get live attendance data for a session."""
    cursor = get_cursor()

    cursor.execute(
        """
        SELECT a.status, a.method, a.recognition_score,
               a.liveness_score, a.marked_at,
               s.id AS student_id, s.usn, u.full_name
        FROM attendance a
        JOIN students s ON s.id = a.student_id
        JOIN users u ON u.id = s.user_id
        WHERE a.session_id = %s
        ORDER BY s.usn
        """,
        (session_id,)
    )
    records = cursor.fetchall()

    # Convert datetime
    for r in records:
        if r.get("marked_at"):
            r["marked_at"] = r["marked_at"].strftime("%H:%M:%S")

    present = sum(1 for r in records if r["status"] == "present")
    absent = sum(1 for r in records if r["status"] == "absent")

    # Get stream status
    stream_status = {}
    try:
        from app import stream_manager
        if stream_manager:
            stream_status = stream_manager.get_stream_status(session_id)
    except Exception:
        pass

    return jsonify({
        "attendance": records,
        "present": present,
        "absent": absent,
        "total": len(records),
        "stream": stream_status,
    })


# ============================================
# ADD NOTE TO ATTENDANCE
# ============================================

@session_view_bp.route("/<int:session_id>/note/<int:student_id>", methods=["POST"])
@admin_or_faculty_required
@profile_completed_required
def add_attendance_note(session_id, student_id):
    """Add a note to a student's attendance record."""
    note = request.form.get("note", "").strip()

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE attendance
            SET notes = %s
            WHERE session_id = %s AND student_id = %s
            """,
            (note, session_id, student_id)
        )
        conn.commit()
        flash("Note added.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": True})

    return redirect(url_for("session_view.view_session", session_id=session_id))

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "../../static/uploads/attendance_sheets")
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
CLASSROOM_UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "../../static/uploads/classroom_photos")

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# ============================================
# ROUTE 1: Upload sheet → show preview
# ============================================

@session_view_bp.route("/<int:session_id>/upload-sheet", methods=["POST"])
@admin_or_faculty_required
@profile_completed_required
def upload_attendance_sheet(session_id):
    """Receive image upload, run OCR, show confirmation preview."""
    if "sheet_image" not in request.files:
        flash("No file uploaded.", "danger")
        return redirect(url_for("session_view.view_session", session_id=session_id))

    file = request.files["sheet_image"]
    if not file or not allowed_file(file.filename):
        flash("Invalid file. Upload a JPG, PNG, or WEBP image.", "danger")
        return redirect(url_for("session_view.view_session", session_id=session_id))

    # Save file
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    filename = f"{session_id}_{uuid.uuid4().hex[:8]}_{secure_filename(file.filename)}"
    save_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(save_path)

    # Get section_id for this session
    cursor = get_cursor()
    cursor.execute("SELECT section_id FROM sessions WHERE id = %s", (session_id,))
    sess = cursor.fetchone()
    if not sess:
        flash("Session not found.", "danger")
        return redirect(url_for("session_view.view_session", session_id=session_id))

    # Run OCR + matching
    try:
        identifiers = extract_identifiers_from_image(save_path)
        result = match_students(identifiers, sess["section_id"])
    except Exception as e:
        logger.error("OCR error: %s", e)
        flash(f"OCR failed: {e}", "danger")
        return redirect(url_for("session_view.view_session", session_id=session_id))

    return render_template(
        "faculty/sheet_upload_preview.html",
        session_id=session_id,
        matched=result["matched"],
        unmatched=result["unmatched"],
        image_filename=filename,
    )


# ============================================
# ROUTE 2: Confirm → write to attendance table
# ============================================

@session_view_bp.route("/<int:session_id>/confirm-sheet-upload", methods=["POST"])
@admin_or_faculty_required
@profile_completed_required
def confirm_sheet_attendance(session_id):
    """Commit confirmed student IDs as present."""
    student_ids = request.form.getlist("student_ids")  # checkboxes
    if not student_ids:
        flash("No students selected.", "warning")
        return redirect(url_for("session_view.view_session", session_id=session_id))

    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)

        for sid in student_ids:
            cursor.execute("SELECT usn FROM students WHERE id = %s", (int(sid),))
            stu = cursor.fetchone()
            if not stu:
                continue
            usn = stu["usn"]

            cursor.execute(
                """
                INSERT INTO attendance (session_id, student_id, usn, status, method, marked_by, marked_at, notes)
                VALUES (%s, %s, %s, 'present', 'manual_individual', %s, NOW(), 'Marked via sheet image upload')
                ON DUPLICATE KEY UPDATE
                    status = 'present',
                    method = 'manual_individual',
                    marked_by = %s,
                    marked_at = NOW(),
                    notes = 'Updated via sheet image upload'
                """,
                (session_id, int(sid), usn, current_user.id, current_user.id),
            )

        conn.commit()
        log_audit(
            current_user.id, "sheet_upload_attendance",
            "session", session_id, None, f"{len(student_ids)} students marked present",
            reason="Manual sheet image upload",
            ip_address=get_client_ip(),
        )
        flash(f"✅ {len(student_ids)} students marked present from sheet.", "success")

    except Exception as e:
        conn.rollback()
        flash(f"Error saving attendance: {e}", "danger")

    return redirect(url_for("session_view.view_session", session_id=session_id))

# ============================================
# ROUTE 1: Upload classroom photo(s) -> show preview
# ============================================

@session_view_bp.route("/<int:session_id>/upload-classroom-photo", methods=["POST"])
@admin_or_faculty_required
@profile_completed_required
def upload_classroom_photo(session_id):
    """Receive 1-3 classroom photos, run face recognition, show confirmation preview."""
    files = request.files.getlist("classroom_photos")
    files = [f for f in files if f and f.filename]

    if not files:
        flash("No photo uploaded.", "danger")
        return redirect(url_for("session_view.view_session", session_id=session_id))

    if len(files) > 3:
        flash("Upload at most 3 photos per class.", "danger")
        return redirect(url_for("session_view.view_session", session_id=session_id))

    for f in files:
        if not allowed_file(f.filename):
            flash("Invalid file type. Upload JPG, PNG, or WEBP images only.", "danger")
            return redirect(url_for("session_view.view_session", session_id=session_id))

    cursor = get_cursor()
    cursor.execute("SELECT section_id FROM sessions WHERE id = %s", (session_id,))
    sess = cursor.fetchone()
    if not sess:
        flash("Session not found.", "danger")
        return redirect(url_for("session_view.view_session", session_id=session_id))

    # Save all photos
    os.makedirs(CLASSROOM_UPLOAD_FOLDER, exist_ok=True)
    saved_filenames = []
    saved_paths = []
    for f in files:
        filename = f"{session_id}_{uuid.uuid4().hex[:8]}_{secure_filename(f.filename)}"
        save_path = os.path.join(CLASSROOM_UPLOAD_FOLDER, filename)
        f.save(save_path)
        saved_filenames.append(filename)
        saved_paths.append(save_path)

    # Ensure recognition engine is ready + embeddings/cache are fresh
    try:
        import app as _app
        if not _app.recognition_engine or not _app.recognition_engine.is_initialized:
            _app.init_recognition()

        recognition_engine = _app.recognition_engine
        if not recognition_engine or not recognition_engine.is_initialized:
            flash("Recognition engine failed to initialize.", "danger")
            return redirect(url_for("session_view.view_session", session_id=session_id))

        from core.db import connection_pool
        recognition_engine.reload_embedding_index(connection_pool)
        recognition_engine.refresh_student_cache(connection_pool)

        result = recognize_classroom_photos(
            saved_paths, recognition_engine, sess["section_id"]
        )
    except Exception as e:
        logger.error("Classroom photo recognition error: %s", e)
        flash(f"Recognition failed: {e}", "danger")
        return redirect(url_for("session_view.view_session", session_id=session_id))

    return render_template(
        "faculty/classroom_photo_preview.html",
        session_id=session_id,
        matched=result["matched"],
        unrecognized_count=len(result["unrecognized_faces"]),
        total_faces_detected=result["total_faces_detected"],
        photos_processed=result["photos_processed"],
        image_filenames=saved_filenames,
    )

# ============================================
# ROUTE 2: Confirm -> write to attendance table
# ============================================

@session_view_bp.route("/<int:session_id>/confirm-classroom-photo", methods=["POST"])
@admin_or_faculty_required
@profile_completed_required
def confirm_classroom_photo_attendance(session_id):
    """Commit confirmed student IDs (from classroom photo recognition) as present."""
    student_ids = request.form.getlist("student_ids")  # checkboxes
    if not student_ids:
        flash("No students selected.", "warning")
        return redirect(url_for("session_view.view_session", session_id=session_id))

    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)

        for sid in student_ids:
            cursor.execute("SELECT usn FROM students WHERE id = %s", (int(sid),))
            stu = cursor.fetchone()
            if not stu:
                continue
            usn = stu["usn"]

            cursor.execute(
                """
                INSERT INTO attendance (session_id, student_id, usn, status, method, marked_by, marked_at, notes)
                VALUES (%s, %s, %s, 'present', 'classroom_photo', %s, NOW(), 'Marked via classroom photo recognition')
                ON DUPLICATE KEY UPDATE
                    status = 'present',
                    method = 'classroom_photo',
                    marked_by = %s,
                    marked_at = NOW(),
                    notes = 'Updated via classroom photo recognition'
                """,
                (session_id, int(sid), usn, current_user.id, current_user.id),
            )

        conn.commit()
        log_audit(
            current_user.id, "classroom_photo_attendance",
            "session", session_id, None, f"{len(student_ids)} students marked present",
            reason="Classroom photo recognition",
            ip_address=get_client_ip(),
        )
        flash(f"✅ {len(student_ids)} students marked present from classroom photo(s).", "success")

    except Exception as e:
        conn.rollback()
        flash(f"Error saving attendance: {e}", "danger")

    return redirect(url_for("session_view.view_session", session_id=session_id))