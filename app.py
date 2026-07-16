
"""
attendance_system/app.py
Main entry point. Creates Flask app, registers blueprints,
initializes background services.
"""

import os
import sys
import logging
from datetime import datetime, timedelta

from flask import Flask, app, redirect, url_for, g, session, make_response
from flask_login import LoginManager, current_user, UserMixin

from config import Config
from core.db import init_db_pool, close_db, get_db, get_cursor
from core import db as core_db

# ============================================
# Logging Setup
# ============================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("attendance_system.log", encoding="utf-8"),
    ]
)
logger = logging.getLogger(__name__)


# ============================================
# Flask-Login Setup
# ============================================
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "warning"
login_manager.session_protection = "strong"  # Enable strong session protection


class UserSession(UserMixin):
    def __init__(self, user_row):
        self.id = user_row.get("id")
        self.email = user_row.get("email")
        self.full_name = user_row.get("full_name")
        self.role = user_row.get("role")

        self.status = user_row.get("status", "active")

        self.department_id = user_row.get("department_id")

        self.is_hod = user_row.get("is_hod", False)

        # ---------- NEW ----------         
        self.must_change_password = bool(
            user_row.get("must_change_password", 0)
        )

        self.profile_completed = bool(
            user_row.get("profile_completed", 1)
        )

        self.email_verified = bool(
            user_row.get("email_verified", 0)
        )

    @property
    def is_active(self):
        return self.status == "active"

    def get_id(self):
        return str(self.id)


@login_manager.user_loader
def load_user(user_id):
    from core.db import get_db

    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)

        # Get user
        cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        user_row = cursor.fetchone()

        if not user_row:
            return None

        # 🔥 GET FACULTY DATA (IMPORTANT)
        if user_row["role"] == "faculty":
            cursor.execute("""
                SELECT designation, department_id
                FROM faculty
                WHERE user_id = %s
            """, (user_id,))
            fac = cursor.fetchone()

            if fac:
                designation = (fac.get("designation") or "").strip().lower()
                user_row["is_hod"] = (designation == "hod")
                user_row["department_id"] = fac.get("department_id")
            else:
                user_row["is_hod"] = False

        return UserSession(user_row)

    except Exception as e:
        logger.error("load_user error: %s", e)
        return None

    finally:
        # Keep request-scoped connection lifecycle in close_db teardown.
        try:
            if cursor:
                cursor.close()
        except Exception:
            pass


# ============================================
# Recognition Engine (Lazy Global)
# ============================================
recognition_engine = None
stream_manager = None

def ensure_schema_updates():
    """Apply lightweight runtime-safe schema updates."""
    conn = None
    cursor = None
    try:
        conn = core_db.connection_pool.get_connection()
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute(
            """
            SELECT 1
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'cameras'
              AND COLUMN_NAME = 'assigned_section_id'
            LIMIT 1
            """
        )
        if cursor.fetchone():
            return

        cursor.execute("ALTER TABLE cameras ADD COLUMN assigned_section_id INT NULL")
        cursor.execute(
            """
            ALTER TABLE cameras
            ADD CONSTRAINT fk_cameras_assigned_section
            FOREIGN KEY (assigned_section_id) REFERENCES sections(id)
            ON DELETE SET NULL
            """
        )
        conn.commit()
        logger.info("Schema update applied: cameras.assigned_section_id")
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        logger.warning("Schema update skipped/failed: %s", e)
    finally:
        try:
            if cursor:
                cursor.close()
        except Exception:
            pass


def init_recognition():
    """Initialize the face recognition engine and stream manager."""
    global recognition_engine, stream_manager

    from recognition.engine import RecognitionEngine
    from recognition.stream_manager import StreamManager

    if recognition_engine is None:
        logger.info("Initializing Recognition Engine...")
        recognition_engine = RecognitionEngine()
        recognition_engine.initialize()
        logger.info("Recognition Engine ready.")

    if stream_manager is None:
        logger.info("Initializing Stream Manager...")
        stream_manager = StreamManager(
            engine=recognition_engine,
            max_streams=Config.MAX_STREAM_THREADS
        )
        logger.info("Stream Manager ready.")


