import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import pytest
from aiogram.exceptions import TelegramNetworkError, TelegramForbiddenError
from aiogram.methods import SendMessage
from promobot.config import ConfigurationError, Settings
from promobot.dialog import Dialog
from promobot.storage import Store, enqueue, one
from promobot.telegram import Outbox, ProcessLock, Runtime


async def test_backup_restore_preserves_all_tables(app, tmp_path):
    await app.intake()
    await app.worker.tick()
    await app.event(action="menu:new", uid=10003)
    await app.event("Мария", uid=10003)
    await app.db.backup(tmp_path / "backup.sqlite3")
    restored = await Store(tmp_path / "restored.sqlite3").open()
    source = await Store(tmp_path / "backup.sqlite3").open()
    await source.backup(tmp_path / "restored.sqlite3")
    await source.close()
    for table in ("requests", "drafts", "jobs", "outbox", "contact_claims", "inbox", "metadata", "schema_migrations"):
        assert await restored.query(f"SELECT * FROM {table}") == await app.db.query(f"SELECT * FROM {table}")
    assert (await restored.query("PRAGMA integrity_check"))[0]["integrity_check"] == "ok"
    await restored.close()
    await app.db.close()
    app.db = await Store(app.s.database).open()
    assert len(await app.db.query("SELECT * FROM requests")) == 1
    await app.db.close()


async def test_atomic_ingest_disk_failure_rolls_back_offset(app):
    before = await app.db.offset()
    await app.db.execute("CREATE TRIGGER disk_full BEFORE INSERT ON inbox BEGIN SELECT RAISE(ABORT,'disk full simulation'); END")
    with pytest.raises(sqlite3.Error):
        await app.db.ingest("42", [{"update_id": 9999, "message": {"from": {"id": 10002}, "chat": {"id": 10002}}}])
    assert await app.db.offset() == before
    assert not await app.db.query("SELECT * FROM inbox")
    assert not await app.db.query("SELECT * FROM outbox")


async def test_confirmation_disk_failure_no_false_ack_or_partial_request(app):
    await app.intake(confirm=False)
    d = await app.draft()
    await app.db.execute("CREATE TRIGGER full_jobs BEFORE INSERT ON jobs BEGIN SELECT RAISE(ABORT,'disk full simulation'); END")
    u = {"update_id": 999, "callback_query": {"id": "failq", "from": {"id": 10002},
          "message": {"message_id": 20, "chat": {"id": 10002, "type": "private"}},
          "data": f'd:{d["id"]}:{d["version"]}:confirm'}}
    await app.db.ingest("42", [u])
    with pytest.raises(sqlite3.Error):
        await app.dialog.process(999)
    assert not await app.db.query("SELECT * FROM requests")
    assert (await app.draft())["step"] == "review"
    assert (await app.db.query("SELECT state FROM inbox WHERE update_id=999"))[0]["state"] == "pending"
    assert not await app.db.query("SELECT * FROM outbox WHERE dedupe LIKE 'in:42:999%'")


async def test_transaction_ownership_no_coroutine_interleaving(app):
    started, release = asyncio.Event(), asyncio.Event()
    async def writer():
        async with app.db.tx() as c:
            await c.execute("INSERT INTO metadata VALUES('tx-test','uncommitted')")
            started.set()
            await release.wait()
            raise ValueError("rollback")
    async def reader():
        return await app.db.query("SELECT value FROM metadata WHERE key='tx-test'")
    task = asyncio.create_task(writer())
    await started.wait()
    read = asyncio.create_task(reader())
    await asyncio.sleep(.01)
    assert not read.done()
    release.set()
    with pytest.raises(ValueError):
        await task
    assert await read == []


async def test_environment_bot_branch_db_separation(app):
    await app.db.bind("test", "42", app.s.branch)
    for env, bot, branch in (("prod", "42", app.s.branch), ("test", "43", app.s.branch),
                             ("test", "42", {**app.s.branch, "crm_id": 999})):
        with pytest.raises(ValueError):
            await app.db.bind(env, bot, branch)


async def test_real_mode_without_verified_contract_no_http_write(app):
    await app.intake()
    app.s.mode = "real"
    await app.worker.tick()
    assert (await app.db.query("SELECT state FROM requests"))[0]["state"] == "failed"
    assert not await app.mock.store.query("SELECT * FROM mock_models")


async def test_polling_never_advances_after_ingest_failure(app):
    calls = []
    class FakeBot:
        async def get_updates(self, **kwargs):
            calls.append(kwargs["offset"])
            return [SimpleNamespace(model_dump=lambda **kwargs: {"update_id": 99, "message": {"from": {"id": 10002}, "chat": {"id": 10002}}})]
    await app.db.execute("CREATE TRIGGER full_inbox BEFORE INSERT ON inbox BEGIN SELECT RAISE(ABORT,'full'); END")
    rt = Runtime(app.db, app.s, FakeBot(), app.dialog, app.worker)
    with pytest.raises(sqlite3.Error):
        await rt.polling()
    assert calls == [0] and await app.db.offset() == 0


