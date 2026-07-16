"""
attendance_system/auth/routes.py
Authentication routes: login, register, logout, password change.
"""

import logging
from urllib.parse import urlsplit

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, session, make_response
)
from flask_login import login_user, logout_user, current_user, login_required

from auth.helpers import (
    hash_password,
    verify_password,
    get_user_by_email,
    update_last_login,
    log_audit,
    get_client_ip,
)
from core.db import get_db

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)


def _get_user_session_class():
    """Import UserSession from app to avoid circular imports."""
    from app import UserSession
    return UserSession


def _add_no_cache_headers(response):
    """Add comprehensive no-cache headers to response."""
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, post-check=0, pre-check=0, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ============================================
# LOGIN
# ============================================
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    """Login page for all roles."""
    # Force logout any existing session when visiting login page
    if current_user.is_authenticated:
        return _redirect_to_dashboard()

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            flash("Please enter both email and password.", "warning")
            return render_template("auth/login.html")

        conn = get_db()
        user_row = get_user_by_email(email)

        if user_row is None:
            flash("Invalid email or password.", "danger")
            return render_template("auth/login.html")

        if not verify_password(password, user_row["password_hash"]):
            flash("Invalid email or password.", "danger")
            return render_template("auth/login.html")

        status = (user_row.get("status") or "").strip().lower()

        if status == "pending":
            flash(
                "Your account is pending approval. Please wait.",
                "warning"
            )
            return render_template("auth/login.html")

        if status == "rejected":
            flash("Your registration was rejected. Contact administrator.", "danger")
            return render_template("auth/login.html")

        if status == "suspended":
            flash("Your account has been suspended. Contact administrator.", "danger")
            return render_template("auth/login.html")

        if status != "active":
            flash("Your account is not active.", "danger")
            return render_template("auth/login.html")

        # Normalize role before creating session
        user_row["role"] = (user_row.get("role") or "").strip().lower()
        user_row["status"] = status
        user_row["department_id"] = None
        role = user_row["role"]

        # Get department_id based on role
        try:
            conn = get_db()
            cur2 = conn.cursor(dictionary=True, buffered=True)

            if role == "faculty":
                cur2.execute(
                    """
                    SELECT department_id, designation
                    FROM faculty
                    WHERE user_id = %s
                    LIMIT 1
                    """,
                    (user_row["id"],)
                )
                fac = cur2.fetchone()

                if fac:
                    user_row["department_id"] = fac.get("department_id")

                    # 🔥 AUTO DETECT HOD (without changing role)
                    designation = (fac.get("designation") or "").strip().lower()
                    user_row["is_hod"] = (designation == "hod")
                    user_row["role"] = "faculty"
                    print("LOGIN → ROLE:", user_row["role"], "| IS_HOD:", user_row["is_hod"])
                    print("LOGIN DEBUG →", user_row["role"], user_row.get("is_hod"))
            elif role == "student":
                cur2.execute(
                    """
                    SELECT 
                        COALESCE(s.department_id, sec.department_id) AS department_id
                    FROM students s
                    LEFT JOIN sections sec ON sec.id = s.section_id
                    WHERE s.user_id = %s
                    LIMIT 1
                    """,
                    (user_row["id"],)
                )
                stu = cur2.fetchone()
                if stu:
                    user_row["department_id"] = stu.get("department_id")

            cur2.close()

        except Exception as e:
            logger.warning("Login dept lookup non-fatal: %s", e)
            user_row["department_id"] = None

        # Load department_id for HOD users
        try:
            conn = get_db()
            cursor = conn.cursor(dictionary=True, buffered=True)
            cursor.execute(
                """
                SELECT COALESCE(f.department_id, s.department_id) AS department_id
                FROM users u
                LEFT JOIN faculty f ON f.user_id = u.id
                LEFT JOIN students s ON s.user_id = u.id
                WHERE u.id = %s
                """,
                (user_row["id"],)
            )
            dept_row = cursor.fetchone()
            if dept_row:
                user_row["department_id"] = dept_row.get("department_id")
            cursor.close()
        except Exception as e:
            logger.error(f"Department lookup error: {e}")
            user_row["department_id"] = None

        UserSession = _get_user_session_class()
        user_obj = UserSession(user_row)
        
        # Clear any existing session data before login
        session.clear()
        
        login_user(user_obj, remember=True)

        # ======================================
        # First Login Activation
        # ======================================

        if (
            user_row["role"] == "faculty"
            and user_row.get("must_change_password", 0)
        ):
            update_last_login(user_row["id"])

            log_audit(
                user_row["id"],
                "first_login_activation",
                ip_address=get_client_ip()
            )

            flash(
                "Welcome! Please complete your profile before continuing.",
                "info"
            )

            return redirect(
                url_for("faculty.complete_profile")
            )
        # Regenerate session ID for security
        session.modified = True

        logger.info(
            "LOGIN SUCCESS: email=%s role=%s status=%s user_id=%s",
            email, user_row["role"], status, user_row["id"]
        )

        update_last_login(user_row["id"])
        log_audit(user_row["id"], "login", ip_address=get_client_ip())

        flash(f"Welcome, {user_row['full_name']}!", "success")

        next_page = request.args.get("next", "").strip()
        parsed_next = urlsplit(next_page) if next_page else None
        if parsed_next and not parsed_next.scheme and not parsed_next.netloc and next_page.startswith("/") and not next_page.startswith("//"):
            return redirect(next_page)

        return _redirect_to_dashboard()

    # GET request - render login page with no-cache headers
    response = make_response(render_template("auth/login.html"))
    return _add_no_cache_headers(response)


