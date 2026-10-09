# Employee Attendance & Analytics API

FastAPI and MongoDB implementation of the attendance API contract. Requires Python 3.11+ and MongoDB 6.0+.

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:MONGO_URI = "mongodb+srv://<username>:<password>@<cluster-host>/?appName=<cluster>"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

Use a MongoDB database with the `employees` and `attendance_logs` collections populated according to the API data model. The sample data and assignment statement are intentionally not included in this repository. Startup creates the required indexes; `/health` checks the MongoDB connection.

See [REVIEW.md](REVIEW.md) for the starter-code review and [DECISIONS.md](DECISIONS.md) for implementation choices.