async def test_outbox_restart_network_and_unknown_send(app):
    await app.intake()
    await app.db.execute("UPDATE outbox SET state='done'")
    async with app.db.tx() as c:
        await enqueue(c, "test-send", 10002, "Заявка сохранена")
    class FakeBot:
        sent = []
        async def send_message(self, chat, text, **kwargs):
            self.sent.append(text)
            if len(self.sent) == 1:
                raise TelegramNetworkError(SendMessage(chat_id=chat, text=text), "sensitive token /botABC")
            return SimpleNamespace(message_id=55)
    bot = FakeBot()
    out = Outbox(app.db, bot, app.s)
    await out.tick()
    r = (await app.db.query("SELECT * FROM outbox WHERE dedupe='test-send'"))[0]
    assert r["state"] == "pending" and r["error"] == "telegram_unavailable"
    await app.db.execute("UPDATE outbox SET state='processing' WHERE dedupe='test-send'")
    await app.worker.recover()
    await out.tick()
    r = (await app.db.query("SELECT * FROM outbox WHERE dedupe='test-send'"))[0]
    assert r["state"] == "done" and r["message_id"] == 55 and r["payload"] is None
    assert len(bot.sent) == 2  # unknown send can duplicate notification, never CRM creation
    assert len(await app.db.query("SELECT * FROM requests")) == 1


async def test_outbox_chat_order_and_other_chat_progress(app):
    async with app.db.tx() as c:
        await enqueue(c, "first", 10002, "first")
        await enqueue(c, "second", 10002, "second")
        await enqueue(c, "other", 10003, "other")
        await c.execute("UPDATE outbox SET next_at=9999999999 WHERE dedupe='first'")
    class FakeBot:
        async def send_message(self, chat, text, **kwargs):
            assert text == "other"
            return SimpleNamespace(message_id=22)
    out = Outbox(app.db, FakeBot(), app.s)
    await out.tick()
    assert (await app.db.query("SELECT state FROM outbox WHERE dedupe='second'"))[0]["state"] == "pending"
    assert (await app.db.query("SELECT state FROM outbox WHERE dedupe='other'"))[0]["state"] == "done"


async def test_long_reply_split_and_plain_text(app):
    async with app.db.tx() as c:
        await enqueue(c, "long", 10002, "<" * 9000, [[("OK", "menu:home")]])
    payloads = [json.loads(r["payload"]) for r in await app.db.query("SELECT payload FROM outbox ORDER BY id")]
    assert "".join(p["text"] for p in payloads) == "<" * 9000
    assert all(len(p["text"]) <= 3500 for p in payloads)
    assert payloads[-1]["keyboard"] and not payloads[0]["keyboard"]


def test_process_lock_rejects_second_receiver(tmp_path):
    with ProcessLock(tmp_path / "bot.lock"):
        with pytest.raises(ValueError):
            with ProcessLock(tmp_path / "bot.lock"):
                pass


async def test_network_alert_aggregation_and_recovery(app):
    rt = Runtime(app.db, app.s, None, app.dialog, app.worker)
    await rt.network_state("up")
    await rt.network_state("down")
    await rt.network_state("down")
    await rt.network_state("up")
    found = await app.db.query("SELECT * FROM outbox")
    assert len(found) == 2 and all(r["chat_id"] == app.s.admin for r in found)


async def test_cleanup_keeps_unfinished_events(app):
    u = {"update_id": 99, "message": {"from": {"id": 10002}, "chat": {"id": 10002}, "text": "secret"}}
    await app.db.ingest("42", [u])
    await app.db.execute("UPDATE inbox SET received_at='2020-01-01T00:00:00+00:00'")
    rt = Runtime(app.db, app.s, None, app.dialog, app.worker)
    task = asyncio.create_task(rt.cleanup())
    await asyncio.sleep(.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await app.db.query("SELECT payload FROM inbox"))[0]["payload"]


async def test_real_aiogram_updates_preserve_admin_identity(app):
    from aiogram.types import Update
    from promobot.telegram import Runtime
    from_user={'id':app.s.admin,'is_bot':False,'first_name':'Synthetic','username':'test_admin'}
    message={'message_id':800,'date':1790992800,'from':from_user,
             'chat':{'id':app.s.admin,'type':'private'},'text':'/start'}
    updates=[Update.model_validate({'update_id':800,'message':message}),
             Update.model_validate({'update_id':801,'callback_query':{'id':'test-alias','chat_instance':'synthetic','from':from_user,
                 'message':message,'data':'admin:list'}})]
    class Bot:
        count=0
        async def get_updates(self,**kwargs):
            self.count+=1
            if self.count==1:return updates
            raise asyncio.CancelledError
    runtime=Runtime(app.db,app.s,Bot(),app.dialog,app.worker)
    with pytest.raises(asyncio.CancelledError):await runtime.polling()
    stored=await app.db.query('SELECT user_id FROM inbox ORDER BY update_id')
    assert [x['user_id'] for x in stored]==[app.s.admin,app.s.admin]
    await app.dialog.process(800)
    await app.dialog.process(801)
    responses=await app.db.query("SELECT payload FROM outbox WHERE kind IN ('send','ui') ORDER BY id")
    assert 'Добавить лида' in responses[0]['payload']
    assert 'Админское меню' in responses[0]['payload']
    assert 'Промоутеры' in responses[1]['payload']
    assert 'Доступ не разрешён' not in str(responses)
    users=await app.db.query('SELECT user_id,username FROM telegram_users')
    assert users==[{'user_id':app.s.admin,'username':'test_admin'}]
