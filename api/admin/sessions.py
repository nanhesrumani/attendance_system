# attendance_system/api/admin/sessions.py
"""
ERP-style Session Management Module — v2
Replaces the original single-section session creation with:
  - Multi-section targeting via session_sections junction table
  - Session types: manual | workshop | makeup | seminar | exam
  - Role-based section visibility (admin = all, faculty = assigned only)
  - Automatic status lifecycle (scheduled → active → completed)
  - Dashboard analytics counters with dept/sem/section filters

Blueprint prefix: /admin/sessions  (register in app.py)
"""

import logging
import json
from datetime import datetime, date, time as dtime     

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, jsonify, abort
)
from flask_login import current_user, login_required

from core.db import get_db, get_cursor
from auth.helpers import admin_required, log_audit, get_client_ip

logger = logging.getLogger(__name__)



sessions_bp = Blueprint(
    "sessions", __name__,
    template_folder="../../templates/admin",
    url_prefix="/admin/sessions",
)

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────

MANUAL_SESSION_TYPES = ("manual", "workshop", "makeup", "seminar", "exam")
SESSION_TYPE_LABELS = {
    "regular":  "Regular (Timetable)",
    "manual":   "Manual / Extra Class",
    "workshop": "Workshop",
    "makeup":   "Make-up Class",
    "seminar":  "Seminar / Guest Lecture",
    "exam":     "Examination",
}

import datetime as _dt

def _td_to_str(td):
    """Convert a timedelta (MySQL TIME) to 'HH:MM' string."""
    if td is None:
        return "—"
    if isinstance(td, str):
        return td[:5]
    if isinstance(td, _dt.time):
        return td.strftime("%H:%M")
    # timedelta
    total_seconds = int(td.total_seconds())
    h, rem = divmod(total_seconds, 3600)
    m, _   = divmod(rem, 60)
    return f"{h:02d}:{m:02d}"


def _str_to_time(s):
    """Parse 'HH:MM' or 'HH:MM:SS' string → datetime.time object."""
    if not s or s == "—":
        return _dt.time(0, 0)
    parts = s.split(":")
    return _dt.time(int(parts[0]), int(parts[1]))

# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────

def _current_faculty_id():
    """Return faculty.id for the logged-in faculty user, or None."""
    if current_user.role == "faculty":
        cursor = get_cursor()
        cursor.execute(
            "SELECT id FROM faculty WHERE user_id = %s", (current_user.id,)
        )
        row = cursor.fetchone()
        return row["id"] if row else None
    return None


def _get_sections_for_role(faculty_id=None):
    """
    Return sections the current user is permitted to target.
    Admin → all active sections.
    Faculty → only sections where they are assigned via section_subjects.
    """
    cursor = get_cursor()
    if faculty_id:
        cursor.execute("""
            SELECT DISTINCT
                sec.id          AS section_id,
                sec.section_label,
                sec.sem_number,
                d.id            AS department_id,
                d.code          AS dept_code,
                d.name          AS dept_name
            FROM   section_subjects ss
            JOIN   sections         sec ON sec.id = ss.section_id
            JOIN   departments      d   ON d.id   = sec.department_id
            WHERE  ss.faculty_id = %s
            ORDER  BY d.code, sec.sem_number, sec.section_label
        """, (faculty_id,))
    else:
        # Admin: all sections
        cursor.execute("""
            SELECT
                sec.id          AS section_id,
                sec.section_label,
                sec.sem_number,
                d.id            AS department_id,
                d.code          AS dept_code,
                d.name          AS dept_name
            FROM   sections     sec
            JOIN   departments  d  ON d.id = sec.department_id
            ORDER  BY d.code, sec.sem_number, sec.section_label
        """)
    return cursor.fetchall()


def _compute_status(session_date, start_time, end_time, current_status):
    """
    Derive live status from clock time.
    Terminal states (dismissed/cancelled) are never overridden.
    """
    if current_status in ("dismissed", "cancelled"):
        return current_status
    now = datetime.now()
    try:
        dt_start = datetime.combine(session_date, start_time)
        dt_end   = datetime.combine(session_date, end_time)
    except Exception:
        return current_status
    if now >= dt_end:
        return "completed"
    if dt_start <= now < dt_end:
        return "active"
    return "scheduled"


def _rows_to_grouped(sections_flat):
    """
    Convert flat section rows into a nested structure:
    { dept_id: { dept_code, dept_name, semesters: { sem: [sections] } } }
    Useful for building the multi-select UI data.
    """
    grouped = {}
    for row in sections_flat:
        did = row["department_id"]
        sem = row["sem_number"]
        if did not in grouped:
            grouped[did] = {
                "dept_id":   did,
                "dept_code": row["dept_code"],
                "dept_name": row["dept_name"],
                "semesters": {},
            }
        if sem not in grouped[did]["semesters"]:
            grouped[did]["semesters"][sem] = []
        grouped[did]["semesters"][sem].append({
            "section_id":    row["section_id"],
            "section_label": row["section_label"],
        })
    return grouped


# ─────────────────────────────────────────────────────────────
# ROUTE 1 — Dashboard / Analytics
# ─────────────────────────────────────────────────────────────

