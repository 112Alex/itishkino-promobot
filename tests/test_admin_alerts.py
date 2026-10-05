import json
from types import SimpleNamespace
import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage
from promobot.telegram import Outbox,Runtime
from promobot.storage import enqueue


async def test_delivery_failure_fans_out_and_dedupes(app):
    app.s.administrators={10005:'preobrazhenka',10006:'preobrazhenka'}
    await app.intake()
    await app.db.execute('DELETE FROM outbox')
    from promobot.crm import CRMError
    async def unavailable(*args): raise CRMError('network',True)
    app.crm.customers=unavailable
    await app.worker.tick()
    await app.db.execute('UPDATE jobs SET next_at=0')
    await app.worker.tick()
    found=await app.db.query("SELECT chat_id FROM outbox WHERE dedupe LIKE 'alert:%'")
    assert sorted(r['chat_id'] for r in found)==[10001,10005,10006]


async def test_undeliverable_alert_does_not_recursively_create_alerts(app):
    app.s.administrators={10005:'preobrazhenka',10006:'preobrazhenka'}
    class Bot:
        async def send_message(self,*a,**k):
            raise TelegramForbiddenError(method=SendMessage(chat_id=1,text='x'),message='synthetic')
    async with app.db.tx() as c: await enqueue(c,'original',20001,'Тест')
    out=Outbox(app.db,Bot(),app.s)
    await out.tick()
    assert len(await app.db.query('SELECT * FROM outbox'))==4
    for _ in range(5): await out.tick()
    assert len(await app.db.query('SELECT * FROM outbox'))==4
    assert len(await app.db.query("SELECT * FROM outbox WHERE state='held'"))==4


async def test_invalid_event_notifies_every_admin(app,monkeypatch):
    app.s.administrators={10005:'preobrazhenka',10006:'preobrazhenka'}
    await app.db.ingest('42',[{'update_id':88,'message':{'from':{'id':10002},'chat':{'id':10002,'type':'private'},'text':'x'}}])
    async def fail(*args):raise ValueError('synthetic')
    monkeypatch.setattr(app.dialog,'process',fail)
    runtime=Runtime(app.db,app.s,None,app.dialog,app.worker)
    # Break the infinite loop after handling one batch.
    original=app.db.query
    async def query(sql,*args):
        if "FROM inbox WHERE state='pending'" in sql and (await original("SELECT state FROM inbox WHERE update_id=88"))[0]['state']=='failed':
            raise RuntimeError('done')
        return await original(sql,*args)
    monkeypatch.setattr(app.db,'query',query)
    with pytest.raises(RuntimeError,match='done'): await runtime.inbox()
    assert sorted(r['chat_id'] for r in await original('SELECT chat_id FROM outbox'))==[10001,10005,10006]


async def test_service_failure_queues_all_admins_without_secret_or_crash_spam(app, monkeypatch):
    app.s.administrators={10005:'preobrazhenka',10006:'preobrazhenka'}
    async def fail():
        raise RuntimeError('synthetic-secret-must-not-appear')
    monkeypatch.setattr(app.worker,'recover',fail)
    runtime=Runtime(app.db,app.s,None,app.dialog,app.worker)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await runtime.run()
    notices=await app.db.query("SELECT chat_id,payload FROM outbox WHERE dedupe LIKE 'alert:service:%'")
    assert sorted(r['chat_id'] for r in notices)==[10001,10005,10006]
    assert all('synthetic-secret' not in r['payload'] for r in notices)
