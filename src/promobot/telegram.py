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
from .storage import dumps, enqueue, one
from .access import administrators

log = logging.getLogger("promobot")


def keyboard(value):
    if not value:
        return None
    def label(text, action):
        if text and ord(text[0]) > 0x2000:
            return text
        tag = action.rsplit(':', 1)[-1]
        icons = {'new': '➕', 'continue': '▶️', 'mine': '📋', 'help': '❓', 'home': '🏠', 'back': '⬅️', 'cancel': '❌',
                 'next': '➡️', 'confirm': '📤', 'edit': '✏️', 'phone': '📞', 'username': '💬', 'addchild': '👧',
                 'comment': '📝', 'skip': '⏭️', 'unknown': '❔', 'list': '👥', 'add': '➕'}
        return icons.get(tag, '🔹') + ' ' + text
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label(t, a), callback_data=a) for t, a in row] for row in value])


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
        # Claim atomically, then release the lock before network I/O. Other
        # workers can serve different chats while this chat awaits Telegram.
        async with self.lock:
            async with self.db.tx() as c:
                r = await one(c, """SELECT * FROM outbox WHERE state='pending' AND next_at<=?
                    AND (kind IN ('ack','delete') OR NOT EXISTS (SELECT 1 FROM outbox p WHERE p.chat_id=outbox.chat_id AND p.kind NOT IN ('ack','delete')
                        AND p.id<outbox.id AND p.state IN ('pending','processing'))
                    )
                    ORDER BY id LIMIT 1""", (time.time(),))
                if not r:
                    return False
                await c.execute("UPDATE outbox SET state='processing',attempts=attempts+1 WHERE id=?", (r["id"],))
        started = time.perf_counter()
        outcome = 'operation_failed'
        queue_ms = int((datetime.now(timezone.utc) - datetime.fromisoformat(r['created_at'])).total_seconds() * 1000)
        try:
            p = json.loads(r["payload"])
            if r["kind"] == "ack":
                await self.bot.answer_callback_query(p["text"])
                mid = None
            elif r['kind'] == 'delete':
                try:
                    await self.bot.delete_message(chat_id=r['chat_id'], message_id=int(p['text']))
                except TelegramBadRequest:
                    # Already removed or too old: nothing to retry and no disruption of the menu.
                    pass
                mid = None
            elif r['kind'] == 'ui':
                anchors = await self.db.query('SELECT message_id FROM ui_messages WHERE bot_id=? AND chat_id=?', (p['bot_id'], r['chat_id']))
                mid = anchors[0]['message_id'] if anchors else None
                if mid:
                    try:
                        await self.bot.edit_message_text(p['text'], chat_id=r['chat_id'], message_id=mid,
                                                        reply_markup=keyboard(p['keyboard']), parse_mode=None)
                    except TelegramBadRequest as exc:
                        error = exc.message.lower()
                        if 'message is not modified' in error:
                            pass
                        elif 'message to edit not found' in error or "message can't be edited" in error or 'message identifier is not specified' in error:
                            mid = None
                        else:
                            raise
                if not mid:
                    result = await self.bot.send_message(r['chat_id'], p['text'], reply_markup=keyboard(p['keyboard']), parse_mode=None)
                    mid = result.message_id
                async with self.db.tx() as c:
                    await c.execute('INSERT OR REPLACE INTO ui_messages VALUES(?,?,?)', (p['bot_id'], r['chat_id'], mid))
                    await c.execute("UPDATE outbox SET state='done',message_id=?,error=NULL,payload=NULL WHERE id=?", (mid, r['id']))
                outcome = 'done'
                return True
            else:
                result = await self.bot.send_message(r["chat_id"], p["text"], reply_markup=keyboard(p["keyboard"]), parse_mode=None)
                mid = result.message_id
            await self.db.execute("UPDATE outbox SET state='done',message_id=?,error=NULL,payload=NULL WHERE id=?", (mid, r["id"]))
            outcome = 'done'
        except (TelegramForbiddenError, TelegramBadRequest):
            await self.db.execute("UPDATE outbox SET state='held',error='telegram_rejected' WHERE id=?", (r["id"],))
            await self.alert(r, 'telegram_rejected')
            outcome = 'held'
        except (TelegramAPIError, OSError) as exc:
            delay = exc.retry_after if isinstance(exc, TelegramRetryAfter) else min(self.s.retry_cap, self.s.retry_base * 2 ** min(r["attempts"] + 1, 16))
            await self.db.execute("UPDATE outbox SET state='pending',next_at=?,error='telegram_unavailable' WHERE id=?", (time.time()+delay, r["id"]))
            await self.alert(r, 'telegram_unavailable')
            outcome = 'retry'
        finally:
            log.info(dumps({'operation': 'telegram_outbox', 'outbox_id': r['id'], 'kind': r['kind'],
                            'status': outcome, 'queue_ms': queue_ms,
                            'duration_ms': int((time.perf_counter()-started)*1000)}))
        return True


    async def alert(self, message, code):
        if message['kind'] not in ('send', 'ui') or message['dedupe'].startswith('alert:'):
            return
        async with self.db.tx() as c:
            payload = json.loads(message['payload'] or '{}')
            branch = payload.get('branch_key')
            if message.get('request_id'):
                request = await one(c, 'SELECT branch_key FROM requests WHERE id=?', (message['request_id'],))
                branch = request['branch_key'] if request else branch
            for admin in await administrators(c, self.s, branch):
                await enqueue(c, f'alert:outbox:{message["id"]}:{admin}', admin,
                              f'Не удалось доставить уведомление №{message["id"]}. Код: {code}. Очередь сохранена.')


