"""
attendance_system/run.py
Simple launcher — use this instead of 'python app.py' for cleaner startup.
"""

import os
import sys

# Ensure we're in the right directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Add to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app

if __name__ == "__main__":
    app = create_app()

    print("\n" + "=" * 60)
    print("  AI-Based Automated Attendance System")
    print("  http://localhost:5000")
    print("=" * 60)
    print("  Admin login: admin@college.edu / admin123")
    print("=" * 60 + "\n")

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=True,
        threaded=True,
        use_reloader=True,
    )