@sessions_bp.route("/dashboard")
@login_required
@admin_required
def dashboard():
    """
    ERP analytics dashboard.
    Counters + mini-charts, no raw session table.
    Supports ?dept_id=&semester=&section_id=&date_from=&date_to= filters.
    """
    cursor = get_cursor()

    # ── Filter params ──────────────────────────────────────────
    dept_id    = request.args.get("dept_id",    type=int)
    semester   = request.args.get("semester",   type=int)
    section_id = request.args.get("section_id", type=int)
    print("SECTION ID =", section_id)
    date_from  = request.args.get("date_from",  date.today().strftime("%Y-%m-%d"))
    date_to    = request.args.get("date_to",    date.today().strftime("%Y-%m-%d"))
    session_type = request.args.get("session_type", "")

    where_clauses = ["s.session_date BETWEEN %s AND %s"]
    params        = [date_from, date_to]

    if dept_id:
        where_clauses.append("d.id = %s")
        params.append(dept_id)
    if semester:
        where_clauses.append("sec.sem_number = %s")
        params.append(semester)
    if section_id:
        where_clauses.append("ss.section_id = %s")
        params.append(section_id)
    if session_type:
        where_clauses.append("s.session_type = %s")
        params.append(session_type)

    where_sql = "WHERE " + " AND ".join(where_clauses)

    # ── Counter: total, by-status breakdown ───────────────────
    cursor.execute(f"""
        SELECT
            COUNT(DISTINCT s.id)                                                    AS total_sessions,
            SUM(s.status = 'scheduled')                                             AS scheduled_count,
            SUM(s.status = 'active')                                                AS active_count,
            SUM(s.status = 'completed')                                             AS completed_count,
            SUM(s.status IN ('dismissed','cancelled'))                               AS cancelled_count,
            SUM(s.source = 'manual')                                                AS manual_sessions,
            SUM(s.source = 'timetable')                                             AS timetable_sessions,
            ROUND(AVG(va.attendance_pct), 1)                                        AS avg_attendance_pct
        FROM      sessions         s
        JOIN      session_sections ss  ON ss.session_id  = s.id
        JOIN      sections         sec ON sec.id          = ss.section_id
        JOIN      departments      d   ON d.id            = sec.department_id
        LEFT JOIN v_session_analytics va
               ON va.session_id = s.id AND va.section_id = ss.section_id
        {where_sql}
    """, params)
    counters = cursor.fetchone()

    # ── Per-department breakdown ───────────────────────────────
    cursor.execute(f"""
        SELECT
            d.code                                                                  AS dept_code,
            d.name                                                                  AS dept_name,
            COUNT(DISTINCT s.id)                                                    AS session_count,
            ROUND(AVG(va.attendance_pct), 1)                                        AS avg_pct
        FROM      sessions         s
        JOIN      session_sections ss  ON ss.session_id  = s.id
        JOIN      sections         sec ON sec.id          = ss.section_id
        JOIN      departments      d   ON d.id            = sec.department_id
        LEFT JOIN v_session_analytics va
               ON va.session_id = s.id AND va.section_id = ss.section_id
        {where_sql}
        GROUP BY  d.id, d.code, d.name
        ORDER BY  session_count DESC
    """, params)
    dept_breakdown = cursor.fetchall()

    # ── Recent manual sessions (last 10) ──────────────────────
    cursor.execute(f"""
        SELECT
            s.id,
            s.session_date,
            s.start_time,
            s.end_time,
            s.status,
            s.session_type,
            s.title,
            s.scope,
            COUNT(DISTINCT ss2.section_id)                          AS section_count,
            u.full_name                                             AS created_by_name
        FROM      sessions   s
        JOIN      session_sections ss  ON ss.session_id = s.id
        JOIN      session_sections ss2 ON ss2.session_id = s.id
        JOIN      sections    sec ON sec.id = ss.section_id
        JOIN      departments d   ON d.id = sec.department_id
        LEFT JOIN users       u   ON u.id = s.created_by
        {where_sql}
          AND s.source = 'manual'
        GROUP BY  s.id
        ORDER BY  s.session_date DESC, s.start_time DESC
        LIMIT 10
    """, params)
    recent_manual = cursor.fetchall()

    # ── Session-type distribution (for pie chart) ─────────────
    cursor.execute(f"""
        SELECT   s.session_type, COUNT(DISTINCT s.id) AS cnt
        FROM     sessions         s
        JOIN     session_sections ss  ON ss.session_id  = s.id
        JOIN     sections         sec ON sec.id          = ss.section_id
        JOIN     departments      d   ON d.id            = sec.department_id
        {where_sql}
        GROUP BY s.session_type
    """, params)
    type_dist = cursor.fetchall()

    # ── Filter dropdowns ──────────────────────────────────────
    cursor.execute("SELECT id, code, name FROM departments ORDER BY code")
    departments = cursor.fetchall()

    cursor.execute("SELECT DISTINCT sem_number FROM sections ORDER BY sem_number")
    semesters = [r["sem_number"] for r in cursor.fetchall()]

    return render_template(
        "admin/sessions_dashboard.html",
        counters=counters,
        dept_breakdown=dept_breakdown,
        recent_manual=recent_manual,
        type_dist=type_dist,
        departments=departments,
        semesters=semesters,
        session_types=SESSION_TYPE_LABELS,
        # Pass back filter state
        f_dept_id=dept_id,
        f_semester=semester,
        f_section_id=section_id,
        f_date_from=date_from,
        f_date_to=date_to,
        f_session_type=session_type,
    )


# ─────────────────────────────────────────────────────────────
# ROUTE 2 — Create Manual Session (GET form + POST save)
# ─────────────────────────────────────────────────────────────

# @sessions_bp.route("/create", methods=["GET", "POST"])
# @login_required
# @admin_required
# def create_session():
#     faculty_id = _current_faculty_id()
#     all_sections = _get_sections_for_role(faculty_id)
#     grouped = _rows_to_grouped(all_sections)

#     cursor = get_cursor()
#     cursor.execute("SELECT id, code, name FROM departments ORDER BY code")
#     departments = cursor.fetchall()

#     cursor.execute("SELECT DISTINCT sem_number FROM sections ORDER BY sem_number")
#     semesters = [r["sem_number"] for r in cursor.fetchall()]

#     # Subjects for optional selection
#     cursor.execute("SELECT id, code, name FROM subjects ORDER BY name")
#     subjects = cursor.fetchall()
#     elective_group_id = request.form.get("elective_group_id", type=int) or None
#     # If an elective group is chosen, section_ids becomes informational only
#     # (it still drives which timetable slots this session is visible under);
#     # the actual attendance roster comes from resolve_session_roster().
#     primary_section_id = selected_sections[0]

#     # Cameras
#     cursor.execute("SELECT id, name FROM cameras WHERE status = 'active' ORDER BY name")
#     cameras = cursor.fetchall()

#     cursor.execute("""
#         INSERT INTO sessions (
#             section_id, subject_id, elective_group_id, faculty_id,
#             session_date, start_time, end_time,
#             status, session_type, source, scope,
#             title, room, camera_id, created_by, created_at
#         ) VALUES (%s,%s,%s,%s,%s,%s,%s,'scheduled',%s,'manual',%s,%s,%s,%s,%s,NOW())
#     """, (
#         primary_section_id, subject_id, elective_group_id, faculty_id,
#         session_date, start_time_str, end_time_str,
#         session_type, scope, title, room, camera_id, current_user.id,
#     ))
#     session_id = cursor.lastrowid

#     junction_rows = [(session_id, sid, current_user.id) for sid in selected_sections]
#     cursor.executemany(
#         "INSERT IGNORE INTO session_sections (session_id, section_id, added_by) VALUES (%s,%s,%s)",
#         junction_rows
#     )

#     roster = resolve_session_roster(cursor, selected_sections, elective_group_id)
#     attendance_rows = [(session_id, stu["id"], stu["usn"]) for stu in roster]
#     cursor.executemany(
#         """
#         INSERT IGNORE INTO attendance (session_id, student_id, usn, status, marked_at)
#         VALUES (%s,%s,%s,'absent',NOW())
#         """,
#         attendance_rows
#     )

#     cursor.execute("""
#         SELECT
#             sec.id,
#             sec.section_label,
#             d.code AS dept_code
#         FROM sections sec
#         JOIN departments d
#             ON d.id = sec.department_id
#         ORDER BY d.code, sec.section_label
#     """)

