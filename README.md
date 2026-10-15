# Employee Attendance & Analytics API

Read in this order:

1. `PROBLEM_STATEMENT.docx`: the assignment requirements and evaluation criteria.
3. `DATA_MODEL.md`: the MongoDB data model and sample attendance records.
4. `app/main.py`: the implementation of the Employee Attendance & Analytics API.
5. `REVIEW.md`, `DECISIONS.md`: the starter-code review, identified defects, fixes, and design decisions.



## Local setup

```bash
python -m venv .venv
```

Activate the virtual environment and install dependencies:

```bash
# Windows PowerShell
.venv\Scripts\Activate.ps1

# Linux/macOS
source .venv/bin/activate

pip install -r requirements.txt
```

Configure `MONGO_URI` and `MONGO_DB` in your local `.env` file. For example:

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Alternatively, use a MongoDB Atlas connection string. Keep `.env` and all database credentials out of Git.



Start the API:

```bash
uvicorn app.main:app --port 8000 --reload
```

The API runs at `http://localhost:8000`. Interactive API documentation is available at `http://localhost:8000/docs`.

MongoDB **6.0 or newer** is required for the aggregation stages used by the department trend endpoint. MongoDB Atlas is also supported.

## Implementation

The API includes employee management, attendance punch-in/punch-out, attendance regularization with audit history, monthly employee analytics, department summaries, a late-arrival leaderboard, department attendance trends, and query execution-plan endpoints.

MongoDB indexes are created during application startup. See `DECISIONS.md` for the design rationale and `REVIEW.md` for the review of the starter implementation.

## Verification status

The implementation has been reviewed against the supplied API contract. Full integration verification of MongoDB aggregation results and execution plans remains incomplete; these should be tested against a real MongoDB instance before relying on them.


Include openapi.yaml, DATA_MODEL.md, and permitted sample data/supporting files as appropriate.


