"""
attendance_system/api/admin/timetable.py

Timetable management: view, CSV/PDF upload, conflict detection, holiday calendar.

KEY DESIGN DECISIONS:
- Accepts CSV and PDF timetable uploads.
- PDF text is extracted and parsed into structured rows.
- Missing subjects/faculty are AUTO-CREATED (no placeholders, no needs_review).
- Unique constraint on (section_id, day_of_week, start_time, academic_period_id)
  prevents duplicates at database level.
- INSERT ... ON DUPLICATE KEY UPDATE performs true UPSERT.
- Slots removed from CSV/PDF are soft-deleted (is_active=0), never hard-deleted,
  so sessions/attendance foreign keys remain intact.
- Hard delete only when no sessions reference the slot.

SCHEMA NOTE: `users` has no username column — login identifier is `email`.
`faculty` has no username/email column of its own — it links to `users`
via `user_id`. faculty.faculty_code is UNIQUE. users.email is UNIQUE.
"""

import io
import re
import csv
import logging
from datetime import datetime
from auth.helpers import hash_password

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, flash, jsonify
)
from flask_login import current_user

from core.db import get_db, get_cursor
from auth.helpers import admin_required, log_audit, get_client_ip

# ── Optional PDF support ─────────────────────────────────────────────────────
try:
    import pdfplumber
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

logger = logging.getLogger(__name__)

timetable_bp = Blueprint(
    "timetable", __name__,
    template_folder="../../templates/admin"
)

VALID_DAYS = [
    "Monday", "Tuesday", "Wednesday",
    "Thursday", "Friday", "Saturday"
]
VALID_SLOT_TYPES = ["Theory", "Lab", "Lab-Merged", "Interval", "Lunch"]

# ── Day aliases for flexible PDF/CSV parsing ─────────────────────────────────
DAY_ALIASES = {
    "MON": "Monday",   "TUE": "Tuesday",  "WED": "Wednesday",
    "THU": "Thursday", "FRI": "Friday",   "SAT": "Saturday",
    "MONDAY": "Monday","TUESDAY": "Tuesday","WEDNESDAY": "Wednesday",
    "THURSDAY": "Thursday","FRIDAY": "Friday","SATURDAY": "Saturday",
}

_GENERIC_TOKENS = {"LAB FACULTY", "TBA", "TBD", "-", "N/A", ""}

# Default temp password for auto-created faculty accounts.
# Only the hash is ever stored (users.password_hash).
DEFAULT_TEMP_PASSWORD = "Change@123"

# Status to assign newly auto-created faculty `users` rows.
# users.status enum = 'pending','active','rejected','suspended' (default 'pending').
# Set to 'active' so these accounts immediately satisfy the
# `WHERE u.status = 'active'` filters already used elsewhere in this file
# (e.g. section_data_api, get_or_create_faculty's own lookup query).
# Change to 'pending' if you want admin to manually approve auto-created
# faculty before they count as active.
AUTO_FACULTY_STATUS = "active"


# ─────────────────────────────────────────────────────────────────────────────
# TIMETABLE VIEW  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

@timetable_bp.route("/")
@admin_required
def view_timetable():

    department_id = request.args.get("department_id")
    sem_number = request.args.get("sem_number")

    cursor = get_cursor()

    cursor.execute("""
    SELECT
        d.id,
        d.code,
        d.name,
        COUNT(sec.id) AS section_count
    FROM departments d
    LEFT JOIN sections sec
        ON sec.department_id = d.id
    GROUP BY d.id
    ORDER BY d.name
    """)

    departments = cursor.fetchall()

    if department_id:

        cursor.execute("""
            SELECT
                sec.sem_number,
                COUNT(*) AS total
            FROM sections sec
            WHERE sec.department_id = %s
            GROUP BY sec.sem_number
            ORDER BY sec.sem_number
        """, (department_id,))

    else:

        cursor.execute("""
            SELECT
                sec.sem_number,
                COUNT(*) AS total
            FROM sections sec
            GROUP BY sec.sem_number
            ORDER BY sec.sem_number
        """)

    semester_cards = cursor.fetchall()

    

    query = """
    SELECT

        sec.id,

        sec.section_label,

        sec.sem_number,

        d.code AS dept_code,

        b.label AS batch_label,

        c.cluster_name,

        COUNT(DISTINCT tt.id) AS slot_count,

        COUNT(DISTINCT ss.subject_id) AS subject_count

    FROM sections sec

    JOIN departments d
    ON d.id = sec.department_id

    JOIN batches b
    ON b.id = sec.batch_id

    LEFT JOIN cluster_sections cs
    ON cs.section_id = sec.id

    LEFT JOIN clusters c
    ON c.id = cs.cluster_id

    LEFT JOIN timetable tt
    ON tt.section_id = sec.id
    AND tt.is_active = 1

    LEFT JOIN section_subjects ss
    ON ss.section_id = sec.id

    WHERE 1=1
    """

    params = []

    if department_id:
        query += " AND sec.department_id=%s"
        params.append(department_id)

    if sem_number:
        query += " AND sec.sem_number=%s"
        params.append(sem_number)

    query += """
    GROUP BY
        sec.id,
        sec.section_label,
        sec.sem_number,
        d.code,
        b.label,
        c.cluster_name

    ORDER BY
        sec.sem_number,
        d.code,
        sec.section_label
    """

    cursor.execute(query, params)

    section_cards = cursor.fetchall()

    selected_section = request.args.get("section_id", "")
    timetable_data   = {}
    section_info     = None

    if selected_section:
        cursor.execute(
            """
            SELECT sec.*, d.code AS dept_code, d.name AS dept_name
            FROM sections sec
            JOIN departments d ON d.id = sec.department_id
            WHERE sec.id = %s
            """,
            (int(selected_section),)
        )
        section_info = cursor.fetchone()

        cursor.execute(
            """
            SELECT
                t.*,
                sub.code      AS subject_code,
                sub.name      AS subject_name,
                u.full_name   AS faculty_name
            FROM timetable t
            LEFT JOIN subjects sub ON sub.id = t.subject_id
            LEFT JOIN faculty  f   ON f.id   = t.faculty_id
            LEFT JOIN users    u   ON u.id   = f.user_id
            WHERE t.section_id = %s
            ORDER BY
                FIELD(t.day_of_week,
                    'Monday','Tuesday','Wednesday',
                    'Thursday','Friday','Saturday'),
                t.start_time
            """,
            (int(selected_section),)
        )
        slots = cursor.fetchall()

        for slot in slots:
            slot["start_time"] = _td_to_str(slot["start_time"])
            slot["end_time"]   = _td_to_str(slot["end_time"])

        for day in VALID_DAYS:
            timetable_data[day] = [s for s in slots if s["day_of_week"] == day]

    now          = datetime.now()
    current_day  = now.strftime("%A")
    current_time = now.strftime("%H:%M")

    return render_template(
        "admin/timetable.html",

        departments=departments,

        semester_cards=semester_cards,

        section_cards=section_cards,

        selected_department=department_id,

        selected_sem=sem_number,

        selected_section=selected_section,

        section_info=section_info,

        timetable_data=timetable_data,

        days=VALID_DAYS,

        current_day=current_day,

        current_time=current_time
    )


