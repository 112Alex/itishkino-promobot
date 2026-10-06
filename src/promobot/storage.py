import asyncio
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
import aiosqlite
from .domain import now


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def one(conn, sql, args=()):
    async with conn.execute(sql, args) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def rows(conn, sql, args=()):
    async with conn.execute(sql, args) as cursor:
        return [dict(r) for r in await cursor.fetchall()]


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = asyncio.Lock()
        self.conn = None

    async def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        self.conn = await aiosqlite.connect(self.path, isolation_level=None)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=FULL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        os.chmod(self.path, 0o600)
        async with self.tx() as c:
            await c.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY)")
        applied = await self.query("SELECT version FROM schema_migrations")
        versions = {r["version"] for r in applied}
        for p in sorted(Path(__file__).with_name("migrations").glob("*.sql")):
            v = int(p.name.split("_")[0])
            if v in versions:
                continue
            if versions:
                await self.backup(self.path.parent / f"before-migration-{v}.sqlite3")
            # executescript manages its own transaction; lock still owns the connection.
            async with self.lock:
                await self.conn.executescript("BEGIN IMMEDIATE;\n" + p.read_text() +
                                              f"\nINSERT INTO schema_migrations VALUES({v});\nCOMMIT;")
        return self

    @asynccontextmanager
    async def tx(self):
        async with self.lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                await self.conn.commit()
            except BaseException:
                await self.conn.rollback()
                raise

    async def query(self, sql, args=()):
        async with self.lock:
            return await rows(self.conn, sql, args)

    async def execute(self, sql, args=()):
        async with self.tx() as c:
            await c.execute(sql, args)

    async def bind(self, environment, bot_id, branch, mode="mock", crm_url="https://itishkino.s20.online", branches=None):
        identity = dumps([environment, bot_id, branch["key"], branch.get("crm_id"), mode, crm_url])
        async with self.tx() as c:
            old = await one(c, "SELECT value FROM metadata WHERE key='identity'")
            if old and old["value"] != identity:
                raise ValueError("Эта БД принадлежит другому окружению/боту/филиалу")
            await c.execute("INSERT OR IGNORE INTO metadata VALUES('identity',?)", (identity,))
            for key, b in (branches or {branch['key']: branch}).items():
                mapping = f'branch_binding:{key}'
                old_mapping = await one(c, 'SELECT value FROM metadata WHERE key=?', (mapping,))
                if old_mapping and int(old_mapping['value']) != b['crm_id']:
                    raise ValueError('CRM ID существующего филиала изменён; проверьте конфигурацию')
                await c.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)', (mapping, str(b['crm_id'])))

    async def ingest(self, bot_id, updates):
        async with self.tx() as c:
            for u in updates:
                # Persist only the fields necessary to replay this dialog event.
                def message_fields(m):
                    return {k: m[k] for k in ("message_id", "date", "text") if k in m} | {
                        "chat": {k: m.get("chat", {})[k] for k in ("id", "type") if k in m.get("chat", {})},
                        "from": {k: m.get("from", {})[k] for k in ("id", "username", "is_bot", "first_name", "last_name") if k in m.get("from", {})}}
                original = u
                u = {"update_id": original["update_id"]}
                if "message" in original:
                    u["message"] = message_fields(original["message"])
                elif "callback_query" in original:
                    q = original["callback_query"]
                    u["callback_query"] = {"id": q["id"], "data": q.get("data", ""),
                        "from": {k: q.get("from", {})[k] for k in ("id", "username", "is_bot", "first_name", "last_name") if k in q.get("from", {})},
                        "message": message_fields(q.get("message", {}))}
                m = u.get("message") or u.get("callback_query", {}).get("message") or {}
                uid = (u.get("callback_query", {}).get("from") or m.get("from") or {}).get("id", 0)
                await c.execute("INSERT OR IGNORE INTO inbox(bot_id,update_id,user_id,chat_id,payload,received_at) VALUES(?,?,?,?,?,?)",
                                (bot_id, u["update_id"], uid, m.get("chat", {}).get("id", 0), dumps(u), now()))
            old = await one(c, "SELECT value FROM metadata WHERE key='offset'")
            offset = max([int(old["value"]) if old else 0] + [u["update_id"] + 1 for u in updates])
            await c.execute("INSERT OR REPLACE INTO metadata VALUES('offset',?)", (str(offset),))
            await c.execute("INSERT OR REPLACE INTO metadata VALUES('last_poll',?)", (now(),))

    async def offset(self):
        r = await self.query("SELECT value FROM metadata WHERE key='offset'")
        return int(r[0]["value"]) if r else 0

    async def backup(self, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        async with self.lock:
            target = await aiosqlite.connect(destination)
            try:
                await self.conn.backup(target)
            finally:
                await target.close()
        os.chmod(destination, 0o600)
        def verify():
            with sqlite3.connect(destination) as c:
                if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup integrity failed")
        await asyncio.to_thread(verify)

    @staticmethod
    async def restore(source, destination):
        source, destination = Path(source).resolve(), Path(destination).resolve()
        if not source.is_file() or source == destination:
            raise ValueError("Недопустимый источник восстановления")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        def copy():
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            try:
                with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
                    if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("Копия повреждена")
                    with sqlite3.connect(destination) as dst:
                        src.backup(dst)
                        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise ValueError("Восстановление не прошло проверку")
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
        await asyncio.to_thread(copy)

    async def close(self):
        if self.conn:
            await self.conn.close()


async def enqueue(c, dedupe, chat, text, keyboard=None, request_id=None, kind="send", bot_id=None, branch_key=None):
    if kind == "send" and len(text) > 3500:
        chunks = [text[i:i+3500] for i in range(0, len(text), 3500)]
        for i, chunk in enumerate(chunks):
            await enqueue(c, f"{dedupe}:part{i}", chat, chunk, keyboard if i == len(chunks)-1 else None, request_id, kind)
        return
    await c.execute("INSERT OR IGNORE INTO outbox(dedupe,chat_id,kind,payload,request_id,created_at) VALUES(?,?,?,?,?,?)",
                    (dedupe, chat, kind, dumps({"text": text, "keyboard": keyboard, "bot_id": bot_id, "branch_key": branch_key}), request_id, now()))
