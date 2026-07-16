"""
attendance_system/api/student/dashboard.py
Student dashboard: subject-wise attendance table, color-coded percentages,
session-by-session history, alerts.
"""

import logging
from datetime import datetime

from flask_login import login_required
from flask import Blueprint, render_template, request, jsonify
from flask_login import current_user

from core.db import get_db, get_cursor
from auth.helpers import student_required

logger = logging.getLogger(__name__)

student_bp = Blueprint(
    "student", __name__,
    template_folder="../../templates/student"
)


@student_bp.route("/")
@login_required
@student_required
def dashboard():
    """Student dashboard — attendance overview."""
    cursor = get_cursor()

    # Get student record
    cursor.execute(
        """
        SELECT s.id AS student_id, s.usn, s.section_id, s.current_sem,
               s.enrollment_status, s.image_path,
               sec.section_label, d.code AS dept_code, d.name AS dept_name,
               ap.name AS period_name, ap.start_date, ap.end_date
        FROM students s
        LEFT JOIN sections sec ON sec.id = s.section_id
        LEFT JOIN departments d ON d.id = sec.department_id
        LEFT JOIN academic_periods ap ON ap.id = sec.academic_period_id
        WHERE s.user_id = %s
        """,
        (current_user.id,)
    )
    student = cursor.fetchone()

    if not student:
        return render_template(
            "student/dashboard.html",
            error="Student profile not found. Contact administrator.",
            student=None,
            subjects=[],
            alerts=[],
        )

    student_id = student["student_id"]
    section_id = student["section_id"]

    # ---- Subject-wise attendance summary ----
    subjects = []
    if section_id:
        cursor.execute(
            """
            SELECT ss.id, sub.id AS subject_id,
                   sub.code AS subject_code, sub.name AS subject_name,
                   sub.subject_type, sub.credits,
                   u.full_name AS faculty_name
            FROM section_subjects ss
            JOIN subjects sub ON sub.id = ss.subject_id
            JOIN faculty f ON f.id = ss.faculty_id
            JOIN users u ON u.id = f.user_id
            JOIN academic_periods ap ON ap.id = ss.academic_period_id
            WHERE ss.section_id = %s AND ap.is_active = 1
            ORDER BY sub.code
            """,
            (section_id,)
        )
        section_subjects = cursor.fetchall()

        for subj in section_subjects:
            cursor.execute(
                """
                SELECT
                    COUNT(*) AS total_sessions,
                    SUM(a.status = 'present') AS present,
                    SUM(a.status = 'absent') AS absent
                FROM attendance a
                JOIN sessions s ON s.id = a.session_id
                WHERE a.student_id = %s
                  AND s.subject_id = %s
                  AND s.section_id = %s
                  AND s.status IN ('completed', 'active')
                """,
                (student_id, subj["subject_id"], section_id)
            )
            stats = cursor.fetchone()

            total = stats["total_sessions"] or 0
            present = stats["present"] or 0
            absent = stats["absent"] or 0
            pct = round((present / total * 100), 1) if total > 0 else 0.0

            # Color coding
            if pct < 75:
                color = "danger"    # Red
            elif pct < 85:
                color = "warning"   # Orange
            else:
                color = "success"   # Green

            subjects.append({
                "subject_id": subj["subject_id"],
                "subject_code": subj["subject_code"],
                "subject_name": subj["subject_name"],
                "subject_type": subj["subject_type"],
                "credits": subj["credits"],
                "faculty_name": subj["faculty_name"],
                "total": total,
                "present": present,
                "absent": absent,
                "percentage": pct,
                "color": color,
            })


    # ---- Elective / AEC / Open Elective Subjects ----

    cursor.execute("""
    SELECT
        eg.id AS group_ref,
        sub.id AS subject_id,
        sub.code AS subject_code,
        sub.name AS subject_name,
        sub.subject_type,
        sub.credits,
        u.full_name AS faculty_name
    FROM elective_group_members egm
    JOIN elective_groups eg
        ON eg.id = egm.elective_group_id
    JOIN subjects sub
        ON sub.id = eg.subject_id
    LEFT JOIN faculty f
        ON f.id = eg.faculty_id
    LEFT JOIN users u
        ON u.id = f.user_id
    WHERE
        egm.student_id = %s
        AND egm.is_active = 1
        AND eg.academic_period_id = (
            SELECT id
            FROM academic_periods
            WHERE is_active = 1
            LIMIT 1
        )
    """, (student_id,))

    elective_subjects = cursor.fetchall()

    for subj in elective_subjects:

        cursor.execute("""
            SELECT
                COUNT(*) AS total_sessions,
                SUM(a.status='present') AS present,
                SUM(a.status='absent') AS absent
            FROM attendance a
            JOIN sessions s
                ON s.id = a.session_id
            WHERE
                a.student_id=%s
                AND s.subject_id=%s
                AND s.elective_group_id=%s
                AND s.status IN ('completed','active')
        """, (
            student_id,
            subj["subject_id"],
            subj["group_ref"],
        ))

        stats = cursor.fetchone()

        total = stats["total_sessions"] or 0
        present = stats["present"] or 0
        absent = stats["absent"] or 0

        pct = round((present / total * 100), 1) if total else 0.0

        if pct < 75:
            color = "danger"
        elif pct < 85:
            color = "warning"
        else:
            color = "success"

        subjects.append({
            "subject_id": subj["subject_id"],
            "subject_code": subj["subject_code"],
            "subject_name": subj["subject_name"],
            "subject_type": subj["subject_type"],
            "credits": subj["credits"],
            "faculty_name": subj["faculty_name"],
            "total": total,
            "present": present,
            "absent": absent,
            "percentage": pct,
            "color": color,
        })
    # ---- Overall attendance ----
    overall_total = sum(s["total"] for s in subjects)
    overall_present = sum(s["present"] for s in subjects)
    overall_pct = round((overall_present / overall_total * 100), 1) if overall_total > 0 else 0.0

    # ---- Alerts ----
    alerts = []
    for subj in subjects:
        if subj["percentage"] < 75:
            deficit = subj["total"] - int(subj["total"] * 0.75)
            needed = max(0, int(subj["total"] * 0.75) - subj["present"])
            alerts.append({
                "type": "danger",
                "message": (
                    f"⚠ {subj['subject_code']}: Your attendance is {subj['percentage']}% "
                    f"(below 75%). You need {needed} more classes to reach 75%."
                ),
            })
        elif subj["percentage"] < 85:
            alerts.append({
                "type": "warning",
                "message": (
                    f"⚡ {subj['subject_code']}: Your attendance is {subj['percentage']}% "
                    f"(below 85%). Be careful!"
                ),
            })

    if overall_pct < 75:
        alerts.insert(0, {
            "type": "danger",
            "message": f"🚨 OVERALL attendance is {overall_pct}%. You are a defaulter!",
        })

    return render_template(
        "student/dashboard.html",
        student=student,
        subjects=subjects,
        overall_total=overall_total,
        overall_present=overall_present,
        overall_pct=overall_pct,
        alerts=alerts,
    )


