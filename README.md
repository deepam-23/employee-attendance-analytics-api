# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee attendance tracking, manual attendance corrections with an audit trail, and attendance analytics.

## Requirements

* Python 3.11+
* MongoDB 6.0+ (MongoDB Atlas or a local MongoDB instance)

## Setup

1. Clone this repository and open the project directory.

2. Create and activate a virtual environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

4. Configure the following environment variables in your terminal. Use your own MongoDB connection URI; do not commit credentials.

   ```powershell
   $env:MONGO_URI = "YOUR_MONGODB_CONNECTION_URI"
   $env:MONGO_DB = "attendance_db"
   ```

5. Start the API:

   ```powershell
   python -m uvicorn app.main:app --port 8000
   ```

## Verify the API

* Health check: `http://localhost:8000/health`
* Interactive API documentation: `http://localhost:8000/docs`

The health endpoint checks the application's MongoDB connectivity.

## Database Setup

Configure a MongoDB database named `attendance_db` with the collections and indexes expected by the application.

Prepare employee and attendance records according to the supplied data model. If using the sample seed script, review its behavior first because it replaces the `employees` and `attendance_logs` collections with sample data.

## Design Notes

* [Starter-code review](REVIEW.md)
* [Implementation decisions](DECISIONS.md)

## Known Limitations

Document any incomplete endpoints, unimplemented business rules, or analytics that still need work before submission.
