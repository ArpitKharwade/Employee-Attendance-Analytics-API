# DECISIONS.md

1. **Indexes**

   I added indexes to support the API's common queries. In `employees`, a unique index on `emp_code` prevents duplicate employee codes. The `(department, emp_code)` index supports department-wise employee listing, while `(department, joined_on)` helps with queries involving departments and joining dates.

   In `attendance_logs`, a unique `(emp_code, date)` index ensures that an employee has at most one attendance record per day. The `(date DESC, emp_code ASC)` index supports date-based attendance queries. I also added `(emp_code, date, status)`, `(status, date)`, and `(punch_in DESC)` indexes for other attendance filtering and punch-time queries. I did not add an index on `late_minutes` because date-based filtering can narrow the records before the analytics calculations, and an extra index would add storage and write overhead.

2. **Handling concurrent punch-ins**

   Two punch-in requests for the same employee and date could arrive at nearly the same time. Both attempt to insert an attendance record, and the unique `(emp_code, date)` index prevents duplicates at the database level. One request succeeds with HTTP 201, while the other raises `DuplicateKeyError`, which the API handles by returning HTTP 409. This avoids relying on a separate check-before-insert step.

3. **Handling leaderboard ties**

   I use MongoDB's `$rank` to rank employees by their total late minutes before applying the requested limit. After ranking, I keep employees whose rank is less than or equal to the limit. This means employees tied at the cutoff are all included, even if the response contains more records than the requested limit. I use `emp_code` as a secondary sort field to keep the result order consistent.

4. **Calculating headcount**

   The headcount pipeline starts with the `employees` collection and joins attendance records using `$lookup`. It includes employees who joined on or before the end of the reporting month, even if they have no attendance logs. This ensures that headcount is based on eligible employees rather than only those who recorded attendance.

5. **Handling 100x more data**

   If the dataset grows significantly, I would introduce pre-computed daily attendance summaries for each employee, including the department. Analytics could then use these summaries instead of repeatedly scanning and joining raw attendance logs. I would also plan how to update the summaries when attendance records are corrected or an employee changes departments, so that reports remain accurate.
