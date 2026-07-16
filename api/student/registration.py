"""
api/student/registration.py
Student self-service subject registration for Open Electives and
Ability/Skill Enhancement Courses. Professional Electives are listed too,
but a student joins a group the admin already created for their cluster
(faculty capacity is cluster-managed, so students don't spin up new groups).
"""
import logging
from flask import Blueprint, render_template, request, jsonify
from flask_login import current_user, login_required

from core.db import get_db, get_cursor
from auth.helpers import student_required, log_audit, get_client_ip
from api.admin.electives import create_elective_group, check_registration_conflict

logger = logging.getLogger(__name__)

student_reg_bp = Blueprint(
    "student_registration", __name__,
    template_folder="../../templates/student",
)


def _get_student(cursor):
    cursor.execute(
        """
        SELECT s.id, s.section_id, s.department_id, s.current_sem
        FROM students s WHERE s.user_id = %s
        """,
        (current_user.id,)
    )
    return cursor.fetchone()


def _get_active_period(cursor):
    cursor.execute("SELECT id, name, sem_number FROM academic_periods WHERE is_active = 1 LIMIT 1")
    return cursor.fetchone()


def _get_student_cluster(cursor, section_id):
    cursor.execute(
        """
        SELECT c.id, c.cluster_name
        FROM cluster_sections cs
        JOIN clusters c ON c.id = cs.cluster_id
        WHERE cs.section_id = %s
        LIMIT 1
        """,
        (section_id,)
    )
    return cursor.fetchone()


@student_reg_bp.route("/")
@login_required
@student_required
def registration_home():
    cursor = get_cursor()
    student = _get_student(cursor)
    if not student:
        return render_template("student/registration.html", error="Student profile not found.")

    period = _get_active_period(cursor)
    if not period:
        return render_template("student/registration.html", error="No active academic period.")

    cluster = _get_student_cluster(cursor, student["section_id"])

    # ---- What's already registered (locks the UI to read-only for these) ----
    cursor.execute(
        """
        SELECT eg.id AS group_id, eg.group_type, eg.subject_id,
               sub.code, sub.name
        FROM elective_group_members egm
        JOIN elective_groups eg ON eg.id = egm.elective_group_id
        JOIN subjects sub ON sub.id = eg.subject_id
        WHERE egm.student_id = %s AND egm.is_active = 1
          AND eg.academic_period_id = %s
        """,
        (student["id"], period["id"])
    )
    registered = {r["group_type"]: r for r in cursor.fetchall()}

    # ---- Professional Electives available to this student's cluster ----
    professional = []
    if cluster:
        cursor.execute(
            """
            SELECT eg.id AS group_id, sub.id AS subject_id, sub.code, sub.name,
                   eg.max_capacity,
                   (SELECT COUNT(*) FROM elective_group_members m
                     WHERE m.elective_group_id = eg.id AND m.is_active = 1) AS enrolled
            FROM elective_groups eg
            JOIN subjects sub ON sub.id = eg.subject_id
            WHERE eg.group_type = 'professional_elective'
              AND eg.cluster_id = %s
              AND eg.academic_period_id = %s
              AND eg.is_active = 1
            ORDER BY sub.name
            """,
            (cluster["id"], period["id"])
        )
        professional = cursor.fetchall()

    # ---- Open Electives (department-agnostic, batch-wide) ----
    cursor.execute(
        """
        SELECT id, code, name FROM subjects
        WHERE offering_mode = 'open_elective'
          AND (sem_number = %s OR sem_number IS NULL)
        ORDER BY name
        """,
        (student["current_sem"],)
    )
    open_elective_subjects = cursor.fetchall()

    # ---- AEC/SEC options for this student's department + semester ----
    cursor.execute(
        """
        SELECT id, code, name FROM subjects
        WHERE offering_mode = 'ability_enhancement'
          AND department_id = %s
          AND sem_number = %s
        ORDER BY name
        """,
        (student["department_id"], student["current_sem"])
    )
    aec_subjects = cursor.fetchall()

    return render_template(
        "student/registration.html",
        period=period,
        cluster=cluster,
        registered=registered,
        professional=professional,
        open_elective_subjects=open_elective_subjects,
        aec_subjects=aec_subjects,
    )


