"""
attendance_system/api/faculty/dashboard.py
Faculty dashboard: today's sessions, quick attendance counts.
"""

import logging
from datetime import datetime

from flask_login import login_required
from flask import Blueprint, redirect, render_template, jsonify, session, url_for,request,flash
from flask_login import current_user, login_required, logout_user

from core.db import get_db, get_cursor
from auth.helpers import faculty_required, admin_or_faculty_required, hash_password, logout_user,log_audit, get_client_ip,profile_completed_required, verify_password

logger = logging.getLogger(__name__)

faculty_bp = Blueprint(
    "faculty", __name__,
    template_folder="../../templates/faculty"
)


def _td_to_str(value) -> str:
    """Convert timedelta to HH:MM string."""
    if value is None:
        return ""
    if hasattr(value, "total_seconds"):
        total = int(value.total_seconds())
        h, rem = divmod(total, 3600)
        m, _ = divmod(rem, 60)
        return f"{h:02d}:{m:02d}"
    return str(value)[:5]


@faculty_bp.route("/")
@login_required
@admin_or_faculty_required
@profile_completed_required
def dashboard():
    # if (
    #     current_user.role == "faculty"
    #     and current_user.must_change_password
    # ):
    #     return redirect(
    #         url_for("faculty.complete_profile")
    #     )
    """Faculty dashboard — shows today's sessions for this faculty."""
    cursor = get_cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    day_name = datetime.now().strftime("%A")

    # Get faculty record for current user
    cursor.execute(
        "SELECT id FROM faculty WHERE user_id = %s",
        (current_user.id,)
    )
    fac = cursor.fetchone()

    if not fac:
        if current_user.role == "admin":
            faculty_id = None
        else:
            return render_template(
                "faculty/dashboard.html",
                error="Faculty profile not found.",
                todays_sessions=[],
                faculty_name=current_user.full_name,
                today=today,
                day_name=day_name,
            )
    else:
        faculty_id = fac["id"]
    # in dashboard(): join elective_groups to pick up group_type for the badge
    # stats: union section_subjects with elective_groups the faculty owns
    cursor.execute("""
        SELECT
        (SELECT COUNT(DISTINCT subject_id) FROM (
            SELECT ss.subject_id FROM section_subjects ss
            JOIN academic_periods ap ON ap.id = ss.academic_period_id
            WHERE ss.faculty_id = %s AND ap.is_active = 1
            UNION
            SELECT eg.subject_id FROM elective_groups eg
            JOIN academic_periods ap ON ap.id = eg.academic_period_id
            WHERE eg.faculty_id = %s AND ap.is_active = 1 AND eg.is_active = 1
        ) t) AS subject_count,
        (SELECT COUNT(DISTINCT section_id) FROM section_subjects ss
            JOIN academic_periods ap ON ap.id = ss.academic_period_id
            WHERE ss.faculty_id = %s AND ap.is_active = 1) AS section_count
    """, (faculty_id, faculty_id, faculty_id) if faculty_id else (0, 0, 0))
    stats = cursor.fetchone()
    # Get today's sessions — EXCLUDE breaks (no subject_id)
    query = """
        SELECT s.id, s.session_date, s.status,
               s.start_time, s.end_time,
               s.rtsp_url, s.opened_at, s.closed_at,
               s.camera_id,
               sub.code AS subject_code, sub.name AS subject_name,
               sec.section_label, d.code AS dept_code,
               cam.name AS camera_name,
               (SELECT COUNT(*) FROM attendance a
                WHERE a.session_id = s.id AND a.status = 'present') AS present_count,
               (SELECT COUNT(*) FROM attendance a WHERE a.session_id = s.id) AS total_count,
                (CASE WHEN s.elective_group_id IS NOT NULL THEN 1 ELSE 0 END) AS is_elective,
                eg.group_type
        FROM sessions s
        JOIN sections sec ON sec.id = s.section_id
        JOIN departments d ON d.id = sec.department_id
        LEFT JOIN subjects sub ON sub.id = s.subject_id
        LEFT JOIN cameras cam ON cam.id = s.camera_id
        LEFT JOIN elective_groups eg ON eg.id = s.elective_group_id
        WHERE s.session_date = %s
          AND s.subject_id IS NOT NULL
    """
    params = [today]

    if faculty_id:
        query += " AND (s.faculty_id = %s OR s.substitute_faculty_id = %s)"
        params.extend([faculty_id, faculty_id])

    query += " ORDER BY s.start_time"

    cursor.execute(query, tuple(params))
    todays_sessions = cursor.fetchall()

    # Convert timedelta fields
    for s in todays_sessions:
        s["start_time"] = _td_to_str(s.get("start_time"))
        s["end_time"] = _td_to_str(s.get("end_time"))

    # Get active streams
    active_streams = {}
    try:
        from app import stream_manager
        if stream_manager:
            all_streams = stream_manager.list_active_streams()
            for sid, info in all_streams.items():
                if sid in [s["id"] for s in todays_sessions]:
                    active_streams[sid] = info
    except Exception:
        pass

    # Quick stats
    cursor.execute("""
        SELECT COUNT(DISTINCT ss.subject_id) AS subject_count,
               COUNT(DISTINCT ss.section_id) AS section_count
        FROM section_subjects ss
        JOIN academic_periods ap ON ap.id = ss.academic_period_id
        WHERE ss.faculty_id = %s AND ap.is_active = 1
    """, (faculty_id,) if faculty_id else (0,))
    stats = cursor.fetchone()

    # Available cameras (for faculty to select when running session)
    cursor.execute("""
        SELECT id, name, rtsp_url, location
        FROM cameras WHERE status = 'active'
        ORDER BY name
    """)
    cameras = cursor.fetchall()

    return render_template(
        "faculty/dashboard.html",
        todays_sessions=todays_sessions,
        active_streams=active_streams,
        faculty_name=current_user.full_name,
        today=today,
        day_name=day_name,
        stats=stats,
        faculty_id=faculty_id,
        cameras=cameras,
    )