# ─────────────────────────────────────────────────────────────────────────────
# ADD SINGLE SLOT  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

@timetable_bp.route("/add-slot", methods=["POST"])
@admin_required
def add_slot():
    section_id  = request.form.get("section_id",  "").strip()
    subject_id  = request.form.get("subject_id",  "").strip() or None
    faculty_id  = request.form.get("faculty_id",  "").strip() or None
    day_of_week = request.form.get("day_of_week", "").strip()
    start_time  = request.form.get("start_time",  "").strip()
    end_time    = request.form.get("end_time",     "").strip()
    room        = request.form.get("room",         "").strip() or None
    slot_type   = request.form.get("slot_type",    "Theory").strip()

    if not all([section_id, day_of_week, start_time, end_time]):
        flash("Section, day, start time, and end time are required.", "danger")
        return redirect(url_for("timetable.view_timetable", section_id=section_id))

    if day_of_week not in VALID_DAYS:
        flash(f"Invalid day: {day_of_week}", "danger")
        return redirect(url_for("timetable.view_timetable", section_id=section_id))

    if slot_type not in VALID_SLOT_TYPES:
        flash(f"Invalid slot type: {slot_type}", "danger")
        return redirect(url_for("timetable.view_timetable", section_id=section_id))

    conn = cursor = None
    try:
        conn   = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)

        cursor.execute("SELECT id FROM academic_periods WHERE is_active = 1 LIMIT 1")
        period = cursor.fetchone()
        if not period:
            flash("No active academic period.", "danger")
            return redirect(url_for("timetable.view_timetable"))
        period_id = period["id"]

        cursor.execute(
            """
            INSERT INTO timetable
                (section_id, subject_id, faculty_id, day_of_week,
                 start_time, end_time, room, slot_type,
                 academic_period_id, is_active, needs_review, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,0,NOW())
            ON DUPLICATE KEY UPDATE
                subject_id   = VALUES(subject_id),
                faculty_id   = VALUES(faculty_id),
                end_time     = VALUES(end_time),
                room         = VALUES(room),
                slot_type    = VALUES(slot_type),
                is_active    = 1,
                needs_review = 0,
                updated_at   = NOW()
            """,
            (
                int(section_id),
                int(subject_id) if subject_id else None,
                int(faculty_id) if faculty_id else None,
                day_of_week, start_time, end_time,
                room, slot_type, period_id,
            )
        )
        if subject_id and faculty_id:
            _upsert_section_subject(
                cursor, int(section_id),
                int(subject_id), int(faculty_id), period_id
            )

        conn.commit()
        flash("Timetable slot saved.", "success")

    except Exception as exc:
        if conn:
            conn.rollback()
        logger.exception("add_slot error")
        flash(f"Error saving slot: {exc}", "danger")

    return redirect(url_for("timetable.view_timetable", section_id=section_id))


# ─────────────────────────────────────────────────────────────────────────────
# DELETE SLOT  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

@timetable_bp.route("/delete-slot/<int:slot_id>", methods=["POST"])
@admin_required
def delete_slot(slot_id):
    section_id = request.form.get("section_id", "")
    conn = cursor = None
    try:
        conn   = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)

        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM sessions WHERE timetable_id = %s",
            (slot_id,)
        )
        linked = cursor.fetchone()["cnt"]

        if linked == 0:
            cursor.execute("DELETE FROM timetable WHERE id = %s", (slot_id,))
            flash("Slot permanently deleted.", "success")
        else:
            cursor.execute(
                "UPDATE timetable SET is_active = 0, updated_at = NOW() WHERE id = %s",
                (slot_id,)
            )
            flash(
                f"Slot deactivated (has {linked} linked session(s) — "
                "data preserved).",
                "warning"
            )

        conn.commit()

    except Exception as exc:
        if conn:
            conn.rollback()
        logger.exception("delete_slot error")
        flash(f"Error deleting slot: {exc}", "danger")

    return redirect(url_for("timetable.view_timetable", section_id=section_id))


# ─────────────────────────────────────────────────────────────────────────────
# NOTE: mark_reviewed route removed — the needs_review/placeholder workflow
# no longer exists. If your timetable.html template still has a button
# posting to /mark-reviewed/<id>, remove that button (search the template
# for "mark-reviewed" or "needs_review").
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# TEXT / NAME NORMALIZATION HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def normalize_text(text):
    """
    Normalize generic text for comparison (subjects, codes).
    'Cloud  Computing' -> 'cloudcomputing'
    """
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r'[^a-z0-9]', '', text)
    return text


