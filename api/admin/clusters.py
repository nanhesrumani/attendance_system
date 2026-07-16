"""
attendance_system/api/admin/clusters.py
Cluster Management
"""

import logging

from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash
)

from flask_login import current_user

from core.db import get_db, get_cursor
from auth.helpers import (
    admin_required,
    log_audit,
    get_client_ip
)

logger = logging.getLogger(__name__)

cluster_bp = Blueprint(
    "clusters",
    __name__,
    url_prefix="/admin/clusters",
    template_folder="../../templates/admin"
)

@cluster_bp.route("/")
@admin_required
def list_clusters():
    """Backward-compatible entry point for cluster management."""
    return redirect(url_for("academic.departments"))


@cluster_bp.route("/department/<int:department_id>/add", methods=["POST"])
@admin_required
def add_cluster(department_id):
    """Create a department-level cluster."""
    cluster_name = request.form.get("cluster_name", "").strip()
    cluster_head_faculty_id = request.form.get("cluster_head_faculty_id", "").strip()
    remarks = request.form.get("remarks", "").strip()

    if not cluster_name:
        flash("Cluster name is required.", "danger")
        return redirect(url_for("academic.department_workspace", dept_id=department_id, tab="clusters"))

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO clusters
                (department_id, semester, cluster_name,
                 cluster_head_faculty_id, remarks, created_at)
            VALUES (%s, 0, %s, %s, %s, NOW())
            """,
            (
                department_id,
                cluster_name,
                int(cluster_head_faculty_id) if cluster_head_faculty_id else None,
                remarks if remarks else None
            )
        )
        cluster_id = cursor.lastrowid

        if cluster_head_faculty_id:
            cursor.execute(
                """
                UPDATE faculty
                SET department_id = %s, cluster_id = %s
                WHERE id = %s
                """,
                (department_id, cluster_id, int(cluster_head_faculty_id))
            )

        conn.commit()
        log_audit(
            current_user.id,
            "add_cluster",
            "clusters",
            cluster_id,
            None,
            cluster_name,
            ip_address=get_client_ip()
        )
        flash("Cluster created.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error("add_cluster error: %s", e)
        flash(f"Error: {e}", "danger")

    return redirect(url_for("academic.department_workspace", dept_id=department_id, tab="clusters"))


@cluster_bp.route("/<int:cluster_id>/edit", methods=["POST"])
@admin_required
def edit_cluster(cluster_id):
    """Edit a department-level cluster."""
    department_id = request.form.get("department_id", type=int)
    cluster_name = request.form.get("cluster_name", "").strip()
    cluster_head_faculty_id = request.form.get("cluster_head_faculty_id", "").strip()
    remarks = request.form.get("remarks", "").strip()

    if not department_id:
        flash("Department is required.", "danger")
        return redirect(url_for("academic.departments"))

    if not cluster_name:
        flash("Cluster name is required.", "danger")
        return redirect(url_for("academic.department_workspace", dept_id=department_id, tab="clusters"))

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE clusters
            SET cluster_name = %s,
                cluster_head_faculty_id = %s,
                remarks = %s
            WHERE id = %s
              AND department_id = %s
            """,
            (
                cluster_name,
                int(cluster_head_faculty_id) if cluster_head_faculty_id else None,
                remarks if remarks else None,
                cluster_id,
                department_id
            )
        )

        if cluster_head_faculty_id:
            cursor.execute(
                """
                UPDATE faculty
                SET department_id = %s, cluster_id = %s
                WHERE id = %s
                """,
                (department_id, cluster_id, int(cluster_head_faculty_id))
            )

        conn.commit()
        log_audit(
            current_user.id,
            "edit_cluster",
            "clusters",
            cluster_id,
            None,
            cluster_name,
            ip_address=get_client_ip()
        )
        flash("Cluster updated.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error("edit_cluster error: %s", e)
        flash(f"Error: {e}", "danger")

    return redirect(url_for("academic.department_workspace", dept_id=department_id, tab="clusters"))


@cluster_bp.route("/<int:cluster_id>/delete", methods=["POST"])
@admin_required
def delete_cluster(cluster_id):
    """Delete an unused cluster."""
    department_id = request.form.get("department_id", type=int)
    if not department_id:
        flash("Department is required.", "danger")
        return redirect(url_for("academic.departments"))

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor(dictionary=True, buffered=True)
        cursor.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM faculty WHERE cluster_id = %s) AS faculty_count,
                (SELECT COUNT(*) FROM cluster_sections WHERE cluster_id = %s) AS section_count
            """,
            (cluster_id, cluster_id)
        )
        usage = cursor.fetchone()
        if usage and (usage["faculty_count"] or usage["section_count"]):
            flash("Cannot delete a cluster with assigned faculty or sections.", "warning")
            return redirect(url_for("academic.department_workspace", dept_id=department_id, tab="clusters"))

        cursor.execute(
            "DELETE FROM clusters WHERE id = %s AND department_id = %s",
            (cluster_id, department_id)
        )
        conn.commit()
        log_audit(
            current_user.id,
            "delete_cluster",
            "clusters",
            cluster_id,
            None,
            None,
            ip_address=get_client_ip()
        )
        flash("Cluster deleted.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error("delete_cluster error: %s", e)
        flash(f"Error: {e}", "danger")

    return redirect(url_for("academic.department_workspace", dept_id=department_id, tab="clusters"))


@cluster_bp.route("/<int:cluster_id>/assign-faculty", methods=["POST"])
@admin_required
def assign_faculty(cluster_id):
    """Assign selected faculty to one cluster."""
    department_id = request.form.get("department_id", type=int)
    faculty_ids = request.form.getlist("faculty_ids", type=int)

    if not department_id:
        flash("Department is required.", "danger")
        return redirect(url_for("academic.departments"))

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        if faculty_ids:
            fmt = ",".join(["%s"] * len(faculty_ids))
            cursor.execute(
                f"""
                UPDATE faculty
                SET cluster_id = %s,
                    department_id = %s
                WHERE id IN ({fmt})
                  AND department_id = %s
                """,
                tuple([cluster_id, department_id] + faculty_ids + [department_id])
            )
        conn.commit()
        flash("Faculty assigned to cluster.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error("assign_faculty error: %s", e)
        flash(f"Error: {e}", "danger")

    return redirect(url_for("academic.department_workspace", dept_id=department_id, tab="clusters"))


@cluster_bp.route("/<int:cluster_id>/assign-sections", methods=["POST"])
@admin_required
def assign_sections(cluster_id):
    """Assign selected sections to exactly one cluster."""
    department_id = request.form.get("department_id", type=int)
    section_ids = request.form.getlist("section_ids", type=int)

    if not department_id:
        flash("Department is required.", "danger")
        return redirect(url_for("academic.departments"))

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()

        if section_ids:
            fmt = ",".join(["%s"] * len(section_ids))
            cursor.execute(
                f"""
                DELETE cs
                FROM cluster_sections cs
                JOIN sections sec ON sec.id = cs.section_id
                WHERE sec.id IN ({fmt})
                  AND sec.department_id = %s
                """,
                tuple(section_ids + [department_id])
            )

            cursor.executemany(
                """
                INSERT INTO cluster_sections (cluster_id, section_id)
                VALUES (%s, %s)
                """,
                [(cluster_id, sid) for sid in section_ids]
            )

        conn.commit()
        flash("Sections assigned to cluster.", "success")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error("assign_sections error: %s", e)
        flash(f"Error: {e}", "danger")

    return redirect(url_for("academic.department_workspace", dept_id=department_id, tab="clusters"))


@cluster_bp.route("/<int:cluster_id>/remove-section/<int:section_id>", methods=["POST"])
@admin_required
def remove_section(cluster_id, section_id):
    """Remove one section from a cluster."""
    department_id = request.form.get("department_id", type=int)
    if not department_id:
        flash("Department is required.", "danger")
        return redirect(url_for("academic.departments"))

    conn = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            DELETE FROM cluster_sections
            WHERE cluster_id = %s
              AND section_id = %s
            """,
            (cluster_id, section_id)
        )
        conn.commit()
        flash("Section removed from cluster.", "info")
    except Exception as e:
        if conn:
            conn.rollback()
        flash(f"Error: {e}", "danger")

    return redirect(url_for("academic.department_workspace", dept_id=department_id, tab="clusters"))


