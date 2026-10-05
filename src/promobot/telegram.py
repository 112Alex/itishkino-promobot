import asyncio
import fcntl
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from .domain import now
from .storage import enqueue, one

log = logging.getLogger("promobot")


def keyboard(value):
    if not value:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=a) for t, a in row] for row in value])


def make_bot(settings):
    return Bot(token=settings.token, session=AiohttpSession(proxy=settings.proxy, timeout=settings.timeout))


class ProcessLock:
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.file = os.fdopen(fd, "a")
        os.chmod(self.path, 0o600)
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise ValueError("Другой процесс уже использует этот бот/БД") from None
        return self

    def __exit__(self, *args):
        self.file.close()


class Outbox:
    def __init__(self, store, bot, settings):
        self.db, self.bot, self.s = store, bot, settings
        self.lock = asyncio.Lock()

    async def tick(self):
        async with self.lock:
            async with self.db.tx() as c:
                r = await one(c, "SELECT * FROM outbox WHERE state='pending' AND next_at<=? ORDER BY id LIMIT 1", (time.time(),))
                if not r:
                    return False
                # Preserve responses in order for each chat, even after delayed retries.
                earlier = await one(c, "SELECT id FROM outbox WHERE chat_id=? AND id<? AND state IN ('pending','processing') LIMIT 1", (r["chat_id"], r["id"]))
                if earlier:
                    r = await one(c, "SELECT * FROM outbox WHERE state='pending' AND next_at<=? AND NOT EXISTS (SELECT 1 FROM outbox p WHERE p.chat_id=outbox.chat_id AND p.id<outbox.id AND p.state IN ('pending','processing')) ORDER BY id LIMIT 1", (time.time(),))
                    if not r:
                        return False
                await c.execute("UPDATE outbox SET state='processing',attempts=attempts+1 WHERE id=?", (r["id"],))
            p = json.loads(r["payload"])
            try:
                if r["kind"] == "ack":
                    await self.bot.answer_callback_query(p["text"])
                    mid = None
                else:
                    result = await self.bot.send_message(r["chat_id"], p["text"], reply_markup=keyboard(p["keyboard"]), parse_mode=None)
                    mid = result.message_id
                await self.db.execute("UPDATE outbox SET state='done',message_id=?,error=NULL,payload=NULL WHERE id=?", (mid, r["id"]))
            except (TelegramForbiddenError, TelegramBadRequest):
                await self.db.execute("UPDATE outbox SET state='held',error='telegram_rejected' WHERE id=?", (r["id"],))
                await self.alert(r, 'telegram_rejected')
            except (TelegramAPIError, OSError) as exc:
                delay = exc.retry_after if isinstance(exc, TelegramRetryAfter) else min(self.s.retry_cap, self.s.retry_base * 2 ** min(r["attempts"] + 1, 16))
                await self.db.execute("UPDATE outbox SET state='pending',next_at=?,error='telegram_unavailable' WHERE id=?", (time.time()+delay, r["id"]))
                await self.alert(r, 'telegram_unavailable')
            return True


    async def alert(self, message, code):
        if message['kind'] != 'send' or message['dedupe'].startswith('alert:'):
            return
        async with self.db.tx() as c:
            for admin in self.s.admin_ids:
                await enqueue(c, f'alert:outbox:{message["id"]}:{admin}', admin,
                              f'Не удалось доставить уведомление №{message["id"]}. Код: {code}. Очередь сохранена.')


class Runtime:
    def __init__(self, db, settings, bot, dialog, worker):
        self.db, self.s, self.bot, self.dialog, self.worker = db, settings, bot, dialog, worker
        self.outbox = Outbox(db, bot, settings)

    async def polling(self):
        delay = 1
        while True:
            try:
                batch = await self.bot.get_updates(offset=await self.db.offset(), timeout=30,
                                                   allowed_updates=["message", "callback_query"], request_timeout=45)
                await self.db.ingest(self.dialog.bot_id, [u.model_dump(mode="json", exclude_none=True) for u in batch])
                await self.network_state("up")
                delay = 1
            except (TelegramAPIError, OSError):
                # No logging of raw exceptions, which can contain bot tokens in URLs.
                await self.network_state("down")
                await asyncio.sleep(delay)
                delay = min(60, delay*2)

    async def network_state(self, state):
        async with self.db.tx() as c:
            old = await one(c, "SELECT value FROM metadata WHERE key='telegram_network'")
            if old and old["value"] == state:
                return
            await c.execute("INSERT OR REPLACE INTO metadata VALUES('telegram_network',?)", (state,))
            if state == "down" or old:
                stamp = now()
                for admin in self.s.admin_ids:
                    await enqueue(c, f'alert:network:{state}:{stamp}:{admin}', admin,
                                  "Связь с Telegram восстановлена" if state == "up" else "Связь с Telegram была недоступна. Сохранённая очередь не потеряна.")

    async def inbox(self):
        while True:
            events = await self.db.query("SELECT min(update_id) AS update_id,user_id FROM inbox WHERE state='pending' AND bot_id=? GROUP BY user_id ORDER BY update_id LIMIT 3", (self.dialog.bot_id,))
            if not events:
                await asyncio.sleep(.5)
                continue
            async def apply(e):
                try:
                    await self.dialog.process(e["update_id"])
                except sqlite3.Error:
                    raise
                except (ValueError, KeyError, TypeError):
                    async with self.db.tx() as c:
                        await c.execute("UPDATE inbox SET state='failed',error='invalid_event' WHERE bot_id=? AND update_id=?", (self.dialog.bot_id, e["update_id"]))
                        for admin in self.s.admin_ids:
                            await enqueue(c, f'alert:invalid:{self.dialog.bot_id}:{e["update_id"]}:{admin}', admin,
                                          "Входящее событие требует проверки. Код: invalid_event. Анкета сохранена.")
            # Each user contributes only their oldest event; the next batch follows commit.
            await asyncio.gather(*(apply(e) for e in events))

    async def loop(self, tick, pause):
        while True:
            worked = await tick()
            if not worked:
                await asyncio.sleep(pause)

    async def cleanup(self):
        while True:
            cutoff = (datetime.now(timezone.utc)-timedelta(days=self.s.retention_days)).isoformat()
            await self.db.execute("UPDATE inbox SET payload=NULL WHERE state='done' AND received_at<?", (cutoff,))
            # Keep update keys and request IDs: deleting them would permit old events to replay.
            await asyncio.sleep(3600)

    async def run(self):
        try:
            await self.worker.recover()
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self.polling())
                tasks.create_task(self.inbox())
                tasks.create_task(self.loop(self.worker.tick, .5))
                tasks.create_task(self.loop(self.outbox.tick, .5))
                tasks.create_task(self.cleanup())
        except Exception:
            # Deliver after restart, if SQLite remains writable. Bound crash-loop alerts.
            try:
                async with self.db.tx() as c:
                    for admin in self.s.admin_ids:
                        await enqueue(c, f'alert:service:{int(time.time() // 3600)}:{admin}', admin,
                                      'Бот завершился с ошибкой service_failed. Проверьте журнал контейнера. Сохранённая очередь будет обработана после запуска.')
            except Exception:
                log.error('service_alert_unavailable')
            raise
