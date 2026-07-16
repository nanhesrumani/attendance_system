# api/admin/subjects_maintenance.py

from flask import Blueprint, jsonify, request, render_template
from flask_login import current_user, login_required
from core.db import get_db, get_cursor
from auth.helpers import admin_required, log_audit, get_client_ip
import difflib
from api.admin.timetable import normalize_text, build_acronym

subj_maint_bp = Blueprint("subj_maint", __name__, url_prefix="/admin/subjects")


@subj_maint_bp.route("/dedup")
@login_required
@admin_required
def dedup_page():
    """Renders the duplicate-review screen; data loads via find-duplicates JSON."""
    return render_template("admin/subjects_dedup.html")


@subj_maint_bp.route("/find-duplicates")
@admin_required
def find_duplicates():
    """
    Scans all subjects grouped by (department_id, sem_number) and reports
    pairs that look like the same subject — by acronym match or high fuzzy
    similarity — for the admin to review before merging.
    """
    cursor = get_cursor()
    cursor.execute("SELECT id, code, name, department_id, sem_number FROM subjects")
    subjects = cursor.fetchall()

    groups = {}
    for s in subjects:
        groups.setdefault((s["department_id"], s["sem_number"]), []).append(s)

    suggestions = []
    for _, group in groups.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                na, nb = normalize_text(a["name"]), normalize_text(b["name"])
                score = difflib.SequenceMatcher(None, na, nb).ratio()
                acr_hit = (
                    build_acronym(a["name"]) == normalize_text(b["code"]) or
                    build_acronym(b["name"]) == normalize_text(a["code"]) or
                    build_acronym(a["name"]) == build_acronym(b["name"])
                )
                if score >= 0.7 or acr_hit:
                    suggestions.append({
                        "subject_a": a, "subject_b": b,
                        "similarity": round(score, 2), "acronym_match": acr_hit,
                    })

    suggestions.sort(key=lambda x: (x["acronym_match"], x["similarity"]), reverse=True)
    return jsonify({"suggestions": suggestions})


@subj_maint_bp.route("/merge", methods=["POST"])
@admin_required
def merge_subjects():
    """
    Merge duplicate_id INTO primary_id: repoint every FK, keep primary's
    row, register duplicate's code+name as aliases of primary, delete duplicate.
    """
    primary_id = request.form.get("primary_id", type=int)
    duplicate_id = request.form.get("duplicate_id", type=int)
    if not primary_id or not duplicate_id or primary_id == duplicate_id:
        return jsonify({"success": False, "message": "Invalid subject ids."}), 400

    conn = get_db()
    cursor = conn.cursor(dictionary=True, buffered=True)
    try:
        cursor.execute("SELECT id, code, name FROM subjects WHERE id IN (%s,%s)",
                        (primary_id, duplicate_id))
        rows = {r["id"]: r for r in cursor.fetchall()}
        if primary_id not in rows or duplicate_id not in rows:
            return jsonify({"success": False, "message": "Subject not found."}), 404
        dup = rows[duplicate_id]

        fk_tables = [
            ("timetable", "subject_id"),
            ("sessions", "subject_id"),
            ("section_subjects", "subject_id"),
            ("cluster_subjects", "subject_id"),
            ("faculty_subjects", "subject_id"),
            ("elective_groups", "subject_id"),
        ]
        for table, col in fk_tables:
            try:
                cursor.execute(f"UPDATE {table} SET {col} = %s WHERE {col} = %s",
                                (primary_id, duplicate_id))
            except Exception:
                pass  # table may not exist in every deployment state yet

        cursor.execute("""
            DELETE ss1 FROM section_subjects ss1
            JOIN section_subjects ss2
              ON ss1.section_id = ss2.section_id
             AND ss1.subject_id = ss2.subject_id
             AND ss1.academic_period_id = ss2.academic_period_id
             AND ss1.id > ss2.id
            WHERE ss1.subject_id = %s
        """, (primary_id,))
        cursor.execute("""
            DELETE cs1 FROM cluster_subjects cs1
            JOIN cluster_subjects cs2
              ON cs1.cluster_id = cs2.cluster_id AND cs1.subject_id = cs2.subject_id
             AND cs1.id > cs2.id
            WHERE cs1.subject_id = %s
        """, (primary_id,))

        for text, atype in [(dup["code"], "code"), (dup["name"], "name_variant")]:
            norm = normalize_text(text)
            cursor.execute("SELECT id FROM subject_aliases WHERE alias_normalized=%s", (norm,))
            existing = cursor.fetchone()
            if existing:
                cursor.execute("UPDATE subject_aliases SET subject_id=%s WHERE id=%s",
                                (primary_id, existing["id"]))
            else:
                cursor.execute(
                    """INSERT INTO subject_aliases
                       (subject_id, alias_raw, alias_normalized, alias_type, confidence, created_by, created_at)
                       VALUES (%s,%s,%s,%s,'exact',%s,NOW())""",
                    (primary_id, text[:150], norm[:150], atype, current_user.id)
                )
        cursor.execute("UPDATE subject_aliases SET subject_id=%s WHERE subject_id=%s",
                        (primary_id, duplicate_id))

        cursor.execute("DELETE FROM subjects WHERE id = %s", (duplicate_id,))
        conn.commit()

        log_audit(current_user.id, "merge_subjects", "subjects", primary_id,
                  old_value=str(dup), new_value=str(primary_id), ip_address=get_client_ip())
        return jsonify({"success": True, "message": f"Merged '{dup['name']}' into subject {primary_id}."})

    except Exception as exc:
        conn.rollback()
        return jsonify({"success": False, "message": str(exc)}), 500