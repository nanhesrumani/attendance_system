"""
attendance_system/api/admin/academic.py
Academic management: Departments, Batches, Sections, Subjects,
Academic Periods CRUD.
"""

from datetime import date
import logging

from flask_login import login_required
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import current_user
from core.db import get_db, get_cursor
from auth.helpers import admin_required, log_audit, get_client_ip

logger = logging.getLogger(__name__)

academic_bp = Blueprint(
    "academic", __name__,
    template_folder="../../templates/admin"
)

def refresh_section_academics(section_id):
    """
    Rebuild section_subjects from curriculum.
    Core Subjects  -> Always added.
    Cluster Electives -> Added only if the section belongs to a cluster.
    Faculty is left NULL and will be updated from timetable.
    """

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    # ---------------------------------------------------
    # STEP 1 : Get Section Context
    # ---------------------------------------------------

    cursor.execute("""
        SELECT
            s.id,
            s.department_id,
            s.sem_number,
            s.academic_period_id,
            cs.cluster_id
        FROM sections s

        LEFT JOIN cluster_sections cs
               ON cs.section_id = s.id

        WHERE s.id=%s
    """, (section_id,))

    section = cursor.fetchone()

    if not section:
        return

    department_id = section["department_id"]
    semester = section["sem_number"]
    academic_period_id = section["academic_period_id"]
    cluster_id = section["cluster_id"]

    # ---------------------------------------------------
    # STEP 2 : Load Core Subjects
    # ---------------------------------------------------

    cursor.execute("""
        SELECT id

        FROM subjects

        WHERE department_id=%s
        AND sem_number=%s
        AND subject_type NOT LIKE 'Elective%%'
    """,
    (
        department_id,
        semester
    ))

    core_subjects = cursor.fetchall()

    # ---------------------------------------------------
    # STEP 3 : Load Cluster Electives
    # ---------------------------------------------------

    elective_subjects = []

    if cluster_id:

        cursor.execute("""
            SELECT subject_id AS id

            FROM cluster_subjects

            WHERE cluster_id=%s
        """,
        (
            cluster_id,
        ))

        elective_subjects = cursor.fetchall()

    # ---------------------------------------------------
    # STEP 4 : Merge Subjects
    # ---------------------------------------------------

    subject_ids = []

    for s in core_subjects:
        subject_ids.append(s["id"])

    for s in elective_subjects:
        if s["id"] not in subject_ids:
            subject_ids.append(s["id"])

    # ---------------------------------------------------
    # STEP 5 : Delete Old Mapping
    # ---------------------------------------------------

    cursor.execute("""
        DELETE

        FROM section_subjects

        WHERE section_id=%s
    """,
    (
        section_id,
    ))

    # ---------------------------------------------------
    # STEP 6 : Insert Fresh Mapping
    # ---------------------------------------------------

    for subject_id in subject_ids:

        cursor.execute("""
            INSERT INTO section_subjects
            (
                section_id,
                subject_id,
                faculty_id,
                academic_period_id
            )
            VALUES
            (
                %s,
                %s,
                NULL,
                %s
            )
        """,
        (
            section_id,
            subject_id,
            academic_period_id
        ))

    conn.commit()
# ============================================
# DEPARTMENTS
# ============================================

@academic_bp.route("/departments")
@login_required
@admin_required
def departments():
    """List all departments."""
    sem_number = request.args.get("sem_number")

    cursor = get_cursor()
    cursor.execute(
        """
        SELECT
            d.*,

            0 AS batch_count,

            (
                SELECT COUNT(*)
                FROM sections s
                WHERE s.department_id=d.id
            ) AS section_count,

            (
                SELECT COUNT(*)
                FROM faculty f
                WHERE f.department_id=d.id
            ) AS faculty_count,

            (
                SELECT COUNT(*)
                FROM clusters c
                WHERE c.department_id=d.id
            ) AS cluster_count

        FROM departments d

        ORDER BY d.name
        """
    )
    depts = cursor.fetchall()

    return render_template(
        "admin/departments.html",
        departments=depts,
        sem_number=sem_number
    )


