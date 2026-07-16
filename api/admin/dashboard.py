"""
attendance_system/api/admin/dashboard.py
Admin dashboard: overview of today's sessions, pending approvals,
active streams, defaulters summary.
"""

import logging
from datetime import datetime

from flask_login import login_required, current_user
from flask import Blueprint, render_template, jsonify, request
from core.db import get_db, get_cursor
import core.db as core_db
from datetime import datetime
from auth.helpers import admin_required

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, template_folder="../../templates/admin")


# ──────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────

def _td_to_str(value) -> str:
    """Convert timedelta / string to HH:MM."""
    if value is None:
        return ""
    if hasattr(value, "total_seconds"):
        total = int(value.total_seconds())
        h, rem = divmod(total, 3600)
        m, _   = divmod(rem, 60)
        return f"{h:02d}:{m:02d}"
    return str(value)[:5]


def _auto_create_todays_sessions(cursor, conn, today: str, day_name: str) -> int:
    """
    Auto-create missing sessions from timetable for today.
    Skips Interval / Lunch slots and slots with no subject.
    Returns the number of sessions created.
    """
    cursor.execute(
        "SELECT id FROM academic_periods WHERE is_active = 1 LIMIT 1"
    )
    period = cursor.fetchone()
    if not period:
        return 0

    period_id     = period["id"]
    created_count = 0

    # All timetable slots for today — real subjects only
    cursor.execute("""
        SELECT t.*
        FROM timetable t
        WHERE t.day_of_week        = %s
          AND t.academic_period_id = %s
          AND t.is_active          = 1
          AND t.subject_id         IS NOT NULL
          AND (t.slot_type IS NULL
               OR t.slot_type NOT IN ('Interval', 'Lunch'))
    """, (day_name, period_id))
    slots = cursor.fetchall()

    for slot in slots:
        # Skip truly empty rows (defensive)
        if not slot.get("subject_id"):
            continue

        # Check if session already exists for this exact slot
        cursor.execute("""
            SELECT id FROM sessions
            WHERE section_id   = %s
              AND subject_id   = %s
              AND session_date = %s
              AND start_time   = %s
        """, (slot["section_id"], slot["subject_id"], today, slot["start_time"]))

        if cursor.fetchone():
            continue

        # Insert the missing session
        cursor.execute("""
            INSERT INTO sessions
                (section_id, subject_id, faculty_id, session_date,
                 start_time, end_time, status)
            VALUES (%s, %s, %s, %s, %s, %s, 'scheduled')
        """, (
            slot["section_id"],
            slot["subject_id"],
            slot["faculty_id"],
            today,
            slot["start_time"],
            slot["end_time"],
        ))
        created_count += 1

    conn.commit()
    return created_count


def _fetch_todays_sessions(cursor, today: str) -> list:
    """
    Fetch all real (non-break) sessions for today with full join info.
    Returns a list of dicts with HH:MM time strings.
    """
    cursor.execute("""
        SELECT
            s.id,
            s.session_date,
            s.status,
            s.start_time,
            s.end_time,
            s.camera_id,
            s.rtsp_url,
            s.section_id,
            sub.code        AS subject_code,
            sub.name        AS subject_name,
            sec.section_label,
            sec.sem_number,
            d.id            AS department_id,
            d.code          AS dept_code,
            d.name          AS dept_name,
            u.full_name     AS faculty_name,
            cam.name        AS camera_name,
            (SELECT COUNT(*)
             FROM attendance a
             WHERE a.session_id = s.id
               AND a.status     = 'present') AS present_count,
            (SELECT COUNT(*)
             FROM students st
             WHERE st.section_id = s.section_id) AS total_count
        FROM sessions s
        JOIN sections sec    ON sec.id  = s.section_id
        JOIN departments d   ON d.id    = sec.department_id
        LEFT JOIN subjects sub ON sub.id = s.subject_id
        LEFT JOIN faculty f    ON f.id   = s.faculty_id
        LEFT JOIN users u      ON u.id   = f.user_id
        LEFT JOIN cameras cam  ON cam.id = s.camera_id
        WHERE s.session_date = %s
          AND s.subject_id   IS NOT NULL
        ORDER BY s.start_time, d.code, sec.section_label
    """, (today,))

    sessions = cursor.fetchall()

    current_time = datetime.now().strftime("%H:%M")

    for s in sessions:

        s["start_time"] = _td_to_str(s.get("start_time"))
        s["end_time"]   = _td_to_str(s.get("end_time"))

        # Dynamic live status
        if s["status"] == "scheduled":

            if s["start_time"] <= current_time <= s["end_time"]:
                s["live_status"] = "active"

            elif current_time > s["end_time"]:
                s["live_status"] = "completed"

            else:
                s["live_status"] = "scheduled"

        else:
            s["live_status"] = s["status"]
            
    return sessions


# ──────────────────────────────────────────────────────────────────
# DASHBOARD ROUTE
# ──────────────────────────────────────────────────────────────────

