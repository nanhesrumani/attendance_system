"""
attendance_system/api/admin/faculty.py
Faculty management: add, edit, list, assign to departments.
"""

import logging

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash
)
from flask_login import current_user

from core.db import get_db, get_cursor
from auth.helpers import admin_required, hash_password, log_audit, get_client_ip

logger = logging.getLogger(__name__)

faculty_bp = Blueprint(
    "faculty_mgmt", __name__,
    template_folder="../../templates/admin"
)


@faculty_bp.route("/")
@admin_required
def list_faculty():
    cursor = get_cursor()

    search = request.args.get("search", "").strip()

    department = request.args.get("department", "")

    status = request.args.get("status", "")
    """List all faculty members."""
    query = """
    SELECT
        f.id AS faculty_id,
        f.faculty_code,
        f.designation,

        u.id AS user_id,
        u.full_name,
        u.email,
        u.phone,
        u.status AS user_status,
        u.last_login,

        u.must_change_password,
        u.profile_completed,
        u.password_changed_at,

        d.id AS department_id,
        d.code AS dept_code,
        d.name AS dept_name,

        (
            SELECT COUNT(*)
            FROM section_subjects ss
            WHERE ss.faculty_id=f.id
        ) AS subject_count

    FROM faculty f

    JOIN users u
    ON u.id=f.user_id

    LEFT JOIN departments d
    ON d.id=f.department_id

    LEFT JOIN clusters c
    ON c.id=f.cluster_id

    WHERE 1=1
    """

    params=[]

    if search:

        query += """
        AND (
            u.full_name LIKE %s
            OR u.email LIKE %s
            OR f.faculty_code LIKE %s
        )
        """

        keyword=f"%{search}%"

        params.extend([keyword,keyword,keyword])

    if department:

        query+=" AND d.id=%s"
        params.append(department)
    if status=="verified":

        query+=" AND u.profile_completed=1"

    elif status=="pending":

        query+=" AND u.profile_completed=0"

    elif status=="suspended":

        query+=" AND u.status='suspended'"

    query+=" ORDER BY u.full_name"

    cursor.execute(query,tuple(params))

    faculty_list=cursor.fetchall()

    cursor.execute("SELECT id, code, name FROM departments ORDER BY name")
    departments = cursor.fetchall()

    cursor.execute("""
    SELECT
        id,
        cluster_name
    FROM clusters
    ORDER BY cluster_name
    """)

    clusters = cursor.fetchall()

    cursor.execute("""

    SELECT

    COUNT(*) total,

    SUM(profile_completed=1) activated,

    SUM(profile_completed=0) pending,

    SUM(status='suspended') suspended

    FROM users

    WHERE role='faculty'

    """)

    stats = cursor.fetchone()


    return render_template(
        "admin/faculty.html",
        faculty=faculty_list,
        departments=departments,
        clusters=clusters,
        stats=stats
    )


@faculty_bp.route("/add", methods=["POST"])
@admin_required
def add_faculty():
    """Add a new faculty member."""
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "").strip()
    phone = request.form.get("phone", "").strip()
    faculty_code = request.form.get("faculty_code", "").strip()
    department_id = request.form.get("department_id", "")
    designation = request.form.get("designation", "").strip().lower()

    if not full_name or not email or not password:
        flash("Name, email, and password are required.", "danger")
        return redirect(url_for("faculty_mgmt.list_faculty"))

    try:
        conn = get_db()
        cursor = conn.cursor()

        password_hash = hash_password(password)

        # ✅ ALWAYS SET ROLE = faculty (IMPORTANT FIX)
        cursor.execute(
            """
            INSERT INTO users
                (email, password_hash, full_name, role, status,
                 college_id, phone, created_at)
            VALUES (%s, %s, %s, 'faculty', 'active', %s, %s, NOW())
            """,
            (
                email,
                password_hash,
                full_name,
                faculty_code if faculty_code else None,
                phone if phone else None
            )
        )
        user_id = cursor.lastrowid

        # ✅ STORE designation separately (hod / assistant prof etc)
        cursor.execute(
            """
            INSERT INTO faculty (user_id, department_id, designation, created_at)
            VALUES (%s, %s, %s, NOW())
            """,
            (user_id, department_id, designation)
        )

        conn.commit()
        
        log_audit(
              current_user.id, "add_faculty",
            "faculty", user_id, None, f"{full_name} ({email})",
            ip_address=get_client_ip()
        )

        flash(f"Faculty '{full_name}' added.", "success")

    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")
        logger.error(f"add_faculty error: {e}")

    return redirect(url_for("faculty_mgmt.list_faculty"))