@student_reg_bp.route("/register", methods=["POST"])
@login_required
@student_required
def register():
    """
    Body: group_id (professional elective — join existing admin-made group)
       OR: subject_id (open_elective / ability_enhancement — auto find-or-create
           the department/batch-wide group on first registration)
    """
    conn = get_db()
    cursor = conn.cursor(dictionary=True, buffered=True)
    try:
        student = _get_student(cursor)
        if not student:
            return jsonify({"success": False, "message": "Student profile not found."}), 400

        period = _get_active_period(cursor)
        if not period:
            return jsonify({"success": False, "message": "No active academic period."}), 400

        group_id = request.form.get("group_id", type=int)
        subject_id = request.form.get("subject_id", type=int)

        if group_id:
            cursor.execute("SELECT * FROM elective_groups WHERE id=%s AND is_active=1", (group_id,))
            group = cursor.fetchone()
            if not group:
                return jsonify({"success": False, "message": "Group not found."}), 404

            if group["group_type"] == "professional_elective":
                cluster = _get_student_cluster(cursor, student["section_id"])
                if not cluster or group["cluster_id"] != cluster["id"]:
                    return jsonify({
                        "success": False,
                        "message": "This elective isn't offered to your cluster."
                    }), 403

        elif subject_id:
            cursor.execute("SELECT * FROM subjects WHERE id=%s", (subject_id,))
            subject = cursor.fetchone()
            if not subject or subject["offering_mode"] not in ("open_elective", "ability_enhancement"):
                return jsonify({"success": False, "message": "Invalid subject for self-registration."}), 400

            if subject["offering_mode"] == "ability_enhancement" and subject["department_id"] != student["department_id"]:
                return jsonify({"success": False, "message": "This AEC/SEC option isn't offered to your department."}), 403

            dept_scope = student["department_id"] if subject["offering_mode"] == "ability_enhancement" else None

            cursor.execute(
                """
                SELECT * FROM elective_groups
                WHERE subject_id = %s AND academic_period_id = %s
                  AND group_type = %s
                  AND (%s IS NULL OR department_id = %s)
                  AND parent_section_id IS NULL
                  AND is_active = 1
                LIMIT 1
                """,
                (subject_id, period["id"], subject["offering_mode"], dept_scope, dept_scope)
            )
            group = cursor.fetchone()
            if not group:
                new_id = create_elective_group(
                    cursor, subject_id=subject_id, academic_period_id=period["id"],
                    group_type=subject["offering_mode"],
                    department_id=dept_scope, sem_number=student["current_sem"],
                    created_by=current_user.id,
                )
                cursor.execute("SELECT * FROM elective_groups WHERE id=%s", (new_id,))
                group = cursor.fetchone()
        else:
            return jsonify({"success": False, "message": "No subject or group specified."}), 400

        conflict = check_registration_conflict(cursor, student["id"], group)
        if conflict:
            return jsonify({"success": False, "message": conflict.capitalize() + "."}), 409

        cursor.execute(
            """
            INSERT INTO elective_group_members
                (elective_group_id, student_id, home_section_id, is_active, enrolled_at)
            VALUES (%s,%s,%s,1,NOW())
            ON DUPLICATE KEY UPDATE is_active = 1
            """,
            (group["id"], student["id"], student["section_id"])
        )
        conn.commit()
        log_audit(current_user.id, "self_register_elective", "elective_group_members",
                  group["id"], None, f"student:{student['id']}", ip_address=get_client_ip())
        return jsonify({"success": True, "group_id": group["id"]})

    except Exception as exc:
        conn.rollback()
        logger.error("student register error: %s", exc, exc_info=True)
        return jsonify({"success": False, "message": "Registration failed."}), 500