@admin_bp.route("/")
@login_required
@admin_required
def dashboard():
    """Admin dashboard page."""
    conn   = get_db()
    cursor = conn.cursor(dictionary=True)

    today        = datetime.now().strftime("%Y-%m-%d")
    day_name     = datetime.now().strftime("%A")
    current_time = datetime.now().strftime("%H:%M")

    # ── 1. Auto-create missing sessions ──────────────────────────
    try:
        created = _auto_create_todays_sessions(cursor, conn, today, day_name)
        if created:
            logger.info(f"Auto-created {created} session(s) for {today}")
    except Exception as exc:
        logger.error(f"Auto-create error: {exc}", exc_info=True)

    # ── 2. Fetch all of today's sessions ─────────────────────────
    all_sessions = _fetch_todays_sessions(cursor, today)

    # ── 3. Read filter params from GET ───────────────────────────
    dept_filter    = request.args.get("dept_filter",    "").strip()
    sem_filter     = request.args.get("sem_filter",     "").strip()
    section_filter = request.args.get("section_filter", "").strip()
    status_filter  = request.args.get("status_filter",  "").strip()
    per_page       = min(int(request.args.get("per_page", 25)), 200)

    # ── 4. Apply filters in Python (sessions already in memory) ──
    filtered_sessions = all_sessions

    if dept_filter:
        filtered_sessions = [
            s for s in filtered_sessions
            if s.get("dept_code") == dept_filter
        ]

    if sem_filter:
        filtered_sessions = [
            s for s in filtered_sessions
            if str(s.get("sem_number", "")) == sem_filter
        ]

    if section_filter:
        filtered_sessions = [
            s for s in filtered_sessions
            if str(s.get("section_id", "")) == section_filter
        ]

    if status_filter:
        filtered_sessions = [
            s for s in filtered_sessions
            if s.get("status") == status_filter
        ]

    # Limit rows shown
    displayed_sessions = filtered_sessions[:per_page]

    # ── 5. Build filter-dropdown data from today's sessions ───────
    # Departments present today
    seen_depts = {}
    for s in all_sessions:
        code = s.get("dept_code")
        if code and code not in seen_depts:
            seen_depts[code] = s.get("dept_name", code)
    filter_departments = [
        {"code": k, "name": v} for k, v in sorted(seen_depts.items())
    ]

    # Semesters present today
    filter_semesters = sorted({
        s["sem_number"]
        for s in all_sessions
        if s.get("sem_number") is not None
    })

    # Sections present today
    seen_sections = {}
    for s in all_sessions:
        sid = s.get("section_id")
        if sid and sid not in seen_sections:
            seen_sections[sid] = {
                "id":            sid,
                "section_label": s.get("section_label"),
                "dept_code":     s.get("dept_code"),
            }
    filter_sections = sorted(
        seen_sections.values(),
        key=lambda x: (x["dept_code"], x["section_label"])
    )

    # ── 6. Status counters (from full unfiltered list) ────────────
    def _count(lst, field, val):
        return sum(1 for s in lst if s.get(field) == val)

    total_sessions_today     = len(all_sessions)
    active_sessions_today    = _count(all_sessions, "status", "active")
    completed_sessions_today = _count(all_sessions, "status", "completed")
    upcoming_sessions_today  = _count(all_sessions, "status", "scheduled")
    cancelled_sessions_today = sum(
        1 for s in filtered_sessions
        if s.get("status") in ("cancelled", "dismissed")
    )

    # ── 7. Global stats ───────────────────────────────────────────

    # Pending approvals
    cursor.execute(
        "SELECT COUNT(*) AS cnt FROM approval_queue WHERE reviewed_at IS NULL"
    )
    pending_approvals = cursor.fetchone()["cnt"]

    # Active streams
    active_streams    = 0
    active_stream_ids = set()
    try:
        from app import stream_manager
        if stream_manager:
            streams           = stream_manager.list_active_streams()
            active_streams    = len(streams)
            active_stream_ids = set(streams.keys())
    except Exception:
        pass

    # Enrolled students
    cursor.execute(
        "SELECT COUNT(*) AS cnt FROM students "
        "WHERE enrollment_status = 'fully_enrolled'"
    )
    enrolled_students = cursor.fetchone()["cnt"]

    # Pending face enrollment
    cursor.execute(
        "SELECT COUNT(*) AS cnt FROM students "
        "WHERE enrollment_status = 'approved_face_pending'"
    )
    pending_face = cursor.fetchone()["cnt"]

    # Active sections
    cursor.execute("""
        SELECT COUNT(*) AS cnt
        FROM sections sec
        JOIN academic_periods ap ON ap.id = sec.academic_period_id
        WHERE ap.is_active = 1
    """)
    active_sections = cursor.fetchone()["cnt"]

    # College name
    cursor.execute(
        "SELECT setting_value FROM settings WHERE setting_key = 'college_name'"
    )
    row          = cursor.fetchone()
    college_name = row["setting_value"] if row else "Attendance System"

    # Defaulters (< 75 % attendance in active period)
    cursor.execute("""
        SELECT COUNT(*) AS cnt FROM (
            SELECT a.student_id
            FROM attendance a
            JOIN sessions s ON s.id = a.session_id
            JOIN academic_periods ap ON ap.is_active = 1
            WHERE s.session_date BETWEEN ap.start_date AND ap.end_date
            GROUP BY a.student_id
            HAVING (
                SUM(CASE WHEN a.status = 'present' THEN 1 ELSE 0 END)
                / COUNT(*) * 100
            ) < 75
        ) AS defaulters
    """)
    defaulter_count = cursor.fetchone()["cnt"]

    # Cameras available for assignment
    cursor.execute("""
        SELECT id, name, rtsp_url, location
        FROM cameras
        WHERE status = 'active'
        ORDER BY name
    """)
    cameras = cursor.fetchall()

    # ── 8. Render ─────────────────────────────────────────────────
    return render_template(
        "admin/dashboard.html",
        # identity
        college_name=college_name,
        today=today,
        day_name=day_name,
        current_time=current_time,
        # sessions
        todays_sessions=displayed_sessions,      # filtered + limited
        all_sessions_count=len(all_sessions),    # unfiltered total
        # status counters (always from full list)
        # status counters
        total_sessions_today=len(filtered_sessions),

        active_sessions_today=_count(
            filtered_sessions,
            "status",
            "active"
        ),

        completed_sessions_today=_count(
            filtered_sessions,
            "status",
            "completed"
        ),

        upcoming_sessions_today=_count(
            filtered_sessions,
            "status",
            "scheduled"
        ),
        cancelled_sessions_today=sum(
            1 for s in filtered_sessions
            if s.get("status") in ("cancelled", "dismissed")
        ),
        # streams
        active_streams=active_streams,
        active_stream_ids=active_stream_ids,
        # students
        enrolled_students=enrolled_students,
        pending_face=pending_face,
        # misc stats
        active_sections=active_sections,
        defaulter_count=defaulter_count,
        pending_approvals=pending_approvals,
        # cameras
        cameras=cameras,
        # filter state (so template can re-select dropdowns)
        filter_departments=filter_departments,
        filter_semesters=filter_semesters,
        filter_sections=filter_sections,
        dept_filter=dept_filter,
        sem_filter=sem_filter,
        section_filter=section_filter,
        status_filter=status_filter,
        per_page=per_page,
    )


