"""
attendance_system/config.py
Central configuration for the entire application.
All settings, DB config, paths, recognition tunables.
"""

import os
from datetime import timedelta
from dotenv import load_dotenv

# Load .env file if present
load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    """Base configuration."""

    # ----- Flask Core -----
    SECRET_KEY = os.getenv("SECRET_KEY") or os.urandom(32).hex()
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)

    # ----- Database -----
    DB_HOST = os.getenv("DB_HOST", "localhost")
    DB_PORT = int(os.getenv("DB_PORT", "3306"))
    DB_USER = os.getenv("DB_USER", "root")
    DB_PASS = os.getenv("DB_PASS", "")
    DB_NAME = os.getenv("DB_NAME", "attendance_system")

    # ----- Dataset Paths -----
    DATASET_DIR = os.path.join(BASE_DIR, "dataset")
    DATASET_IMG_DIR = os.path.join(DATASET_DIR, "images")
    DATASET_EMB_DIR = os.path.join(DATASET_DIR, "embeddings")
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")


    # ----- Student Table Mapping -----
    # Adjust based on DB schema
    STUDENT_TABLE_FIELDS = {
        "name": "name",          # change if column differs
        "email": "email",
        "phone": "phone",
        "usn": "usn",
        "section_id": "section_id"
    }
    
    # ----- Anti-Spoof Model Paths -----
    ANTI_SPOOF_MODEL_DIR = os.path.join(BASE_DIR, 'anti_spoof_models')

    LIVENESS_THRESHOLD = 0.80

    # ----- Google Sheets -----
    SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE")

    if not SERVICE_ACCOUNT_FILE:
        SERVICE_ACCOUNT_FILE = os.path.join(BASE_DIR, "credentials.json")
    SHEET_ID = os.getenv("SHEET_ID", "")
    SHEET_RANGE = os.getenv("SHEET_RANGE", "'Form Responses 1'!A:Z")

    # ----- Student Identification (VERY IMPORTANT) -----
    # Defines how students are identified during enrollment
    # Options: "email", "usn", "sid", "phone"
    PRIMARY_ID_FIELD = os.getenv("PRIMARY_ID_FIELD", "email")

    # Optional fallback fields (used if primary not available)
    FALLBACK_ID_FIELDS = ["email", "phone", "usn", "sid"]

    # Map Google Sheet column names (flexible matching)
    SHEET_COLUMN_MAP = {
        "email": ["EMAIL", "EMAIL ADDRESS"],
        "usn": ["USN", "ROLL NO", "ROLL NUMBER"],
        "sid": ["SID", "STUDENT ID"],
        "name": ["NAME", "FULL NAME"],
        "phone": ["PHONE", "MOBILE", "CONTACT"],
        "selfie": ["SELFIE", "PHOTO", "IMAGE", "ENROLL VIA SELFIE"]
    }
    # ----- Recognition Tunables -----
    RECOGNITION_THRESHOLD = float(os.getenv("RECOGNITION_THRESHOLD", "0.35"))
    DETECTION_THRESHOLD = float(os.getenv("DETECTION_THRESHOLD", "0.60"))
    MIN_FACE_SIZE = int(os.getenv("MIN_FACE_SIZE", "80"))
    LIVENESS_THRESHOLD = float(os.getenv("LIVENESS_THRESHOLD", "0.80"))
    ATTENDANCE_COOLDOWN_MIN = int(os.getenv("ATTENDANCE_COOLDOWN_MIN", "55"))

    # ----- Recognition Engine -----
    RECOGNITION_PROCESS_FPS = float(os.getenv("RECOGNITION_FPS", "8"))
    MAX_STREAM_THREADS = int(os.getenv("MAX_STREAM_THREADS", "10"))
    INSIGHTFACE_MODEL = os.getenv("INSIGHTFACE_MODEL", "buffalo_l")
    DETECTION_SIZE = (640, 640)

    # ----- Grid Focus (for merged recognition) -----
    FOCUS_GRID = (3, 3)
    FOCUS_TOP_K = 1
    FOCUS_UPSCALE = 1.6
    FOCUS_MARGIN = 0.18
    UNKNOWN_SUPPRESS_SEC = 8.0
    PROCESSED_SID_TTL = 300.0

    # ----- Attendance Thresholds (for reports) -----
    ATTENDANCE_RED_THRESHOLD = 75    # Below this = defaulter
    ATTENDANCE_ORANGE_THRESHOLD = 85  # Below this = warning

    # ----- Session Auto-Scheduler -----
    SESSION_AUTO_OPEN_TIME = os.getenv("SESSION_AUTO_OPEN_TIME", "08:00")

    # ----- Color codes (BGR for OpenCV overlays) -----
    COLOR_GREEN = (0, 255, 0)
    COLOR_ORANGE = (0, 165, 255)
    COLOR_RED = (0, 0, 255)
    COLOR_WHITE = (255, 255, 255)

    @staticmethod
    def ensure_dirs():
        """Create all necessary directories."""
        dirs = [
            Config.DATASET_DIR,
            Config.DATASET_IMG_DIR,
            Config.DATASET_EMB_DIR,
            Config.UPLOAD_FOLDER,
            Config.ANTI_SPOOF_MODEL_DIR,
            os.path.join(BASE_DIR, "static", "css"),
            os.path.join(BASE_DIR, "static", "js"),
            os.path.join(BASE_DIR, "static", "img"),
        ]
        for d in dirs:
            os.makedirs(d, exist_ok=True) 