@faculty_bp.route("/<int:faculty_id>/edit", methods=["POST"])
@admin_required
def edit_faculty(faculty_id):
    """Edit a faculty member."""
    full_name = request.form.get("full_name", "").strip()
    phone = request.form.get("phone", "").strip()
    faculty_code = request.form.get("faculty_code", "").strip()
    department_id = request.form.get("department_id", "")
    designation = request.form.get("designation", "").strip()
    qualification = request.form.get("qualification", "").strip()
    specialization = request.form.get("specialization", "").strip()
    experience_years = request.form.get("experience_years") or 0
    joining_date = request.form.get("joining_date") or None
    remarks = request.form.get("remarks", "").strip()
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)

        # Get user_id
        cursor.execute("SELECT user_id FROM faculty WHERE id = %s", (faculty_id,))
        fac = cursor.fetchone()
        if not fac:
            flash("Faculty not found.", "danger")
            return redirect(url_for("faculty_mgmt.list_faculty"))

        # Update user
        if full_name:
            cursor.execute(
                """
                UPDATE users
                SET
                    full_name=%s,
                    phone=%s
                WHERE id=%s
                """,
                (
                    full_name,
                    phone if phone else None,
                    fac["user_id"]
                )
            )

        # Update faculty
        cursor.execute(
            """
            UPDATE faculty
            SET
                faculty_code=%s,
                department_id=%s,
                designation=%s,
                qualification=%s,
                specialization=%s,
                experience_years=%s,
                joining_date=%s,
                remarks=%s
            WHERE id=%s
            """,
            (faculty_code if faculty_code else None,
             int(department_id) if department_id else None,
             designation if designation else None,
             qualification if qualification else None,
             specialization if specialization else None,
             experience_years,
             joining_date,
             remarks if remarks else None,
             faculty_id)
        )

        conn.commit()
        flash("Faculty updated.", "success")

    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("faculty_mgmt.list_faculty"))


@faculty_bp.route("/<int:faculty_id>/toggle-status", methods=["POST"])
@admin_required
def toggle_faculty_status(faculty_id):
    """Activate or suspend a faculty account."""
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)

        cursor.execute("SELECT user_id FROM faculty WHERE id = %s", (faculty_id,))
        fac = cursor.fetchone()
        if not fac:
            flash("Faculty not found.", "danger")
            return redirect(url_for("faculty_mgmt.list_faculty"))

        cursor.execute("SELECT status FROM users WHERE id = %s", (fac["user_id"],))
        user = cursor.fetchone()

        new_status = "suspended" if user["status"] == "active" else "active"
        cursor.execute(
            "UPDATE users SET status = %s WHERE id = %s",
            (new_status, fac["user_id"])
        )
        conn.commit()

        log_audit(
              current_user.id, f"faculty_status_{new_status}",
            "users", fac["user_id"], user["status"], new_status,
            ip_address=get_client_ip()
        )

        flash(f"Faculty status changed to '{new_status}'.", "info")

    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("faculty_mgmt.list_faculty"))