# ──────────────────────────────────────────────────────────────────
# AJAX  — live stats
# ──────────────────────────────────────────────────────────────────

@admin_bp.route("/api/dashboard-stats")
@login_required
@admin_required
def dashboard_stats_api():
    """AJAX endpoint — live dashboard stats (polled by JS)."""
    conn   = get_db()
    cursor = conn.cursor(dictionary=True)
    today  = datetime.now().strftime("%Y-%m-%d")

    cursor.execute("""
        SELECT
            COUNT(DISTINCT s.id) AS total_sessions,
            SUM(CASE WHEN s.status = 'active'    THEN 1 ELSE 0 END) AS active_sessions,
            SUM(CASE WHEN s.status = 'completed' THEN 1 ELSE 0 END) AS completed_sessions
        FROM sessions s
        WHERE s.session_date = %s
          AND s.subject_id   IS NOT NULL
    """, (today,))
    session_stats = cursor.fetchone()

    cursor.execute("""
        SELECT COUNT(DISTINCT a.student_id) AS cnt
        FROM attendance a
        JOIN sessions s ON s.id = a.session_id
        WHERE s.session_date = %s
          AND a.status = 'present'
    """, (today,))
    present_today = cursor.fetchone()["cnt"]

    cursor.execute("""
        SELECT COUNT(*) AS cnt
        FROM spoof_attempts
        WHERE DATE(detected_at) = %s
    """, (today,))
    spoofs_today = cursor.fetchone()["cnt"]

    active_streams = {}
    try:
        from app import stream_manager
        if stream_manager:
            active_streams = stream_manager.list_active_streams()
    except Exception:
        pass

    return jsonify({
        "session_stats": session_stats,
        "present_today": present_today,
        "spoofs_today":  spoofs_today,
        "active_streams": active_streams,
    })


# ──────────────────────────────────────────────────────────────────
# Init recognition models
# ──────────────────────────────────────────────────────────────────

@admin_bp.route("/init-models", methods=["POST"])
@login_required
@admin_required
def init_models():
    """Initialize recognition engine and reload embeddings."""
    try:
        from app import init_recognition, recognition_engine
        init_recognition()

        if recognition_engine:
            count = recognition_engine.reload_embedding_index(
                core_db.connection_pool
            )
            recognition_engine.refresh_student_cache(core_db.connection_pool)
            return jsonify({
                "success":    True,
                "message":    (
                    f"Models initialized on {recognition_engine.device}. "
                    f"{count} embeddings loaded."
                ),
                "device":     recognition_engine.device,
                "embeddings": count,
            })

        return jsonify({"success": False, "message": "Engine not initialized."})

    except Exception as exc:
        logger.error(f"init_models error: {exc}", exc_info=True)
        return jsonify({"success": False, "message": str(exc)})