#     sections = cursor.fetchall()


#     cursor.execute("""
#         SELECT
#             f.id,
#             u.full_name
#         FROM faculty f
#         JOIN users u
#             ON u.id = f.user_id
#         ORDER BY u.full_name
#     """)

#     faculty_list = cursor.fetchall()


#     cursor.execute("""
#         SELECT
#             id,
#             code,
#             name
#         FROM subjects
#         ORDER BY code
#     """)

#     subjects = cursor.fetchall()
#     if request.method == "GET":
#         return render_template(
#             "admin/session_create.html",
#             sections=sections,
#             faculty_list=faculty_list,
#             subjects=subjects,
#             grouped_sections=grouped,
#             departments=departments,
#             semesters=semesters,
#             cameras=cameras,
#             session_types=MANUAL_SESSION_TYPES,
#             session_type_labels=SESSION_TYPE_LABELS,
#             today=date.today().isoformat(),
#         )

#     # ── POST: validate + save ──────────────────────────────────
#     errors = []

#     # Core fields
#     session_type   = request.form.get("session_type", "manual")
#     title          = request.form.get("title", "").strip() or None
#     session_date   = request.form.get("session_date", "")
#     start_time_str = request.form.get("start_time", "")
#     end_time_str   = request.form.get("end_time", "")
#     subject_id     = request.form.get("subject_id", type=int)
#     camera_id      = request.form.get("camera_id",  type=int)
#     room           = request.form.get("room", "").strip() or None
#     scope          = request.form.get("scope", "multi_section")

#     # Section selection: list of section_id ints
#     selected_sections = request.form.getlist("section_ids", type=int)
#     institute_wide    = request.form.get("institute_wide") == "1"

#     # Validation
#     if session_type not in MANUAL_SESSION_TYPES:
#         errors.append("Invalid session type.")
#     if not session_date:
#         errors.append("Session date is required.")
#     if not start_time_str or not end_time_str:
#         errors.append("Start and end time are required.")
#     if start_time_str >= end_time_str:
#         errors.append("End time must be after start time.")
#     if not institute_wide and not selected_sections:
#         errors.append("Select at least one section, or choose Institute-Wide.")

#     if errors:
#         for e in errors:
#             flash(e, "danger")
#         return render_template(
#             "admin/session_create.html",
#             grouped_sections=grouped,
#             departments=departments,
#             semesters=semesters,
#             subjects=subjects,
#             cameras=cameras,
#             session_types=MANUAL_SESSION_TYPES,
#             session_type_labels=SESSION_TYPE_LABELS,
#             today=date.today().isoformat(),
#         )

#     # Resolve institute_wide → all sections
#     if institute_wide:
#         scope = "institute_wide"
#         selected_sections = [r["section_id"] for r in all_sections]
#     elif len(selected_sections) == 1:
#         scope = "section"
#     else:
#         scope = "multi_section"

#     # Role guard: faculty cannot target sections not assigned to them
#     if faculty_id:
#         allowed_ids = {r["section_id"] for r in all_sections}
#         disallowed  = set(selected_sections) - allowed_ids
#         if disallowed:
#             flash("You are not assigned to one or more selected sections.", "danger")
#             return redirect(url_for("sessions.create_session"))

#     conn = get_db()
#     try:
#         cursor = conn.cursor(dictionary=True, buffered=True)

#         # Primary section_id = first selected (keeps FK valid; junction has all)
#         primary_section_id = selected_sections[0]

#         cursor.execute("""
#             INSERT INTO sessions (
#                 section_id, subject_id, faculty_id,
#                 session_date, start_time, end_time,
#                 status, session_type, source, scope,
#                 title, room, camera_id, created_by, created_at
#             ) VALUES (
#                 %s, %s, %s,
#                 %s, %s, %s,
#                 'scheduled', %s, 'manual', %s,
#                 %s, %s, %s, %s, NOW()
#             )
#         """, (
#             primary_section_id, subject_id, faculty_id,
#             session_date, start_time_str, end_time_str,
#             session_type, scope,
#             title, room, camera_id, current_user.id,
#         ))
#         session_id = cursor.lastrowid

#         # Populate junction table
#         junction_rows = [
#             (session_id, sid, current_user.id) for sid in selected_sections
#         ]
#         cursor.executemany("""
#             INSERT IGNORE INTO session_sections (session_id, section_id, added_by)
#             VALUES (%s, %s, %s)
#         """, junction_rows)


#         # Prepopulate attendance rows

#         for sid in selected_sections:

#             cursor.execute("""
#                 SELECT id, usn
#                 FROM students
#                 WHERE section_id = %s
#             """, (sid,))

#             students = cursor.fetchall()

#             attendance_rows = [
#                 (
#                     session_id,
#                     stu["id"],
#                     stu["usn"]
#                 )
#                 for stu in students
#             ]

#             cursor.executemany("""
#                 INSERT IGNORE INTO attendance
#                 (
#                     session_id,
#                     student_id,
#                     usn,
#                     status,
#                     method,
#                     marked_at
#                 )
#                 VALUES
#                 (
#                     %s, %s, %s,
#                     'absent',
#                     'system',
#                     NOW()
#                 )
#             """, attendance_rows)
#         log_audit(
#             user_id=current_user.id,
#             action="create_manual_session",
#             target_table="sessions",
#             target_id=session_id,
#             new_value=json.dumps({
#                 "type": session_type,
#                 "date": session_date,
#                 "sections": selected_sections,
#                 "scope": scope,
#             }),
#             ip_address=get_client_ip(),
#         )

#         conn.commit()
#         flash(
#             f"Session created successfully for {len(selected_sections)} section(s).",
#             "success",
#         )
#         return redirect(url_for("sessions.session_detail", session_id=session_id))

#     except Exception as exc:
#         conn.rollback()
#         logger.error("create_session error: %s", exc, exc_info=True)
#         flash("Database error while creating session. Please try again.", "danger")
#         return redirect(url_for("sessions.create_session"))

