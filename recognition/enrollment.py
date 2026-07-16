"""
attendance_system/recognition/enrollment.py
Student face enrollment: webcam capture, photo upload, bulk ZIP, Google Sheets.
All methods save to DB (new schema) + dataset files.
"""

import email
import os
import io
import zipfile
import logging
import pickle

import cv2
import numpy as np
import requests
from PIL import Image

from config import Config
from recognition.utils import (
    normalize_embedding,
    serialize_embedding,
    save_face_image,
    save_embedding_npz,
    pil_to_bgr,
    bgr_to_pil,
    download_image_from_url,
)

logger = logging.getLogger(__name__)


class EnrollmentManager:
    """Handles all face enrollment operations."""

    def __init__(self, engine):
        """
        Args:
            engine: RecognitionEngine instance
        """
        self.engine = engine

    def enroll_from_image(self, pil_image, student_id, usn, db_pool,
                          enrollment_method="single_photo"):
        """
        Enroll a single student from a PIL image.

        Args:
            pil_image: PIL Image (RGB)
            student_id: int (students.id)
            usn: str (students.usn)
            db_pool: MySQL connection pool
            enrollment_method: str

        Returns:
            dict with keys: success, message, image_path, embedding_path
        """
        if not self.engine.is_initialized:
            return {"success": False, "message": "Recognition engine not initialized."}

        # Compute embedding
        emb, face_crop_pil = self.engine.compute_embedding_from_pil(pil_image)
        if emb is None:
            return {"success": False, "message": "No face detected in the image."}

        # Save face crop image
        try:
            img_path = save_face_image(student_id, usn, face_crop_pil)
        except Exception as e:
            logger.error(f"save_face_image error: {e}")
            img_path = None

        # Save embedding file
        try:
            emb_path = save_embedding_npz(student_id, emb)
        except Exception as e:
            logger.error(f"save_embedding_npz error: {e}")
            emb_path = None

        # Save to DB
        try:
            conn = db_pool.get_connection()
            cursor = conn.cursor(dictionary=True, buffered=True)

            # Delete existing face embeddings for this student (full region)
            cursor.execute(
                "DELETE FROM faces WHERE student_id = %s AND region = 'full'",
                (student_id,)
            )

            # Insert new embedding
            blob = serialize_embedding(emb)
            cursor.execute(
                """
                INSERT INTO faces
                    (student_id, usn, region, embedding, enrollment_method, created_at)
                VALUES (%s, %s, 'full', %s, %s, NOW())
                """,
                (student_id, usn, blob, enrollment_method)
            )

            # Update student image_path and enrollment_status
            status = "enrolled_single_photo" if enrollment_method == "single_photo" else "fully_enrolled"
            cursor.execute(
                """
                UPDATE students
                SET image_path = %s,
                    enrollment_status = %s
                WHERE id = %s
                """,
                (img_path, status, student_id)
            )

            conn.commit()
            cursor.close()
            conn.close()

            # Reload embedding index
            self.engine.reload_embedding_index(db_pool)
            self.engine.refresh_student_cache(db_pool)

            logger.info(f"Enrolled student {usn} (ID={student_id}) via {enrollment_method}")
            return {
                "success": True,
                "message": f"Successfully enrolled {usn}.",
                "image_path": img_path,
                "embedding_path": emb_path,
            }

        except Exception as e:
            logger.error(f"DB enrollment error for {usn}: {e}")
            try:
                conn.rollback()
                cursor.close()
                conn.close()
            except Exception:
                pass
            return {"success": False, "message": f"Database error: {e}"}

    def enroll_from_uploaded_file(self, file_storage, student_id, usn, db_pool):
        """
        Enroll from a Flask FileStorage (uploaded file).

        Args:
            file_storage: Flask request.files['photo']
            student_id: int
            usn: str
            db_pool: MySQL connection pool
        """
        try:
            content = file_storage.read()
            pil = Image.open(io.BytesIO(content)).convert("RGB")
        except Exception as e:
            return {"success": False, "message": f"Cannot read image: {e}"}

        return self.enroll_from_image(
            pil, student_id, usn, db_pool, enrollment_method="single_photo"
        )

    def enroll_from_webcam_frame(self, bgr_frame, student_id, usn, db_pool):
        """
        Enroll from a BGR webcam frame (numpy array).
        """
        pil = bgr_to_pil(bgr_frame)
        return self.enroll_from_image(
            pil, student_id, usn, db_pool, enrollment_method="webcam"
        )

    def enroll_from_bulk_zip(self, zip_file_storage, db_pool):
        """
        Bulk enroll from a ZIP file.
        Each image in the ZIP should be named as USN.jpg (e.g., 1BY23CS114.jpg).

        Args:
            zip_file_storage: Flask FileStorage for ZIP file
            db_pool: MySQL connection pool

        Returns:
            dict with keys: success, total, enrolled, failed, errors
        """
        results = {
            "success": True,
            "total": 0,
            "enrolled": 0,
            "failed": 0,
            "errors": [],
        }

        try:
            zip_data = io.BytesIO(zip_file_storage.read())
            zf = zipfile.ZipFile(zip_data, "r")
        except Exception as e:
            return {
                "success": False, "total": 0, "enrolled": 0, "failed": 0,
                "errors": [f"Cannot read ZIP file: {e}"]
            }

        # Get student USN -> ID mapping from DB
        try:
            conn = db_pool.get_connection()
            cursor = conn.cursor(dictionary=True, buffered=True)
            cursor.execute("SELECT id, sid, usn FROM students")
            sid_map = {
                ((row.get("sid") or row.get("usn") or "").strip().upper()): row["id"]
                for row in cursor.fetchall()
                if (row.get("sid") or row.get("usn"))
            }
            cursor.close()
            conn.close()
        except Exception as e:
            return {
                "success": False, "total": 0, "enrolled": 0, "failed": 0,
                "errors": [f"DB error: {e}"]
            }

        image_extensions = {".jpg", ".jpeg", ".png", ".heic"}

        for name in zf.namelist():
            # Skip directories and hidden files
            if name.endswith("/") or name.startswith("__") or name.startswith("."):
                continue

            ext = os.path.splitext(name)[1].lower()
            if ext not in image_extensions:
                continue

            results["total"] += 1

            # Extract SID from filename
            sid_raw = os.path.splitext(os.path.basename(name))[0].strip().upper()
            if sid_raw not in sid_map:
                results["failed"] += 1
                results["errors"].append(f"{name}: SID '{sid_raw}' not found in DB.")
                continue

            student_id = sid_map[sid_raw]

            try:
                img_data = zf.read(name)
                pil = Image.open(io.BytesIO(img_data)).convert("RGB")
            except Exception as e:
                results["failed"] += 1
                results["errors"].append(f"{name}: Cannot read image - {e}")
                continue

            result = self.enroll_from_image(
                pil, student_id, sid_raw, db_pool, enrollment_method="bulk_zip"
            )

            if result["success"]:
                results["enrolled"] += 1
            else:
                results["failed"] += 1
                results["errors"].append(f"{name}: {result['message']}")

        zf.close()
        logger.info(
            f"Bulk ZIP enrollment: {results['enrolled']}/{results['total']} succeeded."
        )
        return results

    # def download_image_from_url(url):
    #     try:
    #         # 🔥 Convert Google Drive link → direct link
    #         if "drive.google.com" in url:
    #             import re
                
    #             file_id = None

    #             # Extract ID
    #             if "id=" in url:
    #                 file_id = url.split("id=")[-1]
    #             elif "/d/" in url:
    #                 file_id = url.split("/d/")[1].split("/")[0]

    #             if not file_id:
    #                 return None

    #             # 🔥 Use thumbnail instead of download
    #             url = f"https://drive.google.com/thumbnail?id={file_id}"

    #         headers = {
    #             "User-Agent": "Mozilla/5.0"
    #         }

    #         resp = requests.get(url, timeout=10, headers=headers)
    #         if resp.status_code != 200:
    #             return None

    #         img_array = np.frombuffer(resp.content, np.uint8)
    #         img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)

    #         response = requests.get(url)

    #         print("CONTENT TYPE:", response.headers.get("Content-Type"))

    #         return img

    #     except Exception as e:
    #         print("Image download error:", e)
    #         return None
        
    def enroll_from_google_sheet(self, df, db_pool, default_section_id=1, default_dept_id=1):
        """
        FIXED: Bulk enrollment from Google Sheets with proper error handling.
        """
        results = {
            "total": len(df),
            "enrolled": 0,
            "skipped": 0,
            "failed": 0,
            "created": 0,  # NEW: Track new students created
            "errors": [],
        }

        if df.empty:
            results["errors"].append("Sheet is empty.")
            return results

        # Normalize column names
        df.columns = df.columns.str.strip().str.upper()

        # Flexible column detection
        def find_col(keyword):
            for c in df.columns:
                if keyword in c:
                    return c
            return None

        # 🔍 DEBUG: Print all columns and first row
        logger.info(f"🔍 ALL COLUMNS: {list(df.columns)}")
        logger.info(f"📊 FIRST ROW: {df.iloc[0].to_dict()}")

        # 🎯 Improved flexible column detection
        sid_col = find_col("SID") or find_col("USN") or find_col("ROLL")
        name_col = find_col("NAME") or find_col("FULL")
        email_col = find_col("EMAIL")
        phone_col = find_col("PHONE") or find_col("MOBILE")
        selfie_col = (
            find_col("SELFIE") 
            or find_col("ENROLL VIA SELFIE") 
            or find_col("PHOTO") 
            or find_col("IMAGE")
        )

        # 🔍 Log detected columns
        logger.info(f"🎯 DETECTED: SID='{sid_col}', Selfie='{selfie_col}', Name='{name_col}'")

        if not sid_col or not selfie_col:
            results["errors"].append(
                f"❌ Missing columns! Need: SID/USN ansd SELFIE/PHOTO/IMAGE. Found: {list(df.columns)}"
            )
            return results

        logger.info(f"🔍 Columns found - SID: '{sid_col}', Selfie: '{selfie_col}'")

        conn = None
        cursor = None
        try:
            conn = db_pool.get_connection()
            cursor = conn.cursor(dictionary=True, buffered=True)

            # Get existing students SID -> ID mapping
            cursor.execute("SELECT id, sid, usn FROM students WHERE sid IS NOT NULL OR usn IS NOT NULL")
            sid_map = {}
            usn_map = {}
            for row in cursor.fetchall():
                sid = (row.get("sid") or "").strip().upper()
                usn = (row.get("usn") or "").strip().upper()
                if sid:
                    sid_map[sid] = row["id"]
                if usn:
                    usn_map[usn] = row["id"]

            # Get already enrolled students
            cursor.execute("SELECT DISTINCT student_id FROM faces WHERE region='full'")
            already_enrolled = {row["student_id"] for row in cursor.fetchall()}

            for idx, row in df.iterrows():

                # 🚫 Skip fully empty rows
                if row.isnull().all():
                    continue
                # Extract data
                sid_raw = row.get(sid_col)

                # 🚫 Proper SID validation (CORRECT PLACE)
                import pandas as pd
                if not sid_raw or pd.isna(sid_raw) or str(sid_raw).strip().lower() in ["", "nan"]:
                    logger.info(f"⏭️ Skipping empty SID at row {idx+2}")
                    continue

                raw_sid = str(sid_raw).strip().upper()
                name = str(row.get(name_col, raw_sid)).strip()
                email = str(row.get(email_col, f"{raw_sid}@student.example.com")).strip().lower()
                phone = str(row.get(phone_col, "")).strip()
                selfie_url = str(row.get(selfie_col, "")).strip()

                if not selfie_url or not selfie_url.startswith(("http://", "https://")):
                    results["skipped"] += 1
                    results["errors"].append(f"Row {idx+2} ({raw_sid}): Invalid selfie URL")
                    continue

                sid = raw_sid  # Clean SID

                # Check if student exists (SID or USN)
                student_id = sid_map.get(sid) or usn_map.get(sid)

                logger.info(f"🚀 Processing row {idx+2} SID={row.get(sid_col)}")

                # 🔥 AUTO-CREATE STUDENT IF NOT EXISTS
                if not student_id:
                    try:
                        # 🔍 STEP 1: Check if user already exists
                        cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
                        existing_user = cursor.fetchone()

                        if existing_user:
                            user_id = existing_user["id"]   # ✅ Use existing user
                        else:
                            from werkzeug.security import generate_password_hash

                            default_password = "student123"
                            password_hash = generate_password_hash(default_password)

                            cursor.execute("""
                                INSERT INTO users (email, full_name, password_hash, role, status, created_at)
                                VALUES (%s, %s, %s, 'student', 'active', NOW())
                            """, (email, name, password_hash))

                            user_id = cursor.lastrowid   # ✅ New user created

                       # 🔍 Check if student already exists for this user
                        cursor.execute("SELECT id FROM students WHERE user_id = %s", (user_id,))
                        existing_student = cursor.fetchone()

                        if existing_student:
                            student_id = existing_student["id"]
                            logger.info(f"⚠️ Student already exists for user {user_id}, using existing ID {student_id}")
                        else:
                            # ✅ SAFE TO INSERT
                            cursor.execute("""
                                INSERT INTO students 
                                    (user_id, usn, sid, section_id, department_id, 
                                    name, email, phone, enrollment_status, created_at)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'enrolled_single_photo', NOW())
                            """, (user_id, sid, sid, default_section_id, default_dept_id, 
                                name, email, phone))
                            
                            student_id = cursor.lastrowid
                            sid_map[sid] = student_id
                            results["created"] += 1
                            logger.info(f"✅ Created new student: {sid} (ID: {student_id})")

                    except Exception as e:
                        results["failed"] += 1
                        results["errors"].append(f"Row {idx+2} ({sid}): Create failed - {e}")
                        continue

                # Skip if already enrolled
                if student_id in already_enrolled:
                    results["skipped"] += 1
                    continue

                # 🔥 DOWNLOAD & ENROLL
                try:
                    from recognition.utils import download_image_from_url
                    bgr_img = download_image_from_url(selfie_url)
                    
                    if bgr_img is None:
                        results["failed"] += 1
                        results["errors"].append(f"Row {idx+2} ({sid}): Image download failed")
                        continue

                    pil_img = bgr_to_pil(bgr_img)
                    enroll_result = self.enroll_from_image(
                        pil_img, student_id, sid, db_pool, 
                        enrollment_method="google_sheet"
                    )

                    logger.info(f"📥 Downloaded image for {sid}")

                    if enroll_result["success"]:
                        results["enrolled"] += 1
                        already_enrolled.add(student_id)
                        logger.info(f"✅ Enrolled {sid}")
                    else:
                        results["failed"] += 1
                        results["errors"].append(f"Row {idx+2} ({sid}): {enroll_result['message']}")

                except Exception as e:
                    results["failed"] += 1
                    results["errors"].append(f"Row {idx+2} ({sid}): {e}")

            conn.commit()
            logger.info(f"Sheet complete: {results}")

        except Exception as e:
            results["errors"].append(f"Database error: {e}")
            logger.error(f"Sheet enrollment failed: {e}")
            if conn:
                conn.rollback()
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

        return results