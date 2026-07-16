"""
attendance_system/api/admin/students.py
Student management: approval queue, section assignment,
face enrollment (photo/webcam/ZIP/Google Sheet), enrollment status.
"""
from PIL import Image
import io
import logging
from datetime import datetime
from api.admin.dashboard import admin_bp
import pandas as pd
from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, jsonify, Response
)
from flask_login import current_user
from core.db import get_db, get_cursor
from core import db as core_db
from auth.helpers import (
    admin_required, log_audit, get_client_ip, hash_password
)

logger = logging.getLogger(__name__)

students_bp = Blueprint(
    "students_mgmt", __name__,
    template_folder="../../templates/admin"
)

@students_bp.route("/filtered")
@admin_required
def filtered_students():
    """Legacy route compatibility: redirect to modern filtered list."""
    section = request.args.get("section") or request.args.get("section_id") or ""
    enrollment_status = request.args.get("enrollment_status", "")
    search = request.args.get("search", "")
    return redirect(
        url_for(
            "students_mgmt.list_students",
            section_id=section,
            enrollment_status=enrollment_status,
            search=search,
        )
    )

# ============================================
# APPROVAL QUEUE
# ============================================

@admin_bp.route("/students")
@admin_required
def students():
    students = get_students()
    return render_template("admin/students.html", students=students)

@students_bp.route("/approvals")
@admin_required
def approval_queue():
    """View pending student approvals."""
    cursor = get_cursor()
    cursor.execute(
        """
        SELECT aq.*, u.email, u.full_name, u.phone, u.college_id AS usn,
               d.code AS dept_code, d.name AS dept_name,
               u.created_at AS registered_at
        FROM approval_queue aq
        JOIN users u ON u.id = aq.user_id
        LEFT JOIN departments d ON d.id = aq.department_id
        WHERE aq.reviewed_at IS NULL
        ORDER BY aq.submitted_at DESC
        """
    )
    pending = cursor.fetchall()

    # Sections for assignment dropdown
    cursor.execute(
        """
        SELECT sec.id, sec.section_label, sec.sem_number, sec.room,
               d.code AS dept_code
        FROM sections sec
        JOIN departments d ON d.id = sec.department_id
        JOIN academic_periods ap ON ap.id = sec.academic_period_id
        WHERE ap.is_active = 1
        ORDER BY d.code, sec.section_label
        """
    )
    sections = cursor.fetchall()

    return render_template(
        "admin/approval_queue.html",
        pending=pending,
        sections=sections,
    )


@students_bp.route("/approvals/<int:queue_id>/approve", methods=["POST"])
@admin_required
def approve_student(queue_id):
    """Approve a student registration."""
    section_id = request.form.get("section_id", "")
    current_sem = request.form.get("current_sem", "")

    if not section_id:
        flash("Please select a section for the student.", "danger")
        return redirect(url_for("students_mgmt.approval_queue"))

    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)

        # Get queue entry
        cursor.execute(
            "SELECT * FROM approval_queue WHERE id = %s AND reviewed_at IS NULL",
            (queue_id,)
        )
        queue_entry = cursor.fetchone()
        if not queue_entry:
            flash("Approval entry not found or already reviewed.", "warning")
            return redirect(url_for("students_mgmt.approval_queue"))

        user_id = queue_entry["user_id"]

        # Update user status
        cursor.execute(
            "UPDATE users SET status = 'active' WHERE id = %s",
            (user_id,)
        )

        # Update student record
        update_fields = ["section_id = %s", "enrollment_status = 'approved_face_pending'"]
        update_values = [int(section_id)]

        if current_sem:
            update_fields.append("current_sem = %s")
            update_values.append(int(current_sem))

        update_values.append(user_id)
        cursor.execute(
            f"UPDATE students SET {', '.join(update_fields)} WHERE user_id = %s",
            tuple(update_values)
        )

        # Update approval queue
        cursor.execute(
            """
            UPDATE approval_queue
            SET reviewed_at = NOW(), reviewed_by = %s
            WHERE id = %s
            """,
            (current_user.id, queue_id)
        )

        conn.commit()

        log_audit(
              current_user.id, "approve_student",
            "approval_queue", queue_id, "pending", "approved",
            ip_address=get_client_ip()
        )

        flash("Student approved and assigned to section.", "success")

    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")
        logger.error(f"approve_student error: {e}")

    return redirect(url_for("students_mgmt.approval_queue"))