@academic_bp.route("/departments/add", methods=["POST"])
@login_required
@admin_required
def add_department():
    """Add a new department."""
    code = request.form.get("code", "").strip().upper()
    name = request.form.get("name", "").strip()

    if not code or not name:
        flash("Department code and name are required.", "danger")
        return redirect(url_for("academic.departments"))

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO departments (code, name, created_at) VALUES (%s, %s, NOW())",
            (code, name)
        )
        conn.commit()
        log_audit(
            current_user.id, "add_department",
            "departments", cursor.lastrowid, None, f"{code}: {name}",
            ip_address=get_client_ip()
        )
        flash(f"Department '{name}' added.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("academic.departments"))


@academic_bp.route("/departments/<int:dept_id>/edit", methods=["POST"])
@login_required
@admin_required
def edit_department(dept_id):
    """Edit a department."""
    code = request.form.get("code", "").strip().upper()
    name = request.form.get("name", "").strip()

    if not code or not name:
        flash("Code and name are required.", "danger")
        return redirect(url_for("academic.departments"))

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE departments SET code = %s, name = %s WHERE id = %s",
            (code, name, dept_id)
        )
        conn.commit()
        log_audit(
            current_user.id, "edit_department",
            "departments", dept_id, None, f"{code}: {name}",
            ip_address=get_client_ip()
        )
        flash("Department updated.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("academic.departments"))


@academic_bp.route("/departments/<int:dept_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_department(dept_id):
    """Delete a department."""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM departments WHERE id = %s", (dept_id,))
        conn.commit()
        log_audit(
            current_user.id, "delete_department",
            "departments", dept_id, None, None,
            ip_address=get_client_ip()
        )
        flash("Department deleted.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Cannot delete: {e}", "danger")

    return redirect(url_for("academic.departments"))


@academic_bp.route("/departments/<int:dept_id>/workspace")
@login_required
@admin_required
def department_workspace(dept_id):
    """Department workspace for organizational management."""
    active_tab = request.args.get("tab", "faculty")
    cursor = get_cursor()

    cursor.execute(
        "SELECT id, code, name, created_at FROM departments WHERE id = %s",
        (dept_id,)
    )
    department = cursor.fetchone()
    if not department:
        flash("Department not found.", "danger")
        return redirect(url_for("academic.departments"))

    cursor.execute(
        """
        SELECT f.id, f.faculty_code, f.designation, f.cluster_id,
               f.qualification, f.specialization, f.experience_years,
               f.joining_date, f.bio, f.profile_photo,
               u.full_name, u.email, u.phone, u.status,
               c.cluster_name
        FROM faculty f
        JOIN users u ON u.id = f.user_id
        LEFT JOIN clusters c ON c.id = f.cluster_id
        WHERE f.department_id = %s
        ORDER BY u.full_name
        """,
        (dept_id,)
    )
    faculty = cursor.fetchall()

    cursor.execute(
        """
        SELECT sec.id, sec.section_label, sec.sem_number, sec.room,
               b.admission_year, b.graduation_year,
               ap.name AS period_name,
               c.id AS cluster_id,
               c.cluster_name,
               (
                   SELECT COUNT(*)
                   FROM students st
                   WHERE st.section_id = sec.id
               ) AS student_count
        FROM sections sec
        JOIN batches b ON b.id = sec.batch_id
        JOIN academic_periods ap ON ap.id = sec.academic_period_id
        LEFT JOIN cluster_sections cs ON cs.section_id = sec.id
        LEFT JOIN clusters c ON c.id = cs.cluster_id
        WHERE sec.department_id = %s
        ORDER BY sec.sem_number, sec.section_label
        """,
        (dept_id,)
    )
    sections = cursor.fetchall()

    cursor.execute(
        """
        SELECT

        id,
        code,
        name,
        sem_number,
        subject_type,
        credits

        FROM subjects

        WHERE department_id=%s

        ORDER BY sem_number, subject_type, code;
        """,
        (dept_id,)
    )
    subjects = cursor.fetchall()

    cursor.execute(
        """
        SELECT

            c.id,
            c.cluster_name,
            c.remarks,

            u.full_name AS cluster_head,

            (
                SELECT COUNT(*)
                FROM faculty f
                WHERE f.cluster_id=c.id
            ) faculty_count,

            (
                SELECT COUNT(*)
                FROM cluster_sections cs
                WHERE cs.cluster_id=c.id
            ) section_count,

            (
                SELECT COUNT(*)
                FROM students st
                JOIN sections s
                    ON s.id=st.section_id
                JOIN cluster_sections cs
                    ON cs.section_id=s.id
                WHERE cs.cluster_id=c.id
            ) student_count,

            (
                SELECT COUNT(*)
                FROM subjects s
                WHERE s.department_id = c.department_id
                AND
                (
                    s.subject_type NOT IN
                    (
                        'Elective-Theory',
                        'Elective-Lab'
                    )

                    OR

                    s.id IN
                    (
                        SELECT subject_id
                        FROM cluster_subjects
                        WHERE cluster_id = c.id
                    )
                )
            ) AS subject_count

        FROM clusters c

        LEFT JOIN faculty hf
            ON hf.id=c.cluster_head_faculty_id

        LEFT JOIN users u
            ON u.id=hf.user_id

        WHERE c.department_id=%s

        ORDER BY c.cluster_name;
        """,
        (dept_id,)
    )
    clusters = cursor.fetchall()

    cursor.execute(
        """
        SELECT cs.cluster_id, sec.id, sec.section_label, sec.sem_number
        FROM cluster_sections cs
        JOIN sections sec ON sec.id = cs.section_id
        JOIN clusters c ON c.id = cs.cluster_id
        WHERE c.department_id = %s
        ORDER BY sec.sem_number, sec.section_label
        """,
        (dept_id,)
    )
    cluster_sections = {}
    for row in cursor.fetchall():
        cluster_sections.setdefault(row["cluster_id"], []).append(row)

    cursor.execute(
        """
        SELECT f.id, f.cluster_id, u.full_name, f.faculty_code
        FROM faculty f
        JOIN users u ON u.id = f.user_id
        WHERE f.department_id = %s
        ORDER BY u.full_name
        """,
        (dept_id,)
    )
    faculty_options = cursor.fetchall()

    cursor.execute(
        """
        SELECT sec.id, sec.section_label, sec.sem_number, cs.cluster_id
        FROM sections sec
        LEFT JOIN cluster_sections cs ON cs.section_id = sec.id
        WHERE sec.department_id = %s
        ORDER BY sec.sem_number, sec.section_label
        """,
        (dept_id,)
    )
    section_options = cursor.fetchall()

    cursor.execute("""
    SELECT
    cluster_id,
    subject_id
    FROM cluster_subjects
    """)

    cluster_subjects = {}

    for row in cursor.fetchall():
        cluster_subjects.setdefault(
            row["cluster_id"],
            set()
        ).add(row["subject_id"])

    stats = {
        "faculty": len(faculty),
        "sections": len(sections),
        "subjects": len(subjects),
        "clusters": len(clusters),
        "unassigned_faculty": sum(1 for f in faculty if not f.get("cluster_id")),
        "unassigned_sections": sum(1 for s in sections if not s.get("cluster_id")),
    }

    return render_template(
        "admin/department_workspace.html",
        department=department,
        active_tab=active_tab,
        faculty=faculty,
        sections=sections,
        subjects=subjects,
        clusters=clusters,
        cluster_subjects=cluster_subjects,
        cluster_sections=cluster_sections,
        faculty_options=faculty_options,
        section_options=section_options,
        stats=stats
    )

@academic_bp.route(
    "/departments/<int:dept_id>/clusters/create",
    methods=["POST"]
)
@login_required
@admin_required
def create_cluster(dept_id):

    name = request.form.get("cluster_name","").strip()

    head = request.form.get("cluster_head") or None

    remarks = request.form.get("remarks","").strip()

    department_id = dept_id

    if not name:

        flash(
            "Cluster name is required.",
            "warning"
        )

        return redirect(request.referrer)

    conn = get_db()

    cursor = conn.cursor()

    # Duplicate name check
    cursor.execute("""
        SELECT id
        FROM clusters
        WHERE
            department_id=%s
            AND LOWER(cluster_name)=LOWER(%s)
    """,(department_id,name))

    if cursor.fetchone():

        flash(
            "Cluster already exists.",
            "danger"
        )

        return redirect(request.referrer)

    cursor.execute("""
        INSERT INTO clusters(

            department_id,

            cluster_name,

            cluster_head_faculty_id,

            remarks

        )

        VALUES(%s,%s,%s,%s)
    """,

    (

        department_id,

        name,

        head,

        remarks

    ))

    conn.commit()

    flash(
        "Cluster created successfully.",
        "success"
    )

    return redirect(request.referrer)

@academic_bp.route("/clusters/<int:cluster_id>")
@login_required
@admin_required
def cluster_workspace(cluster_id):

    cursor = get_cursor()

    # --------------------------
    # Cluster Details
    # --------------------------
    cursor.execute("""
        SELECT

            c.*,

            d.code AS department_code,
            d.name AS department_name,

            u.full_name AS cluster_head

        FROM clusters c

        JOIN departments d
            ON d.id=c.department_id

        LEFT JOIN faculty f
            ON f.id=c.cluster_head_faculty_id

        LEFT JOIN users u
            ON u.id=f.user_id

        WHERE c.id=%s
    """, (cluster_id,))

    cluster = cursor.fetchone()

    if not cluster:
        flash("Cluster not found.", "danger")
        return redirect(url_for("academic.departments"))
    
    cursor.execute("""
        SELECT

            f.id,

            f.faculty_code,

            f.designation,

            f.qualification,

            f.specialization,

            u.full_name,

            u.email

        FROM faculty f

        JOIN users u

            ON u.id=f.user_id

        WHERE f.cluster_id=%s

        ORDER BY u.full_name
    """,(cluster_id,))

    faculty=cursor.fetchall()

    cursor.execute("""
        SELECT

            f.id,

            f.faculty_code,

            f.designation,

            f.cluster_id,

            u.full_name

        FROM faculty f

        JOIN users u
        ON u.id=f.user_id

        WHERE f.department_id=%s

        ORDER BY
        u.full_name
        """,(cluster["department_id"],))

    all_faculty=cursor.fetchall()

    cursor.execute("""

        SELECT

            s.id,

            s.section_label,

            s.sem_number,

            s.room,

            (

                SELECT COUNT(*)

                FROM students st

                WHERE st.section_id=s.id

            ) student_count

        FROM cluster_sections cs

        JOIN sections s

            ON s.id=cs.section_id

        WHERE cs.cluster_id=%s

        ORDER BY

            s.sem_number,

            s.section_label

    """,(cluster_id,))

    sections=cursor.fetchall()

    cursor.execute("""
        SELECT

            sec.id,

            sec.section_label,

            sec.sem_number,

            sec.room,

            cs.cluster_id

        FROM sections sec

        LEFT JOIN cluster_sections cs
            ON cs.section_id = sec.id

        WHERE sec.department_id = %s

        ORDER BY
            sec.sem_number,
            sec.section_label
        """, (cluster["department_id"],))

    all_sections = cursor.fetchall()
    cursor.execute("""
        SELECT

            st.id,
            st.usn,
            st.sid,
            st.name,
            st.enrollment_status,

            sec.section_label,
            sec.sem_number

        FROM students st

        JOIN sections sec
            ON sec.id = st.section_id

        JOIN cluster_sections cs
            ON cs.section_id = sec.id

        WHERE cs.cluster_id = %s

        ORDER BY
            sec.sem_number,
            sec.section_label,
            st.sid
        """, (cluster_id,))

    students = cursor.fetchall()

    # ----------------------------------------
    # Curriculum
    # ----------------------------------------

    cursor.execute("""

    SELECT

    s.id,
    s.code,
    s.name,
    s.sem_number,
    s.subject_type,

    CASE

    WHEN cs.subject_id IS NULL

    THEN 0

    ELSE 1

    END assigned

    FROM subjects s

    LEFT JOIN cluster_subjects cs

    ON cs.subject_id=s.id

    AND cs.cluster_id=%s

    WHERE

    s.department_id=%s

    ORDER BY

    s.sem_number,

    s.subject_type,

    s.code

    """,

    (

    cluster_id,

    cluster["department_id"]

    ))

    subject_rows = cursor.fetchall()


    curriculum = {}

    for sub in subject_rows:

        sem = sub["sem_number"]

        if sem not in curriculum:

            curriculum[sem] = {

                "core": [],

                "electives": []

            }

        if sub["subject_type"] in (

            "Theory",

            "Lab",

            "Lab-Merged"

        ):

            curriculum[sem]["core"].append(sub)

        else:

            curriculum[sem]["electives"].append(sub)

    cluster_subjects = {}

    try:
        cursor.execute("""
            SELECT cluster_id, subject_id
            FROM cluster_subjects
        """)

        for row in cursor.fetchall():
            cluster_subjects.setdefault(
                row["cluster_id"], set()
            ).add(row["subject_id"])

    except Exception:
        # Table doesn't exist yet
        cluster_subjects = {}

    subject_count = 0

    for sem in curriculum.values():
        subject_count += len(sem["core"])
        subject_count += sum(
            1 for sub in sem["electives"]
            if sub["assigned"]
        )

    stats = {
        "faculty": len(faculty),
        "sections": len(sections),
        "subjects": subject_count,
        "students": sum(
            s["student_count"]
            for s in sections
        )
    }

    active_tab = request.args.get("tab", "faculty")

    return render_template(
        "admin/cluster/cluster_workspace.html",
        cluster=cluster,
        faculty=faculty,
        all_faculty=all_faculty,
        sections=sections,
        all_sections=all_sections,
        students=students,
        cluster_id=cluster_id,
        context="cluster_workspace",    
        cluster_subjects=cluster_subjects,
        # subjects=subjects,
        curriculum=curriculum,
        stats=stats,
        active_tab=active_tab
    )

@academic_bp.route( 
    "/clusters/<int:cluster_id>/faculty/update",
    methods=["POST"]
)
@login_required
@admin_required
def update_cluster_faculty(cluster_id):

    selected = request.form.getlist("faculty_ids")
    force_move = request.form.get("force_move", "0") == "1"

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    conflicts = []

    if not force_move:

        for fid in selected:

            cursor.execute("""
                SELECT

                    f.id,
                    u.full_name,
                    c.id AS cluster_id,
                    c.cluster_name

                FROM faculty f

                JOIN users u
                    ON u.id = f.user_id

                JOIN clusters c
                    ON c.id = f.cluster_id

                WHERE
                    f.id=%s
                    AND f.cluster_id<>%s
            """,(fid,cluster_id))

            row = cursor.fetchone()

            if row:
                conflicts.append(row)

        if conflicts:

            return render_template(

                "admin/cluster/faculty_conflict.html",

                cluster_id=cluster_id,

                conflicts=conflicts,

                selected=selected

            )

    cursor.executemany("""

        UPDATE faculty

        SET cluster_id=%s

        WHERE id=%s

    """,

    [(cluster_id,fid) for fid in selected])

    conn.commit()

    flash(

        "Faculty assignment updated.",

        "success"

    )

    return redirect(

        url_for(

            "academic.cluster_workspace",

            cluster_id=cluster_id,

            tab="faculty"

        )

    )

@academic_bp.route(
    "/clusters/<int:cluster_id>/curriculum/save",
    methods=["POST"]
)
@login_required
@admin_required
def save_cluster_curriculum(cluster_id):

    semester = request.form.get("semester")

    subject_ids = request.form.getlist("subject_ids")

    cursor = get_cursor()

    # Remove existing electives for this semester
    cursor.execute("""
        DELETE cs
        FROM cluster_subjects cs
        JOIN subjects s
            ON s.id = cs.subject_id
        WHERE
            cs.cluster_id = %s
            AND s.sem_number = %s
    """, (cluster_id, semester))

    # Insert newly selected electives
    for subject_id in subject_ids:

        cursor.execute("""
            INSERT INTO cluster_subjects
            (
                cluster_id,
                subject_id
            )
            VALUES
            (
                %s,
                %s
            )
        """, (
            cluster_id,
            subject_id
        ))

    get_db().commit()

    flash(
        "Curriculum updated successfully.",
        "success"
    )

    return redirect(
        url_for(
            "academic.cluster_workspace",
            cluster_id=cluster_id,
            tab="curriculum"
        )
    )


@academic_bp.route(
    "/clusters/<int:cluster_id>/sections/update",
    methods=["POST"]
)
@login_required
@admin_required
def update_cluster_sections(cluster_id):

    selected = request.form.getlist("section_ids")
    force_move = request.form.get("force_move", "0") == "1"

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    # ----------------------------------
    # Find conflicting sections
    # ----------------------------------

    conflicts = []

    if not force_move:

        for sid in selected:

            cursor.execute("""
                SELECT

                    cs.cluster_id,
                    c.cluster_name,
                    s.section_label,
                    s.sem_number

                FROM cluster_sections cs

                JOIN clusters c
                    ON c.id = cs.cluster_id

                JOIN sections s
                    ON s.id = cs.section_id

                WHERE
                    cs.section_id=%s
                    AND cs.cluster_id<>%s
            """, (sid, cluster_id))

            row = cursor.fetchone()

            if row:
                conflicts.append(row)

        if conflicts:

            return render_template(
                "admin/cluster/section_conflict.html",
                cluster_id=cluster_id,
                conflicts=conflicts,
                selected=selected
            )

    # ----------------------------------
    # Move Sections
    # ----------------------------------

    for sid in selected:

        cursor.execute(
            """
            DELETE FROM cluster_sections
            WHERE section_id=%s
            """,
            (sid,)
        )

        cursor.execute(
            """
            INSERT INTO cluster_sections
            (cluster_id,section_id)

            VALUES(%s,%s)
            """,
            (cluster_id, sid)
        )

    conn.commit()

    flash(
        "Sections updated successfully.",
        "success"
    )

    return redirect(
        url_for(
            "academic.cluster_workspace",
            cluster_id=cluster_id,
            tab="sections"
        )
    )

@academic_bp.route("/clusters/<int:cluster_id>/edit")
@login_required
@admin_required
def edit_cluster(cluster_id):

    cursor = get_cursor(dictionary=True)

    # -----------------------
    # Cluster
    # -----------------------

    cursor.execute("""
        SELECT
            *
        FROM clusters
        WHERE id=%s
    """,(cluster_id,))

    cluster = cursor.fetchone()

    if not cluster:

        flash("Cluster not found.","danger")

        return redirect(url_for("academic.departments"))

    # -----------------------
    # Faculty belonging ONLY
    # to this cluster
    # -----------------------

    cursor.execute("""

        SELECT

            f.id,

            u.full_name

        FROM faculty f

        JOIN users u
            ON u.id=f.user_id

        WHERE
            f.cluster_id=%s

        ORDER BY
            u.full_name

    """,(cluster_id,))

    faculty = cursor.fetchall()

    return render_template(

        "admin/cluster/edit_cluster.html",

        cluster=cluster,

        faculty=faculty

    )

@academic_bp.route(
"/clusters/<int:cluster_id>/update",
methods=["POST"]
)
@login_required
@admin_required
def update_cluster(cluster_id):

    name = request.form["cluster_name"].strip()

    head = request.form.get("cluster_head") or None

    remarks = request.form.get("remarks","")

    conn = get_db()

    cursor = conn.cursor(dictionary=True)

    # ---------------------------
    # Duplicate Name Check
    # ---------------------------

    cursor.execute("""

        SELECT id

        FROM clusters

        WHERE

            department_id=(
                SELECT department_id
                FROM clusters
                WHERE id=%s
            )

            AND LOWER(cluster_name)=LOWER(%s)

            AND id<>%s

    """,(cluster_id,name,cluster_id))

    if cursor.fetchone():

        flash(

            "Cluster name already exists.",

            "warning"

        )

        return redirect(

            url_for(

                "academic.edit_cluster",

                cluster_id=cluster_id

            )

        )

    # ---------------------------
    # Cluster Head Validation
    # ---------------------------

    if head:

        cursor.execute("""

            SELECT id

            FROM faculty

            WHERE

                id=%s

                AND cluster_id=%s

        """,(head,cluster_id))

        if not cursor.fetchone():

            flash(

                "Selected faculty does not belong to this cluster.",

                "danger"

            )

            return redirect(

                url_for(

                    "academic.edit_cluster",

                    cluster_id=cluster_id

                )

            )

    cursor.execute("""

        UPDATE clusters

        SET

            cluster_name=%s,

            cluster_head_faculty_id=%s,

            remarks=%s

        WHERE id=%s

    """,

    (

        name,

        head,

        remarks,

        cluster_id

    ))

    conn.commit()

    flash(

        "Cluster updated successfully.",

        "success"

    )

    return redirect(

        url_for(

            "academic.cluster_workspace",

            cluster_id=cluster_id

        )

    )

@academic_bp.route("/clusters/<int:cluster_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_cluster(cluster_id):

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    # -----------------------
    # Check Faculty
    # -----------------------

    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM faculty
        WHERE cluster_id=%s
    """,(cluster_id,))

    faculty_count = cursor.fetchone()["total"]

    # -----------------------
    # Check Sections
    # -----------------------

    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM cluster_sections
        WHERE cluster_id=%s
    """,(cluster_id,))

    section_count = cursor.fetchone()["total"]

    if faculty_count > 0 or section_count > 0:

        flash(
            f"Cannot delete cluster. Remove {faculty_count} faculty and {section_count} sections first.",
            "warning"
        )

        return redirect(
            request.referrer
            or url_for("academic.departments")
        )

    cursor.execute("""
        DELETE
        FROM clusters
        WHERE id=%s
    """,(cluster_id,))

    conn.commit()

    flash(
        "Cluster deleted successfully.",
        "success"
    )

    return redirect(
        url_for("academic.departments")
    )
# ============================================
# ACADEMIC PERIODS
# ============================================

ACADEMIC_LEVELS = {
    1: {"name": "First Year", "odd_sem": 1, "even_sem": 2},
    2: {"name": "Second Year", "odd_sem": 3, "even_sem": 4},
    3: {"name": "Third Year", "odd_sem": 5, "even_sem": 6},
    4: {"name": "Fourth Year", "odd_sem": 7, "even_sem": 8},
}

@academic_bp.route("/periods")
@login_required
@admin_required
def periods():
    """
    Academic Cycle page structured by Academic Levels.
    """
    cursor = get_cursor(dictionary=True)

    cursor.execute("""
        SELECT *
        FROM academic_periods
        ORDER BY sem_number
    """)
    rows = cursor.fetchall()

    today = date.today()

    # Organize into levels
    levels = {}

    for level_no, level_data in ACADEMIC_LEVELS.items():

        level_cycles = {
            "level_no": level_no,
            "level_name": level_data["name"],
            "odd": None,
            "even": None
        }

        for row in rows:

            if row["sem_number"] == level_data["odd_sem"]:
                status, badge = calculate_status(
                    row["start_date"],
                    row["end_date"]
                )
                row["cycle_type"] = "Odd"
                row["semester"] = level_data["odd_sem"]
                row["status"] = status
                row["badge"] = badge
                level_cycles["odd"] = row

            elif row["sem_number"] == level_data["even_sem"]:
                status, badge = calculate_status(
                    row["start_date"],
                    row["end_date"]
                )
                row["cycle_type"] = "Even"
                row["semester"] = level_data["even_sem"]
                row["status"] = status
                row["badge"] = badge
                level_cycles["even"] = row

        levels[level_no] = level_cycles

    return render_template(
        "admin/periods.html",
        levels=levels
    )


def calculate_status(start_date, end_date):
    today = date.today()

    if today < start_date:
        return "Upcoming", "warning"
    elif start_date <= today <= end_date:
        return "Active", "success"
    else:
        return "Completed", "secondary"
    
@academic_bp.route("/periods/add", methods=["POST"])
@login_required
@admin_required
def add_period():
    """
    Add Academic Cycle.
    sem_number is automatically derived from level + cycle.
    """
    name = request.form.get("name", "").strip()
    level = request.form.get("level", "").strip()
    cycle_type = request.form.get("cycle_type", "").strip()
    start_date = request.form.get("start_date", "").strip()
    end_date = request.form.get("end_date", "").strip()

    if not name or not level or not cycle_type or not start_date or not end_date:
        flash("All fields are required.", "danger")
        return redirect(url_for("academic.periods"))

    level = int(level)

    if level not in ACADEMIC_LEVELS:
        flash("Invalid academic level.", "danger")
        return redirect(url_for("academic.periods"))

    if cycle_type == "Odd":
        sem_number = ACADEMIC_LEVELS[level]["odd_sem"]
    else:
        sem_number = ACADEMIC_LEVELS[level]["even_sem"]

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()

        # Only one cycle per semester allowed
        cursor.execute("""
            SELECT id
            FROM academic_periods
            WHERE sem_number = %s
        """, (sem_number,))
        if cursor.fetchone():
            flash("Cycle already exists for this level.", "warning")
            return redirect(url_for("academic.periods"))

        cursor.execute("""
            INSERT INTO academic_periods
                (name, sem_number, start_date, end_date, is_active, created_at)
            VALUES (%s, %s, %s, %s, 0, NOW())
        """, (name, sem_number, start_date, end_date))

        conn.commit()

        flash("Academic cycle created successfully.", "success")

    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("academic.periods"))

@academic_bp.route("/periods/<int:period_id>/activate", methods=["POST"])
@login_required
@admin_required
def activate_period(period_id):

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT id,
            sem_number,
            is_archived,
            is_active,
            name
        FROM academic_periods
        WHERE id=%s
    """, (period_id,))

    period = cursor.fetchone()

    if not period:
        flash("Academic Cycle not found.", "danger")
        return redirect(url_for("academic.periods"))
    
    if period["is_archived"]:
        flash(
            "Archived Academic Cycles cannot be activated.",
            "warning"
        )
        return redirect(url_for("academic.periods"))

    level = None

    for lvl, data in ACADEMIC_LEVELS.items():
        if period["sem_number"] in (data["odd_sem"], data["even_sem"]):
            level = lvl
            break

    if level is None:
        flash("Invalid Academic Level.", "danger")
        return redirect(url_for("academic.periods"))

    odd_sem = ACADEMIC_LEVELS[level]["odd_sem"]
    even_sem = ACADEMIC_LEVELS[level]["even_sem"]

    # deactivate both cycles of this level
    cursor.execute("""
        UPDATE academic_periods
        SET is_active = 0
        WHERE sem_number IN (%s, %s)
    """, (odd_sem, even_sem))

    # activate selected cycle
    cursor.execute("""
        UPDATE academic_periods
        SET is_active = 1
        WHERE id=%s
    """, (period_id,))

    conn.commit()

    flash("Academic Cycle activated successfully.", "success")

    return redirect(url_for("academic.periods"))

@academic_bp.route("/periods/<int:period_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def edit_period(period_id):

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT *
        FROM academic_periods
        WHERE id=%s
    """, (period_id,))

    period = cursor.fetchone()

    if not period:
        flash("Academic Cycle not found.", "danger")
        return redirect(url_for("academic.periods"))
    
    if period["is_archived"]:
        flash(
            "Archived Academic Cycles cannot be edited.",
            "warning"
        )
        return redirect(url_for("academic.periods"))

    if request.method == "POST":

        name = request.form["name"].strip()
        start_date = request.form["start_date"]
        end_date = request.form["end_date"]

        if end_date <= start_date:

            flash(
                "End Date must be greater than Start Date.",
                "danger"
            )

            return redirect(
                url_for(
                    "academic.edit_period",
                    period_id=period_id
                )
            )

        cursor.execute("""
            UPDATE academic_periods
            SET
                name=%s,
                start_date=%s,
                end_date=%s
            WHERE id=%s
        """, (

            name,

            start_date,

            end_date,

            period_id

        ))

        conn.commit()

        flash(
            "Academic Cycle updated successfully.",
            "success"
        )

        return redirect(url_for("academic.periods"))

    return render_template(

        "admin/edit_period.html",

        period=period

    )