@sessions_bp.route("/create", methods=["GET", "POST"])
@login_required
@admin_required
def create_session():
    faculty_id = _current_faculty_id()
    all_sections = _get_sections_for_role(faculty_id)
    grouped = _rows_to_grouped(all_sections)

    cursor = get_cursor()
    cursor.execute("SELECT id, code, name FROM departments ORDER BY code")
    departments = cursor.fetchall()

    cursor.execute("SELECT DISTINCT sem_number FROM sections ORDER BY sem_number")
    semesters = [r["sem_number"] for r in cursor.fetchall()]

    cursor.execute("SELECT id, code, name FROM subjects ORDER BY code")
    subjects = cursor.fetchall()

    cursor.execute("SELECT id, name FROM cameras WHERE status = 'active' ORDER BY name")
    cameras = cursor.fetchall()

    cursor.execute("""
        SELECT sec.id, sec.section_label, d.code AS dept_code
        FROM sections sec
        JOIN departments d ON d.id = sec.department_id
        ORDER BY d.code, sec.section_label
    """)
    sections = cursor.fetchall()

    cursor.execute("""
        SELECT f.id, u.full_name
        FROM faculty f
        JOIN users u ON u.id = f.user_id
        ORDER BY u.full_name
    """)
    faculty_list = cursor.fetchall()

    common_ctx = dict(
        sections=sections,
        faculty_list=faculty_list,
        subjects=subjects,
        grouped_sections=grouped,
        departments=departments,
        semesters=semesters,
        cameras=cameras,
        session_types=MANUAL_SESSION_TYPES,
        session_type_labels=SESSION_TYPE_LABELS,
        today=date.today().isoformat(),
    )

    if request.method == "GET":
        return render_template("admin/session_create.html", **common_ctx)

    # ── POST: validate + save ──────────────────────────────────
    errors = []

    session_type   = request.form.get("session_type", "manual")
    title          = request.form.get("title", "").strip() or None
    session_date   = request.form.get("session_date", "")
    start_time_str = request.form.get("start_time", "")
    end_time_str   = request.form.get("end_time", "")
    subject_id     = request.form.get("subject_id", type=int)
    camera_id      = request.form.get("camera_id", type=int)
    room           = request.form.get("room", "").strip() or None
    scope          = request.form.get("scope", "multi_section")
    elective_group_id = request.form.get("elective_group_id", type=int) or None

    selected_sections = request.form.getlist("section_ids", type=int)
    institute_wide     = request.form.get("institute_wide") == "1"

    if session_type not in MANUAL_SESSION_TYPES:
        errors.append("Invalid session type.")
    if not session_date:
        errors.append("Session date is required.")
    if not start_time_str or not end_time_str:
        errors.append("Start and end time are required.")
    if start_time_str and end_time_str and start_time_str >= end_time_str:
        errors.append("End time must be after start time.")
    # An elective/split group already fixes its own roster, so it's a valid
    # alternative to picking sections manually.
    if not institute_wide and not selected_sections and not elective_group_id:
        errors.append("Select at least one section, choose Institute-Wide, or pick an elective/split group.")

    if errors:
        for e in errors:
            flash(e, "danger")
        return render_template("admin/session_create.html", **common_ctx)

    if institute_wide:
        scope = "institute_wide"
        selected_sections = [r["section_id"] for r in all_sections]
    elif elective_group_id and not selected_sections:
        # Session is still tagged onto the group's declared section context
        # (e.g. cluster_id / parent_section_id) for timetable visibility;
        # the roster itself is resolved from elective_group_members below.
        scope = "elective_group"
    elif len(selected_sections) == 1:
        scope = "section"
    else:
        scope = "multi_section"

    if faculty_id:
        allowed_ids = {r["section_id"] for r in all_sections}
        disallowed = set(selected_sections) - allowed_ids
        if disallowed:
            flash("You are not assigned to one or more selected sections.", "danger")
            return redirect(url_for("sessions.create_session"))

    conn = get_db()
    try:
        cursor = conn.cursor(dictionary=True, buffered=True)

        primary_section_id = selected_sections[0] if selected_sections else None

        cursor.execute("""
            INSERT INTO sessions (
                section_id, subject_id, elective_group_id, faculty_id,
                session_date, start_time, end_time,
                status, session_type, source, scope,
                title, room, camera_id, created_by, created_at
            ) VALUES (
                %s, %s, %s, %s,
                %s, %s, %s,
                'scheduled', %s, 'manual', %s,
                %s, %s, %s, %s, NOW()
            )
        """, (
            primary_section_id, subject_id, elective_group_id, faculty_id,
            session_date, start_time_str, end_time_str,
            session_type, scope,
            title, room, camera_id, current_user.id,
        ))
        session_id = cursor.lastrowid

        if selected_sections:
            junction_rows = [(session_id, sid, current_user.id) for sid in selected_sections]
            cursor.executemany("""
                INSERT IGNORE INTO session_sections (session_id, section_id, added_by)
                VALUES (%s, %s, %s)
            """, junction_rows)

        # Single source of truth for the roster: elective group members if
        # one was chosen, otherwise every student in the selected section(s).
        roster = resolve_session_roster(cursor, selected_sections, elective_group_id)
        attendance_rows = [(session_id, stu["id"], stu["usn"]) for stu in roster]
        cursor.executemany("""
            INSERT IGNORE INTO attendance
                (session_id, student_id, usn, status, method, marked_at)
            VALUES (%s, %s, %s, 'absent', 'system', NOW())
        """, attendance_rows)

        log_audit(
            user_id=current_user.id,
            action="create_manual_session",
            target_table="sessions",
            target_id=session_id,
            new_value=json.dumps({
                "type": session_type,
                "date": session_date,
                "sections": selected_sections,
                "elective_group_id": elective_group_id,
                "scope": scope,
            }),
            ip_address=get_client_ip(),
        )

        conn.commit()
        flash(f"Session created successfully ({len(roster)} student(s) in roster).", "success")
        return redirect(url_for("sessions.session_detail", session_id=session_id))

    except Exception as exc:
        conn.rollback()
        logger.error("create_session error: %s", exc, exc_info=True)
        flash("Database error while creating session. Please try again.", "danger")
        return redirect(url_for("sessions.create_session"))

# ─────────────────────────────────────────────────────────────
# ROUTE 3 — Session Detail
# ─────────────────────────────────────────────────────────────