@students_bp.route("/approvals/<int:queue_id>/reject", methods=["POST"])
@admin_required
def reject_student(queue_id):
    """Reject a student registration."""
    reason = request.form.get("reason", "").strip()

    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)

        cursor.execute(
            "SELECT user_id FROM approval_queue WHERE id = %s",
            (queue_id,)
        )
        entry = cursor.fetchone()
        if not entry:
            flash("Entry not found.", "warning")
            return redirect(url_for("students_mgmt.approval_queue"))

        # Update user status
        cursor.execute(
            "UPDATE users SET status = 'rejected' WHERE id = %s",
            (entry["user_id"],)
        )

        # Update queue
        cursor.execute(
            """
            UPDATE approval_queue
            SET reviewed_at = NOW(), reviewed_by = %s, notes = %s
            WHERE id = %s
            """,
            (current_user.id, reason if reason else "Rejected by admin", queue_id)
        )

        conn.commit()

        log_audit(
              current_user.id, "reject_student",
            "approval_queue", queue_id, "pending", "rejected",
            reason=reason, ip_address=get_client_ip()
        )

        flash("Student registration rejected.", "info")

    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("students_mgmt.approval_queue"))


## ============================================
# STUDENT LIST & MANAGEMENT
# ============================================

@students_bp.route("/")
@admin_required
def list_students():

    cursor = get_cursor()

    # =====================================================
    # FILTERS
    # =====================================================

    department_id = request.args.get("department_id", "").strip()

    sem_number = request.args.get("sem_number", "").strip()

    section_id = request.args.get("section_id", "").strip()

    student_id = request.args.get("student_id", "").strip()

    search = request.args.get("search", "").strip()

    enrollment_filter = request.args.get(
        "enrollment_status",
        ""
    ).strip()

    # =====================================================
    # DEPARTMENT OVERVIEW
    # =====================================================

    cursor.execute("""

        SELECT
            d.id,
            d.code,
            d.name,

            COUNT(st.id) AS student_count

        FROM departments d

        LEFT JOIN sections sec
            ON sec.department_id = d.id

        LEFT JOIN students st
            ON st.section_id = sec.id

        GROUP BY
            d.id,
            d.code,
            d.name

        ORDER BY d.name

    """)

    department_overview = cursor.fetchall()

    # =====================================================
    # SEMESTER OVERVIEW
    # =====================================================

    semester_overview = []

    if department_id:

        cursor.execute("""

            SELECT
                sec.sem_number,

                COUNT(st.id) AS student_count

            FROM sections sec

            LEFT JOIN students st
                ON st.section_id = sec.id

            WHERE sec.department_id = %s

            GROUP BY sec.sem_number

            ORDER BY sec.sem_number

        """, (int(department_id),))

        semester_overview = cursor.fetchall()

    # =====================================================
    # SECTION OVERVIEW
    # =====================================================

    section_overview = []

    if department_id and sem_number:

        cursor.execute("""

            SELECT
                sec.id,

                sec.section_label,

                sec.sem_number,

                d.code AS dept_code,

                COUNT(st.id) AS student_count

            FROM sections sec

            JOIN departments d
                ON d.id = sec.department_id

            LEFT JOIN students st
                ON st.section_id = sec.id

            WHERE sec.department_id = %s
            AND sec.sem_number = %s

            GROUP BY
                sec.id,
                sec.section_label,
                sec.sem_number,
                d.code

            ORDER BY sec.section_label

        """, (
            int(department_id),
            int(sem_number)
        ))

        section_overview = cursor.fetchall()

    # =====================================================
    # STUDENT OVERVIEW
    # =====================================================

    student_overview = []

    if section_id:

        query = """

            SELECT
                s.id,
                s.usn,

                s.enrollment_status,

                u.full_name,
                u.email,

                sec.section_label,
                sec.sem_number,

                d.code AS dept_code

            FROM students s

            JOIN users u
                ON u.id = s.user_id

            LEFT JOIN sections sec
                ON sec.id = s.section_id

            LEFT JOIN departments d
                ON d.id = sec.department_id

            WHERE s.section_id = %s

        """

        params = [int(section_id)]

        if enrollment_filter:

            query += " AND s.enrollment_status = %s"

            params.append(enrollment_filter)

        if search:

            query += """
                AND (
                    s.usn LIKE %s
                    OR u.full_name LIKE %s
                    OR u.email LIKE %s
                )
            """

            like = f"%{search}%"

            params.extend([like, like, like])

        query += " ORDER BY s.usn"

        cursor.execute(query, tuple(params))

        student_overview = cursor.fetchall()

    # =====================================================
    # SELECTED STUDENT DETAIL
    # =====================================================

    selected_student_data = None

    if student_id:

        cursor.execute("""

            SELECT
                s.*,

                u.full_name,
                u.email,
                u.phone,

                sec.section_label,
                sec.sem_number,

                d.name AS dept_name,
                d.code AS dept_code

            FROM students s

            JOIN users u
                ON u.id = s.user_id

            LEFT JOIN sections sec
                ON sec.id = s.section_id

            LEFT JOIN departments d
                ON d.id = sec.department_id

            WHERE s.id = %s

        """, (int(student_id),))

        selected_student_data = cursor.fetchone()

    # =====================================================
    # QUICK STATS
    # =====================================================

    cursor.execute("SELECT COUNT(*) AS total FROM students")

    total_students = cursor.fetchone()["total"]

    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM students
        WHERE enrollment_status = 'enrolled'
    """)

    enrolled_students = cursor.fetchone()["total"]

    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM students
        WHERE enrollment_status = 'pending'
    """)

    pending_students = cursor.fetchone()["total"]

    # =====================================================
    # RENDER
    # =====================================================

    return render_template(

        "admin/students.html",

        department_overview=department_overview,

        semester_overview=semester_overview,

        section_overview=section_overview,

        student_overview=student_overview,

        selected_student_data=selected_student_data,

        selected_department=department_id,

        selected_semester=sem_number,

        selected_section=section_id,

        total_students=total_students,

        enrolled_students=enrolled_students,

        pending_students=pending_students,

        filters={
            "search": search,
            "enrollment_status": enrollment_filter,
        }
    )

