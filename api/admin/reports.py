"""
attendance_system/api/admin/reports.py
Admin reports: college-wide, section-wise, subject-wise attendance,
defaulter lists, PDF/Excel downloads.
"""

import io
import logging
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, jsonify, send_file
)

from core.db import get_db, get_cursor
from auth.helpers import admin_required

logger = logging.getLogger(__name__)

admin_reports_bp = Blueprint(
    "admin_reports", __name__,
    template_folder="../../templates/admin"
)


@admin_reports_bp.route("/")
@admin_required
def reports_home():
    """Reports dashboard."""
    cursor = get_cursor()

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

    cursor.execute(
        "SELECT id, code, name FROM subjects ORDER BY code"
    )
    subjects = cursor.fetchall()

    return render_template(
        "admin/reports.html",
        sections=sections,
        subjects=subjects,
    )


@admin_reports_bp.route("/section/<int:section_id>")
@admin_required
def section_report(section_id):
    """Attendance report for a section."""
    cursor = get_cursor()

    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")

    # Section info
    cursor.execute(
        """
        SELECT sec.*, d.code AS dept_code, d.name AS dept_name,
               ap.name AS period_name, ap.start_date, ap.end_date
        FROM sections sec
        JOIN departments d ON d.id = sec.department_id
        JOIN academic_periods ap ON ap.id = sec.academic_period_id
        WHERE sec.id = %s
        """,
        (section_id,)
    )
    section = cursor.fetchone()
    if not section:
        flash("Section not found.", "danger")
        return redirect(url_for("admin_reports.reports_home"))

    if not date_from:
        date_from = str(section["start_date"])
    if not date_to:
        date_to = datetime.now().strftime("%Y-%m-%d")

    # Get students in section
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

    # Get attendance summary per student
    report_data = []
    for stu in students:
        cursor.execute(
            """
            SELECT
                COUNT(*) AS total_sessions,
                SUM(a.status = 'present') AS present,
                SUM(a.status = 'absent') AS absent
            FROM attendance a
            JOIN sessions s ON s.id = a.session_id
            WHERE a.student_id = %s
              AND s.section_id = %s
              AND s.session_date BETWEEN %s AND %s
              AND s.status IN ('completed', 'active')
            """,
            (stu["id"], section_id, date_from, date_to)
        )
        stats = cursor.fetchone()

        total = stats["total_sessions"] or 0
        present = stats["present"] or 0
        absent = stats["absent"] or 0
        pct = round((present / total * 100), 1) if total > 0 else 0.0

        report_data.append({
            "student_id": stu["id"],
            "usn": stu["usn"],
            "name": stu["full_name"],
            "total": total,
            "present": present,
            "absent": absent,
            "percentage": pct,
        })

    # Sort by percentage ascending (defaulters first)
    report_data.sort(key=lambda x: x["percentage"])

    return render_template(
        "admin/section_report.html",
        section=section,
        report_data=report_data,
        date_from=date_from,
        date_to=date_to,
    )


@admin_reports_bp.route("/defaulters")
@admin_required
def defaulters():
    """College-wide defaulter list (below 75%)."""
    cursor = get_cursor()

    threshold = request.args.get("threshold", 75, type=int)

    cursor.execute(
        """
        SELECT s.id AS student_id, s.usn, u.full_name,
               sec.section_label, d.code AS dept_code,
               COUNT(a.id) AS total_sessions,
               SUM(a.status = 'present') AS present,
               ROUND(SUM(a.status = 'present') / COUNT(a.id) * 100, 1) AS percentage
        FROM attendance a
        JOIN sessions sess ON sess.id = a.session_id
        JOIN students s ON s.id = a.student_id
        JOIN users u ON u.id = s.user_id
        LEFT JOIN sections sec ON sec.id = s.section_id
        LEFT JOIN departments d ON d.id = sec.department_id
        JOIN academic_periods ap ON ap.is_active = 1
        WHERE sess.session_date BETWEEN ap.start_date AND ap.end_date
          AND sess.status IN ('completed', 'active')
        GROUP BY s.id
        HAVING percentage < %s
        ORDER BY percentage ASC
        """,
        (threshold,)
    )
    defaulters_list = cursor.fetchall()

    return render_template(
        "admin/defaulters.html",
        defaulters=defaulters_list,
        threshold=threshold,
    )


@admin_reports_bp.route("/download/section/<int:section_id>")
@admin_required
def download_section_excel(section_id):
    """Download section attendance report as Excel."""
    try:
        import openpyxl

        cursor = get_cursor()

        date_from = request.args.get("from", "2000-01-01")
        date_to = request.args.get("to", datetime.now().strftime("%Y-%m-%d"))

        # Section info
        cursor.execute(
            """
            SELECT sec.section_label, d.code AS dept_code
            FROM sections sec
            JOIN departments d ON d.id = sec.department_id
            WHERE sec.id = %s
            """,
            (section_id,)
        )
        section = cursor.fetchone()

        # Students
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

        # Create workbook
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Attendance Report"

        # Header
        ws.append([
            f"Attendance Report - {section['dept_code']} Section {section['section_label']}"
        ])
        ws.append([f"Period: {date_from} to {date_to}"])
        ws.append([])
        ws.append(["USN", "Name", "Total Sessions", "Present", "Absent", "Percentage"])

        for stu in students:
            cursor.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(a.status = 'present') AS present,
                       SUM(a.status = 'absent') AS absent
                FROM attendance a
                JOIN sessions s ON s.id = a.session_id
                WHERE a.student_id = %s AND s.section_id = %s
                  AND s.session_date BETWEEN %s AND %s
                  AND s.status IN ('completed', 'active')
                """,
                (stu["id"], section_id, date_from, date_to)
            )
            stats = cursor.fetchone()
            total = stats["total"] or 0
            present = stats["present"] or 0
            absent = stats["absent"] or 0
            pct = round((present / total * 100), 1) if total > 0 else 0

            ws.append([stu["usn"], stu["full_name"], total, present, absent, pct])

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)

        filename = f"attendance_{section['dept_code']}_{section['section_label']}_{date_to}.xlsx"
        return send_file(
            output, as_attachment=True, download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

    except Exception as e:
        flash(f"Download error: {e}", "danger")
        return redirect(url_for("admin_reports.reports_home"))