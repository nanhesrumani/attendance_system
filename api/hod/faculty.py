"""
HOD Faculty Approvals
"""

from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required, current_user

from auth.helpers import hod_required
from core.db import get_db, get_cursor

faculty_bp = Blueprint("hod_faculty", __name__, url_prefix="/hod")


@faculty_bp.route("/faculty-approvals")
@login_required
@hod_required
def faculty_approvals():
    dept_id = getattr(current_user, "department_id", None)
    if not dept_id:
        return render_template("hod/faculty_approvals.html", requests=[])

    cursor = get_cursor()
    cursor.execute(
        """
        SELECT aq.id, aq.user_id, u.full_name, u.email
        FROM approval_queue aq
        JOIN users u ON u.id = aq.user_id
        WHERE aq.requested_role = 'faculty'
          AND aq.approver_role = 'hod'
          AND aq.department_id = %s
          AND aq.reviewed_at IS NULL
        ORDER BY aq.id DESC
        """,
        (dept_id,)
    )
    requests = cursor.fetchall()
    return render_template("hod/faculty_approvals.html", requests=requests)


@faculty_bp.route("/approve/<int:user_id>", methods=["POST"])
@login_required
@hod_required
def approve_faculty(user_id):
    dept_id = getattr(current_user, "department_id", None)
    if not dept_id:
        flash("Department not found for current HOD.", "danger")
        return redirect(url_for("hod_faculty.faculty_approvals"))

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)

        cursor.execute(
            """
            SELECT aq.id
            FROM approval_queue aq
            WHERE aq.user_id = %s
              AND aq.requested_role = 'faculty'
              AND aq.approver_role = 'hod'
              AND aq.department_id = %s
              AND aq.reviewed_at IS NULL
            LIMIT 1
            """,
            (user_id, dept_id)
        )
        request_row = cursor.fetchone()

        if not request_row:
            flash("Approval request not found.", "danger")
            return redirect(url_for("hod_faculty.faculty_approvals"))

        cursor.execute(
            "UPDATE users SET status = 'active' WHERE id = %s",
            (user_id,)
        )
        cursor.execute(
            """
            UPDATE approval_queue
            SET reviewed_at = NOW(), reviewed_by = %s
            WHERE id = %s
            """,
            (current_user.id, request_row["id"])
        )

        conn.commit()
        flash("Faculty approved.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("hod_faculty.faculty_approvals"))