@students_bp.route("/<int:student_id>/assign-section", methods=["POST"])
@admin_required
def assign_section(student_id):
    """Assign or change a student's section."""
    section_id = request.form.get("section_id", "")

    if not section_id:
        flash("Section is required.", "danger")
        return redirect(url_for("students_mgmt.list_students"))

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE students SET section_id = %s WHERE id = %s",
            (int(section_id), student_id)
        )
        conn.commit()
        flash("Section assigned.", "success")
    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("students_mgmt.list_students"))

@students_bp.route("/<int:student_id>/edit", methods=["POST"])
@admin_required
def edit_student(student_id):
    """Edit a student's basic details."""
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    phone = request.form.get("phone", "").strip()
    usn = request.form.get("usn", "").strip().upper()
    current_sem = request.form.get("current_sem", "").strip()

    if not full_name or not email or not usn:
        flash("Name, email and USN are required.", "danger")
        return redirect(url_for("students_mgmt.list_students", student_id=student_id))

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)

        cursor.execute("SELECT user_id FROM students WHERE id = %s", (student_id,))
        st = cursor.fetchone()
        if not st:
            flash("Student not found.", "danger")
            return redirect(url_for("students_mgmt.list_students"))

        cursor.execute(
            "UPDATE users SET full_name=%s, email=%s, phone=%s WHERE id=%s",
            (full_name, email, phone if phone else None, st["user_id"])
        )
        cursor.execute(
            "UPDATE students SET usn=%s, current_sem=%s WHERE id=%s",
            (usn, int(current_sem) if current_sem else None, student_id)
        )
        conn.commit()

        log_audit(
            current_user.id, "edit_student",
            "students", student_id, None, f"{usn}: {full_name}",
            ip_address=get_client_ip()
        )
        flash("Student updated.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("students_mgmt.list_students", student_id=student_id))


@students_bp.route("/<int:student_id>/delete", methods=["POST"])
@admin_required
def delete_student(student_id):
    """Permanently delete a student."""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)

        cursor.execute("SELECT user_id, usn FROM students WHERE id = %s", (student_id,))
        st = cursor.fetchone()
        if not st:
            flash("Student not found.", "danger")
            return redirect(url_for("students_mgmt.list_students"))

        cursor.execute("DELETE FROM attendance WHERE student_id = %s", (student_id,))
        cursor.execute("DELETE FROM faces WHERE student_id = %s", (student_id,))
        cursor.execute("DELETE FROM students WHERE id = %s", (student_id,))
        cursor.execute("DELETE FROM users WHERE id = %s", (st["user_id"],))
        conn.commit()

        log_audit(
            current_user.id, "delete_student",
            "students", student_id, None, f"Deleted {st['usn']}",
            ip_address=get_client_ip()
        )
        flash("Student deleted permanently.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Cannot delete: {e}", "danger")

    return redirect(url_for("students_mgmt.list_students"))