@sessions_bp.route("/<int:session_id>")
@login_required
@admin_required
def session_detail(session_id):
    cursor = get_cursor()

    # Core session row
    cursor.execute("""
        SELECT
            s.*,

            sub.code AS subject_code,
            sub.name AS subject_name,

            fu.full_name AS faculty_name, 

            u.full_name AS created_by_name,

            cam.name AS camera_name,

            sec.section_label,
            sec.sem_number,

            d.code AS dept_code

        FROM sessions s

        LEFT JOIN subjects sub
            ON sub.id = s.subject_id

        LEFT JOIN faculty f
            ON f.id = s.faculty_id

        LEFT JOIN users fu
            ON fu.id = f.user_id

        LEFT JOIN users u
            ON u.id = s.created_by

        LEFT JOIN cameras cam
            ON cam.id = s.camera_id

        LEFT JOIN sections sec
            ON sec.id = s.section_id

        LEFT JOIN departments d
            ON d.id = sec.department_id

        WHERE s.id = %s
    """, (session_id,))
    session = cursor.fetchone()
    if not session:
        abort(404)

    # Live status
    live_status = _compute_status(
        session["session_date"],
        session["start_time"],
        session["end_time"],
        session["status"],
    )

    session["live_status"] = live_status

    # Sections in this session
    cursor.execute("""
        SELECT sec.id, sec.section_label, sec.sem_number,
               d.code AS dept_code, d.name AS dept_name,
               (SELECT COUNT(*) FROM students st WHERE st.section_id = sec.id) AS enrolled,
               (SELECT COUNT(*) FROM attendance a
                WHERE  a.session_id = %s
                  AND  a.student_id IN (
                         SELECT id FROM students WHERE section_id = sec.id
                       )
                  AND  a.status = 'present') AS present_count
        FROM   session_sections ss
        JOIN   sections         sec ON sec.id = ss.section_id
        JOIN   departments      d   ON d.id   = sec.department_id
        WHERE  ss.session_id = %s
        ORDER  BY d.code, sec.sem_number, sec.section_label
    """, (session_id, session_id))
    sections = cursor.fetchall()

    cursor.execute("""
        SELECT
            sec.section_label,
            d.code AS dept_code
        FROM session_sections ss
        JOIN sections sec      
            ON sec.id = ss.section_id
        JOIN departments d
            ON d.id = sec.department_id
        WHERE ss.session_id = %s
    """, (session_id,))

    session_sections = cursor.fetchall()

    # Attendance records
   # Attendance records
    cursor.execute("""
        SELECT
            st.usn,
            st.name AS student_name,
            COALESCE(a.status, 'absent') AS status
        FROM students st

        JOIN sessions s
            ON s.section_id = st.section_id

        LEFT JOIN attendance a
            ON a.student_id = st.id
        AND a.session_id = s.id

        WHERE s.id = %s

        ORDER BY st.usn
    """, (session_id,))

    attendance = cursor.fetchall()


    # Attendance summary
    total_count = len(attendance)
    present_count = sum(1 for a in attendance if a["status"] == "present")
    absent_count = total_count - present_count
    return render_template(
        "admin/session_detail.html",
        session_data=session,
        live_status=live_status,
        session_sections=session_sections,
        sections=sections,
        attendance=attendance,
        total_count=total_count,
        present_count=present_count,
        absent_count=absent_count,
    )


# ─────────────────────────────────────────────────────────────
# ROUTE 4 — Edit Manual Session
# ─────────────────────────────────────────────────────────────

@sessions_bp.route("/<int:session_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def edit_session(session_id):
    cursor = get_cursor()
    cursor.execute("SELECT * FROM sessions WHERE id = %s AND source = 'manual'", (session_id,))
    session_row = cursor.fetchone()
    if not session_row:
        flash("Session not found or is timetable-generated (not editable here).", "warning")
        return redirect(url_for("sessions.dashboard"))

    if session_row["status"] in ("completed", "dismissed", "cancelled"):
        flash("Cannot edit a completed or cancelled session.", "warning")
        return redirect(url_for("sessions.session_detail", session_id=session_id))

    faculty_id   = _current_faculty_id()
    all_sections = _get_sections_for_role(faculty_id)
    grouped      = _rows_to_grouped(all_sections)

    # Currently assigned sections
    cursor.execute(
        "SELECT section_id FROM session_sections WHERE session_id = %s", (session_id,)
    )
    current_section_ids = {r["section_id"] for r in cursor.fetchall()}

    cursor.execute("SELECT id, code, name FROM subjects ORDER BY name")
    subjects = cursor.fetchall()
    cursor.execute("SELECT id, name FROM cameras WHERE status = 'active' ORDER BY name")
    cameras = cursor.fetchall()
    cursor.execute("SELECT id, code, name FROM departments ORDER BY code")
    departments = cursor.fetchall()

    if request.method == "GET":
        return render_template(
            "admin/session_edit.html",
            s=session_row,
            grouped_sections=grouped,
            departments=departments,
            subjects=subjects,
            cameras=cameras,
            current_section_ids=current_section_ids,
            session_types=MANUAL_SESSION_TYPES,
            session_type_labels=SESSION_TYPE_LABELS,
        )

    # ── POST ──────────────────────────────────────────────────
    session_type   = request.form.get("session_type", session_row["session_type"])
    title          = request.form.get("title", "").strip() or None
    session_date   = request.form.get("session_date", "")
    start_time_str = request.form.get("start_time", "")
    end_time_str   = request.form.get("end_time", "")
    subject_id     = request.form.get("subject_id",  type=int)
    camera_id      = request.form.get("camera_id",   type=int)
    room           = request.form.get("room", "").strip() or None
    new_section_ids = set(request.form.getlist("section_ids", type=int))
    institute_wide  = request.form.get("institute_wide") == "1"

    if institute_wide:
        new_section_ids = {r["section_id"] for r in all_sections}
        scope = "institute_wide"
    elif len(new_section_ids) == 1:
        scope = "section"
    else:
        scope = "multi_section"

    if not new_section_ids:
        flash("Select at least one section.", "danger")
        return redirect(url_for("sessions.edit_session", session_id=session_id))

    conn = get_db()
    try:
        cur = conn.cursor(dictionary=True, buffered=True)

        cur.execute("""
            UPDATE sessions
            SET    session_type = %s, title = %s, session_date = %s,
                   start_time = %s, end_time = %s, subject_id = %s,
                   camera_id = %s, room = %s, scope = %s,
                   section_id = %s
            WHERE  id = %s
        """, (
            session_type, title, session_date,
            start_time_str, end_time_str, subject_id,
            camera_id, room, scope,
            next(iter(new_section_ids)),  # primary section_id
            session_id,
        ))

        # Diff the junction table
        to_add    = new_section_ids - current_section_ids
        to_remove = current_section_ids - new_section_ids

        if to_remove:
            fmt = ",".join(["%s"] * len(to_remove))
            cur.execute(
                f"DELETE FROM session_sections WHERE session_id = %s AND section_id IN ({fmt})",
                (session_id, *to_remove),
            )

            # REMOVE attendance rows of removed sections
            cur.execute(f"""
                DELETE a
                FROM attendance a
                JOIN students st
                    ON st.id = a.student_id
                WHERE a.session_id = %s
                AND st.section_id IN ({fmt})
            """, (session_id, *to_remove))


        if to_add:
            rows = [(session_id, sid, current_user.id) for sid in to_add]

            cur.executemany(
                """
                INSERT IGNORE INTO session_sections
                (session_id, section_id, added_by)
                VALUES (%s, %s, %s)
                """,
                rows,
            )

            # ADD attendance rows for newly added sections
            for sid in to_add:

                cur.execute("""
                    SELECT id, usn
                    FROM students
                    WHERE section_id = %s
                """, (sid,))

                students = cur.fetchall()

                attendance_rows = [
                    (
                        session_id,
                        stu["id"],
                        stu["usn"]
                    )
                    for stu in students
                ]

                cur.executemany("""
                    INSERT IGNORE INTO attendance
                    (
                        session_id,
                        student_id,
                        usn,
                        status,
                        method,
                        marked_at
                    )
                    VALUES
                    (
                        %s, %s, %s,
                        'absent',
                        'system',
                        NOW()
                    )
                """, attendance_rows)

        conn.commit()
        log_audit(
            user_id=current_user.id,
            action="edit_manual_session",
            target_table="sessions",
            target_id=session_id,
            new_value=json.dumps({"sections": list(new_section_ids)}),
            ip_address=get_client_ip(),
        )
        flash("Session updated successfully.", "success")
        return redirect(url_for("sessions.session_detail", session_id=session_id))

    except Exception as exc:
        conn.rollback()
        logger.error("edit_session error: %s", exc, exc_info=True)
        flash("Database error while updating session.", "danger")
        return redirect(url_for("sessions.edit_session", session_id=session_id))


@sessions_bp.route("/<int:session_id>/open", methods=["POST"])
@login_required
@admin_required
def open_session(session_id):
    """Open a scheduled session."""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE sessions
            SET status = 'active', opened_by = %s, opened_at = NOW()
            WHERE id = %s AND status = 'scheduled'
        """, (current_user.id, session_id))
        conn.commit()
        if cursor.rowcount > 0:
            flash("Session opened successfully.", "success")
        else:
            flash("Session not found or not in 'scheduled' state.", "warning")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(request.referrer or url_for("sessions.list_sessions"))


@sessions_bp.route("/<int:session_id>/close", methods=["POST"])
@login_required
@admin_required
def close_session(session_id):
    """Close an active session."""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE sessions
            SET status = 'completed', closed_at = NOW()
            WHERE id = %s AND status = 'active'
        """, (session_id,))
        conn.commit()

        try:
            from app import stream_manager
            if stream_manager:
                stream_manager.stop_stream(session_id)
        except Exception:
            pass

        if cursor.rowcount > 0:
            flash("Session closed.", "success")
        else:
            flash("Session not in active state.", "warning")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(request.referrer or url_for("sessions.session_detail", session_id=session_id))

