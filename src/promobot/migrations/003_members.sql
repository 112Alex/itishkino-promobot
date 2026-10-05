CREATE TABLE promoters (
 bot_id TEXT NOT NULL, branch_key TEXT NOT NULL, user_id INTEGER NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
 added_by INTEGER NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(bot_id,branch_key,user_id));
CREATE TABLE telegram_users (
 bot_id TEXT NOT NULL, user_id INTEGER NOT NULL, username TEXT,
 seen_at TEXT NOT NULL, PRIMARY KEY(bot_id,user_id));
CREATE UNIQUE INDEX telegram_username ON telegram_users(bot_id,username) WHERE username IS NOT NULL;
CREATE TABLE promoter_invites (
 id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, branch_key TEXT NOT NULL,
 username TEXT NOT NULL, created_by INTEGER NOT NULL, candidate_id INTEGER,
 state TEXT NOT NULL DEFAULT 'waiting', created_at TEXT NOT NULL);
CREATE UNIQUE INDEX pending_username ON promoter_invites(bot_id,branch_key,username)
 WHERE state IN ('waiting','approval');
CREATE TABLE admin_sessions (
 bot_id TEXT NOT NULL, branch_key TEXT NOT NULL, user_id INTEGER NOT NULL,
 step TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(bot_id,branch_key,user_id));
CREATE TABLE membership_audit (
 id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, branch_key TEXT NOT NULL,
 actor_id INTEGER NOT NULL, target_id INTEGER, action TEXT NOT NULL, created_at TEXT NOT NULL);