def normalize_faculty_name(name):
    """
    Normalize a faculty name for matching purposes:
    - lowercase
    - strip common honorifics (Dr, Prof, Mr, Mrs, Ms, Mx) with or without a dot
    - strip remaining punctuation (dots, commas, etc.)
    - collapse whitespace

    'Dr. A. Sharma'  -> 'a sharma'
    'PROF Sharma A'  -> 'sharma a'
    'Mrs Asha K.'    -> 'asha k'

    Note: token order is preserved on purpose — 'Sharma A' and 'A Sharma'
    are NOT treated as equal, since reordering risks merging two different
    people who share a surname.
    """
    if not name:
        return ""
    name = name.strip().lower()
    name = re.sub(r'\b(dr|prof|mr|mrs|ms|mx)\.?\b', '', name)
    name = re.sub(r'[^a-z\s]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


# ─────────────────────────────────────────────────────────────────────────────
# SUBJECT RESOLUTION  (single canonical version)
# subjects columns used: id, code, name, department_id, sem_number,
#                        subject_type, credits, created_at
# ─────────────────────────────────────────────────────────────────────────────

import difflib

# ─────────────────────────────────────────────────────────────────────────────
# ALIAS-AWARE SUBJECT RESOLUTION
# Resolution order, each step scoped to (department_id, sem_number) unless noted:
#   1. batch cache (this upload only)
#   2. exact subjects.code match                              -> instant, global
#   3. exact subject_aliases.alias_normalized match            -> instant, global
#      (admin-curated, e.g. "OS" = Operating Systems college-wide)
#   4. exact normalized name match                             -> scoped
#   5. acronym match (CC -> Cloud Computing)                   -> scoped
#   6. fuzzy similarity match                                  -> scoped
#   7. create new subject + auto-register its own aliases
# Steps 5 and 6 never silently merge on ambiguity — they either auto-apply
# (single confident candidate) or drop into subject_match_review for a
# human to confirm once. Once confirmed, it's promoted to subject_aliases
# so the exact same input is instant (step 3) forever after.
# ─────────────────────────────────────────────────────────────────────────────

ACRONYM_MAX_LEN = 6          # "CC","ML","DBMS","OOPS" etc — longer inputs aren't acronym candidates
FUZZY_MATCH_THRESHOLD = 0.84
FUZZY_MARGIN_OVER_RUNNER_UP = 0.08   # best must beat 2nd-best by this much to auto-apply


def build_acronym(name: str) -> str:
    """
    'Cloud Computing'   -> 'cc'
    'CloudComputing'    -> 'cc'   (splits on internal capitalization too)
    'cloud_computing'   -> 'cc'
    'Machine Learning'  -> 'ml'
    'Artificial Neural Network' -> 'ann'
    """
    if not name:
        return ""
    # Split on whitespace/underscore/hyphen first
    rough_words = re.split(r'[\s_\-]+', name.strip())
    words = []
    for w in rough_words:
        if not w:
            continue
        # Also split CamelCase ('CloudComputing' -> ['Cloud','Computing'])
        camel_parts = re.findall(r'[A-Z]?[a-z0-9]+|[A-Z]+(?=[A-Z]|$)', w)
        words.extend(camel_parts if camel_parts else [w])
    return ''.join(w[0] for w in words if w).lower()


def _candidate_subjects_in_scope(cursor, department_id, semester):
    """All subjects for this dept+sem, used by acronym/fuzzy matching."""
    cursor.execute(
        "SELECT id, code, name FROM subjects WHERE department_id = %s AND sem_number = %s",
        (department_id, semester)
    )
    return cursor.fetchall()


def _register_alias(cursor, subject_id, raw_text, alias_type, confidence, created_by=None):
    norm = normalize_text(raw_text)
    if not norm:
        return
    cursor.execute(
        "SELECT id FROM subject_aliases WHERE alias_normalized = %s LIMIT 1", (norm,)
    )
    if cursor.fetchone():
        return  # already registered (possibly to a different subject — never overwrite silently)
    try:
        cursor.execute(
            """
            INSERT INTO subject_aliases
                (subject_id, alias_raw, alias_normalized, alias_type, confidence, created_by, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            """,
            (subject_id, raw_text[:150], norm[:150], alias_type, confidence, created_by)
        )
    except Exception:
        pass  # race on the UNIQUE key — harmless, another row already covers it


def get_or_create_subject(cursor, subject_input, department_id, semester,
                           created_subjects_cache, created_by=None):
    subject_input = (subject_input or "").strip()
    if not subject_input:
        return None, False, "Subject is empty."

    cache_key = normalize_text(subject_input)
    if cache_key in created_subjects_cache:
        return created_subjects_cache[cache_key], False, None

    # ── 2. Exact code match ──────────────────────────────────────────────
    cursor.execute(
        "SELECT id FROM subjects WHERE UPPER(code) = UPPER(%s) LIMIT 1",
        (subject_input,)
    )
    row = cursor.fetchone()
    if row:
        created_subjects_cache[cache_key] = row["id"]
        return row["id"], False, None

    # ── 3. Exact alias match (global, admin-curated + previously learned) ──
    cursor.execute(
        "SELECT subject_id FROM subject_aliases WHERE alias_normalized = %s LIMIT 1",
        (cache_key,)
    )
    row = cursor.fetchone()
    if row:
        created_subjects_cache[cache_key] = row["subject_id"]
        return row["subject_id"], False, None

    candidates = _candidate_subjects_in_scope(cursor, department_id, semester)

    # ── 4. Exact normalized name match (scoped) ─────────────────────────
    for r in candidates:
        if normalize_text(r["name"]) == cache_key:
            created_subjects_cache[cache_key] = r["id"]
            _register_alias(cursor, r["id"], subject_input, "name_variant", "exact", created_by)
            return r["id"], False, None

    # ── 5. Acronym match (scoped) ───────────────────────────────────────
    # Only treat the input as a possible acronym if it's short and has no
    # spaces — a full subject name typed differently shouldn't be forced
    # through acronym logic.
    if len(cache_key) <= ACRONYM_MAX_LEN and re.match(r'^[a-z0-9]+$', cache_key):
        acronym_hits = [r for r in candidates if build_acronym(r["name"]) == cache_key]
        if len(acronym_hits) == 1:
            match = acronym_hits[0]
            created_subjects_cache[cache_key] = match["id"]
            _register_alias(cursor, match["id"], subject_input, "acronym", "high", created_by)
            return match["id"], False, None
        elif len(acronym_hits) > 1:
            # Ambiguous acronym (e.g. "DS" = Data Structures or Data Science
            # in the same dept/sem) — don't guess, flag for admin.
            for m in acronym_hits:
                cursor.execute(
                    """
                    INSERT INTO subject_match_review
                        (raw_input, candidate_subject_id, match_score, match_reason,
                         department_id, sem_number, status, created_at)
                    VALUES (%s, %s, %s, 'ambiguous_acronym', %s, %s, 'pending', NOW())
                    """,
                    (subject_input, m["id"], 1.0, department_id, semester)
                )
            warning = (
                f"'{subject_input}' matches multiple subjects by acronym "
                f"({', '.join(m['name'] for m in acronym_hits)}) — flagged for "
                f"admin review under Subject Match Review. A NEW subject was NOT created."
            )
            # Fall through to create-new below would cause the exact duplicate
            # problem we're fixing — instead, park it: create nothing, return
            # None so the row is skipped, not silently duplicated.
            return None, False, warning

    # ── 6. Fuzzy similarity match (scoped) ──────────────────────────────
    scored = []
    for r in candidates:
        ratio = difflib.SequenceMatcher(None, cache_key, normalize_text(r["name"])).ratio()
        scored.append((ratio, r))
    scored.sort(key=lambda x: x[0], reverse=True)

    if scored and scored[0][0] >= FUZZY_MATCH_THRESHOLD:
        best_score, best = scored[0]
        runner_up_score = scored[1][0] if len(scored) > 1 else 0.0
        if best_score - runner_up_score >= FUZZY_MARGIN_OVER_RUNNER_UP:
            # Confident enough to auto-apply, but still logged for audit/undo.
            created_subjects_cache[cache_key] = best["id"]
            _register_alias(cursor, best["id"], subject_input, "name_variant", "review", created_by)
            cursor.execute(
                """
                INSERT INTO subject_match_review
                    (raw_input, candidate_subject_id, match_score, match_reason,
                     department_id, sem_number, status, resolved_subject_id,
                     reviewed_at, created_at)
                VALUES (%s, %s, %s, 'fuzzy', %s, %s, 'approved', %s, NOW(), NOW())
                """,
                (subject_input, best["id"], best_score, department_id, semester, best["id"])
            )
            return best["id"], False, (
                f"'{subject_input}' auto-matched to existing subject "
                f"'{best['name']}' by similarity ({best_score:.0%}) — verify in Subject Match Review."
            )
        else:
            # Too close between top 2 candidates to guess safely.
            cursor.execute(
                """
                INSERT INTO subject_match_review
                    (raw_input, candidate_subject_id, match_score, match_reason,
                     department_id, sem_number, status, created_at)
                VALUES (%s, %s, %s, 'ambiguous_fuzzy', %s, %s, 'pending', NOW())
                """,
                (subject_input, best["id"], best_score, department_id, semester)
            )
            return None, False, (
                f"'{subject_input}' is ambiguously similar to multiple existing subjects "
                f"— flagged for admin review, no new subject created."
            )

    # ── 7. Create new subject, register its own aliases up front ───────
    subject_code = subject_input.upper().replace(" ", "_")[:30]
    cursor.execute(
        "SELECT id FROM subjects WHERE UPPER(code) = UPPER(%s) LIMIT 1", (subject_code,)
    )
    existing = cursor.fetchone()
    if existing:
        created_subjects_cache[cache_key] = existing["id"]
        return existing["id"], False, None

    display_name = subject_input.title()[:150]
    cursor.execute(
        """
        INSERT INTO subjects
            (code, name, department_id, sem_number, credits, subject_type, offering_mode, created_at)
        VALUES (%s, %s, %s, %s, 0, 'Theory', 'regular', NOW())
        """,
        (subject_code, display_name, department_id, semester)
    )
    new_id = cursor.lastrowid
    created_subjects_cache[cache_key] = new_id

    # Pre-register the code, the display name, and its own acronym so the
    # *next* CSV that uses any variant of this subject hits step 2/3 instantly.
    _register_alias(cursor, new_id, subject_code, "code", "exact", created_by)
    _register_alias(cursor, new_id, display_name, "name_variant", "exact", created_by)
    auto_acr = build_acronym(display_name)
    if auto_acr and len(auto_acr) >= 2:
        _register_alias(cursor, new_id, auto_acr, "acronym", "exact", created_by)

    return new_id, True, None


# ─────────────────────────────────────────────────────────────────────────────
# FACULTY RESOLUTION
# users columns used:   id, email, password_hash, full_name, role, status,
#                       department_id  (NO username column exists)
# faculty columns used: id, user_id, faculty_code, department_id
# ─────────────────────────────────────────────────────────────────────────────

def get_or_create_faculty(cursor, fac_input, department_id, dept_code,
                           created_faculty_cache, credentials_log):
    """
    Resolve a faculty member by faculty_code, then by normalized name,
    then create a new `users` row + linked `faculty` row.

    Args:
        cursor: open DB cursor (dictionary=True)
        fac_input: raw faculty_code/name string from the row
        department_id: department of the CURRENT SECTION (stored on both
            users.department_id and faculty.department_id)
        dept_code: department code string (e.g. 'CSE'), used as the
            faculty_code prefix -> 'CSE001'
        created_faculty_cache: dict shared across the whole upload batch,
            keyed by normalize_faculty_name(...) -> faculty_id
        credentials_log: list this function APPENDS a dict to for every
            NEWLY CREATED faculty member (used to build the credentials CSV)

    Returns:
        (faculty_id, created: bool, warning: str | None)

    Generic tokens (TBA, TBD, '-', '') return (None, False, None) — no
    faculty is created or expected for breaks / unspecified rows.
    """
    fac_input = (fac_input or "").strip()
    if fac_input.upper() in _GENERIC_TOKENS:
        return None, False, None

    norm_target = normalize_faculty_name(fac_input)
    if not norm_target:
        return None, False, None

    if norm_target in created_faculty_cache:
        return created_faculty_cache[norm_target], False, None

    # 1. Exact match by faculty_code (faculty.faculty_code is UNIQUE)
    cursor.execute(
        "SELECT id FROM faculty WHERE UPPER(faculty_code) = UPPER(%s) LIMIT 1",
        (fac_input,)
    )
    row = cursor.fetchone()
    if row:
        created_faculty_cache[norm_target] = row["id"]
        return row["id"], False, None

    # 2. Normalized full-name match against users.full_name via faculty.user_id
    cursor.execute(
        """
        SELECT f.id, u.full_name
        FROM faculty f
        JOIN users u ON u.id = f.user_id
        WHERE u.status = 'active'
        """
    )
    for r in cursor.fetchall():
        if normalize_faculty_name(r["full_name"]) == norm_target:
            created_faculty_cache[norm_target] = r["id"]
            return r["id"], False, None

    # 3. Not found anywhere — create users row + faculty row.
    faculty_code  = _generate_faculty_code(cursor, dept_code)
    # users.email is UNIQUE and NOT NULL — there is no username column,
    # so the generated login identifier IS the email address.
    login_local_part = faculty_code.lower()
    email          = f"{login_local_part}@college.local"

    # Guard against an email collision (extremely unlikely given
    # faculty_code is itself unique, but cheap to check).
    cursor.execute("SELECT id FROM users WHERE email = %s LIMIT 1", (email,))
    if cursor.fetchone():
        # Extremely rare collision path — append a short disambiguator.
        email = f"{login_local_part}.{department_id}@college.local"

    temp_password  = DEFAULT_TEMP_PASSWORD
    password_hash = hash_password(temp_password)
    display_name   = fac_input.strip().title()

    cursor.execute(
        """
        INSERT INTO users
            (email, password_hash, full_name, role, status,
             department_id, created_at)
        VALUES (%s, %s, %s, 'faculty', %s, %s, NOW())
        """,
        (email, password_hash, display_name, AUTO_FACULTY_STATUS, department_id)
    )
    user_id = cursor.lastrowid

    cursor.execute(
        """
        INSERT INTO faculty
            (user_id, faculty_code, department_id, created_at)
        VALUES (%s, %s, %s, NOW())
        """,
        (user_id, faculty_code, department_id)
    )
    faculty_id = cursor.lastrowid

    created_faculty_cache[norm_target] = faculty_id

    credentials_log.append({
        "faculty_name":   display_name,
        "faculty_code":   faculty_code,
        "login_email":    email,   # this IS the login identifier (no username column exists)
        "temp_password":  temp_password,
    })

    return faculty_id, True, None


def _generate_faculty_code(cursor, dept_code):
    """
    Generate the next sequential faculty code for a department, e.g. CSE001.
    faculty.faculty_code is varchar(30) UNIQUE.
    """
    prefix = re.sub(r'[^A-Za-z0-9]', '', (dept_code or "GEN")).upper()

    cursor.execute(
        """
        SELECT faculty_code FROM faculty
        WHERE faculty_code LIKE %s
        ORDER BY CAST(SUBSTRING(faculty_code, %s) AS UNSIGNED) DESC
        LIMIT 1
        """,
        (f"{prefix}%", len(prefix) + 1)
    )
    row = cursor.fetchone()
    if row and row["faculty_code"]:
        m = re.search(r'(\d+)$', row["faculty_code"])
        next_num = int(m.group(1)) + 1 if m else 1
    else:
        next_num = 1

    code = f"{prefix}{next_num:03d}"

    # Final safety check against the UNIQUE constraint in case of gaps/races.
    cursor.execute("SELECT id FROM faculty WHERE faculty_code = %s LIMIT 1", (code,))
    while cursor.fetchone():
        next_num += 1
        code = f"{prefix}{next_num:03d}"
        cursor.execute("SELECT id FROM faculty WHERE faculty_code = %s LIMIT 1", (code,))

    return code[:30]  # respect varchar(30)


# ─────────────────────────────────────────────────────────────────────────────
# CSV / PDF UPLOAD  (unified entry point — routing/template unchanged,
# only the summary message reflects the new report shape)
# ─────────────────────────────────────────────────────────────────────────────

@timetable_bp.route("/upload", methods=["GET", "POST"])
@admin_required
def upload_timetable():
    cursor = get_cursor()
    cursor.execute("""
    SELECT
        sec.id,

        d.code AS dept,

        sec.sem_number,

        sec.section_label,

        b.label AS batch

    FROM sections sec

    JOIN departments d
        ON d.id = sec.department_id

    JOIN batches b
        ON b.id = sec.batch_id

    ORDER BY
        d.code,
        sec.sem_number,
        sec.section_label
    """)

    sections = cursor.fetchall()

    if request.method == "POST":
        section_id     = request.form.get("section_id",     "").strip()
        clear_existing = request.form.get("clear_existing") == "on"

        if not section_id:
            flash("Please select a section.", "danger")
            return render_template("admin/timetable_upload.html",
                                   sections=sections, pdf_support=PDF_SUPPORT)

        uploaded_file = request.files.get("timetable_file")
        if not uploaded_file or not uploaded_file.filename:
            flash("No file selected.", "danger")
            return render_template("admin/timetable_upload.html",
                                   sections=sections, pdf_support=PDF_SUPPORT)

        filename  = uploaded_file.filename.lower()
        file_data = uploaded_file.read()

        try:
            if filename.endswith(".csv"):
                rows = _parse_csv(file_data)
            elif filename.endswith(".pdf"):
                if not PDF_SUPPORT:
                    flash(
                        "PDF support is not installed. "
                        "Run: pip install pdfplumber",
                        "danger"
                    )
                    return render_template("admin/timetable_upload.html",
                                           sections=sections,
                                           pdf_support=PDF_SUPPORT)
                rows = _parse_pdf(file_data)
            else:
                flash("Only .csv and .pdf files are accepted.", "danger")
                return render_template("admin/timetable_upload.html",
                                       sections=sections, pdf_support=PDF_SUPPORT)

        except Exception as exc:
            logger.exception("File parsing error")
            flash(f"Could not parse file: {exc}", "danger")
            return render_template("admin/timetable_upload.html",
                                   sections=sections, pdf_support=PDF_SUPPORT)

        if not rows:
            flash("No timetable rows could be extracted from the file.", "warning")
            return render_template("admin/timetable_upload.html",
                                   sections=sections, pdf_support=PDF_SUPPORT)

        # ── Process the normalised rows ──────────────────────────────────────
        result = _process_rows(int(section_id), rows, clear_existing)

        # ── Flash summary (new report shape — no placeholders) ───────────────
        if result["success"]:
            flash(
                f"Upload complete — "
                f"Subjects Created: {result['subjects_created']}, "
                f"Faculty Created: {result['faculty_created']}, "
                f"Rows Inserted: {result['inserted']}, "
                f"Rows Updated: {result['updated']}, "
                f"Rows Skipped: {result['skipped']}, "
                f"Errors: {len(result['errors'])}.",
                "success" if not result["errors"] else "warning"
            )

            if result["credentials_csv"]:
                flash(
                    f"{result['faculty_created']} new faculty account(s) created. "
                    "Credentials CSV generated for this upload — see "
                    "result['credentials_csv'] (string, in memory) to wire "
                    "into a download endpoint as needed.",
                    "info"
                )
        else:
            flash(f"Upload failed: {result['message']}", "danger")

        for warn in result["warnings"][:10]:
            flash(warn, "warning")
        for err in result["errors"][:10]:
            flash(err, "danger")

        log_audit(
            current_user.id,
            "upload_timetable", "timetable", None, None,
            f"section={section_id} file={filename} "
            f"inserted={result.get('inserted',0)} "
            f"updated={result.get('updated',0)} "
            f"subjects_created={result.get('subjects_created',0)} "
            f"faculty_created={result.get('faculty_created',0)}",
            ip_address=get_client_ip()
        )

        return redirect(url_for("timetable.view_timetable",
                                section_id=section_id))

    return render_template("admin/timetable_upload.html",
                           sections=sections, pdf_support=PDF_SUPPORT)


# ─────────────────────────────────────────────────────────────────────────────
# FILE PARSERS  (unchanged from your original)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_csv(file_bytes: bytes) -> list[dict]:
    content = file_bytes.decode("utf-8-sig")
    reader  = csv.DictReader(io.StringIO(content))

    if not reader.fieldnames:
        raise ValueError("CSV has no header row.")

    rows = []
    for raw in reader:
        norm = {
            k.strip().lower().replace(" ", "_"): (v or "").strip()
            for k, v in raw.items()
        }
        rows.append(norm)

    return rows


def _parse_pdf(file_bytes: bytes) -> list[dict]:
    rows = []

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:

            tables = page.extract_tables()
            for table in tables:
                if not table:
                    continue

                header = [
                    (c or "").strip().lower().replace(" ", "_")
                    for c in table[0]
                ]

                if _is_timetable_header(header):
                    for data_row in table[1:]:
                        if not data_row or all(
                            (c or "").strip() == "" for c in data_row
                        ):
                            continue
                        row_dict = {
                            header[i]: (data_row[i] or "").strip()
                            for i in range(min(len(header), len(data_row)))
                        }
                        rows.append(row_dict)
                else:
                    heuristic = _heuristic_table_rows(table)
                    rows.extend(heuristic)

            if not rows:
                text = page.extract_text() or ""
                rows.extend(_parse_text_lines(text))

    return rows


def _is_timetable_header(header: list) -> bool:
    key_cols = {"day", "day_of_week", "start", "start_time", "subject", "subject_code"}
    return bool(key_cols & set(header))


def _heuristic_table_rows(table: list) -> list[dict]:
    rows = []
    time_re = re.compile(r"\d{1,2}[:.]\d{2}")

    for data_row in table:
        if not data_row:
            continue
        cells = [(c or "").strip() for c in data_row]

        day = DAY_ALIASES.get(cells[0].upper())
        if not day:
            continue

        for cell in cells[1:]:
            if not cell:
                continue
            times = time_re.findall(cell)
            if len(times) >= 2:
                rows.append({
                    "day_of_week": day,
                    "start_time":  times[0].replace(".", ":"),
                    "end_time":    times[1].replace(".", ":"),
                    "raw_cell":    cell,
                })

    return rows


def _parse_text_lines(text: str) -> list[dict]:
    rows  = []
    lines = text.splitlines()

    time_re = re.compile(r"(\d{1,2}:\d{2})")

    for line in lines:
        line = line.strip()
        if not line:
            continue

        day = None
        upper = line.upper()
        for alias, canonical in DAY_ALIASES.items():
            if alias in upper.split():
                day = canonical
                break
        if not day:
            continue

        times = time_re.findall(line)
        if len(times) < 2:
            continue

        cleaned = re.sub(
            r"\b(" + "|".join(DAY_ALIASES.keys()) + r")\b",
            "",
            line,
            flags=re.IGNORECASE
        ).strip()
        parts = re.split(r"\s{2,}|\t|,", cleaned)
        parts = [p.strip() for p in parts if p.strip()]

        rows.append({
            "day_of_week":  day,
            "start_time":   times[0],
            "end_time":     times[1],
            "subject_code": parts[0] if len(parts) > 0 else "",
            "faculty_code": parts[1] if len(parts) > 1 else "",
            "room":         parts[2] if len(parts) > 2 else "",
            "slot_type":    parts[3] if len(parts) > 3 else "Theory",
        })

    return rows


# ─────────────────────────────────────────────────────────────────────────────
# CORE ROW PROCESSOR  (auto-create instead of placeholder)
# timetable columns used: section_id, subject_id, faculty_id, day_of_week,
#   start_time, end_time, room, slot_type, academic_period_id, is_active,
#   needs_review, created_at, updated_at
# ─────────────────────────────────────────────────────────────────────────────

def _process_rows(section_id: int, rows: list[dict], clear_existing: bool) -> dict:
    """
    Validate, resolve (auto-creating subjects/faculty as needed), and
    UPSERT a list of normalised timetable row dicts.

    Returns a result dict:
        success, inserted, updated, deactivated, skipped,
        subjects_created, faculty_created,
        warnings, errors, message, credentials_csv
    """
    result = {
        "success":          True,
        "inserted":         0,
        "updated":          0,
        "deactivated":      0,
        "skipped":          0,
        "subjects_created": 0,
        "faculty_created":  0,
        "warnings":         [],
        "errors":           [],
        "message":          "",
        "credentials_csv":  None,
    }

    conn   = get_db()
    cursor = conn.cursor(dictionary=True, buffered=True)

    try:
        # ── Section (sections has no dept_code column — joined from
        # departments) — needed for department_id / sem_number / dept_code. ──
        cursor.execute(
            """
            SELECT sec.id, sec.department_id, sec.sem_number, d.code AS dept_code
            FROM sections sec
            JOIN departments d ON d.id = sec.department_id
            WHERE sec.id = %s
            """,
            (section_id,)
        )
        section = cursor.fetchone()
        if not section:
            result["success"] = False
            result["message"] = f"Section {section_id} not found."
            return result

        # ── Active academic period ───────────────────────────────────────────
        cursor.execute("SELECT id FROM academic_periods WHERE is_active = 1 LIMIT 1")
        period = cursor.fetchone()
        if not period:
            result["success"] = False
            result["message"] = "No active academic period."
            return result
        period_id = period["id"]

        # ── Optionally deactivate existing slots ─────────────────────────────
        if clear_existing:
            cursor.execute(
                """
                UPDATE timetable
                SET is_active = 0, updated_at = NOW()
                WHERE section_id = %s AND academic_period_id = %s
                """,
                (section_id, period_id)
            )
            result["deactivated"] = cursor.rowcount

        # ── Per-batch caches so the same new subject/faculty within THIS
        # upload isn't created twice. Re-uploading the same file later finds
        # the already-created records via the DB lookups instead. ───────────
        created_subjects_cache = {}
        created_faculty_cache  = {}
        credentials_log        = []

        # ── Process each row ──────────────────────────────────────────────────
        for row_num, raw_row in enumerate(rows, start=2):
            norm = _normalise_row_keys(raw_row)

            if not any(norm.values()):
                continue  # completely empty

            # ── Day ──────────────────────────────────────────────────────────
            day_raw = norm.get("day_of_week") or norm.get("day") or ""
            day     = DAY_ALIASES.get(day_raw.upper().strip()) or day_raw.title().strip()

            if not day or day not in VALID_DAYS:
                result["errors"].append(
                    f"Row {row_num}: Unrecognised day '{day_raw}' — skipped."
                )
                result["skipped"] += 1
                continue

            # ── Times ─────────────────────────────────────────────────────────
            start_raw = norm.get("start_time") or norm.get("start") or ""
            end_raw   = norm.get("end_time")   or norm.get("end")   or ""

            if "-" in start_raw and not end_raw:
                parts     = start_raw.split("-", 1)
                start_raw = parts[0].strip()
                end_raw   = parts[1].strip()

            start_str, end_str, time_err = _parse_times(start_raw, end_raw)
            if time_err:
                result["errors"].append(f"Row {row_num}: {time_err} — skipped.")
                result["skipped"] += 1
                continue

            # ── Slot type ─────────────────────────────────────────────────────
            slot_type = (
                norm.get("slot_type") or norm.get("type") or "Theory"
            ).strip().title()
            if slot_type not in VALID_SLOT_TYPES:
                result["warnings"].append(
                    f"Row {row_num}: Unknown slot_type '{slot_type}' "
                    "— defaulting to 'Theory'."
                )
                slot_type = "Theory"

            is_break = slot_type in ("Interval", "Lunch")

            # ── Room ──────────────────────────────────────────────────────────
            room = norm.get("room", "").strip() or None

            # ── Subject resolution (auto-create, no placeholder) ────────────
            subject_id = None
            if not is_break:
                sub_raw = (
                    norm.get("subject_code") or norm.get("subject") or
                    norm.get("raw_cell", "")
                ).strip()

                if sub_raw:
                    subject_id, created, sub_warn = get_or_create_subject(
                        cursor, sub_raw,
                        section["department_id"], section["sem_number"],
                        created_subjects_cache, created_by=current_user.id
                    )
                    if sub_warn:
                        result["warnings"].append(f"Row {row_num}: {sub_warn}")
                    if created:
                        result["subjects_created"] += 1

            # ── Faculty resolution (auto-create, no placeholder) ─────────────
            faculty_id = None
            # ── Elective group auto-link ─────────────────────────────────
            elective_group_id = None
            if subject_id and not is_break:
                cursor.execute(
                    """
                    SELECT eg.id, eg.group_type
                    FROM elective_groups eg
                    LEFT JOIN cluster_sections cs
                           ON cs.cluster_id = eg.cluster_id AND cs.section_id = %s
                    WHERE eg.subject_id = %s
                      AND eg.academic_period_id = %s
                      AND eg.is_active = 1
                      AND (
                            eg.group_type = 'open_elective'
                         OR (eg.group_type = 'professional_elective' AND cs.section_id IS NOT NULL)
                         OR (eg.group_type = 'split_group' AND eg.parent_section_id = %s)
                      )
                    LIMIT 1
                    """,
                    (section_id, subject_id, period_id, section_id)
                )
                eg_row = cursor.fetchone()
                if eg_row:
                    elective_group_id = eg_row["id"]

            cursor.execute(
                    """
                    INSERT INTO timetable
                        (section_id, subject_id, elective_group_id, faculty_id, day_of_week,
                         start_time, end_time, room, slot_type,
                         academic_period_id, is_active, needs_review, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,0,NOW())
                    ON DUPLICATE KEY UPDATE
                        subject_id        = VALUES(subject_id),
                        elective_group_id = VALUES(elective_group_id),
                        faculty_id        = VALUES(faculty_id),
                        end_time          = VALUES(end_time),
                        room              = VALUES(room),
                        slot_type         = VALUES(slot_type),
                        is_active         = 1,
                        needs_review      = 0,
                        updated_at        = NOW()
                    """,
                    (section_id, subject_id, elective_group_id, faculty_id,
                     day, start_str, end_str, room, slot_type, period_id)
                )
            
            if not is_break:
                fac_raw = (
                    norm.get("faculty_code") or norm.get("faculty") or
                    norm.get("raw_cell", "")
                ).strip()

                if fac_raw:
                    faculty_id, created, fac_warn = get_or_create_faculty(
                        cursor, fac_raw,
                        section["department_id"], section["dept_code"],
                        created_faculty_cache, credentials_log
                    )
                    if fac_warn:
                        result["warnings"].append(f"Row {row_num}: {fac_warn}")
                    if created:
                        result["faculty_created"] += 1

            # ── UPSERT (needs_review hardcoded to 0 — placeholder mechanism
            # is removed; the column itself still exists in the schema and
            # defaults safely, so no migration is required) ──────────────────
            try:
                cursor.execute(
                    """
                    INSERT INTO timetable
                        (section_id, subject_id, faculty_id, day_of_week,
                         start_time, end_time, room, slot_type,
                         academic_period_id, is_active,
                         needs_review, created_at)
                    VALUES
                        (%s,%s,%s,%s,%s,%s,%s,%s,%s,1,0,NOW())
                    ON DUPLICATE KEY UPDATE
                        subject_id   = VALUES(subject_id),
                        faculty_id   = VALUES(faculty_id),
                        end_time     = VALUES(end_time),
                        room         = VALUES(room),
                        slot_type    = VALUES(slot_type),
                        is_active    = 1,
                        needs_review = 0,
                        updated_at   = NOW()
                    """,
                    (
                        section_id, subject_id, faculty_id,
                        day, start_str, end_str,
                        room, slot_type, period_id,
                    )
                )

                # mysql-connector: rowcount=1 -> insert, 2 -> update, 0 -> no change
                if cursor.rowcount == 1:
                    result["inserted"] += 1
                elif cursor.rowcount == 2:
                    result["updated"] += 1

            except Exception as exc:
                result["errors"].append(f"Row {row_num}: DB error — {exc}")
                result["skipped"] += 1
                continue

            # ── section_subjects mapping ──────────────────────────────────────
            if subject_id and faculty_id:
                try:
                    _upsert_section_subject(
                        cursor, section_id, subject_id, faculty_id, period_id
                    )
                except Exception as exc:
                    result["warnings"].append(
                        f"Row {row_num}: section_subjects mapping failed — {exc}"
                    )

        conn.commit()

        # ── Build credentials CSV (in-memory string, not a file) ─────────────
        if credentials_log:
            result["credentials_csv"] = _build_credentials_csv(credentials_log)

    except Exception as exc:
        conn.rollback()
        logger.exception("_process_rows fatal error")
        result["success"] = False
        result["message"] = f"Upload failed and was rolled back: {exc}"

    return result


def _build_credentials_csv(credentials_log: list[dict]) -> str:
    """
    Build a CSV string (not a file) listing every newly created faculty
    account's credentials for this upload.

    Columns: Faculty Name, Faculty Code, Login Email, Temporary Password
    (no "Username" column — users table has no username field; email IS
    the login identifier per the actual schema)
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["Faculty Name", "Faculty Code", "Login Email", "Temporary Password"]
    )
    for c in credentials_log:
        writer.writerow([
            c["faculty_name"], c["faculty_code"],
            c["login_email"], c["temp_password"],
        ])
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# HOLIDAYS  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

@timetable_bp.route("/holidays")
@admin_required
def holidays():
    cursor = get_cursor()
    cursor.execute(
        """
        SELECT
            h.*,
            d.code AS dept_code,
            sec.section_label,
            u.full_name AS created_by_name
        FROM holiday_calendar h
        LEFT JOIN departments d   ON d.id   = h.department_id
        LEFT JOIN sections sec    ON sec.id = h.section_id
        LEFT JOIN users    u      ON u.id   = h.created_by
        ORDER BY h.holiday_date DESC
        """
    )
    holidays_list = cursor.fetchall()

    cursor.execute("SELECT id, code, name FROM departments ORDER BY name")
    departments = cursor.fetchall()

    cursor.execute(
        """
        SELECT sec.id, sec.section_label, d.code AS dept_code
        FROM sections sec
        JOIN departments d       ON d.id  = sec.department_id
        JOIN academic_periods ap ON ap.id = sec.academic_period_id
        WHERE ap.is_active = 1
        ORDER BY d.code, sec.section_label
        """
    )
    sections = cursor.fetchall()

    return render_template(
        "admin/holidays.html",
        holidays=holidays_list,
        departments=departments,
        sections=sections,
    )


@timetable_bp.route("/holidays/add", methods=["POST"])
@admin_required
def add_holiday():
    holiday_date  = request.form.get("holiday_date",  "").strip()
    reason        = request.form.get("reason",        "").strip() or None
    scope         = request.form.get("scope",         "college")
    department_id = request.form.get("department_id", "") or None
    section_id    = request.form.get("section_id",    "") or None

    if not holiday_date:
        flash("Date is required.", "danger")
        return redirect(url_for("timetable.holidays"))

    conn = cursor = None
    try:
        conn   = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO holiday_calendar
                (holiday_date, reason, scope,
                 department_id, section_id,
                 created_by, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,NOW())
            """,
            (
                holiday_date, reason, scope,
                int(department_id) if department_id else None,
                int(section_id)    if section_id    else None,
                current_user.id,
            )
        )
        conn.commit()
        flash("Holiday added.", "success")

    except Exception as exc:
        if conn:
            conn.rollback()
        flash(f"Error adding holiday: {exc}", "danger")

    return redirect(url_for("timetable.holidays"))