# ─────────────────────────────────────────────────────────────
# ROUTE 5 — Cancel / Dismiss Session
# ─────────────────────────────────────────────────────────────

@sessions_bp.route("/<int:session_id>/cancel", methods=["POST"])
@login_required
@admin_required
def dismiss_session(session_id):
    reason = request.form.get("reason", "Cancelled by admin").strip()
    action = request.form.get("action", "cancelled")   # 'cancelled' or 'dismissed'
    if action not in ("cancelled", "dismissed"):
        action = "cancelled"

    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE sessions SET status = %s, dismiss_reason = %s, closed_at = NOW() WHERE id = %s",
            (action, reason, session_id),
        )
        conn.commit()
        log_audit(
            user_id=current_user.id,
            action=f"session_{action}",
            target_table="sessions",
            target_id=session_id,
            reason=reason,
            ip_address=get_client_ip(),
        )

        try:
            from app import stream_manager

            if stream_manager:
                stream_manager.stop_stream(session_id)

        except Exception:
            pass
        flash(f"Session marked as {action}.", "success")
    except Exception as exc:
        conn.rollback()
        logger.error("cancel_session error: %s", exc)
        flash("Failed to cancel session.", "danger")

    return redirect(url_for("sessions.session_detail", session_id=session_id))

def resolve_session_roster(cursor, section_ids, elective_group_id=None):
    """
    Single source of truth for "who is expected in this session".
    - elective_group_id set  -> roster = elective_group_members (subset of students,
                                 possibly spanning multiple home sections)
    - elective_group_id None -> roster = every student in the selected section(s)
                                 (existing whole-section-merge behaviour, unchanged)
    """
    if elective_group_id:
        cursor.execute(
            """
            SELECT s.id, s.usn
            FROM elective_group_members egm
            JOIN students s ON s.id = egm.student_id
            WHERE egm.elective_group_id = %s AND egm.is_active = 1
            """,
            (elective_group_id,)
        )
        return cursor.fetchall()

    fmt = ",".join(["%s"] * len(section_ids))
    cursor.execute(
        f"SELECT id, usn FROM students WHERE section_id IN ({fmt})",
        tuple(section_ids)
    )
    return cursor.fetchall()

@sessions_bp.route("/<int:session_id>/assign-camera", methods=["POST"])
@login_required
@admin_required
def assign_camera(session_id):
    camera_id = request.form.get("camera_id", "") or None
    rtsp_url = request.form.get("rtsp_url", "").strip() or None
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)

        if camera_id:
            cursor.execute(
                """
                SELECT c.rtsp_url, c.assigned_section_id, s.section_id
                FROM cameras c
                JOIN sessions s ON s.id = %s
                WHERE c.id = %s
                """,
                (session_id, int(camera_id))
            )
            cam_row = cursor.fetchone()
            if cam_row and cam_row.get("assigned_section_id") and cam_row["assigned_section_id"] != cam_row["section_id"]:
                flash("Camera is assigned to a different section.", "danger")
                return redirect(request.referrer or url_for("sessions.dashboard"))
            if cam_row and cam_row["rtsp_url"]:
                rtsp_url = cam_row["rtsp_url"]

        cursor.execute("""
            UPDATE sessions
            SET camera_id = %s, rtsp_url = %s
            WHERE id = %s
        """, (
            int(camera_id) if camera_id else None,
            rtsp_url,
            session_id
        ))
        conn.commit()
        flash("Camera assigned to session.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Error assigning camera: {e}", "danger")

    return redirect(request.referrer or url_for("sessions.dashboard"))


@sessions_bp.route("/auto-assign-cameras", methods=["POST"])
@login_required
@admin_required
def auto_assign_cameras():
    """Auto-assign cameras to today's sessions by room match."""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        today = datetime.now().strftime("%Y-%m-%d")

        cursor.execute("""
            SELECT s.id, s.section_id, sec.room
            FROM sessions s
            JOIN sections sec ON sec.id = s.section_id
            WHERE s.session_date = %s
              AND s.camera_id IS NULL
              AND s.subject_id IS NOT NULL
        """, (today,))
        sessions_today = cursor.fetchall()

        cursor.execute("SELECT id, rtsp_url, location, assigned_section_id FROM cameras WHERE status = 'active'")
        cameras_list = cursor.fetchall()

        assigned = 0
        for sess in sessions_today:
            section_camera = next(
                (
                    cam for cam in cameras_list
                    if cam.get("assigned_section_id") == sess["section_id"]
                ),
                None
            )
            if section_camera:
                cursor.execute(
                    "UPDATE sessions SET camera_id = %s, rtsp_url = %s WHERE id = %s",
                    (section_camera["id"], section_camera["rtsp_url"], sess["id"])
                )
                assigned += 1
                continue

            room = (sess.get("room") or "").strip().lower()
            if not room:
                continue
            for cam in cameras_list:
                if cam.get("assigned_section_id"):
                    continue
                cam_loc = (cam.get("location") or "").strip().lower()
                if cam_loc and cam_loc == room:
                    cursor.execute("""
                        UPDATE sessions SET camera_id = %s, rtsp_url = %s WHERE id = %s
                    """, (cam["id"], cam["rtsp_url"], sess["id"]))
                    assigned += 1
                    break

        conn.commit()
        flash(f"Auto-assigned cameras to {assigned} session(s).", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("sessions.dashboard"))