# ============================================
# REGISTER
# ============================================
@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    """User self-registration page."""
    if current_user.is_authenticated:
        return _redirect_to_dashboard()

    conn = get_db()
    departments = []
    try:
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute("SELECT id, code, name FROM departments ORDER BY name")
        departments = cursor.fetchall()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"Load departments error: {e}")

    if request.method == "POST":
        role = request.args.get("role", "student").strip().lower()
        if role not in ["student", "faculty"]:
            role = "student"

        full_name = request.form.get("full_name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        usn = request.form.get("usn", "").strip().upper()
        phone = request.form.get("phone", "").strip()
        department_id = request.form.get("department_id", "").strip()
        notes = request.form.get("notes", "").strip()

        errors = []
        if not full_name:
            errors.append("Full name is required.")
        if not email:
            errors.append("Email is required.")
        if not password:
            errors.append("Password is required.")
        if password and len(password) < 6:
            errors.append("Password must be at least 6 characters.")
        if password != confirm_password:
            errors.append("Passwords do not match.")
        if not department_id:
            errors.append("Department is required.")
        if role == "student" and not usn:
            errors.append("USN is required.")

        if email:
            existing = get_user_by_email(email)
            if existing:
                errors.append("An account with this email already exists.")

        if role == "student" and usn:
            try:
                conn = get_db()
                cursor = conn.cursor(dictionary=True, buffered=True)
                cursor.execute("SELECT id FROM students WHERE usn = %s", (usn,))
                if cursor.fetchone():
                    errors.append("A student with this USN already exists.")
                cursor.close()
                conn.close()
            except Exception as e:
                logger.error(f"USN check error: {e}")

        if errors:
            for err in errors:
                flash(err, "danger")
            return render_template(
                "auth/register.html",
                departments=departments,
                form_data=request.form
            )

        conn = None
        cursor = None
        try:
            conn = get_db()
            cursor = conn.cursor(dictionary=True, buffered=True)

            password_hash = hash_password(password)
            college_id = usn if role == "student" else None
            approver_role = "admin"

            cursor.execute(
                """
                INSERT INTO users
                    (email, password_hash, full_name, role, status,
                     college_id, phone, created_at)
                VALUES (%s, %s, %s, %s, 'pending', %s, %s, NOW())
                """,
                (email, password_hash, full_name, role,
                 college_id, phone or None)
            )
            user_id = cursor.lastrowid

            if role == "student":
                cursor.execute(
                    """
                    INSERT INTO students
                        (user_id, usn, department_id, enrollment_status, created_at)
                    VALUES (%s, %s, %s, 'pending_approval', NOW())
                    """,
                    (user_id, usn, int(department_id))
                )
            elif role == "faculty":
                cursor.execute(
                    """
                    INSERT INTO faculty
                        (user_id, department_id, created_at)
                    VALUES (%s, %s, NOW())
                    """,
                    (user_id, int(department_id))
                )

            cursor.execute(
                """
                INSERT INTO approval_queue
                    (user_id, requested_role, department_id, notes,
                     submitted_at, approver_role)
                VALUES (%s, %s, %s, %s, NOW(), %s)
                """,
                (user_id, role, int(department_id),
                 notes if notes else None, approver_role)
            )

            conn.commit()
            cursor.close()
            conn.close()

            flash(
                "Registration successful! Your account is pending approval.",
                "success"
            )
            return redirect(url_for("auth.login"))

        except Exception as e:
            logger.error(f"Registration error: {e}")
            try:
                if conn:
                    conn.rollback()
                if cursor:
                    cursor.close()
                if conn:
                    conn.close()
            except Exception:
                pass
            flash(f"Registration failed: {e}", "danger")
            return render_template(
                "auth/register.html",
                departments=departments,
                form_data=request.form
            )

    return render_template(
        "auth/register.html",
        departments=departments,
        form_data={}
    )


# ============================================
# LOGOUT - FIXED
# ============================================
@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    """Logout the current user and clear all session data."""
    user_name = "Unknown"
    user_id = None
    
    # Get user info before logout
    if current_user.is_authenticated:
        user_name = getattr(current_user, 'full_name', 'Unknown')
        user_id = getattr(current_user, 'id', None)
        
        # Log audit before logout
        try:
            if user_id:
                log_audit(user_id, "logout", ip_address=get_client_ip())
        except Exception as e:
            logger.error(f"Audit log error during logout: {e}")

    # ========== CRITICAL: Complete logout sequence ==========
    
    # 1. Logout from Flask-Login
    try:
        logout_user()
    except Exception as e:
        logger.error(f"logout_user() error: {e}")
    
    # 2. Mark session as modified before clearing
    session.modified = True
    
    # 3. Clear all session data
    session.clear()
    
    # 4. Pop specific Flask-Login keys (extra safety)
    for key in ['_user_id', '_fresh', '_id', 'user_id', 'csrf_token', '_flashes']:
        session.pop(key, None)
    
    # 5. Set a flag to ensure logout is recognized
    session['_logged_out'] = True
    session.modified = True
    
    logger.info(f"User logged out successfully: {user_name} (ID: {user_id})")
    
    # 6. Create redirect response with flash message
    flash("You have been logged out successfully.", "info")
    
    # 7. Build response with comprehensive no-cache headers
    response = make_response(redirect(url_for("auth.login")))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, post-check=0, pre-check=0, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["Clear-Site-Data"] = '"cache", "storage"'
    
    # 8. Delete session cookie explicitly
    response.delete_cookie('session')
    response.delete_cookie('remember_token')
    
    return response


# ============================================
# FORCE LOGOUT (for debugging/emergency)
# ============================================
@auth_bp.route("/force-logout", methods=["POST"])
@login_required
def force_logout():
    """Force logout - clears everything regardless of state."""
    try:
        logout_user()
    except:
        pass
    
    session.clear()
    session.modified = True
    
    flash("Session cleared. Please login again.", "info")
    
    response = make_response(redirect(url_for("auth.login")))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.delete_cookie('session')
    response.delete_cookie('remember_token')
    
    return response


# ============================================
# PASSWORD CHANGE
# ============================================
@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    """Allow logged-in users to change their password."""
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        errors = []
        if not current_password:
            errors.append("Current password is required.")
        if not new_password:
            errors.append("New password is required.")
        if new_password and len(new_password) < 6:
            errors.append("New password must be at least 6 characters.")
        if new_password != confirm_password:
            errors.append("New passwords do not match.")

        if errors:
            for err in errors:
                flash(err, "danger")
            return render_template("auth/change_password.html")

        conn = None
        cursor = None
        try:
            conn = get_db()
            cursor = conn.cursor(dictionary=True, buffered=True)
            cursor.execute(
                "SELECT password_hash FROM users WHERE id = %s",
                (current_user.id,)
            )
            row = cursor.fetchone()

            if not row or not verify_password(current_password, row["password_hash"]):
                flash("Current password is incorrect.", "danger")
                cursor.close()
                conn.close()
                return render_template("auth/change_password.html")

            new_hash = hash_password(new_password)
            cursor.execute(
                "UPDATE users SET password_hash = %s WHERE id = %s",
                (new_hash, current_user.id)
            )
            conn.commit()

            log_audit(
                current_user.id, "change_password",
                target_table="users", target_id=current_user.id,
                ip_address=get_client_ip()
            )

            cursor.close()
            conn.close()

            flash("Password changed successfully.", "success")
            return redirect(url_for("auth.change_password"))

        except Exception as e:
            logger.error(f"Change password error: {e}")
            try:
                if conn:
                    conn.rollback()
                if cursor:
                    cursor.close()
                if conn:
                    conn.close()
            except Exception:
                pass
            flash(f"Error changing password: {e}", "danger")

    return render_template("auth/change_password.html")


# ============================================
# Helper: Redirect to role-specific dashboard
# ============================================
# REPLACE _redirect_to_dashboard function (around line 600+)
def _redirect_to_dashboard():
    """Redirect current user to their role's dashboard."""
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))

    role = (getattr(current_user, "role", "") or "").strip().lower()
    is_hod = getattr(current_user, "is_hod", False)

    logger.info("Redirecting user %s | role: '%s' | is_hod: %s",
                getattr(current_user, "id", None), role, is_hod)

    # ✅ Admin
    if role == "admin":
        return redirect(url_for("admin.dashboard"))

    # 🔥 Faculty Logic: Check is_hod flag
    if role == "faculty":
        if is_hod:
            return redirect(url_for("hod.dashboard"))  # HOD gets HOD dashboard
        return redirect(url_for("faculty.dashboard"))  # Regular faculty

    # ✅ Student
    if role == "student":
        return redirect(url_for("student.dashboard"))

    # ❌ Unknown role
    logger.warning(f"Unknown role: '{role}' - forcing logout")
    flash("Invalid role. Contact admin.", "danger")
    try:
        logout_user()
    except:
        pass
    session.clear()
    return redirect(url_for("auth.login"))