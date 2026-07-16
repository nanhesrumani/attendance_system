"""
attendance_system/setup_check.py
Run this to verify everything is installed and configured correctly.
Usage: python setup_check.py
"""

import os
import sys

print("=" * 60)
print("  AI Attendance System — Setup Verification")
print("=" * 60)

errors = []
warnings = []

# ---- Python Version ----
print(f"\n[1] Python: {sys.version}")
if sys.version_info < (3, 9):
    errors.append("Python 3.9+ required")
else:
    print("    ✅ Python version OK")

# ---- Required Packages ----
print("\n[2] Required Packages:")

packages = {
    "flask": "Flask",
    "flask_login": "Flask-Login",
    "mysql.connector": "mysql-connector-python",
    "bcrypt": "bcrypt",
    "cv2": "opencv-python",
    "numpy": "numpy",
    "PIL": "Pillow",
    "pandas": "pandas",
    "requests": "requests",
    "openpyxl": "openpyxl",
    "apscheduler": "APScheduler",
    "dotenv": "python-dotenv",
}

for module, package in packages.items():
    try:
        __import__(module)
        print(f"    ✅ {package}")
    except ImportError:
        errors.append(f"Missing: {package} (pip install {package})")
        print(f"    ❌ {package} — NOT INSTALLED")

# ---- InsightFace ----
print("\n[3] InsightFace:")
try:
    import insightface
    print(f"    ✅ insightface {insightface.__version__}")

    from insightface.app import FaceAnalysis
    print("    ✅ FaceAnalysis available")
except ImportError as e:
    errors.append(f"InsightFace not installed: {e}")
    print(f"    ❌ insightface — {e}")

# ---- ONNX Runtime ----
print("\n[4] ONNX Runtime:")
try:
    import onnxruntime as ort
    providers = ort.get_available_providers()
    print(f"    ✅ onnxruntime (providers: {providers})")
    if "CUDAExecutionProvider" in providers:
        print("    ✅ GPU (CUDA) available!")
    else:
        print("    ⚠️  CPU only (no CUDA). This is fine for small deployments.")
        warnings.append("ONNX Runtime running on CPU only")
except ImportError:
    errors.append("onnxruntime not installed")
    print("    ❌ onnxruntime — NOT INSTALLED")

# ---- Google APIs (optional) ----
print("\n[5] Google APIs (optional):")
try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    print("    ✅ Google API packages installed")
except ImportError:
    warnings.append("Google API packages not installed (optional)")
    print("    ⚠️  Not installed (optional for Google Sheets enrollment)")

# ---- MySQL Connection ----
print("\n[6] MySQL Connection:")
try:
    from dotenv import load_dotenv
    load_dotenv()

    import mysql.connector
    conn = mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASS", "Root@2507"),
        database=os.getenv("DB_NAME", "attendance_system"),
    )
    cursor = conn.cursor(dictionary=True)

    # Check tables
    cursor.execute("SHOW TABLES")
    tables = [row[list(row.keys())[0]] for row in cursor.fetchall()]
    required_tables = [
        "users", "students", "faculty", "faces", "attendance",
        "sessions", "timetable", "sections", "departments",
        "subjects", "batches", "academic_periods", "settings",
        "cameras", "approval_queue", "audit_log", "holiday_calendar",
        "section_subjects", "spoof_attempts"
    ]

    print(f"    ✅ Connected to {os.getenv('DB_NAME', 'attendance_system')}")
    print(f"    Tables found: {len(tables)}")

    missing_tables = [t for t in required_tables if t not in tables]
    if missing_tables:
        errors.append(f"Missing tables: {', '.join(missing_tables)}")
        print(f"    ❌ Missing tables: {', '.join(missing_tables)}")
    else:
        print("    ✅ All required tables present")

    # Check admin user
    cursor.execute("SELECT email, status FROM users WHERE role = 'admin' LIMIT 1")
    admin = cursor.fetchone()
    if admin:
        print(f"    ✅ Admin user: {admin['email']} (status: {admin['status']})")
    else:
        errors.append("No admin user found in database")
        print("    ❌ No admin user found")

    # Check settings
    cursor.execute("SELECT COUNT(*) AS cnt FROM settings")
    settings_count = cursor.fetchone()["cnt"]
    print(f"    ✅ Settings entries: {settings_count}")

    # Check timetable
    cursor.execute("SELECT COUNT(*) AS cnt FROM timetable")
    tt_count = cursor.fetchone()["cnt"]
    print(f"    ✅ Timetable slots: {tt_count}")

    cursor.close()
    conn.close()

