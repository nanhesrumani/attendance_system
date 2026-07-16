# attendance_system/api/faculty/reports.py
"""
Faculty reports: attendance for my subjects, defaulters in my subjects,
PDF/Excel download.
"""

import io
import logging
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, send_file
)
from flask_login import current_user

from core.db import get_db, get_cursor
from auth.helpers import admin_or_faculty_required, profile_completed_required

logger = logging.getLogger(__name__)

faculty_reports_bp = Blueprint(
    "faculty_reports", __name__,
    template_folder="../../templates/faculty"
)


@faculty_reports_bp.route("/")
@admin_or_faculty_required
@profile_completed_required
def reports_home():
    """Faculty reports home — shows subject-wise attendance summaries."""
    cursor = get_cursor()

    cursor.execute(
        "SELECT id FROM faculty WHERE user_id = %s",
        (current_user.id,)
    )
    fac = cursor.fetchone()
    faculty_id = fac["id"] if fac else None

    if not faculty_id and current_user.role != "admin":
        flash("Faculty profile not found.", "danger")
        return redirect(url_for("faculty.dashboard"))

    query = """
        SELECT ss.id AS ss_id, ss.section_id,
               sub.id AS subject_id, sub.code AS subject_code, sub.name AS subject_name,
               sec.section_label, d.code AS dept_code,
               ap.name AS period_name
        FROM section_subjects ss
        JOIN subjects sub ON sub.id = ss.subject_id
        JOIN sections sec ON sec.id = ss.section_id
        JOIN departments d ON d.id = sec.department_id
        JOIN academic_periods ap ON ap.id = ss.academic_period_id
        WHERE ap.is_active = 1
    """
    params = []

    if faculty_id:
        query += " AND ss.faculty_id = %s"
        params.append(faculty_id)

    query += " ORDER BY sub.code"
    cursor.execute(query, tuple(params))
    my_subjects = cursor.fetchall()

    for subj in my_subjects:
        cursor.execute(
            """
            SELECT COUNT(DISTINCT s.id) AS total_sessions
            FROM sessions s
            WHERE s.subject_id = %s
              AND s.section_id = %s
              AND s.status IN ('completed', 'active')
            """,
            (subj["subject_id"], subj["section_id"])
        )
        sess_stats = cursor.fetchone()
        subj["total_sessions"] = sess_stats["total_sessions"] or 0

        cursor.execute(
            """
            SELECT
                COUNT(*) AS total_records,
                SUM(a.status = 'present') AS total_present
            FROM attendance a
            JOIN sessions s ON s.id = a.session_id
            WHERE s.subject_id = %s
              AND s.section_id = %s
              AND s.status IN ('completed', 'active')
            """,
            (subj["subject_id"], subj["section_id"])
        )
        att_stats = cursor.fetchone()
        total = att_stats["total_records"] or 0
        present = att_stats["total_present"] or 0
        subj["avg_percentage"] = round((present / total * 100), 1) if total > 0 else 0.0

    return render_template(
        "faculty/reports.html",
        my_subjects=my_subjects,
        faculty_id=faculty_id,
    )


@faculty_reports_bp.route("/subject/<int:subject_id>/section/<int:section_id>")
@admin_or_faculty_required
@profile_completed_required
def subject_report(subject_id, section_id):
    """Detailed attendance report for a specific subject in a section."""
    cursor = get_cursor()

    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")

    # Subject info — note: ap linked via sections.academic_period_id
    cursor.execute(
        """
        SELECT sub.code AS subject_code, sub.name AS subject_name,
               sec.section_label, d.code AS dept_code,
               ap.start_date, ap.end_date
        FROM subjects sub
        JOIN sections sec ON sec.id = %s
        JOIN departments d ON d.id = sec.department_id
        JOIN academic_periods ap ON ap.id = sec.academic_period_id
        WHERE sub.id = %s
        """,
        (section_id, subject_id)
    )
    info = cursor.fetchone()
    if not info:
        flash("Subject/section not found.", "danger")
        return redirect(url_for("faculty_reports.reports_home"))

    if not date_from:
        date_from = str(info["start_date"])
    if not date_to:
        date_to = datetime.now().strftime("%Y-%m-%d")

    # Students in section
    cursor.execute(
        """
        SELECT s.id, s.usn, u.full_name
        FROM students s
        JOIN users u ON u.id = s.user_id
        WHERE s.section_id = %s
        ORDER BY s.usn
        """,
        (section_id,)
    )
    students = cursor.fetchall()

    # Sessions for this subject — NO timetable join needed, times are on sessions directly
    cursor.execute(
        """
        SELECT s.id, s.session_date, s.start_time, s.end_time
        FROM sessions s
        WHERE s.subject_id = %s
          AND s.section_id = %s
          AND s.session_date BETWEEN %s AND %s
          AND s.status IN ('completed', 'active')
        ORDER BY s.session_date, s.start_time
        """,
        (subject_id, section_id, date_from, date_to)
    )
    sessions = cursor.fetchall()

    # Convert timedelta in sessions
    for s in sessions:
        for k in ("start_time", "end_time"):
            val = s.get(k)
            if val and hasattr(val, "total_seconds"):
                total = int(val.total_seconds())
                hours, remainder = divmod(total, 3600)
                minutes, _ = divmod(remainder, 60)
                s[k] = f"{hours:02d}:{minutes:02d}"
            elif val is None:
                s[k] = ""

    # Build student report data
    report_data = []
    for stu in students:
        cursor.execute(
            """
            SELECT a.session_id, a.status
            FROM attendance a
            JOIN sessions s ON s.id = a.session_id
            WHERE a.student_id = %s
              AND s.subject_id = %s
              AND s.section_id = %s
              AND s.session_date BETWEEN %s AND %s
              AND s.status IN ('completed', 'active')
            """,
            (stu["id"], subject_id, section_id, date_from, date_to)
        )
        session_attendance = {row["session_id"]: row["status"] for row in cursor.fetchall()}

        total = len(sessions)
        present = sum(1 for s in sessions if session_attendance.get(s["id"]) == "present")
        absent = total - present
        pct = round((present / total * 100), 1) if total > 0 else 0.0

        report_data.append({
            "student_id": stu["id"],
            "usn": stu["usn"],
            "name": stu["full_name"],
            "total": total,
            "present": present,
            "absent": absent,
            "percentage": pct,
            "session_data": session_attendance,
        })

    report_data.sort(key=lambda x: x["percentage"])

    return render_template(
        "faculty/subject_report.html",
        info=info,
        report_data=report_data,
        sessions=sessions,
        date_from=date_from,
        date_to=date_to,
        subject_id=subject_id,
        section_id=section_id,
    )