@faculty_bp.route("/complete-profile", methods=["GET", "POST"])
@login_required
@faculty_required
def complete_profile():
    """
    First login profile completion.
    """

    cursor = get_cursor()

    # Get current user
    cursor.execute("""
        SELECT
            id,
            full_name,
            email,
            phone,
            must_change_password,
            profile_completed
        FROM users
        WHERE id=%s
    """, (current_user.id,))

    user = cursor.fetchone()

    if not user:
        flash("User not found.", "danger")
        return redirect(url_for("auth.logout"))

    # Already completed
    if (
        not user["must_change_password"]
        and user["profile_completed"]
    ):
        return redirect(url_for("faculty.dashboard"))

    # ===========================
    # POST
    # ===========================

    if request.method == "POST":

        new_email = request.form.get(
            "email",
            ""
        ).strip().lower()

        phone = request.form.get(
            "phone",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        confirm = request.form.get(
            "confirm_password",
            ""
        )

        # -------------------------
        # Validation
        # -------------------------

        if not new_email:
            flash("Email is required.", "danger")
            return render_template(
                "faculty/complete_profile.html",
                user=user
            )

        if password != confirm:
            flash(
                "Passwords do not match.",
                "danger"
            )
            return render_template(
                "faculty/complete_profile.html",
                user=user
            )

        if len(password) < 6:
            flash(
                "Password must contain at least 6 characters.",
                "danger"
            )
            return render_template(
                "faculty/complete_profile.html",
                user=user
            )

        # -------------------------
        # Duplicate Email Check
        # -------------------------

        cursor.execute("""
            SELECT id
            FROM users
            WHERE email=%s
            AND id<>%s
        """, (
            new_email,
            current_user.id
        ))

        if cursor.fetchone():

            flash(
                "Email already exists.",
                "danger"
            )

            return render_template(
                "faculty/complete_profile.html",
                user=user
            )

        conn = get_db()

        try:

            cur = conn.cursor()

            cur.execute("""
                UPDATE users
                SET
                    email=%s,
                    phone=%s,
                    password_hash=%s,
                    must_change_password=0,
                    profile_completed=1,
                    email_verified=0,
                    password_changed_at=NOW(),
                    profile_completed_at=NOW()
                WHERE id=%s
            """, (

                new_email,

                phone if phone else None,

                hash_password(password),

                current_user.id

            ))

            conn.commit()

            log_audit(

                current_user.id,

                "complete_profile",

                target_table="users",

                target_id=current_user.id,

                ip_address=get_client_ip()

            )

            flash(
                "Profile updated successfully. Please login using your new email and password.",
                "success"
            )

            logout_user()
            session.clear()

            return redirect(url_for("auth.login"))

        except Exception as e:

            conn.rollback()

            flash(str(e), "danger")

    return render_template(

        "faculty/complete_profile.html",

        user=user

    )

@faculty_bp.route("/profile")
@login_required
@faculty_required
@profile_completed_required
def profile():

    cursor = get_cursor()

    cursor.execute("""
        SELECT
            u.id,
            u.full_name,
            u.email,
            u.phone,
            u.status,
            u.last_login,
            u.password_changed_at,
            u.profile_completed_at,

            f.faculty_code,
            f.designation,
            f.profile_photo,

            d.name AS department_name,
            d.code AS department_code

        FROM users u

        JOIN faculty f
        ON f.user_id=u.id

        LEFT JOIN departments d
        ON d.id=f.department_id

        WHERE u.id=%s
    """,(current_user.id,))

    profile=cursor.fetchone()

    return render_template(
        "faculty/profile.html",
        profile=profile
    )

@faculty_bp.route("/profile",methods=["POST"])
@login_required
@faculty_required
@profile_completed_required
def update_profile():

    email=request.form.get("email","").strip().lower()

    phone=request.form.get("phone","").strip()

    cursor=get_cursor()

    cursor.execute("""
        SELECT id
        FROM users
        WHERE email=%s
        AND id<>%s
    """,(email,current_user.id))

    if cursor.fetchone():

        flash(
            "Email already exists.",
            "danger"
        )

        return redirect(
            url_for("faculty.profile")
        )

    conn=get_db()

    try:

        cur=conn.cursor()

        cur.execute("""

            UPDATE users

            SET

                email=%s,

                phone=%s

            WHERE id=%s

        """,(

            email,

            phone if phone else None,

            current_user.id

        ))

        conn.commit()

        log_audit(

            current_user.id,

            "update_profile",

            "users",

            current_user.id,

            ip_address=get_client_ip()

        )

        flash(

            "Profile updated successfully.",

            "success"

        )

    except Exception as e:

        conn.rollback()

        flash(str(e),"danger")

    return redirect(
        url_for("faculty.profile")
    )

@faculty_bp.route("/change-password",methods=["POST"])
@login_required
@faculty_required
@profile_completed_required
def change_password():

    current=request.form.get("current_password","")

    new=request.form.get("new_password","")

    confirm=request.form.get("confirm_password","")

    if new!=confirm:

        flash(
            "Passwords do not match.",
            "danger"
        )

        return redirect(
            url_for("faculty.profile")
        )

    if len(new)<8:

        flash(
            "Password must be at least 8 characters.",
            "danger"
        )

        return redirect(
            url_for("faculty.profile")
        )

    cursor=get_cursor()

    cursor.execute("""

        SELECT password_hash

        FROM users

        WHERE id=%s

    """,(current_user.id,))

    row=cursor.fetchone()

    if not verify_password(

        current,

        row["password_hash"]

    ):

        flash(

            "Current password is incorrect.",

            "danger"

        )

        return redirect(
            url_for("faculty.profile")
        )

    conn=get_db()

    try:

        cur=conn.cursor()

        cur.execute("""

            UPDATE users

            SET

                password_hash=%s,

                password_changed_at=NOW()

            WHERE id=%s

        """,(

            hash_password(new),

            current_user.id

        ))

        conn.commit()

        log_audit(

            current_user.id,

            "change_password",

            "users",

            current_user.id,

            ip_address=get_client_ip()

        )

        flash(

            "Password changed successfully.",

            "success"

        )

    except Exception as e:

        conn.rollback()

        flash(str(e),"danger")

    return redirect(
        url_for("faculty.profile")
    )


@faculty_bp.route("/api/my-sessions")
@login_required
@admin_or_faculty_required
@profile_completed_required
def my_sessions_api():
    """AJAX: Get today's sessions for the logged-in faculty."""
    cursor = get_cursor()
    today = datetime.now().strftime("%Y-%m-%d")

    cursor.execute(
        "SELECT id FROM faculty WHERE user_id = %s",
        (current_user.id,)
    )
    fac = cursor.fetchone()
    if not fac:
        return jsonify({"sessions": []})

    cursor.execute("""
        SELECT s.id, s.status,
               sub.code AS subject_code,
               sec.section_label, d.code AS dept_code,
               s.start_time, s.end_time,
               (SELECT COUNT(*) FROM attendance a
                WHERE a.session_id = s.id AND a.status = 'present') AS present_count,
               (SELECT COUNT(*) FROM attendance a WHERE a.session_id = s.id) AS total_count,
                (CASE WHEN s.elective_group_id IS NOT NULL THEN 1 ELSE 0 END) AS is_elective,
                eg.group_type
        FROM sessions s
        JOIN sections sec ON sec.id = s.section_id
        JOIN departments d ON d.id = sec.department_id
        LEFT JOIN subjects sub ON sub.id = s.subject_id
        LEFT JOIN elective_groups eg ON eg.id = s.elective_group_id
        WHERE s.session_date = %s
          AND s.subject_id IS NOT NULL
          AND (s.faculty_id = %s OR s.substitute_faculty_id = %s)
        ORDER BY s.start_time
    """, (today, fac["id"], fac["id"]))
    sessions = cursor.fetchall()

    for s in sessions:
        s["start_time"] = _td_to_str(s.get("start_time"))
        s["end_time"] = _td_to_str(s.get("end_time"))

    return jsonify({"sessions": sessions})