### list sessions added from previous code 
@sessions_bp.route("/")
@login_required
@admin_required
def list_sessions():

    """List sessions with filters."""

    conn = get_db()
    cursor = get_cursor()

    today = datetime.now().strftime("%Y-%m-%d")
    day_name = datetime.now().strftime("%A")

    try:
        _auto_create_todays_sessions(
            cursor,
            conn,
            today,
            day_name
        )
    except Exception as exc:
        logger.error(f"Auto-create error: {exc}")

    date_filter    = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    status_filter  = request.args.get("status", "")
    dept_filter    = request.args.get("dept_id", "")
    dept_id        = request.args.get("dept_id",    type=int)
    semester       = request.args.get("semester",   type=int)
    section_id     = request.args.get("section_id", type=int)

    query = """
        SELECT
            s.id,
            s.session_date,
            s.status,
            s.dismiss_reason,
            s.opened_at,
            s.closed_at,
            s.start_time,
            s.end_time,
            s.camera_id,
            s.rtsp_url,
            s.session_type,

            sub.code AS subject_code,
            sub.name AS subject_name,

            GROUP_CONCAT(
                DISTINCT CONCAT(d.code, '-', sec.section_label)
                ORDER BY d.code, sec.section_label
                SEPARATOR ', '
            ) AS section_names,

            COUNT(DISTINCT sec.id) AS section_count,

            u.full_name  AS faculty_name,
            su.full_name AS substitute_name,
            cam.name     AS camera_name,

            (
                SELECT COUNT(*)
                FROM attendance a
                WHERE a.session_id = s.id
                  AND a.status = 'present'
            ) AS present_count,

            (
                SELECT COUNT(*)
                FROM attendance a
                WHERE a.session_id = s.id
            ) AS total_students

        FROM sessions s

        LEFT JOIN session_sections ss
            ON ss.session_id = s.id

        LEFT JOIN sections sec
            ON sec.id = COALESCE(ss.section_id, s.section_id)

        LEFT JOIN departments d
            ON d.id = sec.department_id
        LEFT JOIN subjects sub
               ON sub.id = s.subject_id
        LEFT JOIN faculty f
               ON f.id = s.faculty_id
        LEFT JOIN users u
               ON u.id = f.user_id
        LEFT JOIN faculty sf
               ON sf.id = s.substitute_faculty_id
        LEFT JOIN users su
               ON su.id = sf.user_id
        LEFT JOIN cameras cam
               ON cam.id = s.camera_id

        WHERE 1=1
    """
    params = []


    if date_filter:
        query += " AND s.session_date = %s"
        params.append(date_filter)

    if section_id:
        query += """
            AND COALESCE(ss.section_id, s.section_id) = %s
        """
        params.append(section_id)

    if status_filter:
        query += " AND s.status = %s"
        params.append(status_filter)

    if dept_filter:
        query += " AND d.id = %s"
        params.append(int(dept_filter))

    query += """
        GROUP BY
            s.id, s.session_date, s.status, s.dismiss_reason,
            s.opened_at, s.closed_at, s.start_time, s.end_time,
            s.camera_id, s.rtsp_url, s.session_type,
            sub.code, sub.name,
            u.full_name, su.full_name, cam.name
        ORDER BY s.start_time
    """

    # ── FIX: Always run the query, not only when section_id exists ──
    show_sessions = bool(section_id)
    sessions_list = []

    if show_sessions:
        cursor.execute(query, tuple(params))
        sessions_list = cursor.fetchall()

    # Convert timedelta → string AND compute live_status
    for s in sessions_list:
        s["start_time"] = _td_to_str(s.get("start_time"))
        s["end_time"]   = _td_to_str(s.get("end_time"))

        # ── FIX: compute live_status so template can use it ──────
        try:
            sess_date  = s["session_date"]
            t_start    = _str_to_time(s["start_time"])
            t_end      = _str_to_time(s["end_time"])

            s["live_status"] = _compute_status(
                sess_date,
                t_start,
                t_end,
                s["status"]
            )

            # ── AUTO SYNC DATABASE STATUS ─────────────────
            if s["live_status"] != s["status"]:

                update_cursor = get_cursor()

                update_cursor.execute("""
                    UPDATE sessions
                    SET status = %s
                    WHERE id = %s
                """, (
                    s["live_status"],
                    s["id"]
                ))

                get_db().commit()

                s["status"] = s["live_status"]

        except Exception:
            s["live_status"] = s["status"]

    # ── Filter dropdowns ────────────────────────────────────────
    cursor.execute("""
        SELECT sec.id, sec.section_label, d.code AS dept_code, d.id AS dept_id
        FROM   sections    sec
        JOIN   departments d   ON d.id = sec.department_id
        JOIN   academic_periods ap ON ap.id = sec.academic_period_id
        WHERE  ap.is_active = 1
        ORDER  BY d.code, sec.section_label
    """)
    sections = cursor.fetchall()

    cursor.execute("SELECT id, code, name FROM departments ORDER BY name")
    departments = cursor.fetchall()

    # ── Department overview ─────────────────────────────────────
    cursor.execute("""
    SELECT
        d.id,
        d.name,
        COUNT(DISTINCT s.id) AS session_count

    FROM departments d

    LEFT JOIN sections sec
        ON sec.department_id = d.id

    LEFT JOIN sessions s
        ON s.section_id = sec.id
        AND s.session_date = %s

    GROUP BY d.id,d.name

    ORDER BY d.name
    """, (date_filter,))
    department_overview = cursor.fetchall()

    # ── Semester overview ───────────────────────────────────────
    semester_query = """
    SELECT

        sec.sem_number,

        COUNT(DISTINCT s.id) AS session_count

    FROM sections sec

    LEFT JOIN sessions s
        ON s.section_id = sec.id
        AND s.session_date=%s

    WHERE 1=1
    """

    params=[date_filter]

    if dept_id:
        semester_query+=" AND sec.department_id=%s"
        params.append(dept_id)

    semester_query+="""
    GROUP BY sec.sem_number
    ORDER BY sec.sem_number
    """

    cursor.execute(semester_query,tuple(params))
    semester_overview=cursor.fetchall()

    # ── Section overview ────────────────────────────────────────
    section_query="""
    SELECT

        sec.id,

        sec.section_label AS name,

        COUNT(DISTINCT s.id) AS session_count

    FROM sections sec

    LEFT JOIN sessions s
        ON s.section_id=sec.id
        AND s.session_date=%s

    WHERE 1=1
    """

    params=[date_filter]

    if dept_id:
        section_query+=" AND sec.department_id=%s"
        params.append(dept_id)

    if semester:
        section_query+=" AND sec.sem_number=%s"
        params.append(semester)

    section_query+="""
    GROUP BY sec.id,sec.section_label
    ORDER BY sec.section_label
    """

    cursor.execute(section_query,tuple(params))
    section_overview=cursor.fetchall()

    # ── Cameras ─────────────────────────────────────────────────
    cursor.execute("""
        SELECT id, name, rtsp_url, location, notes, assigned_section_id
        FROM   cameras
        WHERE  status = 'active'
        ORDER  BY name
    """)
    cameras = cursor.fetchall()

    # ── Status counters ─────────────────────────────────────────
    # Build dynamic WHERE for counters matching active filters
    counter_where  = ["s.session_date = %s"]
    counter_params = [date_filter]

    if dept_filter:
        counter_where.append("""
            EXISTS(
            SELECT 1
            FROM sections sec2
            WHERE sec2.id=s.section_id
            AND sec2.department_id=%s
            )
        """)
        counter_params.append(int(dept_filter))

    if semester:
        counter_where.append("""
            EXISTS(
            SELECT 1
            FROM sections sec2
            WHERE sec2.id=s.section_id
            AND sec2.sem_number=%s
            )
        """)
        counter_params.append(semester)

    if section_id:
        counter_where.append("""
            s.section_id = %s
        """)
        counter_params.append(section_id)

    counter_sql = "WHERE " + " AND ".join(counter_where)

    cursor.execute(f"""
        SELECT status, COUNT(*) AS total
        FROM   sessions s
        {counter_sql}
        GROUP  BY status
    """, tuple(counter_params))

    status_rows     = cursor.fetchall()
    total_count     = sum(r["total"] for r in status_rows)
    active_count    = next((r["total"] for r in status_rows if r["status"] == "active"),    0)
    completed_count = next((r["total"] for r in status_rows if r["status"] == "completed"), 0)
    scheduled_count = next((r["total"] for r in status_rows if r["status"] == "scheduled"), 0)
    return render_template(
        "admin/sessions.html",

        sessions=sessions_list,
        show_sessions=show_sessions,

        sections=sections,
        departments=departments,
        cameras=cameras,

        total_count=total_count,
        active_count=active_count,
        completed_count=completed_count,
        scheduled_count=scheduled_count,

        department_overview=department_overview,
        semester_overview=semester_overview,
        section_overview=section_overview,

        selected_department=dept_id,
        selected_semester=semester,
        selected_section=section_id,

        filters={
            "date": date_filter,
            "section_id":  section_id,
            "status": status_filter,
            "dept_id": dept_filter
        }
    )