@academic_bp.route("/periods/<int:period_id>/deactivate", methods=["POST"])
@login_required
@admin_required
def deactivate_period(period_id):

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT *
        FROM academic_periods
        WHERE id=%s
    """, (period_id,))

    period = cursor.fetchone()

    if not period:
        flash("Academic Cycle not found.", "danger")
        return redirect(url_for("academic.periods"))

    if period["is_archived"]:
        flash(
            "Archived Academic Cycles cannot be modified.",
            "warning"
        )
        return redirect(url_for("academic.periods"))

    cursor.execute("""
        UPDATE academic_periods
        SET is_active=0
        WHERE id=%s
    """, (period_id,))

    conn.commit()

    flash(
        "Academic Cycle deactivated successfully.",
        "success"
    )

    return redirect(url_for("academic.periods")) 

@academic_bp.route("/periods/<int:period_id>/archive", methods=["POST"])
@login_required
@admin_required
def archive_period(period_id):

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT *
        FROM academic_periods
        WHERE id=%s
    """, (period_id,))

    period = cursor.fetchone()

    if not period:
        flash("Academic Cycle not found.", "danger")
        return redirect(url_for("academic.periods"))

    if period["is_archived"]:
        flash(
            "Academic Cycle is already archived.",
            "warning"
        )
        return redirect(url_for("academic.periods"))

    if period["is_active"]:
        flash(
            "Deactivate the Academic Cycle before archiving.",
            "warning"
        )
        return redirect(url_for("academic.periods"))

    cursor.execute("""
        UPDATE academic_periods
        SET is_archived=1
        WHERE id=%s
    """, (period_id,))

    conn.commit()

    flash(
        "Academic Cycle archived successfully.",
        "success"
    )

    return redirect(url_for("academic.periods"))

