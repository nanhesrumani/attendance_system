"""
attendance_system/recognition/classroom_photo.py
Bulk face recognition on faculty-uploaded classroom photo(s).
Reuses the existing RecognitionEngine (InsightFace) — no new model, no liveness.
Multiple photos (wide classes) are merged and de-duplicated by student_id.
"""

import logging
import cv2
import numpy as np
from PIL import Image

from config import Config

logger = logging.getLogger(__name__)

# Classroom photos have many small/far faces — looser than live-stream defaults.
CLASSROOM_MIN_FACE_SIZE = 35
CLASSROOM_DETECTION_THRESHOLD = 0.50


def _load_bgr(image_path):
    """Load an image file as BGR numpy array (handles EXIF rotation via PIL)."""
    pil_img = Image.open(image_path)
    pil_img = pil_img.convert("RGB")
    rgb = np.array(pil_img)
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    return bgr


def recognize_classroom_photos(image_paths, recognition_engine, section_id,
                                threshold=None, min_face_size=None,
                                detection_threshold=None):
    """
    Run face recognition on 1-3 classroom photos and merge results.

    Args:
        image_paths: list of file paths (1 for small class, 2-3 for wide class)
        recognition_engine: the app-wide RecognitionEngine instance (already initialized,
                             embedding index already loaded by caller)
        section_id: section_id of the session, used to ignore matches outside this class
        threshold: recognition similarity threshold (default Config.RECOGNITION_THRESHOLD)
        min_face_size: min face bbox side in px (default CLASSROOM_MIN_FACE_SIZE)
        detection_threshold: min detector confidence (default CLASSROOM_DETECTION_THRESHOLD)

    Returns dict:
        {
          "matched": [{"student_id", "usn", "full_name", "score", "source_photo"}],
          "unrecognized_faces": [{"source_photo", "bbox", "det_score"}],
          "total_faces_detected": int,
          "photos_processed": int,
        }
    """
    if threshold is None:
        threshold = Config.RECOGNITION_THRESHOLD
    if min_face_size is None:
        min_face_size = CLASSROOM_MIN_FACE_SIZE
    if detection_threshold is None:
        detection_threshold = CLASSROOM_DETECTION_THRESHOLD

    # best match per student_id across all photos (keep highest score)
    best_by_student = {}
    unrecognized_faces = []
    total_faces_detected = 0

    for photo_index, image_path in enumerate(image_paths):
        try:
            bgr = _load_bgr(image_path)
        except Exception as e:
            logger.error("classroom_photo: failed to load %s: %s", image_path, e)
            continue

        faces = recognition_engine.detect_faces(bgr)
        total_faces_detected += len(faces)

        for face in faces:
            x1, y1, x2, y2 = recognition_engine.get_face_bbox(face)
            w, h = x2 - x1, y2 - y1
            det_score = recognition_engine.get_det_score(face)

            if min(w, h) < min_face_size or det_score < detection_threshold:
                continue

            emb = recognition_engine.get_embedding(face)
            if emb is None:
                continue

            student_id, score = recognition_engine.match_top1(emb)

            if student_id is not None and score >= threshold:
                info = recognition_engine._student_cache.get(student_id)
                # ignore matches from a different section (e.g. someone walked past)
                if info and info.get("section_id") == section_id:
                    prev = best_by_student.get(student_id)
                    if prev is None or score > prev["score"]:
                        best_by_student[student_id] = {
                            "student_id": student_id,
                            "usn": info["usn"],
                            "full_name": info["name"],
                            "score": float(score),
                            "source_photo": photo_index,
                            "bbox": (x1, y1, x2, y2),
                        }
            else:
                unrecognized_faces.append({
                    "source_photo": photo_index,
                    "bbox": (x1, y1, x2, y2),
                    "det_score": float(det_score),
                    "best_score": float(score),
                })

    matched = sorted(best_by_student.values(), key=lambda m: m["usn"] or "")

    return {
        "matched": matched,
        "unrecognized_faces": unrecognized_faces,
        "total_faces_detected": total_faces_detected,
        "photos_processed": len(image_paths),
    }