@students_bp.route("/add", methods=["POST"])
@admin_required
def add_student_manual():
    """Admin manually adds a student (skips approval queue)."""
    full_name = request.form.get("full_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    usn = request.form.get("usn", "").strip().upper()
    password = request.form.get("password", "").strip()
    phone = request.form.get("phone", "").strip()
    section_id = request.form.get("section_id", "")
    current_sem = request.form.get("current_sem", "")

    if not full_name or not email or not usn or not password:
        flash("Name, email, USN, and password are required.", "danger")
        return redirect(url_for("students_mgmt.list_students"))

    try:
        conn = get_db()
        cursor = conn.cursor()

        password_hash = hash_password(password)

        # Create user
        cursor.execute(
            """
            INSERT INTO users
                (email, password_hash, full_name, role, status,
                 college_id, phone, created_at)
            VALUES (%s, %s, %s, 'student', 'active', %s, %s, NOW())
            """,
            (email, password_hash, full_name, usn, phone if phone else None)
        )
        user_id = cursor.lastrowid

        # Create student
        cursor.execute(
            """
            INSERT INTO students
                (user_id, usn, section_id, current_sem,
                 enrollment_status, created_at)
            VALUES (%s, %s, %s, %s, 'approved_face_pending', NOW())
            """,
            (user_id, usn,
             int(section_id) if section_id else None,
             int(current_sem) if current_sem else None)
        )

        conn.commit()

        log_audit(
              current_user.id, "add_student_manual",
            "students", cursor.lastrowid, None, f"{usn}: {full_name}",
            ip_address=get_client_ip()
        )

        flash(f"Student {usn} added. Proceed to face enrollment.", "success")

    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")
        logger.error(f"add_student_manual error: {e}")

    return redirect(url_for("students_mgmt.list_students"))


# ============================================
# FACE ENROLLMENT
# ============================================

@students_bp.route("/enrollment")
@admin_required
def enrollment_page():
    """Face enrollment dashboard."""
    cursor = get_cursor()

    # Students pending face enrollment
    cursor.execute(
        """
        SELECT s.id, s.usn, s.enrollment_status, s.image_path,
               u.full_name, sec.section_label, d.code AS dept_code
        FROM students s
        JOIN users u ON u.id = s.user_id
        LEFT JOIN sections sec ON sec.id = s.section_id
        LEFT JOIN departments d ON d.id = sec.department_id
        WHERE s.enrollment_status IN ('approved_face_pending', 'enrolled_single_photo', 'reenrollment_needed')
          AND u.status = 'active'
        ORDER BY s.usn
        """
    )
    pending_enrollment = cursor.fetchall()

    # Fully enrolled students
    cursor.execute(
        """
        SELECT s.id, s.usn, s.enrollment_status, s.image_path,
               u.full_name, sec.section_label, d.code AS dept_code,
               (SELECT COUNT(*) FROM faces f WHERE f.student_id = s.id) AS face_count
        FROM students s
        JOIN users u ON u.id = s.user_id
        LEFT JOIN sections sec ON sec.id = s.section_id
        LEFT JOIN departments d ON d.id = sec.department_id
        WHERE s.enrollment_status = 'fully_enrolled'
        ORDER BY s.usn
        """
    )
    enrolled = cursor.fetchall()

    return render_template(
        "admin/enrollment.html",
        pending_enrollment=pending_enrollment,
        enrolled=enrolled,
    )


@students_bp.route("/enrollment/<int:student_id>/photo", methods=["POST"])
@admin_required
def enroll_photo(student_id):
    """Enroll a student via photo upload."""
    if "photo" not in request.files:
        flash("No photo uploaded.", "danger")
        return redirect(url_for("students_mgmt.enrollment_page"))

    file = request.files["photo"]
    if file.filename == "":
        flash("No file selected.", "danger")
        return redirect(url_for("students_mgmt.enrollment_page"))

    try:
        # Get student USN
        cursor = get_cursor()
        cursor.execute("SELECT usn FROM students WHERE id = %s", (student_id,))
        student = cursor.fetchone()
        if not student:
            flash("Student not found.", "danger")
            return redirect(url_for("students_mgmt.enrollment_page"))

        from app import recognition_engine
        if not recognition_engine or not recognition_engine.is_initialized:
            flash("Recognition engine not initialized. Click 'Initialize Models' first.", "danger")
            return redirect(url_for("students_mgmt.enrollment_page"))

        from recognition.enrollment import EnrollmentManager
        em = EnrollmentManager(recognition_engine)
        result = em.enroll_from_uploaded_file(
            file, student_id, student["usn"], core_db.connection_pool
        )

        if result["success"]:
            flash(result["message"], "success")
        else:
            flash(result["message"], "danger")

    except Exception as e:
        flash(f"Enrollment error: {e}", "danger")
        logger.error(f"enroll_photo error: {e}")

    return redirect(url_for("students_mgmt.enrollment_page"))


@students_bp.route("/enrollment/bulk-zip", methods=["POST"])
@admin_required
def enroll_bulk_zip():
    """Bulk enroll from ZIP file (images named as USN.jpg)."""
    if "zipfile" not in request.files:
        flash("No ZIP file uploaded.", "danger")
        return redirect(url_for("students_mgmt.enrollment_page"))

    file = request.files["zipfile"]
    if file.filename == "":
        flash("No file selected.", "danger")
        return redirect(url_for("students_mgmt.enrollment_page"))

    try:
        from app import recognition_engine
        if not recognition_engine or not recognition_engine.is_initialized:
            flash("Recognition engine not initialized.", "danger")
            return redirect(url_for("students_mgmt.enrollment_page"))

        from recognition.enrollment import EnrollmentManager
        em = EnrollmentManager(recognition_engine)
        result = em.enroll_from_bulk_zip(file, core_db.connection_pool)

        msg = (
            f"Bulk enrollment: {result['enrolled']}/{result['total']} succeeded, "
            f"{result['failed']} failed."
        )
        flash(msg, "success" if result["enrolled"] > 0 else "warning")

        if result["errors"]:
            for err in result["errors"][:10]:
                flash(err, "warning")

    except Exception as e:
        flash(f"Bulk enrollment error: {e}", "danger")
        logger.error(f"enroll_bulk_zip error: {e}")

    return redirect(url_for("students_mgmt.enrollment_page"))


@students_bp.route("/enrollment/google-sheet", methods=["POST"])
@admin_required
def enroll_google_sheet():
    """Enroll from Google Sheet - FIXED VERSION."""
    try:
        from app import recognition_engine
        if not recognition_engine or not recognition_engine.is_initialized:
            flash("Recognition engine not initialized.", "danger")
            return redirect(url_for("students_mgmt.enrollment_page"))

        # Read Google Sheet
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        from config import Config
        import pandas as pd

        if not Config.SHEET_ID:
            flash("Google Sheet ID not configured in settings.", "danger")
            return redirect(url_for("students_mgmt.enrollment_page"))

        creds = Credentials.from_service_account_file(
            Config.SERVICE_ACCOUNT_FILE,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets.readonly",
                "https://www.googleapis.com/auth/drive.readonly",
            ]
        )
        svc = build("sheets", "v4", credentials=creds)
        result = svc.spreadsheets().values().get(
            spreadsheetId=Config.SHEET_ID,
            range=Config.SHEET_RANGE
        ).execute()
        values = result.get("values", [])

        if not values or len(values) < 2:
            flash("No data found in the Google Sheet.", "warning")
            return redirect(url_for("students_mgmt.enrollment_page"))

        df = pd.DataFrame(values[1:], columns=values[0])

        # 🔥 FIXED CALL WITH DEFAULTS
        from recognition.enrollment import EnrollmentManager
        em = EnrollmentManager(recognition_engine)
        
        # Get default section/dept from form or use 1
        default_section_id = request.form.get('default_section_id', 1, type=int)
        default_dept_id = request.form.get('default_dept_id', 1, type=int)
        
        enroll_result = em.enroll_from_google_sheet(
            df,
            core_db.connection_pool,
            default_section_id=default_section_id,
            default_dept_id=default_dept_id
        )

        # Success message
        msg = (
            f"✅ {enroll_result['enrolled']} enrolled, "
            f"📝 {enroll_result.get('created', 0)} created, "
            f"⏭️ {enroll_result['skipped']} skipped, "
            f"❌ {enroll_result['failed']} failed"
        )
        flash(msg, "success" if enroll_result["enrolled"] > 0 else "info")

        # Show first 5 errors
        if enroll_result["errors"]:
            flash("Errors:", "warning")
            for err in enroll_result["errors"][:5]:
                flash(f"• {err}", "warning")

    except Exception as e:
        flash(f"❌ Google Sheet error: {str(e)}", "danger")
        logger.error(f"enroll_google_sheet error: {e}", exc_info=True)

    return redirect(url_for("students_mgmt.enrollment_page"))