@faculty_reports_bp.route("/download/subject/<int:subject_id>/section/<int:section_id>")
@admin_or_faculty_required
@profile_completed_required
def download_subject_excel(subject_id, section_id):
    """Download subject attendance as Excel."""
    try:
        import openpyxl

        cursor = get_cursor()
        date_from = request.args.get("from", "2000-01-01")
        date_to = request.args.get("to", datetime.now().strftime("%Y-%m-%d"))

        cursor.execute(
            """
            SELECT sub.code, sub.name AS subject_name,
                   sec.section_label, d.code AS dept_code
            FROM subjects sub
            JOIN sections sec ON sec.id = %s
            JOIN departments d ON d.id = sec.department_id
            WHERE sub.id = %s
            """,
            (section_id, subject_id)
        )
        info = cursor.fetchone()

        cursor.execute(
            """
            SELECT s.id, s.usn, u.full_name
            FROM students s
            JOIN users u ON u.id = s.user_id
            WHERE s.section_id = %s ORDER BY s.usn
            """,
            (section_id,)
        )
        students = cursor.fetchall()

        cursor.execute(
            """
            SELECT s.id, s.session_date
            FROM sessions s
            WHERE s.subject_id = %s AND s.section_id = %s
              AND s.session_date BETWEEN %s AND %s
              AND s.status IN ('completed', 'active')
            ORDER BY s.session_date
            """,
            (subject_id, section_id, date_from, date_to)
        )
        sessions = cursor.fetchall()

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Attendance"

        ws.append([
            f"{info['dept_code']} - Section {info['section_label']} - "
            f"{info['code']}: {info['subject_name']}"
        ])
        ws.append([f"Period: {date_from} to {date_to}"])
        ws.append([])

        header = ["USN", "Name"]
        for sess in sessions:
            header.append(str(sess["session_date"]))
        header.extend(["Total", "Present", "Absent", "%"])
        ws.append(header)

        for stu in students:
            row = [stu["usn"], stu["full_name"]]
            present_count = 0

            for sess in sessions:
                cursor.execute(
                    "SELECT status FROM attendance WHERE session_id = %s AND student_id = %s",
                    (sess["id"], stu["id"])
                )
                att = cursor.fetchone()
                status = att["status"] if att else "N/A"
                row.append("P" if status == "present" else "A" if status == "absent" else "-")
                if status == "present":
                    present_count += 1

            total = len(sessions)
            absent_count = total - present_count
            pct = round((present_count / total * 100), 1) if total > 0 else 0

            row.extend([total, present_count, absent_count, pct])
            ws.append(row)

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)

        filename = (
            f"attendance_{info['dept_code']}_{info['section_label']}_"
            f"{info['code']}_{date_to}.xlsx"
        )
        return send_file(
            output, as_attachment=True, download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    except Exception as e:
        flash(f"Download error: {e}", "danger")
        return redirect(url_for("faculty_reports.reports_home"))


@faculty_reports_bp.route("/defaulters")
@admin_or_faculty_required
@profile_completed_required
def my_defaulters():
    """Defaulters in my subjects."""
    cursor = get_cursor()
    threshold = request.args.get("threshold", 75, type=int)

    cursor.execute("SELECT id FROM faculty WHERE user_id = %s", (current_user.id,))
    fac = cursor.fetchone()
    faculty_id = fac["id"] if fac else None

    query = """
        SELECT s.usn, u.full_name,
               sub.code AS subject_code, sub.name AS subject_name,
               sec.section_label, d.code AS dept_code,
               COUNT(a.id) AS total_sessions,
               SUM(a.status = 'present') AS present,
               ROUND(SUM(a.status = 'present') / COUNT(a.id) * 100, 1) AS percentage
        FROM attendance a
        JOIN sessions sess ON sess.id = a.session_id
        JOIN students s ON s.id = a.student_id
        JOIN users u ON u.id = s.user_id
        JOIN subjects sub ON sub.id = sess.subject_id
        JOIN sections sec ON sec.id = sess.section_id
        JOIN departments d ON d.id = sec.department_id
        JOIN section_subjects ss ON ss.subject_id = sub.id AND ss.section_id = sec.id
        JOIN academic_periods ap ON ap.is_active = 1
        WHERE sess.session_date BETWEEN ap.start_date AND ap.end_date
          AND sess.status IN ('completed', 'active')
    """
    params = []

    if faculty_id:
        query += " AND ss.faculty_id = %s"
        params.append(faculty_id)

    query += """
        GROUP BY s.id, sub.id, sec.id
        HAVING percentage < %s
        ORDER BY percentage ASC, sub.code, s.usn
    """
    params.append(threshold)

    cursor.execute(query, tuple(params))
    defaulters_list = cursor.fetchall()

    return render_template(
        "faculty/defaulters.html",
        defaulters=defaulters_list,
        threshold=threshold,
    )