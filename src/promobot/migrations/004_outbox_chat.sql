CREATE INDEX outbox_chat_pending ON outbox(chat_id,id)
WHERE state IN ('pending','processing');
