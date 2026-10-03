CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE inbox (
 bot_id TEXT NOT NULL, update_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
 chat_id INTEGER NOT NULL, payload TEXT, received_at TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', error TEXT,
 PRIMARY KEY(bot_id,update_id));
CREATE INDEX inbox_order ON inbox(state,user_id,update_id);
CREATE TABLE drafts (
 id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 branch_key TEXT NOT NULL, crm_branch_id INTEGER, step TEXT NOT NULL, version INTEGER NOT NULL,
 data TEXT NOT NULL, history TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 paused INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 first_message_at TEXT, first_received_at TEXT);
CREATE UNIQUE INDEX active_draft ON drafts(bot_id,user_id,chat_id) WHERE active=1;
CREATE TABLE requests (
 id TEXT PRIMARY KEY, draft_id TEXT NOT NULL UNIQUE REFERENCES drafts(id), bot_id TEXT NOT NULL,
 user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, branch_key TEXT NOT NULL, crm_branch_id INTEGER,
 data TEXT NOT NULL, state TEXT NOT NULL, confirmed_at TEXT NOT NULL, saved_at TEXT NOT NULL,
 verified_at TEXT, crm_id INTEGER, communication_id INTEGER, matches TEXT,
 error TEXT, notice_state TEXT);
CREATE TABLE jobs (
 request_id TEXT PRIMARY KEY REFERENCES requests(id), state TEXT NOT NULL DEFAULT 'pending',
 phase TEXT NOT NULL DEFAULT 'check', attempts INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL DEFAULT 0, last_attempt_at TEXT, error TEXT);
CREATE TABLE contact_claims (
 branch_key TEXT NOT NULL, crm_branch_id INTEGER NOT NULL, contact_key TEXT NOT NULL,
 request_id TEXT NOT NULL REFERENCES requests(id), PRIMARY KEY(branch_key,crm_branch_id,contact_key));
CREATE TABLE outbox (
 id INTEGER PRIMARY KEY, dedupe TEXT NOT NULL UNIQUE, chat_id INTEGER NOT NULL,
 kind TEXT NOT NULL, payload TEXT, state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
 message_id INTEGER, error TEXT, request_id TEXT REFERENCES requests(id), created_at TEXT NOT NULL);
CREATE INDEX outbox_due ON outbox(state,next_at);