@faculty_bp.route("/<int:faculty_id>/reset-password", methods=["POST"])
@admin_required
def reset_faculty_password(faculty_id):
    """Reset faculty password (admin sets a new one)."""
    new_password = request.form.get("new_password", "").strip()

    if not new_password or len(new_password) < 6:
        flash("Password must be at least 6 characters.", "danger")
        return redirect(url_for("faculty_mgmt.list_faculty"))

    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)

        cursor.execute("SELECT user_id FROM faculty WHERE id = %s", (faculty_id,))
        fac = cursor.fetchone()
        if not fac:
            flash("Faculty not found.", "danger")
            return redirect(url_for("faculty_mgmt.list_faculty"))

        new_hash = hash_password(new_password)
        cursor.execute("""
        UPDATE users
        SET
            password_hash=%s,
            must_change_password=1,
            profile_completed=0,
            password_changed_at=NULL
        WHERE id=%s
        """,
        (
            new_hash,
            fac["user_id"]
        ))
        conn.commit()

        log_audit(
              current_user.id, "reset_faculty_password",
            "users", fac["user_id"],
            ip_address=get_client_ip()
        )

        flash("Faculty password reset.", "success")

    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("faculty_mgmt.list_faculty"))


@faculty_bp.route("/<int:faculty_id>/subjects")
@admin_required
def faculty_subjects(faculty_id):
    """View subjects assigned to a faculty member."""
    cursor = get_cursor()

    cursor.execute(
        """
        SELECT f.id, u.full_name, f.faculty_code, d.code AS dept_code
        FROM faculty f
        JOIN users u ON u.id = f.user_id
        LEFT JOIN departments d ON d.id = f.department_id
        WHERE f.id = %s
        """,
        (faculty_id,)
    )
    fac = cursor.fetchone()

    cursor.execute(
        """
        SELECT ss.id, sub.code AS subject_code, sub.name AS subject_name,
               sec.section_label, dept.code AS dept_code,
               sub.subject_type, ap.name AS period_name
        FROM section_subjects ss
        JOIN subjects sub ON sub.id = ss.subject_id
        JOIN sections sec ON sec.id = ss.section_id
        JOIN departments dept ON dept.id = sec.department_id
        JOIN academic_periods ap ON ap.id = ss.academic_period_id
        WHERE ss.faculty_id = %s
        ORDER BY sub.code
        """,
        (faculty_id,)
    )
    subjects = cursor.fetchall()

    return render_template(
        "admin/faculty_subjects.html",
        faculty=fac,
        subjects=subjects,
    )

@faculty_bp.route("/<int:faculty_id>/delete", methods=["POST"])
@admin_required
def delete_faculty(faculty_id):

    try:

        conn = get_db()
        cursor = conn.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                f.user_id,
                (
                    SELECT COUNT(*)
                    FROM section_subjects
                    WHERE faculty_id=f.id
                    )
                    +
                    (
                    SELECT COUNT(*)
                    FROM sessions
                    WHERE faculty_id=f.id
                    )
                    AS usage_count
            FROM faculty f
            WHERE f.id=%s
        """,(faculty_id,))

        faculty = cursor.fetchone()

        if not faculty:

            flash("Faculty not found.","danger")
            return redirect(url_for("faculty_mgmt.list_faculty"))

        if faculty["usage_count"] > 0:

            flash(
                f"Cannot delete faculty. "
                f"{faculty['usage_count']} active assignments found. "
                "Suspend the account instead.",
                "warning"
            )

            return redirect(url_for("faculty_mgmt.list_faculty"))

        cursor.execute(
            "DELETE FROM faculty WHERE id=%s",
            (faculty_id,)
        )

        cursor.execute(
            "DELETE FROM users WHERE id=%s",
            (faculty["user_id"],)
        )

        conn.commit()

        log_audit(
            current_user.id,
            "delete_faculty",
            "faculty",
            faculty_id,
            None,
            "Faculty deleted",
            ip_address=get_client_ip()
        )

        flash(
            "Faculty deleted successfully.",
            "success"
        )

    except Exception as e:

        conn.rollback()

        flash(
            f"Error : {e}",
            "danger"
        )

    return redirect(url_for("faculty_mgmt.list_faculty"))