def _legacy_cluster_context():
    """Legacy context builder retained for old template compatibility."""

    cursor = get_cursor()

    # -------------------------------
    # Load Clusters
    # -------------------------------
    cursor.execute("""
        SELECT

            c.id,
            c.cluster_name,
             
            c.remarks,

            d.id AS department_id,
            d.code AS department,

            u.full_name AS cluster_head,

            (
                SELECT COUNT(*)
                FROM cluster_sections cs
                WHERE cs.cluster_id = c.id
            ) AS section_count,

            (
                SELECT COUNT(*)
                FROM faculty f
                WHERE f.cluster_id = c.id
            ) AS faculty_count

        FROM clusters c

        JOIN departments d
            ON d.id = c.department_id

        LEFT JOIN faculty f
            ON f.id = c.cluster_head_faculty_id

        LEFT JOIN users u
            ON u.id = f.user_id

        ORDER BY
            d.code,
             
            c.cluster_name
    """)

    clusters = cursor.fetchall()

    # -------------------------------
    # Load Departments
    # -------------------------------
    cursor.execute("""
        SELECT
            id,
            code,
            name
        FROM departments
        ORDER BY code
    """)

    departments = cursor.fetchall()

    # -------------------------------
    # Load Faculty
    # -------------------------------
    cursor.execute("""
        SELECT
            f.id,
            u.full_name
        FROM faculty f
        JOIN users u
            ON u.id = f.user_id
        ORDER BY
            u.full_name
    """)

    faculty = cursor.fetchall()

    # -------------------------------
    # Dashboard Statistics
    # -------------------------------
    stats = {
        "total": len(clusters),
        "faculty": sum(c["faculty_count"] for c in clusters),
        "sections": sum(c["section_count"] for c in clusters)
    }

    # -------------------------------
    # Render Page
    # -------------------------------
    return render_template(
        "admin/clusters.html",
        clusters=clusters,
        departments=departments,
        faculty=faculty,
        stats=stats
    )