@timetable_bp.route("/holidays/<int:holiday_id>/delete", methods=["POST"])
@admin_required
def delete_holiday(holiday_id):
    conn = cursor = None
    try:
        conn   = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM holiday_calendar WHERE id = %s", (holiday_id,)
        )
        conn.commit()
        flash("Holiday removed.", "success")
    except Exception as exc:
        if conn:
            conn.rollback()
        flash(f"Error: {exc}", "danger")

    return redirect(url_for("timetable.holidays"))


# ─────────────────────────────────────────────────────────────────────────────
# AJAX  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

@timetable_bp.route("/api/section-data/<int:section_id>")
@admin_required
def section_data_api(section_id):
    cursor = get_cursor()
    cursor.execute(
        "SELECT id, code, name, subject_type FROM subjects ORDER BY code"
    )
    subjects = [_serialise_row(r) for r in cursor.fetchall()]

    cursor.execute(
        """
        SELECT f.id, u.full_name, f.faculty_code
        FROM faculty f
        JOIN users u ON u.id = f.user_id
        WHERE u.status = 'active'
        ORDER BY u.full_name
        """
    )
    faculty = [_serialise_row(r) for r in cursor.fetchall()]

    return jsonify({"subjects": subjects, "faculty": faculty})


# ─────────────────────────────────────────────────────────────────────────────
# PRIVATE HELPERS  (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def _normalise_row_keys(row: dict) -> dict:
    return {
        k.strip().lower().replace(" ", "_"): (v or "").strip()
        for k, v in row.items()
    }