class Runtime:
    def __init__(self, db, settings, bot, dialog, worker):
        self.db, self.s, self.bot, self.dialog, self.worker = db, settings, bot, dialog, worker
        self.outbox = Outbox(db, bot, settings)
        self.inbox_ready, self.outbox_ready, self.jobs_ready = (asyncio.Event() for _ in range(3))

    async def polling(self):
        delay = 1
        while True:
            try:
                batch = await self.bot.get_updates(offset=await self.db.offset(), timeout=30,
                                                   allowed_updates=["message", "callback_query"], request_timeout=45)
                await self.db.ingest(self.dialog.bot_id, [u.model_dump(mode="json", by_alias=True, exclude_none=True) for u in batch])
                if batch:
                    self.inbox_ready.set()
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
                for admin in await administrators(c, self.s):
                    await enqueue(c, f'alert:network:{state}:{stamp}:{admin}', admin,
                                  "Связь с Telegram восстановлена" if state == "up" else "Связь с Telegram была недоступна. Сохранённая очередь не потеряна.")

        self.outbox_ready.set()

    async def inbox(self):
        while True:
            self.inbox_ready.clear()
            events = await self.db.query("SELECT min(update_id) AS update_id,user_id FROM inbox WHERE state='pending' AND bot_id=? GROUP BY user_id ORDER BY update_id LIMIT 3", (self.dialog.bot_id,))
            if not events:
                await self.wait_ready(self.inbox_ready, 1)
                continue
            async def apply(e):
                try:
                    await self.dialog.process(e["update_id"])
                except sqlite3.Error:
                    raise
                except (ValueError, KeyError, TypeError):
                    async with self.db.tx() as c:
                        await c.execute("UPDATE inbox SET state='failed',error='invalid_event' WHERE bot_id=? AND update_id=?", (self.dialog.bot_id, e["update_id"]))
                        draft = await one(c, 'SELECT branch_key FROM drafts WHERE bot_id=? AND user_id=? AND active=1', (self.dialog.bot_id, e['user_id']))
                        for admin in await administrators(c, self.s, draft['branch_key'] if draft else None):
                            await enqueue(c, f'alert:invalid:{self.dialog.bot_id}:{e["update_id"]}:{admin}', admin,
                                          "Входящее событие требует проверки. Код: invalid_event. Анкета сохранена.")
            # Each user contributes only their oldest event; the next batch follows commit.
            await asyncio.gather(*(apply(e) for e in events))
            self.outbox_ready.set()
            self.jobs_ready.set()

    @staticmethod
    async def wait_ready(event, timeout):
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    async def loop(self, tick, pause, ready):
        while True:
            ready.clear()
            worked = await tick()
            if worked:
                self.outbox_ready.set()
            else:
                await self.wait_ready(ready, pause)

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
                tasks.create_task(self.loop(self.worker.tick, 1, self.jobs_ready))
                for _ in range(3):
                    tasks.create_task(self.loop(self.outbox.tick, 1, self.outbox_ready))
                tasks.create_task(self.cleanup())
        except Exception:
            # Deliver after restart, if SQLite remains writable. Bound crash-loop alerts.
            try:
                async with self.db.tx() as c:
                    for admin in await administrators(c, self.s):
                        await enqueue(c, f'alert:service:{int(time.time() // 3600)}:{admin}', admin,
                                      'Бот завершился с ошибкой service_failed. Проверьте журнал контейнера. Сохранённая очередь будет обработана после запуска.')
            except Exception:
                log.error('service_alert_unavailable')
            raise