@academic_bp.route("/periods/<int:period_id>/restore", methods=["POST"])
@login_required
@admin_required
def restore_period(period_id):

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    cursor.execute("""
        SELECT *
        FROM academic_periods
        WHERE id=%s
    """, (period_id,))

    period = cursor.fetchone()

    if not period:
        flash("Academic Cycle not found.", "danger")
        return redirect(url_for("academic.periods"))

    if not period["is_archived"]:
        flash("Academic Cycle is not archived.", "warning")
        return redirect(url_for("academic.periods"))

    cursor.execute("""
        UPDATE academic_periods
        SET is_archived = 0
        WHERE id=%s
    """, (period_id,))

    conn.commit()

    flash(
        "Academic Cycle restored successfully.",
        "success"
    )

    return redirect(url_for("academic.periods"))

@academic_bp.route("/periods/<int:period_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_period(period_id):

    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    # Dependency check: Sections
    cursor.execute("""
        SELECT COUNT(*) AS total
        FROM sections
        WHERE academic_period_id=%s
    """, (period_id,))
    count = cursor.fetchone()["total"]

    if count > 0:
        flash("Cannot delete. Sections are linked to this cycle.", "warning")
        return redirect(url_for("academic.periods"))

    cursor.execute("""
        DELETE FROM academic_periods
        WHERE id=%s
    """, (period_id,))

    conn.commit()

    flash("Cycle deleted successfully.", "success")
    return redirect(url_for("academic.periods"))
# ============================================
# BATCHES
# ============================================

@academic_bp.route("/batches")
@login_required
@admin_required
def batches():
    """Batch overview"""

    cursor = get_cursor()

    cursor.execute("""
        SELECT
            b.id,
            b.label,
            b.admission_year,
            b.graduation_year,
            b.current_sem,

            COUNT(DISTINCT st.id) AS student_count

        FROM batches b

        LEFT JOIN sections sec
            ON sec.batch_id = b.id

        LEFT JOIN students st
            ON st.section_id = sec.id

        GROUP BY
            b.id,
            b.label,
            b.admission_year,
            b.graduation_year,
            b.current_sem

        ORDER BY
            b.admission_year DESC
    """)

    batches = cursor.fetchall()

    active_batches = []
    alumni_batches = []

    for batch in batches:

        if batch["current_sem"] >= 9:
            alumni_batches.append(batch)
        else:
            active_batches.append(batch)

    cursor.execute("""
        SELECT
            id,
            code,
            name
        FROM departments
        ORDER BY name
    """)

    departments = cursor.fetchall()

    return render_template(
        "admin/batches.html",
        active_batches=active_batches,
        alumni_batches=alumni_batches,
        departments=departments
    )

    

@academic_bp.route("/batches/add", methods=["POST"])
@login_required
@admin_required
def add_batch():

    admission_year = request.form.get("admission_year", "").strip()
    current_sem = request.form.get("current_sem", "1").strip()

    if not admission_year:
        flash("Admission year is required.", "danger")
        return redirect(url_for("academic.batches"))

    graduation_year = int(admission_year) + 4
    label = f"{admission_year}-{graduation_year}"

    conn = None

    try:

        conn = get_db()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT id
            FROM batches
            WHERE admission_year=%s
        """, (admission_year,))

        if cursor.fetchone():
            flash("Batch already exists.", "warning")
            return redirect(url_for("academic.batches"))

        cursor.execute("""
            INSERT INTO batches
            (
                admission_year,
                graduation_year,
                current_sem,
                label,
                created_at
            )
            VALUES
            (
                %s,
                %s,
                %s,
                %s,
                NOW()
            )
        """,
        (
            admission_year,
            graduation_year,
            current_sem,
            label
        ))

        conn.commit()

        flash("Batch created successfully.", "success")

    except Exception as e:

        if conn:
            conn.rollback()

        flash(str(e), "danger")

    return redirect(url_for("academic.batches"))


@academic_bp.route("/batches/<int:batch_id>/edit", methods=["POST"])
@login_required
@admin_required
def edit_batch(batch_id):
    """Edit a batch."""
    current_sem = request.form.get("current_sem", "").strip()
    label = request.form.get("label", "").strip()

    if not current_sem:
        flash("Current semester is required.", "danger")
        return redirect(url_for("academic.batches"))

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE batches SET current_sem = %s, label = %s WHERE id = %s",
            (int(current_sem), label if label else None, batch_id)
        )
        conn.commit()
        log_audit(
            current_user.id, "edit_batch",
            "batches", batch_id, None, label or "",
            ip_address=get_client_ip()
        )
        flash("Batch updated.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("academic.batches"))


@academic_bp.route("/batches/<int:batch_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_batch(batch_id):
    """Delete a batch."""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM batches WHERE id = %s", (batch_id,))
        conn.commit()
        log_audit(
            current_user.id, "delete_batch",
            "batches", batch_id, None, None,
            ip_address=get_client_ip()
        )
        flash("Batch deleted.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("academic.batches"))

@academic_bp.route("/batches/<int:batch_id>")
@login_required
@admin_required
def batch_workspace(batch_id):

    cursor = get_cursor()

    # -------------------------
    # Batch Details
    # -------------------------
    cursor.execute("""
        SELECT
            *
        FROM batches
        WHERE id=%s
        """, (batch_id,))
    batch = cursor.fetchone()

    if not batch:
        flash("Batch not found.", "danger")
        return redirect(url_for("academic.batches"))

    # ----------------------------------------
    # Total students joined in this batch
    # ----------------------------------------

    cursor.execute("""
    SELECT COUNT(*) AS total
    FROM students st
    JOIN sections s
        ON s.id = st.section_id
    WHERE s.batch_id=%s
    """,(batch_id,))

    total_students = cursor.fetchone()["total"]


    # ----------------------------------------
    # Currently studying
    # ----------------------------------------

    cursor.execute("""
    SELECT COUNT(*) AS total
    FROM students st
    JOIN sections s
        ON s.id=st.section_id
    WHERE
        s.batch_id=%s
    AND
        st.enrollment_status!='graduated'
    """,(batch_id,))

    active_students = cursor.fetchone()["total"]


    # ----------------------------------------
    # Graduated
    # ----------------------------------------

    cursor.execute("""
    SELECT COUNT(*) AS total
    FROM students st
    JOIN sections s
        ON s.id=st.section_id
    WHERE
        s.batch_id=%s
    AND
        st.enrollment_status='graduated'
    """,(batch_id,))

    graduated_students = cursor.fetchone()["total"]


    # ----------------------------------------
    # Placeholder values (to be implemented later)
    # ----------------------------------------

    dropped_students = 0

    detained_students = 0

    graduated_students = 0

    eligible_students = active_students

    not_eligible_students = 0

    return render_template(
        "admin/batch/batch_workspace.html",
        batch=batch,
        total_students=total_students,
        active_students=active_students,
        graduated_students=graduated_students,
        dropped_students=dropped_students,
        detained_students=detained_students,
        eligible_students=eligible_students,
        not_eligible_students=not_eligible_students
    )

@academic_bp.route("/batches/<int:batch_id>/promote", methods=["POST"])
@login_required
@admin_required
def promote_batch(batch_id):

    conn = None

    try:

        conn = get_db()
        cursor = conn.cursor(dictionary=True)

        # ---------------------------------
        # Read Batch
        # ---------------------------------

        cursor.execute("""
            SELECT *
            FROM batches
            WHERE id=%s
        """, (batch_id,))

        batch = cursor.fetchone()

        if not batch:
            flash("Batch not found.", "danger")
            return redirect(url_for("academic.batches"))

        current_sem = batch["current_sem"]

        if current_sem >= 8:
            flash("Final year batch cannot be promoted.", "warning")
            return redirect(url_for(
                "academic.batch_workspace",
                batch_id=batch_id
            ))

        next_sem = current_sem + 1

        # ---------------------------------
        # Update Batch
        # ---------------------------------

        cursor.execute("""
            UPDATE batches
            SET current_sem=%s
            WHERE id=%s
        """,
        (
            next_sem,
            batch_id
        ))

        # ---------------------------------
        # Update Students
        # ---------------------------------

        cursor.execute("""
            UPDATE students
            SET current_sem=%s
            WHERE section_id IN
            (
                SELECT id
                FROM sections
                WHERE batch_id=%s
            )
        """,
        (
            next_sem,
            batch_id
        ))

        # ---------------------------------
        # Update Sections
        # ---------------------------------

        cursor.execute("""
            UPDATE sections
            SET sem_number=%s
            WHERE batch_id=%s
        """,
        (
            next_sem,
            batch_id
        ))

        conn.commit()

        flash(
            f"Batch promoted from Semester {current_sem} to Semester {next_sem}.",
            "success"
        )

        cursor.execute("""
            SELECT id
            FROM sections
            WHERE batch_id=%s
        """, (batch_id,))

        sections = cursor.fetchall()

        for sec in sections:
            refresh_section_academics(sec["id"])

    except Exception as e:

        if conn:
            conn.rollback()

        flash(str(e), "danger")

    return redirect(
        url_for(
            "academic.batch_workspace",
            batch_id=batch_id
        )
    )
# ============================================
# SECTIONS
# ============================================

@academic_bp.route("/sections")
@login_required
@admin_required
def sections():
    """List all sections."""
    department_id = request.args.get("department_id", "").strip()
    batch_id = request.args.get("batch_id", "").strip()
    sem_number = request.args.get("sem_number", "").strip()
    selected_section_id = request.args.get("section_id", "").strip()
    academic_period_id = request.args.get("academic_period_id", "").strip()

    cursor = get_cursor()

    # --- Department overview (always loaded) ---
    cursor.execute("""
        SELECT
            d.id,
            d.code,
            d.name,
            COUNT(sec.id) AS section_count
        FROM departments d
        LEFT JOIN sections sec ON sec.department_id = d.id
        GROUP BY d.id, d.code, d.name
        ORDER BY d.name
    """)
    department_overview = cursor.fetchall()

    # --- Always load departments, batches, periods, active_period ---
    cursor.execute("SELECT id, code, name FROM departments ORDER BY name")
    departments = cursor.fetchall()

    cursor.execute(
        """
        SELECT
            id,
            admission_year,
            graduation_year,
            label,
            current_sem
        FROM batches
        ORDER BY admission_year DESC;
        """
    )
    batches_list = cursor.fetchall()

    cursor.execute(
        "SELECT id, name, sem_number, is_active FROM academic_periods ORDER BY start_date DESC"
    )
    periods = cursor.fetchall()

    cursor.execute(
        "SELECT id, name FROM academic_periods WHERE is_active = 1 LIMIT 1"
    )
    active_period = cursor.fetchone()

    cursor.execute(
        """
        SELECT c.id, c.department_id, c.cluster_name, d.code AS dept_code
        FROM clusters c
        JOIN departments d ON d.id = c.department_id
        ORDER BY d.code, c.cluster_name
        """
    )
    clusters = cursor.fetchall()

    # --- Semester overview (loaded when department is selected) ---
    semester_overview = []
    if department_id:
        cursor.execute("""
            SELECT
                sec.sem_number,
                COUNT(sec.id) AS section_count
            FROM sections sec
            WHERE sec.department_id = %s
            GROUP BY sec.sem_number
            ORDER BY sec.sem_number
        """, (int(department_id),))
        semester_overview = cursor.fetchall()

    # --- Section overview (loaded when department + semester are selected) ---
    section_overview = []
    if department_id and sem_number:
        cursor.execute("""
            SELECT
                sec.id,
                sec.section_label,
                sec.sem_number,
                d.code AS dept_code,
                sec.room,
                c.cluster_name,
                (
                    SELECT COUNT(*)
                    FROM students st
                    WHERE st.section_id = sec.id
                ) AS student_count
            FROM sections sec
            JOIN departments d ON d.id = sec.department_id
            LEFT JOIN cluster_sections cs ON cs.section_id = sec.id
            LEFT JOIN clusters c ON c.id = cs.cluster_id
            WHERE sec.department_id = %s
              AND sec.sem_number = %s
            ORDER BY sec.section_label
        """, (int(department_id), int(sem_number)))
        section_overview = cursor.fetchall()

    # --- Selected section detail ---
    selected_section = None
    if selected_section_id:
        cursor.execute("""
            SELECT
                sec.*,
                d.code AS dept_code,
                d.name AS dept_name,
                b.admission_year,
                b.graduation_year,
                ap.name AS period_name,
                c.id AS cluster_id,
                c.cluster_name,
                (
                    SELECT COUNT(*)
                    FROM students st
                    WHERE st.section_id = sec.id
                ) AS student_count
            FROM sections sec
            JOIN departments d ON d.id = sec.department_id
            JOIN batches b ON b.id = sec.batch_id
            JOIN academic_periods ap ON ap.id = sec.academic_period_id
            LEFT JOIN cluster_sections cs ON cs.section_id = sec.id
            LEFT JOIN clusters c ON c.id = cs.cluster_id
            WHERE sec.id = %s
        """, (int(selected_section_id),))
        selected_section = cursor.fetchone()

    return render_template(
        "admin/sections.html",
        departments=departments,
        batches=batches_list,
        periods=periods,
        active_period=active_period,
        clusters=clusters,
        department_overview=department_overview,
        semester_overview=semester_overview,
        section_overview=section_overview,
        selected_section_data=selected_section,
        selected_department=department_id,
        selected_semester=sem_number,
        selected_section=selected_section_id,
    )


@academic_bp.route("/sections/add", methods=["POST"])
@login_required
@admin_required
def add_section():
    """Add a new section."""
    department_id = request.form.get("department_id", "").strip()
    batch_id = request.form.get("batch_id", "").strip()
    academic_period_id = request.form.get("academic_period_id", "").strip()
    section_label = request.form.get("section_label", "").strip().upper()
    sem_number = request.form.get("sem_number", "").strip()
    room = request.form.get("room", "").strip()
    cluster_id = request.form.get("cluster_id", "").strip()

    if not department_id or not batch_id or not academic_period_id or not section_label or not sem_number or not cluster_id:
        flash("All required fields, including cluster, must be filled.", "danger")
        return redirect(url_for("academic.sections"))

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)

        cursor.execute(
            "SELECT id FROM academic_periods WHERE id = %s LIMIT 1",
            (int(academic_period_id),)
        )
        period = cursor.fetchone()
        if not period:
            flash("Selected academic period not found.", "danger")
            return redirect(url_for("academic.sections"))

        cursor.execute(
            """
            SELECT id
            FROM clusters
            WHERE id = %s
              AND department_id = %s
            LIMIT 1
            """,
            (int(cluster_id), int(department_id))
        )
        cluster = cursor.fetchone()
        if not cluster:
            flash("Selected cluster does not belong to the department.", "danger")
            return redirect(url_for("academic.sections"))

        cursor.execute(
            """
            INSERT INTO sections
                (department_id, batch_id, academic_period_id,
                 section_label, sem_number, room, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            """,
            (
                int(department_id),
                int(batch_id),
                int(academic_period_id),
                section_label,
                int(sem_number),
                room if room else None
            )
        )
        section_id = cursor.lastrowid

        cursor.execute(
            """
            INSERT INTO cluster_sections (cluster_id, section_id)
            VALUES (%s, %s)
            """,
            (int(cluster_id), section_id)
        )
        conn.commit()
        log_audit(
            current_user.id, "add_section",
            "sections", section_id, None, section_label,
            ip_address=get_client_ip()
        )
        flash(f"Section '{section_label}' added.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("academic.sections"))


@academic_bp.route("/sections/<int:section_id>/edit", methods=["POST"])
@login_required
@admin_required
def edit_section(section_id):
    """Edit a section."""
    room = request.form.get("room", "").strip()
    sem_number = request.form.get("sem_number", "").strip()
    cluster_id = request.form.get("cluster_id", "").strip()

    if not sem_number or not cluster_id:
        flash("Semester number and cluster are required.", "danger")
        return redirect(url_for("academic.sections"))

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute(
            "SELECT department_id FROM sections WHERE id = %s",
            (section_id,)
        )
        section = cursor.fetchone()
        if not section:
            flash("Section not found.", "danger")
            return redirect(url_for("academic.sections"))

        cursor.execute(
            """
            SELECT id
            FROM clusters
            WHERE id = %s
              AND department_id = %s
            LIMIT 1
            """,
            (int(cluster_id), section["department_id"])
        )
        cluster = cursor.fetchone()
        if not cluster:
            flash("Selected cluster does not belong to the section department.", "danger")
            return redirect(url_for("academic.sections"))

        cursor.execute(
            "UPDATE sections SET room = %s, sem_number = %s WHERE id = %s",
            (room if room else None, int(sem_number), section_id)
        )
        cursor.execute(
            "DELETE FROM cluster_sections WHERE section_id = %s",
            (section_id,)
        )
        cursor.execute(
            """
            INSERT INTO cluster_sections (cluster_id, section_id)
            VALUES (%s, %s)
            """,
            (int(cluster_id), section_id)
        )
        conn.commit()
        log_audit(
            current_user.id, "edit_section",
            "sections", section_id, None, room or "",
            ip_address=get_client_ip()
        )
        flash("Section updated.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("academic.sections"))


# ============================================
# SUBJECTS
# ============================================

@academic_bp.route("/subjects")
@login_required
@admin_required
def subjects():

    department_id = request.args.get("department_id", "").strip()
    sem_number = request.args.get("sem_number", "").strip()
    subject_id = request.args.get("subject_id", "").strip()

    cursor = get_cursor()

    # =========================
    # DEPARTMENT OVERVIEW
    # =========================

    cursor.execute("""
        SELECT
            d.id,
            d.code,
            d.name,

            COUNT(sub.id) AS subject_count

        FROM departments d

        LEFT JOIN subjects sub
            ON sub.department_id = d.id

        GROUP BY d.id, d.code, d.name

        ORDER BY d.name
    """)

    department_overview = cursor.fetchall()

    # =========================
    # SEMESTER OVERVIEW
    # =========================

    semester_overview = []

    if department_id:

        cursor.execute("""
            SELECT
                COALESCE(sem_number, 0) AS sem_number,

                COUNT(id) AS subject_count

            FROM subjects

            WHERE department_id = %s

            GROUP BY COALESCE(sem_number, 0)

            ORDER BY sem_number
        """, (int(department_id),))

        semester_overview = cursor.fetchall()

    # =========================
    # SUBJECT OVERVIEW
    # =========================

    subject_overview = []

    if department_id and sem_number and sem_number != "None":

        cursor.execute("""
            SELECT
                id,
                code,
                name,
                subject_type,
                credits

            FROM subjects

            WHERE department_id = %s
            AND sem_number = %s

            ORDER BY code
        """, (
            int(department_id),
            int(sem_number)
        ))

        subject_overview = cursor.fetchall()

    elif department_id and sem_number == "None":

        cursor.execute("""
            SELECT
                id,
                code,
                name,
                subject_type,
                credits

            FROM subjects

            WHERE department_id = %s
            AND sem_number IS NULL

            ORDER BY code
        """, (int(department_id),))

        subject_overview = cursor.fetchall()
    # =========================
    # SELECTED SUBJECT
    # =========================

    selected_subject_data = None

    if subject_id:

        cursor.execute("""
            SELECT
                sub.*,

                d.code AS dept_code,
                d.name AS dept_name

            FROM subjects sub

            LEFT JOIN departments d
                ON d.id = sub.department_id

            WHERE sub.id = %s
        """, (int(subject_id),))

        selected_subject_data = cursor.fetchone()

    # =========================
    # FORM DROPDOWNS
    # =========================

    cursor.execute("""
        SELECT id, code, name
        FROM departments
        ORDER BY name
    """)

    departments = cursor.fetchall()

    return render_template(
        "admin/subjects.html",

        department_overview=department_overview,
        semester_overview=semester_overview,
        subject_overview=subject_overview,

        selected_subject_data=selected_subject_data,

        departments=departments,

        selected_department=department_id,
        selected_semester=sem_number,
        selected_subject=subject_id
    )



@academic_bp.route("/subjects/add", methods=["POST"])
@login_required
@admin_required
def add_subject():
    """Add a new subject."""

    code = request.form.get("code", "").strip().upper()
    name = request.form.get("name", "").strip()
    department_id = request.form.get("department_id", "").strip() or None
    sem_number = request.form.get("sem_number", "").strip()
    subject_type = request.form.get("subject_type", "Theory").strip()
    credits = request.form.get("credits", "0").strip()
    offering_mode = request.form.get("offering_mode", "regular").strip()

    VALID_TYPES = {
        "Theory",
        "Lab",
        "Theory-Lab-Integrated"
    }

    VALID_MODES = {
        "regular",
        "professional_elective",
        "open_elective",
        "ability_enhancement"
    }

    # ----------------------------
    # Validation
    # ----------------------------

    if not code or not name:
        flash("Subject code and subject name are required.", "danger")
        return redirect(url_for("academic.subjects"))

    if subject_type not in VALID_TYPES:
        flash("Invalid subject type.", "danger")
        return redirect(url_for("academic.subjects"))

    if offering_mode not in VALID_MODES:
        flash("Invalid offering mode.", "danger")
        return redirect(url_for("academic.subjects"))

    try:
        sem_number = int(sem_number)
        if sem_number < 1 or sem_number > 8:
            raise ValueError
    except:
        flash("Semester must be between 1 and 8.", "danger")
        return redirect(url_for("academic.subjects"))

    try:
        credits = int(credits)
    except:
        flash("Credits must be numeric.", "danger")
        return redirect(url_for("academic.subjects"))

    conn = None

    try:
        conn = get_db()
        cursor = conn.cursor()

        # ----------------------------
        # Duplicate Code Check
        # ----------------------------
        cursor.execute(
            "SELECT id FROM subjects WHERE code=%s",
            (code,)
        )

        if cursor.fetchone():
            flash(f"Subject code '{code}' already exists.", "warning")
            return redirect(url_for("academic.subjects"))

        # ----------------------------
        # Duplicate Name Warning
        # (don't stop insert yet)
        # ----------------------------
        cursor.execute("""
            SELECT code
            FROM subjects
            WHERE LOWER(TRIM(name)) = LOWER(TRIM(%s))
            LIMIT 1
        """, (name,))

        existing = cursor.fetchone()

        if existing:
            flash(
                f"A subject with the same name already exists "
                f"({existing[0]}). Please verify before creating another.",
                "warning"
            )

        # ----------------------------
        # Insert
        # ----------------------------
        cursor.execute("""
            INSERT INTO subjects
            (
                code,
                name,
                department_id,
                sem_number,
                subject_type,
                offering_mode,
                credits,
                created_at
            )
            VALUES
            (
                %s,%s,%s,%s,%s,%s,%s,NOW()
            )
        """, (
            code,
            name,
            int(department_id) if department_id else None,
            sem_number,
            subject_type,
            offering_mode,
            credits
        ))

        conn.commit()

        log_audit(
            current_user.id,
            "add_subject",
            "subjects",
            cursor.lastrowid,
            None,
            f"{code}: {name}",
            ip_address=get_client_ip()
        )

        flash(
            f"Subject '{code} - {name}' added successfully.",
            "success"
        )

    except Exception as e:

        if conn:
            conn.rollback()

        flash(f"Error: {e}", "danger")

    return redirect(url_for("academic.subjects"))


@academic_bp.route("/subjects/<int:subject_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_subject(subject_id):
    """Delete a subject."""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM subjects WHERE id = %s", (subject_id,))
        conn.commit()
        log_audit(
            current_user.id, "delete_subject",
            "subjects", subject_id, None, None,
            ip_address=get_client_ip()
        )
        flash("Subject deleted.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Cannot delete: {e}", "danger")

    return redirect(url_for("academic.subjects"))


# ============================================
# SECTION-SUBJECT ASSIGNMENTS
# ============================================

@academic_bp.route("/section-subjects/<int:section_id>/add", methods=["POST"])
@login_required
@admin_required
def add_section_subject(section_id):
    subject_id = request.form.get("subject_id", "").strip()
    faculty_id = request.form.get("faculty_id", "").strip()

    if not subject_id or not faculty_id:
        flash("Subject and faculty are required.", "danger")
        return redirect(url_for("academic.section_subjects", section_id=section_id))

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)

        cursor.execute("SELECT academic_period_id FROM sections WHERE id=%s", (section_id,))
        section = cursor.fetchone()
        if not section:
            flash("Section not found.", "danger")
            return redirect(url_for("academic.sections"))

        cursor.execute("SELECT offering_mode FROM subjects WHERE id=%s", (int(subject_id),))
        subj = cursor.fetchone()
        if not subj:
            flash("Subject not found.", "danger")
            return redirect(url_for("academic.section_subjects", section_id=section_id))

        # Only 'regular' subjects get direct full-roster section assignment.
        # Everything else must be routed through an elective_group (cluster /
        # open / AEC) so registration — not section membership — drives roster.
        if subj["offering_mode"] != "regular":
            flash(
                f"'{subj['offering_mode'].replace('_',' ').title()}' subjects are assigned "
                "via Electives/AEC groups, not directly to a section.",
                "warning"
            )
            return redirect(url_for("academic.section_subjects", section_id=section_id))

        is_elective = 0  # regular subjects are never electives by definition now
        cursor.execute(
            """
            INSERT INTO section_subjects
                (section_id, subject_id, faculty_id, academic_period_id, is_elective, created_at)
            VALUES (%s, %s, %s, %s, %s, NOW())
            """,
            (section_id, int(subject_id), int(faculty_id), section["academic_period_id"], is_elective)
        )
        conn.commit()
        log_audit(current_user.id, "add_section_subject", "section_subjects",
                   cursor.lastrowid, None, f"section:{section_id}", ip_address=get_client_ip())
        flash("Subject assigned to section.", "success")
    except Exception as e:
        if conn: conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("academic.section_subjects", section_id=section_id))

@academic_bp.route("/sections/<int:section_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_section(section_id):
    """Delete a section."""
    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)

        cursor.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM students WHERE section_id = %s) +
                (SELECT COUNT(*) FROM section_subjects WHERE section_id = %s)
                AS usage_count
            """,
            (section_id, section_id)
        )
        usage = cursor.fetchone()

        if usage["usage_count"] > 0:
            flash(
                f"Cannot delete section. It has {usage['usage_count']} "
                "linked students/subjects. Remove those first.",
                "warning"
            )
            return redirect(url_for("academic.sections"))

        cursor.execute("DELETE FROM cluster_sections WHERE section_id = %s", (section_id,))
        cursor.execute("DELETE FROM sections WHERE id = %s", (section_id,))
        conn.commit()

        log_audit(
            current_user.id, "delete_section",
            "sections", section_id, None, None,
            ip_address=get_client_ip()
        )
        flash("Section deleted.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Cannot delete: {e}", "danger")

    return redirect(url_for("academic.sections"))

@academic_bp.route("/section-subjects/remove/<int:ss_id>", methods=["POST"])
@login_required
@admin_required
def remove_section_subject(ss_id):
    """Remove a subject assignment from a section."""
    section_id = request.form.get("section_id", "").strip()

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM section_subjects WHERE id = %s", (ss_id,))
        conn.commit()
        log_audit(
            current_user.id, "remove_section_subject",
            "section_subjects", ss_id, None, None,
            ip_address=get_client_ip()
        )
        flash("Subject assignment removed.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Error: {e}", "danger")

    if section_id:
        return redirect(url_for("academic.section_subjects", section_id=section_id))
    return redirect(url_for("academic.sections"))