def _parse_times(start_raw: str, end_raw: str):
    start_raw = start_raw.replace(".", ":")
    end_raw   = end_raw.replace(".", ":")

    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            s = datetime.strptime(start_raw, fmt)
            e = datetime.strptime(end_raw,   fmt)
            return s.strftime("%H:%M:%S"), e.strftime("%H:%M:%S"), None
        except ValueError:
            continue

    return None, None, f"Invalid time '{start_raw}' or '{end_raw}'"


def _upsert_section_subject(cursor, section_id, subject_id,
                             faculty_id, period_id):
    cursor.execute(
        """
        INSERT IGNORE INTO section_subjects
            (section_id, subject_id, faculty_id,
             academic_period_id, created_at)
        VALUES (%s,%s,%s,%s,NOW())
        """,
        (section_id, subject_id, faculty_id, period_id)
    )


def _td_to_str(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "total_seconds"):
        total = int(value.total_seconds())
        h, rem = divmod(total, 3600)
        m, _   = divmod(rem,   60)
        return f"{h:02d}:{m:02d}"
    return str(value)[:5]


def _serialise_row(row: dict) -> dict:
    return {
        k: (
            _td_to_str(v)   if hasattr(v, "total_seconds") else
            v.isoformat()   if hasattr(v, "isoformat")     else
            v
        )
        for k, v in row.items()
    }