@student_bp.route("/subject/<int:subject_id>/history")
@login_required
@student_required
def subject_history(subject_id):
    """Session-by-session attendance history for a subject."""
    cursor = get_cursor()

    # Get student
    cursor.execute(
        "SELECT id AS student_id, section_id FROM students WHERE user_id = %s",
        (current_user.id,)
    )
    student = cursor.fetchone()
    if not student:
        return jsonify({"error": "Student not found."})

    # Subject info
    cursor.execute(
        "SELECT code, name FROM subjects WHERE id = %s",
        (subject_id,)
    )
    subject = cursor.fetchone()

    # Session-by-session history
    cursor.execute(
        """
        SELECT s.id AS session_id, s.session_date,
               t.start_time, t.end_time, t.slot_type,
               a.status, a.method, a.recognition_score,
               a.liveness_score, a.marked_at
        FROM sessions s
        JOIN timetable t ON t.section_id = s.section_id AND t.subject_id = s.subject_id
        LEFT JOIN attendance a ON a.session_id = s.id AND a.student_id = %s
        WHERE s.subject_id = %s
          AND s.section_id = %s
          AND s.status IN ('completed', 'active')
        ORDER BY s.session_date DESC, t.start_time DESC
        """,
        (student["student_id"], subject_id, student["section_id"])
    )
    history = cursor.fetchall()

    # Convert timedelta
    for h in history:
        for k in ("start_time", "end_time"):
            if h.get(k) and hasattr(h[k], "total_seconds"):
                total = int(h[k].total_seconds())
                hours, remainder = divmod(total, 3600)
                minutes, _ = divmod(remainder, 60)
                h[k] = f"{hours:02d}:{minutes:02d}"
        if h.get("marked_at"):
            h["marked_at"] = h["marked_at"].strftime("%H:%M:%S")
        if h.get("session_date"):
            h["session_date"] = h["session_date"].strftime("%Y-%m-%d")

    # If AJAX request
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({
            "subject": subject,
            "history": history,
        })

    return render_template(
        "student/subject_history.html",
        subject=subject,
        history=history,
    )


@student_bp.route("/api/attendance-summary")
@login_required
@student_required
def attendance_summary_api():
    """AJAX: Get attendance summary for the logged-in student."""
    cursor = get_cursor()

    cursor.execute(
        "SELECT id AS student_id, section_id FROM students WHERE user_id = %s",
        (current_user.id,)
    )
    student = cursor.fetchone()
    if not student:
        return jsonify({"error": "Student not found."})

    cursor.execute(
        """
        SELECT sub.code AS subject_code, sub.name AS subject_name,
               COUNT(*) AS total,
               SUM(a.status = 'present') AS present,
               ROUND(SUM(a.status = 'present') / COUNT(*) * 100, 1) AS percentage
        FROM attendance a
        JOIN sessions s ON s.id = a.session_id
        JOIN subjects sub ON sub.id = s.subject_id
        WHERE a.student_id = %s
          AND s.section_id = %s
          AND s.status IN ('completed', 'active')
        GROUP BY sub.id
        ORDER BY sub.code
        """,
        (student["student_id"], student["section_id"])
    )
    summary = cursor.fetchall()

    return jsonify({"summary": summary})