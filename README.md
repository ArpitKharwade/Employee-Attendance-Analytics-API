# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`. Contract: `openapi.yaml` from the kit.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or a local .env (not committed)
uvicorn app.main:app --port 8000
```
Indexes are created at startup (idempotent); `GET /health` is 200 once MongoDB answers. Needs MongoDB 6.0+
(`$densify`, `$setWindowFields`, `$rank`, `$lookup` with `localField` + `pipeline`).

## Notes for reviewers
* Everything in the brief is implemented: punch-in/out, PATCH regularization with audit history, the three aggregation
  analytics endpoints, the trend endpoint (`$densify` + `$setWindowFields`), and `/admin/explain/{endpoint}`.
* Instants are epoch milliseconds at the API and BSON dates (UTC) in MongoDB; the client uses `tz_aware=True`.
* Rounding is half-up everywhere (`Decimal`), never `round()` / `$round`, which round half to even.
* `moving_avg_7d` averages the *unrounded* daily rates and rounds the result once; `attendance_rate` is rounded on its own.
* `/analytics/.../trend` accepts a range of at most 92 calendar days, counted inclusively.
* Concurrent PATCHes use optimistic concurrency: the loser gets 409 (never a lost history entry); there is no retry.

## What I could not verify
I developed in a sandbox with **no MongoDB server**, so the pipelines and the `explain` plans were **not run against a real
database**. I tested the rules (R1-R5), validation, status codes, and concurrency on the HTTP layer, and I checked the
pipelines' logic with a small in-memory interpreter of the stages I use against a separate pure-Python implementation
of the spec. That proves my logic, not MongoDB's behaviour: please run `/admin/explain/*` and the analytics once on a real
server before relying on them.
