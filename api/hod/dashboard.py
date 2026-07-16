# api/hod/dashboard.py

from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required, current_user
from core.db import get_cursor

hod_bp = Blueprint("hod", __name__, url_prefix="/hod")


@hod_bp.route('/dashboard')
@login_required
def dashboard():
    if not getattr(current_user, "is_hod", False):
        flash("HOD privileges required", "danger")
        return redirect(url_for("faculty.dashboard"))

    cursor = get_cursor()
    dept_id = current_user.department_id
    faculty_id = current_user.id

    cursor.execute(
        "SELECT COUNT(*) as sections FROM sections WHERE department_id = %s",
        (dept_id,)
    )
    sections = cursor.fetchone()['sections']

    cursor.execute("""
        SELECT COUNT(*) as faculty_count
        FROM faculty f
        JOIN users u ON f.user_id = u.id
        WHERE f.department_id = %s AND u.status = 'active'
    """, (dept_id,))
    faculty = cursor.fetchone()['faculty_count']

    cursor.execute("""
        SELECT COUNT(DISTINCT t.subject_id) as subjects
        FROM timetable t
        WHERE t.faculty_id = %s
    """, (faculty_id,))
    my_subjects = cursor.fetchone()['subjects']

    cursor.execute("""
        SELECT COUNT(*) as today_sessions
        FROM sessions s
        WHERE s.faculty_id = %s AND DATE(s.session_date) = CURDATE()
    """, (faculty_id,))
    todays_sessions = cursor.fetchone()['today_sessions']

    return render_template(
        'hod/dashboard.html',
        sections=sections,
        faculty=faculty,
        my_subjects=my_subjects,
        todays_sessions=todays_sessions,
        department_name=f"Department {dept_id}"
    )