# ============================================
# Session Auto-Scheduler
# ============================================
def init_scheduler(app):
    """Initialize APScheduler for daily session auto-creation."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        logger.warning("APScheduler not installed. Auto-scheduler disabled.")
        return None

    scheduler = BackgroundScheduler(daemon=True)

    def auto_create_sessions():
        """Create sessions for today's timetable slots."""
        with app.app_context():
            conn = None
            cursor = None
            try:
                from core.db import get_db
                conn = get_db()
                cursor = conn.cursor(dictionary=True, buffered=True)

                today = datetime.now().strftime("%Y-%m-%d")
                day_name = datetime.now().strftime("%A")

                # Check holiday
                cursor.execute(
                    """
                    SELECT id FROM holiday_calendar
                    WHERE holiday_date = %s
                    AND (scope = 'college' OR department_id IS NULL)
                    """,
                    (today,)
                )
                if cursor.fetchone():
                    logger.info("Today (%s) is a holiday. Skipping.", today)
                    return

                # Get timetable slots
                cursor.execute(
                    """
                    SELECT t.id AS timetable_id, t.section_id,
                        t.subject_id, t.faculty_id
                    FROM timetable t
                    JOIN academic_periods ap ON ap.id = t.academic_period_id
                    WHERE t.day_of_week = %s
                    AND ap.is_active = 1
                    AND t.slot_type NOT IN ('Interval', 'Lunch')
                    AND t.subject_id IS NOT NULL
                    """,
                    (day_name,)
                )
                slots = cursor.fetchall()

                created = 0
                for slot in slots:
                    cursor.execute(
                        "SELECT id FROM sessions WHERE timetable_id = %s AND session_date = %s",
                        (slot["timetable_id"], today)
                    )
                    if cursor.fetchone():
                        continue

                    cursor.execute(
                        """
                        INSERT INTO sessions
                            (timetable_id, section_id, subject_id, faculty_id,
                            session_date, status, created_at)
                        VALUES (%s, %s, %s, %s, %s, 'scheduled', NOW())
                        """,
                        (slot["timetable_id"], slot["section_id"],
                        slot["subject_id"], slot["faculty_id"], today)
                    )
                    session_id = cursor.lastrowid

                    cursor.execute(
                        "SELECT id, usn FROM students WHERE section_id = %s",
                        (slot["section_id"],)
                    )
                    students = cursor.fetchall()
                    for stu in students:
                        cursor.execute(
                            """
                            INSERT IGNORE INTO attendance
                                (session_id, student_id, usn, status, method, marked_at)
                            VALUES (%s, %s, %s, 'absent', 'system', NOW())
                            """,
                            (session_id, stu["id"], stu["usn"])
                        )
                    created += 1

                conn.commit()
                logger.info("Auto-scheduler: Created %s sessions for %s", created, today)

            except Exception as e:
                logger.error(f"Auto-scheduler error: {e}")
                if conn:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
            finally:
                try:
                    if cursor:
                        cursor.close()
                    if conn:
                        conn.close()
                except Exception:
                    pass

    try:
        hour, minute = Config.SESSION_AUTO_OPEN_TIME.split(":")
        hour, minute = int(hour), int(minute)
    except Exception:
        hour, minute = 8, 0

    scheduler.add_job(
        auto_create_sessions,
        trigger="cron",
        hour=hour,
        minute=minute,
        id="auto_create_sessions",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started: %02d:%02d daily", hour, minute)
    return scheduler


# ============================================
# App Factory
# ============================================
def create_app():
    """Create and configure the Flask application."""
    Config.ensure_dirs()

    base_dir = os.path.abspath(os.path.dirname(__file__))

    app = Flask(
        __name__,
        template_folder=os.path.join(base_dir, "templates"),
        static_folder=os.path.join(base_dir, "static"),
    )
    app.config.from_object(Config)
    
    # ---- Session configuration for better security ----
    app.config['SESSION_COOKIE_SECURE'] = os.getenv("FLASK_ENV", "").lower() == "production"
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=1)

    # ---- Initialize DB (single pool) ----
    init_db_pool()
    ensure_schema_updates()

    # ---- Flask-Login ----
    login_manager.init_app(app)

    # ---- Teardown ----
    app.teardown_appcontext(close_db)

    # ---- Context Processor ----
    @app.context_processor
    def inject_globals():
        return {
            "now": datetime.now,
            "current_user": current_user,
        }

    # ---- Root Route ----
    @app.route("/")
    def index():
        if current_user.is_authenticated:
            role = (getattr(current_user, "role", "") or "").strip().lower()
            if role == "admin":
                return redirect(url_for("admin.dashboard"))
            if role == "hod":
                return redirect(url_for("hod.dashboard"))
            if role == "faculty":
                return redirect(url_for("faculty.dashboard"))
            if role == "student":
                return redirect(url_for("student.dashboard"))
        return redirect(url_for("auth.login"))

    # ---- CRITICAL: No-cache headers for ALL responses ----
    @app.after_request
    def add_no_cache_headers(response):
        """Add no-cache headers to prevent browser caching of authenticated pages."""
        # Always add these headers for HTML responses
        if response.content_type and 'text/html' in response.content_type:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, post-check=0, pre-check=0, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        
        # For authenticated users, be extra strict
        if current_user.is_authenticated:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, private, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        
        return response

    # ---- Before request: Check session validity ----
    @app.before_request
    def check_session_validity():
        """Check if user session is still valid."""
        if current_user.is_authenticated:
            # Verify user still exists and is active
            if not current_user.is_active:
                from flask_login import logout_user
                logout_user()
                session.clear()
                from flask import flash
                flash("Your session has expired. Please login again.", "warning")
                return redirect(url_for("auth.login"))

    # ============================================
    # Register ALL Blueprints
    # ============================================
    from auth.routes import auth_bp
    app.register_blueprint(auth_bp, url_prefix="/auth")

    # Admin blueprints
    from api.admin.dashboard import admin_bp
    from api.admin.academic import academic_bp
    from api.admin.clusters import cluster_bp
    from api.admin.students import students_bp
    from api.admin.faculty import faculty_bp as admin_faculty_bp
    from api.admin.timetable import timetable_bp
    from api.admin.sessions import sessions_bp
    from api.admin.cameras import cameras_bp
    from api.admin.recognition_control import recog_bp
    from api.admin.reports import admin_reports_bp
    from api.admin.settings import settings_bp
    from api.admin.subjects_maintenance import subj_maint_bp
    from api.admin.electives import electives_bp
    from api.student.registration import student_reg_bp

    
    app.register_blueprint(student_reg_bp, url_prefix="/student/registration")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(academic_bp, url_prefix="/admin/academic")
    app.register_blueprint(cluster_bp, url_prefix="/admin/clusters")
    app.register_blueprint(students_bp, url_prefix="/admin/students")
    app.register_blueprint(admin_faculty_bp, url_prefix="/admin/faculty")
    app.register_blueprint(timetable_bp, url_prefix="/admin/timetable")
    app.register_blueprint(sessions_bp, url_prefix="/admin/sessions")
    app.register_blueprint(cameras_bp, url_prefix="/admin/cameras")
    app.register_blueprint(recog_bp, url_prefix="/admin/recognition")
    app.register_blueprint(admin_reports_bp, url_prefix="/admin/reports")
    app.register_blueprint(settings_bp, url_prefix="/admin/settings")
    app.register_blueprint(subj_maint_bp)   # already carries url_prefix="/admin/subjects"
    app.register_blueprint(electives_bp)    # already carries url_prefix="/admin/electives"
    # HOD blueprints
    from api.hod.dashboard import hod_bp
    from api.hod.faculty import faculty_bp 
    app.register_blueprint(hod_bp)
    app.register_blueprint(faculty_bp)

    # Faculty blueprints
    from api.faculty.dashboard import faculty_bp as fac_bp
    from api.faculty.session_view import session_view_bp
    from api.faculty.reports import faculty_reports_bp
    app.register_blueprint(fac_bp, url_prefix="/faculty")
    app.register_blueprint(session_view_bp, url_prefix="/faculty/session")
    app.register_blueprint(faculty_reports_bp, url_prefix="/faculty/reports")

    # Student blueprint
    from api.student.dashboard import student_bp
    app.register_blueprint(student_bp, url_prefix="/student")

    # ---- Scheduler ----
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        init_scheduler(app)

    logger.info("Flask app created successfully.")
    app.teardown_appcontext(close_db)
    return app


# ============================================
# Direct Run
# ============================================
if __name__ == "__main__":
    app = create_app()
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True,
        threaded=True,
        use_reloader=True,
    )