except Exception as e:
    errors.append(f"MySQL connection failed: {e}")
    print(f"    ❌ Connection failed: {e}")

# ---- Directory Structure ----
print("\n[7] Directory Structure:")
base = os.path.dirname(os.path.abspath(__file__))
required_dirs = [
    "templates", "templates/auth", "templates/admin",
    "templates/faculty", "templates/student",
    "static", "static/css", "static/js",
    "recognition", "auth", "api", "api/admin",
    "api/faculty", "api/student",
    "dataset", "dataset/images", "dataset/embeddings",
    "anti_spoof_models",
]

for d in required_dirs:
    full_path = os.path.join(base, d)
    if os.path.isdir(full_path):
        print(f"    ✅ {d}/")
    else:
        errors.append(f"Missing directory: {d}/")
        print(f"    ❌ {d}/ — MISSING")

# ---- Key Files ----
print("\n[8] Key Files:")
required_files = [
    "app.py", "config.py", "requirements.txt", ".env",
    "recognition/engine.py", "recognition/liveness.py",
    "recognition/stream_manager.py", "recognition/enrollment.py",
    "recognition/utils.py",
    "auth/routes.py", "auth/helpers.py",
    "api/admin/dashboard.py", "api/admin/academic.py",
    "api/admin/students.py", "api/admin/faculty.py",
    "api/admin/timetable.py", "api/admin/sessions.py",
    "api/admin/cameras.py", "api/admin/recognition_control.py",
    "api/admin/reports.py", "api/admin/settings.py",
    "api/faculty/dashboard.py", "api/faculty/session_view.py",
    "api/faculty/reports.py",
    "api/student/dashboard.py",
    "templates/base.html",
    "templates/auth/login.html", "templates/auth/register.html",
    "templates/admin/dashboard.html",
    "templates/faculty/dashboard.html",
    "templates/student/dashboard.html",
    "static/css/style.css", "static/js/app.js",
]

for f in required_files:
    full_path = os.path.join(base, f)
    if os.path.isfile(full_path):
        size = os.path.getsize(full_path)
        if size > 10:
            print(f"    ✅ {f} ({size} bytes)")
        else:
            warnings.append(f"File might be empty: {f}")
            print(f"    ⚠️  {f} — exists but very small ({size} bytes)")
    else:
        errors.append(f"Missing file: {f}")
        print(f"    ❌ {f} — MISSING")

# ---- Anti-Spoof Models ----
print("\n[9] Anti-Spoof Models:")
model_dir = os.path.join(base, "anti_spoof_models")
model_files = [
    "2.7_80x80_MiniFASNetV2.onnx",
    "4_0_0_80x80_MiniFASNetV1SE.onnx",
]
for mf in model_files:
    mp = os.path.join(model_dir, mf)
    if os.path.isfile(mp):
        size_mb = os.path.getsize(mp) / (1024 * 1024)
        print(f"    ✅ {mf} ({size_mb:.1f} MB)")
    else:
        warnings.append(f"Anti-spoof model not found: {mf}")
        print(f"    ⚠️  {mf} — NOT FOUND (liveness check will be disabled)")

# ---- InsightFace Model Cache ----
print("\n[10] InsightFace Model Cache:")
home = os.path.expanduser("~")
model_cache = os.path.join(home, ".insightface", "models", "buffalo_l")
if os.path.isdir(model_cache):
    files = os.listdir(model_cache)
    print(f"    ✅ buffalo_l cache found ({len(files)} files)")
else:
    warnings.append("InsightFace buffalo_l model not cached yet. Will download on first run.")
    print(f"    ⚠️  buffalo_l not cached at {model_cache}")
    print("       It will auto-download on first model initialization (~300MB)")

# ---- Summary ----
print("\n" + "=" * 60)
if errors:
    print(f"  ❌ ERRORS: {len(errors)}")
    for e in errors:
        print(f"     • {e}")
else:
    print("  ✅ No errors!")

if warnings:
    print(f"\n  ⚠️  WARNINGS: {len(warnings)}")
    for w in warnings:
        print(f"     • {w}")

if not errors:
    print("\n  🚀 System is ready! Run: python run.py")
    print("     Then open: http://localhost:5000")
    print("     Login: admin@college.edu / admin123")
else:
    print("\n  ⚠️  Fix the errors above before running.")

print("=" * 60)