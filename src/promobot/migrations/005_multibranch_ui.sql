ALTER TABLE promoters ADD COLUMN display_name TEXT;
ALTER TABLE telegram_users ADD COLUMN full_name TEXT;
ALTER TABLE admin_sessions ADD COLUMN target_id INTEGER;
CREATE TABLE administrators (
 bot_id TEXT NOT NULL, user_id INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 display_name TEXT, added_by INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 updated_at TEXT NOT NULL, PRIMARY KEY(bot_id,user_id));
CREATE TABLE admin_watches (
 bot_id TEXT NOT NULL, user_id INTEGER NOT NULL, branches TEXT NOT NULL,
 PRIMARY KEY(bot_id,user_id));
CREATE TABLE user_branches (
 bot_id TEXT NOT NULL, user_id INTEGER NOT NULL, branch_key TEXT NOT NULL,
 PRIMARY KEY(bot_id,user_id));
CREATE TABLE ui_messages (
 bot_id TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
 PRIMARY KEY(bot_id,chat_id));
CREATE TABLE ui_screens (
 bot_id TEXT NOT NULL, chat_id INTEGER NOT NULL, token TEXT NOT NULL,
 pages TEXT NOT NULL, keyboard TEXT, branch_key TEXT NOT NULL,
 PRIMARY KEY(bot_id,chat_id));
