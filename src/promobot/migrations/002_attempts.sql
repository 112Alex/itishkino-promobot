CREATE TABLE delivery_attempts (
 id INTEGER PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(id),
 branch_key TEXT NOT NULL, crm_branch_id INTEGER,
 attempt INTEGER NOT NULL, phase TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, outcome TEXT, safe_error TEXT);
CREATE INDEX delivery_attempts_request ON delivery_attempts(request_id,id);