@students_bp.route("/enrollment/<int:student_id>/webcam-capture", methods=["POST"])
@admin_required
def enroll_webcam_capture(student_id):
    """
    Receive a webcam capture (base64 image from browser JS) and enroll.
    Expects JSON: { "image": "data:image/jpeg;base64,..." }
    """
    import base64

    try:
        data = request.get_json()
        if not data or "image" not in data:
            return jsonify({"success": False, "message": "No image data."})

        # Parse base64 image
        img_data = data["image"]
        if "," in img_data:
            img_data = img_data.split(",")[1]

        img_bytes = base64.b64decode(img_data)
        pil_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")

        # Get student USN
        cursor = get_cursor()
        cursor.execute("SELECT usn FROM students WHERE id = %s", (student_id,))
        student = cursor.fetchone()
        if not student:
            return jsonify({"success": False, "message": "Student not found."})

        from app import recognition_engine
        if not recognition_engine or not recognition_engine.is_initialized:
            return jsonify({"success": False, "message": "Models not initialized."})

        from recognition.enrollment import EnrollmentManager
        em = EnrollmentManager(recognition_engine)
        result = em.enroll_from_image(
            pil_img, student_id, student["usn"], core_db.connection_pool,
            enrollment_method="webcam"
        )

        return jsonify(result)

    except Exception as e:
        logger.error(f"enroll_webcam_capture error: {e}")
        return jsonify({"success": False, "message": str(e)})


@students_bp.route("/<int:student_id>/re-enroll", methods=["POST"])
@admin_required
def re_enroll(student_id):
    """Set a student's status to reenrollment_needed."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE students SET enrollment_status = 'reenrollment_needed' WHERE id = %s",
            (student_id,)
        )
        # Delete existing faces
        cursor.execute("DELETE FROM faces WHERE student_id = %s", (student_id,))
        conn.commit()
        flash("Student marked for re-enrollment.", "info")
    except Exception as e:
        conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("students_mgmt.enrollment_page"))
