# Attendance System

A Flask-based attendance management and face-recognition application for educational institutions. The system supports faculty, HOD, student, and admin workflows, with automated attendance capture, reporting, session tracking, and role-based dashboards.

## Overview

This project is designed for colleges and campuses that want to manage:

- Student registration and profile management
- Faculty and department assignment
- Timetable and academic session handling
- Face-based attendance recognition
- Liveness and anti-spoof checks
- Attendance reports and summaries
- Student and faculty dashboards
- Exportable reports for evaluation and analysis

The application is built using Flask, MySQL, OpenCV, InsightFace, and a range of reporting and authentication libraries.

## Core Features

- Role-based access control for admin, faculty, HOD, and students
- Student enrollment with identity-based matching
- Face recognition attendance scanning
- Liveness/anti-spoofing model support
- Camera and stream management
- Automated academic session scheduling
- Attendance reporting and analytics
- Support for exportable reports and Excel/PDF generation
- Environment-based configuration for local and deployment setups

## Tech Stack

- Python 3.10+
- Flask
- Flask-Login
- MySQL / MariaDB
- OpenCV
- InsightFace
- ONNX Runtime
- APScheduler
- Pandas
- OpenPyXL
- ReportLab
- Google APIs and service account integration
- Docker support via Dockerfile and docker-compose.yml

## Project Structure

```text
attendance-system/
├── app.py
├── config.py
├── run.py
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── nginx.conf
├── .gitignore
├── env.example
├── setup_check.py
├── attendance_system.log
├── api/
│   ├── admin/
│   ├── faculty/
│   ├── hod/
│   └── student/
├── auth/
├── core/
├── dataset/
│   ├── images/
│   └── embeddings/
├── recognition/
│   ├── anti_spoof/
│   └── ...
├── scheduler/
├── services/
├── static/
├── templates/
├── tests/
├── uploads/
├── utils/
├── anti_spoof_models/
└── README.md
```

## Prerequisites

Before running the project, install:

- Python 3.10 or newer
- MySQL Server
- Git
- Optional: Docker and Docker Compose
- Optional: NVIDIA GPU drivers for GPU-based ONNX runtime

## Environment Setup

1. Clone the repository.
2. Create a virtual environment:

```bash
python -m venv venv
source venv/bin/activate
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Copy the sample environment file:

```bash
cp env.example .env
```

5. Update the values in `.env` for your database and application settings.

Example values include:

```env
SECRET_KEY=your-secret-key
DB_HOST=localhost
DB_PORT=3306
DB_USER=attendance
DB_PASS=your-password
DB_NAME=attendance_system
PRIMARY_ID_FIELD=email
```

The application also supports Google Sheets credentials and recognition tuning through environment variables.

## Database Setup

1. Create the MySQL database.
2. Import the schema and seed data if provided by the project SQL file.
3. Verify the DB credentials in `.env` match your server configuration.

The project includes SQL schema files such as:

- `sql_structure_and_data.sql`

## Running the Application

### Local development

```bash
python run.py
```

or:

```bash
flask run
```

The app will start using the Flask application factory and configured routes.

### Docker

```bash
docker-compose up --build
```

You can also run the app using the included Dockerfile if you want a containerized deployment setup.

## Application Roles

The system is organized around user roles such as:

- Admin
- Faculty
- HOD
- Student

Each role has separate dashboard and route modules in the `api/` package.

## Important Notes

- Face recognition and anti-spoofing models can be large; ensure the model directories are available before running training or recognition workloads.
- The project uses local data directories such as `dataset/`, `uploads/`, and `anti_spoof_models/`.
- Some generated files, logs, and media artifacts should not be committed to version control.

## Security and Privacy

This project handles student identity and attendance data. In production, make sure you:

- Keep `.env` files off version control
- Use secure database credentials
- Restrict access to generated attendance reports and uploads
- Use network and host protections for camera and recognition endpoints

## License

This project does not include a license file in the repository yet. If the project is intended for public use, add an appropriate license before distribution.

## Contributing

Contributions are welcome. For a clean workflow:

```bash
git checkout -b feature/your-change
git add .
git commit -m "Add your change"
git push origin feature/your-change
```

## Support

For setup issues, environment variables, model downloads, or database configuration, check the config files, environment examples, and project modules before deployment.
