"""
attendance_system/auth/helpers.py
Authentication helper functions: password hashing, role decorators,
user lookup utilities, audit logging.
"""
import functools
from functools import wraps
import logging

import bcrypt
from flask import redirect, url_for, flash, request, session
from flask_login import current_user, logout_user
from core.db import get_db

logger = logging.getLogger(__name__)


# ============================================
# Helpers
# ============================================
def normalize_role(role):
    return (role or "").strip().lower()


# ============================================
# Password Hashing
# ============================================
def hash_password(plain_password):
    """Hash a plain text password using bcrypt."""
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(plain_password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def verify_password(plain_password, hashed_password):
    """Verify a plain password against a bcrypt hash."""
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8")
        )
    except Exception as e:
        logger.error(f"Password verification error: {e}")
        return False




# ============================================
# Role-Based Access Decorators - FIXED
# ============================================
def login_required_role(*roles):
    """
    Decorator factory for role-based access control.
    Checks authentication AND role membership.
    """
    def decorator(f):
        @functools.wraps(f)
        def decorated_function(*args, **kwargs):
            # Check if user is authenticated
            if not current_user.is_authenticated:
                logger.warning("Access denied: User not authenticated")
                flash("Please log in to access this page.", "warning")
                return redirect(url_for("auth.login", next=request.path))

            # Check if user account is active
            if not current_user.is_active:
                logger.warning(f"Access denied: User {current_user.id} account not active")
                flash("Your account is not active.", "danger")
                # Force logout inactive user
                try:
                    logout_user()
                except:
                    pass
                session.clear()
                return redirect(url_for("auth.login"))

            # Get and normalize user role
            user_role = normalize_role(getattr(current_user, "role", ""))
            allowed_roles = [normalize_role(r) for r in roles]

            logger.debug(
                "ACCESS CHECK: user_id=%s role='%s' allowed=%s path=%s",
                getattr(current_user, "id", None),
                user_role,
                allowed_roles,
                request.path
            )

            # Check if user has required role
            if user_role not in allowed_roles:
                logger.warning(
                    f"Access denied: User {current_user.id} role '{user_role}' "
                    f"not in allowed roles {allowed_roles}"
                )
                flash("You don't have permission to access this page.", "danger")
                # Redirect to their appropriate dashboard instead of login
                return _redirect_user_to_dashboard(user_role)

            return f(*args, **kwargs)

        return decorated_function
    return decorator


def _redirect_user_to_dashboard(role):
    """Redirect user to their role-specific dashboard."""
    if role == "admin":
        return redirect(url_for("admin.dashboard"))
    elif role == "hod":
        return redirect(url_for("hod.dashboard"))
    elif role == "faculty":
        return redirect(url_for("faculty.dashboard"))
    elif role == "student":
        return redirect(url_for("student.dashboard"))
    else:
        return redirect(url_for("auth.login"))


def admin_required(f):
    """Restrict access to admin users only."""
    @functools.wraps(f)
    @login_required_role("admin")
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)
    return wrapper


def faculty_required(f):
    """Allow regular faculty users."""
    @functools.wraps(f)
    @login_required_role("faculty")
    def wrapper(*args, **kwargs):
        # Check if HOD trying to access faculty-only page
        if current_user.is_hod:
            flash("HODs access HOD dashboard first", "info")
            return redirect(url_for("hod.dashboard"))
        return f(*args, **kwargs)
    return wrapper

def profile_completed_required(f):
    """
    Prevent faculty from accessing dashboard pages
    until they complete their profile.
    """

    @wraps(f)
    def decorated_function(*args, **kwargs):

        if (
            current_user.is_authenticated
            and current_user.role == "faculty"
            and getattr(current_user, "must_change_password", False)
        ):

            flash(
                "Please complete your profile first.",
                "warning"
            )

            return redirect(
                url_for("faculty.complete_profile")
            )

        return f(*args, **kwargs)

    return decorated_function

def student_required(f):
    """Restrict access to student users only."""
    @functools.wraps(f)
    @login_required_role("student")
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)
    return wrapper


def hod_required(f):
    """Restrict access to HOD users only (designation='hod')."""
    @functools.wraps(f)
    @login_required_role("faculty")  # Role is still 'faculty'
    def wrapper(*args, **kwargs):
        if not getattr(current_user, 'is_hod', False):
            flash("HOD privileges required", "danger")
            return redirect(url_for("faculty.dashboard"))
        return f(*args, **kwargs)
    return wrapper


def admin_or_faculty_required(f):
    """Restrict access to admin or faculty users."""
    @functools.wraps(f)
    @login_required_role("admin", "faculty")
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)
    return wrapper

def faculty_or_hod_required(f):
    """Allow both regular faculty AND HODs."""
    @functools.wraps(f)
    @login_required_role("faculty")
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)
    return wrapper

def admin_or_hod_required(f):
    """Restrict access to admin or HOD users."""
    @functools.wraps(f)
    @login_required_role("admin", "hod")
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)
    return wrapper


def faculty_or_hod_required(f):
    """Restrict access to faculty or HOD users."""
    @functools.wraps(f)
    @login_required_role("faculty", "hod")
    def wrapper(*args, **kwargs):
        return f(*args, **kwargs)
    return wrapper


# ============================================
# User Lookup Utilities
# ============================================
def get_user_by_email(email):
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute(
            """
            SELECT id, email, password_hash, full_name, role, status,
                   college_id, phone, created_at, last_login
            FROM users
            WHERE email = %s
            LIMIT 1
            """,
            (email,)
        )
        row = cursor.fetchone()
        return row
    except Exception as e:
        logger.error(f"get_user_by_email error: {e}")
        return None
    finally:
        try:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
        except Exception:
            pass


def get_user_by_id(user_id):
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute(
            """
            SELECT id, email, password_hash, full_name, role, status,
                   college_id, phone, created_at, last_login
            FROM users
            WHERE id = %s
            LIMIT 1
            """,
            (int(user_id),)
        )
        row = cursor.fetchone()
        return row
    except Exception as e:
        logger.error(f"get_user_by_id error: {e}")
        return None
    finally:
        try:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
        except Exception:
            pass

def update_last_login(user_id):
    """Update the last_login timestamp for a user."""
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET last_login = NOW() WHERE id = %s",
            (int(user_id),)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"update_last_login error: {e}")
    finally:
        try:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
        except Exception:
            pass


# ============================================
# Audit Log Helper
# ============================================
def log_audit(user_id, action,
              target_table=None, target_id=None,
              old_value=None, new_value=None,
              reason=None, ip_address=None):
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO audit_log
                (user_id, action, target_table, target_id,
                 old_value, new_value, ip_address, reason, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """,
            (
                user_id,
                action,
                target_table,
                target_id,
                str(old_value) if old_value is not None else None,
                str(new_value) if new_value is not None else None,
                ip_address,
                reason,
            )
        )
        conn.commit()
    except Exception as e:
        logger.error(f"log_audit error: {e}")
    finally:
        try:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
        except Exception:
            pass


# ============================================
# Client IP Helper
# ============================================
def get_client_ip():
    """Get the real client IP address."""
    try:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.remote_addr or "unknown"
    except Exception:
        return "unknown"