# ─────────────────────────────────────────────────────────────
# ROUTE 6 — API: Fetch sections (AJAX, for dynamic UI)
# ─────────────────────────────────────────────────────────────

@sessions_bp.route("/api/sections")
@login_required
def api_sections():
    """
    GET /admin/sessions/api/sections
    Query params: dept_id (int, repeatable), semester (int, repeatable)
    Returns JSON list of matching sections the current user may target.
    Used by the multi-select UI to dynamically filter sections.
    """
    faculty_id = _current_faculty_id()
    all_sections = _get_sections_for_role(faculty_id)

    dept_ids  = request.args.getlist("dept_id",  type=int)
    semesters = request.args.getlist("semester",  type=int)

    filtered = all_sections
    if dept_ids:
        filtered = [s for s in filtered if s["department_id"] in dept_ids]
    if semesters:
        filtered = [s for s in filtered if s["sem_number"] in semesters]

    return jsonify([
        {
            "section_id":    r["section_id"],
            "section_label": r["section_label"],
            "sem_number":    r["sem_number"],
            "dept_id":       r["department_id"],
            "dept_code":     r["dept_code"],
            "label":         f"{r['dept_code']} Sem {r['sem_number']} - {r['section_label']}",
        }
        for r in filtered
    ])


# ─────────────────────────────────────────────────────────────
# ROUTE 7 — API: Dashboard counter (AJAX refresh)
# ─────────────────────────────────────────────────────────────

@sessions_bp.route("/api/counters")
@login_required
@admin_required
def api_counters():
    """
    GET /admin/sessions/api/counters
    Returns live status counters for dashboard cards.
    Supports same filters as dashboard view.
    """
    cursor = get_cursor()
    dept_id    = request.args.get("dept_id",    type=int)
    semester   = request.args.get("semester",   type=int)
    section_id = request.args.get("section_id", type=int)
    today      = date.today().isoformat()

    wheres = ["s.session_date = %s"]
    params = [today]
    if dept_id:
        wheres.append("d.id = %s"); params.append(dept_id)
    if semester:
        wheres.append("sec.sem_number = %s"); params.append(semester)
    if section_id:
        wheres.append("ss.section_id = %s"); params.append(section_id)

    where_sql = "WHERE " + " AND ".join(wheres)

    cursor.execute(f"""
        SELECT
            COUNT(DISTINCT s.id)                        AS total,
            SUM(s.status = 'active')                    AS active,
            SUM(s.status = 'scheduled')                 AS scheduled,
            SUM(s.status = 'completed')                 AS completed,
            SUM(s.status IN ('dismissed','cancelled'))  AS cancelled
        FROM      sessions         s
        JOIN      session_sections ss  ON ss.session_id  = s.id
        JOIN      sections         sec ON sec.id          = ss.section_id
        JOIN      departments      d   ON d.id            = sec.department_id
        {where_sql}
    """, params)
    row = cursor.fetchone()
    return jsonify(row or {})


# ─────────────────────────────────────────────────────────────
# APScheduler job — auto-update session status every 5 min
# Register this in app.py:
#   from api.admin.sessions import job_update_session_statuses
#   scheduler.add_job(job_update_session_statuses, 'interval', minutes=5)
# ─────────────────────────────────────────────────────────────

def job_update_session_statuses():
    """
    Background job: auto-advance session statuses based on clock time.
    Only updates non-terminal states.
    """
    from app import create_app
    app = create_app()
    with app.app_context():
        try:
            conn = get_db()
            cur  = conn.cursor()
            cur.execute("CALL compute_session_status()")
            conn.commit()
            logger.info("session status update job ran OK")
        except Exception as exc:
            logger.error("session status job failed: %s", exc)

